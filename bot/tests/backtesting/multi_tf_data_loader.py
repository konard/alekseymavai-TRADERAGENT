"""
Multi-timeframe data loader for backtesting.

Generates or loads synchronized OHLCV data across D1, H4, H1, M15, M5
timeframes. Data is generated at M5 resolution first, then aggregated
upward using pandas resample to ensure perfect timestamp alignment.

Usage:
    loader = MultiTimeframeDataLoader()
    data = loader.load("BTC/USDT", start, end, trend="up")
    df_d1, df_h4, df_h1, df_m15, df_m5 = loader.get_context_at(data, base_index=200, lookback=50)
"""

import warnings
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pandas as pd

from bot.tests.backtesting.test_data import HistoricalDataProvider

# Map human-readable timeframe strings to pandas resample rules
_TF_TO_RULE: dict[str, str] = {
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}


@dataclass
class MultiTimeframeData:
    """Container for synchronized multi-timeframe OHLCV DataFrames."""

    d1: pd.DataFrame
    h4: pd.DataFrame
    h1: pd.DataFrame
    m15: pd.DataFrame
    m5: pd.DataFrame

    def as_tuple(
        self,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Return (d1, h4, h1, m15, m5) tuple for unpacking."""
        return (self.d1, self.h4, self.h1, self.m15, self.m5)


class MultiTimeframeDataLoader:
    """
    Loads/generates synchronized OHLCV data across D1, H4, H1, M15, M5.

    Data is generated at M5 resolution first, then aggregated upward
    to ensure perfect alignment. Every M5 candle has a corresponding
    parent candle in M15, H1, H4, and D1.
    """

    def __init__(self, provider: HistoricalDataProvider | None = None) -> None:
        self._provider = provider or HistoricalDataProvider()

    def load(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        trend: str = "up",
        base_price: Decimal = Decimal("45000"),
    ) -> MultiTimeframeData:
        """
        Generate synchronized multi-TF data.

        1. Generate M5 candles using HistoricalDataProvider.
        2. Convert to DataFrame with DatetimeIndex.
        3. Resample/aggregate into M15, H1, H4, D1.

        Args:
            symbol: Trading pair (e.g. "BTC/USDT").
            start_date: Start datetime.
            end_date: End datetime.
            trend: "up", "down", or "sideways".
            base_price: Starting price for synthetic data.

        Returns:
            MultiTimeframeData with all five aligned DataFrames.
        """
        warnings.warn(
            "MultiTimeframeDataLoader.load() generates synthetic data using a simplified "
            "trending model. This data does not reflect real market microstructure, liquidity, "
            "or volatility regimes. Results obtained with synthetic data may not be "
            "representative of live trading performance. Use load_csv() with real OHLCV data "
            "for production backtesting.",
            UserWarning,
            stacklevel=2,
        )

        # Generate M5 candles as the finest resolution
        candles = self._provider.generate_trending_data(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            interval="5m",
            trend=trend,
            base_price=base_price,
        )

        # Convert to DataFrame with DatetimeIndex
        df_m5 = self._candles_to_dataframe(candles)

        # Resample upward
        df_m15 = self._resample(df_m5, "15min")
        df_h1 = self._resample(df_m5, "1h")
        df_h4 = self._resample(df_m5, "4h")
        df_d1 = self._resample(df_m5, "1D")

        return MultiTimeframeData(d1=df_d1, h4=df_h4, h1=df_h1, m15=df_m15, m5=df_m5)

    def load_csv(
        self,
        filepath: str,
        base_timeframe: str = "5m",
    ) -> MultiTimeframeData:
        """
        Load CSV data and build multi-timeframe DataFrames.

        Reads OHLCV CSV, creates DatetimeIndex, then resamples upward to
        fill all five timeframes. For timeframes finer than the base,
        the base data is used as-is (downsampling is impossible).

        Args:
            filepath: Path to CSV file.
            base_timeframe: Timeframe of the CSV data ("5m", "15m", "1h", etc.).

        Returns:
            MultiTimeframeData with all five DataFrames.
        """
        candles = self._provider.load_csv_data(filepath)
        df_base = self._candles_to_dataframe(candles)

        # Define the target timeframes in order from finest to coarsest
        tf_order = ["5m", "15m", "1h", "4h", "1d"]
        tf_minutes = {"5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        base_minutes = tf_minutes.get(base_timeframe.lower(), 5)

        frames: dict[str, pd.DataFrame] = {}
        for tf in tf_order:
            tf_min = tf_minutes[tf]
            if tf_min < base_minutes:
                # Can't downsample — use base data
                frames[tf] = df_base.copy()
            elif tf_min == base_minutes:
                frames[tf] = df_base.copy()
            else:
                rule = _TF_TO_RULE.get(tf, tf)
                frames[tf] = self._resample(df_base, rule)

        return MultiTimeframeData(
            d1=frames["1d"],
            h4=frames["4h"],
            h1=frames["1h"],
            m15=frames["15m"],
            m5=frames["5m"],
        )

    def _candles_to_dataframe(self, candles: list[dict]) -> pd.DataFrame:
        """Convert list-of-dicts candles to OHLCV DataFrame with DatetimeIndex."""
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
        df = df.sort_index()
        return df[["open", "high", "low", "close", "volume"]]

    def _resample(self, df: pd.DataFrame, rule: str) -> pd.DataFrame:
        """
        Resample data to a higher timeframe.

        Uses standard OHLCV aggregation:
          open: first, high: max, low: min, close: last, volume: sum

        Args:
            df: DataFrame with DatetimeIndex.
            rule: Pandas resample rule ('15min', '1h', '4h', '1D').

        Returns:
            Resampled DataFrame.
        """
        resampled = (
            df.resample(rule)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
        )
        return resampled

    def get_context_at(
        self,
        data: MultiTimeframeData,
        base_index: int | None = None,
        m15_index: int | None = None,
        lookback: int = 100,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Get rolling context DataFrames at a specific bar index.

        For each timeframe, returns the last `lookback` completed candles
        as of the base timestamp.

        Args:
            data: Full MultiTimeframeData.
            base_index: Current position in the finest-resolution DataFrame (m5).
                        If None, falls back to m15_index for backwards compat.
            m15_index: Legacy parameter — index into m15. Used when base_index is None.
            lookback: Number of historical bars to include per TF.

        Returns:
            (df_d1, df_h4, df_h1, df_m15, df_m5) tuple of rolling windows.
        """
        # Determine which DataFrame is the iteration base
        if base_index is not None:
            base_df = data.m5
            idx = base_index
        elif m15_index is not None:
            # Backwards compatibility: iterate m15
            base_df = data.m15
            idx = m15_index
        else:
            raise ValueError("Either base_index or m15_index must be provided")

        current_ts = base_df.index[idx]

        # Base TF: simple slice
        start = max(0, idx - lookback + 1)
        df_base = base_df.iloc[start : idx + 1]

        # All other TFs: use searchsorted (O(log n)) instead of boolean mask (O(n)).
        # Boolean mask on 50k-row DataFrame × 5 TFs × 47k bars = 11.75 billion ops.
        # searchsorted reduces this to O(5 × log(50k)) per bar — ~3000x faster.
        def _slice(df: pd.DataFrame) -> pd.DataFrame:
            pos = df.index.searchsorted(current_ts, side="right")
            return df.iloc[max(0, pos - lookback) : pos]

        df_m5 = _slice(data.m5)
        df_m15 = _slice(data.m15)
        df_h1 = _slice(data.h1)
        df_h4 = _slice(data.h4)
        df_d1 = _slice(data.d1)

        # If we iterated on m5, use our precise slice; otherwise use m15
        if base_index is not None:
            df_m5 = df_base
        else:
            df_m15 = df_base

        return (df_d1, df_h4, df_h1, df_m15, df_m5)
