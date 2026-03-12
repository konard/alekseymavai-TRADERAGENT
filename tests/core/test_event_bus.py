"""Tests for EventBus, EventStore, EventRegistry, and Reducers."""

import asyncio
import time

import pytest

from bot.core.event_bus import DomainEvent, EventBus, EventPriority
from bot.core.event_registry import (
    EVENT_REGISTRY,
    get_causes,
    get_entity_events,
    get_event_meta,
    get_valid_transitions,
)
from bot.core.reducers import (
    get_state_at,
    reduce_portfolio,
    reduce_position,
    reduce_regime,
    reduce_strategy,
)


# ---------------------------------------------------------------------------
# DomainEvent
# ---------------------------------------------------------------------------


class TestDomainEvent:
    def test_create_event(self):
        evt = DomainEvent(
            entity_type="position",
            entity_id="pos_1",
            event_type="SIGNAL_GENERATED",
            data={"direction": "LONG", "price": 100.0},
            bot_name="test_bot",
        )
        assert evt.event_id  # UUID auto-generated
        assert evt.entity_type == "position"
        assert evt.ts > 0
        assert evt.causes == []
        assert evt.enables == []
        assert evt.priority == EventPriority.NORMAL

    def test_json_roundtrip(self):
        evt = DomainEvent(
            entity_type="regime",
            entity_id="r1",
            event_type="REGIME_DETECTED",
            data={"regime": "bull_trend", "confidence": 0.85},
            bot_name="bot1",
            causes=["evt_abc"],
            enables=["REGIME_CHANGE_CANDIDATE"],
        )
        json_str = evt.to_json()
        restored = DomainEvent.from_json(json_str)
        assert restored.event_id == evt.event_id
        assert restored.entity_type == evt.entity_type
        assert restored.data == evt.data
        assert restored.causes == ["evt_abc"]
        assert restored.enables == ["REGIME_CHANGE_CANDIDATE"]


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class TestEventBus:
    @pytest.fixture
    def bus(self):
        return EventBus(bot_name="test")

    def _make_event(self, event_type="TEST_EVENT", entity_type="test", entity_id="1", **kwargs):
        return DomainEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            data=kwargs.get("data", {}),
            bot_name="test",
            causes=kwargs.get("causes", []),
            enables=kwargs.get("enables", []),
            priority=kwargs.get("priority", EventPriority.NORMAL),
        )

    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self, bus):
        received = []

        def handler(evt):
            received.append(evt)

        bus.subscribe("TEST_EVENT", handler)
        evt = self._make_event()
        await bus.publish(evt)

        assert len(received) == 1
        assert received[0].event_type == "TEST_EVENT"

    @pytest.mark.asyncio
    async def test_async_handler(self, bus):
        received = []

        async def handler(evt):
            received.append(evt)

        bus.subscribe("ASYNC_TEST", handler)
        evt = self._make_event(event_type="ASYNC_TEST")
        await bus.publish(evt)

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe(self, bus):
        received = []

        def handler(evt):
            received.append(evt)

        unsub = bus.subscribe("UNSUB_TEST", handler)
        await bus.publish(self._make_event(event_type="UNSUB_TEST"))
        assert len(received) == 1

        unsub()
        await bus.publish(self._make_event(event_type="UNSUB_TEST"))
        assert len(received) == 1  # not called again

    @pytest.mark.asyncio
    async def test_timeline(self, bus):
        for i in range(5):
            await bus.publish(
                self._make_event(entity_type="position", entity_id="p1", data={"i": i})
            )

        timeline = bus.get_timeline("position", "p1")
        assert len(timeline) == 5

    @pytest.mark.asyncio
    async def test_get_all_events(self, bus):
        for i in range(10):
            await bus.publish(self._make_event(entity_id=str(i)))

        all_events = bus.get_all_events(limit=5)
        assert len(all_events) == 5

    @pytest.mark.asyncio
    async def test_priority_ordering(self, bus):
        order = []

        def handler_normal(evt):
            order.append("normal")

        def handler_critical(evt):
            order.append("critical")

        bus.subscribe("PRIO_TEST", handler_normal, priority=EventPriority.NORMAL)
        bus.subscribe("PRIO_TEST", handler_critical, priority=EventPriority.CRITICAL)
        await bus.publish(self._make_event(event_type="PRIO_TEST"))

        assert order[0] == "critical"
        assert order[1] == "normal"

    @pytest.mark.asyncio
    async def test_handler_error_does_not_propagate(self, bus):
        def bad_handler(evt):
            raise ValueError("boom")

        received = []

        def good_handler(evt):
            received.append(evt)

        bus.subscribe("ERR_TEST", bad_handler)
        bus.subscribe("ERR_TEST", good_handler)

        await bus.publish(self._make_event(event_type="ERR_TEST"))
        assert len(received) == 1  # good handler still called

    @pytest.mark.asyncio
    async def test_entity_subscription(self, bus):
        received = []

        def handler(evt):
            received.append(evt)

        bus.subscribe_entity("position", "p42", handler)
        await bus.publish(self._make_event(entity_type="position", entity_id="p42"))
        await bus.publish(self._make_event(entity_type="position", entity_id="p99"))

        assert len(received) == 1  # only p42

    @pytest.mark.asyncio
    async def test_get_events_since(self, bus):
        t0 = time.time()
        await bus.publish(self._make_event(data={"seq": 1}))
        await asyncio.sleep(0.01)
        t1 = time.time()
        await bus.publish(self._make_event(data={"seq": 2}))

        events = bus.get_events_since(t1)
        assert len(events) >= 1
        assert events[-1].data.get("seq") == 2


