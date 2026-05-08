"""
=============================================================================
TRTRADING - VERSION 6 MAI 2026
=============================================================================
Strategy V2 Runner - bot autonome strategy_v6.
Deploye : 2026-05-05 ~08:45 Paris
Refonte complete suite a backtest 90j MT5 + 15j M1 reel.

Setups : 16 keepers (P&L > +100E sur 15j backtest M1 reel) avec mode optimal par setup.
Bugfix : SL/TP poses APRES fill (post-fill price), pas pre-calcules sur close H1.
Securites : cascade conditionnelle (H1 alignee), trail TP par mode, cut-loss 70% / 5min sustained 50-65%.

Job DB-sync : reconcile MT5 <-> open_positions toutes les 30s.
"""

VERSION = "v6_mai_2026"
DEPLOYED_AT = "2026-05-05"

import asyncio
import os
import sys
import time
import json
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, time as dtime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, '/app')
from app.trading.mt5_client import MT5Client

# ============================================================================
# CONFIG
# ============================================================================
PARIS = ZoneInfo('Europe/Paris')
CAP_EUR = 3.0        # 2026-05-08 cap reduit pour valider strict+M5
TP_EUR_MIN_MOMENTUM = 29.0  # TP min MOMENTUM (Fibo+best-of, R:R 2.0)
TP_EUR_MIN_ORB      = 21.0  # TP min ORB-Retest (range x1.05/x1.5, R:R 1.43)
TP_EUR_MIN = TP_EUR_MIN_MOMENTUM  # legacy alias (non utilise par compute_lot apres fix)
CUT_LOSS_ENABLED = False    # 2026-05-08 sans limite SL pour validation strategy
# Fix rollover Fusion Markets 2026-05-06 : pas de trade dans cette fenetre Paris (server time UTC+3 -> 22:30 Paris = 01:30 server, mais en pratique broker bloque ~22:30 - 00:30)
ROLLOVER_START_H = 21
ROLLOVER_START_M = 50
ROLLOVER_END_H = 1
ROLLOVER_END_M = 30

def in_rollover_window(now_paris=None):
    """True si on est dans la fenetre rollover Fusion Markets."""
    if now_paris is None: now_paris = datetime.now(PARIS)
    h, m = now_paris.hour, now_paris.minute
    cur_min = h*60 + m
    start_min = ROLLOVER_START_H*60 + ROLLOVER_START_M
    end_min = ROLLOVER_END_H*60 + ROLLOVER_END_M
    if start_min < end_min:
        return start_min <= cur_min < end_min
    # Cas 22:30-00:30 (traverse minuit)
    return cur_min >= start_min or cur_min < end_min
RR_FLOOR = 2.0
HOLD_HOURS = 2
CASCADE_PCT = 0.15  # Cascade autorisee uniquement quand pos precedente a son SL >= BE (15% TP par trail A_15)

# =============================================================================
# DOUBLE STRATEGIE V6 - 2026-05-06 :
# 1) V6 MOMENTUM : 16 keepers M15 momentum + cascade conditionnelle (WR>=75%)
# 2) V6 ORB-RETEST : 21 keepers ORB-Retest strict
# Les 2 tournent en parallele, chacune avec ses propres regles.
# =============================================================================

# --- 16 keepers V6 MOMENTUM (WR >= 75%) ---
# Format: (h, m, symbol, direction, cascade_mode, trail_mode, tf_check, hold_min)
SETUPS_MOMENTUM = []  # DESACTIVE 2026-05-07 - remplace par D1-Breakout V3

# --- 21 keepers V6 ORB-RETEST STRICT ---
# Format: (range_h, range_m, sym, session_name, hold_min)
SETUPS_ORB = []  # DESACTIVE 2026-05-07 - remplace par D1-Breakout V3


# === D1-Breakout V3 BASE - deploye 2026-05-07 ===
SETUPS_D1 = [
    'EUR/USD','GBP/USD','USD/JPY','GBP/JPY','AUD/USD','EUR/JPY',
    'USD/CHF','USD/CAD','NASDAQ','US30','GOLD','DAX40',
]
D1_WINDOW_M1 = 360  # 6h apres breakout pour chercher engulfing
D1_HOLD_MIN = 60
D1_SCAN_INTERVAL_SEC = 60   # scan toutes les 60s pour freshness 90s

# === H4-Breakout switch 2026-05-07 21h ===
H4_CAP_EUR = 3.0
H4_TP_EUR_MIN = 7.0
H4_RR = 3.0
H4_WINDOW_M1 = 238  # 3h58 post-breakout (jusquau prochain H4)
H4_COOLDOWN_H = 4    # 1 trade par H4-block
# Filtre heure par symbole : ne scan qu'apres l'ouverture liquide
D1_START_HOUR = {
    'CAC40': 9, 'DAX40': 9,        # Euronext / Eurex
    'NASDAQ': 15, 'US30': 15,      # NY cash open 15:30
    'GOLD': 1,                      # commod 24h mais tres calme tot matin
    # forex et autres : pas de filtre (24h)
}
def d1_in_active_hours(sym, now=None):
    from datetime import datetime
    if now is None: now=datetime.now(PARIS)
    start_h = D1_START_HOUR.get(sym)
    if start_h is None: return True  # pas de filtre
    return now.hour >= start_h


def d1_is_bull_engulf(prev, cur):
    """STRICT : cur engulfe entierement prev (mèches incluses) + body cur >= body prev"""
    po, ph, pl, pc = prev[1], prev[2], prev[3], prev[4]
    o, h, l, c = cur[1], cur[2], cur[3], cur[4]
    if pc >= po: return False
    if c <= o: return False
    if h <= ph: return False
    if l >= pl: return False
    if (c - o) < (po - pc): return False
    return True

def d1_is_bear_engulf(prev, cur):
    """STRICT : cur engulfe entierement prev (mèches incluses) + body cur >= body prev"""
    po, ph, pl, pc = prev[1], prev[2], prev[3], prev[4]
    o, h, l, c = cur[1], cur[2], cur[3], cur[4]
    if pc <= po: return False
    if c >= o: return False
    if h <= ph: return False
    if l >= pl: return False
    if (o - c) < (pc - po): return False
    return True

async def d1_fetch_d1_prev(c, sym, today):
    """H4-Breakout : recupere le dernier H4 completed (= reference range)."""
    now = datetime.now(PARIS)
    # On veut le H4 qui vient de se terminer
    end_ts = int(now.timestamp())
    start_ts = end_ts - 8*3600  # last 8 hours
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='H4', from_ts=start_ts, to_ts=end_ts)
        items = d.get('items', [])
        # Trouve le DERNIER H4 dont l'ouverture + 4h <= now (= H4 fully closed)
        last_closed = None
        for it in items:
            t_open = datetime.fromtimestamp(int(it['t']), tz=timezone.utc).astimezone(PARIS)
            if t_open + timedelta(hours=4) <= now:
                last_closed = (t_open, float(it['o']), float(it['h']), float(it['l']), float(it['c']))
        return last_closed
    except Exception as e:
        log(f"h4_fetch {sym} err: {e}")
    return None

async def d1_fetch_m15_today(c, sym, today, start_after=None):
    """Recupere les M15 depuis start_after (ou debut today)."""
    if start_after is None:
        start_ts = int(datetime.combine(today, dtime(0,0), tzinfo=PARIS).timestamp())
    else:
        start_ts = int(start_after.timestamp())
    end_ts = int(datetime.now(PARIS).timestamp())
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='M15', from_ts=start_ts, to_ts=end_ts)
        out = []
        for it in d.get('items', []):
            tt = datetime.fromtimestamp(int(it['t']), tz=timezone.utc).astimezone(PARIS)
            if tt.date() == today:
                out.append((tt, float(it['o']), float(it['h']), float(it['l']), float(it['c'])))
        return sorted(out)
    except Exception as e:
        log(f"d1_fetch_m15_today {sym} err: {e}"); return []

async def d1_fetch_m1_window(c, sym, start_dt, end_dt):
    """Recupere M1 entre start_dt et end_dt."""
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='M1',
                         from_ts=int(start_dt.timestamp()), to_ts=int(end_dt.timestamp()))
        out = []
        for it in d.get('items', []):
            tt = datetime.fromtimestamp(int(it['t']), tz=timezone.utc).astimezone(PARIS)
            if start_dt <= tt <= end_dt:
                out.append((tt, float(it['o']), float(it['h']), float(it['l']), float(it['c'])))
        return sorted(out)
    except Exception as e:
        log(f"d1_fetch_m1_window {sym} err: {e}"); return []


async def d1_fetch_m5_current(c, sym):
    """Recupere la M5 courante (la plus recente)."""
    import time as _time
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='M5', from_ts=int(_time.time())-600, to_ts=int(_time.time()))
        items = d.get('items', [])
        if items:
            it = items[-1]
            return (float(it.get('o',0)), float(it.get('c',0)))
    except: pass
    return None

