"""Tests for CrossEntityAutomation."""

import pytest
from bot.core.event_bus import DomainEvent, EventBus
from bot.core.automation import AutomationRule, CrossEntityAutomation


@pytest.fixture
def bus():
    return EventBus(bot_name="test")


@pytest.fixture
def automation(bus):
    a = CrossEntityAutomation(bus)
    a.register_defaults()
    return a


class TestAutomationRules:
    @pytest.mark.asyncio
    async def test_portfolio_stop_loss_cascade(self, bus, automation):
        """Portfolio stop-loss should trigger strategy deactivation."""
        await automation.start()

        fired_events = []
        bus.subscribe("STRATEGY_DEACTIVATED", lambda e: fired_events.append(e))
        bus.subscribe("BOT_EMERGENCY_STOP", lambda e: fired_events.append(e))

        # Fire portfolio stop-loss
        trigger = DomainEvent(
            entity_type="risk",
            entity_id="portfolio",
            event_type="PORTFOLIO_STOP_LOSS",
            data={"loss_pct": 0.25},
            bot_name="test_bot",
        )
        await bus.publish(trigger)

        assert len(fired_events) == 2
        types = {e.event_type for e in fired_events}
        assert "STRATEGY_DEACTIVATED" in types
        assert "BOT_EMERGENCY_STOP" in types

        await automation.stop()

    @pytest.mark.asyncio
    async def test_loss_streak_detection(self, bus, automation):
        """3 consecutive losses should trigger RISK_HALTED."""
        await automation.start()

        halted = []
        bus.subscribe("RISK_HALTED", lambda e: halted.append(e))

        # Fire 3 losing positions
        for i in range(3):
            evt = DomainEvent(
                entity_type="position",
                entity_id=f"pos_{i}",
                event_type="POSITION_CLOSED",
                data={"pnl": -100},
                bot_name="test",
            )
            await bus.publish(evt)

        assert len(halted) == 1
        assert halted[0].data.get("streak") == 3

        await automation.stop()

    @pytest.mark.asyncio
    async def test_loss_streak_reset_on_win(self, bus, automation):
        """A winning trade should reset the loss streak counter."""
        await automation.start()

        halted = []
        bus.subscribe("RISK_HALTED", lambda e: halted.append(e))

        # 2 losses, then a win, then 2 more losses
        for pnl in [-100, -50, 200, -100, -50]:
            evt = DomainEvent(
                entity_type="position",
                entity_id="pos",
                event_type="POSITION_CLOSED",
                data={"pnl": pnl},
                bot_name="test",
            )
            await bus.publish(evt)

        assert len(halted) == 0  # streak was broken by win

        await automation.stop()

    @pytest.mark.asyncio
    async def test_committee_reject_cascade(self, bus, automation):
        """Committee REJECT should produce TRADE_REJECTED event."""
        await automation.start()

        rejected = []
        bus.subscribe("TRADE_REJECTED", lambda e: rejected.append(e))

        evt = DomainEvent(
            entity_type="session",
            entity_id="session_123",
            event_type="DECISION_MADE",
            data={"verdict": "reject", "score": 0.3, "reasoning": "Too risky"},
            bot_name="test",
        )
        await bus.publish(evt)

        assert len(rejected) == 1
        assert "Committee rejected" in rejected[0].data.get("reason", "")

        await automation.stop()

    @pytest.mark.asyncio
    async def test_cooldown_prevents_spam(self, bus, automation):
        """Cooldown should prevent rapid re-firing."""
        await automation.start()

        fired = []
        bus.subscribe("STRATEGY_DEACTIVATED", lambda e: fired.append(e))

        # Fire twice quickly
        for _ in range(2):
            evt = DomainEvent(
                entity_type="risk",
                entity_id="portfolio",
                event_type="PORTFOLIO_STOP_LOSS",
                data={},
                bot_name="test",
            )
            await bus.publish(evt)

        # Only first should fire (cooldown=60s)
        deactivated = [e for e in fired if e.event_type == "STRATEGY_DEACTIVATED"]
        assert len(deactivated) == 1

        await automation.stop()

    @pytest.mark.asyncio
    async def test_disable_rule(self, bus, automation):
        """Disabled rules should not fire."""
        automation.disable_rule("portfolio_stop_loss_cascade")
        await automation.start()

        fired = []
        bus.subscribe("STRATEGY_DEACTIVATED", lambda e: fired.append(e))

        evt = DomainEvent(
            entity_type="risk",
            entity_id="portfolio",
            event_type="PORTFOLIO_STOP_LOSS",
            data={},
            bot_name="test",
        )
        await bus.publish(evt)

        assert len(fired) == 0

        await automation.stop()

    @pytest.mark.asyncio
    async def test_causal_chain(self, bus, automation):
        """Automation events should have causes pointing to trigger."""
        await automation.start()

        fired = []
        bus.subscribe("RISK_HALTED", lambda e: fired.append(e))

        trigger = DomainEvent(
            entity_type="risk",
            entity_id="daily",
            event_type="DAILY_LOSS_LIMIT_HIT",
            data={"daily_loss": 500},
            bot_name="test",
        )
        await bus.publish(trigger)

        assert len(fired) == 1
        assert trigger.event_id in fired[0].causes  # causal link

        await automation.stop()


class TestAutomationStatus:
    def test_status(self, automation):
        status = automation.get_status()
        assert status["total_rules"] >= 8
        assert status["active_rules"] >= 8

    def test_custom_rule(self, bus):
        auto = CrossEntityAutomation(bus)
        auto.add_rule(AutomationRule(
            name="custom_test",
            description="Test rule",
            trigger_event="TEST_TRIGGER",
            condition=lambda e: True,
            actions=[{"entity_type": "test", "entity_id": "1", "event_type": "TEST_OUTPUT", "data": {}}],
        ))
        assert len(auto.rules) == 1
        assert auto.rules[0].name == "custom_test"
