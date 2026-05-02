"""
Technical analysis indicators — ported from App.tsx lines 106-288.
All functions operate on lists of floats (closes) or dicts (candles).
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MACDResult:
    macd: float
    signal: float
    histogram: float


@dataclass
class BollingerBands:
    upper: float
    middle: float
    lower: float
    width: float


@dataclass
class FibonacciLevels:
    pivot: float
    s1: float
    s2: float
    s3: float
    r1: float
    r2: float
    r3: float


@dataclass
class StochasticResult:
    k: float
    d: float


@dataclass
class TechnicalIndicators:
    rsi14: Optional[float] = None
    rsi_prev5: Optional[float] = None  # RSI from 5 candles ago (exhaustion detection)
    macd: Optional[MACDResult] = None
    macd_hist_prev: Optional[float] = None  # MACD histogram from 3 candles ago (declining detection)
    bollinger_bands: Optional[BollingerBands] = None
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    ema12: Optional[float] = None
    ema26: Optional[float] = None
    atr14: Optional[float] = None
    fibonacci: Optional[FibonacciLevels] = None
    volume_avg20: Optional[float] = None
    volume_ratio: Optional[float] = None
    stochastic: Optional[StochasticResult] = None
    adx: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None
    vwap: Optional[float] = None


def compute_sma(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    s = closes[-period:]
    return sum(s) / period


def compute_ema(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
    return ema


def compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_macd(closes: list[float]) -> Optional[MACDResult]:
    if len(closes) < 35:
        return None
    ema12 = compute_ema(closes, 12)
    ema26 = compute_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None
    macd_line = ema12 - ema26

    # Compute MACD history for signal line
    macd_history: list[float] = []
    for i in range(26, len(closes) + 1):
        e12 = compute_ema(closes[:i], 12)
        e26 = compute_ema(closes[:i], 26)
        if e12 is not None and e26 is not None:
            macd_history.append(e12 - e26)

    if len(macd_history) < 9:
        return None
    signal_line = compute_ema(macd_history, 9)
    if signal_line is None:
        return None

    return MACDResult(
        macd=round(macd_line, 4),
        signal=round(signal_line, 4),
        histogram=round(macd_line - signal_line, 4),
    )


def compute_bollinger_bands(closes: list[float], period: int = 20, multiplier: float = 2.0) -> Optional[BollingerBands]:
    if len(closes) < period:
        return None
    s = closes[-period:]
    mean = sum(s) / period
    variance = sum((x - mean) ** 2 for x in s) / period
    std_dev = variance ** 0.5
    upper = mean + multiplier * std_dev
    lower = mean - multiplier * std_dev
    return BollingerBands(
        upper=round(upper, 4),
        middle=round(mean, 4),
        lower=round(lower, 4),
        width=round((upper - lower) / mean * 100, 2) if mean > 0 else 0,
    )


def compute_atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(len(candles) - period, len(candles)):
        high = candles[i].high
        low = candles[i].low
        prev_close = candles[i - 1].close
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / period


def compute_fibonacci(candles: list[Candle]) -> Optional[FibonacciLevels]:
    if len(candles) < 20:
        return None
    recent = candles[-20:]
    high = max(c.high for c in recent)
    low = min(c.low for c in recent)
    close = candles[-1].close
    pivot = (high + low + close) / 3
    r = high - low
    return FibonacciLevels(
        pivot=round(pivot, 4),
        s1=round(pivot - r * 0.236, 4),
        s2=round(pivot - r * 0.382, 4),
        s3=round(pivot - r * 0.618, 4),
        r1=round(pivot + r * 0.236, 4),
        r2=round(pivot + r * 0.382, 4),
        r3=round(pivot + r * 0.618, 4),
    )


def compute_stochastic(candles: list[Candle], k_period: int = 14, d_period: int = 3) -> Optional[StochasticResult]:
    if len(candles) < k_period + d_period:
        return None
    k_values: list[float] = []
    for i in range(len(candles) - d_period, len(candles)):
        sl = candles[i - k_period + 1: i + 1]
        high = max(c.high for c in sl)
        low = min(c.low for c in sl)
        close = candles[i].close
        k_values.append(50.0 if high == low else ((close - low) / (high - low)) * 100)
    k = k_values[-1]
    d = sum(k_values) / len(k_values)
    return StochasticResult(k=round(k, 1), d=round(d, 1))


def compute_adx(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period * 2 + 1:
        return None
    plus_dms: list[float] = []
    minus_dms: list[float] = []
    trs: list[float] = []

    for i in range(1, len(candles)):
        up_move = candles[i].high - candles[i - 1].high
        down_move = candles[i - 1].low - candles[i].low
        plus_dms.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dms.append(down_move if down_move > up_move and down_move > 0 else 0)
        tr = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
        trs.append(tr)

    if len(trs) < period:
        return None

    atr_smooth = sum(trs[:period])
    plus_dm_smooth = sum(plus_dms[:period])
    minus_dm_smooth = sum(minus_dms[:period])
    dx_values: list[float] = []

    for i in range(period, len(trs)):
        atr_smooth = atr_smooth - atr_smooth / period + trs[i]
        plus_dm_smooth = plus_dm_smooth - plus_dm_smooth / period + plus_dms[i]
        minus_dm_smooth = minus_dm_smooth - minus_dm_smooth / period + minus_dms[i]
        plus_di = (plus_dm_smooth / atr_smooth) * 100 if atr_smooth > 0 else 0
        minus_di = (minus_dm_smooth / atr_smooth) * 100 if atr_smooth > 0 else 0
        di_sum = plus_di + minus_di
        dx = (abs(plus_di - minus_di) / di_sum) * 100 if di_sum > 0 else 0
        dx_values.append(dx)

    if len(dx_values) < period:
        return None
    adx = sum(dx_values[-period:]) / period
    # Return ADX + last +DI/-DI values
    return round(adx, 1), round(plus_di, 1), round(minus_di, 1)


def compute_vwap(candles: list[Candle]) -> Optional[float]:
    """Compute Volume Weighted Average Price over the available candles.
    VWAP = Σ(Typical Price × Volume) / Σ(Volume)
    Typical Price = (High + Low + Close) / 3
    Useful for indices to determine if price is trading above/below fair value.
    """
    if len(candles) < 5:
        return None
    cum_tp_vol = 0.0
    cum_vol = 0.0
    for c in candles:
        typical_price = (c.high + c.low + c.close) / 3.0
        cum_tp_vol += typical_price * c.volume
        cum_vol += c.volume
    if cum_vol <= 0:
        return None
    return round(cum_tp_vol / cum_vol, 4)


def compute_all_indicators(candles: list[Candle]) -> TechnicalIndicators:
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    volume_avg20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
    current_vol = volumes[-1] if volumes else 0

    _adx_result = compute_adx(candles, 14)

    # MACD histogram from 3 candles ago (declining momentum detection)
    _macd_prev = compute_macd(closes[:-3]) if len(closes) > 30 else None
    _macd_hist_prev = _macd_prev.histogram if _macd_prev else None

    # RSI from 5 candles ago (exhaustion detection)
    _rsi_prev5 = compute_rsi(closes[:-5], 14) if len(closes) > 25 else None

    return TechnicalIndicators(
        rsi14=compute_rsi(closes, 14),
        rsi_prev5=_rsi_prev5,
        macd=compute_macd(closes),
        macd_hist_prev=_macd_hist_prev,
        bollinger_bands=compute_bollinger_bands(closes, 20, 2),
        sma20=compute_sma(closes, 20),
        sma50=compute_sma(closes, 50),
        ema12=compute_ema(closes, 12),
        ema26=compute_ema(closes, 26),
        atr14=compute_atr(candles, 14),
        fibonacci=compute_fibonacci(candles),
        volume_avg20=volume_avg20,
        volume_ratio=round(current_vol / volume_avg20, 2) if volume_avg20 and volume_avg20 > 0 else None,
        stochastic=compute_stochastic(candles, 14, 3),
        adx=_adx_result[0] if _adx_result else None,
        plus_di=_adx_result[1] if _adx_result else None,
        minus_di=_adx_result[2] if _adx_result else None,
        vwap=compute_vwap(candles),
    )
