from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum


class PipelineStage(Enum):
    MARKET_DATA = "market_data"
    ANALYSIS = "analysis"
    COMMITTEE_VOTE = "committee_vote"
    POSITION_SIZING = "position_sizing"
    EXECUTION = "execution"
    MONITORING = "monitoring"
    EXIT = "exit"


_PIPELINE_ORDER = [
    PipelineStage.MARKET_DATA,
    PipelineStage.ANALYSIS,
    PipelineStage.COMMITTEE_VOTE,
    PipelineStage.POSITION_SIZING,
    PipelineStage.EXECUTION,
    PipelineStage.MONITORING,
]


@dataclass
class PipelineResult:
    stage: PipelineStage
    success: bool
    data: dict
    duration_ms: float
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "success": self.success,
            "data": self.data,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


def _default_handler(stage: PipelineStage) -> Callable[[dict], Awaitable[dict]]:
    """Return a default async handler that produces mock data for a stage."""

    async def handler(context: dict) -> dict:
        defaults: dict[PipelineStage, dict] = {
            PipelineStage.MARKET_DATA: {
                "price": 0.0,
                "volume": 0.0,
                "timestamp": time.time(),
            },
            PipelineStage.ANALYSIS: {
                "trend": "neutral",
                "confidence": 0.5,
            },
            PipelineStage.COMMITTEE_VOTE: {
                "vote": "hold",
                "votes_for": 0,
                "votes_against": 0,
            },
            PipelineStage.POSITION_SIZING: {
                "size": 0.0,
                "risk_pct": 0.0,
            },
            PipelineStage.EXECUTION: {
                "executed": False,
                "order_id": None,
            },
            PipelineStage.MONITORING: {
                "status": "idle",
            },
            PipelineStage.EXIT: {
                "exit_signal": False,
            },
        }
        return defaults.get(stage, {})

    return handler


class SignalPipeline:
    def __init__(self) -> None:
        self._handlers: dict[PipelineStage, Callable[[dict], Awaitable[dict]]] = {}
        self._last_run: list[PipelineResult] | None = None
        self._total_runs: int = 0
        self._successful_runs: int = 0
        self._stage_durations: dict[PipelineStage, list[float]] = {s: [] for s in PipelineStage}

    def register_stage(
        self, stage: PipelineStage, handler: Callable[[dict], Awaitable[dict]]
    ) -> None:
        self._handlers[stage] = handler

    async def run(self, signal: dict) -> list[PipelineResult]:
        return await self._execute(signal, skip_execution=False)

    async def run_dry(self, signal: dict) -> list[PipelineResult]:
        return await self._execute(signal, skip_execution=True)

    async def _execute(self, signal: dict, *, skip_execution: bool) -> list[PipelineResult]:
        results: list[PipelineResult] = []
        context: dict = dict(signal)

        for stage in _PIPELINE_ORDER:
            if skip_execution and stage == PipelineStage.EXECUTION:
                continue

            handler = self._handlers.get(stage, _default_handler(stage))
            t0 = time.monotonic()
            try:
                output = await handler(context)
            except Exception as exc:
                duration_ms = (time.monotonic() - t0) * 1000
                results.append(
                    PipelineResult(
                        stage=stage,
                        success=False,
                        data={},
                        duration_ms=duration_ms,
                        error=str(exc),
                    )
                )
                self._stage_durations[stage].append(duration_ms)
                self._total_runs += 1
                self._last_run = results
                return results

            duration_ms = (time.monotonic() - t0) * 1000

            if isinstance(output, dict) and "error" in output:
                results.append(
                    PipelineResult(
                        stage=stage,
                        success=False,
                        data=output,
                        duration_ms=duration_ms,
                        error=output["error"],
                    )
                )
                self._stage_durations[stage].append(duration_ms)
                self._total_runs += 1
                self._last_run = results
                return results

            results.append(
                PipelineResult(
                    stage=stage,
                    success=True,
                    data=output if isinstance(output, dict) else {},
                    duration_ms=duration_ms,
                )
            )
            self._stage_durations[stage].append(duration_ms)
            if isinstance(output, dict):
                context.update(output)

        self._total_runs += 1
        self._successful_runs += 1
        self._last_run = results
        return results

    def get_last_run(self) -> list[PipelineResult] | None:
        return self._last_run

    def get_stats(self) -> dict:
        all_durations: list[float] = []
        stage_avg: dict[str, float] = {}
        for stage, durations in self._stage_durations.items():
            if durations:
                stage_avg[stage.value] = sum(durations) / len(durations)
                all_durations.extend(durations)

        return {
            "total_runs": self._total_runs,
            "success_rate": (self._successful_runs / self._total_runs if self._total_runs else 0.0),
            "avg_duration_ms": (sum(all_durations) / len(all_durations) if all_durations else 0.0),
            "stage_durations": stage_avg,
        }


