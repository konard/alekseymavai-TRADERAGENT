"""
Tests for _get_total_open_exposure() in BotOrchestrator and get_total_invested()
in DCAEngine — issue #339: unified cross-strategy exposure tracking.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from bot.config.schemas import (
    BotConfig,
    DCAConfig,
    ExchangeConfig,
    GridConfig,
    RiskManagementConfig,
)
from bot.core.dca_engine import DCAEngine
from bot.core.grid_engine import GridEngine, GridOrder
from bot.orchestrator.bot_orchestrator import BotOrchestrator

# ---------------------------------------------------------------------------
# DCAEngine.get_total_invested
# ---------------------------------------------------------------------------


class TestDCAEngineGetTotalInvested:
    """Tests for DCAEngine.get_total_invested()."""

    def _make_engine(self) -> DCAEngine:
        return DCAEngine(
            symbol="BTC/USDT",
            trigger_percentage=Decimal("0.05"),
            amount_per_step=Decimal("100"),
            max_steps=5,
            take_profit_percentage=Decimal("0.10"),
        )

    def test_no_position_returns_zero(self):
        """Returns 0 when no open position exists."""
        engine = self._make_engine()
        assert engine.get_total_invested() == Decimal("0")

    def test_single_step_position(self):
        """Returns total_cost of a single-step position."""
        engine = self._make_engine()
        engine.execute_dca_step(Decimal("40000"))
        # position.total_cost = 40000 * 100 = 4_000_000
        expected = Decimal("40000") * Decimal("100")
        assert engine.get_total_invested() == expected

    def test_multi_step_position(self):
        """Returns accumulated total_cost after multiple DCA steps."""
        engine = self._make_engine()
        engine.execute_dca_step(Decimal("40000"))  # step 1
        # Force a big price drop so the second step triggers
        engine.last_buy_price = Decimal("40000")
        engine.execute_dca_step(Decimal("37000"))  # step 2 (>5% drop)
        # total_cost = 40000*100 + 37000*100 = 7_700_000
        expected = Decimal("40000") * Decimal("100") + Decimal("37000") * Decimal("100")
        assert engine.get_total_invested() == expected

    def test_returns_zero_after_position_closed(self):
        """Returns 0 when position has been cleared (simulated TP)."""
        engine = self._make_engine()
        engine.execute_dca_step(Decimal("40000"))
        # Simulate position close by resetting
        engine.reset()
        assert engine.get_total_invested() == Decimal("0")


# ---------------------------------------------------------------------------
# Helpers for building a minimal BotOrchestrator without I/O
# ---------------------------------------------------------------------------


def _make_orchestrator_no_io(max_position_size: str = "1000") -> BotOrchestrator:
    """Return a BotOrchestrator whose engines and strategies are all None.

    Uses ``hybrid`` strategy so that BotConfig validation succeeds without
    requiring any particular engine to be pre-configured.  After construction
    the engines remain None (no ``await initialize()`` is called), so every
    ``_get_total_open_exposure()`` branch that guards on ``if self.grid_engine``
    etc. is exercisable independently via direct attribute assignment.
    """
    config = BotConfig(
        version=1,
        name="test_bot",
        symbol="BTC/USDT",
        strategy="hybrid",
        exchange=ExchangeConfig(
            exchange_id="binance",
            credentials_name="test",
            sandbox=True,
        ),
        grid=GridConfig(
            enabled=True,
            upper_price="50000",
            lower_price="40000",
            grid_levels=5,
            amount_per_grid="100",
            profit_per_grid="0.01",
        ),
        dca=DCAConfig(
            enabled=True,
            trigger_percentage="0.05",
            amount_per_step="100",
            max_steps=3,
            take_profit_percentage="0.10",
        ),
        risk_management=RiskManagementConfig(
            max_position_size=max_position_size,
            stop_loss_percentage="0.15",
            min_order_size="10",
        ),
        dry_run=True,
        auto_start=False,
    )
    mock_exchange = AsyncMock()
    mock_db = AsyncMock()
    orch = BotOrchestrator(
        bot_config=config,
        exchange_client=mock_exchange,
        db_manager=mock_db,
    )
    # Engines are None by default (initialize() not called).
    # Tests set them directly to exercise each branch.
    return orch


# ---------------------------------------------------------------------------
# BotOrchestrator._get_total_open_exposure — unit tests
# ---------------------------------------------------------------------------


class TestGetTotalOpenExposure:
    """Tests for BotOrchestrator._get_total_open_exposure()."""

    def test_all_engines_none_returns_zero(self):
        """Returns 0 when no strategy engines are configured."""
        orch = _make_orchestrator_no_io()
        assert orch._get_total_open_exposure() == Decimal("0")

    # ----- Grid -----

    def test_grid_buy_orders_counted(self):
        """Buy-side grid orders contribute price × amount to exposure."""
        orch = _make_orchestrator_no_io()
        engine = GridEngine(
            symbol="BTC/USDT",
            upper_price=Decimal("50000"),
            lower_price=Decimal("40000"),
            grid_levels=5,
            amount_per_grid=Decimal("0.01"),
            profit_per_grid=Decimal("0.01"),
        )
        buy_order = GridOrder(
            level=1,
            price=Decimal("40000"),
            amount=Decimal("0.02"),
            side="buy",
            order_id="o1",
        )
        engine.active_orders["o1"] = buy_order
        orch.grid_engine = engine

        expected = Decimal("40000") * Decimal("0.02")  # 800
        assert orch._get_total_open_exposure() == expected

    def test_grid_sell_orders_not_counted(self):
        """Sell-side grid orders are *not* counted as open exposure."""
        orch = _make_orchestrator_no_io()
        engine = GridEngine(
            symbol="BTC/USDT",
            upper_price=Decimal("50000"),
            lower_price=Decimal("40000"),
            grid_levels=5,
            amount_per_grid=Decimal("0.01"),
            profit_per_grid=Decimal("0.01"),
        )
        sell_order = GridOrder(
            level=3,
            price=Decimal("45000"),
            amount=Decimal("0.01"),
            side="sell",
            order_id="o2",
        )
        engine.active_orders["o2"] = sell_order
        orch.grid_engine = engine

        assert orch._get_total_open_exposure() == Decimal("0")

    def test_grid_multiple_buy_orders_summed(self):
        """Multiple buy orders are summed correctly."""
        orch = _make_orchestrator_no_io()
        engine = GridEngine(
            symbol="BTC/USDT",
            upper_price=Decimal("50000"),
            lower_price=Decimal("40000"),
            grid_levels=5,
            amount_per_grid=Decimal("0.01"),
            profit_per_grid=Decimal("0.01"),
        )
        o1 = GridOrder(
            level=1, price=Decimal("40000"), amount=Decimal("0.01"), side="buy", order_id="o1"
        )
        o2 = GridOrder(
            level=2, price=Decimal("42000"), amount=Decimal("0.01"), side="buy", order_id="o2"
        )
        engine.active_orders["o1"] = o1
        engine.active_orders["o2"] = o2
        orch.grid_engine = engine

        expected = Decimal("40000") * Decimal("0.01") + Decimal("42000") * Decimal("0.01")
        assert orch._get_total_open_exposure() == expected

    # ----- DCA -----

    def test_dca_no_position_zero(self):
        """DCA with no open position contributes 0."""
        orch = _make_orchestrator_no_io()
        orch.dca_engine = DCAEngine(
            symbol="BTC/USDT",
            trigger_percentage=Decimal("0.05"),
            amount_per_step=Decimal("100"),
            max_steps=3,
            take_profit_percentage=Decimal("0.10"),
        )
        assert orch._get_total_open_exposure() == Decimal("0")

    def test_dca_with_position_counted(self):
        """DCA position's total_cost is included in exposure."""
        orch = _make_orchestrator_no_io()
        dca = DCAEngine(
            symbol="BTC/USDT",
            trigger_percentage=Decimal("0.05"),
            amount_per_step=Decimal("100"),
            max_steps=3,
            take_profit_percentage=Decimal("0.10"),
        )
        dca.execute_dca_step(Decimal("500"))  # cost = 500 * 100 = 50_000
        orch.dca_engine = dca

        expected = Decimal("500") * Decimal("100")
        assert orch._get_total_open_exposure() == expected

    # ----- TrendFollower -----

    def test_trend_follower_positions_counted(self):
        """TrendFollower active position sizes are summed."""
        orch = _make_orchestrator_no_io()

        mock_pos = MagicMock()
        mock_pos.size = Decimal("200")

        mock_pm = MagicMock()
        mock_pm.active_positions = {"pos1": mock_pos}

        mock_tf = MagicMock()
        mock_tf.position_manager = mock_pm

        orch.trend_follower_strategy = mock_tf

        assert orch._get_total_open_exposure() == Decimal("200")

    def test_trend_follower_no_positions_zero(self):
        """TrendFollower with no active positions contributes 0."""
        orch = _make_orchestrator_no_io()

        mock_pm = MagicMock()
        mock_pm.active_positions = {}

        mock_tf = MagicMock()
        mock_tf.position_manager = mock_pm

        orch.trend_follower_strategy = mock_tf

        assert orch._get_total_open_exposure() == Decimal("0")

    # ----- SMC -----

    def test_smc_positions_counted(self):
        """SMC active position sizes are summed."""
        orch = _make_orchestrator_no_io()

        mock_pos = MagicMock()
        mock_pos.size = Decimal("300")

        mock_smc = MagicMock()
        mock_smc.get_active_positions.return_value = [mock_pos]

        orch.smc_strategy = mock_smc

        assert orch._get_total_open_exposure() == Decimal("300")

    def test_smc_no_positions_zero(self):
        """SMC with no active positions contributes 0."""
        orch = _make_orchestrator_no_io()

        mock_smc = MagicMock()
        mock_smc.get_active_positions.return_value = []

        orch.smc_strategy = mock_smc

        assert orch._get_total_open_exposure() == Decimal("0")

    # ----- Cross-strategy scenarios (acceptance criteria) -----

    def test_grid_800_plus_smc_300_blocked(self):
        """
        Acceptance criterion: Grid=$800 + SMC tries $300 → blocked (max=$1000).

        With _get_total_open_exposure() returning 800, check_trade(300, 800,
        balance) triggers check_position_limit(800, 300) → 1100 > 1000 → blocked.
        """
        orch = _make_orchestrator_no_io(max_position_size="1000")

        # Grid holds $800 exposure
        engine = GridEngine(
            symbol="BTC/USDT",
            upper_price=Decimal("50000"),
            lower_price=Decimal("40000"),
            grid_levels=5,
            amount_per_grid=Decimal("0.02"),
            profit_per_grid=Decimal("0.01"),
        )
        buy_order = GridOrder(
            level=1,
            price=Decimal("40000"),
            amount=Decimal("0.02"),
            side="buy",
            order_id="o1",
        )
        engine.active_orders["o1"] = buy_order
        orch.grid_engine = engine

        from bot.core.risk_manager import RiskManager

        rm = RiskManager(
            max_position_size=Decimal("1000"),
            min_order_size=Decimal("10"),
        )
        orch.risk_manager = rm

        current_exposure = orch._get_total_open_exposure()
        assert current_exposure == Decimal("800")  # 40000 * 0.02

        result = rm.check_trade(
            order_value=Decimal("300"),
            current_position=current_exposure,
            available_balance=Decimal("10000"),
        )
        assert not result.allowed, f"Expected blocked, got: {result}"
        assert "exceed" in result.reason.lower()

    def test_grid_500_plus_smc_300_allowed(self):
        """
        Acceptance criterion: Grid=$500 + SMC tries $300 → allowed (total $800 < max $1000).
        """
        orch = _make_orchestrator_no_io(max_position_size="1000")

        # Grid holds $500 exposure
        engine = GridEngine(
            symbol="BTC/USDT",
            upper_price=Decimal("50000"),
            lower_price=Decimal("40000"),
            grid_levels=5,
            amount_per_grid=Decimal("0.01"),
            profit_per_grid=Decimal("0.01"),
        )
        buy_order = GridOrder(
            level=1,
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            side="buy",
            order_id="o1",
        )
        engine.active_orders["o1"] = buy_order
        orch.grid_engine = engine

        from bot.core.risk_manager import RiskManager

        rm = RiskManager(
            max_position_size=Decimal("1000"),
            min_order_size=Decimal("10"),
        )
        orch.risk_manager = rm

        current_exposure = orch._get_total_open_exposure()
        assert current_exposure == Decimal("500")  # 50000 * 0.01

        result = rm.check_trade(
            order_value=Decimal("300"),
            current_position=current_exposure,
            available_balance=Decimal("10000"),
        )
        assert result.allowed, f"Expected allowed, got: {result}"

    def test_combined_all_strategies(self):
        """All four strategies sum correctly."""
        orch = _make_orchestrator_no_io()

        # Grid: 1 buy order = 40000 * 0.01 = 400
        engine = GridEngine(
            symbol="BTC/USDT",
            upper_price=Decimal("50000"),
            lower_price=Decimal("40000"),
            grid_levels=5,
            amount_per_grid=Decimal("0.01"),
            profit_per_grid=Decimal("0.01"),
        )
        o1 = GridOrder(
            level=1, price=Decimal("40000"), amount=Decimal("0.01"), side="buy", order_id="o1"
        )
        engine.active_orders["o1"] = o1
        orch.grid_engine = engine

        # DCA: position total_cost = 30000 * 0.01 = 300
        dca = DCAEngine(
            symbol="BTC/USDT",
            trigger_percentage=Decimal("0.05"),
            amount_per_step=Decimal("0.01"),
            max_steps=3,
            take_profit_percentage=Decimal("0.10"),
        )
        dca.execute_dca_step(Decimal("30000"))
        orch.dca_engine = dca

        # TrendFollower: one position = 200
        mock_tf_pos = MagicMock()
        mock_tf_pos.size = Decimal("200")
        mock_pm = MagicMock()
        mock_pm.active_positions = {"p1": mock_tf_pos}
        mock_tf = MagicMock()
        mock_tf.position_manager = mock_pm
        orch.trend_follower_strategy = mock_tf

        # SMC: one position = 100
        mock_smc_pos = MagicMock()
        mock_smc_pos.size = Decimal("100")
        mock_smc = MagicMock()
        mock_smc.get_active_positions.return_value = [mock_smc_pos]
        orch.smc_strategy = mock_smc

        # 400 + 300 + 200 + 100 = 1000
        expected = Decimal("400") + Decimal("300") + Decimal("200") + Decimal("100")
        assert orch._get_total_open_exposure() == expected
