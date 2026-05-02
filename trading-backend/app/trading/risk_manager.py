"""
Risk management module — enforces all risk constraints before trade execution.
Every order must pass through check_trade() before submission.
Supports ESMA leverage and DYNAMIC per-market capital allocation based on signals.
"""
from dataclasses import dataclass
from typing import Optional, Literal
from app.config import settings
from app.trading.signals import Signal
import math
from app.trading.symbol_mapper import get_leverage, get_market_for_symbol, get_min_quantity
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class TradeDecision:
    approved: bool
    reason: str
    quantity: float = 0.0
    position_size_eur: float = 0.0
    risk_eur: float = 0.0


class DynamicAllocator:
    """
    FIXED allocation per user requirement:
      - FOREX     : 60%
      - INDICES   : 30%
      - COMMODITY : 10%
      - STOCKS    :  0% (disabled)

    Aucune dépendance aux signaux : l'allocation reste constante quelle que soit
    la qualité du scan. Lors des heures de fermeture EU/US, la part indices est
    reportée vers le forex (qui trade 24h/5j) pour ne pas geler du capital.
    """

    # Allocation fixe — règle utilisateur 60% forex / 40% indices et autres
    FIXED_ALLOCATION = {
        "FOREX": 0.60,
        "INDICES": 0.30,
        "COMMODITY": 0.10,
        "STOCKS": 0.0,
    }
    # Allocation hors heures EU/US — indices fermés → reportés vers forex
    OFFHOURS_ALLOCATION = {
        "FOREX": 0.90,
        "INDICES": 0.0,
        "COMMODITY": 0.10,
        "STOCKS": 0.0,
    }

    def __init__(self):
        self._current: dict[str, float] = dict(self.FIXED_ALLOCATION)
        self._signal_scores: dict[str, float] = {}

    @property
    def current_allocations(self) -> dict[str, float]:
        return dict(self._current)

    def update_from_signals(self, signals: list[dict]):
        """
        Allocation FIXE — les signaux ne changent pas la répartition.
        Seul le statut des marchés EU/US bascule entre allocation normale et off-hours.
        """
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        hour_utc = now_utc.hour
        weekday = now_utc.weekday()  # 0=Monday

        # Markets closed: weekends OR after US close (21:00 UTC) and before EU open (07:00 UTC)
        eu_us_closed = weekday >= 5 or hour_utc >= 21 or hour_utc < 7

        if eu_us_closed:
            self._current = dict(self.OFFHOURS_ALLOCATION)
            session = "OFF-HOURS" if weekday < 5 else "WEEKEND"
            logger.info(f"Allocation FIXE [{session}]: EU/US fermés → {self._format_alloc()}")
        else:
            self._current = dict(self.FIXED_ALLOCATION)
            logger.info(f"Allocation FIXE: {self._format_alloc()}")

    def get_allocation(self, market_category: str) -> float:
        """Return fixed allocation for a market category."""
        return self._current.get(market_category, 0.0)

    def _format_alloc(self) -> str:
        return (
            f"FX={self._current.get('FOREX', 0):.0%} "
            f"STK={self._current.get('STOCKS', 0):.0%} "
            f"IDX={self._current.get('INDICES', 0):.0%} "
            f"CMD={self._current.get('COMMODITY', 0):.0%}"
        )


