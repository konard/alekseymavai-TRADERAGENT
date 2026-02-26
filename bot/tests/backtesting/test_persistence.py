"""
Tests for persistence modules: checkpoint, job_store, preset_export.

Phase 2 tests.
"""

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bot.tests.backtesting.backtesting_engine import BacktestResult
from bot.tests.backtesting.checkpoint import OptimizationCheckpoint
from bot.tests.backtesting.job_store import JobStore
from bot.tests.backtesting.preset_export import PresetExporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(**overrides) -> BacktestResult:
    """Create a minimal BacktestResult for testing."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        strategy_name="test",
        symbol="BTC/USDT",
        start_time=now - timedelta(days=1),
        end_time=now,
        duration=timedelta(days=1),
        initial_balance=Decimal("10000"),
        final_balance=Decimal("10500"),
        total_return=Decimal("500"),
        total_return_pct=Decimal("5.0"),
        max_drawdown=Decimal("200"),
        max_drawdown_pct=Decimal("2.0"),
        total_trades=10,
        winning_trades=7,
        losing_trades=3,
        win_rate=Decimal("70"),
        total_buy_orders=10,
        total_sell_orders=10,
        avg_profit_per_trade=Decimal("50"),
        sharpe_ratio=Decimal("1.5"),
        sortino_ratio=Decimal("2.0"),
        calmar_ratio=Decimal("2.5"),
        profit_factor=Decimal("2.33"),
    )
    defaults.update(overrides)
    return BacktestResult(**defaults)


# ===========================================================================
# Checkpoint Tests
# ===========================================================================


class TestOptimizationCheckpoint:
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt = OptimizationCheckpoint(directory=tmpdir)
            ckpt.save_trial("run1", "t1", "hash_a", {"return": 5.0})
            completed = ckpt.load_completed("run1")
            assert "hash_a" in completed
            assert completed["hash_a"]["return"] == 5.0

    def test_cleanup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt = OptimizationCheckpoint(directory=tmpdir)
            ckpt.save_trial("run1", "t1", "hash_a", {"return": 5.0})
            ckpt.cleanup("run1")
            completed = ckpt.load_completed("run1")
            assert len(completed) == 0

    def test_config_hash_deterministic(self):
        h1 = OptimizationCheckpoint.config_hash({"a": 1, "b": 2})
        h2 = OptimizationCheckpoint.config_hash({"b": 2, "a": 1})
        assert h1 == h2
        assert len(h1) == 16

    def test_multiple_trials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt = OptimizationCheckpoint(directory=tmpdir)
            ckpt.save_trial("run1", "t1", "hash_a", {"return": 1.0})
            ckpt.save_trial("run1", "t2", "hash_b", {"return": 2.0})
            ckpt.save_trial("run1", "t3", "hash_c", {"return": 3.0})
            completed = ckpt.load_completed("run1")
            assert len(completed) == 3
            assert completed["hash_b"]["return"] == 2.0

    def test_load_nonexistent_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ckpt = OptimizationCheckpoint(directory=tmpdir)
            completed = ckpt.load_completed("nonexistent")
            assert completed == {}


# ===========================================================================
# Job Store Tests
# ===========================================================================


class TestJobStore:
    def test_create_and_get(self):
        store = JobStore(":memory:")
        store.initialize()
        job_id = store.create("backtest", {"symbol": "BTC/USDT"})
        job = store.get(job_id)
        assert job is not None
        assert job["job_type"] == "backtest"
        assert job["status"] == "pending"
        assert job["config"]["symbol"] == "BTC/USDT"
        store.close()

    def test_update_status(self):
        store = JobStore(":memory:")
        store.initialize()
        job_id = store.create("backtest")
        store.update_status(job_id, "running")
        job = store.get(job_id)
        assert job["status"] == "running"

        store.update_status(job_id, "completed", result={"return_pct": 5.0})
        job = store.get(job_id)
        assert job["status"] == "completed"
        assert job["result"]["return_pct"] == 5.0
        store.close()

    def test_update_status_with_error(self):
        store = JobStore(":memory:")
        store.initialize()
        job_id = store.create("backtest")
        store.update_status(job_id, "failed", error="Connection timeout")
        job = store.get(job_id)
        assert job["status"] == "failed"
        assert job["error_message"] == "Connection timeout"
        store.close()

    def test_list_filter(self):
        store = JobStore(":memory:")
        store.initialize()
        store.create("backtest", {"symbol": "BTC"})
        store.create("optimization", {"symbol": "ETH"})
        store.create("backtest", {"symbol": "SOL"})

        all_jobs = store.list_jobs()
        assert len(all_jobs) == 3

        bt_jobs = store.list_jobs(job_type="backtest")
        assert len(bt_jobs) == 2

        opt_jobs = store.list_jobs(job_type="optimization")
        assert len(opt_jobs) == 1
        store.close()

    def test_list_filter_by_status(self):
        store = JobStore(":memory:")
        store.initialize()
        j1 = store.create("backtest")
        j2 = store.create("backtest")
        store.update_status(j1, "completed")

        completed = store.list_jobs(status="completed")
        assert len(completed) == 1
        assert completed[0]["job_id"] == j1
        store.close()

    def test_cleanup_old(self):
        store = JobStore(":memory:")
        store.initialize()
        # Create a job and manually backdate it
        job_id = store.create("backtest")
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        store._conn.execute(
            "UPDATE jobs SET created_at = ? WHERE job_id = ?",
            (old_time, job_id),
        )
        store._conn.commit()

        # Create a recent job
        store.create("backtest")

        deleted = store.cleanup_old(days=30)
        assert deleted == 1
        assert len(store.list_jobs()) == 1
        store.close()

    def test_get_nonexistent(self):
        store = JobStore(":memory:")
        store.initialize()
        assert store.get("nonexistent") is None
        store.close()


# ===========================================================================
# Preset Export Tests
# ===========================================================================


class TestPresetExporter:
    def test_export_yaml(self):
        exporter = PresetExporter()
        result = _make_result()
        yaml_str = exporter.export_yaml("smc", {"tp_pct": 0.02, "sl_pct": 0.01}, result)
        assert "strategy: smc" in yaml_str
        assert "tp_pct: 0.02" in yaml_str
        assert "sl_pct: 0.01" in yaml_str
        assert "total_return_pct:" in yaml_str
        assert "sharpe_ratio:" in yaml_str

    def test_export_json(self):
        exporter = PresetExporter()
        result = _make_result()
        json_str = exporter.export_json("grid", {"levels": 10}, result)
        parsed = json.loads(json_str)
        assert parsed["strategy"] == "grid"
        assert parsed["params"]["levels"] == 10
        assert "metrics" in parsed
        assert parsed["metrics"]["total_trades"] == 10

    def test_export_save(self):
        exporter = PresetExporter()
        result = _make_result()
        content = exporter.export_json("test", {}, result)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = exporter.save(f"{tmpdir}/preset.json", content)
            assert path.exists()
            loaded = json.loads(path.read_text())
            assert loaded["strategy"] == "test"

    def test_export_yaml_with_decimal_params(self):
        exporter = PresetExporter()
        result = _make_result()
        yaml_str = exporter.export_yaml(
            "test", {"tp": Decimal("0.015"), "sl": Decimal("0.01")}, result
        )
        assert "tp: 0.015" in yaml_str

    def test_export_json_metrics_complete(self):
        exporter = PresetExporter()
        result = _make_result()
        json_str = exporter.export_json("test", {}, result)
        parsed = json.loads(json_str)
        metrics = parsed["metrics"]
        assert "sortino_ratio" in metrics
        assert "calmar_ratio" in metrics
        assert "profit_factor" in metrics
        assert "win_rate" in metrics