# ---------------------------------------------------------------------------
# EventRegistry
# ---------------------------------------------------------------------------


class TestEventRegistry:
    def test_registry_not_empty(self):
        assert len(EVENT_REGISTRY) > 30

    def test_get_event_meta(self):
        meta = get_event_meta("SIGNAL_GENERATED")
        assert meta is not None
        assert "label" in meta
        assert meta["entity_type"] == "position"

    def test_get_valid_transitions(self):
        transitions = get_valid_transitions("SIGNAL_GENERATED")
        assert "RISK_CHECK_PASSED" in transitions
        assert "RISK_CHECK_REJECTED" in transitions

    def test_get_causes(self):
        causes = get_causes("RISK_CHECK_PASSED")
        assert "SIGNAL_GENERATED" in causes

    def test_get_entity_events(self):
        position_events = get_entity_events("position")
        assert "SIGNAL_GENERATED" in position_events
        assert "POSITION_OPENED" in position_events
        assert "POSITION_CLOSED" in position_events

    def test_unknown_event_returns_empty(self):
        meta = get_event_meta("NONEXISTENT")
        assert meta is None or meta == {}
        assert get_valid_transitions("NONEXISTENT") == []


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def _evt(event_type, entity_type="position", entity_id="p1", ts=None, **data):
    return DomainEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        data=data,
        bot_name="test",
        ts=ts or time.time(),
    )


class TestReducePosition:
    def test_full_lifecycle(self):
        events = [
            _evt("SIGNAL_GENERATED", direction="LONG", strategy="trend_follower", ts=1.0),
            _evt("ORDER_PLACED", amount=0.5, ts=2.0),
            _evt("POSITION_OPENED", entry_price=100.0, stop_loss=95.0, take_profit=110.0, ts=3.0),
            _evt("POSITION_UPDATED", unrealized_pnl=5.0, ts=4.0),
            _evt("POSITION_CLOSED", exit_price=108.0, pnl=4.0, ts=5.0),
        ]
        state = reduce_position(events)
        assert state["status"] == "closed"
        assert state["direction"] == "LONG"
        assert state["entry_price"] == 100.0
        assert state["exit_price"] == 108.0
        assert state["pnl"] == 4.0

    def test_rejected_signal(self):
        events = [
            _evt("SIGNAL_GENERATED", direction="SHORT", ts=1.0),
            _evt("RISK_CHECK_REJECTED", ts=2.0),
        ]
        state = reduce_position(events)
        assert state["status"] == "rejected"


