/**
 * Hook to connect to the trading backend via WebSocket + REST.
 * Falls back gracefully to the existing Yahoo Finance data source
 * when the backend is unavailable.
 */
import { useState, useEffect, useCallback, useRef } from 'react';

// Set this to your backend URL (DigitalOcean server with Caddy)
const BACKEND_URL = import.meta.env.VITE_BACKEND_URL || '';
const API_KEY = import.meta.env.VITE_API_KEY || '';

interface BotStatus {
  running: boolean;
  scan_only: boolean;
  mt5_connected: boolean;
  primary_broker: string;
  capital: number;
  daily_pnl: number;
  open_positions: number;
  trades_today: number;
  consecutive_losses: number;
  circuit_breaker: boolean;
  dynamic_allocation?: Record<string, number>;
}

interface BotConfig {
  starting_capital: number;
  current_capital: number;
  max_order_size: number;
  max_risk_per_trade: number;
  max_daily_loss: number;
  max_open_positions: number;
  scan_interval: number;
}

interface BackendSignal {
  symbol: string;
  name: string;
  market: string;
  price: number;
  change: number;
  change_percent: number;
  signal: 'buy' | 'sell' | 'hold';
  confidence: number;
  reason: string;
  suggested_entry: number;
  suggested_sl: number;
  suggested_tp: number;
}

interface TradeEvent {
  symbol: string;
  pnl: number;
  reason: string;
  exit_price: number;
}

interface AccountData {
  balance: number;
  net_liquidation: number;
  buying_power: number;
  daily_pnl: number;
  currency: string;
  capital: number;
}

interface DailyTradeSummary {
  pnl: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
}

export interface BackendState {
  connected: boolean;
  backendAvailable: boolean;
  botStatus: BotStatus | null;
  botConfig: BotConfig | null;
  account: AccountData | null;
  signals: BackendSignal[];
  dailySummary: DailyTradeSummary | null;
  logs: string[];
  // Actions
  startBot: () => Promise<void>;
  scanOnly: () => Promise<void>;
  setMode: (mode: 'auto' | 'scan') => Promise<void>;
  stopBot: (closePositions?: boolean) => Promise<void>;
  emergencyStop: () => Promise<void>;
  updateConfig: (config: Partial<BotConfig>) => Promise<void>;
  placeOrder: (symbol: string, action: string, amount: number, stop_loss?: number, take_profit?: number) => Promise<any>;
  closePosition: (symbol: string) => Promise<void>;
  refreshAccount: () => Promise<void>;
  refreshSignals: () => Promise<void>;
}

const headers = () => ({
  'Authorization': `Bearer ${API_KEY}`,
  'Content-Type': 'application/json',
});

async function apiFetch(path: string, options?: RequestInit) {
  if (!BACKEND_URL) throw new Error('Backend URL not configured');
  const resp = await fetch(`${BACKEND_URL}${path}`, {
    ...options,
    headers: { ...headers(), ...options?.headers },
    cache: 'no-store',
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`API ${resp.status}: ${text}`);
  }
  return resp.json();
}

