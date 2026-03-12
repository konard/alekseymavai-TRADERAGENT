from __future__ import annotations

import pytest

from bot.agents.meta.advanced_strategies import (
    FundingOpportunity,
    FundingRateHarvester,
    SmartOrderRouter,
    VenueQuote,
)


# ---------------------------------------------------------------------------
# FundingOpportunity
# ---------------------------------------------------------------------------

class TestFundingOpportunity:
    def test_to_dict(self):
        opp = FundingOpportunity(
            symbol="BTCUSDT",
            funding_rate=0.0001,
            annualized_yield=0.1095,
            direction="short",
            risk_score=0.3,
            estimated_daily_pnl=0.3,
        )
        d = opp.to_dict()
        assert d["symbol"] == "BTCUSDT"
        assert d["direction"] == "short"
        assert d["annualized_yield"] == pytest.approx(0.1095)


# ---------------------------------------------------------------------------
# FundingRateHarvester
# ---------------------------------------------------------------------------

class TestFundingRateHarvester:
    def _harvester_with_rates(
        self, symbol: str = "BTCUSDT", rates: list[float] | None = None
    ) -> FundingRateHarvester:
        h = FundingRateHarvester(min_annualized_yield=0.05, max_risk_score=0.8)
        if rates is None:
            rates = [0.0001] * 5
        for i, r in enumerate(rates):
            h.observe_rate(symbol, r, ts=1000.0 + i * 100)
        return h

    def test_observe_rate_stores_history(self):
        h = FundingRateHarvester()
        h.observe_rate("BTCUSDT", 0.0001, ts=1000.0)
        h.observe_rate("BTCUSDT", 0.0002, ts=2000.0)
        hist = h.get_rate_history("BTCUSDT")
        assert len(hist) == 2
        assert hist[0]["rate"] == 0.0001
        assert hist[1]["rate"] == 0.0002

    def test_observe_rate_rolling_window(self):
        h = FundingRateHarvester()
        for i in range(120):
            h.observe_rate("X", float(i) * 0.0001, ts=float(i))
        assert len(h.get_rate_history("X")) == 100

    def test_find_opportunities_positive_funding(self):
        """Positive avg funding -> direction=short (collect funding by shorting)."""
        h = self._harvester_with_rates("ETHUSDT", [0.0003] * 5)
        opps = h.find_opportunities()
        assert len(opps) >= 1
        opp = opps[0]
        assert opp.symbol == "ETHUSDT"
        assert opp.direction == "short"
        assert opp.annualized_yield == pytest.approx(0.0003 * 3 * 365)

    def test_find_opportunities_negative_funding(self):
        """Negative avg funding -> direction=long (collect funding by going long)."""
        h = self._harvester_with_rates("SOLUSDT", [-0.0005] * 5)
        opps = h.find_opportunities()
        assert len(opps) >= 1
        assert opps[0].direction == "long"

    def test_find_opportunities_low_yield_filtered(self):
        """Yields below min_annualized_yield are excluded."""
        h = FundingRateHarvester(min_annualized_yield=0.50)
        for i in range(5):
            h.observe_rate("LOW", 0.00001, ts=float(i))  # annualized ~1.1%
        assert h.find_opportunities() == []

    def test_find_opportunities_high_risk_filtered(self):
        """High volatility rates produce high risk_score -> filtered out."""
        h = FundingRateHarvester(min_annualized_yield=0.01, max_risk_score=0.1)
        # Alternating sign rates -> high volatility, low consistency
        rates = [0.001, -0.001, 0.001, -0.001, 0.001]
        for i, r in enumerate(rates):
            h.observe_rate("WILD", r, ts=float(i))
        opps = h.find_opportunities()
        assert opps == []

    def test_find_opportunities_sorted_by_yield(self):
        h = FundingRateHarvester(min_annualized_yield=0.01, max_risk_score=0.9)
        for i in range(5):
            h.observe_rate("A", 0.0002, ts=float(i))
            h.observe_rate("B", 0.0005, ts=float(i))
        opps = h.find_opportunities()
        assert len(opps) == 2
        assert opps[0].symbol == "B"
        assert opps[0].annualized_yield > opps[1].annualized_yield

    def test_get_best_opportunity(self):
        h = self._harvester_with_rates("BTCUSDT", [0.0002] * 5)
        best = h.get_best_opportunity()
        assert best is not None
        assert best.symbol == "BTCUSDT"

    def test_get_best_opportunity_none(self):
        h = FundingRateHarvester(min_annualized_yield=99.0)
        for i in range(5):
            h.observe_rate("X", 0.0001, ts=float(i))
        assert h.get_best_opportunity() is None

    def test_portfolio_yield_collecting(self):
        h = FundingRateHarvester(position_size_usd=1000.0)
        for i in range(5):
            h.observe_rate("BTCUSDT", 0.0001, ts=float(i))
        result = h.get_portfolio_yield({"BTCUSDT": "short"})
        assert result["total_daily_yield"] > 0
        assert result["per_symbol"]["BTCUSDT"]["daily_yield"] > 0

    def test_portfolio_yield_paying(self):
        """Wrong direction means you pay funding."""
        h = FundingRateHarvester(position_size_usd=1000.0)
        for i in range(5):
            h.observe_rate("BTCUSDT", 0.0001, ts=float(i))
        result = h.get_portfolio_yield({"BTCUSDT": "long"})
        assert result["total_daily_yield"] < 0

    def test_rate_history_empty(self):
        h = FundingRateHarvester()
        assert h.get_rate_history("UNKNOWN") == []

    def test_insufficient_history(self):
        """Need >= 3 observations to produce opportunities."""
        h = FundingRateHarvester(min_annualized_yield=0.01, max_risk_score=0.9)
        h.observe_rate("X", 0.001, ts=1.0)
        h.observe_rate("X", 0.001, ts=2.0)
        assert h.find_opportunities() == []

    def test_risk_score_stable_rates(self):
        """Perfectly stable rates should have low risk score."""
        h = FundingRateHarvester(min_annualized_yield=0.01, max_risk_score=0.9)
        for i in range(10):
            h.observe_rate("STABLE", 0.0005, ts=float(i))
        opps = h.find_opportunities()
        assert len(opps) == 1
        assert opps[0].risk_score < 0.1

    def test_get_stats(self):
        h = self._harvester_with_rates("A", [0.0003] * 5)
        stats = h.get_stats()
        assert stats["symbols_tracked"] == 1
        assert stats["total_observations"] == 5