class TestReduceRegime:
    def test_regime_transition(self):
        events = [
            _evt("REGIME_DETECTED", entity_type="regime", entity_id="r1", regime="tight_range", confidence=0.8, ts=1.0),
            _evt("PRE_SWITCH_ENTERED", entity_type="regime", entity_id="r1", target_regime="bull_trend", ts=2.0),
            _evt("SWITCH_CONFIRMED", entity_type="regime", entity_id="r1", ts=3.0),
            _evt("TRANSITION_COMPLETE", entity_type="regime", entity_id="r1", new_regime="bull_trend", ts=4.0),
        ]
        state = reduce_regime(events)
        assert state["current_regime"] == "bull_trend"
        assert state["transitions"] == 1
        assert state["phase"] == "stable"

    def test_cancelled_pre_switch(self):
        events = [
            _evt("REGIME_DETECTED", entity_type="regime", entity_id="r1", regime="tight_range", ts=1.0),
            _evt("PRE_SWITCH_ENTERED", entity_type="regime", entity_id="r1", target_regime="bull_trend", ts=2.0),
            _evt("PRE_SWITCH_CANCELLED", entity_type="regime", entity_id="r1", ts=3.0),
        ]
        state = reduce_regime(events)
        assert state["phase"] == "stable"
        assert state["cancelled_switches"] == 1


class TestReducePortfolio:
    def test_portfolio_tracking(self):
        events = [
            _evt("BALANCE_UPDATED", entity_type="portfolio", entity_id="main", balance=10000.0, ts=1.0),
            _evt("PNL_REALIZED", entity_type="portfolio", entity_id="main", pnl=500.0, ts=2.0),
            _evt("FEE_CHARGED", entity_type="portfolio", entity_id="main", fee=10.0, ts=3.0),
            _evt("BALANCE_UPDATED", entity_type="portfolio", entity_id="main", balance=10490.0, ts=4.0),
            _evt("PNL_REALIZED", entity_type="portfolio", entity_id="main", pnl=-200.0, ts=5.0),
        ]
        state = reduce_portfolio(events)
        assert state["initial_balance"] == 10000.0
        assert state["balance"] == 10490.0
        assert state["total_pnl"] == 300.0
        assert state["total_fees"] == 10.0
        assert state["wins"] == 1
        assert state["losses"] == 1


class TestReduceStrategy:
    def test_strategy_lifecycle(self):
        events = [
            _evt("STRATEGY_ACTIVATED", entity_type="strategy", entity_id="grid", ts=1.0),
            _evt("STRATEGY_SIGNAL", entity_type="strategy", entity_id="grid", ts=2.0),
            _evt("STRATEGY_SIGNAL", entity_type="strategy", entity_id="grid", ts=3.0),
            _evt("STRATEGY_ERROR", entity_type="strategy", entity_id="grid", ts=4.0),
            _evt("STRATEGY_DEACTIVATED", entity_type="strategy", entity_id="grid", ts=5.0),
        ]
        state = reduce_strategy(events)
        assert state["status"] == "inactive"
        assert state["signals_generated"] == 2
        assert state["errors"] == 1


class TestGetStateAt:
    def test_temporal_replay(self):
        events = [
            _evt("REGIME_DETECTED", entity_type="regime", entity_id="r1", regime="tight_range", confidence=0.8, ts=1.0),
            _evt("TRANSITION_COMPLETE", entity_type="regime", entity_id="r1", new_regime="bull_trend", ts=5.0),
            _evt("TRANSITION_COMPLETE", entity_type="regime", entity_id="r1", new_regime="bear_trend", ts=10.0),
        ]
        # At t=3, should only see first event
        state_at_3 = get_state_at(events, "regime", timestamp=3.0)
        assert state_at_3["current_regime"] == "tight_range"

        # At t=7, should see transition to bull_trend
        state_at_7 = get_state_at(events, "regime", timestamp=7.0)
        assert state_at_7["current_regime"] == "bull_trend"

        # At t=12, should see transition to bear_trend
        state_at_12 = get_state_at(events, "regime", timestamp=12.0)
        assert state_at_12["current_regime"] == "bear_trend"
