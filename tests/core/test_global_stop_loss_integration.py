"""
Integration tests for per-symbol global stop-loss aggregation.

Covers the scenario from issue #358:
  DCA ($3000) + Grid ($900) + TF ($1000) = $4900 total exposure on BTC/USDT.

Tests verify:
- Correct aggregation across DCAAdapter, GridAdapter, TrendFollowerAdapter
- force_close_all is triggered when aggregate risk exceeds global_stop_loss_pct
- Cooldown blocks new entries after force_close_all
- Cross-symbol isolation: ETH positions do not affect BTC stop-loss
- Unit tests for each adapter's get_open_positions() method
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from bot.core.portfolio_risk_manager import PortfolioRiskManager, RiskCheckStatus
from bot.strategies.dca_adapter import DCAAdapter
from bot.strategies.grid_adapter import GridAdapter
from bot.strategies.smc_adapter import SMCStrategyAdapter
from bot.strategies.base import SignalDirection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_prm(
    total: float = 10000.0,
    global_stop_pct: float = 0.025,
    cooldown: int = 0,  # 0 = instant expiry for most tests
) -> PortfolioRiskManager:
    return PortfolioRiskManager(
        total_capital=Decimal(str(total)),
        global_stop_loss_pct=global_stop_pct,
        force_close_cooldown_seconds=cooldown,
    )


# ---------------------------------------------------------------------------
# DCAAdapter.get_open_positions()
# ---------------------------------------------------------------------------


class TestDCAAdapterGetOpenPositions:
    def test_no_positions_returns_empty(self) -> None:
        adapter = DCAAdapter(symbol="BTC/USDT")
        assert adapter.get_open_positions() == []

    def test_single_position_correct_fields(self) -> None:
        adapter = DCAAdapter(
            symbol="BTC/USDT",
            base_order_size=Decimal("100"),
            take_profit_pct=Decimal("0.08"),
        )
        # Manually inject a position
        adapter._positions["pos1"] = {
            "direction": SignalDirection.LONG,
            "entry_price": Decimal("50000"),
            "avg_price": Decimal("50000"),
            "stop_loss": Decimal("47500"),   # 5% below entry
            "take_profit": Decimal("54000"),
            "size": Decimal("0.01"),
            "total_invested": Decimal("500"),
            "safety_orders_filled": 0,
            "entry_time": None,
            "current_price": Decimal("50000"),
        }
        positions = adapter.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "BTC/USDT"
        assert pos["size"] == Decimal("500")
        # stop distance: |50000 - 47500| / 50000 = 0.05
        assert abs(float(pos["atr_stop_distance"]) - 0.05) < 1e-10

    def test_symbol_field_propagated(self) -> None:
        adapter = DCAAdapter(symbol="ETH/USDT")
        adapter._positions["p1"] = {
            "direction": SignalDirection.LONG,
            "entry_price": Decimal("2000"),
            "avg_price": Decimal("2000"),
            "stop_loss": Decimal("1900"),
            "take_profit": Decimal("2200"),
            "size": Decimal("1"),
            "total_invested": Decimal("2000"),
            "safety_orders_filled": 0,
            "entry_time": None,
            "current_price": Decimal("2000"),
        }
        pos = adapter.get_open_positions()[0]
        assert pos["symbol"] == "ETH/USDT"


# ---------------------------------------------------------------------------
# GridAdapter.get_open_positions()
# ---------------------------------------------------------------------------


class TestGridAdapterGetOpenPositions:
    def test_no_positions_returns_empty(self) -> None:
        adapter = GridAdapter(symbol="BTC/USDT")
        assert adapter.get_open_positions() == []

    def test_single_position_correct_fields(self) -> None:
        adapter = GridAdapter(symbol="BTC/USDT", grid_range_pct=Decimal("0.05"))
        adapter._positions["p1"] = {
            "direction": SignalDirection.LONG,
            "entry_price": Decimal("50000"),
            "stop_loss": Decimal("47500"),
            "take_profit": Decimal("51000"),
            "size": Decimal("0.01"),       # 0.01 BTC
            "entry_time": None,
            "current_price": Decimal("50000"),
        }
        positions = adapter.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "BTC/USDT"
        # size = 0.01 BTC × 50000 = 500
        assert pos["size"] == Decimal("500")
        # stop distance: |50000 - 47500| / 50000 = 0.05
        assert abs(float(pos["atr_stop_distance"]) - 0.05) < 1e-10

    def test_symbol_field_propagated(self) -> None:
        adapter = GridAdapter(symbol="SOL/USDT")
        adapter._positions["p1"] = {
            "direction": SignalDirection.LONG,
            "entry_price": Decimal("100"),
            "stop_loss": Decimal("90"),
            "take_profit": Decimal("110"),
            "size": Decimal("10"),
            "entry_time": None,
            "current_price": Decimal("100"),
        }
        pos = adapter.get_open_positions()[0]
        assert pos["symbol"] == "SOL/USDT"


# ---------------------------------------------------------------------------
# SMCStrategyAdapter.get_open_positions()
# ---------------------------------------------------------------------------


class TestSMCAdapterGetOpenPositions:
    def _make_smc_adapter(self, symbol: str = "BTC/USDT") -> SMCStrategyAdapter:
        from bot.strategies.smc.config import SMCConfig
        config = SMCConfig()
        config.symbol = symbol  # type: ignore[attr-defined]
        return SMCStrategyAdapter(config=config, name="test-smc")

    def test_no_positions_returns_empty(self) -> None:
        adapter = self._make_smc_adapter()
        assert adapter.get_open_positions() == []

    def test_single_position_correct_fields(self) -> None:
        adapter = self._make_smc_adapter(symbol="BTC/USDT")
        adapter._positions["p1"] = {
            "direction": SignalDirection.LONG,
            "entry_price": Decimal("50000"),
            "stop_loss": Decimal("49000"),
            "take_profit": Decimal("52000"),
            "size": Decimal("200"),
            "entry_time": None,
            "current_price": Decimal("50000"),
            "signal": None,
        }
        positions = adapter.get_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "BTC/USDT"
        assert pos["size"] == Decimal("200")
        # |50000 - 49000| / 50000 = 0.02
        assert abs(float(pos["atr_stop_distance"]) - 0.02) < 1e-10


# ---------------------------------------------------------------------------
# Multi-strategy integration: DCA + Grid + TF aggregation
# ---------------------------------------------------------------------------


class TestMultiStrategyGlobalStopLoss:
    """
    Scenario from issue #358:
    DCA ($3000) + Grid ($900) + TF ($1000) running simultaneously on BTC/USDT.
    Verify that PortfolioRiskManager correctly aggregates exposure across all
    three and triggers force_close_all when the combined risk exceeds the threshold.
    """

    def _build_scenario(
        self,
        total_capital: float = 10000.0,
        global_stop_pct: float = 0.025,
    ) -> tuple[PortfolioRiskManager, MagicMock, MagicMock, MagicMock]:
        """
        Returns (prm, dca_provider, grid_provider, tf_provider).
        Each provider has a single open position on BTC/USDT.
        """
        prm = _make_prm(total=total_capital, global_stop_pct=global_stop_pct, cooldown=1800)

        dca = MagicMock()
        dca.get_open_positions.return_value = [
            {
                "symbol": "BTC/USDT",
                "size": Decimal("3000"),      # $3000 invested
                "atr_stop_distance": Decimal("0.04"),  # 4% stop → exposure $120
            }
        ]

        grid = MagicMock()
        grid.get_open_positions.return_value = [
            {
                "symbol": "BTC/USDT",
                "size": Decimal("900"),        # $900 notional
                "atr_stop_distance": Decimal("0.012"),  # 1.2% → exposure $10.8
            }
        ]

        tf = MagicMock()
        tf.get_open_positions.return_value = [
            {
                "symbol": "BTC/USDT",
                "size": Decimal("1000"),       # $1000
                "atr_stop_distance": Decimal("0.03"),   # 3% → exposure $30
            }
        ]

        prm.register_symbol_provider("BTC/USDT", "dca", dca)
        prm.register_symbol_provider("BTC/USDT", "grid", grid)
        prm.register_symbol_provider("BTC/USDT", "tf", tf)

        return prm, dca, grid, tf

    def test_combined_exposure_below_threshold_no_trigger(self) -> None:
        """
        DCA $120 + Grid $10.8 + TF $30 = $160.8 total exposure.
        As fraction of $10,000: 1.608% < 2.5% → no trigger.
        """
        prm, _, _, _ = self._build_scenario(total_capital=10000, global_stop_pct=0.025)
        triggered = prm.tick_global_stop_loss()
        assert triggered == []
        assert prm._is_symbol_cooling_down("BTC/USDT") is False

    def test_combined_exposure_exceeds_threshold_triggers(self) -> None:
        """
        Increase stop distances so total exposure > 2.5%.
        DCA: 3000 × 0.06 = 180, Grid: 900 × 0.06 = 54, TF: 1000 × 0.06 = 60
        Total = 294 → 2.94% > 2.5% → trigger.
        """
        prm = _make_prm(total=10000, global_stop_pct=0.025, cooldown=1800)
        for name, size in [("dca", 3000), ("grid", 900), ("tf", 1000)]:
            p = MagicMock()
            p.get_open_positions.return_value = [
                {
                    "symbol": "BTC/USDT",
                    "size": Decimal(str(size)),
                    "atr_stop_distance": Decimal("0.06"),
                }
            ]
            prm.register_symbol_provider("BTC/USDT", name, p)

        triggered = prm.tick_global_stop_loss()
        assert "BTC/USDT" in triggered
        assert prm._is_symbol_cooling_down("BTC/USDT") is True

    def test_force_close_callback_receives_symbol_and_reason(self) -> None:
        prm = _make_prm(total=10000, global_stop_pct=0.025, cooldown=1800)
        p = MagicMock()
        p.get_open_positions.return_value = [
            {"symbol": "BTC/USDT", "size": Decimal("5000"), "atr_stop_distance": Decimal("0.1")}
        ]
        prm.register_symbol_provider("BTC/USDT", "test", p)

        cb = MagicMock()
        prm.set_force_close_callback(cb)
        prm.tick_global_stop_loss()
        cb.assert_called_once()
        symbol, reason = cb.call_args[0]
        assert symbol == "BTC/USDT"
        assert "BTC/USDT" in reason

    def test_cooldown_blocks_new_entries_after_trigger(self) -> None:
        prm = _make_prm(total=10000, global_stop_pct=0.025, cooldown=1800)
        p = MagicMock()
        p.get_open_positions.return_value = [
            {"symbol": "BTC/USDT", "size": Decimal("5000"), "atr_stop_distance": Decimal("0.1")}
        ]
        prm.register_symbol_provider("BTC/USDT", "test", p)
        prm.tick_global_stop_loss()  # triggers force_close_all

        # New allocation should be blocked
        result = prm.check_allocation("bot1", Decimal("100"), symbol="BTC/USDT")
        assert result.approved is False
        assert result.status == RiskCheckStatus.REJECTED_SYMBOL_COOLDOWN

    def test_eth_not_affected_when_btc_triggers(self) -> None:
        prm = _make_prm(total=10000, global_stop_pct=0.025, cooldown=1800)

        btc_p = MagicMock()
        btc_p.get_open_positions.return_value = [
            {"symbol": "BTC/USDT", "size": Decimal("5000"), "atr_stop_distance": Decimal("0.1")}
        ]
        eth_p = MagicMock()
        eth_p.get_open_positions.return_value = [
            {"symbol": "ETH/USDT", "size": Decimal("100"), "atr_stop_distance": Decimal("0.01")}
        ]

        prm.register_symbol_provider("BTC/USDT", "btc-dca", btc_p)
        prm.register_symbol_provider("ETH/USDT", "eth-dca", eth_p)

        triggered = prm.tick_global_stop_loss()
        assert "BTC/USDT" in triggered
        assert "ETH/USDT" not in triggered

        # ETH allocation still allowed
        result = prm.check_allocation("bot-eth", Decimal("50"), symbol="ETH/USDT")
        # Should not be blocked by symbol cooldown (though other checks may apply)
        assert result.status != RiskCheckStatus.REJECTED_SYMBOL_COOLDOWN

    def test_risk_report_reflects_three_strategies(self) -> None:
        prm, _, _, _ = self._build_scenario(total_capital=10000)
        report = prm.get_risk_report()
        btc = report["symbols"]["BTC/USDT"]
        assert set(btc["strategies"]) == {"dca", "grid", "tf"}
        # exposure = 120 + 10.8 + 30 = 160.8
        assert abs(btc["aggregate_exposure"] - 160.8) < 0.01
        # exposure_pct = 160.8 / 10000 × 100 = 1.608% (rounded to 2 decimal places)
        assert abs(btc["exposure_pct"] - 1.608) < 0.01
