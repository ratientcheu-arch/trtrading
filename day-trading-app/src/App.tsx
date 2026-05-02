import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  TrendingUp, TrendingDown, Clock, BarChart3, Target,
  Play, Square, AlertTriangle, CheckCircle, XCircle,
  RefreshCw, Info, Shield, Star,
  Calendar, ArrowUpRight, ArrowDownRight, Minus, Eye, Plus, Trash2, Filter,
  LineChart, BookOpen, Home, Activity, Zap, Cpu, Wifi
} from 'lucide-react';
import { useBackend } from './useBackend';
import ControlPanel from './ControlPanel';
import Dashboard from './components/Dashboard';
import PositionsTab from './components/PositionsTab';

// ─── Config ──────────────────────────────────────────────────────────────────

const SUPABASE_URL = 'https://wvzrbugugabdyjpvpzto.supabase.co';
const REFRESH_INTERVAL = 30 * 60 * 1000; // 30 minutes — scalping mode

// ─── Types ───────────────────────────────────────────────────────────────────

interface Candle {
  timestamp: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

interface TechnicalIndicators {
  rsi14: number | null;
  macd: { macd: number; signal: number; histogram: number } | null;
  bollingerBands: { upper: number; middle: number; lower: number; width: number } | null;
  sma20: number | null;
  sma50: number | null;
  ema12: number | null;
  ema26: number | null;
  atr14: number | null;
  fibonacci: { r1: number; r2: number; r3: number; s1: number; s2: number; s3: number; pivot: number } | null;
  volumeAvg20: number | null;
  volumeRatio: number | null;
  stochastic: { k: number; d: number } | null;
  adx: number | null;
}

interface Asset {
  symbol: string;
  name: string;
  type: 'stock' | 'etf' | 'crypto' | 'index' | 'forex' | 'commodity';
  market: 'EU' | 'US' | 'CRYPTO' | 'FOREX' | 'COMMODITY';
  currentPrice: number;
  change: number;
  changePercent: number;
  volume: string;
  entryWindow: { start: string; end: string };
  exitWindow: { start: string; end: string };
  signal: 'buy' | 'sell' | 'hold';
  confidence: number;
  reason: string;
  indicators?: TechnicalIndicators;
  suggestedEntry?: number;
  suggestedStopLoss?: number;
  suggestedTakeProfit?: number;
  candles?: Candle[];
}

interface Position {
  id: string;
  symbol: string;
  name: string;
  type: 'long' | 'short';
  entryPrice: number;
  currentPrice: number;
  quantity: number;
  entryTime: string;
  exitTime?: string;
  exitPrice?: number;
  status: 'open' | 'closed';
  pnl: number;
  pnlPercent: number;
  stopLoss?: number;
  takeProfit?: number;
}

interface DailyPerformance {
  date: string;
  pnl: number;
  trades: number;
  winRate: number;
  totalInvested: number;
}

interface Platform {
  name: string;
  logo: string;
  regulator: string;
  country: string;
  minDeposit: number;
  fees: string;
  instruments: string[];
  rating: number;
  pros: string[];
  cons: string[];
  url: string;
  amfRegistered: boolean;
}

// ─── Technical Analysis Functions ────────────────────────────────────────────

function computeSMA(closes: number[], period: number): number | null {
  if (closes.length < period) return null;
  const slice = closes.slice(-period);
  return slice.reduce((a, b) => a + b, 0) / period;
}

function computeEMA(closes: number[], period: number): number | null {
  if (closes.length < period) return null;
  const k = 2 / (period + 1);
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
  }
  return ema;
}

function computeRSI(closes: number[], period: number = 14): number | null {
  if (closes.length < period + 1) return null;
  let gains = 0, losses = 0;
  for (let i = closes.length - period; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff > 0) gains += diff;
    else losses -= diff;
  }
  const avgGain = gains / period;
  const avgLoss = losses / period;
  if (avgLoss === 0) return 100;
  const rs = avgGain / avgLoss;
  return 100 - (100 / (1 + rs));
}

function computeMACD(closes: number[]): { macd: number; signal: number; histogram: number } | null {
  if (closes.length < 35) return null;
  const ema12 = computeEMA(closes, 12);
  const ema26 = computeEMA(closes, 26);
  if (ema12 === null || ema26 === null) return null;
  const macdLine = ema12 - ema26;

  // Compute MACD history for signal line
  const macdHistory: number[] = [];
  for (let i = 26; i <= closes.length; i++) {
    const e12 = computeEMA(closes.slice(0, i), 12);
    const e26 = computeEMA(closes.slice(0, i), 26);
    if (e12 !== null && e26 !== null) macdHistory.push(e12 - e26);
  }

  const signalLine = macdHistory.length >= 9
    ? computeEMA(macdHistory, 9)
    : null;
  if (signalLine === null) return null;

  return {
    macd: parseFloat(macdLine.toFixed(4)),
    signal: parseFloat(signalLine.toFixed(4)),
    histogram: parseFloat((macdLine - signalLine).toFixed(4)),
  };
}

function computeBollingerBands(closes: number[], period: number = 20, multiplier: number = 2): { upper: number; middle: number; lower: number; width: number } | null {
  if (closes.length < period) return null;
  const slice = closes.slice(-period);
  const mean = slice.reduce((a, b) => a + b, 0) / period;
  const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period;
  const stdDev = Math.sqrt(variance);
  const upper = mean + multiplier * stdDev;
  const lower = mean - multiplier * stdDev;
  return {
    upper: parseFloat(upper.toFixed(4)),
    middle: parseFloat(mean.toFixed(4)),
    lower: parseFloat(lower.toFixed(4)),
    width: parseFloat(((upper - lower) / mean * 100).toFixed(2)),
  };
}

function computeATR(candles: Candle[], period: number = 14): number | null {
  if (candles.length < period + 1) return null;
  const trs: number[] = [];
  for (let i = candles.length - period; i < candles.length; i++) {
    const high = candles[i].high;
    const low = candles[i].low;
    const prevClose = candles[i - 1].close;
    trs.push(Math.max(high - low, Math.abs(high - prevClose), Math.abs(low - prevClose)));
  }
  return trs.reduce((a, b) => a + b, 0) / period;
}

function computeFibonacci(candles: Candle[]): { r1: number; r2: number; r3: number; s1: number; s2: number; s3: number; pivot: number } | null {
  if (candles.length < 20) return null;
  const recent = candles.slice(-20);
  const high = Math.max(...recent.map(c => c.high));
  const low = Math.min(...recent.map(c => c.low));
  const close = candles[candles.length - 1].close;
  const pivot = (high + low + close) / 3;
  const range = high - low;
  return {
    pivot: parseFloat(pivot.toFixed(4)),
    s1: parseFloat((pivot - range * 0.236).toFixed(4)),
    s2: parseFloat((pivot - range * 0.382).toFixed(4)),
    s3: parseFloat((pivot - range * 0.618).toFixed(4)),
    r1: parseFloat((pivot + range * 0.236).toFixed(4)),
    r2: parseFloat((pivot + range * 0.382).toFixed(4)),
    r3: parseFloat((pivot + range * 0.618).toFixed(4)),
  };
}

function computeStochastic(candles: Candle[], kPeriod: number = 14, dPeriod: number = 3): { k: number; d: number } | null {
  if (candles.length < kPeriod + dPeriod) return null;
  const kValues: number[] = [];
  for (let i = candles.length - dPeriod; i < candles.length; i++) {
    const slice = candles.slice(i - kPeriod + 1, i + 1);
    const high = Math.max(...slice.map(c => c.high));
    const low = Math.min(...slice.map(c => c.low));
    const close = candles[i].close;
    kValues.push(high === low ? 50 : ((close - low) / (high - low)) * 100);
  }
  const k = kValues[kValues.length - 1];
  const d = kValues.reduce((a, b) => a + b, 0) / kValues.length;
  return { k: parseFloat(k.toFixed(1)), d: parseFloat(d.toFixed(1)) };
}

function computeADX(candles: Candle[], period: number = 14): number | null {
  if (candles.length < period * 2 + 1) return null;
  const plusDMs: number[] = [];
  const minusDMs: number[] = [];
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const upMove = candles[i].high - candles[i - 1].high;
    const downMove = candles[i - 1].low - candles[i].low;
    plusDMs.push(upMove > downMove && upMove > 0 ? upMove : 0);
    minusDMs.push(downMove > upMove && downMove > 0 ? downMove : 0);
    const tr = Math.max(
      candles[i].high - candles[i].low,
      Math.abs(candles[i].high - candles[i - 1].close),
      Math.abs(candles[i].low - candles[i - 1].close)
    );
    trs.push(tr);
  }

  if (trs.length < period) return null;
  let atrSmooth = trs.slice(0, period).reduce((a, b) => a + b, 0);
  let plusDMSmooth = plusDMs.slice(0, period).reduce((a, b) => a + b, 0);
  let minusDMSmooth = minusDMs.slice(0, period).reduce((a, b) => a + b, 0);
  const dxValues: number[] = [];

  for (let i = period; i < trs.length; i++) {
    atrSmooth = atrSmooth - atrSmooth / period + trs[i];
    plusDMSmooth = plusDMSmooth - plusDMSmooth / period + plusDMs[i];
    minusDMSmooth = minusDMSmooth - minusDMSmooth / period + minusDMs[i];
    const plusDI = atrSmooth > 0 ? (plusDMSmooth / atrSmooth) * 100 : 0;
    const minusDI = atrSmooth > 0 ? (minusDMSmooth / atrSmooth) * 100 : 0;
    const diSum = plusDI + minusDI;
    const dx = diSum > 0 ? (Math.abs(plusDI - minusDI) / diSum) * 100 : 0;
    dxValues.push(dx);
  }

  if (dxValues.length < period) return null;
  const adx = dxValues.slice(-period).reduce((a, b) => a + b, 0) / period;
  return parseFloat(adx.toFixed(1));
}

function computeAllIndicators(candles: Candle[]): TechnicalIndicators {
  const closes = candles.map(c => c.close);
  const volumes = candles.map(c => c.volume);
  const volumeAvg20 = volumes.length >= 20
    ? volumes.slice(-20).reduce((a, b) => a + b, 0) / 20
    : null;
  const currentVol = volumes[volumes.length - 1] || 0;
  return {
    rsi14: computeRSI(closes, 14),
    macd: computeMACD(closes),
    bollingerBands: computeBollingerBands(closes, 20, 2),
    sma20: computeSMA(closes, 20),
    sma50: computeSMA(closes, 50),
    ema12: computeEMA(closes, 12),
    ema26: computeEMA(closes, 26),
    atr14: computeATR(candles, 14),
    fibonacci: computeFibonacci(candles),
    volumeAvg20,
    volumeRatio: volumeAvg20 && volumeAvg20 > 0 ? parseFloat((currentVol / volumeAvg20).toFixed(2)) : null,
    stochastic: computeStochastic(candles, 14, 3),
    adx: computeADX(candles, 14),
  };
}

// ─── Signal Generation (Multi-Indicator Confluence) ──────────────────────────

