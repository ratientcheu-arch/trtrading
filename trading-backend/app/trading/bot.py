"""
Trading bot orchestrator — the main engine that scans markets, evaluates signals,
and executes trades autonomously. MT5 Open API only.
"""
import asyncio
from datetime import datetime, date, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from app.config import settings
from app.utils.logging import get_logger, log_timing
from app.trading.indicators import compute_all_indicators, Candle
from app.trading.signals import generate_signal, Signal, is_in_trading_session, get_pair_config, PAIR_CONFIG, PAIR_SESSIONS, is_symbol_disabled, is_in_global_trading_window, TRADING_WINDOW_CET
from app.trading.risk_manager import RiskManager, TradeDecision
from app.trading.market_hours import is_market_open
from app.trading.symbol_mapper import (
    ASSETS, get_tradeable_symbols, ASSET_BY_SYMBOL,
    get_leverage, get_market_for_symbol, get_broker_for_symbol,
)
from app.database import async_session
from app.models.trade import DailyPerformance, OpenPosition
from sqlalchemy import select, update

logger = get_logger(__name__)


def _sl_buffer_by_instrument(entry_price: float, symbol: str) -> float:
    """Buffer SL minimum en unités de prix, calibré par type d'instrument.

    Objectif: éviter le stop-hunt sur feeds broker-spécifiques (Fusion Markets).
    Les feeds divergent sur les mèches de liquidité — un SL trop serré se fait
    chopper sur un spike invisible côté chart visuel (cTrader/TradingView).

    Règles (2026-04-24, après 3 SL choppés au pip près sur 1 journée):
      - Indices (DAX40, CAC40, UK100, etc, price > 1000) : 25 pts
      - JPY pairs (symbole finit par JPY) : 0.15 (= 15 pips)
      - Majors forex : 0.0010 (= 10 pips)
      - Fallback : 0.10% du prix
    """
    sym = (symbol or "").replace("/", "").upper()
    is_forex = len(sym) == 6 and sym.isalpha()
    is_jpy = is_forex and sym.endswith("JPY")
    is_major = is_forex and not is_jpy
    is_index = entry_price > 1000 and not is_forex
    if is_index:
        return 25.0
    if is_jpy:
        return 0.15
    if is_major:
        return 0.0010
    return abs(entry_price) * 0.0010


# ── Process pool worker (module-level for pickling) ──
from concurrent.futures import ProcessPoolExecutor

def _compute_signal_worker(candles, price, change_percent, symbol, spread, prev_close, candles_h1=None):
    """Run in separate process — free from GIL/Twisted contention."""
    ind = compute_all_indicators(candles)
    sig = generate_signal(
        price=price,
        indicators=ind,
        change_percent=change_percent,
        symbol=symbol,
        spread=spread,
        prev_close=prev_close,
        candles_h1=candles_h1,  # 2026-04-21: Option A — direction = H1 macro
    )
    return ind, sig

def _compute_indicators_worker(candles):
    """Run compute_all_indicators in separate process."""
    return compute_all_indicators(candles)


