/**
 * Operator Control Panel — manages the trading bot from the web UI.
 * Displays real-time bot status, account info, config controls,
 * and activity logs.
 */
import { useState } from 'react';
import {
  Play, Square, AlertTriangle, Settings, Activity, RefreshCw,
  Wifi, WifiOff, XCircle, CheckCircle,
  Send, ArrowUpRight, ArrowDownRight, X, Eye
} from 'lucide-react';
import type { BackendState } from './useBackend';

interface ControlPanelProps {
  backend: BackendState;
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat('fr-FR', { style: 'currency', currency: 'EUR' }).format(value);
}

export default function ControlPanel({ backend }: ControlPanelProps) {
  const {
    connected, backendAvailable, botStatus, botConfig, account,
    dailySummary, logs,
    startBot, scanOnly, stopBot, emergencyStop, updateConfig, placeOrder, closePosition,
  } = backend;

  const [showConfig, setShowConfig] = useState(false);
  const [configDraft, setConfigDraft] = useState({
    max_order_size: botConfig?.max_order_size || 20,
    max_risk_per_trade: (botConfig?.max_risk_per_trade || 0.02) * 100,
    max_daily_loss: (botConfig?.max_daily_loss || 0.05) * 100,
    max_open_positions: botConfig?.max_open_positions || 5,
    scan_interval: botConfig?.scan_interval || 60,
  });

  const [confirmEmergency, setConfirmEmergency] = useState(false);

  // Manual trading state
  const [showManualTrade, setShowManualTrade] = useState(false);
  const [manualSymbol, setManualSymbol] = useState('GBP/USD');
  const [manualAction, setManualAction] = useState<'buy' | 'sell'>('buy');
  const [manualAmount, setManualAmount] = useState(20);
  const [manualLoading, setManualLoading] = useState(false);
  const [manualResult, setManualResult] = useState<{ success: boolean; message: string } | null>(null);

  const TRADEABLE_SYMBOLS = [
    // Forex
    'EUR/USD', 'GBP/USD', 'USD/JPY', 'EUR/GBP', 'AUD/USD', 'NZD/USD', 'EUR/JPY', 'GBP/JPY',
    'EUR/CAD', 'EUR/CHF', 'EUR/AUD', 'GBP/AUD', 'AUD/JPY', 'AUD/CHF', 'AUD/NZD',
    'USD/CAD', 'USD/CHF',
    // Indices
    'DAX40', 'SP500', 'NASDAQ', 'CAC40', 'NKY', 'HK50', 'AUS200',
    // Commodities
    'OIL_CRUDE',
  ];

  const handleManualOrder = async () => {
    setManualLoading(true);
    setManualResult(null);
    try {
      const result = await placeOrder(manualSymbol, manualAction, manualAmount);
      setManualResult({
        success: result.success !== false,
        message: result.success !== false
          ? `Ordre ${manualAction.toUpperCase()} ${manualSymbol} ${manualAmount}EUR execute`
          : (result.error || 'Erreur lors de l\'execution'),
      });
    } catch (e: any) {
      setManualResult({ success: false, message: e.message || 'Erreur de connexion' });
    } finally {
      setManualLoading(false);
    }
  };

  if (!backendAvailable) {
    return (
      <div className="bg-yellow-50 border border-yellow-200 rounded-xl p-8 text-center">
        <WifiOff className="w-12 h-12 text-yellow-500 mx-auto mb-4" />
        <h3 className="text-lg font-bold text-yellow-800 mb-2">Backend Non Connecte</h3>
        <p className="text-sm text-yellow-700 mb-4">
          Le serveur de trading n'est pas accessible. Verifiez que le backend tourne sur votre serveur DigitalOcean.
        </p>
        <div className="bg-white rounded-lg p-4 text-left text-xs font-mono text-gray-600 max-w-md mx-auto">
          <p className="mb-1">1. Connectez-vous a votre serveur :</p>
          <p className="text-blue-600 mb-2">   ssh user@votre-ip</p>
          <p className="mb-1">2. Lancez les services :</p>
          <p className="text-blue-600 mb-2">   cd trading-backend && docker compose up -d</p>
          <p className="mb-1">3. Configurez l'URL dans .env :</p>
          <p className="text-blue-600">   VITE_BACKEND_URL=https://api.votre-domaine.com</p>
          <p className="text-blue-600">   VITE_API_KEY=votre-cle-api</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Fusion Markets MT5 — Broker unique */}
      <div className="space-y-3">
        <div className="flex items-center justify-between bg-gradient-to-r from-green-50 to-white rounded-xl shadow-sm border p-4">
          <div className="flex items-center gap-3">
            <span className={`w-3 h-3 rounded-full flex-shrink-0 ${botStatus?.mt5_connected ? 'bg-green-500' : 'bg-red-500'}`} />
            <div>
              <div className="flex items-center gap-2">
                <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-green-100 text-green-700">Fusion Markets</span>
                <span className="font-medium text-sm text-gray-900">MetaTrader 5 (ZMQ bridge)</span>
              </div>
              <div className="text-xs text-gray-500">
                {botStatus?.mt5_connected ? (
                  <span className="text-green-600 font-medium">Connecte — SL/TP natif</span>
                ) : (
                  <span className="text-red-600 font-medium">Deconnecte</span>
                )}
                {account && <span className="ml-2">· Capital: {formatCurrency(account.capital || 0)}</span>}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            {botStatus?.running ? (
              <>
                {botStatus?.scan_only && (
                  <span className="text-[10px] font-bold px-1.5 py-0.5 rounded bg-blue-100 text-blue-700">SCAN ONLY</span>
                )}
                <button onClick={() => stopBot(false)}
                  title="Arreter le bot"
                  className="flex items-center gap-2 px-4 py-2 bg-orange-600 text-white rounded-lg hover:bg-orange-700 transition text-sm font-medium">
                  <Square className="w-4 h-4" /> Arreter
                </button>
              </>
            ) : (
              <>
                <button onClick={scanOnly}
                  disabled={!botStatus?.mt5_connected && !connected}
                  title="Scanner les signaux sans executer"
                  className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed">
                  <Eye className="w-4 h-4" /> Scan
                </button>
                <button onClick={startBot}
                  disabled={!botStatus?.mt5_connected && !connected}
                  title="Demarrer le bot Fusion Markets MT5"
                  className="flex items-center gap-2 px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 transition text-sm font-medium disabled:opacity-50 disabled:cursor-not-allowed">
                  <Play className="w-4 h-4" /> Demarrer
                </button>
              </>
            )}
            <button onClick={() => setConfirmEmergency(true)}
              title="Urgence — fermer toutes les positions"
              className="flex items-center gap-2 px-3 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 transition text-sm">
              <AlertTriangle className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Backend global status */}
        <div className="flex items-center gap-3 px-3 py-2 text-xs text-gray-500">
          {connected ? (
            <><Wifi className="w-3.5 h-3.5 text-green-600" /> Backend connecte</>
          ) : (
            <><WifiOff className="w-3.5 h-3.5 text-red-500" /> Reconnexion en cours...</>
          )}
        </div>
      </div>

      {/* Emergency Stop Confirmation */}
      {confirmEmergency && (
        <div className="bg-red-50 border-2 border-red-300 rounded-xl p-4">
          <div className="flex items-center gap-2 mb-2">
            <AlertTriangle className="w-5 h-5 text-red-600" />
            <span className="font-bold text-red-800">Confirmer l'arret d'urgence ?</span>
          </div>
          <p className="text-sm text-red-700 mb-3">
            Toutes les positions ouvertes seront fermees au prix du marche et tous les ordres seront annules.
          </p>
          <div className="flex gap-2">
            <button
              onClick={() => { emergencyStop(); setConfirmEmergency(false); }}
              className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
            >
              Confirmer l'arret d'urgence
            </button>
            <button
              onClick={() => setConfirmEmergency(false)}
              className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
            >
              Annuler
            </button>
          </div>
        </div>
      )}


      {/* Bot Configuration */}
      <div className="bg-white rounded-xl shadow-sm border">
        <button
          onClick={() => setShowConfig(!showConfig)}
          className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition"
        >
          <div className="flex items-center gap-2">
            <Settings className="w-5 h-5 text-gray-600" />
            <span className="font-medium text-gray-900">Configuration du Bot</span>
          </div>
          <span className="text-gray-400">{showConfig ? '▲' : '▼'}</span>
        </button>

        {showConfig && (
          <div className="border-t p-4">
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Max par ordre (EUR)
                </label>
                <input
                  type="number"
                  value={configDraft.max_order_size}
                  onChange={e => setConfigDraft(d => ({ ...d, max_order_size: parseFloat(e.target.value) || 0 }))}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  min={1}
                  max={100}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Risque max par trade (%)
                </label>
                <input
                  type="number"
                  value={configDraft.max_risk_per_trade}
                  onChange={e => setConfigDraft(d => ({ ...d, max_risk_per_trade: parseFloat(e.target.value) || 0 }))}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  min={0.5}
                  max={5}
                  step={0.5}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Perte max journaliere (%)
                </label>
                <input
                  type="number"
                  value={configDraft.max_daily_loss}
                  onChange={e => setConfigDraft(d => ({ ...d, max_daily_loss: parseFloat(e.target.value) || 0 }))}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  min={1}
                  max={10}
                  step={1}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Positions max ouvertes
                </label>
                <input
                  type="number"
                  value={configDraft.max_open_positions}
                  onChange={e => setConfigDraft(d => ({ ...d, max_open_positions: parseInt(e.target.value) || 1 }))}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  min={1}
                  max={10}
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">
                  Intervalle scan (secondes)
                </label>
                <input
                  type="number"
                  value={configDraft.scan_interval}
                  onChange={e => setConfigDraft(d => ({ ...d, scan_interval: parseInt(e.target.value) || 30 }))}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  min={10}
                  max={300}
                  step={10}
                />
              </div>
            </div>

            <div className="mt-4 flex gap-2">
              <button
                onClick={() => updateConfig({
                  max_order_size: configDraft.max_order_size,
                  max_risk_per_trade: configDraft.max_risk_per_trade / 100,
                  max_daily_loss: configDraft.max_daily_loss / 100,
                  max_open_positions: configDraft.max_open_positions,
                  scan_interval: configDraft.scan_interval,
                })}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm"
              >
                Sauvegarder
              </button>
              <button
                onClick={() => setShowConfig(false)}
                className="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300 text-sm"
              >
                Annuler
              </button>
            </div>
          </div>
        )}
      </div>


      {/* Manual Trading Panel */}
      <div className="bg-white rounded-xl shadow-sm border">
        <button
          onClick={() => setShowManualTrade(!showManualTrade)}
          className="w-full flex items-center justify-between p-4 hover:bg-gray-50 transition"
        >
          <div className="flex items-center gap-2">
            <Send className="w-5 h-5 text-purple-600" />
            <span className="font-medium text-gray-900">Trading Manuel</span>
          </div>
          <span className="text-gray-400">{showManualTrade ? '▲' : '▼'}</span>
        </button>

        {showManualTrade && (
          <div className="border-t p-4">
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-4">
              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Symbole</label>
                <select
                  value={manualSymbol}
                  onChange={e => setManualSymbol(e.target.value)}
                  className="w-full border rounded-lg px-3 py-2 text-sm bg-white"
                >
                  <optgroup label="Forex">
                    {TRADEABLE_SYMBOLS.filter(s => s.includes('/')).map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </optgroup>
                  <optgroup label="Indices">
                    {TRADEABLE_SYMBOLS.filter(s => ['DAX40', 'SP500', 'NASDAQ', 'CAC40', 'NKY', 'HK50', 'AUS200'].includes(s)).map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </optgroup>
                  <optgroup label="Matieres Premieres">
                    {TRADEABLE_SYMBOLS.filter(s => s.startsWith('OIL')).map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </optgroup>
                </select>
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Direction</label>
                <div className="flex gap-2">
                  <button
                    onClick={() => setManualAction('buy')}
                    className={`flex-1 flex items-center justify-center gap-1 px-3 py-2 rounded-lg text-sm font-medium transition ${
                      manualAction === 'buy'
                        ? 'bg-green-600 text-white'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    <ArrowUpRight className="w-3.5 h-3.5" /> BUY
                  </button>
                  <button
                    onClick={() => setManualAction('sell')}
                    className={`flex-1 flex items-center justify-center gap-1 px-3 py-2 rounded-lg text-sm font-medium transition ${
                      manualAction === 'sell'
                        ? 'bg-red-600 text-white'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                    }`}
                  >
                    <ArrowDownRight className="w-3.5 h-3.5" /> SELL
                  </button>
                </div>
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-600 mb-1">Montant (EUR)</label>
                <input
                  type="number"
                  value={manualAmount}
                  onChange={e => setManualAmount(parseFloat(e.target.value) || 0)}
                  className="w-full border rounded-lg px-3 py-2 text-sm"
                  min={1}
                  max={100}
                  step={5}
                />
              </div>

              <div className="flex items-end">
                <button
                  onClick={handleManualOrder}
                  disabled={manualLoading || manualAmount <= 0}
                  className={`w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition ${
                    manualAction === 'buy'
                      ? 'bg-green-600 text-white hover:bg-green-700'
                      : 'bg-red-600 text-white hover:bg-red-700'
                  } disabled:opacity-50 disabled:cursor-not-allowed`}
                >
                  {manualLoading ? (
                    <RefreshCw className="w-4 h-4 animate-spin" />
                  ) : (
                    <Send className="w-4 h-4" />
                  )}
                  {manualLoading ? 'Envoi...' : `${manualAction.toUpperCase()} ${manualSymbol}`}
                </button>
              </div>
            </div>

            {manualResult && (
              <div className={`flex items-center gap-2 p-3 rounded-lg text-sm ${
                manualResult.success
                  ? 'bg-green-50 text-green-700 border border-green-200'
                  : 'bg-red-50 text-red-700 border border-red-200'
              }`}>
                {manualResult.success ? (
                  <CheckCircle className="w-4 h-4 flex-shrink-0" />
                ) : (
                  <XCircle className="w-4 h-4 flex-shrink-0" />
                )}
                <span className="flex-1">{manualResult.message}</span>
                <button onClick={() => setManualResult(null)} className="p-0.5 hover:bg-black/10 rounded">
                  <X className="w-3.5 h-3.5" />
                </button>
              </div>
            )}

            <div className="mt-3 text-xs text-gray-500">
              Les ordres manuels utilisent les memes garde-fous que le bot (stop-loss, take-profit, trailing stops).
              Max {formatCurrency(20)} par ordre.
            </div>
          </div>
        )}
      </div>

      {/* Activity Log */}
      <div className="bg-white rounded-xl shadow-sm border">
        <div className="flex items-center justify-between p-4 border-b">
          <h3 className="flex items-center gap-2 font-medium text-gray-900">
            <Activity className="w-5 h-5 text-blue-600" /> Journal d'Activite
          </h3>
          <span className="text-xs text-gray-500">{logs.length} evenements</span>
        </div>
        <div className="max-h-64 overflow-y-auto p-4 space-y-1">
          {logs.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-4">
              Aucune activite pour le moment
            </p>
          ) : (
            logs.map((log, i) => (
              <div key={i} className={`text-xs font-mono py-1 px-2 rounded ${
                log.includes('URGENCE') || log.includes('CRITICAL') ? 'bg-red-50 text-red-700' :
                log.includes('Ordre') || log.includes('Trade') ? 'bg-green-50 text-green-700' :
                log.includes('erreur') || log.includes('ERROR') ? 'bg-orange-50 text-orange-700' :
                'text-gray-600'
              }`}>
                {log}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