async def d1_try_breakout_pattern(c, sym, today, traded_today):
    """Essaye de detecter un D1 breakout + M1 engulfing sur un symbole. Place ordre si valide."""
    now_p = datetime.now(PARIS)
    last_trade_t = traded_today.get(sym)
    if last_trade_t and (now_p - last_trade_t).total_seconds() < 180:
        return False
    if not d1_in_active_hours(sym):
        return False
    d1 = await d1_fetch_d1_prev(c, sym, today)
    if d1 is None: return False
    _, _, d1_high, d1_low, _ = d1
    h4_ref_end = d1[0] + timedelta(hours=4)  # quand le H4 ref a clos
    m15 = await d1_fetch_m15_today(c, sym, today, start_after=h4_ref_end)
    if not m15: return False
    # Trouve 1er breakout
    breakout_t = breakout_side = None
    for tt, o, h, l, cc in m15:
        if cc > d1_high: breakout_side = 'BUY'; breakout_t = tt; break
        if cc < d1_low: breakout_side = 'SELL'; breakout_t = tt; break
    if not breakout_side: return False
    # M1 dans 3h post-breakout
    m1_end = min(datetime.now(PARIS), breakout_t + timedelta(minutes=H4_WINDOW_M1))
    m1 = await d1_fetch_m1_window(c, sym, breakout_t, m1_end)
    if len(m1) < 4: return False
    # Cherche TOUTE englobante stricte (BULL ou BEAR) apres breakout
    # Le breakout (HIGH ou LOW) declenche la fenetre de recherche uniquement
    # C'est l'englobante qui determine la direction du trade
    entry = sl = None
    pattern = ''
    trade_side = None
    for i in range(1, len(m1)):
        if d1_is_bull_engulf(m1[i-1], m1[i]):
            entry = m1[i-1][2]; sl = m1[i-1][3]; pattern = 'BULL_ENG'; trade_side = 'BUY'; break
        if d1_is_bear_engulf(m1[i-1], m1[i]):
            entry = m1[i-1][3]; sl = m1[i-1][2]; pattern = 'BEAR_ENG'; trade_side = 'SELL'; break
    if entry is None: return False
    # Filtre M5 en formation : doit confirmer la direction de l'englobante
    m5_oc = await d1_fetch_m5_current(c, sym)
    if m5_oc:
        m5_o, m5_c = m5_oc
        if trade_side == 'BUY' and m5_c < m5_o:
            log(f"D1[{sym}] SKIP M5 rouge (engulf BUY mais M5 bear) o={m5_o} c={m5_c}")
            traded_today[sym] = now_p
            return False
        if trade_side == 'SELL' and m5_c > m5_o:
            log(f"D1[{sym}] SKIP M5 verte (engulf SELL mais M5 bull) o={m5_o} c={m5_c}")
            traded_today[sym] = now_p
            return False
    sl_d = abs(entry - sl)
    if sl_d <= 0: return False
    tp_d = sl_d * H4_RR  # H4-Breakout R:R 3.0
    lot, status = compute_lot(sym, sl_d, tp_d, tp_min=H4_TP_EUR_MIN)
    if lot is None:
        log(f"D1[{sym}] {trade_side}: rejet sizing {status}")
        return False
    pattern_t = m1[i][0]
    age_min = (datetime.now(PARIS) - pattern_t).total_seconds() / 60
    if age_min > 1.5:
        log(f"D1[{sym}] PATTERN {pattern} M1@{pattern_t.strftime('%H:%M')} TROP VIEUX ({age_min:.1f}min, max 1.5min) skip")
        traded_today[sym] = now_p
        return False
    log(f"D1[{sym}] *** PATTERN {pattern} {trade_side} brk={breakout_side} M1@{pattern_t.strftime('%H:%M')} (frais {age_min:.0f}min) entry={entry} SL={sl} sl_d={sl_d:.5f} ***")
    # FIX 2026-05-07 : verif divergence prix marché vs calc entry. Si > 1× sl_d -> abort
    try:
        q_check = await c._rpc('quote', symbol=MT5_SYM[sym])
        cur_check = float(q_check.get('ask') if trade_side == 'BUY' else q_check.get('bid'))
        divergence = abs(cur_check - entry)
        max_div = sl_d * 1.0  # tolerance 100% du sl_d
        if divergence > max_div:
            log(f"D1[{sym}] ABORT : prix marche {cur_check} diverge {divergence:.5f} (max {max_div:.5f}) vs entry calc {entry}")
            traded_today[sym] = now_p
            return False
    except Exception as e:
        log(f"D1[{sym}] divergence check err: {e}")
    p = await open_position(c, sym, trade_side, lot, sl_d, tp_d, 'A_15', pos_id=1, hold_min=D1_HOLD_MIN)
    if p:
        log(f"D1[{sym}] *** POSITION D1-BREAKOUT {trade_side} (brk={breakout_side}) #{p.ticket} ***")
        await db_insert_position(p)
        asyncio.create_task(manage_position(c, p, sym, 'OFF', [p], D1_HOLD_MIN))
        traded_today[sym] = now_p
        return True
    return False

async def d1_breakout_scanner(c):
    """Scanner D1-Breakout V3 BASE : tourne en continu sur les 12 symboles keepers."""
    log(f"D1-Breakout scanner ON - {len(SETUPS_D1)} symboles - scan every {D1_SCAN_INTERVAL_SEC}s")
    traded_today = {}  # (date, sym) -> True
    while True:
        try:
            now = datetime.now(PARIS)
            now_p = now
            today = now.date()
            if in_rollover_window():
                await asyncio.sleep(60); continue
            for sym in SETUPS_D1:
                try:
                    await d1_try_breakout_pattern(c, sym, today, traded_today)
                except Exception as e:
                    log(f"D1[{sym}] try err: {e}")
            # Cleanup ancien (>2j)
            cutoff_t = now - timedelta(hours=8)
            traded_today = {k: v for k, v in traded_today.items() if v >= cutoff_t}
        except Exception as e:
            log(f"d1_breakout_scanner top err: {e}")
        await asyncio.sleep(D1_SCAN_INTERVAL_SEC)


# Compatibility wrapper for scheduler: union des 2 listes
SETUPS = []  # liste etiquetee (strategy, params...)
for s in SETUPS_MOMENTUM:
    SETUPS.append(('MOMENTUM',) + s)  # ('MOMENTUM', h, m, sym, dr, mode, trail, tf, hold_min)
for s in SETUPS_ORB:
    SETUPS.append(('ORB',) + s)        # ('ORB', range_h, range_m, sym, session_name, hold_min)

# Criteres ORB
ORB_BREAKOUT_DIST_PCT = 0.02  # baisse 0.10->0.02 le 2026-05-06
ORB_RETEST_TOL_PCT    = 0.30
ORB_BREAKOUT_TIMEOUT  = 90
ORB_RETEST_TIMEOUT    = 45
ORB_REQUIRE_REVERSAL  = True

MT5_SYM = {
    'EUR/USD':'EURUSD','GBP/USD':'GBPUSD','USD/JPY':'USDJPY','GBP/JPY':'GBPJPY',
    'AUD/USD':'AUDUSD','EUR/JPY':'EURJPY','USD/CHF':'USDCHF','USD/CAD':'USDCAD',
    'CAC40':'FRA40','NKY':'JPN225','GOLD':'XAUUSD','OIL_CRUDE':'XTIUSD',
    'NASDAQ':'NAS100','US30':'US30','HK50':'HK50','DAX40':'GER40',
}
CONTRACTS = {'NASDAQ':1,'GOLD':100,'OIL_CRUDE':1000,'CAC40':1,'NKY':0.063,'US30':1,'HK50':1,'DAX40':1}
FX_TO_EUR = {"USD":1/1.17,"EUR":1.0,"GBP":1/0.86,"JPY":1/(150*1.17),
             "CHF":1.05,"AUD":1/1.65,"CAD":1/1.51,"NZD":1/1.78,"HKD":1/(7.85*1.17)}
QUOTE_OVERRIDE = {'NASDAQ':'USD','GOLD':'USD','OIL_CRUDE':'USD','CAC40':'EUR','NKY':'JPY','US30':'USD','HK50':'HKD','DAX40':'EUR'}
FX_CCYS = {"EUR","USD","GBP","JPY","CHF","AUD","NZD","CAD"}

DRY_RUN = os.getenv('STRATV6_DRY_RUN', os.getenv('STRATV2_DRY_RUN', '0')) == '1'  # si 1: pas d'envoi reel

# ============================================================================
# HELPERS SIZING
# ============================================================================

def is_forex(sym):
    s = sym.replace("/","")
    return len(s)==6 and s.isalpha() and s[:3] in FX_CCYS and s[3:] in FX_CCYS

def get_contract(sym):
    """Contract size pour calcul P&L. 100_000 pour TOUS forex (incluant JPY).
    Fix 2026-05-05 : avant retournait 1000 pour JPY -> lot ×100 trop gros."""
    if sym in CONTRACTS: return CONTRACTS[sym]
    if is_forex(sym): return 100000  # Toujours 100k base ccy units
    return 1

def get_quote(sym):
    if sym in QUOTE_OVERRIDE: return QUOTE_OVERRIDE[sym]
    if is_forex(sym): return sym.replace("/","")[3:]
    return 'USD'

def pnl_eur(sym, price_diff, lot):
    return price_diff * get_contract(sym) * lot * FX_TO_EUR[get_quote(sym)]

def compute_tp_sl(direction, h1_high, h1_low, h1_close):
    h1r = h1_high - h1_low
    entry = h1_close
    if direction == 'BUY':
        tp_d = (h1_high + h1r) - entry
        sl_d_fibo = entry - (h1_low - h1r/2)
    else:
        tp_d = entry - (h1_low - h1r)
        sl_d_fibo = (h1_high + h1r/2) - entry
    if sl_d_fibo > 0 and tp_d > 0 and tp_d/sl_d_fibo < RR_FLOOR:
        sl_d = tp_d / RR_FLOOR
    else:
        sl_d = sl_d_fibo
    return tp_d, sl_d

