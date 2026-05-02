"""
Filter constants — single source of truth for thresholds shared between
scalping_filters.py (trend pipeline) and range_filters.py (range pipeline).

Change a value HERE ONLY — all call sites import from this module.

Created 2026-04-23 after a duplicated-threshold bug: 0.90 / 0.60 / 1.20 were
inlined in both scalping_filters and range_filters. A single constant file
prevents drift when the user tunes thresholds.
"""

# ── Volume (M5 tick_volume ratio vs rolling median, 20 bars) ──────────────
#
# Paliers utilisés par check_volume_spike (trend pipeline) :
#   ratio >= VOLUME_FULL_RATIO        → OK, size_factor = 1.0 (taille pleine)
#   VOLUME_MIN_RATIO <= ratio < FULL  → OK, size_factor = SIZE_REDUCED
#   ratio < VOLUME_MIN_RATIO          → FAIL (momentum insuffisant)
#
# Range pipeline utilise VOLUME_MIN_RATIO comme seuil minimum (pas de palier,
# le range demande une liquidité minimale, pas un palier pour la taille).

VOLUME_MIN_RATIO: float = 0.90   # 0.9× médiane = plancher liquidité (TREND pipeline)
VOLUME_FULL_RATIO: float = 1.20  # 1.2× médiane = taille pleine (TREND only)

# Range pipeline — BANDE volume avec 2 paliers (user-validated 2026-04-23) :
#   < VOLUME_MIN_RATIO_RANGE          → marché mort, pas de liquidité
#   [0.30, 0.50)                       → OK, taille 40% (SIZE_REDUCED_LOW)
#   [0.50, 0.85]                       → OK, taille 60% (SIZE_REDUCED)
#   > VOLUME_MAX_RATIO_RANGE          → trop fort, c'est un trend qui démarre
# 2026-04-23 expert standards : plafond serré à 0.85 (au-delà = trend imminent).
VOLUME_MIN_RATIO_RANGE: float = 0.30
VOLUME_MID_RATIO_RANGE: float = 0.50
VOLUME_MAX_RATIO_RANGE: float = 0.85

# ── Size factor (fraction de la taille nominale) ──────────────────────────
#
# Utilisé par :
#   - trend pipeline (palier volume 0.9-1.2× → taille réduite)
#   - range pipeline (toute entrée range → taille réduite par design)

SIZE_REDUCED: float = 0.60       # 60% — palier trend 0.9-1.2× (user-validated 2026-04-23)
SIZE_REDUCED_LOW: float = 0.40   # 40% — palier range bas 0.3-0.5× (user-validated 2026-04-23)

# ── M5 body ratio (trend only) ────────────────────────────────────────────
#
# Minimum fraction du range couverte par le corps de la bougie M5 pour valider
# le momentum (vs doji/indécis).

M5_BODY_MIN_RATIO: float = 0.60

# ── M1 filter (trend only) ────────────────────────────────────────────────
#
# Body ratio alternatif au breakout strict sur M1 : permet d'entrer sur une
# bougie à corps plein (>=70% du range) même sans casser le max/min des 2
# précédentes. Évite l'entrée en fin de cascade.

M1_FULL_BODY_RATIO: float = 0.70

# ── Range pipeline RSI gates (range only) ─────────────────────────────────
#
# RSI M5 seuils pour valider qu'on est bien en extension dans le range
# (pas déjà revenu vers le milieu).

# 2026-04-23 expert standards : 30/70 (classique survente/surachat strict)
RANGE_RSI_BUY_MAX: float = 30.0   # RSI <= 30 pour BUY bas du range (survente)
RANGE_RSI_SELL_MIN: float = 70.0  # RSI >= 70 pour SELL haut du range (surachat)

# ── Range pipeline geometry ───────────────────────────────────────────────
#
# Zone d'entrée = X% du range à partir de la borne.
# SL padding  = Y% du range hors des bornes.
# R:R minimum = Z.

# 2026-04-23 expert standards :
#   - Zone 15% (plus serrée = oblige entrée plus proche de la borne → meilleur R:R)
#   - SL padding 15% (équilibre protection vs taille de SL)
#   - TP target 80% vers la borne opposée (captures la majeure partie du range)
#   - R:R min 2.0 (standard expert)
RANGE_ENTRY_ZONE_PCT: float = 0.15
RANGE_SL_PADDING_PCT: float = 0.15
RANGE_TP_TARGET_PCT: float = 0.80      # TP = 80% de la distance vers borne opposée
RANGE_RR_MIN: float = 2.0

# ── Regime detector thresholds (H1) ───────────────────────────────────────

REGIME_ADX_TREND_MIN: float = 24.0
# 2026-04-23 user-validated : range band ne s'arrête pas à 18, elle va jusqu'à
# REGIME_ADX_TREND_MIN (pas de gap). ADX 22.3 = range, pas 'none ambigu'.
REGIME_ADX_RANGE_MAX: float = 24.0
REGIME_BBWIDTH_RANGE_MAX_PCT: float = 2.0
REGIME_SLOPE_FLAT_THRESHOLD_PCT: float = 0.05
REGIME_RANGE_LOOKBACK: int = 30          # H1 bars (30 = 30h de contexte)
# 2026-04-23 : box M5 pour ranges court-terme visibles à l'œil (DAX/CAC/EURAUD
# consolident typiquement sur 2-4h après un move directionnel). 40 bars M5 =
# 3h20 de contexte — exclut les extrêmes anciens qui élargiraient la box.
# User-validated : 80 trop large (captait la chute matinale), 40 coller mieux
# à la zone de consolidation actuelle.
REGIME_RANGE_LOOKBACK_M5: int = 40

# ── Session activation flags ─────────────────────────────────────────────
#
# 2026-04-23 — user demande "désactiver Asie la nuit" : aucun trade pendant Tokyo/Sydney.
# Désactive la détection Liquidity Candle ASIA (23h UTC) ET bloque les symboles
# principalement asiatiques (NKY/HK50/AUS200/AUD*/NZD*) en dehors du global window.
# TRADING_WINDOW_CET dans signals.py gouverne le main scan loop (déjà 9h-21h → Asia off côté 4TF).
# Ce flag neutralise UNIQUEMENT la pipeline Liquidity Candle pour la bucket ASIA.

ASIA_SESSION_ENABLED: bool = False
