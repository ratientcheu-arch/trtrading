"""
Test sanity pré-ouverture EU (2026-04-24)

Valide les chemins critiques du système M-only + scoring avant le marché.
Pas de MT5 réel, tout se fait avec des candles synthétiques.

Run:
    docker exec trading-bot python3 /app/tests/test_preopen_sanity.py
"""
import sys
import os

# Bootstrap path si lancé hors container
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.trading.indicators import Candle
from app.trading.scalping_filters import (
    compute_entry_score, SCORE_ENTRY_MIN, EntryScore,
    check_body_ratio, check_volume_spike, check_m1_trigger,
)
from app.trading.structure_detector import detect_m15_structure, detect_regime_m15
from app.trading.range_filters import (
    evaluate_range_entry, is_rangeable_symbol,
    _is_bullish_hammer, _is_bearish_shooting_star,
    _is_bullish_engulfing, _is_bearish_engulfing,
    _is_bullish_pin_bar, _is_bearish_pin_bar,
)
from app.trading.symbol_selector import (
    is_whitelisted, get_current_session, get_session_whitelist,
    is_regime_compatible, compute_quality_score, SESSION_WHITELIST,
)
from app.trading.filters_config import (
    VOLUME_MIN_RATIO, VOLUME_FULL_RATIO, SIZE_REDUCED, SIZE_REDUCED_LOW,
    VOLUME_MIN_RATIO_RANGE, VOLUME_MAX_RATIO_RANGE,
    RANGE_ENTRY_ZONE_PCT, RANGE_SL_PADDING_PCT, RANGE_TP_TARGET_PCT, RANGE_RR_MIN,
    REGIME_ADX_TREND_MIN, REGIME_RANGE_LOOKBACK_M5,
)

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

PASS, FAIL = [], []

