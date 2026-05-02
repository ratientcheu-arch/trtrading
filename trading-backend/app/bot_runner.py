"""
Standalone bot process — runs the trading bot with MT5/ZMQ in isolation.

This process is completely separate from the API (uvicorn) process.
Communication happens via shared files (IPC) and the PostgreSQL database.

Usage:
    python -m app.bot_runner
"""
import asyncio
import signal
import time
import os
import sys
import json
import subprocess
from datetime import datetime, timezone

# Ensure the project root is in PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.utils.logging import setup_logging, get_logger
from app.database import init_db
from app.ipc import (
    IPC_DIR, poll_commands, send_response, append_event,
    write_json, read_json, cleanup_stale_commands,
    truncate_events_file, STATUS_FILE, ACCOUNT_FILE,
    POSITIONS_FILE, SIGNALS_FILE, CONFIG_FILE,
    DAILY_FILE, TRADES_FILE, OPERATOR_FILE,
    MANUAL_BALANCE_FILE,
)

logger = get_logger(__name__)

bot = None
_shutdown = False


async def process_command(cmd: dict):
    """Execute a command received from the API process."""
    global bot
    cmd_id = cmd.get("id", "")
    cmd_name = cmd.get("cmd", "")
    params = cmd.get("params", {})

    logger.info(f"[IPC] Processing command: {cmd_name} (id={cmd_id})")

    try:
        if cmd_name == "start":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            if not bot.mt5_available:
                send_response(cmd_id, False, error="MT5 non connecté — impossible de trader.")
                return
            bot._scan_only = False
            if bot._running:
                logger.info("[IPC] Bot already running — switched to AUTO mode")
            else:
                await bot.start()
            send_response(cmd_id, True, data={"status": bot.status})

        elif cmd_name == "scan_only":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            bot._scan_only = True
            if bot._running:
                logger.info("[IPC] Bot already running — switched to SCAN-ONLY mode")
            else:
                await bot.start()
            send_response(cmd_id, True, data={"mode": "scan_only", "status": bot.status})

        elif cmd_name == "stop":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            close_positions = params.get("close_positions", False)
            await bot.stop(close_positions=close_positions)
            send_response(cmd_id, True, data={"status": bot.status})

        elif cmd_name == "emergency_stop":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            await bot.emergency_stop()
            send_response(cmd_id, True, data={"message": "Emergency stop executed"})

        elif cmd_name == "set_mode":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            if not bot._running:
                send_response(cmd_id, False, error="Bot not running — start it first")
                return
            mode = params.get("mode", "auto")
            bot._scan_only = (mode == "scan")
            send_response(cmd_id, True, data={"scan_only": bot._scan_only, "status": bot.status})

        elif cmd_name == "manual_order":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            symbol = params.get("symbol", "")
            action = params.get("action", "buy")
            amount_eur = params.get("amount_eur", 20)
            result = await bot.manual_order(symbol, action, amount_eur)
            send_response(cmd_id, result.get("success", False), data=result,
                          error=result.get("error"))

        elif cmd_name == "manual_close":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            symbol = params.get("symbol", "")
            result = await bot.manual_close(symbol)
            send_response(cmd_id, result.get("success", False), data=result,
                          error=result.get("error"))

        elif cmd_name == "modify_sl":
            if not bot or not bot.mt5_available:
                send_response(cmd_id, False, error="MT5 not connected")
                return
            try:
                ticket = int(params.get("ticket", 0))
                new_sl = float(params.get("new_sl", 0))
            except (TypeError, ValueError):
                send_response(cmd_id, False, error="Invalid ticket or new_sl")
                return
            if ticket <= 0 or new_sl <= 0:
                send_response(cmd_id, False, error="ticket and new_sl must be > 0")
                return
            # Fetch current TP to preserve it (amend_position_sltp sends both)
            current_tp = None
            try:
                positions = await bot.mt5.get_positions()
                for pos in positions:
                    if pos.get("ticket") == ticket:
                        current_tp = pos.get("take_profit") or None
                        break
            except Exception as e:
                logger.warning(f"[modify_sl] Could not fetch current TP for {ticket}: {e}")
            ok = await bot.mt5.amend_position_sltp(ticket, stop_loss=new_sl, take_profit=current_tp)
            if ok:
                logger.info(f"[modify_sl] ticket={ticket} new_sl={new_sl} tp_preserved={current_tp}")
                send_response(cmd_id, True, data={"ticket": ticket, "new_sl": new_sl, "tp": current_tp})
            else:
                send_response(cmd_id, False, error="Broker rejected SL modification")

        elif cmd_name == "close_position_direct":
            # Fallback: close directly on MT5 (when bot doesn't track the position)
            if not bot or not bot.mt5_available:
                send_response(cmd_id, False, error="MT5 not connected")
                return
            symbol = params.get("symbol", "")
            # Get position details BEFORE closing
            positions = await bot.mt5.get_positions()
            target_pos = None
            for pos in positions:
                if pos["symbol"] == symbol or pos["symbol"].replace("/", "") == symbol.replace("/", ""):
                    target_pos = pos
                    break
            ct_result = await bot.mt5.close_position(symbol)
            if ct_result:
                # Save trade to DB
                try:
                    from app.models.trade import Trade, TradeStatus, TradeSide
                    from app.database import async_session
                    from datetime import datetime, timezone

                    entry_price = target_pos.get("entry_price", 0) if target_pos else 0
                    quantity = target_pos.get("quantity", 0) if target_pos else 0
                    action = target_pos.get("action", "BUY") if target_pos else "BUY"
                    sl = target_pos.get("stop_loss") if target_pos else None
                    tp = target_pos.get("take_profit") if target_pos else None
                    pnl = ct_result.get("pnl", 0)
                    margin = target_pos.get("margin", 0) if target_pos else 1

                    if entry_price and entry_price > 0:
                        exit_price = target_pos.get("current_price", entry_price) if target_pos else entry_price
                        async with async_session() as session:
                            trade = Trade(
                                symbol=symbol,
                                name=symbol,
                                side=TradeSide.BUY if action == "BUY" else TradeSide.SELL,
                                status=TradeStatus.CLOSED,
                                entry_price=entry_price,
                                quantity=quantity,
                                entry_amount=entry_price * quantity,
                                exit_price=exit_price,
                                exit_time=datetime.now(timezone.utc),
                                exit_reason="manual",
                                stop_loss=sl,
                                take_profit=tp,
                                pnl=round(pnl, 2),
                                pnl_percent=round((pnl / (margin or 1)) * 100, 2),
                                market=target_pos.get("market_category", "FOREX") if target_pos else "FOREX",
                                asset_type=target_pos.get("asset_type", "forex") if target_pos else "forex",
                            )
                            session.add(trade)
                            await session.commit()
                            logger.info(f"[DIRECT CLOSE] Trade saved: {symbol} PnL={pnl:.2f}")
                except Exception as e:
                    logger.error(f"[DIRECT CLOSE] Failed to save trade: {e}")

                send_response(cmd_id, True, data={"pnl": ct_result.get("pnl", 0), "broker": "mt5", "direct": True})
            else:
                send_response(cmd_id, False, error=f"Failed to close {symbol} on MT5")

        elif cmd_name == "update_config":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            rm = bot.risk_manager
            if params.get("max_order_size") is not None:
                rm.max_order_size = params["max_order_size"]
            if params.get("max_risk_per_trade") is not None:
                rm.max_risk_per_trade = params["max_risk_per_trade"]
            if params.get("max_daily_loss") is not None:
                rm.max_daily_loss = params["max_daily_loss"]
            if params.get("max_open_positions") is not None:
                rm.max_open_positions = params["max_open_positions"]
            if params.get("scan_interval") is not None:
                bot._scan_interval = params["scan_interval"]
            logger.info(f"Bot config updated via IPC: {params}")
            _write_config_file()
            send_response(cmd_id, True, data={"config": read_json(CONFIG_FILE, {})})

        elif cmd_name == "sync_balance":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            real_balance = params.get("balance", 0)
            if real_balance <= 0:
                send_response(cmd_id, False, error="Balance must be > 0")
                return
            old_capital = bot.risk_manager.capital
            bot.risk_manager.capital = real_balance
            write_json(MANUAL_BALANCE_FILE, {
                "balance": real_balance,
                "timestamp": time.time(),
            })
            logger.info(f"[SYNC] Capital manually updated: {old_capital:.2f} → {real_balance:.2f} EUR")
            send_response(cmd_id, True, data={"old_capital": round(old_capital, 2), "new_capital": round(real_balance, 2)})

        elif cmd_name == "register_operator":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            bot.register_operator_order(params.get("symbol", ""), params.get("side", "BUY"), params.get("ttl_seconds", 600))
            send_response(cmd_id, True)

        elif cmd_name == "cancel_operator":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            bot.cancel_operator_order(params.get("symbol", ""), params.get("side", "BUY"))
            send_response(cmd_id, True)

        elif cmd_name == "reclassify":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            ok = await bot.reclassify_position(params.get("pos_key", ""), params.get("origin", "bot"))
            send_response(cmd_id, ok, error=None if ok else "Position not found")

        elif cmd_name == "reset_daily_pnl":
            # 2026-04-21: reset soft du _daily_pnl pour autoriser trading après
            # grosse perte session Asia. N'annule pas les trades en DB, ne stoppe rien.
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            _old = bot.risk_manager._daily_pnl
            bot.risk_manager._daily_pnl = 0.0
            bot.risk_manager._consecutive_losses = 0
            logger.warning(f"[RESET DAILY PNL] daily_pnl {_old:+.2f} → 0.00, consecutive_losses → 0 (soft reset)")
            send_response(cmd_id, True, data={"old_daily_pnl": _old, "new_daily_pnl": 0.0})

        elif cmd_name == "save_performance":
            if not bot:
                send_response(cmd_id, False, error="Bot not initialized")
                return
            await bot._save_daily_performance()
            send_response(cmd_id, True)

        elif cmd_name == "get_market_quote":
            if not bot or not bot.mt5_available:
                send_response(cmd_id, False, error="MT5 not connected")
                return
            symbol = params.get("symbol", "")
            quote = await bot.mt5.get_realtime_quote(symbol)
            if quote:
                send_response(cmd_id, True, data=quote)
            else:
                send_response(cmd_id, False, error=f"No data for {symbol}")

        elif cmd_name in ("sync_broker_history", "sync_icmarkets_history"):
            # sync_icmarkets_history kept as alias for backwards compat
            if not bot or not bot.mt5_available:
                send_response(cmd_id, False, error="MT5 not connected")
                return
            try:
                days = int(params.get("days", 30))
                from_ts = time.time() - days * 86400
                to_ts = time.time()
                deals = await bot.mt5.fetch_deal_list(from_ts, to_ts)
                inserted, updated = await _persist_broker_deals(deals)
                send_response(cmd_id, True, data={
                    "days": days,
                    "deals_fetched": len(deals),
                    "trades_inserted": inserted,
                    "trades_updated": updated,
                })
            except Exception as e:
                logger.error(f"[sync_broker_history] {e}", exc_info=True)
                send_response(cmd_id, False, error=str(e))

        else:
            send_response(cmd_id, False, error=f"Unknown command: {cmd_name}")

    except Exception as e:
        logger.error(f"[IPC] Command {cmd_name} failed: {e}", exc_info=True)
        send_response(cmd_id, False, error=str(e))


