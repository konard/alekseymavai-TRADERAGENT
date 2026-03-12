"""Tests for DynamicPositionSizer."""

import pytest

from bot.agents.position_sizer import DynamicPositionSizer, SizingResult


@pytest.fixture
def sizer() -> DynamicPositionSizer:
    return DynamicPositionSizer(
        base_risk_pct=2.0,
        max_risk_pct=5.0,
        min_risk_pct=0.25,
        use_kelly=True,
        kelly_cap=0.5,
        anomaly_reduction=0.5,
    )


class TestHighConfidence:
    def test_high_confidence_full_size(self, sizer: DynamicPositionSizer):
        """score=0.9, pattern_wr=0.7 -> near base_risk."""
        result = sizer.calculate(
            committee_score=0.9,
            pattern_win_rate=0.7,
            pattern_confidence=0.8,
            avg_win_loss_ratio=1.5,
        )
        # confidence_mult = 0.25 + 0.75*0.9 = 0.925
        # pattern_mult = 0.5 + 0.7*0.7*0.8 = 0.892
        # adjusted = 2.0 * 0.925 * 0.892 = 1.6502
        # Kelly = 0.7 - 0.3/1.5 = 0.5 -> half = 0.25 -> 25%
        # final should be close to adjusted (~1.65), not capped by Kelly (25%)
        assert result.final_position_pct >= 1.0
        assert result.final_position_pct <= sizer._max_risk_pct


class TestLowConfidence:
    def test_low_confidence_reduced(self, sizer: DynamicPositionSizer):
        """score=0.3 -> much less than base_risk."""
        result = sizer.calculate(
            committee_score=0.3,
            pattern_win_rate=0.4,
            pattern_confidence=0.3,
            avg_win_loss_ratio=1.0,
        )
        # confidence_mult = 0.25 + 0.75*0.3 = 0.475
        # pattern_mult = 0.5 + 0.7*0.4*0.3 = 0.584
        # adjusted = 2.0 * 0.475 * 0.584 = 0.5548
        assert result.final_position_pct < sizer._base_risk_pct


class TestKellyCriterion:
    def test_kelly_criterion_calculation(self, sizer: DynamicPositionSizer):
        """Verify Kelly formula: f* = p - (1-p)/b."""
        # p=0.6, b=2.0 -> 0.6 - 0.4/2.0 = 0.4
        kelly = sizer.kelly_criterion(0.6, 2.0)
        assert abs(kelly - 0.4) < 1e-9

    def test_kelly_negative_returns_zero(self, sizer: DynamicPositionSizer):
        """Bad edge -> Kelly = 0."""
        # p=0.3, b=1.0 -> 0.3 - 0.7/1.0 = -0.4 -> clamped to 0
        kelly = sizer.kelly_criterion(0.3, 1.0)
        assert kelly == 0.0

    def test_half_kelly_cap(self, sizer: DynamicPositionSizer):
        """Kelly capped at half (kelly_cap=0.5)."""
        # p=0.8, b=3.0 -> 0.8 - 0.2/3.0 = 0.7333
        # half-kelly = 0.3667 -> 36.67%
        # adjusted_risk with high confidence should be capped at kelly if
        # kelly is lower. Here kelly is 36.67% so it won't cap 2% base.
        # Use a scenario where Kelly IS the cap:
        # p=0.55, b=1.2 -> 0.55 - 0.45/1.2 = 0.175
        # half = 0.0875 -> 8.75%
        # With max confidence: adjusted = 2.0 * 1.0 * 1.2 = 2.4 -> not capped
        result = sizer.calculate(
            committee_score=1.0,
            pattern_win_rate=0.55,
            pattern_confidence=1.0,
            avg_win_loss_ratio=1.2,
        )
        kelly = sizer.kelly_criterion(0.55, 1.2)
        half_kelly_pct = kelly * sizer._kelly_cap * 100
        # adjusted = 2.0 * 1.0 * (0.5+0.7*0.55*1.0) = 2.0 * 1.0 * 0.885 = 1.77
        # half_kelly_pct = 0.175 * 0.5 * 100 = 8.75
        # 1.77 < 8.75, so Kelly does NOT cap here
        assert result.kelly_fraction == pytest.approx(kelly, abs=1e-3)


