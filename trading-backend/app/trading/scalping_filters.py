"""
Scalping filters — 4TF pro system (2026-04-18).

5 filtres obligatoires pour entrée (système pro):
1. D1 direction (macro trend)   — nouveau 2026-04-18
2. H1 MTF (structure)           — price vs SMA50 H1
3. M5 body ≥ 60%                — momentum visible
4. M5 volume ≥ 1.0-1.2× avg     — palier Mode C (size_factor)
5. M1 trigger                    — timing précis (nouveau 2026-04-18)

Les gates hybrides (RSI/MACD/BB/ADX/BBwidth/DI) sont BYPASSÉS — les 5 TF filters
sont la vraie garantie de qualité selon la littérature scalping pro.
"""
from dataclasses import dataclass
from typing import Optional

from app.trading.indicators import Candle, compute_sma, compute_adx
from app.trading.filters_config import (
    VOLUME_MIN_RATIO, VOLUME_FULL_RATIO, SIZE_REDUCED,
    M5_BODY_MIN_RATIO, M1_FULL_BODY_RATIO,
)

# ═══════════════════════════════════════════════════════════════════════
# SCORE PONDÉRÉ 0-100 (user-validated 2026-04-23)
# ═══════════════════════════════════════════════════════════════════════
# Weights:
#   M15 ADX  : 30 (force de la tendance M15)
#   Body M5  : 20 (momentum directionnel bougie courante)
#   Vol M5   : 20 (activité du marché)
#   M1       : 20 (trigger précis d'entrée)
#   Pattern  : 10 (hammer / engulfing / pin bar bonus)
# Total max : 100
# Threshold exécution : SCORE_ENTRY_MIN (60 par défaut)

SCORE_WEIGHT_ADX: int = 30
SCORE_WEIGHT_BODY: int = 20
SCORE_WEIGHT_VOLUME: int = 20
SCORE_WEIGHT_M1: int = 20
SCORE_WEIGHT_PATTERN: int = 10
SCORE_ENTRY_MIN: int = 60   # seuil pour passer à l'exécution (/100)


@dataclass
class EntryScore:
    total: int
    adx_score: int
    body_score: int
    volume_score: int
    m1_score: int
    pattern_score: int
    reasons: list[str]

    def summary(self) -> str:
        return (
            f"SCORE {self.total}/100 "
            f"[ADX {self.adx_score}/{SCORE_WEIGHT_ADX} | "
            f"BODY {self.body_score}/{SCORE_WEIGHT_BODY} | "
            f"VOL {self.volume_score}/{SCORE_WEIGHT_VOLUME} | "
            f"M1 {self.m1_score}/{SCORE_WEIGHT_M1} | "
            f"PATTERN {self.pattern_score}/{SCORE_WEIGHT_PATTERN}] "
            f"{' | '.join(self.reasons)}"
        )


