"""
Signal generation via multi-indicator confluence — hybrid scalping strategy.
Based on analysis of 10 Backtrader scripts: per-pair TP/SL, session windows,
and hybrid confirmation system (Stoch + SMA20 + MACD + RSI filter + Price Action).

v3 — Hybrid strategy:
- Per-pair TP/SL in pips (replaces fixed 10 pips min)
- Per-pair trading sessions (CET)
- 3 volatility groups: low (90% threshold), medium (80%), high (4 confirmations + lot -50%)
- RSI hard gates: BUY only 50-70, SELL only 30-50
- Price Action filter: bullish candle for BUY, bearish for SELL
- Stoch + SMA20 + MACD confirmation (2/3 standard, 3/4 high vol)
"""
from dataclasses import dataclass
from typing import Literal, Optional
from app.trading.indicators import TechnicalIndicators, compute_sma


# ── Per-pair configuration ───────────────────────────────────────────────
# PHILOSOPHY: SL must be wide enough to survive noise (spread + 1-2 candle wicks).
# TP = 1.5× SL for positive expectancy — trailing stop extends winners further.
# R:R = 1:1.5 → breakeven at ~40% win rate, profitable above 42%.
# ── Symboles blacklistés (perdants sur 7J — analyse 2026-04-10) ──
# USDJPY 2/10 -35€, EURCHF 1/5 -30€ → garde désactivés
# 2026-04-20: SP500 et NASDAQ RÉACTIVÉS (test avec filtres 4TF Variant B plus stricts)
DISABLED_SYMBOLS: set[str] = {"USDJPY", "EURCHF"}

# ── Fenêtre horaire globale (CET) — Europe + US seulement ──
# 2026-04-23: Asie DÉSACTIVÉE (user : pas de surveillance la nuit). Fenêtre resserrée
# à 8h-22h CET → exclut la session Tokyo (1h-7h30 CET) et Sydney (23h-7h CET).
# Avant : (1.0, 22.0) avec Asie activée.
TRADING_WINDOW_CET: tuple[float, float] = (8.0, 22.0)


