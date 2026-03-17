"""Cross-Entity Automation — cascading event rules engine.

Subscribes to EventBus events and triggers automated side-effects
across entity boundaries. Inspired by VentureOS crossEntityAutomation.

Examples:
- PORTFOLIO_STOP_LOSS → auto STRATEGY_DEACTIVATED for all strategies
- 3x consecutive POSITION_CLOSED(pnl<0) → RISK_HALTED + BOT_PAUSED
- REGIME_CHANGE to bull_trend → STRATEGY_ACTIVATED(trend_follower)
- DAILY_LOSS_LIMIT_HIT → all strategies paused
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bot.utils.logger import get_logger

if TYPE_CHECKING:
    from bot.core.event_bus import DomainEvent, EventBus

logger = get_logger(__name__)


@dataclass
class AutomationRule:
    """A single automation rule: when trigger_event fires, produce side-effect events."""

    name: str
    description: str
    trigger_event: str  # event_type to listen for
    condition: Callable[[DomainEvent], bool]  # must return True to fire
    actions: list[dict[str, Any]]  # list of {entity_type, entity_id_fn, event_type, data_fn}
    enabled: bool = True
    cooldown_seconds: float = 0  # min time between firings
    _last_fired: float = 0
    fire_count: int = 0

    def can_fire(self) -> bool:
        if not self.enabled:
            return False
        if self.cooldown_seconds > 0:
            return (time.time() - self._last_fired) >= self.cooldown_seconds
        return True

    def mark_fired(self) -> None:
        self._last_fired = time.time()
        self.fire_count += 1


class CrossEntityAutomation:
    """Engine that manages automation rules and fires cascading events.

    Usage:
        automation = CrossEntityAutomation(event_bus)
        automation.register_defaults()  # register built-in trading rules
        await automation.start()  # subscribe to EventBus
    """

    def __init__(self, event_bus: EventBus):
        self._bus = event_bus
        self._rules: list[AutomationRule] = []
        self._unsubscribers: list[Callable] = []
        self._consecutive_losses: int = 0  # track for loss streak rule
        self._loss_streak_threshold: int = 3
        self._history: list[dict[str, Any]] = []
        self._max_history: int = 200

    @property
    def rules(self) -> list[AutomationRule]:
        return list(self._rules)

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

    def add_rule(self, rule: AutomationRule) -> None:
        """Register an automation rule."""
        self._rules.append(rule)
        logger.info("automation_rule_added", name=rule.name, trigger=rule.trigger_event)

    def remove_rule(self, name: str) -> None:
        """Remove a rule by name."""
        self._rules = [r for r in self._rules if r.name != name]

    def enable_rule(self, name: str) -> None:
        for r in self._rules:
            if r.name == name:
                r.enabled = True

    def disable_rule(self, name: str) -> None:
        for r in self._rules:
            if r.name == name:
                r.enabled = False

    async def start(self) -> None:
        """Subscribe to all trigger events."""
        triggers = {r.trigger_event for r in self._rules}
        for trigger in triggers:
            unsub = self._bus.subscribe(trigger, self._on_event)
            self._unsubscribers.append(unsub)
        logger.info("automation_started", rules=len(self._rules), triggers=len(triggers))

    async def stop(self) -> None:
        """Unsubscribe from all events."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()
        logger.info("automation_stopped")

    async def _on_event(self, event: DomainEvent) -> None:
        """Handle incoming event — check rules and fire actions."""
        for rule in self._rules:
            if rule.trigger_event != event.event_type:
                continue
            if not rule.can_fire():
                continue
            try:
                if not rule.condition(event):
                    continue
            except Exception as e:
                logger.warning("automation_condition_error", rule=rule.name, error=str(e))
                continue

            # Fire all actions
            for action in rule.actions:
                try:
                    await self._fire_action(event, rule, action)
                except Exception as e:
                    logger.error("automation_action_error", rule=rule.name, error=str(e))

            rule.mark_fired()

            # Record in history
            self._history.insert(
                0,
                {
                    "rule": rule.name,
                    "trigger_event": event.event_type,
                    "trigger_entity": f"{event.entity_type}:{event.entity_id}",
                    "ts": time.time(),
                    "fire_count": rule.fire_count,
                },
            )
            if len(self._history) > self._max_history:
                self._history = self._history[: self._max_history]

    async def _fire_action(
        self, trigger_event: DomainEvent, rule: AutomationRule, action: dict
    ) -> None:
        """Fire a single automation action — create and publish a new event."""
        from bot.core.event_bus import DomainEvent as DE

        entity_type = action.get("entity_type", "automation")

        # entity_id can be static or dynamic (function of trigger event)
        entity_id_fn = action.get("entity_id_fn")
        if callable(entity_id_fn):
            entity_id = entity_id_fn(trigger_event)
        else:
            entity_id = action.get("entity_id", trigger_event.entity_id)

        event_type = action["event_type"]

        # data can be static or dynamic
        data_fn = action.get("data_fn")
        if callable(data_fn):
            data = data_fn(trigger_event)
        else:
            data = action.get("data", {})

        new_event = DE(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            data={
                **data,
                "_automation_rule": rule.name,
                "_trigger_event_id": trigger_event.event_id,
            },
            bot_name=trigger_event.bot_name,
            causes=[trigger_event.event_id],
        )

        await self._bus.publish(new_event)

        logger.info(
            "automation_action_fired",
            rule=rule.name,
            action_event=event_type,
            trigger=trigger_event.event_type,
            entity=f"{entity_type}:{entity_id}",
        )

    def register_defaults(self) -> None:
        """Register built-in trading automation rules."""

        # Rule 1: Portfolio stop-loss → deactivate all strategies
        self.add_rule(
            AutomationRule(
                name="portfolio_stop_loss_cascade",
                description="При стоп-лоссе портфеля деактивировать все стратегии",
                trigger_event="PORTFOLIO_STOP_LOSS",
                condition=lambda e: True,
                actions=[
                    {
                        "entity_type": "strategy",
                        "entity_id": "all",
                        "event_type": "STRATEGY_DEACTIVATED",
                        "data_fn": lambda e: {
                            "reason": "Portfolio stop-loss triggered",
                            "source": "automation",
                        },
                    },
                    {
                        "entity_type": "bot",
                        "entity_id_fn": lambda e: e.bot_name,
                        "event_type": "BOT_EMERGENCY_STOP",
                        "data_fn": lambda e: {
                            "reason": "Portfolio stop-loss",
                            "source": "automation",
                        },
                    },
                ],
                cooldown_seconds=60,
            )
        )

        # Rule 2: Daily loss limit → pause all strategies
        self.add_rule(
            AutomationRule(
                name="daily_loss_limit_cascade",
                description="При достижении дневного лимита убытков — пауза",
                trigger_event="DAILY_LOSS_LIMIT_HIT",
                condition=lambda e: True,
                actions=[
                    {
                        "entity_type": "risk",
                        "entity_id": "daily",
                        "event_type": "RISK_HALTED",
                        "data_fn": lambda e: {
                            "reason": "Daily loss limit",
                            "daily_loss": e.data.get("daily_loss"),
                        },
                    },
                ],
                cooldown_seconds=300,
            )
        )

        # Rule 3: Consecutive losses → escalate risk
        self.add_rule(
            AutomationRule(
                name="loss_streak_escalation",
                description="3 убыточные позиции подряд → остановка торговли",
                trigger_event="POSITION_CLOSED",
                condition=lambda e: self._check_loss_streak(e),
                actions=[
                    {
                        "entity_type": "risk",
                        "entity_id": "streak",
                        "event_type": "RISK_HALTED",
                        "data_fn": lambda e: {
                            "reason": f"Loss streak: {self._consecutive_losses} consecutive losses",
                            "streak": self._consecutive_losses,
                        },
                    },
                ],
                cooldown_seconds=600,
            )
        )

        # Rule 4: Regime change → strategy activation events
        self.add_rule(
            AutomationRule(
                name="regime_strategy_activation",
                description="Смена режима → уведомление стратегий",
                trigger_event="TRANSITION_COMPLETE",
                condition=lambda e: True,
                actions=[
                    {
                        "entity_type": "strategy",
                        "entity_id": "routing",
                        "event_type": "STRATEGY_ACTIVATED",
                        "data_fn": lambda e: {
                            "regime": e.data.get("new_regime"),
                            "source": "automation",
                        },
                    },
                ],
            )
        )

        # Rule 5: Risk resumed → notify strategies
        self.add_rule(
            AutomationRule(
                name="risk_resumed_notification",
                description="Возобновление торговли → уведомление стратегий",
                trigger_event="RISK_RESUMED",
                condition=lambda e: True,
                actions=[
                    {
                        "entity_type": "strategy",
                        "entity_id": "all",
                        "event_type": "STRATEGY_ACTIVATED",
                        "data_fn": lambda e: {"reason": "Risk resumed", "source": "automation"},
                    },
                ],
            )
        )

        # Rule 6: Bot error → risk notification
        self.add_rule(
            AutomationRule(
                name="bot_error_risk_notification",
                description="Ошибка бота → уведомление риск-менеджера",
                trigger_event="BOT_ERROR",
                condition=lambda e: True,
                actions=[
                    {
                        "entity_type": "risk",
                        "entity_id_fn": lambda e: e.bot_name,
                        "event_type": "TRADE_REJECTED",
                        "data_fn": lambda e: {
                            "reason": f"Bot error: {e.data.get('error', 'unknown')}",
                            "source": "automation",
                        },
                    },
                ],
                cooldown_seconds=30,
            )
        )

        # Rule 7: Position opened → update portfolio state
        self.add_rule(
            AutomationRule(
                name="position_portfolio_sync",
                description="Открытие позиции → обновление портфеля",
                trigger_event="POSITION_OPENED",
                condition=lambda e: True,
                actions=[
                    {
                        "entity_type": "portfolio",
                        "entity_id": "main",
                        "event_type": "BALANCE_UPDATED",
                        "data_fn": lambda e: {
                            "source": "position_opened",
                            "position_id": e.entity_id,
                        },
                    },
                ],
            )
        )

        # Rule 8: Committee REJECT → trade rejected event
        self.add_rule(
            AutomationRule(
                name="committee_reject_cascade",
                description="Комитет отклонил → событие отклонения",
                trigger_event="DECISION_MADE",
                condition=lambda e: e.data.get("verdict") == "reject",
                actions=[
                    {
                        "entity_type": "risk",
                        "entity_id_fn": lambda e: e.entity_id,
                        "event_type": "TRADE_REJECTED",
                        "data_fn": lambda e: {
                            "reason": f"Committee rejected: {e.data.get('reasoning', '')}",
                            "score": e.data.get("score"),
                            "source": "committee",
                        },
                    },
                ],
            )
        )

        logger.info("default_automation_rules_registered", count=len(self._rules))

    def _check_loss_streak(self, event: DomainEvent) -> bool:
        """Track consecutive losses and return True when threshold hit."""
        pnl = event.data.get("pnl", 0)
        if isinstance(pnl, str):
            try:
                pnl = float(pnl)
            except ValueError:
                pnl = 0

        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        return self._consecutive_losses >= self._loss_streak_threshold

    def get_status(self) -> dict[str, Any]:
        """Get automation engine status."""
        return {
            "rules": [
                {
                    "name": r.name,
                    "description": r.description,
                    "trigger": r.trigger_event,
                    "enabled": r.enabled,
                    "fire_count": r.fire_count,
                    "cooldown": r.cooldown_seconds,
                }
                for r in self._rules
            ],
            "total_rules": len(self._rules),
            "active_rules": sum(1 for r in self._rules if r.enabled),
            "total_fires": sum(r.fire_count for r in self._rules),
            "consecutive_losses": self._consecutive_losses,
            "recent_history": self._history[:10],
        }
