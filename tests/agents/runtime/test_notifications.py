from __future__ import annotations

import asyncio
import time

import pytest

from bot.agents.runtime.notifications import (
    Alert,
    AlertManager,
    AlertPriority,
    TelegramCommand,
    TelegramDashboard,
)


# ======================================================================
# TelegramDashboard tests
# ======================================================================


@pytest.fixture
def dashboard() -> TelegramDashboard:
    return TelegramDashboard()


@pytest.mark.asyncio
async def test_help_lists_all_commands(dashboard: TelegramDashboard) -> None:
    result = await dashboard.handle_message("/help")
    assert "/status" in result
    assert "/portfolio" in result
    assert "/committee" in result
    assert "/heatmap" in result
    assert "/scenarios" in result
    assert "/override" in result
    assert "/help" in result


@pytest.mark.asyncio
async def test_status_with_provider(dashboard: TelegramDashboard) -> None:
    async def status_provider() -> dict:
        return {
            "mode": "live",
            "uptime": "2h 15m",
            "active_positions": 3,
            "daily_pnl": 125.50,
        }

    dashboard.set_data_provider("status", status_provider)
    result = await dashboard.handle_message("/status")
    assert "live" in result
    assert "2h 15m" in result
    assert "3" in result
    assert "$+125.50" in result


@pytest.mark.asyncio
async def test_unknown_command(dashboard: TelegramDashboard) -> None:
    result = await dashboard.handle_message("/foobar")
    assert "Unknown command" in result
    assert "/foobar" in result


@pytest.mark.asyncio
async def test_no_provider_returns_not_available(dashboard: TelegramDashboard) -> None:
    result = await dashboard.handle_message("/status")
    assert "Data not available" in result


@pytest.mark.asyncio
async def test_override_command(dashboard: TelegramDashboard) -> None:
    result = await dashboard.handle_message("/override pause_trading")
    assert "Override set" in result
    assert "pause_trading" in result


@pytest.mark.asyncio
async def test_override_no_args(dashboard: TelegramDashboard) -> None:
    result = await dashboard.handle_message("/override")
    assert "Usage" in result


@pytest.mark.asyncio
async def test_register_custom_command(dashboard: TelegramDashboard) -> None:
    async def custom_handler(args: list[str]) -> str:
        return f"Custom: {','.join(args)}"

    cmd = TelegramCommand("/custom", "A custom command", custom_handler)
    dashboard.register_command(cmd)
    result = await dashboard.handle_message("/custom arg1 arg2")
    assert result == "Custom: arg1,arg2"

    help_result = await dashboard.handle_message("/help")
    assert "/custom" in help_result


def test_ascii_heatmap_format() -> None:
    data = {
        "BTCUSDT": {"Grid": 0.8, "DCA": 0.2},
        "ETHUSDT": {"Grid": 0.4, "DCA": 1.0},
    }
    result = TelegramDashboard.format_ascii_heatmap(data)
    assert "BTCUSDT" in result
    assert "ETHUSDT" in result
    assert "Grid" in result
    assert "DCA" in result


def test_ascii_heatmap_empty() -> None:
    assert TelegramDashboard.format_ascii_heatmap({}) == "No data"


@pytest.mark.asyncio
async def test_dashboard_stats(dashboard: TelegramDashboard) -> None:
    await dashboard.handle_message("/help")
    await dashboard.handle_message("/help")
    await dashboard.handle_message("/unknown_cmd")

    stats = dashboard.get_stats()
    assert stats["commands_processed"] == 2
    assert stats["most_used_command"] == "/help"
    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_portfolio_with_provider(dashboard: TelegramDashboard) -> None:
    async def portfolio_provider() -> dict:
        return {
            "value": 50000.0,
            "positions": ["BTCUSDT LONG 0.5", "ETHUSDT SHORT 2.0"],
            "total_exposure": 35000.0,
        }

    dashboard.set_data_provider("portfolio", portfolio_provider)
    result = await dashboard.handle_message("/portfolio")
    assert "$50,000.00" in result
    assert "BTCUSDT LONG 0.5" in result
    assert "$35,000.00" in result