def compute_lot(sym, sl_d, tp_d, tp_min=None):
    if tp_min is None: tp_min = TP_EUR_MIN_MOMENTUM
    sl_eur_per_lot = abs(pnl_eur(sym, sl_d, 1.0))
    if sl_eur_per_lot <= 0: return None, "sl_per_lot=0"
    lot = max(0.001, round(CAP_EUR / sl_eur_per_lot, 4))
    tp_eur = abs(pnl_eur(sym, tp_d, lot))
    if tp_eur < tp_min - 1.0:
        return None, f"TP={tp_eur:.1f}€ < {tp_min}€"
    return lot, "OK"

def lot_to_qty(sym, lot):
    """Convertit lot strategie -> qty MT5 (units bot)."""
    if 'JPY' in sym.replace('/','') and is_forex(sym): return lot * 1000
    if is_forex(sym): return lot * 100000
    return lot  # indices/commos: 1:1

# ============================================================================
# POSITION STATE MACHINE
# ============================================================================

@dataclass
class Pos:
    pos_id: int
    ticket: Optional[int]
    sym: str
    dr: str
    lot: float
    entry: float
    tp: float
    sl: float
    sl_initial: float
    tp_d: float
    sl_d: float
    opened_at: datetime
    trail_mode: str
    hold_min: int = 120
    mode_cascade: str = 'OFF'
    sustain_start: Optional[datetime] = None
    trail_active: bool = False
    status: str = 'OPEN'
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_at: Optional[datetime] = None

def update_trail_sl(p, fav_pct):
    """Calcule le nouveau SL selon le mode trail."""
    sign = 1 if p.dr == 'BUY' else -1
    if p.trail_mode == 'A_15':
        # Phase 2 : paliers de 10% a partir de 50% TP
        if fav_pct >= 0.90: return p.entry + sign * 0.80 * p.tp_d, True
        if fav_pct >= 0.80: return p.entry + sign * 0.70 * p.tp_d, True
        if fav_pct >= 0.70: return p.entry + sign * 0.60 * p.tp_d, True
        if fav_pct >= 0.60: return p.entry + sign * 0.50 * p.tp_d, True
        if fav_pct >= 0.50: return p.entry + sign * 0.40 * p.tp_d, True
        # Phase 1 : continu de 30% a 50%, SL trail 30% behind (commence + tard)
        if fav_pct >= 0.30:
            return p.entry + sign * (fav_pct - 0.30) * p.tp_d, True
    elif p.trail_mode == 'A_30':
        if fav_pct >= 0.30:
            return p.entry + sign * (fav_pct - 0.30) * p.tp_d, True
    elif p.trail_mode == 'B_slow':
        if fav_pct >= 0.15:
            return p.entry + sign * (fav_pct - 0.15) * p.tp_d / 3.0, True
    elif p.trail_mode == 'D_palier':
        if fav_pct >= 0.60: return p.entry + sign * 0.40 * p.tp_d, True
        if fav_pct >= 0.50: return p.entry + sign * 0.30 * p.tp_d, True
        if fav_pct >= 0.40: return p.entry + sign * 0.20 * p.tp_d, True
        if fav_pct >= 0.30: return p.entry, True
    return None, False

# ============================================================================
# DB SYNC
# ============================================================================
import asyncpg

DB_URL = "postgresql://trading:Trading2026!@trading-db:5432/trading"

async def db_insert_position(p: Pos):
    """Insert position dans open_positions."""
    if DRY_RUN: return
    try:
        conn = await asyncpg.connect(DB_URL)
        extra = json.dumps({
            'ticket': p.ticket, 'origin': 'strategy_v6',
            'trail_mode': p.trail_mode, 'pos_id_runner': p.pos_id,
            'sl_initial': p.sl_initial, 'tp_d': p.tp_d, 'sl_d': p.sl_d,
            'hold_min': p.hold_min, 'mode_cascade': p.mode_cascade,
        })
        await conn.execute("""
            INSERT INTO open_positions (pos_key, symbol, action, entry_price, quantity,
                stop_loss, take_profit, broker, opened_at, is_open, extra, origin)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'mt5', $8, true, $9::jsonb, 'strategy_v6')
            ON CONFLICT (pos_key) DO NOTHING
        """, f"{p.sym}_{p.ticket}", p.sym, p.dr, p.entry, p.lot,
            p.sl, p.tp, p.opened_at, extra)
        await conn.close()
    except Exception as e:
        log(f"DB insert err: {e}")

async def db_update_close(ticket: int, close_price: float, pnl_eur: float):
    if DRY_RUN: return
    try:
        conn = await asyncpg.connect(DB_URL)
        await conn.execute("""
            UPDATE open_positions SET is_open=false, closed_at=NOW(),
                close_price=$2, pnl=$3
            WHERE (extra->>'ticket')::bigint=$1 AND is_open=true
        """, ticket, close_price, pnl_eur)
        await conn.close()
    except Exception as e:
        log(f"DB update_close err: {e}")

# ============================================================================
# RUNNER
# ============================================================================

def log(msg):
    print(f"[{datetime.now(PARIS).strftime('%H:%M:%S')}] {msg}", flush=True)

async def fetch_h1_prev(c, sym, target_h):
    """Recupere H1 ouvrant a (target_h - 1) du jour courant. Retry 3x si H1 pas encore flushee."""
    today = datetime.now(PARIS).date()
    for attempt in range(3):
        end_ts = int(time.time()); from_ts = end_ts - 6*3600
        try:
            d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='H1', from_ts=from_ts, to_ts=end_ts)
        except Exception as e:
            log(f"fetch_h1_prev err {sym}: {e}")
            await asyncio.sleep(2); continue
        items = d.get('items', [])
        for it in reversed(items):
            bt = datetime.fromtimestamp(int(it['t']), tz=ZoneInfo('UTC')).astimezone(PARIS)
            if bt.hour == target_h - 1 and bt.date() == today:
                return float(it['o']), float(it['h']), float(it['l']), float(it['c'])
        if attempt < 2:
            log(f"H1 prev {sym} target_h={target_h} introuvable (essai {attempt+1}/3) - retry 2s")
            await asyncio.sleep(2)
    return None

async def fetch_m15_prev(c, sym, target_h, target_m):
    """Recupere M15 ouvrant a (target_h, target_m - 15) du jour courant. Retry 3x.
    Ex: trigger 15h45 -> retourne M15 [15h30-15h45]."""
    today = datetime.now(PARIS).date()
    open_h = target_h; open_m = target_m - 15
    if open_m < 0: open_m += 60; open_h -= 1
    for attempt in range(3):
        end_ts = int(time.time()); from_ts = end_ts - 3*3600
        try:
            d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='M15', from_ts=from_ts, to_ts=end_ts)
        except Exception as e:
            log(f"fetch_m15_prev err {sym}: {e}"); await asyncio.sleep(2); continue
        items = d.get('items', [])
        for it in reversed(items):
            bt = datetime.fromtimestamp(int(it['t']), tz=ZoneInfo('UTC')).astimezone(PARIS)
            if bt.hour == open_h and bt.minute == open_m and bt.date() == today:
                return float(it['o']), float(it['h']), float(it['l']), float(it['c'])
        if attempt < 2:
            log(f"M15 prev {sym} {open_h:02d}h{open_m:02d} introuvable (essai {attempt+1}/3) - retry 2s")
            await asyncio.sleep(2)
    return None

async def fetch_h1_current(c, sym):
    """Recupere la H1 en cours (dans laquelle on est)."""
    now = datetime.now(PARIS)
    end_ts = int(time.time()); from_ts = end_ts - 3*3600
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='H1', from_ts=from_ts, to_ts=end_ts)
    except: return None
    items = d.get('items', [])
    for it in reversed(items):
        bt = datetime.fromtimestamp(int(it['t']), tz=ZoneInfo('UTC')).astimezone(PARIS)
        if bt.hour == now.hour and bt.date() == now.date():
            return float(it['o']), float(it['h']), float(it['l']), float(it['c'])
    return None

def get_digits(sym):
    """Retourne le nombre de decimales pour arrondir prix selon le symbole."""
    if 'JPY' in sym.replace('/',''): return 3
    if sym in ('GOLD','XAUUSD'): return 2
    if sym in ('NASDAQ','NAS100','US30','CAC40','FRA40','DAX40','GER40','NKY','JPN225','HK50'): return 1
    if sym in ('OIL_CRUDE','XTIUSD'): return 2
    return 5

def round_lot_down(sym, lot):
    """Arrondit DOWN au step broker (jamais d'overshoot du risque)."""
    import math
    if is_forex(sym): return math.floor(lot * 100) / 100          # forex step 0.01
    return math.floor(lot * 10) / 10                              # indices/commos step 0.1

def round_lot(sym, lot):
    """Compat : arrondit DOWN au step (jamais d'overshoot)."""
    rl = round_lot_down(sym, lot)
    # min step minimum si trop petit
    if is_forex(sym): return max(0.01, rl)
    return max(0.1, rl)

