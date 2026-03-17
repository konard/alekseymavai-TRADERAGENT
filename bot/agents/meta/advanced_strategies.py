from __future__ import annotations

import time
from dataclasses import dataclass, field
from statistics import mean, stdev


@dataclass
class FundingOpportunity:
    symbol: str
    funding_rate: float
    annualized_yield: float
    direction: str
    risk_score: float
    estimated_daily_pnl: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "funding_rate": self.funding_rate,
            "annualized_yield": self.annualized_yield,
            "direction": self.direction,
            "risk_score": self.risk_score,
            "estimated_daily_pnl": self.estimated_daily_pnl,
        }


class FundingRateHarvester:
    def __init__(
        self,
        min_annualized_yield: float = 0.10,
        max_risk_score: float = 0.6,
        position_size_usd: float = 1000.0,
    ) -> None:
        self.min_annualized_yield = min_annualized_yield
        self.max_risk_score = max_risk_score
        self.position_size_usd = position_size_usd
        self._history: dict[str, list[dict]] = {}
        self._max_history = 100

    def observe_rate(self, symbol: str, funding_rate: float, ts: float | None = None) -> None:
        if ts is None:
            ts = time.time()
        if symbol not in self._history:
            self._history[symbol] = []
        self._history[symbol].append({"rate": funding_rate, "ts": ts})
        if len(self._history[symbol]) > self._max_history:
            self._history[symbol] = self._history[symbol][-self._max_history :]

    def find_opportunities(self) -> list[FundingOpportunity]:
        opportunities: list[FundingOpportunity] = []
        for symbol, entries in self._history.items():
            if len(entries) < 3:
                continue
            rates = [e["rate"] for e in entries]
            avg_rate = mean(rates)
            if avg_rate == 0:
                continue
            annualized = abs(avg_rate) * 3 * 365
            direction = "short" if avg_rate > 0 else "long"
            risk_score = self._compute_risk_score(rates, avg_rate)
            daily_pnl = abs(avg_rate) * 3 * self.position_size_usd
            if annualized >= self.min_annualized_yield and risk_score <= self.max_risk_score:
                opportunities.append(
                    FundingOpportunity(
                        symbol=symbol,
                        funding_rate=avg_rate,
                        annualized_yield=annualized,
                        direction=direction,
                        risk_score=risk_score,
                        estimated_daily_pnl=daily_pnl,
                    )
                )
        opportunities.sort(key=lambda o: o.annualized_yield, reverse=True)
        return opportunities

    def get_best_opportunity(self) -> FundingOpportunity | None:
        opps = self.find_opportunities()
        return opps[0] if opps else None

    def get_portfolio_yield(self, positions: dict[str, str]) -> dict:
        total_daily = 0.0
        per_symbol: dict[str, dict] = {}
        for symbol, direction in positions.items():
            entries = self._history.get(symbol, [])
            if len(entries) < 1:
                per_symbol[symbol] = {"daily_yield": 0.0, "annualized_yield": 0.0}
                continue
            rates = [e["rate"] for e in entries]
            avg_rate = mean(rates)
            # If direction matches the collecting side, yield is positive
            if (direction == "short" and avg_rate > 0) or (direction == "long" and avg_rate < 0):
                daily = abs(avg_rate) * 3 * self.position_size_usd
            else:
                daily = -abs(avg_rate) * 3 * self.position_size_usd
            annualized = daily * 365
            total_daily += daily
            per_symbol[symbol] = {"daily_yield": daily, "annualized_yield": annualized}
        total_annualized = total_daily * 365
        return {
            "total_daily_yield": total_daily,
            "total_annualized_yield": total_annualized,
            "per_symbol": per_symbol,
        }

    def get_rate_history(self, symbol: str) -> list[dict]:
        return list(self._history.get(symbol, []))

    def get_stats(self) -> dict:
        return {
            "symbols_tracked": len(self._history),
            "total_observations": sum(len(v) for v in self._history.values()),
            "min_annualized_yield": self.min_annualized_yield,
            "max_risk_score": self.max_risk_score,
            "position_size_usd": self.position_size_usd,
            "opportunities_count": len(self.find_opportunities()),
        }

    @staticmethod
    def _compute_risk_score(rates: list[float], avg_rate: float) -> float:
        if len(rates) < 2 or avg_rate == 0:
            return 1.0
        volatility = stdev(rates) / abs(avg_rate)
        # Direction consistency: fraction of rates on same side as avg
        same_side = sum(1 for r in rates if (r > 0) == (avg_rate > 0)) / len(rates)
        direction_risk = 1.0 - same_side
        # Combine: 60% volatility, 40% direction inconsistency
        raw = 0.6 * min(volatility, 1.0) + 0.4 * direction_risk
        return round(min(max(raw, 0.0), 1.0), 4)


