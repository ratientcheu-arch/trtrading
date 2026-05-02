"""
Symbol selector (2026-04-23) — 3 niveaux de filtrage pour ne trader QUE les paires/indices
les plus adaptés au moment présent.

B) WHITELIST : liste fermée de symboles historiquement rentables (exclut tout le reste)
D) QUALITY SCORE : score dynamique recalculé toutes les heures (ATR + spread + volume)
F) PREFERRED REGIME : chaque symbole a son régime de prédilection (trend vs range)

User-validated 2026-04-23.
"""
from dataclasses import dataclass
from typing import Optional, Literal

from app.trading.indicators import Candle, compute_atr


# ═══════════════════════════════════════════════════════════════════════
# B) WHITELIST PAR SESSION — 5 forex + 3 indices max actifs à chaque moment
# ═══════════════════════════════════════════════════════════════════════
# Règle user 2026-04-23 : "pour chaque marché 5 paires et 3 indices".
# Sélection basée sur : profitabilité historique + volatilité + liquidité.
# ASIA entièrement désactivée (user).

SESSION_WHITELIST: dict[str, dict] = {
    "EU": {
        # 9h-15h30 CET — session européenne pure
        # Forex 5 plus profitables/volatils en matinée EU
        "forex": {"EUR/GBP", "EUR/JPY", "GBP/JPY", "GBP/USD", "EUR/USD"},
        # Indices EU 3 (DAX/CAC/UK100 ouverts, très liquides)
        "indices": {"DAX40", "CAC40", "UK100"},
    },
    "US": {
        # 15h30-22h CET — session US (et overlap EU jusqu'à 17h30)
        # Forex 5 : majors avec USD (liquidity peak)
        "forex": {"EUR/USD", "GBP/USD", "AUD/USD", "USD/CAD", "USD/CHF"},
        # Indices 3 actifs : user 2026-04-23 veut DAX/CAC dispos car ouverts
        # jusqu'à 17h30. DAX/CAC = excellents en overlap EU-US (15h30-17h30).
        # Après 17h30 : market_hours check fera le filtre (DAX/CAC market=fermé).
        # SP500/NASDAQ actifs toute la session US. On met les 5 dans la whitelist,
        # la limite "3 indices simultanés" viendra de l'ouverture de marché par
        # symbole (is_market_open) + quality score top 8.
        "indices": {"DAX40", "CAC40", "SP500", "NASDAQ", "UK100"},
    },
    "ASIA": {
        # DÉSACTIVÉE — user a explicitement demandé pas de trading la nuit
        "forex": set(),
        "indices": set(),
    },
}


def get_current_session(cet_hour: float) -> str:
    """Retourne la session active selon l'heure CET.
    EU 8h-15h30 (global window start) | US 15h30-22h | ASIA 22h-8h (désactivée).
    8h-9h = forex only (DAX/CAC filtrés par is_market_open)."""
    if 8.0 <= cet_hour < 15.5:
        return "EU"
    if 15.5 <= cet_hour < 22.0:
        return "US"
    return "ASIA"  # disabled (22h-8h)


def get_session_whitelist(cet_hour: float) -> set[str]:
    """Retourne l'union forex + indices pour la session active."""
    session = get_current_session(cet_hour)
    sw = SESSION_WHITELIST.get(session, {"forex": set(), "indices": set()})
    return sw.get("forex", set()) | sw.get("indices", set())


def is_whitelisted(symbol: str, cet_hour: Optional[float] = None) -> bool:
    """True si le symbole est dans la whitelist de la session active.
    Si cet_hour=None (backward compat), accepte l'union de toutes les sessions actives."""
    if not symbol:
        return False
    s = symbol.upper()
    if cet_hour is not None:
        wl = get_session_whitelist(cet_hour)
    else:
        # Fallback : union EU + US (ASIA exclue)
        wl = SESSION_WHITELIST["EU"]["forex"] | SESSION_WHITELIST["EU"]["indices"] \
             | SESSION_WHITELIST["US"]["forex"] | SESSION_WHITELIST["US"]["indices"]
    wl_normalized = {w.upper() for w in wl} | {w.upper().replace("/", "") for w in wl}
    return s in wl_normalized or s.replace("/", "") in wl_normalized


# ═══════════════════════════════════════════════════════════════════════
# F) PREFERRED REGIME — compatibilité symbole ↔ régime détecté
# ═══════════════════════════════════════════════════════════════════════
# "trend"  : symbole plus profitable en tendance (volatilité directionnelle)
# "range"  : symbole plus profitable en range (oscille bien entre supports/résistances)
# "both"   : aucune préférence forte, accepté dans les 2 régimes

PREFERRED_REGIME: dict[str, Literal["trend", "range", "both"]] = {
    # Forex — pairs avec fort carry/moment = trend
    "GBP/JPY": "trend",      # Volatilité JPY × GBP, souvent directionnel
    "EUR/JPY": "trend",      # idem
    "AUD/JPY": "trend",      # carry trade
    "GBP/USD": "both",       # scalpable les 2 manières
    "EUR/USD": "range",      # EU majeur, oscille en session EU
    "EUR/GBP": "range",      # 2 devises EU, gamme serrée
    "AUD/USD": "trend",      # commodity-linked
    "NZD/USD": "trend",      # idem
    "USD/CAD": "both",
    "USD/CHF": "both",
    "EUR/CAD": "range",
    "EUR/AUD": "both",
    "GBP/AUD": "trend",
    # Indices
    # 2026-04-24: DAX40 & CAC40 passés de "range" à "both" — marché EU parfois en trend
    # franc (M15 bearish LH+LL ADX>24) et le filtre range bloquait les signaux légitimes.
    "DAX40": "both",
    "CAC40": "both",
    "UK100": "both",
    # Commodities
    "GOLD": "trend",         # tendance macro dominante
    "OIL_CRUDE": "trend",    # volatil sur news EIA
}


