"""
REST API endpoints for the trading dashboard.
Two-process architecture: all bot commands go through IPC.
Read-only data comes from shared files and PostgreSQL.
"""
import asyncio
import time
import json
from datetime import datetime, timezone, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Header, WebSocket, WebSocketDisconnect
from typing import Optional

from app.config import settings
from app.schemas.trade import (
    ManualOrderRequest, ProjectionOrderRequest, ClosePositionRequest, BotConfigUpdate,
    AccountResponse, HealthResponse,
    ExpectOperatorOrderRequest, ReclassifyPositionRequest, ModifySLRequest,
)
from app.api.websocket import ws_manager
from app.utils.logging import get_logger
from app.database import async_session
from app.models.trade import DailyPerformance
from app.ipc import (
    read_json, send_command,
    STATUS_FILE, ACCOUNT_FILE, POSITIONS_FILE, SIGNALS_FILE,
    CONFIG_FILE, DAILY_FILE, TRADES_FILE, OPERATOR_FILE,
)
from sqlalchemy import select

logger = get_logger(__name__)
router = APIRouter(prefix="/api")

_start_time = time.time()


def verify_api_key(authorization: Optional[str] = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="API key required")
    token = authorization.replace("Bearer ", "")
    if token != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid API key")


# ── Health ────────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse)
async def health():
    cached = read_json(STATUS_FILE)
    if cached:
        return HealthResponse(
            status="ok",
            bot_running=cached.get("running", False),
            uptime_seconds=round(time.time() - _start_time, 1),
            mt5_connected=cached.get("mt5_connected", False),
        )
    return HealthResponse(
        status="ok",
        bot_running=False,
        uptime_seconds=round(time.time() - _start_time, 1),
        mt5_connected=False,
    )


# ── Account ───────────────────────────────────────────────────────────────

@router.get("/account", dependencies=[Depends(verify_api_key)])
async def get_account():
    """Account summary — reads from bot cache file (NEVER calls MT5 directly)."""
    cached = read_json(ACCOUNT_FILE)
    if cached:
        return cached
    return {
        "balance": 0, "net_liquidation": 0, "unrealized_pnl": 0,
        "buying_power": 0, "daily_pnl": 0,
        "deposit": round(settings.starting_capital, 2),
        "profit_loss": 0, "total_pnl": 0,
        "currency": "EUR", "capital": 0,
        "primary_broker": "Fusion Markets MT5",
    }


@router.post("/account/sync-balance", dependencies=[Depends(verify_api_key)])
async def sync_balance(data: dict):
    """Manually set the real MT5 balance."""
    real_balance = data.get("balance", 0)
    if real_balance <= 0:
        return {"success": False, "error": "Balance must be > 0"}
    result = await send_command("sync_balance", {"balance": real_balance})
    return result.get("data", result)


# ── Positions ─────────────────────────────────────────────────────────────

@router.get("/positions", dependencies=[Depends(verify_api_key)])
async def get_positions():
    return read_json(POSITIONS_FILE, [])


@router.get("/positions/live", dependencies=[Depends(verify_api_key)])
async def get_positions_live():
    """Positions LIVE — reads from bot cache file + adds hold_seconds (real age UTC).

    2026-04-21: ajout de hold_seconds pour fix bug dashboard qui affichait +2h
    systématique à cause d'un parsing naïf du timestamp ISO entry_time.
    Le frontend peut désormais afficher directement hold_seconds sans conversion.
    """
    positions = read_json(POSITIONS_FILE, [])
    if not isinstance(positions, list):
        return positions
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    for p in positions:
        try:
            et_str = p.get("entry_time", "")
            if et_str:
                # Parse ISO8601 with timezone support
                et = datetime.fromisoformat(et_str)
                if et.tzinfo is None:
                    et = et.replace(tzinfo=timezone.utc)
                hold_sec = int((now_utc - et).total_seconds())
                p["hold_seconds"] = max(hold_sec, 0)
                p["hold_str"] = _format_hold(hold_sec)
        except Exception:
            p["hold_seconds"] = 0
            p["hold_str"] = "0s"
    return positions


def _format_hold(sec: int) -> str:
    """Format hold duration : 45s, 5min, 1h23min, 3h05min."""
    if sec < 60:
        return f"{sec}s"
    m = sec // 60
    if m < 60:
        return f"{m}min"
    h = m // 60
    mm = m % 60
    return f"{h}h{mm:02d}min"