function generateSignalFromIndicators(
  price: number,
  indicators: TechnicalIndicators,
  changePercent: number,
): { signal: 'buy' | 'sell' | 'hold'; confidence: number; reason: string; suggestedEntry: number; suggestedSL: number; suggestedTP: number } {
  let bullScore = 0;
  let bearScore = 0;
  const reasons: string[] = [];

  // ── 1. RSI Analysis (weight: 2) ──
  if (indicators.rsi14 !== null) {
    if (indicators.rsi14 < 30) { bullScore += 2; reasons.push(`RSI ${indicators.rsi14.toFixed(0)} survendu`); }
    else if (indicators.rsi14 < 45) { bullScore += 1; reasons.push(`RSI ${indicators.rsi14.toFixed(0)} zone basse`); }
    else if (indicators.rsi14 > 70) { bearScore += 2; reasons.push(`RSI ${indicators.rsi14.toFixed(0)} surachete`); }
    else if (indicators.rsi14 > 55) { bearScore += 1; reasons.push(`RSI ${indicators.rsi14.toFixed(0)} zone haute`); }
    else { reasons.push(`RSI ${indicators.rsi14.toFixed(0)} neutre`); }
  }

  // ── 2. MACD Analysis (weight: 2) ──
  if (indicators.macd) {
    if (indicators.macd.histogram > 0 && indicators.macd.macd > indicators.macd.signal) {
      bullScore += 2;
      reasons.push('MACD haussier');
    } else if (indicators.macd.histogram < 0 && indicators.macd.macd < indicators.macd.signal) {
      bearScore += 2;
      reasons.push('MACD baissier');
    } else if (indicators.macd.histogram > 0) {
      bullScore += 1;
      reasons.push('MACD positif');
    } else {
      bearScore += 1;
      reasons.push('MACD negatif');
    }
  }

  // ── 3. Bollinger Bands (weight: 2) ──
  if (indicators.bollingerBands) {
    const bb = indicators.bollingerBands;
    const bbPosition = (price - bb.lower) / (bb.upper - bb.lower);
    if (bbPosition < 0.15) { bullScore += 2; reasons.push('Prix sur bande Bollinger basse'); }
    else if (bbPosition < 0.35) { bullScore += 1; reasons.push('Prix en zone basse Bollinger'); }
    else if (bbPosition > 0.85) { bearScore += 2; reasons.push('Prix sur bande Bollinger haute'); }
    else if (bbPosition > 0.65) { bearScore += 1; reasons.push('Prix en zone haute Bollinger'); }

    if (bb.width < 5) { reasons.push('Bollinger squeeze'); bullScore += 1; }
  }

  // ── 4. Moving Averages (weight: 2) ──
  if (indicators.sma20 !== null && indicators.sma50 !== null) {
    if (indicators.sma20 > indicators.sma50 && price > indicators.sma20) {
      bullScore += 2;
      reasons.push('Tendance haussiere (Prix > SMA20 > SMA50)');
    } else if (indicators.sma20 < indicators.sma50 && price < indicators.sma20) {
      bearScore += 2;
      reasons.push('Tendance baissiere (Prix < SMA20 < SMA50)');
    } else if (price > indicators.sma20) {
      bullScore += 1;
      reasons.push('Prix au-dessus SMA20');
    } else if (price > indicators.sma50) {
      // Price between SMA50 (support) and SMA20 - potential bounce
      bullScore += 1;
      reasons.push('Prix entre SMA50 (support) et SMA20');
    } else {
      bearScore += 1;
      reasons.push('Prix sous SMA20 et SMA50');
    }
  }

  // ── 5. Stochastic (weight: 2 - widened zones) ──
  if (indicators.stochastic) {
    if (indicators.stochastic.k < 20) {
      bullScore += 2;
      reasons.push(`Stoch survendu K:${indicators.stochastic.k}`);
    } else if (indicators.stochastic.k < 35) {
      bullScore += 1;
      reasons.push(`Stoch bas K:${indicators.stochastic.k}`);
    } else if (indicators.stochastic.k > 80) {
      bearScore += 2;
      reasons.push(`Stoch surachete K:${indicators.stochastic.k}`);
    } else if (indicators.stochastic.k > 65) {
      bearScore += 1;
      reasons.push(`Stoch haut K:${indicators.stochastic.k}`);
    }
  }

  // ── 6. Price Momentum (weight: 2 - NEW) ──
  if (changePercent > 2) { bullScore += 2; reasons.push(`Momentum +${changePercent.toFixed(1)}% fort`); }
  else if (changePercent > 0.5) { bullScore += 1; reasons.push(`Momentum +${changePercent.toFixed(1)}%`); }
  else if (changePercent < -2) { bearScore += 2; reasons.push(`Momentum ${changePercent.toFixed(1)}% fort`); }
  else if (changePercent < -0.5) { bearScore += 1; reasons.push(`Momentum ${changePercent.toFixed(1)}%`); }

  // ── 7. ADX - Trend Strength (weight: 1) ──
  if (indicators.adx !== null) {
    if (indicators.adx > 25) {
      reasons.push(`ADX ${indicators.adx} tendance forte`);
      if (bullScore > bearScore) bullScore += 1;
      else if (bearScore > bullScore) bearScore += 1;
    } else if (indicators.adx > 20) {
      reasons.push(`ADX ${indicators.adx} tendance moderee`);
    } else {
      reasons.push(`ADX ${indicators.adx} pas de tendance`);
    }
  }

  // ── 8. Volume confirmation (weight: 1) ──
  if (indicators.volumeRatio !== null) {
    if (indicators.volumeRatio > 1.3) {
      reasons.push(`Volume ${indicators.volumeRatio}x moy.`);
      if (bullScore > bearScore) bullScore += 1;
      else if (bearScore > bullScore) bearScore += 1;
    } else if (indicators.volumeRatio < 0.5) {
      reasons.push('Volume faible');
    }
  }

  // ── 9. Fibonacci levels (weight: 1) ──
  if (indicators.fibonacci) {
    const fib = indicators.fibonacci;
    const tolerance = 0.01; // 1% tolerance
    if (Math.abs(price - fib.s1) / price < tolerance) {
      bullScore += 1;
      reasons.push('Support Fibonacci S1');
    } else if (Math.abs(price - fib.s2) / price < tolerance) {
      bullScore += 1;
      reasons.push('Support Fibonacci S2');
    } else if (price < fib.pivot) {
      bullScore += 1;
      reasons.push('Sous le pivot Fibonacci');
    } else if (Math.abs(price - fib.r1) / price < tolerance) {
      bearScore += 1;
      reasons.push('Resistance Fibonacci R1');
    } else if (price > fib.r2) {
      bearScore += 1;
      reasons.push('Au-dessus R2 Fibonacci');
    }
  }

  // ── Calculate confidence and signal ──
  const totalPossible = 17; // max possible bull or bear score
  const netScore = bullScore - bearScore;
  const absNet = Math.abs(netScore);
  const rawConfidence = absNet / totalPossible * 100;

  // Signal thresholds: netScore >= 3 for buy, <= -3 for sell
  // This means at least 3 more bullish indicators than bearish
  let signal: 'buy' | 'sell' | 'hold';
  let confidence: number;

  // Day trading: seuil bas pour generer plus de signaux intraday
  // netScore >= 2 = ACHAT, >= 4 = ACHAT FORT
  // netScore <= -2 = VENTE, <= -4 = VENTE FORTE
  if (netScore >= 4) {
    signal = 'buy';
    confidence = Math.min(95, 65 + rawConfidence);
  } else if (netScore >= 2) {
    signal = 'buy';
    confidence = Math.min(80, 50 + rawConfidence);
  } else if (netScore <= -4) {
    signal = 'sell';
    confidence = Math.min(95, 65 + rawConfidence);
  } else if (netScore <= -2) {
    signal = 'sell';
    confidence = Math.min(80, 50 + rawConfidence);
  } else {
    signal = 'hold';
    confidence = Math.max(20, 40 - absNet * 5);
  }

  // Calculate entry, stop-loss and take-profit for DAY TRADING
  // Use tighter levels suitable for intraday positions (few hours)
  const atr = indicators.atr14 || price * 0.015;
  const suggestedEntry = price;
  let suggestedSL: number;
  let suggestedTP: number;

  if (signal === 'buy') {
    suggestedSL = price - atr * 1;   // 1x ATR stop loss (tight for day trading)
    suggestedTP = price + atr * 2;   // 2x ATR take profit (2:1 risk/reward)
    // Ensure minimum 2% gain target for day trading
    const minTP = price * 1.02;
    if (suggestedTP < minTP) suggestedTP = minTP;
    // Cap at 8% for realistic intraday target
    const maxTP = price * 1.08;
    if (suggestedTP > maxTP) suggestedTP = maxTP;
  } else if (signal === 'sell') {
    suggestedSL = price + atr * 1;
    suggestedTP = price - atr * 2;
    const minTP = price * 0.98;
    if (suggestedTP > minTP) suggestedTP = minTP;
    const maxTP = price * 0.92;
    if (suggestedTP < maxTP) suggestedTP = maxTP;
  } else {
    suggestedSL = price - atr * 1;
    suggestedTP = price + atr * 2;
  }

  return {
    signal,
    confidence: parseFloat(confidence.toFixed(0)),
    reason: reasons.slice(0, 6).join(' | '),
    suggestedEntry,
    suggestedSL: parseFloat(suggestedSL.toFixed(4)),
    suggestedTP: parseFloat(suggestedTP.toFixed(4)),
  };
}

// ─── Constants ───────────────────────────────────────────────────────────────

const MARKET_HOURS: Record<string, { open: string; close: string; label: string; timezone: string }> = {
  EU: { open: '09:00', close: '17:30', label: 'Euronext Paris', timezone: 'CET' },
  US: { open: '15:30', close: '22:00', label: 'NYSE / NASDAQ', timezone: 'CET (heure FR)' },
  CRYPTO: { open: '00:00', close: '23:59', label: 'Crypto (24/7)', timezone: 'UTC' },
  FOREX: { open: '00:00', close: '23:59', label: 'Forex (24/5)', timezone: 'UTC' },
  COMMODITY: { open: '08:00', close: '22:00', label: 'Matieres Premieres', timezone: 'CET' },
};

const REGULATED_PLATFORMS: Platform[] = [
  {
    name: 'Trade Republic',
    logo: '🏦',
    regulator: 'BaFin (Allemagne) + Enregistre AMF',
    country: 'Allemagne / France',
    minDeposit: 1,
    fees: '1€ par ordre',
    instruments: ['Actions', 'ETF', 'Crypto', 'Obligations'],
    rating: 4.5,
    pros: ['1€ par transaction', 'Plans d\'epargne gratuits', 'Interface simple', 'Fractions d\'actions des 1€'],
    cons: ['Pas de PEA', 'Nombre limite de marches'],
    url: 'https://traderepublic.com/fr-fr',
    amfRegistered: true,
  },
  {
    name: 'DEGIRO',
    logo: '📊',
    regulator: 'AFM (Pays-Bas) + BaFin + Enregistre AMF',
    country: 'Pays-Bas',
    minDeposit: 0,
    fees: '1€ + 1€ frais de gestion pour US',
    instruments: ['Actions', 'ETF', 'Options', 'Futures', 'Obligations'],
    rating: 4.3,
    pros: ['Frais tres bas', 'Large choix de marches', 'ETF gratuits (selection)', 'Ideal petits budgets'],
    cons: ['Pas de PEA', 'Interface peut etre complexe', 'Pas de crypto'],
    url: 'https://www.degiro.fr',
    amfRegistered: true,
  },
  {
    name: 'Bourse Direct',
    logo: '🇫🇷',
    regulator: 'AMF + ACPR (France)',
    country: 'France',
    minDeposit: 0,
    fees: '0,99€ min par ordre Euronext',
    instruments: ['Actions', 'ETF', 'OPCVM', 'Warrants', 'Turbos'],
    rating: 4.0,
    pros: ['PEA disponible', 'Courtier 100% francais', 'Frais competitifs', 'CTO + PEA + PEA-PME'],
    cons: ['Interface datee', 'Pas de crypto', 'Marches US plus chers'],
    url: 'https://www.boursedirect.fr',
    amfRegistered: true,
  },
  {
    name: 'Interactive Brokers',
    logo: '🌐',
    regulator: 'SEC/FINRA + FCA + Enregistre AMF',
    country: 'Etats-Unis',
    minDeposit: 0,
    fees: '0,005$/action (min 1$) ou fixe 1,25€/ordre',
    instruments: ['Actions', 'ETF', 'Options', 'Futures', 'Forex', 'Obligations', 'CFD'],
    rating: 4.7,
    pros: ['Acces mondial (150+ marches)', 'Outils pro', 'Frais tres bas pour gros volumes', 'Ideal pour le day trading'],
    cons: ['Interface complexe', 'Courbe d\'apprentissage', 'Pas de PEA'],
    url: 'https://www.interactivebrokers.eu',
    amfRegistered: true,
  },
  {
    name: 'Boursorama (BoursoBank)',
    logo: '🏛️',
    regulator: 'AMF + ACPR (France)',
    country: 'France',
    minDeposit: 0,
    fees: '1,99€ min par ordre (offre Decouverte)',
    instruments: ['Actions', 'ETF', 'OPCVM', 'Warrants'],
    rating: 3.8,
    pros: ['Banque + courtier', 'PEA disponible', 'Fiabilite bancaire', 'Interface moderne'],
    cons: ['Frais plus eleves', 'Pas de crypto', 'Outils limites pour le day trading'],
    url: 'https://www.boursorama.com',
    amfRegistered: true,
  },
  {
    name: 'Binance',
    logo: '🟡',
    regulator: 'PSAN enregistre AMF (France)',
    country: 'International / France',
    minDeposit: 1,
    fees: '0,1% par trade (spot), reduit avec BNB',
    instruments: ['Crypto (600+)', 'Staking', 'Futures', 'Options Crypto', 'NFT'],
    rating: 4.4,
    pros: ['Plus grande plateforme crypto mondiale', 'Frais tres bas (0,1%)', '600+ cryptomonnaies', 'Staking & DeFi integres'],
    cons: ['Regulation en evolution', 'Complexe pour debutants', 'Pas d\'actions/ETF classiques', 'Support client perfectible'],
    url: 'https://www.binance.com/fr',
    amfRegistered: true,
  },
  {
    name: 'Crypto.com',
    logo: '🔵',
    regulator: 'PSAN enregistre AMF (France)',
    country: 'Singapour / France',
    minDeposit: 1,
    fees: '0,075% maker / 0,075% taker (avec CRO)',
    instruments: ['Crypto (250+)', 'Staking', 'NFT', 'Carte Visa Crypto'],
    rating: 4.2,
    pros: ['Carte Visa crypto (cashback)', 'Interface mobile excellente', '250+ cryptos', 'Staking attractif'],
    cons: ['Frais plus eleves sans CRO', 'Pas d\'actions classiques', 'Spread parfois eleve', 'Reductions liees au token CRO'],
    url: 'https://crypto.com/fr',
    amfRegistered: true,
  },
];