def is_regime_compatible(symbol: str, regime: str) -> bool:
    """True si le symbole est compatible avec le régime actuel (F).
    Si symbole absent du dict → 'both' par défaut (permissif)."""
    if not symbol or regime not in ("trend", "range"):
        return True  # no filter
    # Normalisation avec ou sans slash
    key = symbol
    if key not in PREFERRED_REGIME:
        key = symbol.replace("/", "")
        if key not in PREFERRED_REGIME:
            # Essayer reverse : "EURUSD" -> "EUR/USD"
            if len(key) == 6 and key.isalpha():
                key = f"{key[:3]}/{key[3:]}"
    pref = PREFERRED_REGIME.get(key, "both")
    if pref == "both":
        return True
    return pref == regime


# ═══════════════════════════════════════════════════════════════════════
# D) QUALITY SCORE — score dynamique recalculé toutes les heures
# ═══════════════════════════════════════════════════════════════════════
# Score composé de 3 facteurs (poids égaux) :
#   - Volatilité normalisée : ATR M15 / prix (plus = mieux pour TP)
#   - Spread : spread relatif (moins = mieux, inversé dans score)
#   - Activité : volume médian M5 relatif (plus = mieux)
# Plus score élevé = symbole plus intéressant à scalper maintenant.

@dataclass
class QualityScore:
    symbol: str
    score: float
    atr_pct: float
    spread_pct: float
    vol_score: float
    reason: str = ""

    def __str__(self) -> str:
        return (
            f"{self.symbol}: score={self.score:.2f} "
            f"(atr={self.atr_pct:.2f}% spread={self.spread_pct:.3f}% vol={self.vol_score:.2f})"
        )


def compute_quality_score(
    symbol: str,
    candles_m15: Optional[list[Candle]],
    candles_m5: Optional[list[Candle]],
    spread: float,
    price: float,
) -> Optional[QualityScore]:
    """Score de qualité [0..100]. None si données insuffisantes."""
    if not candles_m15 or len(candles_m15) < 15 or not candles_m5 or len(candles_m5) < 20:
        return None
    if price <= 0:
        return None

    # 1. Volatilité : ATR M15 normalisé par prix
    _atr = compute_atr(candles_m15, period=14)
    if _atr is None or _atr <= 0:
        return None
    atr_pct = (_atr / price) * 100.0
    # Volatilité idéale scalping : 0.05% à 0.30% (ATR M15)
    # - Trop bas (<0.05%) = pas assez de mouvement pour TP
    # - Trop haut (>0.5%) = SL touché vite
    if atr_pct < 0.03:
        vol_factor = 20  # bas mais pas exclu
    elif atr_pct < 0.05:
        vol_factor = 60
    elif atr_pct < 0.30:
        vol_factor = 100  # sweet spot
    elif atr_pct < 0.60:
        vol_factor = 70
    else:
        vol_factor = 30  # trop volatil

    # 2. Spread : plus c'est bas mieux c'est (inversé)
    spread_pct = (spread / price) * 100.0 if spread > 0 else 0.01
    # Scalping : <0.02% excellent, 0.02-0.05% OK, >0.05% pénalisant
    if spread_pct < 0.01:
        spread_factor = 100
    elif spread_pct < 0.03:
        spread_factor = 85
    elif spread_pct < 0.05:
        spread_factor = 60
    elif spread_pct < 0.10:
        spread_factor = 30
    else:
        spread_factor = 10

    # 3. Volume M5 : activité relative
    vols = [c.volume for c in candles_m5[-20:]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    vol_last = candles_m5[-2].volume if len(candles_m5) >= 2 else candles_m5[-1].volume
    vol_ratio = (vol_last / avg_vol) if avg_vol > 0 else 0
    # Volume ratio 0.8-1.5 = normal, <0.3 = mort, >2 = news
    if vol_ratio < 0.3:
        vol_score = 20
    elif vol_ratio < 0.8:
        vol_score = 70
    elif vol_ratio < 1.5:
        vol_score = 100
    elif vol_ratio < 2.5:
        vol_score = 75
    else:
        vol_score = 40

    # Score final = moyenne pondérée (équipondéré)
    score = (vol_factor + spread_factor + vol_score) / 3.0
    return QualityScore(
        symbol=symbol,
        score=round(score, 2),
        atr_pct=round(atr_pct, 3),
        spread_pct=round(spread_pct, 4),
        vol_score=round(vol_ratio, 2),
        reason=f"atr={atr_pct:.2f}% spread={spread_pct:.3f}% vol_r={vol_ratio:.2f}",
    )


def select_top_symbols(
    scores: list[QualityScore],
    top_n: int = 8,
    min_score: float = 50.0,
) -> list[str]:
    """Retourne le top N symboles par score, filtrés par min_score."""
    filtered = [s for s in scores if s.score >= min_score]
    filtered.sort(key=lambda x: x.score, reverse=True)
    return [s.symbol for s in filtered[:top_n]]
