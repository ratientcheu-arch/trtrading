"""Backfill: copie les trades fermes de open_positions vers la table trades.
Usage: docker exec trading-api python3 backfill_trades.py
"""
import asyncio
import asyncpg
import json

DB_URL = "postgresql://trading:Trading2026!@trading-db:5432/trading"

MT5_SYM_REV = {
    'EURUSD':'EUR/USD','GBPUSD':'GBP/USD','USDJPY':'USD/JPY','GBPJPY':'GBP/JPY',
    'AUDUSD':'AUD/USD','EURJPY':'EUR/JPY','USDCHF':'USD/CHF','USDCAD':'USD/CAD',
    'NAS100':'NASDAQ','US30':'US30','XAUUSD':'GOLD','GER40':'DAX40',
    'US500':'US500','UK100':'UK100','FRA40':'CAC40',
}

FX_CCYS = {"EUR","USD","GBP","JPY","CHF","AUD","NZD","CAD"}
CONTRACTS = {'NASDAQ':1,'GOLD':100,'US30':1,'DAX40':1,'US500':1,'UK100':1,'CAC40':1,'NAS100':1,'XAUUSD':100,'GER40':1,'FRA40':1}

def is_forex(sym):
    s = sym.replace("/","")
    return len(s)==6 and s.isalpha() and s[:3] in FX_CCYS and s[3:] in FX_CCYS

def get_contract(sym):
    if sym in CONTRACTS: return CONTRACTS[sym]
    if is_forex(sym): return 100000
    return 1

def get_market(sym):
    sym_name = MT5_SYM_REV.get(sym, sym)
    if is_forex(sym) or is_forex(sym_name): return 'forex', 'forex'
    if sym in ('XAUUSD','GOLD','XTIUSD','OIL_CRUDE'): return 'commodities', 'commodity'
    if sym in ('NAS100','US30','US500','GER40','UK100','FRA40','NASDAQ','DAX40','CAC40','NKY','JPN225','HK50'):
        return 'indices', 'index'
    return 'other', 'other'

async def main():
    conn = await asyncpg.connect(DB_URL)

    # Positions fermees pas encore dans trades
    closed = await conn.fetch("""
        SELECT op.* FROM open_positions op
        WHERE op.is_open = false AND op.close_price IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM trades t
            WHERE t.broker_position_id = (op.extra->>'ticket')
            AND t.source = 'strategy_v6'
        )
        ORDER BY op.closed_at
    """)

    print(f"Found {len(closed)} closed positions to backfill")

    inserted = 0
    for r in closed:
        extra = r['extra'] if isinstance(r['extra'], dict) else json.loads(r['extra'] or '{}')
        ticket = extra.get('ticket', 0)
        sym = r['symbol']
        mt5_sym = sym  # already MT5 format in open_positions
        sym_name = MT5_SYM_REV.get(sym, sym)
        dr = r['action']
        qty = float(r['quantity'])
        entry_p = float(r['entry_price'])
        close_p = float(r['close_price']) if r['close_price'] else entry_p
        pnl = float(r['pnl']) if r['pnl'] else 0.0
        market, asset_type = get_market(sym)
        entry_amount = entry_p * qty * get_contract(sym)
        opened = r['opened_at']
        closed_at = r['closed_at']
        sl = float(r['stop_loss']) if r['stop_loss'] else 0.0
        tp = float(r['take_profit']) if r['take_profit'] else 0.0

        try:
            await conn.execute("""
                INSERT INTO trades (symbol, name, side, status,
                    entry_price, quantity, entry_amount, entry_time,
                    exit_price, exit_time, exit_reason,
                    stop_loss, take_profit,
                    pnl, net_pnl, commission,
                    broker_position_id, source, origin,
                    market, asset_type)
                VALUES ($1, $2, $3, 'CLOSED',
                    $4, $5, $6, $7,
                    $8, $9, 'broker_close',
                    $10, $11,
                    $12, $12, 0.0,
                    $13, 'strategy_v6', 'bot',
                    $14, $15)
            """, mt5_sym, sym_name, dr.upper(),
                entry_p, qty, entry_amount, opened,
                close_p, closed_at, sl, tp,
                pnl, str(ticket), market, asset_type)
            inserted += 1
            print(f"  #{ticket} {sym_name} {dr} PnL={pnl:+.2f} -> inserted")
        except Exception as e:
            print(f"  #{ticket} {sym_name} err: {e}")

    await conn.close()
    print(f"\nDone: {inserted}/{len(closed)} trades backfilled")

asyncio.run(main())