async def _persist_broker_deals(deals: list[dict]) -> tuple[int, int]:
    """Persist MT5 broker deals (IC Markets / Fusion Markets / etc.) into
    the trades table as closed trade pairs.

    Groups deals by position_id: an entry deal (is_close=False) + its closing deals
    (is_close=True) form one complete round trip. Skips deals whose position has
    no close (still open) — those are tracked in open_positions already.

    Returns (inserted, updated).
    """
    from app.database import async_session
    from app.models.trade import Trade, TradeStatus, TradeSide
    from app.trading.symbol_mapper import get_market_for_symbol
    from sqlalchemy import select
    from datetime import datetime as _dt, timezone as _tz

    inserted = 0
    updated = 0

    # Group deals by position_id
    by_pos: dict[int, list[dict]] = {}
    for d in deals:
        pid = d.get("position_id")
        if not pid:
            continue
        by_pos.setdefault(pid, []).append(d)

    async with async_session() as session:
        for pid, pdeals in by_pos.items():
            pdeals.sort(key=lambda x: x.get("execution_timestamp", 0))
            entry = next((d for d in pdeals if not d.get("is_close")), None)
            closes = [d for d in pdeals if d.get("is_close")]
            if not entry or not closes:
                continue  # still open or malformed
            last_close = closes[-1]

            # Check if already persisted (by broker_deal_id of entry)
            entry_deal_id = str(entry.get("deal_id"))
            existing = await session.execute(
                select(Trade).where(Trade.broker_deal_id == entry_deal_id)
            )
            rec = existing.scalar_one_or_none()

            symbol = entry.get("symbol") or ""
            side = TradeSide.BUY if entry.get("trade_side") == "BUY" else TradeSide.SELL
            qty = float(entry.get("volume") or 0)
            entry_price = float(entry.get("execution_price") or 0)
            exit_price = float(last_close.get("execution_price") or 0)
            entry_ts = int(entry.get("execution_timestamp") or 0) / 1000
            exit_ts = int(last_close.get("execution_timestamp") or 0) / 1000
            gross_pnl = sum(float(c.get("gross_profit") or 0) for c in closes)
            swap = sum(float(c.get("swap") or 0) for c in closes)
            commission = float(entry.get("commission") or 0) + sum(
                float(c.get("close_commission") or 0) for c in closes
            )
            net_pnl = gross_pnl + swap - abs(commission)

            values = dict(
                symbol=symbol,
                name=symbol,
                side=side,
                status=TradeStatus.CLOSED,
                entry_price=entry_price,
                quantity=qty,
                entry_amount=entry_price * qty,
                entry_time=_dt.fromtimestamp(entry_ts, tz=_tz.utc) if entry_ts else None,
                exit_price=exit_price,
                exit_time=_dt.fromtimestamp(exit_ts, tz=_tz.utc) if exit_ts else None,
                exit_reason="broker_sync",
                pnl=round(gross_pnl, 4),
                commission=round(abs(commission), 4),
                net_pnl=round(net_pnl, 4),
                market=get_market_for_symbol(symbol),
                broker_deal_id=entry_deal_id,
                broker_position_id=str(pid),
                source="broker_sync",
                origin="bot",
            )
            if rec:
                for k, v in values.items():
                    if v is not None:
                        setattr(rec, k, v)
                updated += 1
            else:
                session.add(Trade(**values))
                inserted += 1

        await session.commit()

    logger.info(f"[broker_sync] persisted: inserted={inserted} updated={updated}")
    return inserted, updated


