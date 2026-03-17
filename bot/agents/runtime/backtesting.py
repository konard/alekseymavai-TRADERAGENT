from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class BacktestTrade:
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    pnl: float
    entry_ts: float
    exit_ts: float
    committee_approved: bool | None = None
    committee_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl": self.pnl,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "committee_approved": self.committee_approved,
            "committee_score": self.committee_score,
        }


@dataclass
class BacktestResult:
    total_trades: int
    approved_trades: int
    rejected_trades: int
    baseline_pnl: float
    committee_pnl: float
    improvement_pnl: float
    baseline_win_rate: float
    committee_win_rate: float
    baseline_sharpe: float
    committee_sharpe: float
    trades: list[BacktestTrade] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_trades": self.total_trades,
            "approved_trades": self.approved_trades,
            "rejected_trades": self.rejected_trades,
            "baseline_pnl": self.baseline_pnl,
            "committee_pnl": self.committee_pnl,
            "improvement_pnl": self.improvement_pnl,
            "baseline_win_rate": self.baseline_win_rate,
            "committee_win_rate": self.committee_win_rate,
            "baseline_sharpe": self.baseline_sharpe,
            "committee_sharpe": self.committee_sharpe,
            "trades": [t.to_dict() for t in self.trades],
        }


class CommitteeBacktestRunner:
    def __init__(self, approval_threshold: float = 0.6) -> None:
        self.approval_threshold = approval_threshold
        self._runs: int = 0
        self._total_trades_processed: int = 0

    async def run(
        self,
        trades: list[BacktestTrade],
        committee_scorer: callable | None = None,
    ) -> BacktestResult:
        self._runs += 1
        self._total_trades_processed += len(trades)

        for trade in trades:
            if committee_scorer is not None:
                trade.committee_score = await committee_scorer(trade)
            trade.committee_approved = trade.committee_score >= self.approval_threshold

        approved = [t for t in trades if t.committee_approved]
        rejected = [t for t in trades if not t.committee_approved]

        baseline_pnls = [t.pnl for t in trades]
        committee_pnls = [t.pnl for t in approved]

        baseline_pnl = sum(baseline_pnls)
        committee_pnl = sum(committee_pnls)

        baseline_wins = sum(1 for p in baseline_pnls if p > 0)
        committee_wins = sum(1 for p in committee_pnls if p > 0)

        baseline_win_rate = baseline_wins / len(trades) if trades else 0.0
        committee_win_rate = committee_wins / len(approved) if approved else 0.0

        return BacktestResult(
            total_trades=len(trades),
            approved_trades=len(approved),
            rejected_trades=len(rejected),
            baseline_pnl=baseline_pnl,
            committee_pnl=committee_pnl,
            improvement_pnl=committee_pnl - baseline_pnl,
            baseline_win_rate=baseline_win_rate,
            committee_win_rate=committee_win_rate,
            baseline_sharpe=self._calculate_sharpe(baseline_pnls),
            committee_sharpe=self._calculate_sharpe(committee_pnls),
            trades=trades,
        )

    @staticmethod
    def _calculate_sharpe(pnls: list[float]) -> float:
        if not pnls:
            return 0.0
        n = len(pnls)
        mean = sum(pnls) / n
        variance = sum((p - mean) ** 2 for p in pnls) / n
        std = math.sqrt(variance)
        if std == 0:
            return 0.0
        return mean / std

    async def run_threshold_sweep(
        self,
        trades: list[BacktestTrade],
        thresholds: list[float] | None = None,
    ) -> list[dict]:
        if thresholds is None:
            thresholds = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

        results: list[dict] = []
        for threshold in thresholds:
            # Deep-copy trades so each sweep iteration is independent
            copied = [
                BacktestTrade(
                    trade_id=t.trade_id,
                    symbol=t.symbol,
                    direction=t.direction,
                    entry_price=t.entry_price,
                    exit_price=t.exit_price,
                    pnl=t.pnl,
                    entry_ts=t.entry_ts,
                    exit_ts=t.exit_ts,
                    committee_score=t.committee_score,
                )
                for t in trades
            ]
            runner = CommitteeBacktestRunner(approval_threshold=threshold)
            result = await runner.run(copied)
            results.append({"threshold": threshold, "result": result.to_dict()})
        return results

    def get_stats(self) -> dict:
        return {
            "runs": self._runs,
            "total_trades_processed": self._total_trades_processed,
            "approval_threshold": self.approval_threshold,
        }


@dataclass
class WalkForwardWindow:
    window_id: int
    train_start: float
    train_end: float
    test_start: float
    test_end: float
    train_result: dict | None = None
    test_result: dict | None = None

    def to_dict(self) -> dict:
        return {
            "window_id": self.window_id,
            "train_start": self.train_start,
            "train_end": self.train_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "train_result": self.train_result,
            "test_result": self.test_result,
        }


class WalkForwardValidator:
    def __init__(self, n_windows: int = 5, train_pct: float = 0.7) -> None:
        self.n_windows = n_windows
        self.train_pct = train_pct
        self._runs: int = 0
        self._total_windows_processed: int = 0

    def generate_windows(self, start_ts: float, end_ts: float) -> list[WalkForwardWindow]:
        total = end_ts - start_ts
        window_size = total / self.n_windows
        windows: list[WalkForwardWindow] = []
        for i in range(self.n_windows):
            w_start = start_ts + i * window_size
            w_end = w_start + window_size
            train_end = w_start + window_size * self.train_pct
            windows.append(
                WalkForwardWindow(
                    window_id=i,
                    train_start=w_start,
                    train_end=train_end,
                    test_start=train_end,
                    test_end=w_end,
                )
            )
        return windows

    async def run(
        self,
        start_ts: float,
        end_ts: float,
        train_fn: callable,
        test_fn: callable,
    ) -> dict:
        self._runs += 1
        windows = self.generate_windows(start_ts, end_ts)
        self._total_windows_processed += len(windows)

        train_sharpes: list[float] = []
        test_sharpes: list[float] = []

        for w in windows:
            train_result = await train_fn(w.train_start, w.train_end)
            w.train_result = train_result
            test_result = await test_fn(w.test_start, w.test_end, train_result)
            w.test_result = test_result
            train_sharpes.append(train_result.get("sharpe", 0.0))
            test_sharpes.append(test_result.get("sharpe", 0.0))

        avg_train = sum(train_sharpes) / len(train_sharpes) if train_sharpes else 0.0
        avg_test = sum(test_sharpes) / len(test_sharpes) if test_sharpes else 0.0

        if avg_train != 0:
            degradation_pct = (avg_train - avg_test) / avg_train * 100
        else:
            degradation_pct = 0.0

        positive_test = sum(1 for s in test_sharpes if s > 0)
        consistency_ratio = positive_test / len(test_sharpes) if test_sharpes else 0.0

        return {
            "windows": [w.to_dict() for w in windows],
            "avg_train_sharpe": avg_train,
            "avg_test_sharpe": avg_test,
            "degradation_pct": degradation_pct,
            "consistent": consistency_ratio >= 0.6,
            "consistency_ratio": consistency_ratio,
        }

    def get_stats(self) -> dict:
        return {
            "runs": self._runs,
            "total_windows_processed": self._total_windows_processed,
            "n_windows": self.n_windows,
            "train_pct": self.train_pct,
        }
