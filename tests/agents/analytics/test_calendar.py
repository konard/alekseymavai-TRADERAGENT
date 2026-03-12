from __future__ import annotations

import uuid

import pytest

from bot.agents.analytics.calendar import (
    EventCalendar,
    MarketEvent,
    Scenario,
    ScenarioPlanner,
)

# ── helpers ──────────────────────────────────────────────────────────────

BASE_TS = 1_700_000_000.0  # fixed reference timestamp


def _make_event(
    *,
    name: str = "Test Event",
    category: str = "macro",
    impact: str = "medium",
    offset_hours: float = 2.0,
    risk_reduction_hours: float = 4.0,
    tags: list[str] | None = None,
) -> MarketEvent:
    return MarketEvent(
        event_id=uuid.uuid4().hex[:12],
        name=name,
        category=category,
        impact=impact,
        scheduled_ts=BASE_TS + offset_hours * 3600,
        risk_reduction_hours=risk_reduction_hours,
        description=f"{name} description",
        tags=tags or [],
    )


# =====================================================================
# EventCalendar tests
# =====================================================================


class TestEventCalendarAdd:
    def test_add_event(self):
        cal = EventCalendar()
        ev = _make_event()
        cal.add_event(ev)
        assert cal.get_stats()["total_events"] == 1

    def test_add_recurring(self):
        cal = EventCalendar()
        cal.add_recurring("FOMC", "macro", "high", "Every 6 weeks")
        assert cal.get_stats()["recurring_definitions"] == 1


class TestEventCalendarUpcoming:
    def test_get_upcoming_within_window(self):
        cal = EventCalendar()
        cal.add_event(_make_event(offset_hours=5))
        result = cal.get_upcoming(hours_ahead=12, current_ts=BASE_TS)
        assert len(result) == 1

    def test_event_outside_window_excluded(self):
        cal = EventCalendar()
        cal.add_event(_make_event(offset_hours=48))
        result = cal.get_upcoming(hours_ahead=24, current_ts=BASE_TS)
        assert len(result) == 0

    def test_upcoming_sorted_by_time(self):
        cal = EventCalendar()
        cal.add_event(_make_event(name="Later", offset_hours=10))
        cal.add_event(_make_event(name="Sooner", offset_hours=2))
        result = cal.get_upcoming(hours_ahead=24, current_ts=BASE_TS)
        assert result[0].name == "Sooner"
        assert result[1].name == "Later"


class TestEventCalendarRiskAdjustment:
    def test_no_event_no_reduction(self):
        cal = EventCalendar()
        adj = cal.get_risk_adjustment(current_ts=BASE_TS)
        assert adj["should_reduce"] is False
        assert adj["risk_multiplier"] == 1.0
        assert adj["next_event"] is None
        assert adj["hours_until"] is None

    def test_medium_impact_multiplier(self):
        cal = EventCalendar()
        # Event in 2 hours, risk_reduction_hours=4 => we are within the window
        cal.add_event(_make_event(impact="medium", offset_hours=2, risk_reduction_hours=4))
        adj = cal.get_risk_adjustment(current_ts=BASE_TS)
        assert adj["should_reduce"] is True
        assert adj["risk_multiplier"] == 0.7

    def test_critical_impact_multiplier(self):
        cal = EventCalendar()
        cal.add_event(_make_event(impact="critical", offset_hours=1, risk_reduction_hours=4))
        adj = cal.get_risk_adjustment(current_ts=BASE_TS)
        assert adj["should_reduce"] is True
        assert adj["risk_multiplier"] == 0.3

    def test_multiple_events_most_conservative_wins(self):
        cal = EventCalendar()
        cal.add_event(_make_event(name="Med", impact="medium", offset_hours=3, risk_reduction_hours=4))
        cal.add_event(_make_event(name="Crit", impact="critical", offset_hours=2, risk_reduction_hours=4))
        adj = cal.get_risk_adjustment(current_ts=BASE_TS)
        assert adj["risk_multiplier"] == 0.3
        assert "Crit" in adj["reason"]

    def test_event_outside_risk_window_no_reduction(self):
        cal = EventCalendar()
        # Event in 10 hours but risk_reduction_hours=4 => outside window
        cal.add_event(_make_event(impact="critical", offset_hours=10, risk_reduction_hours=4))
        adj = cal.get_risk_adjustment(current_ts=BASE_TS)
        assert adj["should_reduce"] is False
        assert adj["risk_multiplier"] == 1.0