# ── Orders ────────────────────────────────────────────────────────────────

@router.post("/orders", dependencies=[Depends(verify_api_key)])
async def place_order(req: ManualOrderRequest):
    result = await send_command("manual_order", {
        "symbol": req.symbol,
        "action": req.action,
        "amount_eur": req.amount_eur,
    }, timeout=20)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Order failed"))
    return result.get("data", result)


# ── Projection Orders (manual TP, auto SL/lot sizing) ────────────────────

@router.post("/projection-order", dependencies=[Depends(verify_api_key)])
async def projection_order(req: ProjectionOrderRequest):
    result = await send_command("projection_order", {
        "symbol": req.symbol,
        "action": req.action,
        "tp_price": req.tp_price,
    }, timeout=25)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Projection order failed"))
    return result.get("data", result)


# ── Trades ────────────────────────────────────────────────────────────────

@router.get("/trades", dependencies=[Depends(verify_api_key)])
async def get_trades():
    return read_json(TRADES_FILE, [])


@router.get("/trades/daily", dependencies=[Depends(verify_api_key)])
async def get_daily_summary():
    """Daily P&L — from bot IPC file, with DB fallback."""
    cached = read_json(DAILY_FILE)
    if cached and cached.get("trades", 0) > 0 or cached and cached.get("pnl", 0) != 0:
        return cached

    # DB fallback
    today = date.today().isoformat()
    try:
        async with async_session() as session:
            result = await session.execute(
                select(DailyPerformance).where(DailyPerformance.date == today)
            )
            perf = result.scalar_one_or_none()
        if perf:
            return {
                "pnl": perf.pnl, "theoretical_pnl": perf.pnl,
                "trades": perf.trades_count, "wins": perf.wins,
                "losses": perf.losses, "win_rate": perf.win_rate,
            }
    except Exception as e:
        logger.error(f"trades/daily DB error: {e}")
    return {"pnl": 0, "theoretical_pnl": 0, "trades": 0, "wins": 0, "losses": 0, "win_rate": 0}


# ── Market Data ───────────────────────────────────────────────────────────

@router.get("/market/scan", dependencies=[Depends(verify_api_key)])
async def market_scan():
    return read_json(SIGNALS_FILE, [])


@router.get("/market/{symbol}", dependencies=[Depends(verify_api_key)])
async def get_market_data(symbol: str):
    """Get real-time quote for a symbol via bot process."""
    result = await send_command("get_market_quote", {"symbol": symbol}, timeout=10)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Quote unavailable"))
    return result.get("data", {})


# ── Bot Control ───────────────────────────────────────────────────────────

@router.get("/bot/status", dependencies=[Depends(verify_api_key)])
async def bot_status():
    cached = read_json(STATUS_FILE)
    if cached:
        return cached
    return {"running": False, "mt5_connected": False}


@router.post("/bot/start", dependencies=[Depends(verify_api_key)])
async def bot_start():
    result = await send_command("start", timeout=15)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed to start bot"))
    return {"success": True, "status": result.get("data", {}).get("status", {})}


@router.post("/bot/scan-only", dependencies=[Depends(verify_api_key)])
async def bot_scan_only():
    """Start bot in SCAN ONLY mode."""
    result = await send_command("scan_only", timeout=15)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed to start bot"))
    return {"success": True, "mode": "scan_only", "status": result.get("data", {}).get("status", {})}


@router.post("/bot/set-mode", dependencies=[Depends(verify_api_key)])
async def bot_set_mode(mode: str = "auto"):
    """Switch bot mode without restart."""
    result = await send_command("set_mode", {"mode": mode})
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to set mode"))
    return {"success": True, "scan_only": result.get("data", {}).get("scan_only"), "status": result.get("data", {}).get("status", {})}


@router.post("/bot/stop", dependencies=[Depends(verify_api_key)])
async def bot_stop(close_positions: bool = False):
    # Longer timeout when closing positions — subprocess takes up to ~18s
    _timeout = 25 if close_positions else 10
    result = await send_command("stop", {"close_positions": close_positions}, timeout=_timeout)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed to stop bot"))
    data = result.get("data", {})
    return {
        "success": True,
        "status": data.get("status", {}),
        "closed": data.get("closed", 0),
        "total": data.get("total", 0),
        "pnl_total": data.get("pnl_total", 0),
    }


