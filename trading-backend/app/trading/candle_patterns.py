"""
Reversal Candle Patterns detection (M5) for Liquidity Candle strategy.

4 patterns recherchés après une bougie de manipulation M15:
- HAMMER              → signal BUY (après manip baissière)
- INVERTED HAMMER     → signal SELL (après manip haussière)
- BULLISH ENGULFING   → signal BUY (après manip baissière)
- BEARISH ENGULFING   → signal SELL (après manip haussière)

Conventions de l'utilisateur (2026-04-19):
- INVERTED HAMMER = corps bas + longue mèche haute, cherché après HAUSSIÈRE = SELL
- HAMMER = corps haut + longue mèche basse, cherché après BAISSIÈRE = BUY
"""
from dataclasses import dataclass
from typing import Optional
from app.trading.indicators import Candle


@dataclass
class PatternDetection:
    pattern_type: str    # "hammer", "inverted_hammer", "bull_engulf", "bear_engulf"
    direction: str       # "buy" or "sell"
    high: float          # extreme haut du pattern (pour SL d'un SELL)
    low: float           # extreme bas du pattern (pour SL d'un BUY)
    timestamp: float     # timestamp de la dernière bougie du pattern (utilisée pour la cassure)
    index: int           # index dans la liste M5


def _body_wicks(c: Candle) -> tuple[float, float, float, float]:
    """Return (body, upper_wick, lower_wick, total_range)."""
    body = abs(c.close - c.open)
    total = c.high - c.low
    upper_wick = c.high - max(c.open, c.close)
    lower_wick = min(c.open, c.close) - c.low
    return body, upper_wick, lower_wick, total


def is_hammer(c: Candle) -> bool:
    """HAMMER: petit corps en haut + LONGUE mèche basse (≥ 2× corps).
    Signal de retournement HAUSSIER.
    """
    body, upper, lower, total = _body_wicks(c)
    if total <= 0 or body <= 0:
        return False
    return (
        body <= 0.30 * total and
        lower >= 2.0 * body and
        upper <= body
    )


def is_inverted_hammer(c: Candle) -> bool:
    """INVERTED HAMMER: petit corps en bas + LONGUE mèche haute (≥ 2× corps).
    Dans notre convention: signal BAISSIER (cherché après manipulation haussière).
    """
    body, upper, lower, total = _body_wicks(c)
    if total <= 0 or body <= 0:
        return False
    return (
        body <= 0.30 * total and
        upper >= 2.0 * body and
        lower <= body
    )


def is_bullish_engulfing(a: Candle, b: Candle) -> bool:
    """BULLISH ENGULFING: A rouge, B verte englobe totalement le corps de A.
    Signal de retournement HAUSSIER.
    """
    a_bear = a.close < a.open
    b_bull = b.close > b.open
    if not (a_bear and b_bull):
        return False
    return (b.open <= a.close) and (b.close >= a.open)


def is_bearish_engulfing(a: Candle, b: Candle) -> bool:
    """BEARISH ENGULFING: A verte, B rouge englobe totalement le corps de A.
    Signal de retournement BAISSIER.
    """
    a_bull = a.close > a.open
    b_bear = b.close < b.open
    if not (a_bull and b_bear):
        return False
    return (b.open >= a.close) and (b.close <= a.open)


def find_pattern_m5(
    direction: str, candles_m5: list[Candle], from_index: int = 0
) -> Optional[PatternDetection]:
    """
    Search for a reversal pattern from `from_index` onwards in M5 candles.
    Returns the FIRST detected pattern (earliest occurrence) matching the direction.

    - direction='sell' → INVERTED HAMMER or BEARISH ENGULFING
    - direction='buy'  → HAMMER or BULLISH ENGULFING
    """
    if len(candles_m5) < 2:
        return None
    start = max(from_index, 1)  # need at least 2 candles for engulfing
    for i in range(start, len(candles_m5)):
        c = candles_m5[i]
        prev = candles_m5[i - 1]
        ts = c.timestamp / 1000 if c.timestamp > 1e12 else c.timestamp

        if direction == "sell":
            if is_inverted_hammer(c):
                return PatternDetection(
                    pattern_type="inverted_hammer",
                    direction="sell",
                    high=c.high,
                    low=c.low,
                    timestamp=ts,
                    index=i,
                )
            if is_bearish_engulfing(prev, c):
                return PatternDetection(
                    pattern_type="bear_engulf",
                    direction="sell",
                    high=max(prev.high, c.high),
                    low=min(prev.low, c.low),
                    timestamp=ts,
                    index=i,
                )
        elif direction == "buy":
            if is_hammer(c):
                return PatternDetection(
                    pattern_type="hammer",
                    direction="buy",
                    high=c.high,
                    low=c.low,
                    timestamp=ts,
                    index=i,
                )
            if is_bullish_engulfing(prev, c):
                return PatternDetection(
                    pattern_type="bull_engulf",
                    direction="buy",
                    high=max(prev.high, c.high),
                    low=min(prev.low, c.low),
                    timestamp=ts,
                    index=i,
                )
    return None


def check_breakout(pattern: PatternDetection, candle: Candle) -> bool:
    """Check if candle confirms the breakout after the pattern.
    - SELL pattern: breakout = close < pattern.low
    - BUY pattern: breakout = close > pattern.high
    """
    if pattern.direction == "sell":
        return candle.close < pattern.low
    else:  # buy
        return candle.close > pattern.high