class TestEventCalendarRemove:
    def test_remove_existing(self):
        cal = EventCalendar()
        ev = _make_event()
        cal.add_event(ev)
        assert cal.remove_event(ev.event_id) is True
        assert cal.get_stats()["total_events"] == 0

    def test_remove_nonexistent(self):
        cal = EventCalendar()
        assert cal.remove_event("nonexistent") is False


class TestEventCalendarPastEvents:
    def test_past_events(self):
        cal = EventCalendar()
        # Event 2 hours ago relative to current_ts
        cal.add_event(_make_event(offset_hours=-2))
        result = cal.get_past_events(hours_back=24, current_ts=BASE_TS)
        assert len(result) == 1

    def test_past_events_outside_window(self):
        cal = EventCalendar()
        cal.add_event(_make_event(offset_hours=-48))
        result = cal.get_past_events(hours_back=24, current_ts=BASE_TS)
        assert len(result) == 0


class TestEventCalendarCategory:
    def test_filter_by_category(self):
        cal = EventCalendar()
        cal.add_event(_make_event(category="macro"))
        cal.add_event(_make_event(category="crypto"))
        cal.add_event(_make_event(category="macro"))
        assert len(cal.get_events_by_category("macro")) == 2
        assert len(cal.get_events_by_category("crypto")) == 1
        assert len(cal.get_events_by_category("earnings")) == 0


class TestEventCalendarDefault:
    def test_default_calendar_has_five_events(self):
        cal = EventCalendar.create_default_calendar(BASE_TS)
        stats = cal.get_stats()
        assert stats["total_events"] == 5
        assert "crypto" in stats["by_category"]
        assert "macro" in stats["by_category"]


class TestMarketEventToDict:
    def test_to_dict_keys(self):
        ev = _make_event(tags=["fed", "rates"])
        d = ev.to_dict()
        assert d["name"] == "Test Event"
        assert d["tags"] == ["fed", "rates"]
        assert "event_id" in d


# =====================================================================
# ScenarioPlanner tests
# =====================================================================


def _make_scenario(
    *,
    name: str = "Test Crash",
    probability: float = 0.1,
    impact_score: float = -0.5,
    triggers: list[str] | None = None,
    actions: list[dict] | None = None,
) -> Scenario:
    return Scenario(
        scenario_id=uuid.uuid4().hex[:12],
        name=name,
        description=f"{name} desc",
        probability=probability,
        impact_score=impact_score,
        triggers=triggers or ["btc_change_24h < -10"],
        actions=actions or [{"action": "reduce_all", "pct": 50}],
    )


class TestScenarioPlannerAdd:
    def test_add_scenario(self):
        sp = ScenarioPlanner()
        sc = _make_scenario()
        sp.add_scenario(sc)
        assert sp.get_stats()["total_scenarios"] == 1


