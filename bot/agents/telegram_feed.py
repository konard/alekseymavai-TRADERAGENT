"""Telegram Committee Feed — publishes committee decisions and alerts to Telegram."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from bot.core.event_bus import DomainEvent, EventBus
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class CommitteeTelegramFeed:
    """Formats and publishes committee decisions, anomaly alerts, and
    weight calibration events to Telegram.

    Subscribes to EventBus events:
    - DECISION_MADE: format committee decision with expert votes
    - ANOMALY_DETECTED: format anomaly alert
    - RISK_HALTED: format halt notification
    - RISK_RESUMED: format resume notification

    Does NOT send messages directly — uses a callback function.
    This keeps the class testable without a real Telegram bot.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        send_callback: Callable[[str], Awaitable[None]] | None = None,
        min_score_to_report: float = 0.0,
        report_rejects: bool = True,
        report_approvals: bool = True,
        report_anomalies: bool = True,
        report_calibrations: bool = False,
    ) -> None:
        self._bus = event_bus
        self._send = send_callback
        self._min_score_to_report = min_score_to_report
        self._report_rejects = report_rejects
        self._report_approvals = report_approvals
        self._report_anomalies = report_anomalies
        self._report_calibrations = report_calibrations
        self._unsubscribers: list[Callable] = []
        self._message_count: int = 0

    # ------------------------------------------------------------------
    # Formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def format_decision(event_data: dict[str, Any]) -> str:
        """Format a committee decision for Telegram.

        Output format::

            🏛 Committee Decision: APPROVE ✅
            Score: 0.742 | Symbol: ETH/USDT

            Expert Votes:
            ├ SMC: APPROVE (0.82) — bullish OB
            ├ Trend: APPROVE (0.71) — aligned with regime
            ├ Risk: DEFER (0.50) — neutral
            └ Contrarian: REJECT (0.65) — volume divergence

            Contradictions: 1
        """
        verdict_raw: str = event_data.get("verdict", "defer")
        verdict = verdict_raw.upper()

        emoji_map = {"APPROVE": "✅", "REJECT": "❌", "DEFER": "⏸"}
        emoji = emoji_map.get(verdict, "")

        score = event_data.get("score", 0.0)
        symbol = event_data.get("symbol", "")

        lines: list[str] = []
        lines.append(f"🏛 Committee Decision: {verdict} {emoji}")

        score_line = f"Score: {score:.3f}"
        if symbol:
            score_line += f" | Symbol: {symbol}"
        lines.append(score_line)
        lines.append("")

        votes: list[dict[str, Any]] = event_data.get("votes", [])
        if votes:
            lines.append("Expert Votes:")
            for i, vote in enumerate(votes):
                expert = vote.get("expert", "unknown")
                v = vote.get("verdict", "defer").upper()
                conf = vote.get("confidence", 0.0)
                reasoning = vote.get("reasoning", "")
                prefix = "└" if i == len(votes) - 1 else "├"
                line = f"{prefix} {expert}: {v} ({conf:.2f})"
                if reasoning:
                    line += f" — {reasoning}"
                lines.append(line)
            lines.append("")

        contradictions = event_data.get("contradictions_count", 0)
        lines.append(f"Contradictions: {contradictions}")

        return "\n".join(lines)

    @staticmethod
    def format_anomaly(event_data: dict[str, Any]) -> str:
        """Format anomaly alert.

        Output::

            ⚠️ Anomaly Detected: VOLUME_SPIKE
            Severity: 0.85 | Z-score: 4.2
            Volume: 5000 (threshold: 3.0σ)
        """
        anomaly_type = event_data.get("type", "UNKNOWN")
        severity = event_data.get("severity", 0.0)
        z_score = event_data.get("z_score", 0.0)
        value = event_data.get("value", "")
        threshold = event_data.get("threshold", "")

        lines: list[str] = []
        lines.append(f"⚠️ Anomaly Detected: {anomaly_type}")
        lines.append(f"Severity: {severity:.2f} | Z-score: {z_score:.1f}")
        if value != "" or threshold != "":
            lines.append(f"Volume: {value} (threshold: {threshold}σ)")
        return "\n".join(lines)

    @staticmethod
    def format_halt(event_data: dict[str, Any]) -> str:
        """Format trading halt notification.

        Output::

            🛑 Trading HALTED
            Reason: Loss streak: 3 consecutive losses
        """
        reason = event_data.get("reason", "Unknown")
        lines = [
            "🛑 Trading HALTED",
            f"Reason: {reason}",
        ]
        return "\n".join(lines)

    @staticmethod
    def format_resume(event_data: dict[str, Any]) -> str:
        """Format trading resume notification."""
        reason = event_data.get("reason", "")
        lines = ["▶️ Trading RESUMED"]
        if reason:
            lines.append(f"Reason: {reason}")
        return "\n".join(lines)

    @staticmethod
    def format_calibration(weights: dict[str, float]) -> str:
        """Format weight recalibration.

        Expects *weights* dict where each key maps to a dict or float.
        If values are dicts with 'old' and 'new' keys, compute the change.
        If values are plain floats, just list them (no delta available).

        Output::

            ⚖️ Weights Recalibrated
            SMC: 1.20 → 1.35 (+12.5%)
            Trend: 1.00 → 0.85 (-15.0%)
            Risk: 1.50 → 1.50 (unchanged)
            Contrarian: 0.80 → 0.72 (-10.0%)
        """
        lines: list[str] = ["⚖️ Weights Recalibrated"]
        for name, info in weights.items():
            if isinstance(info, dict):
                old = info.get("old", 0.0)
                new = info.get("new", 0.0)
                if old and old != 0.0:
                    pct = (new - old) / old * 100
                    if abs(pct) < 0.01:
                        change = "(unchanged)"
                    else:
                        change = f"({pct:+.1f}%)"
                else:
                    change = ""
                lines.append(f"{name}: {old:.2f} → {new:.2f} {change}".rstrip())
            else:
                lines.append(f"{name}: {info:.2f}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Event handlers (wired by start())
    # ------------------------------------------------------------------

    async def _on_decision(self, event: DomainEvent) -> None:
        """Handle DECISION_MADE event."""
        data = event.data
        verdict = data.get("verdict", "defer").lower()
        score = data.get("score", 0.0)

        # Filter by score
        if score < self._min_score_to_report:
            return

        # Filter by verdict type
        if verdict == "reject" and not self._report_rejects:
            return
        if verdict == "approve" and not self._report_approvals:
            return

        msg = self.format_decision(data)
        await self._send_message(msg)

    async def _on_anomaly(self, event: DomainEvent) -> None:
        """Handle ANOMALY_DETECTED event."""
        if not self._report_anomalies:
            return
        msg = self.format_anomaly(event.data)
        await self._send_message(msg)

    async def _on_halt(self, event: DomainEvent) -> None:
        """Handle RISK_HALTED event."""
        msg = self.format_halt(event.data)
        await self._send_message(msg)

    async def _on_resume(self, event: DomainEvent) -> None:
        """Handle RISK_RESUMED event."""
        msg = self.format_resume(event.data)
        await self._send_message(msg)

    async def _send_message(self, msg: str) -> None:
        """Send message via callback."""
        if self._send is not None:
            try:
                await self._send(msg)
                self._message_count += 1
            except Exception:
                logger.exception("telegram_feed_send_error")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to EventBus events."""
        if self._bus is None:
            logger.warning("telegram_feed_start_no_bus")
            return

        self._unsubscribers.append(
            self._bus.subscribe("DECISION_MADE", self._on_decision)
        )
        self._unsubscribers.append(
            self._bus.subscribe("ANOMALY_DETECTED", self._on_anomaly)
        )
        self._unsubscribers.append(
            self._bus.subscribe("RISK_HALTED", self._on_halt)
        )
        self._unsubscribers.append(
            self._bus.subscribe("RISK_RESUMED", self._on_resume)
        )

        logger.info("telegram_feed_started", subscriptions=4)

    async def stop(self) -> None:
        """Unsubscribe from all events."""
        for unsub in self._unsubscribers:
            unsub()
        self._unsubscribers.clear()
        logger.info("telegram_feed_stopped")

    def get_stats(self) -> dict[str, Any]:
        """Return feed statistics."""
        return {
            "message_count": self._message_count,
            "subscriptions": len(self._unsubscribers),
            "report_approvals": self._report_approvals,
            "report_rejects": self._report_rejects,
            "report_anomalies": self._report_anomalies,
            "report_calibrations": self._report_calibrations,
            "min_score_to_report": self._min_score_to_report,
        }
