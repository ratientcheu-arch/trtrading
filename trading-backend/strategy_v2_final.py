"""
Strategy V2 FINAL — Spec utilisateur 2026-05-05.

20 setups TOP, cascade conditionnelle, trail TP progressif, cut-loss 70% et 50-65% sustained 5min.

Regles SL/TP :
  - TP_dist = Fibo natif (high + range pour BUY, low - range pour SELL)
  - SL_dist = max(Fibo, TP_dist/2)  ## R:R floor 2.0
  - Lot sized so SL_eur = €25 (CAP_EUR)
  - Setup rejected si TP_eur < €50

Cascade :
  - Mode COND : pos 2 ouvre si prix atteint 8% TP ET la H1 courante (ou la suivante) est alignee setup
  - Mode FULL : pos 2 ouvre si prix atteint 8% TP, sans condition
  - Pos 3 ouvre selon meme regle, depuis pos 2
  - Chaque pos a son propre TP/SL aux memes distances

Trailing TP (par position) :
  - 15% TP atteint -> SL = entry (BE)
  - 16% TP -> SL = BE + 1% TP_dist
  - X% TP (X >= 15) -> SL = BE + (X - 15)% TP_dist
  - Le SL ne recule jamais

Cut-loss (par position) :
  - Adv >= 70% SL -> EXIT immediat (perte ~17.5€ au lieu de 25€)
  - Adv in [50%, 65%] sustained 5 min sans repasser sous 50% -> EXIT
  - Reset chrono si < 50% ou > 65%
"""
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional


# ============================================================================
# 1. CONFIG
# ============================================================================

CAP_EUR = 25.0
TP_EUR_MIN = 50.0
RR_FLOOR = 2.0
HOLD_HOURS = 2          # timeout par position
CASCADE_PCT = 0.08      # 8% TP atteint -> cascade
TRAIL_START = 0.20      # 20% TP atteint -> BE
SUSTAIN_LOW = 0.50      # zone basse de la fenetre cut-loss sustained
SUSTAIN_HIGH = 0.65     # zone haute
SUSTAIN_DURATION = 300  # 5 min en secondes
EXIT_70 = 0.70          # adv >= 70% -> exit immediat


# ============================================================================
# 2. SETUPS PRODUCTION (20 keepers)
# ============================================================================

# (h, m, symbol, direction, cascade_mode, expected_pnl_90j)
SETUPS_PROD = [
    # ASIE
    ( 2,  0, 'USD/JPY',   'BUY',  'COND', 895),
    ( 2,  0, 'GOLD',      'BUY',  'FULL', 486),  # seul en FULL
    ( 2,  0, 'NASDAQ',    'BUY',  'COND', 353),
    ( 3,  0, 'EUR/USD',   'SELL', 'COND', 577),
    ( 4,  0, 'NASDAQ',    'BUY',  'COND', 1315),
    ( 7,  0, 'AUD/USD',   'BUY',  'COND', 530),
    ( 7,  0, 'GOLD',      'SELL', 'COND', 254),
    # WR85 + EU/US + indices ouverture
    (10,  0, 'EUR/USD',   'SELL', 'COND', 758),
    (10,  0, 'USD/CHF',   'BUY',  'COND', 501),
    (10,  0, 'USD/JPY',   'SELL', 'COND', 500),
    (10, 30, 'NKY',       'SELL', 'COND', 762),
    (11,  0, 'USD/CHF',   'BUY',  'COND', 429),
    (11,  0, 'GBP/JPY',   'SELL', 'COND', 693),
    (13,  0, 'CAC40',     'SELL', 'COND', 307),
    (14,  0, 'OIL_CRUDE', 'BUY',  'COND', 1031),
    (14,  0, 'EUR/USD',   'BUY',  'COND', 379),
    (19,  0, 'GBP/USD',   'SELL', 'COND', 125),
    (19, 30, 'CAC40',     'SELL', 'COND', 1001),
    (21,  0, 'EUR/USD',   'SELL', 'COND', 632),
    (21, 30, 'USD/CHF',   'BUY',  'COND', 1475),
]


# ============================================================================
# 3. SIZING
# ============================================================================

