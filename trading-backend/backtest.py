#!/usr/bin/env python3
"""
Backtest engine — compare current scalping params vs proposed intraday params
Uses MT5 historical candles + the real signal engine
"""
import asyncio
import sys
import os
import json
from datetime import datetime, timedelta
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(__file__))

import logging
logging.getLogger("app.trading.signals").setLevel(logging.ERROR)

from app.trading.indicators import compute_all_indicators, Candle
from app.trading.signals import generate_signal, PAIR_CONFIG, _hybrid_confirmation


@dataclass
class BacktestTrade:
    symbol: str
    direction: str  # buy/sell
    entry_price: float
    entry_time: datetime
    sl: float
    tp: float
    exit_price: float = 0.0
    exit_time: datetime = None
    pnl_pips: float = 0.0
    result: str = ""  # "TP" / "SL" / "timeout"


def pip_value(symbol: str) -> float:
    """Pip size for a symbol"""
    if "JPY" in symbol:
        return 0.01
    if "/" in symbol:
        return 0.0001
    return 1.0  # indices


def simulate_trade(candles_after_entry: list, trade: BacktestTrade, max_hold_candles: int) -> BacktestTrade:
    """Simulate a trade forward using M15 candles after entry"""
    for i, c in enumerate(candles_after_entry[:max_hold_candles]):
        if trade.direction == "buy":
            # Check SL first (conservative)
            if c.low <= trade.sl:
                trade.exit_price = trade.sl
                trade.exit_time = c.timestamp
                trade.result = "SL"
                break
            # Check TP
            if c.high >= trade.tp:
                trade.exit_price = trade.tp
                trade.exit_time = c.timestamp
                trade.result = "TP"
                break
        else:  # sell
            if c.high >= trade.sl:
                trade.exit_price = trade.sl
                trade.exit_time = c.timestamp
                trade.result = "SL"
                break
            if c.low <= trade.tp:
                trade.exit_price = trade.tp
                trade.exit_time = c.timestamp
                trade.result = "TP"
                break

    if not trade.result:
        # Timeout — close at last candle close
        last = candles_after_entry[min(max_hold_candles - 1, len(candles_after_entry) - 1)]
        trade.exit_price = last.close
        trade.exit_time = last.timestamp
        trade.result = "timeout"

    pv = pip_value(trade.symbol)
    if trade.direction == "buy":
        trade.pnl_pips = (trade.exit_price - trade.entry_price) / pv
    else:
        trade.pnl_pips = (trade.entry_price - trade.exit_price) / pv

    return trade


def run_backtest_on_candles(symbol: str, candles: list, sl_pips: float, tp_pips: float,
                             max_hold_candles: int, label: str) -> list:
    """Run backtest on a symbol with given params"""
    trades = []
    pv = pip_value(symbol)
    min_candles = 55  # need 50+ for indicators
    cooldown = 0  # candles to skip after a trade

    i = min_candles
    while i < len(candles) - max_hold_candles:
        # Compute indicators on last 100 candles
        window = candles[max(0, i - 100):i + 1]
        try:
            indicators = compute_all_indicators(window)
        except Exception:
            i += 1
            continue

        if indicators is None:
            i += 1
            continue

        # Generate signal
        current_price = candles[i].close
        # Compute change_percent from candle
        prev_close = candles[i - 1].close if i > 0 else current_price
        change_pct = ((current_price - prev_close) / prev_close * 100) if prev_close else 0
        signal = generate_signal(current_price, indicators, change_pct, symbol=symbol)

        if signal.signal == "hold" or signal.confidence < 80:
            i += 1
            continue

        # Check hybrid filter
        direction = signal.signal
        prev_close2 = candles[i - 1].close if i > 0 else current_price
        passed, reasons = _hybrid_confirmation(direction, indicators, current_price, prev_close2, "normal", symbol=symbol)
        if not passed:
            i += 1
            continue

        # Signal passed! Create trade
        entry = current_price
        if direction == "buy":
            sl = entry - sl_pips * pv
            tp = entry + tp_pips * pv
        else:
            sl = entry + sl_pips * pv
            tp = entry - tp_pips * pv

        trade = BacktestTrade(
            symbol=symbol,
            direction=direction,
            entry_price=entry,
            entry_time=candles[i].timestamp,
            sl=sl,
            tp=tp,
        )

        # Simulate forward
        future_candles = candles[i + 1:i + 1 + max_hold_candles]
        if len(future_candles) < 2:
            break

        trade = simulate_trade(future_candles, trade, max_hold_candles)
        trades.append(trade)

        # Skip forward based on trade duration
        candles_used = max(1, len([c for c in future_candles if c.timestamp <= trade.exit_time])) if trade.exit_time else 1
        i += candles_used + 2  # +2 cooldown

    return trades