class RiskManager:
    def __init__(self):
        self.max_order_size = settings.max_order_size  # EUR 20
        self.max_risk_per_trade = settings.max_risk_per_trade  # 3%
        self.max_daily_loss = settings.max_daily_loss  # fallback % capital
        self.max_daily_loss_eur = getattr(settings, "max_daily_loss_eur", 0.0)  # plafond fixe (prioritaire si >0)
        self.max_open_positions = settings.max_open_positions  # 5
        self.min_risk_reward = 1.0  # 1:1 R:R — SL=TP symétrique, trailing stop étend les gagnants
        self.circuit_breaker_count = 3  # Consecutive losses -> pause
        self.circuit_breaker_pause_minutes = 30

        # Dynamic allocation engine
        self.allocator = DynamicAllocator()

        # Runtime state
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._circuit_breaker_active = False
        self._open_position_count = 0
        self._open_symbols: list[str] = []  # List to allow counting duplicates
        self._capital = 0.0
        # 2026-04-21: balance broker au début de la journée (source de vérité daily_loss)
        self._day_start_balance: Optional[float] = None

    @property
    def capital(self) -> float:
        return self._capital

    @capital.setter
    def capital(self, value: float):
        self._capital = value

    def update_state(
        self,
        daily_pnl: float,
        open_positions: int,
        open_symbols: set[str],
        capital: float,
    ):
        self._daily_pnl = daily_pnl
        self._open_position_count = open_positions
        self._open_symbols = list(open_symbols) if open_symbols else []
        self._capital = capital

    def record_trade_result(self, pnl: float):
        self._daily_pnl += pnl
        if pnl < 0:
            self._consecutive_losses += 1
            # Circuit breaker DISABLED — loss guard (-50€) protects instead
            logger.info(f"[RISK] Consecutive losses: {self._consecutive_losses} (circuit breaker disabled, loss guard active)")
        else:
            self._consecutive_losses = 0

    def reset_daily(self):
        self._daily_pnl = 0.0
        self._consecutive_losses = 0
        self._circuit_breaker_active = False

    def _get_market_allocation(self, market_category: str) -> float:
        """Return DYNAMIC capital allocation ratio for a market category."""
        return self.allocator.get_allocation(market_category)

    def check_trade(self, signal: Signal, symbol: str, broker_capital: float = 0) -> TradeDecision:
        """
        Validate a trade against all risk rules with ESMA leverage.
        Returns APPROVE with quantity or REJECT with reason.

        broker_capital: actual available margin on the executing broker
                        (0 = use global capital, for IBKR compatibility)
        """

        # 1. Circuit breaker DISABLED — loss guard (-50€) protects instead
        # if self._circuit_breaker_active:
        #     return TradeDecision(approved=False, reason="circuit breaker")

        # 2. Daily loss limit — 2026-04-21 FIX: source broker direct, plus fiable que DB
        # Le compteur interne _daily_pnl peut être désynchronisé (bugs DB, exits non enregistrés).
        # On utilise AUSSI la vérité broker : _day_start_balance - current_balance.
        # Si l'un des deux (interne OU broker) dépasse la limite → bloquer.
        # 2026-04-24: plafond fixe en EUR prioritaire sur le % capital
        _pct_limit = self._capital * self.max_daily_loss
        daily_loss_limit = self.max_daily_loss_eur if self.max_daily_loss_eur > 0 else _pct_limit
        # A. Garde-fou interne (comme avant)
        if self._daily_pnl <= -daily_loss_limit:
            return TradeDecision(
                approved=False,
                reason=f"Limite perte journaliere (interne) atteinte: {self._daily_pnl:.2f}EUR (max -{daily_loss_limit:.2f}EUR)",
            )
        # B. Garde-fou broker (nouveau 2026-04-21) — INDÉPENDANT de la DB
        _day_start = getattr(self, "_day_start_balance", None)
        if _day_start and _day_start > 0:
            _broker_delta = self._capital - _day_start
            if _broker_delta <= -daily_loss_limit:
                return TradeDecision(
                    approved=False,
                    reason=f"Limite perte journaliere (broker) atteinte: {_broker_delta:+.2f}EUR (start {_day_start:.2f} → now {self._capital:.2f}, max -{daily_loss_limit:.2f}EUR)",
                )

        # 3. Max open positions
        if self._open_position_count >= self.max_open_positions:
            return TradeDecision(
                approved=False,
                reason=f"Nombre max de positions ouvertes atteint: {self._open_position_count}/{self.max_open_positions}",
            )

        # 4. Max 2 positions per canonical symbol (CL=F==XTIUSD, AUD/NZD==AUDNZD)
        from app.trading.bot import TradingBot
        _canon = TradingBot._canonical_symbol(symbol)
        _sym_count = sum(1 for s in self._open_symbols if TradingBot._canonical_symbol(s) == _canon)
        if _sym_count >= 2:
            return TradeDecision(
                approved=False,
                reason=f"Max 2 positions atteint sur {symbol} ({_canon})",
            )

        # 5. Signal must be buy or sell (not hold)
        if signal.signal == "hold":
            return TradeDecision(approved=False, reason="Signal HOLD - pas de trade")

        # 6. Calculate position size WITH LEVERAGE
        entry = signal.suggested_entry
        sl = signal.suggested_sl
        tp = signal.suggested_tp

        if entry <= 0:
            return TradeDecision(approved=False, reason="Prix d'entree invalide")

        risk_per_unit_raw = abs(entry - sl)
        if risk_per_unit_raw <= 0:
            return TradeDecision(approved=False, reason="Stop-loss identique au prix d'entree")

        # Convert risk_per_unit to EUR for proper position sizing
        # For forex: risk_per_unit is in QUOTE currency
        quote_ccy = symbol[4:7] if "/" in symbol and len(symbol) >= 7 else ""
        QUOTE_TO_EUR = {
            "USD": 0.88,   # 1 USD ≈ 0.88 EUR
            "GBP": 1.17,   # 1 GBP ≈ 1.17 EUR
            "EUR": 1.0,
            "JPY": 0.0055, # 1 JPY ≈ 0.0055 EUR
            "CHF": 1.05,   # 1 CHF ≈ 1.05 EUR
            "CAD": 0.65,   # 1 CAD ≈ 0.65 EUR
            "AUD": 0.62,
            "NZD": 0.51,
        }
        quote_to_eur = QUOTE_TO_EUR.get(quote_ccy, 1.0)
        # For indices: determine currency from symbol
        if not quote_ccy:
            _sym_upper = symbol.upper()
            if _sym_upper in ("SP500", "NASDAQ", "DJ30"):
                quote_to_eur = 0.88  # USD-denominated indices
            elif _sym_upper in ("UK100", "FTSE100"):
                quote_to_eur = 1.17  # GBP-denominated
            elif _sym_upper in ("NKY",):
                quote_to_eur = 0.0055  # JPY-denominated
            elif _sym_upper in ("HK50",):
                quote_to_eur = 0.11  # HKD-denominated (1 HKD ≈ 0.11 EUR)
            elif _sym_upper in ("AUS200",):
                quote_to_eur = 0.62  # AUD-denominated
            # DAX40, CAC40, EUSTX50 → EUR = 1.0 (default)
        risk_per_unit = risk_per_unit_raw * quote_to_eur  # Now in EUR

        # Get leverage and allocation for this symbol
        leverage = get_leverage(symbol)
        market_category = get_market_for_symbol(symbol)
        allocation = self._get_market_allocation(market_category)

        # Use broker-specific capital if provided (e.g. Capital.com balance)
        effective_capital = broker_capital if broker_capital > 0 else self._capital

        # Max margin (collateral) for this market = effective capital × allocation ratio
        # Then cap by max_order_size and by actual broker balance
        max_margin_for_market = effective_capital * allocation
        margin_per_trade = min(self.max_order_size, max_margin_for_market)

        # Indices/commodities: buffer 0.85 RETIRÉ 2026-04-17
        # Fusion Markets MT5 n'a pas le problème NOT_ENOUGH_MONEY de IC Markets ESMA.
        # On peut utiliser 100% de l'allocation indices pour maximiser les gains.
        # if market_category in ("INDICES", "COMMODITY"):
        #     margin_per_trade = margin_per_trade * 0.85

        # If broker_capital is specified, never exceed 80% of available funds (safety buffer)
        if broker_capital > 0:
            margin_per_trade = min(margin_per_trade, broker_capital * 0.80)

        # ── DEBUG: trace position sizing ──
        logger.info(
            f"[SIZE DEBUG] {symbol}: broker_capital={broker_capital:.2f} "
            f"self._capital={self._capital:.2f} effective_capital={effective_capital:.2f} "
            f"allocation={allocation:.4f} ({market_category}) "
            f"max_order_size={self.max_order_size:.2f} "
            f"max_margin_for_market={max_margin_for_market:.2f} "
            f"margin_per_trade={margin_per_trade:.2f} "
            f"broker_cap*0.80={broker_capital*0.80:.2f} "
            f"leverage={leverage}"
        )

        # Real position size = margin × leverage
        # e.g. 20€ margin × 30:1 leverage = 600€ real position
        real_position_eur = margin_per_trade * leverage

        # Max risk in EUR — 2026-04-21: MONTANT FIXE prioritaire (15€/trade demande user)
        from app.trading.symbol_mapper import ASSET_BY_SYMBOL as _ASSET_MAP
        _ai = _ASSET_MAP.get(symbol)
        _at = _ai.asset_type if _ai else "forex"
        # Priorité 1: montant fixe en € (si configuré > 0)
        _fixed_risk = getattr(settings, "max_risk_per_trade_eur", 0)
        if _fixed_risk and _fixed_risk > 0:
            max_risk_eur = _fixed_risk
            logger.info(f"[RISK] {symbol}: risque FIXE {_fixed_risk:.1f}€ — R:R appliqué = config per-paire (signals.py)")
        else:
            # Fallback: % du capital
            max_risk_eur = self._capital * self.max_risk_per_trade
            if _at in ("index_cfd", "commodity"):
                max_risk_eur = min(max_risk_eur, 24.0)
            logger.info(f"[RISK] {symbol}: {_at} % capital → max risk {max_risk_eur:.1f}€")

        # Quantity based on risk management (protect capital)
        if risk_per_unit <= 0:
            return TradeDecision(approved=False, reason=f"risk_per_unit <= 0 apres conversion EUR pour {symbol}")
        quantity_from_risk = max_risk_eur / risk_per_unit

        # Quantity based on leveraged position size
        # For forex: must convert EUR position to base currency units
        # 1 unit of EUR/xxx = 1 EUR, 1 unit of USD/xxx = 1 USD, etc.
        base_ccy = symbol[:3] if "/" in symbol else ""
        # Approximate base currency value in EUR (updated periodically would be ideal)
        BASE_EUR_APPROX = {
            "EUR": 1.0,
            "USD": 0.88,   # 1 USD ≈ 0.88 EUR
            "GBP": 1.17,   # 1 GBP ≈ 1.17 EUR
            "AUD": 0.62,   # 1 AUD ≈ 0.62 EUR
            "NZD": 0.51,   # 1 NZD ≈ 0.51 EUR
            "CHF": 1.05,   # 1 CHF ≈ 1.05 EUR
            "CAD": 0.65,   # 1 CAD ≈ 0.65 EUR
            "JPY": 0.0055, # 1 JPY ≈ 0.0055 EUR
        }
        base_value_eur = BASE_EUR_APPROX.get(base_ccy, 1.0)
        # qty = position_eur / unit_cost_eur
        if _at in ("index_cfd", "stock", "commodity"):
            # For indices/stocks: 1 contract = entry price in EUR
            # e.g. DAX40 @ 18000 → 1 contract = 18000€ exposure
            quantity_from_leverage = real_position_eur / entry if entry > 0 else 0
        else:
            quantity_from_leverage = real_position_eur / base_value_eur if base_value_eur > 0 else real_position_eur / entry

        # ── DEBUG: trace quantity calculation ──
        logger.info(
            f"[SIZE DEBUG] {symbol}: real_position_eur={real_position_eur:.2f} "
            f"base_value_eur={base_value_eur:.4f} "
            f"risk_per_unit={risk_per_unit:.8f} risk_per_unit_raw={risk_per_unit_raw:.6f} "
            f"quote_to_eur={quote_to_eur:.4f} "
            f"max_risk_eur={max_risk_eur:.2f} "
            f"qty_from_risk={quantity_from_risk:.2f} qty_from_leverage={quantity_from_leverage:.2f} "
            f"LIMITING={'RISK' if quantity_from_risk < quantity_from_leverage else 'LEVERAGE'}"
        )

        # Take the smallest to stay within both risk and leverage limits
        quantity = min(quantity_from_risk, quantity_from_leverage)

        # Round based on asset type
        from app.trading.symbol_mapper import ASSET_BY_SYMBOL
        _asset_info = ASSET_BY_SYMBOL.get(symbol)
        _asset_type = _asset_info.asset_type if _asset_info else "forex"
        min_qty = get_min_quantity(symbol)

        if _asset_type in ("index_cfd", "stock"):
            # CFD: MT5 minimum lot size applies (volume_min per symbol)
            # quantity is in UNITS (not lots) — the MT5 client converts.
            # e.g. SP500: qty=1 unit = $6603 exposure
            quantity = math.floor(quantity * 100) / 100  # Round to 0.01
            min_qty = 0.01  # MT5: 0.01 unit minimum (converted to lots by client)
            if quantity < min_qty:
                quantity = min_qty
        elif min_qty < 1:
            # Fractional CFD — round to 1 decimal
            quantity = math.floor(quantity * 10) / 10
        else:
            # Forex — floor to integer (minimum 1000 units)
            quantity = math.floor(quantity)

        if quantity < min_qty:
            return TradeDecision(
                approved=False,
                reason=f"Quantite {quantity} < minimum {min_qty} pour {symbol} (capital insuffisant pour cet instrument)",
            )

        # Position size in EUR: real notional exposure
        if _at in ("index_cfd", "stock", "commodity"):
            # For indices: qty is in contracts, notional = qty × price
            position_size_eur = quantity * entry if entry > 0 else quantity
        else:
            # For forex: qty is in base currency units
            position_size_eur = quantity * base_value_eur
        actual_risk_eur = quantity * risk_per_unit  # Real risk in EUR

        # 7. Risk/reward ratio check
        reward_per_unit = abs(tp - entry)
        if risk_per_unit > 0:
            rr_ratio = reward_per_unit / risk_per_unit
            if rr_ratio < self.min_risk_reward:
                return TradeDecision(
                    approved=False,
                    reason=f"Ratio risque/recompense insuffisant: {rr_ratio:.1f}:1 (min {self.min_risk_reward}:1)",
                )

        # 8. Ensure risk doesn't exceed max risk per trade
        if actual_risk_eur > max_risk_eur:
            q = max_risk_eur / risk_per_unit
            quantity = math.floor(q * 10) / 10 if min_qty < 1 else math.floor(q)
            if quantity < min_qty:
                return TradeDecision(
                    approved=False,
                    reason=f"Quantite {quantity} < minimum {min_qty} apres ajustement risque pour {symbol}",
                )
            if _at in ("index_cfd", "stock", "commodity"):
                position_size_eur = quantity * entry if entry > 0 else quantity
            else:
                position_size_eur = quantity * base_value_eur
            actual_risk_eur = quantity * risk_per_unit

        logger.info(
            f"Trade approuve: {symbol} {signal.signal.upper()} "
            f"qty={quantity:.4f} position={position_size_eur:.2f}EUR "
            f"marge={margin_per_trade:.2f}EUR levier={leverage}:1 "
            f"risk={actual_risk_eur:.2f}EUR SL={sl:.4f} TP={tp:.4f}"
        )

        return TradeDecision(
            approved=True,
            reason=f"Trade approuve (levier {leverage}:1, marge {margin_per_trade:.2f}EUR)",
            quantity=quantity,
            position_size_eur=position_size_eur,
            risk_eur=actual_risk_eur,
        )

    def should_emergency_stop(self) -> bool:
        _pct_limit = self._capital * self.max_daily_loss
        daily_loss_limit = self.max_daily_loss_eur if self.max_daily_loss_eur > 0 else _pct_limit
        return self._daily_pnl <= -daily_loss_limit

    def compound_capital(self, base_capital: float, realized_pnl_today: float) -> float:
        """
        Next day's capital = base + gains (only compound gains, never go below base).
        """
        return max(base_capital, base_capital + realized_pnl_today)
