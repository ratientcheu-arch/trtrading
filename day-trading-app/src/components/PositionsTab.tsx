/**
 * Mes Positions — Positions ouvertes LIVE (refresh 2s) avec barre SL↔TP,
 * trades fermés permanents du plus récent au plus ancien.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { Clock, CheckCircle, Activity, XCircle, Square, Calendar, Filter, ArrowDown, Target, Shield, Timer, Zap, TrendingUp, TrendingDown } from 'lucide-react';

const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || '';
const API_KEY = import.meta.env.VITE_API_KEY || '';
const hdrs = { 'Authorization': `Bearer ${API_KEY}` };

interface LivePos {
  symbol: string; action: string; quantity: number;
  entry_price: number; current_price: number; stop_loss: number; take_profit: number;
  entry_time?: string; pnl: number; pnl_percent: number;
  margin: number; exposure: number; leverage_used: string; broker: string;
  ticket?: number; market_category?: string;
  asset_type?: string; pnl_conv_rate?: number;
  sl_pnl_eur?: number; tp_pnl_eur?: number;
}

interface ClosedTrade {
  symbol: string; action: string; entry_price: number; exit_price: number;
  quantity: number; pnl: number; commission?: number;
  reason: string; entry_time: string; exit_time: string;
  duration_min?: number; broker: string; category?: string; market_category?: string;
}

interface DayGroup {
  date: string; trades: ClosedTrade[]; pnl: number;
  trades_count: number; wins: number; losses: number;
  win_rate: number; forex_pnl?: number; indices_pnl?: number;
}

interface HistoryData {
  days: DayGroup[];
  summary: { total_pnl: number; total_trades: number; wins: number; losses: number; win_rate: number };
}

function parseExitMode(reason: string | undefined): string {
  if (!reason) return 'OTHER';
  const r = reason.toLowerCase();
  if (r.includes('stop_loss') || r.includes('sl_hit') || r.includes('stop loss') || r === 'sl') return 'SL';
  if (r.includes('take_profit') || r.includes('tp_hit') || r.includes('take profit') || r === 'tp') return 'TP';
  if (r.includes('timeout') || r.includes('time_limit') || r.includes('max_hold') || r.includes('expir')) return 'TIMEOUT';
  if (r.includes('stage') || r.includes('trailing') || r.includes('partial')) return 'STAGE';
  if (r.includes('manual') || r.includes('user') || r.includes('forced') || r.includes('close_all')) return 'MANUAL';
  return 'OTHER';
}

const EXIT_CFG: Record<string, { label: string; bg: string; text: string; icon: typeof Target }> = {
  SL: { label: 'STOP LOSS', bg: 'bg-red-100', text: 'text-red-700', icon: Shield },
  TP: { label: 'TAKE PROFIT', bg: 'bg-green-100', text: 'text-green-700', icon: Target },
  TIMEOUT: { label: 'TIMEOUT', bg: 'bg-orange-100', text: 'text-orange-700', icon: Timer },
  STAGE: { label: 'STAGE', bg: 'bg-purple-100', text: 'text-purple-700', icon: Zap },
  MANUAL: { label: 'MANUEL', bg: 'bg-gray-100', text: 'text-gray-700', icon: XCircle },
  OTHER: { label: 'CLOTURE', bg: 'bg-gray-100', text: 'text-gray-600', icon: CheckCircle },
};

const BROKERS = [
  { key: 'all', label: 'Tous' },
  { key: 'mt5', label: 'Fusion Markets' },
];

export default function PositionsTab() {
  const [livePositions, setLivePositions] = useState<LivePos[]>([]);
  const [history, setHistory] = useState<HistoryData | null>(null);
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [period, setPeriod] = useState(7);
  const [brokerFilter, setBrokerFilter] = useState('all');
  const [closing, setClosing] = useState<string | null>(null);
  const [expandedTicket, setExpandedTicket] = useState<number | null>(null);
  const prevPnl = useRef<Record<string, number>>({});

  // Fast refresh for live positions (every 2s)
  const fetchPositions = useCallback(async () => {
    try {
      const resp = await fetch(`${BACKEND_URL}/api/positions/live`, { headers: hdrs, cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        setLivePositions(data);
        // Track previous PnL for flash effect
        const pnlMap: Record<string, number> = {};
        for (const p of data) pnlMap[p.ticket || p.symbol] = p.pnl;
        prevPnl.current = pnlMap;
      }
    } catch (e) { console.error('Fetch positions error:', e); }
    setLoading(false);
  }, []);

  // Slower refresh for history (every 60s)
  const fetchHistory = useCallback(async (days: number) => {
    setHistoryLoading(true);
    try {
      const resp = await fetch(`${BACKEND_URL}/api/trades/history?days=${days}`, { headers: hdrs, cache: 'no-store' });
      if (resp.ok) setHistory(await resp.json());
    } catch (e) { console.error('Fetch history error:', e); }
    setHistoryLoading(false);
  }, []);

  useEffect(() => {
    fetchPositions();
    fetchHistory(period);
    const fast = setInterval(fetchPositions, 2000);   // Positions: every 2s
    const slow = setInterval(() => fetchHistory(period), 60000); // History: every 60s
    return () => { clearInterval(fast); clearInterval(slow); };
  }, [fetchPositions, fetchHistory, period]);

  const handlePeriodChange = (days: number) => {
    setPeriod(days);
    fetchHistory(days);
  };

  const closePosition = async (symbol: string) => {
    if (!confirm(`Fermer la position ${symbol} ?`)) return;
    setClosing(symbol);
    try {
      await fetch(`${BACKEND_URL}/api/positions/close`, {
        method: 'POST', headers: { ...hdrs, 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol }),
      });
      setTimeout(fetchPositions, 500);
    } catch (e) { console.error('Close error:', e); }
    setClosing(null);
  };

  const closeAll = async () => {
    if (!confirm(`Fermer TOUTES les ${livePositions.length} positions ?`)) return;
    setClosing('ALL');
    for (const p of livePositions) {
      try {
        await fetch(`${BACKEND_URL}/api/positions/close`, {
          method: 'POST', headers: { ...hdrs, 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbol: p.symbol }),
        });
      } catch (e) { console.error('Close error:', e); }
    }
    setTimeout(fetchPositions, 1000);
    setClosing(null);
  };

  // Closed trades from history
  const allClosed: (ClosedTrade & { exit_mode: string })[] = [];
  if (history) {
    for (const day of history.days) {
      for (const t of day.trades) {
        allClosed.push({ ...t, exit_mode: parseExitMode(t.reason) });
      }
    }
  }
  allClosed.sort((a, b) => {
    const tA = safeTs(a.exit_time) || safeTs(a.entry_time) || 0;
    const tB = safeTs(b.exit_time) || safeTs(b.entry_time) || 0;
    return tB - tA;
  });

  // Sort open: most recently opened first
  const sortedOpen = [...livePositions].sort((a, b) =>
    (safeTs(b.entry_time) || 0) - (safeTs(a.entry_time) || 0)
  );

  // Filter
  const filteredOpen = brokerFilter === 'all' ? sortedOpen
    : sortedOpen.filter(p => (p.broker || '').toLowerCase().includes(brokerFilter));
  const filteredClosed = brokerFilter === 'all' ? allClosed
    : allClosed.filter(t => (t.broker || '').toLowerCase().includes(brokerFilter));

  // Stats
  const totalOpenPnl = livePositions.reduce((s, p) => s + p.pnl, 0);
  const totalMargin = livePositions.reduce((s, p) => s + (p.margin || 0), 0);
  const summary = history?.summary || { total_pnl: 0, total_trades: 0, wins: 0, losses: 0, win_rate: 0 };
  const ctPnl = allClosed.reduce((s, t) => s + t.pnl, 0);

  // Exit mode stats
  const exitStats = filteredClosed.reduce((acc, t) => {
    acc[t.exit_mode] = (acc[t.exit_mode] || 0) + 1; return acc;
  }, {} as Record<string, number>);

  // Potential TP total — prefer backend-computed tp_pnl_eur, fallback to client formula
  const potentialTP = livePositions.reduce((s, p) => {
    if (typeof p.tp_pnl_eur === 'number') {
      return s + p.tp_pnl_eur;
    }
    if (p.take_profit > 0 && p.entry_price > 0) {
      const isBuy = p.action === 'BUY';
      const tpDist = isBuy ? p.take_profit - p.entry_price : p.entry_price - p.take_profit;
      const conv = p.pnl_conv_rate ?? 0.87;
      return s + tpDist * p.quantity * conv;
    }
    return s;
  }, 0);

  if (loading) return <div className="text-center py-12 text-gray-400">Chargement...</div>;

  return (
    <div className="space-y-4">
      {/* KPI Resume */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="text-[10px] text-gray-500">P&L Total ({period}J)</div>
          <div className={`text-lg font-bold ${pc(summary.total_pnl)}`}>
            {fmt(summary.total_pnl)}€
          </div>
        </div>
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="text-[10px] text-gray-500 flex items-center gap-1">
            <span className="w-2 h-2 rounded-full bg-green-500 inline-block" /> Fusion Markets
          </div>
          <div className={`text-lg font-bold ${pc(ctPnl)}`}>{fmt(ctPnl)}€</div>
          <div className="text-[10px] text-gray-400">{allClosed.length} trades</div>
        </div>
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="text-[10px] text-gray-500">Ouvertes</div>
          <div className="text-lg font-bold text-blue-600">{livePositions.length}</div>
          <div className={`text-[10px] ${pc(totalOpenPnl)}`}>{fmt(totalOpenPnl)}€</div>
        </div>
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="text-[10px] text-gray-500">Win Rate</div>
          <div className={`text-lg font-bold ${summary.win_rate >= 55 ? 'text-green-600' : summary.win_rate >= 45 ? 'text-orange-500' : 'text-red-600'}`}>
            {summary.win_rate > 0 ? summary.win_rate.toFixed(0) + '%' : '-'}
          </div>
          <div className="text-[10px] text-gray-400">{summary.wins}W / {summary.losses}L</div>
        </div>
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="text-[10px] text-gray-500">Modes de sortie</div>
          {(() => {
            const totalExit = Object.values(exitStats).reduce((a, b) => a + b, 0);
            return totalExit > 0 ? (
              <div className="flex flex-wrap gap-1 mt-1">
                {Object.entries(exitStats).map(([mode, count]) => {
                  const cfg = EXIT_CFG[mode] || EXIT_CFG.OTHER;
                  const pct = Math.round((count / totalExit) * 100);
                  return <span key={mode} className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${cfg.bg} ${cfg.text}`}>{pct}% {cfg.label}</span>;
                })}
              </div>
            ) : <span className="text-gray-400 text-xs">-</span>;
          })()}
        </div>
      </div>

      {/* Filters bar */}
      <div className="bg-white rounded-xl shadow-sm border p-3 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Filter className="w-4 h-4 text-gray-400" />
          <span className="text-xs text-gray-500 font-medium">Broker:</span>
          {BROKERS.map(b => (
            <button key={b.key} onClick={() => setBrokerFilter(b.key)}
              className={`px-3 py-1.5 text-xs font-bold rounded-lg transition-colors ${brokerFilter === b.key ? 'bg-blue-600 text-white shadow-sm' : 'bg-gray-100 text-gray-500 hover:bg-gray-200'}`}>
              {b.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <Calendar className="w-4 h-4 text-gray-400" />
          <span className="text-xs text-gray-500 font-medium">Periode:</span>
          {[1, 3, 7, 14, 30].map(d => (
            <button key={d} onClick={() => handlePeriodChange(d)}
              className={`px-2.5 py-1.5 text-xs font-bold rounded-lg transition-colors ${period === d ? 'bg-blue-600 text-white shadow-sm' : 'bg-gray-100 text-gray-500 hover:bg-gray-200'}`}>
              {d}J
            </button>
          ))}
        </div>
        {livePositions.length > 0 && (
          <button onClick={closeAll} disabled={closing === 'ALL'}
            className="px-3 py-1.5 text-xs font-bold bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 flex items-center gap-1 shadow-sm">
            <Square className="w-3 h-3" /> {closing === 'ALL' ? 'Fermeture...' : `Fermer tout (${livePositions.length})`}
          </button>
        )}
      </div>

      {/* ═══ POSITIONS OUVERTES — LIVE ═══ */}
      <div className="bg-white rounded-xl shadow-sm border overflow-hidden">
        <div className="px-4 py-2.5 bg-gradient-to-r from-blue-50 to-indigo-50 border-b flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-600 animate-pulse" />
            <span className="text-sm font-bold text-blue-800">
              Positions Ouvertes — Fusion Markets MT5 ({filteredOpen.length})
            </span>
          </div>
          <div className="flex items-center gap-4 text-xs">
            <span className="text-gray-500">Marge: <b>{totalMargin.toFixed(0)} €</b></span>
            <span className={`font-bold ${pc(totalOpenPnl)}`}>P&L: {fmt(totalOpenPnl)} €</span>
            {potentialTP > 0 && (
              <span className="text-green-600 font-bold">Pot. TP: {fmt(potentialTP)}€</span>
            )}
          </div>
        </div>

        {filteredOpen.length > 0 ? (
          <div className="divide-y">
            {filteredOpen.map((p, i) => {
              const dec = decs(p.symbol, p.entry_price);
              const isBuy = p.action === 'BUY';
              const atype = p.asset_type || 'unknown';
              const isFx = atype === 'forex' || p.symbol.includes('/');
              const sym = p.symbol.toUpperCase().replace('/', '');
              const pipSize = sym.includes('JPY') ? 0.01 : (isFx ? 0.0001 : 1);
              const pips = isBuy
                ? (p.current_price - p.entry_price) / pipSize
                : (p.entry_price - p.current_price) / pipSize;

              // Progress bar: entry point = center, red left (loss), green right (gain)
              const sl = p.stop_loss || 0;
              const tp = p.take_profit || 0;
              let slPnl = 0;
              let tpPnl = 0;
              // entryPct = position of entry between SL and TP (0-100%)
              let entryPct = 50;
              // currentPct = position of current price between SL and TP (0-100%)
              let currentPct = 50;
              if (sl > 0 && tp > 0) {
                const range = Math.abs(tp - sl);
                if (range > 0) {
                  if (isBuy) {
                    entryPct = Math.max(0, Math.min(100, ((p.entry_price - sl) / range) * 100));
                    currentPct = Math.max(0, Math.min(100, ((p.current_price - sl) / range) * 100));
                  } else {
                    entryPct = Math.max(0, Math.min(100, ((sl - p.entry_price) / (sl - tp)) * 100));
                    currentPct = Math.max(0, Math.min(100, ((sl - p.current_price) / (sl - tp)) * 100));
                  }
                }
                // 2026-04-23 — prefer backend pre-computed SL/TP € (correct lots→units
                // conversion server-side). Fallback to frontend formula if absent.
                if (typeof p.sl_pnl_eur === 'number' && typeof p.tp_pnl_eur === 'number') {
                  slPnl = p.sl_pnl_eur;
                  tpPnl = p.tp_pnl_eur;
                } else {
                  const slDist = isBuy ? sl - p.entry_price : p.entry_price - sl;
                  const tpDist = isBuy ? tp - p.entry_price : p.entry_price - tp;
                  const conv = p.pnl_conv_rate ?? 0.87;
                  slPnl = slDist * p.quantity * conv;
                  tpPnl = tpDist * p.quantity * conv;
                }
              }

              const lev = p.leverage_used || '--';
              const dur = holdMinutes(p.entry_time || '');
              const key = p.ticket || `${p.symbol}-${i}`;
              const expanded = expandedTicket === p.ticket;

              return (
                <div key={key} className="transition-all">
                  <div className="px-4 py-3 hover:bg-blue-50/40 transition-colors cursor-pointer"
                    onClick={() => setExpandedTicket(expanded ? null : (p.ticket || null))}>

                    {/* Main row */}
                    <div className="flex items-center gap-3">
                      {/* Color bar */}
                      <div className={`w-1.5 h-16 rounded-full flex-shrink-0 ${p.pnl >= 0 ? 'bg-green-500' : 'bg-red-500'}`} />

                      {/* Symbol block */}
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 mb-0.5">
                          <span className="font-bold text-base text-gray-900">{p.symbol}</span>
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${isBuy ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                            {p.action}
                          </span>
                          <span className="text-[9px] text-gray-400 bg-gray-100 px-1 py-0.5 rounded">
                            {atype === 'forex' ? 'Forex' : atype === 'index_cfd' ? 'Index' : atype === 'commodity' ? 'Commodity' : atype === 'stock' ? 'Stock' : 'Other'}
                          </span>
                        </div>
                        <div className="text-[11px] text-gray-500 flex items-center gap-2 flex-wrap">
                          <span>Entree: <b>{p.entry_price.toFixed(dec)}</b></span>
                          <span>Actuel: <b className={p.pnl >= 0 ? 'text-green-600' : 'text-red-600'}>{p.current_price.toFixed(dec)}</b></span>
                          <span>Marge: <b>{p.margin.toFixed(0)} €</b></span>
                          <span>Levier: <b>{lev}</b></span>
                        </div>
                        <div className="text-[11px] text-gray-500 flex items-center gap-2 flex-wrap mt-0.5">
                          <span>SL: <b className="text-red-500">{sl > 0 ? sl.toFixed(dec) : '--'}</b></span>
                          <span>TP: <b className="text-green-500">{tp > 0 ? tp.toFixed(dec) : '--'}</b></span>
                          <span>Qty: <b>{p.quantity}</b></span>
                          <span>Pips: <b className={pips >= 0 ? 'text-green-600' : 'text-red-600'}>{pips >= 0 ? '+' : ''}{pips.toFixed(1)}</b></span>
                        </div>
                      </div>

                      {/* P&L + close */}
                      <div className="flex-shrink-0 text-right min-w-[100px]">
                        <div className={`text-xl font-bold ${pc(p.pnl)} transition-all`}>
                          {fmt(p.pnl)} €
                        </div>
                        <div className={`text-[10px] font-medium ${pc(p.pnl_percent)}`}>
                          {p.pnl_percent >= 0 ? '+' : ''}{p.pnl_percent.toFixed(2)}%
                        </div>
                        {tp > 0 && (
                          <div className="text-[9px] text-green-600 font-medium">
                            TP: {fmt(tpPnl)}€
                          </div>
                        )}
                      </div>

                      {/* Duration + close button */}
                      <div className="flex-shrink-0 flex flex-col items-end gap-1.5">
                        <div className="text-[10px] text-gray-400 flex items-center gap-1">
                          <Clock className="w-3 h-3" />
                          {fmtDur(dur)}
                        </div>
                        <button onClick={(e) => { e.stopPropagation(); closePosition(p.symbol); }}
                          disabled={closing === p.symbol || closing === 'ALL'}
                          className="px-3 py-1.5 rounded-lg bg-red-500 text-white text-[11px] font-bold hover:bg-red-600 disabled:opacity-50 flex items-center gap-1 shadow-sm transition-colors">
                          <XCircle className="w-3.5 h-3.5" />
                          {closing === p.symbol ? '...' : 'Fermer'}
                        </button>
                      </div>
                    </div>

                    {/* Progress bar — entry = pivot, red grows LEFT, green grows RIGHT */}
                    {sl > 0 && tp > 0 && (() => {
                      // 2026-05-01 v6: BARRE DYNAMIQUE avec zoom adaptatif
                      // - Initial (current=entry) : view -20% DD a +50% TP (vue large)
                      // - Profit P% TP : view max(5, 20-P)% DD a max(20, P+10)% TP (zoom suit current)
                      // - DD D% : view max(20, D+10)% DD a max(20, 30-D)% TP
                      // Paliers TP infinis (5%, 9%, 13%, ... pas de cap, suit TP extension chain)
                      const PALIER_BASE = [5,9,13,17,21,25,29,33,37,41,45,49,53,57,61,65,69,73,77,81,85,89,93,97,101,105,109,113,117,121,125,129,133,137,141,145];
                      const labelPaliers = new Map<number, number>([[5,5],[10,9],[25,25],[50,49],[75,73],[95,97],[100,101],[125,125]]);
                      const DD_STEPS = [5, 10, 15];

                      // Compute progress (positif = TP%, negatif = DD%)
                      const tpDistPrice = isBuy ? tp - p.entry_price : p.entry_price - tp;
                      const slDistPrice = isBuy ? p.entry_price - sl : sl - p.entry_price;
                      const tpProg = tpDistPrice > 0
                        ? (isBuy ? ((p.current_price - p.entry_price) / tpDistPrice) * 100
                                 : ((p.entry_price - p.current_price) / tpDistPrice) * 100)
                        : 0;
                      const ddProg = slDistPrice > 0
                        ? (isBuy ? Math.max(0, ((p.entry_price - p.current_price) / slDistPrice) * 100)
                                 : Math.max(0, ((p.current_price - p.entry_price) / slDistPrice) * 100))
                        : 0;

                      // Vue dynamique
                      let viewLeftDd: number, viewRightTp: number;
                      if (tpProg <= 0 && ddProg === 0) {
                        // Juste ouvert : vue large -20 a +50
                        viewLeftDd = 20;
                        viewRightTp = 50;
                      } else if (tpProg > 0) {
                        // En profit : zoom sur droite
                        viewRightTp = Math.max(20, Math.ceil(tpProg + 10));
                        viewLeftDd = Math.max(5, 20 - Math.floor(tpProg));
                      } else {
                        // En perte : zoom sur gauche
                        viewLeftDd = Math.max(20, Math.ceil(ddProg + 10));
                        viewRightTp = Math.max(20, 30 - Math.floor(ddProg));
                      }
                      const viewTotal = viewLeftDd + viewRightTp;

                      // Helpers conversion : valeur en % TP/DD -> position dans la barre [0..100]
                      const toBarTp = (tpPct: number) => ((viewLeftDd + tpPct) / viewTotal) * 100;
                      const toBarDd = (ddPct: number) => ((viewLeftDd - ddPct) / viewTotal) * 100;
                      const entryBar = (viewLeftDd / viewTotal) * 100;
                      const currentBar = tpProg >= 0 ? toBarTp(tpProg) : toBarDd(ddProg);

                      // Palier courant atteint par le PRIX (theorique)
                      const reachedPrice = PALIER_BASE.filter(p => tpProg >= p);
                      const palierAtteint = reachedPrice.length > 0 ? reachedPrice[reachedPrice.length - 1] : 0;

                      // VRAI lock = verifier que SL a ete bouge en zone profit (entry +/- buffer)
                      // BUY: SL > entry+buffer → locked; SELL: SL < entry-buffer → locked
                      const slDistFromEntry = isBuy ? sl - p.entry_price : p.entry_price - sl;
                      const slInProfitZone = slDistFromEntry > 0;  // SL au-dessus entry pour BUY (locked)
                      // Si SL en zone profit, calcul du palier reellement locked depuis position SL
                      let currentPalier = 0;
                      let lockedNetEur = 0;
                      if (slInProfitZone) {
                        // SL position en % du TP = (sl - entry) / (tp - entry) * 100 pour BUY
                        const slPalierPct = isBuy
                          ? ((sl - p.entry_price) / (tp - p.entry_price)) * 100
                          : ((p.entry_price - sl) / (p.entry_price - tp)) * 100;
                        // Trouver le palier correspondant
                        const matched = PALIER_BASE.filter(p => slPalierPct >= p);
                        currentPalier = matched.length > 0 ? matched[matched.length - 1] : 0;
                        lockedNetEur = currentPalier > 0 ? (currentPalier / 5) * 4 : 0;
                      }

                      return (
                      <div className="mt-2 mx-8">
                        <div className="relative h-4 bg-gray-200 rounded-full">
                          {/* LOSS: red bar grows LEFT from entry */}
                          {tpProg < 0 && currentBar < entryBar && (
                            <div className="absolute top-0 h-full bg-gradient-to-l from-red-400 to-red-600 transition-all duration-500"
                              style={{
                                left: `${Math.max(0, currentBar)}%`,
                                width: `${Math.max(0, entryBar - currentBar)}%`,
                                borderRadius: '9999px 0 0 9999px',
                              }} />
                          )}
                          {/* GAIN: green bar grows RIGHT from entry */}
                          {tpProg > 0 && currentBar > entryBar && (
                            <div className="absolute top-0 h-full bg-gradient-to-r from-green-400 to-green-600 transition-all duration-500"
                              style={{
                                left: `${entryBar}%`,
                                width: `${Math.min(100, currentBar) - entryBar}%`,
                                borderRadius: '0 9999px 9999px 0',
                              }} />
                          )}

                          {/* TP-side paliers (only those visible in view) */}
                          {PALIER_BASE.filter(step => step <= viewRightTp).map(step => {
                            const leftPct = toBarTp(step);
                            const isReached = tpProg >= step;
                            const isLockedSl = step === currentPalier;
                            const labelValue = Array.from(labelPaliers.entries()).find(([_, ps]) => ps === step)?.[0];
                            const isKey = labelValue !== undefined;
                            return (
                              <div key={`p${step}`}
                                className="absolute top-0 bottom-0 z-[5]"
                                style={{ left: `calc(${leftPct}% - 0.5px)`, width: '1px' }}>
                                <div className={`h-full ${
                                  isLockedSl ? 'bg-orange-500'
                                  : labelValue === 5 ? 'bg-emerald-500'
                                  : labelValue === 10 ? 'bg-emerald-600'
                                  : isReached ? 'bg-emerald-700'
                                  : 'bg-gray-400/40'
                                }`} style={{ width: (isKey || isLockedSl) ? '2px' : '1px', marginLeft: (isKey || isLockedSl) ? '-0.5px' : '0' }} />
                                {isKey && (
                                  <div className={`absolute -top-[8px] left-1/2 -translate-x-1/2 text-[8px] font-bold whitespace-nowrap ${
                                    labelValue === 5 ? 'text-emerald-600'
                                    : labelValue === 10 ? 'text-emerald-700'
                                    : labelValue! >= 100 ? 'text-purple-600'
                                    : 'text-gray-500'
                                  }`}>{labelValue}%</div>
                                )}
                                {isLockedSl && (
                                  <div className="absolute top-full mt-[1px] left-1/2 -translate-x-1/2 text-[8px] font-bold text-orange-600 whitespace-nowrap">SL</div>
                                )}
                              </div>
                            );
                          })}

                          {/* DD markers (-5%, -10%, -15%) - only those visible in view */}
                          {DD_STEPS.filter(d => d <= viewLeftDd).map(ddStep => {
                            const leftPct = toBarDd(ddStep);
                            const isHit = ddProg >= ddStep;
                            const isCritical = ddStep === 15;
                            const isWarn = ddStep === 10;
                            return (
                              <div key={`dd${ddStep}`}
                                className="absolute top-0 bottom-0 z-[5]"
                                style={{ left: `calc(${leftPct}% - 0.5px)`, width: '1px' }}>
                                <div className={`h-full ${
                                  isCritical ? (isHit ? 'bg-red-700' : 'bg-red-500')
                                  : isWarn ? (isHit ? 'bg-orange-700' : 'bg-orange-400')
                                  : (isHit ? 'bg-amber-600' : 'bg-amber-400/70')
                                }`} style={{ width: '2px', marginLeft: '-0.5px' }} />
                                <div className={`absolute -top-[8px] left-1/2 -translate-x-1/2 text-[8px] font-bold whitespace-nowrap ${
                                  isCritical ? 'text-red-600'
                                  : isWarn ? 'text-orange-600'
                                  : 'text-amber-600'
                                }`}>-{ddStep}%</div>
                              </div>
                            );
                          })}

                          {/* Entry marker */}
                          <div className="absolute top-[-3px] bottom-[-3px] w-[4px] bg-blue-600 z-10 rounded-full shadow"
                            style={{ left: `calc(${entryBar}% - 2px)` }}>
                            <div className="absolute -top-[8px] left-1/2 -translate-x-1/2 text-[8px] font-bold text-blue-600 whitespace-nowrap">E</div>
                          </div>
                          {/* Current price dot */}
                          <div className={`absolute top-1/2 -translate-y-1/2 w-4 h-4 rounded-full border-2 border-white shadow-lg z-20 transition-all duration-500 ${p.pnl >= 0 ? 'bg-green-500' : 'bg-red-500'}`}
                            style={{ left: `calc(${Math.max(0, Math.min(100, currentBar))}% - 8px)` }} />
                        </div>
                        <div className="flex justify-between mt-3 text-[9px]">
                          <span className="text-red-500 font-medium">SL: {sl.toFixed(dec)} ({slPnl >= 0 ? '+' : ''}{slPnl.toFixed(2)}€)</span>
                          {currentPalier > 0 && (
                            <span className="text-orange-600 font-bold">
                              [v2] SL palier {currentPalier}% · LOCKED NET +{lockedNetEur.toFixed(2)}€
                            </span>
                          )}
                          {palierAtteint > 0 && currentPalier === 0 && (
                            <span className="text-amber-600 font-bold">
                              [v2] ⚠ Prix au palier {palierAtteint}% mais SL pas encore bougé
                            </span>
                          )}
                          {currentPalier > 0 && palierAtteint > currentPalier && (
                            <span className="text-blue-600 font-bold">
                              ↗ Prix {palierAtteint}%/SL {currentPalier}%
                            </span>
                          )}
                          {ddProg >= 10 && currentPalier === 0 && palierAtteint === 0 && (
                            <span className="text-red-600 font-bold">
                              ⚠️ DD {ddProg.toFixed(0)}%{ddProg >= 15 ? ' — close instant' : ' — timer 5min'}
                            </span>
                          )}
                          <span className="text-green-500 font-medium">TP: {tp.toFixed(dec)} ({tpPnl >= 0 ? '+' : ''}{tpPnl.toFixed(2)}€)</span>
                        </div>
                        <div className="text-[8px] text-gray-400 text-center mt-0.5">
                          Vue : -{viewLeftDd}% DD ↔ +{viewRightTp}% TP
                        </div>
                      </div>
                      );
                    })()}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="text-center py-6 text-gray-400 text-sm">
            Aucune position ouverte
          </div>
        )}
      </div>

      {/* ═══ TRADES FERMES ═══ */}
      <div className="bg-white rounded-xl shadow-sm border overflow-hidden">
        <div className="px-4 py-2.5 bg-gray-50 border-b flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ArrowDown className="w-4 h-4 text-gray-500" />
            <span className="text-sm font-bold text-gray-700">
              Trades Fermes ({filteredClosed.length})
            </span>
            <span className="text-[10px] text-gray-400">du plus recent au plus ancien</span>
          </div>
          {filteredClosed.length > 0 && (
            <div className={`text-xs font-bold ${pc(summary.total_pnl)}`}>
              Total: {fmt(summary.total_pnl)}€
            </div>
          )}
        </div>

        {historyLoading ? (
          <div className="text-center py-8 text-gray-400 text-sm">Chargement...</div>
        ) : filteredClosed.length > 0 ? (
          <div className="divide-y">
            {filteredClosed.map((t, i) => {
              const isWin = t.pnl > 0;
              const dec = decs(t.symbol, t.entry_price);
              const cfg = EXIT_CFG[t.exit_mode] || EXIT_CFG.OTHER;
              const ExitIcon = cfg.icon;
              const brokerLabel = 'Fusion Markets';
              const brokerColor = 'bg-green-100 text-green-700';
              const catLabel = (t.market_category || t.category || '').toUpperCase();

              let duration = t.duration_min;
              if (duration == null && t.entry_time && t.exit_time) {
                const dEntry = safeDate(t.entry_time);
                const dExit = safeDate(t.exit_time);
                if (dEntry && dExit) duration = Math.round((dExit.getTime() - dEntry.getTime()) / 60000);
              }

              return (
                <div key={`closed-${t.symbol}-${t.exit_time}-${i}`}
                  className={`px-4 py-2.5 flex items-center gap-3 transition-colors ${isWin ? 'hover:bg-green-50/30' : 'hover:bg-red-50/30'}`}>

                  <div className={`w-1 h-12 rounded-full flex-shrink-0 ${isWin ? 'bg-green-500' : 'bg-red-500'}`} />

                  <div className="flex-shrink-0">
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${t.action === 'BUY' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                      {t.action}
                    </span>
                  </div>

                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-bold text-sm text-gray-900">{t.symbol}</span>
                      <span className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${brokerColor}`}>{brokerLabel}</span>
                      {catLabel && catLabel !== 'FOREX' && (
                        <span className="text-[9px] text-gray-400 bg-gray-100 px-1 py-0.5 rounded">{catLabel}</span>
                      )}
                    </div>
                    <div className="text-[10px] text-gray-400 mt-0.5">
                      E: {t.entry_price.toFixed(dec)} → S: {t.exit_price.toFixed(dec)}
                    </div>
                  </div>

                  {/* Entry/Exit times */}
                  <div className="flex-shrink-0 text-right min-w-[140px]">
                    <div className="text-[10px] text-gray-500 flex items-center gap-1 justify-end">
                      <TrendingUp className="w-3 h-3 text-green-400" />
                      <span>{fmtDate(t.entry_time)}</span>
                    </div>
                    <div className="text-[10px] text-gray-500 flex items-center gap-1 justify-end">
                      <TrendingDown className="w-3 h-3 text-red-400" />
                      <span>{fmtDate(t.exit_time)}</span>
                    </div>
                    <div className="text-[9px] text-gray-400 flex items-center gap-1 justify-end">
                      <Clock className="w-2.5 h-2.5" />{fmtDur(duration)}
                    </div>
                  </div>

                  <div className="flex-shrink-0">
                    <span className={`inline-flex items-center gap-1 text-[10px] font-bold px-2 py-1 rounded-lg ${cfg.bg} ${cfg.text}`}>
                      <ExitIcon className="w-3 h-3" />{cfg.label}
                    </span>
                  </div>

                  <div className="flex-shrink-0 text-right min-w-[80px]">
                    <div className={`text-sm font-bold ${pc(t.pnl)}`}>{fmt(t.pnl)}€</div>
                    {t.quantity > 0 && <div className="text-[9px] text-gray-400">Qty: {t.quantity}</div>}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="text-center py-12">
            <Activity className="w-10 h-10 text-gray-300 mx-auto mb-2" />
            <p className="text-gray-500 text-sm">Aucun trade ferme sur cette periode</p>
            <p className="text-gray-400 text-xs mt-1">
              {`Aucun trade sur les ${period} derniers jours`}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Helpers ─── */

function safeDate(t: string | undefined | null): Date | null {
  if (!t) return null;
  const d = new Date(t);
  return isNaN(d.getTime()) ? null : d;
}

function safeTs(t: string | undefined | null): number {
  const d = safeDate(t);
  return d ? d.getTime() : 0;
}

function holdMinutes(t: string): number {
  const d = safeDate(t);
  if (!d) return 0;
  return Math.round((Date.now() - d.getTime()) / 60000);
}

function fmtDate(iso: string | undefined | null): string {
  const d = safeDate(iso);
  if (!d) return '--';
  // 2026-04-29: toujours afficher DD/MM HH:MM (suppression "Auj./Hier" pour clarté)
  const dateStr = d.toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' });
  const time = d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  return `${dateStr} ${time}`;
}

function fmtDur(minutes?: number | null): string {
  if (minutes == null || minutes < 0) return '--';
  if (minutes < 1) return '<1min';
  if (minutes < 60) return `${Math.round(minutes)}min`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return `${h}h${m > 0 ? m + 'min' : ''}`;
}

function decs(symbol: string, price: number): number {
  if (symbol.includes('JPY')) return 3;
  if (price > 100) return 2;
  return 5;
}

function pc(v: number) { return v >= 0 ? 'text-green-600' : 'text-red-600'; }
function fmt(v: number) { return `${v >= 0 ? '+' : ''}${v.toFixed(2)}`; }
