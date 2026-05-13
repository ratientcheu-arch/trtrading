"""Backtest H2 Mode B — Top Performers sur 20 jours ouvres.
Symboles: US500, GBP/USD, EUR/JPY, NASDAQ, GBP/JPY
+ tous les autres pour comparaison."""
import asyncio, sys
sys.path.insert(0, '/app')
from app.trading.mt5_client import MT5Client
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PARIS = ZoneInfo('Europe/Paris')

ALL_SYMBOLS = ['EUR/USD','GBP/USD','USD/JPY','GBP/JPY','AUD/USD','EUR/JPY',
               'USD/CHF','USD/CAD','NASDAQ','US30','GOLD','DAX40',
               'US500','UK100','CAC40']

TOP5 = ['US500','GBP/USD','EUR/JPY','NASDAQ','GBP/JPY']

MT5_SYM = {
    'EUR/USD':'EURUSD','GBP/USD':'GBPUSD','USD/JPY':'USDJPY','GBP/JPY':'GBPJPY',
    'AUD/USD':'AUDUSD','EUR/JPY':'EURJPY','USD/CHF':'USDCHF','USD/CAD':'USDCAD',
    'NASDAQ':'NAS100','US30':'US30','GOLD':'XAUUSD','DAX40':'GER40',
    'US500':'US500','UK100':'UK100','CAC40':'FRA40',
}
FX_CCYS = {"EUR","USD","GBP","JPY","CHF","AUD","NZD","CAD"}
CONTRACTS = {'NASDAQ':1,'GOLD':100,'US30':1,'DAX40':1,'US500':1,'UK100':1,'CAC40':1}
FX_TO_EUR = {"USD":1/1.17,"EUR":1.0,"GBP":1/0.86,"JPY":1/(150*1.17),
             "CHF":1.05,"AUD":1/1.65,"CAD":1/1.51}
QUOTE_OVERRIDE = {'NASDAQ':'USD','GOLD':'USD','US30':'USD','DAX40':'EUR',
                  'US500':'USD','UK100':'GBP','CAC40':'EUR'}
CAP_EUR = 3.0
RR_FLOOR = 2.0
SPREAD = {
    'EUR/USD': 0.00012, 'GBP/USD': 0.00015, 'USD/JPY': 0.015, 'GBP/JPY': 0.025,
    'AUD/USD': 0.00015, 'EUR/JPY': 0.018, 'USD/CHF': 0.00015, 'USD/CAD': 0.00018,
    'NASDAQ': 1.5, 'US30': 3.0, 'GOLD': 0.30, 'DAX40': 1.5,
    'US500': 0.50, 'UK100': 1.5, 'CAC40': 1.5,
}

def is_forex(sym):
    s = sym.replace("/","")
    return len(s)==6 and s.isalpha() and s[:3] in FX_CCYS and s[3:] in FX_CCYS

def get_contract(sym):
    if is_forex(sym): return 100_000
    return CONTRACTS.get(sym, 1)

def quote_ccy(sym):
    if sym in QUOTE_OVERRIDE: return QUOTE_OVERRIDE[sym]
    s = sym.replace("/","")
    return s[3:6] if len(s)==6 else "USD"

def pnl_eur(sym, price_diff, lot):
    ccy = quote_ccy(sym)
    fx = FX_TO_EUR.get(ccy, 1/1.17)
    return price_diff * get_contract(sym) * lot * fx

