from __future__ import annotations

import math

import pytest

from bot.agents.runtime.backtesting import (
    BacktestResult,
    BacktestTrade,
    CommitteeBacktestRunner,
    WalkForwardValidator,
    WalkForwardWindow,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    tid: str = "t1",
    pnl: float = 10.0,
    score: float = 0.8,
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
) -> BacktestTrade:
    return BacktestTrade(
        trade_id=tid,
        symbol=symbol,
        direction=direction,
        entry_price=100.0,
        exit_price=110.0,
        pnl=pnl,
        entry_ts=1000.0,
        exit_ts=2000.0,
        committee_score=score,
    )


# ---------------------------------------------------------------------------
# BacktestTrade
# ---------------------------------------------------------------------------

class TestBacktestTrade:
    def test_to_dict_contains_all_fields(self):
        t = _make_trade()
        d = t.to_dict()
        assert d["trade_id"] == "t1"
        assert d["committee_score"] == 0.8
        assert d["committee_approved"] is None

    def test_default_committee_approved_is_none(self):
        t = _make_trade()
        assert t.committee_approved is None


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — all approved
# ---------------------------------------------------------------------------

class TestAllApproved:
    @pytest.mark.asyncio
    async def test_all_approved_high_scores(self):
        trades = [_make_trade(f"t{i}", pnl=10.0, score=0.9) for i in range(5)]
        runner = CommitteeBacktestRunner(approval_threshold=0.6)
        result = await runner.run(trades)
        assert result.approved_trades == 5
        assert result.rejected_trades == 0
        assert result.committee_pnl == result.baseline_pnl

    @pytest.mark.asyncio
    async def test_committee_pnl_equals_baseline_when_all_approved(self):
        trades = [_make_trade(f"t{i}", pnl=float(i), score=1.0) for i in range(3)]
        runner = CommitteeBacktestRunner(approval_threshold=0.5)
        result = await runner.run(trades)
        assert result.improvement_pnl == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — all rejected
# ---------------------------------------------------------------------------

class TestAllRejected:
    @pytest.mark.asyncio
    async def test_all_rejected_low_scores(self):
        trades = [_make_trade(f"t{i}", pnl=-5.0, score=0.1) for i in range(4)]
        runner = CommitteeBacktestRunner(approval_threshold=0.6)
        result = await runner.run(trades)
        assert result.approved_trades == 0
        assert result.rejected_trades == 4
        assert result.committee_pnl == 0.0

    @pytest.mark.asyncio
    async def test_committee_win_rate_zero_when_all_rejected(self):
        trades = [_make_trade(f"t{i}", pnl=10.0, score=0.0) for i in range(3)]
        runner = CommitteeBacktestRunner(approval_threshold=0.5)
        result = await runner.run(trades)
        assert result.committee_win_rate == 0.0


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — mixed
# ---------------------------------------------------------------------------

class TestMixed:
    @pytest.mark.asyncio
    async def test_mixed_approval(self):
        trades = [
            _make_trade("t1", pnl=20.0, score=0.9),   # approved
            _make_trade("t2", pnl=-10.0, score=0.3),   # rejected
            _make_trade("t3", pnl=15.0, score=0.7),    # approved
            _make_trade("t4", pnl=-5.0, score=0.2),    # rejected
        ]
        runner = CommitteeBacktestRunner(approval_threshold=0.6)
        result = await runner.run(trades)
        assert result.approved_trades == 2
        assert result.rejected_trades == 2
        assert result.committee_pnl == pytest.approx(35.0)
        assert result.baseline_pnl == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_improvement_calculation(self):
        trades = [
            _make_trade("t1", pnl=50.0, score=0.9),
            _make_trade("t2", pnl=-30.0, score=0.1),
        ]
        runner = CommitteeBacktestRunner(approval_threshold=0.5)
        result = await runner.run(trades)
        assert result.improvement_pnl == pytest.approx(50.0 - 20.0)


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — sharpe
# ---------------------------------------------------------------------------

class TestSharpe:
    @pytest.mark.asyncio
    async def test_sharpe_positive(self):
        pnls = [10.0, 12.0, 8.0, 11.0]
        sharpe = CommitteeBacktestRunner._calculate_sharpe(pnls)
        mean = sum(pnls) / len(pnls)
        var = sum((p - mean) ** 2 for p in pnls) / len(pnls)
        expected = mean / math.sqrt(var)
        assert sharpe == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_sharpe_zero_std(self):
        pnls = [5.0, 5.0, 5.0]
        assert CommitteeBacktestRunner._calculate_sharpe(pnls) == 0.0

    @pytest.mark.asyncio
    async def test_sharpe_empty(self):
        assert CommitteeBacktestRunner._calculate_sharpe([]) == 0.0


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — threshold sweep
# ---------------------------------------------------------------------------