CONTRACTS = {'NASDAQ':1,'GOLD':100,'OIL_CRUDE':1000,'CAC40':1,'NKY':0.063,'US30':1,'HK50':1,'DAX40':1}
FX_TO_EUR = {"USD":1/1.17,"EUR":1.0,"GBP":1/0.86,"JPY":1/(150*1.17),
             "CHF":1.05,"AUD":1/1.65,"CAD":1/1.51,"NZD":1/1.78,"HKD":1/(7.85*1.17)}
QUOTE_OVERRIDE = {'NASDAQ':'USD','GOLD':'USD','OIL_CRUDE':'USD','CAC40':'EUR','NKY':'JPY','US30':'USD','HK50':'HKD','DAX40':'EUR'}
FX_CCYS = {"EUR","USD","GBP","JPY","CHF","AUD","NZD","CAD"}


def is_forex(sym: str) -> bool:
    s = sym.replace("/", "")
    return len(s) == 6 and s.isalpha() and s[:3] in FX_CCYS and s[3:] in FX_CCYS


def get_contract(sym: str) -> float:
    if sym in CONTRACTS: return CONTRACTS[sym]
    if is_forex(sym):
        return 1000 if "JPY" in sym.replace("/","") else 100000
    return 1


def get_quote(sym: str) -> str:
    if sym in QUOTE_OVERRIDE: return QUOTE_OVERRIDE[sym]
    if is_forex(sym): return sym.replace("/","")[3:]
    return 'USD'


def pnl_eur(sym: str, price_diff: float, lot: float) -> float:
    return price_diff * get_contract(sym) * lot * FX_TO_EUR[get_quote(sym)]


def compute_tp_sl(direction: str, h1_high: float, h1_low: float, h1_close: float):
    """Retourne (tp, sl, sl_dist, tp_dist, R:R)."""
    h1r = h1_high - h1_low
    entry = h1_close
    if direction == 'BUY':
        tp = h1_high + h1r
        sl_fibo = h1_low - h1r/2
        sl_d_fibo = entry - sl_fibo
        tp_d = tp - entry
        if sl_d_fibo > 0 and tp_d > 0 and tp_d/sl_d_fibo < RR_FLOOR:
            sl_d = tp_d/RR_FLOOR; sl = entry - sl_d
        else:
            sl_d = sl_d_fibo; sl = sl_fibo
    else:
        tp = h1_low - h1r
        sl_fibo = h1_high + h1r/2
        sl_d_fibo = sl_fibo - entry
        tp_d = entry - tp
        if sl_d_fibo > 0 and tp_d > 0 and tp_d/sl_d_fibo < RR_FLOOR:
            sl_d = tp_d/RR_FLOOR; sl = entry + sl_d
        else:
            sl_d = sl_d_fibo; sl = sl_fibo
    rr = tp_d/sl_d if sl_d > 0 else 0
    return tp, sl, sl_d, tp_d, rr


def compute_lot(sym: str, sl_dist: float, tp_dist: float):
    """Retourne (lot, ok, sl_eur, tp_eur, raison_si_rejet)."""
    sl_eur_per_lot = abs(pnl_eur(sym, sl_dist, 1.0))
    if sl_eur_per_lot <= 0:
        return None, False, 0, 0, "sl_per_lot=0"
    lot = max(0.001, round(CAP_EUR / sl_eur_per_lot, 4))
    sl_eur = abs(pnl_eur(sym, sl_dist, lot))
    tp_eur = abs(pnl_eur(sym, tp_dist, lot))
    if tp_eur < TP_EUR_MIN - 1.0:
        return lot, False, sl_eur, tp_eur, f"TP_eur={tp_eur:.1f}€ < {TP_EUR_MIN}€"
    return lot, True, sl_eur, tp_eur, "OK"


# ============================================================================
# 4. POSITION + STATE MACHINE
# ============================================================================

@dataclass
class Position:
    pos_id: int
    entry: float
    tp: float
    sl: float                # SL courant (peut etre trail)
    sl_initial: float        # SL initial (pour calculer adv%)
    tp_dist: float
    sl_dist: float
    direction: str
    lot: float
    sym: str
    opened_at: datetime
    status: str = 'OPEN'
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_at: Optional[datetime] = None
    sustain_start: Optional[datetime] = None
    trail_active: bool = False
    pnl_eur: float = 0.0