class AutoPilotMode(Enum):
    OFF = "off"
    ADVISORY = "advisory"
    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"


class AutoPilot:
    def __init__(
        self,
        pipeline: SignalPipeline,
        mode: AutoPilotMode = AutoPilotMode.OFF,
    ) -> None:
        self._pipeline = pipeline
        self._mode = mode
        self._confirmation_callback: Callable[[dict], Awaitable[bool]] | None = None
        self._signals_processed: int = 0
        self._last_signal_ts: float | None = None
        self._start_time: float = time.time()
        self._max_trades_per_hour: int = 5
        self._max_risk_pct: float = 0.05
        self._allowed_symbols: list[str] | None = None
        self._trade_timestamps: list[float] = []
        self._pending_task: asyncio.Task | None = None

    def set_mode(self, mode: AutoPilotMode) -> None:
        self._mode = mode

    def set_confirmation_callback(self, callback: Callable[[dict], Awaitable[bool]]) -> None:
        self._confirmation_callback = callback

    def set_constraints(
        self,
        max_trades_per_hour: int = 5,
        max_risk_pct: float = 0.05,
        allowed_symbols: list[str] | None = None,
    ) -> None:
        self._max_trades_per_hour = max_trades_per_hour
        self._max_risk_pct = max_risk_pct
        self._allowed_symbols = allowed_symbols

    def _check_constraints(self, signal: dict) -> tuple[bool, str]:
        now = time.time()
        cutoff = now - 3600
        self._trade_timestamps = [t for t in self._trade_timestamps if t > cutoff]
        if len(self._trade_timestamps) >= self._max_trades_per_hour:
            return False, f"max trades per hour exceeded ({self._max_trades_per_hour})"

        risk = signal.get("risk_pct", 0.0)
        if risk > self._max_risk_pct:
            return False, f"risk {risk} exceeds max {self._max_risk_pct}"

        symbol = signal.get("symbol")
        if self._allowed_symbols is not None and symbol not in self._allowed_symbols:
            return False, f"symbol {symbol} not in allowed list"

        return True, ""

    async def process_signal(self, signal: dict) -> dict:
        self._signals_processed += 1
        self._last_signal_ts = time.time()

        if self._mode == AutoPilotMode.OFF:
            return {"action": "ignored", "reason": "autopilot off"}

        if self._mode == AutoPilotMode.ADVISORY:
            results = await self._pipeline.run_dry(signal)
            return {
                "action": "recommendation",
                "results": [r.to_dict() for r in results],
                "executed": False,
            }

        if self._mode == AutoPilotMode.SEMI_AUTO:
            results = await self._pipeline.run_dry(signal)
            recommendation = {
                "action": "awaiting_confirmation",
                "results": [r.to_dict() for r in results],
                "executed": False,
            }
            if self._confirmation_callback is not None:
                confirmed = await self._confirmation_callback(recommendation)
                if confirmed:
                    allowed, reason = self._check_constraints(signal)
                    if not allowed:
                        return {
                            "action": "blocked",
                            "reason": reason,
                            "executed": False,
                        }
                    results = await self._pipeline.run(signal)
                    self._trade_timestamps.append(time.time())
                    return {
                        "action": "executed",
                        "results": [r.to_dict() for r in results],
                        "executed": True,
                    }
                return {
                    "action": "rejected",
                    "results": [r.to_dict() for r in results],
                    "executed": False,
                }
            return recommendation

        if self._mode == AutoPilotMode.FULL_AUTO:
            allowed, reason = self._check_constraints(signal)
            if not allowed:
                return {"action": "blocked", "reason": reason, "executed": False}
            results = await self._pipeline.run(signal)
            self._trade_timestamps.append(time.time())
            return {
                "action": "executed",
                "results": [r.to_dict() for r in results],
                "executed": True,
            }

        return {"action": "unknown_mode", "executed": False}

    def kill_switch(self) -> None:
        self._mode = AutoPilotMode.OFF
        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            self._pending_task = None

    def is_active(self) -> bool:
        return self._mode != AutoPilotMode.OFF

    def get_status(self) -> dict:
        return {
            "mode": self._mode.value,
            "signals_processed": self._signals_processed,
            "last_signal_ts": self._last_signal_ts,
            "uptime_seconds": time.time() - self._start_time,
        }

    def get_stats(self) -> dict:
        return {
            "signals_processed": self._signals_processed,
            "mode": self._mode.value,
            "pipeline_stats": self._pipeline.get_stats(),
        }
