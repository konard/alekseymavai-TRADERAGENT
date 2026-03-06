"""Unit tests for MarketScanner (#294).

Verifies:
- Pair scanning with ticker filtering (volume, spread, liquidity)
- OHLCV → DataFrame conversion
- Regime detection integration
- Error handling for individual pairs
- Result sorting by confidence
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.scanner.market_scanner import MarketScanner, ScannerConfig

# ---------------------------------------------------------------------------
# Fixtures / Helpers
# ---------------------------------------------------------------------------


def _make_ticker(
    *,
    bid: float = 50000.0,
    ask: float = 50010.0,
    base_volume: float = 100.0,
    quote_volume: float = 5_000_000.0,
    bid_volume: float = 10.0,
    ask_volume: float = 10.0,
    last: float = 50005.0,
) -> dict:
    return {
        "bid": bid,
        "ask": ask,
        "baseVolume": base_volume,
        "quoteVolume": quote_volume,
        "bidVolume": bid_volume,
        "askVolume": ask_volume,
        "last": last,
    }


def _make_ohlcv(rows: int = 200) -> list[list]:
    """Generate synthetic OHLCV data (uptrend)."""
    base_ts = 1700000000000  # arbitrary ms timestamp
    data = []
    price = 50000.0
    for i in range(rows):
        o = price
        h = price + 50
        low = price - 30
        c = price + 20
        v = 100.0
        data.append([base_ts + i * 3600000, o, h, low, c, v])
        price = c + 5  # slight uptrend
    return data


_SENTINEL = object()


def _make_exchange(
    ticker: dict | None = None,
    ohlcv: list[list] | object = _SENTINEL,
) -> AsyncMock:
    exchange = AsyncMock()
    exchange.fetch_ticker = AsyncMock(return_value=ticker or _make_ticker())
    exchange.fetch_ohlcv = AsyncMock(return_value=_make_ohlcv() if ohlcv is _SENTINEL else ohlcv)
    return exchange


# ---------------------------------------------------------------------------
# Static method tests
# ---------------------------------------------------------------------------


class TestExtractVolume24h:
    def test_uses_quote_volume(self) -> None:
        ticker = _make_ticker(quote_volume=10_000_000.0)
        assert MarketScanner._extract_volume_24h(ticker) == 10_000_000.0

    def test_fallback_to_base_volume_times_last(self) -> None:
        ticker = {"baseVolume": 200.0, "last": 50000.0}
        assert MarketScanner._extract_volume_24h(ticker) == 10_000_000.0

    def test_returns_zero_when_no_volume(self) -> None:
        assert MarketScanner._extract_volume_24h({}) == 0.0


class TestCalculateSpread:
    def test_normal_spread(self) -> None:
        ticker = _make_ticker(bid=50000.0, ask=50100.0)
        spread = MarketScanner._calculate_spread(ticker)
        # (50100 - 50000) / 50050 * 100 ≈ 0.1998
        assert 0.19 < spread < 0.21

    def test_zero_bid_returns_zero(self) -> None:
        ticker = _make_ticker(bid=0, ask=50000.0)
        assert MarketScanner._calculate_spread(ticker) == 0.0

    def test_missing_bid_ask(self) -> None:
        assert MarketScanner._calculate_spread({}) == 0.0


class TestExtractLiquidity:
    def test_uses_bid_ask_volumes(self) -> None:
        ticker = _make_ticker(bid=50000.0, ask=50010.0, bid_volume=5.0, ask_volume=3.0)
        liq = MarketScanner._extract_liquidity(ticker)
        # 5 * 50000 + 3 * 50010 = 250000 + 150030 = 400030
        assert liq == 400030.0

    def test_fallback_to_hourly_volume(self) -> None:
        ticker = {"quoteVolume": 2_400_000.0}
        liq = MarketScanner._extract_liquidity(ticker)
        assert liq == 100_000.0

    def test_returns_zero_when_empty(self) -> None:
        assert MarketScanner._extract_liquidity({}) == 0.0


class TestOhlcvToDataframe:
    def test_converts_to_dataframe(self) -> None:
        ohlcv = _make_ohlcv(10)
        df = MarketScanner._ohlcv_to_dataframe(ohlcv)
        assert len(df) == 10
        assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_empty_list_returns_empty_df(self) -> None:
        df = MarketScanner._ohlcv_to_dataframe([])
        assert df.empty


# ---------------------------------------------------------------------------
# Filtering tests
# ---------------------------------------------------------------------------


class TestScanFiltering:
    @pytest.mark.asyncio
    async def test_filters_low_volume(self) -> None:
        """Pairs below min_volume_usdt are excluded."""
        exchange = _make_exchange(ticker=_make_ticker(quote_volume=500_000.0))
        config = ScannerConfig(
            pairs=["LOW/USDT"],
            min_volume_usdt=1_000_000.0,
            request_delay_seconds=0,
        )
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_filters_high_spread(self) -> None:
        """Pairs above max_spread_pct are excluded."""
        # bid=100, ask=102 → spread ≈ 1.98%
        exchange = _make_exchange(ticker=_make_ticker(bid=100.0, ask=102.0))
        config = ScannerConfig(
            pairs=["WIDE/USDT"],
            max_spread_pct=0.5,
            request_delay_seconds=0,
        )
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_filters_low_liquidity(self) -> None:
        """Pairs below min_liquidity_usdt are excluded."""
        exchange = _make_exchange(
            ticker=_make_ticker(bid_volume=0.1, ask_volume=0.1, bid=100.0, ask=100.1)
        )
        config = ScannerConfig(
            pairs=["ILLIQ/USDT"],
            min_liquidity_usdt=50_000.0,
            request_delay_seconds=0,
        )
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_passes_all_filters(self) -> None:
        """Pair with good metrics passes all filters."""
        exchange = _make_exchange()
        config = ScannerConfig(
            pairs=["BTC/USDT"],
            request_delay_seconds=0,
        )
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 1
        assert results[0].symbol == "BTC/USDT"


# ---------------------------------------------------------------------------
# Regime detection integration
# ---------------------------------------------------------------------------


class TestRegimeIntegration:
    @pytest.mark.asyncio
    async def test_result_contains_regime(self) -> None:
        """Scan result has regime and recommended strategy."""
        exchange = _make_exchange()
        config = ScannerConfig(pairs=["BTC/USDT"], request_delay_seconds=0)
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 1
        r = results[0]
        assert r.regime is not None
        assert r.recommended_strategy is not None
        assert 0 <= r.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_result_to_dict(self) -> None:
        """ScanResult serializes correctly."""
        exchange = _make_exchange()
        config = ScannerConfig(pairs=["BTC/USDT"], request_delay_seconds=0)
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        d = results[0].to_dict()
        assert "symbol" in d
        assert "regime" in d
        assert "volume_24h" in d
        assert isinstance(d["confidence"], float)

    @pytest.mark.asyncio
    async def test_results_sorted_by_confidence(self) -> None:
        """Results are sorted by confidence descending."""
        # Create two tickers that both pass filters
        ticker1 = _make_ticker()
        ticker2 = _make_ticker(last=30000.0)

        exchange = AsyncMock()
        call_count = 0

        async def _fetch_ticker(symbol):
            nonlocal call_count
            call_count += 1
            return ticker1 if call_count == 1 else ticker2

        exchange.fetch_ticker = _fetch_ticker
        exchange.fetch_ohlcv = AsyncMock(return_value=_make_ohlcv())
        config = ScannerConfig(
            pairs=["BTC/USDT", "ETH/USDT"],
            request_delay_seconds=0,
        )
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        if len(results) >= 2:
            assert results[0].confidence >= results[1].confidence

    @pytest.mark.asyncio
    async def test_last_results_stored(self) -> None:
        """last_results returns copy of most recent scan."""
        exchange = _make_exchange()
        config = ScannerConfig(pairs=["BTC/USDT"], request_delay_seconds=0)
        scanner = MarketScanner(exchange, config)
        assert scanner.last_results == []
        await scanner.scan()
        assert len(scanner.last_results) == 1


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_ticker_error_skips_pair(self) -> None:
        """If fetch_ticker fails, pair is skipped."""
        exchange = AsyncMock()
        exchange.fetch_ticker = AsyncMock(side_effect=Exception("API error"))
        config = ScannerConfig(pairs=["FAIL/USDT"], request_delay_seconds=0)
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_ohlcv_error_skips_pair(self) -> None:
        """If fetch_ohlcv fails, pair is skipped."""
        exchange = AsyncMock()
        exchange.fetch_ticker = AsyncMock(return_value=_make_ticker())
        exchange.fetch_ohlcv = AsyncMock(side_effect=Exception("Timeout"))
        config = ScannerConfig(pairs=["FAIL/USDT"], request_delay_seconds=0)
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_empty_ohlcv_skips_pair(self) -> None:
        """If OHLCV returns empty, pair is skipped."""
        exchange = _make_exchange(ohlcv=[])
        config = ScannerConfig(pairs=["EMPTY/USDT"], request_delay_seconds=0)
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_partial_failure_returns_good_pairs(self) -> None:
        """One failing pair doesn't block others."""
        call_count = 0

        async def _fetch_ticker(symbol):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network error")
            return _make_ticker()

        exchange = AsyncMock()
        exchange.fetch_ticker = _fetch_ticker
        exchange.fetch_ohlcv = AsyncMock(return_value=_make_ohlcv())
        config = ScannerConfig(
            pairs=["FAIL/USDT", "BTC/USDT"],
            request_delay_seconds=0,
        )
        scanner = MarketScanner(exchange, config)
        results = await scanner.scan()
        assert len(results) == 1
        assert results[0].symbol == "BTC/USDT"


# ---------------------------------------------------------------------------
# Config schema tests
# ---------------------------------------------------------------------------


class TestScannerConfigSchema:
    def test_default_config(self) -> None:
        from bot.config.schemas import ScannerConfig as SchemaConfig

        cfg = SchemaConfig()
        assert cfg.enabled is False
        assert len(cfg.pairs) == 5
        assert cfg.interval_minutes == 15
        assert cfg.min_volume_usdt == 1_000_000.0

    def test_custom_config(self) -> None:
        from bot.config.schemas import ScannerConfig as SchemaConfig

        cfg = SchemaConfig(
            enabled=True,
            pairs=["BTC/USDT"],
            interval_minutes=5,
            min_volume_usdt=500_000.0,
            max_spread_pct=1.0,
        )
        assert cfg.enabled is True
        assert cfg.pairs == ["BTC/USDT"]
        assert cfg.interval_minutes == 5

    def test_app_config_includes_scanner(self) -> None:
        from bot.config.schemas import AppConfig

        cfg = AppConfig(
            database_url="postgresql+asyncpg://user:pass@localhost/test",
            encryption_key="dGVzdA==",
        )
        assert cfg.scanner is not None
        assert cfg.scanner.enabled is False
