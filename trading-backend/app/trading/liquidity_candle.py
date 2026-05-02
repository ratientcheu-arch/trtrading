"""
Liquidity Candle strategy — US session only (15h30 Paris / 13h30 UTC).

Stratégie "bougie manipulée" :
- Détection à 13h45 UTC après clôture de la 1ère bougie M15 de la session US
- Si range(B1) >= 25% × ATR(14) D1 → bougie manipulée
- Direction: bougie haussière → SELL, baissière → BUY
- Entry LIMIT: extrême de la bougie manipulée
- TP: Fib 38.2% retracement (distance = 0.382 × range_B1)
- SL: Fib 119.1% extension (distance = 0.191 × range_B1)
- R:R 1:2
- Expiration ordre: 30 min si pas touché

Cette stratégie tourne en PARALLÈLE du 4TF pro, sans interférence :
- Source des trades marquée "liquidity_candle"
- Respecte les mêmes protections (daily loss, cooldown, max positions, news embargo)
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

from app.trading.indicators import Candle

logger = logging.getLogger(__name__)


@dataclass
class LiquiditySignal:
    """A validated liquidity candle setup waiting to be triggered (LIMIT order at Fib 100%)."""
    symbol: str
    direction: str        # "buy" or "sell"
    entry: float          # limit order price
    tp: float             # take profit price
    sl: float             # stop loss price
    range_b1: float       # range of the mother candle
    atr_d1: float         # ATR D1 used for validation
    ratio: float          # range_b1 / atr_d1
    b1_close_ts: float    # UTC timestamp of B1 close (= detection time)
    expires_at: float     # UTC timestamp when order expires (+ 30 min)
    triggered: bool = False
    triggered_at: Optional[float] = None


@dataclass
class PatternSignal:
    """
    Sub-strategy 2026-04-19: after B1 manipulation detected, look on M5 for
    a reversal pattern (hammer / inverted hammer / engulfing) within 90 min.
    Once pattern found, wait for breakout candle to enter.
    R:R 1:2 based on pattern extreme (SL above/below) and 2x that distance as TP.
    """
    symbol: str
    direction: str              # "buy" or "sell"
    b1_high: float              # for Fib TP calculation (optional)
    b1_low: float
    b1_close_ts: float
    range_b1: float
    atr_d1: float
    ratio: float
    expires_at: float           # b1_close_ts + 90 min
    # Pattern detection (filled when found):
    pattern_found: bool = False
    pattern_type: Optional[str] = None
    pattern_high: Optional[float] = None
    pattern_low: Optional[float] = None
    pattern_ts: Optional[float] = None
    # Breakout confirmation (filled when triggered):
    triggered: bool = False
    triggered_at: Optional[float] = None
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


def compute_atr_d1(candles_d1: list[Candle], period: int = 14) -> Optional[float]:
    """Compute ATR(14) on D1 candles using True Range."""
    if not candles_d1 or len(candles_d1) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles_d1)):
        c = candles_d1[i]
        p = candles_d1[i - 1]
        tr = max(
            c.high - c.low,
            abs(c.high - p.close),
            abs(c.low - p.close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def detect_liquidity_candle(
    symbol: str,
    b1: Candle,
    candles_d1: list[Candle],
    min_ratio: float = 0.25,
) -> Optional[LiquiditySignal]:
    """
    Check if bougie B1 is a "liquidity candle" manipulée.
    Returns a LiquiditySignal if valid, else None.
    """
    # 1. ATR D1
    atr_d1 = compute_atr_d1(candles_d1, 14)
    if atr_d1 is None or atr_d1 <= 0:
        logger.info(f"[LIQ CANDLE] {symbol}: ATR D1 unavailable, skip")
        return None

    # 2. Range B1 >= 25% ATR D1
    range_b1 = b1.high - b1.low
    if range_b1 <= 0:
        return None
    ratio = range_b1 / atr_d1
    if ratio < min_ratio:
        logger.debug(f"[LIQ CANDLE] {symbol}: range {range_b1:.5f} / ATR {atr_d1:.5f} = {ratio:.2f} < 0.25, skip")
        return None

    # 3. Direction based on candle body — 2026-04-22: RETOUR aux ratios Fib natifs
    # TP = Fib 38.2% (retracement), SL = Fib 119.1% (extension)
    # R:R ≈ 1:2 naturellement (0.382 / 0.191 = 2.0), pas besoin de forcer
    if b1.close > b1.open:
        # Bullish candle → SELL (expect reversal down)
        direction = "sell"
        entry = b1.high
        tp = b1.high - 0.382 * range_b1
        sl = b1.high + 0.191 * range_b1
    elif b1.close < b1.open:
        direction = "buy"
        entry = b1.low
        tp = b1.low + 0.382 * range_b1
        sl = b1.low - 0.191 * range_b1
    else:
        # Doji: skip
        logger.info(f"[LIQ CANDLE] {symbol}: doji (open=close), skip")
        return None

    # 4. Timestamp B1 close + 30 min expiry (backtest 13-17 avril: 30min > 90min sur US)
    b1_ts = b1.timestamp / 1000 if b1.timestamp > 1e12 else b1.timestamp
    b1_close_ts = b1_ts + 15 * 60  # B1 starts at b1.timestamp, closes 15 min later
    expires_at = b1_close_ts + 30 * 60  # +30 min from B1 close (optimal vs 90 min)

    logger.warning(
        f"[LIQ CANDLE] {symbol} {direction.upper()} DETECTED: "
        f"range={range_b1:.5f} ATR_D1={atr_d1:.5f} ratio={ratio:.2f} | "
        f"entry={entry:.5f} TP={tp:.5f} SL={sl:.5f}"
    )
    return LiquiditySignal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        tp=tp,
        sl=sl,
        range_b1=range_b1,
        atr_d1=atr_d1,
        ratio=ratio,
        b1_close_ts=b1_close_ts,
        expires_at=expires_at,
    )


def detect_pattern_signal(
    symbol: str,
    b1: Candle,
    candles_d1: list,
    min_ratio: float = 0.25,
) -> Optional[PatternSignal]:
    """
    Check if B1 is a liquidity candle and return a PatternSignal (90 min window
    to find a M5 pattern + breakout). Different from LiquiditySignal which uses
    a 30-min LIMIT order at Fib 100%.
    """
    atr_d1 = compute_atr_d1(candles_d1, 14)
    if atr_d1 is None or atr_d1 <= 0:
        return None
    range_b1 = b1.high - b1.low
    if range_b1 <= 0:
        return None
    ratio = range_b1 / atr_d1
    if ratio < min_ratio:
        return None
    # Direction
    if b1.close > b1.open:
        direction = "sell"
    elif b1.close < b1.open:
        direction = "buy"
    else:
        return None
    b1_ts = b1.timestamp / 1000 if b1.timestamp > 1e12 else b1.timestamp
    b1_close_ts = b1_ts + 15 * 60
    expires_at = b1_close_ts + 90 * 60  # 90 min window
    logger.warning(
        f"[PATTERN SIGNAL] {symbol} {direction.upper()} DETECTED: "
        f"ratio={ratio:.2f} — waiting for M5 pattern within 90 min"
    )
    return PatternSignal(
        symbol=symbol, direction=direction,
        b1_high=b1.high, b1_low=b1.low,
        b1_close_ts=b1_close_ts,
        range_b1=range_b1, atr_d1=atr_d1, ratio=ratio,
        expires_at=expires_at,
    )


def should_trigger_limit(signal: LiquiditySignal, current_bid: float, current_ask: float) -> bool:
    """Check if the LIMIT order should trigger (price touched entry level)."""
    if signal.direction == "sell":
        # SELL LIMIT at high: triggers when ask reaches entry level
        return current_ask >= signal.entry
    else:
        # BUY LIMIT at low: triggers when bid reaches entry level
        return current_bid <= signal.entry


def is_expired(signal, now_utc_ts: float) -> bool:
    """Check if signal has expired. Works for both LiquiditySignal and PatternSignal."""
    return now_utc_ts >= signal.expires_at


# ── Session detection ────────────────────────────────────────────────────────
def is_us_open_check_time(now_utc: datetime) -> bool:
    """True if current UTC time is exactly 13:45 (US session B1 closed = 15:45 Paris CEST).
    Weekday only."""
    if now_utc.hour != 13 or now_utc.minute != 45:
        return False
    # Monday-Friday only (weekday 0-4)
    if now_utc.weekday() >= 5:
        return False
    return True


def is_eu_open_check_time(now_utc: datetime) -> bool:
    """True if UTC = 7:15 (EU session B1 M15 closed = 9:15 Paris CEST).
    Activé 2026-04-20 : détection bougie manipulation sur ouverture EU.
    Couvre Euronext Paris (CAC40) + Xetra Frankfurt (DAX40) + paires EUR.
    UK100 EXCLU (LSE ouvre à 08:00 UTC) — voir is_uk_open_check_time.
    Monday-Friday only."""
    if now_utc.hour != 7 or now_utc.minute != 15:
        return False
    if now_utc.weekday() >= 5:
        return False
    return True


def is_uk_open_check_time(now_utc: datetime) -> bool:
    """True if UTC = 8:15 (UK session B1 M15 closed = 10:15 Paris CEST = 09:15 London BST).
    Activé 2026-04-21 : LSE ouvre à 08:00 UTC (≠ Euronext/Xetra qui ouvrent à 07:00 UTC).
    Couvre UK100 (FTSE 100).
    Monday-Friday only."""
    if now_utc.hour != 8 or now_utc.minute != 15:
        return False
    if now_utc.weekday() >= 5:
        return False
    return True


def is_asia_open_check_time(now_utc: datetime) -> bool:
    """True if current UTC time is exactly 23:15 (Asia session B1 closed = 1:15 Paris CEST).
    Tokyo ouvre à 23h UTC (= 1h Paris). B1 M15 démarre à 23h UTC, clôture à 23h15.

    Jours valides (UTC):
    - Dimanche 23:15 UTC = lundi 1:15 Paris → ouverture semaine Asia ✅
    - Lundi-Jeudi 23:15 UTC = mardi-vendredi 1:15 Paris ✅
    - Vendredi 23:15 UTC = samedi 1:15 Paris → marché fermé ❌
    - Samedi → marché fermé ❌
    """
    if now_utc.hour != 23 or now_utc.minute != 15:
        return False
    wd = now_utc.weekday()
    # Friday (4) and Saturday (5) = market closed after Fri 22h UTC
    if wd == 4 or wd == 5:
        return False
    return True
