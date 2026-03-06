"""Tests for v2.0 database models (Strategy, Position, Signal, DCA)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bot.database.models import (
    Base,
    Bot,
    DCADeal,
    DCAOrder,
    DCASignal,
    ExchangeCredential,
    Position,
    Signal,
    Strategy,
)


@pytest.fixture
def engine():
    """Create in-memory SQLite engine for testing."""
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    """Create a test session."""
    SessionLocal = sessionmaker(bind=engine)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture
def sample_bot(session: Session) -> Bot:
    """Create a sample bot with credentials for FK references."""
    cred = ExchangeCredential(
        name="test-cred",
        exchange_id="bybit",
        api_key_encrypted="enc_key",
        api_secret_encrypted="enc_secret",
        is_sandbox=True,
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(cred)
    session.flush()

    bot = Bot(
        name="test-bot",
        credentials_id=cred.id,
        symbol="BTC/USDT",
        strategy="grid",
        status="stopped",
        config_version=1,
        config_data="{}",
        total_invested=Decimal("0"),
        current_profit=Decimal("0"),
        total_trades=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(bot)
    session.flush()
    return bot


@pytest.fixture
def sample_strategy(session: Session, sample_bot: Bot) -> Strategy:
    """Create a sample v2.0 strategy."""
    strategy = Strategy(
        strategy_id="smc-btc-1",
        strategy_type="smc",
        bot_id=sample_bot.id,
        state="idle",
        config_data='{"ema_fast": 20}',
        total_signals=0,
        executed_trades=0,
        profitable_trades=0,
        total_pnl=Decimal("0"),
        error_count=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(strategy)
    session.flush()
    return strategy


# =========================================================================
# Strategy Tests
# =========================================================================


class TestStrategy:
    def test_create_strategy(self, session: Session, sample_bot: Bot):
        strategy = Strategy(
            strategy_id="tf-eth-1",
            strategy_type="trend_follower",
            bot_id=sample_bot.id,
            state="idle",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(strategy)
        session.commit()

        result = session.execute(select(Strategy).where(Strategy.strategy_id == "tf-eth-1"))
        loaded = result.scalar_one()
        assert loaded.strategy_type == "trend_follower"
        assert loaded.state == "idle"
        assert loaded.total_pnl == Decimal("0")

    def test_strategy_unique_id(self, session: Session, sample_strategy: Strategy):
        dup = Strategy(
            strategy_id=sample_strategy.strategy_id,
            strategy_type="grid",
            state="idle",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(dup)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_strategy_types(self, session: Session):
        for stype in ("smc", "trend_follower", "grid", "dca", "hybrid"):
            s = Strategy(
                strategy_id=f"test-{stype}",
                strategy_type=stype,
                state="idle",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(s)
        session.commit()

        result = session.execute(select(Strategy))
        strategies = result.scalars().all()
        assert len(strategies) == 5

    def test_strategy_states(self, session: Session, sample_strategy: Strategy):
        for state in ("idle", "starting", "active", "paused", "stopping", "stopped", "error"):
            sample_strategy.state = state
            session.flush()
            assert sample_strategy.state == state

    def test_strategy_metrics(self, session: Session, sample_strategy: Strategy):
        sample_strategy.total_signals = 42
        sample_strategy.executed_trades = 10
        sample_strategy.profitable_trades = 7
        sample_strategy.total_pnl = Decimal("1250.50")
        sample_strategy.error_count = 2
        sample_strategy.last_error = "Connection timeout"
        session.commit()

        loaded = session.get(Strategy, sample_strategy.id)
        assert loaded.total_signals == 42
        assert loaded.profitable_trades == 7
        assert loaded.total_pnl == Decimal("1250.50")

    def test_strategy_repr(self, sample_strategy: Strategy):
        r = repr(sample_strategy)
        assert "smc-btc-1" in r
        assert "smc" in r


# =========================================================================
# Signal Tests
# =========================================================================


class TestSignal:
    def test_create_signal(self, session: Session, sample_strategy: Strategy):
        signal = Signal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            confidence=0.85,
            risk_reward_ratio=2.0,
            signal_reason="bullish_ob",
            was_executed=False,
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(signal)
        session.commit()

        loaded = session.get(Signal, signal.id)
        assert loaded.direction == "long"
        assert loaded.confidence == 0.85
        assert loaded.entry_price == Decimal("45000")
        assert loaded.was_executed is False

    def test_signal_with_metadata(self, session: Session, sample_strategy: Strategy):
        signal = Signal(
            strategy_db_id=sample_strategy.id,
            symbol="ETH/USDT",
            direction="short",
            entry_price=Decimal("3000"),
            stop_loss=Decimal("3100"),
            take_profit=Decimal("2800"),
            confidence=0.7,
            metadata_json='{"confluence_score": 0.9}',
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(signal)
        session.commit()

        loaded = session.get(Signal, signal.id)
        assert loaded.metadata_json is not None
        assert "confluence_score" in loaded.metadata_json

    def test_signal_strategy_relationship(self, session: Session, sample_strategy: Strategy):
        signal = Signal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            confidence=0.8,
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(signal)
        session.commit()

        session.refresh(sample_strategy)
        assert len(sample_strategy.signals) == 1
        assert sample_strategy.signals[0].symbol == "BTC/USDT"


# =========================================================================
# Position Tests
# =========================================================================


class TestPosition:
    def test_create_position(self, session: Session, sample_strategy: Strategy):
        pos = Position(
            position_id="pos-abc123",
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            status="open",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            size=Decimal("0.1"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        session.commit()

        loaded = session.get(Position, pos.id)
        assert loaded.position_id == "pos-abc123"
        assert loaded.direction == "long"
        assert loaded.status == "open"
        assert loaded.size == Decimal("0.1")

    def test_close_position(self, session: Session, sample_strategy: Strategy):
        pos = Position(
            position_id="pos-close-test",
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            status="open",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            size=Decimal("0.1"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        session.flush()

        pos.status = "closed"
        pos.exit_price = Decimal("46500")
        pos.exit_reason = "take_profit"
        pos.realized_pnl = Decimal("150")
        pos.closed_at = datetime.now(timezone.utc)
        session.commit()

        loaded = session.get(Position, pos.id)
        assert loaded.status == "closed"
        assert loaded.exit_reason == "take_profit"
        assert loaded.realized_pnl == Decimal("150")

    def test_position_unique_id(self, session: Session, sample_strategy: Strategy):
        pos1 = Position(
            position_id="dup-id",
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            size=Decimal("0.1"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pos1)
        session.flush()

        pos2 = Position(
            position_id="dup-id",
            strategy_db_id=sample_strategy.id,
            symbol="ETH/USDT",
            direction="short",
            entry_price=Decimal("3000"),
            stop_loss=Decimal("3100"),
            take_profit=Decimal("2800"),
            size=Decimal("1"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pos2)
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    def test_position_with_signal(self, session: Session, sample_strategy: Strategy):
        signal = Signal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            confidence=0.85,
            was_executed=True,
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(signal)
        session.flush()

        pos = Position(
            position_id="pos-with-signal",
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            size=Decimal("0.1"),
            signal_db_id=signal.id,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        session.commit()

        loaded = session.get(Position, pos.id)
        assert loaded.signal is not None
        assert loaded.signal.confidence == 0.85

    def test_strategy_positions_relationship(self, session: Session, sample_strategy: Strategy):
        for i in range(3):
            pos = Position(
                position_id=f"pos-rel-{i}",
                strategy_db_id=sample_strategy.id,
                symbol="BTC/USDT",
                direction="long",
                entry_price=Decimal("45000"),
                stop_loss=Decimal("44000"),
                take_profit=Decimal("47000"),
                size=Decimal("0.1"),
                opened_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(pos)
        session.commit()

        session.refresh(sample_strategy)
        assert len(sample_strategy.positions) == 3


# =========================================================================
# DCA Deal Tests
# =========================================================================


class TestDCADeal:
    def test_create_deal(self, session: Session, sample_strategy: Strategy):
        deal = DCADeal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            status="active",
            base_order_size=Decimal("100"),
            safety_order_size=Decimal("50"),
            max_safety_orders=5,
            filled_safety_orders=0,
            average_entry_price=Decimal("45000"),
            total_invested=Decimal("100"),
            total_quantity=Decimal("0.00222"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        session.commit()

        loaded = session.get(DCADeal, deal.id)
        assert loaded.status == "active"
        assert loaded.max_safety_orders == 5
        assert loaded.base_order_size == Decimal("100")

    def test_complete_deal(self, session: Session, sample_strategy: Strategy):
        deal = DCADeal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            status="active",
            base_order_size=Decimal("100"),
            safety_order_size=Decimal("50"),
            max_safety_orders=3,
            filled_safety_orders=2,
            average_entry_price=Decimal("44000"),
            total_invested=Decimal("200"),
            total_quantity=Decimal("0.00454"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        session.flush()

        deal.status = "completed"
        deal.realized_pnl = Decimal("25.50")
        deal.closed_at = datetime.now(timezone.utc)
        session.commit()

        loaded = session.get(DCADeal, deal.id)
        assert loaded.status == "completed"
        assert loaded.realized_pnl == Decimal("25.50")


# =========================================================================
# DCA Order Tests
# =========================================================================


class TestDCAOrder:
    def test_create_base_order(self, session: Session, sample_strategy: Strategy):
        deal = DCADeal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            base_order_size=Decimal("100"),
            safety_order_size=Decimal("50"),
            max_safety_orders=3,
            average_entry_price=Decimal("45000"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        session.flush()

        order = DCAOrder(
            deal_id=deal.id,
            order_number=0,
            is_base_order=True,
            side="buy",
            price=Decimal("45000"),
            amount=Decimal("0.00222"),
            status="filled",
            filled_amount=Decimal("0.00222"),
            exchange_order_id="bybit-12345",
            deviation_pct=0.0,
            created_at=datetime.now(timezone.utc),
            filled_at=datetime.now(timezone.utc),
        )
        session.add(order)
        session.commit()

        loaded = session.get(DCAOrder, order.id)
        assert loaded.is_base_order is True
        assert loaded.status == "filled"
        assert loaded.exchange_order_id == "bybit-12345"

    def test_safety_orders(self, session: Session, sample_strategy: Strategy):
        deal = DCADeal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            base_order_size=Decimal("100"),
            safety_order_size=Decimal("50"),
            max_safety_orders=3,
            average_entry_price=Decimal("45000"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        session.flush()

        for i in range(3):
            order = DCAOrder(
                deal_id=deal.id,
                order_number=i + 1,
                is_base_order=False,
                side="buy",
                price=Decimal(str(44000 - i * 500)),
                amount=Decimal("0.001"),
                status="pending",
                deviation_pct=float((i + 1) * 1.5),
                created_at=datetime.now(timezone.utc),
            )
            session.add(order)
        session.commit()

        session.refresh(deal)
        assert len(deal.dca_orders) == 3
        assert deal.dca_orders[0].order_number == 1
        assert deal.dca_orders[2].deviation_pct == 4.5


# =========================================================================
# DCA Signal Tests
# =========================================================================


class TestDCASignal:
    def test_create_start_deal_signal(self, session: Session, sample_strategy: Strategy):
        sig = DCASignal(
            signal_type="start_deal",
            direction="long",
            trigger_price=Decimal("45000"),
            target_price=Decimal("44500"),
            confidence=0.8,
            source_strategy="smc-btc-1",
            reason="Bullish OB detected",
            was_executed=True,
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(sig)
        session.commit()

        loaded = session.get(DCASignal, sig.id)
        assert loaded.signal_type == "start_deal"
        assert loaded.source_strategy == "smc-btc-1"
        assert loaded.was_executed is True

    def test_safety_order_signal(self, session: Session, sample_strategy: Strategy):
        deal = DCADeal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            base_order_size=Decimal("100"),
            safety_order_size=Decimal("50"),
            max_safety_orders=3,
            average_entry_price=Decimal("45000"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        session.flush()

        sig = DCASignal(
            deal_id=deal.id,
            signal_type="safety_order",
            direction="long",
            trigger_price=Decimal("44000"),
            confidence=0.6,
            reason="Price dropped 2.2%",
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(sig)
        session.commit()

        loaded = session.get(DCASignal, sig.id)
        assert loaded.deal_id == deal.id
        assert loaded.signal_type == "safety_order"


# =========================================================================
# Cross-model Relationship Tests
# =========================================================================


class TestRelationships:
    def test_strategy_cascade(self, session: Session, sample_strategy: Strategy):
        """Test that strategy has all related collections."""
        signal = Signal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            confidence=0.85,
            generated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        session.add(signal)
        session.flush()

        pos = Position(
            position_id="rel-test-pos",
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            entry_price=Decimal("45000"),
            stop_loss=Decimal("44000"),
            take_profit=Decimal("47000"),
            size=Decimal("0.1"),
            signal_db_id=signal.id,
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(pos)

        deal = DCADeal(
            strategy_db_id=sample_strategy.id,
            symbol="BTC/USDT",
            direction="long",
            base_order_size=Decimal("100"),
            safety_order_size=Decimal("50"),
            max_safety_orders=3,
            average_entry_price=Decimal("45000"),
            opened_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        session.commit()

        session.refresh(sample_strategy)
        assert len(sample_strategy.signals) == 1
        assert len(sample_strategy.positions) == 1
        assert len(sample_strategy.dca_deals) == 1

    def test_all_tables_created(self, engine):
        """Verify all v2.0 tables exist."""
        tables = Base.metadata.tables.keys()
        v2_tables = {"strategies", "positions", "signals", "dca_deals", "dca_orders", "dca_signals"}
        for t in v2_tables:
            assert t in tables, f"Table '{t}' missing from metadata"