async def open_position(c, sym, dr, lot, sl_d, tp_d, trail_mode, pos_id, hold_min=120):
    # Fix rollover : abort si fenetre rollover Fusion Markets
    if in_rollover_window():
        log(f"[{sym}] ABORT open: dans fenetre rollover Fusion (22:30-00:30 Paris)")
        return None
    """Envoie market order avec SL/TP, recupere fill, repose SL/TP post-fill (corrige slippage)."""
    sym_mt5 = MT5_SYM[sym]
    digits = get_digits(sym_mt5)
    lot_raw = lot
    lot = round_lot(sym, lot)
    # SAFETY : si arrondi forced step minimum > 1.5x cap -> REJET (sinon on overshoot le risque)
    expected_sl_eur = abs(pnl_eur(sym, sl_d, lot))
    if expected_sl_eur > CAP_EUR * 1.5:
        log(f"REJET sizing {sym}: lot arrondi {lot} donne SL={expected_sl_eur:.1f}€ > {CAP_EUR*1.5:.0f}€ (cap*1.5). Lot calcule etait {lot_raw:.4f}.")
        return None
    if DRY_RUN:
        log(f"[DRY] would order {dr} {sym_mt5} lot={lot}")
        try:
            q = await c._rpc('quote', symbol=sym_mt5)
            fill_price = float(q.get('bid') if dr == 'SELL' else q.get('ask'))
        except: fill_price = 1.0
        sign = 1 if dr == 'BUY' else -1
        return Pos(pos_id, None, sym, dr, lot, fill_price,
                   fill_price + sign*tp_d, fill_price - sign*sl_d,
                   fill_price - sign*sl_d, tp_d, sl_d, datetime.now(PARIS), trail_mode)
    sign = 1 if dr == 'BUY' else -1
    # Pre-calculer SL/TP a partir du quote courant (broker exige RequireSlTp=true)
    try:
        q = await c._rpc('quote', symbol=sym_mt5)
        ref = float(q.get('ask') if dr == 'BUY' else q.get('bid'))
    except Exception as e:
        log(f"quote ECHEC {sym}: {e}"); return None
    if not ref:
        log(f"quote retournee 0 pour {sym}"); return None
    pre_sl = round(ref - sign * sl_d, digits)
    pre_tp = round(ref + sign * tp_d, digits)
    log(f"Order pre-fill: ref={ref} pre_sl={pre_sl} pre_tp={pre_tp} lot={lot}")
    # FIX V6 : volume = lot directement (le broker MT5 attend des lots, pas des units)
    try:
        order = await c._rpc('place_order', symbol=sym_mt5, side=dr.lower(), volume=lot, sl=pre_sl, tp=pre_tp)
    except Exception as e:
        log(f"place_order ECHEC {sym} {dr}: {e}"); return None
    ticket = int(order.get('ticket') or order.get('position_id') or 0)
    if not ticket:
        log(f"Pas de ticket: {order}"); return None
    log(f"Order envoye {dr} {sym} lot={lot} ticket={ticket}")
    await asyncio.sleep(2)
    pos = await c.get_positions()
    p_broker = next((x for x in pos if int(x.get('ticket', 0)) == ticket), None)
    if not p_broker:
        log(f"Position {ticket} introuvable apres open"); return None
    fill_price = float(p_broker['entry_price'])
    new_sl = round(fill_price - sign * sl_d, digits)
    new_tp = round(fill_price + sign * tp_d, digits)
    try:
        await c._rpc('modify_sltp', ticket=ticket, sl=new_sl, tp=new_tp)
        log(f"SL/TP poses post-fill: SL={new_sl} TP={new_tp}")
    except Exception as e:
        # 'no changes' = pre-fill etait deja ok, OK
        if 'no changes' not in str(e).lower():
            log(f"modify_sltp ECHEC ticket={ticket}: {e}")
    return Pos(pos_id, ticket, sym, dr, lot, fill_price, new_tp, new_sl, new_sl,
               tp_d, sl_d, datetime.now(PARIS), trail_mode)

async def close_position(c, p: Pos, reason='RUNNER_EXIT'):
    if DRY_RUN or p.ticket is None: return
    try:
        await c._rpc('close', ticket=p.ticket)
        log(f"close ticket={p.ticket} reason={reason}")
    except Exception as e:
        log(f"close err ticket={p.ticket}: {e}")

async def manage_position(c, p: Pos, sym_h1_check, mode_cascade, positions_by_setup, hold_min=120):
    """Loop intra-trade: trail, cut-loss 70%, sustained 50-65%."""
    sym_mt5 = MT5_SYM.get(p.sym, p.sym.replace('/',''))
    digits = get_digits(sym_mt5)
    sign = 1 if p.dr == 'BUY' else -1
    t_end = p.opened_at + timedelta(minutes=hold_min)
    while p.status == 'OPEN' and datetime.now(PARIS) < t_end:
        await asyncio.sleep(8)
        try:
            posb = await c.get_positions()
            pb = next((x for x in posb if int(x.get('ticket', 0)) == p.ticket), None)
            if pb is None:
                p.status = 'CLOSED'; p.exit_reason = 'BROKER_CLOSE'
                log(f"#{p.pos_id} ticket={p.ticket} fermee par broker (TP/SL/manuel)")
                try:
                    d = await c._rpc('deals_by_pos', position_id=p.ticket)
                    items = d.get('items', [])
                    if len(items) >= 2:
                        p.exit_price = float(items[-1].get('price', 0))
                        profit = float(items[-1].get('profit', 0))
                        await db_update_close(p.ticket, p.exit_price, profit)
                except Exception as ee: log(f"deals_by_pos err: {ee}")
                return
            # FIX BUG #3 : fresh quote au lieu de current_price stale
            # FIX BUG #4 : pour fermer BUY on vend au bid, pour fermer SELL on rachete a l'ask
            try:
                q = await c._rpc('quote', symbol=sym_mt5)
                cur = float(q.get('bid') if p.dr == 'BUY' else q.get('ask'))
            except: continue
            if p.dr == 'BUY':
                adverse = max(0, p.entry - cur); favorable = max(0, cur - p.entry)
            else:
                adverse = max(0, cur - p.entry); favorable = max(0, p.entry - cur)
            adv_pct = adverse / p.sl_d if p.sl_d > 0 else 0
            fav_pct = favorable / p.tp_d if p.tp_d > 0 else 0

            # Cut-loss 70% (DESACTIVE par CUT_LOSS_ENABLED, fix 2026-05-06)
            if CUT_LOSS_ENABLED and adv_pct >= 0.80:
                log(f"#{p.pos_id} CUT-LOSS 70% (adv={adv_pct:.1%})")
                await close_position(c, p, 'EXIT_70')
                p.status = 'CLOSED'; p.exit_reason = 'EXIT_70'
                continue
            # Cut-loss 5min sustained (DESACTIVE par CUT_LOSS_ENABLED)
            if False and 0.50 <= adv_pct <= 0.65:  # disabled, trop agressif
                if p.sustain_start is None:
                    p.sustain_start = datetime.now(PARIS)
                elif (datetime.now(PARIS) - p.sustain_start).total_seconds() >= 300:
                    log(f"#{p.pos_id} CUT-LOSS 5MIN (adv={adv_pct:.1%})")
                    await close_position(c, p, 'EXIT_5MIN')
                    p.status = 'CLOSED'; p.exit_reason = 'EXIT_5MIN'
                    continue
            elif adv_pct < 0.50:
                p.sustain_start = None  # reset uniquement si retour < 50%
            # Trail FIX BUG #5 : digits dynamique
            new_sl, do_trail = update_trail_sl(p, fav_pct)
            if do_trail and new_sl is not None:
                if (p.dr == 'BUY' and new_sl > p.sl) or (p.dr == 'SELL' and new_sl < p.sl):
                    new_sl_r = round(new_sl, digits)
                    try:
                        await c._rpc('modify_sltp', ticket=p.ticket, sl=new_sl_r, tp=p.tp)
                        log(f"#{p.pos_id} {p.sym} TRAIL SL {p.sl} -> {new_sl_r} (fav={fav_pct:.1%})")
                        p.sl = new_sl_r; p.trail_active = True
                    except Exception as e:
                        if 'no changes' not in str(e).lower():
                            log(f"trail err: {e}")
        except Exception as e:
            log(f"manage err: {e}\n{traceback.format_exc()[:300]}")
    if p.status == 'OPEN':
        log(f"#{p.pos_id} TIMEOUT {hold_min}min, close")
        await close_position(c, p, 'TIMEOUT')
        p.status = 'CLOSED'; p.exit_reason = 'TIMEOUT'

