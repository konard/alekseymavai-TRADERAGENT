"""Market Scanner — scan pairs, classify regimes, rank by suitability.

Periodically fetches OHLCV and ticker data for a configurable list of
trading pairs, runs each through ``MarketRegimeDetector.analyze()``, and
filters by minimum 24h volume, maximum spread, and minimum liquidity.

Usage::

    scanner = MarketScanner(exchange_client, config)
    results = await scanner.scan()
    for r in results:
        print(r.symbol, r.regime, r.recommended_strategy, r.volume_24h)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd

from bot.orchestrator.market_regime import (
    MarketRegime,
    MarketRegimeDetector,
    RecommendedStrategy,
    RegimeAnalysis,
)
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ScannerConfig:
    """Configuration for MarketScanner."""

    pairs: list[str] = field(
        default_factory=lambda: [
            "BTC/USDT",
            "ETH/USDT",
            "SOL/USDT",
            "XRP/USDT",
            "DOGE/USDT",
        ]
    )
    interval_minutes: int = 15
    timeframe: str = "1h"
    ohlcv_limit: int = 200
    min_volume_usdt: float = 1_000_000.0
    max_spread_pct: float = 0.5
    min_liquidity_usdt: float = 50_000.0
    request_delay_seconds: float = 0.2


@dataclass
class ScanResult:
    """Result of scanning a single trading pair."""

    symbol: str
    regime: MarketRegime
    recommended_strategy: RecommendedStrategy
    confidence: float
    volume_24h: float
    spread_pct: float
    liquidity_usdt: float
    regime_analysis: RegimeAnalysis
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "symbol": self.symbol,
            "regime": self.regime.value,
            "recommended_strategy": self.recommended_strategy.value,
            "confidence": round(self.confidence, 4),
            "volume_24h": round(self.volume_24h, 2),
            "spread_pct": round(self.spread_pct, 4),
            "liquidity_usdt": round(self.liquidity_usdt, 2),
            "timestamp": self.timestamp.isoformat(),
        }


class MarketScanner:
    """Scan multiple trading pairs, classify regimes, filter by quality.

    Args:
        exchange: Exchange client with ``fetch_ticker`` and ``fetch_ohlcv``.
        config: Scanner configuration.
        detector: Optional pre-configured regime detector (uses default if None).
    """

    def __init__(
        self,
        exchange: Any,
        config: ScannerConfig | None = None,
        detector: MarketRegimeDetector | None = None,
    ) -> None:
        self.exchange = exchange
        self.config = config or ScannerConfig()
        self.detector = detector or MarketRegimeDetector()
        self._last_results: list[ScanResult] = []

    @property
    def last_results(self) -> list[ScanResult]:
        """Return results from the most recent scan."""
        return self._last_results.copy()

    async def scan(self) -> list[ScanResult]:
        """Scan all configured pairs and return filtered, sorted results.

        Steps for each pair:
        1. Fetch ticker (24h volume, bid/ask for spread, quote volume for liquidity)
        2. Filter by min_volume, max_spread, min_liquidity
        3. Fetch OHLCV and run regime detection
        4. Sort results by confidence (descending)

        Returns:
            List of ScanResult for pairs that pass all filters.
        """
        results: list[ScanResult] = []
        delay = self.config.request_delay_seconds

        for symbol in self.config.pairs:
            try:
                result = await self._scan_pair(symbol)
                if result is not None:
                    results.append(result)
            except Exception as e:
                logger.warning("scan_pair_failed", symbol=symbol, error=str(e))

            if delay > 0:
                await asyncio.sleep(delay)

        results.sort(key=lambda r: r.confidence, reverse=True)
        self._last_results = results

        logger.info(
            "scan_completed",
            total_pairs=len(self.config.pairs),
            passed_filters=len(results),
            top_pair=results[0].symbol if results else None,
        )

        return results

    async def _scan_pair(self, symbol: str) -> ScanResult | None:
        """Scan a single pair: fetch data, filter, classify regime.

        Returns None if the pair fails any filter.
        """
        # 1. Fetch ticker
        ticker = await self.exchange.fetch_ticker(symbol)
        volume_24h = self._extract_volume_24h(ticker)
        spread_pct = self._calculate_spread(ticker)
        liquidity_usdt = self._extract_liquidity(ticker)

        # 2. Apply filters
        if volume_24h < self.config.min_volume_usdt:
            logger.debug(
                "scan_filtered_low_volume",
                symbol=symbol,
                volume_24h=round(volume_24h, 2),
                min_required=self.config.min_volume_usdt,
            )
            return None

        if spread_pct > self.config.max_spread_pct:
            logger.debug(
                "scan_filtered_high_spread",
                symbol=symbol,
                spread_pct=round(spread_pct, 4),
                max_allowed=self.config.max_spread_pct,
            )
            return None

        if liquidity_usdt < self.config.min_liquidity_usdt:
            logger.debug(
                "scan_filtered_low_liquidity",
                symbol=symbol,
                liquidity_usdt=round(liquidity_usdt, 2),
                min_required=self.config.min_liquidity_usdt,
            )
            return None

        # 3. Fetch OHLCV and run regime detection
        ohlcv_raw = await self.exchange.fetch_ohlcv(
            symbol,
            timeframe=self.config.timeframe,
            limit=self.config.ohlcv_limit,
        )

        df = self._ohlcv_to_dataframe(ohlcv_raw)
        if df.empty:
            logger.warning("scan_empty_ohlcv", symbol=symbol)
            return None

        analysis = self.detector.analyze(df)

        if analysis.regime == MarketRegime.UNKNOWN:
            logger.debug("scan_unknown_regime", symbol=symbol)
            return None

        return ScanResult(
            symbol=symbol,
            regime=analysis.regime,
            recommended_strategy=analysis.recommended_strategy,
            confidence=analysis.confidence,
            volume_24h=volume_24h,
            spread_pct=spread_pct,
            liquidity_usdt=liquidity_usdt,
            regime_analysis=analysis,
        )

    @staticmethod
    def _extract_volume_24h(ticker: dict[str, Any]) -> float:
        """Extract 24h USDT volume from ticker.

        CCXT tickers use ``quoteVolume`` for quote-currency volume (USDT).
        Falls back to ``baseVolume * last`` when quoteVolume is missing.
        """
        quote_vol = ticker.get("quoteVolume")
        if quote_vol is not None:
            return float(quote_vol)
        base_vol = ticker.get("baseVolume", 0)
        last_price = ticker.get("last", 0)
        if base_vol and last_price:
            return float(Decimal(str(base_vol)) * Decimal(str(last_price)))
        return 0.0

    @staticmethod
    def _calculate_spread(ticker: dict[str, Any]) -> float:
        """Calculate bid-ask spread as percentage of mid price."""
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        if not bid or not ask:
            return 0.0
        bid_d = Decimal(str(bid))
        ask_d = Decimal(str(ask))
        if bid_d <= 0 or ask_d <= 0:
            return 0.0
        mid = (bid_d + ask_d) / 2
        spread = (ask_d - bid_d) / mid * 100
        return float(spread)

    @staticmethod
    def _extract_liquidity(ticker: dict[str, Any]) -> float:
        """Estimate orderbook liquidity from bid/ask volumes.

        Uses ``bidVolume`` and ``askVolume`` from CCXT ticker, multiplied
        by their respective prices to get USDT-denominated liquidity.
        Falls back to quoteVolume / 24 (hourly average) if absent.
        """
        bid = ticker.get("bid", 0)
        ask = ticker.get("ask", 0)
        bid_vol = ticker.get("bidVolume", 0)
        ask_vol = ticker.get("askVolume", 0)

        if bid_vol and ask_vol and bid and ask:
            return float(
                Decimal(str(bid_vol)) * Decimal(str(bid))
                + Decimal(str(ask_vol)) * Decimal(str(ask))
            )

        # Fallback: average hourly volume
        quote_vol = ticker.get("quoteVolume", 0)
        if quote_vol:
            return float(Decimal(str(quote_vol)) / 24)
        return 0.0

    @staticmethod
    def _ohlcv_to_dataframe(ohlcv_raw: list[list]) -> pd.DataFrame:
        """Convert raw OHLCV list to DataFrame expected by MarketRegimeDetector."""
        if not ohlcv_raw:
            return pd.DataFrame()
        df = pd.DataFrame(
            ohlcv_raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
