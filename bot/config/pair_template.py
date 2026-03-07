"""
PairTemplateManager — auto-generates BotConfig for a new trading pair.

Uses ATR-based heuristics to set reasonable Grid bounds and DCA thresholds,
taking a first bot's config as the base template for exchange credentials
and other shared settings.

Usage::

    manager = PairTemplateManager()
    config = await manager.create_config(
        symbol="ETH/USDT",
        strategy="hybrid",
        exchange_client=client,
        base_config=existing_bot_config,
    )
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

# ATR-to-grid multipliers (how wide to make each grid level relative to ATR)
_GRID_ATR_MULTIPLIER = Decimal("2.0")
# Minimum number of grid levels
_MIN_GRID_LEVELS = 5
# Default grid levels when we don't have ATR data
_DEFAULT_GRID_LEVELS = 10
# DCA trigger as multiple of ATR (relative to current price)
_DCA_ATR_TRIGGER_MULT = Decimal("1.5")
# DCA take-profit as multiple of ATR
_DCA_ATR_TP_MULT = Decimal("3.0")


def _round_price(price: Decimal, sig_digits: int = 4) -> Decimal:
    """Round price to a reasonable number of significant digits."""
    if price <= 0:
        return price
    from decimal import ROUND_HALF_UP

    magnitude = price.log10().to_integral_value(rounding=ROUND_HALF_UP)
    quantum = Decimal("10") ** (int(magnitude) - sig_digits + 1)
    return price.quantize(quantum)


class PairTemplateManager:
    """
    Generates BotConfig instances for new pairs using ATR-based heuristics.

    The generated config is always a valid BotConfig — grid bounds are
    derived from recent ATR, and DCA thresholds are set to sensible
    multiples of daily volatility.
    """

    async def create_config(
        self,
        symbol: str,
        strategy: str,
        exchange_client: Any,
        base_config: Any,  # BotConfig — avoid circular import
        ohlcv_timeframe: str = "1h",
        ohlcv_limit: int = 200,
        allocation_per_bot: Decimal | None = None,
    ) -> Any:
        """
        Build a complete BotConfig for *symbol* derived from *base_config*.

        ATR is fetched via *exchange_client* on *ohlcv_timeframe*.
        If the fetch fails, conservative defaults are used.

        Args:
            symbol:            Trading pair, e.g. "ETH/USDT".
            strategy:          Strategy type string ("grid", "dca", "hybrid", …).
            exchange_client:   Initialised exchange client (must support fetch_ohlcv).
            base_config:       Existing BotConfig used as credential/exchange template.
            ohlcv_timeframe:   Timeframe for ATR calculation.
            ohlcv_limit:       Number of candles to fetch.
            allocation_per_bot: Capital to allocate to this bot (optional).

        Returns:
            A new BotConfig instance.
        """
        # Lazy import to avoid circular dependency at module load time
        from bot.config.schemas import (
            BotConfig,
            ExchangeConfig,
            RiskManagementConfig,
            StrategyType,
            TrendFollowerConfig,
        )

        # Fetch current price and ATR
        current_price, atr = await self._fetch_atr(
            exchange_client, symbol, ohlcv_timeframe, ohlcv_limit
        )

        # Build strategy-specific sub-configs
        grid_cfg = None
        dca_cfg = None
        tf_cfg = None

        if strategy in ("grid", "hybrid"):
            grid_cfg = self._make_grid_config(current_price, atr, allocation_per_bot)

        if strategy in ("dca", "hybrid"):
            dca_cfg = self._make_dca_config(current_price, atr, allocation_per_bot)

        if strategy == "trend_follower":
            tf_cfg = TrendFollowerConfig()

        # Derive name
        safe_symbol = symbol.replace("/", "_")
        bot_name = f"auto_{safe_symbol}_{strategy}"

        # Allocation for risk management
        alloc = allocation_per_bot or Decimal("1000")
        risk_cfg = RiskManagementConfig(
            max_position_size=alloc * Decimal("2"),
            min_order_size=Decimal("10"),
            stop_loss_percentage=Decimal("0.15"),
        )

        return BotConfig(
            name=bot_name,
            symbol=symbol,
            strategy=StrategyType(strategy),
            exchange=ExchangeConfig(
                exchange_id=base_config.exchange.exchange_id,
                credentials_name=base_config.exchange.credentials_name,
                sandbox=base_config.exchange.sandbox,
            ),
            grid=grid_cfg,
            dca=dca_cfg,
            trend_follower=tf_cfg,
            risk_management=risk_cfg,
            dry_run=base_config.dry_run,
            auto_start=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_atr(
        self,
        exchange_client: Any,
        symbol: str,
        timeframe: str,
        limit: int,
    ) -> tuple[Decimal, Decimal]:
        """
        Fetch OHLCV data and compute ATR14 + last close price.

        Falls back to (price=1000, atr=20) if anything fails.
        """
        try:
            ohlcv = await exchange_client.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not ohlcv or len(ohlcv) < 15:
                return Decimal("1000"), Decimal("20")

            closes = [Decimal(str(c[4])) for c in ohlcv]
            highs = [Decimal(str(c[2])) for c in ohlcv]
            lows = [Decimal(str(c[3])) for c in ohlcv]

            current_price = closes[-1]

            # True Range = max(high-low, |high-prev_close|, |low-prev_close|)
            true_ranges = []
            for i in range(1, len(closes)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                true_ranges.append(tr)

            period = min(14, len(true_ranges))
            atr: Decimal = sum(true_ranges[-period:], Decimal("0")) / period

            logger.info(
                "atr_computed symbol=%s current_price=%s atr=%s atr_pct=%s",
                symbol,
                float(current_price),
                float(atr),
                float(atr / current_price * 100),
            )
            return current_price, atr

        except Exception as e:
            logger.warning("Failed to fetch ATR for %s: %s — using defaults", symbol, e)
            return Decimal("1000"), Decimal("20")

    def _make_grid_config(
        self,
        price: Decimal,
        atr: Decimal,
        allocation: Decimal | None,
    ) -> Any:
        """Build GridConfig from ATR-derived bounds."""
        from bot.config.schemas import GridConfig

        # Grid spans ±N * ATR around current price
        half_span = atr * _GRID_ATR_MULTIPLIER * Decimal("5")
        upper = _round_price(price + half_span)
        lower = _round_price(max(price - half_span, price * Decimal("0.5")))

        # Grid levels: span / ATR * multiplier, clamped to reasonable range
        if atr > 0:
            levels = int((upper - lower) / (atr * _GRID_ATR_MULTIPLIER))
        else:
            levels = _DEFAULT_GRID_LEVELS
        levels = max(_MIN_GRID_LEVELS, min(levels, 50))

        # Amount per grid: distribute allocation across levels
        alloc = allocation or Decimal("1000")
        amount_per_grid = _round_price(alloc / levels / price * Decimal("10"))
        amount_per_grid = max(amount_per_grid, Decimal("10"))  # min $10 per grid

        return GridConfig(
            enabled=True,
            upper_price=upper,
            lower_price=lower,
            grid_levels=levels,
            amount_per_grid=amount_per_grid,
            profit_per_grid=Decimal("0.005"),  # 0.5% per level
        )

    def _make_dca_config(
        self,
        price: Decimal,
        atr: Decimal,
        allocation: Decimal | None,
    ) -> Any:
        """Build DCAConfig from ATR-derived thresholds."""
        from bot.config.schemas import DCAConfig

        # Trigger when price drops by 1.5x ATR
        trigger_pct = (
            (atr * _DCA_ATR_TRIGGER_MULT / price).quantize(Decimal("0.001"))
            if price > 0
            else Decimal("0.05")
        )
        trigger_pct = max(Decimal("0.02"), min(trigger_pct, Decimal("0.15")))

        # Take-profit at 3x ATR recovery
        tp_pct = (
            (atr * _DCA_ATR_TP_MULT / price).quantize(Decimal("0.001"))
            if price > 0
            else Decimal("0.10")
        )
        tp_pct = max(Decimal("0.03"), min(tp_pct, Decimal("0.30")))

        alloc = allocation or Decimal("1000")
        amount_per_step = _round_price(alloc / Decimal("5"))
        amount_per_step = max(amount_per_step, Decimal("50"))

        return DCAConfig(
            enabled=True,
            trigger_percentage=trigger_pct,
            amount_per_step=amount_per_step,
            max_steps=5,
            take_profit_percentage=tp_pct,
        )
