"""Tests for Self-Play Simulator."""

import pytest

from bot.agents.self_play import SyntheticSignalGenerator, SelfPlaySimulator, SelfPlayResult
from bot.agents.committee import InvestmentCommittee
from bot.agents.smc_expert import SMCExpert
from bot.agents.trend_expert import TrendExpert
from bot.agents.risk_expert import RiskExpert
from bot.agents.contrarian_expert import ContrarianExpert
from bot.agents.feedback_tracker import ExpertFeedbackTracker
from bot.agents.regime_profile import RegimeProfileStore
from bot.agents.weight_calibrator import WeightCalibrator


# ---------------------------------------------------------------------------
# SyntheticSignalGenerator tests
# ---------------------------------------------------------------------------

REQUIRED_SIGNAL_KEYS = {"direction", "price", "strategy", "symbol", "risk_pct", "stop_loss", "take_profit"}
REQUIRED_MARKET_KEYS = {"regime", "adx", "trend_strength", "ema_spread", "balance", "drawdown",
                        "daily_loss", "max_daily_loss", "open_positions", "max_positions",
                        "exposure_pct", "volume_trend"}
REQUIRED_TOP_KEYS = {"signal", "market_data", "regime", "true_pnl", "is_winner"}


def test_synthetic_generator_produces_valid_signals():
    """Generate 10 signals and verify all have required keys."""
    gen = SyntheticSignalGenerator(seed=42)
    signals = gen.generate(10)

    assert len(signals) == 10
    for sig in signals:
        assert REQUIRED_TOP_KEYS.issubset(sig.keys()), f"Missing top-level keys: {REQUIRED_TOP_KEYS - sig.keys()}"
        assert REQUIRED_SIGNAL_KEYS.issubset(sig["signal"].keys()), f"Missing signal keys: {REQUIRED_SIGNAL_KEYS - sig['signal'].keys()}"
        assert REQUIRED_MARKET_KEYS.issubset(sig["market_data"].keys()), f"Missing market_data keys: {REQUIRED_MARKET_KEYS - sig['market_data'].keys()}"
        assert isinstance(sig["true_pnl"], float)
        assert isinstance(sig["is_winner"], bool)
        assert sig["signal"]["direction"] in ("LONG", "SHORT")


def test_synthetic_signals_cover_regimes():
    """Generate 100 signals with default mix; all 8 regimes should appear."""
    gen = SyntheticSignalGenerator(seed=42)
    signals = gen.generate(100)

    regimes_seen = {s["regime"] for s in signals}
    expected = {
        "bull_trend", "bear_trend", "tight_range", "wide_range",
        "quiet_transition", "volatile_transition", "accumulation", "distribution",
    }
    assert expected == regimes_seen, f"Missing regimes: {expected - regimes_seen}"


def test_synthetic_signals_reproducible():
    """Same seed must produce identical signals."""
    gen1 = SyntheticSignalGenerator(seed=42)
    gen2 = SyntheticSignalGenerator(seed=42)
    signals1 = gen1.generate(20)
    signals2 = gen2.generate(20)

    for a, b in zip(signals1, signals2):
        assert a["regime"] == b["regime"]
        assert a["true_pnl"] == b["true_pnl"]
        assert a["is_winner"] == b["is_winner"]
        assert a["signal"]["direction"] == b["signal"]["direction"]
        assert a["signal"]["price"] == b["signal"]["price"]


def test_synthetic_win_rates_approximate():
    """Bull_trend LONG should win ~65% of the time (within 20% tolerance)."""
    gen = SyntheticSignalGenerator(seed=42)
    signals = gen.generate(500, regime_mix={"bull_trend": 1.0})

    longs = [s for s in signals if s["signal"]["direction"] == "LONG"]
    assert len(longs) > 50, "Should have enough LONG samples in bull_trend"

    win_count = sum(1 for s in longs if s["is_winner"])
    actual_rate = win_count / len(longs)
    # 65% target, allow 20% absolute tolerance -> [45%, 85%]
    assert 0.45 <= actual_rate <= 0.85, (
        f"Bull_trend LONG win rate {actual_rate:.2%} outside [45%, 85%] tolerance"
    )