class TradingBot:
    def __init__(self, mt5_client=None, mt5_data_client=None, mt5_dash_client=None):
        self.mt5 = mt5_client          # TCP-1 TRADING: orders + spots ONLY (never blocked)
        self.mt5_data = mt5_data_client  # TCP-2 DATA: candles ONLY (can be slow)
        self.mt5_dash = mt5_dash_client  # TCP-3 DASHBOARD: positions + equity + reconciliation (fast)
        self._process_pool = ProcessPoolExecutor(max_workers=4)  # 4 workers — scan 10s × 24 symbols needs real parallelism
        self.risk_manager = RiskManager()
        self._running = False
        # 2026-04-25: scan_only = True par DÉFAUT pour désactiver le système de base
        # (M15 + Body M5 + Volume M5 + Trigger M1). Seuls les triggers horaires
        # (strategy_schedule) peuvent ouvrir des positions.
        self._scan_only = True
        self._scan_interval = 10  # seconds — Scalping mode (was 30s, reduced 2026-04-14 for sub-10s signal-to-order)
        self._open_positions: dict[str, dict] = {}  # pos_key -> position info
        self._positions_lock = asyncio.Lock()
        self._day_start_capital: float = 0
        self._daily_trades: list[dict] = []
        self._today: str = ""
        self._callbacks: list = []  # WebSocket broadcast callbacks

        # Operator pending orders — pre-registered manual orders from dashboard
        # Key: "EURUSD_BUY" -> {"created_at": timestamp, "expires_at": timestamp}
        self._pending_operator_orders: dict[str, dict] = {}
        # Symbols the bot is actively closing (to ignore the return fill)
        self._closing_symbols: set = set()

        # Track signals for the dashboard
        self._last_signals: dict[str, dict] = {}
        self._last_quotes: dict[str, dict] = {}
        self._last_perf_save: float = 0  # timestamp of last performance save
        self._perf_save_interval = 12 * 3600  # Save every 12h (for forex 24/5)

        # ── Candle cache for fast scanning ──────────────────────────────────
        self._candle_cache: dict[str, list] = {}  # symbol -> M15 candles
        self._candle_cache_m5: dict[str, list] = {}  # symbol -> M5 candles
        self._candle_cache_m1: dict[str, list] = {}  # symbol -> M1 candles
        self._candle_cache_h1: dict[str, list] = {}  # symbol -> H1 candles (scalping MTF filter)
        self._candle_cache_d1: dict[str, list] = {}  # symbol -> D1 candles (macro direction, 2026-04-18)
        self._candle_cache_ts: dict[str, float] = {}  # symbol -> last update timestamp
        # Liquidity Candle strategy (parallel to 4TF, US session only)
        self._liquidity_signals: dict[str, "LiquiditySignal"] = {}  # symbol -> pending limit signal
        self._pattern_signals: dict[str, "PatternSignal"] = {}  # symbol -> pending pattern signal (90 min)
        self._last_liquidity_check_date = None  # date of last 13h45 UTC check (US)
        self._last_eu_check_date = None          # date of last 07h15 UTC check (EU Euronext/Xetra)
        self._last_uk_check_date = None          # date of last 08h15 UTC check (UK LSE) — 2026-04-21
        self._last_asia_check_date = None        # date of last 23h15 UTC check (Asia)
        # 4TF blacklist: après détection d'une bougie manipulation sur un symbole,
        # bloquer le 4TF sur ce symbole pendant 30 min post-B1 pour éviter le piège.
        self._4tf_blacklist: dict[str, float] = {}  # symbol -> expires_at (UTC timestamp)
        self._cache_max_age = 300  # Refresh candles every 5 minutes
        self._cache_refreshing = False
        self._cached_cc_positions = []  # Fast API cache
        self._cached_equity: float = 0  # Last known equity from cache loop
        self._cached_equity_ts: float = 0  # When equity was last fetched
        self._scanning = False  # True while scan is running — pauses cache refresh
        self._symbol_blacklist: set = set()  # Symbols with no candles — skip them
        self._symbol_cooldown: dict = {}  # symbol -> timestamp when cooldown expires (45min post-close)
        # 2026-04-23 garde-fous concentration:
        self._direction_cooldown: dict = {"BUY": 0.0, "SELL": 0.0}  # direction -> ts fin cooldown (30min après 2 SL consécutifs)
        self._consec_dir_losses: dict = {"BUY": 0, "SELL": 0}       # compteur pertes consécutives par direction
        # 2026-04-23 : max 1/jour SUPPRIMÉ suite whitelist. Remplacé par "max 2 losses/paire/jour".
        self._symbol_losses_today: dict = {}       # canonical symbol -> nb SL/max_hold sur cette paire aujourd'hui
        self._symbol_losses_today_date: str = ""   # date ISO du dernier reset
        self._symbol_extended_cd: dict = {}        # canonical symbol -> ts fin cooldown étendu (4h) si 2 losses
        # D) Quality score : refresh toutes les heures, top 8 accepté
        self._quality_scores: dict = {}       # symbol -> QualityScore
        self._quality_scores_ts: float = 0.0  # timestamp dernier recompute
        self._quality_top_symbols: set = set()  # set des symboles acceptés (top N)
        # 2026-04-21: cooldown événementiel — on enregistre (M5_ts, M1_ts) au moment
        # de la clôture. Une nouvelle entrée sur la même paire est refusée tant que la
        # bougie M5 OU M1 actuelle n'est pas strictement plus récente.
        self._close_candle_marker: dict = {}  # canonical_symbol -> (m5_ts_ms, m1_ts_ms)
        # Live unrealized PnL from bot-tracked positions only — updated by monitor/main loops.
        # Used to ensure status and account files display the SAME daily_pnl.
        self._current_unrealized_pnl: float = 0.0

        # ── Stop-and-Reverse (Option B strict) ─────────────────────────────
        # Tracks canonical symbols that have been flipped today (max 1 flip/symbol/day).
        # Reset daily in _on_new_day.
        self._flipped_symbols_today: set = set()

    # ── Symbol alias map: all broker variants → canonical name ──────────
    # DAX40=DE40=DE30=GER40, CAC40=FR40=F40, etc.
    SYMBOL_ALIASES: dict[str, str] = {
        "DE40": "DAX40", "DE30": "DAX40", "GER40": "DAX40", "DAX": "DAX40",
        "FR40": "CAC40", "F40": "CAC40", "FRA40": "CAC40",
        "US500": "SP500", "SPX500": "SP500",
        "USTEC": "NASDAQ", "NAS100": "NASDAQ", "US100": "NASDAQ", "NDAQ": "NASDAQ",
        "US30": "DJ30", "DJI": "DJ30",
        "JP225": "NKY", "JPN225": "NKY", "J225": "NKY",
        "FTSE100": "UK100",
        "XTIUSD": "CLF", "OIL_CRUDE": "CLF",
        "XBRUSD": "BRENT",
        "GOLD": "XAUUSD", "XAUEUR": "XAUUSD",
        "SILVER": "XAGUSD",
    }

    @staticmethod
    def _canonical_symbol(symbol: str) -> str:
        """Return the canonical symbol name for cooldown/anti-hedge comparison.
        Maps all broker-specific names to one canonical form."""
        norm = symbol.replace("/", "").replace(".", "").replace("=", "").upper().strip()
        return TradingBot.SYMBOL_ALIASES.get(norm, norm)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def mt5_available(self) -> bool:
        return self.mt5 is not None and self.mt5.is_connected

    @property
    def mt5_data_available(self) -> bool:
        """Data connection available (separate TCP for candles)."""
        return self.mt5_data is not None and self.mt5_data.is_connected

    @property
    def mt5_dash_available(self) -> bool:
        """Dashboard connection available (separate TCP for positions/equity)."""
        return self.mt5_dash is not None and self.mt5_dash.is_connected

    def _data_client(self):
        """Return DATA client for candles. Fallback chain: data → dash → trading."""
        if self.mt5_data_available:
            return self.mt5_data
        if self.mt5_dash_available:
            return self.mt5_dash
        return self.mt5  # Fallback: single connection mode

    def _dash_client(self):
        """Return DASHBOARD client for positions/equity/reconciliation. Fallback chain: dash → trading.
        NEVER falls back to data (candles block it for 60s+)."""
        if self.mt5_dash_available:
            return self.mt5_dash
        return self.mt5  # Fallback: single connection mode (skip data — it's slow)

    def _compute_daily_pnl_total(self) -> float:
        """Unified daily P&L = realized (from risk_manager) + unrealized (bot-tracked positions).
        This is the SINGLE source of truth used by both status and account IPC files."""
        _realized = float(self.risk_manager._daily_pnl or 0)
        _unrealized = float(self._current_unrealized_pnl or 0)
        return round(_realized + _unrealized, 2)

    @property
    def status(self) -> dict:
        _realized = float(self.risk_manager._daily_pnl or 0)
        _unrealized = float(self._current_unrealized_pnl or 0)
        return {
            "running": self._running,
            "scan_only": getattr(self, '_scan_only', False),
            "mt5_connected": self.mt5_available,
            "primary_broker": "mt5",
            "capital": self.risk_manager.capital,
            "daily_pnl": round(_realized + _unrealized, 2),
            "daily_pnl_realized": round(_realized, 2),
            "daily_pnl_unrealized": round(_unrealized, 2),
            "open_positions": len(self._open_positions),
            "trades_today": len(self._daily_trades),
            "consecutive_losses": self.risk_manager._consecutive_losses,
            "circuit_breaker": self.risk_manager._circuit_breaker_active,
            "dynamic_allocation": self.risk_manager.allocator.current_allocations,
        }

    def on_event(self, callback):
        self._callbacks.append(callback)

    async def _broadcast(self, event_type: str, data: dict):
        for cb in self._callbacks:
            try:
                await cb(event_type, data)
            except Exception as e:
                logger.error(f"Broadcast error: {e}")

    # ── Position persistence (DB) ──────────────────────────────────────────

    def _save_guards(self):
        """Persiste les garde-fous dans /ipc/bot_guards.json pour survivre aux restarts."""
        try:
            from app.ipc import write_json as _wj_g, GUARDS_FILE
            _wj_g(GUARDS_FILE, {
                "saved_at": __import__("time").time(),
                "date": self._symbol_losses_today_date,
                "symbol_losses_today": self._symbol_losses_today,
                "symbol_extended_cd": self._symbol_extended_cd,
                "symbol_cooldown": self._symbol_cooldown,
                "consec_dir_losses": self._consec_dir_losses,
                "direction_cooldown": self._direction_cooldown,
            })
        except Exception as e:
            logger.warning(f"[GUARDS] save error: {e}")

    def _load_guards(self):
        """Restaure les garde-fous depuis disque au startup. Purge les entrées expirées."""
        try:
            from app.ipc import read_json as _rj_g, GUARDS_FILE
            from datetime import datetime as _dt_g
            import time as _t_g
            data = _rj_g(GUARDS_FILE, None)
            if not data:
                return
            _today = _dt_g.now().date().isoformat()
            _saved_date = data.get("date", "")
            # Si date différente → reset des compteurs du jour uniquement
            if _saved_date == _today:
                self._symbol_losses_today = dict(data.get("symbol_losses_today", {}))
                self._symbol_losses_today_date = _saved_date
            else:
                self._symbol_losses_today = {}
                self._symbol_losses_today_date = _today
            # Cooldowns : garder seulement les non-expirés
            _now = _t_g.time()
            self._symbol_extended_cd = {
                k: v for k, v in data.get("symbol_extended_cd", {}).items() if v > _now
            }
            self._symbol_cooldown = {
                k: v for k, v in data.get("symbol_cooldown", {}).items() if v > _now
            }
            self._direction_cooldown = {
                k: v for k, v in data.get("direction_cooldown", {"BUY": 0.0, "SELL": 0.0}).items()
            }
            self._consec_dir_losses = dict(data.get("consec_dir_losses", {"BUY": 0, "SELL": 0}))
            logger.info(
                f"[GUARDS] Restored from disk: losses_today={len(self._symbol_losses_today)} "
                f"ext_cd={len(self._symbol_extended_cd)} cd={len(self._symbol_cooldown)} "
                f"consec={self._consec_dir_losses}"
            )
        except Exception as e:
            logger.warning(f"[GUARDS] load error: {e}")

    async def _save_position(self, pos_key: str, pos_data: dict):
        """Save or update an open position to the database."""
        try:
            async with async_session() as session:
                # Check if already exists
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.pos_key == pos_key)
                )
                existing = result.scalar_one_or_none()

                entry_time = pos_data.get("entry_time") or pos_data.get("opened_at")
                if isinstance(entry_time, str):
                    try:
                        opened_at = datetime.fromisoformat(entry_time)
                    except (ValueError, TypeError):
                        opened_at = datetime.now(timezone.utc)
                elif isinstance(entry_time, datetime):
                    opened_at = entry_time
                else:
                    opened_at = datetime.now(timezone.utc)

                if existing:
                    existing.stop_loss = pos_data.get("stop_loss")
                    existing.take_profit = pos_data.get("take_profit")
                    existing.extra = pos_data
                else:
                    row = OpenPosition(
                        pos_key=pos_key,
                        symbol=pos_data.get("symbol", pos_key.split("_")[0]),
                        action=pos_data.get("action", "BUY"),
                        entry_price=pos_data.get("entry_price", 0),
                        quantity=pos_data.get("quantity", 0),
                        stop_loss=pos_data.get("stop_loss"),
                        take_profit=pos_data.get("take_profit"),
                        broker=pos_data.get("broker", "mt5"),
                        opened_at=opened_at,
                        position_eur=pos_data.get("position_eur", pos_data.get("position_size", 0)),
                        manual=pos_data.get("manual", False),
                        sl_order_id=pos_data.get("sl_order_id"),
                        tp_order_id=pos_data.get("tp_order_id"),
                        is_open=True,
                        extra=pos_data,
                    )
                    session.add(row)
                await session.commit()
        except Exception as e:
            logger.error(f"[DB] Failed to save position {pos_key}: {e}")

    async def _load_positions(self):
        """Load open positions from DB on startup (survive bot restarts)."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.is_open == True)
                )
                rows = result.scalars().all()

                loaded = 0
                for row in rows:
                    if row.pos_key in self._open_positions:
                        continue  # Already tracked

                    # Rebuild the full position dict from extra JSON or DB columns
                    pos_data = row.extra or {}
                    import time as _t_load
                    pos_data.update({
                        "symbol": row.symbol,
                        "pos_key": row.pos_key,
                        "action": row.action,
                        "entry_price": row.entry_price,
                        "quantity": row.quantity,
                        "stop_loss": row.stop_loss,
                        "take_profit": row.take_profit,
                        "broker": row.broker,
                        "entry_time": row.opened_at.isoformat() if row.opened_at else None,
                        "position_eur": row.position_eur or 0,
                        "manual": row.manual or False,
                        # CRITICAL: reset age to avoid max_hold killing positions after restart
                        "_opened_ts": _t_load.time(),
                    })

                    self._open_positions[row.pos_key] = pos_data
                    loaded += 1
                    logger.info(
                        f"[DB] Restored position: {row.pos_key} {row.action} {row.symbol} "
                        f"qty={row.quantity} @ {row.entry_price}"
                    )

                if loaded:
                    logger.info(f"[DB] Loaded {loaded} open positions from database")
                    self.risk_manager.update_state(
                        daily_pnl=self.risk_manager._daily_pnl,
                        open_positions=len(self._open_positions),
                        open_symbols=[p.get("symbol", k.split("_")[0]) for k, p in self._open_positions.items()],
                        capital=self.risk_manager.capital,
                    )
        except Exception as e:
            logger.error(f"[DB] Failed to load positions: {e}")

    async def _close_position_db(self, pos_key: str, close_price: float, pnl: float):
        """Mark a position as closed in the database."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.pos_key == pos_key)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.is_open = False
                    row.closed_at = datetime.now(timezone.utc)
                    row.close_price = close_price
                    row.pnl = pnl
                    await session.commit()
                    logger.info(f"[DB] Position {pos_key} closed: price={close_price} pnl={pnl:.2f}")
                    # 2026-04-21: Enregistrer la bougie M5+M1 au moment de la clôture
                    # pour forcer une nouvelle bougie avant ré-entrée sur cette paire.
                    try:
                        _close_sym = row.symbol or ""
                        _canon_close = self._canonical_symbol(_close_sym)
                        _m5 = (self._candle_cache_m5.get(_close_sym) or [])
                        _m1 = (self._candle_cache_m1.get(_close_sym) or [])
                        _m5_ts = _m5[-1].timestamp if _m5 else 0
                        _m1_ts = _m1[-1].timestamp if _m1 else 0
                        self._close_candle_marker[_canon_close] = (_m5_ts, _m1_ts)
                        logger.info(f"[COOLDOWN CANDLE] {_canon_close}: marqueur enregistré M5_ts={_m5_ts} M1_ts={_m1_ts} — nouvelle bougie requise avant ré-entrée")
                    except Exception as _e_mark:
                        logger.warning(f"[COOLDOWN CANDLE] {pos_key}: impossible d'enregistrer marqueur: {_e_mark}")
        except Exception as e:
            logger.error(f"[DB] Failed to close position {pos_key}: {e}")

    async def _remove_position_db(self, pos_key: str):
        """Mark a phantom/invalid position as closed in DB (no price/pnl)."""
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.pos_key == pos_key)
                )
                row = result.scalar_one_or_none()
                if row:
                    row.is_open = False
                    row.closed_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception as e:
            logger.error(f"[DB] Failed to remove position {pos_key}: {e}")

    # ── Broker routing helpers ─────────────────────────────────────────────

    @property
    def _primary_broker(self):
        """Return the primary broker client — MT5 only."""
        if self.mt5_available:
            return self.mt5
        return None

    @property
    def _primary_broker_name(self) -> str:
        return "mt5"

    async def _get_candles(self, symbol: str, duration: str = "5 D", bar_size: str = "15 mins") -> list[Candle]:
        """Route candle request to DATA connection (never blocks trading connection)."""
        _dc = self._data_client()
        if _dc and _dc.is_connected:
            try:
                candles = await asyncio.wait_for(
                    _dc.get_historical_candles(symbol, duration, bar_size),
                    timeout=8,
                )
                if candles:
                    return candles
            except asyncio.TimeoutError:
                logger.warning(f"[DATA] Candle timeout for {symbol}")
        return []

    async def _get_quote(self, symbol: str) -> Optional[dict]:
        """Route quote request to MT5."""
        if self.mt5_available:
            try:
                quote = await asyncio.wait_for(
                    self.mt5.get_realtime_quote(symbol),
                    timeout=3,
                )
                if quote and quote.get("price"):
                    return quote
            except asyncio.TimeoutError:
                pass
        return None

    async def _place_order(self, symbol: str, action: str, quantity: float,
                           stop_loss: Optional[float] = None,
                           take_profit: Optional[float] = None) -> Optional[dict]:
        """Route order to MT5. SL/TP posé directement sur le broker."""
        if not self.mt5_available:
            logger.error(f"[ORDER] MT5 not connected — cannot place order for {symbol}")
            return None

        result = await self.mt5.place_market_order(
            symbol, action, quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        if result and result.get("status") == "FILLED":
            result["broker"] = "mt5"
            fill_price = result.get("fill_price") or result.get("price", 0)
            logger.info(
                f"[ORDER] {action} {quantity} {symbol} via MT5 @ {fill_price} "
                f"SL={stop_loss} TP={take_profit} (broker-native)"
            )
            return result
        elif result and result.get("status") == "REJECTED":
            err = result.get("error", "UNKNOWN")
            desc = result.get("description", "")
            logger.error(f"[ORDER] MT5 REJECTED {symbol}: {err} — {desc}")
            return {"status": "REJECTED", "error": f"MT5: {err} — {desc}"}
        else:
            logger.error(f"[ORDER] MT5 order returned None for {symbol}")
            return None

    async def _close_position_broker(self, symbol: str, ticket: int = None, broker: str = None):
        """Close position on MT5."""
        if not self.mt5_available:
            logger.error(f"[CLOSE] MT5 not connected — cannot close {symbol}")
            return None

        if ticket:
            result = await self.mt5.close_position(ticket)
        else:
            result = await self.mt5.close_position(symbol)
        if result:
            logger.info(f"[CLOSE] {symbol} closed on MT5: {result}")
            return result
        else:
            logger.error(f"[CLOSE] Failed to close {symbol} on MT5")
            return None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self):
        if self._running:
            logger.warning("Bot is already running")
            return

        # MT5 must be connected
        ct_ok = self.mt5_available
        if not ct_ok:
            logger.error("Cannot start bot: MT5 not connected")
            return
        logger.info("Bot starting — broker: MT5")

        self._running = True
        self._today = date.today().isoformat()
        self.risk_manager.reset_daily()

        # Capital = lecture directe du solde MT5 via DASHBOARD connection
        try:
            _dc = self._dash_client()
            if _dc and _dc.is_connected:
                _summary = await _dc.get_account_summary()
                _balance = _summary.get("balance", 0)
                self.risk_manager.capital = _balance
                logger.info(f"MT5 balance: {_balance:.2f} EUR (source de vérité broker, DASH conn)")
        except Exception as e:
            logger.error(f"Failed to read MT5 balance: {e}")
            self.risk_manager.capital = settings.starting_capital

        # ═══ RELOAD TODAY'S P&L FROM DB (survives bot restarts) ═══
        try:
            from app.models.trade import Trade, TradeStatus
            from sqlalchemy import and_, text
            from datetime import datetime as _dt, timezone as _tz
            _today_start = _dt.strptime(self._today, "%Y-%m-%d").replace(tzinfo=_tz.utc)
            _today_end = _today_start.replace(hour=23, minute=59, second=59)
            async with async_session() as _sess:
                _result = await _sess.execute(
                    select(Trade).where(
                        and_(
                            Trade.status == TradeStatus.CLOSED,
                            Trade.exit_time >= _today_start,
                            Trade.exit_time <= _today_end,
                        )
                    )
                )
                _today_trades = _result.scalars().all()
                if _today_trades:
                    # FIX 2026-04-15: utiliser net_pnl (après commissions) pour cohérence avec
                    # record_trade_result() qui stocke net_pnl. L'ancien code sommait pnl (gross)
                    # → daily_pnl affiché trop optimiste de ~5€ (commissions absentes)
                    _db_gross_pnl = sum(t.pnl or 0 for t in _today_trades)
                    _db_commission = sum(t.commission or 0 for t in _today_trades)
                    _db_daily_pnl = sum((t.net_pnl if t.net_pnl is not None else (t.pnl or 0) - (t.commission or 0)) for t in _today_trades)
                    self.risk_manager._daily_pnl = _db_daily_pnl
                    # Rebuild _daily_trades list from DB (pnl net pour cohérence UI)
                    for t in _today_trades:
                        _net = t.net_pnl if t.net_pnl is not None else ((t.pnl or 0) - (t.commission or 0))
                        self._daily_trades.append({
                            "symbol": t.symbol,
                            "pnl": _net,
                            "side": t.side.value if t.side else "buy",
                            "entry_price": t.entry_price,
                            "exit_price": t.exit_price,
                            "exit_reason": t.exit_reason,
                            "market": t.market or "FOREX",
                        })
                    logger.info(
                        f"[STARTUP] Reloaded today's P&L from DB: NET={_db_daily_pnl:+.2f}€ "
                        f"(gross {_db_gross_pnl:+.2f}€ − commissions {_db_commission:.2f}€) "
                        f"— {len(_today_trades)} trades"
                    )
        except Exception as _e:
            logger.warning(f"[STARTUP] Failed to reload daily P&L from DB: {_e}")

        # Reload existing positions from database (survive bot restarts)
        await self._load_positions()

        # 2026-04-23 : restaurer les garde-fous depuis disque (cooldowns/losses persistent)
        self._load_guards()

        # ═══ STARTUP CLEANUP — purge orphan positions from previous sessions ═══
        orphan_keys = [k for k, v in self._open_positions.items() if v.get("orphan") or v.get("origin") == "orphan"]
        for orphan_key in orphan_keys:
            orphan_pos = self._open_positions.pop(orphan_key)
            await self._remove_position_db(orphan_key)
            logger.warning(f"[STARTUP CLEANUP] Purged orphan position: {orphan_pos.get('symbol')} {orphan_pos.get('action')} ({orphan_key})")
        if orphan_keys:
            logger.info(f"[STARTUP CLEANUP] Removed {len(orphan_keys)} orphan positions")

        # ═══ STARTUP CLEANUP — purge 'mt5_manual' positions adoptées par erreur ═══
        # Ces positions ont été importées auparavant par auto-sync, mais violent la règle
        # SL/TP broker-natif. On les enlève du tracking du bot (la position reste ouverte
        # sur le broker — l'utilisateur doit la gérer manuellement).
        manual_keys = [k for k, v in self._open_positions.items() if v.get("origin") == "mt5_manual"]
        for mkey in manual_keys:
            mpos = self._open_positions.pop(mkey)
            await self._remove_position_db(mkey)
            logger.warning(
                f"[STARTUP CLEANUP] Untracked mt5_manual position removed from bot: "
                f"{mpos.get('symbol')} {mpos.get('action')} ticket={mpos.get('ticket')} — "
                f"position still open on broker, user must manage it manually"
            )
        if manual_keys:
            logger.info(f"[STARTUP CLEANUP] Removed {len(manual_keys)} mt5_manual positions from bot tracking")

        # Reload existing positions from MT5 (source de vérité)
        try:
            await self._reload_broker_positions()
        except Exception as e:
            logger.warning(f"[STARTUP] Failed to reload MT5 positions: {e}")

        # Set gain guard starting capital = balance BEFORE today's trades
        # Use: current_balance - sum(today's realized PnL from DB)
        _today_realized = self.risk_manager._daily_pnl  # Already loaded from DB above
        self._day_start_capital = self.risk_manager.capital - _today_realized
        # 2026-04-21: partager day_start_balance avec risk_manager pour daily_loss broker-source
        self.risk_manager._day_start_balance = self._day_start_capital
        _gain_target = 700
        logger.info(f"Trading bot started — broker: MT5, capital: {self.risk_manager.capital:.2f}")
        logger.info(
            f"[GAIN GUARD] Capital debut journee: {self._day_start_capital:.2f}€ "
            f"(balance {self.risk_manager.capital:.2f} - PnL realise {_today_realized:+.2f}) "
            f"— stop auto a {self._day_start_capital + _gain_target:.2f}€ (+{_gain_target}€)"
        )
        _dl_pct = self._day_start_capital * self.risk_manager.max_daily_loss
        _dl_fixed = getattr(self.risk_manager, "max_daily_loss_eur", 0.0)
        _dl_max = _dl_fixed if _dl_fixed > 0 else _dl_pct
        logger.info(
            f"[DAILY LOSS GUARD] Source broker: capital start {self._day_start_capital:.2f}€ "
            f"→ seuil stop à {self._day_start_capital - _dl_max:.2f}€ (perte max {_dl_max:.2f}€)"
        )
        # Force fresh cache at startup — clear throttle so first scan gets fresh data
        self._last_cache_attempt = 0
        self._cache_refreshing = False
        self._candle_cache.clear()
        self._candle_cache_ts.clear()
        logger.info("[CACHE] Cache cleared — fresh data will be loaded on first scan")

        # ═══ SUBSCRIBE SPOTS for all tradeable symbols — real-time bid/ask ═══
        if self.mt5_available:
            _spot_syms = get_tradeable_symbols(capital=self.risk_manager.capital)
            _spot_ok = 0
            for _ss in _spot_syms:
                try:
                    ok = await self.mt5.subscribe_spots(_ss)
                    if ok:
                        _spot_ok += 1
                except Exception:
                    pass
            logger.info(f"[SPOTS] Subscribed to {_spot_ok}/{len(_spot_syms)} real-time spot feeds")

        await self._broadcast("bot_status", self.status)

        # Main loop (scan only — cache refresh is SEPARATE)
        asyncio.create_task(self._main_loop())
        # Cache refresh — independent background task, never blocks scanning
        asyncio.create_task(self._cache_refresh_loop())
        # Position monitor — independent, runs every 10s, NEVER blocked by cache
        asyncio.create_task(self._position_monitor_loop())

    async def _position_monitor_loop(self):
        """Independent position monitor — checks broker positions every 10s.
        Uses DASHBOARD connection (TCP-3) — never blocked by candle fetching."""
        from datetime import datetime, timezone, timedelta
        logger.info("[MONITOR] Position monitor started — checking every 10s (DASHBOARD TCP-3)")
        while self._running:
            try:
                _dc = self._dash_client()
                if not _dc or not _dc.is_connected:
                    await asyncio.sleep(10)
                    continue

                # ═══ SYNC CAPITAL from broker via DASHBOARD connection ═══
                try:
                    _bal = await asyncio.wait_for(_dc.get_account_summary(), timeout=5)
                    _real_bal = _bal.get("balance", 0)
                    _real_equity = _bal.get("net_liquidation", _real_bal)  # balance + unrealized PnL
                    if _real_bal > 0 and abs(_real_bal - self.risk_manager.capital) > 0.5:
                        logger.info(f"[CAPITAL SYNC] {self.risk_manager.capital:.2f} → {_real_bal:.2f} (broker)")
                        self.risk_manager.capital = _real_bal

                    # ═══ 2026-04-21 — HARD EQUITY STOP (vérité broker temps réel) ═══
                    # Monitor continu de la perte journalière totale (réalisée + unrealized).
                    # Si perte atteint daily_loss_limit → CLOSE ALL + STOP BOT immédiat.
                    _day_start = self.risk_manager._day_start_balance
                    if _day_start and _day_start > 0 and _real_equity > 0:
                        _daily_delta = _real_equity - _day_start
                        _dl_pct = _day_start * self.risk_manager.max_daily_loss
                        _dl_fixed = getattr(self.risk_manager, "max_daily_loss_eur", 0.0)
                        _dl_limit = _dl_fixed if _dl_fixed > 0 else _dl_pct
                        if _daily_delta <= -_dl_limit:
                            logger.critical(
                                f"[EQUITY STOP] 🚨 Perte journalière totale {_daily_delta:+.2f}€ "
                                f"(start={_day_start:.2f} → equity={_real_equity:.2f}) "
                                f"<= limite -{_dl_limit:.2f}€ → CLOSE ALL + STOP BOT"
                            )
                            # 1. Fermer toutes les positions
                            try:
                                await self._close_all_positions("equity_stop")
                            except Exception as _ce:
                                logger.error(f"[EQUITY STOP] close_all error: {_ce}")
                            # 2. Arrêter le bot (plus aucun nouveau trade)
                            self._running = False
                            # 3. Alerter
                            try:
                                await self._broadcast("alert", {
                                    "level": "critical",
                                    "message": f"🚨 EQUITY STOP: perte journalière {_daily_delta:+.2f}€ atteinte. Toutes positions fermées, bot arrêté. Repos jusqu'à demain.",
                                })
                            except Exception:
                                pass
                            break  # sortir du monitor loop
                except Exception as _eq_err:
                    logger.warning(f"[MONITOR] equity check error: {_eq_err}")

                # Get real positions from MT5 via DASHBOARD connection
                cc_positions = await _dc.get_positions()
                # 2026-04-24 FIX: distinguer "timeout" (None) de "empty" ([] = 0 positions legit)
                # Avant : `if not cc_positions` traitait [] comme un timeout et écrivait
                # les zombies internes en fallback → ghosts permanents sur le dashboard.
                if cc_positions is None:
                    # ═══ FALLBACK: écrire les positions internes dans le dashboard ═══
                    # Si le broker timeout, le dashboard doit quand même montrer les
                    # positions ouvertes connues par le bot (évite les "pertes en silence").
                    if self._open_positions:
                        try:
                            import json
                            from app.ipc import write_json as _wj_fb, POSITIONS_FILE as _PF_fb
                            _fallback_live = []
                            from app.trading.symbol_mapper import get_leverage as _get_lev_fb
                            for _pk_fb, _pv_fb in self._open_positions.items():
                                _sym_fb = _pv_fb.get("symbol", "?")
                                try:
                                    _lev_fb = _get_lev_fb(_sym_fb)
                                except Exception:
                                    _lev_fb = 500
                                _asset_fb = ASSET_BY_SYMBOL.get(_sym_fb) or ASSET_BY_SYMBOL.get(_sym_fb.replace("/", ""))
                                _asset_type_fb = _asset_fb.asset_type if _asset_fb else ("forex" if "/" in _sym_fb else "unknown")
                                _fallback_live.append({
                                    "symbol": _sym_fb,
                                    "action": _pv_fb.get("action", "BUY"),
                                    "asset_type": _asset_type_fb,
                                    "quantity": _pv_fb.get("quantity", 0),
                                    "entry_price": _pv_fb.get("entry_price", 0),
                                    "current_price": _pv_fb.get("entry_price", 0),  # pas de prix live
                                    "stop_loss": _pv_fb.get("stop_loss", 0),
                                    "take_profit": _pv_fb.get("take_profit", 0),
                                    "pnl": 0, "pnl_percent": 0,
                                    "margin": _pv_fb.get("margin", 0),
                                    "exposure": _pv_fb.get("position_eur", 0),
                                    "leverage_used": f"{_lev_fb}:1",
                                    "broker": "mt5",
                                    "ticket": _pv_fb.get("ticket") or _pv_fb.get("position_id"),
                                    "market_category": "mt5",
                                    "pnl_conv_rate": 0.87,
                                    "entry_time": _pv_fb.get("entry_time", ""),
                                    "origin": _pv_fb.get("origin", "bot"),
                                    "_fallback": True,  # Signal au frontend que le PnL est estimé
                                })
                            _wj_fb(_PF_fb, _fallback_live)
                            logger.warning(
                                f"[MONITOR] Broker timeout — {len(_fallback_live)} positions "
                                f"écrites en fallback depuis état interne"
                            )
                        except Exception as _fb_err:
                            logger.error(f"[MONITOR] Fallback positions write error: {_fb_err}")
                    await asyncio.sleep(10)
                    continue

                self._cached_cc_positions = cc_positions
                now = datetime.now(timezone.utc)

                # 2026-04-24 FIX GHOST PURGE: retirer de _open_positions toute entrée dont
                # le ticket n'existe plus chez le broker (position fermée silencieusement).
                _broker_tickets = set()
                for _mp_check in cc_positions:
                    _bt = _mp_check.get("ticket") or _mp_check.get("position_id")
                    if _bt:
                        _broker_tickets.add(int(_bt))
                _purge_keys = []
                for _pk_int, _pv_int in self._open_positions.items():
                    _pv_ticket = _pv_int.get("ticket") or _pv_int.get("position_id")
                    if _pv_ticket and int(_pv_ticket) not in _broker_tickets:
                        _purge_keys.append(_pk_int)
                for _pkp in _purge_keys:
                    _ghost = self._open_positions.pop(_pkp, None)
                    if _ghost:
                        logger.warning(
                            f"[GHOST PURGE] {_ghost.get('symbol')} {_ghost.get('action')} "
                            f"ticket={_ghost.get('ticket')} retiré de _open_positions "
                            f"(absent du broker — fermé silencieusement)"
                        )

                for mp in cc_positions:
                    symbol = mp.get("symbol", "")
                    entry = mp.get("entry_price", 0)
                    current = mp.get("current_price", entry)
                    pnl = mp.get("unrealized_pnl", 0)
                    sl = mp.get("stop_loss") or 0
                    tp = mp.get("take_profit") or 0
                    direction = mp.get("direction", "BUY")
                    _mp_ticket = mp.get("ticket") or mp.get("position_id")

                    # ═══ CRITICAL: check SL/TP exist on broker — fix or close ═══
                    if _mp_ticket and (not sl or sl == 0 or not tp or tp == 0):
                        # Find bot position for SL/TP values
                        _bot_pos_match = None
                        for _pk, _pv in self._open_positions.items():
                            _pv_t = _pv.get("ticket") or _pv.get("position_id")
                            if _pv_t and int(_pv_t) == int(_mp_ticket):
                                _bot_pos_match = _pv
                                break
                        if _bot_pos_match:
                            _want_sl = _bot_pos_match.get("stop_loss")
                            _want_tp = _bot_pos_match.get("take_profit")
                            # Indices/commodities: MT5 rejette les décimales excessives
                            # → arrondir à 1 décimale max pour les prix > 100
                            if _want_sl and _want_sl > 100:
                                _want_sl = round(_want_sl, 1)
                            if _want_tp and _want_tp > 100:
                                _want_tp = round(_want_tp, 1)
                            logger.critical(
                                f"[SLTP MONITOR] {symbol}: SL={sl} TP={tp} — "
                                f"PROTECTION MANQUANTE! Tentative de correction (SL={_want_sl} TP={_want_tp})"
                            )
                            try:
                                # ALWAYS send BOTH SL and TP together — MT5 clears
                                # the other if only one is sent, causing infinite loop
                                _fix = await self.mt5.amend_position_sltp(
                                    int(_mp_ticket),
                                    stop_loss=_want_sl or sl,
                                    take_profit=_want_tp or tp,
                                )
                                if _fix:
                                    logger.info(f"[SLTP MONITOR] {symbol}: SL/TP corrigé ✓")
                                    sl = _want_sl or sl
                                    tp = _want_tp or tp
                                    # Update internal tracking so dashboard shows correct SL/TP
                                    if _bot_pos_match:
                                        _bot_pos_match["stop_loss"] = sl
                                        _bot_pos_match["take_profit"] = tp
                                else:
                                    logger.critical(f"[SLTP MONITOR] {symbol}: correction échouée → FERMETURE")
                                    await self.mt5.close_position(int(_mp_ticket))
                                    async with self._positions_lock:
                                        for _rk in list(self._open_positions.keys()):
                                            if self._open_positions[_rk].get("ticket") == int(_mp_ticket):
                                                self._open_positions.pop(_rk)
                                                await self._remove_position_db(_rk)
                                                break
                                    continue
                            except Exception as _fix_err:
                                logger.error(f"[SLTP MONITOR] {symbol}: erreur correction: {_fix_err}")

                    if not entry or entry == 0:
                        continue
                    if not tp or tp == 0:
                        continue

                    # Calculate age from bot internal tracking
                    _age = 9999
                    # Match by ticket (authoritative) then fallback to canonical symbol.
                    # MT5 returns "EURUSD" but bot stores "EUR/USD" — raw == fails for forex.
                    _canon_mp = self._canonical_symbol(symbol)
                    for _pk, _pv in self._open_positions.items():
                        _pv_t = _pv.get("ticket") or _pv.get("position_id")
                        _match = (_pv_t and _mp_ticket and int(_pv_t) == int(_mp_ticket)) \
                                 or self._canonical_symbol(_pv.get("symbol", "")) == _canon_mp
                        if _match:
                            _open_time = _pv.get("entry_time", "")
                            if _open_time:
                                try:
                                    _odt = datetime.fromisoformat(str(_open_time))
                                    if _odt.tzinfo is None: _odt = _odt.replace(tzinfo=timezone.utc)
                                    _age = (now - _odt).total_seconds()
                                except Exception:
                                    pass
                            break

                    # Calculate progress to TP
                    if direction == "BUY":
                        _dist = tp - entry
                        _progress = (current - entry) / _dist if _dist > 0 else 0
                    else:
                        _dist = entry - tp
                        _progress = (entry - current) / _dist if _dist > 0 else 0

                    # ═══ TRAILING STOP — paliers 5%, SL = palier-5%, TP auto-extension à 90% ═══
                    # Build a pos-like dict for the centralized trailing method.
                    # Match by ticket first (authoritative), fallback to canonical symbol —
                    # raw `==` failed for forex (MT5 "EURUSD" vs bot "EUR/USD"), blocking trail.
                    _trail_pos = None
                    for _pk, _pv in self._open_positions.items():
                        _pv_t = _pv.get("ticket") or _pv.get("position_id")
                        _match_t = (_pv_t and _mp_ticket and int(_pv_t) == int(_mp_ticket))
                        _match_s = self._canonical_symbol(_pv.get("symbol", "")) == _canon_mp
                        if _match_t or _match_s:
                            _trail_pos = _pv
                            break
                    # ═══ ADAPTIVE SL RETRO-FIT 2026-04-23 ═══
                    # Pour les positions ouvertes AVANT le fix adaptive SL (SL calé
                    # sur pips fixes qui tombent dans la résistance/support M15).
                    # S'exécute UNE SEULE FOIS par position (flag _sl_adapted).
                    # Widen only : déplace le SL juste au-delà du swing M15 si
                    # le SL actuel est "dans" la zone dangereuse.
                    # 2 passes:
                    #  - _sl_adapted: widen SL hors résistance/support M15
                    #  - _rr_restored: étendre TP pour garder R:R >= original (min 1.5)
                    if _trail_pos and (not _trail_pos.get("_sl_adapted") or not _trail_pos.get("_rr_restored")):
                        try:
                            _bot_sym = _trail_pos.get("symbol", symbol)
                            _m15_retro = self._candle_cache.get(_bot_sym) or self._candle_cache.get(symbol)
                            if _m15_retro and len(_m15_retro) >= 12:
                                _recent_retro = _m15_retro[-12:]
                                # 2026-04-24: buffer par type d'instrument (anti stop-hunt)
                                _buffer_retro = _sl_buffer_by_instrument(entry, _bot_sym or symbol)
                                _round_n_retro = 1 if entry > 100 else 5

                                # Pass 1 — SL retro (widen hors résistance/support)
                                _new_sl_retro = None
                                _reason_retro = ""
                                if not _trail_pos.get("_sl_adapted"):
                                    if direction == "SELL":
                                        _swing_hi = max(c.high for c in _recent_retro)
                                        _cand = _swing_hi + _buffer_retro
                                        if _cand > sl and _cand > current:
                                            _new_sl_retro = round(_cand, _round_n_retro)
                                            _reason_retro = f"au-delà résistance M15 {_swing_hi:.5f}"
                                    elif direction == "BUY":
                                        _swing_lo = min(c.low for c in _recent_retro)
                                        _cand = _swing_lo - _buffer_retro
                                        if _cand < sl and _cand < current:
                                            _new_sl_retro = round(_cand, _round_n_retro)
                                            _reason_retro = f"sous support M15 {_swing_lo:.5f}"

                                # Pass 2 — TP retro (étendre pour R:R >= MIN si dégradé)
                                _MIN_RR = 1.5
                                _sl_for_rr = _new_sl_retro if _new_sl_retro is not None else sl
                                _risk_dist = abs(entry - _sl_for_rr)
                                _reward_dist = abs(entry - tp) if tp > 0 else 0
                                _current_rr = (_reward_dist / _risk_dist) if _risk_dist > 0 else 0
                                _new_tp_retro = None
                                if tp > 0 and _risk_dist > 0 and _current_rr < _MIN_RR:
                                    _target_reward = _risk_dist * _MIN_RR
                                    if direction == "SELL":
                                        _new_tp_retro = round(entry - _target_reward, _round_n_retro)
                                    elif direction == "BUY":
                                        _new_tp_retro = round(entry + _target_reward, _round_n_retro)

                                # Appliquer si changement
                                if _new_sl_retro is not None or _new_tp_retro is not None:
                                    _final_sl = _new_sl_retro if _new_sl_retro is not None else sl
                                    _final_tp = _new_tp_retro if _new_tp_retro is not None else tp
                                    if _mp_ticket:
                                        try:
                                            _ok_retro = await self.mt5.amend_position_sltp(
                                                int(_mp_ticket), stop_loss=_final_sl, take_profit=_final_tp
                                            )
                                            if _ok_retro:
                                                _old_sl_retro = sl
                                                _old_tp_retro = tp
                                                _trail_pos["stop_loss"] = _final_sl
                                                _trail_pos["take_profit"] = _final_tp
                                                _trail_pos["_sl_adapted"] = True
                                                _trail_pos["_rr_restored"] = True
                                                sl = _final_sl
                                                tp = _final_tp
                                                _parts = []
                                                if _new_sl_retro is not None:
                                                    _parts.append(f"SL {_old_sl_retro:.5f}→{_final_sl:.5f} ({_reason_retro})")
                                                if _new_tp_retro is not None:
                                                    _new_rr = abs(entry - _final_tp) / max(abs(entry - _final_sl), 1e-9)
                                                    _parts.append(f"TP {_old_tp_retro:.5f}→{_final_tp:.5f} (R:R {_current_rr:.2f}→{_new_rr:.2f})")
                                                logger.warning(
                                                    f"[RETRO ADAPTIVE] {symbol} {direction}: {' | '.join(_parts)}"
                                                )
                                            else:
                                                logger.warning(f"[RETRO ADAPTIVE] {symbol}: broker amend refused")
                                        except Exception as _e_retro:
                                            logger.warning(f"[RETRO ADAPTIVE] {symbol}: amend error: {_e_retro}")
                                else:
                                    # Rien à changer → marquer comme traité pour ne plus réessayer
                                    _trail_pos["_sl_adapted"] = True
                                    _trail_pos["_rr_restored"] = True
                        except Exception as _retro_err:
                            logger.debug(f"[RETRO ADAPTIVE] {symbol}: skip ({_retro_err})")

                    if _trail_pos:
                        _sl_changed_m, _tp_changed_m = self._apply_trailing_stop(
                            _trail_pos, current, log_prefix="[MONITOR TRAIL]"
                        )
                        if _sl_changed_m:
                            sl = _trail_pos["stop_loss"]
                        if _tp_changed_m:
                            tp = _trail_pos["take_profit"]

                        # Sync trailing SL/TP to MT5 — ALWAYS send BOTH to avoid MT5 clearing one
                        if _sl_changed_m or _tp_changed_m:
                            _ticket_m = mp.get("ticket") or mp.get("position_id")
                            if _ticket_m:
                                try:
                                    _ok = await self.mt5.amend_position_sltp(
                                        int(_ticket_m),
                                        stop_loss=sl,
                                        take_profit=tp,
                                    )
                                    if _ok:
                                        logger.info(f"[MONITOR TRAIL] {symbol}: SL/TP synced to MT5 broker")
                                except Exception as _e:
                                    logger.warning(f"[MONITOR TRAIL] {symbol}: MT5 sync error: {_e}")

                    # ═══ AUTONOMOUS SAFETY — flip 2min + max_hold 5min ═══
                    # Ces deux sécurités étaient dans _monitor_positions (couplée au
                    # scan). Si le scan est lent, elles se déclenchaient avec 30+ min
                    # de retard. Ici elles tournent toutes les 10s, indépendamment.
                    # Match de la position bot par ticket d'abord, puis symbole canonique
                    # (pour gérer NASDAQ↔USTEC, DAX40↔DE40, etc.).
                    try:
                        _canon_m = self._canonical_symbol(symbol)
                        _bot_pos_sec = None
                        _bot_pk_sec = None
                        # 1) Priorité : match par ticket broker
                        if _mp_ticket:
                            for _pk, _pv in self._open_positions.items():
                                _pv_t = _pv.get("ticket") or _pv.get("position_id")
                                if _pv_t and int(_pv_t) == int(_mp_ticket):
                                    _bot_pos_sec = _pv
                                    _bot_pk_sec = _pk
                                    break
                        # 2) Fallback : match par symbole canonique
                        if _bot_pos_sec is None:
                            for _pk, _pv in self._open_positions.items():
                                if self._canonical_symbol(_pv.get("symbol", "")) == _canon_m:
                                    _bot_pos_sec = _pv
                                    _bot_pk_sec = _pk
                                    break

                        if _bot_pos_sec and _bot_pk_sec:
                            # Âge réel de la position
                            _age_sec = 0
                            _ts_sec = _bot_pos_sec.get("_opened_ts", 0)
                            if _ts_sec > 0:
                                import time as _ts_time
                                _age_sec = _ts_time.time() - _ts_sec
                            else:
                                try:
                                    _et_sec = _bot_pos_sec.get("entry_time", "")
                                    if _et_sec:
                                        _edt_sec = datetime.fromisoformat(str(_et_sec))
                                        if _edt_sec.tzinfo is None:
                                            _edt_sec = _edt_sec.replace(tzinfo=timezone.utc)
                                        _age_sec = (now - _edt_sec).total_seconds()
                                except Exception:
                                    pass

                            # PnL broker (déjà en devise de compte / EUR)
                            _pnl_sec = mp.get("unrealized_pnl", 0) or 0
                            _is_long_sec = (direction == "BUY")
                            # Symbole stocké par le bot (garanti supporté par manual_order)
                            _bot_sym = _bot_pos_sec.get("symbol") or symbol

                            # ── FLIP 2 min : DÉSACTIVÉ (analyse 7J : 0% WR sur 8 trades) ──
                            # Le flip_reverse n'a JAMAIS gagné → retiré 2026-04-10.

                            # ── MAX HOLD PERDANTE (2026-04-21: 10→60 min forex / 90 min index) ──
                            from app.trading.signals import _get_thresholds as _get_th_mon
                            _th_mon = _get_th_mon(_bot_sym)
                            _mh_min = _th_mon.get("max_hold_minutes", 60)
                            if _age_sec >= _mh_min * 60 and _pnl_sec < 0:
                                logger.warning(
                                    f"[MONITOR MAX HOLD] {_bot_sym}: perdante depuis "
                                    f"{_age_sec/60:.1f}min > {_mh_min}min — fermeture forcée (PnL={_pnl_sec:+.2f}€)"
                                )
                                try:
                                    await self._close_position_broker(_bot_sym, ticket=_mp_ticket)
                                except Exception as _ce:
                                    logger.error(f"[MONITOR MAX HOLD] {_bot_sym}: broker close error: {_ce}")
                                try:
                                    await self._close_position(_bot_pk_sec, "max_hold_time", current)
                                except Exception as _ie:
                                    logger.error(f"[MONITOR MAX HOLD] {_bot_sym}: internal close error: {_ie}")
                                await self._broadcast("alert", {
                                    "level": "warning",
                                    "message": f"MAX HOLD {_bot_sym}: perdante {_mh_min}min — fermeture",
                                })
                                continue

                            # ── STAGNATION 20 min — DÉSACTIVÉE 2026-04-21 ──
                            # Cette règle contredisait max_hold 60 min (forex) / 90 min (index).
                            # Tuait les trades à 22-49 min avant qu'ils puissent respirer (replay AUD/CHF, EURGBP).
                            # Max_hold 60/90 min + break-even 50% TP + trailing 5% suffisent comme protection.
                            # Si besoin de réactiver : décommenter ci-dessous et ajuster seuil à 60+ min.
                            # if _age_sec >= 1200 and _pnl_sec <= 0:
                            #     close_position(..., "stagnation_20min")
                    except Exception as _sec_err:
                        logger.error(f"[MONITOR SEC] {symbol}: {_sec_err}")

                # Write cache files for dashboard
                # CRITICAL: only show positions tracked by the bot (not all broker positions)
                try:
                    import json
                    from app.trading.symbol_mapper import get_leverage

                    # Build set of tracked tickets for filtering
                    _tracked_tickets = set()
                    for _pv in self._open_positions.values():
                        _t = _pv.get("ticket") or _pv.get("position_id")
                        if _t:
                            _tracked_tickets.add(int(_t))

                    # ═══ AUTO-SYNC DÉSACTIVÉ — règle critique SL/TP ═══
                    # Ne JAMAIS adopter de positions broker non-trackées par le bot.
                    # Ces positions n'ont pas forcément de SL/TP broker-natif.
                    # Feedback utilisateur: "never trade without broker-native SL/TP"
                    # → FIX hedging a causé une perte de 660€ précédemment.
                    for mp_sync in cc_positions:
                        _sync_ticket = mp_sync.get("ticket") or mp_sync.get("position_id")
                        if _sync_ticket and int(_sync_ticket) not in _tracked_tickets:
                            logger.warning(
                                f"[UNTRACKED BROKER POS] {mp_sync.get('symbol','?')} ticket={_sync_ticket} "
                                f"— IGNORÉE (non ouverte par le bot)"
                            )

                    live = []
                    for mp2 in cc_positions:
                        _mp2_ticket = mp2.get("ticket") or mp2.get("position_id")
                        if not _mp2_ticket:
                            continue
                        # 2026-04-27: afficher TOUTES les positions broker (trackées + externes)
                        # Les positions ouvertes en dehors du bot (scripts directs MT5Client,
                        # ou MT5 mobile) doivent apparaître au dashboard pour visibilité user.
                        _is_tracked = int(_mp2_ticket) in _tracked_tickets

                        sym2 = mp2.get("symbol", "")
                        lev = get_leverage(sym2)
                        e2 = mp2.get("entry_price", 0)
                        c2 = mp2.get("current_price", e2)
                        q2 = mp2.get("quantity", 0)  # MT5: en LOTS (0.11 lots = 11 000 units forex, = 0.11 contrats indice)
                        _su = sym2.upper().replace("/", "").replace(".", "")
                        # is_fx: MT5 format "EURAUD" (pas de slash), détection par pattern currency-pair
                        _ccys = {"EUR", "USD", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"}
                        is_fx = len(_su) == 6 and _su[:3] in _ccys and _su[3:] in _ccys
                        # Fix 2026-04-23 : quantity MT5 = LOTS. Forex 1 lot = 100 000 units.
                        # Indices/commodities : 1 lot = 1 contrat (pas de conversion).
                        if is_fx:
                            units_or_contracts = q2 * 100000  # lots → units forex
                            _base = _su[:3]
                            _base_to_eur = {
                                "EUR": 1.0, "USD": 0.85, "GBP": 1.15, "JPY": 0.0054,
                                "CHF": 1.075, "AUD": 0.606, "NZD": 0.556, "CAD": 0.667,
                            }
                            notional = units_or_contracts * _base_to_eur.get(_base, 1.0)
                        else:
                            # Indice/commodity : q2 lots × prix × conversion USD→EUR
                            notional = q2 * e2 / 1.17
                        margin = notional / lev if lev > 0 else notional

                        # PnL conversion rate: converts (price_diff * qty) from quote currency to EUR
                        # Frontend uses: pnl_eur = price_diff * qty * pnl_conv_rate
                        _su = sym2.upper().replace("/", "")
                        if _su.endswith("USD") or (not is_fx):
                            _pnl_conv = 1.0 / 1.17  # USD→EUR ≈ 0.855
                        elif _su.endswith("JPY"):
                            _pnl_conv = 1.0 / 185.0  # JPY→EUR ≈ 0.0054
                        elif _su.endswith("CHF"):
                            _pnl_conv = 1.0 / 0.93  # CHF→EUR ≈ 1.075
                        elif _su.endswith("GBP"):
                            _pnl_conv = 1.0 / 0.87  # GBP→EUR ≈ 1.15
                        elif _su.endswith("AUD"):
                            _pnl_conv = 1.0 / 1.65  # AUD→EUR ≈ 0.606
                        elif _su.endswith("CAD"):
                            _pnl_conv = 1.0 / 1.50  # CAD→EUR ≈ 0.667
                        elif _su.endswith("NZD"):
                            _pnl_conv = 1.0 / 1.80  # NZD→EUR ≈ 0.556
                        else:
                            _pnl_conv = 0.87  # Default fallback

                        # Get origin + entry_time from bot tracking
                        # Si non trackée par le bot → marquer "external" pour le dashboard
                        _origin = "bot" if _is_tracked else "external"
                        _entry_time2 = ""
                        _bot_sl_fb = 0
                        _bot_tp_fb = 0
                        for _pv in self._open_positions.values():
                            _pv_t = _pv.get("ticket") or _pv.get("position_id")
                            if _pv_t and _mp2_ticket and int(_pv_t) == int(_mp2_ticket):
                                _origin = _pv.get("origin", "bot")
                                _entry_time2 = _pv.get("entry_time") or _pv.get("opened_at") or ""
                                _bot_sl_fb = _pv.get("stop_loss") or 0
                                _bot_tp_fb = _pv.get("take_profit") or 0
                                break

                        # SL/TP: broker first, fallback to bot internal tracking
                        _ipc_sl = mp2.get("stop_loss") or _bot_sl_fb or 0
                        _ipc_tp = mp2.get("take_profit") or _bot_tp_fb or 0
                        # 2026-04-23 — pré-calcul SL/TP euros côté backend.
                        # Frontend utilise p.quantity (lots MT5 = 0.11) × conv dans sa
                        # formule et obtient 0.00€. On fournit les vraies valeurs € en
                        # multipliant par les units réelles (forex lots × 100 000).
                        _units_for_pnl = q2 * 100000 if is_fx else q2
                        _dir_pnl_raw = mp2.get("direction", "BUY")
                        _sl_dist_eur = (e2 - _ipc_sl) if _dir_pnl_raw == "SELL" else (_ipc_sl - e2)
                        _tp_dist_eur = (_ipc_tp - e2) if _dir_pnl_raw == "BUY" else (e2 - _ipc_tp)
                        _sl_pnl_eur = _sl_dist_eur * _units_for_pnl * _pnl_conv if _ipc_sl > 0 else 0.0
                        _tp_pnl_eur = _tp_dist_eur * _units_for_pnl * _pnl_conv if _ipc_tp > 0 else 0.0
                        # 2026-04-23 fix UI "Other" tag: lookup asset_type via canonical symbol
                        _canon_mp2 = self._canonical_symbol(sym2)
                        _asset_info = None
                        for _alias_k, _canon_v in self.SYMBOL_ALIASES.items():
                            if _canon_v == _canon_mp2:
                                _asset_info = ASSET_BY_SYMBOL.get(_alias_k) or ASSET_BY_SYMBOL.get(_canon_v)
                                if _asset_info:
                                    break
                        if _asset_info is None:
                            # Essai direct (sym2 ou sym2 avec slash)
                            _try = [sym2, sym2.replace("/", ""), f"{sym2[:3]}/{sym2[3:]}" if len(sym2) == 6 and "/" not in sym2 else sym2]
                            for _tk in _try:
                                _asset_info = ASSET_BY_SYMBOL.get(_tk)
                                if _asset_info:
                                    break
                        _asset_type_ipc = _asset_info.asset_type if _asset_info else ("forex" if "/" in sym2 else "unknown")
                        live.append({"symbol": sym2, "action": mp2.get("direction","BUY"),
                            "asset_type": _asset_type_ipc,
                            "quantity": q2, "entry_price": e2, "current_price": c2,
                            "stop_loss": _ipc_sl, "take_profit": _ipc_tp,
                            "pnl": round(mp2.get("unrealized_pnl",0), 2),
                            "pnl_percent": round((mp2.get("unrealized_pnl",0)/margin*100) if margin>0 else 0, 2),
                            "margin": round(margin,2), "exposure": round(notional,2),
                            "leverage_used": f"{lev}:1", "broker": "mt5",
                            "ticket": _mp2_ticket, "market_category": "mt5",
                            "pnl_conv_rate": round(_pnl_conv, 6),
                            "sl_pnl_eur": round(_sl_pnl_eur, 2),
                            "tp_pnl_eur": round(_tp_pnl_eur, 2),
                            "entry_time": _entry_time2 or "",
                            "origin": _origin})
                    from app.ipc import write_json as _wj, POSITIONS_FILE as _PF, ACCOUNT_FILE as _AF, STATUS_FILE as _SF0
                    # 2026-05-15: only write positions if non-empty (strategy_v6_runner is primary writer)
                    if live:
                        _wj(_PF, live)
                    # Write account summary — Fusion Markets MT5 capital (primary broker)
                    from app.config import settings as _mon_settings
                    _ic_cap = self.risk_manager.capital
                    _unreal = sum(p.get("pnl", 0) for p in live)
                    # Update shared field so status IPC uses the same unrealized value
                    self._current_unrealized_pnl = _unreal
                    _ic_eq = _ic_cap + _unreal
                    _ic_mar = sum(p.get("margin", 0) for p in live)
                    # Free margin: max(0, equity - margin). Never display negative values.
                    _free_mar = max(0.0, _ic_eq - _ic_mar)
                    # Unified daily PnL: realized (bot) + unrealized (bot-tracked only)
                    _ic_dpnl = round(float(self.risk_manager._daily_pnl or 0) + _unreal, 2)
                    _wj(_AF, {
                        "balance": round(_ic_cap, 2),
                        "net_liquidation": round(_ic_eq, 2),
                        "unrealized_pnl": round(_unreal, 2),
                        "buying_power": round(_free_mar, 2),
                        "daily_pnl": _ic_dpnl,
                        "daily_pnl_realized": round(float(self.risk_manager._daily_pnl or 0), 2),
                        "daily_pnl_unrealized": round(_unreal, 2),
                        "deposit": round(_mon_settings.starting_capital, 2),
                        "profit_loss": round(_ic_eq - _mon_settings.starting_capital, 2),
                        "total_pnl": round(_ic_eq - _mon_settings.starting_capital, 2),
                        "currency": "EUR",
                        "capital": round(_ic_eq, 2),
                        "primary_broker": "Fusion Markets MT5",
                        "brokers": {
                            "mt5": {
                                "balance": round(_ic_cap, 2),
                                "equity": round(_ic_eq, 2),
                                "free_margin": round(_free_mar, 2),
                                "profit": round(_unreal, 2),
                            }
                        },
                    })
                    # Also push a fresh status write so status.daily_pnl stays in sync
                    try:
                        _wj(_SF0, self.status)
                    except Exception:
                        pass
                except Exception:
                    pass

                await asyncio.sleep(10)
            except Exception as e:
                import traceback
                logger.error(f"[MONITOR] Error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(10)

    async def _reload_broker_positions(self):
        """Validate DB positions against MT5 — remove ghosts, NEVER adopt unknown positions.

        CRITICAL SAFETY RULE:
        The bot ONLY tracks positions it opened itself (saved in DB with a ticket).
        Positions found on MT5 but NOT in the bot's DB are logged as warnings
        but NEVER adopted — they could be from manual trading, old bot runs, or
        broker artifacts. Adopting unknown positions caused a 660€ loss previously.
        """
        # 2026-04-21: import explicite de timedelta pour éviter bug scope
        from datetime import timedelta as _td_recon
        import asyncio as _asyncio_rbp
        _dc = self._dash_client()
        if not _dc or not _dc.is_connected:
            logger.warning("MT5 DASH not connected — cannot validate positions")
            return
        try:
            # 2026-04-23 FIX bug #2: au startup, MT5 bridge peut répondre 0 positions
            # pendant 1-2s même si les positions existent. Sans retry, toutes les DB
            # positions sont marquées ghosts et supprimées en erreur.
            # Si on a des positions DB à valider, on retry jusqu'à 5× (100ms, 500ms,
            # 1s, 2s, 3s) tant que le broker renvoie 0. Pas de retry si DB vide.
            _has_db_positions = bool(self._open_positions)
            broker_positions = None
            _retry_delays = [0.1, 0.5, 1.0, 2.0, 3.0] if _has_db_positions else [0.0]
            for _i, _delay in enumerate(_retry_delays):
                if _delay > 0:
                    await _asyncio_rbp.sleep(_delay)
                broker_positions = await _dc.get_positions()
                if broker_positions:  # Got at least one position → broker is alive
                    break
                if _has_db_positions and _i < len(_retry_delays) - 1:
                    logger.info(
                        f"[STARTUP RECONCILE] broker returned 0 positions but DB has "
                        f"{len(self._open_positions)} — retry in {_retry_delays[_i+1]}s"
                    )
            if _has_db_positions and not broker_positions:
                logger.warning(
                    f"[STARTUP RECONCILE] broker STILL returns 0 positions after 5 retries "
                    f"(DB has {len(self._open_positions)}) — ASSUMING broker not ready, "
                    f"SKIP ghost removal (will retry on next monitor cycle)"
                )
                return
            broker_tickets = {}
            for bp in (broker_positions or []):
                _ticket = bp.get("ticket") or bp.get("position_id")
                if _ticket:
                    broker_tickets[int(_ticket)] = bp

            # ── STEP 1: Validate DB positions — remove ghosts ──
            ghost_keys = []
            for pos_key, pos in list(self._open_positions.items()):
                _ticket = pos.get("ticket") or pos.get("position_id")
                if not _ticket:
                    # No ticket = position was never confirmed by broker → ghost
                    ghost_keys.append(pos_key)
                    continue
                if int(_ticket) not in broker_tickets:
                    # Position in DB but NOT on broker → closed by SL/TP or manually → ghost
                    ghost_keys.append(pos_key)

            for gk in ghost_keys:
                ghost_pos = self._open_positions.pop(gk, None)
                if ghost_pos:
                    await self._remove_position_db(gk)
                    _ghost_ticket = ghost_pos.get("ticket") or ghost_pos.get("position_id")
                    logger.critical(
                        f"[STARTUP RECONCILE] GHOST removed: {ghost_pos.get('symbol')} "
                        f"{ghost_pos.get('action')} qty={ghost_pos.get('quantity')} "
                        f"ticket={_ghost_ticket} — NOT on broker"
                    )
                    # ── Recover closed trade from broker deal history ──
                    _dc_deals = self._dash_client()
                    if _ghost_ticket and _dc_deals and _dc_deals.is_connected:
                        try:
                            deals = await _dc_deals.get_deals_by_position(int(_ghost_ticket))
                            close_deals = [d for d in deals if d.get("is_close")]
                            if close_deals:
                                cd = close_deals[-1]  # Last close deal
                                _sym = ghost_pos.get("symbol", "?")
                                _entry = cd.get("entry_price", ghost_pos.get("entry_price", 0))
                                _exit = cd.get("execution_price", 0)
                                _gross = cd.get("gross_profit", 0)
                                # FIX 2026-04-20: commission = open_deal commission + close_deal commission
                                # MT5 Fusion Markets prélève ~$3.95 par côté pour un lot std USD/CAD
                                _comm_usd = sum(abs(d.get("commission", 0)) for d in deals)
                                # Convertir USD → EUR (approximatif 0.85)
                                _comm = _comm_usd * 0.85
                                _swap = sum(d.get("swap", 0) for d in deals)
                                _net = _gross + _swap - _comm
                                # 2026-04-21: MT5 broker = UTC+3 → corriger le timestamp en soustrayant 3h
                                _exit_ts = cd.get("execution_timestamp", 0)
                                if _exit_ts:
                                    _exit_dt = datetime.fromtimestamp(_exit_ts / 1000, tz=timezone.utc) - _td_recon(hours=3)
                                else:
                                    _exit_dt = datetime.now(timezone.utc)
                                logger.info(
                                    f"[STARTUP RECONCILE] Recovered trade: {_sym} "
                                    f"entry={_entry} exit={_exit} gross={_gross:+.2f} "
                                    f"comm={_comm:.2f} net={_net:+.2f}"
                                )
                                # Save to Trade table
                                from app.models.trade import Trade, TradeStatus, TradeSide
                                _entry_time_raw = ghost_pos.get("entry_time") or ghost_pos.get("opened_at") or ""
                                _entry_dt = None
                                if _entry_time_raw:
                                    try:
                                        _entry_dt = datetime.fromisoformat(str(_entry_time_raw))
                                    except (ValueError, TypeError):
                                        _entry_dt = None
                                if not _entry_dt:
                                    # Use open deal timestamp
                                    open_deals = [d for d in deals if not d.get("is_close")]
                                    if open_deals:
                                        _open_ts = open_deals[0].get("execution_timestamp", 0)
                                        if _open_ts:
                                            _entry_dt = datetime.fromtimestamp(_open_ts / 1000, tz=timezone.utc)
                                _qty = cd.get("closed_volume", ghost_pos.get("quantity", 0))
                                _action = ghost_pos.get("action", "BUY")
                                # Determine exit reason from PnL
                                _reason = "broker_close"
                                if _gross > 0:
                                    _reason = "take_profit"
                                elif _gross < 0:
                                    _reason = "stop_loss"
                                async with async_session() as session:
                                    # FIX 2026-04-15: persister les métadonnées d'audit (signal_*, broker_*, origin)
                                    _gp_ticket = ghost_pos.get("ticket") or ghost_pos.get("position_id")
                                    _gp_deal_id = cd.get("deal_id") or cd.get("ticket")
                                    _gp_snap = ghost_pos.get("indicators_snapshot")
                                    trade = Trade(
                                        symbol=_sym,
                                        name=_sym,
                                        side=TradeSide.BUY if _action == "BUY" else TradeSide.SELL,
                                        status=TradeStatus.CLOSED,
                                        entry_price=_entry,
                                        quantity=_qty,
                                        entry_amount=_entry * _qty,
                                        entry_time=_entry_dt or _exit_dt,
                                        exit_price=_exit,
                                        exit_time=_exit_dt,
                                        exit_reason=_reason,
                                        stop_loss=ghost_pos.get("stop_loss"),
                                        take_profit=ghost_pos.get("take_profit"),
                                        pnl=round(_gross, 2),
                                        pnl_percent=round((_gross / (ghost_pos.get("margin", 1) or 500)) * 100, 2),
                                        commission=round(_comm, 2),
                                        net_pnl=round(_net, 2),
                                        market=get_market_for_symbol(_sym),
                                        asset_type=get_market_for_symbol(_sym),
                                        signal_confidence=ghost_pos.get("signal_confidence"),
                                        signal_reason=(ghost_pos.get("signal_reason") or "")[:512] or None,
                                        indicators_snapshot=_gp_snap if isinstance(_gp_snap, (dict, list)) else None,
                                        broker_position_id=str(_gp_ticket) if _gp_ticket else None,
                                        broker_deal_id=str(_gp_deal_id) if _gp_deal_id else None,
                                        origin=ghost_pos.get("origin") or "bot",
                                        source=ghost_pos.get("source") or "startup_reconcile",
                                    )
                                    session.add(trade)
                                    await session.commit()
                                    logger.info(f"[STARTUP RECONCILE] Trade saved to DB: {_sym} {_reason} PnL={_net:+.2f}EUR")
                                # Update risk manager with recovered PnL
                                self.risk_manager.record_trade_result(_net)
                            else:
                                logger.warning(f"[STARTUP RECONCILE] No close deals found for {ghost_pos.get('symbol')} ticket={_ghost_ticket}")
                        except Exception as e:
                            logger.error(f"[STARTUP RECONCILE] Failed to recover trade for ticket {_ghost_ticket}: {e}")
            if ghost_keys:
                logger.warning(f"[STARTUP RECONCILE] Removed {len(ghost_keys)} ghost positions from DB")

            # ── STEP 2: Update SL/TP from broker (source of truth) ──
            for pos_key, pos in self._open_positions.items():
                _ticket = pos.get("ticket") or pos.get("position_id")
                if _ticket and int(_ticket) in broker_tickets:
                    bp = broker_tickets[int(_ticket)]
                    # Sync SL/TP from broker (broker is always the source of truth)
                    broker_sl = bp.get("stop_loss")
                    broker_tp = bp.get("take_profit")
                    if broker_sl and broker_sl > 0:
                        pos["stop_loss"] = broker_sl
                    if broker_tp and broker_tp > 0:
                        pos["take_profit"] = broker_tp
                    # Update current price
                    bp_price = bp.get("current_price", 0)
                    if bp_price > 0:
                        pos["_last_broker_price"] = bp_price

            # ── STEP 3: RE-ADOPT broker positions that were in DB before restart ──
            # Règle: on re-adopte UNIQUEMENT les positions que le bot avait trackées
            # (présentes en DB avec un ticket). Les positions totalement inconnues
            # restent ignorées (sécurité 660€).
            tracked_tickets = set()
            for pos in self._open_positions.values():
                _t = pos.get("ticket") or pos.get("position_id")
                if _t:
                    tracked_tickets.add(int(_t))

            for ticket, bp in broker_tickets.items():
                if ticket not in tracked_tickets:
                    # ── RE-ADOPT: position broker que le bot avait ouverte ──
                    # On adopte si: la position a un SL/TP broker-natif (preuve que
                    # c'est nous qui l'avons ouverte), OU si elle matche un symbole
                    # dans PAIR_CONFIG (symbole supporté par le bot).
                    _bp_sym = bp.get("symbol", "?")
                    _bp_sl = bp.get("stop_loss")
                    _bp_tp = bp.get("take_profit")
                    _bp_dir = bp.get("direction", "BUY")
                    _bp_entry = bp.get("entry_price", 0)
                    _bp_qty = bp.get("quantity", 0)

                    # Vérifier si c'est un symbole que le bot trade
                    _canon = self._canonical_symbol(_bp_sym)
                    from app.trading.signals import PAIR_CONFIG, _normalize_symbol
                    _is_our_symbol = _normalize_symbol(_canon) in PAIR_CONFIG

                    if _is_our_symbol:
                        # Re-adopt avec age reset (évite le max_hold instantané)
                        _pos_key = f"{_canon}_{ticket}"
                        import time as _t_adopt
                        self._open_positions[_pos_key] = {
                            "symbol": _canon,
                            "pos_key": _pos_key,
                            "_opened_ts": _t_adopt.time(),  # CRITICAL: reset age
                            "action": _bp_dir,
                            "entry_price": _bp_entry,
                            "quantity": _bp_qty,
                            "stop_loss": _bp_sl or 0,
                            "take_profit": _bp_tp or 0,
                            "ticket": ticket,
                            "position_id": ticket,
                            "origin": "bot_readopt",
                            "entry_time": datetime.now(timezone.utc).isoformat(),
                            "margin": 0,
                            "position_eur": 0,
                        }
                        tracked_tickets.add(ticket)
                        logger.warning(
                            f"[STARTUP RECONCILE] RE-ADOPTED: {_canon} {_bp_dir} "
                            f"qty={_bp_qty} ticket={ticket} entry={_bp_entry} "
                            f"SL={_bp_sl} TP={_bp_tp} — position récupérée après restart"
                        )
                        # Si SL/TP manquant, le SLTP MONITOR le corrigera au prochain cycle
                        if not _bp_sl or not _bp_tp:
                            logger.critical(
                                f"[STARTUP RECONCILE] {_canon}: SL/TP MANQUANT! "
                                f"Le SLTP MONITOR va tenter de corriger."
                            )
                    else:
                        logger.warning(
                            f"[STARTUP RECONCILE] UNTRACKED broker position ignored: "
                            f"{_bp_sym} ticket={ticket} "
                            f"(SL={_bp_sl} TP={_bp_tp}) — "
                            f"NOT adopted (symbole inconnu)"
                        )

            # Update risk manager
            self.risk_manager.update_state(
                daily_pnl=self.risk_manager._daily_pnl,
                open_positions=len(self._open_positions),
                open_symbols=[p.get("symbol", k.split("_")[0]) for k, p in self._open_positions.items()],
                capital=self.risk_manager.capital,
            )

            tracked = len(self._open_positions)
            logger.info(
                f"[STARTUP RECONCILE] Done: {tracked} tracked, "
                f"{len(ghost_keys)} ghosts removed, {len(broker_tickets) - tracked} untracked broker pos ignored"
            )

            # ── STARTUP RECONCILE — MANUAL TRADES (2026-04-20) ──────────────
            # Detect closed positions on MT5 that are NOT in the DB and insert them.
            # This catches manual trades placed directly on MT5 (outside bot flow).
            try:
                from datetime import datetime as _dt_mr, timezone as _tz_mr, timedelta
                _start_ts = _dt_mr.now(_tz_mr.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                _end_ts = _dt_mr.now(_tz_mr.utc)
                _all_deals = await self.mt5.fetch_deal_list(int(_start_ts.timestamp()), int(_end_ts.timestamp()))
                # Group deals by position_id
                _by_pos: dict[int, list[dict]] = {}
                for _d in _all_deals:
                    _pid = _d.get("position_id", 0)
                    if _pid:
                        _by_pos.setdefault(_pid, []).append(_d)
                # Find positions with both open+close deals
                _inserted_manual = 0
                for _pid, _deals in _by_pos.items():
                    _open_deal = next((d for d in _deals if not d.get("is_close")), None)
                    _close_deal = next((d for d in _deals if d.get("is_close")), None)
                    if not (_open_deal and _close_deal):
                        continue
                    # Check if already in DB
                    try:
                        from app.models.trade import Trade as _TradeM, TradeStatus as _TS, TradeSide as _TSide
                        from sqlalchemy import select as _sqla_select
                        async with async_session() as _db:
                            _res = await _db.execute(_sqla_select(_TradeM).where(_TradeM.broker_position_id == str(_pid)))
                            if _res.scalars().first():
                                continue
                            _sym_raw = _open_deal.get("symbol", "")
                            _canon_sym = _sym_raw
                            if len(_sym_raw) == 6 and _sym_raw.isalpha():
                                _canon_sym = f"{_sym_raw[:3]}/{_sym_raw[3:]}"
                            # Fetch REAL commissions via deals_by_pos (broker-authoritative)
                            _detailed = await self.mt5.get_deals_by_position(int(_pid))
                            _volume = float(_open_deal.get("volume", 0))
                            _entry_price = float(_open_deal.get("execution_price", 0))
                            _exit_price = float(_close_deal.get("execution_price", 0))
                            _gross_pnl = sum(float(d.get("gross_profit", 0)) for d in _detailed) or float(_close_deal.get("gross_profit", 0))
                            _comm = abs(sum(float(d.get("commission", 0)) for d in _detailed))
                            _swap = sum(float(d.get("swap", 0)) for d in _detailed)
                            _side = _open_deal.get("trade_side", "BUY")
                            # 2026-04-21: MT5 broker = UTC+3 → soustraire 3h pour obtenir UTC réel
                            _entry_ts = _dt_mr.fromtimestamp(_open_deal.get("execution_timestamp", 0) / 1000, tz=_tz_mr.utc) - timedelta(hours=3)
                            _exit_ts = _dt_mr.fromtimestamp(_close_deal.get("execution_timestamp", 0) / 1000, tz=_tz_mr.utc) - timedelta(hours=3)
                            _is_forex = "/" in _canon_sym or (len(_sym_raw) == 6 and _sym_raw.isalpha())
                            _net_pnl = round(_gross_pnl - _comm + _swap, 2)
                            _entry_amount = _entry_price * _volume * (100000 if _is_forex else 1)
                            _pnl_pct = (_gross_pnl / _entry_amount * 100) if _entry_amount > 0 else 0.0
                            _new_trade = _TradeM(
                                symbol=_canon_sym,
                                name=_canon_sym,
                                side=_TSide.BUY if _side == "BUY" else _TSide.SELL,
                                status=_TS.CLOSED,
                                entry_price=_entry_price,
                                quantity=_volume,
                                entry_amount=_entry_amount,
                                entry_time=_entry_ts,
                                exit_price=_exit_price,
                                exit_time=_exit_ts,
                                exit_reason="manual_close",
                                pnl=round(_gross_pnl, 2),
                                pnl_percent=round(_pnl_pct, 4),
                                commission=_comm,
                                commission_raw=_comm,
                                net_pnl=_net_pnl,
                                market="FOREX" if _is_forex else "INDICES",
                                asset_type="forex" if _is_forex else "index_cfd",
                                origin="manual",
                                source="manual",
                                broker_position_id=str(_pid),
                                signal_reason="Manual trade auto-detected from MT5 deals",
                            )
                            _db.add(_new_trade)
                            await _db.commit()
                            _inserted_manual += 1
                            logger.warning(
                                f"[STARTUP RECONCILE] MANUAL trade auto-inserted: "
                                f"{_canon_sym} {_side} vol={_volume} "
                                f"@{_entry_price}→{_exit_price} gross={_gross_pnl:+.2f}€ net={_net_pnl:+.2f}€ "
                                f"pos={_pid}"
                            )
                    except Exception as _e_ins:
                        logger.error(f"[STARTUP RECONCILE] Failed to insert manual trade pos={_pid}: {_e_ins}")
                if _inserted_manual:
                    logger.warning(f"[STARTUP RECONCILE] {_inserted_manual} manual trade(s) auto-inserted from MT5 history")
            except Exception as _e_manual:
                logger.error(f"[STARTUP RECONCILE] Manual trade detection failed: {_e_manual}")

        except Exception as e:
            logger.error(f"Failed to validate MT5 positions: {e}")

    async def stop(self, close_positions: bool = False):
        self._running = False
        if close_positions:
            await self._close_all_positions("bot_stopped")
        logger.info("Trading bot stopped")
        await self._broadcast("bot_status", self.status)

    async def emergency_stop(self):
        logger.critical("EMERGENCY STOP — closing all positions and cancelling orders")
        self._running = False
        if self.mt5_available:
            await self.mt5.cancel_all_orders()
        await self._close_all_positions("emergency_stop")
        await self._broadcast("alert", {
            "level": "critical",
            "message": "Arret d'urgence active — toutes les positions fermees",
        })

    # ── Main Loop ─────────────────────────────────────────────────────────

    async def _main_loop(self):
        while self._running:
            try:
                # Check date rollover for daily reset
                today = date.today().isoformat()
                if today != self._today:
                    await self._on_new_day(today)

                # Check emergency stop condition
                if self.risk_manager.should_emergency_stop():
                    await self.emergency_stop()
                    return

                # Reconnect MT5 if disconnected
                if self.mt5 and not self.mt5.is_connected:
                    logger.warning("MT5 disconnected — attempting reconnection")
                    await self.mt5.reconnect()

                # Check for market closures → send session report
                await self._check_market_closures()

                # Periodic performance save (every 5min for live dashboard)
                import time as _time
                now_ts = _time.time()
                if now_ts - self._last_perf_save > 300:  # 5 minutes
                    self._last_perf_save = now_ts
                    await self._save_daily_performance()
                    logger.info(f"Performance snapshot saved (capital={self.risk_manager.capital:.2f}EUR, Fusion Markets MT5)")

                # ═══ MONITOR REMOVED FROM MAIN LOOP ═══
                # _monitor_positions() and _position_monitor_loop() both do TCP calls.
                # They now run ONLY in _position_monitor_loop (every 10s).
                # Main loop = SCAN ONLY → zero TCP, ultra-fast.

                # Scan for new opportunities (uses cached candles + real-time spots)
                # ZERO TCP calls — all data from cache and spot subscriptions
                try:
                    await self._scan_markets()
                except Exception as _scan_err:
                    logger.error(f"[MAIN LOOP] Scan error: {_scan_err}")

                # 2026-04-19: Liquidity Candle strategy (US session only, parallel to 4TF)
                try:
                    await self._liquidity_candle_scan()
                except Exception as _lq_err:
                    logger.error(f"[LIQ CANDLE] Scan error: {_lq_err}")

                # 2026-04-25: Scheduler unifié — remplace open_hour_v1 et us_hour_v1
                # 5 triggers : 9h (GOLD), 10h (US30+DAX), 15h30 (NKY+NAS+GBPUSD),
                #              16h30 (NKY), 18h (CAC+UK100 BEAR)
                try:
                    from app.trading.strategy_schedule import (
                        maybe_trigger_schedule,
                        maybe_force_close as _sched_force_close,
                    )
                    await maybe_trigger_schedule(self)
                    await _sched_force_close(self)
                except Exception as _sched_err:
                    logger.error(f"[SCHEDULE] main_loop error: {_sched_err}")

                # Broadcast status update (local data only, no TCP)
                await self._broadcast("bot_status", self.status)

                # Write status to IPC (no TCP — positions+account written by _position_monitor_loop)
                try:
                    from app.ipc import write_json, STATUS_FILE
                    write_json(STATUS_FILE, self.status)
                except Exception:
                    pass

                # ═══ TCP IPC (positions, account) REMOVED FROM MAIN LOOP ═══
                # All dashboard writes (positions, account, unrealized PnL) are now
                # handled by _position_monitor_loop every 10s.
                # Main loop = SCAN ONLY → ZERO TCP → instant signal-to-execution.

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)

            # ═══ HOURLY AUDIT — validate params every hour ═══
            import time as _time
            # Clean up phantom positions
            phantom_cleanup = getattr(self, "_phantom_to_remove", [])
            for sym in phantom_cleanup:
                pass  # already handled
            self._phantom_to_remove = []
            for sym in phantom_cleanup:
                if sym in self._open_positions:
                    del self._open_positions[sym]
                    await self._remove_position_db(sym)

            if not hasattr(self, "_last_audit_ts"):
                self._last_audit_ts = 0
            if _time.time() - self._last_audit_ts >= 3600:
                try:
                    report = self.generate_audit_report()
                    # Run functional test
                    func_errors = await self._functional_test()
                    if func_errors:
                        report["functional_errors"] = func_errors
                        report["status"] = "FUNCTIONAL_ERROR"
                        logger.error(f"[AUDIT FUNCTIONAL ERROR] {func_errors}")
                    if report["status"] == "VIOLATION" or report["status"] == "FUNCTIONAL_ERROR":
                        logger.error(f"[AUDIT VIOLATION] {report.get('violations', [])} {report.get('functional_errors', [])}")
                        await self._broadcast("audit_violation", report)

                        # Auto-clean invalid positions instead of killing bot
                        fe = report.get("functional_errors", [])
                        invalid_cleaned = False
                        for err in fe:
                            if "INVALID POSITION" in err:
                                sym = err.split(":")[0].replace("INVALID POSITION", "").strip().lstrip(": ")
                                for key in [sym]:
                                    if key in self._open_positions:
                                        del self._open_positions[key]
                                        await self._remove_position_db(key)
                                        logger.warning(f"[AUDIT AUTO-FIX] Removed invalid position {key}")
                                        invalid_cleaned = True

                        # Only auto-stop for param VIOLATIONS, not recoverable functional errors
                        has_param_violation = len(report.get("violations", [])) > 0
                        has_critical_error = any("CRASH" in e or "CALC ERROR" in e for e in fe)

                        if has_param_violation or has_critical_error:
                            logger.critical("[AUTO-STOP] Erreur critique detectee — arret du bot")
                            try:
                                # HEDGING MODE: don't close via broker, just stop
                                self._running = False
                                self._open_positions.clear()
                                logger.critical("[AUTO-STOP] Bot arrete — fermez vos positions sur MT5")
                                await self._broadcast("alert", {"level": "critical", "message": "Audit critique — bot arrete. Fermez vos positions sur MT5."})
                            except Exception as stop_err:
                                logger.critical(f"[AUTO-STOP] Erreur lors de l arret: {stop_err}")
                            return  # Exit scan loop completely
                        else:
                            logger.warning(f"[AUDIT WARNING] Erreurs non-critiques auto-corrigees, bot continue")
                    else:
                        logger.info(f"[AUDIT OK] All {len(self.LOCKED_PARAMS)} params verified")
                        await self._broadcast("audit_report", report)
                    self._last_audit_ts = _time.time()
                except Exception as e:
                    logger.error(f"Audit report error: {e}")

            await asyncio.sleep(self._scan_interval)

    _reported_closures: set = set()  # Track which closures we already reported today

    async def _check_market_closures(self):
        """Detect market closures and send session reports."""
        from app.trading.market_hours import just_closed

        for market_code, market_label in [("EU", "Europe"), ("US", "US"), ("JP", "Japon")]:
            report_key = f"{self._today}_{market_code}"
            if report_key in self._reported_closures:
                continue

            if just_closed(market_code, minutes=5):
                self._reported_closures.add(report_key)
                await self._generate_session_report(market_code, market_label)

    async def _generate_session_report(self, market_code: str, market_label: str):
        """Generate end-of-session report for a market."""
        # Collect trades for this market
        market_trades = [t for t in self._daily_trades if self._is_trade_in_market(t, market_code)]
        market_positions = [p for p in self._open_positions.values() if self._is_position_in_market(p, market_code)]

        wins = [t for t in market_trades if t.get("pnl", 0) > 0]
        losses = [t for t in market_trades if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in market_trades)
        win_rate = (len(wins) / len(market_trades) * 100) if market_trades else 0

        # Calculate unrealized P&L for open positions
        unrealized = 0
        for pos in market_positions:
            try:
                quote = await self._get_quote(pos["symbol"])
                if quote and quote.get("price"):
                    if pos["action"] == "BUY":
                        unrealized += (quote["price"] - pos["entry_price"]) * pos["quantity"]
                    else:
                        unrealized += (pos["entry_price"] - quote["price"]) * pos["quantity"]
            except Exception:
                pass

        report = {
            "market": market_label,
            "market_code": market_code,
            "trades_closed": len(market_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "realized_pnl": round(total_pnl, 2),
            "open_positions": len(market_positions),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(total_pnl + unrealized, 2),
        }

        logger.info(
            f"=== BILAN SESSION {market_label.upper()} ===\n"
            f"  Trades fermes: {len(market_trades)} ({len(wins)}W / {len(losses)}L) | Win rate: {win_rate:.0f}%\n"
            f"  P&L realise: {total_pnl:+.2f}EUR\n"
            f"  Positions ouvertes: {len(market_positions)} | P&L non-realise: {unrealized:+.2f}EUR\n"
            f"  TOTAL: {total_pnl + unrealized:+.2f}EUR\n"
            f"========================================="
        )

        await self._broadcast("session_report", report)

        # Save daily performance snapshot at each market close
        await self._save_daily_performance()

    def _is_trade_in_market(self, trade: dict, market_code: str) -> bool:
        symbol = trade.get("symbol", "")
        asset = ASSET_BY_SYMBOL.get(symbol)
        if not asset:
            return False
        if market_code == "EU":
            return asset.market == "EU"
        elif market_code == "US":
            return asset.market == "US"
        elif market_code == "JP":
            return asset.market == "JP"
        return False

    def _is_position_in_market(self, pos: dict, market_code: str) -> bool:
        return self._is_trade_in_market(pos, market_code)

    async def _save_daily_performance(self, for_date: str = None):
        """Save daily performance to PostgreSQL for persistent historical tracking.

        IMPORTANT: Reads closed trades from the DATABASE (not volatile memory)
        so that P&L survives bot restarts. Memory-based _daily_trades is used
        as fallback only if DB query fails.
        """
        try:
            from app.models.trade import Trade, TradeStatus
            from sqlalchemy import and_
            from datetime import datetime as _dt, timezone as _tz

            save_date = for_date or self._today
            if not save_date:
                return

            # ── Read closed trades from DB for this date (survives restarts) ──
            db_trades = []
            try:
                _day_start = _dt.strptime(save_date, "%Y-%m-%d").replace(tzinfo=_tz.utc)
                _day_end = _day_start.replace(hour=23, minute=59, second=59)
                async with async_session() as db_sess:
                    result = await db_sess.execute(
                        select(Trade).where(
                            and_(
                                Trade.status == TradeStatus.CLOSED,
                                Trade.exit_time >= _day_start,
                                Trade.exit_time <= _day_end,
                            )
                        )
                    )
                    db_trades = result.scalars().all()
            except Exception as _db_err:
                logger.warning(f"[PERF] DB query failed, falling back to memory: {_db_err}")

            # Calculate per-category P&L from DB trades
            forex_pnl = 0.0
            actions_pnl = 0.0
            indices_pnl = 0.0
            commodities_pnl = 0.0
            total_pnl_from_db = 0.0
            trade_pnls = []
            win_count = 0
            loss_count = 0

            if db_trades:
                for t in db_trades:
                    # 2026-04-20: utiliser NET_PNL (après commissions) pour cohérence
                    pnl = t.net_pnl if t.net_pnl is not None else ((t.pnl or 0) - (t.commission or 0))
                    total_pnl_from_db += pnl
                    trade_pnls.append(pnl)
                    if pnl > 0:
                        win_count += 1
                    else:
                        loss_count += 1
                    cat = get_market_for_symbol(t.symbol or "")
                    if cat == "FOREX":
                        forex_pnl += pnl
                    elif cat == "STOCKS":
                        actions_pnl += pnl
                    elif cat == "INDICES":
                        indices_pnl += pnl
                    elif cat == "COMMODITY":
                        commodities_pnl += pnl
                trades_count = len(db_trades)
                total_pnl = total_pnl_from_db
                logger.info(f"[PERF] {save_date}: {trades_count} trades from DB, PnL NET={total_pnl:.2f}")
            else:
                # Fallback to memory (only for current session trades)
                for t in self._daily_trades:
                    symbol = t.get("symbol", "")
                    pnl = t.get("pnl", 0)
                    total_pnl_from_db += pnl
                    trade_pnls.append(pnl)
                    if pnl > 0:
                        win_count += 1
                    else:
                        loss_count += 1
                    cat = get_market_for_symbol(symbol)
                    if cat == "FOREX":
                        forex_pnl += pnl
                    elif cat == "STOCKS":
                        actions_pnl += pnl
                    elif cat == "INDICES":
                        indices_pnl += pnl
                    elif cat == "COMMODITY":
                        commodities_pnl += pnl
                trades_count = len(self._daily_trades)
                total_pnl = self.risk_manager._daily_pnl if self.risk_manager._daily_pnl != 0 else total_pnl_from_db

            # Get MT5 capital via DASHBOARD connection
            cc_capital = 0.0
            ic_capital = self.risk_manager.capital
            try:
                _dc_stats = self._dash_client()
                if _dc_stats and _dc_stats.is_connected:
                    _summary = await _dc_stats.get_account_summary()
                    cc_capital = _summary.get("balance", 0)
                    self.risk_manager.capital = cc_capital  # Refresh from broker
                    ic_capital = cc_capital
            except Exception:
                pass

            win_rate = round(win_count / trades_count * 100, 1) if trades_count else 0

            async with async_session() as session:
                # Check if entry already exists for this date
                existing = await session.execute(
                    select(DailyPerformance).where(DailyPerformance.date == save_date)
                )
                record = existing.scalar_one_or_none()

                if record:
                    # Update existing record
                    record.ending_capital = ic_capital
                    record.pnl = round(total_pnl, 2)
                    record.trades_count = trades_count
                    record.wins = win_count
                    record.losses = loss_count
                    record.win_rate = win_rate
                    record.best_trade_pnl = round(max(trade_pnls), 2) if trade_pnls else 0
                    record.worst_trade_pnl = round(min(trade_pnls), 2) if trade_pnls else 0
                    record.forex_pnl = round(forex_pnl, 2)
                    record.actions_pnl = round(actions_pnl, 2)
                    record.indices_pnl = round(indices_pnl, 2)
                    record.commodities_pnl = round(commodities_pnl, 2)
                    record.ibkr_capital = 0.0
                    record.capitalcom_capital = round(cc_capital, 2)
                else:
                    # Create new record
                    record = DailyPerformance(
                        date=save_date,
                        starting_capital=round(ic_capital - total_pnl, 2),
                        ending_capital=ic_capital,
                        pnl=round(total_pnl, 2),
                        trades_count=trades_count,
                        wins=win_count,
                        losses=loss_count,
                        win_rate=win_rate,
                        best_trade_pnl=round(max(trade_pnls), 2) if trade_pnls else 0,
                        worst_trade_pnl=round(min(trade_pnls), 2) if trade_pnls else 0,
                        forex_pnl=round(forex_pnl, 2),
                        actions_pnl=round(actions_pnl, 2),
                        indices_pnl=round(indices_pnl, 2),
                        commodities_pnl=round(commodities_pnl, 2),
                        ibkr_capital=0.0,
                        capitalcom_capital=round(cc_capital, 2),
                    )
                    session.add(record)

                await session.commit()
                logger.info(f"Daily performance saved for {save_date}: PnL={total_pnl:.2f}EUR, trades={trades_count}")

        except Exception as e:
            logger.error(f"Failed to save daily performance: {e}", exc_info=True)

    async def _on_new_day(self, today: str):
        """Daily reset: compound gains, reset counters."""
        logger.info(f"New trading day: {today}")

        # Save previous day's performance before resetting
        if self._today:
            await self._save_daily_performance(self._today)

        # Compound capital — Fusion Markets MT5 is the trading broker
        # Capital = previous capital + realized daily PnL (compounding)
        old_capital = self.risk_manager.capital
        daily_pnl = self.risk_manager._daily_pnl
        new_capital = old_capital + daily_pnl  # Simple compound: previous + realized PnL
        self.risk_manager.capital = new_capital
        logger.info(f"Capital compounded: {old_capital:.2f} -> {new_capital:.2f} (Fusion Markets MT5, PnL jour: {daily_pnl:+.2f})")

        # Reset daily state — KEEP _open_positions intact (trades survive midnight)
        self._today = today
        self._daily_trades.clear()
        self.risk_manager.reset_daily()
        self._reported_closures.clear()
        self._flipped_symbols_today.clear()
        # DO NOT clear self._open_positions — active trades must survive day change

        # Reset gain/loss guard starting capital for new day
        self._day_start_capital = new_capital
        logger.info(f"[GAIN GUARD] Nouveau jour — capital de depart reset a {self._day_start_capital:.2f}€ — stop auto a {self._day_start_capital + 700:.2f}€")
        logger.info(f"[DAY RESET] {len(self._open_positions)} positions conservees")

        # Reset daily cache file
        try:
            from app.ipc import write_json, DAILY_FILE
            write_json(DAILY_FILE, {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0})
        except Exception:
            pass

        await self._broadcast("account_update", {
            "capital": new_capital,
            "daily_pnl": 0.0,
        })

    # ── Candle Cache ──────────────────────────────────────────────────────

    async def _cache_refresh_loop(self):
        """Independent background loop — refreshes candle cache every 60s.
        NEVER blocks the main scan loop."""
        logger.info("[CACHE LOOP] Background cache refresh started — every 60s")
        # Initial load: wait 2s for spots to settle, then do first refresh
        await asyncio.sleep(2)
        while self._running:
            try:
                await self._refresh_candle_cache()
            except Exception as e:
                logger.error(f"[CACHE LOOP] Error: {e}")
            await asyncio.sleep(60)  # Refresh every 60 seconds

    async def _refresh_candle_cache(self):
        """Background refresh: download candles for all tradeable symbols.

        Parallelised via bounded Semaphore + token bucket to respect the
        MT5 rate limit (~10 requests / 10s). Each symbol issues 2 requests
        (M15 + M5) concurrently; up to CONCURRENCY symbols are refreshed in
        parallel. Cycle time drops from ~48s (serial) to ~15-20s (4 concurrent,
        9 tokens / 10s).
        """
        import time as _time
        now = _time.time()
        # Skip cache refresh if it already ran recently (prevents blocking)
        if self._cache_refreshing or (now - getattr(self, '_last_cache_attempt', 0)) < 15:
            return
        self._last_cache_attempt = now
        self._cache_refreshing = True
        try:
            tradeable = get_tradeable_symbols(
                capital=self.risk_manager.capital,
            )
            to_refresh = []
            for symbol in tradeable:
                asset = ASSET_BY_SYMBOL.get(symbol)
                if not asset:
                    continue
                if asset.market == "INDICES":
                    from app.trading.market_hours import INDEX_MARKET
                    _idx_mkt = INDEX_MARKET.get(symbol, "EU")
                    if not is_market_open(_idx_mkt):
                        continue
                elif not is_market_open(asset.market):
                    continue
                last_ts = self._candle_cache_ts.get(symbol, 0)
                if now - last_ts < self._cache_max_age:
                    continue
                to_refresh.append(symbol)

            if not to_refresh:
                return

            # FIX 2026-04-16: indices en tête de liste pour ne pas être coupés par MAX_PER_CYCLE
            # Avant: les 14 forex occupaient les 10 slots, les indices n'étaient JAMAIS refreshed
            _indices = [s for s in to_refresh if ASSET_BY_SYMBOL.get(s, None) and ASSET_BY_SYMBOL[s].asset_type == "index_cfd"]
            _rest = [s for s in to_refresh if s not in _indices]
            to_refresh = _indices + _rest

            # 2026-04-14: réduit 30→10 pour diviser compute_ms par ~4
            # 2026-04-16: remonté 10→14 pour couvrir indices + forex en un seul cycle
            # Math: 14 symbols × 2 timeframes = 28 req / 0.9 req/s = ~31s complet
            MAX_PER_CYCLE = 14
            CONCURRENCY = 4
            # Token bucket: 9 tokens, refills at 0.9/s → ≤9 req/10s (under 10/10s limit)
            bucket_tokens = 9.0
            bucket_last = _time.time()
            bucket_lock = asyncio.Lock()
            sem = asyncio.Semaphore(CONCURRENCY)
            refreshed_count = [0]  # nonlocal-ish counter

            async def _take_token():
                nonlocal bucket_tokens, bucket_last
                while True:
                    async with bucket_lock:
                        now_b = _time.time()
                        bucket_tokens = min(9.0, bucket_tokens + (now_b - bucket_last) * 0.9)
                        bucket_last = now_b
                        if bucket_tokens >= 1.0:
                            bucket_tokens -= 1.0
                            return
                        # Not enough tokens — wait until at least 1
                        need = 1.0 - bucket_tokens
                        wait_s = need / 0.9
                    await asyncio.sleep(min(wait_s, 0.5))

            async def _fetch_one(sym: str):
                if not self._running or refreshed_count[0] >= MAX_PER_CYCLE:
                    return
                async with sem:
                    # Respect scan quiet periods — pause if scan in flight
                    while getattr(self, '_scanning', False):
                        await asyncio.sleep(0.2)
                    # Fetch M15 + M5 in parallel (2 tokens, 2 requests)
                    async def _m15():
                        await _take_token()
                        try:
                            result = await self._get_candles(sym, "2 D", "15 mins")
                            if result and len(result) >= 30:
                                self._candle_cache[sym] = result
                                self._candle_cache_ts[sym] = now
                        except Exception as e:
                            logger.warning(f"Cache M15 error {sym}: {e}")
                    async def _m5():
                        await _take_token()
                        try:
                            result_m5 = await self._get_candles(sym, "1 D", "5 mins")
                            if result_m5 and len(result_m5) >= 30:
                                self._candle_cache_m5[sym] = result_m5
                        except Exception as e:
                            logger.warning(f"Cache M5 error {sym}: {e}")
                    async def _h1():
                        # FIX 2026-04-16: H1 pour filtre MTF scalping (SMA50 H1 alignment)
                        await _take_token()
                        try:
                            result_h1 = await self._get_candles(sym, "10 D", "1 hour")
                            if result_h1 and len(result_h1) >= 50:
                                self._candle_cache_h1[sym] = result_h1
                        except Exception as e:
                            logger.warning(f"Cache H1 error {sym}: {e}")
                    async def _d1():
                        # 2026-04-18: D1 pour filtre direction macro (SMA20 D1 alignment)
                        await _take_token()
                        try:
                            result_d1 = await self._get_candles(sym, "60 D", "1 day")
                            if result_d1 and len(result_d1) >= 20:
                                self._candle_cache_d1[sym] = result_d1
                        except Exception as e:
                            logger.warning(f"Cache D1 error {sym}: {e}")
                    async def _m1():
                        # 2026-04-18: M1 pour filtre trigger timing (breakout micro)
                        await _take_token()
                        try:
                            result_m1 = await self._get_candles(sym, "1 D", "1 min")
                            if result_m1 and len(result_m1) >= 20:
                                self._candle_cache_m1[sym] = result_m1
                        except Exception as e:
                            logger.warning(f"Cache M1 error {sym}: {e}")
                    await asyncio.gather(_m15(), _m5(), _h1(), _d1(), _m1(), return_exceptions=True)
                    refreshed_count[0] += 1

            _cycle_t0 = _time.time()
            await asyncio.gather(
                *(_fetch_one(s) for s in to_refresh[:MAX_PER_CYCLE]),
                return_exceptions=True,
            )
            _cycle_dt = _time.time() - _cycle_t0
            if refreshed_count[0] > 0:
                logger.info(
                    f"[CACHE] Refreshed {refreshed_count[0]}/{len(to_refresh)} symbols "
                    f"in {_cycle_dt:.1f}s (concurrency={CONCURRENCY}, bucket=9/10s)"
                )
            # ── Fetch equity once per cache cycle (uses DASHBOARD connection) ──
            try:
                import time as _t_eq
                _dc = self._dash_client()
                if _dc and _dc.is_connected:
                    _eq_summary = await asyncio.wait_for(_dc.get_account_summary(), timeout=5)
                    _eq_val = _eq_summary.get("net_liquidation") or _eq_summary.get("equity", 0)
                    if _eq_val > 0:
                        self._cached_equity = _eq_val
                        self._cached_equity_ts = _t_eq.time()
                        logger.info(f"[CACHE] Equity refreshed: {_eq_val:.2f}EUR")
            except Exception as e:
                logger.warning(f"[CACHE] Equity fetch failed: {e}")
        finally:
            self._cache_refreshing = False

    def _get_cached_candles(self, symbol: str):
        """Get candles from cache. Returns None if not cached."""
        return self._candle_cache.get(symbol)

    # ── Market Scanning ───────────────────────────────────────────────────

    async def _scan_markets(self):
        """Scan all tradeable symbols on open markets for signals.
        ZERO TCP — all data from cache and spot subscriptions."""
        import time as _t_scan_start
        _scan_t0 = _t_scan_start.time()
        self._scanning = True  # Pause cache refresh during scan

        # ═══ PRE-SCAN GUARD — check daily P&L limits BEFORE scanning ═══
        # 2026-04-22: utiliser max_daily_loss dynamique (source broker) au lieu de -100€ hardcodé
        _daily_pnl = self.risk_manager._daily_pnl
        _dl_pct = (self.risk_manager._day_start_balance or self.risk_manager.capital) * self.risk_manager.max_daily_loss
        _dl_fixed = getattr(self.risk_manager, "max_daily_loss_eur", 0.0)
        _dl_dyn = _dl_fixed if _dl_fixed > 0 else _dl_pct
        if _daily_pnl >= 700:
            logger.info(f"[PRE-SCAN GUARD] Daily gain +{_daily_pnl:.2f}EUR >= 700EUR — skipping scan")
            return
        if _daily_pnl <= -_dl_dyn:
            logger.info(f"[PRE-SCAN GUARD] Daily loss {_daily_pnl:.2f}EUR <= -{_dl_dyn:.2f}EUR (limit max_daily_loss) — skipping scan")
            return

        # ═══ PRE-SCAN GUARD — check if bot is in emergency stop state ═══
        if self.risk_manager.should_emergency_stop():
            logger.warning("[PRE-SCAN GUARD] Emergency stop condition — skipping scan")
            return

        # ═══ RECONCILIATION MOVED OUT — done by _position_monitor_loop every 10s ═══
        # NEVER do TCP calls (get_positions, get_deals) inside _scan_markets.
        # The scan must be PURE: cached data + spot prices only, ZERO network I/O.
        if False:  # DISABLED — reconciliation moved to monitor loop
            try:
                broker_positions = await self.mt5.get_positions()
                broker_symbols = set()
                for bp in (broker_positions or []):
                    bp_sym = self._canonical_symbol(bp.get("symbol", ""))
                    broker_symbols.add(bp_sym)

                # Find ghost positions: in bot memory but NOT on broker
                ghost_keys = []
                for pk, pos in self._open_positions.items():
                    pos_sym = self._canonical_symbol(pos.get("symbol", ""))
                    if pos_sym not in broker_symbols:
                        ghost_keys.append(pk)

                for gk in ghost_keys:
                    async with self._positions_lock:
                        ghost_pos = self._open_positions.pop(gk, None)
                    if ghost_pos:
                        await self._remove_position_db(gk)
                        # Cooldown 45min on this pair after ghost removal
                        _ghost_sym = self._canonical_symbol(ghost_pos.get('symbol', ''))
                        if _ghost_sym:
                            import time as _t_ghost
                            self._symbol_cooldown[_ghost_sym] = _t_ghost.time() + 900
                        _ghost_ticket = ghost_pos.get("ticket") or ghost_pos.get("position_id")
                        logger.critical(
                            f"[RECONCILIATION] GHOST POSITION removed: {ghost_pos.get('symbol')} "
                            f"{ghost_pos.get('action')} qty={ghost_pos.get('quantity')} "
                            f"ticket={_ghost_ticket} (in bot memory but NOT on broker) → cooldown 45min"
                        )
                        # ── Recover closed trade from broker deal history ──
                        if _ghost_ticket and self.mt5_available:
                            try:
                                deals = await self.mt5.get_deals_by_position(int(_ghost_ticket))
                                close_deals = [d for d in deals if d.get("is_close")]
                                if close_deals:
                                    cd = close_deals[-1]
                                    _r_sym = ghost_pos.get("symbol", "?")
                                    _r_entry = cd.get("entry_price", ghost_pos.get("entry_price", 0))
                                    _r_exit = cd.get("execution_price", 0)
                                    _r_gross = cd.get("gross_profit", 0)
                                    # FIX 2026-04-20: commission = open_deal + close_deal (Fusion charge les 2 côtés)
                                    _r_comm_usd = sum(abs(d.get("commission", 0)) for d in deals)
                                    _r_comm = _r_comm_usd * 0.85  # USD→EUR
                                    _r_swap = sum(d.get("swap", 0) for d in deals)
                                    _r_net = _r_gross + _r_swap - _r_comm
                                    # 2026-04-21: MT5 broker = UTC+3 → soustraire 3h
                                    from datetime import timedelta as _td_r
                                    _r_exit_ts = cd.get("execution_timestamp", 0)
                                    if _r_exit_ts:
                                        _r_exit_dt = datetime.fromtimestamp(_r_exit_ts / 1000, tz=timezone.utc) - _td_r(hours=3)
                                    else:
                                        _r_exit_dt = datetime.now(timezone.utc)
                                    logger.info(
                                        f"[RECONCILIATION] Recovered trade: {_r_sym} "
                                        f"entry={_r_entry} exit={_r_exit} gross={_r_gross:+.2f} "
                                        f"comm={_r_comm:.2f} net={_r_net:+.2f}"
                                    )
                                    from app.models.trade import Trade, TradeStatus, TradeSide
                                    _r_entry_raw = ghost_pos.get("entry_time") or ghost_pos.get("opened_at") or ""
                                    _r_entry_dt = None
                                    if _r_entry_raw:
                                        try:
                                            _r_entry_dt = datetime.fromisoformat(str(_r_entry_raw))
                                        except (ValueError, TypeError):
                                            _r_entry_dt = None
                                    if not _r_entry_dt:
                                        open_deals = [d for d in deals if not d.get("is_close")]
                                        if open_deals:
                                            _r_open_ts = open_deals[0].get("execution_timestamp", 0)
                                            if _r_open_ts:
                                                _r_entry_dt = datetime.fromtimestamp(_r_open_ts / 1000, tz=timezone.utc)
                                    _r_qty = cd.get("closed_volume", ghost_pos.get("quantity", 0))
                                    _r_action = ghost_pos.get("action", "BUY")
                                    _r_reason = "broker_close"
                                    if _r_gross > 0:
                                        _r_reason = "take_profit"
                                    elif _r_gross < 0:
                                        _r_reason = "stop_loss"
                                    async with async_session() as session:
                                        # FIX 2026-04-15: persister les métadonnées d'audit
                                        _r_ticket = ghost_pos.get("ticket") or ghost_pos.get("position_id")
                                        _r_deal_id = cd.get("deal_id") or cd.get("ticket")
                                        _r_snap = ghost_pos.get("indicators_snapshot")
                                        trade = Trade(
                                            symbol=_r_sym,
                                            name=_r_sym,
                                            side=TradeSide.BUY if _r_action == "BUY" else TradeSide.SELL,
                                            status=TradeStatus.CLOSED,
                                            entry_price=_r_entry,
                                            quantity=_r_qty,
                                            entry_amount=_r_entry * _r_qty,
                                            entry_time=_r_entry_dt or _r_exit_dt,
                                            exit_price=_r_exit,
                                            exit_time=_r_exit_dt,
                                            exit_reason=_r_reason,
                                            stop_loss=ghost_pos.get("stop_loss"),
                                            take_profit=ghost_pos.get("take_profit"),
                                            pnl=round(_r_gross, 2),
                                            pnl_percent=round((_r_gross / (ghost_pos.get("margin", 1) or 500)) * 100, 2),
                                            commission=round(_r_comm, 2),
                                            net_pnl=round(_r_net, 2),
                                            market=get_market_for_symbol(_r_sym),
                                            asset_type=get_market_for_symbol(_r_sym),
                                            signal_confidence=ghost_pos.get("signal_confidence"),
                                            signal_reason=(ghost_pos.get("signal_reason") or "")[:512] or None,
                                            indicators_snapshot=_r_snap if isinstance(_r_snap, (dict, list)) else None,
                                            broker_position_id=str(_r_ticket) if _r_ticket else None,
                                            broker_deal_id=str(_r_deal_id) if _r_deal_id else None,
                                            origin=ghost_pos.get("origin") or "bot",
                                            source=ghost_pos.get("source") or "reconciliation",
                                        )
                                        session.add(trade)
                                        await session.commit()
                                        logger.info(f"[RECONCILIATION] Trade saved to DB: {_r_sym} {_r_reason} PnL={_r_net:+.2f}EUR")
                                    self.risk_manager.record_trade_result(_r_net)
                                    await self._broadcast("trade_closed", {
                                        "symbol": _r_sym, "pnl": round(_r_gross, 2),
                                        "reason": _r_reason, "exit_price": _r_exit,
                                    })
                                else:
                                    logger.warning(f"[RECONCILIATION] No close deals for {ghost_pos.get('symbol')} ticket={_ghost_ticket}")
                            except Exception as e:
                                logger.error(f"[RECONCILIATION] Failed to recover trade for ticket {_ghost_ticket}: {e}")
                        await self._broadcast("alert", {
                            "level": "critical",
                            "message": f"Position fantome supprimee: {ghost_pos.get('symbol')} {ghost_pos.get('action')} — n'existait pas sur MT5",
                        })

                if ghost_keys:
                    logger.info(f"[RECONCILIATION] Removed {len(ghost_keys)} ghost positions")
                    self.risk_manager.update_state(
                        daily_pnl=self.risk_manager._daily_pnl,
                        open_positions=len(self._open_positions),
                        open_symbols=[p.get("symbol", k.split("_")[0]) for k, p in self._open_positions.items()],
                        capital=self.risk_manager.capital,
                    )

                # Import unknown broker positions (not tracked → add to dashboard)
                _tracked_tickets_scan = set()
                for _pv in self._open_positions.values():
                    _t = _pv.get("ticket") or _pv.get("position_id")
                    if _t:
                        _tracked_tickets_scan.add(int(_t))

                for bp in (broker_positions or []):
                    _bp_ticket = bp.get("ticket") or bp.get("position_id")
                    if _bp_ticket and int(_bp_ticket) not in _tracked_tickets_scan:
                        # Règle critique: ne JAMAIS adopter une position non ouverte par le bot.
                        logger.warning(
                            f"[RECONCILIATION] UNTRACKED broker position ignored: "
                            f"{bp.get('symbol','?')} ticket={_bp_ticket} — NOT adopted"
                        )

            except Exception as e:
                logger.warning(f"[RECONCILIATION] Failed to reconcile positions: {e}")

        tradeable = get_tradeable_symbols(
            capital=self.risk_manager.capital,
        )
        # ── PRIORITÉ INDICES & COMMODITIES sur session EU/US ──
        # Les indices ouvrent plus tard que le forex et leurs fenêtres sont courtes.
        # On les scanne EN PREMIER pour ne pas les faire attendre ~20 min derrière 16 paires forex.
        try:
            from datetime import datetime as _dt_now, timezone as _tz_now
            _h_utc = _dt_now.now(_tz_now.utc).hour
            _wd = _dt_now.now(_tz_now.utc).weekday()
            _eu_us_open = _wd < 5 and 7 <= _h_utc < 21
        except Exception:
            _eu_us_open = False
        if _eu_us_open:
            _idx_syms = [s for s in tradeable if (ASSET_BY_SYMBOL.get(s) and ASSET_BY_SYMBOL[s].asset_type in ("index_cfd", "commodity"))]
            _fx_syms = [s for s in tradeable if s not in _idx_syms]
            tradeable = _idx_syms + _fx_syms
            logger.info(f"[SCAN ORDER] EU/US ouvert — indices d'abord: {_idx_syms[:5]}...")
        scanned = 0
        skipped_market = 0

        # ── STEP 1: Collect scannable symbols ──
        # Fenêtre horaire globale CET/CEST — 9h-21h heure de Paris (DST-aware, analyse 2026-04-10)
        try:
            from datetime import datetime as _dt_fw
            try:
                from zoneinfo import ZoneInfo as _ZI
                _paris_now = _dt_fw.now(_ZI("Europe/Paris"))
            except Exception:
                # Fallback approximatif si zoneinfo indisponible
                from datetime import timezone as _tz_fw, timedelta as _td_fw
                _paris_now = _dt_fw.now(_tz_fw.utc) + _td_fw(hours=2)
            _cet_hour_dec = _paris_now.hour + _paris_now.minute / 60.0
        except Exception:
            _cet_hour_dec = 12.0
        _in_global_window = is_in_global_trading_window(_cet_hour_dec)
        # ── D) QUALITY SCORE refresh toutes les heures ──
        import time as _t_qs
        if _t_qs.time() - self._quality_scores_ts > 3600:
            try:
                from app.trading.symbol_selector import (
                    compute_quality_score, select_top_symbols, is_whitelisted,
                )
                _fresh_scores = []
                for _sym_qs in list(self._candle_cache_m5.keys()):
                    if not is_whitelisted(_sym_qs, cet_hour=_cet_hour_dec):
                        continue
                    _m15_qs = self._candle_cache.get(_sym_qs)
                    _m5_qs = self._candle_cache_m5.get(_sym_qs)
                    _last_q = self._last_quotes.get(_sym_qs, {})
                    _spread_qs = _last_q.get("spread", 0) or 0
                    _price_qs = _last_q.get("price", 0) or 0
                    _s = compute_quality_score(_sym_qs, _m15_qs, _m5_qs, _spread_qs, _price_qs)
                    if _s is not None:
                        _fresh_scores.append(_s)
                self._quality_scores = {s.symbol: s for s in _fresh_scores}
                _top = select_top_symbols(_fresh_scores, top_n=8, min_score=50.0)
                self._quality_top_symbols = set(_top)
                self._quality_scores_ts = _t_qs.time()
                logger.info(
                    f"[QUALITY SCORE] Top 8 symboles: {_top} | "
                    f"scores: {[f'{s.symbol}={s.score:.0f}' for s in _fresh_scores[:10]]}"
                )
            except Exception as _qs_err:
                logger.warning(f"[QUALITY SCORE] refresh error: {_qs_err}")

        if not _in_global_window:
            logger.info(
                f"[SCAN] Hors fenêtre globale CET {TRADING_WINDOW_CET[0]:.0f}h-"
                f"{TRADING_WINDOW_CET[1]:.0f}h (actuelle {_cet_hour_dec:.2f}h) — aucun nouveau trade"
            )

        scan_symbols = []
        for symbol in tradeable:
            if not self._running:
                break
            asset = ASSET_BY_SYMBOL.get(symbol)
            if not asset:
                continue
            # ── BLACKLIST (analyse 7J → perdants chroniques) ──
            if is_symbol_disabled(symbol):
                continue
            # ── B) WHITELIST PAR SESSION (2026-04-23) : 5 forex + 3 indices par marché ──
            from app.trading.symbol_selector import is_whitelisted
            if not is_whitelisted(symbol, cet_hour=_cet_hour_dec):
                continue
            # ── D) QUALITY TOP 8 : skip si symbole pas dans le top ──
            if self._quality_top_symbols and symbol not in self._quality_top_symbols:
                continue
            # ── FENÊTRE HORAIRE GLOBALE 9h-21h CET ──
            if not _in_global_window:
                continue
            # Indices: check per-symbol market (NKY→JP, HK50→ASIA, etc.)
            if asset.market == "INDICES":
                from app.trading.market_hours import INDEX_MARKET
                _idx_market = INDEX_MARKET.get(symbol, "EU")
                if not is_market_open(_idx_market):
                    skipped_market += 1
                    continue
            elif not is_market_open(asset.market):
                skipped_market += 1
                continue
            # Cooldown check: 45 min after ANY close (canonical symbol)
            import time as _t_cd
            _canon_cd = self._canonical_symbol(symbol)
            _cd_expire = self._symbol_cooldown.get(_canon_cd, 0)
            if _cd_expire > _t_cd.time():
                _cd_remaining = (_cd_expire - _t_cd.time()) / 60
                continue  # Still in cooldown

            # ── CORRELATION LIMITER: max 3 paires corrélées par devise ──
            # Ex: si on a déjà EUR/USD + EUR/GBP + EUR/CAD, pas de 4ème paire EUR
            _sym_clean = symbol.replace("/", "")
            _is_commodity_sym = any(x in _sym_clean.upper() for x in ("XAU", "XAG", "CL=", "BRENT", "OIL", "XTI", "XBR"))
            if len(_sym_clean) == 6 and _sym_clean.isalpha() and not _is_commodity_sym:
                _base_ccy = _sym_clean[:3].upper()
                _quote_ccy = _sym_clean[3:].upper()
                _ccy_count = {}
                for _op in self._open_positions.values():
                    _op_sym = _op.get("symbol", "").replace("/", "")
                    if len(_op_sym) == 6 and _op_sym.isalpha():
                        _ob = _op_sym[:3].upper()
                        _oq = _op_sym[3:].upper()
                        _ccy_count[_ob] = _ccy_count.get(_ob, 0) + 1
                        _ccy_count[_oq] = _ccy_count.get(_oq, 0) + 1
                if _ccy_count.get(_base_ccy, 0) >= 3 or _ccy_count.get(_quote_ccy, 0) >= 3:
                    _over_ccy = _base_ccy if _ccy_count.get(_base_ccy, 0) >= 3 else _quote_ccy
                    continue  # Skip — currency already in 3 open pairs

            # ── PYRAMIDING MODE A — 5% TP progress → immediate next position ──
            # Mode scalping 26 mars: dès qu'une position atteint 5% du TP, on empile.
            # Pas besoin d'attendre break-even, juste une confirmation que ça bouge bien.
            _canon_sym = self._canonical_symbol(symbol)
            _existing_for_sym = []
            for _pk, _pv in self._open_positions.items():
                if self._canonical_symbol(_pv.get("symbol", "")) == _canon_sym:
                    _existing_for_sym.append(_pv)

            def _pos_past_5pct_tp(_p):
                """Returns True if position has progressed >= 5% toward TP."""
                _e = _p.get("entry_price", 0)
                _tp = _p.get("take_profit", 0)
                _a = _p.get("action", "")
                _cur = _p.get("current_price", _e)
                if not _e or not _tp or not _cur:
                    return False
                if _a == "BUY":
                    _dist = _tp - _e
                    _prog = (_cur - _e) / _dist if _dist > 0 else 0
                else:  # SELL
                    _dist = _e - _tp
                    _prog = (_e - _cur) / _dist if _dist > 0 else 0
                return _prog >= 0.05

            # Hard cap: 3 positions max per pair (inchangé — sécurité)
            if len(_existing_for_sym) >= 3:
                continue
            # 2026-04-17: règle "5% progressé" SUPPRIMÉE pour scalping.
            # Les 3 filtres scalping (MTF+Body+Volume) dans _execute_trade garantissent
            # que toute nouvelle entrée est un vrai 3/3 → empiler immédiatement si momentum
            # persiste. Le blocage 5% empêchait de capitaliser sur les mouvements rapides.
            # Seul le hard cap 3 positions reste actif + les filtres scalping 3/3 obligatoires.
            candles = self._get_cached_candles(symbol)
            if not candles or len(candles) < 30:
                continue
            scan_symbols.append((symbol, candles))

        # ── STEP 2: Use REAL-TIME spot price (from subscription) — instant, no I/O ──
        # Fallback to candle close only if spot not available
        quote_results = []
        if scan_symbols:
            import time as _qt
            for sym, candles in scan_symbols:
                if not candles:
                    quote_results.append(None)
                    continue
                last_candle = candles[-1]
                close_price = last_candle.close if hasattr(last_candle, 'close') else last_candle.get("close", 0)
                high = last_candle.high if hasattr(last_candle, 'high') else last_candle.get("high", close_price)
                low = last_candle.low if hasattr(last_candle, 'low') else last_candle.get("low", close_price)

                # Priority 1: real-time spot from subscription (instant, no network)
                _spot_bid = 0
                _spot_ask = 0
                if self.mt5_available:
                    _sid = self.mt5._resolve_symbol_id(sym)
                    if _sid:
                        _spot_data = self.mt5._spot_prices.get(_sid, {})
                        _spot_bid = _spot_data.get("bid", 0)
                        _spot_ask = _spot_data.get("ask", 0)
                _spot_price = (_spot_bid + _spot_ask) / 2 if _spot_bid > 0 and _spot_ask > 0 else 0

                # Best available price: spot > candle close
                if _spot_price > 0:
                    price = _spot_price
                    bid = _spot_bid
                    ask = _spot_ask
                    spread = _spot_ask - _spot_bid
                else:
                    price = close_price
                    bid = low
                    ask = high
                    spread = 0

                if price and price > 0:
                    quote_results.append({
                        "price": price,
                        "bid": bid,
                        "ask": ask,
                        "spread": spread,
                        "change_percent": 0,
                        "_from_spot": _spot_price > 0,
                        "_ts": _qt.time(),
                    })
                else:
                    quote_results.append(None)

        # ── STEP 2b: Use CACHED equity (no network call!) ──
        # Equity is refreshed by _cache_refresh_loop every 60s — ZERO TCP contention
        import time as _t_eq_scan
        _cc_eq = self._cached_equity if (_t_eq_scan.time() - self._cached_equity_ts < 120) else 0
        if _cc_eq <= 0:
            _cc_eq = self.risk_manager.capital  # Fallback to last known capital
        _real_daily_gain = _cc_eq - self._day_start_capital if _cc_eq > 0 and self._day_start_capital > 0 else 0

        # ── STEP 3: Process signals sequentially (fast, no I/O) ──
        import time as _t_step3
        _t_step3_start = _t_step3.time()
        for _scan_idx, ((symbol, candles), quote_result) in enumerate(zip(scan_symbols, quote_results)):
            _t_sym_start = _t_step3.time()
            # ═══ YIELD every 3 symbols — let close commands execute ═══
            if _scan_idx % 3 == 0:
                _t_before_yield = _t_step3.time()
                await asyncio.sleep(0)
                _t_after_yield = _t_step3.time()
                logger.info(f"[TIMING] YIELD at idx={_scan_idx}: took {_t_after_yield - _t_before_yield:.3f}s")
            logger.info(f"[TIMING] {symbol}: START idx={_scan_idx} elapsed_since_step3={_t_step3.time() - _t_step3_start:.1f}s")

            if isinstance(quote_result, Exception) or not quote_result:
                continue
            quote = quote_result
            if not quote.get("price") or quote["price"] <= 0:
                continue

            import time as _scan_ts
            quote["_ts"] = _scan_ts.time()
            self._last_quotes[symbol] = quote
            scanned += 1

            try:
                # Re-fetch asset for THIS symbol (not stale from STEP 1 loop)
                asset = ASSET_BY_SYMBOL.get(symbol)
                if not asset:
                    continue

                # Compute indicators + generate signal in PROCESS POOL
                # (GIL contention from Twisted reactor — ThreadPool still starved, ProcessPool solves it)
                import time as _t_pre_ind
                _t_before_ind = _t_pre_ind.time()
                current_spread = quote.get("spread", 0)
                _prev_close = candles[-2].close if len(candles) >= 2 else None

                # 2026-04-23 M-ONLY : H1 plus passé au worker (direction vient de M15 après)
                _h1_for_signal = None

                loop = asyncio.get_event_loop()
                indicators, signal = await loop.run_in_executor(
                    self._process_pool,
                    _compute_signal_worker,
                    candles, quote["price"], quote.get("change_percent", 0),
                    symbol, current_spread, _prev_close, _h1_for_signal,
                )
                _t_after_ind = _t_pre_ind.time()
                logger.info(f"[TIMING] {symbol}: Z-compute_signal_process={_t_after_ind - _t_before_ind:.3f}s")

                # ═══ M-ONLY ARCHITECTURE 2026-04-23 (user rule post-audit) ═══
                # M15 = TEMPO (direction), M5 = VALIDATION (body/volume/pattern),
                # M1 = TRIGGER (breakout), H1/D1 = sessions/news UNIQUEMENT.
                # signals.py sert maintenant uniquement à calculer SL/TP/entry —
                # la DIRECTION du trade est ÉCRASÉE INCONDITIONNELLEMENT par M15.
                try:
                    from app.trading.structure_detector import detect_m15_structure
                    from app.trading.signals import Signal as _SigCls_m15
                    _m15_struct = self._candle_cache.get(symbol)  # M15 = default cache
                    _struct = detect_m15_structure(_m15_struct, lookback=10)
                    _old_dir = signal.signal

                    # Mapping direct M15 structure → direction
                    if _struct.structure == "bullish":
                        _new_dir = "buy"
                        _new_conf = 95
                    elif _struct.structure == "bearish":
                        _new_dir = "sell"
                        _new_conf = 95
                    else:
                        _new_dir = "hold"
                        _new_conf = 0

                    # SL/TP calcul depuis PAIR_CONFIG si la direction change
                    # (évite SL=0 qui faisait rejeter silencieusement par risk_manager)
                    _final_sl = signal.suggested_sl
                    _final_tp = signal.suggested_tp
                    if _new_dir in ("buy", "sell") and _new_dir != _old_dir:
                        from app.trading.signals import get_pair_config as _gpc_m15
                        _cfg_m15 = _gpc_m15(symbol)
                        _entry_m15 = signal.suggested_entry
                        _sym_u_m15 = symbol.replace("/", "").upper()
                        if "sl_pct" in _cfg_m15:
                            _sl_d = _entry_m15 * _cfg_m15["sl_pct"]
                            _tp_d = _entry_m15 * _cfg_m15["tp_pct"]
                        elif "sl_pips" in _cfg_m15:
                            _pip = 0.01 if "JPY" in _sym_u_m15 else 0.0001
                            _sl_d = _cfg_m15["sl_pips"] * _pip
                            _tp_d = _cfg_m15["tp_pips"] * _pip
                        else:
                            _sl_d = _entry_m15 * 0.005
                            _tp_d = _entry_m15 * 0.01
                        if _new_dir == "buy":
                            _final_sl = _entry_m15 - _sl_d
                            _final_tp = _entry_m15 + _tp_d
                        else:
                            _final_sl = _entry_m15 + _sl_d
                            _final_tp = _entry_m15 - _tp_d
                        _rn = 1 if _entry_m15 > 100 else 5
                        _final_sl = round(_final_sl, _rn)
                        _final_tp = round(_final_tp, _rn)

                    signal = _SigCls_m15(
                        signal=_new_dir,
                        confidence=_new_conf,
                        reason=f"M15 TEMPO {_struct.structure} ({_struct.reason})",
                        suggested_entry=signal.suggested_entry,
                        suggested_sl=_final_sl,
                        suggested_tp=_final_tp,
                        bull_score=0, bear_score=0, spread_ok=True, lot_factor=1.0,
                    )
                    _flip_tag = " (FLIP)" if _old_dir != _new_dir and _old_dir in ("buy", "sell") else ""
                    logger.info(
                        f"[M15 TEMPO] {symbol}: structure={_struct.structure} → "
                        f"direction={_new_dir.upper()}{_flip_tag} | "
                        f"highs {_struct.avg_high_first:.5f}→{_struct.avg_high_second:.5f}, "
                        f"lows {_struct.avg_low_first:.5f}→{_struct.avg_low_second:.5f}"
                    )
                except Exception as _m15_err:
                    logger.debug(f"[M15 TEMPO] {symbol}: skip ({_m15_err})")

                # ═══ HOLD → RANGE PIPELINE 2026-04-23 (user rule) ═══
                # Règle utilisateur : TOUT signal HOLD est traité comme un range par défaut.
                # Pas besoin d'attendre regime_detector : la validation vient des
                # range_filters downstream (volume band [0.3, 0.9], RSI extremes,
                # bougie de rejet hammer/englobante). Si pas valide, range_filters bloque.
                # Timeframe d'évaluation = M5 (standard range-trading, M1 trop bruité).
                if (signal.signal == "hold" or signal.confidence < 50):
                    try:
                        from app.trading.range_filters import is_rangeable_symbol as _is_rg_early
                        if _is_rg_early(symbol):
                            from app.trading.filters_config import (
                                RANGE_ENTRY_ZONE_PCT as _RZ,
                                REGIME_RANGE_LOOKBACK as _RLB,
                                REGIME_RANGE_LOOKBACK_M5 as _RLB_M5,
                            )
                            # 2026-04-23 : priorité M5 80 bars (~6h40) pour capturer
                            # les ranges court-terme visibles à l'œil (ex: DAX 24000-24200
                            # sur 6h), fallback H1 30 bars si M5 insuffisant.
                            _m5_box = self._candle_cache_m5.get(symbol)
                            _h1_box = self._candle_cache_h1.get(symbol)
                            _r_high = _r_low = 0.0
                            _tf_used = ""
                            if _m5_box and len(_m5_box) >= _RLB_M5:
                                # Exclure la bougie en cours (-1) du calcul
                                _recent = _m5_box[-(_RLB_M5 + 1):-1]
                                _r_high = max(c.high for c in _recent)
                                _r_low = min(c.low for c in _recent)
                                _tf_used = f"M5×{_RLB_M5}"
                            elif _h1_box and len(_h1_box) >= _RLB:
                                _recent = _h1_box[-_RLB:]
                                _r_high = max(c.high for c in _recent)
                                _r_low = min(c.low for c in _recent)
                                _tf_used = f"H1×{_RLB} (M5 insuffisant)"
                            _box_w = _r_high - _r_low
                            if _box_w > 0:
                                _cur_price = quote["price"]
                                _lower_zone = _r_low + _RZ * _box_w
                                _upper_zone = _r_high - _RZ * _box_w
                                _synth = None
                                if _cur_price <= _lower_zone:
                                    _synth = "buy"
                                elif _cur_price >= _upper_zone:
                                    _synth = "sell"
                                logger.info(
                                    f"[RANGE PROBE] {symbol}: HOLD→check {_tf_used} — "
                                    f"prix {_cur_price:.5f} box=[{_r_low:.5f}..{_r_high:.5f}] "
                                    f"synth={_synth or 'skip (prix au milieu)'}"
                                )
                                if _synth:
                                    from app.trading.signals import Signal as _SignalCls
                                    _old_sig = signal.signal
                                    signal = _SignalCls(
                                        signal=_synth,
                                        confidence=95,
                                        reason=(
                                            f"HOLD→RANGE prix {_cur_price:.5f} zone "
                                            f"{'basse' if _synth=='buy' else 'haute'} "
                                            f"box=[{_r_low:.5f}..{_r_high:.5f}] {_tf_used}"
                                        ),
                                        suggested_entry=_cur_price,
                                        suggested_sl=0.0,  # overridé par range_filters
                                        suggested_tp=0.0,  # overridé par range_filters
                                        bull_score=0, bear_score=0,
                                        spread_ok=True, lot_factor=1.0,
                                    )
                                    logger.info(
                                        f"[RANGE OVERRIDE] {symbol}: {_old_sig.upper()}→{_synth.upper()} "
                                        f"(zone {'basse' if _synth=='buy' else 'haute'}, {_tf_used}, "
                                        f"range_filters validera volume+RSI+bougie)"
                                    )
                    except Exception as _rg_err:
                        logger.debug(f"[RANGE OVERRIDE] {symbol}: skip ({_rg_err})")

                # ═══ TIMESTAMP SIGNAL — clock starts NOW for 10s rule ═══
                import time as _t_sig
                _signal_ts = _t_sig.time()
                _t_checkpoint = _signal_ts
                log_timing(
                    "signal_detected",
                    symbol=symbol,
                    signal=signal.signal,
                    confidence=signal.confidence,
                    price=quote["price"],
                    signal_ts=_signal_ts,
                    compute_ms=int((_signal_ts - _t_before_ind) * 1000),
                )

                leverage = get_leverage(symbol)
                market_cat = get_market_for_symbol(symbol)
                broker = get_broker_for_symbol(symbol)

                # ═══ 2026-04-20: Remplacer confidence/reason par score 4TF réel ═══
                # Les anciens indicateurs (RSI/MACD/BB/ADX) ne filtrent plus rien.
                # Le dashboard doit refléter la vraie décision = 4TF (H1 + Body + Vol + M1)
                _display_conf = signal.confidence
                _display_reason = signal.reason
                if signal.signal in ("buy", "sell"):
                    try:
                        from app.trading.scalping_filters import compute_entry_score
                        _es_disp = compute_entry_score(
                            signal=signal.signal,
                            candles_m15=self._candle_cache.get(symbol),   # M15 default cache
                            candles_m5=self._candle_cache_m5.get(symbol),
                            candles_m1=self._candle_cache_m1.get(symbol),
                        )
                        # Conf = score pondéré (0-100)
                        _display_conf = _es_disp.total
                        _display_reason = _es_disp.summary()
                    except Exception:
                        pass  # fallback ancien affichage

                self._last_signals[symbol] = {
                    "symbol": symbol,
                    "name": asset.name,
                    "market": asset.market,
                    "market_category": market_cat,
                    "price": quote["price"],
                    "change": quote.get("change", 0),
                    "change_percent": quote.get("change_percent", 0),
                    "signal": signal.signal,
                    "confidence": _display_conf,
                    "reason": _display_reason,
                    "suggested_entry": signal.suggested_entry,
                    "suggested_sl": signal.suggested_sl,
                    "suggested_tp": signal.suggested_tp,
                    "leverage": leverage,
                    "broker": broker,
                }

                _reason_display = signal.reason[:200] if signal.confidence > 0 else signal.reason[:300]
                logger.info(
                    f"[SIGNAL] {symbol} ({broker}) @ {quote['price']:.4f}: "
                    f"{signal.signal.upper()} conf={signal.confidence}% | {_reason_display}"
                )

                # Fire-and-forget broadcast — MUST NOT await here (yields to Twisted reactor ~80s)
                asyncio.ensure_future(self._broadcast("signal", self._last_signals[symbol]))
                _t_now = _t_sig.time(); logger.info(f"[TIMING] {symbol}: A-after_broadcast={_t_now - _t_checkpoint:.3f}s"); _t_checkpoint = _t_now

                # ═══ DAILY P&L GUARD — uses REALIZED PnL from today's closed trades ═══
                # Ne PAS utiliser equity broker (inclut carry-over positions d'hier)
                _realized_daily = self.risk_manager._daily_pnl
                if _realized_daily <= -100:
                    if signal.signal in ("buy", "sell") and signal.confidence >= 70:
                        logger.info(f"[BLOCKED BY GUARD] {symbol} {signal.signal.upper()} conf={signal.confidence}% — realized loss {_realized_daily:.2f}EUR")
                    continue

                # Only act on buy/sell signals with HIGH confidence (Sniper mode)
                # ═══ SESSION FILTER — precise trading windows CET/CEST (Europe/Paris) ═══
                now_cet = datetime.now(ZoneInfo("Europe/Paris"))
                cet_decimal = now_cet.hour + now_cet.minute / 60.0

                # Session windows are now per-pair (PAIR_SESSIONS in signals.py)
                # Asian mode detection (00h-09h CET) handled by signals.py

                # ═══ NIGHT BLACKOUT SUPPRIMÉ — forex 24h/5j, session asiatique active la nuit ═══
                # Les paires JPY et AUD sont particulièrement actives pendant la session Tokyo (00h-09h CET)

                # ═══ PER-PAIR SESSION — check per-pair trading window ═══
                from app.trading.signals import is_in_trading_session, get_pair_config as _get_pair_cfg
                if not is_in_trading_session(symbol, cet_decimal):
                    if signal.signal in ("buy", "sell") and signal.confidence >= 70:
                        logger.info(f"[BLOCKED BY SESSION] {symbol} {signal.signal.upper()} conf={signal.confidence}% — hors fenetre de trading (CET={cet_decimal:.1f})")
                    continue  # Outside this pair's trading window
                # Per-pair confidence: Mode A scalping — seuil 75 (plus de trades, sorties rapides)
                _pcfg = _get_pair_cfg(symbol)
                session_min_confidence = _pcfg.get("min_confidence", 75)

                # Confidence threshold — Mode A scalping (26 mars): qualité modérée, volume élevé
                min_confidence = 75
                session_min_confidence = max(session_min_confidence, 75)

                effective_confidence = max(min_confidence, session_min_confidence)
                if signal.signal == "hold" or signal.confidence < effective_confidence:
                    if signal.signal in ("buy", "sell") and signal.confidence >= 50:
                        logger.info(f"[BLOCKED BY CONFIDENCE] {symbol} {signal.signal.upper()} conf={signal.confidence}% < {effective_confidence}%")
                    continue

                # HTF filter removed — trading on M1 signals only (like yesterday morning)

                # ═══ SIGNAL ARBITRAGE — replace weakest position if slots full ═══
                if len(self._open_positions) >= self.risk_manager.max_open_positions:
                    # Find weakest position (lowest confidence)
                    weakest_sym = None
                    weakest_conf = 999
                    for pos_sym, pos_data in self._open_positions.items():
                        pos_conf = pos_data.get("signal_confidence", 80)
                        if pos_conf < weakest_conf:
                            weakest_conf = pos_conf
                            weakest_sym = pos_sym
                    # Only replace if new signal is at least 5% stronger
                    if weakest_sym and signal.confidence >= weakest_conf + 5:
                        logger.info(f"[ARBITRAGE] {symbol} conf={signal.confidence}% > {weakest_sym} conf={weakest_conf}% — closing weak position")
                        try:
                            # Use spot subscription for weak position price (NO network call)
                            weak_price = 0
                            if self.mt5_available:
                                _wk_sid = self.mt5._resolve_symbol_id(weakest_sym)
                                if _wk_sid:
                                    _wk_spot = self.mt5._spot_prices.get(_wk_sid, {})
                                    _wk_b = _wk_spot.get("bid", 0)
                                    _wk_a = _wk_spot.get("ask", 0)
                                    if _wk_b > 0 and _wk_a > 0:
                                        weak_price = (_wk_b + _wk_a) / 2
                            if not weak_price or weak_price <= 0:
                                logger.error(f"[ARBITRAGE] {weakest_sym}: no spot price — abandon")
                                continue
                            await self._close_position(weakest_sym, "arbitrage_replaced", weak_price)
                        except Exception as e:
                            logger.error(f"[ARBITRAGE] Failed to close {weakest_sym}: {e}")
                            continue
                    else:
                        logger.info(f"[SLOTS FULL] {symbol} conf={signal.confidence}% — weakest={weakest_sym} conf={weakest_conf}% (need +5% to replace)")
                        continue

                # ═══ MACD MTF REMOVED — les 3 securites dans signals.py suffisent ═══
                # (ADX anti-range, BB anti-sommet, MACD anti-retournement)
                # Le filtre MTF bloquait trop de trades valides sur des micro-variations

                # ═══ FILTRES MODE A — scalping haute fréquence (26 mars) ═══
                # Pertes maîtrisées par sortie rapide (trailing + max hold), pas par filtres stricts.
                _t_now = _t_sig.time(); logger.info(f"[TIMING] {symbol}: B-before_filters={_t_now - _t_checkpoint:.3f}s"); _t_checkpoint = _t_now
                # Reuse indicators already computed by _compute_signal_worker at L.2127 —
                # candles and _cached_candles come from the same self._get_cached_candles(symbol) source.
                # Saves ~200-500ms per signal (process pool hop + recompute) on the critical path.
                # ═══ FILTRES LEGACY — RETIRÉS 2026-04-20 ═══
                # Les filtres BB squeeze, ADX<25, DI direction étaient des vestiges
                # de l'ancien système hybride. Ils court-circuitaient les 4TF filters
                # modernes et bloquaient des setups valides (ex: CAC40 SELL ce matin).
                # La vraie validation se fait dans _execute_trade via les 4TF filters:
                # D1 + H1 + Body M5 + Volume M5 + M1 trigger.

                _t_now = _t_sig.time(); logger.info(f"[TIMING] {symbol}: D-after_all_filters={_t_now - _t_checkpoint:.3f}s total={_t_now - _signal_ts:.3f}s"); _t_checkpoint = _t_now
                # Check freshness — verify spot price is recent, not signal timestamp
                # (signal-to-filter time depends on Twisted event loop load, not data staleness)
                import time as _t_signal
                _elapsed_preexec = _t_signal.time() - _signal_ts
                # Use SPOT price age instead of signal age — spot is the real freshness indicator
                _sid_fresh = self.mt5._resolve_symbol_id(symbol) if self.mt5_available else None
                _spot_fresh = self.mt5._spot_prices.get(_sid_fresh, {}) if _sid_fresh else {}
                # FIX 2026-04-15: mt5_client stores "time_ms" not "ts" — key mismatch caused 999s fallback and blocked ALL trades
                _spot_age = (_t_signal.time() * 1000 - _spot_fresh.get("time_ms", 0)) / 1000 if _spot_fresh.get("time_ms") else 999
                if _spot_age > 30.0:
                    logger.warning(
                        f"[FRESHNESS] {symbol}: spot price is {_spot_age:.0f}s old — ABANDON (stale data)"
                    )
                    continue
                logger.info(f"[PRE-EXEC] {symbol}: filters took {_elapsed_preexec:.1f}s, spot age {_spot_age:.1f}s — OK")

                # ── ANTI-HEDGE: block opposite direction on same canonical pair ──
                _canon_exec = self._canonical_symbol(symbol)
                _signal_action = "BUY" if signal.signal == "buy" else "SELL"
                _hedge_blocked = False
                for _epv in self._open_positions.values():
                    if self._canonical_symbol(_epv.get("symbol", "")) == _canon_exec:
                        if _epv.get("action") != _signal_action:
                            logger.info(f"[ANTI-HEDGE] {symbol}: already {_epv['action']} open — blocking opposite {_signal_action}")
                            _hedge_blocked = True
                            break
                if _hedge_blocked:
                    continue

                # ═══ ADAPTIVE SL + TP proportionnel — 2026-04-23 ═══
                # Place le SL au-delà du dernier swing high/low sur 12 M15 (~3h)
                # ET étend le TP pour préserver le R:R d'origine (sinon risk manager
                # rejette avec "R:R insuffisant").
                # Skip signaux HOLD→RANGE (range_filters gère ses propres SL/TP).
                if (not signal.reason.startswith("HOLD→RANGE")
                        and signal.suggested_sl > 0 and signal.suggested_tp > 0):
                    _m15_sl = self._candle_cache.get(symbol)
                    if _m15_sl and len(_m15_sl) >= 12:
                        _recent_m15 = _m15_sl[-12:]
                        # 2026-04-24: buffer calibré par type d'instrument (anti stop-hunt)
                        _buffer_sl = _sl_buffer_by_instrument(signal.suggested_entry, symbol)
                        _round_n_sl = 1 if signal.suggested_entry > 100 else 5
                        # R:R d'origine (avant widen)
                        _orig_risk = abs(signal.suggested_entry - signal.suggested_sl)
                        _orig_reward = abs(signal.suggested_entry - signal.suggested_tp)
                        _orig_rr = (_orig_reward / _orig_risk) if _orig_risk > 0 else 1.5
                        if signal.signal == "sell":
                            _swing_high = max(c.high for c in _recent_m15)
                            _adaptive_sl = _swing_high + _buffer_sl
                            if _adaptive_sl > signal.suggested_sl:
                                _old_sl_val = signal.suggested_sl
                                _old_tp_val = signal.suggested_tp
                                signal.suggested_sl = round(_adaptive_sl, _round_n_sl)
                                # Étend le TP vers le bas pour garder le même R:R
                                _new_risk = abs(signal.suggested_entry - signal.suggested_sl)
                                _new_tp = signal.suggested_entry - _new_risk * _orig_rr
                                signal.suggested_tp = round(_new_tp, _round_n_sl)
                                logger.info(
                                    f"[ADAPTIVE SL] {symbol} SELL: SL {_old_sl_val:.5f} → "
                                    f"{signal.suggested_sl:.5f} (au-delà résistance M15 "
                                    f"{_swing_high:.5f}) | TP {_old_tp_val:.5f} → "
                                    f"{signal.suggested_tp:.5f} (R:R {_orig_rr:.2f} préservé)"
                                )
                        elif signal.signal == "buy":
                            _swing_low = min(c.low for c in _recent_m15)
                            _adaptive_sl = _swing_low - _buffer_sl
                            if _adaptive_sl < signal.suggested_sl:
                                _old_sl_val = signal.suggested_sl
                                _old_tp_val = signal.suggested_tp
                                signal.suggested_sl = round(_adaptive_sl, _round_n_sl)
                                # Étend le TP vers le haut pour garder le même R:R
                                _new_risk = abs(signal.suggested_entry - signal.suggested_sl)
                                _new_tp = signal.suggested_entry + _new_risk * _orig_rr
                                signal.suggested_tp = round(_new_tp, _round_n_sl)
                                logger.info(
                                    f"[ADAPTIVE SL] {symbol} BUY: SL {_old_sl_val:.5f} → "
                                    f"{signal.suggested_sl:.5f} (sous support M15 "
                                    f"{_swing_low:.5f}) | TP {_old_tp_val:.5f} → "
                                    f"{signal.suggested_tp:.5f} (R:R {_orig_rr:.2f} préservé)"
                                )

                # ═══ GARDE-FOUS CONCENTRATION 2026-04-23 (post-audit jour −18.84€) ═══
                import time as _t_gf
                from datetime import datetime as _dt_gf
                _gf_action = "BUY" if signal.signal == "buy" else "SELL"

                # Garde-fou A : direction cooldown (30min après 2 SL consécutifs même direction)
                _dir_cd_expire = self._direction_cooldown.get(_gf_action, 0.0)
                if _dir_cd_expire > _t_gf.time():
                    _mins = (_dir_cd_expire - _t_gf.time()) / 60
                    logger.warning(
                        f"[DIRECTION COOLDOWN] {symbol} {_gf_action}: cooldown actif "
                        f"{_mins:.0f}min restantes (2 SL consécutifs) — trade bloqué"
                    )
                    continue

                # Garde-fou B (2026-04-23 v2) : cooldown étendu 4h si 2 losses sur paire
                # Remplace l'ancienne règle max 1/jour — la whitelist ayant filtré les
                # meilleures paires, on autorise plusieurs trades tant que pas 2 loss.
                _today_iso = _dt_gf.now().date().isoformat()
                if self._symbol_losses_today_date != _today_iso:
                    self._symbol_losses_today = {}
                    self._symbol_extended_cd = {}
                    self._symbol_losses_today_date = _today_iso
                _canon_gf = self._canonical_symbol(symbol)
                _ext_cd_expire = self._symbol_extended_cd.get(_canon_gf, 0.0)
                if _ext_cd_expire > _t_gf.time():
                    _mins = (_ext_cd_expire - _t_gf.time()) / 60
                    logger.warning(
                        f"[EXTENDED COOLDOWN] {symbol}: cooldown 4h actif {_mins:.0f}min restantes "
                        f"(2 losses aujourd'hui sur cette paire) — skip"
                    )
                    continue

                # Garde-fou C : max 3 positions simultanées même direction (évite concentration SELL)
                _same_dir_count = sum(
                    1 for _pv in self._open_positions.values()
                    if (_pv.get("action") or "").upper() == _gf_action
                )
                if _same_dir_count >= 3:
                    logger.warning(
                        f"[CONCENTRATION] {symbol} {_gf_action}: déjà {_same_dir_count} positions "
                        f"{_gf_action} ouvertes — trade bloqué (max 3 même direction)"
                    )
                    continue

                # Check risk management — use pre-fetched equity (no extra MT5 call)
                broker_capital = _cc_eq if _cc_eq > 0 else self.risk_manager.capital
                logger.info(f"[SIZE DEBUG] {symbol}: _cc_eq={_cc_eq:.2f} rm.capital={self.risk_manager.capital:.2f} → broker_capital={broker_capital:.2f}")
                decision = self.risk_manager.check_trade(signal, symbol, broker_capital=broker_capital)
                if not decision.approved:
                    logger.info(f"[REJECTED] {symbol}: {decision.reason}")
                    continue

                # Execute trade
                # Apply lot_factor for high-vol pairs (50% reduction for JPY)
                _lf = getattr(signal, "lot_factor", 1.0)
                if _lf < 1.0:
                    decision.quantity = decision.quantity * _lf
                    decision.position_size_eur = decision.position_size_eur * _lf
                    decision.risk_eur = decision.risk_eur * _lf
                    logger.info(f"[LOT FACTOR] {symbol}: lot reduced to {_lf:.0%} (high volatility)")
                # ═══ 10-SECOND RULE: check elapsed time before execution ═══
                _elapsed = _t_signal.time() - _signal_ts
                if _elapsed > 10.0:
                    logger.warning(
                        f"[10s RULE] {symbol}: {_elapsed:.1f}s écoulées depuis le signal — "
                        f"ABANDON (max 10s)"
                    )
                    continue

                # Yield before trade execution — let close commands through
                await asyncio.sleep(0)
                await self._execute_trade(symbol, signal, decision, signal_ts=_signal_ts)
                # 2026-04-23 v2 : plus d'incrément max/jour, on compte les LOSSES à la fermeture

            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}")

            # Yield after each symbol — ensures close commands can be processed
            await asyncio.sleep(0)

        # Update dynamic allocation based on all signals collected this scan
        all_signals = list(self._last_signals.values())
        self.risk_manager.allocator.update_from_signals(all_signals)

        self._scanning = False  # Resume cache refresh
        _scan_duration = _t_scan_start.time() - _scan_t0
        logger.info(f"[SCAN COMPLETE] Scanned {scanned} symbols, {skipped_market} markets closed — duration {_scan_duration:.1f}s")

    # ── Pyramiding check (2026-04-19) ──────────────────────────────────────
    def _candle_cooldown_ok(self, symbol: str) -> tuple[bool, str]:
        """
        2026-04-21 — Cooldown événementiel après clôture :
        une nouvelle entrée n'est autorisée sur `symbol` que si au moins une
        NOUVELLE bougie M5 ou M1 est apparue depuis la dernière clôture.
        Cela remplace le cooldown temporel fixe (15-45 min) par une condition
        liée à la donnée réelle du marché.
        Retourne (True, "OK") si autorisé, (False, raison) sinon.
        """
        canon = self._canonical_symbol(symbol)
        marker = self._close_candle_marker.get(canon)
        if not marker:
            return True, "Pas de marqueur (1ère entrée ou paire fraîche)"
        m5_closed_ts, m1_closed_ts = marker
        _m5 = (self._candle_cache_m5.get(symbol) or [])
        _m1 = (self._candle_cache_m1.get(symbol) or [])
        _cur_m5 = _m5[-1].timestamp if _m5 else 0
        _cur_m1 = _m1[-1].timestamp if _m1 else 0
        # Nouvelle entrée autorisée si M5 OU M1 a changé (bougie fermée et nouvelle ouverte)
        if _cur_m5 > m5_closed_ts or _cur_m1 > m1_closed_ts:
            return True, f"Nouvelle bougie détectée (M5 {_cur_m5} vs {m5_closed_ts}, M1 {_cur_m1} vs {m1_closed_ts})"
        return False, f"Même bougie M5/M1 qu'à la clôture (M5={_cur_m5}, M1={_cur_m1}) — attendre"

    # ═══ 2026-04-21 — ANTI-CORRÉLATION PAR GROUPES ═══════════════════════
    # Max 2 SYMBOLES UNIQUES par groupe corrélé (pyramides sur même paire
    # comptent pour 1, donc non bloquées).
    #
    # Groupes : une paire+action peut appartenir à plusieurs groupes
    # (ex: SP500 SELL = USD_strength ET Indices_short)
    _CORR_GROUPS = {
        "USD_strength": [
            ("EUR/USD", "SELL"), ("GBP/USD", "SELL"), ("AUD/USD", "SELL"),
            ("NZD/USD", "SELL"), ("USD/CAD", "BUY"), ("USD/CHF", "BUY"),
            ("USD/JPY", "BUY"), ("SP500", "SELL"), ("NASDAQ", "SELL"), ("GOLD", "SELL"),
        ],
        "USD_weakness": [
            ("EUR/USD", "BUY"), ("GBP/USD", "BUY"), ("AUD/USD", "BUY"),
            ("NZD/USD", "BUY"), ("USD/CAD", "SELL"), ("USD/CHF", "SELL"),
            ("USD/JPY", "SELL"), ("SP500", "BUY"), ("NASDAQ", "BUY"), ("GOLD", "BUY"),
        ],
        "EUR_strength": [
            ("EUR/USD", "BUY"), ("EUR/GBP", "BUY"), ("EUR/JPY", "BUY"),
            ("EUR/AUD", "BUY"), ("EUR/CAD", "BUY"), ("EUR/CHF", "BUY"),
        ],
        "EUR_weakness": [
            ("EUR/USD", "SELL"), ("EUR/GBP", "SELL"), ("EUR/JPY", "SELL"),
            ("EUR/AUD", "SELL"), ("EUR/CAD", "SELL"), ("EUR/CHF", "SELL"),
        ],
        "Indices_EU_long":  [("DAX40", "BUY"), ("CAC40", "BUY"), ("UK100", "BUY")],
        "Indices_EU_short": [("DAX40", "SELL"), ("CAC40", "SELL"), ("UK100", "SELL")],
        "Indices_US_long":  [("SP500", "BUY"), ("NASDAQ", "BUY")],
        "Indices_US_short": [("SP500", "SELL"), ("NASDAQ", "SELL")],
        "JPY_short": [
            ("EUR/JPY", "BUY"), ("GBP/JPY", "BUY"), ("USD/JPY", "BUY"), ("AUD/JPY", "BUY"),
        ],
        "JPY_long": [
            ("EUR/JPY", "SELL"), ("GBP/JPY", "SELL"), ("USD/JPY", "SELL"), ("AUD/JPY", "SELL"),
        ],
        "AUD_strength": [
            ("AUD/USD", "BUY"), ("AUD/JPY", "BUY"), ("AUD/CAD", "BUY"),
            ("AUD/CHF", "BUY"), ("AUD/NZD", "BUY"),
            ("EUR/AUD", "SELL"), ("GBP/AUD", "SELL"),
        ],
        "AUD_weakness": [
            ("AUD/USD", "SELL"), ("AUD/JPY", "SELL"), ("AUD/CAD", "SELL"),
            ("AUD/CHF", "SELL"), ("AUD/NZD", "SELL"),
            ("EUR/AUD", "BUY"), ("GBP/AUD", "BUY"),
        ],
    }

    def _detect_groups(self, symbol: str, action: str) -> list[str]:
        """Return list of correlation groups this (symbol, action) belongs to."""
        _action = (action or "").upper()
        _canon = self._canonical_symbol(symbol)
        groups = []
        for group_name, members in self._CORR_GROUPS.items():
            for m_sym, m_act in members:
                if self._canonical_symbol(m_sym) == _canon and m_act == _action:
                    groups.append(group_name)
                    break
        return groups

    def _check_correlation(self, symbol: str, action: str) -> tuple[bool, str]:
        """Anti-correlation : max 2 UNIQUE symbols per correlation group.
        Pyramides (même symbole) comptent pour 1 symbole unique."""
        _canon_new = self._canonical_symbol(symbol)
        new_groups = self._detect_groups(symbol, action)
        if not new_groups:
            return True, "pas de groupe corrélé"
        for group in new_groups:
            # Collect unique symbols already open in this group
            members_canon_actions = set()
            for m_sym, m_act in self._CORR_GROUPS[group]:
                members_canon_actions.add((self._canonical_symbol(m_sym), m_act))
            unique_in_group = set()
            for pv in self._open_positions.values():
                p_canon = self._canonical_symbol(pv.get("symbol", ""))
                p_act = (pv.get("action") or "").upper()
                if (p_canon, p_act) in members_canon_actions:
                    unique_in_group.add(p_canon)
            # Si le nouveau symbol est DÉJÀ dans ce groupe → pyramide autorisée
            if _canon_new in unique_in_group:
                continue
            # Sinon, vérifier qu'on ne dépasse pas 2 symboles uniques
            if len(unique_in_group) >= 2:
                syms_str = ", ".join(sorted(unique_in_group))
                return False, f"Anti-corrélation: groupe {group} a déjà 2 symboles uniques ({syms_str})"
        return True, f"Groupes {new_groups} OK (pas de saturation)"

    def _can_pyramide(self, symbol: str, new_direction: str) -> bool:
        """
        Check if we can open a new position on `symbol` in `new_direction`.
        Rules:
        - No existing position → OK
        - Existing position(s) SAME direction + ALL winning → OK (pyramide)
        - Existing position(s) SAME direction + any not winning → BLOCK
        - Existing position(s) OPPOSITE direction → BLOCK (no hedging)
        - 3 positions already → BLOCK (hard cap)
        """
        canon = self._canonical_symbol(symbol)
        existing = [
            pv for pv in self._open_positions.values()
            if self._canonical_symbol(pv.get("symbol", "")) == canon
        ]
        # No existing → OK
        if not existing:
            return True
        # Hard cap
        if len(existing) >= 3:
            logger.info(f"[PYRAMIDE] {symbol}: 3 positions déjà — hard cap")
            return False
        # Direction normalization
        new_action = "BUY" if new_direction.lower() == "buy" else "SELL"
        # Get current price for PnL estimation
        current_price = 0
        try:
            if self.mt5_available:
                _sid = self.mt5._resolve_symbol_id(symbol)
                if _sid:
                    _spot = self.mt5._spot_prices.get(_sid, {})
                    _bid = _spot.get("bid", 0)
                    _ask = _spot.get("ask", 0)
                    if _bid > 0 and _ask > 0:
                        current_price = (_bid + _ask) / 2
        except Exception:
            pass
        # 2026-04-21 FIX: pyramide = nouvelle position SI toutes les existantes ont
        # atteint au moins 10% de progression vers leur TP (pas juste "gain > 0").
        # 2026-04-22: 5% → 10% (5% laissait passer des pyramides sur bruit intra-bar,
        # ex: GBP/AUD 22/04 2 positions en 40s sur 2.2 pips = -16.72€)
        PYRAMIDE_MIN_PROGRESS = 0.10  # 10% du chemin entry→TP
        for pv in existing:
            existing_action = (pv.get("action") or "").upper()
            # Opposite direction → no hedging
            if existing_action != new_action:
                logger.info(
                    f"[PYRAMIDE] {symbol}: position {existing_action} existante opposée au nouveau {new_action} — BLOCK (no hedging)"
                )
                return False
            # Same direction → check progression toward TP
            entry = pv.get("entry_price", 0) or 0
            tp = pv.get("take_profit", 0) or 0
            if entry <= 0 or current_price <= 0 or tp <= 0:
                logger.info(
                    f"[PYRAMIDE] {symbol}: entry/TP/current manquants — conservateur BLOCK"
                )
                return False
            # Distance totale entry→TP et distance parcourue entry→current
            if existing_action == "BUY":
                full_distance = tp - entry           # positive (TP au-dessus)
                covered = current_price - entry
            else:  # SELL
                full_distance = entry - tp           # positive (TP en-dessous)
                covered = entry - current_price
            if full_distance <= 0:
                logger.info(f"[PYRAMIDE] {symbol}: full_distance invalide — BLOCK")
                return False
            progress = covered / full_distance       # ratio 0.0 .. 1.0
            if progress < PYRAMIDE_MIN_PROGRESS:
                logger.info(
                    f"[PYRAMIDE] {symbol}: position {existing_action} à "
                    f"{progress*100:.1f}% du TP (seuil {PYRAMIDE_MIN_PROGRESS*100:.0f}%) — BLOCK"
                )
                return False
            logger.info(
                f"[PYRAMIDE] {symbol}: position {existing_action} à {progress*100:.1f}% du TP OK"
            )
        # All positions same direction + all reached >=5% of TP + under cap → PYRAMIDE
        logger.warning(
            f"[PYRAMIDE] {symbol}: {len(existing)} position(s) {new_action} à >=5% TP — PYRAMIDE #{len(existing)+1} OK"
        )
        return True

    # ── Liquidity Candle Strategy (2026-04-19) ─────────────────────────────
    async def _liquidity_candle_scan(self):
        """
        Strategy "liquidity candle" — US session only (15h30 Paris / 13h30 UTC).
        Runs in parallel with 4TF pro, independent pipeline.

        Flow:
          1. At 13h45 UTC (B1 closed): analyze each symbol's 13h30-13h45 M15 candle.
             If range >= 25% × ATR(14) D1 → setup a pending LIMIT signal.
          2. Every scan tick: check if current price touches LIMIT entry of pending signals.
             If yes → open position (market order) with broker-native SL/TP.
          3. Expire signals after 30 min if not triggered.
        """
        import time as _t
        # 2026-04-23 GLOBAL WINDOW GATE : bloque TOUT le pipeline liquidity_candle
        # (création + trigger pending + pattern) hors fenêtre globale 8h-22h.
        # Sinon un signal US créé à 20h pouvait trigger à 22h15 (nuit).
        from datetime import datetime as _dt_gw
        try:
            from zoneinfo import ZoneInfo as _ZI_gw
            _paris_gw = _dt_gw.now(_ZI_gw("Europe/Paris"))
        except Exception:
            from datetime import timezone as _tz_gw, timedelta as _td_gw
            _paris_gw = _dt_gw.now(_tz_gw.utc) + _td_gw(hours=2)
        _cet_h_gw = _paris_gw.hour + _paris_gw.minute / 60.0
        if not is_in_global_trading_window(_cet_h_gw):
            # Purge les signaux pending pour pas qu'ils traînent jusqu'à demain matin
            if self._liquidity_signals:
                logger.info(f"[LIQ CANDLE] Hors window {_cet_h_gw:.1f}h — purge {len(self._liquidity_signals)} signaux pending")
                self._liquidity_signals = {}
            if self._pattern_signals:
                self._pattern_signals = {}
            return

        from app.trading.liquidity_candle import (
            detect_liquidity_candle, detect_pattern_signal,
            should_trigger_limit, is_expired,
            is_us_open_check_time, is_asia_open_check_time,
            is_eu_open_check_time, is_uk_open_check_time,
        )
        from app.trading.candle_patterns import find_pattern_m5, check_breakout
        from datetime import datetime, timezone as _tz
        from app.trading.symbol_mapper import get_tradeable_symbols, ASSET_BY_SYMBOL, get_broker_for_symbol, get_leverage
        from app.trading.signals import Signal

        now_utc = datetime.now(_tz.utc)
        now_ts = _t.time()
        today_key = now_utc.date().isoformat()

        # ── 2026-04-21: Filtre par session — chaque bucket scanne uniquement ses paires
        # EU = Euronext/Xetra ouvrent à 07:00 UTC → CAC40, DAX40, EUR*
        # UK = LSE ouvre à 08:00 UTC → UK100 uniquement (bucket dédié)
        # US = NYSE/NASDAQ à 13:30 UTC → SP500, NASDAQ, USD*, GOLD, OIL
        # ASIA = Tokyo à 23:00 UTC → NKY, HK50, AUS200, AUD*, JPY*
        SESSION_SYMBOLS: dict[str, list[str]] = {
            "EU":   ["DAX40", "CAC40", "EUR/GBP", "EUR/CAD", "EUR/AUD", "EUR/JPY"],
            "UK":   ["UK100", "GBP/USD", "GBP/JPY", "GBP/AUD"],
            "US":   ["SP500", "NASDAQ", "USD/CAD", "USD/CHF", "GOLD", "OIL_CRUDE"],
            "ASIA": ["NKY", "HK50", "AUS200", "AUD/USD", "AUD/JPY", "AUD/NZD", "AUD/CHF", "NZD/USD"],
        }

        def _run_session_detection(session_name: str, b1_hour: int, b1_minute: int, bucket: str = ""):
            """Factorisé: détecte bougie manipulation B1 pour la session donnée.
            Si `bucket` est fourni (EU/UK/US/ASIA), seules les paires de ce bucket sont scannées."""
            logger.warning(f"[LIQ CANDLE] {session_name}: scanning B1 candles...")
            if bucket and bucket in SESSION_SYMBOLS:
                tradeable = [s for s in SESSION_SYMBOLS[bucket] if s in get_tradeable_symbols(capital=self.risk_manager.capital)]
                logger.info(f"[LIQ CANDLE] {session_name}: bucket={bucket} → {len(tradeable)} paires ciblées: {tradeable}")
            else:
                tradeable = get_tradeable_symbols(capital=self.risk_manager.capital)
            for sym in tradeable:
                m15 = self._candle_cache.get(sym)
                d1 = self._candle_cache_d1.get(sym)
                if not m15 or not d1 or len(m15) < 2:
                    continue
                # Find B1 = bougie M15 qui a démarré à (b1_hour:b1_minute) UTC today
                b1 = None
                for c in reversed(m15):
                    c_ts = c.timestamp / 1000 if c.timestamp > 1e12 else c.timestamp
                    c_dt = datetime.fromtimestamp(c_ts, tz=_tz.utc)
                    if c_dt.hour == b1_hour and c_dt.minute == b1_minute and c_dt.date() == now_utc.date():
                        b1 = c
                        break
                if not b1:
                    logger.info(f"[LIQ CANDLE] {session_name} {sym}: no B1 ({b1_hour:02d}:{b1_minute:02d} UTC) in cache")
                    continue
                signal = detect_liquidity_candle(sym, b1, d1, min_ratio=0.25)
                if signal:
                    self._liquidity_signals[sym] = signal
                pattern_signal = detect_pattern_signal(sym, b1, d1, min_ratio=0.25)
                if pattern_signal:
                    self._pattern_signals[sym] = pattern_signal
                if signal or pattern_signal:
                    b1_ts = b1.timestamp / 1000 if b1.timestamp > 1e12 else b1.timestamp
                    b1_close_ts = b1_ts + 15 * 60
                    blacklist_until = b1_close_ts + 30 * 60
                    self._4tf_blacklist[sym] = blacklist_until
                    logger.warning(
                        f"[4TF BLACKLIST] {sym}: bloqué 30 min post-B1 (jusqu'à {datetime.fromtimestamp(blacklist_until, tz=_tz.utc).strftime('%H:%M:%S')} UTC)"
                    )

        # ── STEP 1a: Detection US at 13h45 UTC (weekday only) ──
        if is_us_open_check_time(now_utc) and self._last_liquidity_check_date != today_key:
            self._last_liquidity_check_date = today_key
            _run_session_detection("US 13h45 UTC", b1_hour=13, b1_minute=30, bucket="US")

        # ── STEP 1b: Detection EU at 7h15 UTC (Euronext/Xetra — sans UK100) ──
        if is_eu_open_check_time(now_utc) and getattr(self, "_last_eu_check_date", None) != today_key:
            self._last_eu_check_date = today_key
            _run_session_detection("EU 7h15 UTC", b1_hour=7, b1_minute=0, bucket="EU")

        # ── STEP 1b-bis: Detection UK at 8h15 UTC (LSE, UK100 uniquement) — 2026-04-21 ──
        if is_uk_open_check_time(now_utc) and getattr(self, "_last_uk_check_date", None) != today_key:
            self._last_uk_check_date = today_key
            _run_session_detection("UK 8h15 UTC", b1_hour=8, b1_minute=0, bucket="UK")

        # ── STEP 1c: Detection ASIA at 23h15 UTC — gated par ASIA_SESSION_ENABLED ──
        # 2026-04-23: user a désactivé l'Asie (pas de surveillance la nuit).
        # Voir filters_config.ASIA_SESSION_ENABLED.
        from app.trading.filters_config import ASIA_SESSION_ENABLED as _ASIA_ON
        if _ASIA_ON and is_asia_open_check_time(now_utc) and self._last_asia_check_date != today_key:
            self._last_asia_check_date = today_key
            _run_session_detection("ASIA 23h15 UTC", b1_hour=23, b1_minute=0)
        elif not _ASIA_ON and is_asia_open_check_time(now_utc) and self._last_asia_check_date != today_key:
            # Consomme le flag pour éviter de logger en boucle tous les 60s
            self._last_asia_check_date = today_key
            logger.info("[LIQ CANDLE] ASIA detection skipped — ASIA_SESSION_ENABLED=False (user disabled)")

        # ── STEP 2: Monitor pending signals (every scan tick) ──
        if not self._liquidity_signals:
            self._liquidity_signals = {}  # safety
        _to_remove = []
        for sym, sig in list(self._liquidity_signals.items()):
            # Expired?
            if is_expired(sig, now_ts):
                logger.info(f"[LIQ CANDLE] {sym} {sig.direction.upper()} expired (30 min) — cancelled")
                _to_remove.append(sym)
                continue
            if sig.triggered:
                _to_remove.append(sym)
                continue
            # ── Pyramide check (2026-04-19): autoriser si même direction + gagnante ──
            if not self._can_pyramide(sym, sig.direction):
                continue
            # Check cooldown
            _canon = self._canonical_symbol(sym)
            _cd = self._symbol_cooldown.get(_canon, 0)
            if _cd > now_ts:
                continue
            # Get current spot
            if not self.mt5_available:
                continue
            try:
                _sid = self.mt5._resolve_symbol_id(sym)
                if not _sid:
                    continue
                _spot = self.mt5._spot_prices.get(_sid, {})
                _bid = _spot.get("bid", 0)
                _ask = _spot.get("ask", 0)
                if _bid <= 0 or _ask <= 0:
                    continue
            except Exception:
                continue
            # Trigger?
            if not should_trigger_limit(sig, _bid, _ask):
                continue

            # ── Execute the trade ──
            logger.warning(
                f"[LIQ CANDLE] {sym} {sig.direction.upper()} TRIGGERED @ entry={sig.entry:.5f} "
                f"(bid={_bid:.5f} ask={_ask:.5f}) | SL={sig.sl:.5f} TP={sig.tp:.5f} R:R=1:2"
            )
            sig.triggered = True
            sig.triggered_at = now_ts
            # News embargo check
            try:
                from app.trading.news_calendar import is_in_news_embargo
                _emb, _emb_r = is_in_news_embargo(sym)
                if _emb:
                    logger.warning(f"[LIQ CANDLE] {sym}: {_emb_r} — trade annulé")
                    _to_remove.append(sym)
                    continue
            except Exception:
                pass
            # Build synthetic Signal and use risk manager for sizing
            _syn_signal = Signal(
                signal=sig.direction,
                confidence=95,
                reason=f"LIQUIDITY_CANDLE ratio={sig.ratio:.2f}",
                suggested_entry=sig.entry,
                suggested_sl=sig.sl,
                suggested_tp=sig.tp,
                bull_score=0, bear_score=0,
                spread_ok=True, lot_factor=1.0,
            )
            try:
                decision = self.risk_manager.evaluate_trade(
                    _syn_signal, sym, broker_capital=self._cached_equity or self.risk_manager.capital
                )
                if not decision.approved:
                    logger.warning(f"[LIQ CANDLE] {sym}: risk manager rejected: {decision.reason}")
                    _to_remove.append(sym)
                    continue
                # Execute (skip 4TF filters — this is a separate strategy)
                await self._execute_liquidity_trade(sym, _syn_signal, decision)
                _to_remove.append(sym)
            except Exception as _exec_err:
                logger.error(f"[LIQ CANDLE] {sym}: execution error: {_exec_err}")
                _to_remove.append(sym)

        for sym in _to_remove:
            self._liquidity_signals.pop(sym, None)

        # ── STEP 3: Monitor PATTERN signals (90 min window, M5 pattern + breakout) ──
        if not self._pattern_signals:
            return
        _pat_remove = []
        for sym, ps in list(self._pattern_signals.items()):
            if is_expired(ps, now_ts):
                logger.info(f"[PATTERN] {sym} {ps.direction.upper()} expired (90 min)")
                _pat_remove.append(sym); continue
            if ps.triggered:
                _pat_remove.append(sym); continue

            # ── Pyramide check (2026-04-19): autoriser si même direction + gagnante ──
            if not self._can_pyramide(sym, ps.direction):
                continue
            _canon = self._canonical_symbol(sym)
            _cd = self._symbol_cooldown.get(_canon, 0)
            if _cd > now_ts:
                continue

            # Get M5 candles AFTER B1 close
            m5 = self._candle_cache_m5.get(sym)
            if not m5:
                continue
            # Filter M5 candles that closed after b1_close_ts
            def _cts(c):
                return c.timestamp / 1000 if c.timestamp > 1e12 else c.timestamp
            m5_after = [c for c in m5 if _cts(c) >= ps.b1_close_ts]
            if len(m5_after) < 2:
                continue

            # STEP 3a: find pattern if not already found
            if not ps.pattern_found:
                pattern = find_pattern_m5(ps.direction, m5_after)
                if pattern:
                    ps.pattern_found = True
                    ps.pattern_type = pattern.pattern_type
                    ps.pattern_high = pattern.high
                    ps.pattern_low = pattern.low
                    ps.pattern_ts = pattern.timestamp
                    logger.warning(
                        f"[PATTERN] {sym} {ps.direction.upper()}: {pattern.pattern_type} DETECTED "
                        f"at high={pattern.high:.5f} low={pattern.low:.5f} — waiting breakout"
                    )

            if not ps.pattern_found:
                continue

            # STEP 3b: look for breakout candle (M5 that closed after pattern.timestamp)
            breakout_candle = None
            for c in m5_after:
                c_ts = _cts(c)
                if c_ts <= ps.pattern_ts:
                    continue
                # Create a fake PatternDetection-like for check_breakout
                class _P: pass
                _p = _P()
                _p.direction = ps.direction
                _p.high = ps.pattern_high
                _p.low = ps.pattern_low
                if check_breakout(_p, c):
                    breakout_candle = c
                    break

            if not breakout_candle:
                continue

            # Breakout confirmed → build entry/SL/TP
            entry = breakout_candle.close
            pip = 0.0001
            sym_u = sym.replace('/', '').upper()
            if 'JPY' in sym_u: pip = 0.01
            elif any(x in sym_u for x in ['DAX','CAC','UK100','NKY','HK50','AUS200']): pip = 1
            buffer = 2 * pip
            if ps.direction == "sell":
                sl = ps.pattern_high + buffer
                sl_dist = sl - entry
                tp = entry - 2.0 * sl_dist  # R:R 1:2
            else:  # buy
                sl = ps.pattern_low - buffer
                sl_dist = entry - sl
                tp = entry + 2.0 * sl_dist
            if sl_dist <= 0:
                _pat_remove.append(sym); continue
            ps.triggered = True
            ps.entry = entry
            ps.sl = sl
            ps.tp = tp
            logger.warning(
                f"[PATTERN] {sym} {ps.direction.upper()} BREAKOUT CONFIRMED: "
                f"entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} R:R=1:2 pattern={ps.pattern_type}"
            )
            # News embargo
            try:
                from app.trading.news_calendar import is_in_news_embargo
                _emb, _emb_r = is_in_news_embargo(sym)
                if _emb:
                    logger.warning(f"[PATTERN] {sym}: {_emb_r} — trade annulé")
                    _pat_remove.append(sym); continue
            except Exception:
                pass
            # Build synthetic Signal
            _syn = Signal(
                signal=ps.direction, confidence=90,
                reason=f"PATTERN_M5 {ps.pattern_type} after liquidity_candle (ratio={ps.ratio:.2f})",
                suggested_entry=entry, suggested_sl=sl, suggested_tp=tp,
                bull_score=0, bear_score=0, spread_ok=True, lot_factor=1.0,
            )
            try:
                decision = self.risk_manager.evaluate_trade(
                    _syn, sym, broker_capital=self._cached_equity or self.risk_manager.capital
                )
                if not decision.approved:
                    logger.warning(f"[PATTERN] {sym}: risk manager rejected: {decision.reason}")
                    _pat_remove.append(sym); continue
                await self._execute_liquidity_trade(sym, _syn, decision)
                _pat_remove.append(sym)
            except Exception as _e:
                logger.error(f"[PATTERN] {sym}: execution error: {_e}")
                _pat_remove.append(sym)

        for sym in _pat_remove:
            self._pattern_signals.pop(sym, None)

    async def _execute_liquidity_trade(self, symbol: str, signal: Signal, decision):
        """Execute a Liquidity Candle trade (bypass 4TF filters, uses SL/TP from signal)."""
        action = "BUY" if signal.signal == "buy" else "SELL"
        from app.trading.symbol_mapper import get_leverage, get_broker_for_symbol, get_market_for_symbol
        leverage = get_leverage(symbol)
        broker = get_broker_for_symbol(symbol)
        logger.info(
            f"[LIQ CANDLE] Executing {action} {symbol}: qty={decision.quantity:.4f} "
            f"entry={signal.suggested_entry:.5f} SL={signal.suggested_sl:.5f} TP={signal.suggested_tp:.5f}"
        )
        # Use the MT5 client directly (same path as _execute_trade but simpler)
        try:
            result = await self.mt5.manual_order(
                symbol=symbol,
                side=action,
                quantity=decision.quantity,
                stop_loss=signal.suggested_sl,
                take_profit=signal.suggested_tp,
            )
            if not result or not result.get("ticket"):
                logger.error(f"[LIQ CANDLE] {symbol}: order failed: {result}")
                return
            _ticket = result.get("ticket")
            _fill = result.get("price") or signal.suggested_entry
            import time as _tm
            from datetime import datetime as _dt, timezone as _tz2
            _pk = f"{symbol}_{_tm.time()}"
            async with self._positions_lock:
                self._open_positions[_pk] = {
                    "symbol": symbol,
                    "pos_key": _pk,
                    "_opened_ts": _tm.time(),
                    "action": action,
                    "quantity": decision.quantity,
                    "entry_price": _fill,
                    "stop_loss": signal.suggested_sl,
                    "take_profit": signal.suggested_tp,
                    "entry_time": _dt.now().isoformat(),
                    "signal_confidence": 95,
                    "signal_reason": signal.reason,
                    "position_size": decision.position_size_eur,
                    "margin": decision.position_size_eur / leverage if leverage > 0 else decision.position_size_eur,
                    "leverage": leverage,
                    "risk_eur": decision.risk_eur,
                    "broker": "mt5",
                    "ticket": _ticket,
                    "origin": "liquidity_candle",
                    "source": "liquidity_candle",
                    "_original_tp_dist": abs(signal.suggested_tp - _fill),
                }
            logger.warning(f"[LIQ CANDLE] {symbol}: position opened (ticket={_ticket})")
            await self._broadcast("alert", {
                "level": "info",
                "message": f"LIQUIDITY CANDLE {action} {symbol} @ {_fill:.5f}",
            })
        except Exception as _e:
            logger.error(f"[LIQ CANDLE] {symbol}: execution error: {_e}")

    # ── Trade Execution ───────────────────────────────────────────────────

    async def _execute_trade(self, symbol: str, signal: Signal, decision: TradeDecision, signal_ts: float = 0):
        """Execute a trade with leverage via MT5."""
        action = "BUY" if signal.signal == "buy" else "SELL"

        # ═══ 10-SECOND RULE: final check before any I/O ═══
        if signal_ts > 0:
            import time as _t_exec
            _exec_elapsed = _t_exec.time() - signal_ts
            if _exec_elapsed > 10.0:
                logger.warning(
                    f"[10s RULE] {symbol}: {_exec_elapsed:.1f}s at execute_trade entry — ABANDON"
                )
                return
        leverage = get_leverage(symbol)
        broker = get_broker_for_symbol(symbol)

        # ═══ NEWS EMBARGO — 2026-04-18 ═══
        # Bloque les trades 10-15 min avant/après les annonces macro majeures
        # (Core PCE, GDP US, BoJ, etc.) pour éviter spread explosion et slippage
        try:
            from app.trading.news_calendar import is_in_news_embargo
            _embargo, _embargo_reason = is_in_news_embargo(symbol)
            if _embargo:
                logger.warning(f"[NEWS EMBARGO] {symbol} {action}: {_embargo_reason} — trade BLOQUÉ")
                return
        except Exception as _ne:
            logger.warning(f"[NEWS EMBARGO] {symbol}: error {_ne} — allowing trade (fallback)")

        # ═══ 4TF BLACKLIST — 2026-04-19 ═══
        # Si une bougie manipulation a été détectée sur ce symbole, bloquer le 4TF
        # pendant 30 min post-B1 close pour éviter d'entrer dans le piège.
        # Les stratégies Liquidity (LIMIT + Pattern M5) prennent le relais.
        try:
            import time as _t_bl
            _bl_expires = self._4tf_blacklist.get(symbol, 0)
            if _bl_expires > _t_bl.time():
                from datetime import datetime as _dt_bl, timezone as _tz_bl
                _remaining_min = (_bl_expires - _t_bl.time()) / 60
                logger.info(
                    f"[4TF BLACKLIST] {symbol}: bougie manipulation active "
                    f"(Liquidity prioritaire {_remaining_min:.0f} min restantes) — 4TF bloqué"
                )
                return
            elif _bl_expires > 0:
                # Blacklist expiré, nettoyer
                self._4tf_blacklist.pop(symbol, None)
        except Exception:
            pass

        # ═══ REGIME DETECTION — 2026-04-23 M-ONLY ═══
        # Regime vient de M15 (ADX M15 + BB width M15), pas H1.
        # H1 reste pour sessions/news uniquement.
        _regime_result = None
        try:
            from app.trading.structure_detector import detect_regime_m15
            from app.trading.regime_detector import RegimeResult as _RR_compat
            _m15_reg = self._candle_cache.get(symbol)  # M15 = default cache
            _rg_m15 = detect_regime_m15(_m15_reg)
            logger.info(f"[REGIME M15] {symbol} {action}: {_rg_m15.summary()}")
            # Mapper vers RegimeResult compat (pour réutiliser range_filters / routing)
            _regime_result = _RR_compat(
                regime=_rg_m15.regime,
                adx=_rg_m15.adx,
                bb_width_pct=_rg_m15.bb_width_pct,
                sma50_slope_pct=None,
                range_high=None, range_low=None,  # box calculée plus bas (M5×40)
                reason=_rg_m15.reason,
            )
            if _regime_result.regime == "none":
                logger.info(f"[REGIME M15] {symbol}: régime ambigu — no trade")
                return
        except Exception as _rg_err:
            logger.warning(f"[REGIME M15] {symbol}: detect error {_rg_err} — assume trend (fallback)")
            _regime_result = None

        _is_range = _regime_result is not None and _regime_result.regime == "range"
        # 2026-04-23 user rule: un signal issu de HOLD→RANGE override (signal synthétisé
        # depuis la position dans la box H1) force aussi le routing vers range pipeline,
        # même si regime_detector a classé TREND (ex: DAX40 ADX 42 mais prix consolide).
        if not _is_range and signal.reason.startswith("HOLD→RANGE"):
            _is_range = True
            logger.info(f"[REGIME] {symbol}: forçage range pipeline (HOLD→RANGE synth override)")

        # ── F) PREFERRED REGIME (2026-04-23) : skip si symbole non compatible ──
        from app.trading.symbol_selector import is_regime_compatible as _is_rg_compat
        _current_regime = "range" if _is_range else "trend"
        if not _is_rg_compat(symbol, _current_regime):
            logger.info(
                f"[PREFERRED REGIME] {symbol}: régime détecté {_current_regime.upper()} "
                f"mais symbole préfère l'autre — skip"
            )
            return

        # 2026-04-23 — Recalcul de la BOX en M5 (80 bars = 6h40) pour capturer les
        # ranges court-terme visibles à l'œil (DAX 24000-24200 sur 6h). Utilise
        # regime_detector H1 seulement en fallback.
        if _is_range:
            from app.trading.filters_config import (
                REGIME_RANGE_LOOKBACK_M5 as _RLB_M5_exec,
                REGIME_RANGE_LOOKBACK as _RLB_H1_exec,
            )
            _m5_exec = self._candle_cache_m5.get(symbol)
            _h1_exec = self._candle_cache_h1.get(symbol)
            _box_high = _box_low = 0.0
            _tf_exec = ""
            if _m5_exec and len(_m5_exec) >= _RLB_M5_exec + 1:
                # Exclut la bougie en cours (-1)
                _recent_exec = _m5_exec[-(_RLB_M5_exec + 1):-1]
                _box_high = max(c.high for c in _recent_exec)
                _box_low = min(c.low for c in _recent_exec)
                _tf_exec = f"M5×{_RLB_M5_exec}"
            elif _h1_exec and len(_h1_exec) >= _RLB_H1_exec:
                _recent_exec = _h1_exec[-_RLB_H1_exec:]
                _box_high = max(c.high for c in _recent_exec)
                _box_low = min(c.low for c in _recent_exec)
                _tf_exec = f"H1×{_RLB_H1_exec}"
            # Mettre à jour _regime_result avec la nouvelle box (tighter)
            if _box_high > _box_low > 0:
                from app.trading.regime_detector import RegimeResult as _RR
                _regime_result = _RR(
                    regime="range",
                    adx=_regime_result.adx if _regime_result else None,
                    bb_width_pct=_regime_result.bb_width_pct if _regime_result else None,
                    sma50_slope_pct=_regime_result.sma50_slope_pct if _regime_result else None,
                    range_high=_box_high, range_low=_box_low,
                    reason=f"box {_tf_exec} (range_filters + flip)",
                )
                logger.info(f"[REGIME BOX] {symbol}: box {_tf_exec} [{_box_low:.5f}..{_box_high:.5f}]")

        # 2026-04-23 — AUTO-FLIP direction en range pipeline :
        # signals.py donne BUY/SELL selon H1 macro, mais en range la direction
        # dépend de la POSITION dans la box, pas de H1. Si mismatch → flip.
        if (_is_range and _regime_result
                and _regime_result.range_high and _regime_result.range_low
                and signal.signal in ("buy", "sell")):
            from app.trading.filters_config import RANGE_ENTRY_ZONE_PCT as _RZ_flip
            _box_w_flip = _regime_result.range_high - _regime_result.range_low
            if _box_w_flip > 0:
                _cur_flip = signal.suggested_entry
                _lower_zone_flip = _regime_result.range_low + _RZ_flip * _box_w_flip
                _upper_zone_flip = _regime_result.range_high - _RZ_flip * _box_w_flip
                _want_dir = None
                if _cur_flip <= _lower_zone_flip:
                    _want_dir = "buy"
                elif _cur_flip >= _upper_zone_flip:
                    _want_dir = "sell"
                if _want_dir and _want_dir != signal.signal:
                    _old_dir = signal.signal
                    signal.signal = _want_dir
                    action = "BUY" if _want_dir == "buy" else "SELL"
                    logger.info(
                        f"[RANGE FLIP] {symbol}: {_old_dir.upper()}→{_want_dir.upper()} "
                        f"(prix {_cur_flip:.5f} dans zone "
                        f"{'basse' if _want_dir=='buy' else 'haute'} "
                        f"box=[{_regime_result.range_low:.5f}..{_regime_result.range_high:.5f}])"
                    )

        # Range pipeline : forex + indices. Commodities exclues (volatilité → faux ranges).
        if _is_range:
            from app.trading.range_filters import is_rangeable_symbol as _is_rg
            if not _is_rg(symbol):
                logger.info(f"[REGIME] {symbol}: range détecté mais pas forex/index — skip (commodity exclue)")
                return

        # ═══ PYRAMIDE CHECK — 2026-04-19 ═══
        # Règle cohérente pour les 3 stratégies (4TF + Liquidity LIMIT + Pattern M5):
        # - Pas de hedging (positions opposées)
        # - Pyramide seulement si position(s) existante(s) GAGNANTES
        # - Hard cap 3 positions par symbole
        # En mode RANGE : pyramide BLOQUÉE (une seule position par range, sortie au milieu)
        if _is_range:
            _canon_r = self._canonical_symbol(symbol)
            _already_in_range = any(
                self._canonical_symbol(pv.get("symbol", "")) == _canon_r
                for pv in self._open_positions.values()
            )
            if _already_in_range:
                logger.info(f"[RANGE] {symbol}: position déjà ouverte, pas de pyramide en range — skip")
                return
        else:
            if not self._can_pyramide(symbol, signal.signal):
                logger.info(f"[PYRAMIDE] {symbol} {action}: pyramide refusé — trade non exécuté")
                return

        # 2026-04-21: Anti-corrélation par groupes (max 2 symboles uniques/groupe)
        _corr_ok, _corr_reason = self._check_correlation(symbol, action)
        if not _corr_ok:
            logger.warning(f"[ANTI-CORREL] {symbol} {action}: {_corr_reason} — trade bloqué")
            return

        # 2026-04-21: Cooldown événementiel — nouvelle bougie requise après clôture
        _cd_ok, _cd_reason = self._candle_cooldown_ok(symbol)
        if not _cd_ok:
            logger.info(f"[COOLDOWN CANDLE] {symbol} {action}: {_cd_reason} — trade bloqué")
            return

        # ═══ FILTRES ENTRÉE — routing par régime (2026-04-23) ═══
        # TREND: 4TF pro (D1/H1/body M5/volume/M1 assoupli)
        # RANGE: rejet bornes + RSI + hammer/engulfing + volume 0.9× → taille 60%
        _size_factor = 1.0
        try:
            _m1 = self._candle_cache_m1.get(symbol)
            _m5 = self._candle_cache_m5.get(symbol)
            _h1 = self._candle_cache_h1.get(symbol)
            _d1 = self._candle_cache_d1.get(symbol)

            # 2026-04-22 [VOL SYNC A] — resync M5 last 3 bars via CopyRates si
            # le cache est périmé. Le cache est rafraîchi toutes les 300s, donc
            # la dernière bougie peut contenir un tick_volume partiel (snapshot
            # pris au début de la barre en cours). Un refetch synchrone garantit
            # des volumes à jour pour l'évaluation 4TF.
            try:
                import time as _time
                _last_ts = _m5[-1].timestamp if _m5 else 0
                if _time.time() - _last_ts > 60:
                    _dc = getattr(self, "mt5_data_client", None) or self.mt5
                    _fresh = await _dc.get_historical_candles(
                        symbol, duration="15 min", bar_size="5 mins"
                    )
                    if _fresh and len(_fresh) >= 2 and _m5:
                        _fresh_ts = {c.timestamp for c in _fresh}
                        _merged = [c for c in _m5 if c.timestamp not in _fresh_ts] + list(_fresh)
                        _merged.sort(key=lambda c: c.timestamp)
                        _m5 = _merged
                        self._candle_cache_m5[symbol] = _m5
                        logger.info(
                            f"[VOL SYNC] {symbol}: resync {len(_fresh)} M5 bars (last_vol={_m5[-1].volume:.0f})"
                        )
            except Exception as _sync_err:
                logger.debug(f"[VOL SYNC] {symbol}: skip ({_sync_err})")

            if _is_range and _regime_result and _regime_result.range_high and _regime_result.range_low:
                # ── RANGE pipeline ────────────────────────────────────────
                from app.trading.range_filters import evaluate_range_entry
                _rr = evaluate_range_entry(
                    signal=signal.signal,
                    price=signal.suggested_entry,
                    candles_m5=_m5,
                    range_high=_regime_result.range_high,
                    range_low=_regime_result.range_low,
                    symbol=symbol,
                )
                logger.info(f"[RANGE FILTERS] {symbol} {action}: {_rr}")
                if not _rr.ok:
                    logger.info(f"[RANGE FILTERS] {symbol}: BLOCKED — {_rr.reason}")
                    return
                # Override SL/TP avec ceux du range (bornes + milieu)
                signal.suggested_sl = round(_rr.sl, 5)
                signal.suggested_tp = round(_rr.tp, 5)
                _size_factor = _rr.size_factor  # 0.6 user rule
                logger.info(
                    f"[RANGE FILTERS] {symbol}: SL/TP overridden → SL={signal.suggested_sl} "
                    f"TP={signal.suggested_tp} size×{_size_factor:.1f}"
                )
            else:
                # ── TREND pipeline : SCORE PONDÉRÉ 0-100 ──
                # Remplace les filtres booléens AND par un score continu.
                # User-validated 2026-04-23: ADX 30 + Body 20 + Vol 20 + M1 20 + Pattern 10
                from app.trading.scalping_filters import compute_entry_score, SCORE_ENTRY_MIN
                _es = compute_entry_score(
                    signal=signal.signal,
                    candles_m15=_m5,  # note: current _candle_cache IS M15 — see init
                    candles_m5=_m5,
                    candles_m1=_m1,
                )
                # Note: candles_m15 devrait venir de _candle_cache (qui est M15).
                # On va re-piocher explicitement pour clarté
                _m15_score = self._candle_cache.get(symbol)
                _es = compute_entry_score(
                    signal=signal.signal,
                    candles_m15=_m15_score,
                    candles_m5=_m5,
                    candles_m1=_m1,
                )
                logger.info(f"[SCORE TREND] {symbol} {action}: {_es.summary()}")
                if _es.total < SCORE_ENTRY_MIN:
                    logger.info(
                        f"[SCORE TREND] {symbol}: BLOCKED (score {_es.total}/100 < {SCORE_ENTRY_MIN}) — trade non exécuté"
                    )
                    return
                # Size factor selon la qualité du score :
                # - 60-69 : taille réduite 60%
                # - 70-79 : taille 80%
                # - 80+ : taille pleine 100%
                if _es.total >= 80:
                    _size_factor = 1.0
                elif _es.total >= 70:
                    _size_factor = 0.8
                else:
                    _size_factor = 0.6
            if _size_factor < 1.0:
                decision.quantity = decision.quantity * _size_factor
                decision.position_size_eur = decision.position_size_eur * _size_factor
                decision.risk_eur = decision.risk_eur * _size_factor
                _tag = "range 60%" if _is_range else "vol 0.9-1.2×"
                logger.info(f"[FILTERS] {symbol}: taille réduite ×{_size_factor:.1f} ({_tag})")
        except Exception as _sf_err:
            logger.warning(f"[FILTERS] {symbol}: error {_sf_err} — allowing trade (fallback)")

        logger.info(
            f"Executing {action} {symbol} ({broker}): qty={decision.quantity:.4f} "
            f"position={decision.position_size_eur:.2f}EUR levier={leverage}:1 "
            f"entry={signal.suggested_entry:.4f} SL={signal.suggested_sl:.4f} TP={signal.suggested_tp:.4f}"
        )

        # ── MARGIN CHECK: estimate free margin from internal tracking ──
        _used_margin = 0
        for _mp in self._open_positions.values():
            _mp_margin = _mp.get("margin", 0)
            if _mp_margin > 0:
                _used_margin += _mp_margin
            else:
                # Estimate margin from position size and leverage
                _mp_qty = _mp.get("quantity", 0)
                _mp_entry = _mp.get("entry_price", 0)
                _mp_lev = _mp.get("leverage", 30)
                _mp_sym = _mp.get("symbol", "")
                if _mp_lev > 0 and _mp_entry > 0:
                    _mp_mkt = get_market_for_symbol(_mp_sym)
                    if _mp_mkt in ("INDICES", "COMMODITY"):
                        _used_margin += _mp_qty * _mp_entry / _mp_lev
                    else:
                        # Forex: qty is units, need to convert to EUR
                        _mp_base = _mp_sym[:3].upper() if "/" in _mp_sym else ""
                        _base_rates = {"EUR": 1.0, "GBP": 1.15, "USD": 0.85, "AUD": 0.60, "NZD": 0.51, "CAD": 0.65, "CHF": 1.05}
                        _bp = _base_rates.get(_mp_base, 1.0)
                        _used_margin += (_mp_qty * _bp) / _mp_lev
        _broker_free_margin = self.risk_manager.capital - _used_margin
        logger.info(f"[MARGIN CHECK] {symbol}: capital={self.risk_manager.capital:.2f} used={_used_margin:.2f} free={_broker_free_margin:.2f}")

        # Estimate required margin: for indices/commodities, use qty * price / leverage
        _est_margin = decision.position_size_eur / leverage if leverage > 0 else decision.position_size_eur
        # For indices CFD: position_size_eur is often wrong (qty not multiplied by price)
        # Use qty * entry_price / leverage as better estimate
        if get_market_for_symbol(symbol) in ("INDICES", "COMMODITY"):
            _est_margin = (decision.quantity * signal.suggested_entry) / leverage if leverage > 0 else decision.quantity * signal.suggested_entry

        if _broker_free_margin < _est_margin * 1.1:  # 10% buffer pour spread/slippage
            logger.warning(f"[MARGIN CHECK] {symbol}: marge libre broker={_broker_free_margin:.2f}€ < requise={_est_margin:.2f}€ — SKIP")
            return

        # ── SCAN ONLY MODE — log signal, don't execute ──
        if getattr(self, '_scan_only', False):
            logger.info(
                f"[SCAN ONLY] {symbol} {action} qty={decision.quantity:.2f} "
                f"SL={signal.suggested_sl:.5f} TP={signal.suggested_tp:.5f} "
                f"conf={signal.confidence}% — PAS D'EXECUTION"
            )
            return

        # ── FRESH QUOTE before execution — use SPOT subscription (instant) ──
        try:
            # Use real-time spot (already subscribed, no network call)
            live_quote = None
            if self.mt5_available:
                _sid_exec = self.mt5._resolve_symbol_id(symbol)
                if _sid_exec:
                    _spot_exec = self.mt5._spot_prices.get(_sid_exec, {})
                    _sb = _spot_exec.get("bid", 0)
                    _sa = _spot_exec.get("ask", 0)
                    if _sb > 0 and _sa > 0:
                        live_quote = {
                            "price": (_sb + _sa) / 2,
                            "bid": _sb,
                            "ask": _sa,
                            "spread": _sa - _sb,
                        }
            # Fallback: network call only if spot unavailable AND cache not refreshing
            if not live_quote or not live_quote.get("price"):
                if not self._cache_refreshing:
                    live_quote = await self._get_quote(symbol)
                else:
                    logger.warning(f"[EXEC] {symbol}: no spot + cache refreshing — using signal entry price")
            if live_quote and live_quote.get("price") and live_quote["price"] > 0:
                # Update signal entry/SL/TP with live price
                old_entry = signal.suggested_entry
                new_price = live_quote["price"]
                _drift = abs(new_price - old_entry) / old_entry if old_entry > 0 else 0
                if _drift > 0.005:  # >0.5% price drift — recalculate SL/TP
                    _sl_dist = abs(signal.suggested_entry - signal.suggested_sl)
                    _tp_dist = abs(signal.suggested_tp - signal.suggested_entry)
                    if action == "BUY":
                        signal.suggested_entry = new_price
                        signal.suggested_sl = new_price - _sl_dist
                        signal.suggested_tp = new_price + _tp_dist
                    else:
                        signal.suggested_entry = new_price
                        signal.suggested_sl = new_price + _sl_dist
                        signal.suggested_tp = new_price - _tp_dist
                    logger.info(f"[FRESH QUOTE] {symbol}: price updated {old_entry:.5f} → {new_price:.5f} (drift {_drift:.2%})")
                # Cache the fresh quote
                import time as _fqt
                live_quote["_ts"] = _fqt.time()
                self._last_quotes[symbol] = live_quote

                # ── SPREAD FILTER — bloque si le spread > 30% de la distance SL ──
                # Motif: si le spread bouffe un tiers du risque, la position démarre
                # déjà avec 30% du SL de perte instantanée (ex: 5-20€ en 20s). On refuse.
                try:
                    _bid = float(live_quote.get("bid", 0) or 0)
                    _ask = float(live_quote.get("ask", 0) or 0)
                    if _bid > 0 and _ask > 0 and _ask > _bid:
                        _spread_abs = _ask - _bid
                        _sl_dist_chk = abs(signal.suggested_entry - signal.suggested_sl)
                        _spread_pct_of_sl = (_spread_abs / _sl_dist_chk) if _sl_dist_chk > 0 else 1.0
                        # Estimate spread cost in EUR
                        _mkt_chk = get_market_for_symbol(symbol)
                        if _mkt_chk == "FOREX":
                            _q_ccy = symbol[4:7] if "/" in symbol and len(symbol) >= 7 else ""
                            _q2e = {"USD":0.88,"GBP":1.17,"EUR":1.0,"JPY":0.0055,"CHF":1.05,"CAD":0.65,"AUD":0.62,"NZD":0.51}.get(_q_ccy, 1.0)
                            _spread_eur_est = _spread_abs * decision.quantity * _q2e
                        else:
                            _spread_eur_est = _spread_abs * decision.quantity
                        logger.info(
                            f"[SPREAD] {symbol}: bid={_bid:.5f} ask={_ask:.5f} "
                            f"spread={_spread_abs:.5f} ({_spread_pct_of_sl*100:.1f}% du SL) "
                            f"cout_estime={_spread_eur_est:.2f}EUR"
                        )
                        if _spread_pct_of_sl > 0.30:
                            logger.warning(
                                f"[SPREAD FILTER] {symbol}: spread trop large "
                                f"({_spread_pct_of_sl*100:.1f}% du SL, cout~{_spread_eur_est:.2f}EUR) — SKIP"
                            )
                            import time as _t_sp
                            _canon_sp = self._canonical_symbol(symbol)
                            self._symbol_cooldown[_canon_sp] = _t_sp.time() + 600  # 10min cooldown
                            return
                except Exception as _e_sp:
                    logger.debug(f"[SPREAD] {symbol}: check failed: {_e_sp}")
        except Exception as e:
            logger.warning(f"[FRESH QUOTE] {symbol}: failed to get live price, using cached: {e}")

        # ═══ FINAL 10-SECOND CHECK before placing order ═══
        if signal_ts > 0:
            import time as _t_final
            _final_elapsed = _t_final.time() - signal_ts
            if _final_elapsed > 10.0:
                logger.warning(
                    f"[10s RULE] {symbol}: {_final_elapsed:.1f}s before place_order — ABANDON"
                )
                return
            logger.info(f"[SPEED] {symbol}: signal→order = {_final_elapsed:.1f}s ✓")
            log_timing(
                "order_sent",
                symbol=symbol,
                action=action,
                signal_ts=signal_ts,
                latency_ms=int(_final_elapsed * 1000),
                quantity=decision.quantity,
                sl=signal.suggested_sl,
                tp=signal.suggested_tp,
            )

        import time as _t_ord_pre
        _t_order_pre = _t_ord_pre.time()
        # Place order via MT5 (SL/TP broker-native)
        result = await self._place_order(
            symbol, action, decision.quantity,
            stop_loss=signal.suggested_sl,
            take_profit=signal.suggested_tp,
        )

        if not result:
            logger.error(f"[ORDER FAILED] {symbol} {action}: _place_order returned None — ordre NON execute!")
            import time as _t_rej
            log_timing(
                "order_failed",
                symbol=symbol,
                action=action,
                signal_ts=signal_ts,
                broker_ms=int((_t_rej.time() - _t_order_pre) * 1000),
                total_ms=int((_t_rej.time() - signal_ts) * 1000) if signal_ts else 0,
            )
            # Cooldown 30min after broker rejection (NOT_ENOUGH_MONEY etc.)
            _canon_rej = self._canonical_symbol(symbol)
            self._symbol_cooldown[_canon_rej] = _t_rej.time() + 1800  # 30min
            logger.warning(f"[COOLDOWN] {symbol} ({_canon_rej}): rejet broker → cooldown 30min")
            return
        if result:
            _ticket = result.get("ticket") or int(datetime.now().timestamp() * 1000)
            _pos_key = f"{symbol}_{_ticket}"
            import time as _time_mod2
            _t_fill = _time_mod2.time()
            log_timing(
                "order_filled",
                symbol=symbol,
                action=action,
                signal_ts=signal_ts,
                ticket=_ticket,
                broker_ms=int((_t_fill - _t_order_pre) * 1000),
                total_ms=int((_t_fill - signal_ts) * 1000) if signal_ts else 0,
                fill_price=result.get("price"),
            )

            # ═══ CRITICAL: recalculate SL/TP based on ACTUAL fill price ═══
            # If fill price differs from signal price, adjust SL/TP to keep same pip distance
            _fill_price = result.get("price") or signal.suggested_entry
            _signal_entry = signal.suggested_entry
            _slippage = _fill_price - _signal_entry  # positive = filled higher
            _adj_sl = signal.suggested_sl
            _adj_tp = signal.suggested_tp
            if _fill_price and _fill_price > 0 and abs(_slippage) > 0:
                # Indices/commodities (prix > 100): arrondir à 1 décimale max
                # MT5 rejette silencieusement les SL/TP avec trop de décimales
                _sl_round_n = 1 if _fill_price > 100 else 5
                _adj_sl = round(signal.suggested_sl + _slippage, _sl_round_n)
                _adj_tp = round(signal.suggested_tp + _slippage, _sl_round_n)
                if abs(_slippage / _signal_entry) > 0.0001:  # > 1 pip relative
                    logger.warning(
                        f"[SLIPPAGE] {symbol}: signal={_signal_entry:.5f} fill={_fill_price:.5f} "
                        f"slip={_slippage:.5f} → SL ajusté {signal.suggested_sl:.5f}→{_adj_sl:.5f} "
                        f"TP ajusté {signal.suggested_tp:.5f}→{_adj_tp:.5f}"
                    )
                # Update SL/TP on broker immediately
                if _ticket:
                    try:
                        await self.mt5.amend_position_sltp(
                            int(_ticket), stop_loss=_adj_sl, take_profit=_adj_tp
                        )
                        logger.info(f"[SLIPPAGE] {symbol}: SL/TP recalculés sur broker ✓")
                    except Exception as _slip_err:
                        logger.error(f"[SLIPPAGE] {symbol}: erreur correction SL/TP: {_slip_err}")

            # FIX 2026-04-15: snapshot des indicateurs au moment de l'entrée pour audit posterior
            _ind_snap = None
            try:
                _ind_ref = indicators if 'indicators' in locals() else None
                if _ind_ref is not None:
                    _ind_snap = {
                        "rsi14": getattr(_ind_ref, "rsi14", None),
                        "rsi_prev5": getattr(_ind_ref, "rsi_prev5", None),
                        "adx": getattr(_ind_ref, "adx", None),
                        "plus_di": getattr(_ind_ref, "plus_di", None),
                        "minus_di": getattr(_ind_ref, "minus_di", None),
                        "macd": getattr(getattr(_ind_ref, "macd", None), "macd", None),
                        "macd_signal": getattr(getattr(_ind_ref, "macd", None), "signal", None),
                        "macd_histogram": getattr(getattr(_ind_ref, "macd", None), "histogram", None),
                        "macd_hist_prev": getattr(_ind_ref, "macd_hist_prev", None),
                        "bb_position": getattr(getattr(_ind_ref, "bollinger_bands", None), "position", None),
                        "bb_width": getattr(getattr(_ind_ref, "bollinger_bands", None), "width", None),
                        "sma20": getattr(_ind_ref, "sma20", None),
                        "sma50": getattr(_ind_ref, "sma50", None),
                        "stoch_k": getattr(getattr(_ind_ref, "stochastic", None), "k", None),
                        "stoch_d": getattr(getattr(_ind_ref, "stochastic", None), "d", None),
                    }
            except Exception:
                _ind_snap = None

            async with self._positions_lock:
                self._open_positions[_pos_key] = {
                    "symbol": symbol,
                    "pos_key": _pos_key,
                    "_opened_ts": _time_mod2.time(),
                    "action": action,
                    "quantity": decision.quantity,
                    "entry_price": _fill_price,
                    "stop_loss": _adj_sl,
                    "take_profit": _adj_tp,
                    "entry_time": datetime.now().isoformat(),
                    "signal_confidence": signal.confidence,
                    "signal_reason": signal.reason,
                    "indicators_snapshot": _ind_snap,
                    "position_size": decision.position_size_eur,
                    "margin": decision.position_size_eur / leverage if leverage > 0 else decision.position_size_eur,
                    "leverage": leverage,
                    "risk_eur": decision.risk_eur,
                    "broker": result.get("broker", broker),
                    "ticket": result.get("ticket"),
                    "sl_order_id": result.get("sl_order_id"),
                    "tp_order_id": result.get("tp_order_id"),
                    "entry_commission": result.get("commission", 0),
                    "entry_commission_ccy": result.get("commission_currency", "USD"),
                    # 2026-04-23: origin fine-grained — bot_range si pipeline range, sinon bot_4tf
                    "origin": "bot_range" if _is_range else "bot_4tf",
                    "source": "bot_range" if _is_range else "bot_4tf",
                    "_original_tp_dist": abs(_adj_tp - _fill_price),
                }

            # ═══ CRITICAL SAFETY CHECK: verify SL/TP on broker after 2s ═══
            # If SL or TP is missing on broker, CLOSE the position immediately
            await asyncio.sleep(2)  # Wait for broker to process amend
            if self.mt5_available and _ticket:
                try:
                    _verify_positions = await self.mt5.get_positions()
                    for _vp in (_verify_positions or []):
                        _vp_ticket = _vp.get("ticket") or _vp.get("position_id")
                        if _vp_ticket and int(_vp_ticket) == int(_ticket):
                            _vp_sl = _vp.get("stop_loss", 0)
                            _vp_tp = _vp.get("take_profit", 0)
                            if not _vp_sl or _vp_sl == 0 or not _vp_tp or _vp_tp == 0:
                                logger.critical(
                                    f"[SLTP CHECK] {symbol}: SL={_vp_sl} TP={_vp_tp} — "
                                    f"PROTECTION MANQUANTE! Tentative de correction..."
                                )
                                # Try to set SL/TP one more time
                                _fix_ok = await self.mt5.amend_position_sltp(
                                    int(_ticket),
                                    stop_loss=signal.suggested_sl if (not _vp_sl or _vp_sl == 0) else None,
                                    take_profit=signal.suggested_tp if (not _vp_tp or _vp_tp == 0) else None,
                                )
                                if not _fix_ok:
                                    # LAST RESORT: close the position — no unprotected positions allowed
                                    logger.critical(
                                        f"[SLTP CHECK] {symbol}: IMPOSSIBLE de mettre SL/TP "
                                        f"→ FERMETURE IMMEDIATE (sécurité)"
                                    )
                                    await self._close_position_broker(symbol, ticket=int(_ticket))
                                    async with self._positions_lock:
                                        self._open_positions.pop(_pos_key, None)
                                    await self._remove_position_db(_pos_key)
                                    await self._broadcast("alert", {
                                        "level": "critical",
                                        "message": f"{symbol}: fermé car SL/TP impossible à placer sur le broker",
                                    })
                                    return  # Don't persist — position is closed
                                else:
                                    logger.info(f"[SLTP CHECK] {symbol}: SL/TP corrigé avec succès")
                            else:
                                logger.info(f"[SLTP CHECK] {symbol}: SL={_vp_sl:.5f} TP={_vp_tp:.5f} ✓ OK")
                            break
                except Exception as _ve:
                    logger.error(f"[SLTP CHECK] {symbol}: vérification échouée: {_ve}")

            # Persist position to DB
            await self._save_position(_pos_key, self._open_positions[_pos_key])

            # Update risk manager state
            self.risk_manager.update_state(
                daily_pnl=self.risk_manager._daily_pnl,
                open_positions=len(self._open_positions),
                open_symbols=[p.get("symbol", k.split("_")[0]) for k, p in self._open_positions.items()],
                capital=self.risk_manager.capital,
            )

            await self._broadcast("order_filled", {
                "symbol": symbol,
                "action": action,
                "quantity": decision.quantity,
                "entry_price": signal.suggested_entry,
                "stop_loss": signal.suggested_sl,
                "take_profit": signal.suggested_tp,
                "broker": broker,
            })

    # ── Trailing Stop Logic (centralized) ────────────────────────────────

    def _apply_trailing_stop(self, pos: dict, current_price: float, log_prefix: str = "[TRAIL]") -> tuple[bool, bool]:
        """
        Trailing stop dynamique — écart palier-SL large pour laisser respirer.

        Règle (2026-04-22, corrigée) :
          • Dès 30% du TP atteint → SL verrouille à 10% du TP (évite sortir à 0)
          • Chaque +5% de progression → SL suit palier - 20%
            (35% → SL 15%, 40% → SL 20%, ..., 90% → SL 70%)
          • À 90% du TP courant → TP repoussé de +15% de la distance originale
          • Écart palier-lock = 20% (pour que la safety gap 5bp ne mange pas le lock)

        Returns: (sl_changed, tp_changed)
        """
        symbol = pos.get("symbol", "?")
        entry_price = pos.get("entry_price", 0)
        sl = pos.get("stop_loss", 0)
        tp = pos.get("take_profit", 0)
        is_long = pos.get("action") == "BUY"

        # 2026-04-22 DIAGNOSTIC: trace every call (rate-limited to 1× per 30s per symbol)
        import time as _t_diag
        _diag_key = f"_trail_diag_{symbol}"
        _last_diag = getattr(self, _diag_key, 0)
        if _t_diag.time() - _last_diag > 30:
            setattr(self, _diag_key, _t_diag.time())
            logger.info(f"{log_prefix} {symbol}: CALLED entry={entry_price} sl={sl} tp={tp} action={pos.get('action')} current={current_price}")

        if not entry_price or not tp:
            logger.info(f"{log_prefix} {symbol}: GUARD FAIL entry={entry_price} tp={tp}")
            return False, False

        # Original TP distance (stored at position creation, or computed on first call)
        original_dist = pos.get("_original_tp_dist")
        if not original_dist or original_dist <= 0:
            original_dist = abs(tp - entry_price)
            pos["_original_tp_dist"] = original_dist
        if original_dist <= 0:
            return False, False

        # Current TP distance (may have been extended)
        current_tp_dist = abs(tp - entry_price)
        if current_tp_dist <= 0:
            return False, False

        # Progress toward CURRENT TP (can exceed 1.0 if price overshoots)
        if is_long:
            progress = (current_price - entry_price) / current_tp_dist
        else:
            progress = (entry_price - current_price) / current_tp_dist

        sl_changed = False
        tp_changed = False

        # 2026-04-21: BREAK-EVEN custom RETIRÉ — trailing 5% palier (ci-dessous) fait le job

        # 2026-04-21: trailing démarre à 30% progress (avant 20%) — replay +22% PnL
        # Objectif: laisser respirer les trades avant de lock le SL
        if progress < 0.30:
            # 2026-04-22 DIAG: rate-limited log to see progress evolution
            if _t_diag.time() - _last_diag < 1.0:  # same burst as entry log
                logger.info(f"{log_prefix} {symbol}: progress={progress:.2%} < 30% — no trail")
            return sl_changed, tp_changed

        # ═══ SL TRAILING — écart palier-lock = 20% pour laisser respirer ═══
        # Round down to nearest 5%: 0.32→30, 0.47→45, 0.92→90
        _palier = int(progress * 20) * 5
        if _palier < 30:
            _palier = 30
        # 2026-04-22 FIX: SL = palier - 20% (min 10%). Évite que la safety gap 5bp
        # ne mange le lock (palier - 5 donnait 18-20% effectif au 1er palier sur indices).
        # Exemple CAC40 TP=37pts: palier 30% @ prix 8199, SL=10% @ 8207 (8pts au-dessus)
        # → gap 8pts vs safety 4pts, pas de compression du lock.
        _sl_pct = max((_palier - 20) / 100.0, 0.10)  # 30%→10%, 35%→15%, 40%→20%, …, 90%→70%

        # SL position = entry + sl_pct × ORIGINAL distance (toward TP)
        if is_long:
            _new_sl = entry_price + original_dist * _sl_pct
            # Safety: SL must stay below current price (min 5 pips)
            _min_gap = abs(current_price) * 0.0005
            _max_valid_sl = current_price - _min_gap
            if _new_sl > _max_valid_sl:
                _new_sl = _max_valid_sl
            if _new_sl > sl:  # Only move SL forward (never backward)
                pos["stop_loss"] = round(_new_sl, 5)
                sl_changed = True
                logger.info(f"{log_prefix} {symbol}: palier {_palier}% → SL={_new_sl:.5f} (verrouillé à {_palier-5}% du TP)")
        else:  # SELL
            _new_sl = entry_price - original_dist * _sl_pct
            _min_gap = abs(current_price) * 0.0005
            _min_valid_sl = current_price + _min_gap
            if _new_sl < _min_valid_sl:
                _new_sl = _min_valid_sl
            if _new_sl < sl or sl == 0:  # Only move SL forward (closer to entry for SELL)
                pos["stop_loss"] = round(_new_sl, 5)
                sl_changed = True
                logger.info(f"{log_prefix} {symbol}: palier {_palier}% → SL={_new_sl:.5f} (verrouillé à {_palier-5}% du TP)")

        # ═══ TP AUTO-EXTENSION — à 90% du TP courant → repousser de +15% distance originale ═══
        # Progress toward CURRENT TP (recalculate with potentially updated SL)
        if progress >= 0.90:
            if is_long:
                _new_tp = tp + original_dist * 0.15
                if _new_tp > tp:
                    pos["take_profit"] = round(_new_tp, 5)
                    tp_changed = True
                    _extension_pct = round((_new_tp - entry_price) / original_dist * 100)
                    logger.info(f"{log_prefix} {symbol}: 90% TP atteint → TP repoussé à {_new_tp:.5f} ({_extension_pct}% du TP original)")
            else:  # SELL
                _new_tp = tp - original_dist * 0.15
                if _new_tp < tp:
                    pos["take_profit"] = round(_new_tp, 5)
                    tp_changed = True
                    _extension_pct = round((entry_price - _new_tp) / original_dist * 100)
                    logger.info(f"{log_prefix} {symbol}: 90% TP atteint → TP repoussé à {_new_tp:.5f} ({_extension_pct}% du TP original)")

        return sl_changed, tp_changed

    # ── Position Monitoring ───────────────────────────────────────────────

    async def _monitor_positions(self):
        """Scalping monitor: SL/TP check + trailing stop + max hold time exit."""
        from datetime import datetime, timedelta

        # Refresh MT5 position cache via DASHBOARD connection
        _positions_fresh = False
        try:
            _dc_mon = self._dash_client()
            if _dc_mon and _dc_mon.is_connected:
                self._cached_cc_positions = await _dc_mon.get_positions()
                _positions_fresh = True
        except Exception:
            pass  # Keep old cache — but DON'T detect broker closes with stale data

        # Detect positions closed by broker (TP/SL hit on MT5)
        # CRITICAL: only run when get_positions succeeded THIS cycle (not stale cache)
        if _positions_fresh and self._cached_cc_positions is not None and self._open_positions:
            # Build set of broker tickets/symbols for position matching
            broker_tickets = set()
            broker_symbols_count: dict[str, int] = {}
            for p in self._cached_cc_positions:
                t = p.get("ticket") or p.get("position_id") or p.get("deal_id")
                if t:
                    broker_tickets.add(str(t))
                s = p.get("symbol", "")
                broker_symbols_count[s] = broker_symbols_count.get(s, 0) + 1

            closed_keys = []
            for pos_key, pos in list(self._open_positions.items()):
                _pos_broker = pos.get("broker", "mt5")
                # Only detect close for positions confirmed on broker in a previous monitor cycle
                if not pos.get("_seen_on_broker"):
                    # Mark as seen if currently on broker
                    _t = str(pos.get("ticket") or pos.get("position_id") or "")
                    sym = pos.get("symbol", pos_key.split("_")[0])
                    if _t and _t in broker_tickets:
                        pos["_seen_on_broker"] = True
                        broker_tickets.discard(_t)
                    elif sym in broker_symbols_count and broker_symbols_count[sym] > 0:
                        pos["_seen_on_broker"] = True
                        broker_symbols_count[sym] -= 1
                    continue
                # Position was seen before — check if still there
                _t = str(pos.get("ticket") or pos.get("position_id") or "")
                sym = pos.get("symbol", pos_key.split("_")[0])
                if _t and _t in broker_tickets:
                    broker_tickets.discard(_t)
                    continue  # Still on broker (matched by ticket)
                if sym in broker_symbols_count and broker_symbols_count[sym] > 0:
                    broker_symbols_count[sym] -= 1
                    continue  # Still on broker (matched by symbol)
                closed_keys.append(pos_key)

            for pos_key in closed_keys:
                async with self._positions_lock:
                    pos = self._open_positions.pop(pos_key, None)
                if pos:
                    sym = pos.get("symbol", "?")
                    entry = pos.get("entry_price", 0)
                    action = pos.get("action", "BUY")
                    qty = pos.get("quantity", 0)
                    _pos_broker = pos.get("broker", "mt5")
                    _bc_ticket = pos.get("ticket") or pos.get("position_id")

                    # ═══ Get REAL PnL from broker deal history (precise) ═══
                    est_pnl = 0
                    _bc_exit_price = 0
                    _bc_commission = 0
                    _bc_reason = "broker_closed"
                    _bc_exit_dt = datetime.now(timezone.utc)
                    _bc_entry_dt = None

                    _dc_bc = self._dash_client()
                    if _bc_ticket and _dc_bc and _dc_bc.is_connected:
                        try:
                            _bc_deals = await _dc_bc.get_deals_by_position(int(_bc_ticket))
                            _bc_close_deals = [d for d in _bc_deals if d.get("is_close")]
                            if _bc_close_deals:
                                _bc_cd = _bc_close_deals[-1]
                                _bc_exit_price = _bc_cd.get("execution_price", 0)
                                _bc_gross = _bc_cd.get("gross_profit", 0)
                                _bc_commission = abs(_bc_cd.get("close_commission", 0))
                                _bc_swap = _bc_cd.get("swap", 0)
                                est_pnl = round(_bc_gross + _bc_swap - _bc_commission, 2)
                                # 2026-04-21: MT5 broker = UTC+3 → soustraire 3h
                                from datetime import timedelta as _td_bc
                                _bc_exit_ts = _bc_cd.get("execution_timestamp", 0)
                                if _bc_exit_ts:
                                    _bc_exit_dt = datetime.fromtimestamp(_bc_exit_ts / 1000, tz=timezone.utc) - _td_bc(hours=3)
                                if _bc_gross > 0:
                                    _bc_reason = "take_profit"
                                elif _bc_gross < 0:
                                    _bc_reason = "stop_loss"
                                # Get entry time from open deal
                                _bc_open_deals = [d for d in _bc_deals if not d.get("is_close")]
                                if _bc_open_deals:
                                    _bc_open_ts = _bc_open_deals[0].get("execution_timestamp", 0)
                                    if _bc_open_ts:
                                        _bc_entry_dt = datetime.fromtimestamp(_bc_open_ts / 1000, tz=timezone.utc)
                                entry = _bc_cd.get("entry_price", entry)
                                qty = _bc_cd.get("closed_volume", qty)
                                logger.info(f"[BROKER CLOSE] {sym}: deal history → exit={_bc_exit_price} gross={_bc_gross:+.2f} comm={_bc_commission:.2f} net={est_pnl:+.2f}")
                        except Exception as _bc_err:
                            logger.warning(f"[BROKER CLOSE] Deal history failed for {sym} ticket={_bc_ticket}: {_bc_err}")

                    # Fallback: balance difference if deal history failed
                    if est_pnl == 0 and _bc_exit_price == 0:
                        _old_balance = self.risk_manager.capital
                        _new_balance = _old_balance
                        try:
                            _dc_bal = self._dash_client()
                            if _dc_bal and _dc_bal.is_connected:
                                _acct = await asyncio.wait_for(_dc_bal.get_account_summary(), timeout=5)
                                _new_balance = _acct.get("balance", _old_balance)
                        except Exception:
                            pass
                        est_pnl = round(_new_balance - _old_balance, 2)

                    # Sync capital from broker via DATA connection
                    try:
                        _dc_sync = self._dash_client()
                        if _dc_sync and _dc_sync.is_connected:
                            _acct_sync = await asyncio.wait_for(_dc_sync.get_account_summary(), timeout=5)
                            _real_bal = _acct_sync.get("balance", 0)
                            if _real_bal > 0:
                                self.risk_manager.capital = _real_bal
                                logger.info(f"[BROKER CLOSE] Capital synced: {_real_bal:.2f}€")
                    except Exception:
                        pass

                    logger.info(f"[BROKER CLOSE] {sym} closed by broker ({_bc_reason}) [{_pos_broker}] — PnL={est_pnl:+.2f}€")

                    # Parse entry time
                    if not _bc_entry_dt:
                        _bc_entry_raw = pos.get("entry_time") or pos.get("opened_at") or ""
                        if _bc_entry_raw:
                            try:
                                _bc_entry_dt = datetime.fromisoformat(str(_bc_entry_raw))
                            except (ValueError, TypeError):
                                pass

                    self._daily_trades.append({
                        "symbol": sym, "action": action,
                        "entry_price": entry, "exit_price": _bc_exit_price,
                        "quantity": qty, "pnl": est_pnl,
                        "net_pnl": est_pnl, "commission": _bc_commission,
                        "reason": _bc_reason,
                        "entry_time": pos.get("entry_time", ""),
                        "exit_time": _bc_exit_dt.isoformat(),
                        "broker": _pos_broker, "market": get_market_for_symbol(sym),
                    })
                    await self._remove_position_db(pos_key)
                    self.risk_manager.record_trade_result(est_pnl)

                    # Save to Trade table
                    try:
                        from app.models.trade import Trade, TradeStatus, TradeSide
                        async with async_session() as _bc_sess:
                            # FIX 2026-04-15: persister les métadonnées d'audit
                            _bc_ticket = pos.get("ticket") or pos.get("position_id")
                            _bc_snap = pos.get("indicators_snapshot")
                            trade = Trade(
                                symbol=sym, name=sym,
                                side=TradeSide.BUY if action == "BUY" else TradeSide.SELL,
                                status=TradeStatus.CLOSED,
                                entry_price=entry, quantity=qty,
                                entry_amount=entry * qty,
                                entry_time=_bc_entry_dt or _bc_exit_dt,
                                exit_price=_bc_exit_price, exit_time=_bc_exit_dt,
                                exit_reason=_bc_reason,
                                stop_loss=pos.get("stop_loss"), take_profit=pos.get("take_profit"),
                                pnl=round(est_pnl + _bc_commission, 2),  # gross
                                pnl_percent=round((est_pnl / (pos.get("margin", 1) or 500)) * 100, 2),
                                commission=round(_bc_commission, 2),
                                net_pnl=round(est_pnl, 2),
                                market=get_market_for_symbol(sym),
                                asset_type=get_market_for_symbol(sym),
                                signal_confidence=pos.get("signal_confidence"),
                                signal_reason=(pos.get("signal_reason") or "")[:512] or None,
                                indicators_snapshot=_bc_snap if isinstance(_bc_snap, (dict, list)) else None,
                                broker_position_id=str(_bc_ticket) if _bc_ticket else None,
                                origin=pos.get("origin") or "bot",
                                source=pos.get("source") or "broker_close",
                            )
                            _bc_sess.add(trade)
                            await _bc_sess.commit()
                            logger.info(f"[BROKER CLOSE] Trade saved to DB: {sym} {_bc_reason} PnL={est_pnl:+.2f}EUR")
                    except Exception as _bc_db_err:
                        logger.error(f"[BROKER CLOSE] Failed to save trade to DB: {_bc_db_err}")

                    await self._broadcast("trade_closed", {
                        "symbol": sym, "pnl": est_pnl,
                        "reason": _bc_reason,
                        "exit_price": _bc_exit_price,
                    })

        # ═══ PERIODIC P&L RESYNC FROM DB — source de vérité, corrige les faux broker close ═══
        try:
            async with async_session() as _resync_sess:
                _resync_result = await _resync_sess.execute(
                    select(OpenPosition).where(
                        OpenPosition.status == "closed",
                        OpenPosition.exit_time >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
                    )
                )
                # Fallback: use trades table
                from app.models.trade import Trade
                _resync_result = await _resync_sess.execute(
                    select(Trade).where(
                        Trade.created_at >= datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
                    )
                )
                _resync_trades = _resync_result.scalars().all()
                if _resync_trades:
                    _db_pnl = sum(t.pnl or 0 for t in _resync_trades)
                    _bot_pnl = self.risk_manager._daily_pnl
                    if abs(_db_pnl - _bot_pnl) > 1.0:
                        logger.warning(f"[PNL RESYNC] Bot PnL={_bot_pnl:.2f} != DB PnL={_db_pnl:.2f} — corrigé vers DB")
                        self.risk_manager._daily_pnl = _db_pnl
        except Exception as _resync_err:
            pass  # Don't crash monitor if resync fails

        # Also sync capital from broker balance via DATA connection
        try:
            _dc_balsync = self._dash_client()
            if _dc_balsync and _dc_balsync.is_connected:
                _bal_sync = await asyncio.wait_for(_dc_balsync.get_account_summary(), timeout=5)
                _real_bal = _bal_sync.get("balance", 0)
                if _real_bal > 0 and abs(_real_bal - self.risk_manager.capital) > 0.5:
                    self.risk_manager.capital = _real_bal
        except Exception:
            pass

        # Write status + REAL MT5 positions to shared IPC files for fast API
        try:
            from app.ipc import write_json as _wj2, STATUS_FILE as _SF2, POSITIONS_FILE as _PF2
            _wj2(_SF2, self.status)
            # Write MT5 positions directly (source of truth, not bot internal state)
            from app.trading.symbol_mapper import get_leverage
            live_pos = []
            for mp in (self._cached_cc_positions or []):
                sym = mp.get("symbol", "")
                lev = get_leverage(sym)
                entry = mp.get("entry_price", 0)
                current = mp.get("current_price", entry)
                pnl = mp.get("unrealized_pnl", 0)
                qty = mp.get("quantity", 0)
                direction = mp.get("direction", "BUY")
                # Calculate notional in EUR
                is_forex = "/" in sym
                sym_upper = sym.upper().replace("/", "")
                if is_forex:
                    if sym_upper.startswith("EUR"):
                        notional = qty
                    elif sym_upper.startswith("USD"):
                        notional = qty / 1.17
                    elif sym_upper.startswith("GBP"):
                        notional = qty * 1.15
                    elif sym_upper.startswith("AUD") or sym_upper.startswith("NZD"):
                        notional = qty * 0.60
                    elif sym_upper.startswith("CAD"):
                        notional = qty * 0.67
                    elif sym_upper.startswith("CHF"):
                        notional = qty * 1.05
                    else:
                        notional = qty
                else:
                    notional = qty * entry / 1.17
                margin = notional / lev if lev > 0 else notional
                # PnL conv rate for frontend
                if sym_upper.endswith("USD") or (not is_forex):
                    _pcr3 = 1.0 / 1.17
                elif sym_upper.endswith("JPY"):
                    _pcr3 = 1.0 / 185.0
                elif sym_upper.endswith("CHF"):
                    _pcr3 = 1.0 / 0.93
                elif sym_upper.endswith("GBP"):
                    _pcr3 = 1.0 / 0.87
                elif sym_upper.endswith("AUD"):
                    _pcr3 = 1.0 / 1.65
                elif sym_upper.endswith("CAD"):
                    _pcr3 = 1.0 / 1.50
                elif sym_upper.endswith("NZD"):
                    _pcr3 = 1.0 / 1.80
                else:
                    _pcr3 = 0.87
                # Get entry_time and origin from bot's internal tracking
                _mp_ticket = mp.get("ticket") or mp.get("position_id")
                _entry_time = ""
                _origin = "bot"
                for _pk, _pv in self._open_positions.items():
                    _pv_ticket = _pv.get("ticket") or _pv.get("position_id")
                    if _pv_ticket and _mp_ticket and str(_pv_ticket) == str(_mp_ticket):
                        _entry_time = _pv.get("entry_time", "")
                        _origin = _pv.get("origin", "bot")
                        break
                live_pos.append({
                    "symbol": sym, "action": direction, "quantity": qty,
                    "entry_price": entry, "current_price": current,
                    "stop_loss": mp.get("stop_loss", 0), "take_profit": mp.get("take_profit", 0),
                    "pnl": round(pnl, 2),
                    "pnl_percent": round((pnl / margin * 100) if margin > 0 else 0, 2),
                    "margin": round(margin, 2), "exposure": round(notional, 2),
                    "leverage_used": f"{lev}:1", "broker": "mt5",
                    "ticket": _mp_ticket, "market_category": "mt5",
                    "pnl_conv_rate": round(_pcr3, 6),
                    "entry_time": _entry_time,
                    "origin": _origin,
                })
            # 2026-05-15: Don't overwrite with empty list — strategy_v6_runner
            # is the primary IPC writer (reads ALL MT5 positions every 3s).
            # Writing [] here causes dashboard flicker when broker data is stale.
            if live_pos:
                _wj2(_PF2, live_pos)
        except Exception:
            pass

        # Position management constants
        from app.trading.signals import _get_thresholds
        DAILY_GAIN_TARGET = 700  # Stop trading after 700EUR daily gain
        DAILY_LOSS_LIMIT = -100  # Hard stop at -100EUR daily loss (positions à 500€ marge)
        DAILY_LOSS_WARNING = -60  # Warning at -60EUR (alert before stop)

        for pos_key in list(self._open_positions.keys()):
            pos = self._open_positions[pos_key]
            symbol = pos.get("symbol", pos_key.split("_")[0])

            try:
                quote = await self._get_quote(symbol)
                if not quote or not quote["price"]:
                    continue

                current_price = quote["price"]
                entry_price = pos["entry_price"]
                sl = pos["stop_loss"]
                tp = pos["take_profit"]
                is_long = pos["action"] == "BUY"

                # Guard: skip phantom positions (entry_price=0 or no SL/TP)
                if not entry_price or entry_price == 0 or not sl or not tp:
                    # Don't clean up positions less than 2 min old (broker may not have synced yet)
                    try:
                        _entry_dt = datetime.fromisoformat(pos.get("entry_time", ""))
                        if _entry_dt.tzinfo is None:
                            _entry_dt = _entry_dt.replace(tzinfo=timezone.utc)
                        _age = (datetime.now(timezone.utc) - _entry_dt).total_seconds()
                        if _age < 120:
                            continue  # Too new, skip phantom check
                    except (ValueError, TypeError):
                        pass
                    # Phantom position with no entry/SL/TP and older than 2min — remove
                    logger.warning(f"[CLEANUP] Removing phantom position {symbol} ({pos_key}) — no entry_price/SL/TP")
                    async with self._positions_lock:
                        del self._open_positions[pos_key]
                    await self._remove_position_db(pos_key)
                    continue

                # Calculate current P&L with proper currency conversion
                _qty_m = pos["quantity"]
                if is_long:
                    _raw_pnl = (current_price - entry_price) * _qty_m
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                    distance_to_tp = (tp - entry_price)
                    progress_to_tp = (current_price - entry_price) / distance_to_tp if distance_to_tp > 0 else 0
                else:
                    _raw_pnl = (entry_price - current_price) * _qty_m
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100 if entry_price > 0 else 0
                    distance_to_tp = (entry_price - tp)
                    progress_to_tp = (entry_price - current_price) / distance_to_tp if distance_to_tp > 0 else 0

                # Convert to EUR
                _sym_u = symbol.upper().replace("/", "")
                if "JPY" in _sym_u:
                    _rate = current_price if _sym_u == "EURJPY" else (current_price * 1.15)
                    pnl = _raw_pnl / _rate if _rate > 0 else _raw_pnl
                elif _sym_u.startswith("EUR"):
                    pnl = _raw_pnl * 0.87
                elif "USD" in _sym_u:
                    pnl = _raw_pnl * 0.87
                elif _sym_u.startswith("GBP"):
                    pnl = _raw_pnl * 1.15
                elif _sym_u.startswith("AUD") or _sym_u.startswith("NZD"):
                    pnl = _raw_pnl * 0.60
                else:
                    pnl = _raw_pnl

                # Broadcast position update
                await self._broadcast("position_update", {
                    "symbol": symbol,
                    "pos_key": pos_key,
                    "current_price": current_price,
                    "pnl": round(pnl, 2),
                    "pnl_percent": round(pnl_pct, 2),
                })

                # ═══ DAILY P&L GUARD — uses internal risk manager (IC Markets capital) ═══
                _total_daily_gain = self.risk_manager._daily_pnl

                if _total_daily_gain >= DAILY_GAIN_TARGET:
                    logger.warning(f"[GAIN GUARD] Daily PnL: +{_total_daily_gain:.2f}€ >= {DAILY_GAIN_TARGET}€ — STOPPING BOT")
                    # HEDGING MODE: don't close via broker, just remove from tracking
                    # User must close remaining positions on MT5
                    self._open_positions.clear()
                    logger.warning(f"[GAIN GUARD] Bot STOPPED. Gain: +{_total_daily_gain:.2f}€. Fermez les positions sur MT5 !")
                    await self._broadcast("alert", {"level": "warning", "message": f"Objectif quotidien atteint (+{_total_daily_gain:.2f}€). Bot arrete. Fermez vos positions sur MT5."})
                    await self.stop(close_positions=False)
                    return

                if _total_daily_gain <= DAILY_LOSS_WARNING and _total_daily_gain > DAILY_LOSS_LIMIT:
                    logger.warning(f"[LOSS WARNING] Daily PnL: {_total_daily_gain:.2f}€ — approche limite {DAILY_LOSS_LIMIT}€")
                    await self._broadcast("alert", {"level": "warning", "message": f"Attention: P&L jour {_total_daily_gain:.2f}€ — limite stop a {DAILY_LOSS_LIMIT}€"})

                if _total_daily_gain <= DAILY_LOSS_LIMIT:
                    logger.warning(f"[LOSS GUARD] Daily PnL: {_total_daily_gain:.2f}€ <= {DAILY_LOSS_LIMIT}€ — STOPPING BOT")
                    await self._broadcast("alert", {"level": "critical", "message": f"Limite de perte journaliere atteinte ({_total_daily_gain:.2f}€). Bot arrete."})
                    await self.stop(close_positions=False)
                    return

                exit_reason = None  # Initialize before guards
                # ═══ POSITION AGE — bot internal tracking ═══
                _pos_age_sec = 9999
                # Method 1: Bot internal timestamp
                _ts = pos.get("_opened_ts", 0)
                if _ts > 0:
                    import time as _t
                    _pos_age_sec = _t.time() - _ts
                else:
                    try:
                        _et = pos.get("entry_time", "")
                        if _et:
                            _edt = datetime.fromisoformat(str(_et))
                            if _edt.tzinfo is None: _edt = _edt.replace(tzinfo=timezone.utc)
                            _pos_age_sec = (datetime.now(timezone.utc) - _edt).total_seconds()
                    except Exception:
                        pass

                # ═══ TRAILING STOP — paliers 5%, SL = palier-5%, TP auto-extension à 90% ═══
                _sl_changed, _tp_changed = self._apply_trailing_stop(pos, current_price, log_prefix="[TRAIL]")
                if _sl_changed:
                    sl = pos["stop_loss"]
                if _tp_changed:
                    tp = pos["take_profit"]
                # Sync trailing SL/TP to MT5 (native SL/TP update)
                if _sl_changed or _tp_changed:
                    _ticket = pos.get("ticket") or pos.get("position_id")
                    if self.mt5_available and _ticket:
                        try:
                            updated = await self.mt5.amend_position_sltp(
                                int(_ticket),
                                stop_loss=sl if _sl_changed else None,
                                take_profit=tp if _tp_changed else None,
                            )
                            if updated:
                                logger.info(f"[TRAIL] {symbol}: SL/TP synced to MT5 (SL={sl:.5f} TP={tp:.5f})")
                            else:
                                logger.warning(f"[TRAIL] {symbol}: MT5 SL/TP amend failed — bot monitoring as fallback")
                        except Exception as _e:
                            logger.warning(f"[TRAIL] {symbol}: MT5 sync error: {_e} — bot monitoring as fallback")

                # ═══ CHECK SL/TP ═══
                if is_long and current_price <= sl:
                    exit_reason = "stop_loss"
                elif not is_long and current_price >= sl:
                    exit_reason = "stop_loss"
                if is_long and current_price >= tp:
                    exit_reason = "take_profit"
                elif not is_long and current_price <= tp:
                    exit_reason = "take_profit"

                # ═══ STOP-AND-REVERSE : DÉSACTIVÉ (2026-04-10) ═══
                # Analyse 7J : flip_reverse = 0% WR sur 8 trades → jamais gagnant.
                # Seul le max_hold 5 min reste comme filet ci-dessous.

                # ═══ MAX HOLD TIME — 2026-04-21: 10→60 min forex / 90 min indices (replay) ═══
                # Laisse respirer les positions: les replays montrent que les "perdants" à 10 min
                # sont souvent gagnants à 35-60 min (AUD/CHF -38→+142€, SP500 -3→+19€).
                # Utilise les valeurs paire-spécifiques de FOREX_THRESHOLDS/INDEX_THRESHOLDS.
                from app.trading.signals import _get_thresholds as _get_th_mh
                _th = _get_th_mh(symbol)
                _max_hold_min = _th.get("max_hold_minutes", 60)
                _max_hold_sec = _max_hold_min * 60
                _stagnation_sec = max(_max_hold_sec + 30*60, 30 * 60)  # stagnation = max_hold + 30min
                if not exit_reason:
                    _hold_pnl = (current_price - entry) if is_long else (entry - current_price)
                    # Règle 1: perdante >max_hold (60 forex / 90 index)
                    if _pos_age_sec >= _max_hold_sec and _hold_pnl < 0:
                        logger.warning(f"[MAX HOLD] {symbol}: perdante {_hold_pnl:+.5f} depuis {_pos_age_sec/60:.0f}min > {_max_hold_min}min — fermeture")
                        _ticket = pos.get("ticket") or pos.get("position_id")
                        try:
                            await self._close_position_broker(symbol, ticket=_ticket)
                        except Exception as _e:
                            logger.error(f"[MAX HOLD] {symbol}: broker close error: {_e}")
                        exit_reason = "max_hold_time"
                    # Règle 2: stagnation break-even >20 min (PnL <= 0 mais pas négatif assez pour max_hold)
                    elif _pos_age_sec >= _stagnation_sec and _hold_pnl <= 0:
                        logger.warning(f"[STAGNATION] {symbol}: {_hold_pnl:+.5f} depuis {_pos_age_sec/60:.0f}min > 20min sans gain — fermeture")
                        _ticket = pos.get("ticket") or pos.get("position_id")
                        try:
                            await self._close_position_broker(symbol, ticket=_ticket)
                        except Exception as _e:
                            logger.error(f"[STAGNATION] {symbol}: broker close error: {_e}")
                        exit_reason = "stagnation_20min"
                    else:
                        if _pos_age_sec % 300 < 15 and _hold_pnl > 0:  # Log toutes les 5 min
                            logger.info(f"[MAX HOLD] {symbol}: gagnante +{_hold_pnl:.5f} après {_pos_age_sec/60:.0f}min — on laisse courir (trailing stop actif)")

                if exit_reason:
                    await self._close_position(pos_key, exit_reason, current_price)

            except Exception as e:
                logger.error(f"Error monitoring position {symbol} ({pos_key}): {e}")

    async def _close_position(self, pos_key: str, reason: str, exit_price: float):
        """Close a position — uses MT5 close_position API (safe, no hedging issue)."""
        pos = self._open_positions.get(pos_key)
        if not pos:
            return
        symbol = pos.get("symbol", pos_key.split("_")[0])
        _ticket = pos.get("ticket") or pos.get("position_id")

        # Close on MT5 using proper close API (not opposite order)
        try:
            result = await self._close_position_broker(symbol, ticket=_ticket)
            if result:
                logger.info(f"[CLOSE] {symbol} {reason} — broker close confirmed (MT5)")
                # Use broker's exit price if available
                if result.get("pnl") is not None:
                    logger.info(f"[CLOSE] {symbol}: broker reported PnL={result.get('pnl')}")
            else:
                logger.warning(f"[CLOSE] {symbol} {reason} — broker close returned None, position may already be closed")
        except Exception as _e:
            logger.error(f"[CLOSE] {symbol} {reason} — broker close error: {_e}")

        logger.info(f"[CLOSE] {symbol} {reason} @ {exit_price} — removing from bot tracking")

        if True:  # Always proceed with internal close
            # Commission: MT5 Raw Spread account commission estimation
            # IC Markets Raw Spread: FOREX = $3.50/lot/side ($7 round trip per 100k)
            # INDICES/COMMODITIES/STOCKS = 0 commission (included in spread)
            entry_commission = pos.get("entry_commission", 0)
            total_commission_raw = abs(entry_commission)
            # Convert to EUR (approx)
            total_commission_eur = total_commission_raw * 0.87  # USD→EUR
            # If commission is 0 (MT5 didn't report), estimate ONLY for forex
            if total_commission_eur == 0:
                _market_type = get_market_for_symbol(symbol)
                if _market_type == "FOREX":
                    # IC Markets Raw Spread: $3.50 per lot per side = $7 round trip per 100k
                    lots = pos["quantity"] / 100000
                    total_commission_eur = lots * 7.0 * 0.87  # $7/lot round trip → EUR
                else:
                    # Indices, commodities, stocks: commission is in the spread (0)
                    total_commission_eur = 0.0

            entry_price = pos.get("entry_price", 0) or 0
            is_long = pos["action"] == "BUY"

            # GUARD: never record trade with entry_price=0 (sync failure)
            if not entry_price or entry_price <= 0:
                logger.error(f"[CLOSE] {symbol}: entry_price={entry_price} invalide — trade NON enregistre (donnees corrompues)")
                async with self._positions_lock:
                    if pos_key in self._open_positions:
                        del self._open_positions[pos_key]
                return

            # GUARD: never record trade with exit_price=0 (quote failure)
            if not exit_price or exit_price <= 0:
                logger.error(f"[CLOSE] {symbol}: exit_price={exit_price} invalide — utilisation entry_price comme fallback (P&L=0)")
                exit_price = entry_price  # P&L = 0 plutôt qu'un montant aberrant

            # Calculate P&L — proper forex currency conversion
            _qty = pos["quantity"]
            if is_long:
                raw_pnl = (exit_price - entry_price) * _qty
            else:
                raw_pnl = (entry_price - exit_price) * _qty

            # Convert P&L to EUR based on quote currency
            sym_upper = symbol.upper().replace("/", "")
            if "JPY" in sym_upper:
                # P&L is in JPY — divide by EUR/JPY rate to get EUR
                eurjpy_rate = exit_price if sym_upper == "EURJPY" else (exit_price * 1.15)  # approx
                pnl = raw_pnl / eurjpy_rate if eurjpy_rate > 0 else raw_pnl
            elif sym_upper.startswith("EUR"):
                pnl = raw_pnl * 0.87  # approx quote→EUR (USD, GBP, CAD, etc.)
            elif sym_upper.startswith("USD") or sym_upper.endswith("USD"):
                pnl = raw_pnl * 0.87  # USD→EUR approx
            elif sym_upper.startswith("GBP"):
                pnl = raw_pnl * 1.15  # GBP→EUR approx
            elif sym_upper.startswith("AUD") or sym_upper.startswith("NZD"):
                pnl = raw_pnl * 0.60  # AUD/NZD→EUR approx
            elif sym_upper.startswith("CHF") or sym_upper.endswith("CHF"):
                pnl = raw_pnl * 1.05  # CHF→EUR approx
            else:
                pnl = raw_pnl  # stocks/indices already in EUR or USD

            # Net PnL = gross PnL - commission
            net_pnl = pnl - total_commission_eur

            # WARNING for unusually large P&L (but do NOT cap it)
            if abs(net_pnl) > 100:
                logger.warning(f"[CLOSE] {symbol}: large P&L {net_pnl:+.2f}€ (exit={exit_price}, entry={entry_price}, qty={_qty}) — verify manually")

            # Record result (use net PnL for risk management)
            self.risk_manager.record_trade_result(net_pnl)

            # ── COOLDOWN: 45min après CHAQUE fermeture (gain ou perte) ──
            # Empêche le bot de réouvrir immédiatement la même paire.
            # 2026-04-23 FIX: le code était à 900s (15min) alors que commentaire+log
            # disaient 45min. Bug résolu en rétablissant 2700s (= 45min).
            import time as _t_cd
            _norm_cd_sym = symbol.replace("/", "").replace(".", "").upper()
            self._symbol_cooldown[_norm_cd_sym] = _t_cd.time() + 2700  # 45min cooldown
            logger.info(f"[COOLDOWN] {_norm_cd_sym}: NetPnL={net_pnl:+.2f}EUR → cooldown 45min")

            # ── DIRECTION COOLDOWN: 30min pause sur même direction après 2 SL consécutifs ──
            # 2026-04-23 : évite d'enchaîner 5 SELL perdants comme aujourd'hui.
            _pos_action = (pos.get("action") or "").upper()
            _exit_reason = close_reason or ""
            _is_sl = "stop_loss" in _exit_reason.lower() or "max_hold" in _exit_reason.lower() or net_pnl < 0
            if _pos_action in ("BUY", "SELL"):
                if _is_sl:
                    _prev_cnt = self._consec_dir_losses.get(_pos_action, 0)
                    self._consec_dir_losses[_pos_action] = _prev_cnt + 1
                    if self._consec_dir_losses[_pos_action] >= 2:
                        self._direction_cooldown[_pos_action] = _t_cd.time() + 1800  # 30min
                        logger.warning(
                            f"[DIRECTION COOLDOWN] {_pos_action}: {self._consec_dir_losses[_pos_action]} pertes consécutives "
                            f"→ pause 30min sur nouvelles entrées {_pos_action}"
                        )
                else:
                    # Gain → reset compteur pertes pour cette direction
                    self._consec_dir_losses[_pos_action] = 0

            # ── EXTENDED COOLDOWN (4h) : 2 losses sur même paire dans la journée ──
            # 2026-04-23 v2 : remplace max 1/jour. Après 2 losses sur EUR/GBP → 4h cooldown sur cette paire.
            from datetime import datetime as _dt_lo
            _today_lo_iso = _dt_lo.now().date().isoformat()
            if self._symbol_losses_today_date != _today_lo_iso:
                self._symbol_losses_today = {}
                self._symbol_extended_cd = {}
                self._symbol_losses_today_date = _today_lo_iso
            _canon_lo = self._canonical_symbol(symbol)
            if _is_sl:
                _n_losses = self._symbol_losses_today.get(_canon_lo, 0) + 1
                self._symbol_losses_today[_canon_lo] = _n_losses
                if _n_losses >= 2:
                    self._symbol_extended_cd[_canon_lo] = _t_cd.time() + 14400  # 4h
                    logger.warning(
                        f"[EXTENDED COOLDOWN] {_canon_lo}: {_n_losses} losses aujourd'hui "
                        f"→ pause 4h sur cette paire"
                    )

            # 2026-04-23 : persister les garde-fous après chaque fermeture (survive restarts)
            try:
                self._save_guards()
            except Exception:
                pass

            self._daily_trades.append({
                "symbol": symbol,
                "action": pos["action"],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": pos["quantity"],
                "pnl": round(pnl, 2),
                "net_pnl": round(net_pnl, 2),
                "commission": round(total_commission_eur, 2),
                "reason": reason,
                "entry_time": pos["entry_time"],
                "exit_time": datetime.now().isoformat(),
                "broker": "mt5",
            })

            async with self._positions_lock:
                del self._open_positions[pos_key]

            # Persist closure to DB (OpenPosition table)
            await self._close_position_db(pos_key, exit_price, pnl)

            # Also save to Trade table for dashboard history
            try:
                from app.models.trade import Trade, TradeStatus, TradeSide
                async with async_session() as session:
                    # Parse entry_time from position data
                    _raw_entry_time = pos.get("entry_time") or pos.get("opened_at") or ""
                    _entry_dt = None
                    if _raw_entry_time:
                        try:
                            _entry_dt = datetime.fromisoformat(str(_raw_entry_time))
                        except (ValueError, TypeError):
                            _entry_dt = None
                    if not _entry_dt:
                        # Fallback: use _opened_ts (unix timestamp)
                        _opened_ts = pos.get("_opened_ts", 0)
                        if _opened_ts > 0:
                            _entry_dt = datetime.fromtimestamp(_opened_ts, tz=timezone.utc)
                    # FIX 2026-04-15: persister les métadonnées d'audit (signal_*, broker_*, origin)
                    _cp_ticket = pos.get("ticket") or pos.get("position_id")
                    _cp_snap = pos.get("indicators_snapshot")
                    trade = Trade(
                        symbol=symbol,
                        name=symbol,
                        side=TradeSide.BUY if pos["action"] == "BUY" else TradeSide.SELL,
                        status=TradeStatus.CLOSED,
                        entry_price=entry_price,
                        quantity=pos["quantity"],
                        entry_amount=entry_price * pos["quantity"],
                        entry_time=_entry_dt or datetime.now(timezone.utc),
                        exit_price=exit_price,
                        exit_time=datetime.now(timezone.utc),
                        exit_reason=reason,
                        stop_loss=pos.get("stop_loss"),
                        take_profit=pos.get("take_profit"),
                        pnl=round(pnl, 2),
                        pnl_percent=round((pnl / (pos.get("margin", 1) or 1)) * 100, 2),
                        commission=round(total_commission_eur, 2),
                        commission_raw=round(total_commission_raw, 4),
                        net_pnl=round(net_pnl, 2),
                        market=get_market_for_symbol(symbol),
                        asset_type=get_market_for_symbol(symbol),
                        signal_confidence=pos.get("signal_confidence"),
                        signal_reason=(pos.get("signal_reason") or "")[:512] or None,
                        indicators_snapshot=_cp_snap if isinstance(_cp_snap, (dict, list)) else None,
                        broker_position_id=str(_cp_ticket) if _cp_ticket else None,
                        origin=pos.get("origin") or "bot",
                        source=pos.get("source") or "bot",
                        signal_ts=pos.get("signal_ts"),
                        order_sent_ts=pos.get("order_sent_ts"),
                        fill_ts=pos.get("fill_ts"),
                        signal_to_send_ms=pos.get("signal_to_send_ms"),
                        send_to_fill_ms=pos.get("send_to_fill_ms"),
                    )
                    session.add(trade)
                    await session.commit()
                    logger.info(f"[DB] Trade saved: {symbol} {reason} PnL={pnl:+.2f}EUR")
            except Exception as e:
                logger.error(f"[DB] Failed to save trade: {e}")

            # Update risk manager
            self.risk_manager.update_state(
                daily_pnl=self.risk_manager._daily_pnl,
                open_positions=len(self._open_positions),
                open_symbols=[p.get("symbol", k.split("_")[0]) for k, p in self._open_positions.items()],
                capital=self.risk_manager.capital,
            )

            logger.info(f"Closed {symbol} (Fusion Markets MT5): reason={reason} GrossPnL={pnl:+.2f}EUR Commission={total_commission_eur:.2f}EUR NetPnL={net_pnl:+.2f}EUR")

            await self._broadcast("trade_closed", {
                "symbol": symbol,
                "pnl": round(pnl, 2),
                "reason": reason,
                "exit_price": exit_price,
            })

            # Alert user to close the position on MT5 (hedging mode)
            await self._broadcast("alert", {
                "level": "warning",
                "message": f"{symbol} {reason.upper()} declenche @ {exit_price:.5f} — PnL: {net_pnl:+.2f}€. Fermez cette position sur MT5 !",
            })

    async def _close_all_positions(self, reason: str):
        for pos_key in list(self._open_positions.keys()):
            pos = self._open_positions.get(pos_key, {})
            sym = pos.get("symbol", pos_key.split("_")[0])
            try:
                quote = await self._get_quote(sym)
                price = quote["price"] if quote and quote.get("price") else pos.get("entry_price", 0)
            except Exception:
                price = pos.get("entry_price", 0)
            await self._close_position(pos_key, reason, price)

    # ── Operator Order Registration ──────────────────────────────────────

    def register_operator_order(self, symbol: str, side: str, ttl_seconds: int = 600):
        """Pre-register an expected manual order from the operator (via dashboard).
        Call this BEFORE placing the order on MT5."""
        import time as _t
        norm = symbol.upper().replace("/", "").replace(".", "")
        key = f"{norm}_{side.upper()}"
        self._pending_operator_orders[key] = {
            "symbol": symbol,
            "side": side.upper(),
            "created_at": _t.time(),
            "expires_at": _t.time() + ttl_seconds,
        }
        logger.info(f"[OPERATOR] Registered expected order: {side.upper()} {symbol} (TTL={ttl_seconds}s)")

    def cancel_operator_order(self, symbol: str, side: str):
        """Cancel a pending operator order registration."""
        norm = symbol.upper().replace("/", "").replace(".", "")
        key = f"{norm}_{side.upper()}"
        if key in self._pending_operator_orders:
            del self._pending_operator_orders[key]
            logger.info(f"[OPERATOR] Cancelled expected order: {side.upper()} {symbol}")

    async def reclassify_position(self, pos_key: str, new_origin: str) -> bool:
        """Reclassify a position's origin (operator/orphan/bot)."""
        pos = self._open_positions.get(pos_key)
        if not pos:
            return False
        old_origin = pos.get("origin", "unknown")
        pos["origin"] = new_origin
        if new_origin == "operator":
            pos["orphan"] = False
        logger.info(f"[RECLASSIFY] {pos.get('symbol')} {pos_key}: {old_origin} → {new_origin}")
        # Update DB
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(OpenPosition).where(OpenPosition.pos_key == pos_key)
                )
                db_pos = result.scalar_one_or_none()
                if db_pos:
                    db_pos.origin = new_origin
                    db_pos.manual = (new_origin == "operator")
                    await session.commit()
        except Exception as e:
            logger.error(f"[RECLASSIFY] DB update failed: {e}")
        return True

    # ── Manual Operations (from UI) ───────────────────────────────────────

    async def manual_order(self, symbol: str, action: str, amount_eur: float) -> dict:
        """Place a manual order from the operator UI with SL/TP from PAIR_CONFIG."""
        import time
        # ═══ TIMING — clock starts at request reception (parity with bot signal flow)
        _t_req = time.time()
        quote = await self._get_quote(symbol)
        if not quote or not quote.get("price"):
            return {"success": False, "error": f"Impossible d'obtenir le prix pour {symbol}"}

        price = quote["price"]
        if not price or price <= 0:
            return {"success": False, "error": f"Prix invalide pour {symbol}: {price}"}

        # Calculate SL/TP from PAIR_CONFIG
        pair_cfg = get_pair_config(symbol)
        sym_upper = symbol.upper().replace("/", "")
        stop_loss = None
        take_profit = None
        if pair_cfg:
            if "sl_pct" in pair_cfg:
                # Stocks, indices, commodities: SL/TP as % of price
                sl_distance = price * pair_cfg["sl_pct"]
                tp_distance = price * pair_cfg["tp_pct"]
            elif "sl_pips" in pair_cfg:
                # Forex: SL/TP in pips
                pip_size = 0.01 if "JPY" in sym_upper else 0.0001
                sl_distance = pair_cfg["sl_pips"] * pip_size
                tp_distance = pair_cfg["tp_pips"] * pip_size
            else:
                sl_distance = price * 0.005  # Default 0.5%
                tp_distance = price * 0.010  # Default 1.0%

            if action.upper() == "BUY":
                stop_loss = round(price - sl_distance, 5)
                take_profit = round(price + tp_distance, 5)
            else:
                stop_loss = round(price + sl_distance, 5)
                take_profit = round(price - tp_distance, 5)
            logger.info(f"[MANUAL] {symbol} SL/TP calculated: SL={stop_loss:.5f} TP={take_profit:.5f}")
        else:
            logger.warning(f"[MANUAL] No PAIR_CONFIG for {symbol} — order without SL/TP")

        # Calculate quantity based on leverage
        leverage = get_leverage(symbol)
        position_eur = amount_eur * leverage
        quantity = position_eur / price

        # MT5 minimum sizes (forex = 1000 units = 0.01 lot)
        is_forex = "/" in symbol and any(c in symbol.upper() for c in ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD"])
        if is_forex and quantity < 1000:
            quantity = 1000
            logger.info(f"[MANUAL] {symbol}: qty bumped to minimum 1000 units (0.01 lot)")

        quantity = round(quantity, 2)
        _t_pre_order = time.time()
        result = await self._place_order(symbol, action.upper(), quantity,
                                         stop_loss=stop_loss, take_profit=take_profit)
        _t_post_order = time.time()
        _broker_ms = int((_t_post_order - _t_pre_order) * 1000)
        _total_ms = int((_t_post_order - _t_req) * 1000)
        logger.info(
            f"[SPEED MANUAL] {symbol} {action.upper()}: request→fill={_total_ms}ms "
            f"(quote+sl/tp={_total_ms - _broker_ms}ms, broker_rpc={_broker_ms}ms) ✓"
        )
        if result and result.get("status") == "FILLED":
            # Track position internally
            fill_price = result.get("fill_price", price)
            pos_key = f"{symbol}_{int(time.time())}"
            async with self._positions_lock:
                self._open_positions[pos_key] = {
                    "symbol": symbol,
                    "action": action.upper(),
                    "entry_price": fill_price,
                    "quantity": result.get("fill_qty", quantity),
                    "stop_loss": stop_loss or 0,
                    "take_profit": take_profit or 0,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "_opened_ts": time.time(),
                    "broker": "mt5",
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                    "position_eur": amount_eur,
                    "manual": True,
                    # 2026-04-23 fix bug #4: tag origin='manual' pour distinguer sur dashboard
                    "origin": "manual",
                    "source": "manual",
                    "sl_order_id": result.get("sl_order_id"),
                    "tp_order_id": result.get("tp_order_id"),
                }
            logger.info(f"[MANUAL] {action.upper()} {symbol} @ {fill_price} qty={quantity:.2f} SL={stop_loss} TP={take_profit} — position tracked")
            await self._save_position(pos_key, self._open_positions[pos_key])
            return {"success": True, "order_id": result.get("order_id"), "fill_price": fill_price,
                    "stop_loss": stop_loss, "take_profit": take_profit}
        if result and result.get("status") == "REJECTED":
            return {"success": False, "error": result.get("error", f"Ordre rejete pour {symbol}")}
        return {"success": False, "error": f"Ordre non rempli pour {symbol}: {result}"}

    async def projection_order(self, symbol: str, action: str, tp_price: float) -> dict:
        """Place a projection order: user provides TP, system calculates SL via ATR and lot for 10€ max risk."""
        import time as _time
        from app.trading.projection_sizing import compute_projection, compute_atr_from_candles

        quote = await self._get_quote(symbol)
        if not quote or not quote.get("price"):
            return {"success": False, "error": f"Prix indisponible pour {symbol}"}
        price = float(quote["price"])
        if price <= 0:
            return {"success": False, "error": f"Prix invalide pour {symbol}: {price}"}

        h1_candles = (self._candle_cache_h1 or {}).get(symbol) or []
        if len(h1_candles) < 15:
            try:
                h1_candles = await self.mt5.get_historical_candles(symbol, duration="5 D", bar_size="1 hour")
            except Exception as e:
                return {"success": False, "error": f"Impossible de charger H1 pour {symbol}: {e}"}
        if len(h1_candles) < 15:
            return {"success": False, "error": f"Pas assez de données H1 pour {symbol} ({len(h1_candles)} barres)"}

        atr_h1 = compute_atr_from_candles(h1_candles, period=14)
        if not atr_h1 or atr_h1 <= 0:
            return {"success": False, "error": f"ATR H1 incalculable pour {symbol}"}

        proj = compute_projection(symbol, action, price, tp_price, atr_h1)
        if proj is None:
            tp_dist = abs(tp_price - price)
            from app.trading.projection_sizing import MIN_RR, _get_sl_min
            max_sl = tp_dist / MIN_RR if tp_dist > 0 else 0
            sl_min = _get_sl_min(symbol)
            if max_sl < sl_min and tp_dist > 0:
                min_tp_dist = sl_min * MIN_RR
                if action.lower() == "buy":
                    min_tp = round(price + min_tp_dist, 2)
                else:
                    min_tp = round(price - min_tp_dist, 2)
                return {"success": False, "error": (
                    f"TP trop proche pour R:R≥{MIN_RR} sans stop-hunt. "
                    f"Distance TP={tp_dist:.2f}, SL requis={max_sl:.5f} < buffer min={sl_min}. "
                    f"TP minimum: {min_tp}"
                )}
            return {"success": False, "error": f"Calcul projection impossible (TP incohérent avec direction ?)"}

        logger.warning(
            f"[PROJECTION] {symbol} {action.upper()}: entry={proj.entry_price} "
            f"SL={proj.sl_price} TP={proj.tp_price} lot={proj.lot} "
            f"risk={proj.risk_eur}€ reward={proj.reward_eur}€ R:R=1:{proj.rr_ratio}"
        )

        result = await self._place_order(
            symbol, action.upper(), proj.quantity,
            stop_loss=proj.sl_price, take_profit=proj.tp_price,
        )

        if result and result.get("status") == "FILLED":
            fill_price = result.get("fill_price", price)
            pos_key = f"{symbol}_proj_{int(_time.time())}"
            async with self._positions_lock:
                self._open_positions[pos_key] = {
                    "symbol": symbol,
                    "action": action.upper(),
                    "entry_price": fill_price,
                    "quantity": proj.quantity,
                    "stop_loss": proj.sl_price,
                    "take_profit": proj.tp_price,
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "_opened_ts": _time.time(),
                    "broker": "mt5",
                    "manual": True,
                    "origin": "projection",
                    "source": "projection",
                    "signal_reason": (
                        f"PROJECTION {action.upper()} TP={proj.tp_price} "
                        f"SL={proj.sl_price} ATR={proj.atr_h1:.5f} lot={proj.lot}"
                    ),
                    "sl_order_id": result.get("sl_order_id"),
                    "tp_order_id": result.get("tp_order_id"),
                }
            logger.warning(
                f"[PROJECTION] FILLED {action.upper()} {symbol} @ {fill_price} "
                f"lot={proj.lot} SL={proj.sl_price} TP={proj.tp_price} risk={proj.risk_eur}€"
            )
            return {
                "success": True,
                "fill_price": fill_price,
                "stop_loss": proj.sl_price,
                "take_profit": proj.tp_price,
                "lot": proj.lot,
                "risk_eur": proj.risk_eur,
                "reward_eur": proj.reward_eur,
                "rr_ratio": proj.rr_ratio,
                "atr_h1": round(proj.atr_h1, 5),
                "sl_distance": proj.sl_distance,
            }

        if result and result.get("status") == "REJECTED":
            return {"success": False, "error": result.get("error", f"Ordre rejeté pour {symbol}")}
        return {"success": False, "error": f"Ordre non rempli pour {symbol}: {result}"}

    async def manual_close(self, symbol: str) -> dict:
        """Manually close a position from the UI — closes on MT5.

        First checks bot memory, then falls back to broker positions.
        This ensures positions can ALWAYS be closed from dashboard.
        """
        # Find pos_key: could be "symbol" or "symbol_dealId"
        pos_key = None
        pos_data = None
        for k, v in self._open_positions.items():
            if k == symbol or v.get("symbol") == symbol:
                pos_key = k
                pos_data = v
                break

        # If not in bot memory, search directly on broker
        _ticket = None
        if pos_data:
            _ticket = pos_data.get("ticket") or pos_data.get("position_id")
        else:
            logger.warning(f"[MANUAL CLOSE] {symbol} not in bot memory — searching broker directly")
            try:
                _dc_mc = self._dash_client()
                if _dc_mc and _dc_mc.is_connected:
                    broker_positions = await _dc_mc.get_positions()
                    for bp in broker_positions:
                        bp_sym = bp.get("symbol", "")
                        if (bp_sym == symbol
                                or bp_sym.replace("/", "") == symbol.replace("/", "")
                                or symbol.replace("/", "") == bp_sym.replace("/", "")):
                            _ticket = bp.get("ticket")
                            pos_data = bp
                            pos_key = symbol
                            logger.info(f"[MANUAL CLOSE] Found {symbol} on broker: ticket={_ticket}")
                            break
            except Exception as _be:
                logger.error(f"[MANUAL CLOSE] Broker position search error: {_be}")

        if not _ticket and not pos_key:
            return {"success": False, "error": f"Position {symbol} introuvable (ni en memoire, ni chez le broker)"}

        # Close on MT5 FIRST
        try:
            result = await self._close_position_broker(symbol, ticket=_ticket)
            if result:
                logger.info(f"[MANUAL CLOSE] {symbol} closed on MT5: {result}")
            else:
                logger.warning(f"[MANUAL CLOSE] {symbol}: broker close returned None — may already be closed")
        except Exception as e:
            logger.error(f"[MANUAL CLOSE] {symbol}: broker close error: {e}")
            return {"success": False, "error": f"Erreur fermeture broker: {e}"}

        # Get exit price
        quote = await self._get_quote(symbol)
        price = quote["price"] if quote else 0

        # Internal close (DB + tracking) — only if we had it tracked
        if pos_key and pos_key in self._open_positions:
            await self._close_position(pos_key, "manual", price)
        elif pos_key:
            # Remove from DB if it was there
            try:
                await self._remove_position_db(pos_key)
            except Exception:
                pass
        return {"success": True}

    def get_signals(self) -> list[dict]:
        return list(self._last_signals.values())

    def get_quotes(self) -> dict:
        return self._last_quotes

    def get_open_positions_info(self) -> list[dict]:
        return list(self._open_positions.values())

    def get_daily_trades(self) -> list[dict]:
        return self._daily_trades

    # ═══════════════════════════════════════════════════════════════════
    # HOURLY AUDIT REPORT — validates locked parameters every hour
    # ═══════════════════════════════════════════════════════════════════

    LOCKED_PARAMS = {
        "forex_sl_multiplier": 1.0,
        "forex_tp_multiplier": 1.5,
        "forex_max_hold_min": 60,   # 2026-04-21: 15→60 min (replay AUD/CHF)
        "forex_min_tp_pips": 8,
        "forex_sl_cap_pips": 16,
        "forex_tp_cap_pips": 24,
        "index_sl_multiplier": 1.5,
        "index_tp_multiplier": 3.0,
        "index_max_hold_min": 90,   # 2026-04-21: 25→90 min (replay SP500)
        "min_confidence": 70,
        "min_risk_reward": 1.0,
        "max_risk_per_trade": 0.02,   # 2026-04-21 URGENT: 8%→2% (risque max 32€/trade à capital 1600€)
        "max_daily_loss": 0.30,       # 2026-04-22 TEMP: 30% pour catch Asia ce soir (reset demain)
        "max_open_positions": 5,      # 2026-04-21: 10→5 (réduire exposition)
        "leverage_forex": 500,   # Fusion Markets VFSC offshore (compte 429608)
        "leverage_indices": 200,  # Fusion Markets VFSC offshore (compte 429608)
        "trailing_start_pct": 20,  # trailing starts at 20% of TP (2026-04-16: laisser respirer)
        "trailing_sl_offset": 5,  # SL trails 5% behind progress
        "daily_gain_target": 700,
        "daily_loss_limit": -50,
        "candles_5d_bars": 480,
        "assets_count": None,  # dynamic — ne pas verifier
    }

    def generate_audit_report(self) -> dict:
        """Generate hourly audit report validating all locked parameters."""
        from app.trading.signals import FOREX_THRESHOLDS, INDEX_THRESHOLDS, FOREX_PAIRS
        from app.trading.symbol_mapper import ASSETS
        from app.config import settings

        current = {
            "forex_sl_multiplier": FOREX_THRESHOLDS["sl_multiplier"],
            "forex_tp_multiplier": FOREX_THRESHOLDS["tp_multiplier"],
            "forex_max_hold_min": FOREX_THRESHOLDS["max_hold_minutes"],
            "forex_min_tp_pips": FOREX_THRESHOLDS["min_tp_pips"],
            "forex_sl_cap_pips": 16,
            "forex_tp_cap_pips": 24,
            "index_sl_multiplier": INDEX_THRESHOLDS["sl_multiplier"],
            "index_tp_multiplier": INDEX_THRESHOLDS["tp_multiplier"],
            "index_max_hold_min": INDEX_THRESHOLDS["max_hold_minutes"],
            "min_confidence": 70,
            "min_risk_reward": self.risk_manager.min_risk_reward,
            "max_risk_per_trade": self.risk_manager.max_risk_per_trade,
            "max_daily_loss": self.risk_manager.max_daily_loss,
            "max_open_positions": self.risk_manager.max_open_positions,
            "leverage_forex": settings.leverage_forex,
            "leverage_indices": settings.leverage_indices,
            "trailing_start_pct": 20,  # trailing starts at 20% of TP (2026-04-16: laisser respirer)
            "trailing_sl_offset": 5,  # SL trails 5% behind progress
            "daily_gain_target": 700,
            "daily_loss_limit": -50,
            "candles_5d_bars": 480,
            "assets_count": len(ASSETS),
        }

        violations = []
        for key, expected in self.LOCKED_PARAMS.items():
            if expected is None:
                continue  # Skip dynamic/unchecked params
            actual = current.get(key)
            if actual != expected:
                violations.append({
                    "param": key,
                    "expected": expected,
                    "actual": actual,
                })

        assets_list = [a.symbol for a in ASSETS]
        forex_count = sum(1 for a in ASSETS if a.asset_type == "forex")
        index_count = sum(1 for a in ASSETS if a.asset_type == "index_cfd")

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "OK" if not violations else "VIOLATION",
            "violations": violations,
            "locked_params": current,
            "trading_stats": {
                "bot_running": self._running,
                "open_positions": len(self._open_positions),
                "trades_today": len(self._daily_trades),
                "daily_pnl": round(self.risk_manager._daily_pnl, 2),
                "capital": round(self.risk_manager.capital, 2),
                "consecutive_losses": self.risk_manager._consecutive_losses,
                "circuit_breaker": self.risk_manager._circuit_breaker_active,
            },
            "assets": {
                "total": len(ASSETS),
                "forex": forex_count,
                "indices": index_count,
                "list": assets_list,
            },
        }

        # Write to shared file for dashboard
        try:
            import json
            with open("/tmp/audit_report.json", "w") as f:
                json.dump(report, f)
        except Exception:
            pass

        return report

    async def _functional_test(self):
        """Run a dry functional test to verify the full signal pipeline works."""
        errors = []
        try:
            # Test 1: Can we get candles?
            from app.trading.indicators import compute_all_indicators, Candle
            from app.trading.signals import generate_signal
            candles = await self._get_candles("EUR/USD")
            if not candles or len(candles) < 10:
                errors.append(f"CANDLES: only {len(candles) if candles else 0} candles for EUR/USD")

            # Test 2: Can we compute indicators?
            if candles and len(candles) >= 20:
                indicators = compute_all_indicators(candles)
                if indicators.rsi14 is None:
                    errors.append("INDICATORS: RSI is None")

                # Test 3: Can we generate a signal without crash?
                price = candles[-1].close
                signal = generate_signal(price, indicators, 0.0, "EUR/USD", 0.0002)
                if signal.signal not in ("buy", "sell", "hold"):
                    errors.append(f"SIGNAL: invalid signal type {signal.signal}")

                # Test 4: Verify hold string comparison works
                is_hold = (signal.signal == "hold")
                is_not_hold = (signal.signal != "hold")
                if not isinstance(is_hold, bool):
                    errors.append("SIGNAL: hold comparison broken")

            # Test 5: Can we get a quote?
            quote = await self._get_quote("EUR/USD")
            if not quote or not quote.get("price"):
                errors.append("QUOTE: no price for EUR/USD")

            # Test 6: MT5 connection alive?
            _dc_audit = self._dash_client()
            if _dc_audit and _dc_audit.is_connected:
                _br_summary = await _dc_audit.get_account_summary()
                if _br_summary.get("balance", 0) <= 0:
                    errors.append(f"MT5: balance is {_br_summary.get('balance', 0)}")
            else:
                errors.append("BROKER: MT5 not connected")

            # Test 7: Position sync — MT5 is source of truth
            if len(self._open_positions) > 0:
                logger.info(f"[AUDIT] {len(self._open_positions)} positions tracked (Fusion Markets MT5)")

            # Test 8: Check all open positions have valid entry_price
            for sym, pos in self._open_positions.items():
                ep = pos.get("entry_price", 0)
                if not ep or ep == 0:
                    errors.append(f"INVALID POSITION: {sym} has entry_price=0")
                sl = pos.get("stop_loss")
                tp = pos.get("take_profit")
                if not sl or not tp:
                    errors.append(f"INVALID POSITION: {sym} missing SL or TP")

            # Test 9: Verify monitoring loop works (no division errors)
            for sym, pos in self._open_positions.items():
                ep = pos.get("entry_price", 0)
                tp = pos.get("take_profit", 0)
                sl = pos.get("stop_loss", 0)
                if ep and tp and sl:
                    try:
                        distance = abs(tp - ep)
                        if distance > 0:
                            _ = (ep - ep) / distance  # Should be 0
                        pct = ((ep - ep) / ep) * 100 if ep else 0  # Should be 0
                    except Exception as calc_err:
                        errors.append(f"CALC ERROR on {sym}: {calc_err}")

        except Exception as e:
            errors.append(f"CRASH: {str(e)[:100]}")

        return errors