@pytest.mark.asyncio
async def test_scenarios_with_provider(dashboard: TelegramDashboard) -> None:
    async def scenarios_provider() -> dict:
        return {"active": ["Bull breakout", "Range bound"]}

    dashboard.set_data_provider("scenarios", scenarios_provider)
    result = await dashboard.handle_message("/scenarios")
    assert "Bull breakout" in result
    assert "Range bound" in result


# ======================================================================
# AlertManager tests
# ======================================================================


@pytest.fixture
def alert_mgr() -> AlertManager:
    return AlertManager(digest_interval_seconds=0, dedup_window_seconds=60)


def test_add_alert(alert_mgr: AlertManager) -> None:
    alert = alert_mgr.add_alert(
        AlertPriority.HIGH, "trade", "Stop loss hit", "ETHUSDT stop loss triggered"
    )
    assert isinstance(alert, Alert)
    assert alert.priority == AlertPriority.HIGH
    assert alert.category == "trade"
    assert alert.title == "Stop loss hit"
    assert len(alert.alert_id) == 12
    assert alert.acknowledged is False


def test_alert_to_dict(alert_mgr: AlertManager) -> None:
    alert = alert_mgr.add_alert(AlertPriority.LOW, "system", "Heartbeat", "OK")
    d = alert.to_dict()
    assert d["priority"] == "LOW"
    assert d["category"] == "system"
    assert d["acknowledged"] is False


def test_deduplication_same_title_within_window(alert_mgr: AlertManager) -> None:
    a1 = alert_mgr.add_alert(AlertPriority.HIGH, "trade", "Stop loss hit", "msg1")
    a2 = alert_mgr.add_alert(AlertPriority.HIGH, "trade", "Stop loss hit", "msg2")
    assert a1.alert_id == a2.alert_id
    stats = alert_mgr.get_stats()
    assert stats["dedup_count"] == 1
    assert stats["total_alerts"] == 1


def test_no_dedup_for_critical(alert_mgr: AlertManager) -> None:
    a1 = alert_mgr.add_alert(AlertPriority.CRITICAL, "risk", "Margin call", "msg1")
    a2 = alert_mgr.add_alert(AlertPriority.CRITICAL, "risk", "Margin call", "msg2")
    assert a1.alert_id != a2.alert_id
    assert alert_mgr.get_stats()["total_alerts"] == 2


def test_pending_sorted_by_priority(alert_mgr: AlertManager) -> None:
    alert_mgr.add_alert(AlertPriority.LOW, "system", "Info", "low")
    alert_mgr.add_alert(AlertPriority.CRITICAL, "risk", "Critical", "crit")
    alert_mgr.add_alert(AlertPriority.MEDIUM, "trade", "Medium", "med")

    pending = alert_mgr.get_pending()
    assert pending[0].priority == AlertPriority.CRITICAL
    assert pending[1].priority == AlertPriority.MEDIUM
    assert pending[2].priority == AlertPriority.LOW


def test_acknowledge(alert_mgr: AlertManager) -> None:
    alert = alert_mgr.add_alert(AlertPriority.HIGH, "trade", "Alert1", "msg")
    assert len(alert_mgr.get_pending()) == 1
    alert_mgr.acknowledge(alert.alert_id)
    assert len(alert_mgr.get_pending()) == 0


def test_acknowledge_all(alert_mgr: AlertManager) -> None:
    alert_mgr.add_alert(AlertPriority.HIGH, "trade", "A", "m")
    alert_mgr.add_alert(AlertPriority.LOW, "system", "B", "m")
    assert len(alert_mgr.get_pending()) == 2
    alert_mgr.acknowledge_all()
    assert len(alert_mgr.get_pending()) == 0


