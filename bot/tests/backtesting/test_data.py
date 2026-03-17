"""Historical data provider for backtesting"""

import random
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any


class HistoricalDataProvider:
    """
    Provides historical price data for backtesting.

    In a real implementation, this would fetch data from an exchange API
    or local database. For testing purposes, it generates realistic synthetic data.
    """

    def __init__(self):
        self.cache: dict[str, list[dict[str, Any]]] = {}

    def get_historical_prices(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str = "1h",
    ) -> list[dict[str, Any]]:
        """
        Get historical OHLCV candlestick data.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            start_date: Start date for data
            end_date: End date for data
            interval: Candle interval ("1m", "5m", "15m", "1h", "4h", "1d")

        Returns:
            List of candle dictionaries with OHLCV data
        """
        cache_key = f"{symbol}_{start_date}_{end_date}_{interval}"

        if cache_key in self.cache:
            return self.cache[cache_key]

        # Generate synthetic data
        data = self._generate_synthetic_data(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            interval=interval,
        )

        self.cache[cache_key] = data
        return data

    def _generate_synthetic_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str,
    ) -> list[dict[str, Any]]:
        """Generate realistic synthetic OHLCV data"""
        # Convert interval to timedelta
        interval_delta = self._parse_interval(interval)

        # Starting price
        base_price = Decimal("45000")  # BTC starting price
        current_price = base_price
        volatility = Decimal("0.02")  # 2% volatility

        candles = []
        current_time = start_date

        while current_time < end_date:
            # Generate price movement
            change_pct = Decimal(str(random.gauss(0, float(volatility))))
            price_change = current_price * change_pct

            open_price = current_price
            close_price = current_price + price_change

            # Generate high/low with some randomness
            high_offset = Decimal(str(random.uniform(0, float(volatility))))
            low_offset = Decimal(str(random.uniform(0, float(volatility))))

            high_price = max(open_price, close_price) * (Decimal("1") + high_offset)
            low_price = min(open_price, close_price) * (Decimal("1") - low_offset)

            # Generate volume
            volume = Decimal(str(random.uniform(100, 1000)))

            candle = {
                "timestamp": current_time,
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(volume),
            }

            candles.append(candle)

            # Update for next candle
            current_price = close_price
            current_time += interval_delta

        return candles

    def _parse_interval(self, interval: str) -> timedelta:
        """Parse interval string to timedelta"""
        value = int(interval[:-1])
        unit = interval[-1]

        if unit == "m":
            return timedelta(minutes=value)
        elif unit == "h":
            return timedelta(hours=value)
        elif unit == "d":
            return timedelta(days=value)
        elif unit == "w":
            return timedelta(weeks=value)
        else:
            raise ValueError(f"Invalid interval: {interval}")

    def load_csv_data(self, filepath: str) -> list[dict[str, Any]]:
        """
        Load historical data from CSV file.

        Supports CSV formats:
        1. timestamp (ISO datetime),open,high,low,close,volume
        2. timestamp (Unix ms),datetime,open,high,low,close,volume
        3. Open time (Unix ms),open,high,low,close,volume  (Binance)
        """
        import csv

        candles = []

        with open(filepath) as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Parse timestamp: prefer 'datetime', then 'timestamp', then 'Open time'
                if "datetime" in row and row["datetime"]:
                    ts = datetime.fromisoformat(row["datetime"])
                elif "Open time" in row:
                    raw_ts = row["Open time"]
                    try:
                        ts = datetime.fromisoformat(raw_ts)
                    except ValueError:
                        ts = datetime.utcfromtimestamp(int(raw_ts) / 1000)
                else:
                    raw_ts = row["timestamp"]
                    try:
                        ts = datetime.fromisoformat(raw_ts)
                    except ValueError:
                        # Unix milliseconds
                        ts = datetime.utcfromtimestamp(int(raw_ts) / 1000)

                candle = {
                    "timestamp": ts,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
                candles.append(candle)

        return candles

    def generate_trending_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        interval: str,
        trend: str = "up",  # "up", "down", or "sideways"
        base_price: Decimal = Decimal("45000"),
    ) -> list[dict[str, Any]]:
        """
        Generate synthetic data with a specific trend.

        Useful for testing strategy performance in different market conditions.
        """
        interval_delta = self._parse_interval(interval)

        # Estimate total bars to scale trend_strength adaptively.
        # Target: price triples (×3) over the full series for "up" trend.
        total_seconds = (end_date - start_date).total_seconds()
        est_bars = max(int(total_seconds / interval_delta.total_seconds()), 1)

        current_price = base_price
        volatility = Decimal("0.003")  # 0.3% volatility per bar

        # Adaptive trend: ln(3)/est_bars per bar → price ×3 over full series.
        import math

        _drift_per_bar = Decimal(str(math.log(3) / est_bars))
        if trend == "up":
            trend_strength = _drift_per_bar
        elif trend == "down":
            trend_strength = -_drift_per_bar
        else:  # sideways
            trend_strength = Decimal("0")

        candles = []
        current_time = start_date

        while current_time < end_date:
            # Apply trend
            trend_change = current_price * trend_strength
            # Add random volatility
            random_change = current_price * Decimal(str(random.gauss(0, float(volatility))))
            total_change = trend_change + random_change

            open_price = current_price
            close_price = current_price + total_change

            # Generate high/low
            high_offset = Decimal(str(abs(random.gauss(0, float(volatility) / 2))))
            low_offset = Decimal(str(abs(random.gauss(0, float(volatility) / 2))))

            high_price = max(open_price, close_price) * (Decimal("1") + high_offset)
            low_price = min(open_price, close_price) * (Decimal("1") - low_offset)

            volume = Decimal(str(random.uniform(100, 1000)))

            candle = {
                "timestamp": current_time,
                "open": float(open_price),
                "high": float(high_price),
                "low": float(low_price),
                "close": float(close_price),
                "volume": float(volume),
            }

            candles.append(candle)

            current_price = close_price
            current_time += interval_delta

        return candles

    def clear_cache(self) -> None:
        """Clear cached data"""
        self.cache.clear()