async def run_setup(c, h, m, sym, dr, mode_cascade, trail_mode, tf_check='H1', hold_min=120):
    """Execute le cycle complet d'un setup. tf_check='H1' ou 'M15'."""
    log(f"=== TRIGGER {h:02d}h{m:02d} {sym} {dr} (mode={mode_cascade}, trail={trail_mode}, tf={tf_check}, hold={hold_min}min) ===")
    if tf_check == 'M15':
        bar = await fetch_m15_prev(c, sym, h, m)
        if bar is None:
            log(f"M15 prev introuvable, SKIP"); return
        op, hi, lo, cl = bar
        bar_d = 'BUY' if cl > op else 'SELL' if cl < op else 'DOJI'
        log(f"M15 prec: O={op} H={hi} L={lo} C={cl} -> {bar_d}")
        if bar_d != dr:
            log(f"M15 {bar_d} != {dr}, SETUP NON DECLENCHE"); return
    else:
        bar = await fetch_h1_prev(c, sym, h)
        if bar is None:
            log(f"H1 prev introuvable, SKIP"); return
        op, hi, lo, cl = bar
        bar_d = 'BUY' if cl > op else 'SELL' if cl < op else 'DOJI'
        log(f"H1 [{h-1}h-{h}h]: O={op} H={hi} L={lo} C={cl} -> {bar_d}")
        if bar_d != dr:
            log(f"H1 {bar_d} != {dr}, SETUP NON DECLENCHE"); return
    tp_d, sl_d = compute_tp_sl(dr, hi, lo, cl)
    if sl_d <= 0 or tp_d <= 0:
        log(f"distances invalides"); return
    lot, status = compute_lot(sym, sl_d, tp_d)
    if lot is None:
        log(f"REJET sizing: {status}"); return
    log(f"sizing OK lot={lot:.4f}, sl_d={sl_d:.5f}, tp_d={tp_d:.5f}, R:R={tp_d/sl_d:.2f}")

    # Pos 1
    p1 = await open_position(c, sym, dr, lot, sl_d, tp_d, trail_mode, pos_id=1, hold_min=hold_min)
    if p1 is None: return
    await db_insert_position(p1)
    positions = [p1]
    # Manage p1 + cascade dans une coroutine separee
    asyncio.create_task(manage_position(c, p1, sym, mode_cascade, positions, hold_min))

    # Cascade : monitoring trigger 15% TP atteint + SL pos precedente >= BE
    sign = 1 if dr == 'BUY' else -1
    next_th = p1.entry + sign * CASCADE_PCT * tp_d  # 15% TP du dernier
    cascade_count = 1
    t_end = p1.opened_at + timedelta(minutes=hold_min)
    while cascade_count < 3 and datetime.now(PARIS) < t_end:
        await asyncio.sleep(20)
        try:
            q = await c._rpc('quote', symbol=MT5_SYM[sym])
            cur = float(q.get('bid') if dr == 'BUY' else q.get('ask'))  # FIX bid/ask: BUY ferme bid
        except: continue
        triggered = (cur >= next_th) if dr == 'BUY' else (cur <= next_th)
        if not triggered: continue
        # CRITIQUE : verifier que la DERNIERE pos a son SL >= BE (gain locke)
        last_pos = positions[-1]
        # Lit le SL courant broker (peut avoir ete trail)
        try:
            posb = await c.get_positions()
            pb = next((x for x in posb if int(x.get('ticket', 0)) == last_pos.ticket), None)
            cur_sl = float(pb.get('stop_loss', 0)) if pb else last_pos.sl
        except: cur_sl = last_pos.sl
        # Pour BUY: SL doit etre >= entry. Pour SELL: SL doit etre <= entry.
        sl_at_be = (cur_sl >= last_pos.entry) if dr == 'BUY' else (cur_sl <= last_pos.entry)
        if not sl_at_be:
            log(f"Cascade {cascade_count+1} bloquee : pos #{last_pos.pos_id} SL={cur_sl} pas encore BE (entry={last_pos.entry}) - attente trail")
            await asyncio.sleep(30)
            continue
        # Check H1 alignment si COND (legacy garde-fou)
        if mode_cascade == 'COND':
            ch1 = await fetch_h1_current(c, sym)
            if ch1:
                oh, _, _, ch = ch1
                aligned = (ch > oh) if dr == 'BUY' else (ch < oh)
                if not aligned:
                    log(f"Cascade {cascade_count+1} bloquee (H1 courante non alignee close vs open)")
                    continue
        # Open nouvelle position cascade
        log(f"Cascade {cascade_count+1} declenche @ {cur} (seuil {next_th})")
        p_new = await open_position(c, sym, dr, lot, sl_d, tp_d, trail_mode, pos_id=cascade_count+1)
        if p_new:
            positions.append(p_new)
            await db_insert_position(p_new)
            asyncio.create_task(manage_position(c, p_new, sym, mode_cascade, positions))
            cascade_count += 1
            next_th = p_new.entry + sign * CASCADE_PCT * tp_d

# ============================================================================
# RECONCILE JOB (DB <-> MT5)
# ============================================================================

IPC_POSITIONS_FILE = '/ipc/bot_positions.json'

def write_ipc_positions(positions_broker):
    """Ecrit le cache IPC pour le dashboard (format complet attendu par le frontend)."""
    import json as _json
    from datetime import datetime, timezone
    out = []
    for p in positions_broker:
        ticket = int(p.get('ticket', 0) or 0)
        sym = p.get('symbol', '') or ''
        side = (p.get('direction') or p.get('side') or '').upper()
        entry = float(p.get('entry_price', 0) or 0)
        cur = float(p.get('current_price', 0) or 0)
        qty = float(p.get('quantity', 0) or 0)
        sl = float(p.get('stop_loss', 0) or 0)
        tp = float(p.get('take_profit', 0) or 0)
        pnl = float(p.get('unrealized_pnl', 0) or 0)
        swap = float(p.get('swap', 0) or 0)
        comm = float(p.get('commission', 0) or 0)
        ot = p.get('open_time') or p.get('open_timestamp') or 0
        try: ot = int(ot)
        except: ot = 0
        # Le broker Fusion retourne des epoch en time-zone serveur (CEST = UTC+2 en ete).
        # On compense l'offset connu : -7200s pour CEST, sinon fallback now() pour eviter hold negatif.
        BROKER_OFFSET_SEC = 7200  # CEST = UTC+2 (a ajuster en hiver)
        if ot:
            ot_utc = ot - BROKER_OFFSET_SEC
            now_ts = int(datetime.now(timezone.utc).timestamp())
            # Sanity: si ot_utc est dans le futur, fallback now
            if ot_utc > now_ts + 60: ot_utc = now_ts
            et_iso = datetime.fromtimestamp(ot_utc, tz=timezone.utc).isoformat()
        else:
            et_iso = datetime.now(timezone.utc).isoformat()
        # Distances + notional + montants en EUR
        sl_d = abs(entry - sl) if (entry and sl) else 0.0
        tp_d = abs(tp - entry) if (entry and tp) else 0.0
        # notional en EUR : prix x qty x contract_size x fx_to_eur(quote)
        try:
            contract = get_contract(sym) if sym else 1
            quote_ccy = get_quote(sym) if sym else 'USD'
            fx_to_eur_q = FX_TO_EUR.get(quote_ccy, 1.0)
            notional_eur = entry * qty * contract * fx_to_eur_q if entry and qty else 0.0
        except: notional_eur = 0.0
        leverage = 500
        margin_eur = (notional_eur / leverage) if notional_eur and leverage else 0.0
        # Risk reel = SL distance x lot x contract x fx (en EUR)
        try:
            risk_eur = abs(sl_d * qty * contract * fx_to_eur_q) if sl_d else CAP_EUR
        except: risk_eur = CAP_EUR
        # Reward potentiel en EUR
        try:
            reward_eur = abs(tp_d * qty * contract * fx_to_eur_q) if tp_d else 0.0
        except: reward_eur = 0.0
        # P&L en pourcentage du risk reel
        pnl_percent = (pnl / risk_eur * 100) if risk_eur > 0 else 0.0
        # Distance courante du SL/TP en %
        sl_progress = 0.0; tp_progress = 0.0
        if entry and sl_d > 0 and tp_d > 0:
            if side == 'BUY':
                sl_progress = max(0, (entry - cur) / sl_d * 100) if cur < entry else 0.0
                tp_progress = max(0, (cur - entry) / tp_d * 100) if cur > entry else 0.0
            else:
                sl_progress = max(0, (cur - entry) / sl_d * 100) if cur > entry else 0.0
                tp_progress = max(0, (entry - cur) / tp_d * 100) if cur < entry else 0.0
        # SL deplace du initial = trail actif
        sl_dist_from_entry = (entry - sl) if side == 'BUY' else (sl - entry)
        trail_active = (sl_dist_from_entry < 0) if side == 'BUY' else (sl_dist_from_entry < 0)
        # En BUY, SL initial < entry. Si SL > entry => trail active (SL au-dessus = protege gains)
        if side == 'BUY':
            trail_active = sl >= entry
            gain_locked = (sl - entry) * get_contract(sym) * qty * FX_TO_EUR.get(get_quote(sym), 1) if sl >= entry else 0.0
        else:
            trail_active = sl <= entry
            gain_locked = (entry - sl) * get_contract(sym) * qty * FX_TO_EUR.get(get_quote(sym), 1) if sl <= entry else 0.0
        out.append({
            # identification
            'symbol': sym, 'mt5_symbol': sym, 'ticket': ticket, 'position_id': ticket,
            'pos_key': f"{sym}_{ticket}",
            # direction
            'action': side, 'side': side, 'direction': side,
            # qty/lot
            'quantity': qty, 'volume_lots': qty,
            # prix
            'entry_price': entry, 'current_price': cur,
            'stop_loss': sl, 'take_profit': tp,
            # pnl + percent
            'unrealized_pnl': pnl, 'profit': pnl, 'pnl': pnl,
            'pnl_percent': pnl_percent,
            'pnl_pct': pnl_percent,
            'roi': pnl_percent,
            'sl_progress': sl_progress,
            'tp_progress': tp_progress,
            'swap': swap, 'commission': comm,
            'entry_commission': 0.0, 'entry_commission_ccy': 'EUR',
            # timestamps
            'open_time': str(ot), 'open_timestamp': ot, 'entry_time': et_iso,
            '_opened_ts': float(ot),
            # metadata strategique
            'signal_confidence': 95,
            'signal_reason': 'V6 strategy_v6',
            'indicators_snapshot': None,
            'origin': 'strategy_v6', 'source': 'strategy_v6',
            'broker': 'mt5',
            # sizing
            'position_size': notional_eur,
            'notional_eur': notional_eur,
            'margin': margin_eur,
            'margin_eur': margin_eur,
            'leverage': leverage,
            'risk_eur': risk_eur,
            'reward_eur': reward_eur,
            '_original_tp_dist': tp_d,
            'sl_dist': sl_d, 'tp_dist': tp_d,
            'sl_order_id': None, 'tp_order_id': None,
        })
    try:
        with open(IPC_POSITIONS_FILE, 'w') as f: _json.dump(out, f)
    except Exception as e:
        log(f"IPC write err: {e}")

import glob

