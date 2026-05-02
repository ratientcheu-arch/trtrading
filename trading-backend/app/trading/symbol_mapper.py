"""
Symbol mapper — Forex, Indices & Commodities.
Single broker: Fusion Markets via MT5 (ZMQ bridge).
"""
from dataclasses import dataclass


@dataclass
class AssetInfo:
    symbol: str
    name: str
    asset_type: str
    market: str
    min_quantity: float = 1000


# ═══════════════════════════════════════════════════════════════════════
# ALL TRADEABLE ASSETS — Capital.com primary
# ═══════════════════════════════════════════════════════════════════════
ASSETS: list[AssetInfo] = [
    # ── Forex majors & crosses ────────────────────────────────────────
    # BLACKLISTÉS (DISABLED_SYMBOLS analyse 7J → perdants chroniques):
    #   EUR/USD (worst performer), USD/JPY, EUR/CHF, AUD/CAD (signaux non fiables)
    AssetInfo("GBP/USD", "Livre Sterling / Dollar US", "forex", "FOREX"),
    AssetInfo("EUR/GBP", "Euro / Livre Sterling", "forex", "FOREX"),
    AssetInfo("AUD/USD", "Dollar Australien / Dollar US", "forex", "FOREX"),
    AssetInfo("NZD/USD", "Dollar Neo-Zelandais / Dollar US", "forex", "FOREX"),
    AssetInfo("EUR/JPY", "Euro / Yen Japonais", "forex", "FOREX"),
    AssetInfo("GBP/JPY", "Livre Sterling / Yen Japonais", "forex", "FOREX"),
    AssetInfo("EUR/CAD", "Euro / Dollar Canadien", "forex", "FOREX"),
    AssetInfo("EUR/AUD", "Euro / Dollar Australien", "forex", "FOREX"),
    AssetInfo("GBP/AUD", "Livre / Dollar Australien", "forex", "FOREX"),
    AssetInfo("AUD/JPY", "Dollar Australien / Yen", "forex", "FOREX"),
    AssetInfo("AUD/CHF", "Dollar Australien / Franc Suisse", "forex", "FOREX"),
    AssetInfo("AUD/NZD", "Dollar Australien / Dollar Neo-Zelandais", "forex", "FOREX"),
    AssetInfo("USD/CAD", "Dollar US / Dollar Canadien", "forex", "FOREX"),
    AssetInfo("USD/CHF", "Dollar US / Franc Suisse", "forex", "FOREX"),
    # 2026-04-27: paires manquantes du frontend → ajoutées
    AssetInfo("USD/JPY", "Dollar US / Yen Japonais", "forex", "FOREX"),
    AssetInfo("EUR/CHF", "Euro / Franc Suisse", "forex", "FOREX"),
    AssetInfo("GBP/NZD", "Livre / Dollar Neo-Zelandais", "forex", "FOREX"),
    AssetInfo("CAD/JPY", "Dollar Canadien / Yen Japonais", "forex", "FOREX"),
    AssetInfo("NZD/JPY", "Dollar Neo-Zelandais / Yen Japonais", "forex", "FOREX"),
    AssetInfo("GBP/CAD", "Livre / Dollar Canadien", "forex", "FOREX"),

    # ── Indices — Fusion Markets CFD (SL/TP sur broker, sizing correct) ──
    # 2026-04-20: SP500 et NASDAQ RÉACTIVÉS pour tester avec filtres 4TF Variant B
    AssetInfo("DAX40", "DAX 40 (Allemagne)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("CAC40", "CAC 40 (France)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("NKY", "Nikkei 225 (Tokyo)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("HK50", "Hang Seng 50 (Hong Kong)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("AUS200", "ASX 200 (Australie)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("UK100", "FTSE 100 (UK)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("SP500", "S&P 500 (US)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("NASDAQ", "NASDAQ 100 (US)", "index_cfd", "INDICES", min_quantity=0.5),
    AssetInfo("US30", "DOW 30 (US)", "index_cfd", "INDICES", min_quantity=0.5),

    # ── Commodities — RÉACTIVÉES 2026-04-20 (GOLD + OIL_CRUDE uniquement) ──
    # GOLD = valeur refuge, trends clairs en session US. OIL = volatilité EIA/OPEC.
    AssetInfo("GOLD", "Gold Spot (XAU/USD)", "commodity", "COMMODITY", min_quantity=0.01),
    AssetInfo("OIL_CRUDE", "Crude Oil (WTI)", "commodity", "COMMODITY", min_quantity=0.1),

    # ── Stocks — PAS disponibles sur le compte MT5 Fusion (désactivés) ──
    # AssetInfo("AAPL", "Apple Inc.", "stock", "US", min_quantity=0.01),
    # AssetInfo("TSLA", "Tesla Inc.", "stock", "US", min_quantity=0.01),
    # AssetInfo("MSFT", "Microsoft Corp.", "stock", "US", min_quantity=0.01),
    # AssetInfo("NVDA", "NVIDIA Corp.", "stock", "US", min_quantity=0.01),
    # AssetInfo("AMZN", "Amazon.com Inc.", "stock", "US", min_quantity=0.01),
    # AssetInfo("META", "Meta Platforms", "stock", "US", min_quantity=0.01),
    # AssetInfo("GOOGL", "Alphabet (Google)", "stock", "US", min_quantity=0.01),
    # AssetInfo("NFLX", "Netflix Inc.", "stock", "US", min_quantity=0.01),
]

ASSET_BY_SYMBOL = {a.symbol: a for a in ASSETS}


def get_broker_for_symbol(symbol: str) -> str:
    """Return broker for symbol — MT5 for everything."""
    return "mt5"


def get_tradeable_symbols(capital: float = 100.0, **kwargs) -> list[str]:
    """Return all tradeable symbols."""
    return [a.symbol for a in ASSETS]


def get_min_quantity(symbol: str) -> float:
    asset = ASSET_BY_SYMBOL.get(symbol)
    if asset:
        if asset.asset_type in ("index_cfd", "stock", "commodity"):
            return 0.01  # CFD: 0.01 lot minimum
        return asset.min_quantity
    return 1000  # Default forex: 1000 units


def get_all_symbols() -> list[str]:
    return [a.symbol for a in ASSETS]


def get_leverage(symbol: str) -> int:
    """Fusion Markets VFSC offshore leverage (compte 429608).
    Forex majors : 500:1 | Indices : 200:1 | Commodities : 100:1 | Stocks : 20:1
    (config.py : leverage_forex=500, leverage_indices=200, leverage_commodities=100, leverage_stocks=20)
    """
    from app.config import settings
    asset = ASSET_BY_SYMBOL.get(symbol)
    if asset:
        if asset.asset_type == "index_cfd":
            return settings.leverage_indices  # 200:1 Fusion VFSC
        elif asset.asset_type == "stock":
            return settings.leverage_stocks   # 20:1 Fusion VFSC
        elif asset.asset_type == "commodity":
            # Fusion VFSC : gold/silver/oil = 100:1 par défaut
            return settings.leverage_commodities
    return settings.leverage_forex  # 500:1 Fusion VFSC forex majors


def get_market_for_symbol(symbol: str) -> str:
    asset = ASSET_BY_SYMBOL.get(symbol)
    if asset:
        if asset.asset_type == "index_cfd":
            return "INDICES"
        elif asset.asset_type == "stock":
            return "STOCKS"
        elif asset.asset_type == "commodity":
            return "COMMODITY"
    return "FOREX"


