"""
Range pipeline (2026-04-23) — entries on range borders with rejection candle.

Use when regime_detector returns regime == "range". Forex only (per user request).

Seuils centralisés dans filters_config.py — ne jamais inliner ici.

Entry rules:
  BUY  : price in lower RANGE_ENTRY_ZONE_PCT of range AND bullish hammer/engulfing
         AND RSI_M5 < RANGE_RSI_BUY_MAX  AND  volume >= VOLUME_MIN_RATIO × median
  SELL : price in upper RANGE_ENTRY_ZONE_PCT of range AND bearish shooting-star/engulfing
         AND RSI_M5 > RANGE_RSI_SELL_MIN AND  volume >= VOLUME_MIN_RATIO × median

SL/TP:
  BUY  : SL = range_low  - RANGE_SL_PADDING_PCT × box_width,  TP = middle of range
  SELL : SL = range_high + RANGE_SL_PADDING_PCT × box_width,  TP = middle of range

Refuse entry if R:R < RANGE_RR_MIN.
Position sizing: SIZE_REDUCED (filters_config.py).
"""
from dataclasses import dataclass, field
from typing import Optional

from app.trading.indicators import Candle, compute_rsi
from app.trading.filters_config import (
    VOLUME_MIN_RATIO_RANGE, VOLUME_MID_RATIO_RANGE, VOLUME_MAX_RATIO_RANGE,
    SIZE_REDUCED, SIZE_REDUCED_LOW,
    RANGE_RSI_BUY_MAX, RANGE_RSI_SELL_MIN,
    RANGE_ENTRY_ZONE_PCT, RANGE_SL_PADDING_PCT, RANGE_TP_TARGET_PCT, RANGE_RR_MIN,
)


@dataclass
class RangeEntryResult:
    ok: bool
    reason: str
    sl: Optional[float] = None
    tp: Optional[float] = None
    size_factor: float = field(default=SIZE_REDUCED)  # from filters_config (user rule)

    def __str__(self) -> str:
        tag = "RANGE OK" if self.ok else "RANGE FAIL"
        if self.ok and self.sl and self.tp:
            return f"[{tag}] {self.reason} SL={self.sl:.5f} TP={self.tp:.5f}"
        return f"[{tag}] {self.reason}"


def _is_bullish_hammer(c: Candle) -> bool:
    """Hammer: small body (≤ 35% of range) with long lower wick (≥ 2× body) and close > open."""
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = abs(c.close - c.open)
    lower_wick = min(c.open, c.close) - c.low
    upper_wick = c.high - max(c.open, c.close)
    return (
        body / rng <= 0.35
        and lower_wick >= 2 * body
        and upper_wick <= body
        and c.close > c.open
    )


def _is_bearish_shooting_star(c: Candle) -> bool:
    """Shooting star: small body, long upper wick (≥ 2× body), close < open."""
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = abs(c.close - c.open)
    upper_wick = c.high - max(c.open, c.close)
    lower_wick = min(c.open, c.close) - c.low
    return (
        body / rng <= 0.35
        and upper_wick >= 2 * body
        and lower_wick <= body
        and c.close < c.open
    )


def _is_bullish_pin_bar(c: Candle) -> bool:
    """Pin bar haussier : mèche basse ≥ 66% du range, corps et mèche haute petits.
    Plus strict que le hammer classique : la mèche DOIT dominer tout le range.
    Standard expert pour signaler un rejet fort d'un niveau."""
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = abs(c.close - c.open)
    lower_wick = min(c.open, c.close) - c.low
    upper_wick = c.high - max(c.open, c.close)
    return (
        lower_wick / rng >= 0.66          # mèche basse ≥ 66% du range
        and body / rng <= 0.33            # corps ≤ 33%
        and upper_wick / rng <= 0.15      # mèche haute très réduite
    )


def _is_bearish_pin_bar(c: Candle) -> bool:
    """Pin bar baissier : symétrique du bullish — mèche haute ≥ 66% du range."""
    rng = c.high - c.low
    if rng <= 0:
        return False
    body = abs(c.close - c.open)
    upper_wick = c.high - max(c.open, c.close)
    lower_wick = min(c.open, c.close) - c.low
    return (
        upper_wick / rng >= 0.66
        and body / rng <= 0.33
        and lower_wick / rng <= 0.15
    )


