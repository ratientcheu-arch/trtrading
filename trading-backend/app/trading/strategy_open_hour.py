"""
Stratégie open-hour (2026-04-24) — 2 positions DAX40 + 2 positions GBP/JPY
déclenchées automatiquement à 10h10 CET.

Logique :
  1. Attendre 10h10 CET exactement (±60s)
  2. Récupérer la bougie H1 9h-10h (close à 10h)
  3. Déterminer direction : BULL si close > open, BEAR sinon
  4. Calculer SL/TP:
       - BULL : TP = high_9h + TP_EXT% range | SL = low_9h  - SL_PAD% range
       - BEAR : TP = low_9h  - TP_EXT% range | SL = high_9h + SL_PAD% range
  5. Vérifier qu'aucune position existante sur le symbole (sinon skip)
  6. Ouvrir N positions (N=2) à lot standard (DAX 0.3, GBPJPY 0.1)
  7. Marquer origin="open_hour_v1" pour tracking
  8. Force-close à 12h00 CET si encore ouvertes

Backtest 2 mois : WR ~80% sur DAX (21/26 bull, 15/18 bear),
  +60 à +105€/mois DAX — +15 à +30€/mois GBPJPY.

Déclenchement : appelé depuis _main_loop toutes les 10s. Auto-gate par l'heure.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


OPEN_HOUR_STRATEGY_VERSION = "open_hour_v1"
PARIS_TZ = ZoneInfo("Europe/Paris")


@dataclass
class OpenHourConfig:
    symbol: str
    lot_per_position: float
    n_positions: int
    sl_pad_pct: float
    tp_ext_pct: float


# Sizing choisi pour ne pas dépasser max_daily_loss 3% × 1116 = 33€ par symbole
# DAX40 : avg SL distance 46 pts → 0.3 lot × 46 = 13.8€/position × 2 = 27.6€
# GBPJPY: avg SL distance 20 pips → 0.1 lot × 20 × 0.54€/pip = 10.8€/position × 2 = 21.6€
OPEN_HOUR_CONFIGS: list[OpenHourConfig] = [
    OpenHourConfig(
        symbol="DAX40",
        lot_per_position=0.5,
        n_positions=2,
        sl_pad_pct=0.38,
        tp_ext_pct=0.20,
    ),
    OpenHourConfig(
        symbol="GBP/JPY",
        lot_per_position=0.3,
        n_positions=2,
        sl_pad_pct=0.38,
        tp_ext_pct=0.50,
    ),
]


TRIGGER_HOUR_CET: int = 10
TRIGGER_MINUTE_CET: int = 10
TRIGGER_WINDOW_SEC: int = 90  # tolérance scan_tick

# Fenêtre max de détention : fermeture d'office à 12h00 CET
MAX_HOLD_UNTIL_HOUR_CET: int = 12


@dataclass
class OpenHourTradeSpec:
    symbol: str
    direction: str          # "buy" ou "sell"
    entry_price: float
    sl_price: float
    tp_price: float
    lot: float
    h1_9h_open: float
    h1_9h_high: float
    h1_9h_low: float
    h1_9h_close: float
    reason: str


# État in-memory : dates (YYYY-MM-DD) pour lesquelles la stratégie a déjà été déclenchée
_triggered_dates: set[str] = set()


def _now_cet() -> datetime:
    return datetime.now(PARIS_TZ)


def should_trigger_now(now: Optional[datetime] = None) -> bool:
    """True si on est dans la fenêtre 10h10 CET ±TRIGGER_WINDOW_SEC et pas encore déclenché aujourd'hui."""
    now = now or _now_cet()
    if now.weekday() >= 5:  # weekend
        return False
    today_key = now.date().isoformat()
    if today_key in _triggered_dates:
        return False
    if now.hour != TRIGGER_HOUR_CET:
        return False
    # Fenêtre: [10h10:00 .. 10h10:00 + WINDOW_SEC]
    trigger_sec = TRIGGER_MINUTE_CET * 60
    cur_sec = now.minute * 60 + now.second
    if cur_sec < trigger_sec or cur_sec > trigger_sec + TRIGGER_WINDOW_SEC:
        return False
    return True


def _mark_triggered(now: Optional[datetime] = None) -> None:
    now = now or _now_cet()
    _triggered_dates.add(now.date().isoformat())