class TestThresholdSweep:
    @pytest.mark.asyncio
    async def test_sweep_default_thresholds(self):
        trades = [_make_trade(f"t{i}", pnl=10.0, score=0.5) for i in range(3)]
        runner = CommitteeBacktestRunner()
        results = await runner.run_threshold_sweep(trades)
        assert len(results) == 6
        thresholds = [r["threshold"] for r in results]
        assert thresholds == [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

    @pytest.mark.asyncio
    async def test_sweep_custom_thresholds(self):
        trades = [_make_trade("t1", pnl=10.0, score=0.55)]
        runner = CommitteeBacktestRunner()
        results = await runner.run_threshold_sweep(trades, thresholds=[0.5, 0.6])
        assert len(results) == 2
        # 0.5 threshold: approved; 0.6 threshold: rejected
        assert results[0]["result"]["approved_trades"] == 1
        assert results[1]["result"]["approved_trades"] == 0


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — custom scorer
# ---------------------------------------------------------------------------

class TestCustomScorer:
    @pytest.mark.asyncio
    async def test_custom_scorer_overrides_score(self):
        trades = [_make_trade("t1", pnl=10.0, score=0.0)]

        async def always_high(trade: BacktestTrade) -> float:
            return 0.99

        runner = CommitteeBacktestRunner(approval_threshold=0.6)
        result = await runner.run(trades, committee_scorer=always_high)
        assert result.approved_trades == 1
        assert trades[0].committee_score == pytest.approx(0.99)


# ---------------------------------------------------------------------------
# CommitteeBacktestRunner — empty trades
# ---------------------------------------------------------------------------

class TestEmptyTrades:
    @pytest.mark.asyncio
    async def test_empty_trades(self):
        runner = CommitteeBacktestRunner()
        result = await runner.run([])
        assert result.total_trades == 0
        assert result.baseline_pnl == 0.0
        assert result.committee_pnl == 0.0
        assert result.baseline_sharpe == 0.0

    @pytest.mark.asyncio
    async def test_stats_after_empty_run(self):
        runner = CommitteeBacktestRunner()
        await runner.run([])
        stats = runner.get_stats()
        assert stats["runs"] == 1
        assert stats["total_trades_processed"] == 0


# ---------------------------------------------------------------------------
# WalkForwardValidator — generate_windows
# ---------------------------------------------------------------------------

class TestGenerateWindows:
    def test_correct_number_of_windows(self):
        v = WalkForwardValidator(n_windows=5, train_pct=0.7)
        windows = v.generate_windows(0.0, 1000.0)
        assert len(windows) == 5

    def test_correct_boundaries(self):
        v = WalkForwardValidator(n_windows=2, train_pct=0.5)
        windows = v.generate_windows(0.0, 100.0)
        # Window 0: [0,50), train [0,25), test [25,50)
        assert windows[0].train_start == pytest.approx(0.0)
        assert windows[0].train_end == pytest.approx(25.0)
        assert windows[0].test_start == pytest.approx(25.0)
        assert windows[0].test_end == pytest.approx(50.0)
        # Window 1: [50,100)
        assert windows[1].train_start == pytest.approx(50.0)
        assert windows[1].train_end == pytest.approx(75.0)
        assert windows[1].test_start == pytest.approx(75.0)
        assert windows[1].test_end == pytest.approx(100.0)

    def test_non_overlapping(self):
        v = WalkForwardValidator(n_windows=4, train_pct=0.7)
        windows = v.generate_windows(0.0, 400.0)
        for i in range(len(windows) - 1):
            assert windows[i].test_end == pytest.approx(windows[i + 1].train_start)

    def test_window_ids_sequential(self):
        v = WalkForwardValidator(n_windows=3)
        windows = v.generate_windows(0.0, 300.0)
        assert [w.window_id for w in windows] == [0, 1, 2]


# ---------------------------------------------------------------------------
# WalkForwardValidator — run
# ---------------------------------------------------------------------------

class TestWalkForwardRun:
    @pytest.mark.asyncio
    async def test_run_with_mock_fns(self):
        async def train_fn(start, end):
            return {"sharpe": 1.5, "params": {"a": 1}}

        async def test_fn(start, end, train_result):
            return {"sharpe": 1.0}

        v = WalkForwardValidator(n_windows=3, train_pct=0.7)
        result = await v.run(0.0, 300.0, train_fn, test_fn)
        assert len(result["windows"]) == 3
        assert result["avg_train_sharpe"] == pytest.approx(1.5)
        assert result["avg_test_sharpe"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_degradation_calculation(self):
        async def train_fn(start, end):
            return {"sharpe": 2.0}

        async def test_fn(start, end, train_result):
            return {"sharpe": 1.0}

        v = WalkForwardValidator(n_windows=2, train_pct=0.7)
        result = await v.run(0.0, 200.0, train_fn, test_fn)
        # degradation = (2.0 - 1.0) / 2.0 * 100 = 50%
        assert result["degradation_pct"] == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_consistency_all_positive(self):
        async def train_fn(s, e):
            return {"sharpe": 1.0}

        async def test_fn(s, e, tr):
            return {"sharpe": 0.5}

        v = WalkForwardValidator(n_windows=5, train_pct=0.7)
        result = await v.run(0.0, 500.0, train_fn, test_fn)
        assert result["consistent"] is True
        assert result["consistency_ratio"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_consistency_mixed(self):
        call_count = 0

        async def train_fn(s, e):
            return {"sharpe": 1.0}

        async def test_fn(s, e, tr):
            nonlocal call_count
            call_count += 1
            # 3 out of 5 positive → 0.6 → consistent
            return {"sharpe": 0.5 if call_count <= 3 else -0.5}

        v = WalkForwardValidator(n_windows=5, train_pct=0.7)
        result = await v.run(0.0, 500.0, train_fn, test_fn)
        assert result["consistency_ratio"] == pytest.approx(0.6)
        assert result["consistent"] is True

    @pytest.mark.asyncio
    async def test_consistency_below_threshold(self):
        call_count = 0

        async def train_fn(s, e):
            return {"sharpe": 1.0}

        async def test_fn(s, e, tr):
            nonlocal call_count
            call_count += 1
            # 1 out of 5 positive → 0.2 → not consistent
            return {"sharpe": 0.5 if call_count == 1 else -1.0}

        v = WalkForwardValidator(n_windows=5, train_pct=0.7)
        result = await v.run(0.0, 500.0, train_fn, test_fn)
        assert result["consistent"] is False

    @pytest.mark.asyncio
    async def test_single_window(self):
        async def train_fn(s, e):
            return {"sharpe": 2.0}

        async def test_fn(s, e, tr):
            return {"sharpe": 1.5}

        v = WalkForwardValidator(n_windows=1, train_pct=0.7)
        result = await v.run(0.0, 100.0, train_fn, test_fn)
        assert len(result["windows"]) == 1
        assert result["consistent"] is True

    @pytest.mark.asyncio
    async def test_stats_after_run(self):
        async def train_fn(s, e):
            return {"sharpe": 1.0}

        async def test_fn(s, e, tr):
            return {"sharpe": 0.5}

        v = WalkForwardValidator(n_windows=3, train_pct=0.7)
        await v.run(0.0, 300.0, train_fn, test_fn)
        stats = v.get_stats()
        assert stats["runs"] == 1
        assert stats["total_windows_processed"] == 3
        assert stats["n_windows"] == 3
        assert stats["train_pct"] == 0.7


# ---------------------------------------------------------------------------
# WalkForwardWindow to_dict
# ---------------------------------------------------------------------------

class TestWalkForwardWindow:
    def test_to_dict(self):
        w = WalkForwardWindow(
            window_id=0,
            train_start=0.0,
            train_end=70.0,
            test_start=70.0,
            test_end=100.0,
            train_result={"sharpe": 1.0},
            test_result={"sharpe": 0.5},
        )
        d = w.to_dict()
        assert d["window_id"] == 0
        assert d["train_result"] == {"sharpe": 1.0}
        assert d["test_result"] == {"sharpe": 0.5}


# ---------------------------------------------------------------------------
# BacktestResult to_dict
# ---------------------------------------------------------------------------

class TestBacktestResultDict:
    def test_to_dict(self):
        r = BacktestResult(
            total_trades=1,
            approved_trades=1,
            rejected_trades=0,
            baseline_pnl=10.0,
            committee_pnl=10.0,
            improvement_pnl=0.0,
            baseline_win_rate=1.0,
            committee_win_rate=1.0,
            baseline_sharpe=1.0,
            committee_sharpe=1.0,
            trades=[_make_trade()],
        )
        d = r.to_dict()
        assert d["total_trades"] == 1
        assert len(d["trades"]) == 1
        assert d["trades"][0]["trade_id"] == "t1"