def _write_config_file():
    """Write current bot config to IPC file."""
    if not bot:
        return
    rm = bot.risk_manager
    write_json(CONFIG_FILE, {
        "starting_capital": settings.starting_capital,
        "current_capital": rm.capital,
        "max_order_size": rm.max_order_size,
        "max_risk_per_trade": rm.max_risk_per_trade,
        "max_daily_loss": rm.max_daily_loss,
        "max_open_positions": rm.max_open_positions,
        "scan_interval": bot._scan_interval,
        "leverage": {
            "forex": settings.leverage_forex,
            "indices": settings.leverage_indices,
            "commodities": settings.leverage_commodities,
            "stocks": settings.leverage_stocks,
            "crypto": settings.leverage_crypto,
        },
        "allocation": {
            "forex": settings.allocation_forex,
            "indices": settings.allocation_indices,
            "stocks": settings.allocation_stocks,
            "commodities": settings.allocation_commodities,
        },
        "dynamic_allocation": rm.allocator.current_allocations if hasattr(rm, 'allocator') else {},
    })


_last_broker_refresh = 0  # Track when we last refreshed from broker


def _refresh_positions_from_broker():
    """Refresh positions from broker via subprocess — fills in missing prices."""
    global _last_broker_refresh
    import subprocess, json as _json

    # Only refresh every 10s to avoid hammering MT5
    if time.time() - _last_broker_refresh < 10:
        return
    _last_broker_refresh = time.time()

    positions = read_json(POSITIONS_FILE, [])
    if not positions:
        return

    # Check if any position has missing entry_price or current_price
    needs_refresh = any(
        p.get("entry_price", 0) == 0 or p.get("current_price", 0) == 0
        for p in positions
    )
    if not needs_refresh:
        return

    logger.info("[STATE] Refreshing positions from broker (missing prices)")
    script = '''
import asyncio, sys, json
sys.path.insert(0, "/app")
from app.trading.mt5_client import MT5Client

async def get_pos():
    c = MT5Client()
    ok = await c.connect()
    if not ok:
        print(json.dumps([]))
        return
    positions = await c.get_positions()
    await c.disconnect()
    result = []
    for p in (positions or []):
        result.append({
            "ticket": p.get("ticket") or p.get("position_id"),
            "entry_price": p.get("entry_price", 0),
            "current_price": p.get("current_price", 0),
            "unrealized_pnl": p.get("unrealized_pnl", 0),
            "quantity": p.get("quantity", 0),
            "direction": p.get("direction", "BUY"),
            "symbol": p.get("symbol", ""),
        })
    print(json.dumps(result))

asyncio.run(get_pos())
'''
    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=20, cwd="/app",
        )
        stdout = result.stdout.strip()
        if not stdout:
            return

        for line in reversed(stdout.split("\n")):
            line = line.strip()
            if line.startswith("["):
                broker_positions = _json.loads(line)
                break
        else:
            return

        # Build lookup by ticket
        broker_by_ticket = {}
        for bp in broker_positions:
            t = bp.get("ticket")
            if t:
                broker_by_ticket[int(t)] = bp

        # Update positions with broker data
        updated = False
        from app.trading.symbol_mapper import get_leverage
        for pos in positions:
            ticket = pos.get("ticket")
            if not ticket:
                continue
            bp = broker_by_ticket.get(int(ticket))
            if not bp:
                continue
            # Update entry_price if missing
            if pos.get("entry_price", 0) == 0 and bp.get("entry_price", 0) > 0:
                pos["entry_price"] = bp["entry_price"]
                updated = True
                # Also update bot._open_positions
                if bot:
                    for pk, pv in bot._open_positions.items():
                        if pv.get("ticket") == ticket or pv.get("position_id") == ticket:
                            pv["entry_price"] = bp["entry_price"]
                            break
            # Always update current_price and pnl from broker
            if bp.get("current_price", 0) > 0:
                pos["current_price"] = bp["current_price"]
                pos["pnl"] = round(bp.get("unrealized_pnl", 0), 2)
                entry = pos.get("entry_price", 0) or bp.get("entry_price", 0)
                qty = bp.get("quantity", pos.get("quantity", 0))
                sym = pos.get("symbol", "")
                lev = get_leverage(sym)
                # Correct notional calculation in EUR
                sym_upper = sym.upper().replace("/", "")
                if "/" in sym:
                    # Forex: qty is base currency units
                    if sym_upper.startswith("EUR"):
                        notional = qty
                    elif sym_upper.startswith("USD"):
                        notional = qty / 1.17
                    elif sym_upper.startswith("GBP"):
                        notional = qty * 1.15
                    elif sym_upper.startswith("AUD") or sym_upper.startswith("NZD"):
                        notional = qty * 0.60
                    else:
                        notional = qty  # Fallback
                else:
                    notional = qty * (entry or 1) / 1.17  # Indices/commodities in USD
                margin = notional / lev if lev > 0 else notional
                pos["margin"] = round(margin, 2)
                pos["exposure"] = round(notional, 2)
                if margin > 0:
                    pos["pnl_percent"] = round((pos["pnl"] / margin) * 100, 2)
                updated = True

        if updated:
            write_json(POSITIONS_FILE, positions)
            logger.info(f"[STATE] Positions updated from broker ({len(broker_positions)} broker pos)")

    except subprocess.TimeoutExpired:
        logger.warning("[STATE] Broker refresh subprocess timed out")
    except Exception as e:
        logger.error(f"[STATE] Broker refresh failed: {e}")


