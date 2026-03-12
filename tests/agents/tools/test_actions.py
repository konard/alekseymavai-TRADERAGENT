from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from bot.agents.tools.actions import AlertSkill, HedgeSkill, ScaleSkill, VetoSkill


# ---------------------------------------------------------------------------
# AlertSkill
# ---------------------------------------------------------------------------

class TestAlertSkill:
    def test_create_alert(self):
        skill = AlertSkill()
        result = skill.execute({
            "symbol": "ETHUSDT",
            "target_price": 3600,
            "direction": "above",
            "message": "Resistance hit",
            "expert_name": "smc_expert",
        })
        assert result.success is True
        assert "alert_id" in result.data
        assert len(result.data["alert_id"]) == 12

    def test_check_alert_triggered_above(self):
        skill = AlertSkill()
        skill.execute({
            "symbol": "ETHUSDT",
            "target_price": 3600,
            "direction": "above",
        })
        triggered = skill.check_alerts({"ETHUSDT": 3650})
        assert len(triggered) == 1
        assert triggered[0]["symbol"] == "ETHUSDT"
        assert triggered[0]["triggered"] is True

    def test_check_alert_triggered_below(self):
        skill = AlertSkill()
        skill.execute({
            "symbol": "BTCUSDT",
            "target_price": 60000,
            "direction": "below",
        })
        triggered = skill.check_alerts({"BTCUSDT": 59000})
        assert len(triggered) == 1
        assert triggered[0]["direction"] == "below"

    def test_check_alert_not_triggered(self):
        skill = AlertSkill()
        skill.execute({
            "symbol": "ETHUSDT",
            "target_price": 3600,
            "direction": "above",
        })
        triggered = skill.check_alerts({"ETHUSDT": 3500})
        assert len(triggered) == 0

    def test_alert_not_triggered_twice(self):
        skill = AlertSkill()
        skill.execute({
            "symbol": "ETHUSDT",
            "target_price": 3600,
            "direction": "above",
        })
        skill.check_alerts({"ETHUSDT": 3700})
        triggered = skill.check_alerts({"ETHUSDT": 3800})
        assert len(triggered) == 0

    def test_cancel_alert(self):
        skill = AlertSkill()
        result = skill.execute({
            "symbol": "ETHUSDT",
            "target_price": 3600,
            "direction": "above",
        })
        alert_id = result.data["alert_id"]
        assert skill.cancel_alert(alert_id) is True
        assert skill.get_active_alerts() == []

    def test_cancel_nonexistent_alert(self):
        skill = AlertSkill()
        assert skill.cancel_alert("doesnotexist") is False

    def test_get_active_alerts(self):
        skill = AlertSkill()
        skill.execute({"symbol": "ETHUSDT", "target_price": 3600, "direction": "above"})
        skill.execute({"symbol": "BTCUSDT", "target_price": 60000, "direction": "below"})
        active = skill.get_active_alerts()
        assert len(active) == 2

    def test_invalid_params(self):
        skill = AlertSkill()
        result = skill.execute({"symbol": "ETHUSDT"})
        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# VetoSkill
# ---------------------------------------------------------------------------

class TestVetoSkill:
    def test_hard_veto_blocks(self):
        skill = VetoSkill()
        skill.execute({"reason": "drawdown_exceeded", "severity": "hard"})
        assert skill.is_vetoed() is True

    def test_soft_veto_does_not_block(self):
        skill = VetoSkill()
        skill.execute({"reason": "high_volatility", "severity": "soft"})
        assert skill.is_vetoed() is False

    def test_veto_with_hedge_action(self):
        skill = VetoSkill()
        result = skill.execute({
            "reason": "drawdown_exceeded",
            "severity": "hard",
            "hedge_action": {
                "symbol": "ETHUSDT",
                "direction": "short",
                "size_pct": 0.5,
            },
        })
        assert result.success is True
        vetoes = skill.get_active_vetoes()
        assert vetoes[0]["hedge_action"]["symbol"] == "ETHUSDT"

    def test_veto_expiry(self):
        skill = VetoSkill(ttl_seconds=60)
        skill.execute({"reason": "test", "severity": "hard"})
        assert skill.is_vetoed() is True

        future = time.time() + 120
        with patch("bot.agents.tools.actions.time") as mock_time:
            mock_time.time.return_value = future
            assert skill.is_vetoed() is False
            assert skill.get_active_vetoes() == []

    def test_clear_veto(self):
        skill = VetoSkill()
        result = skill.execute({"reason": "test", "severity": "hard"})
        veto_id = result.data["veto_id"]
        assert skill.clear_veto(veto_id) is True
        assert skill.is_vetoed() is False

    def test_clear_nonexistent_veto(self):
        skill = VetoSkill()
        assert skill.clear_veto("doesnotexist") is False

    def test_veto_log(self):
        skill = VetoSkill()
        skill.execute({"reason": "r1", "severity": "hard"})
        skill.execute({"reason": "r2", "severity": "soft"})
        vetoes = skill.get_active_vetoes()
        assert len(vetoes) == 2

    def test_invalid_severity(self):
        skill = VetoSkill()
        result = skill.execute({"reason": "test", "severity": "medium"})
        assert result.success is False