PAIR_CONFIG: dict[str, dict] = {
    # ═════════════════════════════════════════════════════════════════════
    # 2026-04-18: 2 profils clairs pour cibler 100€/jour avec capital 1582€
    # - NORMAL:   SL 10 / TP 15 pips  (R:R 1:1.5) — majors + paires calmes
    # - VOLATILE: SL 13 / TP 22 pips  (R:R 1:1.7) — JPY + AUD crosses
    # ═════════════════════════════════════════════════════════════════════

    # ── PROFIL NORMAL (SL 10 / TP 15) — majors + paires calmes ──
    "EURUSD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},
    "GBPUSD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.5},  # historique +22€
    "NZDUSD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.5},  # historique +11€
    "AUDUSD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.5},  # historique +42€
    "USDCAD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},
    "EURCAD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},
    "EURGBP":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},
    "USDCHF":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},
    "AUDCHF":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},  # D1 filter activé pour cette paire (voir D1_PAIRS)
    "AUDNZD":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},
    "EURCHF":  {"tp_pips": 15, "sl_pips": 10, "volatility": "normal", "min_confidence": 80, "lot_factor": 1.0},  # DISABLED

    # ── PROFIL VOLATILE (SL 13 / TP 22) — JPY crosses + AUD crosses ──
    "EURJPY":  {"tp_pips": 22, "sl_pips": 13, "volatility": "volatile", "min_confidence": 80, "lot_factor": 1.5},
    "GBPJPY":  {"tp_pips": 22, "sl_pips": 13, "volatility": "volatile", "min_confidence": 80, "lot_factor": 1.5},
    "AUDJPY":  {"tp_pips": 22, "sl_pips": 13, "volatility": "volatile", "min_confidence": 80, "lot_factor": 1.0},
    "USDJPY":  {"tp_pips": 22, "sl_pips": 13, "volatility": "volatile", "min_confidence": 80, "lot_factor": 1.0},  # DISABLED
    "GBPAUD":  {"tp_pips": 22, "sl_pips": 13, "volatility": "volatile", "min_confidence": 80, "lot_factor": 1.0},
    "EURAUD":  {"tp_pips": 22, "sl_pips": 13, "volatility": "volatile", "min_confidence": 80, "lot_factor": 1.0},
    # ── Indices CFD — SL/TP en % du prix (pas en pips) ──
    "DAX40":  {"sl_pct": 0.0015, "tp_pct": 0.0022, "volatility": "high", "min_confidence": 80, "lot_factor": 1.5},  # +15€
    "SP500":  {"sl_pct": 0.0020, "tp_pct": 0.0030, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},  # DISABLED
    "NASDAQ": {"sl_pct": 0.0020, "tp_pct": 0.0030, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},  # DISABLED
    "CAC40":  {"sl_pct": 0.0030, "tp_pct": 0.0045, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},
    "NKY":    {"sl_pct": 0.0020, "tp_pct": 0.0030, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
    "HK50":   {"sl_pct": 0.0025, "tp_pct": 0.0037, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
    "AUS200": {"sl_pct": 0.0028, "tp_pct": 0.0042, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},
    "UK100":  {"sl_pct": 0.0017, "tp_pct": 0.0026, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},  # 2026-04-21: ajout config UK100
    "US30":   {"sl_pct": 0.0015, "tp_pct": 0.0025, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},  # 2026-04-24: ajout DOW 30

    # ── Commodities (SL/TP en % du prix) ──
    "GOLD":       {"sl_pct": 0.004, "tp_pct": 0.006, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
    "SILVER":     {"sl_pct": 0.006, "tp_pct": 0.009, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
    "OIL_CRUDE":  {"sl_pct": 0.006, "tp_pct": 0.009, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
    "NATURALGAS": {"sl_pct": 0.010, "tp_pct": 0.015, "volatility": "high", "min_confidence": 85, "lot_factor": 0.3},

    # ── Stocks US (SL/TP en % du prix) ──
    "AAPL":  {"sl_pct": 0.005, "tp_pct": 0.010, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},
    "TSLA":  {"sl_pct": 0.008, "tp_pct": 0.015, "volatility": "high", "min_confidence": 85, "lot_factor": 0.5},
    "MSFT":  {"sl_pct": 0.005, "tp_pct": 0.010, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},
    "NVDA":  {"sl_pct": 0.008, "tp_pct": 0.015, "volatility": "high", "min_confidence": 85, "lot_factor": 0.5},
    "AMZN":  {"sl_pct": 0.005, "tp_pct": 0.010, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},
    "META":  {"sl_pct": 0.006, "tp_pct": 0.012, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
    "GOOGL": {"sl_pct": 0.005, "tp_pct": 0.010, "volatility": "medium", "min_confidence": 80, "lot_factor": 1.0},
    "NFLX":  {"sl_pct": 0.006, "tp_pct": 0.012, "volatility": "high", "min_confidence": 80, "lot_factor": 1.0},
}


# ── Per-pair trading session windows (CET hours) ────────────────────────
PAIR_SESSIONS: dict[str, list[tuple[int, int]]] = {
    "EURUSD": [(0, 24)],                    # Forex 24h
    "USDJPY": [(0, 24)],                    # Forex 24h
    "GBPJPY": [(0, 24)],                    # Forex 24h
    "AUDUSD": [(0, 24)],                    # Forex 24h
    "EURGBP": [(0, 24)],                    # Forex 24h
    "NZDUSD": [(0, 24)],                    # Forex 24h
    "EURJPY": [(0, 24)],                    # Forex 24h
    "EURCAD": [(0, 24)],                    # Forex 24h
    "GBPUSD": [(0, 24)],                    # Forex 24h
    "EURCHF": [(0, 24)],                    # Forex 24h
    "EURAUD": [(0, 24)],                    # Forex 24h
    "GBPAUD": [(0, 24)],                    # Forex 24h
    "AUDJPY": [(0, 24)],                    # Forex 24h
    # AUDCAD retiré
    "AUDCHF": [(0, 24)],                    # Forex 24h
    "AUDNZD": [(0, 24)],                    # Forex 24h
    "USDCAD": [(0, 24)],                    # Forex 24h
    "USDCHF": [(0, 24)],                    # Forex 24h
    # ── US Stocks — 15h30-22h CET (14h30-21h GMT) ──
    "AAPL":   [(15.5, 22)],
    "MSFT":   [(15.5, 22)],
    "GOOGL":  [(15.5, 22)],
    "META":   [(15.5, 22)],
    "AMZN":   [(15.5, 22)],
    "NVDA":   [(15.5, 22)],
    "NFLX":   [(15.5, 22)],
    "AMD":    [(15.5, 22)],
    "TSLA":   [(15.5, 22)],
    "INTC":   [(15.5, 22)],
    # ── Indices ──
    "CAC40":  [(9, 17.5)],                  # Euronext Paris
    "DAX40":  [(9, 17.5)],                  # Xetra
    "UK100":  [(9, 17.5)],                  # LSE
    "SP500":  [(15.5, 22)],                 # US session
    "DJ30":   [(15.5, 22)],                 # US session
    "NASDAQ": [(15.5, 22)],                 # US session
    "NKY":    [(1, 7)],                     # Tokyo session
    "EUSTX50": [(9, 17.5)],                 # Euro Stoxx 50
    "HK50":   [(2, 9)],                     # Hong Kong session
    "AUS200": [(1, 7)],                     # Sydney session
    "CHINAH": [(2, 9)],                     # Hong Kong session
    # ── Commodities — main session 8h-22h CET ──
    "XAUUSD": [(1, 23)],                     # Gold — near 24h
    "XAGUSD": [(1, 23)],                     # Silver — near 24h
    "CLF":    [(1, 23)],                      # WTI Crude — near 24h
    "BRENT":  [(1, 23)],                      # Brent Crude — near 24h
    "GOLD":       [(1, 23)],                  # Gold Capital.com — near 24h
    "SILVER":     [(1, 23)],                  # Silver Capital.com — near 24h
    "OIL_CRUDE":  [(1, 23)],                  # WTI Capital.com — near 24h
    "NATURALGAS": [(1, 23)],                  # Gas Capital.com — near 24h
}


# ── Asset-class specific thresholds (kept for indices/stocks/commodities) ─
FOREX_THRESHOLDS = {
    "bb_squeeze": 0.3,
    "momentum_strong": 0.15,
    "momentum_weak": 0.03,
    "fib_tolerance": 0.002,
    "min_tp_spread_ratio": 3.0,
    "sl_multiplier": 1.0,
    "tp_multiplier": 1.5,
    "max_hold_minutes": 60,  # 2026-04-21: 15→60 min (replay: AUD/CHF aurait fini +142€ à +60min vs -38€ fermé à 10min)
    "min_tp_pips": 8,
}

STOCK_THRESHOLDS = {
    "bb_squeeze": 3.0,
    "momentum_strong": 1.5,
    "momentum_weak": 0.4,
    "fib_tolerance": 0.01,
    "min_tp_spread_ratio": 2.0,
    "sl_multiplier": 1.5,
    "tp_multiplier": 3.0,
    "max_hold_minutes": 15,
    "min_tp_pips": 0,
}

INDEX_THRESHOLDS = {
    "bb_squeeze": 1.0,
    "momentum_strong": 0.5,
    "momentum_weak": 0.1,
    "fib_tolerance": 0.005,
    "min_tp_spread_ratio": 2.5,
    "sl_multiplier": 1.5,
    "tp_multiplier": 3.0,
    "max_hold_minutes": 90,  # 2026-04-21: 25→90 min (replay: SP500 aurait fini +19€ à +60min vs -3€ fermé à 10min)
    "min_tp_pips": 0,
}

COMMODITY_THRESHOLDS = {
    "bb_squeeze": 1.5,
    "momentum_strong": 0.8,
    "momentum_weak": 0.2,
    "fib_tolerance": 0.005,
    "min_tp_spread_ratio": 2.5,
    "sl_multiplier": 1.5,
    "tp_multiplier": 3.0,
    "max_hold_minutes": 90,  # 2026-04-21: 25→90 min (aligné avec indices)
    "min_tp_pips": 0,
}


FOREX_PAIRS = {"EURUSD","GBPUSD","USDJPY","AUDUSD","NZDUSD","EURJPY","GBPJPY","EURGBP","EURCAD","EURCHF","EURAUD","GBPAUD","AUDJPY","AUDCHF","AUDNZD","USDCAD","USDCHF"}
INDEX_NAMES = {"CAC40","DAX40","SP500","NKY","NASDAQ","FRA40","GER40","US500","JPN225","NAS100","DJ30","UK100","EUSTX50","HK50","AUS200","CHINAH","DJI","US30","FTSE100"}
COMMODITY_NAMES = {"XAUUSD","XAGUSD","CLF","BRENT","CL=F","XBRUSD","XTIUSD","GOLD","SILVER","OIL_CRUDE"}


def _get_thresholds(symbol: str) -> dict:
    """Return asset-class appropriate thresholds."""
    sym = symbol.upper().replace("/", "")
    if sym in FOREX_PAIRS:
        return FOREX_THRESHOLDS
    if sym in INDEX_NAMES:
        return INDEX_THRESHOLDS
    if "XAU" in sym or "XAG" in sym or "CL" in sym or "OIL" in sym:
        return COMMODITY_THRESHOLDS
    return STOCK_THRESHOLDS


def _normalize_symbol(symbol: str) -> str:
    """Normalize symbol to match PAIR_CONFIG keys (e.g. EUR/USD -> EURUSD)."""
    return symbol.upper().replace("/", "").replace("_", "").replace("-", "")


# ── Signal dataclass ─────────────────────────────────────────────────────

@dataclass
class Signal:
    signal: Literal["buy", "sell", "hold"]
    confidence: float
    reason: str
    suggested_entry: float
    suggested_sl: float
    suggested_tp: float
    bull_score: int = 0
    bear_score: int = 0
    spread_ok: bool = True
    lot_factor: float = 1.0


# ── Helper functions ─────────────────────────────────────────────────────

def get_pair_config(symbol: str) -> dict:
    """Return the per-pair config dict (TP/SL pips, volatility, etc.)."""
    sym = _normalize_symbol(symbol)
    return PAIR_CONFIG.get(sym, {})


def is_symbol_disabled(symbol: str) -> bool:
    """True si le symbole est blacklisté (voir DISABLED_SYMBOLS)."""
    return _normalize_symbol(symbol) in DISABLED_SYMBOLS


def is_in_global_trading_window(cet_hour: float) -> bool:
    """True si l'heure CET est dans la fenêtre globale TRADING_WINDOW_CET."""
    start, end = TRADING_WINDOW_CET
    return start <= cet_hour < end


def is_in_trading_session(symbol: str, cet_hour: float) -> bool:
    """Check if the current CET hour (decimal, e.g. 14.5 = 14h30) falls within trading windows."""
    sym = _normalize_symbol(symbol)
    sessions = PAIR_SESSIONS.get(sym)
    if sessions is None:
        return True  # Unknown pair — allow trading by default
    for start, end in sessions:
        if start <= cet_hour < end:
            return True
    return False


def is_asian_session(cet_hour: float) -> bool:
    """Check if we are in the Asian low-volatility window (00h-09h CET)."""
    return 0 <= cet_hour < 9


# ── Hybrid confirmation filter ───────────────────────────────────────────
# Strategie PULLBACK EN TENDANCE :
#   4 conditions OBLIGATOIRES (gate) — si une seule manque, pas de trade
#   3 conditions BONUS (confirmation) — au moins 1 requise
#
# OBLIGATOIRES:
#   1. RSI dans la zone (BUY 50-70, SELL 27-47)
#   2. MACD croissant/decroissant (meme diff = 1)
#   3. BB position 30-70% (pullback, pas d'extremes)
#   4. ADX > 20 (anti-range)
#
# BONUS (1 sur 3 minimum):
#   5. SMA alignment (Prix>SMA20>SMA50 pour BUY, inverse pour SELL)
#   6. Stoch cross (K>D D[20-50] pour BUY, K<D D[50-80] pour SELL)
#   7. Bougie dans le sens du trade

def _hybrid_confirmation(
    direction: Literal["buy", "sell"],
    indicators: TechnicalIndicators,
    price: float,
    prev_close: Optional[float],
    volatility: str,
    asian_mode: bool = False,
    symbol: str = "",
) -> tuple[bool, list[str]]:
    filter_reasons: list[str] = []
    mandatory_ok = 0
    mandatory_total = 6  # RSI + MACD + BB_pos + ADX>25 + BB_squeeze + DI_direction
    bonus_ok = 0

    if direction == "buy":
        # ── OBLIGATOIRE 1: RSI 50-75 (momentum haussier UNIQUEMENT, pas de rebond) ──
        # Durci 2026-04-15: RSI < 50 = rebond depuis survente, RSI > 75 = sommet
        if indicators.rsi14 is not None and 50 <= indicators.rsi14 <= 75:
            mandatory_ok += 1
            filter_reasons.append(f"[GATE] RSI {indicators.rsi14:.0f} zone BUY [50-75] OK (momentum)")
        else:
            _v = f"{indicators.rsi14:.0f}" if indicators.rsi14 is not None else "N/A"
            filter_reasons.append(f"[GATE FAIL] RSI {_v} hors zone BUY [50-75] (rebond ou sommet)")

        # ── OBLIGATOIRE 2: MACD haussier — 2/3 conditions parmi: hist>0, macd>signal, croissant ──
        # Assoupli 2026-04-16: 3/3 bloquait 100% des signaux (4020 scans 5/6, 0 trade)
        # 2/3 garde la sélectivité tout en autorisant un léger ralentissement du momentum
        if indicators.macd and indicators.macd_hist_prev is not None:
            _h_ok = indicators.macd.histogram > 0
            _cross_ok = indicators.macd.macd > indicators.macd.signal
            _growing = indicators.macd.histogram > indicators.macd_hist_prev
            _macd_score = int(_h_ok) + int(_cross_ok) + int(_growing)
            if _macd_score >= 2:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] MACD haussier {_macd_score}/3 (hist>0={_h_ok} macd>signal={_cross_ok} croissant={_growing})")
            else:
                filter_reasons.append(f"[GATE FAIL] MACD insuffisant BUY {_macd_score}/3 (hist>0={_h_ok} macd>signal={_cross_ok} croissant={_growing})")
        else:
            filter_reasons.append("[GATE FAIL] MACD indisponible")

        # ── OBLIGATOIRE 3: BB position 40-85% (pas de rebond depuis le bas, pas de sommet) ──
        # Durci 2026-04-15: BB<40% = rebond, BB>85% = sommet/extended
        if indicators.bollinger_bands:
            _bb = indicators.bollinger_bands
            _bb_range = _bb.upper - _bb.lower
            _bb_pos = (price - _bb.lower) / _bb_range if _bb_range > 0 else 0.5
            if 0.40 <= _bb_pos <= 0.85:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] BB {_bb_pos:.0%} zone BUY [40-85%] OK")
            else:
                _tag = "rebond" if _bb_pos < 0.40 else "sommet"
                filter_reasons.append(f"[GATE FAIL] BB {_bb_pos:.0%} hors zone [40-85%] ({_tag})")
        else:
            filter_reasons.append("[GATE FAIL] BB indisponibles")

        # ── OBLIGATOIRE 4: ADX > 28 (tendance confirmée, pas zone grise 25-28) ──
        # Durci 2026-04-15: ADX 25-28 = zone incertaine range/trend
        if indicators.adx is not None and indicators.adx > 28:
            mandatory_ok += 1
            filter_reasons.append(f"[GATE] ADX={indicators.adx:.0f}>28 OK (tendance confirmée)")
        else:
            _v = f"{indicators.adx:.0f}" if indicators.adx is not None else "N/A"
            filter_reasons.append(f"[GATE FAIL] ADX={_v} <= 28 (range ou tendance faible)")

        # ── OBLIGATOIRE 5: Pas de BB squeeze OU ADX>32 (breakout fort en cours) ──
        # Durci 2026-04-15: seuils width relevés + ADX breakout 30→32
        if indicators.bollinger_bands:
            _bb_w = indicators.bollinger_bands.width if hasattr(indicators.bollinger_bands, 'width') else 0
            _bb_sq_th = 0.7 if "/" in symbol else 1.5  # forex=0.7%, indices=1.5% (relevé)
            _adx_breakout = indicators.adx is not None and indicators.adx > 32
            if _bb_w >= _bb_sq_th:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] BB width={_bb_w:.2f}%>={_bb_sq_th} OK (pas de squeeze)")
            elif _adx_breakout:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] BB squeeze={_bb_w:.2f}% MAIS ADX={indicators.adx:.0f}>32 = BREAKOUT fort autorisé")
            else:
                filter_reasons.append(f"[GATE FAIL] BB squeeze={_bb_w:.2f}%<{_bb_sq_th} + ADX<32 (RANGE)")
        else:
            mandatory_ok += 1
            filter_reasons.append("[GATE] BB indisponibles — squeeze non vérifiable")

        # ── OBLIGATOIRE 6: +DI/-DI confirme la direction (BUY = +DI > -DI) ──
        if indicators.plus_di is not None and indicators.minus_di is not None:
            if indicators.plus_di > indicators.minus_di:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] +DI={indicators.plus_di:.0f}>-DI={indicators.minus_di:.0f} direction BUY OK")
            else:
                filter_reasons.append(f"[GATE FAIL] +DI={indicators.plus_di:.0f}<=-DI={indicators.minus_di:.0f} (direction SELL)")
        else:
            mandatory_ok += 1
            filter_reasons.append("[GATE] DI indisponible")

        # ── BONUS 5: SMA alignment ──
        if indicators.sma20 is not None and indicators.sma50 is not None:
            if price > indicators.sma20 and indicators.sma20 > indicators.sma50:
                bonus_ok += 1
                filter_reasons.append("[BONUS] Prix>SMA20>SMA50 OK")
            elif price > indicators.sma20:
                bonus_ok += 1
                filter_reasons.append("[BONUS] Prix>SMA20 OK")
            else:
                filter_reasons.append("[BONUS FAIL] SMA pas aligne BUY")
        # ── BONUS 6: Stoch favorable ──
        if indicators.stochastic:
            if indicators.stochastic.k > indicators.stochastic.d or indicators.stochastic.k < 70:
                bonus_ok += 1
                filter_reasons.append(f"[BONUS] Stoch K={indicators.stochastic.k:.0f} D={indicators.stochastic.d:.0f} OK")
            else:
                filter_reasons.append(f"[BONUS FAIL] Stoch K={indicators.stochastic.k:.0f} D={indicators.stochastic.d:.0f}")
        # ── BONUS 7: Bougie haussiere ──
        if prev_close is not None and price > prev_close:
            bonus_ok += 1
            filter_reasons.append("[BONUS] Bougie haussiere OK")
        else:
            filter_reasons.append("[BONUS FAIL] Pas de bougie haussiere")

    else:  # SELL
        # ── OBLIGATOIRE 1: RSI 25-50 (momentum baissier UNIQUEMENT, pas de rebond) ──
        # Durci 2026-04-15: RSI > 50 = rebond depuis surachat, RSI < 25 = plancher
        if indicators.rsi14 is not None and 25 <= indicators.rsi14 <= 50:
            mandatory_ok += 1
            filter_reasons.append(f"[GATE] RSI {indicators.rsi14:.0f} zone SELL [25-50] OK (momentum)")
        else:
            _v = f"{indicators.rsi14:.0f}" if indicators.rsi14 is not None else "N/A"
            filter_reasons.append(f"[GATE FAIL] RSI {_v} hors zone SELL [25-50] (rebond ou plancher)")

        # ── OBLIGATOIRE 2: MACD baissier — 2/3 conditions parmi: hist<0, macd<signal, décroissant ──
        # Assoupli 2026-04-16: 2/3 au lieu de 3/3 (même logique que BUY)
        if indicators.macd and indicators.macd_hist_prev is not None:
            _h_ok = indicators.macd.histogram < 0
            _cross_ok = indicators.macd.macd < indicators.macd.signal
            _falling = indicators.macd.histogram < indicators.macd_hist_prev
            _macd_score = int(_h_ok) + int(_cross_ok) + int(_falling)
            if _macd_score >= 2:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] MACD baissier {_macd_score}/3 (hist<0={_h_ok} macd<signal={_cross_ok} décroissant={_falling})")
            else:
                filter_reasons.append(f"[GATE FAIL] MACD insuffisant SELL {_macd_score}/3 (hist<0={_h_ok} macd<signal={_cross_ok} décroissant={_falling})")
        else:
            filter_reasons.append("[GATE FAIL] MACD indisponible")

        # ── OBLIGATOIRE 3: BB position 15-60% (pas de rebond depuis le haut, pas de plancher) ──
        # Durci 2026-04-15: BB>60% = rebond, BB<15% = plancher/extended
        if indicators.bollinger_bands:
            _bb = indicators.bollinger_bands
            _bb_range = _bb.upper - _bb.lower
            _bb_pos = (price - _bb.lower) / _bb_range if _bb_range > 0 else 0.5
            if 0.15 <= _bb_pos <= 0.60:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] BB {_bb_pos:.0%} zone SELL [15-60%] OK")
            else:
                _tag = "rebond" if _bb_pos > 0.60 else "plancher"
                filter_reasons.append(f"[GATE FAIL] BB {_bb_pos:.0%} hors zone [15-60%] ({_tag})")
        else:
            filter_reasons.append("[GATE FAIL] BB indisponibles")

        # ── OBLIGATOIRE 4: ADX > 28 (tendance confirmée, pas zone grise 25-28) ──
        if indicators.adx is not None and indicators.adx > 28:
            mandatory_ok += 1
            filter_reasons.append(f"[GATE] ADX={indicators.adx:.0f}>28 OK (tendance confirmée)")
        else:
            _v = f"{indicators.adx:.0f}" if indicators.adx is not None else "N/A"
            filter_reasons.append(f"[GATE FAIL] ADX={_v} <= 28 (range ou tendance faible)")

        # ── OBLIGATOIRE 5: Pas de BB squeeze OU ADX>32 (breakout fort en cours) ──
        if indicators.bollinger_bands:
            _bb_w = indicators.bollinger_bands.width if hasattr(indicators.bollinger_bands, 'width') else 0
            _bb_sq_th = 0.7 if "/" in symbol else 1.5  # forex=0.7%, indices=1.5% (relevé)
            _adx_breakout = indicators.adx is not None and indicators.adx > 32
            if _bb_w >= _bb_sq_th:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] BB width={_bb_w:.2f}%>={_bb_sq_th} OK (pas de squeeze)")
            elif _adx_breakout:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] BB squeeze={_bb_w:.2f}% MAIS ADX={indicators.adx:.0f}>32 = BREAKOUT fort autorisé")
            else:
                filter_reasons.append(f"[GATE FAIL] BB squeeze={_bb_w:.2f}%<{_bb_sq_th} + ADX<32 (RANGE)")
        else:
            mandatory_ok += 1
            filter_reasons.append("[GATE] BB indisponibles — squeeze non vérifiable")

        # ── OBLIGATOIRE 6: -DI/+DI confirme la direction (SELL = -DI > +DI) ──
        if indicators.plus_di is not None and indicators.minus_di is not None:
            if indicators.minus_di > indicators.plus_di:
                mandatory_ok += 1
                filter_reasons.append(f"[GATE] -DI={indicators.minus_di:.0f}>+DI={indicators.plus_di:.0f} direction SELL OK")
            else:
                filter_reasons.append(f"[GATE FAIL] -DI={indicators.minus_di:.0f}<=+DI={indicators.plus_di:.0f} (direction BUY)")
        else:
            mandatory_ok += 1
            filter_reasons.append("[GATE] DI indisponible")

        # ── BONUS 5: SMA alignment ──
        if indicators.sma20 is not None and indicators.sma50 is not None:
            if price < indicators.sma20 and indicators.sma20 < indicators.sma50:
                bonus_ok += 1
                filter_reasons.append("[BONUS] Prix<SMA20<SMA50 OK")
            elif price < indicators.sma20:
                bonus_ok += 1
                filter_reasons.append("[BONUS] Prix<SMA20 OK")
            else:
                filter_reasons.append("[BONUS FAIL] SMA pas aligne SELL")
        # ── BONUS 6: Stoch favorable ──
        if indicators.stochastic:
            if indicators.stochastic.k < indicators.stochastic.d or indicators.stochastic.k > 30:
                bonus_ok += 1
                filter_reasons.append(f"[BONUS] Stoch K={indicators.stochastic.k:.0f} D={indicators.stochastic.d:.0f} OK")
            else:
                filter_reasons.append(f"[BONUS FAIL] Stoch K={indicators.stochastic.k:.0f} D={indicators.stochastic.d:.0f}")
        # ── BONUS 7: Bougie baissiere ──
        if prev_close is not None and price < prev_close:
            bonus_ok += 1
            filter_reasons.append("[BONUS] Bougie baissiere OK")
        else:
            filter_reasons.append("[BONUS FAIL] Pas de bougie baissiere")

    # ── VERDICT 2026-04-18: BYPASS gates hybrides — système 4TF pro ────
    # Les gates hybrides (RSI/MACD/BB/ADX/BBwidth/DI) sont des indicateurs LAGGING.
    # En scalping pro on ne les utilise pas — on lit price action + volume sur 4TF.
    # Les 5 filtres scalping 4TF (D1 + H1 + Body M5 + Vol M5 + M1) dans bot._execute_trade
    # sont la vraie garantie de qualité.
    # Ici on détecte juste la direction large (bull/bear) pour orienter le signal.
    MANDATORY_MIN = 0  # BYPASS — vraie validation en aval par 4TF filters
    all_mandatory = mandatory_ok >= MANDATORY_MIN
    has_bonus = True  # BYPASS — bonus pas bloquant en 4TF pro
    passed = all_mandatory and has_bonus

    filter_reasons.append(
        f"Gate hybrides: {mandatory_ok}/{mandatory_total} BYPASS (4TF pro — validation en aval) | Bonus: {bonus_ok}/3"
    )
    # Debug: log full gate/bonus status if blocked
    if not passed:
        import logging
        _gate_log = [r for r in filter_reasons if "[GATE" in r or "[BONUS" in r]
        logging.getLogger("app.trading.signals").warning(
            f"[HYBRID FILTER] {direction.upper()} {symbol}: "
            f"mandatory={mandatory_ok}/{mandatory_total} bonus={bonus_ok}/3 | "
            f"{' | '.join(_gate_log)}"
        )
    return passed, filter_reasons