def _write_all_state_files():
    """Write all state files to IPC directory (called periodically)."""
    if not bot:
        return
    try:
        # ═══ SYNC POSITIONS_FILE with bot._open_positions ═══
        # When bot is stopped or after a restart, POSITIONS_FILE may hold
        # stale entries. The bot's in-memory dict is the source of truth
        # for tracked positions — mirror it so the dashboard doesn't show
        # phantom positions from before a restart.
        if not bot._open_positions:
            # No tracked positions → POSITIONS_FILE must be empty
            try:
                _current = read_json(POSITIONS_FILE, [])
                if _current:
                    logger.info(f"[STATE] Clearing {len(_current)} stale positions from POSITIONS_FILE (bot has 0 tracked)")
                    write_json(POSITIONS_FILE, [])
            except Exception:
                pass

        # Refresh positions from broker FIRST so unrealized is up to date
        try:
            _refresh_positions_from_broker()
        except Exception as e:
            logger.error(f"[STATE] Position refresh error: {e}")

        # Compute unrealized from bot-tracked positions only
        _ic_capital = bot.risk_manager.capital
        _ic_pnl = float(bot.risk_manager._daily_pnl or 0)
        _positions = read_json(POSITIONS_FILE, [])
        _unrealized = sum(p.get("pnl", 0) for p in _positions)
        # Push the value into the bot so status uses the SAME unrealized
        try:
            bot._current_unrealized_pnl = _unrealized
        except Exception:
            pass

        _ic_equity = _ic_capital + _unrealized
        _ic_margin = sum(p.get("margin", 0) for p in _positions)
        # Free margin: never negative on the dashboard
        _free_mar = max(0.0, _ic_equity - _ic_margin)
        _daily_total = round(_ic_pnl + _unrealized, 2)

        # Status — written AFTER unrealized is set so daily_pnl is in sync
        write_json(STATUS_FILE, bot.status)

        # Account
        write_json(ACCOUNT_FILE, {
            "balance": round(_ic_capital, 2),
            "net_liquidation": round(_ic_equity, 2),
            "unrealized_pnl": round(_unrealized, 2),
            "buying_power": round(_free_mar, 2),
            "daily_pnl": _daily_total,
            "daily_pnl_realized": round(_ic_pnl, 2),
            "daily_pnl_unrealized": round(_unrealized, 2),
            "deposit": round(settings.starting_capital, 2),
            "profit_loss": round(_ic_equity - settings.starting_capital, 2),
            "total_pnl": round(_ic_equity - settings.starting_capital, 2),
            "currency": "EUR",
            "capital": round(_ic_equity, 2),
            "primary_broker": "Fusion Markets MT5",
            "brokers": {
                "mt5": {
                    "balance": round(_ic_capital, 2),
                    "equity": round(_ic_equity, 2),
                    "free_margin": round(_free_mar, 2),
                    "profit": round(_unrealized, 2),
                }
            },
        })

        # Signals
        write_json(SIGNALS_FILE, bot.get_signals())

        # Daily trades list
        write_json(TRADES_FILE, bot.get_daily_trades())

        # Daily summary
        trades = bot._daily_trades or []
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) <= 0 and t.get("pnl") is not None]
        daily_pnl = bot.risk_manager._daily_pnl if bot.risk_manager else 0
        write_json(DAILY_FILE, {
            "pnl": round(daily_pnl, 2),
            "theoretical_pnl": round(daily_pnl, 2),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        })

        # Operator pending orders
        now = time.time()
        pending = []
        for key, val in bot._pending_operator_orders.items():
            remaining = val["expires_at"] - now
            if remaining > 0:
                pending.append({
                    "symbol": val["symbol"],
                    "side": val["side"],
                    "remaining_seconds": round(remaining),
                })
        write_json(OPERATOR_FILE, pending)

        # Config
        _write_config_file()

    except Exception as e:
        logger.error(f"[IPC] Failed to write state files: {e}")


_pending_futures = {}  # Track async commands in progress


def _emergency_close_position(symbol: str) -> dict:
    """
    Close a position using a SUBPROCESS.

    Legacy subprocess isolation (inherited from the Twisted era).
    With MT5/ZMQ the event loop no longer blocks, so this could be inlined —
    kept as-is for behaviour parity during the migration.
    """
    import subprocess, json as _json
    logger.info(f"[EMERGENCY] Spawning subprocess to close {symbol}")

    script = f'''
import asyncio, sys, json
sys.path.insert(0, "/app")
from app.trading.mt5_client import MT5Client

async def close():
    c = MT5Client()
    ok = await c.connect()
    if not ok:
        print(json.dumps({{"success": False, "error": "connect failed"}}))
        return
    positions = await c.get_positions()
    if not positions:
        print(json.dumps({{"success": False, "error": "no positions"}}))
        await c.disconnect()
        return
    target = None
    sym = "{symbol}"
    for p in positions:
        ps = p.get("symbol", "")
        if ps == sym or ps.replace("/","") == sym.replace("/",""):
            target = p
            break
    if not target:
        syms = [p.get("symbol","?") for p in positions]
        print(json.dumps({{"success": False, "error": f"not found. Open: {{syms}}"}}))
        await c.disconnect()
        return
    r = await c.close_position(target["ticket"])
    pnl = r.get("pnl", 0) if r else 0
    print(json.dumps({{"success": True, "pnl": pnl, "symbol": sym}}))
    await c.disconnect()

asyncio.run(close())
'''
    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=25,
            cwd="/app",
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            logger.warning(f"[EMERGENCY] subprocess stderr: {stderr[-300:]}")
        if stdout:
            # Find last JSON line in output
            for line in reversed(stdout.split("\n")):
                line = line.strip()
                if line.startswith("{"):
                    parsed = _json.loads(line)
                    if parsed.get("success"):
                        logger.info(f"[EMERGENCY] Closed {symbol} via subprocess — PnL={parsed.get('pnl', 0)}")
                    else:
                        logger.error(f"[EMERGENCY] Subprocess close failed: {parsed.get('error')}")
                    return parsed
        return {"success": False, "error": f"No JSON output. stdout={stdout[-200:]} stderr={stderr[-200:]}"}
    except subprocess.TimeoutExpired:
        logger.error(f"[EMERGENCY] Subprocess timed out (25s) for {symbol}")
        return {"success": False, "error": "subprocess timeout"}
    except Exception as e:
        logger.error(f"[EMERGENCY] Subprocess failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def _save_position_to_db(pos_key, symbol, action, entry_price, quantity, sl, tp, ticket, amount_eur):
    """Save a subprocess-opened position to DB so it survives restarts."""
    try:
        from app.models.trade import Trade, TradeStatus, TradeSide
        from app.database import async_session
        from app.trading.symbol_mapper import get_leverage

        lev = get_leverage(symbol)
        notional = quantity * (entry_price or 1)
        margin = notional / lev if lev > 0 else notional

        async with async_session() as session:
            trade = Trade(
                symbol=symbol,
                name=symbol,
                side=TradeSide.BUY if action == "BUY" else TradeSide.SELL,
                status=TradeStatus.OPEN,
                entry_price=entry_price,
                quantity=quantity,
                entry_amount=margin,
                stop_loss=sl,
                take_profit=tp,
                market="FOREX" if "/" in symbol else "INDICES",
                asset_type="forex" if "/" in symbol else "index_cfd",
            )
            # Store pos_key as reference
            trade.notes = f"pos_key={pos_key},ticket={ticket}"
            session.add(trade)
            await session.commit()
            logger.info(f"[EMERGENCY ORDER] Position saved to DB: {symbol} ticket={ticket}")
    except Exception as e:
        logger.error(f"[EMERGENCY ORDER] Failed to save to DB: {e}")


def _save_position_db_sync(pos_key, symbol, action, entry_price, quantity, sl, tp, ticket, amount_eur):
    """Save position to DB via subprocess when event loop is blocked."""
    script = f'''
import asyncio, sys
sys.path.insert(0, "/app")
from app.models.trade import Trade, TradeStatus, TradeSide
from app.database import async_session, init_db

async def save():
    await init_db()
    async with async_session() as session:
        trade = Trade(
            symbol="{symbol}",
            name="{symbol}",
            side=TradeSide.{"BUY" if action == "BUY" else "SELL"},
            status=TradeStatus.OPEN,
            entry_price={entry_price},
            quantity={quantity},
            entry_amount={amount_eur},
            stop_loss={sl or 0},
            take_profit={tp or 0},
            market="{"FOREX" if "/" in symbol else "INDICES"}",
            asset_type="{"forex" if "/" in symbol else "index_cfd"}",
        )
        trade.notes = "pos_key={pos_key},ticket={ticket}"
        session.add(trade)
        await session.commit()
        print("OK")

asyncio.run(save())
'''
    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=15, cwd="/app",
        )
        if "OK" in result.stdout:
            logger.info(f"[EMERGENCY ORDER] Position saved to DB via subprocess: {symbol}")
        else:
            logger.error(f"[EMERGENCY ORDER] DB save subprocess failed: {result.stderr[-200:]}")
    except Exception as e:
        logger.error(f"[EMERGENCY ORDER] DB save subprocess error: {e}")


