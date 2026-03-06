"""Unit tests for BotOrchestrator"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.config.schemas import (
    BotConfig,
    DCAConfig,
    ExchangeConfig,
    GridConfig,
    RiskManagementConfig,
)
from bot.orchestrator.bot_orchestrator import BotOrchestrator, BotState
from bot.orchestrator.events import EventType
from bot.strategies.trend_follower.entry_logic import SignalType


@pytest.fixture
def bot_config():
    """Create test bot configuration."""
    return BotConfig(
        version=1,
        name="test_bot",
        symbol="BTC/USDT",
        strategy="hybrid",
        exchange=ExchangeConfig(
            exchange_id="binance",
            credentials_name="test_creds",
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
            take_profit_percentage="0.1",
        ),
        risk_management=RiskManagementConfig(
            max_position_size="5000",
            stop_loss_percentage="0.15",
            min_order_size="10",
        ),
        dry_run=True,
        auto_start=False,
    )


@pytest.fixture
def mock_exchange():
    """Create mock exchange client."""
    exchange = AsyncMock()
    # Correct structure for fetch_balance (CCXT format)
    exchange.fetch_balance.return_value = {
        "free": {"USDT": 10000},
        "used": {"USDT": 0},
        "total": {"USDT": 10000},
    }
    exchange.fetch_ticker.return_value = {"last": 45000}
    exchange.fetch_open_orders.return_value = []
    exchange.create_order.return_value = {"id": "order_123"}
    return exchange


@pytest.fixture
def mock_db():
    """Create mock database manager."""
    db = AsyncMock()
    return db


@pytest.fixture
async def orchestrator(bot_config, mock_exchange, mock_db):
    """Create BotOrchestrator instance for testing."""
    with patch("bot.orchestrator.bot_orchestrator.redis.from_url") as mock_redis:
        mock_redis_client = AsyncMock()
        mock_redis_client.ping = AsyncMock()
        mock_redis_client.publish = AsyncMock()
        mock_redis_client.aclose = AsyncMock()
        mock_redis.return_value = mock_redis_client

        orch = BotOrchestrator(
            bot_config=bot_config,
            exchange_client=mock_exchange,
            db_manager=mock_db,
            redis_url="redis://localhost:6379",
        )
        await orch.initialize()
        yield orch
        await orch.cleanup()


class TestBotOrchestratorInitialization:
    """Test BotOrchestrator initialization."""

    @pytest.mark.asyncio
    async def test_initialization(self, orchestrator, bot_config):
        """Test orchestrator initializes correctly."""
        assert orchestrator.config == bot_config
        assert orchestrator.state == BotState.STOPPED
        assert orchestrator.grid_engine is not None
        assert orchestrator.dca_engine is not None
        assert orchestrator.risk_manager is not None
        assert orchestrator.redis_client is not None

    @pytest.mark.asyncio
    async def test_initialization_grid_only(self, mock_exchange, mock_db):
        """Test initialization with grid strategy only."""
        config = BotConfig(
            version=1,
            name="grid_bot",
            symbol="BTC/USDT",
            strategy="grid",
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
            risk_management=RiskManagementConfig(
                max_position_size="5000",
                stop_loss_percentage="0.15",
                min_order_size="10",
            ),
            dry_run=True,
            auto_start=False,
        )

        with patch("bot.orchestrator.bot_orchestrator.redis.from_url") as mock_redis:
            mock_redis_client = AsyncMock()
            mock_redis_client.ping = AsyncMock()
            mock_redis_client.aclose = AsyncMock()
            mock_redis.return_value = mock_redis_client

            orch = BotOrchestrator(
                bot_config=config,
                exchange_client=mock_exchange,
                db_manager=mock_db,
            )
            await orch.initialize()

            assert orch.grid_engine is not None
            assert orch.dca_engine is None

            await orch.cleanup()

    @pytest.mark.asyncio
    async def test_initialization_dca_only(self, mock_exchange, mock_db):
        """Test initialization with DCA strategy only."""
        config = BotConfig(
            version=1,
            name="dca_bot",
            symbol="BTC/USDT",
            strategy="dca",
            exchange=ExchangeConfig(
                exchange_id="binance",
                credentials_name="test",
                sandbox=True,
            ),
            dca=DCAConfig(
                enabled=True,
                trigger_percentage="0.05",
                amount_per_step="100",
                max_steps=3,
                take_profit_percentage="0.1",
            ),
            risk_management=RiskManagementConfig(
                max_position_size="5000",
                stop_loss_percentage="0.15",
                min_order_size="10",
            ),
            dry_run=True,
            auto_start=False,
        )

        with patch("bot.orchestrator.bot_orchestrator.redis.from_url") as mock_redis:
            mock_redis_client = AsyncMock()
            mock_redis_client.ping = AsyncMock()
            mock_redis_client.aclose = AsyncMock()
            mock_redis.return_value = mock_redis_client

            orch = BotOrchestrator(
                bot_config=config,
                exchange_client=mock_exchange,
                db_manager=mock_db,
            )
            await orch.initialize()

            assert orch.grid_engine is None
            assert orch.dca_engine is not None

            await orch.cleanup()


class TestBotOrchestratorLifecycle:
    """Test BotOrchestrator lifecycle management."""

    @pytest.mark.asyncio
    async def test_start_bot(self, orchestrator):
        """Test starting the bot."""
        await orchestrator.start()

        assert orchestrator.state == BotState.RUNNING
        assert orchestrator._running is True
        assert orchestrator._main_task is not None
        assert orchestrator._price_monitor_task is not None

        # Cleanup
        await orchestrator.stop()

    @pytest.mark.asyncio
    async def test_stop_bot(self, orchestrator):
        """Test stopping the bot."""
        await orchestrator.start()
        await orchestrator.stop()

        assert orchestrator.state == BotState.STOPPED
        assert orchestrator._running is False

    @pytest.mark.asyncio
    async def test_pause_bot(self, orchestrator):
        """Test pausing the bot."""
        await orchestrator.start()
        await orchestrator.pause()

        assert orchestrator.state == BotState.PAUSED

        await orchestrator.stop()

    @pytest.mark.asyncio
    async def test_resume_bot(self, orchestrator):
        """Test resuming the bot from paused state."""
        await orchestrator.start()
        await orchestrator.pause()
        await orchestrator.resume()

        assert orchestrator.state == BotState.RUNNING

        await orchestrator.stop()

    @pytest.mark.asyncio
    async def test_emergency_stop(self, orchestrator):
        """Test emergency stop."""
        await orchestrator.start()
        await orchestrator.emergency_stop()

        assert orchestrator.state == BotState.EMERGENCY
        assert orchestrator._running is False

    @pytest.mark.asyncio
    async def test_start_already_running(self, orchestrator):
        """Test starting bot when already running."""
        await orchestrator.start()

        # Try to start again
        await orchestrator.start()

        # Should still be running, no error
        assert orchestrator.state == BotState.RUNNING

        await orchestrator.stop()

    @pytest.mark.asyncio
    async def test_stop_already_stopped(self, orchestrator):
        """Test stopping bot when already stopped."""
        # Bot is already stopped
        await orchestrator.stop()

        # Should remain stopped, no error
        assert orchestrator.state == BotState.STOPPED

    @pytest.mark.asyncio
    async def test_pause_not_running(self, orchestrator):
        """Test pausing bot when not running."""
        # Bot is stopped
        await orchestrator.pause()

        # Should remain stopped
        assert orchestrator.state == BotState.STOPPED

    @pytest.mark.asyncio
    async def test_resume_not_paused(self, orchestrator):
        """Test resuming bot when not paused."""
        await orchestrator.start()

        # Try to resume when running
        await orchestrator.resume()

        # Should remain running
        assert orchestrator.state == BotState.RUNNING

        await orchestrator.stop()


class TestBotOrchestratorEvents:
    """Test event publishing."""

    @pytest.mark.asyncio
    async def test_publish_event(self, orchestrator):
        """Test event publishing to Redis."""
        await orchestrator._publish_event(
            EventType.BOT_STARTED,
            {"strategy": "hybrid"},
        )

        # Verify Redis publish was called
        orchestrator.redis_client.publish.assert_called_once()
        call_args = orchestrator.redis_client.publish.call_args
        channel = call_args[0][0]
        message = call_args[0][1]

        assert channel == f"trading_events:{orchestrator.config.name}"
        assert "bot_started" in message

    @pytest.mark.asyncio
    async def test_start_publishes_event(self, orchestrator):
        """Test that starting bot publishes BOT_STARTED event."""
        await orchestrator.start()

        # Check that publish was called
        assert orchestrator.redis_client.publish.called

        await orchestrator.stop()


class TestBotOrchestratorStatus:
    """Test status reporting."""

    @pytest.mark.asyncio
    async def test_get_status_stopped(self, orchestrator):
        """Test getting status when bot is stopped."""
        status = await orchestrator.get_status()

        assert status["bot_name"] == orchestrator.config.name
        assert status["symbol"] == orchestrator.config.symbol
        assert status["strategy"] == orchestrator.config.strategy
        assert status["state"] == BotState.STOPPED.value
        assert status["dry_run"] is True
        assert "grid" in status
        assert "dca" in status
        assert "risk" in status

    @pytest.mark.asyncio
    async def test_get_status_running(self, orchestrator):
        """Test getting status when bot is running."""
        await orchestrator.start()
        status = await orchestrator.get_status()

        assert status["state"] == BotState.RUNNING.value
        assert status["current_price"] is not None

        await orchestrator.stop()


class TestBotOrchestratorIntegration:
    """Test orchestrator integration with engines."""

    @pytest.mark.asyncio
    async def test_grid_initialization_on_start(self, orchestrator):
        """Test that grid is initialized when bot starts."""
        await orchestrator.start()

        # Check that grid was initialized
        assert orchestrator.grid_engine is not None
        assert len(orchestrator.grid_engine.grid_orders) > 0

        await orchestrator.stop()

    @pytest.mark.asyncio
    async def test_dca_reset_on_start(self, orchestrator):
        """Test that DCA is reset when bot starts."""
        # Set up DCA with a position
        orchestrator.dca_engine.execute_dca_step(Decimal("45000"))

        await orchestrator.start()

        # DCA should be reset (position is None after reset)
        assert orchestrator.dca_engine.position is None

        await orchestrator.stop()

    @pytest.mark.asyncio
    async def test_risk_manager_balance_update(self, orchestrator, mock_exchange):
        """Test that risk manager balance is updated."""
        # Mock different balance
        mock_exchange.get_balance.return_value = {"USDT": 12000}

        await orchestrator.start()

        # Give time for update
        import asyncio

        await asyncio.sleep(0.1)

        # Risk manager should have initial balance
        assert orchestrator.risk_manager.initial_balance == Decimal("10000")

        await orchestrator.stop()


class TestBotOrchestratorErrorHandling:
    """Test error handling."""

    @pytest.mark.asyncio
    async def test_start_with_exchange_error(self, orchestrator, mock_exchange):
        """Test starting bot when exchange fails."""
        mock_exchange.fetch_ticker.side_effect = Exception("Exchange error")

        with pytest.raises((Exception, RuntimeError)):
            await orchestrator.start()

        # Bot should remain stopped
        assert orchestrator.state == BotState.STOPPED

    @pytest.mark.asyncio
    async def test_event_publish_failure(self, orchestrator):
        """Test handling of event publish failures."""
        orchestrator.redis_client.publish.side_effect = Exception("Redis error")

        # Should not raise exception
        await orchestrator._publish_event(EventType.BOT_STARTED, {})

        # Event publish failure should be logged but not crash
        assert True


class TestReduceOnlyOnCloseOrders:
    """Test that reduceOnly=True is passed for all strategy close/exit orders."""

    @pytest.mark.asyncio
    async def test_execute_trend_follower_exit_passes_reduce_only(
        self, orchestrator, mock_exchange
    ):
        """Test that _execute_trend_follower_exit passes reduceOnly=True to create_order."""
        # Build a minimal mock position (LONG)
        position = MagicMock()
        position.signal_type = SignalType.LONG
        position.size = Decimal("450")  # quote-currency size
        position.entry_price = Decimal("45000")

        await orchestrator._execute_trend_follower_exit(position)

        mock_exchange.create_order.assert_called_once()
        call_kwargs = mock_exchange.create_order.call_args.kwargs
        assert call_kwargs.get("params") == {"reduceOnly": True}, (
            "Expected params={'reduceOnly': True} to be passed on trend_follower exit order"
        )
        # Side must be the opposite of LONG (i.e. sell)
        assert call_kwargs.get("side") == "sell"

    @pytest.mark.asyncio
    async def test_execute_trend_follower_exit_short_passes_reduce_only(
        self, orchestrator, mock_exchange
    ):
        """Test that _execute_trend_follower_exit passes reduceOnly=True for SHORT positions."""
        position = MagicMock()
        position.signal_type = SignalType.SHORT
        position.size = Decimal("450")
        position.entry_price = Decimal("45000")

        await orchestrator._execute_trend_follower_exit(position)

        mock_exchange.create_order.assert_called_once()
        call_kwargs = mock_exchange.create_order.call_args.kwargs
        assert call_kwargs.get("params") == {"reduceOnly": True}
        # Side must be the opposite of SHORT (i.e. buy)
        assert call_kwargs.get("side") == "buy"

    @pytest.mark.asyncio
    async def test_execute_smc_exit_passes_reduce_only(self, orchestrator, mock_exchange):
        """Test that _execute_smc_exit passes reduceOnly=True to create_order."""
        position_id = "pos_abc123"
        # SMC exit looks up the closed trade in smc_strategy._closed_trades
        closed_trade = {
            "position_id": position_id,
            "direction": "long",
            "size": Decimal("450"),
            "entry_price": Decimal("45000"),
        }

        mock_smc = MagicMock()
        mock_smc._closed_trades = [closed_trade]
        orchestrator.smc_strategy = mock_smc

        exit_reason = MagicMock()
        exit_reason.value = "take_profit"

        await orchestrator._execute_smc_exit(position_id, exit_reason)

        mock_exchange.create_order.assert_called_once()
        call_kwargs = mock_exchange.create_order.call_args.kwargs
        assert call_kwargs.get("params") == {"reduceOnly": True}, (
            "Expected params={'reduceOnly': True} to be passed on SMC exit order"
        )
        # Side must be opposite of long (i.e. sell)
        assert call_kwargs.get("side") == "sell"

    @pytest.mark.asyncio
    async def test_execute_smc_exit_short_passes_reduce_only(self, orchestrator, mock_exchange):
        """Test that _execute_smc_exit passes reduceOnly=True for SHORT positions."""
        position_id = "pos_short456"
        closed_trade = {
            "position_id": position_id,
            "direction": "short",
            "size": Decimal("450"),
            "entry_price": Decimal("45000"),
        }

        mock_smc = MagicMock()
        mock_smc._closed_trades = [closed_trade]
        orchestrator.smc_strategy = mock_smc

        exit_reason = MagicMock()
        exit_reason.value = "stop_loss"

        await orchestrator._execute_smc_exit(position_id, exit_reason)

        mock_exchange.create_order.assert_called_once()
        call_kwargs = mock_exchange.create_order.call_args.kwargs
        assert call_kwargs.get("params") == {"reduceOnly": True}
        # Side must be opposite of short (i.e. buy)
        assert call_kwargs.get("side") == "buy"