def _is_bullish_engulfing(c: Candle, prev: Candle) -> bool:
    """Current bullish body fully engulfs previous bearish body."""
    return (
        prev.close < prev.open          # prev bearish
        and c.close > c.open            # curr bullish
        and c.open <= prev.close        # opens at/below prev close
        and c.close >= prev.open        # closes at/above prev open
    )


def _is_bearish_engulfing(c: Candle, prev: Candle) -> bool:
    return (
        prev.close > prev.open          # prev bullish
        and c.close < c.open            # curr bearish
        and c.open >= prev.close        # opens at/above prev close
        and c.close <= prev.open        # closes at/below prev open
    )


def _median_volume(candles: list[Candle], lookback: int = 20) -> float:
    # 2026-04-23 fix: exclure la dernière bougie (en cours / partielle) du calcul
    # de la médiane ET de la comparaison. Utilise candles[-2] (dernière bougie
    # COMPLÈTE) comme référence.
    vols = sorted([c.volume for c in candles[-(lookback + 2):-2]])
    if not vols:
        return 0.0
    n = len(vols)
    return vols[n // 2] if n % 2 == 1 else (vols[n // 2 - 1] + vols[n // 2]) / 2


def evaluate_range_entry(
    signal: str,
    price: float,
    candles_m5: Optional[list[Candle]],
    range_high: float,
    range_low: float,
    symbol: Optional[str] = None,
) -> RangeEntryResult:
    """
    Evaluate a potential range entry given a regime classified as "range".
    Returns RangeEntryResult with SL/TP filled if ok.
    """
    if range_high <= range_low:
        return RangeEntryResult(False, f"range invalide [{range_low}..{range_high}]")
    if not candles_m5 or len(candles_m5) < 22:
        return RangeEntryResult(
            False, f"M5 insuffisant ({len(candles_m5) if candles_m5 else 0}/22)"
        )

    box_width = range_high - range_low
    middle = (range_high + range_low) / 2.0
    lower_zone_top = range_low + RANGE_ENTRY_ZONE_PCT * box_width
    upper_zone_bottom = range_high - RANGE_ENTRY_ZONE_PCT * box_width

    c = candles_m5[-1]
    c_prev = candles_m5[-2]

    # Volume check — baseline median (uses last COMPLETED bar)
    median_vol = _median_volume(candles_m5, lookback=20)
    if median_vol <= 0:
        return RangeEntryResult(False, "volume médian = 0")
    # 2026-04-23 fix: current_vol = dernière bougie COMPLÈTE, pas celle en cours.
    _current_vol = candles_m5[-2].volume if len(candles_m5) >= 2 else c.volume
    vol_ratio = _current_vol / median_vol
    if vol_ratio < VOLUME_MIN_RATIO_RANGE:
        return RangeEntryResult(
            False, f"volume {vol_ratio:.2f}× < {VOLUME_MIN_RATIO_RANGE:.2f} (range mort)"
        )
    if vol_ratio > VOLUME_MAX_RATIO_RANGE:
        return RangeEntryResult(
            False,
            f"volume {vol_ratio:.2f}× > {VOLUME_MAX_RATIO_RANGE:.2f} "
            f"(pas un range — trend qui démarre)"
        )
    # 2 paliers dans la bande range — user rule 2026-04-23
    _range_size_factor = SIZE_REDUCED_LOW if vol_ratio < VOLUME_MID_RATIO_RANGE else SIZE_REDUCED

    # RSI M5 check
    closes = [x.close for x in candles_m5]
    rsi = compute_rsi(closes, period=14)
    if rsi is None:
        return RangeEntryResult(False, "RSI M5 indisponible")

    if signal == "buy":
        if price > lower_zone_top:
            return RangeEntryResult(
                False,
                f"prix {price:.5f} hors zone basse (<= {lower_zone_top:.5f}) — pas de rebond"
            )
        if rsi > RANGE_RSI_BUY_MAX:
            return RangeEntryResult(
                False, f"RSI {rsi:.1f} > {RANGE_RSI_BUY_MAX:.0f} — pas en survente stricte"
            )
        _pattern = None
        if _is_bullish_pin_bar(c):
            _pattern = "pin bar"
        elif _is_bullish_hammer(c):
            _pattern = "marteau"
        elif _is_bullish_engulfing(c, c_prev):
            _pattern = "englobante"
        if _pattern is None:
            return RangeEntryResult(
                False, "pas de bougie de rejet haussière (ni pin bar, marteau, englobante)"
            )
        sl = range_low - RANGE_SL_PADDING_PCT * box_width
        # TP expert standard : 80% vers la borne opposée (capture majeure du range)
        tp = range_low + RANGE_TP_TARGET_PCT * box_width
        rr = (tp - price) / max(price - sl, 1e-9)
        if rr < RANGE_RR_MIN:
            return RangeEntryResult(
                False, f"R:R {rr:.2f} < {RANGE_RR_MIN:.2f} (entre plus près de range_low pour meilleur R:R)"
            )
        return RangeEntryResult(
            True,
            f"rebond bas [{_pattern}] prix {price:.5f} zone<= {lower_zone_top:.5f} "
            f"RSI {rsi:.1f} vol {vol_ratio:.2f}× R:R {rr:.2f}",
            sl=sl, tp=tp, size_factor=_range_size_factor,
        )

    if signal == "sell":
        if price < upper_zone_bottom:
            return RangeEntryResult(
                False,
                f"prix {price:.5f} hors zone haute (>= {upper_zone_bottom:.5f}) — pas de rejet"
            )
        if rsi < RANGE_RSI_SELL_MIN:
            return RangeEntryResult(
                False, f"RSI {rsi:.1f} < {RANGE_RSI_SELL_MIN:.0f} — pas en surachat strict"
            )
        _pattern = None
        if _is_bearish_pin_bar(c):
            _pattern = "pin bar"
        elif _is_bearish_shooting_star(c):
            _pattern = "shooting-star"
        elif _is_bearish_engulfing(c, c_prev):
            _pattern = "englobante"
        if _pattern is None:
            return RangeEntryResult(
                False, "pas de bougie de rejet baissière (ni pin bar, shooting-star, englobante)"
            )
        sl = range_high + RANGE_SL_PADDING_PCT * box_width
        tp = range_high - RANGE_TP_TARGET_PCT * box_width
        rr = (price - tp) / max(sl - price, 1e-9)
        if rr < RANGE_RR_MIN:
            return RangeEntryResult(
                False, f"R:R {rr:.2f} < {RANGE_RR_MIN:.2f} (entre plus près de range_high pour meilleur R:R)"
            )
        return RangeEntryResult(
            True,
            f"rejet haut [{_pattern}] prix {price:.5f} zone>= {upper_zone_bottom:.5f} "
            f"RSI {rsi:.1f} vol {vol_ratio:.2f}× R:R {rr:.2f}",
            sl=sl, tp=tp, size_factor=_range_size_factor,
        )

    return RangeEntryResult(False, "signal non directionnel")


def is_forex_symbol(symbol: str) -> bool:
    """True if symbol is a forex pair (contains '/' with currency codes)."""
    if not symbol or "/" not in symbol:
        return False
    parts = symbol.split("/")
    if len(parts) != 2:
        return False
    ccys = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"}
    return parts[0].upper() in ccys and parts[1].upper() in ccys


# Indices autorisés pour le pipeline range (2026-04-23 user rule).
# Commodities exclues (GOLD/OIL trop volatiles pour un vrai range).
_RANGEABLE_INDICES = {
    "DAX40", "CAC40", "UK100", "SP500", "NASDAQ", "DJ30",
    "NKY", "HK50", "AUS200",
}


def is_rangeable_symbol(symbol: str) -> bool:
    """True si le symbole peut utiliser le pipeline range : forex OU indices.
    Commodities exclues. User-validated 2026-04-23."""
    if is_forex_symbol(symbol):
        return True
    return symbol.upper().replace("/", "").replace(".", "") in _RANGEABLE_INDICES