def assert_that(name: str, cond: bool, details: str = ""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append((name, details))
        print(f"  ❌ {name}  — {details}")


def make_candle(t, o, h, l, c, v=100):
    return Candle(timestamp=t, open=o, high=h, low=l, close=c, volume=v)


def make_bullish_m15(n=30, start=1.0, step=0.0015):
    """M15 en structure bullish claire (HH + HL)."""
    bars = []
    for i in range(n):
        p = start + i * step
        bars.append(make_candle(i * 900, p, p + step*0.6, p - step*0.3, p + step*0.4, 150))
    return bars


def make_bearish_m15(n=30, start=1.05, step=0.0015):
    bars = []
    for i in range(n):
        p = start - i * step
        bars.append(make_candle(i * 900, p, p + step*0.3, p - step*0.6, p - step*0.4, 150))
    return bars


def make_neutral_m15(n=30, mid=1.0):
    """M15 oscillant autour d'un niveau."""
    bars = []
    for i in range(n):
        p = mid + (0.001 if i % 2 == 0 else -0.001)
        bars.append(make_candle(i * 900, p, p + 0.0005, p - 0.0005, p + 0.0003, 100))
    return bars


# ═══════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════

def test_config_constants():
    print("\n── Constantes config ──")
    assert_that("VOLUME_MIN_RATIO = 0.90", VOLUME_MIN_RATIO == 0.90)
    assert_that("VOLUME_FULL_RATIO = 1.20", VOLUME_FULL_RATIO == 1.20)
    assert_that("SIZE_REDUCED = 0.60", SIZE_REDUCED == 0.60)
    assert_that("SIZE_REDUCED_LOW = 0.40", SIZE_REDUCED_LOW == 0.40)
    assert_that("RANGE volume band [0.30, 0.85]",
                VOLUME_MIN_RATIO_RANGE == 0.30 and VOLUME_MAX_RATIO_RANGE == 0.85)
    assert_that("RANGE entry zone = 15%", RANGE_ENTRY_ZONE_PCT == 0.15)
    assert_that("RANGE SL padding = 15%", RANGE_SL_PADDING_PCT == 0.15)
    assert_that("RANGE TP target = 80%", RANGE_TP_TARGET_PCT == 0.80)
    assert_that("RANGE R:R min = 2.0", RANGE_RR_MIN == 2.0)
    assert_that("REGIME ADX trend = 24", REGIME_ADX_TREND_MIN == 24.0)
    assert_that("REGIME M5 lookback = 40", REGIME_RANGE_LOOKBACK_M5 == 40)
    assert_that("SCORE_ENTRY_MIN = 60", SCORE_ENTRY_MIN == 60)


def test_session_whitelist():
    print("\n── Whitelist par session ──")
    # EU: 9h-15h30
    assert_that("9h CET = EU", get_current_session(10.0) == "EU")
    assert_that("EU whitelist 8 symboles", len(get_session_whitelist(10.0)) == 8)
    assert_that("EUR/GBP whitelisted EU 10h", is_whitelisted("EUR/GBP", cet_hour=10.0))
    assert_that("DAX40 whitelisted EU 10h", is_whitelisted("DAX40", cet_hour=10.0))
    assert_that("SP500 BLOCKED in EU 10h", not is_whitelisted("SP500", cet_hour=10.0))
    assert_that("NKY BLOCKED (Asia) in EU", not is_whitelisted("NKY", cet_hour=10.0))
    # US: 15h30-22h
    assert_that("16h CET = US", get_current_session(16.0) == "US")
    assert_that("SP500 whitelisted US 16h", is_whitelisted("SP500", cet_hour=16.0))
    assert_that("DAX40 whitelisted US 16h (overlap)", is_whitelisted("DAX40", cet_hour=16.0))
    # ASIA: 22h-9h
    assert_that("23h CET = ASIA", get_current_session(23.0) == "ASIA")
    assert_that("ASIA disabled (0 whitelist)", len(get_session_whitelist(23.0)) == 0)


def test_m15_structure():
    print("\n── M15 structure detector ──")
    bull = detect_m15_structure(make_bullish_m15())
    assert_that("Bullish M15 detected", bull.structure == "bullish")
    bear = detect_m15_structure(make_bearish_m15())
    assert_that("Bearish M15 detected", bear.structure == "bearish")
    neut = detect_m15_structure(make_neutral_m15())
    assert_that("Neutral M15 detected", neut.structure == "neutral")
    short = detect_m15_structure([make_candle(0, 1, 1.001, 0.999, 1.0)])
    assert_that("Short data → neutral", short.structure == "neutral")

    # ══ HL / LH explicit tests ══
    # HH + HL confirmed bullish
    bars_hh_hl = []
    # première moitié : highs autour 1.0010, lows autour 0.9990
    for i in range(5):
        bars_hh_hl.append(make_candle(i*900, 1.0000, 1.0010, 0.9990, 1.0005))
    # seconde moitié : highs 1.0025 (HH), lows 1.0005 (HL)
    for i in range(5, 10):
        bars_hh_hl.append(make_candle(i*900, 1.0015, 1.0025, 1.0005, 1.0020))
    r = detect_m15_structure(bars_hh_hl)
    assert_that(
        f"HH+HL explicite → bullish (got {r.structure})",
        r.structure == "bullish",
        f"highs {r.avg_high_first:.5f}→{r.avg_high_second:.5f}, lows {r.avg_low_first:.5f}→{r.avg_low_second:.5f}"
    )

    # LH + LL confirmed bearish
    bars_lh_ll = []
    for i in range(5):
        bars_lh_ll.append(make_candle(i*900, 1.0000, 1.0010, 0.9990, 0.9995))
    for i in range(5, 10):
        bars_lh_ll.append(make_candle(i*900, 0.9985, 0.9995, 0.9975, 0.9980))
    r = detect_m15_structure(bars_lh_ll)
    assert_that(
        f"LH+LL explicite → bearish (got {r.structure})",
        r.structure == "bearish",
        f"highs {r.avg_high_first:.5f}→{r.avg_high_second:.5f}, lows {r.avg_low_first:.5f}→{r.avg_low_second:.5f}"
    )

    # HL seul (lows montent, highs stables) → bullish (accumulation)
    bars_hl_only = []
    for i in range(5):
        bars_hl_only.append(make_candle(i*900, 1.0000, 1.0020, 0.9980, 1.0000))
    for i in range(5, 10):
        # Highs stables (1.0020 ≈ 1.0020), lows montent (0.9980 → 1.0000)
        bars_hl_only.append(make_candle(i*900, 1.0010, 1.0020, 1.0000, 1.0010))
    r = detect_m15_structure(bars_hl_only)
    assert_that(
        f"HL seul (highs stables) → bullish (got {r.structure})",
        r.structure == "bullish",
        f"highs {r.avg_high_first:.5f}→{r.avg_high_second:.5f}, lows {r.avg_low_first:.5f}→{r.avg_low_second:.5f}"
    )

    # LH seul (highs baissent, lows stables) → bearish (distribution)
    bars_lh_only = []
    for i in range(5):
        bars_lh_only.append(make_candle(i*900, 1.0000, 1.0020, 0.9980, 1.0000))
    for i in range(5, 10):
        bars_lh_only.append(make_candle(i*900, 0.9990, 1.0000, 0.9980, 0.9990))
    r = detect_m15_structure(bars_lh_only)
    assert_that(
        f"LH seul (lows stables) → bearish (got {r.structure})",
        r.structure == "bearish",
        f"highs {r.avg_high_first:.5f}→{r.avg_high_second:.5f}, lows {r.avg_low_first:.5f}→{r.avg_low_second:.5f}"
    )

    # HH seul (highs montent, lows stables) → bullish
    bars_hh_only = []
    for i in range(5):
        bars_hh_only.append(make_candle(i*900, 1.0000, 1.0020, 0.9980, 1.0000))
    for i in range(5, 10):
        # Highs montent (1.0020→1.0035), lows stables
        bars_hh_only.append(make_candle(i*900, 1.0010, 1.0035, 0.9980, 1.0020))
    r = detect_m15_structure(bars_hh_only)
    assert_that(
        f"HH seul (lows stables) → bullish (got {r.structure})",
        r.structure == "bullish",
        f"highs {r.avg_high_first:.5f}→{r.avg_high_second:.5f}, lows {r.avg_low_first:.5f}→{r.avg_low_second:.5f}"
    )

    # LL seul (lows cassent, highs stables) → bearish
    bars_ll_only = []
    for i in range(5):
        bars_ll_only.append(make_candle(i*900, 1.0000, 1.0020, 0.9980, 1.0000))
    for i in range(5, 10):
        bars_ll_only.append(make_candle(i*900, 0.9990, 1.0020, 0.9960, 0.9990))
    r = detect_m15_structure(bars_ll_only)
    assert_that(
        f"LL seul (highs stables) → bearish (got {r.structure})",
        r.structure == "bearish",
        f"highs {r.avg_high_first:.5f}→{r.avg_high_second:.5f}, lows {r.avg_low_first:.5f}→{r.avg_low_second:.5f}"
    )

    # EXPANSION HH + LL (highs montent ET lows cassent) → neutral (volatilité)
    bars_expansion = []
    for i in range(5):
        bars_expansion.append(make_candle(i*900, 1.0000, 1.0020, 0.9980, 1.0000))
    for i in range(5, 10):
        # Highs montent, lows cassent
        bars_expansion.append(make_candle(i*900, 1.0010, 1.0040, 0.9960, 1.0000))
    r = detect_m15_structure(bars_expansion)
    assert_that(
        f"HH + LL (expansion) → neutral (got {r.structure})",
        r.structure == "neutral",
    )

    # COMPRESSION LH + HL (highs baissent, lows montent) → neutral
    bars_compression = []
    for i in range(5):
        bars_compression.append(make_candle(i*900, 1.0000, 1.0030, 0.9970, 1.0000))
    for i in range(5, 10):
        bars_compression.append(make_candle(i*900, 1.0000, 1.0015, 0.9985, 1.0000))
    r = detect_m15_structure(bars_compression)
    assert_that(
        f"LH + HL (compression) → neutral (got {r.structure})",
        r.structure == "neutral",
    )

    # Test threshold : variation < 5bps = neutral (bruit)
    bars_noise = []
    for i in range(10):
        # variations de ±2bps seulement, sous le seuil 5bps
        p = 1.0000 + (i * 0.00001)
        bars_noise.append(make_candle(i*900, p, p + 0.0002, p - 0.0002, p))
    r = detect_m15_structure(bars_noise, threshold_pct=0.0005)
    assert_that(
        f"Variation < 5bps → neutral (got {r.structure})",
        r.structure == "neutral",
    )


def test_m15_regime():
    print("\n── Regime M15 detector ──")
    # Strong bull trend → regime trend
    tr = detect_regime_m15(make_bullish_m15(n=30))
    assert_that("Strong bull → regime trend", tr.regime == "trend")
    # Neutral → range
    rg = detect_regime_m15(make_neutral_m15(n=30))
    assert_that("Neutral → regime range (ADX low)", rg.regime in ("range", "none"))


def test_regime_compat():
    print("\n── Regime compatibility ──")
    assert_that("DAX40 + range → OK", is_regime_compatible("DAX40", "range"))
    assert_that("DAX40 + trend → NOT OK", not is_regime_compatible("DAX40", "trend"))
    assert_that("GBP/JPY + trend → OK", is_regime_compatible("GBP/JPY", "trend"))
    assert_that("GBP/USD + both → OK range", is_regime_compatible("GBP/USD", "range"))
    assert_that("GBP/USD + both → OK trend", is_regime_compatible("GBP/USD", "trend"))


def test_score_full():
    print("\n── Score pondéré 0-100 (chemin complet) ──")
    m15 = make_bullish_m15(n=30)
    # M5 : 22 bougies, la -2e complete fortement bullish
    m5 = []
    for i in range(22):
        p = 1.030 + i * 0.0001
        m5.append(make_candle(i * 300, p, p + 0.0002, p - 0.0001, p + 0.0001, 100))
    m5[-2] = make_candle(6300, 1.0320, 1.0330, 1.0318, 1.0329, 150)  # big bull body + high vol
    # M1 : breakout sur 3ème
    m1 = [
        make_candle(1, 1.0329, 1.0330, 1.0326, 1.0327, 30),
        make_candle(2, 1.0327, 1.0328, 1.0325, 1.0327, 25),
        make_candle(3, 1.0327, 1.0335, 1.0326, 1.0334, 40),  # breakout big body
    ]
    es = compute_entry_score("buy", m15, m5, m1)
    assert_that(f"BUY strong setup score ≥ 60 (got {es.total})", es.total >= 60)
    assert_that("ADX score > 0", es.adx_score > 0)
    assert_that("Body score = 20 (strong bull body)", es.body_score == 20)
    assert_that("Vol score ≥ 16", es.volume_score >= 16)
    assert_that("M1 score = 20 (breakout)", es.m1_score == 20)

    # Opposite: same M15 bullish but SELL signal → body contre-dir = 0
    es_wrong = compute_entry_score("sell", m15, m5, m1)
    assert_that(f"SELL on bullish M15 → low score (got {es_wrong.total})",
                es_wrong.total < SCORE_ENTRY_MIN)


def test_m1_patterns():
    print("\n── M1 patterns (hammer/englobante/pin bar) ──")
    # Hammer BUY → accepted
    hammer = [
        make_candle(1, 1.005, 1.0052, 1.0049, 1.0051),
        make_candle(2, 1.0051, 1.0052, 1.0049, 1.0050),
        make_candle(3, 1.0050, 1.0051, 1.0044, 1.00505),  # hammer: long lower wick
    ]
    ok, reason = check_m1_trigger("buy", hammer)
    assert_that(f"Hammer BUY accepted: {reason}", ok)

    # Shooting star SELL → accepted
    ss = [
        make_candle(1, 1.005, 1.0052, 1.0049, 1.0051),
        make_candle(2, 1.0051, 1.0052, 1.0049, 1.0050),
        make_candle(3, 1.0050, 1.0060, 1.0049, 1.00495),  # shooting: long upper wick
    ]
    ok_ss, reason_ss = check_m1_trigger("sell", ss)
    assert_that(f"Shooting-star SELL accepted: {reason_ss}", ok_ss)

    # Shooting star on BUY → BLOCKED (reversal against)
    ok_block, reason_block = check_m1_trigger("buy", ss)
    assert_that(f"Shooting-star on BUY blocked: {reason_block}", not ok_block)


def test_range_pipeline():
    print("\n── Range pipeline (rejet borne + pattern) ──")
    # Box [1.000, 1.020], prix near low 1.0015 avec hammer
    m5 = []
    for i in range(22):
        p = 1.005 + (0.001 if i % 2 == 0 else -0.001)
        m5.append(make_candle(i * 300, p, p + 0.0003, p - 0.0003, p, 100))
    # Force RSI survente : derniers closes en baisse
    for i in range(15, 22):
        m5[i] = make_candle(i * 300, 1.003, 1.0035, 1.0020, 1.0022, 60)
    # Hammer sur -1
    m5[-1] = make_candle(6300, 1.0025, 1.0028, 1.0010, 1.0027, 60)  # hammer bull

    result = evaluate_range_entry(
        signal="buy", price=1.0015,
        candles_m5=m5, range_high=1.020, range_low=1.000,
    )
    print(f"     Range BUY: ok={result.ok} reason={result.reason}")
    # Au moins validate la fonction ne plante pas
    assert_that("Range evaluate_range_entry returns result", hasattr(result, "ok"))


def test_whitelist_smoke():
    print("\n── Symbol selector smoke test ──")
    assert_that("is_rangeable_symbol('EUR/USD')", is_rangeable_symbol("EUR/USD"))
    assert_that("is_rangeable_symbol('DAX40')", is_rangeable_symbol("DAX40"))
    assert_that("is_rangeable_symbol('GOLD') = False (commodity)",
                not is_rangeable_symbol("GOLD"))


def test_quality_score():
    print("\n── Quality score ──")
    m15 = make_bullish_m15(n=30)
    m5 = [make_candle(i * 300, 1.03 + i*0.0001, 1.0305, 1.0295, 1.03, 100) for i in range(22)]
    qs = compute_quality_score("EUR/USD", m15, m5, spread=0.00005, price=1.03)
    assert_that(f"Quality score computed (score={qs.score if qs else None})", qs is not None)


def test_night_backdoors():
    """Vérifie qu'AUCUN pipeline ne peut trader la nuit (22h-8h CET)."""
    print("\n── Garde-fous anti-backdoor nuit ──")
    # 1. Whitelist : à 23h CET aucun symbole n'est whitelisté
    assert_that("23h CET: EUR/USD not whitelisted", not is_whitelisted("EUR/USD", cet_hour=23.0))
    assert_that("23h CET: NKY not whitelisted", not is_whitelisted("NKY", cet_hour=23.0))
    assert_that("02h CET: EUR/GBP not whitelisted", not is_whitelisted("EUR/GBP", cet_hour=2.0))
    assert_that("07h CET: (avant EU open 8h) EUR/USD not whitelisted",
                not is_whitelisted("EUR/USD", cet_hour=7.5))
    assert_that("08h CET: EU open, EUR/USD whitelisted",
                is_whitelisted("EUR/USD", cet_hour=8.0))

    # 2. ASIA_SESSION_ENABLED flag
    from app.trading.filters_config import ASIA_SESSION_ENABLED
    assert_that("ASIA_SESSION_ENABLED = False", ASIA_SESSION_ENABLED is False)

    # 3. TRADING_WINDOW_CET
    from app.trading.signals import TRADING_WINDOW_CET, is_in_global_trading_window
    assert_that(f"TRADING_WINDOW_CET = (8, 22) (got {TRADING_WINDOW_CET})",
                TRADING_WINDOW_CET == (8.0, 22.0))
    assert_that("is_in_global_trading_window(10h) = True",
                is_in_global_trading_window(10.0))
    assert_that("is_in_global_trading_window(23h) = False",
                not is_in_global_trading_window(23.0))
    assert_that("is_in_global_trading_window(3h) = False",
                not is_in_global_trading_window(3.0))
    assert_that("is_in_global_trading_window(22.5h) = False (après close)",
                not is_in_global_trading_window(22.5))

    # 4. Session whitelist pour ASIA = 0 symboles
    assert_that("ASIA session whitelist vide (forex)",
                len(SESSION_WHITELIST["ASIA"]["forex"]) == 0)
    assert_that("ASIA session whitelist vide (indices)",
                len(SESSION_WHITELIST["ASIA"]["indices"]) == 0)

    # 5. Range pipeline : symbole rangeable mais la nuit → whitelist bloque
    # (le range pipeline est inclus dans _scan_markets, gated par whitelist + window)
    assert_that("Range pipeline: EUR/USD rangeable mais bloqué nuit (whitelist)",
                is_rangeable_symbol("EUR/USD") and not is_whitelisted("EUR/USD", cet_hour=23.0))
    assert_that("Range pipeline: DAX40 rangeable mais bloqué nuit",
                is_rangeable_symbol("DAX40") and not is_whitelisted("DAX40", cet_hour=1.0))
    # 6. Aucune fonction de range scan indépendante (range n'a pas son propre scan loop)
    # → vérifier qu'il n'y a que les pipelines gated (_scan_markets + _liquidity_candle_scan)
    import app.trading.bot as _bot_mod
    _scan_fns = [n for n in dir(_bot_mod.TradingBot) if "scan" in n.lower()]
    assert_that(
        f"Pas de _range_scan indépendant (fns: {_scan_fns})",
        not any("range_scan" in n or "range_loop" in n for n in _scan_fns),
    )


# ═══════════════════════════════════════════════════════════════════════
# Run all
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("═" * 70)
    print("  TEST PRE-OPEN SANITY — " + __import__("datetime").datetime.now().isoformat(timespec="seconds"))
    print("═" * 70)

    test_config_constants()
    test_session_whitelist()
    test_m15_structure()
    test_m15_regime()
    test_regime_compat()
    test_score_full()
    test_m1_patterns()
    test_range_pipeline()
    test_whitelist_smoke()
    test_quality_score()
    test_night_backdoors()

    print()
    print("═" * 70)
    print(f"  RÉSULTAT : ✅ {len(PASS)} PASS  |  ❌ {len(FAIL)} FAIL")
    print("═" * 70)
    if FAIL:
        print("\nÉchecs :")
        for n, d in FAIL:
            print(f"  ❌ {n}")
            if d:
                print(f"     → {d}")
        sys.exit(1)
    else:
        print("\n🎯 Tous les tests passent. Système prêt pour l'ouverture EU 8h CET.")
        sys.exit(0)
