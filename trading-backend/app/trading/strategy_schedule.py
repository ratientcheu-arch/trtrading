"""
Scheduler unifié de stratégies horaires (2026-04-25).

5 triggers quotidiens :
  09:00 CET  →  H1 08:00-09:00  →  GOLD BULL            (hold jusqu'à 11:00)
  10:00 CET  →  H1 09:00-10:00  →  US30 BULL + DAX40 BULL (hold jusqu'à 12:00)
  15:30 CET  →  H1 14:30-15:30  →  NKY BULL + NASDAQ BULL + GBPUSD BULL (→ 17:30)
  16:30 CET  →  H1 15:30-16:30  →  NKY BULL              (→ 18:30)
  18:00 CET  →  H1 17:00-18:00  →  CAC40 BEAR + UK100 BEAR (→ 20:00)

Pour chaque trigger :
  1. Lire la bougie H1 de référence (agrégée depuis M5)
  2. Déterminer direction (BULL = close > open, BEAR = close < open)
  3. Si direction correspond à celle voulue par chaque symbole → ouvrir position
  4. SL/TP via Fibonacci (TP = high+range pour BUY, SL = low-range/2)
  5. Force-close à l'heure fin de window

Chaque position est marquée origin="schedule_v1:<trigger_label>" pour tracking.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

STRATEGY_VERSION = "schedule_v1"
PARIS_TZ = ZoneInfo("Europe/Paris")
TRIGGER_WINDOW_SEC = 120  # tolérance 2 min sur l'heure précise


@dataclass
class SymbolConfig:
    symbol: str           # ex "DAX40", "GOLD", "GBP/USD"
    direction: str        # "buy" ou "sell"
    lot: float            # lot size
    required_H1_dir: str  # "BULL" ou "BEAR" — direction H1 qui doit matcher


@dataclass
class Trigger:
    label: str
    trigger_h: int
    trigger_m: int
    # H1 de référence (ce qui se clôture À trigger_h:trigger_m)
    h1_start_h: int
    h1_start_m: int
    # Force close
    close_h: int
    close_m: int
    configs: list[SymbolConfig]


# Configuration des 5 triggers
TRIGGERS: list[Trigger] = [
    Trigger(
        label="09h-GOLD",
        trigger_h=9, trigger_m=0,
        h1_start_h=8, h1_start_m=0,
        close_h=12, close_m=0,   # 2026-04-25: hold 3h (9h-12h) au lieu de 2h
        configs=[
            SymbolConfig("GOLD", "buy", 0.3, "BULL"),
        ],
    ),
    Trigger(
        label="10h-EU",
        trigger_h=10, trigger_m=0,
        h1_start_h=9, h1_start_m=0,
        close_h=12, close_m=0,
        configs=[
            SymbolConfig("US30",  "buy", 0.3, "BULL"),
            SymbolConfig("DAX40", "buy", 0.3, "BULL"),
        ],
    ),
    Trigger(
        label="15h30-US",
        trigger_h=15, trigger_m=30,
        h1_start_h=14, h1_start_m=30,
        close_h=17, close_m=30,
        configs=[
            # NKY 100% WR 7j, +€467 @ 0.3 lot (top setup backtest)
            SymbolConfig("NKY",     "buy",  0.3, "BULL"),
            SymbolConfig("NASDAQ",  "buy",  0.3, "BULL"),   # 86% WR, +€94
            SymbolConfig("GBP/USD", "buy",  0.3, "BULL"),   # 83% WR, +€40
            # GOLD : volatile, les 2 directions rentables
            SymbolConfig("GOLD",    "buy",  0.1, "BULL"),   # 50% WR, +€47
            SymbolConfig("GOLD",    "sell", 0.1, "BEAR"),   # 33% WR, +€1709 move massif
        ],
    ),
    Trigger(
        label="16h30-NKY",
        trigger_h=16, trigger_m=30,
        h1_start_h=15, h1_start_m=30,
        close_h=18, close_m=30,
        configs=[
            SymbolConfig("NKY", "buy", 0.3, "BULL"),   # 73% WR, +€271 @ 0.3 lot
        ],
    ),
    Trigger(
        label="18h-BEAR",
        trigger_h=18, trigger_m=0,
        h1_start_h=17, h1_start_m=0,
        close_h=20, close_m=0,
        configs=[
            SymbolConfig("CAC40", "sell", 0.3, "BEAR"),
            SymbolConfig("UK100", "sell", 0.3, "BEAR"),
        ],
    ),
]


# État in-memory : triggers_fired[date][label] = True
_triggers_fired: dict[str, set[str]] = {}


def _now_cet() -> datetime:
    return datetime.now(PARIS_TZ)


def _is_weekend(now: Optional[datetime] = None) -> bool:
    now = now or _now_cet()
    return now.weekday() >= 5


def _should_trigger(trig: Trigger, now: Optional[datetime] = None) -> bool:
    now = now or _now_cet()
    if _is_weekend(now):
        return False
    today_key = now.date().isoformat()
    if today_key in _triggers_fired and trig.label in _triggers_fired[today_key]:
        return False
    if now.hour != trig.trigger_h:
        return False
    trigger_sec = trig.trigger_m * 60
    cur_sec = now.minute * 60 + now.second
    return trigger_sec <= cur_sec <= trigger_sec + TRIGGER_WINDOW_SEC


def _mark_fired(trig: Trigger, now: Optional[datetime] = None) -> None:
    now = now or _now_cet()
    today_key = now.date().isoformat()
    _triggers_fired.setdefault(today_key, set()).add(trig.label)


async def _fetch_h1_candle(bot, symbol: str, h1_start_h: int, h1_start_m: int,
                            h1_end_h: int, h1_end_m: int):
    """Agrège la bougie H1 custom depuis les M5 (fetch broker si cache vide)."""
    from datetime import timezone as _tz
    today = _now_cet().date()
    t_start = datetime.combine(today, dtime(h1_start_h, h1_start_m), tzinfo=PARIS_TZ)
    t_end = datetime.combine(today, dtime(h1_end_h, h1_end_m), tzinfo=PARIS_TZ)

    # Try cache first
    m5 = (getattr(bot, "_candle_cache_m5", {}) or {}).get(symbol) or []
    if not m5:
        # Fetch direct from broker
        try:
            m5 = await bot.mt5.get_historical_candles(symbol, duration="1 D", bar_size="5 mins")
        except Exception as e:
            logger.error(f"[SCHEDULE] {symbol} fetch M5 error: {e}")
            return None
    if not m5:
        return None

    bars_in = []
    for b in m5:
        ts = b.timestamp / 1000 if b.timestamp > 1e12 else b.timestamp
        try:
            dt = datetime.fromtimestamp(ts, tz=_tz.utc).astimezone(PARIS_TZ)
        except Exception:
            continue
        if t_start <= dt < t_end:
            bars_in.append((dt, b))
    if len(bars_in) < 8:
        return None
    bars_in.sort(key=lambda x: x[0])
    o = bars_in[0][1].open
    c = bars_in[-1][1].close
    h = max(b[1].high for b in bars_in)
    l = min(b[1].low for b in bars_in)
    return (o, h, l, c)


def _symbol_has_position(bot, symbol: str) -> bool:
    for pos in bot._open_positions.values():
        if pos.get("symbol") == symbol:
            return True
    # Also check broker via cc_positions cache
    cc = getattr(bot, "_cached_cc_positions", None) or []
    for p in cc:
        if str(p.get("symbol", "")).upper() == symbol.upper():
            return True
    return False


async def _open_one(bot, trig: Trigger, cfg: SymbolConfig,
                     h1_high: float, h1_low: float, current_price: float) -> bool:
    """Ouvre 1 position avec SL/TP Fibonacci."""
    action = "BUY" if cfg.direction == "buy" else "SELL"
    sym_clean = cfg.symbol.replace("/", "").upper()
    is_forex = len(sym_clean) == 6 and sym_clean.isalpha()
    is_jpy = is_forex and "JPY" in sym_clean
    qty = cfg.lot * 100_000 if is_forex else cfg.lot

    # Rounding
    if is_jpy:
        r_n = 3
    elif is_forex:
        r_n = 5
    else:
        r_n = 1

    h1_range = h1_high - h1_low
    half_range = h1_range / 2
    if h1_range <= 0:
        logger.warning(f"[SCHEDULE:{trig.label}] {cfg.symbol}: H1 range <= 0, skip")
        return False

    if cfg.direction == "buy":
        tp_price = round(h1_high + h1_range, r_n)
        sl_price = round(h1_low - half_range, r_n)
    else:
        tp_price = round(h1_low - h1_range, r_n)
        sl_price = round(h1_high + half_range, r_n)

    # Safety 1 : SL côté cohérent
    if cfg.direction == "buy" and sl_price >= current_price:
        logger.warning(
            f"[SCHEDULE:{trig.label}] {cfg.symbol} BUY skip: SL {sl_price} >= prix {current_price}"
        )
        return False
    if cfg.direction == "sell" and sl_price <= current_price:
        logger.warning(
            f"[SCHEDULE:{trig.label}] {cfg.symbol} SELL skip: SL {sl_price} <= prix {current_price}"
        )
        return False

    # Safety 2 : TP côté cohérent
    if cfg.direction == "buy" and tp_price <= current_price:
        logger.warning(
            f"[SCHEDULE:{trig.label}] {cfg.symbol} BUY skip: TP {tp_price} <= prix {current_price}"
        )
        return False
    if cfg.direction == "sell" and tp_price >= current_price:
        logger.warning(
            f"[SCHEDULE:{trig.label}] {cfg.symbol} SELL skip: TP {tp_price} >= prix {current_price}"
        )
        return False

    risk_dist = abs(current_price - sl_price)
    reward_dist = abs(tp_price - current_price)
    rr = reward_dist / risk_dist if risk_dist > 0 else 0
    logger.info(
        f"[SCHEDULE:{trig.label}] {cfg.symbol} {action} levels: "
        f"entry={current_price} SL={sl_price} TP={tp_price} R:R 1:{rr:.2f}"
    )

    try:
        result = await bot.mt5.place_market_order(
            cfg.symbol, action, qty,
            stop_loss=sl_price, take_profit=tp_price,
        )
    except Exception as e:
        logger.error(f"[SCHEDULE:{trig.label}] {cfg.symbol} place_market_order error: {e}")
        return False
    if not result or not result.get("ticket"):
        logger.error(f"[SCHEDULE:{trig.label}] {cfg.symbol} order failed: {result}")
        return False

    ticket = result.get("ticket")
    fill = result.get("price") or current_price
    logger.warning(
        f"[SCHEDULE:{trig.label}] {cfg.symbol} {action} OPENED @ {fill} "
        f"SL={sl_price} TP={tp_price} lot={cfg.lot} ticket={ticket}"
    )

    # Register position in bot._open_positions with origin tag for force-close tracking
    try:
        from app.trading.symbol_mapper import get_leverage
        leverage = get_leverage(cfg.symbol) or 1
        pos_key = f"{cfg.symbol}_sched_{trig.label}_{int(_time.time())}"
        pos_data = {
            "symbol": cfg.symbol,
            "pos_key": pos_key,
            "_opened_ts": _time.time(),
            "action": action,
            "quantity": cfg.lot if not is_forex else cfg.lot * 100_000,
            "entry_price": fill,
            "stop_loss": sl_price,
            "take_profit": tp_price,
            "entry_time": datetime.now().isoformat(),
            "signal_confidence": 90,
            "signal_reason": f"SCHEDULE {trig.label} H1 {trig.h1_start_h:02d}h{trig.h1_start_m:02d}",
            "position_size": cfg.lot * fill,
            "margin": (cfg.lot * fill) / leverage if leverage else cfg.lot * fill,
            "leverage": leverage,
            "broker": "mt5",
            "ticket": ticket,
            "origin": f"{STRATEGY_VERSION}:{trig.label}",
            "source": f"{STRATEGY_VERSION}:{trig.label}",
        }
        async with bot._positions_lock:
            bot._open_positions[pos_key] = pos_data
    except Exception as e:
        logger.error(f"[SCHEDULE:{trig.label}] {cfg.symbol} register internal: {e}")

    try:
        await bot._broadcast("alert", {
            "level": "info",
            "message": f"SCHEDULE {trig.label} — {action} {cfg.symbol} @ {fill}",
        })
    except Exception:
        pass
    return True


async def _execute_trigger(bot, trig: Trigger) -> None:
    """Exécute un trigger : lit H1, ouvre positions qui matchent la direction requise."""
    logger.warning(f"[SCHEDULE:{trig.label}] ═══ TRIGGER {trig.trigger_h:02d}h{trig.trigger_m:02d} CET ═══")

    for cfg in trig.configs:
        try:
            if _symbol_has_position(bot, cfg.symbol):
                logger.info(f"[SCHEDULE:{trig.label}] {cfg.symbol}: position déjà ouverte, skip")
                continue

            # Fetch H1 candle for this specific symbol
            h1 = await _fetch_h1_candle(
                bot, cfg.symbol,
                trig.h1_start_h, trig.h1_start_m,
                trig.trigger_h, trig.trigger_m,
            )
            if not h1:
                logger.error(f"[SCHEDULE:{trig.label}] {cfg.symbol}: H1 introuvable, skip")
                continue
            o, h, l, c = h1
            direction = "BULL" if c > o else "BEAR" if c < o else "DOJI"
            logger.info(
                f"[SCHEDULE:{trig.label}] {cfg.symbol} H1: O={o} H={h} L={l} C={c} → {direction}"
            )

            if direction != cfg.required_H1_dir:
                logger.info(
                    f"[SCHEDULE:{trig.label}] {cfg.symbol}: H1={direction} ≠ requis={cfg.required_H1_dir}, skip"
                )
                continue

            # Current quote
            quote = None
            try:
                quote = await bot._get_quote(cfg.symbol)
            except Exception as e:
                logger.warning(f"[SCHEDULE:{trig.label}] {cfg.symbol} get_quote error: {e}")
            price = None
            if quote:
                price = quote.get("price") or quote.get("ask") or quote.get("bid")
            if not price:
                price = c
            if not price or price <= 0:
                logger.error(f"[SCHEDULE:{trig.label}] {cfg.symbol}: prix indisponible")
                continue

            await _open_one(bot, trig, cfg, h, l, float(price))
        except Exception as e:
            logger.error(f"[SCHEDULE:{trig.label}] {cfg.symbol} error: {e}", exc_info=True)


async def maybe_trigger_schedule(bot) -> None:
    """Hook _main_loop — vérifie tous les triggers et exécute ceux dans leur fenêtre."""
    try:
        if not getattr(bot, "mt5_available", False):
            return
        for trig in TRIGGERS:
            if _should_trigger(trig):
                _mark_fired(trig)
                await _execute_trigger(bot, trig)
    except Exception as e:
        logger.error(f"[SCHEDULE] maybe_trigger error: {e}", exc_info=True)


async def maybe_force_close(bot) -> None:
    """Ferme chaque position schedule_v1 à son heure de close_h:close_m."""
    try:
        now = _now_cet()
        to_close = []
        for pk, pos in list(bot._open_positions.items()):
            origin = pos.get("origin") or ""
            if not origin.startswith(STRATEGY_VERSION):
                continue
            # Parse label from origin: "schedule_v1:<label>"
            parts = origin.split(":", 1)
            if len(parts) < 2:
                continue
            label = parts[1]
            trig = next((t for t in TRIGGERS if t.label == label), None)
            if not trig:
                continue
            # Est-ce l'heure de close ?
            if now.hour > trig.close_h or (now.hour == trig.close_h and now.minute >= trig.close_m):
                to_close.append((pk, pos))
        if not to_close:
            return
        logger.warning(f"[SCHEDULE] Force-close {len(to_close)} positions")
        for pk, pos in to_close:
            ticket = pos.get("ticket")
            symbol = pos.get("symbol")
            try:
                r = await bot._close_position_broker(symbol, ticket=ticket)
                if r:
                    async with bot._positions_lock:
                        bot._open_positions.pop(pk, None)
                    logger.warning(f"[SCHEDULE] Closed {symbol} ticket={ticket}")
            except Exception as e:
                logger.error(f"[SCHEDULE] close error {symbol}: {e}")
    except Exception as e:
        logger.error(f"[SCHEDULE] force_close error: {e}", exc_info=True)