def print_results(trades: list, label: str, capital: float = 1950.0):
    """Print backtest results"""
    if not trades:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"  Aucun trade généré")
        print(f"{'='*60}")
        return

    wins = [t for t in trades if t.result == "TP"]
    losses = [t for t in trades if t.result == "SL"]
    timeouts = [t for t in trades if t.result == "timeout"]

    total_pnl_pips = sum(t.pnl_pips for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    avg_win = sum(t.pnl_pips for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_pips for t in losses) / len(losses) if losses else 0

    # Estimate PnL in EUR (rough: 1 pip ≈ 1€ for 10k lot on majors)
    pip_eur = 0.85  # approximate for 10k lots
    est_pnl_eur = total_pnl_pips * pip_eur

    # Max drawdown in pips
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t.pnl_pips
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Consecutive losses
    max_consec_loss = 0
    current_consec = 0
    for t in trades:
        if t.pnl_pips < 0:
            current_consec += 1
            max_consec_loss = max(max_consec_loss, current_consec)
        else:
            current_consec = 0

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Trades:           {len(trades)}")
    print(f"  Wins (TP):        {len(wins)} ({win_rate:.1f}%)")
    print(f"  Losses (SL):      {len(losses)}")
    print(f"  Timeouts:         {len(timeouts)}")
    print(f"  ---")
    print(f"  PnL total:        {total_pnl_pips:+.1f} pips  (~{est_pnl_eur:+.0f}€)")
    print(f"  Gain moyen:       +{avg_win:.1f} pips")
    print(f"  Perte moyenne:    {avg_loss:.1f} pips")
    print(f"  R:R effectif:     {abs(avg_win/avg_loss):.2f}" if avg_loss != 0 else "  R:R: N/A")
    print(f"  Max drawdown:     -{max_dd:.1f} pips")
    print(f"  Max pertes consec: {max_consec_loss}")
    print(f"  ---")

    # Per-symbol breakdown
    symbols = set(t.symbol for t in trades)
    if len(symbols) > 1:
        print(f"  Par paire:")
        for sym in sorted(symbols):
            sym_trades = [t for t in trades if t.symbol == sym]
            sym_wins = len([t for t in sym_trades if t.result == "TP"])
            sym_pnl = sum(t.pnl_pips for t in sym_trades)
            print(f"    {sym:10} {len(sym_trades):3} trades  {sym_wins:2}W  PnL={sym_pnl:+.1f} pips")

    print(f"{'='*60}")


async def fetch_candles_from_mt5(symbols: list) -> dict:
    """Fetch H1 candles from MT5 for backtesting (more reliable than M15)"""
    from app.trading.mt5_client import MT5Client

    client = MT5Client()
    await client.connect()

    all_candles = {}
    for symbol in symbols:
        try:
            # Try H1 with 3 months duration
            candles = await client.get_historical_candles(symbol, duration="3 M", bar_size="1 hour")
            if candles and len(candles) > 100:
                all_candles[symbol] = candles
                days = len(candles) / 24
                print(f"  {symbol}: {len(candles)} bougies H1 ({days:.0f} jours)")
            else:
                # Fallback: try M5 with 1 month
                candles = await client.get_historical_candles(symbol, duration="1 M", bar_size="5 mins")
                if candles and len(candles) > 100:
                    all_candles[symbol] = candles
                    print(f"  {symbol}: {len(candles)} bougies M5")
                else:
                    print(f"  {symbol}: pas assez de données ({len(candles) if candles else 0})")
            await asyncio.sleep(0.3)  # rate limit
        except Exception as e:
            print(f"  {symbol}: erreur — {e}")

    return all_candles


async def main():
    print("=" * 60)
    print("  BACKTEST — Données réelles MT5 (30 derniers jours)")
    print("=" * 60)

    # Symbols to test (top traded)
    symbols = [
        "GBP/USD", "AUD/USD", "NZD/USD", "EUR/JPY", "GBP/JPY",
        "EUR/GBP", "EUR/AUD", "EUR/CAD", "USD/CHF", "AUD/NZD",
        "GBP/AUD", "AUD/CHF", "AUD/JPY"
    ]

    print("\nChargement des bougies M15...")
    all_candles = await fetch_candles_from_mt5(symbols)

    if not all_candles:
        print("ERREUR: Aucune donnée historique disponible")
        return

    # ===== SCENARIO A: Current scalping params (SL 10-12, TP 15-18, max_hold ~1h) =====
    print("\n\nSCENARIO A: Paramètres actuels (scalping SL=10-12 pips, TP=15-18, hold max 1h)")
    all_trades_a = []
    for symbol, candles in all_candles.items():
        cfg = PAIR_CONFIG.get(symbol.replace("/", ""), {})
        sl = cfg.get("sl", 10)
        tp = cfg.get("tp", 15)
        max_hold = 1  # 1 bougie H1
        trades = run_backtest_on_candles(symbol, candles, sl, tp, max_hold, "Scalping")
        all_trades_a.extend(trades)

    print_results(all_trades_a, "SCENARIO A — SCALPING (paramètres actuels)")

    # ===== SCENARIO B: Intraday params (SL x2.5, TP x5, max_hold 4h) =====
    print("\n\nSCENARIO B: Paramètres intraday (SL x2.5, TP x5 = R:R 1:2, hold max 4h)")
    all_trades_b = []
    for symbol, candles in all_candles.items():
        cfg = PAIR_CONFIG.get(symbol.replace("/", ""), {})
        base_sl = cfg.get("sl", 10)
        sl = round(base_sl * 2.5)
        tp = round(base_sl * 5.0)  # R:R ≈ 1:2
        max_hold = 4  # 4h en H1
        trades = run_backtest_on_candles(symbol, candles, sl, tp, max_hold, "Intraday")
        all_trades_b.extend(trades)

    print_results(all_trades_b, "SCENARIO B — INTRADAY (SL x2.5, TP x5, R:R 1:2)")

    # ===== SCENARIO C: Conservative intraday (SL x3, TP x4.5, R:R 1:1.5, 2h hold) =====
    print("\n\nSCENARIO C: Intraday conservateur (SL x3, TP x4.5, R:R 1:1.5, hold 2h)")
    all_trades_c = []
    for symbol, candles in all_candles.items():
        cfg = PAIR_CONFIG.get(symbol.replace("/", ""), {})
        base_sl = cfg.get("sl", 10)
        sl = round(base_sl * 3.0)
        tp = round(base_sl * 4.5)  # R:R ≈ 1:1.5
        max_hold = 2  # 2h en H1
        trades = run_backtest_on_candles(symbol, candles, sl, tp, max_hold, "Conservateur")
        all_trades_c.extend(trades)

    print_results(all_trades_c, "SCENARIO C — INTRADAY CONSERVATEUR (SL x3, TP x4.5)")

    # ===== SUMMARY =====
    print("\n" + "=" * 60)
    print("  COMPARAISON FINALE")
    print("=" * 60)
    for label, trades in [("A-Scalping", all_trades_a), ("B-Intraday", all_trades_b), ("C-Conservateur", all_trades_c)]:
        if trades:
            wins = len([t for t in trades if t.result == "TP"])
            wr = wins / len(trades) * 100
            pnl = sum(t.pnl_pips for t in trades)
            print(f"  {label:20} {len(trades):3} trades  WR={wr:.0f}%  PnL={pnl:+.0f} pips  (~{pnl*0.85:+.0f}€)")
        else:
            print(f"  {label:20} 0 trades")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
