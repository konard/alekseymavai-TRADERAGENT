from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum


@dataclass
class TelegramCommand:
    command: str
    description: str
    handler: Callable[[list[str]], Awaitable[str]]


class TelegramDashboard:
    def __init__(self) -> None:
        self._commands: dict[str, TelegramCommand] = {}
        self._data_providers: dict[str, Callable[[], Awaitable[dict]]] = {}
        self._stats_commands_processed: int = 0
        self._stats_command_usage: dict[str, int] = defaultdict(int)
        self._stats_errors: int = 0

        self._register_builtins()

    def register_command(self, cmd: TelegramCommand) -> None:
        self._commands[cmd.command] = cmd

    def set_data_provider(self, name: str, provider: Callable[[], Awaitable[dict]]) -> None:
        self._data_providers[name] = provider

    async def handle_message(self, text: str) -> str:
        parts = text.strip().split()
        if not parts:
            return "Empty command"
        command = parts[0].lower()
        args = parts[1:]

        cmd = self._commands.get(command)
        if cmd is None:
            self._stats_errors += 1
            return f"Unknown command: {command}. Use /help for available commands."

        self._stats_commands_processed += 1
        self._stats_command_usage[command] += 1

        try:
            return await cmd.handler(args)
        except Exception as exc:
            self._stats_errors += 1
            return f"Error executing {command}: {exc}"

    def get_stats(self) -> dict:
        most_used = None
        if self._stats_command_usage:
            most_used = max(self._stats_command_usage, key=lambda k: self._stats_command_usage[k])
        return {
            "commands_processed": self._stats_commands_processed,
            "most_used_command": most_used,
            "errors": self._stats_errors,
        }

    @staticmethod
    def format_ascii_heatmap(data: dict) -> str:
        blocks = " ", "\u2591", "\u2592", "\u2593", "\u2588"

        if not data:
            return "No data"

        strategies: set[str] = set()
        for strats in data.values():
            strategies.update(strats.keys())
        strategies_sorted = sorted(strategies)

        if not strategies_sorted:
            return "No data"

        all_values: list[float] = []
        for strats in data.values():
            all_values.extend(strats.values())

        max_val = max(all_values) if all_values else 1
        if max_val == 0:
            max_val = 1

        sym_width = max(len(s) for s in data) if data else 6
        col_width = max(max((len(s) for s in strategies_sorted), default=4), 4)

        header = " " * (sym_width + 1) + "  ".join(s.center(col_width) for s in strategies_sorted)
        lines = [header]

        for symbol in sorted(data):
            row_parts = [symbol.ljust(sym_width)]
            for strat in strategies_sorted:
                val = data[symbol].get(strat, 0)
                ratio = val / max_val
                idx = min(int(ratio * (len(blocks) - 1)), len(blocks) - 1)
                cell = blocks[idx].center(col_width)
                row_parts.append(cell)
            lines.append(" ".join(row_parts))

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Built-in commands
    # ------------------------------------------------------------------

    def _register_builtins(self) -> None:
        builtins = [
            TelegramCommand("/status", "Bot mode, uptime, positions, daily PnL", self._cmd_status),
            TelegramCommand(
                "/portfolio", "Portfolio value, positions, exposure", self._cmd_portfolio
            ),
            TelegramCommand("/committee", "Last committee decision summary", self._cmd_committee),
            TelegramCommand("/heatmap", "ASCII heatmap of portfolio exposure", self._cmd_heatmap),
            TelegramCommand("/scenarios", "Active scenarios list", self._cmd_scenarios),
            TelegramCommand("/override", "Manual override", self._cmd_override),
            TelegramCommand("/help", "List all commands", self._cmd_help),
        ]
        for cmd in builtins:
            self.register_command(cmd)

    async def _get_provider_data(self, name: str) -> dict | None:
        provider = self._data_providers.get(name)
        if provider is None:
            return None
        return await provider()

    async def _cmd_status(self, args: list[str]) -> str:
        data = await self._get_provider_data("status")
        if data is None:
            return "Data not available"
        mode = data.get("mode", "unknown")
        uptime = data.get("uptime", "unknown")
        positions = data.get("active_positions", 0)
        daily_pnl = data.get("daily_pnl", 0.0)
        return (
            f"Mode: {mode}\n"
            f"Uptime: {uptime}\n"
            f"Active positions: {positions}\n"
            f"Daily PnL: ${daily_pnl:+.2f}"
        )

    async def _cmd_portfolio(self, args: list[str]) -> str:
        data = await self._get_provider_data("portfolio")
        if data is None:
            return "Data not available"
        value = data.get("value", 0.0)
        positions = data.get("positions", [])
        exposure = data.get("total_exposure", 0.0)
        pos_lines = "\n".join(f"  - {p}" for p in positions) if positions else "  None"
        return (
            f"Portfolio value: ${value:,.2f}\n"
            f"Positions:\n{pos_lines}\n"
            f"Total exposure: ${exposure:,.2f}"
        )

    async def _cmd_committee(self, args: list[str]) -> str:
        data = await self._get_provider_data("committee")
        if data is None:
            return "Data not available"
        return str(data.get("summary", "No committee decision available"))

    async def _cmd_heatmap(self, args: list[str]) -> str:
        data = await self._get_provider_data("heatmap")
        if data is None:
            return "Data not available"
        return self.format_ascii_heatmap(data)

    async def _cmd_scenarios(self, args: list[str]) -> str:
        data = await self._get_provider_data("scenarios")
        if data is None:
            return "Data not available"
        scenarios = data.get("active", [])
        if not scenarios:
            return "No active scenarios"
        lines = [f"  - {s}" for s in scenarios]
        return "Active scenarios:\n" + "\n".join(lines)

    async def _cmd_override(self, args: list[str]) -> str:
        if not args:
            return "Usage: /override <action>"
        action = " ".join(args)
        return f"Override set: {action}"

    async def _cmd_help(self, args: list[str]) -> str:
        lines = []
        for cmd in sorted(self._commands.values(), key=lambda c: c.command):
            lines.append(f"  {cmd.command} - {cmd.description}")
        return "Available commands:\n" + "\n".join(lines)