def step_position(p: Position, ts: datetime, bar_open: float, bar_high: float, bar_low: float, bar_close: float, trail_mode='A_15'):
    """
    Avance la position d'une bougie M1. Retourne ('open', None) tant que pos est ouverte,
    sinon ('closed', exit_reason).
    """
    if p.status != 'OPEN':
        return 'closed', p.exit_reason
    sign = 1 if p.direction == 'BUY' else -1
    # Excursions intra-bar
    if p.direction == 'BUY':
        adverse = max(0, p.entry - bar_low)
        favorable = max(0, bar_high - p.entry)
    else:
        adverse = max(0, bar_high - p.entry)
        favorable = max(0, p.entry - bar_low)
    sl_dist_init = p.sl_dist
    tp_dist = p.tp_dist
    adv_pct = adverse / sl_dist_init if sl_dist_init > 0 else 0
    fav_pct = favorable / tp_dist if tp_dist > 0 else 0

    # 1. Trailing TP — 4 modes
    new_sl = None
    if trail_mode == 'A_30':
        # A : Trail demarre a 30%, formule 1:1 (BE + (fav-30%) * TP)
        if fav_pct >= 0.30:
            p.trail_active = True
            new_sl = p.entry + sign * (fav_pct - 0.30) * tp_dist
    elif trail_mode == 'B_slow':
        # B : Trail demarre 15%, coefficient 1:3 (lent)
        if fav_pct >= 0.15:
            p.trail_active = True
            new_sl = p.entry + sign * (fav_pct - 0.15) * tp_dist / 3.0
    elif trail_mode == 'C_step5':
        # C : Trail par paliers de 5% TP (BE@15%, +5@20%, +10@25%, +15@30% ...)
        if fav_pct >= 0.15:
            p.trail_active = True
            steps_done = int((fav_pct - 0.15) / 0.05)  # 0,1,2,3...
            lock_pct = steps_done * 0.05
            new_sl = p.entry + sign * lock_pct * tp_dist
    elif trail_mode == 'D_palier':
        # D : Paliers discrets : SL locke a 0% (BE) au 30%, +20% au 40%, +30% au 50%, +40% au 60%
        if fav_pct >= 0.60:
            new_sl = p.entry + sign * 0.40 * tp_dist
        elif fav_pct >= 0.50:
            new_sl = p.entry + sign * 0.30 * tp_dist
        elif fav_pct >= 0.40:
            new_sl = p.entry + sign * 0.20 * tp_dist
        elif fav_pct >= 0.30:
            new_sl = p.entry  # BE
        if new_sl is not None: p.trail_active = True
    else:  # default A_15
        if fav_pct >= 0.15:
            p.trail_active = True
            new_sl = p.entry + sign * (fav_pct - 0.15) * tp_dist
    if new_sl is not None:
        if p.direction == 'BUY' and new_sl > p.sl: p.sl = new_sl
        elif p.direction == 'SELL' and new_sl < p.sl: p.sl = new_sl

    # 2. SL hard touche (ou trail)
    if p.direction == 'BUY' and bar_low <= p.sl:
        p.status = 'CLOSED'
        p.exit_price = p.sl
        p.exit_reason = 'SL_TRAIL' if p.trail_active else 'SL'
        p.exit_at = ts
        return 'closed', p.exit_reason
    if p.direction == 'SELL' and bar_high >= p.sl:
        p.status = 'CLOSED'; p.exit_price = p.sl
        p.exit_reason = 'SL_TRAIL' if p.trail_active else 'SL'
        p.exit_at = ts; return 'closed', p.exit_reason

    # 3. TP touche
    if p.direction == 'BUY' and bar_high >= p.tp:
        p.status = 'CLOSED'; p.exit_price = p.tp
        p.exit_reason = 'TP'; p.exit_at = ts
        return 'closed', 'TP'
    if p.direction == 'SELL' and bar_low <= p.tp:
        p.status = 'CLOSED'; p.exit_price = p.tp
        p.exit_reason = 'TP'; p.exit_at = ts
        return 'closed', 'TP'

    # 4. Cut-loss EXIT_70
    if adv_pct >= EXIT_70:
        exit_price = p.entry - sign * EXIT_70 * sl_dist_init
        p.status = 'CLOSED'; p.exit_price = exit_price
        p.exit_reason = 'EXIT_70'; p.exit_at = ts
        return 'closed', 'EXIT_70'

    # 5. Cut-loss sustained 50-65%
    if SUSTAIN_LOW <= adv_pct <= SUSTAIN_HIGH:
        if p.sustain_start is None:
            p.sustain_start = ts
        elif (ts - p.sustain_start).total_seconds() >= SUSTAIN_DURATION:
            exit_price = p.entry - sign * adv_pct * sl_dist_init
            p.status = 'CLOSED'; p.exit_price = exit_price
            p.exit_reason = 'EXIT_5MIN'; p.exit_at = ts
            return 'closed', 'EXIT_5MIN'
    else:
        p.sustain_start = None

    return 'open', None


