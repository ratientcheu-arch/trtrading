"""
MT5 broker client — the ONLY broker client in this project.

Architecture:
    - Python (this file) talks to MT5 terminal via ZeroMQ sockets exposed by
      the MQL5 EA `ZmqBridge.mq5` running inside a Wine/Docker container.
    - REQ (tcp://…:5556)  → RPC calls (place_order, get_positions, modify, …)
    - SUB (tcp://…:5555)  → live ticks broadcast by the EA (bid/ask/time)

Protocol (pipe-delimited text; no JSON dep on the MQL5 side):
    Request : "method:k1=v1;k2=v2;..."
    Response: "OK:k1=v1;k2=v2[|record2|record3…]" | "ERR:reason"
    Tick    : "TICK|SYMBOL|bid|ask|time_msc"

Key design notes:
    - Symbol normalisation: the bot uses "EUR/USD" in-memory, MT5 uses
      "EURUSD". We accept both on input and normalise to MT5 form on the wire.
    - `_resolve_symbol_id(sym)` returns the MT5 symbol name (string): there
      is no integer proto ID concept in MT5. bot.py call sites use the
      return value as a hashable key into `_spot_prices`, which works.
    - Hedging / pyramiding guards are enforced server-side in the EA
      (authoritative). This client adds a best-effort preflight check so
      the bot sees clean errors fast, without a network round-trip for
      obvious violations.
    - Mandatory SL/TP is enforced client-side AND server-side.
    - PnL values come straight from MT5 (already account-currency) — no
      FX cross conversion needed.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

import zmq
import zmq.asyncio

from app.trading.indicators import Candle

logger = logging.getLogger(__name__)


class MT5Error(Exception):
    """Raised when the EA returns ERR: or the RPC times out / disconnects."""


# Fusion Markets MT5 broker symbols — generic names → actual instrument codes
# Confirmed available via REP rpc 'symbols' on 2026-04-15
BROKER_SYMBOL_MAP: dict[str, str] = {
    # Indices
    "DAX40": "GER40",
    "SP500": "US500",
    "NASDAQ": "NAS100",
    "CAC40": "FRA40",
    "NKY": "JPN225",
    "HK50": "HK50",         # same name on Fusion
    "AUS200": "AUS200",     # same name on Fusion
    "UK100": "UK100",       # same name on Fusion
    # Commodities
    "OIL_CRUDE": "XTIUSD",  # WTI Crude
    "OIL_BRENT": "XBRUSD",  # Brent Crude
    "NATURALGAS": "XNGUSD",
    "GOLD": "XAUUSD",
    "SILVER": "XAGUSD",
    "COPPER": "XCUUSD",
}


def _to_mt5_symbol(s: str) -> str:
    """Normalise symbol to MT5 form.
    - Forex: `EUR/USD` → `EURUSD`
    - Indices/Commodities: apply BROKER_SYMBOL_MAP (DAX40 → GER40, etc.)
    """
    key = s.replace("/", "").strip()
    return BROKER_SYMBOL_MAP.get(key, key)


def _from_mt5_symbol(s: str, template: str | None = None) -> str:
    """Return the symbol in the convention expected by callers.

    If a template (e.g. "EUR/USD") is provided, we keep the bot's slash form
    so existing bot code stays compatible. Otherwise we return the raw MT5 form.
    """
    if template and "/" in template:
        # Attempt to restore slash by matching the first 3 letters
        if len(s) >= 6:
            return s[:3] + "/" + s[3:]
    return s


class MT5Client:
    # ── Life-cycle ────────────────────────────────────────────────────────
    def __init__(
        self,
        pub_endpoint: str | None = None,
        rep_endpoint: str | None = None,
        rpc_timeout_s: float = 10.0,
    ):
        self._pub_ep = pub_endpoint or os.environ.get(
            "MT5_PUB_ENDPOINT", "tcp://127.0.0.1:5555"
        )
        self._rep_ep = rep_endpoint or os.environ.get(
            "MT5_REP_ENDPOINT", "tcp://127.0.0.1:5556"
        )
        self._rpc_timeout_s = rpc_timeout_s

        self._ctx: zmq.asyncio.Context | None = None
        self._sub: zmq.asyncio.Socket | None = None
        self._req: zmq.asyncio.Socket | None = None
        self._req_lock = asyncio.Lock()
        self._sub_task: asyncio.Task | None = None
        self._connected = False

        # Ticks cache — keyed by MT5 symbol string. Values: {bid, ask, time_ms}.
        self._spot_prices: dict[str, dict] = {}
        self._spot_subscriptions: set[str] = set()

        # Cache for account + symbol digits
        self._account: dict = {}
        self._balance: float = 0.0
        self._leverage: int = 500
        self._currency: str = "EUR"
        self._summary_cache_time: float = 0.0
        self._cached_summary: dict | None = None

    # ── Properties ────────────────────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Connect / disconnect ──────────────────────────────────────────────
    async def connect(self, skip_symbols: bool = False) -> bool:
        """Open ZMQ sockets, handshake with EA, start tick loop.

        `skip_symbols` is a noop here (kept for legacy call-site parity: MT5
        symbols are fetched lazily on subscribe/quote requests).
        """
        try:
            self._ctx = zmq.asyncio.Context.instance()
            self._req = self._ctx.socket(zmq.REQ)
            self._req.setsockopt(zmq.LINGER, 0)
            self._req.setsockopt(zmq.RCVTIMEO, int(self._rpc_timeout_s * 1000))
            self._req.setsockopt(zmq.SNDTIMEO, int(self._rpc_timeout_s * 1000))
            self._req.connect(self._rep_ep)

            self._sub = self._ctx.socket(zmq.SUB)
            self._sub.setsockopt(zmq.LINGER, 0)
            self._sub.setsockopt(zmq.RCVHWM, 10_000)
            self._sub.setsockopt_string(zmq.SUBSCRIBE, "TICK|")  # all ticks
            self._sub.connect(self._pub_ep)

            # Handshake
            pong = await self._rpc("ping")
            if pong.get("pong") != "true":
                logger.error("MT5 ping failed: %s", pong)
                return False

            # Load account info
            await self._load_account_info()

            # Start SUB loop
            self._sub_task = asyncio.create_task(self._sub_loop(), name="mt5-sub-loop")
            self._connected = True
            logger.info(
                "MT5Client connected: login=%s balance=%.2f %s leverage=1:%d",
                self._account.get("login", "?"),
                self._balance,
                self._currency,
                self._leverage,
            )
            return True
        except Exception as e:
            logger.error("MT5Client.connect failed: %s", e)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        self._connected = False
        if self._sub_task:
            self._sub_task.cancel()
            try:
                await self._sub_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sub_task = None
        for s in (self._req, self._sub):
            if s is not None:
                try:
                    s.close(linger=0)
                except Exception:
                    pass
        self._req = self._sub = None

    async def reconnect(self) -> bool:
        """Close + re-open sockets. Used by bot watchdog."""
        logger.info("MT5Client: reconnecting…")
        await self.disconnect()
        await asyncio.sleep(0.5)
        return await self.connect()

    # ── RPC transport ─────────────────────────────────────────────────────
    async def _rpc(self, method: str, **params) -> dict:
        """Send a RPC via REQ/REP and return the parsed dict.

        On error (ERR: response or timeout), raises MT5Error. On timeout we
        recycle the REQ socket because the REQ/REP state machine is toast.
        """
        if self._ctx is None or self._req is None:
            raise MT5Error("not_connected")

        # Build request: "method:k1=v1;k2=v2"
        kv = ";".join(f"{k}={v}" for k, v in params.items())
        req = f"{method}:{kv}" if kv else f"{method}:"

        async with self._req_lock:
            try:
                await asyncio.wait_for(
                    self._req.send_string(req), timeout=self._rpc_timeout_s
                )
                resp: str = await asyncio.wait_for(
                    self._req.recv_string(), timeout=self._rpc_timeout_s
                )
            except (asyncio.TimeoutError, zmq.Again, zmq.ZMQError) as e:
                # Recycle REQ (REQ state machine forbids another send before recv)
                logger.error("MT5 RPC timeout/socket error on %s: %s", method, e)
                try:
                    self._req.close(linger=0)
                except Exception:
                    pass
                self._req = self._ctx.socket(zmq.REQ)
                self._req.setsockopt(zmq.LINGER, 0)
                self._req.setsockopt(zmq.RCVTIMEO, int(self._rpc_timeout_s * 1000))
                self._req.setsockopt(zmq.SNDTIMEO, int(self._rpc_timeout_s * 1000))
                self._req.connect(self._rep_ep)
                raise MT5Error(f"rpc_timeout:{method}")

        return self._parse_response(resp)

    @staticmethod
    def _parse_kv(s: str) -> dict:
        out: dict[str, str] = {}
        if not s:
            return out
        for pair in s.split(";"):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            out[k.strip()] = v.strip()
        return out

    @classmethod
    def _parse_response(cls, resp: str) -> dict:
        if not resp:
            raise MT5Error("empty_response")
        if resp.startswith("ERR:"):
            raise MT5Error(resp[4:])
        if not resp.startswith("OK:"):
            raise MT5Error(f"unexpected_response:{resp[:120]}")
        body = resp[3:]
        # Multi-record responses are separated by `|`. First record = head.
        records = body.split("|")
        head = cls._parse_kv(records[0])
        if len(records) > 1:
            head["items"] = [cls._parse_kv(r) for r in records[1:]]
        return head

    # ── SUB loop: update tick cache ───────────────────────────────────────
    async def _sub_loop(self) -> None:
        logger.info("MT5 SUB loop started on %s", self._pub_ep)
        assert self._sub is not None
        while True:
            try:
                msg: str = await self._sub.recv_string()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("MT5 sub recv error: %s", e)
                await asyncio.sleep(0.5)
                continue
            # Format: "TICK|SYMBOL|bid|ask|time_ms"
            parts = msg.split("|", 4)
            if len(parts) < 5 or parts[0] != "TICK":
                continue
            try:
                sym = parts[1]
                bid = float(parts[2])
                ask = float(parts[3])
                t_ms = int(parts[4])
            except (ValueError, IndexError):
                continue
            self._spot_prices[sym] = {"bid": bid, "ask": ask, "time_ms": t_ms}

    # ── Account ───────────────────────────────────────────────────────────
    async def _load_account_info(self) -> None:
        d = await self._rpc("account")
        self._account = d
        self._balance = float(d.get("balance", 0))
        self._leverage = int(d.get("leverage", 500))
        self._currency = d.get("currency", "EUR")

    async def get_account_summary(self) -> dict:
        """Return a dict of balance/equity/margin/free_margin/currency."""
        now = time.time()
        if now - self._summary_cache_time < 5 and self._cached_summary:
            return self._cached_summary
        try:
            d = await self._rpc("account")
            balance = float(d.get("balance", 0))
            equity = float(d.get("equity", balance))
            margin = float(d.get("margin", 0))
            self._cached_summary = {
                "balance": balance,
                "net_liquidation": equity,
                "unrealized_pnl": float(d.get("profit", 0)),
                "buying_power": float(d.get("free_margin", 0)),
                "leverage": int(d.get("leverage", self._leverage)),
                "currency": d.get("currency", self._currency),
                "margin_used": margin,
                "margin_level": float(d.get("margin_level", 0)),
            }
            self._summary_cache_time = now
            self._balance = balance
            return self._cached_summary
        except MT5Error as e:
            logger.error("MT5 account_summary error: %s", e)
            return self._cached_summary or {}

    async def get_net_liquidation(self) -> float:
        s = await self.get_account_summary()
        return float(s.get("net_liquidation", 0.0))

    async def refresh_token(self) -> bool:
        """No-op for MT5 (terminal handles session). Kept for API parity."""
        return True

    # ── Positions ─────────────────────────────────────────────────────────
    async def get_positions(self) -> list[dict]:
        """Return list of position dicts (one per open MT5 position).

        Key fields consumed by bot.py: ticket, position_id, symbol, side,
        direction, quantity, entry_price, current_price, unrealized_pnl,
        stop_loss, take_profit, open_timestamp.
        """
        if not self._connected:
            return []
        try:
            d = await self._rpc("positions")
        except MT5Error as e:
            logger.error("MT5 get_positions error: %s", e)
            return []

        items = d.get("items", [])
        out: list[dict] = []
        for it in items:
            try:
                ticket = int(it["ticket"])
                symbol = it["symbol"]
                side = it["side"].upper()
                volume = float(it["volume"])
                price = float(it["price"])
                sl = float(it.get("sl", 0))
                tp = float(it.get("tp", 0))
                pnl = float(it.get("pnl", 0))
                swap = float(it.get("swap", 0))
                open_ts = int(it.get("time", 0))
            except (KeyError, ValueError) as e:
                logger.warning("MT5 position parse error: %s item=%s", e, it)
                continue

            spot = self._spot_prices.get(symbol, {})
            current_bid = spot.get("bid", price)
            current_ask = spot.get("ask", price)
            current_price = current_bid if side == "BUY" else current_ask

            # MT5 pnl already in account currency (EUR). Trust it.
            out.append({
                "symbol": symbol,            # "EURUSD" (no slash)
                "mt5_symbol": symbol,
                "ticket": ticket,
                "position_id": ticket,
                "quantity": volume,          # in LOTS (MT5 native)
                "volume_lots": volume,
                "entry_price": price,
                "current_price": current_price,
                "direction": side,
                "side": side,
                "unrealized_pnl": pnl + swap,
                "swap": swap,
                "commission": 0.0,
                "stop_loss": sl if sl > 0 else None,
                "take_profit": tp if tp > 0 else None,
                "open_time": str(open_ts),
                "open_timestamp": open_ts,
                "broker": "mt5",
            })
        return out

    async def get_deals_by_position(self, position_id: int) -> list[dict]:
        if not self._connected:
            return []
        try:
            d = await self._rpc("deals_by_pos", position_id=int(position_id))
        except MT5Error as e:
            logger.error("MT5 deals_by_pos(%s) error: %s", position_id, e)
            return []

        items = d.get("items", [])
        deals: list[dict] = []
        for it in items:
            try:
                volume = float(it.get("volume", 0))
                price = float(it.get("price", 0))
                profit = float(it.get("profit", 0))
                commission = float(it.get("commission", 0))
                swap = float(it.get("swap", 0))
                deal_type = int(it.get("type", 0))  # 0 = buy, 1 = sell
                entry = int(it.get("entry", 0))     # 0 in, 1 out, 2 inout
            except ValueError:
                continue
            deals.append({
                "deal_id": int(it.get("deal", 0)),
                "order_id": int(it.get("order", 0)),
                "position_id": int(it.get("ticket_pos", position_id)),
                "volume": volume,
                "symbol": it.get("symbol", ""),
                "execution_price": price,
                "execution_timestamp": int(it.get("time", 0)) * 1000,  # ms
                "trade_side": "BUY" if deal_type == 0 else "SELL",
                "commission": commission,
                "swap": swap,
                "is_close": entry == 1,
                "gross_profit": profit if entry == 1 else 0.0,
            })
        return deals

    async def fetch_deal_list(
        self, from_ts: float, to_ts: float, max_rows: int = 1000
    ) -> list[dict]:
        if not self._connected:
            return []
        try:
            d = await self._rpc(
                "deals",
                from_ts=int(from_ts),
                to_ts=int(to_ts),
                max_rows=int(max_rows),
            )
        except MT5Error as e:
            logger.error("MT5 fetch_deal_list error: %s", e)
            return []
        out: list[dict] = []
        for it in d.get("items", []):
            try:
                out.append({
                    "deal_id": int(it.get("deal", 0)),
                    "position_id": int(it.get("pos", 0)),
                    "symbol": it.get("symbol", ""),
                    "volume": float(it.get("volume", 0)),
                    "execution_price": float(it.get("price", 0)),
                    "execution_timestamp": int(it.get("time", 0)) * 1000,
                    "trade_side": "BUY" if int(it.get("type", 0)) == 0 else "SELL",
                    "is_close": int(it.get("entry", 0)) == 1,
                    "gross_profit": float(it.get("profit", 0)),
                })
            except ValueError:
                continue
        return out

    # ── Market data ───────────────────────────────────────────────────────
    async def subscribe_spots(self, symbol: str) -> bool:
        sym = _to_mt5_symbol(symbol)
        if sym in self._spot_subscriptions:
            return True
        try:
            await self._rpc("subscribe", symbol=sym)
            self._spot_subscriptions.add(sym)
            return True
        except MT5Error as e:
            logger.error("MT5 subscribe_spots(%s) error: %s", symbol, e)
            return False

    async def get_realtime_quote(self, symbol: str) -> Optional[dict]:
        sym = _to_mt5_symbol(symbol)
        # Auto-subscribe if not already
        if sym not in self._spot_subscriptions:
            await self.subscribe_spots(sym)
            # brief wait for first tick from SUB loop
            for _ in range(10):
                if sym in self._spot_prices:
                    break
                await asyncio.sleep(0.05)

        spot = self._spot_prices.get(sym)
        if spot is None:
            # Fall back to explicit RPC (forces EA to refresh)
            try:
                d = await self._rpc("quote", symbol=sym)
                bid = float(d.get("bid", 0))
                ask = float(d.get("ask", 0))
                if bid == 0 and ask == 0:
                    return None
                self._spot_prices[sym] = {
                    "bid": bid, "ask": ask, "time_ms": int(d.get("time_ms", 0))
                }
                spot = self._spot_prices[sym]
            except MT5Error:
                return None

        bid = spot["bid"]
        ask = spot["ask"]
        if bid == 0 and ask == 0:
            return None
        mid = (bid + ask) / 2
        return {
            "symbol": symbol,
            "price": mid,
            "bid": bid,
            "ask": ask,
            "last": mid,
            "volume": 0,
            "high": 0,
            "low": 0,
            "close": 0,
            "change": 0,
            "change_percent": 0,
            "market_status": "OPEN",
        }

    async def get_historical_candles(
        self,
        symbol: str,
        duration: str = "6 M",
        bar_size: str = "1 day",
    ) -> list[Candle]:
        """Translate a legacy duration/bar_size pair to an MT5 timeframe + count."""
        tf, count = self._translate_bars_request(duration, bar_size)
        try:
            d = await self._rpc("bars", symbol=_to_mt5_symbol(symbol),
                                timeframe=tf, count=count)
        except MT5Error as e:
            logger.error("MT5 get_historical_candles(%s) error: %s", symbol, e)
            return []
        out: list[Candle] = []
        for it in d.get("items", []):
            try:
                out.append(Candle(
                    timestamp=int(it["t"]),
                    open=float(it["o"]),
                    high=float(it["h"]),
                    low=float(it["l"]),
                    close=float(it["c"]),
                    volume=float(it.get("v", 0)),
                ))
            except (KeyError, ValueError):
                continue
        return out

    @staticmethod
    def _translate_bars_request(duration: str, bar_size: str) -> tuple[str, int]:
        # Bar size mapping
        bs = (bar_size or "").strip().lower()
        if "1 day" in bs or "d1" in bs:
            tf = "D1"; per_day = 1
        elif "4 hour" in bs or "h4" in bs:
            tf = "H4"; per_day = 6
        elif "1 hour" in bs or "h1" in bs:
            tf = "H1"; per_day = 24
        elif "30" in bs:
            tf = "M30"; per_day = 48
        elif "15" in bs:
            tf = "M15"; per_day = 96
        elif "5 " in bs or "m5" in bs:
            tf = "M5"; per_day = 288
        else:
            tf = "M1"; per_day = 1440

        # Duration mapping
        d = (duration or "").strip().upper()
        days = 180
        try:
            if d.endswith(" Y"):
                days = int(d.split()[0]) * 365
            elif d.endswith(" M"):
                days = int(d.split()[0]) * 30
            elif d.endswith(" W"):
                days = int(d.split()[0]) * 7
            elif d.endswith(" D"):
                days = int(d.split()[0])
        except Exception:
            days = 180

        count = min(5000, max(50, days * per_day))
        return tf, count

    # ── Orders ────────────────────────────────────────────────────────────
    async def place_market_order(
        self,
        symbol: str,
        action: str,  # "BUY" or "SELL"
        quantity: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[dict]:
        """Place a market order with mandatory SL/TP.

        `quantity` follows the bot's convention: for forex, it's expressed in
        physical currency units (1 lot = 100_000). We convert to MT5 lots
        (0.01 min) here. For indices/commodities the conversion is 1:1 with
        the broker's lot step.
        """
        if not self._connected:
            logger.error("MT5 place_market_order: not connected")
            return None
        if stop_loss is None or take_profit is None:
            logger.critical(
                "MT5 REJECTING order without SL/TP — %s %s %s (SL=%s TP=%s)",
                symbol, action, quantity, stop_loss, take_profit,
            )
            return None

        sym = _to_mt5_symbol(symbol)
        side = action.upper()
        if side not in ("BUY", "SELL"):
            logger.error("MT5: invalid side %s", action)
            return None

        # qty → lots. For forex the bot passes units (e.g. 1000 = 0.01 lot).
        # MT5 lot step is typically 0.01 → derive lots by dividing by 100_000.
        # Indices/commodities typically have 0.1 or 1.0 step; we round to 2
        # decimals and let the broker clamp to step server-side.
        lots = self._qty_to_lots(sym, quantity)
        if lots <= 0.0:
            logger.error("MT5: qty_to_lots(%s, %s) = %s ≤ 0", symbol, quantity, lots)
            return None

        # Preflight: hedging / pyramid check (soft; EA is authoritative).
        positions = await self.get_positions()
        veto = self._preflight_hedging_pyramid(sym, side, positions)
        if veto:
            logger.warning("MT5 place_market_order vetoed client-side: %s", veto)
            return None

        try:
            d = await self._rpc(
                "place_order",
                symbol=sym,
                side=side.lower(),
                volume=f"{lots:.2f}",
                sl=f"{float(stop_loss):.5f}",
                tp=f"{float(take_profit):.5f}",
                comment="bot",
            )
        except MT5Error as e:
            logger.error("MT5 place_market_order(%s %s %s lots=%s) ERR: %s",
                         symbol, side, quantity, lots, e)
            return None

        ticket = int(d.get("ticket", 0))
        fill_price = float(d.get("price", 0))
        filled_volume = float(d.get("volume", lots))
        logger.info(
            "MT5 %s %s: filled @ %s (ticket=%s lots=%s)",
            side, symbol, fill_price, ticket, filled_volume,
        )
        return {
            "order_id": str(ticket),
            "position_id": ticket,
            "ticket": ticket,
            "fill_price": fill_price,
            "fill_qty": quantity,
            "price": fill_price,
            "size": quantity,
            "symbol": symbol,
            "status": "FILLED",
            "broker": "mt5",
        }

    @staticmethod
    def _qty_to_lots(symbol: str, qty: float) -> float:
        """Convert bot's physical-units qty to MT5 lots.

        - Forex majors/minors: 1 lot = 100_000 units → lots = qty / 100_000.
        - Indices / commodities / crypto: bot already passes "lots-ish"
          values (e.g. 0.1 for CAC40). We clamp to min 0.01 and round to 2dp.
        """
        sym = symbol.upper()
        is_forex = (len(sym) == 6 and sym.isalpha())
        if is_forex:
            lots = qty / 100_000.0
        else:
            lots = float(qty)
        # Round to broker's typical 0.01 step
        lots = round(lots, 2)
        if lots < 0.01:
            lots = 0.01
        return lots

    @staticmethod
    def _preflight_hedging_pyramid(
        sym: str, side: str, positions: list[dict]
    ) -> str | None:
        """Mirror of the EA's check_hedging_pyramid — return error reason or None.

        This is a fail-fast client-side check. The EA is authoritative and
        will reject again if the state changed between here and there.
        """
        same_side = [p for p in positions
                     if p.get("symbol") == sym and p.get("side") == side]
        opp_side = [p for p in positions
                    if p.get("symbol") == sym and p.get("side") != side
                    and p.get("side") in ("BUY", "SELL")]
        if opp_side:
            return "hedging_blocked:opposite_position_exists"
        if not same_side:
            return None  # first position on this symbol-side
        # NB: we don't enforce the 5% TP progress rule client-side: needs live
        # price → would duplicate EA logic. Rely on EA to return ERR: if not yet
        # at 5%. Here we only cap the count to give a nicer error.
        if len(same_side) >= 3:
            return "pyramid_blocked:max_3_reached"
        return None

    async def close_position(self, symbol_or_ticket) -> Optional[dict]:
        """Close by ticket (int) or by symbol (str — closes the first match)."""
        if isinstance(symbol_or_ticket, (int,)) or (
            isinstance(symbol_or_ticket, str) and symbol_or_ticket.isdigit()
        ):
            return await self.close_position_by_ticket(int(symbol_or_ticket))
        # Symbol: close all positions on that symbol
        sym = _to_mt5_symbol(str(symbol_or_ticket))
        try:
            d = await self._rpc("close_all", symbol=sym)
            return {
                "status": "CLOSED",
                "symbol": sym,
                "closed": int(d.get("closed", 0)),
                "failed": int(d.get("failed", 0)),
                "broker": "mt5",
            }
        except MT5Error as e:
            logger.error("MT5 close_position(%s) error: %s", symbol_or_ticket, e)
            return None

    async def close_position_by_ticket(self, ticket: int) -> Optional[dict]:
        try:
            d = await self._rpc("close", ticket=int(ticket))
        except MT5Error as e:
            logger.error("MT5 close_position_by_ticket(%s) error: %s", ticket, e)
            return None
        return {
            "status": "CLOSED",
            "ticket": int(d.get("ticket", ticket)),
            "symbol": d.get("symbol", ""),
            "volume": float(d.get("volume", 0)),
            "pnl": float(d.get("pnl", 0)),
            "close_price": float(d.get("close_price", 0)),
            "broker": "mt5",
        }

    async def close_position_by_id(self, position_id: int, volume: int) -> Optional[dict]:
        """`volume` is ignored for MT5 (we close the whole ticket — partial close TBD)."""
        return await self.close_position_by_ticket(position_id)

    async def amend_position_sltp(
        self,
        position_id: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> bool:
        sl = 0.0 if stop_loss is None else float(stop_loss)
        tp = 0.0 if take_profit is None else float(take_profit)
        try:
            await self._rpc("modify_sltp",
                            ticket=int(position_id),
                            sl=f"{sl:.5f}",
                            tp=f"{tp:.5f}")
            return True
        except MT5Error as e:
            logger.error("MT5 amend_position_sltp(%s) error: %s", position_id, e)
            return False

    async def modify_sl(self, ticket: int, new_sl: float) -> bool:
        return await self.amend_position_sltp(ticket, stop_loss=new_sl)

    async def modify_tp(self, ticket: int, new_tp: float) -> bool:
        return await self.amend_position_sltp(ticket, take_profit=new_tp)

    async def close_all_positions(self) -> None:
        try:
            await self._rpc("close_all")
        except MT5Error as e:
            logger.error("MT5 close_all_positions error: %s", e)

    async def cancel_all_orders(self) -> None:
        """MT5 bot never places pending orders — noop for now."""
        logger.debug("MT5 cancel_all_orders: noop (no pending orders in use)")

    # ── Symbol helpers (spot-price cache access by bot.py) ────────────────
    def _resolve_symbol_id(self, symbol: str):
        """MT5 has no numeric symbol ID — we return the symbol name itself
        string itself, which works because callers use it as a key into
        `_spot_prices` (we also key by string there)."""
        if not symbol:
            return None
        return _to_mt5_symbol(symbol)