async def handle_command(c, cmd_data):
    """Process une commande venue de l'API (modify_sl, manual_close, etc.)."""
    cmd_id = cmd_data.get('id'); cmd = cmd_data.get('cmd', ''); params = cmd_data.get('params', {})
    resp_file = f"/ipc/resp_{cmd_id}.json"
    cmd_file = f"/ipc/cmd_{cmd_id}.json"
    log(f"CMD recu id={cmd_id} cmd={cmd} params={params}")
    success = False; data = None; error = None
    try:
        if cmd == 'modify_sl':
            ticket = int(params.get('ticket', 0)); new_sl = float(params.get('new_sl', 0))
            pos = await c.get_positions()
            p = next((x for x in pos if int(x.get('ticket', 0)) == ticket), None)
            if not p: error = f"position {ticket} introuvable"
            else:
                tp = float(p.get('take_profit', 0))
                await c._rpc('modify_sltp', ticket=ticket, sl=new_sl, tp=tp)
                success = True; data = {'ticket': ticket, 'new_sl': new_sl}
                log(f"modify_sl ticket={ticket} new_sl={new_sl} OK")
        elif cmd == 'manual_close' or cmd == 'close_position_direct':
            sym = params.get('symbol', '')
            pos = await c.get_positions()
            mt5_sym = sym.replace('/', '')
            p = next((x for x in pos if (x.get('symbol', '') == mt5_sym or x.get('symbol', '') == sym)), None)
            if not p: error = f"position {sym} introuvable"
            else:
                ticket = int(p['ticket'])
                await c._rpc('close', ticket=ticket)
                success = True; data = {'ticket': ticket, 'symbol': sym}
                log(f"close ticket={ticket} ({sym}) OK")
        elif cmd == 'manual_order':
            # SECURITE V6 : risk plafonne a CAP_EUR (25E) PEU IMPORTE amount_eur recu
            sym = params.get('symbol', ''); action = (params.get('action', '') or '').lower()
            amount_requested = float(params.get('amount_eur', CAP_EUR))
            amount = min(amount_requested, CAP_EUR)  # HARD CAP €25
            if amount_requested > CAP_EUR:
                log(f"manual_order {sym} {action} : amount_eur={amount_requested} > CAP -> CLAMP a €{CAP_EUR}")
            mt5_sym = MT5_SYM.get(sym, sym.replace('/', ''))
            q = await c._rpc('quote', symbol=mt5_sym)
            ref = float(q.get('ask') if action == 'buy' else q.get('bid'))
            sl_d = ref * 0.0005 if is_forex(sym) else ref * 0.002
            tp_d = sl_d * 2
            sl_per_lot = abs(pnl_eur(sym, sl_d, 1.0))
            lot = max(0.01, round(amount / sl_per_lot, 2))
            # Round to step
            if not is_forex(sym): lot = round(lot * 10) / 10
            sign = 1 if action == 'buy' else -1
            digits = 5 if is_forex(sym) and 'JPY' not in sym else 3 if 'JPY' in sym else 1
            sl_p = round(ref - sign * sl_d, digits); tp_p = round(ref + sign * tp_d, digits)
            order = await c._rpc('place_order', symbol=mt5_sym, side=action, volume=lot, sl=sl_p, tp=tp_p)
            success = True; data = order
            log(f"manual_order {sym} {action} lot={lot} risk=€{amount:.0f} OK")
        elif cmd == 'set_trail':
            # Configure le trail mode pour un ticket donne
            ticket = int(params.get('ticket', 0))
            mode = params.get('mode', 'A_15')
            start_pct = float(params.get('start_pct', 0.15))
            if ticket > 0 and mode in ('A_15','A_30','B_slow','D_palier','OFF'):
                if mode == 'OFF':
                    TRAIL_CONFIG.pop(ticket, None)
                    success = True; data = {'ticket': ticket, 'mode': 'OFF (auto-default)'}
                    log(f"set_trail ticket={ticket} -> OFF (revert auto)")
                else:
                    TRAIL_CONFIG[ticket] = {'mode': mode, 'start_pct': start_pct}
                    success = True; data = {'ticket': ticket, 'mode': mode, 'start_pct': start_pct}
                    log(f"set_trail ticket={ticket} mode={mode} start={start_pct} OK")
            else:
                error = f"invalid mode={mode} or ticket={ticket}"
        elif cmd == 'get_trail_config':
            success = True; data = dict(TRAIL_CONFIG)
        elif cmd == 'sync_balance' or cmd == 'start' or cmd == 'scan_only' or cmd == 'stop' or cmd == 'set_mode':
            # Acks sans action (V6 toujours actif)
            success = True; data = {'status': 'V6_runner', 'cmd': cmd}
        elif cmd == 'get_market_quote':
            sym = params.get('symbol', '')
            mt5_sym = MT5_SYM.get(sym, sym.replace('/', ''))
            q = await c._rpc('quote', symbol=mt5_sym)
            success = True; data = q
        else:
            error = f"unknown_cmd={cmd}"
    except Exception as e:
        error = str(e)
        log(f"CMD ECHEC {cmd}: {e}")
    # Ecrire response
    import json as _json
    try:
        with open(resp_file, 'w') as f:
            _json.dump({'id': cmd_id, 'success': success, 'data': data, 'error': error}, f)
    except Exception as e:
        log(f"resp write err: {e}")
    # Cleanup cmd file
    try: os.remove(cmd_file)
    except: pass

async def commands_loop(c):
    """Poll /ipc/cmd_*.json et execute les commandes."""
    while True:
        await asyncio.sleep(1)
        try:
            for f in sorted(glob.glob('/ipc/cmd_*.json')):
                try:
                    with open(f) as fp:
                        import json as _j
                        d = _j.load(fp)
                    asyncio.create_task(handle_command(c, d))
                except Exception as e:
                    log(f"cmd parse err {f}: {e}")
                    try: os.remove(f)
                    except: pass
        except Exception as e:
            log(f"commands_loop err: {e}")

IPC_ACCOUNT_FILE = '/ipc/bot_account.json'
IPC_STATUS_FILE = '/ipc/bot_status.json'
IPC_DAILY_FILE = '/ipc/bot_daily.json'

def write_ipc_account(account_data, n_positions, total_pnl_floating):
    """Ecrit le cache account pour le dashboard (balance, equity, etc.)."""
    import json as _json
    try:
        balance = float(account_data.get('balance', 0))
        equity = float(account_data.get('equity', 0))
        margin = float(account_data.get('margin', 0))
        free_margin = float(account_data.get('free_margin', 0))
        out = {
            'balance': balance, 'capital': balance, 'capital_total': balance,
            'equity': equity, 'free_margin': free_margin, 'margin': margin,
            'currency': account_data.get('currency', 'EUR'),
            'leverage': int(account_data.get('leverage', 500)),
            'profit': float(account_data.get('profit', 0)),
            'open_positions': n_positions,
            'pnl_unrealized': total_pnl_floating,
            'mt5_connected': True,
            'broker': 'mt5',
            'last_update': datetime.now(timezone.utc).isoformat(),
        }
        with open(IPC_ACCOUNT_FILE, 'w') as f: _json.dump(out, f)
    except Exception as e:
        log(f"IPC account write err: {e}")

def write_ipc_status(n_positions):
    """Ecrit le bot status (running, mt5_connected, etc.)."""
    import json as _json
    try:
        out = {
            'running': True, 'mt5_connected': True, 'scan_only': False,
            'primary_broker': 'mt5', 'open_positions': n_positions,
            'origin': 'strategy_v6', 'version': VERSION,
            'last_update': datetime.now(timezone.utc).isoformat(),
        }
        with open(IPC_STATUS_FILE, 'w') as f: _json.dump(out, f)
    except: pass

async def ipc_loop(c):
    """Toutes les 3s: refresh IPC cache pour dashboard live AVEC fresh quotes + account."""
    from datetime import timezone as _tz
    while True:
        await asyncio.sleep(3)
        try:
            posb = await c.get_positions()
            symbols = {p.get('symbol', '') for p in posb if p.get('symbol')}
            quotes = {}
            for s in symbols:
                try:
                    q = await c._rpc('quote', symbol=s)
                    quotes[s] = {'bid': float(q.get('bid', 0) or 0), 'ask': float(q.get('ask', 0) or 0)}
                except: pass
            total_floating = 0.0
            for p in posb:
                sym = p.get('symbol', '')
                side = (p.get('direction') or p.get('side') or '').upper()
                q = quotes.get(sym)
                if q:
                    # FIX BUG #4 : BUY ferme au bid, SELL ferme a l'ask
                    p['current_price'] = q['bid'] if side == 'BUY' else q['ask']
                total_floating += float(p.get('unrealized_pnl', 0) or 0)
            write_ipc_positions(posb)
            # Account fresh
            try:
                a = await c._rpc('account')
                write_ipc_account(a, len(posb), total_floating)
                write_ipc_status(len(posb))
            except Exception as e: log(f"account fetch err: {e}")
        except Exception as e:
            log(f"ipc_loop ERR: {e}")
            import traceback; log(traceback.format_exc()[:300])

# Cache de l'etat sustained par ticket (pour cut-loss 5min)
SUSTAIN_TRACK = {}

# Config trail par ticket (override le trail dynamique par defaut)
# Format: TRAIL_CONFIG[ticket] = {'mode': 'A_15'|'A_30'|'B_slow'|'D_palier', 'start_pct': 0.15}
TRAIL_CONFIG = {}

