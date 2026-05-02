from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime


class ManualOrderRequest(BaseModel):
    symbol: str
    action: Literal["buy", "sell"]
    amount_eur: float


class ClosePositionRequest(BaseModel):
    symbol: str


class ExpectOperatorOrderRequest(BaseModel):
    symbol: str
    side: str  # "buy" or "sell"
    ttl_seconds: int = 600  # 10 min window


class ModifySLRequest(BaseModel):
    new_sl: float


class ReclassifyPositionRequest(BaseModel):
    origin: str  # "operator", "orphan", or "bot"


class BotConfigUpdate(BaseModel):
    starting_capital: Optional[float] = None
    max_order_size: Optional[float] = None
    max_risk_per_trade: Optional[float] = None
    max_daily_loss: Optional[float] = None
    max_open_positions: Optional[int] = None
    scan_interval: Optional[int] = None


class TradeResponse(BaseModel):
    id: int
    symbol: str
    name: str
    side: str
    status: str
    entry_price: float
    quantity: float
    entry_amount: float
    entry_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
    exit_reason: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    signal_confidence: Optional[float] = None
    signal_reason: Optional[str] = None
    market: Optional[str] = None


class AccountResponse(BaseModel):
    balance: float
    net_liquidation: float
    buying_power: float
    daily_pnl: float
    currency: str = "EUR"


class HealthResponse(BaseModel):
    status: str
    bot_running: bool
    uptime_seconds: float
    version: str = "5.0.0"
    mt5_connected: bool = False


class DailyPerformanceResponse(BaseModel):
    date: str
    starting_capital: float
    ending_capital: float
    pnl: float
    trades_count: int
    wins: int
    losses: int
    win_rate: Optional[float] = None
    forex_pnl: float = 0.0
    actions_pnl: float = 0.0
    indices_pnl: float = 0.0
    commodities_pnl: float = 0.0
    mt5_capital: float = 0.0