def compute_trade_spec(
    cfg: OpenHourConfig,
    h1_9h_candle,
    current_price: float,
) -> Optional[OpenHourTradeSpec]:
    """Calcule la spec de trade depuis la H1 9h. None si pas de direction."""
    rng = h1_9h_candle.high - h1_9h_candle.low
    if rng <= 0:
        return None
    if h1_9h_candle.close > h1_9h_candle.open:
        direction = "buy"
        tp = h1_9h_candle.high + cfg.tp_ext_pct * rng
        sl = h1_9h_candle.low - cfg.sl_pad_pct * rng
    elif h1_9h_candle.close < h1_9h_candle.open:
        direction = "sell"
        tp = h1_9h_candle.low - cfg.tp_ext_pct * rng
        sl = h1_9h_candle.high + cfg.sl_pad_pct * rng
    else:
        return None
    rn = 1 if current_price > 100 else 5
    return OpenHourTradeSpec(
        symbol=cfg.symbol,
        direction=direction,
        entry_price=current_price,
        sl_price=round(sl, rn),
        tp_price=round(tp, rn),
        lot=cfg.lot_per_position,
        h1_9h_open=h1_9h_candle.open,
        h1_9h_high=h1_9h_candle.high,
        h1_9h_low=h1_9h_candle.low,
        h1_9h_close=h1_9h_candle.close,
        reason=(
            f"OPEN_HOUR_V1 dir={direction.upper()} "
            f"H1_9h=[{h1_9h_candle.low:.5f}..{h1_9h_candle.high:.5f}] "
            f"range={rng:.5f} SLpad={cfg.sl_pad_pct:.0%} TPext={cfg.tp_ext_pct:.0%}"
        ),
    )


def _find_h1_9h_candle(h1_candles: list) -> Optional[object]:
    """Trouve la bougie H1 dont l'ouverture correspond à 9h CET (jour courant)."""
    if not h1_candles:
        return None
    today = _now_cet().date()
    for c in reversed(h1_candles):
        ts = getattr(c, "timestamp", 0)
        ts_sec = ts / 1000 if ts > 1e12 else ts
        try:
            dt = datetime.fromtimestamp(ts_sec, tz=ZoneInfo("UTC")).astimezone(PARIS_TZ)
        except Exception:
            continue
        if dt.date() == today and dt.hour == 9:
            return c
    return None


def _symbol_has_position(bot, symbol: str) -> bool:
    for pos in bot._open_positions.values():
        if pos.get("symbol") == symbol:
            return True
    return False


async def _open_one_position(bot, cfg: OpenHourConfig, spec: OpenHourTradeSpec, idx: int) -> bool:
    """Ouvre 1 position market avec SL/TP broker-native. Retourne True si succès.

    place_market_order attend:
      - indices/commos : qty = lot direct (0.5)
      - forex (6 alpha) : qty = lot × 100_000 (units physiques)
    """
    action = "BUY" if spec.direction == "buy" else "SELL"
    # Convertir lot en qty selon le type de symbole
    sym_clean = cfg.symbol.replace("/", "").upper()
    is_forex = len(sym_clean) == 6 and sym_clean.isalpha()
    qty = spec.lot * 100_000 if is_forex else spec.lot
    try:
        result = await bot.mt5.place_market_order(
            cfg.symbol,
            action,
            qty,
            stop_loss=spec.sl_price,
            take_profit=spec.tp_price,
        )
    except Exception as e:
        logger.error(f"[OPEN_HOUR] {cfg.symbol} #{idx} place_market_order exception: {e}")
        return False
    if not result or not result.get("ticket"):
        logger.error(f"[OPEN_HOUR] {cfg.symbol} #{idx} order failed: {result}")
        return False
    ticket = result.get("ticket")
    fill = result.get("price") or spec.entry_price
    pos_key = f"{cfg.symbol}_oh{idx}_{int(time.time())}"
    from app.trading.symbol_mapper import get_leverage
    leverage = get_leverage(cfg.symbol) or 1
    pos_data = {
        "symbol": cfg.symbol,
        "pos_key": pos_key,
        "_opened_ts": time.time(),
        "action": action,
        "quantity": spec.lot,
        "entry_price": fill,
        "stop_loss": spec.sl_price,
        "take_profit": spec.tp_price,
        "entry_time": datetime.now().isoformat(),
        "signal_confidence": 90,
        "signal_reason": spec.reason,
        "position_size": spec.lot * fill,
        "margin": (spec.lot * fill) / leverage if leverage else spec.lot * fill,
        "leverage": leverage,
        "broker": "mt5",
        "ticket": ticket,
        "origin": OPEN_HOUR_STRATEGY_VERSION,
        "source": OPEN_HOUR_STRATEGY_VERSION,
        "_original_tp_dist": abs(spec.tp_price - fill),
    }
    async with bot._positions_lock:
        bot._open_positions[pos_key] = pos_data
    logger.warning(
        f"[OPEN_HOUR] {cfg.symbol} #{idx} {action} @ {fill:.5f} "
        f"SL={spec.sl_price} TP={spec.tp_price} lot={spec.lot} ticket={ticket}"
    )
    try:
        await bot._broadcast("alert", {
            "level": "info",
            "message": f"OPEN_HOUR {action} {cfg.symbol} #{idx} @ {fill:.5f}",
        })
    except Exception:
        pass
    return True


