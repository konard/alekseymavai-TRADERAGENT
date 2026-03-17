"""Self-Play Simulator — trains experts on synthetic signals with known outcomes."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from bot.agents.base_expert import Verdict
from bot.agents.committee import InvestmentCommittee
from bot.agents.feedback_tracker import ExpertFeedbackTracker
from bot.agents.regime_profile import RegimeProfileStore
from bot.agents.weight_calibrator import WeightCalibrator


# ---------------------------------------------------------------------------
# Default outcome profiles per (regime, direction)
# ---------------------------------------------------------------------------
DEFAULT_OUTCOME_PROFILES: dict[str, dict[str, Any]] = {
    "bull_trend": {
        "LONG": {"win_rate": 0.65, "avg_win": 120.0, "avg_loss": -80.0},
        "SHORT": {"win_rate": 0.30, "avg_win": 90.0, "avg_loss": -110.0},
        "direction_bias": {"LONG": 0.75, "SHORT": 0.25},
    },
    "bear_trend": {
        "LONG": {"win_rate": 0.25, "avg_win": 90.0, "avg_loss": -110.0},
        "SHORT": {"win_rate": 0.60, "avg_win": 110.0, "avg_loss": -85.0},
        "direction_bias": {"LONG": 0.25, "SHORT": 0.75},
    },
    "tight_range": {
        "LONG": {"win_rate": 0.45, "avg_win": 60.0, "avg_loss": -65.0},
        "SHORT": {"win_rate": 0.45, "avg_win": 60.0, "avg_loss": -65.0},
        "direction_bias": {"LONG": 0.50, "SHORT": 0.50},
    },
    "wide_range": {
        "LONG": {"win_rate": 0.45, "avg_win": 80.0, "avg_loss": -75.0},
        "SHORT": {"win_rate": 0.45, "avg_win": 80.0, "avg_loss": -75.0},
        "direction_bias": {"LONG": 0.50, "SHORT": 0.50},
    },
    "quiet_transition": {
        "LONG": {"win_rate": 0.42, "avg_win": 70.0, "avg_loss": -70.0},
        "SHORT": {"win_rate": 0.42, "avg_win": 70.0, "avg_loss": -70.0},
        "direction_bias": {"LONG": 0.50, "SHORT": 0.50},
    },
    "volatile_transition": {
        "LONG": {"win_rate": 0.40, "avg_win": 150.0, "avg_loss": -120.0},
        "SHORT": {"win_rate": 0.40, "avg_win": 150.0, "avg_loss": -120.0},
        "direction_bias": {"LONG": 0.50, "SHORT": 0.50},
    },
    "accumulation": {
        "LONG": {"win_rate": 0.55, "avg_win": 100.0, "avg_loss": -70.0},
        "SHORT": {"win_rate": 0.35, "avg_win": 70.0, "avg_loss": -90.0},
        "direction_bias": {"LONG": 0.70, "SHORT": 0.30},
    },
    "distribution": {
        "LONG": {"win_rate": 0.35, "avg_win": 70.0, "avg_loss": -90.0},
        "SHORT": {"win_rate": 0.55, "avg_win": 100.0, "avg_loss": -70.0},
        "direction_bias": {"LONG": 0.30, "SHORT": 0.70},
    },
}

ALL_REGIMES = list(DEFAULT_OUTCOME_PROFILES.keys())


class SyntheticSignalGenerator:
    """Generates realistic trading signals with controllable outcome probabilities.

    Regime outcome probabilities (default):
    - bull_trend LONG: 65% win, avg_win=+$120, avg_loss=-$80
    - bull_trend SHORT: 30% win
    - bear_trend SHORT: 60% win
    - bear_trend LONG: 25% win
    - tight_range: 45% win either direction
    - volatile_transition: 40% win
    - accumulation LONG: 55% win
    - distribution SHORT: 55% win
    """

    def __init__(
        self,
        outcome_profiles: dict | None = None,
        seed: int | None = None,
    ) -> None:
        self._profiles = outcome_profiles or dict(DEFAULT_OUTCOME_PROFILES)
        self._rng = random.Random(seed)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def generate(
        self,
        n: int,
        regime_mix: dict[str, float] | None = None,
    ) -> list[dict]:
        """Generate N signals with known outcomes.

        Each signal dict has:
        - "signal": trading signal dict
        - "market_data": market context dict
        - "regime": str
        - "true_pnl": float (the actual outcome, hidden from experts)
        - "is_winner": bool
        """
        if regime_mix is None:
            regimes = list(self._profiles.keys())
            regime_mix = {r: 1.0 / len(regimes) for r in regimes}

        # Normalise weights
        total_w = sum(regime_mix.values())
        norm_mix = {r: w / total_w for r, w in regime_mix.items()}

        regime_names = list(norm_mix.keys())
        regime_weights = [norm_mix[r] for r in regime_names]

        signals: list[dict] = []
        for _ in range(n):
            regime = self._rng.choices(regime_names, weights=regime_weights, k=1)[0]
            profile = self._profiles.get(regime, self._profiles["tight_range"])

            # Pick direction
            bias = profile.get("direction_bias", {"LONG": 0.5, "SHORT": 0.5})
            direction = self._rng.choices(
                ["LONG", "SHORT"],
                weights=[bias["LONG"], bias["SHORT"]],
                k=1,
            )[0]

            # Determine outcome
            dir_profile = profile.get(
                direction, {"win_rate": 0.45, "avg_win": 80.0, "avg_loss": -75.0}
            )
            win_rate = dir_profile["win_rate"]
            is_winner = self._rng.random() < win_rate

            if is_winner:
                base = dir_profile["avg_win"]
                # Add noise +/- 40%
                true_pnl = base * (0.6 + self._rng.random() * 0.8)
            else:
                base = dir_profile["avg_loss"]
                true_pnl = base * (0.6 + self._rng.random() * 0.8)

            market_data = self._generate_market_data(regime, direction)
            price = 40000 + self._rng.uniform(-5000, 5000)
            sl_dist = price * self._rng.uniform(0.005, 0.02)
            tp_dist = price * self._rng.uniform(0.01, 0.04)

            if direction == "LONG":
                stop_loss = price - sl_dist
                take_profit = price + tp_dist
            else:
                stop_loss = price + sl_dist
                take_profit = price - tp_dist

            signal = {
                "direction": direction,
                "price": round(price, 2),
                "strategy": self._rng.choice(["smc", "trend_follower", "grid", "dca"]),
                "symbol": self._rng.choice(["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
                "risk_pct": round(self._rng.uniform(1.0, 2.5), 1),
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
            }

            signals.append(
                {
                    "signal": signal,
                    "market_data": market_data,
                    "regime": regime,
                    "true_pnl": round(true_pnl, 2),
                    "is_winner": is_winner,
                }
            )
        return signals

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _generate_market_data(self, regime: str, direction: str) -> dict:
        """Generate realistic market data for the given regime."""
        adx_ranges: dict[str, tuple[float, float]] = {
            "bull_trend": (25.0, 55.0),
            "bear_trend": (25.0, 55.0),
            "tight_range": (10.0, 20.0),
            "wide_range": (15.0, 25.0),
            "quiet_transition": (12.0, 22.0),
            "volatile_transition": (20.0, 40.0),
            "accumulation": (12.0, 25.0),
            "distribution": (12.0, 25.0),
        }

        adx_lo, adx_hi = adx_ranges.get(regime, (15.0, 30.0))
        adx = round(self._rng.uniform(adx_lo, adx_hi), 2)

        # Trend strength correlated with ADX
        trend_strength = round(adx / 60.0 + self._rng.uniform(-0.1, 0.1), 3)
        trend_strength = max(0.0, min(1.0, trend_strength))

        # EMA spread: positive in bull, negative in bear, small in range
        if regime in ("bull_trend", "accumulation"):
            ema_spread = round(self._rng.uniform(0.001, 0.008), 5)
        elif regime in ("bear_trend", "distribution"):
            ema_spread = round(self._rng.uniform(-0.008, -0.001), 5)
        else:
            ema_spread = round(self._rng.uniform(-0.003, 0.003), 5)

        balance = round(self._rng.uniform(5000, 50000), 2)
        drawdown = round(self._rng.uniform(0.0, 0.12), 4)
        daily_loss = round(self._rng.uniform(0.0, balance * 0.03), 2)
        max_daily_loss = round(balance * self._rng.uniform(0.04, 0.06), 2)
        open_positions = self._rng.randint(0, 5)
        max_positions = self._rng.randint(5, 20)
        exposure_pct = round(self._rng.uniform(5, 60), 1)

        volume_choices = ["rising", "falling", "flat"]
        if regime in ("bull_trend", "bear_trend"):
            volume_weights = [0.5, 0.2, 0.3]
        elif regime in ("volatile_transition",):
            volume_weights = [0.4, 0.3, 0.3]
        else:
            volume_weights = [0.25, 0.25, 0.5]
        volume_trend = self._rng.choices(volume_choices, weights=volume_weights, k=1)[0]

        return {
            "regime": regime,
            "adx": adx,
            "trend_strength": trend_strength,
            "ema_spread": ema_spread,
            "balance": balance,
            "drawdown": drawdown,
            "daily_loss": daily_loss,
            "max_daily_loss": max_daily_loss,
            "open_positions": open_positions,
            "max_positions": max_positions,
            "exposure_pct": exposure_pct,
            "volume_trend": volume_trend,
        }


# ---------------------------------------------------------------------------
# SelfPlayResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class SelfPlayResult:
    """Result of a self-play training run."""

    epochs_run: int = 0
    initial_weights: dict[str, float] = field(default_factory=dict)
    final_weights: dict[str, float] = field(default_factory=dict)
    accuracy_per_epoch: list[float] = field(default_factory=list)
    per_expert_accuracy: dict[str, float] = field(default_factory=dict)
    per_regime_accuracy: dict[str, dict[str, float]] = field(default_factory=dict)
    total_signals: int = 0
    approve_rate: float = 0.0
    correct_approvals: int = 0
    correct_rejections: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "epochs_run": self.epochs_run,
            "initial_weights": dict(self.initial_weights),
            "final_weights": dict(self.final_weights),
            "accuracy_per_epoch": list(self.accuracy_per_epoch),
            "per_expert_accuracy": dict(self.per_expert_accuracy),
            "per_regime_accuracy": {
                regime: dict(experts) for regime, experts in self.per_regime_accuracy.items()
            },
            "total_signals": self.total_signals,
            "approve_rate": round(self.approve_rate, 4),
            "correct_approvals": self.correct_approvals,
            "correct_rejections": self.correct_rejections,
        }


# ---------------------------------------------------------------------------
# SelfPlaySimulator
# ---------------------------------------------------------------------------


class SelfPlaySimulator:
    """Runs Committee through synthetic signals for training.

    Training loop per epoch:
    1. Generate N synthetic signals
    2. Run each through Committee
    3. Score each expert's vote against known outcome (true_pnl)
    4. Update FeedbackTracker + RegimeProfileStore
    5. Recalibrate weights via WeightCalibrator
    6. Record accuracy metrics
    """

    def __init__(
        self,
        committee: InvestmentCommittee | None = None,
        feedback_tracker: ExpertFeedbackTracker | None = None,
        regime_store: RegimeProfileStore | None = None,
        calibrator: WeightCalibrator | None = None,
        signal_generator: SyntheticSignalGenerator | None = None,
    ) -> None:
        if committee is None:
            committee, feedback_tracker, regime_store, calibrator = self._create_defaults()
        self._committee = committee
        self._tracker = feedback_tracker or ExpertFeedbackTracker()
        self._regime_store = regime_store or RegimeProfileStore()
        self._calibrator = calibrator or WeightCalibrator(min_samples=5)
        self._generator = signal_generator or SyntheticSignalGenerator()
        self._total_signals = 0
        self._total_approved = 0
        self._correct_approvals = 0
        self._correct_rejections = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        epochs: int = 5,
        signals_per_epoch: int = 50,
        regime_mix: dict[str, float] | None = None,
        progress_callback: Callable | None = None,
    ) -> SelfPlayResult:
        """Run self-play training loop."""
        initial_weights = {e.name: e.weight for e in self._committee.experts}
        accuracy_per_epoch: list[float] = []

        for epoch in range(epochs):
            signals = self._generator.generate(signals_per_epoch, regime_mix)
            epoch_correct = 0
            epoch_total = 0

            for i, sig in enumerate(signals):
                decision = await self._committee.evaluate(sig["signal"], sig["market_data"])
                position_id = f"sp_{epoch}_{i}"
                regime = sig["regime"]
                true_pnl = sig["true_pnl"]

                # Record decision and outcome in tracker
                self._tracker.record_decision(decision, position_id=position_id, regime=regime)
                self._tracker.record_outcome(position_id, true_pnl)

                # Update regime profiles for each vote
                for vote in decision.votes:
                    if vote.verdict == Verdict.DEFER:
                        continue
                    was_correct = (vote.verdict == Verdict.APPROVE and true_pnl > 0) or (
                        vote.verdict == Verdict.REJECT and true_pnl <= 0
                    )
                    self._regime_store.record_outcome(
                        vote.expert_name,
                        regime,
                        vote.verdict,
                        was_correct,
                        true_pnl,
                    )

                # Track committee-level accuracy
                committee_approved = decision.verdict == Verdict.APPROVE
                self._total_signals += 1

                if committee_approved:
                    self._total_approved += 1
                    if true_pnl > 0:
                        self._correct_approvals += 1
                        epoch_correct += 1
                    epoch_total += 1
                else:
                    # Rejected or deferred
                    if true_pnl <= 0:
                        self._correct_rejections += 1
                        epoch_correct += 1
                    epoch_total += 1

            # End of epoch: calibrate weights
            self._calibrator.calibrate(
                self._committee, self._tracker, self._regime_store, current_regime=None
            )

            # Record epoch accuracy
            epoch_acc = epoch_correct / epoch_total if epoch_total > 0 else 0.0
            accuracy_per_epoch.append(round(epoch_acc, 4))

            if progress_callback is not None:
                progress_callback(epoch + 1, epochs, epoch_acc)

        # Build per-expert accuracy
        per_expert_accuracy: dict[str, float] = {}
        for sc in self._tracker.get_all_scorecards():
            per_expert_accuracy[sc.expert_name] = sc.accuracy

        # Build per-regime accuracy
        per_regime_accuracy: dict[str, dict[str, float]] = {}
        for expert_name, profile in self._regime_store.get_all_profiles().items():
            for regime_name, ra in profile.get_all_regimes().items():
                if regime_name not in per_regime_accuracy:
                    per_regime_accuracy[regime_name] = {}
                per_regime_accuracy[regime_name][expert_name] = ra.accuracy

        final_weights = {e.name: e.weight for e in self._committee.experts}
        approve_rate = (
            self._total_approved / self._total_signals if self._total_signals > 0 else 0.0
        )

        return SelfPlayResult(
            epochs_run=epochs,
            initial_weights=initial_weights,
            final_weights=final_weights,
            accuracy_per_epoch=accuracy_per_epoch,
            per_expert_accuracy=per_expert_accuracy,
            per_regime_accuracy=per_regime_accuracy,
            total_signals=self._total_signals,
            approve_rate=approve_rate,
            correct_approvals=self._correct_approvals,
            correct_rejections=self._correct_rejections,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _create_defaults() -> tuple[
        InvestmentCommittee,
        ExpertFeedbackTracker,
        RegimeProfileStore,
        WeightCalibrator,
    ]:
        """Create committee with 4 experts + tracker + store + calibrator."""
        from bot.agents.smc_expert import SMCExpert
        from bot.agents.trend_expert import TrendExpert
        from bot.agents.risk_expert import RiskExpert
        from bot.agents.contrarian_expert import ContrarianExpert

        committee = InvestmentCommittee()
        committee.add_expert(SMCExpert())
        committee.add_expert(TrendExpert())
        committee.add_expert(RiskExpert())
        committee.add_expert(ContrarianExpert())

        tracker = ExpertFeedbackTracker()
        regime_store = RegimeProfileStore()
        calibrator = WeightCalibrator(min_samples=5)

        return committee, tracker, regime_store, calibrator

    def get_stats(self) -> dict:
        """Return current simulator statistics."""
        return {
            "total_signals": self._total_signals,
            "total_approved": self._total_approved,
            "correct_approvals": self._correct_approvals,
            "correct_rejections": self._correct_rejections,
            "approve_rate": (
                round(self._total_approved / self._total_signals, 4)
                if self._total_signals > 0
                else 0.0
            ),
            "tracker_stats": self._tracker.get_stats(),
            "regime_stats": self._regime_store.get_stats(),
            "calibrator_stats": self._calibrator.get_stats(),
        }
