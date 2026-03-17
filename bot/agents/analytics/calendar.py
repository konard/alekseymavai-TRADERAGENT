from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class MarketEvent:
    """A scheduled market event that may affect trading risk."""

    event_id: str
    name: str
    category: str  # "macro", "crypto", "earnings", "regulatory"
    impact: str  # "low", "medium", "high", "critical"
    scheduled_ts: float
    risk_reduction_hours: float = 4.0
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "name": self.name,
            "category": self.category,
            "impact": self.impact,
            "scheduled_ts": self.scheduled_ts,
            "risk_reduction_hours": self.risk_reduction_hours,
            "description": self.description,
            "tags": list(self.tags),
        }


class EventCalendar:
    """Calendar of market events with risk-adjustment logic."""

    _IMPACT_MULTIPLIER = {
        "low": 1.0,
        "medium": 0.7,
        "high": 0.5,
        "critical": 0.3,
    }

    def __init__(self) -> None:
        self._events: dict[str, MarketEvent] = {}
        self._recurring: list[dict] = []

    # --- mutators ---

    def add_event(self, event: MarketEvent) -> None:
        self._events[event.event_id] = event

    def add_recurring(
        self,
        name: str,
        category: str,
        impact: str,
        cron_description: str,
        risk_reduction_hours: float = 4.0,
    ) -> None:
        self._recurring.append(
            {
                "name": name,
                "category": category,
                "impact": impact,
                "cron_description": cron_description,
                "risk_reduction_hours": risk_reduction_hours,
            }
        )

    def remove_event(self, event_id: str) -> bool:
        return self._events.pop(event_id, None) is not None

    # --- queries ---

    def get_upcoming(
        self, hours_ahead: float = 24, current_ts: float | None = None
    ) -> list[MarketEvent]:
        now = current_ts if current_ts is not None else time.time()
        cutoff = now + hours_ahead * 3600
        upcoming = [e for e in self._events.values() if now <= e.scheduled_ts <= cutoff]
        upcoming.sort(key=lambda e: e.scheduled_ts)
        return upcoming

    def get_risk_adjustment(self, current_ts: float | None = None) -> dict:
        now = current_ts if current_ts is not None else time.time()

        best_multiplier = 1.0
        best_reason = ""
        best_event: MarketEvent | None = None
        best_hours: float | None = None

        for event in self._events.values():
            hours_until = (event.scheduled_ts - now) / 3600
            if 0 <= hours_until <= event.risk_reduction_hours:
                mult = self._IMPACT_MULTIPLIER.get(event.impact, 1.0)
                if mult < best_multiplier:
                    best_multiplier = mult
                    best_reason = f"{event.name} in {hours_until:.1f}h " f"(impact={event.impact})"
                    best_event = event
                    best_hours = hours_until

        return {
            "should_reduce": best_multiplier < 1.0,
            "risk_multiplier": best_multiplier,
            "reason": best_reason,
            "next_event": best_event.to_dict() if best_event else None,
            "hours_until": best_hours,
        }

    def get_past_events(
        self, hours_back: float = 24, current_ts: float | None = None
    ) -> list[MarketEvent]:
        now = current_ts if current_ts is not None else time.time()
        cutoff = now - hours_back * 3600
        past = [e for e in self._events.values() if cutoff <= e.scheduled_ts < now]
        past.sort(key=lambda e: e.scheduled_ts)
        return past

    def get_events_by_category(self, category: str) -> list[MarketEvent]:
        return [e for e in self._events.values() if e.category == category]

    def get_stats(self) -> dict:
        categories: dict[str, int] = {}
        impacts: dict[str, int] = {}
        for e in self._events.values():
            categories[e.category] = categories.get(e.category, 0) + 1
            impacts[e.impact] = impacts.get(e.impact, 0) + 1
        return {
            "total_events": len(self._events),
            "recurring_definitions": len(self._recurring),
            "by_category": categories,
            "by_impact": impacts,
        }

    # --- factory ---

    @classmethod
    def create_default_calendar(cls, base_ts: float) -> EventCalendar:
        cal = cls()
        defaults = [
            ("BTC Halving", "crypto", "critical", 30 * 24, 48.0, "Bitcoin block reward halving"),
            (
                "FOMC Meeting",
                "macro",
                "high",
                7 * 24,
                4.0,
                "Federal Reserve interest rate decision",
            ),
            ("CPI Release", "macro", "high", 3 * 24, 4.0, "Consumer Price Index release"),
            ("ETH Upgrade", "crypto", "medium", 14 * 24, 8.0, "Ethereum network upgrade"),
            ("Options Expiry", "crypto", "medium", 5 * 24, 2.0, "Monthly crypto options expiry"),
        ]
        for name, cat, impact, offset_h, rr_h, desc in defaults:
            cal.add_event(
                MarketEvent(
                    event_id=uuid.uuid4().hex[:12],
                    name=name,
                    category=cat,
                    impact=impact,
                    scheduled_ts=base_ts + offset_h * 3600,
                    risk_reduction_hours=rr_h,
                    description=desc,
                )
            )
        return cal


