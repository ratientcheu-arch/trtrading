"""
Stratégie US-hour (2026-04-24) — déclenchement automatique à 15h30 CEST
IMMÉDIATEMENT après clôture de la bougie H1 14h30-15h30.

Simulation Fibonacci SL/TP réelle (60j BULL) :
  - DOW 30   100% WR (5/5)  → +€252 /10 trades @ 0.3 lot   ← MEILLEUR
  - NASDAQ   75%  WR (3/4)  → +€59  /10 trades @ 0.3 lot
  (DAX40 50% WR en sim → retiré. S&P 500 breakeven → retiré.)

Méthode SL/TP Fibonacci (per user 2026-04-24) :
  - Référence = bougie H1 14h30-15h30
  - TP (BUY)  = H1_high + H1_range        (2 × half_range au-dessus du high)
  - SL (BUY)  = H1_low  - H1_range / 2    (half_range sous le low)

Logique:
  1. Trigger à 15h30 CEST ±90s
  2. Agréger M5 du jour 14h30-15h30 en H1 custom (direction)
  3. Si BULL → BUY DAX40 + NAS100 + US500 avec SL/TP Fibonacci
  4. Si BEAR → no-op
  5. Force-close à 17h30 CEST
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

US_HOUR_STRATEGY_VERSION = "us_hour_v1"
PARIS_TZ = ZoneInfo("Europe/Paris")

# Trigger à 15h30 CEST (Paris) — immédiatement après clôture H1 14h30-15h30
TRIGGER_HOUR_CET = 15
TRIGGER_MINUTE_CET = 30
TRIGGER_WINDOW_SEC = 120   # 2 min tolérance
MAX_HOLD_UNTIL_HOUR_CET = 17   # close à 17h30 CEST (fin fenêtre backtest)
MAX_HOLD_UNTIL_MINUTE_CET = 30

# Seul un déclenchement par jour
_triggered_dates: set[str] = set()


@dataclass
class USHourConfig:
    symbol: str
    direction: str       # "buy" ou "sell" (locké par config, pas dynamique)
    lot: float
    sl_buffer: float     # distance SL au-delà high (sell) ou low (buy) en unités de prix
    tp_points: float     # distance TP en unités de prix (moyenne MFE/2 backtest)


# Configs BULL — simulation Fibonacci réelle sur 60j (2026-04-24) :
#   DOW 30 100% WR (5/5) → +€252 /10 trades  ← meilleur edge
#   NASDAQ 75% WR (3/4)  → +€59  /10 trades
#   DAX40 et S&P retirés : DAX SL trop chopé (50% WR sim),
#   S&P range trop petit (breakeven).
US_CONFIGS_BULL: list[USHourConfig] = [
    USHourConfig(symbol="US30",   direction="buy", lot=0.3, sl_buffer=0.0, tp_points=0.0),   # SL/TP via Fibonacci
    USHourConfig(symbol="NAS100", direction="buy", lot=0.3, sl_buffer=0.0, tp_points=0.0),
]

# Configs BEAR — DÉSACTIVÉ (edge trop faible ~+3€/trade même à 78% WR)
US_CONFIGS_BEAR: list[USHourConfig] = []


def _now_cet() -> datetime:
    return datetime.now(PARIS_TZ)


def should_trigger_now(now: Optional[datetime] = None) -> bool:
    now = now or _now_cet()
    if now.weekday() >= 5:
        return False
    today_key = now.date().isoformat()
    if today_key in _triggered_dates:
        return False
    if now.hour != TRIGGER_HOUR_CET:
        return False
    trigger_sec = TRIGGER_MINUTE_CET * 60
    cur_sec = now.minute * 60 + now.second
    return trigger_sec <= cur_sec <= trigger_sec + TRIGGER_WINDOW_SEC


def _mark_triggered(now: Optional[datetime] = None) -> None:
    now = now or _now_cet()
    _triggered_dates.add(now.date().isoformat())


def _aggregate_h1_from_m5(m5_candles: list, today_date) -> Optional[tuple]:
    """Construit la bougie 'H1 custom 14h30-15h30' depuis les M5 du jour.
       Se clôture À 15h30 Paris, direction connue dès 15h30.
       Retourne (open, high, low, close) ou None."""
    from datetime import timezone as _tz
    t_start = datetime.combine(today_date, dtime(14, 30), tzinfo=PARIS_TZ)
    t_end   = datetime.combine(today_date, dtime(15, 30), tzinfo=PARIS_TZ)
    bars_in = []
    for b in m5_candles:
        ts = b.timestamp / 1000 if b.timestamp > 1e12 else b.timestamp
        dt = datetime.fromtimestamp(ts, tz=_tz.utc).astimezone(PARIS_TZ)
        if t_start <= dt < t_end:
            bars_in.append((dt, b))
    if len(bars_in) < 8:   # besoin d'au moins 8 M5 sur 12 possibles
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
    return False


async def _open_one(bot, cfg: USHourConfig, h1_high: float, h1_low: float, current_price: float) -> bool:
    """SL/TP calculés via Fibonacci sur la bougie H1 14h30-15h30 (user 2026-04-24) :
        BUY  : TP = H1_high + H1_range        (+2×half_range au-dessus du high)
               SL = H1_low  - H1_range / 2    (-half_range sous le low)
        SELL : TP = H1_low  - H1_range
               SL = H1_high + H1_range / 2
    """
    action = "BUY" if cfg.direction == "buy" else "SELL"
    sym_clean = cfg.symbol.replace("/", "").upper()
    is_forex = len(sym_clean) == 6 and sym_clean.isalpha()
    is_jpy = is_forex and "JPY" in sym_clean
    qty = cfg.lot * 100_000 if is_forex else cfg.lot

    # Rounding par type d'instrument
    if is_jpy:
        r_n = 3
    elif is_forex:
        r_n = 5
    else:
        r_n = 1

    h1_range = h1_high - h1_low
    half_range = h1_range / 2

    if cfg.direction == "buy":
        tp_price = round(h1_high + h1_range, r_n)    # +2×half_range au-dessus high
        sl_price = round(h1_low - half_range, r_n)    # −half_range sous low
    else:
        tp_price = round(h1_low - h1_range, r_n)
        sl_price = round(h1_high + half_range, r_n)

    # Safety 1: SL must be < current for BUY, > current for SELL
    if cfg.direction == "buy" and sl_price >= current_price:
        logger.warning(
            f"[US_HOUR] {cfg.symbol} BUY skip: SL {sl_price} >= current {current_price}"
        )
        return False
    if cfg.direction == "sell" and sl_price <= current_price:
        logger.warning(
            f"[US_HOUR] {cfg.symbol} SELL skip: SL {sl_price} <= current {current_price}"
        )
        return False

    # Safety 2: TP must be > current for BUY, < current for SELL
    if cfg.direction == "buy" and tp_price <= current_price:
        logger.warning(
            f"[US_HOUR] {cfg.symbol} BUY skip: TP {tp_price} <= current {current_price} "
            f"(prix a déjà dépassé l'extension Fibonacci — rally hors H1)"
        )
        return False
    if cfg.direction == "sell" and tp_price >= current_price:
        logger.warning(
            f"[US_HOUR] {cfg.symbol} SELL skip: TP {tp_price} >= current {current_price}"
        )
        return False

    # Note: pas de contrainte R:R minimum — user a validé 2026-04-24 que
    # si H1 14h30-15h30 est BULL, on prend le BUY avec les levels Fibonacci
    # tels quels, quel que soit le R:R.
    risk_dist = abs(current_price - sl_price)
    reward_dist = abs(tp_price - current_price)
    rr = reward_dist / risk_dist if risk_dist > 0 else 0
    logger.info(f"[US_HOUR] {cfg.symbol} {action} TP {reward_dist:.1f}p SL {risk_dist:.1f}p R:R 1:{rr:.2f}")

    try:
        result = await bot.mt5.place_market_order(
            cfg.symbol, action, qty, stop_loss=sl_price, take_profit=tp_price,
        )
    except Exception as e:
        logger.error(f"[US_HOUR] {cfg.symbol} exception: {e}")
        return False

    if not result or not result.get("ticket"):
        logger.error(f"[US_HOUR] {cfg.symbol} order failed: {result}")
        return False

    ticket = result.get("ticket")
    fill = result.get("price") or current_price
    logger.warning(
        f"[US_HOUR] {cfg.symbol} {action} @ {fill} "
        f"SL={sl_price} TP={tp_price} lot={cfg.lot} ticket={ticket}"
    )
    try:
        await bot._broadcast("alert", {
            "level": "info",
            "message": f"US_HOUR {action} {cfg.symbol} @ {fill}",
        })
    except Exception:
        pass
    return True


async def maybe_trigger_us_hour(bot) -> None:
    try:
        if not should_trigger_now():
            return
        if not getattr(bot, "mt5_available", False):
            logger.warning("[US_HOUR] MT5 indisponible — skip")
            return
        _mark_triggered()
        logger.warning("[US_HOUR] ═══ DÉCLENCHEMENT 15h30 CEST (Paris) ═══")

        # Récupérer l'H1 15h30-16h30 via les M5 cache du DAX (référence direction marché EU/US)
        m5_cache = getattr(bot, "_candle_cache_m5", {}) or {}
        dax_m5 = m5_cache.get("DAX40") or []
        if not dax_m5:
            logger.error("[US_HOUR] Pas de cache M5 DAX40 — impossible de déterminer direction, SKIP")
            return
        today = _now_cet().date()
        h1 = _aggregate_h1_from_m5(dax_m5, today)
        if not h1:
            logger.error("[US_HOUR] Impossible d'agréger H1 15h30-16h30 DAX, SKIP")
            return
        o, h_, l_, c = h1
        if c == o:
            logger.info("[US_HOUR] H1 15h30 DAX = DOJI, skip")
            return
        direction = "BULL" if c > o else "BEAR"
        logger.warning(
            f"[US_HOUR] H1 14h30-15h30 DAX: O={o} H={h_} L={l_} C={c} → direction {direction}"
        )

        configs = US_CONFIGS_BULL if direction == "BULL" else US_CONFIGS_BEAR
        if not configs:
            logger.warning(
                f"[US_HOUR] Direction={direction} mais aucune config active — skip "
                f"(BEAR désactivé car edge trop faible)"
            )
            return
        for cfg in configs:
            try:
                if _symbol_has_position(bot, cfg.symbol):
                    logger.info(f"[US_HOUR] {cfg.symbol} position déjà ouverte, skip")
                    continue
                # Récupère la H1 14h30-15h30 propre à ce symbole pour SL/TP
                sym_m5 = m5_cache.get(cfg.symbol) or []
                sym_h1 = _aggregate_h1_from_m5(sym_m5, today) if sym_m5 else None
                # Fix 2026-04-24: si cache vide (US30/NAS100 pas scanné),
                # fetch M5 direct broker au lieu de fallback DAX levels.
                if not sym_h1:
                    logger.info(f"[US_HOUR] {cfg.symbol} M5 pas en cache — fetch direct broker")
                    try:
                        sym_m5_live = await bot.mt5.get_historical_candles(
                            cfg.symbol, duration="1 D", bar_size="5 mins"
                        )
                        if sym_m5_live:
                            sym_h1 = _aggregate_h1_from_m5(sym_m5_live, today)
                    except Exception as _fetch_err:
                        logger.error(f"[US_HOUR] {cfg.symbol} fetch M5 error: {_fetch_err}")
                if sym_h1:
                    so, sh, sl_, sc = sym_h1
                else:
                    logger.error(
                        f"[US_HOUR] {cfg.symbol} pas de H1 14h30-15h30 disponible — SKIP "
                        f"(plutôt que fallback dangereux sur levels DAX)"
                    )
                    continue
                quote = None
                try:
                    quote = await bot._get_quote(cfg.symbol)
                except Exception:
                    pass
                price = (quote.get("price") or quote.get("ask") or quote.get("bid")) if quote else sc if sym_h1 else c
                if not price or price <= 0:
                    logger.error(f"[US_HOUR] {cfg.symbol} prix indisponible")
                    continue
                await _open_one(bot, cfg, sh, sl_, float(price))
            except Exception as e:
                logger.error(f"[US_HOUR] {cfg.symbol} error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"[US_HOUR] maybe_trigger error: {e}", exc_info=True)


async def maybe_force_close(bot) -> None:
    """Ferme les positions us_hour_v1 à 17h30 CEST (fin fenêtre backtest)."""
    try:
        now = _now_cet()
        # Fermer si heure > 17 OU (heure == 17 ET minute >= 30)
        if now.hour < MAX_HOLD_UNTIL_HOUR_CET:
            return
        if now.hour == MAX_HOLD_UNTIL_HOUR_CET and now.minute < MAX_HOLD_UNTIL_MINUTE_CET:
            return
        to_close = []
        for pk, pos in list(bot._open_positions.items()):
            if pos.get("origin") == US_HOUR_STRATEGY_VERSION:
                to_close.append((pk, pos))
        if not to_close:
            return
        logger.warning(f"[US_HOUR] Force-close {len(to_close)} positions (>= 18h CET)")
        for pk, pos in to_close:
            ticket = pos.get("ticket")
            symbol = pos.get("symbol")
            try:
                r = await bot._close_position_broker(symbol, ticket=ticket)
                if r:
                    async with bot._positions_lock:
                        bot._open_positions.pop(pk, None)
                    logger.warning(f"[US_HOUR] Closed {symbol} ticket {ticket}")
            except Exception as e:
                logger.error(f"[US_HOUR] close error {symbol}: {e}")
    except Exception as e:
        logger.error(f"[US_HOUR] force_close error: {e}", exc_info=True)
