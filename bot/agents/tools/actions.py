from __future__ import annotations

import time
import uuid
from typing import Any

from bot.agents.tools.market_data import BaseTool, ToolResult


class AlertSkill(BaseTool):
    """Manages price alerts that agents can set."""

    name = "set_alert"
    description = "Set a price alert for a symbol"

    def __init__(self) -> None:
        self._alerts: list[dict[str, Any]] = []

    def execute(self, params: dict[str, Any]) -> ToolResult:
        symbol = params.get("symbol")
        target_price = params.get("target_price")
        direction = params.get("direction")
        message = params.get("message", "")
        expert_name = params.get("expert_name", "")

        if not symbol or target_price is None or direction not in ("above", "below"):
            return ToolResult(
                success=False,
                error="Required: symbol, target_price, direction (above|below)",
            )

        alert_id = uuid.uuid4().hex[:12]
        alert = {
            "alert_id": alert_id,
            "symbol": symbol,
            "target_price": float(target_price),
            "direction": direction,
            "message": message,
            "expert_name": expert_name,
            "created_at": time.time(),
            "triggered": False,
        }
        self._alerts.append(alert)
        return ToolResult(success=True, data={"alert_id": alert_id})

    def check_alerts(self, current_prices: dict[str, float]) -> list[dict]:
        """Return list of alerts that have been triggered by current prices."""
        triggered: list[dict] = []
        for alert in self._alerts:
            if alert["triggered"]:
                continue
            price = current_prices.get(alert["symbol"])
            if price is None:
                continue
            if alert["direction"] == "above" and price >= alert["target_price"]:
                alert["triggered"] = True
                triggered.append(dict(alert))
            elif alert["direction"] == "below" and price <= alert["target_price"]:
                alert["triggered"] = True
                triggered.append(dict(alert))
        return triggered

    def get_active_alerts(self) -> list[dict]:
        """Return all non-triggered alerts."""
        return [dict(a) for a in self._alerts if not a["triggered"]]

    def cancel_alert(self, alert_id: str) -> bool:
        """Cancel an alert by ID. Returns True if found and removed."""
        for i, alert in enumerate(self._alerts):
            if alert["alert_id"] == alert_id:
                self._alerts.pop(i)
                return True
        return False


class VetoSkill(BaseTool):
    """Allows Risk expert to hard-veto a trade with optional hedge action."""

    name = "veto"
    description = "Veto trading activity"

    def __init__(self, ttl_seconds: float = 3600) -> None:
        self._vetoes: list[dict[str, Any]] = []
        self._ttl_seconds = ttl_seconds

    def execute(self, params: dict[str, Any]) -> ToolResult:
        reason = params.get("reason")
        severity = params.get("severity")
        hedge_action = params.get("hedge_action")

        if not reason or severity not in ("hard", "soft"):
            return ToolResult(
                success=False,
                error="Required: reason, severity (hard|soft)",
            )

        veto_id = uuid.uuid4().hex[:12]
        veto = {
            "veto_id": veto_id,
            "reason": reason,
            "severity": severity,
            "hedge_action": hedge_action,
            "created_at": time.time(),
            "expires_at": time.time() + self._ttl_seconds,
        }
        self._vetoes.append(veto)
        return ToolResult(success=True, data={"veto_id": veto_id})

    def _active_vetoes(self) -> list[dict[str, Any]]:
        """Return vetoes that have not expired."""
        now = time.time()
        return [v for v in self._vetoes if v["expires_at"] > now]

    def is_vetoed(self) -> bool:
        """Return True if any active hard veto exists."""
        return any(v["severity"] == "hard" for v in self._active_vetoes())

    def get_active_vetoes(self) -> list[dict]:
        """Return all active (non-expired) vetoes."""
        return [dict(v) for v in self._active_vetoes()]

    def clear_veto(self, veto_id: str) -> bool:
        """Remove a veto by ID. Returns True if found and removed."""
        for i, veto in enumerate(self._vetoes):
            if veto["veto_id"] == veto_id:
                self._vetoes.pop(i)
                return True
        return False