def compute_tp_sl_engulfing(direction, h2_range, prev_low, prev_high, eng_close, eng_high, eng_low):
    """SL/TP ancre sur le pattern englobant, range = H2.
    BUY: SL = low bougie rouge  - 1/3 range H2
         TP = high bougie verte + 2/3 range H2
    SELL: SL = high bougie verte + 1/3 range H2
          TP = low bougie rouge  - 2/3 range H2
    Retourne (tp_d, sl_d) = distances depuis eng_close (≈ entry)."""
    if h2_range <= 0:
        return 0, 0
    entry = eng_close
    if direction == 'BUY':
        sl_price = prev_low - h2_range / 3.0
        tp_price = eng_high + 2.0 * h2_range / 3.0
        sl_d = entry - sl_price
        tp_d = tp_price - entry
    else:
        sl_price = prev_high + h2_range / 3.0
        tp_price = eng_low - 2.0 * h2_range / 3.0
        sl_d = sl_price - entry
        tp_d = entry - tp_price
    if sl_d > 0 and tp_d > 0 and tp_d / sl_d < RR_FLOOR:
        sl_d = tp_d / RR_FLOOR
    return tp_d, sl_d

def compute_lot(sym, sl_d, tp_d, tp_min=7.0):
    sl_eur_per_lot = abs(pnl_eur(sym, sl_d, 1.0))
    if sl_eur_per_lot <= 0: return None
    lot = max(0.001, round(CAP_EUR / sl_eur_per_lot, 4))
    tp_eur = abs(pnl_eur(sym, tp_d, lot))
    if tp_eur < tp_min - 1.0: return None
    lot = round(lot, 2)
    if lot < 0.01: return None
    return lot

def is_bull_engulf(prev, cur):
    po, ph, pl, pc = prev['o'], prev['h'], prev['l'], prev['c']
    o, h, l, c = cur['o'], cur['h'], cur['l'], cur['c']
    if pc >= po: return False
    if c <= o: return False
    if h <= ph: return False
    if l >= pl: return False
    if (c - o) < (po - pc): return False
    return True

def is_bear_engulf(prev, cur):
    po, ph, pl, pc = prev['o'], prev['h'], prev['l'], prev['c']
    o, h, l, c = cur['o'], cur['h'], cur['l'], cur['c']
    if pc <= po: return False
    if c >= o: return False
    if h <= ph: return False
    if l >= pl: return False
    if (o - c) < (pc - po): return False
    return True

async def fetch_bars(c, sym, tf, from_ts, to_ts):
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe=tf,
                         from_ts=from_ts, to_ts=to_ts)
        return [{'t': int(it['t']), 'o': float(it['o']), 'h': float(it['h']),
                 'l': float(it['l']), 'c': float(it['c'])} for it in d.get('items', [])]
    except:
        return []

def merge_h1_to_h2(h1_bars):
    h2_bars = []
    i = 0
    while i < len(h1_bars):
        dt = datetime.fromtimestamp(h1_bars[i]['t'], tz=timezone.utc)
        broker_h = (dt.hour + 3) % 24
        if broker_h % 2 == 0:
            bar = dict(h1_bars[i])
            if i + 1 < len(h1_bars):
                nxt = h1_bars[i+1]
                if (nxt['t'] - bar['t']) <= 3700:
                    bar['h'] = max(bar['h'], nxt['h'])
                    bar['l'] = min(bar['l'], nxt['l'])
                    bar['c'] = nxt['c']
                    i += 2
                else:
                    i += 1
            else:
                i += 1
            h2_bars.append(bar)
        else:
            i += 1
    return h2_bars