@router.post("/bot/emergency-stop", dependencies=[Depends(verify_api_key)])
async def bot_emergency_stop():
    # Emergency stop closes all positions via subprocess — allow up to 25s
    result = await send_command("emergency_stop", timeout=25)
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed"))
    data = result.get("data", {})
    return {
        "success": True,
        "message": "Emergency stop executed",
        "closed": data.get("closed", 0),
        "total": data.get("total", 0),
        "pnl_total": data.get("pnl_total", 0),
    }


@router.get("/bot/config", dependencies=[Depends(verify_api_key)])
async def get_bot_config():
    cached = read_json(CONFIG_FILE)
    if cached:
        return cached
    return {}


@router.put("/bot/config", dependencies=[Depends(verify_api_key)])
async def update_bot_config(config: BotConfigUpdate):
    result = await send_command("update_config", config.model_dump(exclude_none=True))
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed to update config"))
    return {"success": True, "config": result.get("data", {}).get("config", {})}


@router.post("/positions/close", dependencies=[Depends(verify_api_key)])
async def close_position(req: ClosePositionRequest):
    # First try bot's manual_close (it tracks positions internally)
    # Long timeout: event loop may be busy with scan/trade execution
    result = await send_command("manual_close", {"symbol": req.symbol}, timeout=90)
    if result.get("success"):
        return result.get("data", result)

    # Fallback: close directly on MT5
    result2 = await send_command("close_position_direct", {"symbol": req.symbol}, timeout=90)
    if result2.get("success"):
        return result2.get("data", result2)
    return result.get("data", result)


@router.post("/positions/{ticket}/modify_sl", dependencies=[Depends(verify_api_key)])
async def modify_position_sl(ticket: int, req: ModifySLRequest):
    """Modify Stop Loss on an open position (synchronous broker call)."""
    if req.new_sl <= 0:
        raise HTTPException(status_code=400, detail="new_sl must be > 0")
    result = await send_command("modify_sl", {
        "ticket": ticket,
        "new_sl": req.new_sl,
    }, timeout=15)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to modify SL"))
    logger.info(f"[AUDIT] modify_sl ticket={ticket} new_sl={req.new_sl} result=OK")
    return {"success": True, "ticket": ticket, "new_sl": req.new_sl}


# ── Operator Order Management ──────────────────────────────────────────

@router.post("/operator/expect", dependencies=[Depends(verify_api_key)])
async def register_operator_order(req: ExpectOperatorOrderRequest):
    result = await send_command("register_operator", {
        "symbol": req.symbol, "side": req.side.upper(), "ttl_seconds": req.ttl_seconds,
    })
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed"))
    return {"success": True, "message": f"Registered {req.side.upper()} {req.symbol}"}


@router.delete("/operator/expect/{symbol}/{side}", dependencies=[Depends(verify_api_key)])
async def cancel_operator_order(symbol: str, side: str):
    result = await send_command("cancel_operator", {"symbol": symbol, "side": side.upper()})
    return {"success": True}


@router.get("/operator/pending", dependencies=[Depends(verify_api_key)])
async def get_pending_operator_orders():
    return read_json(OPERATOR_FILE, [])


@router.post("/positions/{pos_key}/reclassify", dependencies=[Depends(verify_api_key)])
async def reclassify_position(pos_key: str, req: ReclassifyPositionRequest):
    if req.origin not in ("operator", "orphan", "bot"):
        raise HTTPException(status_code=400, detail="Origin must be 'operator', 'orphan', or 'bot'")
    result = await send_command("reclassify", {"pos_key": pos_key, "origin": req.origin})
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", f"Position {pos_key} not found"))
    return {"success": True, "pos_key": pos_key, "new_origin": req.origin}


# ── Daily Performance History ────────────────────────────────────────────