class ScaleSkill(BaseTool):
    """Requests position scaling (DCA add or partial take-profit)."""

    name = "scale_position"
    description = "Request position scaling"

    def __init__(self) -> None:
        self._requests: list[dict[str, Any]] = []

    def execute(self, params: dict[str, Any]) -> ToolResult:
        position_id = params.get("position_id")
        action = params.get("action")
        amount_pct = params.get("amount_pct")
        reason = params.get("reason", "")
        expert_name = params.get("expert_name", "")

        if not position_id or action not in ("add", "reduce"):
            return ToolResult(
                success=False,
                error="Required: position_id, action (add|reduce)",
            )

        if amount_pct is None:
            return ToolResult(success=False, error="Required: amount_pct")

        amount_pct = float(amount_pct)
        if not (0.01 <= amount_pct <= 1.0):
            return ToolResult(
                success=False,
                error="amount_pct must be between 0.01 and 1.0",
            )

        request_id = uuid.uuid4().hex[:12]
        request = {
            "request_id": request_id,
            "position_id": position_id,
            "action": action,
            "amount_pct": amount_pct,
            "reason": reason,
            "expert_name": expert_name,
            "created_at": time.time(),
            "acknowledged": False,
        }
        self._requests.append(request)
        return ToolResult(success=True, data={"request_id": request_id})

    def get_pending_requests(self) -> list[dict]:
        """Return all unacknowledged scaling requests."""
        return [dict(r) for r in self._requests if not r["acknowledged"]]

    def acknowledge_request(self, request_id: str) -> bool:
        """Mark a scaling request as acknowledged. Returns True if found."""
        for req in self._requests:
            if req["request_id"] == request_id:
                req["acknowledged"] = True
                return True
        return False


class HedgeSkill(BaseTool):
    """Requests opening a hedge position on a correlated instrument."""

    name = "hedge"
    description = "Request a hedge position"

    def __init__(self) -> None:
        self._hedges: list[dict[str, Any]] = []

    def execute(self, params: dict[str, Any]) -> ToolResult:
        original_symbol = params.get("original_symbol")
        hedge_symbol = params.get("hedge_symbol")
        correlation = params.get("correlation")
        hedge_ratio = params.get("hedge_ratio")
        direction = params.get("direction")
        reason = params.get("reason", "")

        if not original_symbol or not hedge_symbol or not direction:
            return ToolResult(
                success=False,
                error="Required: original_symbol, hedge_symbol, direction",
            )

        if correlation is None or hedge_ratio is None:
            return ToolResult(
                success=False,
                error="Required: correlation, hedge_ratio",
            )

        correlation = float(correlation)
        hedge_ratio = float(hedge_ratio)

        if correlation <= 0.3:
            return ToolResult(
                success=False,
                error="correlation must be > 0.3",
            )

        if not (0.1 <= hedge_ratio <= 2.0):
            return ToolResult(
                success=False,
                error="hedge_ratio must be between 0.1 and 2.0",
            )

        request_id = uuid.uuid4().hex[:12]
        hedge = {
            "request_id": request_id,
            "original_symbol": original_symbol,
            "hedge_symbol": hedge_symbol,
            "correlation": correlation,
            "hedge_ratio": hedge_ratio,
            "direction": direction,
            "reason": reason,
            "created_at": time.time(),
            "acknowledged": False,
        }
        self._hedges.append(hedge)
        return ToolResult(success=True, data={"request_id": request_id})

    def get_pending_hedges(self) -> list[dict]:
        """Return all unacknowledged hedge requests."""
        return [dict(h) for h in self._hedges if not h["acknowledged"]]

    def acknowledge_hedge(self, request_id: str) -> bool:
        """Mark a hedge request as acknowledged. Returns True if found."""
        for hedge in self._hedges:
            if hedge["request_id"] == request_id:
                hedge["acknowledged"] = True
                return True
        return False