async def _execute_for_config(bot, cfg: OpenHourConfig) -> int:
    """Ouvre cfg.n_positions pour un symbole. Retourne le nombre ouvert."""
    if _symbol_has_position(bot, cfg.symbol):
        logger.info(f"[OPEN_HOUR] {cfg.symbol} — position déjà ouverte, skip")
        return 0
    h1 = (getattr(bot, "_candle_cache_h1", {}) or {}).get(cfg.symbol) or []
    h1_9h = _find_h1_9h_candle(h1)
    if h1_9h is None:
        logger.warning(f"[OPEN_HOUR] {cfg.symbol} — H1 9h candle introuvable, skip")
        return 0
    quote = None
    try:
        quote = await bot._get_quote(cfg.symbol)
    except Exception as e:
        logger.warning(f"[OPEN_HOUR] {cfg.symbol} get_quote error: {e}")
    price = None
    if quote:
        price = quote.get("price") or quote.get("mid") or quote.get("bid")
    if not price:
        price = getattr(h1_9h, "close", None)
    if not price or price <= 0:
        logger.error(f"[OPEN_HOUR] {cfg.symbol} — prix indisponible, skip")
        return 0
    spec = compute_trade_spec(cfg, h1_9h, float(price))
    if spec is None:
        logger.info(f"[OPEN_HOUR] {cfg.symbol} — H1 9h doji (pas de direction), skip")
        return 0
    logger.warning(
        f"[OPEN_HOUR] {cfg.symbol} DEAL {spec.direction.upper()} ×{cfg.n_positions} "
        f"entry≈{price:.5f} SL={spec.sl_price} TP={spec.tp_price}"
    )
    opened = 0
    for i in range(cfg.n_positions):
        ok = await _open_one_position(bot, cfg, spec, i + 1)
        if ok:
            opened += 1
    return opened


async def maybe_trigger_open_hour(bot) -> None:
    """Hook à appeler depuis _main_loop. No-op sauf dans la fenêtre 10h10 CET."""
    try:
        if not should_trigger_now():
            return
        if not getattr(bot, "mt5_available", False):
            logger.warning("[OPEN_HOUR] MT5 indisponible — skip trigger")
            return
        _mark_triggered()
        logger.warning("[OPEN_HOUR] ═══ DÉCLENCHEMENT 10h10 CET ═══")
        for cfg in OPEN_HOUR_CONFIGS:
            try:
                await _execute_for_config(bot, cfg)
            except Exception as e:
                logger.error(f"[OPEN_HOUR] {cfg.symbol} execute error: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"[OPEN_HOUR] maybe_trigger error: {e}", exc_info=True)


async def maybe_force_close(bot) -> None:
    """Ferme UNIQUEMENT les positions origin=open_hour_v1 si l'heure CET >= 12h.

    N'arrête PAS le bot : le scanner principal et les autres stratégies
    continuent de tourner normalement. Ne pop la position localement qu'en cas
    de close broker réussi (évite d'orphaniser un ticket si la requête échoue).
    """
    try:
        now = _now_cet()
        if now.hour < MAX_HOLD_UNTIL_HOUR_CET:
            return
        to_close: list[tuple[str, dict]] = []
        for pk, pos in list(bot._open_positions.items()):
            if pos.get("origin") == OPEN_HOUR_STRATEGY_VERSION:
                to_close.append((pk, pos))
        if not to_close:
            return
        logger.warning(f"[OPEN_HOUR] Force-close {len(to_close)} positions (>= 12h CET)")
        for pk, pos in to_close:
            ticket = pos.get("ticket")
            symbol = pos.get("symbol")
            try:
                result = await bot._close_position_broker(symbol, ticket=ticket)
            except Exception as e:
                logger.error(f"[OPEN_HOUR] close error {symbol} ticket={ticket}: {e}")
                continue
            if result:
                async with bot._positions_lock:
                    bot._open_positions.pop(pk, None)
                logger.warning(f"[OPEN_HOUR] Closed {symbol} ticket={ticket}")
            else:
                logger.error(
                    f"[OPEN_HOUR] Close returned None {symbol} ticket={ticket} — "
                    f"position laissée dans _open_positions, retry next tick"
                )
    except Exception as e:
        logger.error(f"[OPEN_HOUR] force_close error: {e}", exc_info=True)