# ── Main signal generation ───────────────────────────────────────────────

def generate_signal(
    price: float,
    indicators: TechnicalIndicators,
    change_percent: float,
    symbol: str = "",
    spread: float = 0.0,
    prev_close: Optional[float] = None,
    candles_h1: Optional[list] = None,  # 2026-04-21: Option A — direction = H1 macro
) -> Signal:
    bull_score = 0
    bear_score = 0
    reasons: list[str] = []

    sym_upper = _normalize_symbol(symbol)
    pair_cfg = get_pair_config(symbol)
    volatility = pair_cfg.get("volatility", "medium")
    thresholds = _get_thresholds(symbol)

    # ── Detect trending market ──
    # Primary: ADX > 20 OR SMA alignment with price confirmation
    # Secondary: Price + MACD agree on direction (catches post-whipsaw legs)
    _is_trending = False
    _trend_bearish = False
    _trend_bullish = False
    _adx_strong = indicators.adx is not None and indicators.adx >= 28
    _is_commodity = sym_upper in COMMODITY_NAMES
    if indicators.sma20 is not None and indicators.sma50 is not None:
        _sma_spread = abs(indicators.sma20 - indicators.sma50) / indicators.sma50 * 100 if indicators.sma50 > 0 else 0
        _adx_ok = indicators.adx is not None and indicators.adx > 20
        _sma_aligned = _sma_spread > 0.01
        _macd_bear = indicators.macd is not None and indicators.macd.histogram < 0 and indicators.macd.macd < indicators.macd.signal
        _macd_bull = indicators.macd is not None and indicators.macd.histogram > 0 and indicators.macd.macd > indicators.macd.signal

        if _adx_ok or _sma_aligned:
            _is_trending = True
            # Classic trend: SMA alignment + price confirms
            if indicators.sma20 < indicators.sma50 and price < indicators.sma20:
                _trend_bearish = True
            elif indicators.sma20 > indicators.sma50 and price > indicators.sma20:
                _trend_bullish = True
            # Post-whipsaw: price broke below SMA20 + MACD bearish = new downleg
            elif price < indicators.sma20 and _macd_bear:
                _trend_bearish = True
            # Post-whipsaw: price broke above SMA20 + MACD bullish = new upleg
            elif price > indicators.sma20 and _macd_bull:
                _trend_bullish = True

    # 1. RSI Analysis (weight: 2)
    # In trending market: extreme RSI CONFIRMS trend instead of signaling reversal
    # Indices have wider RSI zones (27-55 SELL, 45-73 BUY) to capture more signals
    _is_index = sym_upper in INDEX_NAMES
    _rsi_oversold = 27 if _is_index else 30
    _rsi_bear_zone = 55 if _is_index else 45
    _rsi_bull_zone = 45 if _is_index else 55
    _rsi_overbought = 73 if _is_index else 70

    _rsi_exhausted = False  # RSI stuck in extreme zone = exhausted momentum
    if indicators.rsi14 is not None:
        if indicators.rsi14 < _rsi_oversold:
            # Check exhaustion: RSI was ALSO below 35 five candles ago
            _os_exhausted = (indicators.rsi_prev5 is not None and indicators.rsi_prev5 < 35)
            if _trend_bearish:
                bear_score += 2  # RSI survendu + tendance baissière = momentum SELL
                reasons.append(f"RSI {indicators.rsi14:.0f} momentum baissier fort")
            elif _os_exhausted:
                bull_score += 1  # Reduced from +2: exhausted, bounce less likely
                _rsi_exhausted = True
                reasons.append(f"RSI {indicators.rsi14:.0f} survendu prolonge (epuise)")
            else:
                bull_score += 2
                reasons.append(f"RSI {indicators.rsi14:.0f} survendu")
        elif indicators.rsi14 < _rsi_bear_zone:
            if _trend_bearish:
                bear_score += 1
                reasons.append(f"RSI {indicators.rsi14:.0f} pression baissiere")
            else:
                bull_score += 1
                reasons.append(f"RSI {indicators.rsi14:.0f} zone basse")
        elif indicators.rsi14 > _rsi_overbought:
            # Check exhaustion: RSI was ALSO above 65 five candles ago
            _ob_exhausted = (indicators.rsi_prev5 is not None and indicators.rsi_prev5 > 65)
            if _trend_bullish:
                bull_score += 2  # RSI suracheté + tendance haussière = momentum BUY
                reasons.append(f"RSI {indicators.rsi14:.0f} momentum haussier fort")
            elif _ob_exhausted:
                bear_score += 1  # Reduced from +2: exhausted, reversal less likely
                _rsi_exhausted = True
                reasons.append(f"RSI {indicators.rsi14:.0f} surachete prolonge (epuise)")
            else:
                bear_score += 2
                reasons.append(f"RSI {indicators.rsi14:.0f} surachete")
        elif indicators.rsi14 > _rsi_bull_zone:
            if _trend_bullish:
                bull_score += 1
                reasons.append(f"RSI {indicators.rsi14:.0f} pression haussiere")
            else:
                bear_score += 1
                reasons.append(f"RSI {indicators.rsi14:.0f} zone haute")

    # 2. MACD Analysis (weight: 2)
    if indicators.macd:
        if indicators.macd.histogram > 0 and indicators.macd.macd > indicators.macd.signal:
            bull_score += 2
            reasons.append("MACD haussier")
        elif indicators.macd.histogram < 0 and indicators.macd.macd < indicators.macd.signal:
            bear_score += 2
            reasons.append("MACD baissier")
        elif indicators.macd.histogram > 0:
            bull_score += 1
            reasons.append("MACD positif")
        else:
            bear_score += 1
            reasons.append("MACD negatif")

    # 2b. MACD Declining Momentum Penalty
    # Histogram still positive but declining = momentum fading → penalize BUY
    # Histogram still negative but rising = bearish momentum fading → penalize SELL
    if indicators.macd and indicators.macd_hist_prev is not None:
        _h_now = indicators.macd.histogram
        _h_prev = indicators.macd_hist_prev
        if _h_now > 0 and _h_now < _h_prev * 0.6:
            bear_score += 1
            reasons.append(f"MACD momentum declinant ({_h_prev:.5f}→{_h_now:.5f})")
        elif _h_now < 0 and _h_now > _h_prev * 0.6:
            bull_score += 1
            reasons.append(f"MACD baissier faiblissant ({_h_prev:.5f}→{_h_now:.5f})")

    # 3. Bollinger Bands (weight: 2) — NEVER reward buying at extremes
    # BB haute = surtendu (risque de retour), BB basse = surtendu (risque de rebond)
    # En tendance: BB milieu (40-60%) = pullback = meilleur point d'entree
    bb_squeeze_threshold = thresholds["bb_squeeze"]
    if indicators.bollinger_bands:
        bb = indicators.bollinger_bands
        bb_range = bb.upper - bb.lower
        bb_position = (price - bb.lower) / bb_range if bb_range > 0 else 0.5
        if bb_position < 0.15:
            if _trend_bearish and _adx_strong:
                bear_score += 2  # Strong downtrend + low BB = momentum SELL
                reasons.append("BB <15% forte tendance baissiere (momentum)")
            elif _trend_bearish:
                bear_score += 1  # Weak downtrend — risky
                bull_score += 1  # Penalty: near bottom
                reasons.append("BB <15% tendance baissiere (penalite SELL)")
            else:
                bull_score += 2  # Rebond potentiel en range
                reasons.append("Prix sur bande Bollinger basse")
        elif bb_position < 0.35:
            if _trend_bearish:
                bear_score += 1
                reasons.append("Prix zone basse BB")
            elif _trend_bullish:
                bull_score += 2  # Pullback dans uptrend = BON point d entree BUY
                reasons.append("Pullback BB en tendance haussiere")
            else:
                bull_score += 1
                reasons.append("Prix en zone basse Bollinger")
        elif bb_position > 0.85:
            if _trend_bullish and _adx_strong:
                bull_score += 2  # Strong uptrend + high BB = momentum BUY
                reasons.append("BB >85% forte tendance haussiere (momentum)")
            elif _trend_bullish:
                bear_score += 1  # Weak uptrend — risky to buy at top
                reasons.append("BB >85% surtendu (penalite BUY)")
            else:
                bear_score += 2
                reasons.append("Prix sur bande Bollinger haute")
        elif bb_position > 0.65:
            if _trend_bearish:
                bear_score += 2  # Pullback dans downtrend = BON point d entree SELL
                reasons.append("Pullback BB en tendance baissiere")
            elif _trend_bullish:
                # Neutre — prix haut en uptrend, ni bon ni mauvais
                reasons.append("BB haute en tendance (neutre)")
            else:
                bear_score += 1
                reasons.append("Prix en zone haute Bollinger")
        if bb.width < bb_squeeze_threshold:
            reasons.append(f"BB squeeze ({bb.width:.2f}%)")

    # 4. Moving Averages (weight: 2)
    if indicators.sma20 is not None and indicators.sma50 is not None:
        if indicators.sma20 > indicators.sma50 and price > indicators.sma20:
            bull_score += 2
            reasons.append("Tendance haussiere (Prix > SMA20 > SMA50)")
        elif indicators.sma20 < indicators.sma50 and price < indicators.sma20:
            bear_score += 2
            reasons.append("Tendance baissiere (Prix < SMA20 < SMA50)")
        elif price > indicators.sma20:
            bull_score += 1
            reasons.append("Prix au-dessus SMA20")
        elif price > indicators.sma50:
            bull_score += 1
            reasons.append("Prix entre SMA50 et SMA20")
        else:
            bear_score += 1
            reasons.append("Prix sous SMA20 et SMA50")

    # 5. Stochastic (weight: 2) — trend-aware
    if indicators.stochastic:
        if indicators.stochastic.k < 20:
            if _trend_bearish:
                bear_score += 2
                reasons.append(f"Stoch K:{indicators.stochastic.k:.0f} momentum SELL")
            else:
                bull_score += 2
                reasons.append(f"Stoch survendu K:{indicators.stochastic.k:.0f}")
        elif indicators.stochastic.k < 35:
            if _trend_bearish:
                bear_score += 1
                reasons.append(f"Stoch K:{indicators.stochastic.k:.0f} pression SELL")
            else:
                bull_score += 1
                reasons.append(f"Stoch bas K:{indicators.stochastic.k:.0f}")
        elif indicators.stochastic.k > 80:
            if _trend_bullish:
                bull_score += 2
                reasons.append(f"Stoch K:{indicators.stochastic.k:.0f} momentum BUY")
            else:
                bear_score += 2
                reasons.append(f"Stoch surachete K:{indicators.stochastic.k:.0f}")
        elif indicators.stochastic.k > 65:
            if _trend_bullish:
                bull_score += 1
                reasons.append(f"Stoch K:{indicators.stochastic.k:.0f} pression BUY")
            else:
                bear_score += 1
                reasons.append(f"Stoch haut K:{indicators.stochastic.k:.0f}")

    # 6. Price Momentum (weight: 2)
    strong_mom = thresholds["momentum_strong"]
    weak_mom = thresholds["momentum_weak"]
    if change_percent > strong_mom:
        bull_score += 2
        reasons.append(f"Momentum +{change_percent:.2f}% fort")
    elif change_percent > weak_mom:
        bull_score += 1
        reasons.append(f"Momentum +{change_percent:.2f}%")
    elif change_percent < -strong_mom:
        bear_score += 2
        reasons.append(f"Momentum {change_percent:.2f}% fort")
    elif change_percent < -weak_mom:
        bear_score += 1
        reasons.append(f"Momentum {change_percent:.2f}%")

    # 7. ADX — Trend Strength (weight: 1)
    if indicators.adx is not None:
        if indicators.adx > 25:
            reasons.append(f"ADX {indicators.adx:.0f} tendance forte")
            if bull_score > bear_score:
                bull_score += 1
            elif bear_score > bull_score:
                bear_score += 1
        elif indicators.adx > 20:
            reasons.append(f"ADX {indicators.adx:.0f} tendance moderee")
        else:
            reasons.append(f"ADX {indicators.adx:.0f} range")

    # 8. Volume confirmation (weight: 1)
    if indicators.volume_ratio is not None:
        if indicators.volume_ratio > 1.3:
            reasons.append(f"Volume {indicators.volume_ratio:.1f}x moy.")
            if bull_score > bear_score:
                bull_score += 1
            elif bear_score > bull_score:
                bear_score += 1
        elif indicators.volume_ratio < 0.5:
            reasons.append("Volume faible — signal moins fiable")

    # 9. Fibonacci levels (weight: 1)
    fib_tol = thresholds["fib_tolerance"]
    if indicators.fibonacci and price > 0:
        fib = indicators.fibonacci
        if abs(price - fib.s1) / price < fib_tol:
            bull_score += 1
            reasons.append("Support Fibonacci S1")
        elif abs(price - fib.s2) / price < fib_tol:
            bull_score += 1
            reasons.append("Support Fibonacci S2")
        elif price < fib.pivot:
            bull_score += 1
            reasons.append("Sous le pivot Fibonacci")
        elif abs(price - fib.r1) / price < fib_tol:
            bear_score += 1
            reasons.append("Resistance Fibonacci R1")
        elif price > fib.r2:
            bear_score += 1
            reasons.append("Au-dessus R2 Fibonacci")

    # 10. VWAP — Index-specific indicator (weight: 2 for indices, 0 for others)
    # Price vs VWAP shows institutional buying/selling pressure
    # Above VWAP = institutions buying (bullish), Below VWAP = institutions selling (bearish)
    if _is_index and indicators.vwap is not None and indicators.vwap > 0:
        vwap_distance_pct = (price - indicators.vwap) / indicators.vwap * 100
        if vwap_distance_pct > 0.15:
            # Price well above VWAP — strong institutional buying
            bull_score += 2
            reasons.append(f"VWAP: prix {vwap_distance_pct:.2f}% au-dessus (achat institutionnel)")
        elif vwap_distance_pct > 0.03:
            # Slightly above VWAP — mild bullish
            bull_score += 1
            reasons.append(f"VWAP: prix au-dessus ({vwap_distance_pct:.2f}%)")
        elif vwap_distance_pct < -0.15:
            # Price well below VWAP — strong institutional selling
            bear_score += 2
            reasons.append(f"VWAP: prix {abs(vwap_distance_pct):.2f}% en-dessous (vente institutionnelle)")
        elif vwap_distance_pct < -0.03:
            # Slightly below VWAP — mild bearish
            bear_score += 1
            reasons.append(f"VWAP: prix en-dessous ({abs(vwap_distance_pct):.2f}%)")
        else:
            reasons.append(f"VWAP: prix au niveau du VWAP ({vwap_distance_pct:+.2f}%)")

    # ── Calculate confidence and preliminary signal ─────────────────────
    total_possible = 18 if _is_index else 16
    net_score = bull_score - bear_score
    abs_net = abs(net_score)
    raw_confidence = abs_net / total_possible * 100

    # ── SIGNAL DECISION — 2026-04-21 Option A: DIRECTION = H1 MACRO ──
    # Règle simple: prix vs SMA50 H1 détermine la direction candidate.
    # Les 10 indicateurs (score bull/bear) servent UNIQUEMENT à logger du contexte,
    # PAS à décider la direction. Le 4TF dans bot.py valide ensuite le setup complet.
    _sma50_h1 = None
    if candles_h1 and len(candles_h1) >= 50:
        try:
            _closes_h1 = [c.close for c in candles_h1]
            _sma50_h1 = compute_sma(_closes_h1, 50)
        except Exception:
            _sma50_h1 = None

    # 2026-04-23 M-ONLY : signals.py ne décide plus de la direction (M15 TEMPO
    # override en aval). On retourne toujours HOLD ici — c'est le M15 TEMPO
    # dans bot.py qui transformera en BUY/SELL selon la structure M15.
    signal = "hold"
    confidence = 0
    reasons.append("[M-ONLY] signals.py passe HOLD, direction = M15 TEMPO dans bot.py")

    # Note informative (pas de décision): score 10 indicateurs historique
    reasons.append(f"[INFO score 10-ind] bull={bull_score} bear={bear_score} net={net_score}")

    # 2026-04-21: MACD contradiction check DÉSACTIVÉ (Option A — H1 macro décide)
    if False and signal == "buy" and indicators.macd:
        _macd_is_bearish = (indicators.macd.histogram < 0 and indicators.macd.macd < indicators.macd.signal)
        if _macd_is_bearish and not _adx_strong:
            reasons.append(f"CONTRADICTION: BUY mais MACD baissier + ADX faible → confiance réduite")
            confidence = min(confidence, 70)
    elif signal == "sell" and indicators.macd:
        _macd_is_bullish = (indicators.macd.histogram > 0 and indicators.macd.macd > indicators.macd.signal)
        if _macd_is_bullish and not _adx_strong:
            reasons.append(f"CONTRADICTION: SELL mais MACD haussier + ADX faible → confiance réduite")
            confidence = min(confidence, 70)

    # ── GUARD ANTI-SOMMET — block BUY at overbought / SELL at oversold ──
    # SEULEMENT quand pas de tendance (ADX < 25). Si ADX >= 25, RSI élevé = momentum.
    # On utilise ADX seul (pas SMA) car les SMA sont lentes à réagir aux nouvelles tendances.
    _bb_pos = 0.5
    if indicators.bollinger_bands:
        _bb_r = indicators.bollinger_bands.upper - indicators.bollinger_bands.lower
        _bb_pos = (price - indicators.bollinger_bands.lower) / _bb_r if _bb_r > 0 else 0.5

    # 2026-04-21: LEGACY ANTI-SURCHAT/SURVENTE DÉSACTIVÉ
    # Les filtres 4TF (H1 macro + M5 body + M5 vol + M1 trigger) couvrent déjà ça.
    # Conservé pour info dans les logs mais ne force plus HOLD.
    _has_trend_strength = indicators.adx is not None and indicators.adx >= 25
    _ob_rsi = 80 if (_is_index or _is_commodity) else 78
    _os_rsi = 20 if (_is_index or _is_commodity) else 22
    if signal == "buy" and not _has_trend_strength:
        if indicators.rsi14 is not None and indicators.rsi14 > _ob_rsi:
            if indicators.stochastic is not None and indicators.stochastic.k > 80:
                reasons.append(f"[INFO] Surachat détecté (RSI={indicators.rsi14:.0f}) — 4TF décidera")
    elif signal == "sell" and not _has_trend_strength:
        if indicators.rsi14 is not None and indicators.rsi14 < _os_rsi:
            if indicators.stochastic is not None and indicators.stochastic.k < 20:
                reasons.append(f"[INFO] Survente détectée (RSI={indicators.rsi14:.0f}) — 4TF décidera")

    # ── HYBRID CONFIRMATION FILTER ──────────────────────────────────────
    lot_factor = pair_cfg.get("lot_factor", 1.0)

    # Volume filter — MANDATORY for stocks/indices (from AAPL script analysis)
    is_stock_or_index = pair_cfg.get("asset") in ("stock", "index")
    if signal in ("buy", "sell") and is_stock_or_index:
        vol_min = pair_cfg.get("volume_min", 1.0)
        if indicators.volume_ratio is not None and indicators.volume_ratio < vol_min:
            reasons.append(f"VOLUME INSUFFISANT ({indicators.volume_ratio:.1f}x < {vol_min}x) pour {pair_cfg.get('asset', 'stock')}")
            signal = "hold"
            confidence = 0

    # Detect Asian session from broker time (UTC+3 -> CET = UTC+1)
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    cet_hour = (now_utc + timedelta(hours=1)).hour + (now_utc + timedelta(hours=1)).minute / 60.0
    _asian_mode = is_asian_session(cet_hour)

    # 2026-04-21: HYBRID CONFIRMATION LEGACY DÉSACTIVÉE
    # Les gates RSI/MACD/BB/ADX/DI ne bloquent plus le signal. Seuls les filtres
    # 4TF (H1+Body+Volume+M1 [+D1 pour paires perdantes]) gouvernent l'exécution.
    # On garde l'appel pour logger les [GATE FAIL] en mode informatif uniquement.
    if signal in ("buy", "sell"):
        try:
            _passed_info, filter_reasons = _hybrid_confirmation(
                direction=signal,
                indicators=indicators,
                price=price,
                prev_close=prev_close,
                volatility=volatility,
                asian_mode=_asian_mode,
                symbol=symbol,
            )
            reasons.extend(filter_reasons)
            if not _passed_info:
                reasons.append("[INFO] Hybrid legacy fail — ignoré (4TF décide)")
        except Exception:
            pass

        # Anti-rebond RSI_prev5 DÉSACTIVÉ (2026-04-21) — signal info only
        if signal == "buy" and indicators.rsi_prev5 is not None and indicators.rsi_prev5 < 35:
            reasons.append(f"[INFO] Rebond détecté RSI_prev5={indicators.rsi_prev5:.0f}<35 — 4TF décide")
        elif signal == "sell" and indicators.rsi_prev5 is not None and indicators.rsi_prev5 > 65:
            reasons.append(f"[INFO] Rebond détecté RSI_prev5={indicators.rsi_prev5:.0f}>65 — 4TF décide")

        # Seuil de confiance : abaissé à 70% (score 10 indicateurs). Le vrai garde-fou
        # est le 4TF (4/4 ou 5/5 selon D1_PAIRS) dans bot.py.
        min_conf = pair_cfg.get("min_confidence", 70)
        if signal in ("buy", "sell") and confidence < min_conf:
            reasons.append(f"Confiance {confidence:.0f}% < seuil {min_conf}%")
            signal = "hold"
            confidence = 0

    # ── SL/TP calculation — per-pair pip-based ──────────────────────────
    suggested_entry = price
    atr = indicators.atr14 or price * 0.002

    if pair_cfg and signal in ("buy", "sell"):
        if "sl_pct" in pair_cfg:
            # Stocks/Indices: SL as % of price
            sl_distance = price * pair_cfg["sl_pct"]
            tp_distance = price * pair_cfg["tp_pct"]
        elif "sl_pips" in pair_cfg and sym_upper in FOREX_PAIRS:
            # Forex: SL in pips
            pip_size = 0.01 if "JPY" in sym_upper else 0.0001
            sl_distance = pair_cfg["sl_pips"] * pip_size
            tp_distance = pair_cfg["tp_pips"] * pip_size
        else:
            sl_distance = atr * thresholds["sl_multiplier"]
            tp_distance = atr * thresholds["tp_multiplier"]
    else:
        # ATR-based for indices/stocks/unknown
        sl_mult = thresholds["sl_multiplier"]
        tp_mult = thresholds["tp_multiplier"]
        sl_distance = atr * sl_mult
        tp_distance = atr * tp_mult

        # Cap SL/TP for forex (safety net)
        if sym_upper in FOREX_PAIRS:
            pip_size = 0.01 if "JPY" in sym_upper else 0.0001
            sl_distance = min(sl_distance, 10 * pip_size)
            tp_distance = min(tp_distance, 15 * pip_size)

    # 2026-04-22: TP override RETIRÉ — on garde les R:R natifs par paire (PAIR_CONFIG)
    # Les R:R 1:1.47 à 1:1.69 par paire sont plus cohérents avec la volatilité de chacune.
    # Forcer 1:2 créait des incohérences (ordres manuels différents, bugs de path).
    try:
        pass  # No-op
    except Exception:
        pass

    if signal == "buy":
        suggested_sl = price - sl_distance
        suggested_tp = price + tp_distance
    elif signal == "sell":
        suggested_sl = price + sl_distance
        suggested_tp = price - tp_distance
    else:
        suggested_sl = price - sl_distance
        suggested_tp = price + tp_distance

    # ── SPREAD FILTER — reject if spread eats the profit ────────────────
    tp_dist_final = abs(suggested_tp - price)
    spread_ok = True
    min_ratio = thresholds["min_tp_spread_ratio"]
    if spread > 0 and tp_dist_final > 0:
        ratio = tp_dist_final / spread
        if ratio < min_ratio:
            spread_ok = False
            reasons.append(f"SPREAD TROP LARGE ({ratio:.1f}x < {min_ratio}x)")
            signal = "hold"
            confidence = 0

    # ── MIN TP PIPS FILTER ──────────────────────────────────────────────
    min_tp_pips = thresholds.get("min_tp_pips", 0)
    if min_tp_pips > 0 and signal != "hold":
        if "XAU" in sym_upper or "XAG" in sym_upper:
            pip_sz = 0.01  # Gold/Silver: 1 pip = $0.01
        elif "CL" in sym_upper or "OIL" in sym_upper or "BRENT" in sym_upper:
            pip_sz = 0.01  # Oil: 1 pip = $0.01
        elif "/JPY" in symbol.upper() or "JPY" in sym_upper:
            pip_sz = 0.01
        elif "/" in symbol:
            pip_sz = 0.0001
        else:
            pip_sz = 0.01
        tp_pips = tp_dist_final / pip_sz
        if tp_pips < min_tp_pips:
            spread_ok = False
            reasons.append(f"TP trop petit ({tp_pips:.1f} pips < {min_tp_pips})")
            signal = "hold"
            confidence = 0

    # Indices/commodities: MT5 rejette les décimales excessives sur les SL/TP
    # Forex = 5 décimales (ex: 1.34567), Indices = 1 max (ex: 23513.5)
    _is_pct_based = "sl_pct" in pair_cfg
    _sl_round = 1 if _is_pct_based else 5
    _tp_round = 1 if _is_pct_based else 5

    # ═══ MODE CONTRARIAN DÉSACTIVÉ — 2026-04-16 ═══
    # Test concluant négatif: 1 trade perdant (-2.26€), 2 positions stagnantes à break-even,
    # capital en baisse continue. Retour au mode trend-following classique.

    return Signal(
        signal=signal,
        confidence=round(confidence),
        # 2026-04-21: Prioriser [H1 MACRO] et [INFO] dans le reason log
        reason=(
            " | ".join(r for r in reasons if "[H1 MACRO]" in r or "[H1 INDISPO]" in r) +
            (" | " if any("[H1 MACRO]" in r or "[H1 INDISPO]" in r for r in reasons) else "") +
            " | ".join(reasons[:5]) +
            (" | " + " | ".join(r for r in reasons[5:] if "GATE FAIL" in r or "FILTRE" in r or "[INFO]" in r)
             if any("GATE FAIL" in r or "FILTRE" in r or "[INFO]" in r for r in reasons[5:]) else "")
        ),
        suggested_entry=suggested_entry,
        suggested_sl=round(suggested_sl, _sl_round),
        suggested_tp=round(suggested_tp, _tp_round),
        bull_score=bull_score,
        bear_score=bear_score,
        spread_ok=spread_ok,
        lot_factor=lot_factor,
    )


# ── Capital.com cost model (spread-only, no commission on most CFDs) ───
def estimate_trade_cost(symbol: str, lot_size: float, spread_price: float) -> dict:
    """Estimate total cost of a round-trip trade on Capital.com."""
    sym = symbol.upper()
    if '/' in sym and 'XAU' not in sym and 'XAG' not in sym:
        contract_size = 100_000
    elif 'XAU' in sym:
        contract_size = 100
    else:
        contract_size = 1

    spread_cost_eur = spread_price * lot_size * contract_size * 0.87

    return {
        'spread_cost': round(spread_cost_eur, 2),
        'commission': 0.0,  # Capital.com: spread-only pricing
        'total_cost': round(spread_cost_eur, 2),
        'lot_size': lot_size,
    }