async def simulate_trade_fibo(c, sym, eng_bar, prev_bar, eng_side, ref_bar, breakout_side, rb_t):
    h2_range = ref_bar['h'] - ref_bar['l']
    tp_d, sl_d = compute_tp_sl_engulfing(eng_side, h2_range,
        prev_bar['l'], prev_bar['h'], eng_bar['c'], eng_bar['h'], eng_bar['l'])
    if sl_d <= 0 or tp_d <= 0: return None
    lot = compute_lot(sym, sl_d, tp_d)
    if lot is None: return None
    spread = SPREAD.get(sym, 0.0002)
    entry = eng_bar['c']
    if eng_side == 'BUY':
        entry += spread / 2; sl_price = entry - sl_d; tp_price = entry + tp_d
    else:
        entry -= spread / 2; sl_price = entry + sl_d; tp_price = entry - tp_d
    remaining_m1 = await fetch_bars(c, sym, 'M1', eng_bar['t'] + 60, eng_bar['t'] + 3600 * 4)
    result = 'TIMEOUT'; pnl = 0
    for rm in remaining_m1:
        if eng_side == 'BUY':
            if rm['l'] - spread/2 <= sl_price: result = 'SL'; pnl = -CAP_EUR; break
            if rm['h'] - spread/2 >= tp_price: result = 'TP'; pnl = abs(pnl_eur(sym, tp_d, lot)); break
        else:
            if rm['h'] + spread/2 >= sl_price: result = 'SL'; pnl = -CAP_EUR; break
            if rm['l'] + spread/2 <= tp_price: result = 'TP'; pnl = abs(pnl_eur(sym, tp_d, lot)); break
    if result == 'TIMEOUT' and remaining_m1:
        last_c = remaining_m1[-1]['c']
        pnl = pnl_eur(sym, (last_c - entry) if eng_side == 'BUY' else (entry - last_c), lot)
    return {
        'sym': sym, 'side': eng_side, 'brk': breakout_side, 'result': result,
        'pnl': round(pnl, 2), 'rr': round(tp_d/sl_d if sl_d > 0 else 0, 2),
        'day': datetime.fromtimestamp(rb_t, tz=timezone.utc).strftime('%m/%d'),
    }

async def backtest_sym(c, sym, ref_bars, day_start_ts, day_end_ts):
    trades = []
    for rb in ref_bars:
        rb_end_ts = rb['t'] + 2 * 3600
        if rb_end_ts < day_start_ts or rb_end_ts > day_end_ts: continue
        m15 = await fetch_bars(c, sym, 'M15', rb_end_ts, rb_end_ts + 118 * 60)
        breakout_t = breakout_side = None
        for bar in m15:
            if bar['c'] > rb['h']: breakout_side = 'BUY'; breakout_t = bar['t']; break
            if bar['c'] < rb['l']: breakout_side = 'SELL'; breakout_t = bar['t']; break
        if not breakout_side: continue
        m1 = await fetch_bars(c, sym, 'M1', breakout_t, breakout_t + 118 * 60)
        if len(m1) < 4: continue
        seen_ts = set()
        engulfings = []
        for j in range(1, len(m1)):
            eng_side_found = None
            if is_bull_engulf(m1[j-1], m1[j]): eng_side_found = 'BUY'
            elif is_bear_engulf(m1[j-1], m1[j]): eng_side_found = 'SELL'
            if eng_side_found and m1[j]['t'] not in seen_ts:
                if breakout_side == 'BUY' and eng_side_found != 'BUY': continue
                if breakout_side == 'SELL' and eng_side_found != 'SELL': continue
                seen_ts.add(m1[j]['t'])
                engulfings.append((m1[j], m1[j-1], eng_side_found))
        if not engulfings: continue
        for eng_bar, prev_bar, eng_side in engulfings:
            m5s = await fetch_bars(c, sym, 'M5', eng_bar['t'] - 300, eng_bar['t'] + 300)
            if m5s:
                m5 = m5s[-1]
                if eng_side == 'BUY' and m5['c'] < m5['o']: continue
                if eng_side == 'SELL' and m5['c'] > m5['o']: continue
            t = await simulate_trade_fibo(c, sym, eng_bar, prev_bar, eng_side, rb, breakout_side, rb['t'])
            if t: trades.append(t)
    return trades