def compute_entry_score(
    signal: str,
    candles_m15: Optional[list[Candle]],
    candles_m5: Optional[list[Candle]],
    candles_m1: Optional[list[Candle]],
) -> EntryScore:
    """Score pondéré 0-100 pour valider une entrée. signal = 'buy' / 'sell'."""
    reasons = []
    adx_sc = body_sc = vol_sc = m1_sc = pat_sc = 0

    # ── 1. M15 ADX (30%) ──
    if candles_m15 and len(candles_m15) >= 29:
        _adx_raw = compute_adx(candles_m15, period=14)
        adx = _adx_raw[0] if isinstance(_adx_raw, tuple) else _adx_raw
        if adx is not None:
            if adx >= 50:      adx_sc = 28   # très fort, possible essoufflement
            elif adx >= 30:    adx_sc = 30   # tendance forte idéale
            elif adx >= 24:    adx_sc = 25
            elif adx >= 20:    adx_sc = 18
            elif adx >= 15:    adx_sc = 10
            else:              adx_sc = 0
            reasons.append(f"ADX_M15 {adx:.1f}→{adx_sc}")
        else:
            reasons.append("ADX_M15 n/a→0")
    else:
        reasons.append("M15 insuff→ADX 0")

    # ── 2. Body M5 (20%) — sur dernière bougie COMPLÈTE (-2) ──
    if candles_m5 and len(candles_m5) >= 2:
        cm5 = candles_m5[-2]
        rng = cm5.high - cm5.low
        if rng > 0:
            body_ratio = abs(cm5.close - cm5.open) / rng
            is_bull_m5 = cm5.close > cm5.open
            dir_match = (signal == "buy" and is_bull_m5) or (signal == "sell" and cm5.close < cm5.open)
            if not dir_match:
                body_sc = 0
                reasons.append(f"body_M5 {body_ratio:.0%} contre-dir→0")
            elif body_ratio >= 0.75:  body_sc = 20
            elif body_ratio >= 0.60:  body_sc = 18
            elif body_ratio >= 0.50:  body_sc = 14
            elif body_ratio >= 0.30:  body_sc = 8
            else:                     body_sc = 0
            reasons.append(f"body_M5 {body_ratio:.0%}→{body_sc}")

    # ── 3. Volume M5 (20%) — ratio vs médiane 20 bars complètes ──
    if candles_m5 and len(candles_m5) >= 22:
        current_vol = candles_m5[-2].volume
        vols = sorted([c.volume for c in candles_m5[-22:-2]])
        n = len(vols)
        median_vol = vols[n // 2] if n % 2 == 1 else (vols[n // 2 - 1] + vols[n // 2]) / 2
        if median_vol > 0:
            ratio = current_vol / median_vol
            if ratio >= 2.0:       vol_sc = 14   # news pic (risqué)
            elif ratio >= 1.2:     vol_sc = 20   # sweet spot trend
            elif ratio >= 0.9:     vol_sc = 16
            elif ratio >= 0.5:     vol_sc = 8
            else:                  vol_sc = 0
            reasons.append(f"vol_M5 {ratio:.2f}×→{vol_sc}")

    # ── 4. M1 trigger (20%) ──
    if candles_m1 and len(candles_m1) >= 3:
        cm1 = candles_m1[-1]
        prev2 = candles_m1[-3:-1]
        max_h = max(p.high for p in prev2)
        min_l = min(p.low for p in prev2)
        rng1 = cm1.high - cm1.low
        body1 = abs(cm1.close - cm1.open) / rng1 if rng1 > 0 else 0
        is_bull1 = cm1.close > cm1.open
        is_bear1 = cm1.close < cm1.open
        if signal == "buy":
            if cm1.close > max_h and is_bull1:       m1_sc = 20  # breakout
            elif is_bull1 and body1 >= 0.70:          m1_sc = 18  # full-body
            elif is_bull1 and body1 >= 0.50:          m1_sc = 12
            elif is_bull1:                             m1_sc = 5
            else:                                      m1_sc = 0
        elif signal == "sell":
            if cm1.close < min_l and is_bear1:       m1_sc = 20
            elif is_bear1 and body1 >= 0.70:          m1_sc = 18
            elif is_bear1 and body1 >= 0.50:          m1_sc = 12
            elif is_bear1:                             m1_sc = 5
            else:                                      m1_sc = 0
        reasons.append(f"M1→{m1_sc}")

    # ── 5. Pattern bonus (10%) sur M5 dernière complète ──
    if candles_m5 and len(candles_m5) >= 3:
        cm5 = candles_m5[-2]
        cm5_prev = candles_m5[-3]
        rng5 = cm5.high - cm5.low
        if rng5 > 0:
            body5 = abs(cm5.close - cm5.open)
            lw = min(cm5.open, cm5.close) - cm5.low
            uw = cm5.high - max(cm5.open, cm5.close)
            _is_bull = cm5.close > cm5.open
            _is_bear = cm5.close < cm5.open
            is_hammer = (body5 / rng5 <= 0.35 and lw >= 2 * body5 and uw <= body5 and _is_bull)
            is_shoot = (body5 / rng5 <= 0.35 and uw >= 2 * body5 and lw <= body5 and _is_bear)
            is_eng_bull = (cm5_prev.close < cm5_prev.open and _is_bull
                           and cm5.open <= cm5_prev.close and cm5.close >= cm5_prev.open)
            is_eng_bear = (cm5_prev.close > cm5_prev.open and _is_bear
                           and cm5.open >= cm5_prev.close and cm5.close <= cm5_prev.open)
            is_pin_bull = (lw / rng5 >= 0.66 and body5 / rng5 <= 0.33 and uw / rng5 <= 0.15)
            is_pin_bear = (uw / rng5 >= 0.66 and body5 / rng5 <= 0.33 and lw / rng5 <= 0.15)
            if signal == "buy":
                if is_pin_bull:     pat_sc = 10
                elif is_hammer:     pat_sc = 8
                elif is_eng_bull:   pat_sc = 7
            elif signal == "sell":
                if is_pin_bear:     pat_sc = 10
                elif is_shoot:      pat_sc = 8
                elif is_eng_bear:   pat_sc = 7
            if pat_sc > 0:
                reasons.append(f"pattern→{pat_sc}")

    total = adx_sc + body_sc + vol_sc + m1_sc + pat_sc
    return EntryScore(
        total=total,
        adx_score=adx_sc,
        body_score=body_sc,
        volume_score=vol_sc,
        m1_score=m1_sc,
        pattern_score=pat_sc,
        reasons=reasons,
    )


@dataclass
class ScalpingFilterResult:
    d1_ok: bool
    d1_reason: str
    mtf_ok: bool
    mtf_reason: str
    body_ok: bool
    body_reason: str
    volume_ok: bool
    volume_reason: str
    m1_ok: bool
    m1_reason: str
    size_factor: float = 1.0  # 2026-04-17 Mode C: 1.0=taille pleine, 0.5=moitié, 0.0=bloqué
    d1_required: bool = False  # True si la paire est dans D1_PAIRS → D1 ajouté au check

    @property
    def all_ok(self) -> bool:
        # 2026-04-23 M-only : H1 MTF et D1 retirés des filtres actifs.
        # Seuls body M5, volume M5, M1 trigger comptent.
        return self.body_ok and self.volume_ok and self.m1_ok

    @property
    def passed_count(self) -> int:
        return sum([self.body_ok, self.volume_ok, self.m1_ok])

    @property
    def total_filters(self) -> int:
        return 3  # body M5 + volume M5 + M1 trigger (H1 et D1 retirés)

    def summary(self) -> str:
        parts = []
        parts.append(("[BODY]" if self.body_ok else "[BODY FAIL]") + " " + self.body_reason)
        parts.append(("[VOL]" if self.volume_ok else "[VOL FAIL]") + " " + self.volume_reason)
        parts.append(("[M1]" if self.m1_ok else "[M1 FAIL]") + " " + self.m1_reason)
        if self.size_factor != 1.0:
            parts.append(f"[SIZE ×{self.size_factor:.1f}]")
        return " | ".join(parts)


# ── Flag D1 filter — 2026-04-20: LISTE EXPLICITE DE PAIRES PERDANTES
# Backtest 13-17 avril: D1 désactivé globalement (Variant B) = +6614€ vs +3570€ avec D1.
# Règle simple: D1 reste OFF par défaut (Variant B) SAUF pour les paires qui perdent
# historiquement sans D1 (contre-tendance macro coûteuse).
#
# Pour ajouter une paire : observer ses pertes cumulées sur 5+ trades sans D1.
# Si net négatif, l'ajouter ici. Pour retirer : reconfirmer avec backtest.
# 2026-04-23 — M-ONLY architecture : D1 et H1 complètement retirés de la décision
# de direction/filtrage. Seuls M15 (tempo), M5 (validation), M1 (trigger) comptent.
# D1_PAIRS vidé → aucune paire n'exige de D1 check. Le set reste pour compat code.
D1_FILTER_ENABLED = False

D1_PAIRS: set[str] = set()


def _is_d1_required(symbol: str | None) -> bool:
    """Returns True only if symbol is in D1_PAIRS (losing pairs needing macro protection)."""
    if not symbol:
        return False
    return symbol in D1_PAIRS or symbol.replace("/", "") in D1_PAIRS


# ── 0. D1 direction (macro trend) — DÉSACTIVÉ 2026-04-20 ─────────────────────
def check_d1_direction(
    signal: str, price: float, candles_d1: Optional[list[Candle]]
) -> tuple[bool, str]:
    """
    Verify signal direction is aligned with D1 macro trend.
    Rule: price vs SMA20 D1 (20 days ≈ 1 month).
    - BUY signal → price >= SMA20 D1 (macro uptrend)
    - SELL signal → price <= SMA20 D1 (macro downtrend)

    2026-04-20: activé seulement pour les paires dans D1_PAIRS (Variant B par défaut).
    """
    if not candles_d1 or len(candles_d1) < 20:
        return False, f"D1 insuffisant ({len(candles_d1) if candles_d1 else 0}/20)"
    closes = [c.close for c in candles_d1]
    sma20_d1 = compute_sma(closes, 20)
    if sma20_d1 is None:
        return False, "SMA20 D1 indisponible"
    if signal == "buy":
        if price >= sma20_d1:
            return True, f"Prix {price:.5f} >= SMA20_D1 {sma20_d1:.5f} (macro bull)"
        return False, f"Prix {price:.5f} < SMA20_D1 {sma20_d1:.5f} (macro bear contre BUY)"
    if signal == "sell":
        if price <= sma20_d1:
            return True, f"Prix {price:.5f} <= SMA20_D1 {sma20_d1:.5f} (macro bear)"
        return False, f"Prix {price:.5f} > SMA20_D1 {sma20_d1:.5f} (macro bull contre SELL)"
    return False, "signal non directionnel"


# ── 1. Multi-timeframe (MTF) alignment ──────────────────────────────────────
def check_mtf_alignment(
    signal: str, price: float, candles_h1: Optional[list[Candle]]
) -> tuple[bool, str]:
    """
    Verify M15 signal direction is aligned with H1 macro trend.
    Rule: price vs SMA50 H1.
    - BUY signal → price must be >= SMA50 H1 (macro uptrend)
    - SELL signal → price must be <= SMA50 H1 (macro downtrend)
    """
    if not candles_h1 or len(candles_h1) < 50:
        # No H1 data → block for safety (scalping requires MTF confirmation)
        return False, f"H1 insuffisant ({len(candles_h1) if candles_h1 else 0}/50)"
    closes = [c.close for c in candles_h1]
    sma50_h1 = compute_sma(closes, 50)
    if sma50_h1 is None:
        return False, "SMA50 H1 indisponible"
    if signal == "buy":
        if price >= sma50_h1:
            return True, f"Prix {price:.5f} >= SMA50_H1 {sma50_h1:.5f} (uptrend macro)"
        return False, f"Prix {price:.5f} < SMA50_H1 {sma50_h1:.5f} (contre-tendance H1)"
    if signal == "sell":
        if price <= sma50_h1:
            return True, f"Prix {price:.5f} <= SMA50_H1 {sma50_h1:.5f} (downtrend macro)"
        return False, f"Prix {price:.5f} > SMA50_H1 {sma50_h1:.5f} (contre-tendance H1)"
    return False, "signal non directionnel"


# ── 2. Candle body ratio (M5 momentum, with N-1 confirmation) ────────────────
def check_body_ratio(
    signal: str, candles_m5: Optional[list[Candle]], min_body_ratio: float = M5_BODY_MIN_RATIO
) -> tuple[bool, str]:
    """
    Verify the last M5 candle has a strong body (>= 60% of range) matching the signal
    AND the previous M5 candle (N-1) is also in the same direction (2026-04-22: ajout N-1).
    Two consecutive candles in direction = momentum confirmé, pas un spike isolé.
    """
    if not candles_m5 or len(candles_m5) < 2:
        return False, f"M5 insuffisant ({len(candles_m5) if candles_m5 else 0}/2)"
    c = candles_m5[-1]
    c_prev = candles_m5[-2]
    rng = c.high - c.low
    if rng <= 0:
        return False, "bougie M5 sans range (prix figé)"
    body = abs(c.close - c.open)
    ratio = body / rng
    is_bull = c.close > c.open
    is_bear = c.close < c.open
    prev_bull = c_prev.close > c_prev.open
    prev_bear = c_prev.close < c_prev.open
    if ratio < min_body_ratio:
        return False, f"corps M5 {ratio:.0%} < {min_body_ratio:.0%} (doji/indécis)"
    if signal == "buy":
        if not is_bull:
            return False, f"corps M5 {ratio:.0%} mais baissier (contre signal BUY)"
        if not prev_bull:
            return False, f"corps M5 {ratio:.0%} OK mais N-1 pas haussier (spike isolé)"
        return True, f"corps M5 {ratio:.0%} haussier + N-1 haussier OK"
    if signal == "sell":
        if not is_bear:
            return False, f"corps M5 {ratio:.0%} mais haussier (contre signal SELL)"
        if not prev_bear:
            return False, f"corps M5 {ratio:.0%} OK mais N-1 pas baissier (spike isolé)"
        return True, f"corps M5 {ratio:.0%} baissier + N-1 baissier OK"
    return False, "signal non directionnel"


# ── 3. Tick volume spike (3 paliers, baseline médiane) ───────────────────────
def check_volume_spike(
    candles_m5: Optional[list[Candle]], lookback: int = 20,
    symbol: Optional[str] = None,
) -> tuple[bool, str, float]:
    """
    3 paliers — seuils dans filters_config.py (VOLUME_MIN_RATIO / VOLUME_FULL_RATIO / SIZE_REDUCED):
    - volume ≥ VOLUME_FULL_RATIO                 → OK, size_factor = 1.0 (taille pleine)
    - VOLUME_MIN_RATIO ≤ vol < VOLUME_FULL_RATIO → OK, size_factor = SIZE_REDUCED
    - volume < VOLUME_MIN_RATIO                  → FAIL (momentum insuffisant)

    Baseline = MÉDIANE (robuste aux outliers : faux ticks, bougies démarrage).
    """
    if not candles_m5 or len(candles_m5) < lookback + 2:
        return False, f"M5 < {lookback + 2} bougies", 0.0
    # 2026-04-23 fix: la dernière bougie est EN COURS (partielle), son tick_volume
    # est inférieur à l'attendu. Utiliser candles[-2] (dernière bougie COMPLÈTE)
    # pour comparer au median. Évite les faux "vol < 0.9" en live.
    current_vol = candles_m5[-2].volume
    vols = [c.volume for c in candles_m5[-(lookback + 2):-2]]
    vols_sorted = sorted(vols)
    n = len(vols_sorted)
    median_vol = (vols_sorted[n // 2] if n % 2 == 1
                  else (vols_sorted[n // 2 - 1] + vols_sorted[n // 2]) / 2)
    if median_vol <= 0:
        return False, "volume médian = 0", 0.0
    ratio = current_vol / median_vol

    if ratio >= VOLUME_FULL_RATIO:
        return True, f"volume M5 {ratio:.2f}× median ≥ {VOLUME_FULL_RATIO:.2f} OK (taille pleine)", 1.0
    if ratio >= VOLUME_MIN_RATIO:
        return (
            True,
            f"volume M5 {ratio:.2f}× median [{VOLUME_MIN_RATIO:.2f}-{VOLUME_FULL_RATIO:.2f}) "
            f"OK (taille {int(SIZE_REDUCED*100)}%)",
            SIZE_REDUCED,
        )
    return False, f"volume M5 {ratio:.2f}× median < {VOLUME_MIN_RATIO:.2f} (momentum insuffisant)", 0.0


# ── 4. M1 trigger (timing precision, 5 chemins valides 2026-04-23) ───────────
def check_m1_trigger(
    signal: str, candles_m1: Optional[list[Candle]]
) -> tuple[bool, str]:
    """
    Vérifie que la dernière M1 confirme l'entrée. 5 chemins valides dans la direction :
      1. BREAKOUT : close > max_high 2 M1 précédentes (BUY) / < min_low_2 (SELL)
      2. FULL BODY : corps ≥ 70% du range dans la direction
      3. HAMMER (BUY) / SHOOTING STAR (SELL) : rejet d'un niveau
      4. ENGULFING : bougie qui avale la précédente, direction favorable
      5. PIN BAR : mèche ≥ 66%, corps ≤ 33%, direction favorable

    Si le pattern est contraire à la direction (ex: shooting star sur BUY) → bloqué.

    2026-04-23 user: les 3 reversal patterns (hammer/engulfing/pin bar) étaient
    bloqués avant car corps < 70% et pas de breakout — donc on ratait les setups
    de continuation classiques en tendance.
    """
    if not candles_m1 or len(candles_m1) < 3:
        return False, f"M1 insuffisant ({len(candles_m1) if candles_m1 else 0}/3)"
    c = candles_m1[-1]
    c_prev = candles_m1[-2]
    prev2 = candles_m1[-3:-1]
    max_high_2 = max(p.high for p in prev2)
    min_low_2 = min(p.low for p in prev2)
    rng = c.high - c.low
    if rng <= 0:
        return False, "M1 sans range (prix figé)"
    body = abs(c.close - c.open)
    body_ratio = body / rng
    upper_wick = c.high - max(c.open, c.close)
    lower_wick = min(c.open, c.close) - c.low
    is_bull = c.close > c.open
    is_bear = c.close < c.open

    # Détection patterns (bougie courante vs précédente)
    is_hammer = (body_ratio <= 0.35 and lower_wick >= 2 * body
                 and upper_wick <= body and is_bull)
    is_shooting_star = (body_ratio <= 0.35 and upper_wick >= 2 * body
                        and lower_wick <= body and is_bear)
    is_engulfing_bull = (c_prev.close < c_prev.open and is_bull
                         and c.open <= c_prev.close and c.close >= c_prev.open)
    is_engulfing_bear = (c_prev.close > c_prev.open and is_bear
                         and c.open >= c_prev.close and c.close <= c_prev.open)
    is_pin_bull = (lower_wick / rng >= 0.66 and body_ratio <= 0.33
                   and upper_wick / rng <= 0.15)
    is_pin_bear = (upper_wick / rng >= 0.66 and body_ratio <= 0.33
                   and lower_wick / rng <= 0.15)

    if signal == "buy":
        # Bloquer si pattern baissier clair (reversal contre nous)
        if is_shooting_star:
            return False, f"M1 shooting-star contre BUY (rejet haut)"
        if is_engulfing_bear:
            return False, f"M1 englobante baissière contre BUY"
        if is_pin_bear:
            return False, f"M1 pin bar baissier contre BUY"
        # Accepter si pattern haussier ou breakout ou full-body
        if c.close > max_high_2 and is_bull:
            return True, f"M1 breakout {c.close:.5f} > max_high_2 {max_high_2:.5f}"
        if is_bull and body_ratio >= M1_FULL_BODY_RATIO:
            return True, f"M1 full-body bull {body_ratio:.0%}"
        if is_hammer:
            return True, f"M1 marteau (rejet bas)"
        if is_engulfing_bull:
            return True, f"M1 englobante haussière"
        if is_pin_bull:
            return True, f"M1 pin bar haussier"
        if not is_bull:
            return False, f"M1 baissier contre BUY (o={c.open:.5f} c={c.close:.5f})"
        return False, f"M1 haussier mais ni breakout ni pattern (body {body_ratio:.0%})"

    if signal == "sell":
        # Bloquer si pattern haussier clair
        if is_hammer:
            return False, f"M1 marteau contre SELL (rejet bas)"
        if is_engulfing_bull:
            return False, f"M1 englobante haussière contre SELL"
        if is_pin_bull:
            return False, f"M1 pin bar haussier contre SELL"
        if c.close < min_low_2 and is_bear:
            return True, f"M1 breakdown {c.close:.5f} < min_low_2 {min_low_2:.5f}"
        if is_bear and body_ratio >= M1_FULL_BODY_RATIO:
            return True, f"M1 full-body bear {body_ratio:.0%}"
        if is_shooting_star:
            return True, f"M1 shooting-star (rejet haut)"
        if is_engulfing_bear:
            return True, f"M1 englobante baissière"
        if is_pin_bear:
            return True, f"M1 pin bar baissier"
        if not is_bear:
            return False, f"M1 haussier contre SELL (o={c.open:.5f} c={c.close:.5f})"
        return False, f"M1 baissier mais ni breakdown ni pattern (body {body_ratio:.0%})"

    return False, "signal non directionnel"


# ── Main entry point ─────────────────────────────────────────────────────────
def evaluate_scalping_filters(
    signal: str,
    price: float,
    candles_m1: Optional[list[Candle]],
    candles_m5: Optional[list[Candle]],
    candles_h1: Optional[list[Candle]],
    candles_d1: Optional[list[Candle]],
    symbol: Optional[str] = None,
) -> ScalpingFilterResult:
    """Run 4 or 5 scalping filters (H1 + M5 body + M5 vol + M1 trigger, + D1 if symbol in D1_PAIRS).
    Returns result with per-filter pass/fail + size_factor (Mode C)."""
    d1_required = _is_d1_required(symbol)
    d1_ok, d1_reason = check_d1_direction(signal, price, candles_d1)
    mtf_ok, mtf_reason = check_mtf_alignment(signal, price, candles_h1)
    body_ok, body_reason = check_body_ratio(signal, candles_m5)
    vol_ok, vol_reason, size_factor = check_volume_spike(candles_m5, symbol=symbol)
    m1_ok, m1_reason = check_m1_trigger(signal, candles_m1)
    return ScalpingFilterResult(
        d1_ok=d1_ok,
        d1_reason=d1_reason,
        mtf_ok=mtf_ok,
        mtf_reason=mtf_reason,
        body_ok=body_ok,
        body_reason=body_reason,
        volume_ok=vol_ok,
        volume_reason=vol_reason,
        m1_ok=m1_ok,
        m1_reason=m1_reason,
        size_factor=size_factor,
        d1_required=d1_required,
    )