# ============================================================================
# 5. CASCADE
# ============================================================================

def should_cascade(mode: str, h1_open_now: float, h1_close_now: float, direction: str) -> bool:
    """Decide si la cascade peut declencher selon le mode et la H1 courante."""
    if mode == 'OFF': return False
    if mode == 'FULL': return True
    if mode == 'COND':
        # H1 courante alignee setup direction
        return (h1_close_now > h1_open_now) if direction == 'BUY' else (h1_close_now < h1_open_now)
    return False


def cascade_threshold(direction: str, ref_price: float, tp_dist: float) -> float:
    sign = 1 if direction == 'BUY' else -1
    return ref_price + sign * CASCADE_PCT * tp_dist


# ============================================================================
# 6. AUDIT (verif sizing pour les 20 setups prod)
# ============================================================================

def audit():
    """Audit avec ranges H1 reels approximatifs (depuis bt_idx_real moyennes)."""
    AVG_RANGES = {
        'EUR/USD': 0.0012, 'GBP/USD': 0.0015, 'USD/JPY': 0.20, 'GBP/JPY': 0.30,
        'AUD/USD': 0.0011, 'USD/CHF': 0.0011, 'EUR/JPY': 0.30,
        'GOLD': 6.0, 'NASDAQ': 50.0, 'OIL_CRUDE': 1.0, 'CAC40': 25.0, 'NKY': 80.0,
    }
    print(f"\n{'='*120}")
    print(f"AUDIT 20 SETUPS PROD | cap={CAP_EUR}€ SL | TP_min={TP_EUR_MIN}€ | R:R={RR_FLOOR} | cascade=8%, trail=15%, sustain=5min")
    print(f"{'='*120}")
    print(f"{'Trig':<7} {'Sym':<11} {'Dir':<5} {'Mode':<5} {'Range H1':<10} {'SL_dist':<10} {'TP_dist':<10} {'Lot':<10} {'SL €':<8} {'TP €':<8} {'Status':<25}")
    print('-'*120)
    ok_count = 0
    for h, m, sym, dr, mode, ref in SETUPS_PROD:
        rng = AVG_RANGES.get(sym, 0.0015)
        # Simu H1 generique
        if 'JPY' in sym.replace('/',''):
            base = 150.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        elif sym == 'GOLD':
            base = 2400.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        elif sym == 'NASDAQ':
            base = 18000.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        elif sym == 'OIL_CRUDE':
            base = 80.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        elif sym == 'CAC40':
            base = 7800.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        elif sym == 'NKY':
            base = 38000.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        else:
            base = 1.0; lo = base; hi = base + rng; cl = (lo+hi)/2
        tp, sl, sl_d, tp_d, rr = compute_tp_sl(dr, hi, lo, cl)
        lot, ok, sl_eur, tp_eur, status = compute_lot(sym, sl_d, tp_d)
        if ok: ok_count += 1
        lot_s = f"{lot:.4f}" if lot else "REJ"
        print(f"{h:02d}h{m:02d}  {sym:<11} {dr:<5} {mode:<5} {rng:<10.5f} {sl_d:<10.5f} {tp_d:<10.5f} {lot_s:<10} -{sl_eur:<7.2f} +{tp_eur:<7.2f} {status:<25}")
    print('-'*120)
    print(f"{ok_count}/{len(SETUPS_PROD)} setups OK pour deploiement")


if __name__ == '__main__':
    audit()
