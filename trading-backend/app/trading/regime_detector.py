"""
Regime detector — classifies each symbol as TREND / RANGE / NONE based on H1 data.

Created 2026-04-23. Goal: route each signal to the appropriate pipeline so the
trend-only 4TF filters don't starve during ranging markets (and vice versa).

Seuils centralisés dans filters_config.py (REGIME_ADX_TREND_MIN / REGIME_ADX_RANGE_MAX /
REGIME_BBWIDTH_RANGE_MAX_PCT / REGIME_SLOPE_FLAT_THRESHOLD_PCT / REGIME_RANGE_LOOKBACK).

Decision logic:
  - TREND  : ADX_H1 >= REGIME_ADX_TREND_MIN  AND  SMA50 H1 slope non plat
  - RANGE  : ADX_H1 <  REGIME_ADX_RANGE_MAX  AND  BB_width_H1 < REGIME_BBWIDTH_RANGE_MAX_PCT
  - NONE   : entre les deux → skip (régime ambigu)

Range box (used only when regime == "range"):
  range_high / range_low = max/min sur REGIME_RANGE_LOOKBACK dernières H1.
"""
from dataclasses import dataclass
from typing import Optional, Literal

from app.trading.indicators import Candle, compute_adx, compute_sma, compute_bollinger_bands
from app.trading.filters_config import (
    REGIME_ADX_TREND_MIN, REGIME_ADX_RANGE_MAX, REGIME_BBWIDTH_RANGE_MAX_PCT,
    REGIME_SLOPE_FLAT_THRESHOLD_PCT, REGIME_RANGE_LOOKBACK,
)


RegimeKind = Literal["trend", "range", "none"]


@dataclass
class RegimeResult:
    regime: RegimeKind
    adx: Optional[float]
    bb_width_pct: Optional[float]
    sma50_slope_pct: Optional[float]
    range_high: Optional[float] = None
    range_low: Optional[float] = None
    reason: str = ""

    def summary(self) -> str:
        parts = [f"regime={self.regime}"]
        if self.adx is not None:
            parts.append(f"ADX={self.adx:.1f}")
        if self.bb_width_pct is not None:
            parts.append(f"BBw={self.bb_width_pct:.2f}%")
        if self.sma50_slope_pct is not None:
            parts.append(f"slope={self.sma50_slope_pct:+.3f}%")
        if self.range_high and self.range_low:
            parts.append(f"box=[{self.range_low:.5f}..{self.range_high:.5f}]")
        if self.reason:
            parts.append(self.reason)
        return " ".join(parts)


def detect_regime(
    candles_h1: Optional[list[Candle]],
    adx_trend_min: float = REGIME_ADX_TREND_MIN,
    adx_range_max: float = REGIME_ADX_RANGE_MAX,
    bb_width_range_max_pct: float = REGIME_BBWIDTH_RANGE_MAX_PCT,
    slope_flat_threshold_pct: float = REGIME_SLOPE_FLAT_THRESHOLD_PCT,
    range_lookback: int = REGIME_RANGE_LOOKBACK,
) -> RegimeResult:
    """
    Return regime classification for a symbol based on H1 candles.

    Args:
      candles_h1: H1 candles (>= 50 required for SMA50, >= 20 for BB/ADX)
      adx_trend_min: ADX threshold above which we consider trend confirmed
      adx_range_max: ADX threshold below which we consider range
      bb_width_range_max_pct: BB_width in % of price below which price is compressed
      slope_flat_threshold_pct: |slope| below this = flat (no trend even if ADX high)
      range_lookback: number of H1 bars to compute range_high / range_low
    """
    if not candles_h1 or len(candles_h1) < 50:
        return RegimeResult(
            regime="none", adx=None, bb_width_pct=None, sma50_slope_pct=None,
            reason=f"H1 insuffisant ({len(candles_h1) if candles_h1 else 0}/50)"
        )

    closes = [c.close for c in candles_h1]
    last_price = closes[-1]
    if last_price <= 0:
        return RegimeResult("none", None, None, None, reason="prix H1 invalide")

    # compute_adx retourne (adx, plus_di, minus_di) malgré son type hint Optional[float].
    _adx_raw = compute_adx(candles_h1, period=14)
    if _adx_raw is None:
        adx = None
    elif isinstance(_adx_raw, tuple):
        adx = _adx_raw[0]  # premier élément = ADX
    else:
        adx = _adx_raw
    bb = compute_bollinger_bands(closes, period=20, multiplier=2.0)
    sma50_now = compute_sma(closes, 50)
    sma50_prev = compute_sma(closes[:-10], 50) if len(closes) >= 60 else None

    bb_width_pct = None
    if bb and bb.middle > 0:
        bb_width_pct = (bb.upper - bb.lower) / bb.middle * 100.0

    slope_pct = None
    if sma50_now and sma50_prev and sma50_prev > 0:
        # Slope = variation SMA50 sur les 10 dernières barres, normalisée par SMA50
        slope_pct = (sma50_now - sma50_prev) / sma50_prev * 100.0

    # 2026-04-23 user rule simple :
    #   Range  = ADX < 24 (pas de gate BBw)
    #   Trend  = ADX >= 24 AND slope confirmant
    #   None   = ADX >= 24 mais slope plat (faux trend) OU data manquante

    if adx is None:
        return RegimeResult(
            regime="none", adx=None, bb_width_pct=bb_width_pct, sma50_slope_pct=slope_pct,
            reason="ADX indisponible (données insuffisantes)"
        )

    # TREND — ADX fort + slope confirmant
    if adx >= adx_trend_min:
        if slope_pct is None or abs(slope_pct) >= slope_flat_threshold_pct:
            return RegimeResult(
                regime="trend", adx=adx, bb_width_pct=bb_width_pct, sma50_slope_pct=slope_pct,
                reason=f"ADX {adx:.1f}>={adx_trend_min:.0f} + slope OK"
            )
        return RegimeResult(
            regime="none", adx=adx, bb_width_pct=bb_width_pct, sma50_slope_pct=slope_pct,
            reason=f"ADX {adx:.1f} mais slope {slope_pct:+.3f}% trop plat (faux trend)"
        )

    # RANGE — ADX faible/modéré, box calculée sur N dernières bougies H1
    recent = candles_h1[-range_lookback:]
    r_high = max(c.high for c in recent)
    r_low = min(c.low for c in recent)
    return RegimeResult(
        regime="range", adx=adx, bb_width_pct=bb_width_pct, sma50_slope_pct=slope_pct,
        range_high=r_high, range_low=r_low,
        reason=f"ADX {adx:.1f}<{adx_trend_min:.0f} = range"
    )
