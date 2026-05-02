/**
 * Dashboard principal — vue d'ensemble du trading live.
 * - Fusion Markets MT5 (ZMQ bridge) — broker unique
 * - Capital, P&L, positions ouvertes/fermees
 * - Courbes d'evolution historique par categorie (persistees en DB)
 * - Signaux actifs du bot
 */
import { useState, useEffect, useCallback } from 'react';
import {
  TrendingUp, TrendingDown, DollarSign, BarChart3, Clock,
  Shield, Activity, ArrowUpRight, ArrowDownRight, Minus,
  Wifi, WifiOff, Play, Square, AlertTriangle, Cpu, Target,
  RefreshCw, Wallet, Globe, ChevronDown, ChevronUp, X,
  PieChart, CheckCircle, XCircle, Eye, Send
} from 'lucide-react';
import type { BackendState } from '../useBackend';

interface DashboardProps {
  backend: BackendState;
}

interface LivePosition {
  symbol: string;
  action: string;
  quantity: number;
  entry_price: number;
  current_price: number;
  stop_loss: number;
  take_profit: number;
  entry_time: string;
  pnl: number;
  pnl_percent: number;
  signal_confidence: number;
  signal_reason: string;
  position_size: number;
  margin: number;
  leverage: number;
  risk_eur: number;
  broker: string;
  asset_type?: string;
  pnl_conv_rate?: number;
}

interface ClosedTrade {
  symbol: string;
  action: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  pnl: number;
  reason: string;
  entry_time: string;
  exit_time: string;
  broker: string;
  category: string;
  market_category: string;
  asset_name: string;
  market: string;
}

interface DailyPerf {
  date: string;
  starting_capital: number;
  ending_capital: number;
  pnl: number;
  trades_count: number;
  wins: number;
  losses: number;
  win_rate: number;
  forex_pnl: number;
  actions_pnl: number;
  indices_pnl: number;
  commodities_pnl: number;
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'EUR' }).format(value);
}

function formatPrice(value: number, symbol: string): string {
  if (!value) return '--';
  const isJpy = symbol.includes('JPY');
  const isForex = symbol.includes('/');
  if (isForex) return value.toFixed(isJpy ? 3 : 5);
  return value.toFixed(2);
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return '--:--';
  }
}

function getBrokerBadge(broker: string) {
  return <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-green-100 text-green-700">Fusion Markets</span>;
}

const INDEX_SYMBOLS = ['CAC40', 'DAX40', 'SP500', 'NKY', 'NASDAQ', 'HK50', 'CHINAH', 'EUSTX50', 'AUS200', 'FTSE100', 'DJI'];

function getCategoryColor(broker: string, symbol: string): string {
  if (symbol.includes('/') && !symbol.includes('XAU') && !symbol.includes('XAG')) return '#3b82f6';
  if (INDEX_SYMBOLS.includes(symbol)) return '#8b5cf6';
  if (symbol.includes('XAU') || symbol.includes('XAG') || symbol.includes('CL=F')) return '#f59e0b';
  return '#10b981';
}

function getCategoryLabel(symbol: string): string {
  if (symbol.includes('/') && !symbol.includes('XAU') && !symbol.includes('XAG')) return 'Forex';
  if (INDEX_SYMBOLS.includes(symbol)) return 'Indice';
  if (symbol.includes('XAU') || symbol.includes('XAG') || symbol.includes('CL=F')) return 'Matiere';
  return 'Action';
}

// ── Créneaux scalping optimaux par marché ────────────────────────────
const SCALPING_WINDOWS = [
  { market: 'ASIE', session: 'Tokyo', startUtc: 0, endUtc: 6, peakStart: 1, peakEnd: 3, color: '#f59e0b', pairs: 'USD/JPY, AUD/USD, NZD/USD, AUD/JPY' },
  { market: 'ASIE', session: 'Sydney', startUtc: 22, endUtc: 4, peakStart: 23, peakEnd: 1, color: '#f59e0b', pairs: 'AUD/USD, NZD/USD' },
  { market: 'EU', session: 'Londres', startUtc: 7, endUtc: 16, peakStart: 8, peakEnd: 11, color: '#3b82f6', pairs: 'EUR/USD, GBP/USD, EUR/GBP, DAX40, CAC40' },
  { market: 'EU', session: 'Francfort', startUtc: 7, endUtc: 15, peakStart: 7, peakEnd: 9, color: '#3b82f6', pairs: 'EUR/USD, EUR/GBP, DAX40' },
  { market: 'US', session: 'New York', startUtc: 13, endUtc: 21, peakStart: 13, peakEnd: 16, color: '#10b981', pairs: 'EUR/USD, GBP/USD, USD/CAD, SP500, NASDAQ' },
  { market: 'EU/US', session: 'Overlap Londres-NY', startUtc: 13, endUtc: 16, peakStart: 13, peakEnd: 16, color: '#ef4444', pairs: 'EUR/USD, GBP/USD — VOLATILITE MAX' },
  { market: 'ASIE/EU', session: 'Overlap Tokyo-Londres', startUtc: 7, endUtc: 8, peakStart: 7, peakEnd: 8, color: '#ef4444', pairs: 'EUR/JPY, GBP/JPY' },
];