async def manage_all_loop(c):
    """Monitor TOUTES les positions broker toutes les 8s, applique trail D_palier + cut-loss.
    Mode par defaut D_palier (le meilleur en moyenne). Cut-loss EXIT_70 + EXIT_5MIN."""
    while True:
        await asyncio.sleep(8)
        try:
            posb = await c.get_positions()
            for p in posb:
                ticket = int(p.get('ticket', 0))
                if not ticket: continue
                sym = p.get('symbol', '')
                side = (p.get('direction') or p.get('side') or '').upper()
                entry = float(p.get('entry_price', 0))
                sl = float(p.get('stop_loss', 0))
                tp = float(p.get('take_profit', 0))
                if not (entry and sl and tp): continue
                sl_d_init = abs(entry - sl)
                tp_d = abs(tp - entry)
                if sl_d_init <= 0 or tp_d <= 0: continue
                # Quote fresh - FIX BUG #4 : pour fermer BUY on vend au bid (= prix sortie reelle)
                # pour fermer SELL on rachete a l'ask (= prix sortie reelle)
                try:
                    q = await c._rpc('quote', symbol=sym)
                    cur = float(q.get('bid') if side == 'BUY' else q.get('ask'))
                except: continue
                sign = 1 if side == 'BUY' else -1
                fav = max(0, (cur - entry) * sign)
                adv = max(0, (entry - cur) * sign)
                fav_pct = fav / tp_d if tp_d > 0 else 0
                adv_pct = adv / sl_d_init if sl_d_init > 0 else 0

                # Cut-loss EXIT_70 (DESACTIVE)
                if CUT_LOSS_ENABLED and adv_pct >= 0.80:
                    log(f"manage #{ticket} {sym} CUT-LOSS 70% (adv={adv_pct:.1%}) -> close")
                    try:
                        await c._rpc('close', ticket=ticket)
                        SUSTAIN_TRACK.pop(ticket, None)
                    except Exception as e: log(f"close err: {e}")
                    continue
                # Cut-loss 5min sustained (DESACTIVE)
                if False and 0.50 <= adv_pct <= 0.65:  # disabled, trop agressif
                    if ticket not in SUSTAIN_TRACK:
                        SUSTAIN_TRACK[ticket] = datetime.now(PARIS)
                    elif (datetime.now(PARIS) - SUSTAIN_TRACK[ticket]).total_seconds() >= 300:
                        log(f"manage #{ticket} {sym} CUT-LOSS 5MIN (adv={adv_pct:.1%}) -> close")
                        try:
                            await c._rpc('close', ticket=ticket)
                            SUSTAIN_TRACK.pop(ticket, None)
                        except Exception as e: log(f"close err: {e}")
                        continue
                elif adv_pct < 0.50:
                    SUSTAIN_TRACK.pop(ticket, None)

                # Trail dynamique : si TRAIL_CONFIG[ticket] specifie, utilise mode custom
                # Sinon : 15% par defaut, 30% si paire calme (range etroit)
                cfg = TRAIL_CONFIG.get(ticket)
                if cfg:
                    mode = cfg.get('mode', 'A_15')
                    trail_start = cfg.get('start_pct', 0.15)
                else:
                    mode = 'A_15'  # default formula = lineaire 1:1
                    if 'JPY' in sym:
                        trail_start = 0.30 if tp_d < 0.15 else 0.15
                    elif sym in ('GER40','NAS100','US30','FRA40','JPN225','HK50','CAC40','DAX40','NKY'):
                        trail_start = 0.30 if tp_d < 30 else 0.15
                    elif sym in ('XAUUSD',):
                        trail_start = 0.30 if tp_d < 5 else 0.15
                    elif sym in ('XTIUSD',):
                        trail_start = 0.30 if tp_d < 0.50 else 0.15
                    else:
                        trail_start = 0.30 if tp_d < 0.0010 else 0.15
                # Calcul new_sl selon mode
                new_sl = None
                if mode == 'A_15' or mode == 'A_30':
                    if fav_pct >= trail_start:
                        lock_pct = fav_pct - trail_start
                        new_sl = entry + sign * lock_pct * tp_d
                elif mode == 'B_slow':
                    if fav_pct >= trail_start:
                        lock_pct = (fav_pct - trail_start) / 3.0
                        new_sl = entry + sign * lock_pct * tp_d
                elif mode == 'D_palier':
                    if fav_pct >= 0.80: new_sl = entry + sign * 0.65 * tp_d
                    elif fav_pct >= 0.70: new_sl = entry + sign * 0.55 * tp_d
                    elif fav_pct >= 0.60: new_sl = entry + sign * 0.40 * tp_d
                    elif fav_pct >= 0.50: new_sl = entry + sign * 0.30 * tp_d
                    elif fav_pct >= 0.40: new_sl = entry + sign * 0.20 * tp_d
                    elif fav_pct >= 0.30: new_sl = entry  # BE
                if new_sl is None: continue
                # Buffer min 5 pips entre SL et prix courant
                if 'JPY' in sym: min_buffer = 0.05      # JPY 5 pips
                elif sym in ('GER40','NAS100','US30','FRA40','JPN225','HK50','CAC40','DAX40','NKY'):
                    min_buffer = 5.0                    # indices 5 pts
                elif sym in ('XAUUSD',): min_buffer = 0.5     # gold 0.5 USD
                elif sym in ('XTIUSD',): min_buffer = 0.05    # oil 5 pips
                else: min_buffer = 0.0005               # forex 5 pips
                # SL ne doit pas etre a moins de buffer du cur (BUY: sl <= cur-buffer ; SELL: sl >= cur+buffer)
                if side == 'BUY':
                    safe_sl = cur - min_buffer
                    if new_sl > safe_sl: new_sl = safe_sl
                else:
                    safe_sl = cur + min_buffer
                    if new_sl < safe_sl: new_sl = safe_sl
                # Round
                if 'JPY' in sym: digits = 3
                elif sym in ('XAUUSD',): digits = 2
                elif sym in ('NAS100','US30','FRA40','GER40','JPN225','HK50','CAC40','DAX40','NKY'): digits = 1
                elif sym in ('XTIUSD',): digits = 2
                else: digits = 5
                new_sl = round(new_sl, digits)
                # Verifier amelioration
                improvement = (new_sl - sl) if side == 'BUY' else (sl - new_sl)
                if improvement <= 0: continue
                try:
                    await c._rpc('modify_sltp', ticket=ticket, sl=new_sl, tp=tp)
                    lock_pct = (fav_pct - 0.30) * 100  # rough lock %
                    log(f"manage #{ticket} {sym} TRAIL palier {int(fav_pct*100)}%: SL {sl} -> {new_sl}")
                except Exception as e:
                    log(f"manage trail err #{ticket}: {e}")
        except Exception as e:
            log(f"manage_all_loop err: {e}")

async def reconcile_loop(c):
    """Toutes les 30s: sync DB <-> broker."""
    while True:
        await asyncio.sleep(30)
        try:
            posb = await c.get_positions()
            broker_tickets = {int(p.get('ticket', 0)) for p in posb}
            conn = await asyncpg.connect(DB_URL)
            db_open = await conn.fetch("SELECT (extra->>'ticket')::bigint AS ticket FROM open_positions WHERE is_open=true AND extra->>'origin'='strategy_v6'")
            for r in db_open:
                if r['ticket'] not in broker_tickets:
                    try:
                        d = await c._rpc('deals_by_pos', position_id=r['ticket'])
                        items = d.get('items', [])
                        if len(items) >= 2:
                            close_price = float(items[-1]['price'])
                            profit = float(items[-1]['profit'])
                            await conn.execute("UPDATE open_positions SET is_open=false, closed_at=NOW(), close_price=$1, pnl=$2 WHERE (extra->>'ticket')::bigint=$3 AND is_open=true",
                                               close_price, profit, r['ticket'])
                            log(f"reconcile: ticket {r['ticket']} -> closed pnl={profit}")
                    except Exception as e:
                        log(f"reconcile err ticket {r['ticket']}: {e}")
            await conn.close()
        except Exception as e:
            log(f"reconcile loop err: {e}")

# ============================================================================
# SCHEDULER
# ============================================================================

async def fetch_m15_at(c, sym, range_h, range_m, retry=3):
    """Recupere M15 ouvrant a (range_h, range_m) du jour courant."""
    today = datetime.now(PARIS).date()
    for attempt in range(retry):
        end_ts = int(time.time()); from_ts = end_ts - 3*3600
        try:
            d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='M15', from_ts=from_ts, to_ts=end_ts)
        except Exception as e:
            log(f"fetch_m15_at err {sym}: {e}")
            await asyncio.sleep(2); continue
        for it in reversed(d.get('items', [])):
            bt = datetime.fromtimestamp(int(it['t']), tz=ZoneInfo('UTC')).astimezone(PARIS)
            if bt.hour == range_h and bt.minute == range_m and bt.date() == today:
                return float(it['o']), float(it['h']), float(it['l']), float(it['c'])
        if attempt < retry - 1:
            await asyncio.sleep(3)
    return None

async def fetch_m5_recent(c, sym):
    """M5 dernieres 3h."""
    end_ts = int(time.time())
    try:
        d = await c._rpc('bars', symbol=MT5_SYM[sym], timeframe='M5', from_ts=end_ts-3*3600, to_ts=end_ts)
        return [(datetime.fromtimestamp(int(it['t']), tz=ZoneInfo('UTC')).astimezone(PARIS),
                 float(it['o']), float(it['h']), float(it['l']), float(it['c']))
                for it in d.get('items', [])]
    except: return []