def _emergency_manual_order(symbol: str, action: str, amount_eur: float) -> dict:
    """
    Place a manual order using a SUBPROCESS.
    Legacy safety net from the Twisted era; kept for parity with close helpers.
    """
    import subprocess, json as _json
    logger.info(f"[EMERGENCY ORDER] {action} {symbol} {amount_eur}€ via subprocess")

    script = f'''
import asyncio, sys, json
sys.path.insert(0, "/app")
from app.trading.mt5_client import MT5Client
from app.trading.signals import get_pair_config
from app.trading.symbol_mapper import get_leverage

async def do_order():
    c = MT5Client()
    ok = await c.connect()
    if not ok:
        print(json.dumps({{"success": False, "error": "connect failed"}}))
        return

    quote = await c.get_realtime_quote("{symbol}")
    if not quote or not quote.get("bid"):
        print(json.dumps({{"success": False, "error": "no price"}}))
        await c.disconnect()
        return

    action = "{action.upper()}"
    price = quote["bid"] if action == "SELL" else quote["ask"]
    if not price or price <= 0:
        print(json.dumps({{"success": False, "error": f"bad price: {{price}}"}}))
        await c.disconnect()
        return

    pair_cfg = get_pair_config("{symbol}")
    sym_upper = "{symbol}".upper().replace("/", "")
    sl = tp = None
    if pair_cfg:
        if "sl_pct" in pair_cfg:
            sl_d = price * pair_cfg["sl_pct"]
            tp_d = price * pair_cfg["tp_pct"]
        elif "sl_pips" in pair_cfg:
            pip = 0.01 if "JPY" in sym_upper else 0.0001
            sl_d = pair_cfg["sl_pips"] * pip
            tp_d = pair_cfg["tp_pips"] * pip
        else:
            sl_d = price * 0.005
            tp_d = price * 0.010
        if action == "BUY":
            sl = round(price - sl_d, 5)
            tp = round(price + tp_d, 5)
        else:
            sl = round(price + sl_d, 5)
            tp = round(price - tp_d, 5)

    leverage = get_leverage("{symbol}")
    pos_eur = {amount_eur} * leverage
    qty = pos_eur / price
    is_fx = "/" in "{symbol}" and any(x in "{symbol}".upper() for x in ["USD","EUR","GBP","JPY","CHF","AUD","NZD","CAD"])
    if is_fx:
        qty = max(1000, round(qty / 1000) * 1000)
    else:
        qty = round(qty, 2)

    r = await c.place_market_order("{symbol}", action, qty, stop_loss=sl, take_profit=tp)
    if r and r.get("status") == "FILLED":
        fp = r.get("fill_price") or r.get("price") or price
        if not fp or fp <= 0:
            fp = price  # Fallback to bid/ask if MT5 returns 0
        ticket = r.get("ticket", r.get("position_id", 0))
        print(json.dumps({{"success": True, "fill_price": fp, "stop_loss": sl, "take_profit": tp,
            "quantity": qty, "ticket": ticket, "direct_order": True}}))
    elif r and r.get("status") == "REJECTED":
        print(json.dumps({{"success": False, "error": f"rejected: {{r.get('error','')}}"}}))
    else:
        print(json.dumps({{"success": False, "error": f"not filled: {{r}}"}}))
    await c.disconnect()

asyncio.run(do_order())
'''
    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=25, cwd="/app",
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            logger.warning(f"[EMERGENCY ORDER] stderr: {stderr[-300:]}")
        if stdout:
            for line in reversed(stdout.split("\n")):
                line = line.strip()
                if line.startswith("{"):
                    parsed = _json.loads(line)
                    if parsed.get("success"):
                        logger.info(f"[EMERGENCY ORDER] {action} {symbol} FILLED via subprocess")
                        # Track in bot memory
                        import time as _time
                        _fill_price = parsed.get("fill_price", 0)
                        _qty = parsed.get("quantity", 0)
                        _ticket = parsed.get("ticket", 0)
                        _sl = parsed.get("stop_loss", 0)
                        _tp = parsed.get("take_profit", 0)
                        if bot:
                            pos_key = f"{symbol}_{int(_time.time())}"
                            bot._open_positions[pos_key] = {
                                "symbol": symbol,
                                "action": action.upper(),
                                "entry_price": _fill_price,
                                "quantity": _qty,
                                "stop_loss": _sl,
                                "take_profit": _tp,
                                "ticket": _ticket,
                                "position_id": _ticket,
                                "entry_time": datetime.now(timezone.utc).isoformat(),
                                "_opened_ts": _time.time(),
                                "broker": "mt5",
                                "opened_at": datetime.now(timezone.utc).isoformat(),
                                "position_eur": amount_eur,
                                "manual": True,
                            }
                            # IMMEDIATELY write to POSITIONS_FILE so dashboard sees it
                            # (don't wait for event loop / position monitor)
                            try:
                                from app.trading.symbol_mapper import get_leverage
                                _lev = get_leverage(symbol)
                                _notional = _qty * (_fill_price or 1)
                                _margin = _notional / _lev if _lev > 0 else _notional
                                _existing = read_json(POSITIONS_FILE, [])
                                _existing.append({
                                    "symbol": symbol,
                                    "action": action.upper(),
                                    "quantity": _qty,
                                    "entry_price": _fill_price,
                                    "current_price": _fill_price,
                                    "stop_loss": _sl,
                                    "take_profit": _tp,
                                    "pnl": 0.0,
                                    "pnl_percent": 0.0,
                                    "margin": round(_margin, 2),
                                    "exposure": round(_notional, 2),
                                    "leverage_used": f"{_lev}:1",
                                    "broker": "mt5",
                                    "ticket": _ticket,
                                    "market_category": "mt5",
                                    "origin": "manual",
                                })
                                write_json(POSITIONS_FILE, _existing)
                                logger.info(f"[EMERGENCY ORDER] Position written to POSITIONS_FILE immediately")
                            except Exception as _wpe:
                                logger.error(f"[EMERGENCY ORDER] Failed to write POSITIONS_FILE: {_wpe}")
                            # Save position to DB so it survives restarts
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    _save_position_to_db(pos_key, symbol, action.upper(), _fill_price, _qty, _sl, _tp, _ticket, amount_eur),
                                    asyncio.get_event_loop()
                                )
                            except Exception:
                                # Event loop may be blocked, save via subprocess
                                _save_position_db_sync(pos_key, symbol, action.upper(), _fill_price, _qty, _sl, _tp, _ticket, amount_eur)
                    return parsed
        return {"success": False, "error": f"No output. stderr={stderr[-200:]}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "subprocess timeout (25s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _emergency_close_all_positions() -> dict:
    """
    Close ALL open positions via a single subprocess (one MT5 connection).
    Guaranteed to complete in <20s even if main event loop is blocked.
    Returns {"success": bool, "closed": int, "pnl_total": float, "errors": [...]}.
    """
    import subprocess, json as _json
    logger.warning("[EMERGENCY] Spawning subprocess to close ALL positions")

    script = '''
import asyncio, sys, json
sys.path.insert(0, "/app")
from app.trading.mt5_client import MT5Client

async def close_all():
    c = MT5Client()
    ok = await c.connect()
    if not ok:
        print(json.dumps({"success": False, "error": "connect failed"}))
        return
    try:
        positions = await c.get_positions()
        if not positions:
            print(json.dumps({"success": True, "closed": 0, "pnl_total": 0, "results": []}))
            return
        results = []
        total_pnl = 0.0
        # Close in parallel for speed
        async def _close_one(p):
            try:
                r = await c.close_position(p.get("ticket"))
                return {
                    "ticket": p.get("ticket"),
                    "symbol": p.get("symbol", ""),
                    "success": bool(r),
                    "pnl": (r or {}).get("pnl", 0) if r else 0,
                }
            except Exception as e:
                return {
                    "ticket": p.get("ticket"),
                    "symbol": p.get("symbol", ""),
                    "success": False,
                    "error": str(e),
                    "pnl": 0,
                }
        results = await asyncio.gather(*[_close_one(p) for p in positions])
        total_pnl = sum(r.get("pnl", 0) or 0 for r in results if r.get("success"))
        closed = sum(1 for r in results if r.get("success"))
        print(json.dumps({
            "success": True,
            "closed": closed,
            "total": len(positions),
            "pnl_total": total_pnl,
            "results": results,
        }))
    finally:
        await c.disconnect()

asyncio.run(close_all())
'''
    try:
        result = subprocess.run(
            ["python3", "-c", script],
            capture_output=True, text=True, timeout=18,
            cwd="/app",
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            logger.warning(f"[EMERGENCY CLOSE ALL] stderr: {stderr[-300:]}")
        if stdout:
            for line in reversed(stdout.split("\n")):
                line = line.strip()
                if line.startswith("{"):
                    parsed = _json.loads(line)
                    if parsed.get("success"):
                        logger.warning(
                            f"[EMERGENCY CLOSE ALL] Closed {parsed.get('closed', 0)}/{parsed.get('total', 0)} "
                            f"positions — PnL total={parsed.get('pnl_total', 0):.2f}€"
                        )
                    return parsed
        return {"success": False, "error": f"No JSON output. stdout={stdout[-200:]} stderr={stderr[-200:]}"}
    except subprocess.TimeoutExpired:
        logger.error("[EMERGENCY CLOSE ALL] Subprocess timed out (18s)")
        return {"success": False, "error": "subprocess timeout (18s)"}
    except Exception as e:
        logger.error(f"[EMERGENCY CLOSE ALL] Subprocess failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


def _thread_command_poller(loop):
    """
    Command poller running in a SEPARATE THREAD.
    This ensures commands are always picked up even when the asyncio
    event loop is temporarily blocked (legacy safety net from Twisted era).
    """
    logger.info("[IPC] Command poller started in SEPARATE THREAD — polling every 0.5s")
    while not _shutdown:
        try:
            # Clean up completed futures
            done_ids = [k for k, v in _pending_futures.items() if v.done()]
            for k in done_ids:
                _pending_futures.pop(k, None)

            commands = poll_commands()
            for cmd in commands:
                cmd_name = cmd.get("cmd", "")
                cmd_id = cmd.get("id", "")

                # Skip if already being processed
                if cmd_id in _pending_futures:
                    continue

                # Close commands get longer expiry (user may retry)
                _is_close_cmd = cmd_name in ("manual_close", "close_position_direct")
                _max_age = 120 if _is_close_cmd else 30

                # Check if command is too old — skip stale commands
                if time.time() - cmd.get("ts", 0) > _max_age:
                    send_response(cmd_id, False, error="Command expired")
                    continue

                # Quick synchronous commands — handle directly in thread
                params = cmd.get("params", {})

                if cmd_name == "start" and bot:
                    bot._scan_only = False
                    if bot._running:
                        logger.info("[IPC] Bot already running — switched to AUTO mode")
                        send_response(cmd_id, True, data={"status": bot.status})
                    elif not bot.mt5_available:
                        send_response(cmd_id, False, error="MT5 non connecté")
                    else:
                        # Need async — schedule on event loop
                        future = asyncio.run_coroutine_threadsafe(process_command(cmd), loop)
                        try:
                            future.result(timeout=20)
                        except Exception as e:
                            send_response(cmd_id, False, error=f"Timeout: {e}")
                    continue

                elif cmd_name == "scan_only" and bot:
                    bot._scan_only = True
                    if bot._running:
                        logger.info("[IPC] Bot already running — switched to SCAN-ONLY mode")
                        send_response(cmd_id, True, data={"mode": "scan_only", "status": bot.status})
                    elif not bot.mt5_available:
                        send_response(cmd_id, False, error="MT5 non connecté")
                    else:
                        future = asyncio.run_coroutine_threadsafe(process_command(cmd), loop)
                        try:
                            future.result(timeout=20)
                        except Exception as e:
                            send_response(cmd_id, False, error=f"Timeout: {e}")
                    continue

                elif cmd_name == "set_mode" and bot:
                    mode = params.get("mode", "auto")
                    bot._scan_only = (mode == "scan")
                    logger.info(f"[IPC] Mode switched to {'scan-only' if bot._scan_only else 'auto'}")
                    send_response(cmd_id, True, data={"scan_only": bot._scan_only, "status": bot.status})
                    continue

                elif cmd_name == "stop" and bot:
                    # Stop scan loop IMMEDIATELY so no new trades open during close
                    bot._running = False
                    _close_pos = bool(params.get("close_positions", False))
                    logger.info(f"[IPC] Bot stopped via IPC (close_positions={_close_pos})")
                    if _close_pos:
                        # Close ALL positions via subprocess (guaranteed <20s, works during scans)
                        _cr = _emergency_close_all_positions()
                        if _cr.get("success"):
                            # Clear bot tracking so UI reflects closed state
                            _closed_keys = list(bot._open_positions.keys())
                            bot._open_positions.clear()
                            logger.info(f"[IPC STOP] Cleared {len(_closed_keys)} positions from bot tracking")
                            # Update realized PnL
                            try:
                                _pnl_total = float(_cr.get("pnl_total", 0) or 0)
                                if bot.risk_manager and _pnl_total != 0:
                                    bot.risk_manager._daily_pnl += _pnl_total
                            except Exception:
                                pass
                            send_response(cmd_id, True, data={
                                "status": bot.status,
                                "closed": _cr.get("closed", 0),
                                "total": _cr.get("total", 0),
                                "pnl_total": _cr.get("pnl_total", 0),
                            })
                        else:
                            # Positions close failed but bot is still stopped
                            send_response(cmd_id, False, error=f"Bot arrêté mais échec fermeture positions: {_cr.get('error', 'unknown')}")
                    else:
                        send_response(cmd_id, True, data={"status": bot.status})
                    continue

                elif cmd_name == "emergency_stop" and bot:
                    bot._running = False
                    logger.warning("[IPC] EMERGENCY STOP via IPC — closing all positions")
                    # Emergency stop ALWAYS closes positions via subprocess
                    _cr = _emergency_close_all_positions()
                    _closed_keys = list(bot._open_positions.keys())
                    bot._open_positions.clear()
                    try:
                        _pnl_total = float(_cr.get("pnl_total", 0) or 0)
                        if bot.risk_manager and _pnl_total != 0:
                            bot.risk_manager._daily_pnl += _pnl_total
                    except Exception:
                        pass
                    logger.warning(
                        f"[IPC EMERGENCY] Closed {_cr.get('closed', 0)}/{_cr.get('total', 0)} — "
                        f"PnL={_cr.get('pnl_total', 0):.2f}€ — cleared {len(_closed_keys)} tracked positions"
                    )
                    send_response(cmd_id, True, data={
                        "message": "Emergency stop executed",
                        "closed": _cr.get("closed", 0),
                        "total": _cr.get("total", 0),
                        "pnl_total": _cr.get("pnl_total", 0),
                    })
                    continue

                # ═══ MANUAL ORDER — ALWAYS use subprocess (guaranteed, no event loop dependency) ═══
                elif cmd_name == "manual_order" and bot:
                    symbol = params.get("symbol", "")
                    action = params.get("action", "buy")
                    amount_eur = params.get("amount_eur", 20)
                    logger.info(f"[IPC] MANUAL ORDER {action} {symbol} {amount_eur}€ — using subprocess (guaranteed)")

                    try:
                        _order_result = _emergency_manual_order(symbol, action, amount_eur)
                        if _order_result and _order_result.get("success"):
                            send_response(cmd_id, True, data=_order_result)
                        else:
                            send_response(cmd_id, False, error=_order_result.get("error", f"Failed to place order"))
                    except Exception as _oe:
                        logger.error(f"[IPC] MANUAL ORDER {symbol} failed: {_oe}")
                        send_response(cmd_id, False, error=str(_oe))
                    continue

                # ═══ URGENT CLOSE — try main event loop first, then dedicated ═══
                elif cmd_name in ("manual_close", "close_position_direct") and bot:
                    symbol = params.get("symbol", "")
                    # ALWAYS use subprocess for close — never wait for event loop
                    # This guarantees close works even during 5-min scans
                    logger.info(f"[IPC] CLOSE {symbol} — using subprocess (guaranteed)")
                    try:
                        _close_result = _emergency_close_position(symbol)
                        if _close_result and _close_result.get("success"):
                            _pnl = _close_result.get("pnl", 0)
                            # Also remove from bot tracking
                            _removed_key = None
                            _removed_pos = None
                            for _pk, _pv in list(bot._open_positions.items()):
                                if _pv.get("symbol") == symbol or _pk == symbol:
                                    _removed_key = _pk
                                    _removed_pos = _pv
                                    break
                            if _removed_key:
                                bot._open_positions.pop(_removed_key, None)
                                logger.info(f"[IPC] {symbol} removed from bot tracking")
                                # Schedule DB cleanup on event loop (non-blocking, best effort)
                                try:
                                    asyncio.run_coroutine_threadsafe(
                                        bot._remove_position_db(_removed_key), loop
                                    )
                                except Exception:
                                    pass

                            # Save trade to DB (best effort, non-blocking)
                            async def _save_emergency_trade():
                                try:
                                    from app.models.trade import Trade, TradeStatus, TradeSide
                                    from app.database import async_session
                                    from datetime import datetime, timezone

                                    _entry = _removed_pos.get("entry_price", 0) if _removed_pos else 0
                                    _qty = _removed_pos.get("quantity", 0) if _removed_pos else 0
                                    _side = _removed_pos.get("direction", "BUY") if _removed_pos else "BUY"
                                    _sl = _removed_pos.get("stop_loss") if _removed_pos else None
                                    _tp = _removed_pos.get("take_profit") if _removed_pos else None
                                    _margin = _removed_pos.get("margin", 1) if _removed_pos else 1

                                    async with async_session() as session:
                                        trade = Trade(
                                            symbol=symbol,
                                            name=symbol,
                                            side=TradeSide.BUY if _side == "BUY" else TradeSide.SELL,
                                            status=TradeStatus.CLOSED,
                                            entry_price=_entry,
                                            quantity=_qty,
                                            entry_amount=_entry * _qty,
                                            exit_price=0,
                                            exit_time=datetime.now(timezone.utc),
                                            exit_reason="emergency_close",
                                            stop_loss=_sl,
                                            take_profit=_tp,
                                            pnl=round(_pnl, 2),
                                            pnl_percent=round((_pnl / (_margin or 1)) * 100, 2),
                                            market=_removed_pos.get("market_category", "FOREX") if _removed_pos else "FOREX",
                                            asset_type=_removed_pos.get("asset_type", "forex") if _removed_pos else "forex",
                                        )
                                        session.add(trade)
                                        await session.commit()
                                        logger.info(f"[EMERGENCY] Trade saved: {symbol} PnL={_pnl:.2f}")
                                except Exception as _te:
                                    logger.error(f"[EMERGENCY] Failed to save trade: {_te}")

                            try:
                                asyncio.run_coroutine_threadsafe(_save_emergency_trade(), loop)
                            except Exception:
                                pass

                            # Update daily PnL
                            if bot.risk_manager and _pnl != 0:
                                bot.risk_manager._daily_pnl += _pnl

                            send_response(cmd_id, True, data={
                                "pnl": _pnl,
                                "symbol": symbol,
                                "broker": "mt5",
                                "direct_close": True,
                            })
                        else:
                            send_response(cmd_id, False, error=_close_result.get("error", f"Failed to close {symbol}"))
                    except Exception as _ce:
                        logger.error(f"[IPC] URGENT CLOSE {symbol} failed: {_ce}")
                        send_response(cmd_id, False, error=str(_ce))
                    continue

                # All other commands — schedule on event loop
                _is_long_cmd = cmd_name in ("manual_close", "close_position_direct", "manual_order")
                future = asyncio.run_coroutine_threadsafe(process_command(cmd), loop)

                if _is_long_cmd:
                    # Non-blocking: track future and let poller continue
                    _pending_futures[cmd_id] = future
                    logger.info(f"[IPC] Command {cmd_name} dispatched async (non-blocking)")
                else:
                    # Short commands: wait up to 20s
                    try:
                        future.result(timeout=20)
                    except Exception as e:
                        logger.error(f"[IPC] Command {cmd_name} async timeout: {e}")
                        try:
                            send_response(cmd_id, False, error=f"Timeout: {e}")
                        except Exception:
                            pass

        except Exception as e:
            logger.error(f"[IPC] Command poller error: {e}")
        time.sleep(0.5)


def _thread_state_writer():
    """
    State writer running in a SEPARATE THREAD.
    Writes all state files every 3s, never blocked by event loop.
    """
    logger.info("[IPC] State writer started in SEPARATE THREAD — writing every 3s")
    event_cleanup_ts = time.time()
    while not _shutdown:
        try:
            _write_all_state_files()
        except Exception as e:
            logger.error(f"[IPC] State writer error: {e}")

        # Cleanup events file every 5 minutes
        if time.time() - event_cleanup_ts > 300:
            truncate_events_file()
            event_cleanup_ts = time.time()

        time.sleep(3)


async def main():
    """Main entry point for the bot process."""
    global bot, _shutdown

    setup_logging()
    logger.info("=" * 60)
    logger.info("BOT PROCESS starting — isolated from API (two-process mode)")
    logger.info("=" * 60)

    # Init database
    await init_db()

    # Cleanup stale IPC files from previous runs
    cleanup_stale_commands()

    # ── MT5 bridge (Fusion Markets) ────────────────────────────────────
    # Bot talks to the MT5 terminal via ZeroMQ exposed by the EA running
    # inside the mt5-bridge container. ZMQ is async-friendly (pyzmq.asyncio)
    # so a single MT5Client is shared across all roles — the three bot
    # slots (trading / data / dash) just reference the same instance.
    from app.trading.mt5_client import MT5Client
    from app.trading.bot import TradingBot

    mt5_client = None
    mt5_dash_client = None
    mt5_balance = 0

    if settings.mt5_enabled:
        # ── Scanner / trading client (consumes REQ lock for candles + orders)
        mt5_client = MT5Client(
            pub_endpoint=settings.mt5_pub_endpoint,
            rep_endpoint=settings.mt5_rep_endpoint,
            rpc_timeout_s=settings.mt5_rpc_timeout_s,
        )
        mt5_ok = await mt5_client.connect()
        if mt5_ok:
            mt5_summary = await mt5_client.get_account_summary()
            mt5_balance = mt5_summary.get("balance", 0)
            logger.info(f"[MT5] Fusion Markets connected — Balance: {mt5_balance:.2f}€")
        else:
            logger.error("[MT5] Fusion Markets connection FAILED")
            mt5_client = None

        # ── Dashboard client (dedicated socket REQ for positions+equity every 10s)
        # Distinct instance → own ZMQ REQ socket + own asyncio.Lock → not starved
        # by the scanner's bulk candle RPCs (critical for live progress bar / trail).
        if mt5_client:
            mt5_dash_client = MT5Client(
                pub_endpoint=settings.mt5_pub_endpoint,
                rep_endpoint=settings.mt5_rep_endpoint,
                rpc_timeout_s=settings.mt5_rpc_timeout_s,
            )
            dash_ok = await mt5_dash_client.connect()
            if dash_ok:
                logger.info("[MT5] Dashboard client connected (dedicated REQ socket)")
            else:
                logger.warning("[MT5] Dashboard client failed — falling back to shared client")
                mt5_dash_client = mt5_client
    else:
        logger.warning("MT5 disabled (set MT5_ENABLED=true)")

    # Init trading bot — scanner client for candles+orders, dedicated dash client
    # for positions/equity. Separate ZMQ REQ sockets avoid lock starvation.
    bot = TradingBot(
        mt5_client=mt5_client,
        mt5_data_client=mt5_client,
        mt5_dash_client=mt5_dash_client or mt5_client,
    )

    # Seed risk_manager capital from MT5 balance IMMEDIATELY
    if mt5_client and mt5_client.is_connected:
        try:
            if mt5_balance and mt5_balance > 0:
                bot.risk_manager.capital = mt5_balance
                logger.info(f"[INIT] risk_manager.capital seeded from MT5: {mt5_balance:.2f}€")
        except Exception as _se:
            logger.warning(f"[INIT] Failed to seed capital: {_se}")

    # Register IPC event broadcaster (replaces WebSocket broadcast)
    async def ipc_broadcast(event_type: str, data: dict):
        append_event(event_type, data)
    bot.on_event(ipc_broadcast)

    if mt5_client and mt5_client.is_connected:
        logger.info("Bot READY — Fusion Markets MT5 connected (ZMQ bridge)")
    else:
        logger.error("Bot NOT ready — MT5 not connected")

    # Write initial state
    _write_all_state_files()

    # Start IPC threads — these run independently of the asyncio event loop
    # so they NEVER freeze even if a long RPC briefly stalls the event loop
    import threading
    loop = asyncio.get_running_loop()

    poller_thread = threading.Thread(
        target=_thread_command_poller, args=(loop,),
        daemon=True, name="ipc-poller"
    )
    poller_thread.start()

    writer_thread = threading.Thread(
        target=_thread_state_writer,
        daemon=True, name="ipc-writer"
    )
    writer_thread.start()

    logger.info("[BOT] Waiting for start command from API (POST /api/bot/start)")
    logger.info(f"[IPC] Shared directory: {IPC_DIR}")

    # Keep running until shutdown
    try:
        while not _shutdown:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Bot process shutting down...")
        _shutdown = True
        if bot and bot.is_running:
            await bot.stop(close_positions=False)
        if mt5_client:
            await mt5_client.disconnect()
        logger.info("Bot process stopped.")


def _signal_handler(sig, frame):
    global _shutdown
    _shutdown = True


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    asyncio.run(main())
