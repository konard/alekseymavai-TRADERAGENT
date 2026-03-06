"""
Tests for SMC M5 two-level entry: H1 structure → M5 precision entry.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pandas as pd

from bot.strategies.smc.config import SMCConfig
from bot.strategies.smc.market_structure import TrendDirection
from bot.strategies.smc.smc_strategy import SMCStrategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n: int = 200, trend: str = "up", base: float = 50000.0) -> pd.DataFrame:
    """Create a simple trending OHLCV DataFrame."""
    np.random.seed(42)
    if trend == "up":
        closes = base + np.cumsum(np.random.uniform(0, 100, n))
    elif trend == "down":
        closes = base - np.cumsum(np.random.uniform(0, 100, n))
    else:
        closes = base + np.random.uniform(-500, 500, n)

    df = pd.DataFrame(
        {
            "open":   closes * 0.999,
            "high":   closes * 1.005,
            "low":    closes * 0.995,
            "close":  closes,
            "volume": np.random.uniform(100, 1000, n),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="5min"),
    )
    return df


def _make_strategy(swing_length_m5: int = 20, swing_length_h1: int = 10) -> SMCStrategy:
    config = SMCConfig(
        swing_length=swing_length_h1,
        swing_length_m5=swing_length_m5,
        swing_length_h1=swing_length_h1,
        m5_limit=1000,
        h1_limit=200,
        warmup_bars=0,  # disable warmup for tests
    )
    # bypass __post_init__ side-effect on warmup_bars
    object.__setattr__(config, "warmup_bars", 0)
    return SMCStrategy(config=config, account_balance=Decimal("10000"))


# ---------------------------------------------------------------------------
# 1. SMCConfig has the new fields
# ---------------------------------------------------------------------------

def test_smc_config_has_m5_fields():
    config = SMCConfig()
    assert hasattr(config, "swing_length_m5")
    assert hasattr(config, "swing_length_h1")
    assert hasattr(config, "m5_limit")
    assert hasattr(config, "h1_limit")


def test_smc_config_m5_defaults():
    config = SMCConfig()
    assert config.swing_length_m5 == 20
    assert config.swing_length_h1 == 10
    assert config.m5_limit == 1000
    assert config.h1_limit == 200


# ---------------------------------------------------------------------------
# 2. generate_signals_m5 method exists
# ---------------------------------------------------------------------------

def test_strategy_has_generate_signals_m5():
    strategy = _make_strategy()
    assert hasattr(strategy, "generate_signals_m5")
    assert callable(strategy.generate_signals_m5)


# ---------------------------------------------------------------------------
# 3. Empty DataFrames return empty list
# ---------------------------------------------------------------------------

def test_m5_returns_empty_for_empty_dfs():
    strategy = _make_strategy()
    result = strategy.generate_signals_m5(pd.DataFrame(), pd.DataFrame())
    assert result == []


# ---------------------------------------------------------------------------
# 4. Ranging H1 → no signals (no directional bias)
# ---------------------------------------------------------------------------

def test_no_entry_when_h1_is_ranging():
    strategy = _make_strategy()

    df_h1 = _make_df(n=100, trend="flat")
    df_m5 = _make_df(n=500, trend="flat")

    # Force H1 structure to return ranging
    with patch.object(
        strategy.market_structure,
        "analyze",
        return_value={"trend": TrendDirection.RANGING},
    ):
        result = strategy.generate_signals_m5(df_h1, df_m5)

    assert result == []


# ---------------------------------------------------------------------------
# 5. Swing length M5 is respected (config value flows through)
# ---------------------------------------------------------------------------

def test_swing_length_m5_respected():
    config = SMCConfig(swing_length_m5=30, warmup_bars=0)
    object.__setattr__(config, "warmup_bars", 0)
    strategy = SMCStrategy(config=config, account_balance=Decimal("10000"))

    assert strategy.config.swing_length_m5 == 30


# ---------------------------------------------------------------------------
# 6. In warmup → no signals
# ---------------------------------------------------------------------------

def test_no_signals_during_warmup():
    config = SMCConfig(warmup_bars=100, swing_length_m5=20)
    strategy = SMCStrategy(config=config, account_balance=Decimal("10000"))

    df_h1 = _make_df(n=200, trend="up")
    df_m5 = _make_df(n=500, trend="up")

    # Calling generate_signals_m5 once — still in warmup (call count 1 <= 100)
    result = strategy.generate_signals_m5(df_h1, df_m5)
    assert result == []


# ---------------------------------------------------------------------------
# 7. Adapter uses generate_signals_m5 when M5 cache available
# ---------------------------------------------------------------------------

def test_adapter_uses_m5_when_cached():
    from bot.strategies.smc_adapter import SMCStrategyAdapter

    config = SMCConfig(warmup_bars=0)
    object.__setattr__(config, "warmup_bars", 0)
    adapter = SMCStrategyAdapter(config=config, account_balance=Decimal("10000"))

    df_h1 = _make_df(n=200, trend="up")
    df_m5 = _make_df(n=500, trend="up")

    # Inject cached DFs
    adapter._cached_dfs = {"h1": df_h1, "m5": df_m5}

    with patch.object(
        adapter._strategy,
        "generate_signals_m5",
        return_value=[],
    ) as mock_m5:
        with patch.object(
            adapter._strategy,
            "generate_signals",
            return_value=[],
        ) as mock_legacy:
            adapter.generate_signal(df_m5, Decimal("10000"))

    mock_m5.assert_called_once()
    mock_legacy.assert_not_called()


# ---------------------------------------------------------------------------
# 8. Adapter falls back to legacy when no M5 cache
# ---------------------------------------------------------------------------

def test_adapter_falls_back_to_legacy_without_m5_cache():
    from bot.strategies.smc_adapter import SMCStrategyAdapter

    config = SMCConfig(warmup_bars=0)
    object.__setattr__(config, "warmup_bars", 0)
    adapter = SMCStrategyAdapter(config=config, account_balance=Decimal("10000"))

    df_h1 = _make_df(n=200, trend="up")
    df_m15 = _make_df(n=200, trend="up")

    # No M5 in cache (empty)
    adapter._cached_dfs = {"h1": df_h1, "m15": df_m15}

    with patch.object(adapter._strategy, "generate_signals_m5", return_value=[]) as mock_m5:
        with patch.object(adapter._strategy, "generate_signals", return_value=[]) as mock_legacy:
            adapter.generate_signal(df_m15, Decimal("10000"))

    mock_m5.assert_not_called()
    mock_legacy.assert_called_once()