# ======================================================================
# Alert system
# ======================================================================


class AlertPriority(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class Alert:
    alert_id: str
    priority: AlertPriority
    category: str
    title: str
    message: str
    ts: float
    acknowledged: bool = False

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "priority": self.priority.name,
            "category": self.category,
            "title": self.title,
            "message": self.message,
            "ts": self.ts,
            "acknowledged": self.acknowledged,
        }


class AlertManager:
    def __init__(
        self,
        digest_interval_seconds: float = 300,
        max_alerts_per_digest: int = 20,
        dedup_window_seconds: float = 60,
    ) -> None:
        self._digest_interval = digest_interval_seconds
        self._max_per_digest = max_alerts_per_digest
        self._dedup_window = dedup_window_seconds

        self._alerts: list[Alert] = []
        self._last_digest_ts: float = 0.0
        self._notification_callback: Callable[[Alert], Awaitable[None]] | None = None

        self._stats_total: int = 0
        self._stats_digests_sent: int = 0
        self._stats_dedup_count: int = 0

    def add_alert(
        self,
        priority: AlertPriority,
        category: str,
        title: str,
        message: str,
    ) -> Alert:
        now = time.time()

        if priority != AlertPriority.CRITICAL:
            for existing in reversed(self._alerts):
                if now - existing.ts > self._dedup_window:
                    break
                if existing.title == title and existing.category == category:
                    self._stats_dedup_count += 1
                    return existing

        alert_id = uuid.uuid4().hex[:12]
        alert = Alert(
            alert_id=alert_id,
            priority=priority,
            category=category,
            title=title,
            message=message,
            ts=now,
        )
        self._alerts.append(alert)
        self._stats_total += 1

        if priority == AlertPriority.CRITICAL and self._notification_callback is not None:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                asyncio.ensure_future(self._notification_callback(alert), loop=loop)
            except RuntimeError:
                pass

        return alert

    def get_pending(self) -> list[Alert]:
        pending = [a for a in self._alerts if not a.acknowledged]
        pending.sort(key=lambda a: a.priority.value, reverse=True)
        return pending

    def acknowledge(self, alert_id: str) -> None:
        for alert in self._alerts:
            if alert.alert_id == alert_id:
                alert.acknowledged = True
                return

    def acknowledge_all(self) -> None:
        for alert in self._alerts:
            alert.acknowledged = True

    def generate_digest(self, current_ts: float | None = None) -> str | None:
        now = current_ts if current_ts is not None else time.time()

        pending = self.get_pending()
        if not pending:
            return None

        if self._last_digest_ts > 0 and (now - self._last_digest_ts) < self._digest_interval:
            return None

        self._last_digest_ts = now

        by_priority: dict[AlertPriority, list[Alert]] = defaultdict(list)
        for alert in pending[: self._max_per_digest]:
            by_priority[alert.priority].append(alert)

        priority_labels = {
            AlertPriority.CRITICAL: "\U0001f534 CRITICAL",
            AlertPriority.HIGH: "\U0001f7e1 HIGH",
            AlertPriority.MEDIUM: "\U0001f7e0 MEDIUM",
            AlertPriority.LOW: "\u2139\ufe0f LOW",
        }

        sections: list[str] = []
        for prio in (
            AlertPriority.CRITICAL,
            AlertPriority.HIGH,
            AlertPriority.MEDIUM,
            AlertPriority.LOW,
        ):
            alerts_in_prio = by_priority.get(prio)
            if not alerts_in_prio:
                continue
            label = priority_labels[prio]
            header = f"{label} ({len(alerts_in_prio)}):"
            items = [f"- {a.category.capitalize()}: {a.title}" for a in alerts_in_prio]
            sections.append(header + "\n" + "\n".join(items))

        count_by = {p.name.lower(): len(lst) for p, lst in by_priority.items()}
        total = sum(count_by.values())
        summary_parts = [f"{total} alerts"]
        for prio_name in ("critical", "high", "medium", "low"):
            cnt = count_by.get(prio_name, 0)
            if cnt:
                summary_parts.append(f"{cnt} {prio_name}")

        sections.append("\U0001f4ca Summary: " + ", ".join(summary_parts))

        self._stats_digests_sent += 1
        return "\n\n".join(sections)

    def get_alert_history(self, limit: int = 50, category: str | None = None) -> list[Alert]:
        alerts = self._alerts
        if category is not None:
            alerts = [a for a in alerts if a.category == category]
        return list(reversed(alerts[-limit:]))

    def set_notification_callback(self, callback: Callable[[Alert], Awaitable[None]]) -> None:
        self._notification_callback = callback

    def get_stats(self) -> dict:
        by_priority: dict[str, int] = defaultdict(int)
        by_category: dict[str, int] = defaultdict(int)
        pending = 0
        for a in self._alerts:
            by_priority[a.priority.name] += 1
            by_category[a.category] += 1
            if not a.acknowledged:
                pending += 1
        return {
            "total_alerts": self._stats_total,
            "pending_count": pending,
            "by_priority": dict(by_priority),
            "by_category": dict(by_category),
            "digests_sent": self._stats_digests_sent,
            "dedup_count": self._stats_dedup_count,
        }