class TestScenarioPlannerTriggers:
    def test_trigger_match(self):
        sp = ScenarioPlanner()
        sp.add_scenario(_make_scenario(triggers=["btc_change_24h < -10"]))
        matched = sp.check_triggers({"btc_change_24h": -15})
        assert len(matched) == 1

    def test_trigger_no_match(self):
        sp = ScenarioPlanner()
        sp.add_scenario(_make_scenario(triggers=["btc_change_24h < -10"]))
        matched = sp.check_triggers({"btc_change_24h": 5})
        assert len(matched) == 0

    def test_multiple_triggers_all_must_match(self):
        sp = ScenarioPlanner()
        sp.add_scenario(
            _make_scenario(
                triggers=["btc_change_24h < -10", "fear_greed < 20"]
            )
        )
        # Only one condition met
        assert len(sp.check_triggers({"btc_change_24h": -15, "fear_greed": 50})) == 0
        # Both met
        assert len(sp.check_triggers({"btc_change_24h": -15, "fear_greed": 10})) == 1

    def test_boolean_trigger(self):
        sp = ScenarioPlanner()
        sp.add_scenario(_make_scenario(triggers=["etf_approved == True"]))
        assert len(sp.check_triggers({"etf_approved": True})) == 1
        assert len(sp.check_triggers({"etf_approved": False})) == 0


class TestScenarioPlannerActionPlan:
    def test_get_action_plan(self):
        sp = ScenarioPlanner()
        sc = _make_scenario(actions=[{"action": "close_all"}, {"action": "hedge", "symbol": "ETH"}])
        sp.add_scenario(sc)
        plan = sp.get_action_plan(sc.scenario_id)
        assert len(plan) == 2
        assert plan[0]["action"] == "close_all"

    def test_action_plan_unknown_id(self):
        sp = ScenarioPlanner()
        assert sp.get_action_plan("missing") == []


class TestScenarioPlannerPortfolio:
    def test_evaluate_portfolio_impact_negative(self):
        sp = ScenarioPlanner()
        sc = _make_scenario(impact_score=-0.5)
        portfolio = {
            "total_value": 10_000.0,
            "positions": [{"symbol": "BTCUSDT", "value": 5000}],
        }
        result = sp.evaluate_portfolio_impact(sc, portfolio)
        assert result["estimated_loss"] == pytest.approx(5000.0)
        assert len(result["affected_positions"]) == 1
        assert len(result["recommended_actions"]) >= 1

    def test_evaluate_portfolio_impact_positive(self):
        sp = ScenarioPlanner()
        sc = _make_scenario(impact_score=0.5)
        portfolio = {"total_value": 10_000.0, "positions": []}
        result = sp.evaluate_portfolio_impact(sc, portfolio)
        assert result["estimated_loss"] == 0.0
        assert result["affected_positions"] == []


class TestScenarioPlannerStatusManagement:
    def test_trigger_manually(self):
        sp = ScenarioPlanner()
        sc = _make_scenario()
        sp.add_scenario(sc)
        triggered = sp.trigger_scenario(sc.scenario_id)
        assert triggered is not None
        assert triggered.status == "triggered"
        # Should no longer appear in active list
        assert len(sp.get_active_scenarios()) == 0

    def test_trigger_unknown_returns_none(self):
        sp = ScenarioPlanner()
        assert sp.trigger_scenario("nope") is None

    def test_expire_scenario(self):
        sp = ScenarioPlanner()
        sc = _make_scenario()
        sp.add_scenario(sc)
        sp.expire_scenario(sc.scenario_id)
        assert sc.status == "expired"
        assert len(sp.get_active_scenarios()) == 0

    def test_expired_not_checked(self):
        sp = ScenarioPlanner()
        sc = _make_scenario(triggers=["btc_change_24h < -10"])
        sp.add_scenario(sc)
        sp.expire_scenario(sc.scenario_id)
        assert len(sp.check_triggers({"btc_change_24h": -20})) == 0


class TestScenarioPlannerDefaults:
    def test_default_scenarios(self):
        sp = ScenarioPlanner.create_default_scenarios()
        stats = sp.get_stats()
        assert stats["total_scenarios"] == 5
        assert stats["by_status"]["active"] == 5
        names = {s.name for s in sp.get_active_scenarios()}
        assert "BTC Flash Crash" in names
        assert "Black Swan" in names
        assert "ETF Approval" in names


class TestScenarioToDict:
    def test_to_dict(self):
        sc = _make_scenario()
        d = sc.to_dict()
        assert d["status"] == "active"
        assert isinstance(d["triggers"], list)
        assert isinstance(d["actions"], list)
