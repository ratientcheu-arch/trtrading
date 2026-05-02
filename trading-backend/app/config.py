from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ── MT5 bridge (SEUL BROKER) ─────────────────────────────────────────
    # Le bot parle au terminal MT5 via ZeroMQ exposé par l'EA ZmqBridge.mq5
    # qui tourne dans le container mt5-bridge (Wine + MT5 terminal).
    # Les identifiants du compte (login/password/server) sont consommés
    # UNIQUEMENT par le container mt5-bridge (voir docker-compose.yml).
    mt5_enabled: bool = True
    mt5_pub_endpoint: str = "tcp://mt5-bridge:5555"  # ticks PUB
    mt5_rep_endpoint: str = "tcp://mt5-bridge:5556"  # orders REP
    mt5_rpc_timeout_s: float = 10.0

    # ── Database ─────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://trading:changeme@localhost:5432/trading"

    # ── API Security ─────────────────────────────────────────────────────
    api_key: str = "change-me"
    allowed_origins: str = "https://day-trading.pages.dev"

    # ── Trading Parameters — Scalping Mode ───────────────────────────────
    starting_capital: float = 1600.0
    max_order_size: float = 200.0      # 2026-04-21: 300→200€ marge max/trade (après -146€ AUDNZD avec marge 500€)
    max_risk_per_trade: float = 0.02   # % du capital (fallback, utilisé si max_risk_per_trade_eur=0)
    max_risk_per_trade_eur: float = 15.0   # 2026-04-21: FIX 15€/trade max. R:R appliqué = celui configuré per-paire dans signals.py PAIR_CONFIG
    max_daily_loss: float = 0.03       # 2026-04-21 URGENT: 50%→3% (≈ 48€ max/jour à capital 1600€). Avant: 50% = 800€ toléré ⚠️
    max_daily_loss_eur: float = 600.0  # 2026-04-25: plafond fixe 600€/jour (relevé pour couvrir pire scénario schedule_v1 avec lots 0.5)
    max_open_positions: int = 5        # 2026-04-21: 10→5 (réduire exposition simultanée)

    # ── Fusion Markets Leverage (compte MT5 VFSC offshore) ───────────────
    # Ces valeurs sont les plafonds côté bot; le broker applique ses propres
    # limites. Compte 429608 Fusion Markets VFSC → 500:1 forex dispo.
    leverage_forex: int = 500
    leverage_indices: int = 200
    leverage_commodities: int = 100
    leverage_stocks: int = 20
    leverage_crypto: int = 5

    # ── Capital Allocation ───────────────────────────────────────────────
    allocation_forex: float = 0.60
    allocation_stocks: float = 0.20
    allocation_indices: float = 0.10
    allocation_commodities: float = 0.10

    max_allocation_forex: float = 0.80
    max_allocation_stocks: float = 0.50
    max_allocation_indices: float = 0.20
    max_allocation_commodities: float = 0.20

    min_allocation_forex: float = 0.10
    min_allocation_stocks: float = 0.05
    min_allocation_indices: float = 0.05
    min_allocation_commodities: float = 0.05

    # Logging
    log_level: str = "INFO"

    class Config:
        extra = "ignore"
        env_file = ".env"
        case_sensitive = False


settings = Settings()