# ---------------------------------------------------------------------------
# VenueQuote
# ---------------------------------------------------------------------------

class TestVenueQuote:
    def test_to_dict(self):
        q = VenueQuote(
            venue="binance", bid=100.0, ask=100.05,
            spread_pct=0.0005, depth_usd=50000.0,
            latency_ms=50.0, fee_pct=0.001,
        )
        d = q.to_dict()
        assert d["venue"] == "binance"
        assert d["bid"] == 100.0


# ---------------------------------------------------------------------------
# SmartOrderRouter
# ---------------------------------------------------------------------------

class TestSmartOrderRouter:
    def _router_with_venues(self) -> SmartOrderRouter:
        r = SmartOrderRouter(max_latency_ms=500.0)
        r.add_venue("binance", fee_pct=0.001, latency_ms=50.0)
        r.add_venue("bybit", fee_pct=0.0006, latency_ms=80.0)
        r.add_venue("okx", fee_pct=0.0008, latency_ms=120.0)
        return r

    def test_add_venue(self):
        r = SmartOrderRouter()
        r.add_venue("binance", fee_pct=0.001)
        stats = r.get_stats()
        assert stats["venues_count"] == 1

    def test_update_quote(self):
        r = self._router_with_venues()
        r.update_quote("binance", "BTCUSDT", bid=50000.0, ask=50010.0, depth_usd=100000.0)
        result = r.route_order("BTCUSDT", "buy", 1000.0)
        assert result["best_venue"] == "binance"

    def test_route_to_best_venue(self):
        """Bybit has lowest fee and tight spread -> should win."""
        r = self._router_with_venues()
        r.update_quote("binance", "ETHUSDT", bid=3000.0, ask=3003.0, depth_usd=80000.0)
        r.update_quote("bybit", "ETHUSDT", bid=3000.0, ask=3001.5, depth_usd=80000.0)
        r.update_quote("okx", "ETHUSDT", bid=3000.0, ask=3004.0, depth_usd=80000.0)
        result = r.route_order("ETHUSDT", "buy", 5000.0)
        assert result["best_venue"] == "bybit"
        assert result["score"] > 0
        assert len(result["all_venues"]) == 3

    def test_route_latency_filter(self):
        """Venue exceeding max_latency_ms should be excluded."""
        r = SmartOrderRouter(max_latency_ms=100.0)
        r.add_venue("fast", fee_pct=0.002, latency_ms=50.0)
        r.add_venue("slow", fee_pct=0.0005, latency_ms=200.0)  # filtered
        r.update_quote("fast", "BTCUSDT", bid=50000.0, ask=50010.0, depth_usd=100000.0)
        r.update_quote("slow", "BTCUSDT", bid=50000.0, ask=50005.0, depth_usd=100000.0)
        result = r.route_order("BTCUSDT", "buy", 1000.0)
        assert result["best_venue"] == "fast"
        assert len(result["all_venues"]) == 1

    def test_route_no_quotes(self):
        r = self._router_with_venues()
        result = r.route_order("UNKNOWN", "buy", 1000.0)
        assert result["best_venue"] is None
        assert result["all_venues"] == []

    def test_estimated_cost(self):
        r = SmartOrderRouter()
        r.add_venue("v1", fee_pct=0.001, latency_ms=50.0)
        r.update_quote("v1", "BTCUSDT", bid=50000.0, ask=50050.0, depth_usd=500000.0)
        result = r.route_order("BTCUSDT", "buy", 10000.0)
        # spread_pct ≈ 50/50025 ≈ 0.000999; cost = 10000*(spread/2 + fee)
        assert result["estimated_cost"] > 0

    def test_split_order_single_venue(self):
        """When order fits in one venue, no split needed."""
        r = SmartOrderRouter()
        r.add_venue("v1", fee_pct=0.001, latency_ms=50.0)
        r.update_quote("v1", "BTCUSDT", bid=50000.0, ask=50010.0, depth_usd=500000.0)
        splits = r.split_order("BTCUSDT", "buy", 1000.0)
        assert len(splits) == 1
        assert splits[0]["venue"] == "v1"
        assert splits[0]["size_usd"] >= 1000.0

    def test_split_large_order(self):
        """Large order should split across venues."""
        r = SmartOrderRouter()
        r.add_venue("a", fee_pct=0.001, latency_ms=50.0)
        r.add_venue("b", fee_pct=0.0008, latency_ms=60.0)
        r.update_quote("a", "ETHUSDT", bid=3000.0, ask=3001.0, depth_usd=10000.0)
        r.update_quote("b", "ETHUSDT", bid=3000.0, ask=3001.0, depth_usd=10000.0)
        # size 15000 > 0.5 * 10000 for each venue
        splits = r.split_order("ETHUSDT", "buy", 15000.0, max_per_venue_pct=0.5)
        assert len(splits) >= 2
        total = sum(s["size_usd"] for s in splits)
        assert total == pytest.approx(15000.0)

    def test_split_no_quotes(self):
        r = self._router_with_venues()
        assert r.split_order("UNKNOWN", "buy", 1000.0) == []

    def test_record_fill(self):
        r = self._router_with_venues()
        r.record_fill("binance", "BTCUSDT", expected_price=50000.0, actual_price=50005.0, size_usd=10000.0)
        stats = r.get_venue_stats()
        assert stats["binance"]["fill_count"] == 1
        assert stats["binance"]["avg_slippage"] == pytest.approx(5.0 / 50000.0)

    def test_venue_stats(self):
        r = self._router_with_venues()
        r.update_quote("binance", "BTCUSDT", bid=50000.0, ask=50010.0, depth_usd=100000.0)
        stats = r.get_venue_stats()
        assert "binance" in stats
        assert stats["binance"]["avg_latency"] == 50.0
        assert stats["bybit"]["fill_count"] == 0

    def test_get_stats(self):
        r = self._router_with_venues()
        r.update_quote("binance", "BTCUSDT", bid=50000.0, ask=50010.0, depth_usd=100000.0)
        stats = r.get_stats()
        assert stats["venues_count"] == 3
        assert stats["symbols_quoted"] == 1
        assert stats["total_fills"] == 0