@dataclass
class VenueQuote:
    venue: str
    bid: float
    ask: float
    spread_pct: float
    depth_usd: float
    latency_ms: float
    fee_pct: float

    def to_dict(self) -> dict:
        return {
            "venue": self.venue,
            "bid": self.bid,
            "ask": self.ask,
            "spread_pct": self.spread_pct,
            "depth_usd": self.depth_usd,
            "latency_ms": self.latency_ms,
            "fee_pct": self.fee_pct,
        }


class SmartOrderRouter:
    def __init__(
        self,
        max_latency_ms: float = 500.0,
        fee_weight: float = 0.3,
        spread_weight: float = 0.4,
        depth_weight: float = 0.2,
        latency_weight: float = 0.1,
    ) -> None:
        self.max_latency_ms = max_latency_ms
        self.fee_weight = fee_weight
        self.spread_weight = spread_weight
        self.depth_weight = depth_weight
        self.latency_weight = latency_weight
        self._venues: dict[str, dict] = {}  # venue -> {fee_pct, latency_ms}
        self._quotes: dict[str, dict[str, VenueQuote]] = {}  # symbol -> {venue -> VenueQuote}
        self._fills: list[dict] = []

    def add_venue(self, venue: str, fee_pct: float, latency_ms: float = 100.0) -> None:
        self._venues[venue] = {"fee_pct": fee_pct, "latency_ms": latency_ms}

    def update_quote(
        self, venue: str, symbol: str, bid: float, ask: float, depth_usd: float
    ) -> None:
        if venue not in self._venues:
            return
        info = self._venues[venue]
        mid = (bid + ask) / 2 if (bid + ask) > 0 else 1.0
        spread_pct = (ask - bid) / mid if mid > 0 else 0.0
        quote = VenueQuote(
            venue=venue,
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            depth_usd=depth_usd,
            latency_ms=info["latency_ms"],
            fee_pct=info["fee_pct"],
        )
        if symbol not in self._quotes:
            self._quotes[symbol] = {}
        self._quotes[symbol][venue] = quote

    def route_order(self, symbol: str, side: str, size_usd: float) -> dict:
        quotes = self._quotes.get(symbol, {})
        if not quotes:
            return {
                "best_venue": None,
                "score": 0.0,
                "quote": None,
                "all_venues": [],
                "estimated_cost": 0.0,
            }
        # Filter by latency
        eligible = {v: q for v, q in quotes.items() if q.latency_ms <= self.max_latency_ms}
        if not eligible:
            return {
                "best_venue": None,
                "score": 0.0,
                "quote": None,
                "all_venues": [],
                "estimated_cost": 0.0,
            }
        max_spread = max(q.spread_pct for q in eligible.values()) or 1e-9
        max_fee = max(q.fee_pct for q in eligible.values()) or 1e-9

        scored: list[dict] = []
        for venue, q in eligible.items():
            spread_score = 1.0 - (q.spread_pct / max_spread) if max_spread > 0 else 1.0
            depth_score = min(1.0, q.depth_usd / (size_usd * 3)) if size_usd > 0 else 1.0
            fee_score = 1.0 - (q.fee_pct / max_fee) if max_fee > 0 else 1.0
            latency_score = 1.0 - (q.latency_ms / self.max_latency_ms)
            total = (
                self.spread_weight * spread_score
                + self.depth_weight * depth_score
                + self.fee_weight * fee_score
                + self.latency_weight * latency_score
            )
            est_cost = size_usd * (q.spread_pct / 2 + q.fee_pct)
            scored.append(
                {
                    "venue": venue,
                    "score": round(total, 6),
                    "quote": q.to_dict(),
                    "estimated_cost": round(est_cost, 6),
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        best = scored[0]
        return {
            "best_venue": best["venue"],
            "score": best["score"],
            "quote": best["quote"],
            "all_venues": scored,
            "estimated_cost": best["estimated_cost"],
        }

    def split_order(
        self, symbol: str, side: str, size_usd: float, max_per_venue_pct: float = 0.5
    ) -> list[dict]:
        quotes = self._quotes.get(symbol, {})
        eligible = {v: q for v, q in quotes.items() if q.latency_ms <= self.max_latency_ms}
        if not eligible:
            return []

        # Sort venues by route score
        route = self.route_order(symbol, side, size_usd)
        all_venues = route.get("all_venues", [])
        if not all_venues:
            return []

        remaining = size_usd
        splits: list[dict] = []
        for entry in all_venues:
            if remaining <= 0:
                break
            venue = entry["venue"]
            q = eligible.get(venue)
            if q is None:
                continue
            max_for_venue = q.depth_usd * max_per_venue_pct
            alloc = min(remaining, max_for_venue)
            if alloc <= 0:
                continue
            est_cost = alloc * (q.spread_pct / 2 + q.fee_pct)
            splits.append(
                {
                    "venue": venue,
                    "size_usd": round(alloc, 6),
                    "estimated_cost": round(est_cost, 6),
                }
            )
            remaining -= alloc

        # If there's remaining, allocate to best venue
        if remaining > 0 and splits:
            splits[0]["size_usd"] = round(splits[0]["size_usd"] + remaining, 6)
            q0 = eligible[splits[0]["venue"]]
            splits[0]["estimated_cost"] = round(
                splits[0]["size_usd"] * (q0.spread_pct / 2 + q0.fee_pct), 6
            )
        elif remaining > 0 and not splits:
            # Fallback: put everything on best venue
            best_venue = all_venues[0]["venue"]
            q = eligible[best_venue]
            est_cost = size_usd * (q.spread_pct / 2 + q.fee_pct)
            splits.append(
                {
                    "venue": best_venue,
                    "size_usd": round(size_usd, 6),
                    "estimated_cost": round(est_cost, 6),
                }
            )
        return splits

    def record_fill(
        self,
        venue: str,
        symbol: str,
        expected_price: float,
        actual_price: float,
        size_usd: float,
    ) -> None:
        slippage = (actual_price - expected_price) / expected_price if expected_price else 0.0
        self._fills.append(
            {
                "venue": venue,
                "symbol": symbol,
                "expected_price": expected_price,
                "actual_price": actual_price,
                "size_usd": size_usd,
                "slippage": slippage,
                "ts": time.time(),
            }
        )

    def get_venue_stats(self) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for venue in self._venues:
            venue_fills = [f for f in self._fills if f["venue"] == venue]
            # Collect spreads across all symbols
            spreads: list[float] = []
            for sym_quotes in self._quotes.values():
                if venue in sym_quotes:
                    spreads.append(sym_quotes[venue].spread_pct)
            stats[venue] = {
                "avg_spread": mean(spreads) if spreads else 0.0,
                "fill_count": len(venue_fills),
                "avg_latency": self._venues[venue]["latency_ms"],
                "avg_slippage": (
                    mean([f["slippage"] for f in venue_fills]) if venue_fills else 0.0
                ),
            }
        return stats

    def get_stats(self) -> dict:
        return {
            "venues_count": len(self._venues),
            "symbols_quoted": len(self._quotes),
            "total_fills": len(self._fills),
            "venue_stats": self.get_venue_stats(),
        }
