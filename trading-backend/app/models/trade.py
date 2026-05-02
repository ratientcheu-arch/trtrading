from sqlalchemy import Column, Integer, String, Float, DateTime, Enum as SAEnum, JSON, Boolean
from sqlalchemy.sql import func
from app.database import Base
import enum


class TradeStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TradeSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    side = Column(SAEnum(TradeSide), nullable=False)
    status = Column(SAEnum(TradeStatus), nullable=False, default=TradeStatus.OPEN)

    # Entry
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    entry_amount = Column(Float, nullable=False)  # entry_price * quantity
    entry_time = Column(DateTime(timezone=True), server_default=func.now())
    entry_order_id = Column(Integer, nullable=True)

    # Exit
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime(timezone=True), nullable=True)
    exit_order_id = Column(Integer, nullable=True)
    exit_reason = Column(String(64), nullable=True)  # stop_loss, take_profit, trailing_stop, signal_reversal, manual

    # Risk management
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    trailing_stop = Column(Float, nullable=True)

    # P&L
    pnl = Column(Float, nullable=True)
    pnl_percent = Column(Float, nullable=True)
    commission = Column(Float, nullable=True, default=0.0)  # Broker commission in EUR
    commission_raw = Column(Float, nullable=True, default=0.0)  # Commission in original currency
    net_pnl = Column(Float, nullable=True)  # pnl - commission = net P&L

    # Signal context
    signal_confidence = Column(Float, nullable=True)
    signal_reason = Column(String(512), nullable=True)
    indicators_snapshot = Column(JSON, nullable=True)

    # ── Scalping latency instrumentation (added 2026-04-14) ─────────────
    # Unix timestamps (seconds as float) for latency analysis
    signal_ts = Column(Float, nullable=True)        # when signal was generated
    order_sent_ts = Column(Float, nullable=True)    # when order was sent to broker
    fill_ts = Column(Float, nullable=True)          # when broker confirmed fill
    # Derived convenience metrics (ms)
    signal_to_send_ms = Column(Integer, nullable=True)
    send_to_fill_ms = Column(Integer, nullable=True)
    # Broker source metadata (MT5 ticket/deal id for backfill)
    broker_deal_id = Column(String(64), nullable=True, index=True)
    broker_position_id = Column(String(64), nullable=True, index=True)
    source = Column(String(16), nullable=True, default="bot")  # "bot" or "icmarkets_sync"

    # Origin: who placed this trade
    origin = Column(String(16), nullable=True, default="bot")  # "bot", "operator", "orphan"

    # Metadata
    ib_contract_id = Column(Integer, nullable=True)
    market = Column(String(16), nullable=True)
    asset_type = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DailyPerformance(Base):
    __tablename__ = "daily_performance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False, unique=True, index=True)  # YYYY-MM-DD
    starting_capital = Column(Float, nullable=False)
    ending_capital = Column(Float, nullable=False)
    pnl = Column(Float, nullable=False, default=0.0)
    trades_count = Column(Integer, nullable=False, default=0)
    wins = Column(Integer, nullable=False, default=0)
    losses = Column(Integer, nullable=False, default=0)
    win_rate = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    best_trade_pnl = Column(Float, nullable=True)
    worst_trade_pnl = Column(Float, nullable=True)

    # Per-category realized P&L
    forex_pnl = Column(Float, nullable=False, default=0.0)
    actions_pnl = Column(Float, nullable=False, default=0.0)
    indices_pnl = Column(Float, nullable=False, default=0.0)
    commodities_pnl = Column(Float, nullable=False, default=0.0)

    # Per-broker capital snapshot
    ibkr_capital = Column(Float, nullable=False, default=0.0)
    capitalcom_capital = Column(Float, nullable=False, default=0.0)
    fxopen_capital = Column(Float, nullable=False, default=0.0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class OpenPosition(Base):
    __tablename__ = "open_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pos_key = Column(String, unique=True, nullable=False, index=True)
    symbol = Column(String, nullable=False)
    action = Column(String, nullable=False)  # BUY or SELL
    entry_price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    take_profit = Column(Float, nullable=True)
    broker = Column(String, default="mt5")
    opened_at = Column(DateTime(timezone=True), nullable=False)
    position_eur = Column(Float, default=0)
    manual = Column(Boolean, default=False)
    origin = Column(String(16), default="bot")  # "bot", "operator", "orphan"
    sl_order_id = Column(String, nullable=True)
    tp_order_id = Column(String, nullable=True)
    is_open = Column(Boolean, default=True, index=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    close_price = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    extra = Column(JSON, nullable=True)  # Store full position dict for extra fields

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class BotConfig(Base):
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), nullable=False, unique=True, index=True)
    value = Column(String(256), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