# ---------------------------------------------------------------------------
# Scenario planning
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    """A pre-defined market scenario with triggers and response actions."""

    scenario_id: str
    name: str
    description: str
    probability: float  # 0-1
    impact_score: float  # -1 to 1
    triggers: list[str]
    actions: list[dict]
    status: str = "active"  # "active", "triggered", "expired"

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "description": self.description,
            "probability": self.probability,
            "impact_score": self.impact_score,
            "triggers": list(self.triggers),
            "actions": [dict(a) for a in self.actions],
            "status": self.status,
        }


_TRIGGER_RE = re.compile(r"^(\w+)\s*(==|!=|<|>|<=|>=)\s*(.+)$")


def _evaluate_trigger(trigger: str, conditions: dict) -> bool:
    m = _TRIGGER_RE.match(trigger.strip())
    if not m:
        return False
    key, op, raw_value = m.group(1), m.group(2), m.group(3).strip()

    if key not in conditions:
        return False

    actual = conditions[key]

    # Try to interpret the expected value in the same type as actual
    if isinstance(actual, bool):
        expected = raw_value in ("True", "true", "1")
    elif isinstance(actual, (int, float)):
        try:
            expected = float(raw_value)
        except ValueError:
            return False
    else:
        expected = raw_value

    ops = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b,
        ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b,
        ">=": lambda a, b: a >= b,
    }
    try:
        return ops[op](actual, expected)
    except TypeError:
        return False


class ScenarioPlanner:
    """Manages pre-defined market scenarios and trigger evaluation."""

    def __init__(self) -> None:
        self._scenarios: dict[str, Scenario] = {}

    def add_scenario(self, scenario: Scenario) -> None:
        self._scenarios[scenario.scenario_id] = scenario

    def check_triggers(self, current_conditions: dict) -> list[Scenario]:
        matched: list[Scenario] = []
        for sc in self._scenarios.values():
            if sc.status != "active":
                continue
            if not sc.triggers:
                continue
            if all(_evaluate_trigger(t, current_conditions) for t in sc.triggers):
                matched.append(sc)
        return matched

    def get_action_plan(self, scenario_id: str) -> list[dict]:
        sc = self._scenarios.get(scenario_id)
        if sc is None:
            return []
        return [dict(a) for a in sc.actions]

    def evaluate_portfolio_impact(self, scenario: Scenario, portfolio: dict) -> dict:
        total_value = portfolio.get("total_value", 0.0)
        estimated_loss = (
            abs(total_value * scenario.impact_score) if scenario.impact_score < 0 else 0.0
        )

        positions = portfolio.get("positions", [])
        affected = [p for p in positions] if scenario.impact_score < 0 else []

        return {
            "estimated_loss": estimated_loss,
            "affected_positions": affected,
            "recommended_actions": [dict(a) for a in scenario.actions],
        }

    def get_active_scenarios(self) -> list[Scenario]:
        return [s for s in self._scenarios.values() if s.status == "active"]

    def trigger_scenario(self, scenario_id: str) -> Scenario | None:
        sc = self._scenarios.get(scenario_id)
        if sc is not None:
            sc.status = "triggered"
        return sc

    def expire_scenario(self, scenario_id: str) -> None:
        sc = self._scenarios.get(scenario_id)
        if sc is not None:
            sc.status = "expired"

    def get_stats(self) -> dict:
        statuses: dict[str, int] = {}
        for s in self._scenarios.values():
            statuses[s.status] = statuses.get(s.status, 0) + 1
        return {
            "total_scenarios": len(self._scenarios),
            "by_status": statuses,
        }

    @classmethod
    def create_default_scenarios(cls) -> ScenarioPlanner:
        planner = cls()
        defaults = [
            Scenario(
                scenario_id=uuid.uuid4().hex[:12],
                name="BTC Flash Crash",
                description="Bitcoin drops more than 15% in 24 hours",
                probability=0.05,
                impact_score=-0.7,
                triggers=["btc_change_24h < -15"],
                actions=[
                    {"action": "reduce_all", "pct": 75},
                    {"action": "hedge", "symbol": "BTCUSDT"},
                ],
            ),
            Scenario(
                scenario_id=uuid.uuid4().hex[:12],
                name="Market Euphoria",
                description="Fear & Greed index exceeds 90",
                probability=0.10,
                impact_score=-0.3,
                triggers=["fear_greed > 90"],
                actions=[
                    {"action": "reduce_all", "pct": 30},
                    {"action": "tighten_stops"},
                ],
            ),
            Scenario(
                scenario_id=uuid.uuid4().hex[:12],
                name="Black Swan",
                description="Extreme volatility spike above 300%",
                probability=0.01,
                impact_score=-0.9,
                triggers=["volatility_spike > 300"],
                actions=[
                    {"action": "close_all"},
                    {"action": "halt_trading"},
                ],
            ),
            Scenario(
                scenario_id=uuid.uuid4().hex[:12],
                name="ETF Approval",
                description="Crypto ETF is officially approved",
                probability=0.15,
                impact_score=0.5,
                triggers=["etf_approved == True"],
                actions=[
                    {"action": "increase_allocation", "pct": 50},
                ],
            ),
            Scenario(
                scenario_id=uuid.uuid4().hex[:12],
                name="Exchange Hack",
                description="Major exchange outflow spike detected",
                probability=0.03,
                impact_score=-0.8,
                triggers=["exchange_outflow_spike > 500"],
                actions=[
                    {"action": "withdraw_all"},
                    {"action": "halt_trading"},
                ],
            ),
        ]
        for sc in defaults:
            planner.add_scenario(sc)
        return planner