# ---------------------------------------------------------------------------
# ScaleSkill
# ---------------------------------------------------------------------------

class TestScaleSkill:
    def test_add_request(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "pos123",
            "action": "add",
            "amount_pct": 0.25,
            "reason": "DCA level hit",
            "expert_name": "dca_expert",
        })
        assert result.success is True
        assert "request_id" in result.data

    def test_reduce_request(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "pos456",
            "action": "reduce",
            "amount_pct": 0.5,
            "reason": "partial TP",
        })
        assert result.success is True

    def test_invalid_amount_too_low(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "pos123",
            "action": "add",
            "amount_pct": 0.001,
        })
        assert result.success is False
        assert "amount_pct" in result.error

    def test_invalid_amount_too_high(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "pos123",
            "action": "add",
            "amount_pct": 1.5,
        })
        assert result.success is False

    def test_invalid_action(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "pos123",
            "action": "swap",
            "amount_pct": 0.5,
        })
        assert result.success is False

    def test_missing_amount(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "pos123",
            "action": "add",
        })
        assert result.success is False

    def test_pending_queue(self):
        skill = ScaleSkill()
        skill.execute({"position_id": "p1", "action": "add", "amount_pct": 0.1})
        skill.execute({"position_id": "p2", "action": "reduce", "amount_pct": 0.5})
        pending = skill.get_pending_requests()
        assert len(pending) == 2

    def test_acknowledge_request(self):
        skill = ScaleSkill()
        result = skill.execute({
            "position_id": "p1",
            "action": "add",
            "amount_pct": 0.2,
        })
        req_id = result.data["request_id"]
        assert skill.acknowledge_request(req_id) is True
        assert len(skill.get_pending_requests()) == 0

    def test_acknowledge_nonexistent(self):
        skill = ScaleSkill()
        assert skill.acknowledge_request("nope") is False


# ---------------------------------------------------------------------------
# HedgeSkill
# ---------------------------------------------------------------------------

class TestHedgeSkill:
    def test_valid_hedge(self):
        skill = HedgeSkill()
        result = skill.execute({
            "original_symbol": "ETHUSDT",
            "hedge_symbol": "BTCUSDT",
            "correlation": 0.85,
            "hedge_ratio": 0.5,
            "direction": "short",
            "reason": "portfolio hedge",
        })
        assert result.success is True
        assert "request_id" in result.data

    def test_invalid_correlation_too_low(self):
        skill = HedgeSkill()
        result = skill.execute({
            "original_symbol": "ETHUSDT",
            "hedge_symbol": "BTCUSDT",
            "correlation": 0.2,
            "hedge_ratio": 0.5,
            "direction": "short",
        })
        assert result.success is False
        assert "correlation" in result.error

    def test_invalid_ratio_too_low(self):
        skill = HedgeSkill()
        result = skill.execute({
            "original_symbol": "ETHUSDT",
            "hedge_symbol": "BTCUSDT",
            "correlation": 0.85,
            "hedge_ratio": 0.05,
            "direction": "short",
        })
        assert result.success is False
        assert "hedge_ratio" in result.error

    def test_invalid_ratio_too_high(self):
        skill = HedgeSkill()
        result = skill.execute({
            "original_symbol": "ETHUSDT",
            "hedge_symbol": "BTCUSDT",
            "correlation": 0.85,
            "hedge_ratio": 3.0,
            "direction": "short",
        })
        assert result.success is False

    def test_pending_hedges(self):
        skill = HedgeSkill()
        skill.execute({
            "original_symbol": "ETHUSDT",
            "hedge_symbol": "BTCUSDT",
            "correlation": 0.85,
            "hedge_ratio": 0.5,
            "direction": "short",
        })
        pending = skill.get_pending_hedges()
        assert len(pending) == 1
        assert pending[0]["original_symbol"] == "ETHUSDT"

    def test_acknowledge_hedge(self):
        skill = HedgeSkill()
        result = skill.execute({
            "original_symbol": "ETHUSDT",
            "hedge_symbol": "BTCUSDT",
            "correlation": 0.85,
            "hedge_ratio": 0.5,
            "direction": "short",
        })
        req_id = result.data["request_id"]
        assert skill.acknowledge_hedge(req_id) is True
        assert len(skill.get_pending_hedges()) == 0

    def test_acknowledge_nonexistent(self):
        skill = HedgeSkill()
        assert skill.acknowledge_hedge("nope") is False

    def test_missing_required_fields(self):
        skill = HedgeSkill()
        result = skill.execute({"original_symbol": "ETHUSDT"})
        assert result.success is False