async def run_orb_universal(c, range_h, range_m, sym, session_name, hold_min):
    """ORB-Retest universel BIDIR : range M15 / breakout M5 / retest M5 / entry."""
    log(f"=== ORB {session_name} {range_h:02d}h{range_m:02d} {sym} START ===")
    m15 = await fetch_m15_at(c, sym, range_h, range_m)
    if m15 is None:
        log(f"  [{sym}] M15 range introuvable, SKIP"); return
    op, or_high, or_low, cl = m15
    range_size = or_high - or_low
    if range_size <= 0:
        log(f"  [{sym}] range nul, SKIP"); return
    log(f"  [{sym}] Range: O={op} H={or_high} L={or_low} C={cl} | range={range_size:.5f}")

    # Phase BREAKOUT
    breakout_t0 = datetime.now(PARIS)
    breakout_side = None; break_time = None
    end_range_dt = datetime.combine(datetime.now(PARIS).date(), dtime(range_h, range_m), tzinfo=PARIS) + timedelta(minutes=15)
    while (datetime.now(PARIS) - breakout_t0).total_seconds() < ORB_BREAKOUT_TIMEOUT * 60:
        await asyncio.sleep(30)
        m5_bars = await fetch_m5_recent(c, sym)
        for bt, m_o, m_h, m_l, m_c in m5_bars:
            if bt < end_range_dt: continue
            if m_c > or_high + ORB_BREAKOUT_DIST_PCT * range_size:
                breakout_side = 'BUY'; break_time = bt
                log(f"  [{sym}] BREAKOUT BUY {bt.strftime('%H:%M')} close={m_c}"); break
            if m_c < or_low - ORB_BREAKOUT_DIST_PCT * range_size:
                breakout_side = 'SELL'; break_time = bt
                log(f"  [{sym}] BREAKOUT SELL {bt.strftime('%H:%M')} close={m_c}"); break
        if breakout_side: break
    if not breakout_side:
        log(f"  [{sym}] Pas de breakout en {ORB_BREAKOUT_TIMEOUT}min, SKIP"); return

    # Phase RETEST
    retest_t0 = datetime.now(PARIS); retest_entry = None
    while (datetime.now(PARIS) - retest_t0).total_seconds() < ORB_RETEST_TIMEOUT * 60:
        await asyncio.sleep(30)
        m5_bars = await fetch_m5_recent(c, sym)
        for bt, m_o, m_h, m_l, m_c in m5_bars:
            if bt <= break_time: continue
            tol = ORB_RETEST_TOL_PCT * range_size
            if breakout_side == 'BUY':
                if or_high - tol <= m_l <= or_high + tol:
                    if (not ORB_REQUIRE_REVERSAL or m_c > m_o) and m_c > or_high:
                        retest_entry = m_c
                        log(f"  [{sym}] RETEST OK {bt.strftime('%H:%M')} low={m_l} close={m_c}"); break
            else:
                if or_low - tol <= m_h <= or_low + tol:
                    if (not ORB_REQUIRE_REVERSAL or m_c < m_o) and m_c < or_low:
                        retest_entry = m_c
                        log(f"  [{sym}] RETEST OK {bt.strftime('%H:%M')} high={m_h} close={m_c}"); break
        if retest_entry: break
    if not retest_entry:
        log(f"  [{sym}] Pas de retest valide, SKIP"); return

    # ENTRY
    sl_d = range_size * 1.05
    tp_d = range_size * 1.5
    lot, status = compute_lot(sym, sl_d, tp_d, tp_min=TP_EUR_MIN_ORB)
    if lot is None:
        log(f"  [{sym}] REJET sizing: {status}"); return
    log(f"  [{sym}] lot={lot:.4f} sl_d={sl_d:.5f} tp_d={tp_d:.5f}")
    p = await open_position(c, sym, breakout_side, lot, sl_d, tp_d, 'A_15', pos_id=1, hold_min=hold_min)
    if p:
        log(f"  [{sym}] *** POSITION ORB {session_name} {breakout_side} #{p.ticket} ***")
        await db_insert_position(p)
        asyncio.create_task(manage_position(c, p, sym, 'OFF', [p], hold_min))


async def db_load_open_positions():
    """Recharge les positions DB encore ouvertes pour reconstruire manage_position tasks."""
    if DRY_RUN: return []
    try:
        conn = await asyncpg.connect(DB_URL)
        rows = await conn.fetch("SELECT pos_key, symbol, action, entry_price, quantity, stop_loss, take_profit, opened_at, extra FROM open_positions WHERE is_open = true AND broker = 'mt5'")
        await conn.close()
        out = []
        for r in rows:
            extra = r['extra'] if isinstance(r['extra'], dict) else json.loads(r['extra'] or '{}')
            ticket = int(extra.get('ticket', 0))
            if ticket == 0: continue
            opened_at = r['opened_at']
            if opened_at.tzinfo is None: opened_at = opened_at.replace(tzinfo=PARIS)
            else: opened_at = opened_at.astimezone(PARIS)
            p = Pos(
                ticket=ticket, sym=r['symbol'], dr=r['action'],
                entry=float(r['entry_price']), lot=float(r['quantity']),
                sl=float(r['stop_loss'] or 0), tp=float(r['take_profit'] or 0),
                sl_initial=float(extra.get('sl_initial', r['stop_loss'] or 0)),
                sl_d=float(extra.get('sl_d', 0)), tp_d=float(extra.get('tp_d', 0)),
                trail_mode=extra.get('trail_mode', 'OFF'),
                pos_id=int(extra.get('pos_id_runner', 1)),
                status='OPEN', opened_at=opened_at,
                hold_min=int(extra.get('hold_min', 120)),
                mode_cascade=extra.get('mode_cascade', 'OFF'),
            )
            out.append(p)
        return out
    except Exception as e:
        log(f"db_load_open_positions err: {e}")
        return []

async def reattach_managers(c, positions):
    """Recree manage_position tasks pour positions chargees au boot."""
    for p in positions:
        log(f"REATTACH manage #{p.ticket} {p.sym} {p.dr} opened_at={p.opened_at.strftime('%H:%M')} hold_min={p.hold_min}")
        asyncio.create_task(manage_position(c, p, p.sym, p.mode_cascade, [p], p.hold_min))


async def connection_monitor(c):
    """Surveille la connexion broker MT5. Si deconnectee >2min -> log critique."""
    log("Connection monitor ON - check every 60s")
    consecutive_disconnects = 0
    while True:
        try:
            a = await c._rpc('account')
            connected = int(a.get('connected', 0))
            if connected == 0:
                consecutive_disconnects += 1
                log(f"[ALERT] MT5 NOT CONNECTED to broker (consecutive={consecutive_disconnects} min)")
                if consecutive_disconnects >= 2:
                    log(f"[CRITICAL] MT5 disconnected 2min+ - need bridge restart manually (docker restart trading-mt5-bridge)")
            else:
                if consecutive_disconnects > 0:
                    log(f"[OK] MT5 reconnected after {consecutive_disconnects} min")
                consecutive_disconnects = 0
        except Exception as e:
            log(f"connection_monitor err: {e}")
        await asyncio.sleep(60)

async def scheduler(c):
    """Scheduler dual : V6 MOMENTUM (M15 momentum) + V6 ORB-RETEST en parallele."""
    log(f"V6 DUAL Runner - {len(SETUPS_MOMENTUM)} MOMENTUM + {len(SETUPS_ORB)} ORB - DRY_RUN={DRY_RUN}")
    fired = set()
    while True:
        now = datetime.now(PARIS)
        for setup in SETUPS:
            strategy = setup[0]
            if strategy == 'MOMENTUM':
                _, h, m, sym, dr, mode, trail, tf, hold_min = setup
                key = (now.date(), 'M', h, m, sym, dr)
                if key in fired: continue
                trigger_dt = now.replace(hour=h, minute=m, second=1, microsecond=0)
                if now >= trigger_dt and now < trigger_dt + timedelta(minutes=5):
                    fired.add(key)
                    if in_rollover_window():
                        fired.add(key)
                        log(f"[SKIP] {sym} {dr} {h:02d}h{m:02d} - dans fenetre rollover Fusion")
                        continue
                    asyncio.create_task(run_setup(c, h, m, sym, dr, mode, trail, tf, hold_min))
            elif strategy == 'ORB':
                _, rh, rm, sym, sname, hmin = setup
                key = (now.date(), 'O', rh, rm, sym)
                if key in fired: continue
                # Trigger 16 min apres open de la range (= 1 min apres close M15)
                trigger_dt = now.replace(hour=rh, minute=rm, second=0, microsecond=0) + timedelta(minutes=16)
                if now >= trigger_dt and now < trigger_dt + timedelta(minutes=5):
                    fired.add(key)
                    if in_rollover_window():
                        fired.add(key)
                        log(f"[SKIP] ORB {sym} {sname} - dans fenetre rollover Fusion")
                        continue
                    asyncio.create_task(run_orb_universal(c, rh, rm, sym, sname, hmin))
        if now.hour == 0 and now.minute < 5:
            fired = {k for k in fired if k[0] == now.date()}
        await asyncio.sleep(20)

async def main():
    c = MT5Client(rep_endpoint='tcp://trading-mt5-bridge:5556', pub_endpoint='tcp://trading-mt5-bridge:5555')
    await c.connect()
    log(f"Connected to MT5 bridge")
    a = await c._rpc('account')
    log(f"Account: balance={a['balance']} EUR equity={a['equity']}")
    # FIX 3 : recharger positions DB et reattacher manage_position tasks
    db_positions = await db_load_open_positions()
    if db_positions:
        log(f"REATTACH {len(db_positions)} positions chargees depuis DB")
        await reattach_managers(c, db_positions)
    # Lancer toutes les coroutines en parallele
    await asyncio.gather(scheduler(c), reconcile_loop(c), commands_loop(c), ipc_loop(c), manage_all_loop(c), d1_breakout_scanner(c), connection_monitor(c))

if __name__ == '__main__':
    asyncio.run(main())