@router.get("/performance/history", dependencies=[Depends(verify_api_key)])
async def get_performance_history(days: int = 90):
    """Return historical daily performance for charts."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(DailyPerformance)
                .order_by(DailyPerformance.date.asc())
                .limit(days)
            )
            records = result.scalars().all()

        history = [
            {
                "date": r.date,
                "starting_capital": r.starting_capital,
                "ending_capital": r.ending_capital,
                "pnl": r.pnl,
                "trades_count": r.trades_count,
                "wins": r.wins,
                "losses": r.losses,
                "win_rate": r.win_rate,
                "forex_pnl": r.forex_pnl or 0,
                "actions_pnl": r.actions_pnl or 0,
                "indices_pnl": r.indices_pnl or 0,
                "commodities_pnl": r.commodities_pnl or 0,
                "mt5_capital": getattr(r, 'capitalcom_capital', 0) or 0,
            }
            for r in records
        ]
        return history
    except Exception as e:
        logger.error(f"Failed to fetch performance history: {e}")
        return []


@router.post("/performance/save-now", dependencies=[Depends(verify_api_key)])
async def save_performance_now():
    result = await send_command("save_performance")
    if not result.get("success"):
        raise HTTPException(status_code=503, detail=result.get("error", "Failed"))
    return {"success": True}


@router.post("/admin/sync-broker-history", dependencies=[Depends(verify_api_key)])
async def sync_broker_history(days: int = 30):
    """Fetch deal history from the currently-connected MT5 broker
    (Fusion Markets) and backfill the `trades` table.

    Calls MT5 HistoryDealsGet via the bot process (IPC). Default: last 30 days.
    Safe to re-run (idempotent on broker_deal_id).
    """
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be in [1, 365]")
    # Long timeout: 30 days = 5 chunks × (request + 1.1s spacing) ≈ 30s worst case
    result = await send_command("sync_broker_history", {"days": days}, timeout=180)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("error", "IPC failure"))
    return result.get("data") or result


# Alias for backwards compatibility — kept so old scripts don't break.
@router.post("/admin/sync-icmarkets-history", dependencies=[Depends(verify_api_key)])
async def sync_icmarkets_history_alias(days: int = 30):
    return await sync_broker_history(days=days)


@router.get("/admin/latency-stats", dependencies=[Depends(verify_api_key)])
async def latency_stats(hours: int = 24):
    """Compute signal→order latency distribution from the timing.jsonl log.

    Returns count, mean, p50, p95, p99 (ms) for order_sent events, plus
    abandoned-signal counters (10s RULE / stale spot).
    """
    import os as _os
    import statistics as _stats

    log_dir = _os.environ.get("LOG_DIR", "/var/log/trading-bot")
    primary = _os.path.join(log_dir, "timing.jsonl")
    fallback = "/tmp/trading-bot-logs/timing.jsonl"
    path = primary if _os.path.exists(primary) else fallback
    if not _os.path.exists(path):
        return {"error": "timing.jsonl not found", "checked": [primary, fallback]}

    cutoff = time.time() - hours * 3600
    latencies_send: list[float] = []
    latencies_fill: list[float] = []
    abandoned = 0
    fills = 0
    failures = 0

    try:
        with open(path, "r") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("ts", 0) < cutoff:
                    continue
                ev = evt.get("event")
                if ev == "order_sent":
                    lat = evt.get("latency_ms")
                    if isinstance(lat, (int, float)):
                        latencies_send.append(float(lat))
                elif ev == "order_filled":
                    fills += 1
                    tm = evt.get("total_ms")
                    if isinstance(tm, (int, float)):
                        latencies_fill.append(float(tm))
                elif ev == "order_failed":
                    failures += 1
                elif ev == "signal_abandoned_10s" or ev == "signal_abandoned_stale":
                    abandoned += 1
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}")

    def _dist(values: list[float]) -> dict:
        if not values:
            return {"count": 0}
        values_sorted = sorted(values)
        n = len(values_sorted)
        def _pct(p: float) -> float:
            k = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
            return round(values_sorted[k], 0)
        return {
            "count": n,
            "mean_ms": round(_stats.fmean(values), 0),
            "p50_ms": _pct(50),
            "p95_ms": _pct(95),
            "p99_ms": _pct(99),
            "min_ms": round(values_sorted[0], 0),
            "max_ms": round(values_sorted[-1], 0),
        }

    return {
        "hours": hours,
        "signal_to_send": _dist(latencies_send),
        "signal_to_fill": _dist(latencies_fill),
        "fills": fills,
        "failures": failures,
        "abandoned": abandoned,
        "log_path": path,
    }


@router.post("/performance/rebuild", dependencies=[Depends(verify_api_key)])
async def rebuild_performance_history():
    """Rebuild daily performance history from closed trades in DB.

    This fixes missing P&L/trades_count for days where the bot was restarted
    and lost in-memory state. Reads all closed trades and recalculates
    per-day stats.
    """
    from app.models.trade import Trade, TradeStatus, DailyPerformance
    from app.trading.symbol_mapper import get_market_for_symbol
    from sqlalchemy import cast, Date, func as sa_func, and_

    try:
        async with async_session() as session:
            # Get all closed trades grouped by exit date
            result = await session.execute(
                select(Trade).where(Trade.status == TradeStatus.CLOSED).order_by(Trade.exit_time)
            )
            all_trades = result.scalars().all()

            # Group trades by date
            trades_by_date = {}
            for t in all_trades:
                if not t.exit_time:
                    continue
                d = str(t.exit_time.date())
                if d not in trades_by_date:
                    trades_by_date[d] = []
                trades_by_date[d].append(t)

            updated_dates = []
            for day, trades in trades_by_date.items():
                pnl_total = sum(t.pnl or 0 for t in trades)
                wins = sum(1 for t in trades if (t.pnl or 0) > 0)
                losses = sum(1 for t in trades if (t.pnl or 0) <= 0)
                trade_pnls = [t.pnl or 0 for t in trades]
                forex_pnl = sum(t.pnl or 0 for t in trades if get_market_for_symbol(t.symbol or "") == "FOREX")
                actions_pnl = sum(t.pnl or 0 for t in trades if get_market_for_symbol(t.symbol or "") == "STOCKS")
                indices_pnl = sum(t.pnl or 0 for t in trades if get_market_for_symbol(t.symbol or "") == "INDICES")
                commodities_pnl = sum(t.pnl or 0 for t in trades if get_market_for_symbol(t.symbol or "") == "COMMODITY")
                win_rate = round(wins / len(trades) * 100, 1) if trades else 0

                existing = await session.execute(
                    select(DailyPerformance).where(DailyPerformance.date == day)
                )
                record = existing.scalar_one_or_none()

                if record:
                    # Only update trade stats if they were missing (pnl=0 with no trades)
                    if record.trades_count == 0 and len(trades) > 0:
                        record.pnl = round(pnl_total, 2)
                        record.trades_count = len(trades)
                        record.wins = wins
                        record.losses = losses
                        record.win_rate = win_rate
                        record.best_trade_pnl = round(max(trade_pnls), 2) if trade_pnls else 0
                        record.worst_trade_pnl = round(min(trade_pnls), 2) if trade_pnls else 0
                        record.forex_pnl = round(forex_pnl, 2)
                        record.actions_pnl = round(actions_pnl, 2)
                        record.indices_pnl = round(indices_pnl, 2)
                        record.commodities_pnl = round(commodities_pnl, 2)
                        updated_dates.append({"date": day, "trades": len(trades), "pnl": round(pnl_total, 2)})
                else:
                    # Create new entry
                    record = DailyPerformance(
                        date=day,
                        starting_capital=0,
                        ending_capital=0,
                        pnl=round(pnl_total, 2),
                        trades_count=len(trades),
                        wins=wins,
                        losses=losses,
                        win_rate=win_rate,
                        best_trade_pnl=round(max(trade_pnls), 2) if trade_pnls else 0,
                        worst_trade_pnl=round(min(trade_pnls), 2) if trade_pnls else 0,
                        forex_pnl=round(forex_pnl, 2),
                        actions_pnl=round(actions_pnl, 2),
                        indices_pnl=round(indices_pnl, 2),
                        commodities_pnl=round(commodities_pnl, 2),
                    )
                    session.add(record)
                    updated_dates.append({"date": day, "trades": len(trades), "pnl": round(pnl_total, 2), "new": True})

            await session.commit()

        return {
            "success": True,
            "total_trades": len(all_trades),
            "days_with_trades": len(trades_by_date),
            "updated": updated_dates,
        }
    except Exception as e:
        logger.error(f"Rebuild performance failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/trades/fix-commissions", dependencies=[Depends(verify_api_key)])
async def fix_trade_commissions():
    """Fix inflated commissions for indices/commodities trades.

    IC Markets Raw Spread: only FOREX has $7/lot commission.
    Indices, commodities, stocks have 0 commission (spread only).
    This endpoint recalculates commission and net_pnl for all affected trades.
    """
    from app.models.trade import Trade, TradeStatus
    from app.trading.symbol_mapper import get_market_for_symbol

    try:
        async with async_session() as session:
            result = await session.execute(
                select(Trade).where(Trade.status == TradeStatus.CLOSED)
            )
            all_trades = result.scalars().all()

            fixed_count = 0
            details = []
            for t in all_trades:
                market = get_market_for_symbol(t.symbol or "")
                old_commission = t.commission or 0

                if market != "FOREX" and old_commission > 1.0:
                    # Non-forex trades should have 0 commission
                    old_net = t.net_pnl or 0
                    t.commission = 0.0
                    t.commission_raw = 0.0
                    t.net_pnl = round((t.pnl or 0), 2)
                    fixed_count += 1
                    details.append({
                        "id": t.id,
                        "symbol": t.symbol,
                        "market": market,
                        "old_commission": round(old_commission, 2),
                        "new_commission": 0.0,
                        "old_net_pnl": round(old_net, 2),
                        "new_net_pnl": t.net_pnl,
                    })
                elif market == "FOREX":
                    # Recalculate forex commission properly: $7/lot round trip
                    qty = t.quantity or 0
                    lots = qty / 100000
                    correct_commission = round(lots * 7.0 * 0.87, 2)  # $7/lot → EUR
                    if abs(old_commission - correct_commission) > 0.5:
                        old_net = t.net_pnl or 0
                        t.commission = correct_commission
                        t.net_pnl = round((t.pnl or 0) - correct_commission, 2)
                        fixed_count += 1
                        details.append({
                            "id": t.id,
                            "symbol": t.symbol,
                            "market": market,
                            "old_commission": round(old_commission, 2),
                            "new_commission": correct_commission,
                            "old_net_pnl": round(old_net, 2),
                            "new_net_pnl": t.net_pnl,
                        })

            await session.commit()

        return {
            "success": True,
            "total_trades": len(all_trades),
            "fixed_count": fixed_count,
            "details": details,
        }
    except Exception as e:
        logger.error(f"Fix commissions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Closed Trades ────────────────────────────────────────────────────────

@router.get("/trades/closed", dependencies=[Depends(verify_api_key)])
async def get_closed_trades(days: int = 30):
    """Return closed trades from DB (single source of truth)."""
    trades = []
    try:
        from app.models.trade import Trade, TradeStatus
        start_date = date.today() if days <= 1 else date.today() - timedelta(days=days)
        async with async_session() as session:
            result = await session.execute(
                select(Trade)
                .where(Trade.status == TradeStatus.CLOSED)
                .where(Trade.exit_time >= start_date)
                .order_by(Trade.exit_time.desc())
                .limit(200)
            )
            db_trades = result.scalars().all()
            for t in db_trades:
                entry_ts = t.entry_time.isoformat() if t.entry_time else ""
                exit_ts = t.exit_time.isoformat() if t.exit_time else ""
                # Calculate duration in minutes
                duration_min = None
                if t.entry_time and t.exit_time:
                    duration_min = round((t.exit_time - t.entry_time).total_seconds() / 60)
                trades.append({
                    "symbol": t.symbol,
                    "action": t.side.value.upper() if t.side else "BUY",
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "pnl": t.pnl or 0,
                    "net_pnl": t.net_pnl or t.pnl or 0,
                    "commission": t.commission or 0,
                    "reason": t.exit_reason or "",
                    "entry_time": entry_ts,
                    "exit_time": exit_ts,
                    "duration_min": duration_min,
                    "broker": "mt5",
                    "market": t.market or "",
                    "origin": t.origin or "bot",
                })
    except Exception as e:
        logger.error(f"Failed to fetch trade history from DB: {e}")

    return trades


# ── Trade History (from database) ─────────────────────────────────────────

@router.get("/trades/history", dependencies=[Depends(verify_api_key)])
async def get_trades_history(days: int = 7):
    days = min(days, 30)
    try:
        from app.models.trade import Trade, TradeStatus
        from collections import defaultdict

        start_date = date.today() - timedelta(days=days)
        start_date_str = start_date.isoformat()

        async with async_session() as session:
            trades_result = await session.execute(
                select(Trade)
                .where(Trade.status == TradeStatus.CLOSED)
                .where(Trade.exit_time >= start_date)
                .order_by(Trade.exit_time.desc())
                .limit(500)
            )
            db_trades = trades_result.scalars().all()

            perf_result = await session.execute(
                select(DailyPerformance)
                .where(DailyPerformance.date >= start_date_str)
                .order_by(DailyPerformance.date.desc())
            )
            perf_records = perf_result.scalars().all()

        # Group trades by date
        trades_by_date: dict[str, list] = defaultdict(list)
        for t in db_trades:
            exit_date = t.exit_time.strftime("%Y-%m-%d") if t.exit_time else (
                t.entry_time.strftime("%Y-%m-%d") if t.entry_time else ""
            )
            if exit_date:
                trades_by_date[exit_date].append({
                    "symbol": t.symbol,
                    "action": t.side.value.upper() if t.side else "BUY",
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "quantity": t.quantity,
                    "pnl": round(t.pnl or 0, 2),
                    "commission": round(t.commission or 0, 2),
                    "reason": t.exit_reason or "",
                    "entry_time": t.entry_time.isoformat() if t.entry_time else "",
                    "exit_time": t.exit_time.isoformat() if t.exit_time else "",
                    "duration_min": (
                        round((t.exit_time - t.entry_time).total_seconds() / 60)
                        if t.exit_time and t.entry_time else None
                    ),
                    "broker": "mt5",
                    "market_category": t.market or "",
                    "origin": t.origin or "bot",
                })

        # Today's trades from bot memory
        bot_trades = read_json(TRADES_FILE, [])
        if bot_trades:
            today_str = date.today().isoformat()
            existing_exits = {
                (t["symbol"], t.get("exit_time", "")[:16])
                for t in trades_by_date.get(today_str, [])
            }
            for bt in bot_trades:
                exit_ts = bt.get("exit_time", "")[:16]
                if (bt.get("symbol", ""), exit_ts) not in existing_exits:
                    trades_by_date[today_str].append(bt)

        perf_by_date = {r.date: r for r in perf_records}
        sorted_days = []
        total_pnl = 0
        total_trades_count = 0
        total_wins = 0
        total_losses = 0

        all_dates = sorted(
            set(list(perf_by_date.keys()) + list(trades_by_date.keys())),
            reverse=True
        )

        for d in all_dates:
            day_trades = trades_by_date.get(d, [])
            perf = perf_by_date.get(d)

            day_wins = len([t for t in day_trades if t.get("pnl", 0) > 0])
            day_losses = len([t for t in day_trades if t.get("pnl", 0) <= 0])
            day_pnl = sum(t.get("pnl", 0) for t in day_trades)

            if perf:
                day_pnl = perf.pnl or day_pnl
                day_wins = perf.wins if perf.wins else day_wins
                day_losses = perf.losses if perf.losses else day_losses

            trades_count = len(day_trades) or (perf.trades_count if perf else 0)
            wr = round(day_wins / trades_count * 100, 1) if trades_count > 0 else 0

            day_data = {
                "date": d,
                "trades": day_trades,
                "pnl": round(day_pnl, 2),
                "trades_count": trades_count,
                "wins": day_wins,
                "losses": day_losses,
                "win_rate": wr,
                "forex_pnl": perf.forex_pnl if perf else 0,
                "indices_pnl": perf.indices_pnl if perf else 0,
            }
            sorted_days.append(day_data)
            total_pnl += day_pnl
            total_trades_count += trades_count
            total_wins += day_wins
            total_losses += day_losses

        return {
            "days": sorted_days,
            "summary": {
                "total_pnl": round(total_pnl, 2),
                "total_trades": total_trades_count,
                "wins": total_wins,
                "losses": total_losses,
                "win_rate": round(total_wins / total_trades_count * 100 if total_trades_count > 0 else 0, 1),
            }
        }
    except Exception as e:
        logger.error(f"Failed to fetch trade history: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {"days": [], "summary": {"total_pnl": 0, "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0}}


# ── Trade Performance by Market ──────────────────────────────────────────

@router.get("/trades/by-market", dependencies=[Depends(verify_api_key)])
async def get_trades_by_market(market: str = "commodities", days: int = 30):
    """Trades filtrés par market (forex, indices, commodities). Ex: /trades/by-market?market=commodities pour Gold."""
    try:
        from app.models.trade import Trade, TradeStatus
        from collections import defaultdict

        start_date = date.today() - timedelta(days=days)
        async with async_session() as session:
            result = await session.execute(
                select(Trade)
                .where(Trade.status == TradeStatus.CLOSED)
                .where(Trade.exit_time >= start_date)
                .where(Trade.market == market)
                .order_by(Trade.exit_time.desc())
                .limit(500)
            )
            db_trades = result.scalars().all()

        trades_by_week: dict[str, list] = defaultdict(list)
        for t in db_trades:
            if t.exit_time:
                # ISO week key
                week_key = t.exit_time.strftime("%Y-W%V")
            elif t.entry_time:
                week_key = t.entry_time.strftime("%Y-W%V")
            else:
                continue
            trades_by_week[week_key].append({
                "symbol": t.symbol,
                "action": t.side.value.upper() if t.side else "BUY",
                "pnl": round(t.pnl or 0, 2),
                "reason": t.exit_reason or "",
                "entry_time": t.entry_time.isoformat() if t.entry_time else "",
                "exit_time": t.exit_time.isoformat() if t.exit_time else "",
            })

        weeks = []
        total_pnl = 0
        total_trades = 0
        total_wins = 0
        for wk in sorted(trades_by_week.keys(), reverse=True):
            wk_trades = trades_by_week[wk]
            wk_pnl = sum(t["pnl"] for t in wk_trades)
            wk_wins = len([t for t in wk_trades if t["pnl"] > 0])
            weeks.append({
                "week": wk, "trades": wk_trades,
                "pnl": round(wk_pnl, 2), "count": len(wk_trades),
                "wins": wk_wins, "win_rate": round(wk_wins / len(wk_trades) * 100 if wk_trades else 0, 1),
            })
            total_pnl += wk_pnl
            total_trades += len(wk_trades)
            total_wins += wk_wins

        return {
            "market": market,
            "weeks": weeks,
            "summary": {
                "total_pnl": round(total_pnl, 2),
                "total_trades": total_trades,
                "wins": total_wins,
                "win_rate": round(total_wins / total_trades * 100 if total_trades else 0, 1),
            }
        }
    except Exception as e:
        logger.error(f"Failed to fetch trades by market: {e}")
        return {"market": market, "weeks": [], "summary": {"total_pnl": 0, "total_trades": 0, "wins": 0, "win_rate": 0}}


# ── WebSocket ─────────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = None):
    if token != settings.api_key:
        await ws.close(code=4001, reason="Invalid API key")
        return

    await ws_manager.connect(ws)

    # Send current state on connect
    status = read_json(STATUS_FILE)
    if status:
        await ws_manager.send_to(ws, "bot_status", status)
    signals = read_json(SIGNALS_FILE, [])
    for sig in signals:
        await ws_manager.send_to(ws, "signal", sig)

    try:
        while True:
            data = await ws.receive_json()
            event_type = data.get("type")

            if event_type == "manual_order":
                d = data.get("data", {})
                result = await send_command("manual_order", {
                    "symbol": d.get("symbol", ""),
                    "action": d.get("action", "buy"),
                    "amount_eur": d.get("amount", 20),
                }, timeout=20)
                await ws_manager.send_to(ws, "order_result", result.get("data", result))

    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        ws_manager.disconnect(ws)


# ═══ AUDIT REPORT ENDPOINT ═══════════════════════════════════════════

@router.get("/audit/report", dependencies=[Depends(verify_api_key)])
async def get_audit_report():
    """Audit report — read from status file or return basic info."""
    status = read_json(STATUS_FILE, {})
    return {
        "status": "OK",
        "bot_running": status.get("running", False),
        "mt5_connected": status.get("mt5_connected", False),
        "message": "Audit détaillé disponible via le processus bot",
    }


@router.post("/sync", dependencies=[Depends(verify_api_key)])
async def force_sync():
    return {"success": True, "message": "MT5 — sync via bot process"}


# ═══════════════════════════════════════════════════════════════════════
# MT5 connection status
# ═══════════════════════════════════════════════════════════════════════
# No OAuth for MT5 — the terminal logs in with login/password/server on
# container start (see trading-backend/mt5-bridge). We just expose the
# bot-reported status here so the frontend keeps a uniform shape.

@router.get("/mt5/status", dependencies=[Depends(verify_api_key)])
async def mt5_status():
    status = read_json(STATUS_FILE, {})
    account = read_json(ACCOUNT_FILE, {})
    connected = bool(status.get("mt5_connected", False))
    return {
        "connected": connected,
        "balance": account.get("balance", 0),
        "equity": account.get("net_liquidation", 0),
        "currency": account.get("currency", "EUR"),
        "primary_broker": account.get("primary_broker", "Fusion Markets MT5"),
    }