async def main():
    c = MT5Client(rep_endpoint='tcp://trading-mt5-bridge:5556',
                  pub_endpoint='tcp://trading-mt5-bridge:5555')
    await c.connect()
    now = datetime.now(PARIS)
    today = now.date()

    days = []
    d = today - timedelta(days=1)
    while len(days) < 10:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()

    print('#' * 65)
    print('  BACKTEST H2 MODE B — TOP PERFORMERS vs ALL')
    print('  Periode: %s -> %s (%d jours ouvres)' % (days[0], days[-1], len(days)))
    print('  TOP5: %s' % ', '.join(TOP5))
    print('  CAP=%s EUR, R:R min=%s' % (CAP_EUR, RR_FLOOR))
    print('#' * 65)

    sym_trades = {s: [] for s in ALL_SYMBOLS}

    for day in days:
        day_start = datetime.combine(day, datetime.min.time()).replace(tzinfo=PARIS)
        day_end = day_start + timedelta(hours=23, minutes=59)
        day_start_ts = int(day_start.timestamp())
        day_end_ts = int(day_end.timestamp())
        for sym in ALL_SYMBOLS:
            h1 = await fetch_bars(c, sym, 'H1', day_start_ts - 2*3600, day_end_ts)
            h2 = merge_h1_to_h2(h1)
            t = await backtest_sym(c, sym, h2, day_start_ts, day_end_ts)
            sym_trades[sym].extend(t)
        day_total = sum(len(sym_trades[s]) for s in ALL_SYMBOLS)
        sys.stdout.write('  %s done (%d trades cumules)\n' % (day, day_total))
        sys.stdout.flush()

    # Resultats par symbole
    print()
    print('=' * 65)
    print('  RESULTATS PAR SYMBOLE (tries par PnL)')
    print('=' * 65)
    ranked = []
    for sym in ALL_SYMBOLS:
        trades = sym_trades[sym]
        if not trades: continue
        pnl = sum(t['pnl'] for t in trades)
        wins = len([t for t in trades if t['pnl'] > 0])
        wr = wins/len(trades)*100
        tp = len([t for t in trades if t['result'] == 'TP'])
        sl = len([t for t in trades if t['result'] == 'SL'])
        ranked.append((sym, len(trades), wins, wr, pnl, tp, sl))

    ranked.sort(key=lambda x: x[4], reverse=True)
    for sym, cnt, wins, wr, pnl, tp, sl in ranked:
        top = ' <<<< TOP5' if sym in TOP5 else ''
        print('  %-10s: %3d trades | %3d wins | WR %5.1f%% | PnL %+8.2f EUR | TP %3d SL %3d%s' % (
            sym, cnt, wins, wr, pnl, tp, sl, top))

    # Comparaison TOP5 vs ALL
    all_trades = []
    top5_trades = []
    for sym in ALL_SYMBOLS:
        all_trades.extend(sym_trades[sym])
        if sym in TOP5:
            top5_trades.extend(sym_trades[sym])

    all_pnl = sum(t['pnl'] for t in all_trades)
    top5_pnl = sum(t['pnl'] for t in top5_trades)
    all_wr = len([t for t in all_trades if t['pnl']>0])/len(all_trades)*100 if all_trades else 0
    top5_wr = len([t for t in top5_trades if t['pnl']>0])/len(top5_trades)*100 if top5_trades else 0

    print()
    print('#' * 65)
    print('  COMPARAISON:')
    print('    ALL 15 sym : %3d trades | WR %5.1f%% | PnL %+.2f EUR' % (len(all_trades), all_wr, all_pnl))
    print('    TOP 5 only : %3d trades | WR %5.1f%% | PnL %+.2f EUR' % (len(top5_trades), top5_wr, top5_pnl))
    rest_trades = [t for t in all_trades if t['sym'] not in TOP5]
    rest_pnl = sum(t['pnl'] for t in rest_trades)
    rest_wr = len([t for t in rest_trades if t['pnl']>0])/len(rest_trades)*100 if rest_trades else 0
    print('    RESTE 10   : %3d trades | WR %5.1f%% | PnL %+.2f EUR' % (len(rest_trades), rest_wr, rest_pnl))
    print('#' * 65)

asyncio.run(main())