// ─── Simulated Market Data ───────────────────────────────────────────────────

function generateAssets(): Asset[] {
  const assets: Asset[] = [
    { symbol: 'CAC40', name: 'CAC 40', type: 'index', market: 'EU', currentPrice: 7856.32, change: 0, changePercent: 0, volume: '4.2B', entryWindow: { start: '09:15', end: '10:30' }, exitWindow: { start: '16:30', end: '17:25' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees - en attente de donnees reelles' },
    { symbol: 'BNP.PA', name: 'BNP Paribas', type: 'stock', market: 'EU', currentPrice: 62.45, change: 0, changePercent: 0, volume: '3.1M', entryWindow: { start: '09:30', end: '10:00' }, exitWindow: { start: '15:00', end: '16:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'TTE.PA', name: 'TotalEnergies', type: 'stock', market: 'EU', currentPrice: 58.92, change: 0, changePercent: 0, volume: '5.8M', entryWindow: { start: '09:15', end: '10:15' }, exitWindow: { start: '16:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'AIR.PA', name: 'Airbus', type: 'stock', market: 'EU', currentPrice: 156.78, change: 0, changePercent: 0, volume: '2.4M', entryWindow: { start: '09:30', end: '11:00' }, exitWindow: { start: '15:30', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'AAPL', name: 'Apple Inc.', type: 'stock', market: 'US', currentPrice: 178.52, change: 0, changePercent: 0, volume: '52.1M', entryWindow: { start: '15:45', end: '16:30' }, exitWindow: { start: '20:30', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'MSFT', name: 'Microsoft', type: 'stock', market: 'US', currentPrice: 415.80, change: 0, changePercent: 0, volume: '22.3M', entryWindow: { start: '15:45', end: '17:00' }, exitWindow: { start: '20:00', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'AMZN', name: 'Amazon', type: 'stock', market: 'US', currentPrice: 186.40, change: 0, changePercent: 0, volume: '45.6M', entryWindow: { start: '16:00', end: '17:00' }, exitWindow: { start: '20:30', end: '21:45' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'TSLA', name: 'Tesla', type: 'stock', market: 'US', currentPrice: 245.50, change: 0, changePercent: 0, volume: '98.2M', entryWindow: { start: '15:45', end: '16:30' }, exitWindow: { start: '20:30', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'NVDA', name: 'Nvidia', type: 'stock', market: 'US', currentPrice: 890.20, change: 0, changePercent: 0, volume: '65.4M', entryWindow: { start: '15:45', end: '16:30' }, exitWindow: { start: '20:30', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'GOOGL', name: 'Alphabet (Google)', type: 'stock', market: 'US', currentPrice: 175.30, change: 0, changePercent: 0, volume: '28.1M', entryWindow: { start: '15:45', end: '17:00' }, exitWindow: { start: '20:00', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'META', name: 'Meta (Facebook)', type: 'stock', market: 'US', currentPrice: 510.60, change: 0, changePercent: 0, volume: '18.5M', entryWindow: { start: '15:45', end: '17:00' }, exitWindow: { start: '20:00', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'BABA', name: 'Alibaba', type: 'stock', market: 'US', currentPrice: 85.40, change: 0, changePercent: 0, volume: '15.8M', entryWindow: { start: '15:45', end: '17:00' }, exitWindow: { start: '20:00', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'NFLX', name: 'Netflix', type: 'stock', market: 'US', currentPrice: 925.80, change: 0, changePercent: 0, volume: '8.3M', entryWindow: { start: '15:45', end: '17:00' }, exitWindow: { start: '20:00', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'AMD', name: 'AMD', type: 'stock', market: 'US', currentPrice: 168.90, change: 0, changePercent: 0, volume: '42.7M', entryWindow: { start: '15:45', end: '16:30' }, exitWindow: { start: '20:30', end: '21:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'NASDAQ', name: 'NASDAQ Composite', type: 'index', market: 'US', currentPrice: 18250.00, change: 0, changePercent: 0, volume: '5.8B', entryWindow: { start: '15:45', end: '16:30' }, exitWindow: { start: '20:30', end: '21:45' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'SP500', name: 'S&P 500', type: 'index', market: 'US', currentPrice: 5680.00, change: 0, changePercent: 0, volume: '4.2B', entryWindow: { start: '15:45', end: '16:30' }, exitWindow: { start: '20:30', end: '21:45' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'BTC/EUR', name: 'Bitcoin', type: 'crypto', market: 'CRYPTO', currentPrice: 82450, change: 0, changePercent: 0, volume: '28.5B', entryWindow: { start: '08:00', end: '10:00' }, exitWindow: { start: '16:00', end: '20:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'ETH/EUR', name: 'Ethereum', type: 'crypto', market: 'CRYPTO', currentPrice: 3245, change: 0, changePercent: 0, volume: '12.1B', entryWindow: { start: '08:00', end: '11:00' }, exitWindow: { start: '15:00', end: '19:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'EWLD.PA', name: 'Lyxor MSCI World ETF', type: 'etf', market: 'EU', currentPrice: 28.45, change: 0, changePercent: 0, volume: '1.2M', entryWindow: { start: '09:30', end: '10:30' }, exitWindow: { start: '16:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'XAU/USD', name: 'Or (Gold)', type: 'commodity', market: 'COMMODITY', currentPrice: 2345.60, change: 0, changePercent: 0, volume: '185K', entryWindow: { start: '09:00', end: '10:30' }, exitWindow: { start: '15:30', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'XAG/USD', name: 'Argent (Silver)', type: 'commodity', market: 'COMMODITY', currentPrice: 27.85, change: 0, changePercent: 0, volume: '92K', entryWindow: { start: '09:00', end: '11:00' }, exitWindow: { start: '16:00', end: '18:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'CL=F', name: 'Petrole Brut (WTI)', type: 'commodity', market: 'COMMODITY', currentPrice: 78.45, change: 0, changePercent: 0, volume: '320K', entryWindow: { start: '09:30', end: '11:00' }, exitWindow: { start: '15:30', end: '20:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'BZ=F', name: 'Petrole Brent', type: 'commodity', market: 'COMMODITY', currentPrice: 82.30, change: 0, changePercent: 0, volume: '245K', entryWindow: { start: '09:00', end: '10:30' }, exitWindow: { start: '16:00', end: '19:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'NG=F', name: 'Gaz Naturel', type: 'commodity', market: 'COMMODITY', currentPrice: 2.85, change: 0, changePercent: 0, volume: '156K', entryWindow: { start: '10:00', end: '12:00' }, exitWindow: { start: '16:00', end: '19:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'HG=F', name: 'Cuivre', type: 'commodity', market: 'COMMODITY', currentPrice: 4.25, change: 0, changePercent: 0, volume: '78K', entryWindow: { start: '09:00', end: '10:30' }, exitWindow: { start: '15:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'W=F', name: 'Ble (Wheat)', type: 'commodity', market: 'COMMODITY', currentPrice: 5.82, change: 0, changePercent: 0, volume: '45K', entryWindow: { start: '10:30', end: '12:00' }, exitWindow: { start: '17:00', end: '19:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'EUR/USD', name: 'Euro / Dollar US', type: 'forex', market: 'FOREX', currentPrice: 1.0865, change: 0, changePercent: 0, volume: '5.2T', entryWindow: { start: '08:00', end: '10:00' }, exitWindow: { start: '14:30', end: '16:30' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'GBP/USD', name: 'Livre Sterling / Dollar US', type: 'forex', market: 'FOREX', currentPrice: 1.2720, change: 0, changePercent: 0, volume: '2.1T', entryWindow: { start: '09:00', end: '11:00' }, exitWindow: { start: '15:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'USD/JPY', name: 'Dollar US / Yen Japonais', type: 'forex', market: 'FOREX', currentPrice: 151.45, change: 0, changePercent: 0, volume: '3.8T', entryWindow: { start: '02:00', end: '05:00' }, exitWindow: { start: '15:30', end: '18:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'EUR/GBP', name: 'Euro / Livre Sterling', type: 'forex', market: 'FOREX', currentPrice: 0.8545, change: 0, changePercent: 0, volume: '1.2T', entryWindow: { start: '09:00', end: '10:30' }, exitWindow: { start: '14:00', end: '16:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'USD/CHF', name: 'Dollar US / Franc Suisse', type: 'forex', market: 'FOREX', currentPrice: 0.8825, change: 0, changePercent: 0, volume: '890B', entryWindow: { start: '09:00', end: '11:00' }, exitWindow: { start: '15:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'AUD/USD', name: 'Dollar Australien / Dollar US', type: 'forex', market: 'FOREX', currentPrice: 0.6580, change: 0, changePercent: 0, volume: '780B', entryWindow: { start: '02:00', end: '04:00' }, exitWindow: { start: '09:00', end: '11:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'USD/CAD', name: 'Dollar US / Dollar Canadien', type: 'forex', market: 'FOREX', currentPrice: 1.3580, change: 0, changePercent: 0, volume: '650B', entryWindow: { start: '14:30', end: '16:00' }, exitWindow: { start: '19:00', end: '21:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'NZD/USD', name: 'Dollar Neo-Zelandais / Dollar US', type: 'forex', market: 'FOREX', currentPrice: 0.6125, change: 0, changePercent: 0, volume: '420B', entryWindow: { start: '02:00', end: '04:00' }, exitWindow: { start: '09:00', end: '11:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'EUR/JPY', name: 'Euro / Yen Japonais', type: 'forex', market: 'FOREX', currentPrice: 164.20, change: 0, changePercent: 0, volume: '1.5T', entryWindow: { start: '08:00', end: '10:00' }, exitWindow: { start: '15:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
    { symbol: 'GBP/JPY', name: 'Livre Sterling / Yen Japonais', type: 'forex', market: 'FOREX', currentPrice: 192.50, change: 0, changePercent: 0, volume: '980B', entryWindow: { start: '09:00', end: '11:00' }, exitWindow: { start: '15:00', end: '17:00' }, signal: 'hold', confidence: 50, reason: 'Donnees simulees' },
  ];
  return assets;
}

// ─── Fetch real market data ──────────────────────────────────────────────────

async function fetchRealMarketData(currentAssets: Asset[]): Promise<Asset[]> {
  try {
    const resp = await fetch(`${SUPABASE_URL}/functions/v1/get-market-data`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const json = await resp.json();
    if (!json.success || !json.quotes) throw new Error('Invalid response');

    const quotes = json.quotes as Record<string, {
      price: number; change: number; changePercent: number; volume: string;
      candles?: Candle[];
    }>;

    const baseAssets = currentAssets.length > 0 ? currentAssets : generateAssets();
    return baseAssets.map(asset => {
      const q = quotes[asset.symbol];
      if (!q) return asset;

      const candles = q.candles || [];
      let indicators: TechnicalIndicators | undefined;
      let signalResult: ReturnType<typeof generateSignalFromIndicators> | undefined;

      if (candles.length >= 30) {
        indicators = computeAllIndicators(candles);
        signalResult = generateSignalFromIndicators(q.price, indicators, q.changePercent);
      }

      return {
        ...asset,
        currentPrice: q.price,
        change: q.change,
        changePercent: q.changePercent,
        volume: q.volume || asset.volume,
        candles,
        indicators,
        signal: signalResult?.signal || asset.signal,
        confidence: signalResult?.confidence || asset.confidence,
        reason: signalResult?.reason || asset.reason,
        suggestedEntry: signalResult?.suggestedEntry,
        suggestedStopLoss: signalResult?.suggestedSL,
        suggestedTakeProfit: signalResult?.suggestedTP,
      };
    });
  } catch (err) {
    console.warn('[MarketData] Fallback to simulated data:', err);
    return generateAssets();
  }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function SignalBadge({ signal }: { signal: 'buy' | 'sell' | 'hold' }) {
  const config = {
    buy: { bg: 'bg-green-100', text: 'text-green-800', label: 'ACHAT', icon: ArrowUpRight },
    sell: { bg: 'bg-red-100', text: 'text-red-800', label: 'VENTE', icon: ArrowDownRight },
    hold: { bg: 'bg-yellow-100', text: 'text-yellow-800', label: 'ATTENTE', icon: Minus },
  };
  const c = config[signal];
  const Icon = c.icon;
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-bold ${c.bg} ${c.text}`}>
      <Icon className="w-3 h-3" />
      {c.label}
    </span>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const color = value >= 70 ? 'bg-green-500' : value >= 50 ? 'bg-yellow-500' : 'bg-red-500';
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${value}%` }} />
      </div>
      <span className="text-xs font-medium text-gray-600">{value}%</span>
    </div>
  );
}

function MarketStatusBadge({ market }: { market: string }) {
  const now = new Date();
  const h = now.getHours();
  const m = now.getMinutes();
  const currentMinutes = h * 60 + m;
  const info = MARKET_HOURS[market];
  const [openH, openM] = info.open.split(':').map(Number);
  const [closeH, closeM] = info.close.split(':').map(Number);
  const openMinutes = openH * 60 + openM;
  const closeMinutes = closeH * 60 + closeM;

  const isWeekend = now.getDay() === 0 || now.getDay() === 6;
  const isOpen = market === 'CRYPTO'
    ? true
    : market === 'FOREX'
    ? !isWeekend
    : !isWeekend && currentMinutes >= openMinutes && currentMinutes <= closeMinutes;

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${
      isOpen ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'
    }`}>
      <span className={`w-2 h-2 rounded-full ${isOpen ? 'bg-green-500 animate-pulse' : 'bg-gray-400'}`} />
      {isOpen ? 'Ouvert' : 'Ferme'}
    </span>
  );
}

function formatCurrency(value: number, currency: string = '€'): string {
  if (currency === '$') {
    return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value);
  }
  return new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'EUR' }).format(value);
}

function formatTime(dateStr: string): string {
  return new Date(dateStr).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
}

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('fr-FR', { day: '2-digit', month: '2-digit' });
}

function formatDuration(startISO: string, endISO?: string): string {
  const start = new Date(startISO).getTime();
  const end = endISO ? new Date(endISO).getTime() : Date.now();
  const diffMs = Math.max(0, end - start);
  const totalMin = Math.floor(diffMs / 60000);
  if (totalMin < 60) return `${totalMin}min`;
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h < 24) return `${h}h ${m > 0 ? m + 'min' : ''}`.trim();
  const d = Math.floor(h / 24);
  return `${d}j ${h % 24}h`;
}

function formatDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString('fr-FR', {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

// Returns timing status relative to entry/exit windows
function getTimingStatus(asset: Asset): {
  phase: 'before_entry' | 'in_entry' | 'between' | 'in_exit' | 'after_exit';
  label: string;
  canEnter: boolean;
  lateEntryRisk: string | null;
  minutesToClose: number;
} {
  const now = new Date();
  const h = now.getHours();
  const m = now.getMinutes();
  const currentMin = h * 60 + m;

  const [entryStartH, entryStartM] = asset.entryWindow.start.split(':').map(Number);
  const [entryEndH, entryEndM] = asset.entryWindow.end.split(':').map(Number);
  const [exitStartH, exitStartM] = asset.exitWindow.start.split(':').map(Number);
  const [exitEndH, exitEndM] = asset.exitWindow.end.split(':').map(Number);

  const entryStart = entryStartH * 60 + entryStartM;
  const entryEnd = entryEndH * 60 + entryEndM;
  const exitStart = exitStartH * 60 + exitStartM;
  const exitEnd = exitEndH * 60 + exitEndM;

  const marketClose = MARKET_HOURS[asset.market]
    ? parseInt(MARKET_HOURS[asset.market].close.split(':')[0]) * 60 + parseInt(MARKET_HOURS[asset.market].close.split(':')[1])
    : exitEnd;
  const minutesToClose = marketClose - currentMin;

  if (currentMin < entryStart) {
    return { phase: 'before_entry', label: 'Marche pas encore ouvert', canEnter: false, lateEntryRisk: null, minutesToClose };
  }
  if (currentMin >= entryStart && currentMin <= entryEnd) {
    return { phase: 'in_entry', label: 'Dans la fenetre d\'entree', canEnter: true, lateEntryRisk: null, minutesToClose };
  }
  if (currentMin > entryEnd && currentMin < exitStart) {
    const hoursLeft = Math.floor(minutesToClose / 60);
    const minsLeft = minutesToClose % 60;
    const timeLeft = hoursLeft > 0 ? `${hoursLeft}h${minsLeft.toString().padStart(2, '0')}` : `${minsLeft}min`;
    // Calculate reduced profit potential (less time = less potential)
    const totalTradingWindow = exitEnd - entryStart;
    const remainingWindow = exitEnd - currentMin;
    const timeRatio = Math.max(0, remainingWindow / totalTradingWindow);
    const reducedGainPct = timeRatio * 100;
    return {
      phase: 'between',
      label: `Entree tardive - ${timeLeft} avant fermeture`,
      canEnter: true,
      lateEntryRisk: reducedGainPct < 30
        ? `Risque eleve : seulement ${timeLeft} avant cloture. Gain potentiel tres reduit.`
        : `Entree tardive : ${timeLeft} restants. Potentiel reduit a ~${reducedGainPct.toFixed(0)}% du signal initial.`,
      minutesToClose,
    };
  }
  if (currentMin >= exitStart && currentMin <= exitEnd) {
    return { phase: 'in_exit', label: 'Zone de sortie - cloturer maintenant', canEnter: false, lateEntryRisk: 'Ne pas entrer, zone de cloture atteinte.', minutesToClose };
  }
  return { phase: 'after_exit', label: 'Marche ferme / session terminee', canEnter: false, lateEntryRisk: null, minutesToClose };
}

function IndicatorBadge({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <div className={`px-2 py-1 rounded text-xs font-medium ${color}`}>
      <span className="opacity-70">{label}: </span>
      <span className="font-bold">{value}</span>
    </div>
  );
}

function IndicatorsPanel({ indicators, price, currency }: { indicators: TechnicalIndicators; price: number; currency: string }) {
  const fmt = (v: number) => {
    if (Math.abs(v) >= 100) return v.toFixed(2);
    if (Math.abs(v) >= 1) return v.toFixed(4);
    return v.toFixed(6);
  };

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 mt-3 pt-3 border-t border-gray-100">
      {indicators.rsi14 !== null && (
        <IndicatorBadge
          label="RSI(14)"
          value={indicators.rsi14.toFixed(1)}
          color={indicators.rsi14 < 30 ? 'bg-green-50 text-green-700' : indicators.rsi14 > 70 ? 'bg-red-50 text-red-700' : 'bg-gray-50 text-gray-700'}
        />
      )}
      {indicators.macd && (
        <IndicatorBadge
          label="MACD"
          value={`${indicators.macd.histogram > 0 ? '+' : ''}${indicators.macd.histogram.toFixed(4)}`}
          color={indicators.macd.histogram > 0 ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}
        />
      )}
      {indicators.bollingerBands && (
        <IndicatorBadge
          label="BB Width"
          value={`${indicators.bollingerBands.width}%`}
          color={indicators.bollingerBands.width < 3 ? 'bg-purple-50 text-purple-700' : 'bg-gray-50 text-gray-700'}
        />
      )}
      {indicators.sma20 !== null && (
        <IndicatorBadge
          label="SMA20"
          value={fmt(indicators.sma20)}
          color={price > indicators.sma20 ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}
        />
      )}
      {indicators.sma50 !== null && (
        <IndicatorBadge
          label="SMA50"
          value={fmt(indicators.sma50)}
          color={price > indicators.sma50 ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}
        />
      )}
      {indicators.stochastic && (
        <IndicatorBadge
          label="Stoch"
          value={`K:${indicators.stochastic.k} D:${indicators.stochastic.d}`}
          color={indicators.stochastic.k < 20 ? 'bg-green-50 text-green-700' : indicators.stochastic.k > 80 ? 'bg-red-50 text-red-700' : 'bg-gray-50 text-gray-700'}
        />
      )}
      {indicators.adx !== null && (
        <IndicatorBadge
          label="ADX"
          value={indicators.adx.toFixed(1)}
          color={indicators.adx > 25 ? 'bg-blue-50 text-blue-700' : 'bg-gray-50 text-gray-700'}
        />
      )}
      {indicators.atr14 !== null && (
        <IndicatorBadge
          label="ATR(14)"
          value={fmt(indicators.atr14)}
          color="bg-gray-50 text-gray-700"
        />
      )}
      {indicators.volumeRatio !== null && (
        <IndicatorBadge
          label="Vol Ratio"
          value={`${indicators.volumeRatio}x`}
          color={indicators.volumeRatio > 1.5 ? 'bg-blue-50 text-blue-700' : indicators.volumeRatio < 0.5 ? 'bg-yellow-50 text-yellow-700' : 'bg-gray-50 text-gray-700'}
        />
      )}
      {indicators.fibonacci && (
        <>
          <IndicatorBadge label="Fib S1" value={fmt(indicators.fibonacci.s1)} color="bg-green-50 text-green-700" />
          <IndicatorBadge label="Fib R1" value={fmt(indicators.fibonacci.r1)} color="bg-red-50 text-red-700" />
          <IndicatorBadge label="Pivot" value={fmt(indicators.fibonacci.pivot)} color="bg-blue-50 text-blue-700" />
        </>
      )}
    </div>
  );
}

// ─── Main App ────────────────────────────────────────────────────────────────

export default function App() {
  const [tab, setTab] = useState<'dashboard' | 'overview' | 'positions' | 'history' | 'platforms' | 'journal' | 'bot'>('dashboard');
  const backend = useBackend();
  const [assets, setAssets] = useState<Asset[]>(generateAssets);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [dataSource, setDataSource] = useState<'live' | 'simulated'>('simulated');
  const [positions, setPositions] = useState<Position[]>(() => {
    try {
      const saved = localStorage.getItem('daytrading_positions');
      return saved ? JSON.parse(saved) : [];
    } catch { return []; }
  });
  const [history, setHistory] = useState<DailyPerformance[]>(() => {
    try {
      const saved = localStorage.getItem('daytrading_history');
      return saved ? JSON.parse(saved) : [];
    } catch { return []; }
  });
  const [journalEntries, setJournalEntries] = useState<{ date: string; text: string }[]>(() => {
    try {
      const saved = localStorage.getItem('daytrading_journal');
      return saved ? JSON.parse(saved) : [];
    } catch { return []; }
  });
  const [showNewPosition, setShowNewPosition] = useState(false);
  const [selectedAsset, setSelectedAsset] = useState<Asset | null>(null);
  const [newAmount, setNewAmount] = useState('');
  const [newStopLoss, setNewStopLoss] = useState('');
  const [newTakeProfit, setNewTakeProfit] = useState('');
  const [journalText, setJournalText] = useState('');
  const [filterMarket, setFilterMarket] = useState<string>('all');
  const [filterSignal, setFilterSignal] = useState<string>('all');
  const [currentTime, setCurrentTime] = useState(new Date());
  const [expandedAsset, setExpandedAsset] = useState<string | null>(null);

  // Persist
  useEffect(() => { localStorage.setItem('daytrading_positions', JSON.stringify(positions)); }, [positions]);
  useEffect(() => { localStorage.setItem('daytrading_history', JSON.stringify(history)); }, [history]);
  useEffect(() => { localStorage.setItem('daytrading_journal', JSON.stringify(journalEntries)); }, [journalEntries]);

  // Clock
  useEffect(() => {
    const timer = setInterval(() => setCurrentTime(new Date()), 1000);
    return () => clearInterval(timer);
  }, []);

  // Fetch real market data on mount and every 20 minutes
  const refreshMarketData = useCallback(async () => {
    setIsRefreshing(true);
    try {
      const newAssets = await fetchRealMarketData(assets);
      setAssets(newAssets);
      setLastUpdate(new Date());
      setDataSource('live');
      setPositions(prev => prev.map(p => {
        if (p.status !== 'open') return p;
        const asset = newAssets.find(a => a.symbol === p.symbol);
        if (!asset) return p;
        const currentPrice = asset.currentPrice;
        const pnl = p.type === 'long'
          ? (currentPrice - p.entryPrice) * p.quantity
          : (p.entryPrice - currentPrice) * p.quantity;
        const pnlPercent = (pnl / (p.entryPrice * p.quantity)) * 100;
        return { ...p, currentPrice, pnl: parseFloat(pnl.toFixed(2)), pnlPercent: parseFloat(pnlPercent.toFixed(2)) };
      }));
    } catch (err) {
      console.error('Market data refresh failed:', err);
    } finally {
      setIsRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refreshMarketData();
    const timer = setInterval(refreshMarketData, REFRESH_INTERVAL);
    return () => clearInterval(timer);
  }, [refreshMarketData]);

  // Generate 2-month history if empty
  useEffect(() => {
    if (history.length === 0) {
      const data: DailyPerformance[] = [];
      const now = new Date();
      for (let i = 59; i >= 0; i--) {
        const date = new Date(now);
        date.setDate(date.getDate() - i);
        if (date.getDay() === 0 || date.getDay() === 6) continue;
        data.push({
          date: date.toISOString().split('T')[0],
          pnl: parseFloat(((Math.random() - 0.4) * 50).toFixed(2)),
          trades: Math.floor(Math.random() * 5) + 1,
          winRate: parseFloat((Math.random() * 40 + 40).toFixed(1)),
          totalInvested: parseFloat((Math.random() * 200 + 50).toFixed(2)),
        });
      }
      setHistory(data);
    }
  }, []);

  const filteredAssets = useMemo(() => {
    return assets.filter(a => {
      if (filterMarket !== 'all' && a.market !== filterMarket) return false;
      if (filterSignal !== 'all' && a.signal !== filterSignal) return false;
      return true;
    });
  }, [assets, filterMarket, filterSignal]);

  const openPositions = positions.filter(p => p.status === 'open');
  const closedPositions = positions.filter(p => p.status === 'closed');
  const totalPnL = positions.reduce((sum, p) => sum + p.pnl, 0);
  const totalOpenPnL = openPositions.reduce((sum, p) => sum + p.pnl, 0);
  const totalInvested = openPositions.reduce((sum, p) => sum + p.entryPrice * p.quantity, 0);
  const winRate = closedPositions.length > 0
    ? (closedPositions.filter(p => p.pnl > 0).length / closedPositions.length * 100)
    : 0;
  const totalRealizedPnL = closedPositions.reduce((sum, p) => sum + p.pnl, 0);
  const bestTrade = closedPositions.length > 0 ? Math.max(...closedPositions.map(p => p.pnl)) : 0;
  const worstTrade = closedPositions.length > 0 ? Math.min(...closedPositions.map(p => p.pnl)) : 0;
  const avgWin = closedPositions.filter(p => p.pnl > 0).length > 0
    ? closedPositions.filter(p => p.pnl > 0).reduce((s, p) => s + p.pnl, 0) / closedPositions.filter(p => p.pnl > 0).length : 0;
  const avgLoss = closedPositions.filter(p => p.pnl < 0).length > 0
    ? closedPositions.filter(p => p.pnl < 0).reduce((s, p) => s + p.pnl, 0) / closedPositions.filter(p => p.pnl < 0).length : 0;

  const cumulativePnL = useMemo(() => {
    let total = 0;
    return history.map(d => {
      total += d.pnl;
      return { ...d, cumulative: parseFloat(total.toFixed(2)) };
    });
  }, [history]);

  const totalHistoryPnL = cumulativePnL.length > 0 ? cumulativePnL[cumulativePnL.length - 1].cumulative : 0;
  const maxDrawdown = useMemo(() => {
    let peak = 0;
    let maxDD = 0;
    for (const d of cumulativePnL) {
      if (d.cumulative > peak) peak = d.cumulative;
      const dd = peak - d.cumulative;
      if (dd > maxDD) maxDD = dd;
    }
    return maxDD;
  }, [cumulativePnL]);

  const openPosition = useCallback((asset: Asset) => {
    const qty = parseFloat(newAmount) / asset.currentPrice;
    if (!qty || qty <= 0) return;
    const newPos: Position = {
      id: Date.now().toString(),
      symbol: asset.symbol,
      name: asset.name,
      type: 'long',
      entryPrice: asset.currentPrice,
      currentPrice: asset.currentPrice,
      quantity: parseFloat(qty.toFixed(6)),
      entryTime: new Date().toISOString(),
      status: 'open',
      pnl: 0,
      pnlPercent: 0,
      stopLoss: newStopLoss ? parseFloat(newStopLoss) : asset.suggestedStopLoss,
      takeProfit: newTakeProfit ? parseFloat(newTakeProfit) : asset.suggestedTakeProfit,
    };
    setPositions(prev => [...prev, newPos]);
    setShowNewPosition(false);
    setSelectedAsset(null);
    setNewAmount('');
    setNewStopLoss('');
    setNewTakeProfit('');
  }, [newAmount, newStopLoss, newTakeProfit]);

  const closePosition = useCallback((posId: string) => {
    setPositions(prev => prev.map(p => {
      if (p.id !== posId || p.status !== 'open') return p;
      return { ...p, status: 'closed' as const, exitTime: new Date().toISOString(), exitPrice: p.currentPrice };
    }));
  }, []);

  const deletePosition = useCallback((posId: string) => {
    setPositions(prev => prev.filter(p => p.id !== posId));
  }, []);

  const addJournalEntry = useCallback(() => {
    if (!journalText.trim()) return;
    setJournalEntries(prev => [{ date: new Date().toISOString(), text: journalText.trim() }, ...prev]);
    setJournalText('');
  }, [journalText]);

  // Count assets with indicators
  const assetsWithIndicators = assets.filter(a => a.indicators).length;
  const buySignals = assets.filter(a => a.signal === 'buy').length;
  const sellSignals = assets.filter(a => a.signal === 'sell').length;

  const tabConfig = [
    { id: 'dashboard', label: 'Tableau de Bord', icon: Home, tooltip: 'Vue d\'ensemble : capital, P&L, positions et graphe d\'evolution' },
    { id: 'bot', label: 'Controle Bot', icon: Cpu, tooltip: 'Demarrer/arreter le bot, configurer les parametres de trading' },
    { id: 'positions', label: 'Mes Positions', icon: BarChart3, tooltip: 'Positions ouvertes et leur P&L en temps reel' },
  ] as const;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Top Navigation Bar */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-30">
        <div className="max-w-7xl mx-auto px-4 lg:px-8">
          <div className="flex items-center justify-between h-16">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-blue-600 rounded-xl flex items-center justify-center">
                <LineChart className="w-6 h-6 text-white" />
              </div>
              <div>
                <h1 className="text-lg font-bold text-gray-900">Day Trading Live</h1>
                <p className="text-xs text-gray-500">Trading Automatique Fusion Markets MT5</p>
              </div>
            </div>
            <div className="flex items-center gap-4">
              {/* Backend Bot Status Indicator */}
              {backend.backendAvailable && (
                <button
                  onClick={() => setTab('dashboard')}
                  title={backend.botStatus?.running
                    ? 'Bot actif — cliquez pour voir le tableau de bord'
                    : 'Bot arrete — cliquez pour voir le tableau de bord et demarrer'}
                  className={`hidden md:flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-bold transition cursor-pointer ${
                    backend.botStatus?.running
                      ? 'bg-green-100 text-green-800 hover:bg-green-200'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  <span className={`w-2 h-2 rounded-full ${
                    backend.botStatus?.running ? 'bg-green-500 animate-pulse' : 'bg-gray-400'
                  }`} />
                  <Cpu className="w-3 h-3" />
                  Bot {backend.botStatus?.running ? 'Actif' : 'Arrete'}
                  {backend.botStatus?.daily_pnl !== undefined && backend.botStatus.daily_pnl !== 0 && (
                    <span className={backend.botStatus.daily_pnl > 0 ? 'text-green-600' : 'text-red-600'}>
                      {backend.botStatus.daily_pnl > 0 ? '+' : ''}{backend.botStatus.daily_pnl.toFixed(2)}EUR
                    </span>
                  )}
                </button>
              )}
              {backend.backendAvailable && (
                <div className="hidden md:flex items-center gap-3 text-xs">
                  <span className={`px-2 py-1 rounded-full font-bold flex items-center gap-1 ${
                    backend.botStatus?.mt5_connected ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
                  }`}>
                    <Activity className="w-3 h-3" /> Fusion MT5 {backend.botStatus?.mt5_connected ? 'Connecte' : 'Deconnecte'}
                  </span>
                  <span className="px-2 py-1 bg-gray-100 text-gray-600 rounded-full font-bold">
                    {backend.botStatus?.open_positions || 0} positions
                  </span>
                  {(backend.botStatus?.daily_pnl ?? 0) !== 0 && (
                    <span className={`px-2 py-1 rounded-full font-bold ${
                      (backend.botStatus?.daily_pnl ?? 0) >= 0 ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                    }`}>
                      P&L: {(backend.botStatus?.daily_pnl ?? 0) >= 0 ? '+' : ''}{(backend.botStatus?.daily_pnl ?? 0).toFixed(2)}€
                    </span>
                  )}
                </div>
              )}
              <div className="text-right hidden sm:block">
                <div className="text-xl font-mono font-bold text-gray-900">
                  {currentTime.toLocaleTimeString('fr-FR')}
                </div>
                <div className="text-xs text-gray-500">
                  {currentTime.toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' })}
                </div>
              </div>
            </div>
          </div>
        </div>
      </header>

      <div className="max-w-7xl mx-auto px-4 lg:px-8 py-6">
        {/* KPI Cards (real backend data) — hidden on dashboard, positions, and bot tabs */}
        {tab !== 'dashboard' && tab !== 'positions' && tab !== 'bot' && (() => {
          const bkCapital = backend.botStatus?.capital || backend.account?.capital || backend.account?.balance || 0;
          const bkDailyPnl = backend.botStatus?.daily_pnl || backend.dailySummary?.pnl || 0;
          const bkOpenCount = backend.botStatus?.open_positions || 0;
          const bkWins = backend.dailySummary?.wins || 0;
          const bkLosses = backend.dailySummary?.losses || 0;
          const bkWinRate = backend.dailySummary?.win_rate || 0;
          const bkTrades = backend.dailySummary?.trades || backend.botStatus?.trades_today || 0;
          return (
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-4 mb-6">
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500 mb-1">Capital Total</div>
              <div className="text-xl font-bold text-gray-900">{bkCapital > 0 ? formatCurrency(bkCapital) : '--'}</div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500 mb-1">P&L du Jour</div>
              <div className={`text-xl font-bold ${bkDailyPnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                {bkDailyPnl >= 0 ? '+' : ''}{formatCurrency(bkDailyPnl)}
              </div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500 mb-1">Trades Aujourd'hui</div>
              <div className="text-xl font-bold text-gray-900">{bkTrades}</div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500 mb-1">Positions Ouvertes</div>
              <div className="text-xl font-bold text-blue-600">{bkOpenCount}</div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-4">
              <div className="text-xs text-gray-500 mb-1">Win Rate</div>
              <div className={`text-xl font-bold ${bkWinRate >= 50 ? 'text-green-600' : bkWinRate > 0 ? 'text-orange-500' : 'text-gray-400'}`}>
                {bkWinRate > 0 ? bkWinRate.toFixed(1) + '%' : '-'}
              </div>
              <div className="text-[10px] text-gray-400">{bkWins}W / {bkLosses}L</div>
            </div>
          </div>
          );
        })()}

        {/* Tabs */}
        <div className="flex flex-wrap gap-2 mb-6 border-b pb-2">
          {tabConfig.map(t => {
            const Icon = t.icon;
            return (
              <button
                key={t.id}
                onClick={() => setTab(t.id as any)}
                title={t.tooltip}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition ${
                  tab === t.id ? 'bg-blue-600 text-white shadow-sm' : 'text-gray-600 hover:bg-gray-100'
                }`}
              >
                <Icon className="w-4 h-4" />
                {t.label}
              </button>
            );
          })}
        </div>

        {/* ═══════════ TAB: Dashboard ═══════════ */}
        {tab === 'dashboard' && (
          <Dashboard backend={backend} />
        )}

        {/* ═══════════ TAB: Bot Control Panel ═══════════ */}
        {tab === 'bot' && (
          <ControlPanel backend={backend} />
        )}

        {/* ═══════════ TAB: Market Overview ═══════════ */}
        {tab === 'overview' && (
          <div>
            <div className="flex flex-wrap gap-3 mb-4">
              <div className="flex items-center gap-2">
                <Filter className="w-4 h-4 text-gray-500" />
                <select value={filterMarket} onChange={e => setFilterMarket(e.target.value)}
                  className="text-sm border border-gray-300 rounded-lg px-3 py-1.5">
                  <option value="all">Tous les marches</option>
                  <option value="EU">Euronext</option>
                  <option value="US">US (NYSE/NASDAQ)</option>
                  <option value="CRYPTO">Crypto</option>
                  <option value="FOREX">Forex</option>
                  <option value="COMMODITY">Matieres Premieres</option>
                </select>
              </div>
              <select value={filterSignal} onChange={e => setFilterSignal(e.target.value)}
                className="text-sm border border-gray-300 rounded-lg px-3 py-1.5">
                <option value="all">Tous les signaux</option>
                <option value="buy">Achat uniquement</option>
                <option value="sell">Vente uniquement</option>
                <option value="hold">Attente uniquement</option>
              </select>
              <button onClick={refreshMarketData} disabled={isRefreshing}
                className="flex items-center gap-1 px-3 py-1.5 text-sm text-blue-600 hover:bg-blue-50 rounded-lg disabled:opacity-50">
                <RefreshCw className={`w-4 h-4 ${isRefreshing ? 'animate-spin' : ''}`} /> {isRefreshing ? 'Chargement...' : 'Actualiser'}
              </button>
            </div>

            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 mb-4 flex items-start gap-3">
              <Info className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
              <div className="text-sm text-blue-800">
                <div className="flex items-center gap-2 mb-1">
                  <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-bold ${dataSource === 'live' ? 'bg-green-100 text-green-800' : 'bg-yellow-100 text-yellow-800'}`}>
                    <span className={`w-2 h-2 rounded-full ${dataSource === 'live' ? 'bg-green-500 animate-pulse' : 'bg-yellow-500'}`} />
                    {dataSource === 'live' ? 'DONNEES TEMPS REEL + ANALYSE TECHNIQUE' : 'DONNEES SIMULEES'}
                  </span>
                  {lastUpdate && (
                    <span className="text-xs text-gray-500">
                      Mis a jour : {lastUpdate.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' })}
                      {' -- '}Prochain rafraichissement dans 20 min
                    </span>
                  )}
                </div>
                <strong>Signaux calcules par analyse multi-indicateurs :</strong> RSI, MACD, Bollinger, Moyennes Mobiles (SMA20/50), Stochastique, ADX, ATR, Fibonacci, Volumes.
                <strong> Signal ACHAT uniquement si 5+ indicateurs concordent.</strong>
                {backend.backendAvailable
                  ? ' Trading automatise via Fusion Markets MT5 -- Bot connecte au serveur.'
                  : ' Bot en attente — deposez des fonds sur Fusion Markets pour demarrer.'
                }
              </div>
            </div>

            {/* Legend */}
            <div className="bg-white border rounded-lg p-3 mb-4 flex flex-wrap gap-4 text-xs">
              <div className="flex items-center gap-2">
                <Zap className="w-3.5 h-3.5 text-blue-600" />
                <span className="text-gray-600">Cliquez sur un actif pour voir les indicateurs techniques detailles</span>
              </div>
              <div className="flex items-center gap-1">
                <span className="w-2 h-2 rounded-full bg-green-500" />
                <span className="text-gray-600">Haussier</span>
              </div>
              <div className="flex items-center gap-1">
                <span className="w-2 h-2 rounded-full bg-red-500" />
                <span className="text-gray-600">Baissier</span>
              </div>
              <div className="flex items-center gap-1">
                <span className="w-2 h-2 rounded-full bg-yellow-500" />
                <span className="text-gray-600">Neutre</span>
              </div>
            </div>

            <div className="grid gap-4">
              {filteredAssets.map(asset => {
                const isExpanded = expandedAsset === asset.symbol;
                const cur = asset.market === 'US' || asset.market === 'COMMODITY' ? '$' : '€';

                return (
                  <div key={asset.symbol}
                    className={`bg-white rounded-xl shadow-sm border hover:shadow-md transition cursor-pointer ${
                      asset.signal === 'buy' ? 'border-l-4 border-l-green-500' :
                      asset.signal === 'sell' ? 'border-l-4 border-l-red-500' : ''
                    }`}
                    onClick={() => setExpandedAsset(isExpanded ? null : asset.symbol)}
                  >
                    <div className="p-4">
                      <div className="flex flex-col lg:flex-row lg:items-center gap-4">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-3 mb-2">
                            <span className="font-bold text-gray-900 text-lg">{asset.symbol}</span>
                            <span className="text-gray-500 text-sm">{asset.name}</span>
                            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                              asset.type === 'stock' ? 'bg-blue-100 text-blue-700' :
                              asset.type === 'etf' ? 'bg-purple-100 text-purple-700' :
                              asset.type === 'crypto' ? 'bg-orange-100 text-orange-700' :
                              asset.type === 'index' ? 'bg-gray-100 text-gray-700' :
                              asset.type === 'commodity' ? 'bg-amber-100 text-amber-700' :
                              asset.type === 'forex' ? 'bg-teal-100 text-teal-700' :
                              'bg-green-100 text-green-700'
                            }`}>
                              {asset.type.toUpperCase()}
                            </span>
                            <MarketStatusBadge market={asset.market} />
                            {asset.indicators && (
                              <span className="text-xs px-2 py-0.5 rounded-full bg-indigo-100 text-indigo-700 font-medium flex items-center gap-1">
                                <Activity className="w-3 h-3" /> TA
                              </span>
                            )}
                          </div>
                          <div className="flex items-center gap-4">
                            <span className="text-2xl font-bold text-gray-900">
                              {formatCurrency(asset.currentPrice, cur)}
                            </span>
                            <span className={`flex items-center gap-1 text-sm font-medium ${asset.change >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                              {asset.change >= 0 ? <TrendingUp className="w-4 h-4" /> : <TrendingDown className="w-4 h-4" />}
                              {asset.change >= 0 ? '+' : ''}{asset.change.toFixed(2)} ({asset.changePercent >= 0 ? '+' : ''}{asset.changePercent}%)
                            </span>
                            <span className="text-xs text-gray-500">Vol: {asset.volume}</span>
                          </div>
                        </div>

                        <div className="flex flex-col sm:flex-row gap-3 lg:w-auto flex-wrap">
                          {/* Entry window */}
                          {asset.signal === 'buy' && (
                            <div className="bg-green-50 rounded-lg p-3 min-w-[130px]">
                              <div className="text-xs text-green-700 font-medium mb-1 flex items-center gap-1">
                                <Play className="w-3 h-3" /> Zone d'entree
                              </div>
                              <div className="text-lg font-bold text-green-800">
                                {asset.entryWindow.start} - {asset.entryWindow.end}
                              </div>
                              <div className="text-[10px] text-gray-500 mt-1">
                                Entrer pendant cette fenetre
                              </div>
                            </div>
                          )}

                          <div className="bg-gray-50 rounded-lg p-3 min-w-[140px]">
                            <div className="text-xs text-gray-500 mb-1">Signal</div>
                            <SignalBadge signal={asset.signal} />
                            <div className="mt-2">
                              <div className="text-xs text-gray-500 mb-1">Confiance</div>
                              <ConfidenceBar value={asset.confidence} />
                            </div>
                          </div>

                          {/* Show SL/TP + exit zone when signal is buy */}
                          {asset.signal === 'buy' && asset.suggestedStopLoss && asset.suggestedTakeProfit && (
                            <div className="bg-green-50 rounded-lg p-3 min-w-[160px]">
                              <div className="text-xs text-green-700 font-medium mb-1 flex items-center gap-1">
                                <Target className="w-3 h-3" /> Niveaux Day Trade
                              </div>
                              <div className="space-y-1">
                                <div className="flex justify-between text-xs">
                                  <span className="text-gray-600">Entree</span>
                                  <span className="font-bold text-green-700">{formatCurrency(asset.suggestedEntry || asset.currentPrice, cur)}</span>
                                </div>
                                <div className="flex justify-between text-xs">
                                  <span className="text-red-600">Stop Loss</span>
                                  <span className="font-bold text-red-700">{formatCurrency(asset.suggestedStopLoss, cur)}</span>
                                </div>
                                <div className="flex justify-between text-xs">
                                  <span className="text-green-600">Take Profit</span>
                                  <span className="font-bold text-green-700">{formatCurrency(asset.suggestedTakeProfit, cur)}</span>
                                </div>
                                <div className="flex justify-between text-xs pt-1 border-t border-green-200">
                                  <span className="text-gray-600">Gain potentiel</span>
                                  <span className="font-bold text-green-700">
                                    +{(((asset.suggestedTakeProfit - asset.currentPrice) / asset.currentPrice) * 100).toFixed(1)}%
                                  </span>
                                </div>
                              </div>
                            </div>
                          )}

                          {/* Exit zone - always shown for buy/sell signals */}
                          {asset.signal !== 'hold' && (
                            <div className="bg-red-50 rounded-lg p-3 min-w-[140px]">
                              <div className="text-xs text-red-700 font-medium mb-1 flex items-center gap-1">
                                <Square className="w-3 h-3" /> Zone de sortie
                              </div>
                              <div className="text-lg font-bold text-red-800">
                                {asset.exitWindow.start} - {asset.exitWindow.end}
                              </div>
                              <div className="text-xs text-red-600 mt-1">
                                Cloturer avant {MARKET_HOURS[asset.market]?.close || '17:30'}
                              </div>
                              <div className="text-[10px] text-gray-500 mt-1">
                                {asset.signal === 'buy' ? 'Sortir au TP ou avant fermeture' : 'Racheter avant fermeture'}
                              </div>
                            </div>
                          )}

                          {asset.signal === 'hold' && (
                            <div className="bg-yellow-50 rounded-lg p-3 min-w-[130px]">
                              <div className="text-xs text-yellow-700 font-medium mb-1 flex items-center gap-1">
                                <AlertTriangle className="w-3 h-3" /> Recommandation
                              </div>
                              <div className="text-sm font-bold text-yellow-800">
                                Attendre un meilleur point d'entree
                              </div>
                            </div>
                          )}
                        </div>

                        <div className="flex flex-col gap-2 min-w-[140px]" onClick={e => e.stopPropagation()}>
                          {(() => {
                            const timing = asset.signal === 'buy' ? getTimingStatus(asset) : null;
                            const canOpen = asset.signal === 'buy' && timing && timing.canEnter;
                            const isLate = timing?.phase === 'between';
                            const isClosed = timing?.phase === 'in_exit' || timing?.phase === 'after_exit';
                            const isBefore = timing?.phase === 'before_entry';

                            return (
                              <>
                                <button
                                  onClick={() => { setSelectedAsset(asset); setShowNewPosition(true); }}
                                  disabled={!canOpen}
                                  className={`flex items-center justify-center gap-1 px-4 py-2 rounded-lg text-sm font-medium ${
                                    canOpen && !isLate
                                      ? 'bg-green-600 text-white hover:bg-green-700'
                                      : canOpen && isLate
                                      ? 'bg-orange-500 text-white hover:bg-orange-600'
                                      : 'bg-gray-200 text-gray-400 cursor-not-allowed'
                                  }`}
                                >
                                  <Plus className="w-4 h-4" />
                                  {asset.signal !== 'buy'
                                    ? 'Signal insuffisant'
                                    : isClosed
                                    ? 'Session terminee'
                                    : isBefore
                                    ? 'Pas encore ouvert'
                                    : isLate
                                    ? 'Entree tardive'
                                    : 'Ouvrir Position'}
                                </button>
                                {timing && (
                                  <div className={`text-[10px] text-center px-1 ${
                                    timing.phase === 'in_entry' ? 'text-green-600 font-bold' :
                                    timing.phase === 'between' ? 'text-orange-600' :
                                    'text-gray-400'
                                  }`}>
                                    {timing.phase === 'in_entry' && 'Fenetre d\'entree ouverte'}
                                    {timing.phase === 'between' && timing.label}
                                    {timing.phase === 'in_exit' && 'Cloturez vos positions'}
                                    {timing.phase === 'after_exit' && 'Session terminee'}
                                    {timing.phase === 'before_entry' && `Ouverture a ${asset.entryWindow.start}`}
                                  </div>
                                )}
                                {isLate && timing?.lateEntryRisk && (
                                  <div className="bg-orange-50 border border-orange-200 rounded p-2 text-[10px] text-orange-700">
                                    <AlertTriangle className="w-3 h-3 inline mr-1" />
                                    {timing.lateEntryRisk}
                                  </div>
                                )}
                                {isClosed && (
                                  <div className="bg-red-50 border border-red-200 rounded p-2 text-[10px] text-red-700">
                                    <XCircle className="w-3 h-3 inline mr-1" />
                                    Position fermee. Attendez la prochaine session.
                                  </div>
                                )}
                              </>
                            );
                          })()}
                        </div>
                      </div>

                      {/* Analysis reason */}
                      <div className="mt-3 pt-3 border-t">
                        <p className="text-sm text-gray-600">
                          <span className="font-medium text-gray-700">Analyse :</span> {asset.reason}
                        </p>
                      </div>

                      {/* Expanded: Technical Indicators */}
                      {isExpanded && asset.indicators && (
                        <IndicatorsPanel indicators={asset.indicators} price={asset.currentPrice} currency={cur} />
                      )}
                      {isExpanded && !asset.indicators && (
                        <div className="mt-3 pt-3 border-t border-gray-100 text-sm text-gray-500">
                          Indicateurs non disponibles - donnees historiques insuffisantes
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* ═══════════ TAB: Positions ═══════════ */}
        {tab === 'positions' && (
          <PositionsTab />
        )}

        {/* DEAD CODE BELOW — replaced by PositionsTab */}
        {false && (
          <div className="space-y-6">
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">Capital Investi</div>
                <div className="text-xl font-bold text-gray-900">{formatCurrency(totalInvested)}</div>
                <div className="text-xs text-gray-400 mt-1">{openPositions.length} position{openPositions.length !== 1 ? 's' : ''} ouverte{openPositions.length !== 1 ? 's' : ''}</div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">P&L Ouvert</div>
                <div className={`text-xl font-bold ${totalOpenPnL >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {totalOpenPnL >= 0 ? '+' : ''}{formatCurrency(totalOpenPnL)}
                </div>
                {totalInvested > 0 && <div className={`text-xs mt-1 ${totalOpenPnL >= 0 ? 'text-green-500' : 'text-red-500'}`}>
                  {totalOpenPnL >= 0 ? '+' : ''}{(totalOpenPnL / totalInvested * 100).toFixed(2)}%
                </div>}
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">P&L Realise</div>
                <div className={`text-xl font-bold ${totalRealizedPnL >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {totalRealizedPnL >= 0 ? '+' : ''}{formatCurrency(totalRealizedPnL)}
                </div>
                <div className="text-xs text-gray-400 mt-1">{closedPositions.length} trade{closedPositions.length !== 1 ? 's' : ''} cloture{closedPositions.length !== 1 ? 's' : ''}</div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">Win Rate</div>
                <div className={`text-xl font-bold ${winRate >= 50 ? 'text-green-600' : winRate > 0 ? 'text-orange-500' : 'text-gray-400'}`}>
                  {winRate.toFixed(0)}%
                </div>
                <div className="text-xs text-gray-400 mt-1">
                  {closedPositions.filter(p => p.pnl > 0).length}W / {closedPositions.filter(p => p.pnl <= 0).length}L
                </div>
              </div>
            </div>

            {/* Stats detaillees si trades clotures */}
            {closedPositions.length > 0 && (
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 text-center">
                  <div>
                    <div className="text-xs text-gray-500">Meilleur Trade</div>
                    <div className="text-sm font-bold text-green-600">+{formatCurrency(bestTrade)}</div>
                  </div>
                  <div>
                    <div className="text-xs text-gray-500">Pire Trade</div>
                    <div className="text-sm font-bold text-red-600">{formatCurrency(worstTrade)}</div>
                  </div>
                  <div>
                    <div className="text-xs text-gray-500">Gain Moyen</div>
                    <div className="text-sm font-bold text-green-600">+{formatCurrency(avgWin)}</div>
                  </div>
                  <div>
                    <div className="text-xs text-gray-500">Perte Moyenne</div>
                    <div className="text-sm font-bold text-red-600">{formatCurrency(avgLoss)}</div>
                  </div>
                </div>
              </div>
            )}

            {openPositions.length === 0 && closedPositions.length === 0 ? (
              <div className="text-center py-12 bg-white rounded-xl border">
                <BarChart3 className="w-12 h-12 text-gray-300 mx-auto mb-3" />
                <p className="text-gray-500 text-lg">Aucune position</p>
                <p className="text-gray-400 text-sm mt-1">Allez dans "Marche & Signaux" pour ouvrir votre premiere position</p>
              </div>
            ) : (
              <>
                {/* Positions Ouvertes */}
                {openPositions.length > 0 && (
                  <div>
                    <h3 className="text-lg font-bold text-gray-900 mb-3 flex items-center gap-2">
                      <Play className="w-5 h-5 text-green-600" /> Positions Ouvertes ({openPositions.length})
                    </h3>
                    <div className="space-y-3">
                      {openPositions.map(pos => {
                        const invested = pos.entryPrice * pos.quantity;
                        const slDist = pos.stopLoss ? ((pos.currentPrice - pos.stopLoss) / pos.currentPrice * 100) : null;
                        const tpDist = pos.takeProfit ? ((pos.takeProfit - pos.currentPrice) / pos.currentPrice * 100) : null;
                        // SL/TP progress: 0 = at SL, 1 = at TP
                        const slTpProgress = (pos.stopLoss && pos.takeProfit)
                          ? Math.max(0, Math.min(1, (pos.currentPrice - pos.stopLoss) / (pos.takeProfit - pos.stopLoss)))
                          : null;

                        return (
                          <div key={pos.id} className={`rounded-xl shadow-sm border p-4 ${pos.pnl >= 0 ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}`}>
                            {/* Header: symbol + P&L */}
                            <div className="flex items-center justify-between mb-3">
                              <div className="flex items-center gap-2">
                                <span className="font-bold text-lg text-gray-900">{pos.symbol}</span>
                                <span className="text-gray-500 text-sm">{pos.name}</span>
                                <span className="px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs font-medium">LONG</span>
                              </div>
                              <div className="text-right">
                                <div className={`text-xl font-bold ${pos.pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                  {pos.pnl >= 0 ? '+' : ''}{formatCurrency(pos.pnl)}
                                </div>
                                <div className={`text-xs font-medium ${pos.pnlPercent >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                                  {pos.pnlPercent >= 0 ? '+' : ''}{pos.pnlPercent.toFixed(2)}%
                                </div>
                              </div>
                            </div>

                            {/* Prix + infos */}
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm mb-3">
                              <div>
                                <span className="text-gray-500">Entree</span>
                                <div className="font-medium">{formatCurrency(pos.entryPrice)}</div>
                              </div>
                              <div>
                                <span className="text-gray-500">Actuel</span>
                                <div className="font-medium">{formatCurrency(pos.currentPrice)}</div>
                              </div>
                              <div>
                                <span className="text-gray-500">Investi</span>
                                <div className="font-medium">{formatCurrency(invested)}</div>
                              </div>
                              <div>
                                <span className="text-gray-500">Duree</span>
                                <div className="font-medium flex items-center gap-1">
                                  <Clock className="w-3 h-3" /> {formatDuration(pos.entryTime)}
                                </div>
                              </div>
                            </div>

                            {/* Barre SL/TP */}
                            {slTpProgress !== null && pos.stopLoss && pos.takeProfit && (
                              <div className="mb-3">
                                <div className="flex justify-between text-xs mb-1">
                                  <span className="text-red-600 font-medium">SL: {formatCurrency(pos.stopLoss)} ({slDist !== null ? slDist.toFixed(1) : '-'}%)</span>
                                  <span className="text-green-600 font-medium">TP: {formatCurrency(pos.takeProfit)} (+{tpDist !== null ? tpDist.toFixed(1) : '-'}%)</span>
                                </div>
                                <div className="h-3 bg-gray-200 rounded-full overflow-hidden relative">
                                  <div className="absolute inset-0 bg-gradient-to-r from-red-400 via-yellow-300 to-green-400 opacity-30" />
                                  <div
                                    className="absolute top-0 h-full w-1 bg-gray-800 rounded"
                                    style={{ left: `${slTpProgress * 100}%`, transform: 'translateX(-50%)' }}
                                  />
                                  <div
                                    className={`absolute top-0 h-full w-2.5 rounded-full border-2 border-white shadow ${pos.pnl >= 0 ? 'bg-green-600' : 'bg-red-600'}`}
                                    style={{ left: `${slTpProgress * 100}%`, transform: 'translateX(-50%)' }}
                                  />
                                </div>
                                <div className="text-center text-xs text-gray-500 mt-1">
                                  Prix actuel: {(slTpProgress * 100).toFixed(0)}% vers le TP
                                </div>
                              </div>
                            )}

                            {/* Bouton cloturer */}
                            <div className="flex justify-end mt-2">
                              <button onClick={() => closePosition(pos.id)}
                                className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 text-sm font-medium flex items-center gap-2">
                                <Square className="w-4 h-4" /> Cloturer
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}

                {/* Positions Cloturees */}
                {closedPositions.length > 0 && (
                  <div>
                    <h3 className="text-lg font-bold text-gray-900 mb-3 flex items-center gap-2">
                      <CheckCircle className="w-5 h-5 text-gray-500" /> Historique ({closedPositions.length})
                    </h3>
                    <div className="space-y-2">
                      {closedPositions.slice().reverse().map(pos => {
                        const isWin = pos.pnl > 0;
                        const duration = formatDuration(pos.entryTime, pos.exitTime);
                        return (
                          <div key={pos.id} className={`bg-white rounded-xl shadow-sm border-l-4 ${isWin ? 'border-l-green-500' : 'border-l-red-500'} border border-gray-100 p-4`}>
                            <div className="flex items-center justify-between mb-2">
                              <div className="flex items-center gap-2">
                                <span className="font-bold text-gray-800">{pos.symbol}</span>
                                <span className="text-gray-400 text-sm">{pos.name}</span>
                                <span className={`px-2 py-0.5 rounded text-xs font-bold ${isWin ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                                  {isWin ? 'GAIN' : 'PERTE'}
                                </span>
                              </div>
                              <div className="flex items-center gap-2">
                                <div className={`text-lg font-bold ${isWin ? 'text-green-600' : 'text-red-600'}`}>
                                  {pos.pnl >= 0 ? '+' : ''}{formatCurrency(pos.pnl)}
                                  <span className="text-xs ml-1">({pos.pnlPercent >= 0 ? '+' : ''}{pos.pnlPercent.toFixed(2)}%)</span>
                                </div>
                                <button onClick={() => deletePosition(pos.id)} className="p-1.5 text-gray-400 hover:text-red-500" title="Supprimer">
                                  <Trash2 className="w-4 h-4" />
                                </button>
                              </div>
                            </div>
                            <div className="flex flex-wrap items-center gap-4 text-xs text-gray-500">
                              <span>Entree: {formatCurrency(pos.entryPrice)} a {formatDateTime(pos.entryTime)}</span>
                              <span>Sortie: {formatCurrency(pos.exitPrice || pos.currentPrice)} a {pos.exitTime ? formatDateTime(pos.exitTime) : '-'}</span>
                              <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {duration}</span>
                              <span>Qte: {pos.quantity.toFixed(4)}</span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* ═══════════ TAB: 2-Month History ═══════════ */}
        {tab === 'history' && (
          <div>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">P&L Cumule (2 mois)</div>
                <div className={`text-xl font-bold ${totalHistoryPnL >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {totalHistoryPnL >= 0 ? '+' : ''}{formatCurrency(totalHistoryPnL)}
                </div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">Jours de Trading</div>
                <div className="text-xl font-bold text-gray-900">{history.length}</div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">Max Drawdown</div>
                <div className="text-xl font-bold text-red-600">-{formatCurrency(maxDrawdown)}</div>
              </div>
              <div className="bg-white rounded-xl shadow-sm border p-4">
                <div className="text-xs text-gray-500 mb-1">Moy. P&L / Jour</div>
                <div className={`text-xl font-bold ${totalHistoryPnL >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                  {history.length > 0 ? formatCurrency(totalHistoryPnL / history.length) : '0,00 €'}
                </div>
              </div>
            </div>

            <div className="bg-white rounded-xl shadow-sm border p-6 mb-6">
              <h3 className="text-lg font-bold text-gray-900 mb-4">Courbe de Performance (P&L Cumule)</h3>
              <div className="overflow-x-auto">
                <div className="min-w-[600px] h-64 flex items-end gap-1">
                  {cumulativePnL.map((d, i) => {
                    const maxVal = Math.max(...cumulativePnL.map(x => Math.abs(x.cumulative)), 1);
                    const height = Math.abs(d.cumulative) / maxVal * 100;
                    return (
                      <div key={d.date} className="flex-1 flex flex-col justify-end items-center group" style={{ minWidth: '8px' }}>
                        <div
                          className={`w-full rounded-t-sm ${d.cumulative >= 0 ? 'bg-green-500' : 'bg-red-500'} hover:opacity-80 transition cursor-pointer`}
                          style={{ height: `${Math.max(height, 2)}%` }}
                          title={`${d.date}: ${d.cumulative >= 0 ? '+' : ''}${d.cumulative.toFixed(2)}€`}
                        />
                        {i % 5 === 0 && (
                          <span className="text-[9px] text-gray-400 mt-1 -rotate-45 origin-left whitespace-nowrap">
                            {formatDate(d.date)}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <div className="bg-white rounded-xl shadow-sm border overflow-hidden">
              <div className="p-4 border-b">
                <h3 className="font-bold text-gray-900">Historique Journalier</h3>
              </div>
              <div className="overflow-x-auto max-h-96">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 sticky top-0">
                    <tr>
                      <th className="text-left px-4 py-2 text-gray-600">Date</th>
                      <th className="text-right px-4 py-2 text-gray-600">P&L</th>
                      <th className="text-right px-4 py-2 text-gray-600">Trades</th>
                      <th className="text-right px-4 py-2 text-gray-600">Win Rate</th>
                      <th className="text-right px-4 py-2 text-gray-600">Investi</th>
                      <th className="text-right px-4 py-2 text-gray-600">Cumule</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {cumulativePnL.slice().reverse().map(d => (
                      <tr key={d.date} className="hover:bg-gray-50">
                        <td className="px-4 py-2 font-medium text-gray-900">
                          {new Date(d.date).toLocaleDateString('fr-FR', { weekday: 'short', day: '2-digit', month: '2-digit' })}
                        </td>
                        <td className={`px-4 py-2 text-right font-medium ${d.pnl >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                          {d.pnl >= 0 ? '+' : ''}{d.pnl.toFixed(2)}€
                        </td>
                        <td className="px-4 py-2 text-right text-gray-600">{d.trades}</td>
                        <td className="px-4 py-2 text-right text-gray-600">{d.winRate}%</td>
                        <td className="px-4 py-2 text-right text-gray-600">{d.totalInvested.toFixed(2)}€</td>
                        <td className={`px-4 py-2 text-right font-bold ${d.cumulative >= 0 ? 'text-green-600' : 'text-red-600'}`}>
                          {d.cumulative >= 0 ? '+' : ''}{d.cumulative.toFixed(2)}€
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        )}

        {/* ═══════════ TAB: Regulated Platforms ═══════════ */}
        {tab === 'platforms' && (
          <div>
            <div className="bg-green-50 border border-green-200 rounded-lg p-4 mb-6 flex items-start gap-3">
              <Shield className="w-6 h-6 text-green-600 mt-0.5 flex-shrink-0" />
              <div className="text-sm text-green-800">
                <strong>Toutes les plateformes ci-dessous sont regulees et enregistrees aupres de l'AMF</strong> (Autorite des Marches Financiers)
                ou d'un regulateur europeen equivalent. Vous etes protege par le cadre reglementaire europeen MiFID II.
                Verifiez toujours sur <strong>regafi.fr</strong> et <strong>amf-france.org</strong> qu'un courtier est bien agree avant d'investir.
              </div>
            </div>

            <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 mb-6 flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-yellow-600 mt-0.5 flex-shrink-0" />
              <div className="text-sm text-yellow-800">
                <strong>Rappel legal :</strong> Investir comporte des risques de perte en capital. Les performances passees ne prejugent pas
                des performances futures. Le day trading est une activite a haut risque. Commencez par des montants que vous etes pret a perdre
                et formez-vous avant d'investir de l'argent reel. En France, les plus-values sont soumises au PFU (flat tax) de 30%.
              </div>
            </div>

            <div className="space-y-4">
              {REGULATED_PLATFORMS.map(platform => (
                <div key={platform.name} className="bg-white rounded-xl shadow-sm border p-6">
                  <div className="flex flex-col lg:flex-row gap-6">
                    <div className="flex-1">
                      <div className="flex items-center gap-3 mb-3">
                        <span className="text-3xl">{platform.logo}</span>
                        <div>
                          <h3 className="text-lg font-bold text-gray-900">{platform.name}</h3>
                          <div className="flex items-center gap-2">
                            <Shield className="w-3.5 h-3.5 text-green-600" />
                            <span className="text-xs text-green-700 font-medium">{platform.regulator}</span>
                          </div>
                        </div>
                        <div className="ml-auto flex items-center gap-1">
                          {Array.from({ length: 5 }).map((_, i) => (
                            <Star key={i} className={`w-4 h-4 ${i < Math.floor(platform.rating) ? 'text-yellow-400 fill-yellow-400' : 'text-gray-300'}`} />
                          ))}
                          <span className="text-sm text-gray-600 ml-1">{platform.rating}</span>
                        </div>
                      </div>

                      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-4">
                        <div>
                          <span className="text-xs text-gray-500">Depot minimum</span>
                          <div className="font-bold text-gray-900">{platform.minDeposit}€</div>
                        </div>
                        <div>
                          <span className="text-xs text-gray-500">Frais</span>
                          <div className="font-bold text-gray-900">{platform.fees}</div>
                        </div>
                        <div>
                          <span className="text-xs text-gray-500">Pays</span>
                          <div className="font-bold text-gray-900">{platform.country}</div>
                        </div>
                      </div>

                      <div className="flex flex-wrap gap-1 mb-3">
                        {platform.instruments.map(inst => (
                          <span key={inst} className="px-2 py-0.5 bg-blue-50 text-blue-700 rounded text-xs font-medium">{inst}</span>
                        ))}
                      </div>

                      <div className="grid sm:grid-cols-2 gap-3">
                        <div>
                          <h4 className="text-xs font-bold text-green-700 mb-1">Avantages</h4>
                          <ul className="space-y-1">
                            {platform.pros.map(pro => (
                              <li key={pro} className="flex items-start gap-1 text-xs text-gray-600">
                                <CheckCircle className="w-3 h-3 text-green-500 mt-0.5 flex-shrink-0" /> {pro}
                              </li>
                            ))}
                          </ul>
                        </div>
                        <div>
                          <h4 className="text-xs font-bold text-red-700 mb-1">Inconvenients</h4>
                          <ul className="space-y-1">
                            {platform.cons.map(con => (
                              <li key={con} className="flex items-start gap-1 text-xs text-gray-600">
                                <XCircle className="w-3 h-3 text-red-500 mt-0.5 flex-shrink-0" /> {con}
                              </li>
                            ))}
                          </ul>
                        </div>
                      </div>
                    </div>

                    <div className="flex flex-col items-center justify-center gap-2 lg:border-l lg:pl-6 min-w-[160px]">
                      {platform.amfRegistered && (
                        <div className="flex items-center gap-1 px-3 py-1 bg-green-100 text-green-700 rounded-full text-xs font-bold">
                          <Shield className="w-3 h-3" /> AMF Agree
                        </div>
                      )}
                      <div className="text-center mt-2 text-xs text-gray-500">
                        Ideal pour petits montants :
                        <div className="font-bold text-gray-900 text-sm mt-1">des {platform.minDeposit}€</div>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>

            <div className="mt-6 bg-blue-50 border border-blue-200 rounded-xl p-6">
              <h3 className="text-lg font-bold text-blue-900 mb-3 flex items-center gap-2">
                <Star className="w-5 h-5 text-blue-600" /> Notre recommandation pour debuter avec de petites sommes
              </h3>
              <div className="grid sm:grid-cols-2 gap-4">
                <div className="bg-white rounded-lg p-4 border border-blue-100">
                  <h4 className="font-bold text-gray-900 mb-1">Trade Republic</h4>
                  <p className="text-sm text-gray-600">
                    Ideal pour debuter : 1€ par transaction, fractions d'actions des 1€, interface tres simple.
                    Parfait pour investir de petites sommes et apprendre.
                  </p>
                </div>
                <div className="bg-white rounded-lg p-4 border border-blue-100">
                  <h4 className="font-bold text-gray-900 mb-1">DEGIRO</h4>
                  <p className="text-sm text-gray-600">
                    Pour aller plus loin : frais tres bas, acces a plus de marches, ETF gratuits.
                    Ideal quand vous maitrisez les bases et voulez diversifier.
                  </p>
                </div>
              </div>
              <p className="text-xs text-blue-700 mt-3">
                Pour le day trading avance : <strong>Interactive Brokers</strong> offre les meilleurs outils et l'acces a 150+ marches,
                mais sa complexite le rend plus adapte aux traders experimentes.
              </p>
            </div>
          </div>
        )}

        {/* ═══════════ TAB: Trading Journal ═══════════ */}
        {tab === 'journal' && (
          <div>
            <div className="bg-white rounded-xl shadow-sm border p-4 mb-4">
              <h3 className="font-bold text-gray-900 mb-3">Ajouter une note</h3>
              <div className="flex gap-3">
                <textarea value={journalText} onChange={e => setJournalText(e.target.value)}
                  placeholder="Decrivez votre analyse, vos observations de marche, vos erreurs, vos apprentissages..."
                  className="flex-1 px-4 py-2 border border-gray-300 rounded-lg resize-none h-24 text-sm" />
                <button onClick={addJournalEntry} disabled={!journalText.trim()}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 text-sm font-medium self-end">
                  Ajouter
                </button>
              </div>
            </div>

            {journalEntries.length === 0 ? (
              <div className="text-center py-12 bg-white rounded-xl border">
                <BookOpen className="w-12 h-12 text-gray-300 mx-auto mb-3" />
                <p className="text-gray-500">Aucune note dans votre journal</p>
                <p className="text-gray-400 text-sm mt-1">Le journal vous aide a analyser vos decisions et progresser</p>
              </div>
            ) : (
              <div className="space-y-3">
                {journalEntries.map((entry, i) => (
                  <div key={i} className="bg-white rounded-xl shadow-sm border p-4">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-sm text-gray-500">
                        {new Date(entry.date).toLocaleDateString('fr-FR', {
                          weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit'
                        })}
                      </span>
                      <button onClick={() => setJournalEntries(prev => prev.filter((_, idx) => idx !== i))}
                        className="text-gray-400 hover:text-red-500">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                    <p className="text-gray-700 text-sm whitespace-pre-wrap">{entry.text}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      {/* ═══════════ New Position Modal ═══════════ */}
      {showNewPosition && selectedAsset && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
            <div className="p-6 border-b">
              <h3 className="text-lg font-bold text-gray-900">Ouvrir une Position</h3>
              <p className="text-sm text-gray-500 mt-1">{selectedAsset.symbol} - {selectedAsset.name}</p>
            </div>
            <div className="p-6 space-y-4">
              <div className="bg-gray-50 rounded-lg p-3">
                <div className="flex justify-between">
                  <span className="text-sm text-gray-600">Prix actuel</span>
                  <span className="font-bold text-gray-900">{formatCurrency(selectedAsset.currentPrice, selectedAsset.market === 'US' ? '$' : '€')}</span>
                </div>
                <div className="flex justify-between mt-1">
                  <span className="text-sm text-gray-600">Signal</span>
                  <SignalBadge signal={selectedAsset.signal} />
                </div>
                <div className="flex justify-between mt-1">
                  <span className="text-sm text-gray-600">Confiance</span>
                  <span className="text-sm font-bold text-gray-900">{selectedAsset.confidence}%</span>
                </div>
              </div>

              {selectedAsset.suggestedStopLoss && selectedAsset.suggestedTakeProfit && (
                <div className="bg-green-50 rounded-lg p-3 text-sm">
                  <div className="font-medium text-green-700 mb-2">Niveaux suggeres par l'analyse technique :</div>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div><span className="text-red-600">Stop Loss :</span> <span className="font-bold">{formatCurrency(selectedAsset.suggestedStopLoss, selectedAsset.market === 'US' ? '$' : '€')}</span></div>
                    <div><span className="text-green-600">Take Profit :</span> <span className="font-bold">{formatCurrency(selectedAsset.suggestedTakeProfit, selectedAsset.market === 'US' ? '$' : '€')}</span></div>
                  </div>
                </div>
              )}

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Montant a investir (€)</label>
                <input type="number" value={newAmount}
                  onChange={e => setNewAmount(e.target.value)}
                  placeholder="Ex: 10, 25, 50..."
                  className="w-full px-4 py-2 border border-gray-300 rounded-lg" />
                {newAmount && parseFloat(newAmount) > 0 && (
                  <p className="text-xs text-gray-500 mt-1">
                    = {(parseFloat(newAmount) / selectedAsset.currentPrice).toFixed(6)} unites
                  </p>
                )}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Stop Loss (€)</label>
                  <input type="number" value={newStopLoss} onChange={e => setNewStopLoss(e.target.value)}
                    placeholder={selectedAsset.suggestedStopLoss ? selectedAsset.suggestedStopLoss.toFixed(2) : 'Optionnel'}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg text-sm" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Take Profit (€)</label>
                  <input type="number" value={newTakeProfit} onChange={e => setNewTakeProfit(e.target.value)}
                    placeholder={selectedAsset.suggestedTakeProfit ? selectedAsset.suggestedTakeProfit.toFixed(2) : 'Optionnel'}
                    className="w-full px-4 py-2 border border-gray-300 rounded-lg text-sm" />
                </div>
              </div>

              <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3 flex items-start gap-2">
                <AlertTriangle className="w-4 h-4 text-yellow-600 mt-0.5 flex-shrink-0" />
                <p className="text-xs text-yellow-800">
                  Trading reel via Fusion Markets MT5. Les pertes sont reelles.
                </p>
              </div>
            </div>
            <div className="p-6 border-t flex gap-3">
              <button onClick={() => { setShowNewPosition(false); setSelectedAsset(null); }}
                className="flex-1 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50 text-sm font-medium">
                Annuler
              </button>
              <button onClick={() => openPosition(selectedAsset)}
                disabled={!newAmount || parseFloat(newAmount) <= 0}
                className="flex-1 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 text-sm font-medium">
                Confirmer l'achat
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Footer */}
      <footer className="border-t bg-white mt-12">
        <div className="max-w-7xl mx-auto px-4 lg:px-8 py-4">
          <p className="text-xs text-gray-500 text-center">
            Trading automatise Fusion Markets MT5 — Scalping mode.
            Signaux generes par analyse technique multi-indicateurs (RSI, MACD, Bollinger, MA, Stochastique, ADX, Fibonacci, ATR).
            Investir comporte des risques. Les performances passees ne prejugent pas des performances futures.
          </p>
        </div>
      </footer>
    </div>
  );
}