export function useBackend(): BackendState {
  const [connected, setConnected] = useState(false);
  const [backendAvailable, setBackendAvailable] = useState(false);
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);
  const [botConfig, setBotConfig] = useState<BotConfig | null>(null);
  const [account, setAccount] = useState<AccountData | null>(null);
  const [signals, setSignals] = useState<BackendSignal[]>([]);
  const [dailySummary, setDailySummary] = useState<DailyTradeSummary | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  const addLog = useCallback((msg: string) => {
    const ts = new Date().toLocaleTimeString('fr-FR');
    setLogs(prev => [`[${ts}] ${msg}`, ...prev].slice(0, 200));
  }, []);

  // WebSocket connection
  const connectWS = useCallback(() => {
    if (!BACKEND_URL || !API_KEY) return;

    const wsUrl = BACKEND_URL.replace('https://', 'wss://').replace('http://', 'ws://');
    const ws = new WebSocket(`${wsUrl}/api/ws?token=${API_KEY}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setBackendAvailable(true);
      addLog('Connecte au backend');
    };

    ws.onclose = () => {
      setConnected(false);
      addLog('Deconnecte du backend — reconnexion dans 5s');
      // Reconnect after 5 seconds
      reconnectTimer.current = setTimeout(connectWS, 5000);
    };

    ws.onerror = () => {
      setConnected(false);
      // Fallback: check if backend is reachable via HTTP even if WS fails
      apiFetch('/api/health').then(() => {
        setBackendAvailable(true);
      }).catch(() => {
        // Backend truly unreachable
      });
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        switch (msg.type) {
          case 'bot_status':
            setBotStatus(msg.data);
            break;
          case 'signal':
            setSignals(prev => {
              const idx = prev.findIndex(s => s.symbol === msg.data.symbol);
              if (idx >= 0) {
                const updated = [...prev];
                updated[idx] = msg.data;
                return updated;
              }
              return [...prev, msg.data];
            });
            break;
          case 'order_filled':
            addLog(`Ordre execute: ${msg.data.action} ${msg.data.quantity}x ${msg.data.symbol} @ ${msg.data.entry_price}`);
            break;
          case 'position_update':
            // Position PnL update handled in real-time
            break;
          case 'account_update':
            setAccount(prev => prev ? { ...prev, ...msg.data } : null);
            break;
          case 'trade_closed':
            addLog(`Trade ferme: ${msg.data.symbol} PnL=${msg.data.pnl}EUR (${msg.data.reason})`);
            // Trigger immediate refresh of daily summary to update P&L
            apiFetch('/api/trades/daily').then(daily => setDailySummary(daily)).catch(() => {});
            break;
          case 'alert':
            addLog(`[${msg.data.level?.toUpperCase()}] ${msg.data.message}`);
            break;
        }
      } catch (e) {
        console.error('WS message parse error:', e);
      }
    };
  }, [addLog]);

  // Check if backend is available on mount
  useEffect(() => {
    if (!BACKEND_URL) return;

    const checkBackend = async () => {
      try {
        const health = await apiFetch('/api/health');
        setBackendAvailable(true);
        addLog(`Backend en ligne: MT5 ${health.mt5_connected ? 'connecte' : 'deconnecte'}, Bot ${health.bot_running ? 'actif' : 'arrete'}`);
        connectWS();
      } catch {
        setBackendAvailable(false);
        addLog('Backend non disponible — mode donnees Yahoo Finance');
      }
    };
    checkBackend();

    // Periodic health check every 15s — recovers backendAvailable after restart
    const healthInterval = setInterval(async () => {
      try {
        await apiFetch('/api/health');
        setBackendAvailable(true);
      } catch {
        setBackendAvailable(false);
      }
    }, 15000);

    return () => {
      if (wsRef.current) wsRef.current.close();
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      clearInterval(healthInterval);
    };
  }, [connectWS, addLog]);

  // Periodic refresh of account + config + bot status (fallback to WS)
  useEffect(() => {
    if (!backendAvailable) return;
    const refresh = async () => {
      try {
        const [acc, cfg, daily, status] = await Promise.all([
          apiFetch('/api/account'),
          apiFetch('/api/bot/config'),
          apiFetch('/api/trades/daily'),
          apiFetch('/api/bot/status'),
        ]);
        setAccount(acc);
        setBotConfig(cfg);
        setDailySummary(daily);
        if (status && typeof status === 'object') setBotStatus(status);
      } catch (e) {
        console.warn('Refresh error:', e);
      }
    };
    refresh();
    const timer = setInterval(refresh, 5000); // Every 5s — keeps mt5_connected fresh
    return () => clearInterval(timer);
  }, [backendAvailable]);

  // Actions
  const startBot = useCallback(async () => {
    const result = await apiFetch('/api/bot/start', { method: 'POST' });
    setBotStatus(result.status);
    addLog('Bot demarre (mode execution)');
  }, [addLog]);

  const scanOnly = useCallback(async () => {
    const result = await apiFetch('/api/bot/scan-only', { method: 'POST' });
    setBotStatus(result.status);
    addLog('Bot demarre en mode SCAN ONLY — aucune execution');
  }, [addLog]);

  const setMode = useCallback(async (mode: 'auto' | 'scan') => {
    const result = await apiFetch(`/api/bot/set-mode?mode=${mode}`, { method: 'POST' });
    setBotStatus(result.status);
    addLog(mode === 'auto' ? 'Bot passe en mode AUTO — execution active' : 'Bot passe en mode SCAN ONLY');
  }, [addLog]);

  const stopBot = useCallback(async (closePositions = false) => {
    const result = await apiFetch(`/api/bot/stop?close_positions=${closePositions}`, { method: 'POST' });
    setBotStatus(result.status);
    addLog(`Bot arrete ${closePositions ? '(positions fermees)' : ''}`);
  }, [addLog]);

  const emergencyStop = useCallback(async () => {
    await apiFetch('/api/bot/emergency-stop', { method: 'POST' });
    addLog('ARRET D\'URGENCE EXECUTE');
  }, [addLog]);

  const updateConfig = useCallback(async (config: Partial<BotConfig>) => {
    const result = await apiFetch('/api/bot/config', {
      method: 'PUT',
      body: JSON.stringify(config),
    });
    setBotConfig(result.config);
    addLog('Configuration mise a jour');
  }, [addLog]);

  const placeOrder = useCallback(async (symbol: string, action: string, amount: number, stop_loss?: number, take_profit?: number) => {
    const body: Record<string, any> = { symbol, action, amount_eur: amount };
    if (stop_loss !== undefined && stop_loss !== null && !isNaN(stop_loss)) body.stop_loss = stop_loss;
    if (take_profit !== undefined && take_profit !== null && !isNaN(take_profit)) body.take_profit = take_profit;
    const result = await apiFetch('/api/orders', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const slTpStr = (stop_loss || take_profit) ? ` SL=${stop_loss || 'auto'} TP=${take_profit || 'auto'}` : '';
    addLog(`Ordre manuel: ${action.toUpperCase()} ${symbol} ${amount}EUR${slTpStr}`);
    return result;
  }, [addLog]);

  const closePosition = useCallback(async (symbol: string) => {
    await apiFetch('/api/positions/close', {
      method: 'POST',
      body: JSON.stringify({ symbol }),
    });
    addLog(`Position fermee manuellement: ${symbol}`);
  }, [addLog]);

  const refreshAccount = useCallback(async () => {
    try {
      const acc = await apiFetch('/api/account');
      setAccount(acc);
    } catch (e) {
      console.warn('Account refresh error:', e);
    }
  }, []);

  const refreshSignals = useCallback(async () => {
    try {
      const sigs = await apiFetch('/api/market/scan');
      setSignals(sigs);
    } catch (e) {
      console.warn('Signals refresh error:', e);
    }
  }, []);

  return {
    connected,
    backendAvailable,
    botStatus,
    botConfig,
    account,
    signals,
    dailySummary,
    logs,
    startBot,
    scanOnly,
    setMode,
    stopBot,
    emergencyStop,
    updateConfig,
    placeOrder,
    closePosition,
    refreshAccount,
    refreshSignals,
  };
}
