from __future__ import annotations

import asyncio
import pytest
import time

from bot.agents.runtime.pipeline import (
    AutoPilot,
    AutoPilotMode,
    PipelineResult,
    PipelineStage,
    SignalPipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _echo_handler(context: dict) -> dict:
    return {"echo": True}


async def _failing_handler(context: dict) -> dict:
    raise RuntimeError("stage exploded")


async def _error_dict_handler(context: dict) -> dict:
    return {"error": "something went wrong"}


async def _analysis_handler(context: dict) -> dict:
    return {"trend": "bullish", "confidence": 0.9}


async def _execution_handler(context: dict) -> dict:
    return {"executed": True, "order_id": "ORD-123"}


async def _monitoring_handler(context: dict) -> dict:
    return {"status": "watching"}


# ---------------------------------------------------------------------------
# PipelineResult
# ---------------------------------------------------------------------------

class TestPipelineResult:
    def test_to_dict(self):
        r = PipelineResult(
            stage=PipelineStage.ANALYSIS,
            success=True,
            data={"a": 1},
            duration_ms=12.5,
        )
        d = r.to_dict()
        assert d["stage"] == "analysis"
        assert d["success"] is True
        assert d["data"] == {"a": 1}
        assert d["duration_ms"] == 12.5
        assert d["error"] is None

    def test_to_dict_with_error(self):
        r = PipelineResult(
            stage=PipelineStage.EXECUTION,
            success=False,
            data={},
            duration_ms=1.0,
            error="boom",
        )
        d = r.to_dict()
        assert d["error"] == "boom"
        assert d["success"] is False


# ---------------------------------------------------------------------------
# SignalPipeline
# ---------------------------------------------------------------------------

class TestSignalPipeline:
    @pytest.mark.asyncio
    async def test_register_and_run_custom_handler(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.ANALYSIS, _analysis_handler)
        results = await pipe.run({"symbol": "BTCUSDT"})
        analysis = [r for r in results if r.stage == PipelineStage.ANALYSIS][0]
        assert analysis.success is True
        assert analysis.data["trend"] == "bullish"

    @pytest.mark.asyncio
    async def test_full_pipeline_runs_all_stages(self):
        pipe = SignalPipeline()
        results = await pipe.run({"symbol": "ETHUSDT"})
        stages_run = [r.stage for r in results]
        assert PipelineStage.MARKET_DATA in stages_run
        assert PipelineStage.ANALYSIS in stages_run
        assert PipelineStage.COMMITTEE_VOTE in stages_run
        assert PipelineStage.POSITION_SIZING in stages_run
        assert PipelineStage.EXECUTION in stages_run
        assert PipelineStage.MONITORING in stages_run
        assert len(results) == 6

    @pytest.mark.asyncio
    async def test_stage_failure_stops_pipeline(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.ANALYSIS, _failing_handler)
        results = await pipe.run({"symbol": "BTCUSDT"})
        assert len(results) == 2  # MARKET_DATA ok, ANALYSIS failed
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].error == "stage exploded"

    @pytest.mark.asyncio
    async def test_error_dict_stops_pipeline(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.COMMITTEE_VOTE, _error_dict_handler)
        results = await pipe.run({"symbol": "BTCUSDT"})
        vote_result = results[-1]
        assert vote_result.stage == PipelineStage.COMMITTEE_VOTE
        assert vote_result.success is False
        assert vote_result.error == "something went wrong"

    @pytest.mark.asyncio
    async def test_dry_run_skips_execution(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.EXECUTION, _execution_handler)
        results = await pipe.run_dry({"symbol": "BTCUSDT"})
        stages_run = [r.stage for r in results]
        assert PipelineStage.EXECUTION not in stages_run
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        pipe = SignalPipeline()
        await pipe.run({"symbol": "A"})
        await pipe.run({"symbol": "B"})
        stats = pipe.get_stats()
        assert stats["total_runs"] == 2
        assert stats["success_rate"] == 1.0
        assert stats["avg_duration_ms"] >= 0
        assert "market_data" in stats["stage_durations"]

    @pytest.mark.asyncio
    async def test_stats_with_failure(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.MARKET_DATA, _failing_handler)
        await pipe.run({})
        stats = pipe.get_stats()
        assert stats["total_runs"] == 1
        assert stats["success_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_default_handlers_produce_data(self):
        pipe = SignalPipeline()
        results = await pipe.run({})
        for r in results:
            assert r.success is True
            assert isinstance(r.data, dict)
            assert len(r.data) > 0

    @pytest.mark.asyncio
    async def test_empty_pipeline_uses_defaults(self):
        pipe = SignalPipeline()
        results = await pipe.run({})
        assert all(r.success for r in results)
        assert len(results) == 6

    @pytest.mark.asyncio
    async def test_get_last_run(self):
        pipe = SignalPipeline()
        assert pipe.get_last_run() is None
        await pipe.run({"x": 1})
        last = pipe.get_last_run()
        assert last is not None
        assert len(last) == 6

    @pytest.mark.asyncio
    async def test_context_accumulates(self):
        """Each stage receives merged context from all previous stages."""
        received_contexts = []

        async def capture_market(ctx: dict) -> dict:
            received_contexts.append(dict(ctx))
            return {"from_market": True}

        async def capture_analysis(ctx: dict) -> dict:
            received_contexts.append(dict(ctx))
            return {"from_analysis": True}

        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.MARKET_DATA, capture_market)
        pipe.register_stage(PipelineStage.ANALYSIS, capture_analysis)
        await pipe.run({"initial": "yes"})

        # Analysis handler should see initial signal + market data output
        assert "initial" in received_contexts[1]
        assert "from_market" in received_contexts[1]

    @pytest.mark.asyncio
    async def test_duration_is_recorded(self):
        async def slow_handler(ctx: dict) -> dict:
            await asyncio.sleep(0.01)
            return {"done": True}

        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.MARKET_DATA, slow_handler)
        results = await pipe.run({})
        assert results[0].duration_ms >= 5  # at least ~10ms


