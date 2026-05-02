"""
Structure detector M15 (2026-04-23) — scalping M-only architecture.

User validated 2026-04-23 — post-audit journée −18.84€ :
  Direction ← M15 structure (HH/LL sur 10 bars)  ← CE MODULE
  Regime    ← ADX M15 + BB width M15              ← CE MODULE
  Box       ← 40 M5 bars (range_filters)
  Trigger   ← M1 breakout (scalping_filters)
  H1        ← news/session timing uniquement, PAS direction
  D1        ← contexte seulement, pas direction

Bénéfice : on trade CE QUE LE MARCHÉ FAIT MAINTENANT (M15), pas ce que H1
macro disait il y a 2-4h. Idéal pour scalping 5-15 min.
"""
from dataclasses import dataclass
from typing import Optional, Literal

from app.trading.indicators import Candle, compute_adx, compute_bollinger_bands


StructureKind = Literal["bullish", "bearish", "neutral"]
RegimeKind = Literal["trend", "range", "none"]


@dataclass
class StructureResult:
    structure: StructureKind
    avg_high_first: float
    avg_high_second: float
    avg_low_first: float
    avg_low_second: float
    reason: str = ""

    def summary(self) -> str:
        return (
            f"structure={self.structure} "
            f"highs {self.avg_high_first:.5f}→{self.avg_high_second:.5f} "
            f"lows {self.avg_low_first:.5f}→{self.avg_low_second:.5f} "
            f"{self.reason}"
        )


@dataclass
class RegimeM15Result:
    regime: RegimeKind
    adx: Optional[float]
    bb_width_pct: Optional[float]
    reason: str = ""

    def summary(self) -> str:
        parts = [f"regime_m15={self.regime}"]
        if self.adx is not None:
            parts.append(f"ADX_M15={self.adx:.1f}")
        if self.bb_width_pct is not None:
            parts.append(f"BBw_M15={self.bb_width_pct:.2f}%")
        if self.reason:
            parts.append(self.reason)
        return " ".join(parts)


def detect_m15_structure(
    candles_m15: Optional[list[Candle]],
    lookback: int = 10,
    threshold_pct: float = 0.0005,  # 5 bps de variation = signal
) -> StructureResult:
    """Détecte la structure (HH/HL = bullish, LH/LL = bearish, autre = neutral) sur
    les 10 dernières bougies M15 (= 2h30 de contexte).

    Méthode : moyenne des highs / lows sur première moitié vs seconde moitié du
    lookback. Si second > first significativement sur les DEUX → bullish. L'inverse
    → bearish. Sinon neutral (signal direction = pas de confiance)."""
    if not candles_m15 or len(candles_m15) < lookback:
        return StructureResult(
            structure="neutral",
            avg_high_first=0, avg_high_second=0,
            avg_low_first=0, avg_low_second=0,
            reason=f"M15 insuffisant ({len(candles_m15) if candles_m15 else 0}/{lookback})",
        )
    recent = candles_m15[-lookback:]
    half = lookback // 2
    first = recent[:half]
    second = recent[half:]

    avg_h1 = sum(c.high for c in first) / len(first)
    avg_h2 = sum(c.high for c in second) / len(second)
    avg_l1 = sum(c.low for c in first) / len(first)
    avg_l2 = sum(c.low for c in second) / len(second)

    # 4 signaux structurels indépendants
    hh = avg_h2 > avg_h1 * (1 + threshold_pct)   # Higher Highs
    hl = avg_l2 > avg_l1 * (1 + threshold_pct)   # Higher Lows
    lh = avg_h2 < avg_h1 * (1 - threshold_pct)   # Lower Highs
    ll = avg_l2 < avg_l1 * (1 - threshold_pct)   # Lower Lows

    def _mk(struct, reason):
        return StructureResult(
            structure=struct,
            avg_high_first=avg_h1, avg_high_second=avg_h2,
            avg_low_first=avg_l1, avg_low_second=avg_l2,
            reason=reason,
        )

    # EXPANSION : HH + LL (highs montent ET lows cassent) = volatilité sans direction
    if hh and ll:
        return _mk("neutral", "expansion HH+LL (volatilité sans direction)")
    # COMPRESSION : LH + HL (highs baissent ET lows montent) = resserrement, range
    if lh and hl:
        return _mk("neutral", "compression LH+HL (range resserré)")

    # BULLISH — HH et/ou HL sans contradiction (pas de LL)
    if hh and hl:
        return _mk("bullish", f"HH + HL confirmé sur {lookback} M15")
    if hh:  # HH seul (lows stables ou hausse)
        return _mk("bullish", f"HH seul (lows {'stables' if not (hl or ll) else '+'}) sur {lookback} M15")
    if hl:  # HL seul (highs stables ou hausse)
        return _mk("bullish", f"HL seul (highs {'stables' if not (hh or lh) else '+'}) sur {lookback} M15")

    # BEARISH — LH et/ou LL sans contradiction (pas de HH)
    if lh and ll:
        return _mk("bearish", f"LH + LL confirmé sur {lookback} M15")
    if ll:
        return _mk("bearish", f"LL seul (highs {'stables' if not (hh or lh) else '-'}) sur {lookback} M15")
    if lh:
        return _mk("bearish", f"LH seul (lows {'stables' if not (hl or ll) else '-'}) sur {lookback} M15")

    return _mk("neutral", "aucun signal structurel (consolidation plate)")


def detect_regime_m15(
    candles_m15: Optional[list[Candle]],
    adx_trend_min: float = 24.0,
    bb_width_range_max_pct: float = 0.8,  # M15 BB typiquement plus serrée que H1
) -> RegimeM15Result:
    """Regime sur M15 (au lieu de H1).
    - Trend si ADX M15 >= 24
    - Range si ADX < 24 ET BB_width_M15 < 0.8% (plus strict que H1 2% car M15 bouge moins)
    - None sinon (données insuffisantes)."""
    if not candles_m15 or len(candles_m15) < 30:
        return RegimeM15Result(
            regime="none", adx=None, bb_width_pct=None,
            reason=f"M15 insuffisant ({len(candles_m15) if candles_m15 else 0}/30)",
        )
    closes = [c.close for c in candles_m15]
    _adx_raw = compute_adx(candles_m15, period=14)
    if _adx_raw is None:
        adx = None
    elif isinstance(_adx_raw, tuple):
        adx = _adx_raw[0]
    else:
        adx = _adx_raw
    bb = compute_bollinger_bands(closes, period=20, multiplier=2.0)
    bb_width_pct = None
    if bb and bb.middle > 0:
        bb_width_pct = (bb.upper - bb.lower) / bb.middle * 100.0

    if adx is None:
        return RegimeM15Result(
            regime="none", adx=None, bb_width_pct=bb_width_pct,
            reason="ADX M15 indisponible",
        )
    if adx >= adx_trend_min:
        return RegimeM15Result(
            regime="trend", adx=adx, bb_width_pct=bb_width_pct,
            reason=f"ADX_M15 {adx:.1f}>={adx_trend_min:.0f}",
        )
    # adx < 24 → range (on n'exige pas BBw pour rester permissif, comme H1)
    return RegimeM15Result(
        regime="range", adx=adx, bb_width_pct=bb_width_pct,
        reason=f"ADX_M15 {adx:.1f}<{adx_trend_min:.0f}",
    )