# ---------------------------------------------------------------------------
# SelfPlaySimulator tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_self_play_runs_single_epoch():
    """Run 1 epoch with 20 signals; result should be valid."""
    gen = SyntheticSignalGenerator(seed=42)
    sim = SelfPlaySimulator(signal_generator=gen)
    result = await sim.run(epochs=1, signals_per_epoch=20)

    assert isinstance(result, SelfPlayResult)
    assert result.epochs_run == 1
    assert result.total_signals == 20
    assert len(result.accuracy_per_epoch) == 1
    assert 0.0 <= result.accuracy_per_epoch[0] <= 1.0
    assert result.approve_rate >= 0.0


@pytest.mark.asyncio
async def test_self_play_weights_change():
    """After 3 epochs with 30 signals each, weights should differ from initial."""
    gen = SyntheticSignalGenerator(seed=42)
    sim = SelfPlaySimulator(signal_generator=gen)
    result = await sim.run(epochs=3, signals_per_epoch=30)

    assert result.initial_weights != result.final_weights, (
        "Weights should change after training"
    )


@pytest.mark.asyncio
async def test_self_play_result_to_dict():
    """SelfPlayResult.to_dict() should contain all expected keys."""
    gen = SyntheticSignalGenerator(seed=42)
    sim = SelfPlaySimulator(signal_generator=gen)
    result = await sim.run(epochs=1, signals_per_epoch=10)
    d = result.to_dict()

    expected_keys = {
        "epochs_run", "initial_weights", "final_weights",
        "accuracy_per_epoch", "per_expert_accuracy", "per_regime_accuracy",
        "total_signals", "approve_rate", "correct_approvals", "correct_rejections",
    }
    assert expected_keys.issubset(d.keys()), f"Missing keys: {expected_keys - d.keys()}"


@pytest.mark.asyncio
async def test_self_play_accuracy_tracked():
    """accuracy_per_epoch should have exactly `epochs` entries."""
    gen = SyntheticSignalGenerator(seed=42)
    sim = SelfPlaySimulator(signal_generator=gen)
    result = await sim.run(epochs=4, signals_per_epoch=15)

    assert len(result.accuracy_per_epoch) == 4
    for acc in result.accuracy_per_epoch:
        assert 0.0 <= acc <= 1.0


@pytest.mark.asyncio
async def test_self_play_regime_profiles_populated():
    """per_regime_accuracy should have data after training."""
    gen = SyntheticSignalGenerator(seed=42)
    sim = SelfPlaySimulator(signal_generator=gen)
    result = await sim.run(epochs=2, signals_per_epoch=40)

    assert len(result.per_regime_accuracy) > 0, "Regime accuracy should be populated"
    # At least one regime should have expert data
    for regime, experts in result.per_regime_accuracy.items():
        assert isinstance(experts, dict)
        if experts:
            for expert_name, acc in experts.items():
                assert 0.0 <= acc <= 1.0


@pytest.mark.asyncio
async def test_self_play_progress_callback():
    """Progress callback should be called exactly `epochs` times."""
    gen = SyntheticSignalGenerator(seed=42)
    sim = SelfPlaySimulator(signal_generator=gen)

    calls: list[tuple[int, int, float]] = []

    def on_progress(epoch: int, total: int, accuracy: float) -> None:
        calls.append((epoch, total, accuracy))

    result = await sim.run(epochs=3, signals_per_epoch=10, progress_callback=on_progress)

    assert len(calls) == 3
    assert calls[0][0] == 1  # first epoch
    assert calls[-1][0] == 3  # last epoch
    assert all(t == 3 for _, t, _ in calls)  # total is always 3