# ---------------------------------------------------------------------------
# AutoPilot
# ---------------------------------------------------------------------------

class TestAutoPilot:
    @pytest.mark.asyncio
    async def test_off_mode_ignores(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.OFF)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "ignored"
        assert result["reason"] == "autopilot off"

    @pytest.mark.asyncio
    async def test_advisory_returns_recommendation(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.ADVISORY)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "recommendation"
        assert result["executed"] is False
        assert "results" in result

    @pytest.mark.asyncio
    async def test_full_auto_executes(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.EXECUTION, _execution_handler)
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "executed"
        assert result["executed"] is True

    @pytest.mark.asyncio
    async def test_kill_switch(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        assert ap.is_active() is True
        ap.kill_switch()
        assert ap.is_active() is False
        assert ap._mode == AutoPilotMode.OFF
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "ignored"

    @pytest.mark.asyncio
    async def test_constraint_max_trades(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        ap.set_constraints(max_trades_per_hour=2)
        await ap.process_signal({"symbol": "BTCUSDT"})
        await ap.process_signal({"symbol": "BTCUSDT"})
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "blocked"
        assert "max trades" in result["reason"]

    @pytest.mark.asyncio
    async def test_constraint_allowed_symbols(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        ap.set_constraints(allowed_symbols=["BTCUSDT", "ETHUSDT"])
        result = await ap.process_signal({"symbol": "DOGEUSDT"})
        assert result["action"] == "blocked"
        assert "not in allowed" in result["reason"]

    @pytest.mark.asyncio
    async def test_constraint_allowed_symbol_passes(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        ap.set_constraints(allowed_symbols=["BTCUSDT"])
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "executed"

    @pytest.mark.asyncio
    async def test_constraint_max_risk(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        ap.set_constraints(max_risk_pct=0.02)
        result = await ap.process_signal({"symbol": "BTCUSDT", "risk_pct": 0.05})
        assert result["action"] == "blocked"
        assert "risk" in result["reason"]

    @pytest.mark.asyncio
    async def test_mode_switching(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe)
        assert ap._mode == AutoPilotMode.OFF

        ap.set_mode(AutoPilotMode.ADVISORY)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "recommendation"

        ap.set_mode(AutoPilotMode.FULL_AUTO)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "executed"

    @pytest.mark.asyncio
    async def test_status(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.ADVISORY)
        await ap.process_signal({"symbol": "BTCUSDT"})
        status = ap.get_status()
        assert status["mode"] == "advisory"
        assert status["signals_processed"] == 1
        assert status["last_signal_ts"] is not None
        assert status["uptime_seconds"] >= 0

    @pytest.mark.asyncio
    async def test_get_stats(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.FULL_AUTO)
        await ap.process_signal({"symbol": "BTCUSDT"})
        stats = ap.get_stats()
        assert stats["signals_processed"] == 1
        assert stats["mode"] == "full_auto"
        assert "pipeline_stats" in stats

    @pytest.mark.asyncio
    async def test_semi_auto_with_confirmation_accepted(self):
        pipe = SignalPipeline()
        pipe.register_stage(PipelineStage.EXECUTION, _execution_handler)
        ap = AutoPilot(pipe, AutoPilotMode.SEMI_AUTO)

        async def confirm(rec: dict) -> bool:
            return True

        ap.set_confirmation_callback(confirm)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "executed"
        assert result["executed"] is True

    @pytest.mark.asyncio
    async def test_semi_auto_with_confirmation_rejected(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.SEMI_AUTO)

        async def reject(rec: dict) -> bool:
            return False

        ap.set_confirmation_callback(reject)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "rejected"
        assert result["executed"] is False

    @pytest.mark.asyncio
    async def test_semi_auto_no_callback(self):
        pipe = SignalPipeline()
        ap = AutoPilot(pipe, AutoPilotMode.SEMI_AUTO)
        result = await ap.process_signal({"symbol": "BTCUSDT"})
        assert result["action"] == "awaiting_confirmation"
        assert result["executed"] is False