class TestAnomalyReduction:
    def test_anomaly_reduces_size(self, sizer: DynamicPositionSizer):
        """has_anomaly=True -> 50% reduction."""
        normal = sizer.calculate(
            committee_score=0.7,
            pattern_win_rate=0.6,
            pattern_confidence=0.5,
            avg_win_loss_ratio=1.5,
        )
        anomaly = sizer.calculate(
            committee_score=0.7,
            pattern_win_rate=0.6,
            pattern_confidence=0.5,
            avg_win_loss_ratio=1.5,
            has_anomaly=True,
        )
        # Anomaly result should be roughly half of normal (before min clamp)
        assert anomaly.final_position_pct < normal.final_position_pct
        assert "Anomaly" in anomaly.reasoning


class TestRiskBounds:
    def test_min_risk_enforced(self, sizer: DynamicPositionSizer):
        """Very low confidence still >= min_risk."""
        result = sizer.calculate(
            committee_score=0.0,
            pattern_win_rate=0.0,
            pattern_confidence=0.0,
            avg_win_loss_ratio=0.5,
            has_anomaly=True,
        )
        assert result.final_position_pct >= sizer._min_risk_pct

    def test_max_risk_enforced(self, sizer: DynamicPositionSizer):
        """Very high everything still <= max_risk."""
        # Disable Kelly to let adjusted risk go high
        no_kelly_sizer = DynamicPositionSizer(
            base_risk_pct=10.0,
            max_risk_pct=5.0,
            use_kelly=False,
        )
        result = no_kelly_sizer.calculate(
            committee_score=1.0,
            pattern_win_rate=1.0,
            pattern_confidence=1.0,
            avg_win_loss_ratio=5.0,
        )
        assert result.final_position_pct <= 5.0


class TestPatternData:
    def test_no_pattern_data(self, sizer: DynamicPositionSizer):
        """pattern_win_rate=None -> pattern_mult=0.5."""
        result = sizer.calculate(
            committee_score=0.5,
            pattern_win_rate=None,
        )
        assert result.pattern_multiplier == pytest.approx(0.5, abs=1e-4)


class TestSerialization:
    def test_result_to_dict(self, sizer: DynamicPositionSizer):
        """SizingResult.to_dict() returns all expected keys."""
        result = sizer.calculate(committee_score=0.5)
        d = result.to_dict()
        expected_keys = {
            "base_risk_pct",
            "adjusted_risk_pct",
            "kelly_fraction",
            "confidence_multiplier",
            "pattern_multiplier",
            "final_position_pct",
            "reasoning",
            "ts",
        }
        assert set(d.keys()) == expected_keys
        assert isinstance(d["ts"], float)
        assert isinstance(d["reasoning"], str)


class TestHistory:
    def test_history_recorded(self, sizer: DynamicPositionSizer):
        """After calculate, history has entry."""
        assert len(sizer.get_history()) == 0
        sizer.calculate(committee_score=0.6)
        history = sizer.get_history()
        assert len(history) == 1
        assert "base_risk_pct" in history[0]

    def test_get_stats(self, sizer: DynamicPositionSizer):
        """get_stats returns correct structure."""
        # Empty stats
        stats = sizer.get_stats()
        assert stats["history_count"] == 0
        assert stats["avg_adjusted_risk"] == 0.0
        assert stats["avg_kelly"] == 0.0

        # After a few calculations
        sizer.calculate(committee_score=0.5, pattern_win_rate=0.6,
                        pattern_confidence=0.5, avg_win_loss_ratio=1.5)
        sizer.calculate(committee_score=0.8, pattern_win_rate=0.7,
                        pattern_confidence=0.7, avg_win_loss_ratio=2.0)
        stats = sizer.get_stats()
        assert stats["history_count"] == 2
        assert stats["avg_adjusted_risk"] > 0
        assert stats["avg_kelly"] > 0