def test_generate_digest_format(alert_mgr: AlertManager) -> None:
    alert_mgr.add_alert(AlertPriority.CRITICAL, "trade", "Stop loss hit on ETHUSDT", "details")
    alert_mgr.add_alert(AlertPriority.HIGH, "committee", "Split vote on BTCUSDT", "details")
    alert_mgr.add_alert(AlertPriority.LOW, "system", "Heartbeat OK", "details")

    digest = alert_mgr.generate_digest(current_ts=time.time() + 1)
    assert digest is not None
    assert "CRITICAL" in digest
    assert "HIGH" in digest
    assert "LOW" in digest
    assert "Summary" in digest
    assert "3 alerts" in digest
    assert "Trade: Stop loss hit on ETHUSDT" in digest


def test_digest_interval_throttle() -> None:
    mgr = AlertManager(digest_interval_seconds=300, dedup_window_seconds=60)
    mgr.add_alert(AlertPriority.HIGH, "trade", "Alert1", "msg")

    now = time.time()
    d1 = mgr.generate_digest(current_ts=now)
    assert d1 is not None

    d2 = mgr.generate_digest(current_ts=now + 100)
    assert d2 is None

    d3 = mgr.generate_digest(current_ts=now + 301)
    assert d3 is not None


def test_digest_none_when_no_pending(alert_mgr: AlertManager) -> None:
    result = alert_mgr.generate_digest()
    assert result is None


def test_notification_callback_for_critical() -> None:
    mgr = AlertManager(digest_interval_seconds=0, dedup_window_seconds=60)
    received: list[Alert] = []

    async def callback(alert: Alert) -> None:
        received.append(alert)

    mgr.set_notification_callback(callback)

    loop = asyncio.new_event_loop()
    try:
        async def run() -> None:
            mgr.add_alert(AlertPriority.CRITICAL, "risk", "Drawdown exceeded", "10% drawdown")
            await asyncio.sleep(0.01)

        loop.run_until_complete(run())
    finally:
        loop.close()

    assert len(received) == 1
    assert received[0].title == "Drawdown exceeded"


def test_no_callback_for_non_critical() -> None:
    mgr = AlertManager(digest_interval_seconds=0, dedup_window_seconds=60)
    received: list[Alert] = []

    async def callback(alert: Alert) -> None:
        received.append(alert)

    mgr.set_notification_callback(callback)

    loop = asyncio.new_event_loop()
    try:
        async def run() -> None:
            mgr.add_alert(AlertPriority.HIGH, "trade", "Some alert", "msg")
            await asyncio.sleep(0.01)

        loop.run_until_complete(run())
    finally:
        loop.close()

    assert len(received) == 0


def test_alert_history(alert_mgr: AlertManager) -> None:
    for i in range(5):
        alert_mgr.add_alert(AlertPriority.LOW, "system", f"Alert {i}", f"msg {i}")

    history = alert_mgr.get_alert_history(limit=3)
    assert len(history) == 3
    assert history[0].title == "Alert 4"
    assert history[2].title == "Alert 2"


def test_alert_history_category_filter(alert_mgr: AlertManager) -> None:
    alert_mgr.add_alert(AlertPriority.LOW, "system", "Sys1", "m")
    alert_mgr.add_alert(AlertPriority.HIGH, "trade", "Trade1", "m")
    alert_mgr.add_alert(AlertPriority.LOW, "system", "Sys2", "m")

    history = alert_mgr.get_alert_history(category="trade")
    assert len(history) == 1
    assert history[0].title == "Trade1"


def test_alert_manager_stats(alert_mgr: AlertManager) -> None:
    alert_mgr.add_alert(AlertPriority.HIGH, "trade", "A", "m")
    alert_mgr.add_alert(AlertPriority.LOW, "system", "B", "m")
    alert_mgr.add_alert(AlertPriority.HIGH, "trade", "A", "m")  # dedup

    alert_mgr.generate_digest(current_ts=time.time() + 1)

    stats = alert_mgr.get_stats()
    assert stats["total_alerts"] == 2
    assert stats["pending_count"] == 2
    assert stats["by_priority"]["HIGH"] == 1
    assert stats["by_priority"]["LOW"] == 1
    assert stats["by_category"]["trade"] == 1
    assert stats["by_category"]["system"] == 1
    assert stats["digests_sent"] == 1
    assert stats["dedup_count"] == 1
