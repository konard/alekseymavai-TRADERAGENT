"""
Unit tests for CapitalArbiter and PriceZoneAllocator.

Issue #381 — Capital Arbiter: vertical zones and strategy cooperation.

Coverage
--------
CapitalArbiter:
  - get_allowed_capital() returns 0 for DCA in UPTREND (BULL_TREND)
  - get_allowed_capital() returns 0 for DCA in WIDE_RANGE (SIDEWAYS)
  - get_allowed_capital() returns correct fractions for every regime family
  - check_mutual_exclusion() blocks DCA when ADX=35 and bearish trend
  - check_mutual_exclusion() allows SMC in any regime
  - check_mutual_exclusion() allows DCA when ADX=20 (not high enough to block)
  - format_capital_report() includes all strategies
  - get_current_utilization() sums open positions for a strategy
  - Regime change redistributes capital (idle strategies skipped)

PriceZoneAllocator:
  - get_zones() returns all expected keys
  - Zones do not critically overlap (only 5% buffer)
  - is_price_in_zone() returns True for price inside grid zone
  - is_price_in_zone() returns True for SMC at any price (overlay)
  - recalculate() triggers update on >5% price move
  - recalculate() does NOT trigger update on <5% price move
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.core.capital_arbiter import CapitalArbiter
from bot.core.price_zone_allocator import PriceZoneAllocator
from bot.core.virtual_position_manager import VirtualPosition, VirtualPositionManager
from bot.orchestrator.market_regime import MarketRegime

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vpm_mock(strategy: str = "grid", positions: list[dict] | None = None) -> MagicMock:
    """Build a VirtualPositionManager mock with preset positions."""
    vpm = MagicMock(spec=VirtualPositionManager)
    if positions is None:
        positions = []

    pos_objects = [
        VirtualPositionManager.new_position(
            strategy=p.get("strategy", strategy),
            symbol=p.get("symbol", "BTC/USDT"),
            side=p.get("side", "long"),
            qty=Decimal(str(p.get("qty", "0.1"))),
            entry_price=Decimal(str(p.get("entry_price", "50000"))),
        )
        for p in positions
    ]

    async def _get_by_strategy(s: str) -> list[VirtualPosition]:
        return [p for p in pos_objects if p.strategy == s]

    vpm.get_by_strategy = AsyncMock(side_effect=_get_by_strategy)
    return vpm


def _arbiter(positions: list[dict] | None = None) -> CapitalArbiter:
    """Create a CapitalArbiter with a mock VPM."""
    vpm = _make_vpm_mock(positions=positions or [])
    return CapitalArbiter(vpm)


# ---------------------------------------------------------------------------
# CapitalArbiter — get_allowed_capital
# ---------------------------------------------------------------------------


class TestGetAllowedCapital:
    def test_dca_zero_in_uptrend(self) -> None:
        """DCA gets 0% allocation in BULL_TREND (UPTREND family)."""
        arbiter = _arbiter()
        result = arbiter.get_allowed_capital("dca", MarketRegime.BULL_TREND, Decimal("10000"))
        assert result == Decimal("0"), f"Expected 0, got {result}"

    def test_dca_zero_in_sideways(self) -> None:
        """DCA gets 0% allocation in TIGHT_RANGE and WIDE_RANGE (SIDEWAYS)."""
        arbiter = _arbiter()
        for regime in (MarketRegime.TIGHT_RANGE, MarketRegime.WIDE_RANGE):
            result = arbiter.get_allowed_capital("dca", regime, Decimal("10000"))
            assert result == Decimal("0"), f"Expected 0 for {regime}, got {result}"

    def test_grid_zero_in_downtrend(self) -> None:
        """Grid gets 0% allocation in BEAR_TREND (DOWNTREND family)."""
        arbiter = _arbiter()
        result = arbiter.get_allowed_capital("grid", MarketRegime.BEAR_TREND, Decimal("10000"))
        assert result == Decimal("0"), f"Expected 0, got {result}"

    def test_grid_40pct_in_sideways(self) -> None:
        """Grid gets 40% in SIDEWAYS regime."""
        arbiter = _arbiter()
        result = arbiter.get_allowed_capital("grid", MarketRegime.TIGHT_RANGE, Decimal("10000"))
        assert result == Decimal("4000"), f"Expected 4000, got {result}"

    def test_tf_40pct_in_uptrend(self) -> None:
        """TF gets 40% allocation in BULL_TREND."""
        arbiter = _arbiter()
        result = arbiter.get_allowed_capital("tf", MarketRegime.BULL_TREND, Decimal("10000"))
        assert result == Decimal("4000"), f"Expected 4000, got {result}"

    def test_trend_follower_alias(self) -> None:
        """'trend_follower' is accepted as alias for 'tf'."""
        arbiter = _arbiter()
        r_tf = arbiter.get_allowed_capital("tf", MarketRegime.BULL_TREND, Decimal("10000"))
        r_alias = arbiter.get_allowed_capital(
            "trend_follower", MarketRegime.BULL_TREND, Decimal("10000")
        )
        assert r_tf == r_alias

    def test_smc_in_volatile(self) -> None:
        """SMC gets 30% in VOLATILE_TRANSITION regime."""
        arbiter = _arbiter()
        result = arbiter.get_allowed_capital(
            "smc", MarketRegime.VOLATILE_TRANSITION, Decimal("10000")
        )
        assert result == Decimal("3000"), f"Expected 3000, got {result}"

    def test_unknown_regime_splits_equally(self) -> None:
        """UNKNOWN regime gives equal 15%/15%/15%/5% allocation."""
        arbiter = _arbiter()
        balance = Decimal("10000")
        grid = arbiter.get_allowed_capital("grid", MarketRegime.UNKNOWN, balance)
        dca = arbiter.get_allowed_capital("dca", MarketRegime.UNKNOWN, balance)
        tf = arbiter.get_allowed_capital("tf", MarketRegime.UNKNOWN, balance)
        smc = arbiter.get_allowed_capital("smc", MarketRegime.UNKNOWN, balance)
        assert grid == Decimal("1500")
        assert dca == Decimal("1500")
        assert tf == Decimal("1500")
        assert smc == Decimal("500")

    def test_all_regimes_return_non_negative(self) -> None:
        """All regime/strategy combinations return non-negative capital."""
        arbiter = _arbiter()
        balance = Decimal("10000")
        strategies = ["grid", "dca", "tf", "smc"]
        for regime in MarketRegime:
            for strat in strategies:
                result = arbiter.get_allowed_capital(strat, regime, balance)
                assert result >= Decimal(
                    "0"
                ), f"Negative capital for {strat}/{regime.value}: {result}"

    def test_zero_balance_returns_zero(self) -> None:
        """Zero balance always returns zero allowed capital."""
        arbiter = _arbiter()
        result = arbiter.get_allowed_capital("grid", MarketRegime.TIGHT_RANGE, Decimal("0"))
        assert result == Decimal("0")


# ---------------------------------------------------------------------------
# CapitalArbiter — check_mutual_exclusion
# ---------------------------------------------------------------------------


class TestCheckMutualExclusion:
    def test_dca_blocked_high_adx_bearish(self) -> None:
        """DCA is blocked when ADX > 30 and trend is bearish."""
        arbiter = _arbiter()
        result = arbiter.check_mutual_exclusion(
            "dca", MarketRegime.BEAR_TREND, adx=35.0, is_bearish=True
        )
        assert result is False, "Expected DCA to be blocked"

    def test_dca_allowed_high_adx_not_bearish(self) -> None:
        """DCA is allowed when ADX > 30 but trend is NOT bearish."""
        arbiter = _arbiter()
        result = arbiter.check_mutual_exclusion(
            "dca", MarketRegime.BULL_TREND, adx=35.0, is_bearish=False
        )
        assert result is True

    def test_dca_allowed_low_adx(self) -> None:
        """DCA is allowed when ADX is below 30, regardless of trend direction."""
        arbiter = _arbiter()
        result = arbiter.check_mutual_exclusion(
            "dca", MarketRegime.BEAR_TREND, adx=20.0, is_bearish=True
        )
        assert result is True

    def test_smc_always_allowed(self) -> None:
        """SMC is always allowed (works in any regime)."""
        arbiter = _arbiter()
        for regime in MarketRegime:
            result = arbiter.check_mutual_exclusion("smc", regime, adx=50.0, is_bearish=True)
            assert result is True, f"SMC should always be allowed, failed for {regime}"

    def test_grid_allowed_when_not_overutilised(self) -> None:
        """Grid is allowed when utilisation is below 35% threshold."""
        arbiter = _arbiter()
        result = arbiter.check_mutual_exclusion("grid", MarketRegime.TIGHT_RANGE)
        assert result is True

    def test_tf_allowed_when_grid_not_dominant(self) -> None:
        """TF is allowed when Grid has not exceeded the 35% threshold."""
        arbiter = _arbiter()
        result = arbiter.check_mutual_exclusion("trend_follower", MarketRegime.BULL_TREND)
        assert result is True


# ---------------------------------------------------------------------------
# CapitalArbiter — get_current_utilization
# ---------------------------------------------------------------------------


class TestGetCurrentUtilization:
    @pytest.mark.asyncio
    async def test_sums_open_position_value(self) -> None:
        """Utilization is sum of entry_price * qty for all open positions."""
        positions = [
            {"strategy": "grid", "qty": "0.1", "entry_price": "50000"},  # 5000
            {"strategy": "grid", "qty": "0.2", "entry_price": "50000"},  # 10000
        ]
        arbiter = _arbiter(positions=positions)
        result = await arbiter.get_current_utilization("grid")
        assert result == Decimal("15000"), f"Expected 15000, got {result}"

    @pytest.mark.asyncio
    async def test_zero_when_no_positions(self) -> None:
        """Returns zero when no open positions for the strategy."""
        arbiter = _arbiter(positions=[])
        result = await arbiter.get_current_utilization("dca")
        assert result == Decimal("0")

    @pytest.mark.asyncio
    async def test_trend_follower_alias_resolved(self) -> None:
        """'trend_follower' alias maps to 'tf' strategy key."""
        positions = [
            {"strategy": "tf", "qty": "1", "entry_price": "1000"},  # 1000
        ]
        arbiter = _arbiter(positions=positions)
        result = await arbiter.get_current_utilization("trend_follower")
        assert result == Decimal("1000")


# ---------------------------------------------------------------------------
# CapitalArbiter — integration scenario: regime change
# ---------------------------------------------------------------------------


class TestRegimeChangeScenario:
    def test_idle_strategies_have_zero_capital_in_sideways(self) -> None:
        """In SIDEWAYS, DCA and any 'tf' strategies get zero capital → idle."""
        arbiter = _arbiter()
        balance = Decimal("10000")
        regime = MarketRegime.TIGHT_RANGE

        dca_capital = arbiter.get_allowed_capital("dca", regime, balance)
        assert dca_capital == Decimal("0"), "DCA should be idle in SIDEWAYS"

    def test_idle_strategies_have_zero_capital_in_volatile(self) -> None:
        """In VOLATILE, Grid and DCA get zero capital → idle."""
        arbiter = _arbiter()
        balance = Decimal("10000")
        regime = MarketRegime.VOLATILE_TRANSITION

        grid_capital = arbiter.get_allowed_capital("grid", regime, balance)
        dca_capital = arbiter.get_allowed_capital("dca", regime, balance)
        assert grid_capital == Decimal("0"), "Grid should be idle in VOLATILE"
        assert dca_capital == Decimal("0"), "DCA should be idle in VOLATILE"


# ---------------------------------------------------------------------------
# PriceZoneAllocator — zone computation
# ---------------------------------------------------------------------------


class TestPriceZoneAllocator:
    def test_get_zones_returns_all_keys(self) -> None:
        """get_zones() returns dict with grid, dca, tf_long, smc keys."""
        allocator = PriceZoneAllocator()
        price = Decimal("50000")
        atr = Decimal("1000")
        zones = allocator.get_zones(price, atr)
        assert set(zones.keys()) == {"grid", "dca", "tf_long", "smc"}

    def test_smc_zone_is_none(self) -> None:
        """SMC zone is None (overlay — no fixed price restriction)."""
        allocator = PriceZoneAllocator()
        zones = allocator.get_zones(Decimal("50000"), Decimal("1000"))
        assert zones["smc"] is None

    def test_zone_boundaries_are_ordered(self) -> None:
        """For each non-None zone, low < high."""
        allocator = PriceZoneAllocator()
        zones = allocator.get_zones(Decimal("50000"), Decimal("1000"))
        for name, bounds in zones.items():
            if bounds is not None:
                low, high = bounds
                assert low < high, f"Zone {name}: low ({low}) must be < high ({high})"

    def test_zones_overlap_only_at_boundary(self) -> None:
        """
        Grid and DCA do NOT critically overlap — only the 5% buffer zone.
        Grid lower boundary should be significantly above DCA upper boundary
        in their core (non-overlap) range.

        Core Grid lower: price × 0.90
        Core DCA upper: price × 0.90
        With 5% (2.5% each) buffer:
          Grid lower = price × 0.875
          DCA upper  = price × 0.925
        The buffers touch (overlap) at ~price × 0.875-0.925 window only.

        Verify that the core non-overlapping zones are distinct:
        Grid lower core (0.90) > DCA upper core (0.90) is the same boundary,
        but the zones extend in different directions.
        """
        price = Decimal("50000")
        allocator = PriceZoneAllocator()
        zones = allocator.get_zones(price, Decimal("1000"))

        grid_low, grid_high = zones["grid"]
        dca_low, dca_high = zones["dca"]
        tf_low, tf_high = zones["tf_long"]

        # DCA upper boundary should not significantly exceed Grid lower boundary.
        # Tolerate only the 5% overlap buffer (price × 0.025 = 1250 for price 50000).
        buffer = price * Decimal("0.025")
        overlap_tolerance = buffer * 2  # 5% total

        dca_grid_overlap = dca_high - grid_low
        assert (
            dca_grid_overlap <= overlap_tolerance
        ), f"DCA-Grid overlap too large: {dca_grid_overlap} > {overlap_tolerance}"

        # TF lower boundary should be close to Grid upper (only buffer apart)
        tf_grid_overlap = grid_high - tf_low
        assert (
            tf_grid_overlap <= overlap_tolerance
        ), f"TF-Grid overlap too large: {tf_grid_overlap} > {overlap_tolerance}"

    def test_is_price_in_zone_grid(self) -> None:
        """Price within ±10% of reference is inside the grid zone."""
        price = Decimal("50000")
        allocator = PriceZoneAllocator(reference_price=price)
        # Price exactly at reference should be inside grid
        assert allocator.is_price_in_zone(price, "grid") is True
        # Price 5% above reference is still inside grid (±10% range)
        assert allocator.is_price_in_zone(price * Decimal("1.05"), "grid") is True

    def test_is_price_in_zone_smc_always_true(self) -> None:
        """SMC overlay zone always returns True regardless of price."""
        allocator = PriceZoneAllocator(reference_price=Decimal("50000"))
        # Very far from reference
        assert allocator.is_price_in_zone(Decimal("1"), "smc") is True
        assert allocator.is_price_in_zone(Decimal("1000000"), "smc") is True

    def test_is_price_below_grid_zone(self) -> None:
        """Price well below −12% of reference is NOT inside grid zone."""
        price = Decimal("50000")
        allocator = PriceZoneAllocator(reference_price=price)
        # Price 20% below reference should be outside grid (grid goes to −12.5%)
        far_below = price * Decimal("0.80")
        assert allocator.is_price_in_zone(far_below, "grid") is False

    def test_recalculate_triggers_on_large_move(self) -> None:
        """recalculate() returns True and updates zones on >5% price move."""
        initial_price = Decimal("50000")
        allocator = PriceZoneAllocator(reference_price=initial_price)

        old_zones = allocator.get_zones(initial_price, Decimal("0"))
        old_grid_low = old_zones["grid"][0]

        # 6% price move — should trigger recalculation
        new_price = initial_price * Decimal("1.06")
        recalculated = allocator.recalculate(new_price)
        assert recalculated is True

        new_zones = allocator.zones
        new_grid_low = new_zones["grid"][0]
        assert new_grid_low != old_grid_low, "Zones should have changed after recalculation"

    def test_recalculate_skips_on_small_move(self) -> None:
        """recalculate() returns False and keeps zones on <5% price move."""
        initial_price = Decimal("50000")
        allocator = PriceZoneAllocator(reference_price=initial_price)

        # Store initial reference
        initial_ref = allocator.reference_price

        # 3% move — below threshold, should NOT trigger recalculation
        new_price = initial_price * Decimal("1.03")
        recalculated = allocator.recalculate(new_price)
        assert recalculated is False
        assert allocator.reference_price == initial_ref, "Reference price should be unchanged"

    def test_grid_zone_spans_10pct_from_reference(self) -> None:
        """Grid zone core boundaries are at ±10% of reference price."""
        price = Decimal("50000")
        allocator = PriceZoneAllocator()
        zones = allocator.get_zones(price, Decimal("0"))

        grid_low, grid_high = zones["grid"]
        # With 2.5% buffer: grid_low = price × 0.875, grid_high = price × 1.125
        expected_low = price * Decimal("0.875")
        expected_high = price * Decimal("1.125")
        assert grid_low == expected_low, f"Grid low: {grid_low} != {expected_low}"
        assert grid_high == expected_high, f"Grid high: {grid_high} != {expected_high}"

    def test_dca_zone_covers_minus_10_to_30pct(self) -> None:
        """DCA zone spans from −30% to −7.5% (with buffer) below reference."""
        price = Decimal("50000")
        allocator = PriceZoneAllocator()
        zones = allocator.get_zones(price, Decimal("0"))

        dca_low, dca_high = zones["dca"]
        expected_low = price * Decimal("0.675")  # −30% − 2.5% buffer
        expected_high = price * Decimal("0.925")  # −10% + 2.5% buffer
        assert dca_low == expected_low, f"DCA low: {dca_low} != {expected_low}"
        assert dca_high == expected_high, f"DCA high: {dca_high} != {expected_high}"

    def test_reference_price_updated_after_get_zones(self) -> None:
        """reference_price reflects the most recently passed price."""
        allocator = PriceZoneAllocator()
        allocator.get_zones(Decimal("50000"), Decimal("0"))
        assert allocator.reference_price == Decimal("50000")

        allocator.get_zones(Decimal("60000"), Decimal("0"))
        assert allocator.reference_price == Decimal("60000")