function ScalpingWindows() {
  const now = new Date();
  const utcH = now.getUTCHours();
  const month = now.getUTCMonth();
  const isCEST = month >= 2 && month <= 9;
  const offset = isCEST ? 2 : 1;
  const toLocal = (utcHour: number) => ((utcHour + offset) % 24);

  const isInWindow = (startUtc: number, endUtc: number) => {
    if (startUtc < endUtc) return utcH >= startUtc && utcH < endUtc;
    return utcH >= startUtc || utcH < endUtc;
  };
  const isInPeak = (peakStart: number, peakEnd: number) => {
    if (peakStart < peakEnd) return utcH >= peakStart && utcH < peakEnd;
    return utcH >= peakStart || utcH < peakEnd;
  };

  return (
    <div className="bg-white rounded-xl shadow-sm border p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 mb-3">
        <Clock className="w-4 h-4 text-blue-600" /> Creneaux Scalping
        <span className="text-[10px] font-normal text-gray-400 ml-1">
          (UTC{offset >= 0 ? '+' : ''}{offset} — {now.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })})
        </span>
      </h3>
      <div className="mb-3">
        <div className="relative h-6 bg-gray-100 rounded-full overflow-hidden">
          {SCALPING_WINDOWS.map((w, i) => {
            const start = toLocal(w.startUtc);
            const end = toLocal(w.endUtc);
            const left = (start / 24) * 100;
            const width = start < end
              ? ((end - start) / 24) * 100
              : ((24 - start + end) / 24) * 100;
            const isPeak = isInPeak(w.peakStart, w.peakEnd);
            return (
              <div key={i} className="absolute top-0 h-full transition-all duration-300"
                style={{ left: `${left}%`, width: `${Math.min(width, 100 - left)}%`, backgroundColor: w.color, opacity: isPeak ? 0.7 : 0.25 }}
                title={`${w.session}: ${toLocal(w.startUtc)}h-${toLocal(w.endUtc)}h`} />
            );
          })}
          <div className="absolute top-0 h-full w-0.5 bg-red-600 z-10"
            style={{ left: `${((now.getHours() + now.getMinutes() / 60) / 24) * 100}%` }}>
            <div className="absolute -top-1 -left-1 w-2.5 h-2.5 bg-red-600 rounded-full" />
          </div>
        </div>
        <div className="flex justify-between text-[8px] text-gray-400 mt-0.5 px-1">
          {[0, 3, 6, 9, 12, 15, 18, 21].map(h => <span key={h}>{h}h</span>)}
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
        {SCALPING_WINDOWS.map((w, i) => {
          const active = isInWindow(w.startUtc, w.endUtc);
          const peak = isInPeak(w.peakStart, w.peakEnd);
          return (
            <div key={i} className={`rounded-lg p-2 border text-xs transition-all ${
              peak ? 'border-red-300 bg-red-50 ring-1 ring-red-200' :
              active ? 'border-green-300 bg-green-50' :
              'border-gray-200 bg-gray-50 opacity-50'
            }`}>
              <div className="flex items-center justify-between mb-1">
                <div className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full" style={{ backgroundColor: w.color }} />
                  <span className="font-bold text-gray-800">{w.session}</span>
                  <span className="text-[10px] text-gray-400">{w.market}</span>
                </div>
                {peak && <span className="text-[9px] font-bold text-red-600 bg-red-100 px-1.5 py-0.5 rounded animate-pulse">PEAK</span>}
                {active && !peak && <span className="text-[9px] font-bold text-green-600 bg-green-100 px-1.5 py-0.5 rounded">ACTIF</span>}
              </div>
              <div className="text-[10px] text-gray-500">
                <span className="font-medium">{toLocal(w.startUtc)}h — {toLocal(w.endUtc)}h</span>
                <span className="text-gray-400 ml-1">(peak {toLocal(w.peakStart)}h-{toLocal(w.peakEnd)}h)</span>
              </div>
              <div className="text-[9px] text-gray-400 mt-0.5 truncate">{w.pairs}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Signaux Proches — opportunités détectées par le bot ──────────────
function SignauxProches({ signals }: { signals: { symbol: string; name: string; market: string; price: number; change: number; change_percent: number; signal: string; confidence: number; reason: string; suggested_entry: number; suggested_sl: number; suggested_tp: number }[] }) {
  // Only show buy/sell signals, sorted by confidence desc
  const actionable = signals
    .filter(s => s.signal === 'buy' || s.signal === 'sell')
    .sort((a, b) => b.confidence - a.confidence);

  const getConfidenceColor = (c: number) => {
    if (c >= 80) return 'text-green-600 bg-green-50 border-green-200';
    if (c >= 65) return 'text-orange-600 bg-orange-50 border-orange-200';
    return 'text-gray-500 bg-gray-50 border-gray-200';
  };

  const getConfidenceLabel = (c: number) => {
    if (c >= 80) return 'FORT';
    if (c >= 65) return 'MOYEN';
    return 'FAIBLE';
  };

  return (
    <div className="bg-white rounded-xl shadow-sm border p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <Target className="w-4 h-4 text-blue-600" /> Signaux Proches d'Entree
          <span className="text-[10px] font-normal text-gray-400 ml-1">
            ({actionable.length} opportunite{actionable.length !== 1 ? 's' : ''})
          </span>
        </h3>
        {actionable.length > 0 && (
          <span className="text-[10px] text-gray-400">
            Tries par confiance — criteres: RSI, MACD, Bollinger, ADX, Stochastique
          </span>
        )}
      </div>

      {actionable.length === 0 ? (
        <div className="text-center py-6 text-sm text-gray-400">
          <Target className="w-8 h-8 mx-auto mb-2 text-gray-300" />
          Aucun signal actionnable — le bot analyse en continu les paires, actions et indices
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {actionable.map((sig, i) => {
            const isBuy = sig.signal === 'buy';
            const catLabel = getCategoryLabel(sig.symbol);
            const catColor = getCategoryColor('', sig.symbol);
            const rr = sig.suggested_sl && sig.suggested_tp && sig.suggested_entry
              ? Math.abs(sig.suggested_tp - sig.suggested_entry) / Math.abs(sig.suggested_entry - sig.suggested_sl)
              : 0;

            return (
              <div key={i} className={`rounded-lg border p-3 transition-all ${
                sig.confidence >= 80
                  ? 'border-green-300 bg-green-50/50 ring-1 ring-green-100'
                  : sig.confidence >= 65
                  ? 'border-orange-200 bg-orange-50/30'
                  : 'border-gray-200 bg-gray-50/30'
              }`}>
                {/* Header */}
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <div className="w-1 h-8 rounded-full" style={{ backgroundColor: catColor }} />
                    <div>
                      <div className="flex items-center gap-1.5">
                        <span className="font-bold text-gray-900 text-sm">{sig.symbol}</span>
                        <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                          isBuy ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
                        }`}>{sig.signal.toUpperCase()}</span>
                      </div>
                      <div className="text-[10px] text-gray-500">
                        {sig.name || catLabel} · {sig.market}
                      </div>
                    </div>
                  </div>
                  <div className="text-right">
                    <div className={`text-xs font-bold px-2 py-1 rounded border ${getConfidenceColor(sig.confidence)}`}>
                      {sig.confidence}% {getConfidenceLabel(sig.confidence)}
                    </div>
                  </div>
                </div>

                {/* Price info */}
                <div className="grid grid-cols-3 gap-2 mb-2 text-[10px]">
                  <div>
                    <span className="text-gray-400">Prix actuel</span>
                    <div className="font-bold text-gray-900">{sig.price?.toFixed(sig.symbol.includes('JPY') ? 3 : sig.symbol.includes('/') ? 5 : 2)}</div>
                    <div className={`text-[9px] font-medium ${sig.change_percent >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                      {sig.change_percent >= 0 ? '+' : ''}{sig.change_percent?.toFixed(2)}%
                    </div>
                  </div>
                  <div>
                    <span className="text-gray-400">Entree</span>
                    <div className="font-bold text-blue-700">{sig.suggested_entry?.toFixed(sig.symbol.includes('JPY') ? 3 : sig.symbol.includes('/') ? 5 : 2) || '--'}</div>
                  </div>
                  <div>
                    <span className="text-gray-400">R/R</span>
                    <div className={`font-bold ${rr >= 2 ? 'text-green-600' : rr >= 1.5 ? 'text-orange-600' : 'text-gray-600'}`}>
                      {rr > 0 ? `1:${rr.toFixed(1)}` : '--'}
                    </div>
                  </div>
                </div>

                {/* SL / TP */}
                <div className="flex items-center gap-3 text-[10px] mb-2">
                  <div className="flex items-center gap-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-red-500" />
                    <span className="text-gray-400">SL:</span>
                    <span className="font-bold text-red-600">{sig.suggested_sl?.toFixed(sig.symbol.includes('JPY') ? 3 : sig.symbol.includes('/') ? 5 : 2) || '--'}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
                    <span className="text-gray-400">TP:</span>
                    <span className="font-bold text-green-600">{sig.suggested_tp?.toFixed(sig.symbol.includes('JPY') ? 3 : sig.symbol.includes('/') ? 5 : 2) || '--'}</span>
                  </div>
                </div>

                {/* Reason */}
                <div className="text-[9px] text-gray-500 leading-tight line-clamp-2 border-t border-gray-100 pt-1.5">
                  {sig.reason}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Calcul du P&L potentiel si le TP est touché */
function calcPotentialTpPnl(pos: LivePosition): number {
  if (!pos.take_profit || !pos.entry_price) return 0;
  const isLong = pos.action === 'BUY';
  const diff = isLong
    ? pos.take_profit - pos.entry_price
    : pos.entry_price - pos.take_profit;
  const conv = pos.pnl_conv_rate ?? 0.87;
  return diff * pos.quantity * conv;
}

/** Calcul du P&L potentiel si le SL est touché */
function calcPotentialSlPnl(pos: LivePosition): number {
  if (!pos.stop_loss || !pos.entry_price) return 0;
  const isLong = pos.action === 'BUY';
  const diff = isLong
    ? pos.stop_loss - pos.entry_price
    : pos.entry_price - pos.stop_loss;
  const conv = pos.pnl_conv_rate ?? 0.87;
  return diff * pos.quantity * conv;
}

// ── Helpers for position display ──────────────────────────────────────
function holdMinutesDash(t: string): number {
  try {
    const d = new Date(t);
    if (isNaN(d.getTime())) return 0;
    return Math.round((Date.now() - d.getTime()) / 60000);
  } catch { return 0; }
}

function fmtDurDash(minutes?: number | null): string {
  if (minutes == null || minutes < 0) return '--';
  if (minutes < 1) return '<1min';
  if (minutes < 60) return `${Math.round(minutes)}min`;
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  return `${h}h${m > 0 ? m + 'min' : ''}`;
}

function decsDash(symbol: string, price: number): number {
  if (symbol.includes('JPY')) return 3;
  if (price > 100) return 2;
  return 5;
}

function pcDash(v: number) { return v >= 0 ? 'text-green-600' : 'text-red-600'; }
function fmtPnl(v: number) { return `${v >= 0 ? '+' : ''}${v.toFixed(2)}`; }

// ── Broker Performance Chart: P&L bars + Capital line ────────────────
function BrokerPerformanceChart({ perfHistory, todayPnl, todayCapital, capitalKey, pnlLabel, capitalLabel, accentColor }: {
  perfHistory: DailyPerf[];
  todayPnl: number;
  todayCapital: number;
  capitalKey: 'ending_capital';
  pnlLabel: string;
  capitalLabel: string;
  accentColor: string;
}) {
  if (perfHistory.length === 0 && todayPnl === 0 && todayCapital <= 0) {
    return (
      <div className="h-56 flex items-center justify-center text-gray-400 text-sm">
        En attente de donnees... Les graphes se construisent a la cloture des marches.
      </div>
    );
  }

  const width = 700;
  const height = 240;
  const pad = { top: 20, right: 60, bottom: 30, left: 60 };
  const cW = width - pad.left - pad.right;
  const cH = height - pad.top - pad.bottom;

  // Data arrays
  const dailyPnls: number[] = [];
  const capitalArr: number[] = [];
  const labels: string[] = [];

  for (const day of perfHistory) {
    dailyPnls.push(day.pnl || 0);
    capitalArr.push((day[capitalKey] || day.ending_capital) || 0);
    const parts = day.date.split('-');
    labels.push(`${parts[2]}/${parts[1]}`);
  }

  // Add today if there's data
  if (todayPnl !== 0 || todayCapital > 0) {
    dailyPnls.push(todayPnl);
    capitalArr.push(todayCapital > 0 ? todayCapital : (capitalArr[capitalArr.length - 1] || 0) + todayPnl);
    labels.push('Auj.');
  }

  const n = dailyPnls.length;
  if (n === 0) return null;

  // P&L scale (left axis)
  const pnlMax = Math.max(...dailyPnls, 0);
  const pnlMin = Math.min(...dailyPnls, 0);
  const pnlMargin = (pnlMax - pnlMin) * 0.15 || 1;
  const pnlYMin = pnlMin - pnlMargin;
  const pnlYMax = pnlMax + pnlMargin;
  const pnlYRange = pnlYMax - pnlYMin;

  // Capital scale (right axis)
  const capValues = capitalArr.filter(v => v > 0);
  const capMin = capValues.length > 0 ? Math.min(...capValues) : 0;
  const capMax = capValues.length > 0 ? Math.max(...capValues) : 1;
  const capMargin = (capMax - capMin) * 0.15 || 1;
  const capYMin = capMin - capMargin;
  const capYMax = capMax + capMargin;
  const capYRange = capYMax - capYMin;

  const barW = Math.max(4, Math.min(20, (cW / n) * 0.6));
  const pnlZeroY = pad.top + cH - ((0 - pnlYMin) / pnlYRange) * cH;

  // Capital line points
  const capPoints = capitalArr.map((v, i) => {
    if (v <= 0) return null;
    const x = pad.left + ((i + 0.5) / n) * cW;
    const y = pad.top + cH - ((v - capYMin) / capYRange) * cH;
    return `${x},${y}`;
  }).filter(Boolean).join(' ');

  // Cumulative P&L line
  const cumPnls: number[] = [];
  let cum = 0;
  for (const p of dailyPnls) { cum += p; cumPnls.push(cum); }
  const cumMax = Math.max(...cumPnls, 0);
  const cumMin = Math.min(...cumPnls, 0);
  const cumMarginV = (cumMax - cumMin) * 0.15 || 1;
  const cumYMin = cumMin - cumMarginV;
  const cumYMax = cumMax + cumMarginV;
  const cumYRange = cumYMax - cumYMin;
  const cumPoints = cumPnls.map((v, i) => {
    const x = pad.left + ((i + 0.5) / n) * cW;
    const y = pad.top + cH - ((v - pnlYMin) / pnlYRange) * cH;
    return `${x},${y}`;
  }).join(' ');

  // Left axis labels (P&L)
  const pnlLabels = Array.from({ length: 5 }, (_, i) => pnlYMin + (pnlYRange * i) / 4);
  // Right axis labels (Capital)
  const capLabels = Array.from({ length: 5 }, (_, i) => capYMin + (capYRange * i) / 4);

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-56">
        {/* Grid lines */}
        {pnlLabels.map((val, i) => {
          const y = pad.top + cH - ((val - pnlYMin) / pnlYRange) * cH;
          return (
            <g key={`g-${i}`}>
              <line x1={pad.left} y1={y} x2={pad.left + cW} y2={y} stroke="#f1f5f9" strokeWidth="1" />
              <text x={pad.left - 6} y={y + 3} textAnchor="end" className="fill-gray-400" fontSize="8">
                {val.toFixed(1)}€
              </text>
            </g>
          );
        })}

        {/* Right axis labels (Capital) */}
        {capLabels.map((val, i) => {
          const y = pad.top + cH - ((val - capYMin) / capYRange) * cH;
          return (
            <text key={`cr-${i}`} x={pad.left + cW + 6} y={y + 3} textAnchor="start" className="fill-gray-400" fontSize="8">
              {val >= 1000 ? `${(val / 1000).toFixed(1)}k` : val.toFixed(0)}€
            </text>
          );
        })}

        {/* Zero line for P&L */}
        <line x1={pad.left} y1={pnlZeroY} x2={pad.left + cW} y2={pnlZeroY}
          stroke="#94a3b8" strokeWidth="1" strokeDasharray="4 2" />

        {/* P&L bars */}
        {dailyPnls.map((pnl, i) => {
          const cx = pad.left + ((i + 0.5) / n) * cW;
          const barTop = pnl >= 0
            ? pad.top + cH - ((pnl - pnlYMin) / pnlYRange) * cH
            : pnlZeroY;
          const barBottom = pnl >= 0
            ? pnlZeroY
            : pad.top + cH - ((pnl - pnlYMin) / pnlYRange) * cH;
          const barH = Math.max(1, barBottom - barTop);
          return (
            <rect key={`bar-${i}`}
              x={cx - barW / 2} y={barTop}
              width={barW} height={barH}
              rx={2}
              fill={pnl >= 0 ? '#16a34a' : '#dc2626'}
              opacity={0.7}
            />
          );
        })}

        {/* Cumulative P&L line */}
        {cumPoints && (
          <polyline points={cumPoints} fill="none" stroke="#1e293b"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" opacity={0.6} />
        )}

        {/* Capital line */}
        {capPoints && (
          <polyline points={capPoints} fill="none" stroke={accentColor}
            strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
        )}

        {/* Capital dots */}
        {capitalArr.map((v, i) => {
          if (v <= 0) return null;
          const x = pad.left + ((i + 0.5) / n) * cW;
          const y = pad.top + cH - ((v - capYMin) / capYRange) * cH;
          return <circle key={`dot-${i}`} cx={x} cy={y} r={2.5} fill={accentColor} />;
        })}

        {/* X-axis labels */}
        {labels.map((label, i) => {
          if (n > 15 && i % Math.ceil(n / 10) !== 0 && i !== n - 1) return null;
          const x = pad.left + ((i + 0.5) / n) * cW;
          return (
            <text key={`x-${i}`} x={x} y={height - 6} textAnchor="middle" className="fill-gray-400" fontSize="8">
              {label}
            </text>
          );
        })}

        {/* Axis titles */}
        <text x={12} y={pad.top + cH / 2} textAnchor="middle" className="fill-gray-400" fontSize="8"
          transform={`rotate(-90, 12, ${pad.top + cH / 2})`}>
          P&L (€)
        </text>
        <text x={width - 8} y={pad.top + cH / 2} textAnchor="middle" className="fill-gray-400" fontSize="8"
          transform={`rotate(90, ${width - 8}, ${pad.top + cH / 2})`}>
          Capital (€)
        </text>
      </svg>

      {/* Legend */}
      <div className="flex flex-wrap gap-4 mt-1 px-2 text-xs text-gray-500">
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-sm bg-green-600 opacity-70 inline-block" />
          <span className="w-3 h-3 rounded-sm bg-red-600 opacity-70 inline-block" />
          P&L journalier
          <span className="font-bold ml-1" style={{ color: todayPnl >= 0 ? '#16a34a' : '#dc2626' }}>
            (aujourd'hui: {todayPnl >= 0 ? '+' : ''}{todayPnl.toFixed(2)}€)
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-0.5 rounded inline-block bg-gray-700" />
          P&L cumule
          <span className="font-bold ml-1" style={{ color: cum >= 0 ? '#16a34a' : '#dc2626' }}>
            ({cum >= 0 ? '+' : ''}{cum.toFixed(2)}€)
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="w-3 h-0.5 rounded inline-block" style={{ backgroundColor: accentColor }} />
          {capitalLabel}
          {capitalArr.length > 0 && capitalArr[capitalArr.length - 1] > 0 && (
            <span className="font-bold" style={{ color: accentColor }}>
              {formatCurrency(capitalArr[capitalArr.length - 1])}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════
// MAIN DASHBOARD
// ═══════════════════════════════════════════════════════════════════════
export default function Dashboard({ backend }: DashboardProps) {
  const {
    connected, backendAvailable, botStatus, botConfig, account,
    dailySummary, signals, logs,
    startBot, scanOnly, setMode, stopBot, emergencyStop,
    placeOrder, closePosition: backendClosePosition,
  } = backend;

  const [livePositions, setLivePositions] = useState<LivePosition[]>([]);
  const [closedTrades, setClosedTrades] = useState<ClosedTrade[]>([]);
  const [perfHistory, setPerfHistory] = useState<DailyPerf[]>([]);
  const [confirmEmergency, setConfirmEmergency] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<Date>(new Date());
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);

  // Manual trade state
  const [showManualTrade, setShowManualTrade] = useState(true);
  const [manualSymbol, setManualSymbol] = useState('GBP/USD');
  const [manualAction, setManualAction] = useState<'buy' | 'sell'>('buy');
  const [manualAmount, setManualAmount] = useState(150);
  const [manualLoading, setManualLoading] = useState(false);
  const [manualResult, setManualResult] = useState<{ success: boolean; message: string } | null>(null);
  const [confirmOrder, setConfirmOrder] = useState(false);

  const TRADEABLE_SYMBOLS = [
    'GBP/USD', 'USD/JPY', 'EUR/GBP', 'AUD/USD', 'NZD/USD', 'EUR/JPY',
    'GBP/JPY', 'EUR/CAD', 'EUR/CHF', 'USD/CHF', 'AUD/NZD', 'GBP/NZD',
    'CAD/JPY', 'NZD/JPY', 'EUR/AUD', 'GBP/CAD',
    'DAX40', 'SP500', 'NASDAQ', 'CAC40', 'NKY', 'HK50', 'AUS200',
    'UK100', 'US30',  // 2026-04-24: ajout indices US30 + UK100
    'GOLD', 'OIL_CRUDE',  // 2026-04-24: ajout commodities
  ];
  const [manualSL, setManualSL] = useState<number | ''>('');
  const [manualTP, setManualTP] = useState<number | ''>('');

  const handleManualOrder = async () => {
    if (!confirmOrder) {
      setConfirmOrder(true);
      return;
    }
    setConfirmOrder(false);
    setManualLoading(true);
    setManualResult(null);
    try {
      const slNum = typeof manualSL === 'number' && manualSL > 0 ? manualSL : undefined;
      const tpNum = typeof manualTP === 'number' && manualTP > 0 ? manualTP : undefined;
      const result = await placeOrder(manualSymbol, manualAction, manualAmount, slNum, tpNum);
      setManualResult({
        success: result.success !== false,
        message: result.success !== false
          ? `${manualAction.toUpperCase()} ${manualSymbol} ${manualAmount}EUR — SL=${result.stop_loss?.toFixed(5) || '?'} TP=${result.take_profit?.toFixed(5) || '?'}`
          : (result.error || 'Erreur'),
      });
    } catch (e: any) {
      setManualResult({ success: false, message: e.message || 'Erreur de connexion' });
    } finally {
      setManualLoading(false);
    }
  };

  const apiUrl = import.meta.env.VITE_BACKEND_URL || '';
  const apiKey = import.meta.env.VITE_API_KEY || '';

  const authHeaders = { 'Authorization': `Bearer ${apiKey}` };

  const syncBalance = useCallback(() => {
    const input = prompt('Entrez le solde reel MT5 (ex: 192.89):');
    if (!input) return;
    const val = parseFloat(input.replace(',', '.'));
    if (isNaN(val) || val <= 0) { alert('Montant invalide'); return; }
    fetch(`${apiUrl}/api/account/sync-balance`, {
      method: 'POST',
      headers: { ...authHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify({ balance: val }),
    }).then(r => r.json()).then(d => {
      if (d.success) alert(`Capital mis a jour: ${d.old_capital}€ → ${d.new_capital}€`);
      else alert(`Erreur: ${d.error}`);
    }).catch(e => alert(`Erreur: ${e}`));
  }, [apiUrl, apiKey]);

  const fetchLivePositions = useCallback(async () => {
    try {
      const resp = await fetch(`${apiUrl}/api/positions/live`, { headers: authHeaders, cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        setLivePositions(data);
        setLastRefresh(new Date());
      }
    } catch (e) {
      console.error('Failed to fetch live positions:', e);
    }
  }, [apiUrl, apiKey]);

  const fetchClosedTrades = useCallback(async () => {
    try {
      // Only fetch today's trades for the dashboard (days=1)
      const resp = await fetch(`${apiUrl}/api/trades/closed?days=1`, { headers: authHeaders, cache: 'no-store' });
      if (resp.ok) {
        setClosedTrades(await resp.json());
      }
    } catch (e) {
      console.error('Failed to fetch closed trades:', e);
    }
  }, [apiUrl, apiKey]);

  const fetchPerfHistory = useCallback(async () => {
    try {
      const resp = await fetch(`${apiUrl}/api/performance/history?days=90`, { headers: authHeaders, cache: 'no-store' });
      if (resp.ok) {
        setPerfHistory(await resp.json());
      }
    } catch (e) {
      console.error('Failed to fetch performance history:', e);
    }
  }, [apiUrl, apiKey]);

  useEffect(() => {
    fetchLivePositions();
    fetchClosedTrades();
    fetchPerfHistory();
    const posInterval = setInterval(fetchLivePositions, 2000);
    const tradesInterval = setInterval(fetchClosedTrades, 30000);
    const perfInterval = setInterval(fetchPerfHistory, 300000);
    return () => {
      clearInterval(posInterval);
      clearInterval(tradesInterval);
      clearInterval(perfInterval);
    };
  }, [fetchLivePositions, fetchClosedTrades, fetchPerfHistory]);

  if (!backendAvailable) {
    return (
      <div className="bg-yellow-50 border border-yellow-200 rounded-xl p-8 text-center">
        <WifiOff className="w-12 h-12 text-yellow-500 mx-auto mb-4" />
        <h3 className="text-lg font-bold text-yellow-800 mb-2">Backend Non Connecte</h3>
        <p className="text-sm text-yellow-700">
          Le serveur de trading n'est pas accessible.
        </p>
      </div>
    );
  }

  // ── Derived Data ──────────────────────────────────────────────────────
  const capital = account?.capital || account?.balance || botStatus?.capital || 0;
  const brokers = (account as any)?.brokers;
  // Fall back to top-level account fields if brokers.mt5 isn't populated yet
  // so the dashboard never shows zeros while the real balance is non-zero.
  const ctBalance = brokers?.mt5?.balance ?? account?.balance ?? 0;
  const ctEquity = brokers?.mt5?.equity ?? account?.net_liquidation ?? account?.capital ?? 0;
  const ctFreeMargin = brokers?.mt5?.free_margin ?? account?.buying_power ?? 0;
  const ctProfit = brokers?.mt5?.profit ?? (account as any)?.unrealized_pnl ?? 0;
  const primaryBalance = ctBalance;
  const primaryEquity = ctEquity;
  const primaryFreeMargin = ctFreeMargin;
  const primaryProfit = ctProfit;

  // All positions are MT5
  const allPositions = livePositions;

  // Trades fermes — UNIQUEMENT trades fermés AUJOURD'HUI (heure locale Paris)
  const now = new Date();
  const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  const allClosedToday = closedTrades.filter(t => {
    // Only count trades that have an exit_time today
    if (!t.exit_time) return false;
    // Parse the exit_time and compare as local date
    const exitDate = new Date(t.exit_time);
    const exitStr = `${exitDate.getFullYear()}-${String(exitDate.getMonth() + 1).padStart(2, '0')}-${String(exitDate.getDate()).padStart(2, '0')}`;
    return exitStr === todayStr;
  });

  const totalMarginUsed = allPositions.reduce((s, p) => s + (p.margin || 0), 0);
  const totalOpenPnl = allPositions.reduce((s, p) => s + (p.pnl || 0), 0);

  // P&L du jour — priorite au dailySummary (source fiable du backend en temps reel)
  // Fallback sur les trades fermes filtres cote frontend
  const totalClosedPnl = dailySummary?.pnl ?? allClosedToday.reduce((s, t) => s + t.pnl, 0);
  const totalWins = dailySummary?.wins ?? allClosedToday.filter(t => t.pnl > 0).length;
  const totalLosses = dailySummary?.losses ?? allClosedToday.filter(t => t.pnl <= 0).length;
  const tradesToday = dailySummary?.trades ?? allClosedToday.length;

  // P&L potentiel total si TP touché
  const totalPotentialTpPnl = livePositions.reduce((s, p) => s + calcPotentialTpPnl(p), 0);

  // Today's P&L by category
  const posForex = livePositions.filter(p => getCategoryLabel(p.symbol) === 'Forex');
  const posIndices = livePositions.filter(p => getCategoryLabel(p.symbol) === 'Indice');
  const posActions = livePositions.filter(p => getCategoryLabel(p.symbol) === 'Action');
  const posCommodities = livePositions.filter(p => getCategoryLabel(p.symbol) === 'Matiere');

  const todayCatPnl = {
    forex: posForex.reduce((s, p) => s + p.pnl, 0) + allClosedToday.filter(t => t.market_category === 'FOREX').reduce((s, t) => s + t.pnl, 0),
    actions: posActions.reduce((s, p) => s + p.pnl, 0) + allClosedToday.filter(t => t.market_category === 'STOCKS').reduce((s, t) => s + t.pnl, 0),
    indices: posIndices.reduce((s, p) => s + p.pnl, 0) + allClosedToday.filter(t => t.market_category === 'INDICES').reduce((s, t) => s + t.pnl, 0),
    commodities: posCommodities.reduce((s, p) => s + p.pnl, 0) + allClosedToday.filter(t => t.market_category === 'COMMODITY').reduce((s, t) => s + t.pnl, 0),
    total: totalClosedPnl + totalOpenPnl,
  };

  return (
    <div className="space-y-4">

      {/* ── Row 1: Connection Status + Emergency ──────────────────────── */}
      <div className="flex items-center justify-between bg-white rounded-xl shadow-sm border p-3">
        <div className="flex items-center gap-3">
          {connected ? (
            <div className="flex items-center gap-2">
              <Wifi className="w-4 h-4 text-green-600" />
              <span className="font-medium text-sm text-gray-900">Live</span>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <WifiOff className="w-4 h-4 text-red-500" />
              <span className="text-sm text-gray-700">Reconnexion...</span>
            </div>
          )}
          <span className="text-[10px] text-gray-400">
            MAJ {lastRefresh.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => { fetchLivePositions(); fetchClosedTrades(); }}
            title="Rafraichir"
            className="p-2 text-gray-500 hover:text-blue-600 transition">
            <RefreshCw className="w-4 h-4" />
          </button>
          <button onClick={() => setConfirmEmergency(true)}
            title="ARRET D'URGENCE"
            className="p-1.5 bg-red-600 text-white rounded-lg hover:bg-red-700 transition">
            <AlertTriangle className="w-4 h-4" />
          </button>
        </div>
      </div>

      {confirmEmergency && (
        <div className="bg-red-50 border-2 border-red-300 rounded-xl p-4 flex items-center justify-between">
          <p className="text-sm text-red-800 font-medium">Fermer toutes les positions et arreter le bot ?</p>
          <div className="flex gap-2">
            <button onClick={() => { emergencyStop(); setConfirmEmergency(false); }}
              className="px-3 py-1.5 bg-red-600 text-white rounded-lg text-sm">Confirmer</button>
            <button onClick={() => setConfirmEmergency(false)}
              className="px-3 py-1.5 bg-gray-200 text-gray-700 rounded-lg text-sm">Annuler</button>
          </div>
        </div>
      )}

      {/* ── Row 2: KPI Resume Global ─────────────────────────────────── */}
      <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
        {/* Capital Total */}
        <div className="bg-gradient-to-br from-slate-800 to-slate-900 rounded-xl shadow-sm p-3 text-white">
          <div className="flex items-center gap-1 text-[10px] text-slate-300 mb-0.5">
            <Wallet className="w-3 h-3" /> Capital Total
          </div>
          <div className="text-xl font-bold">{capital > 0 ? formatCurrency(capital) : '--'}</div>
        </div>

        {/* Capital Engagé */}
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="flex items-center gap-1 text-[10px] text-gray-500 mb-0.5">
            <DollarSign className="w-3 h-3" /> Capital Engage
          </div>
          <div className="text-lg font-bold text-blue-600">{formatCurrency(totalMarginUsed)}</div>
          <div className="text-[10px] text-gray-400">
            {capital > 0 ? `${(totalMarginUsed / capital * 100).toFixed(1)}% du capital` : '--'}
          </div>
        </div>

        {/* P&L du Jour (total) */}
        <div className="bg-gradient-to-br from-white to-blue-50 rounded-xl shadow-sm border-2 border-blue-200 p-3">
          <div className="flex items-center gap-1 text-[10px] text-blue-600 font-bold mb-0.5">
            <BarChart3 className="w-3 h-3" /> P&L du Jour
          </div>
          <div className={`text-xl font-extrabold ${(totalClosedPnl + totalOpenPnl) >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {(totalClosedPnl + totalOpenPnl) >= 0 ? '+' : ''}{formatCurrency(totalClosedPnl + totalOpenPnl)}
          </div>
          <div className="text-[10px] text-gray-400">
            Encaisse + ouvert aujourd'hui
          </div>
        </div>

        {/* P&L Encaissés Aujourd'hui */}
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="flex items-center gap-1 text-[10px] text-gray-500 mb-0.5">
            <CheckCircle className="w-3 h-3" /> Encaisse Aujourd'hui
          </div>
          <div className={`text-lg font-bold ${totalClosedPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {totalClosedPnl >= 0 ? '+' : ''}{formatCurrency(totalClosedPnl)}
          </div>
          <div className="text-[10px] text-gray-400">
            {tradesToday} trade{tradesToday !== 1 ? 's' : ''} ferme{tradesToday !== 1 ? 's' : ''}
          </div>
        </div>

        {/* P&L Ouvert */}
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="flex items-center gap-1 text-[10px] text-gray-500 mb-0.5">
            {totalOpenPnl >= 0 ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
            P&L Ouvert
          </div>
          <div className={`text-lg font-bold ${totalOpenPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {totalOpenPnl >= 0 ? '+' : ''}{formatCurrency(totalOpenPnl)}
          </div>
          <div className="text-[10px] text-gray-400">
            {livePositions.length} position{livePositions.length !== 1 ? 's' : ''}
            {totalPotentialTpPnl > 0 && (
              <span className="ml-1 text-green-500">
                (pot. TP: +{totalPotentialTpPnl.toFixed(2)}€)
              </span>
            )}
          </div>
        </div>

        {/* Win/Loss Aujourd'hui */}
        <div className="bg-white rounded-xl shadow-sm border p-3">
          <div className="flex items-center gap-1 text-[10px] text-gray-500 mb-0.5">
            <Target className="w-3 h-3" /> W/L Aujourd'hui
          </div>
          <div className="text-lg font-bold text-gray-900">
            <span className="text-green-600">{totalWins}</span>
            <span className="text-gray-400 mx-1">/</span>
            <span className="text-red-600">{totalLosses}</span>
          </div>
          <div className="text-[10px] text-gray-400">
            WR: {tradesToday > 0
              ? dailySummary?.win_rate
                ? dailySummary.win_rate.toFixed(0) + '%'
                : (totalWins / tradesToday * 100).toFixed(0) + '%'
              : '-'}
          </div>
        </div>
      </div>

      {/* ── Row 3: Broker Card ──────────────────────────────────────── */}
      <div className="space-y-3">
        <BrokerCard
          name="Fusion Markets MT5"
          subtitle="MetaTrader 5 (ZMQ bridge) — Execution + SL/TP natif"
          badgeClass="bg-green-100 text-green-700"
          gradientFrom="from-green-50"
          gradientTo="to-white"
          accentColor="green"
          balance={primaryBalance}
          equity={primaryEquity}
          freeMargin={primaryFreeMargin}
          profit={primaryProfit}
          marginUsed={totalMarginUsed}
          closedPnl={totalClosedPnl}
          openPnl={totalOpenPnl}
          openCount={allPositions.length}
          closedCount={allClosedToday.length}
          wins={totalWins}
          losses={totalLosses}
          isConnected={botStatus?.mt5_connected}
          isRunning={botStatus?.running}
          onStart={startBot}
          onStop={() => stopBot(false)}
          botConnected={connected}
          onScanOnly={scanOnly}
          onSetMode={setMode}
          isScanOnly={botStatus?.scan_only}
        />
      </div>

      {/* ── Manual Trade Panel ─────────────────────────────────────────── */}
      <div className="bg-white rounded-xl shadow-sm border">
        <button
          onClick={() => setShowManualTrade(!showManualTrade)}
          className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50 transition"
        >
          <div className="flex items-center gap-2">
            <Send className="w-4 h-4 text-green-600" />
            <span className="text-sm font-semibold text-gray-900">Prise de Position Manuelle</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-green-100 text-green-700 font-medium">SL/TP Auto</span>
          </div>
          {showManualTrade ? <ChevronUp className="w-4 h-4 text-gray-400" /> : <ChevronDown className="w-4 h-4 text-gray-400" />}
        </button>

        {showManualTrade && (
          <div className="px-4 pb-4 border-t">
            <div className="mt-3 flex flex-wrap items-end gap-3">
              {/* Symbol */}
              <div>
                <label className="text-[10px] text-gray-500 block mb-1">Paire</label>
                <select
                  value={manualSymbol}
                  onChange={e => setManualSymbol(e.target.value)}
                  className="text-sm border rounded-lg px-2 py-1.5 bg-white"
                >
                  {TRADEABLE_SYMBOLS.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>

              {/* Direction */}
              <div>
                <label className="text-[10px] text-gray-500 block mb-1">Direction</label>
                <div className="flex rounded-lg overflow-hidden border">
                  <button
                    onClick={() => setManualAction('buy')}
                    className={`px-3 py-1.5 text-xs font-medium transition ${manualAction === 'buy' ? 'bg-green-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
                  >
                    <ArrowUpRight className="w-3 h-3 inline mr-1" />BUY
                  </button>
                  <button
                    onClick={() => setManualAction('sell')}
                    className={`px-3 py-1.5 text-xs font-medium transition ${manualAction === 'sell' ? 'bg-red-600 text-white' : 'bg-white text-gray-600 hover:bg-gray-50'}`}
                  >
                    <ArrowDownRight className="w-3 h-3 inline mr-1" />SELL
                  </button>
                </div>
              </div>

              {/* Amount EUR */}
              <div>
                <label className="text-[10px] text-gray-500 block mb-1">Montant (EUR)</label>
                <input
                  type="number"
                  value={manualAmount}
                  onChange={e => setManualAmount(Number(e.target.value))}
                  min={10}
                  max={500}
                  step={10}
                  className="text-sm border rounded-lg px-2 py-1.5 w-20"
                />
              </div>

              {/* SL custom (optionnel) */}
              <div>
                <label className="text-[10px] text-gray-500 block mb-1">SL <span className="text-gray-400">(vide=auto)</span></label>
                <input
                  type="number"
                  value={manualSL}
                  onChange={e => setManualSL(e.target.value === '' ? '' : Number(e.target.value))}
                  step="any"
                  placeholder="auto"
                  className="text-sm border rounded-lg px-2 py-1.5 w-24"
                />
              </div>

              {/* TP custom (optionnel) */}
              <div>
                <label className="text-[10px] text-gray-500 block mb-1">TP <span className="text-gray-400">(vide=auto)</span></label>
                <input
                  type="number"
                  value={manualTP}
                  onChange={e => setManualTP(e.target.value === '' ? '' : Number(e.target.value))}
                  step="any"
                  placeholder="auto"
                  className="text-sm border rounded-lg px-2 py-1.5 w-24"
                />
              </div>

              {/* Execute with confirmation */}
              {confirmOrder ? (
                <div className="flex items-center gap-2 bg-yellow-50 border border-yellow-300 rounded-lg px-3 py-1.5">
                  <AlertTriangle className="w-4 h-4 text-yellow-600" />
                  <span className="text-xs font-medium text-yellow-800">
                    {manualAction.toUpperCase()} {manualSymbol} {manualAmount}€ ?
                  </span>
                  <button
                    onClick={handleManualOrder}
                    className={`px-3 py-1 rounded text-xs font-bold text-white ${
                      manualAction === 'buy' ? 'bg-green-600 hover:bg-green-700' : 'bg-red-600 hover:bg-red-700'
                    }`}
                  >
                    Confirmer
                  </button>
                  <button
                    onClick={() => setConfirmOrder(false)}
                    className="px-2 py-1 rounded text-xs bg-gray-200 hover:bg-gray-300"
                  >
                    Annuler
                  </button>
                </div>
              ) : (
                <button
                  onClick={handleManualOrder}
                  disabled={manualLoading || !backendAvailable}
                  className={`flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-xs font-medium transition disabled:opacity-50 ${
                    manualAction === 'buy'
                      ? 'bg-green-600 text-white hover:bg-green-700'
                      : 'bg-red-600 text-white hover:bg-red-700'
                  }`}
                >
                  {manualLoading ? (
                    <RefreshCw className="w-3 h-3 animate-spin" />
                  ) : (
                    <Send className="w-3 h-3" />
                  )}
                  {manualLoading ? 'Envoi...' : 'Executer'}
                </button>
              )}
            </div>

            {/* Protections info */}
            <div className="mt-2 flex flex-wrap gap-2 text-[10px] text-gray-500">
              <span className="flex items-center gap-1"><Shield className="w-3 h-3 text-blue-500" /> SL/TP auto (PAIR_CONFIG)</span>
              <span className="flex items-center gap-1"><Target className="w-3 h-3 text-purple-500" /> Trailing stop dynamique</span>
              <span className="flex items-center gap-1"><Shield className="w-3 h-3 text-red-500" /> Stop guard -50EUR/jour</span>
              <span className="flex items-center gap-1"><Activity className="w-3 h-3 text-green-500" /> SL/TP natif MT5</span>
            </div>

            {/* Result feedback */}
            {manualResult && (
              <div className={`mt-2 text-xs px-3 py-2 rounded-lg flex items-center gap-2 ${
                manualResult.success ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'
              }`}>
                {manualResult.success ? <CheckCircle className="w-3.5 h-3.5" /> : <XCircle className="w-3.5 h-3.5" />}
                {manualResult.message}
                <button onClick={() => setManualResult(null)} className="ml-auto"><X className="w-3 h-3" /></button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── Row 4: Positions Ouvertes (compact) ────────────────────────── */}
      {allPositions.length > 0 && (
        <div className="bg-white rounded-xl shadow-sm border overflow-hidden">
          <div className="px-4 py-2.5 bg-gradient-to-r from-blue-50 to-indigo-50 border-b flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-blue-600 animate-pulse" />
              <span className="text-sm font-bold text-blue-800">
                Positions Ouvertes ({allPositions.length})
              </span>
            </div>
            <div className="flex items-center gap-4 text-xs">
              <span className="text-gray-500">Marge: <b>{totalMarginUsed.toFixed(0)}€</b></span>
              <span className={`font-bold ${pcDash(totalOpenPnl)}`}>P&L: {fmtPnl(totalOpenPnl)}€</span>
              {totalPotentialTpPnl > 0 && (
                <span className="text-green-600 font-bold">Pot. TP: {fmtPnl(totalPotentialTpPnl)}€</span>
              )}
            </div>
          </div>
          <div className="divide-y">
            {allPositions.map((pos, idx) => {
              const isLong = pos.action === 'BUY';
              const dec = decsDash(pos.symbol, pos.entry_price);
              const sl = pos.stop_loss || 0;
              const tp = pos.take_profit || 0;
              const dur = holdMinutesDash(pos.entry_time || '');
              const atype = pos.asset_type || 'unknown';

              // Progress bar
              let entryPct = 50, currentPct = 50;
              let tpPnl = 0;
              if (sl > 0 && tp > 0) {
                const range = Math.abs(tp - sl);
                if (range > 0) {
                  if (isLong) {
                    entryPct = Math.max(0, Math.min(100, ((pos.entry_price - sl) / range) * 100));
                    currentPct = Math.max(0, Math.min(100, ((pos.current_price - sl) / range) * 100));
                  } else {
                    entryPct = Math.max(0, Math.min(100, ((sl - pos.entry_price) / (sl - tp)) * 100));
                    currentPct = Math.max(0, Math.min(100, ((sl - pos.current_price) / (sl - tp)) * 100));
                  }
                }
                const tpDist = isLong ? tp - pos.entry_price : pos.entry_price - tp;
                const conv = pos.pnl_conv_rate ?? 0.87;
                tpPnl = tpDist * pos.quantity * conv;
              }

              return (
                <div key={`${pos.broker}-${pos.symbol}-${idx}`} className="px-4 py-2.5 hover:bg-blue-50/30 transition-colors">
                  <div className="flex items-center gap-3">
                    {/* Color indicator */}
                    <div className={`w-1 h-12 rounded-full flex-shrink-0 ${pos.pnl >= 0 ? 'bg-green-500' : 'bg-red-500'}`} />

                    {/* Symbol + info */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-bold text-sm text-gray-900">{pos.symbol}</span>
                        <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${isLong ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                          {pos.action}
                        </span>
                        <span className="text-[9px] text-gray-400 bg-gray-100 px-1 py-0.5 rounded">
                          {atype === 'forex' ? 'Forex' : atype === 'index_cfd' ? 'Index' : atype === 'commodity' ? 'Commodity' : 'Other'}
                        </span>
                      </div>
                      <div className="text-[10px] text-gray-500 flex items-center gap-2 mt-0.5">
                        <span>E: <b>{pos.entry_price.toFixed(dec)}</b></span>
                        <span>→ <b className={pos.pnl >= 0 ? 'text-green-600' : 'text-red-600'}>{pos.current_price.toFixed(dec)}</b></span>
                        <span>SL: <b className="text-red-500">{sl > 0 ? sl.toFixed(dec) : '--'}</b></span>
                        <span>TP: <b className="text-green-500">{tp > 0 ? tp.toFixed(dec) : '--'}</b></span>
                        <span className="text-gray-400"><Clock className="w-2.5 h-2.5 inline" /> {fmtDurDash(dur)}</span>
                      </div>
                    </div>

                    {/* P&L */}
                    <div className="flex-shrink-0 text-right">
                      <div className={`text-base font-bold ${pcDash(pos.pnl)}`}>{fmtPnl(pos.pnl)}€</div>
                      <div className={`text-[10px] ${pcDash(pos.pnl_percent || 0)}`}>
                        {(pos.pnl_percent || 0) >= 0 ? '+' : ''}{(pos.pnl_percent || 0).toFixed(2)}%
                      </div>
                    </div>

                    {/* Close button */}
                    <button
                      onClick={() => {
                        if (confirm(`Fermer ${pos.symbol} ?`)) {
                          setClosingSymbol(pos.symbol);
                          backendClosePosition(pos.symbol).finally(() => setClosingSymbol(null));
                        }
                      }}
                      disabled={closingSymbol === pos.symbol}
                      className="flex-shrink-0 px-2.5 py-1.5 rounded-lg bg-red-500 text-white text-[10px] font-bold hover:bg-red-600 disabled:opacity-50 transition-colors"
                    >
                      {closingSymbol === pos.symbol ? '...' : 'Fermer'}
                    </button>
                  </div>

                  {/* Mini progress bar SL↔TP */}
                  {sl > 0 && tp > 0 && (
                    <div className="mt-1.5 mx-4">
                      <div className="relative h-2 bg-gray-200 rounded-full">
                        {currentPct < entryPct && (
                          <div className="absolute top-0 h-full bg-gradient-to-l from-red-400 to-red-600 transition-all duration-500"
                            style={{ left: `${currentPct}%`, width: `${entryPct - currentPct}%`, borderRadius: '9999px 0 0 9999px' }} />
                        )}
                        {currentPct > entryPct && (
                          <div className="absolute top-0 h-full bg-gradient-to-r from-green-400 to-green-600 transition-all duration-500"
                            style={{ left: `${entryPct}%`, width: `${currentPct - entryPct}%`, borderRadius: '0 9999px 9999px 0' }} />
                        )}
                        <div className="absolute top-[-2px] bottom-[-2px] w-[3px] bg-blue-600 z-10 rounded-full"
                          style={{ left: `calc(${entryPct}% - 1.5px)` }} />
                        <div className={`absolute top-1/2 -translate-y-1/2 w-3 h-3 rounded-full border-2 border-white shadow z-20 transition-all duration-500 ${pos.pnl >= 0 ? 'bg-green-500' : 'bg-red-500'}`}
                          style={{ left: `calc(${currentPct}% - 6px)` }} />
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Row 5: Performance Charts ─────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-3">
        <div className="bg-white rounded-xl shadow-sm border p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 mb-2">
            <TrendingUp className="w-4 h-4 text-green-600" />
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-green-100 text-green-700">Fusion Markets MT5</span>
            P&L + Capital
          </h3>
          <div className="grid grid-cols-4 gap-2 mb-3">
            {[
              { label: 'Forex', pnl: todayCatPnl.forex },
              { label: 'Actions', pnl: todayCatPnl.actions },
              { label: 'Indices', pnl: todayCatPnl.indices },
              { label: 'Matieres', pnl: todayCatPnl.commodities },
            ].map(cat => (
              <div key={cat.label} className="text-center p-2 rounded-lg bg-gray-50">
                <div className="text-[10px] text-gray-500">{cat.label}</div>
                <div className={`text-sm font-bold ${cat.pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {cat.pnl >= 0 ? '+' : ''}{cat.pnl.toFixed(2)}
                </div>
              </div>
            ))}
          </div>
          <BrokerPerformanceChart
            perfHistory={perfHistory}
            todayPnl={totalClosedPnl + totalOpenPnl}
            todayCapital={ctEquity || ctBalance}
            capitalKey="ending_capital"
            pnlLabel="P&L Fusion Markets MT5"
            capitalLabel="Capital Fusion Markets MT5"
            accentColor="#14b8a6"
          />

          {/* Tableau P&L journalier — 5 derniers jours */}
          {perfHistory.length > 0 && (
            <div className="mt-3 overflow-hidden rounded-lg border border-gray-200">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-gray-50 text-gray-500">
                    <th className="text-left px-2 py-1.5 font-medium">Date</th>
                    <th className="text-right px-2 py-1.5 font-medium">P&L</th>
                    <th className="text-right px-2 py-1.5 font-medium">Capital</th>
                    <th className="text-right px-2 py-1.5 font-medium">Trades</th>
                    <th className="text-right px-2 py-1.5 font-medium">WR%</th>
                  </tr>
                </thead>
                <tbody>
                  {[...perfHistory].slice(-5).map((day, i) => {
                    const d = new Date(day.date);
                    const dateStr = d.toLocaleDateString('fr-FR', { weekday: 'short', day: '2-digit', month: '2-digit' });
                    const total = day.pnl || 0;
                    const cap = day.ending_capital || 0;
                    const trades = day.trades_count || 0;
                    const wr = day.win_rate || 0;
                    return (
                      <tr key={day.date} className={`border-t border-gray-100 ${i % 2 === 0 ? 'bg-white' : 'bg-gray-50/50'}`}>
                        <td className="px-2 py-1.5 font-medium text-gray-700">{dateStr}</td>
                        <td className={`px-2 py-1.5 text-right font-bold ${total >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                          {total >= 0 ? '+' : ''}{total.toFixed(2)}€
                        </td>
                        <td className="px-2 py-1.5 text-right font-semibold text-teal-700">
                          {cap > 0 ? formatCurrency(cap) : '--'}
                        </td>
                        <td className="px-2 py-1.5 text-right text-gray-600">{trades}</td>
                        <td className={`px-2 py-1.5 text-right font-semibold ${wr >= 55 ? 'text-green-600' : wr >= 45 ? 'text-orange-500' : 'text-red-600'}`}>
                          {wr > 0 ? wr.toFixed(0) + '%' : '-'}
                        </td>
                      </tr>
                    );
                  })}
                  {perfHistory.length > 1 && (() => {
                    const last5 = [...perfHistory].slice(-5);
                    const cumTotal = last5.reduce((s, d) => s + (d.pnl || 0), 0);
                    const cumTrades = last5.reduce((s, d) => s + (d.trades_count || 0), 0);
                    return (
                      <tr className="border-t-2 border-gray-300 bg-gray-100 font-bold">
                        <td className="px-2 py-1.5 text-gray-700">Cumul</td>
                        <td className={`px-2 py-1.5 text-right ${cumTotal >= 0 ? 'text-green-700' : 'text-red-700'}`}>
                          {cumTotal >= 0 ? '+' : ''}{cumTotal.toFixed(2)}€
                        </td>
                        <td className="px-2 py-1.5 text-right text-gray-500">-</td>
                        <td className="px-2 py-1.5 text-right text-gray-700">{cumTrades}</td>
                        <td className="px-2 py-1.5 text-right text-gray-500">-</td>
                      </tr>
                    );
                  })()}
                </tbody>
              </table>
            </div>
          )}
        </div>

      </div>

      {/* ── Row 7: Signaux Proches d'Entrée ─────────────────────────── */}
      <SignauxProches signals={signals} />

      {/* ── Row 9: Allocation Dynamique ───────────────────────────────── */}
      {botStatus?.dynamic_allocation && (
        <div className="bg-white rounded-xl shadow-sm border p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 mb-3">
            <PieChart className="w-4 h-4 text-blue-600" /> Allocation Dynamique
            <span className="text-[10px] font-normal text-gray-400 ml-1">(ajustee automatiquement selon les signaux)</span>
          </h3>
          {(() => {
            const alloc = botStatus.dynamic_allocation as Record<string, number>;
            const categories = [
              { key: 'FOREX', label: 'Forex', color: '#3b82f6', maxLabel: '80%' },
              { key: 'STOCKS', label: 'Actions', color: '#10b981', maxLabel: '50%' },
              { key: 'INDICES', label: 'Indices', color: '#8b5cf6', maxLabel: '20%' },
              { key: 'COMMODITY', label: 'Matieres', color: '#f59e0b', maxLabel: '20%' },
            ];
            return (
              <div>
                <div className="flex h-8 rounded-lg overflow-hidden mb-3">
                  {categories.map(cat => {
                    const pct = (alloc[cat.key] || 0) * 100;
                    if (pct < 1) return null;
                    return (
                      <div key={cat.key} className="flex items-center justify-center text-white text-[10px] font-bold transition-all duration-700"
                        style={{ width: `${pct}%`, backgroundColor: cat.color, minWidth: pct > 3 ? undefined : '20px' }}>
                        {pct >= 5 ? `${pct.toFixed(0)}%` : ''}
                      </div>
                    );
                  })}
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  {categories.map(cat => {
                    const pct = (alloc[cat.key] || 0) * 100;
                    const allocEur = capital * (alloc[cat.key] || 0);
                    return (
                      <div key={cat.key} className="text-center">
                        <div className="flex items-center justify-center gap-1.5 mb-1">
                          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: cat.color }} />
                          <span className="text-xs font-medium text-gray-700">{cat.label}</span>
                        </div>
                        <div className="text-lg font-bold text-gray-900">{pct.toFixed(0)}%</div>
                        <div className="text-[10px] text-gray-500">{formatCurrency(allocEur)}</div>
                        <div className="text-[9px] text-gray-400">max {cat.maxLabel}</div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </div>
      )}

      {/* ── Row 8: Risk + Signaux ────────────────────────────────────── */}
      <div className="grid lg:grid-cols-2 gap-4">
        {botStatus && (
          <div className="bg-white rounded-xl shadow-sm border p-4">
            <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 mb-3">
              <Shield className="w-4 h-4 text-blue-600" /> Garde-fous
            </h3>
            <div className="grid grid-cols-2 gap-3">
              <RiskGauge label="Circuit Breaker" ok={!botStatus.circuit_breaker}
                value={botStatus.circuit_breaker ? 'ACTIF' : 'OK'} />
              <RiskGauge label="Pertes consecutives" ok={botStatus.consecutive_losses < 2}
                value={`${botStatus.consecutive_losses}/3`} />
              <RiskGauge label="Positions" ok={botStatus.open_positions < (botConfig?.max_open_positions || 5)}
                value={`${botStatus.open_positions}/${botConfig?.max_open_positions || 5}`} />
              <RiskGauge label="Marge utilisee" ok={totalMarginUsed < capital * 0.8}
                value={`${formatCurrency(totalMarginUsed)} / ${formatCurrency(capital * 0.8)}`} />
            </div>
          </div>
        )}

        <div className="bg-white rounded-xl shadow-sm border p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-gray-900 mb-3">
            <Target className="w-4 h-4 text-blue-600" /> Derniers Signaux
          </h3>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {signals.filter(s => s.signal !== 'hold').slice(0, 8).map((sig, i) => (
              <div key={i} className="flex items-center justify-between text-xs py-1">
                <div className="flex items-center gap-2">
                  <span className={`font-bold px-1.5 py-0.5 rounded ${
                    sig.signal === 'buy' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'
                  }`}>
                    {sig.signal?.toUpperCase()}
                  </span>
                  <span className="font-medium text-gray-900">{sig.symbol}</span>
                </div>
                <div className="text-right">
                  <span className="text-gray-600">{sig.confidence}%</span>
                  <span className="text-gray-400 ml-2">{sig.price?.toFixed(4)}</span>
                </div>
              </div>
            ))}
            {signals.filter(s => s.signal !== 'hold').length === 0 && (
              <p className="text-xs text-gray-400 text-center py-2">Aucun signal actif</p>
            )}
          </div>
        </div>
      </div>

      {/* ── Row 9: Activity Log ────────────────────────────────────── */}
      <div className="bg-white rounded-xl shadow-sm border">
        <div className="flex items-center justify-between px-4 py-2 border-b">
          <h3 className="flex items-center gap-2 text-xs font-semibold text-gray-600">
            <Activity className="w-3 h-3" /> Journal Bot
          </h3>
          <span className="text-[10px] text-gray-400">{logs.length} events</span>
        </div>
        <div className="max-h-32 overflow-y-auto p-2 space-y-0.5">
          {logs.length === 0 ? (
            <p className="text-xs text-gray-400 text-center py-2">Aucune activite</p>
          ) : (
            logs.slice(0, 30).map((log, i) => (
              <div key={i} className={`text-[10px] font-mono py-0.5 px-2 rounded ${
                log.includes('URGENCE') || log.includes('EMERGENCY') ? 'bg-red-50 text-red-700' :
                log.includes('Ordre') || log.includes('Trade') || log.includes('BUY') || log.includes('SELL') ? 'bg-green-50 text-green-700' :
                log.includes('erreur') || log.includes('ERROR') || log.includes('REJECTED') ? 'bg-orange-50 text-orange-700' :
                'text-gray-500'
              }`}>
                {log}
              </div>
            ))
          )}
        </div>
      </div>

      {/* ── Créneaux Scalping (info) ─────────────────────────────────── */}
      <ScalpingWindows />
    </div>
  );
}

// ═══════════════════════════════════════════════════════════════════════
// SUB-COMPONENTS
// ═══════════════════════════════════════════════════════════════════════

/** Carte broker avec toutes les infos et controles */
function BrokerCard({
  name, subtitle, badgeClass, gradientFrom, gradientTo, accentColor,
  balance, equity, freeMargin, profit,
  marginUsed, closedPnl, openPnl, openCount, closedCount, wins, losses,
  isConnected, isRunning, onStart, onStop, botConnected, onSyncBalance,
  onScanOnly, onSetMode, isScanOnly,
}: {
  name: string; subtitle: string; badgeClass: string;
  gradientFrom: string; gradientTo: string; accentColor: string;
  balance: number; equity: number; freeMargin: number; profit: number;
  marginUsed: number; closedPnl: number; openPnl: number;
  openCount: number; closedCount: number; wins: number; losses: number;
  isConnected?: boolean; isRunning?: boolean;
  onStart: () => Promise<void>; onStop: () => Promise<void>;
  botConnected?: boolean; onSyncBalance?: () => void;
  onScanOnly?: () => Promise<void>; onSetMode?: (mode: 'auto' | 'scan') => Promise<void>; isScanOnly?: boolean;
}) {
  const totalWL = wins + losses;
  const winRate = totalWL > 0 ? (wins / totalWL * 100).toFixed(0) : '-';

  return (
    <div className={`bg-gradient-to-r ${gradientFrom} ${gradientTo} rounded-xl shadow-sm border p-4`}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${badgeClass}`}>{name}</span>
          <span className="text-xs text-gray-500">{subtitle}</span>
          <span className={`w-2 h-2 rounded-full ${isConnected ? 'bg-green-500' : 'bg-gray-400'}`} />
        </div>
        <div className="flex items-center gap-2">
          {isRunning ? (
            <>
              {isScanOnly ? (
                <>
                  <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 animate-pulse">SCAN</span>
                  {onSetMode && (
                    <button onClick={() => onSetMode('auto')}
                      className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white rounded-lg hover:bg-green-700 transition text-xs font-medium">
                      <Play className="w-3 h-3" /> Passer en Auto
                    </button>
                  )}
                </>
              ) : (
                <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-green-100 text-green-700 animate-pulse">AUTO</span>
              )}
              {onSetMode && !isScanOnly && (
                <button onClick={() => onSetMode('scan')}
                  className="flex items-center gap-1.5 px-2.5 py-1.5 bg-blue-500 text-white rounded-lg hover:bg-blue-600 transition text-xs font-medium">
                  <Eye className="w-3 h-3" /> Scan
                </button>
              )}
              <button onClick={onStop}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-orange-600 text-white rounded-lg hover:bg-orange-700 transition text-xs font-medium">
                <Square className="w-3 h-3" /> Stop
              </button>
            </>
          ) : (
            <>
              {onScanOnly && (
                <button onClick={onScanOnly}
                  disabled={!botConnected}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition text-xs font-medium disabled:opacity-50"
                  title="Scanner les signaux sans executer">
                  <Eye className="w-3 h-3" /> Scan
                </button>
              )}
              <button onClick={onStart}
                disabled={!botConnected}
                className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white rounded-lg hover:bg-green-700 transition text-xs font-medium disabled:opacity-50">
                <Play className="w-3 h-3" /> Start Auto
              </button>
            </>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
        {/* Balance */}
        <div>
          <div className="text-[10px] text-gray-500 flex items-center gap-1">
            Balance
            {onSyncBalance && (
              <button onClick={onSyncBalance} title="Synchroniser avec MT5"
                className="text-blue-500 hover:text-blue-700 transition">
                <RefreshCw className="w-3 h-3" />
              </button>
            )}
          </div>
          <div className="text-base font-bold text-gray-900">
            {balance > 0 ? formatCurrency(balance) : formatCurrency(0)}
          </div>
        </div>

        {/* Equity */}
        <div>
          <div className="text-[10px] text-gray-500">Equity</div>
          <div className="text-base font-bold text-gray-900">
            {equity > 0 ? formatCurrency(equity) : '--'}
          </div>
        </div>

        {/* Marge Libre */}
        <div>
          <div className="text-[10px] text-gray-500">Marge Libre</div>
          <div className="text-base font-bold text-gray-900">
            {freeMargin > 0 ? formatCurrency(freeMargin) : '--'}
          </div>
        </div>

        {/* Capital Engagé */}
        <div>
          <div className="text-[10px] text-gray-500">Capital Engage</div>
          <div className="text-base font-bold text-blue-600">
            {formatCurrency(marginUsed)}
          </div>
          <div className="text-[9px] text-gray-400">{openCount} pos.</div>
        </div>

        {/* P&L Encaissé */}
        <div>
          <div className="text-[10px] text-gray-500">P&L Encaisse</div>
          <div className={`text-base font-bold ${closedPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {closedPnl >= 0 ? '+' : ''}{closedPnl.toFixed(2)}€
          </div>
          <div className="text-[9px] text-gray-400">{closedCount} trades</div>
        </div>

        {/* P&L Ouvert */}
        <div>
          <div className="text-[10px] text-gray-500">P&L Ouvert</div>
          <div className={`text-base font-bold ${openPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
            {openPnl >= 0 ? '+' : ''}{openPnl.toFixed(2)}€
          </div>
        </div>

        {/* W/L */}
        <div>
          <div className="text-[10px] text-gray-500">W/L (WR)</div>
          <div className="text-base font-bold text-gray-900">
            <span className="text-green-600">{wins}</span>
            <span className="text-gray-400">/</span>
            <span className="text-red-600">{losses}</span>
            <span className="text-gray-400 text-xs ml-1">({winRate}%)</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function RiskGauge({ label, ok, value }: { label: string; ok: boolean; value: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${ok ? 'bg-green-500' : 'bg-red-500'}`} />
      <div className="min-w-0">
        <div className="text-[10px] text-gray-500 truncate">{label}</div>
        <div className="text-xs font-medium text-gray-900">{value}</div>
      </div>
    </div>
  );
}
