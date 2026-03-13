"""
Diagnostic script: count signal blocks for Grid and TrendFollower.

Runs on real ETH M5 data, mimics Phase 1 factory params, and reports
exactly how many bars are blocked at each filter stage.

Usage:
    python scripts/diagnose_signals.py \
        --csv data/historical/ETHUSDT_5m.csv \
        --bars 5000
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── helpers ─────────────────────────────────────────────────────────────────


def load_csv(csv_path: str, n_bars: int) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [c.lower() for c in df.columns]
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.tail(n_bars).reset_index(drop=True)
    print(f"Loaded {len(df)} bars  close range: ${df['close'].min():.0f}–${df['close'].max():.0f}")
    return df


# ─── GRID diagnostic ──────────────────────────────────────────────────────────


def diagnose_grid(df: pd.DataFrame, warmup: int = 50) -> None:
    from bot.strategies.grid_adapter import GridAdapter

    grid = GridAdapter(
        symbol="ETHUSDT",
        num_levels=10,
        profit_per_grid=Decimal("0.005"),
        grid_range_pct=Decimal("0.06"),
        recenter_cooldown_bars=6,
        adx_filter=25,
        amount_per_grid=Decimal("25"),  # $1000 * 25% / 10
    )

    stats = {
        "total_bars": 0,
        "no_grid_levels": 0,
        "adx_blocked": 0,
        "adx_nan": 0,
        "no_buy_levels_below": 0,
        "proximity_too_far": 0,
        "position_at_level": 0,
        "balance_insufficient": 0,
        "signal_generated": 0,
        "adx_values": [],
        "proximity_values": [],
    }

    balance = Decimal("1000")

    for i in range(warmup, len(df)):
        df_slice = df.iloc[: i + 1].copy()
        try:
            grid.analyze_market(df_slice)
        except Exception as e:
            print(f"  analyze_market ERROR bar {i}: {e}")
            continue

        stats["total_bars"] += 1

        # Step 1: grid levels
        if not grid._grid_levels:
            stats["no_grid_levels"] += 1
            continue

        close = Decimal(str(df_slice["close"].iloc[-1]))

        # Step 2: ADX filter
        adx_val = grid._compute_adx(df_slice)
        if adx_val != adx_val:  # NaN
            stats["adx_nan"] += 1
        else:
            stats["adx_values"].append(adx_val)
            if adx_val > 25:
                stats["adx_blocked"] += 1
                continue

        # Step 3: buy levels below price
        buy_levels = [lvl for lvl in grid._grid_levels if lvl < close]
        if not buy_levels:
            stats["no_buy_levels_below"] += 1
            continue

        nearest_buy = max(buy_levels)
        distance = float(abs(close - nearest_buy) / close)
        stats["proximity_values"].append(distance)

        # Step 4: proximity
        if distance > 0.005:
            stats["proximity_too_far"] += 1
            continue

        # Step 5: existing position
        has_pos = any(
            abs(pos["entry_price"] - nearest_buy) / nearest_buy < Decimal("0.003")
            for pos in grid._positions.values()
        )
        if has_pos:
            stats["position_at_level"] += 1
            continue

        # Step 6: balance
        if balance < grid._amount_per_grid:
            stats["balance_insufficient"] += 1
            continue

        stats["signal_generated"] += 1

    print("\n═══ GRID DIAGNOSTIC ═══")
    print(f"  Total bars processed : {stats['total_bars']}")
    print(f"  No grid levels       : {stats['no_grid_levels']}")
    print(f"  ADX NaN (insuf. data): {stats['adx_nan']}")

    if stats["adx_values"]:
        import statistics
        adx_pct_above = sum(1 for v in stats["adx_values"] if v > 25) / len(stats["adx_values"])
        print(f"  ADX blocked (>25)    : {stats['adx_blocked']}  "
              f"({adx_pct_above:.1%} of non-NaN bars)")
        print(f"  ADX stats            : "
              f"min={min(stats['adx_values']):.1f}  "
              f"median={statistics.median(stats['adx_values']):.1f}  "
              f"max={max(stats['adx_values']):.1f}")

    print(f"  No buy levels below  : {stats['no_buy_levels_below']}")

    if stats["proximity_values"]:
        pct_within = sum(1 for v in stats["proximity_values"] if v <= 0.005) / len(stats["proximity_values"])
        import statistics
        print(f"  Proximity too far    : {stats['proximity_too_far']}  "
              f"({1-pct_within:.1%} of bars with buy level)")
        print(f"  Proximity stats      : "
              f"min={min(stats['proximity_values']):.4f}  "
              f"median={statistics.median(stats['proximity_values']):.4f}  "
              f"max={max(stats['proximity_values']):.4f}")

    print(f"  Position at level    : {stats['position_at_level']}")
    print(f"  Balance insufficient : {stats['balance_insufficient']}")
    print(f"  ✅ Signals generated  : {stats['signal_generated']}")


# ─── TF diagnostic ────────────────────────────────────────────────────────────


def diagnose_tf(df: pd.DataFrame, warmup: int = 100) -> None:
    from bot.strategies.trend_follower.config import TrendFollowerConfig
    from bot.strategies.trend_follower.trend_follower_strategy import TrendFollowerStrategy
    from bot.strategies.trend_follower.market_analyzer import MarketPhase

    cfg = TrendFollowerConfig(
        ema_fast_period=20,
        ema_slow_period=50,
        atr_period=14,
        rsi_period=14,
        risk_per_trade_pct=Decimal("0.01"),
        require_volume_confirmation=False,
        max_atr_filter_pct=Decimal("0.15"),
        support_resistance_threshold=Decimal("0.03"),
        max_distance_from_ema_pct=Decimal("0.03"),
        min_sr_touches=1,
        ema_divergence_threshold=Decimal("0.001"),
        max_daily_loss_usd=Decimal("999999999"),
        max_positions=20,
    )
    tf = TrendFollowerStrategy(config=cfg, log_trades=False)

    stats = {
        "total_bars": 0,
        "phase_unknown": 0,
        "phase_sideways": 0,
        "phase_bullish": 0,
        "phase_bearish": 0,
        "atr_blocked": 0,
        "volume_blocked": 0,
        "no_corridor_match": 0,
        "no_sr_match": 0,
        "signal_generated": 0,
        "ema_divergence_values": [],
        "atr_pct_values": [],
    }

    for i in range(warmup, len(df)):
        df_slice = df.iloc[: i + 1].copy()
        stats["total_bars"] += 1

        try:
            conditions = tf.market_analyzer.analyze(df_slice)
        except Exception as e:
            print(f"  market_analyzer ERROR bar {i}: {e}")
            continue

        stats["ema_divergence_values"].append(float(conditions.ema_divergence_pct))
        stats["atr_pct_values"].append(float(conditions.atr_pct))

        # Step 1: market phase
        phase = conditions.phase
        if phase == MarketPhase.UNKNOWN:
            stats["phase_unknown"] += 1
            continue
        elif phase == MarketPhase.SIDEWAYS:
            stats["phase_sideways"] += 1
            # Sideways can also generate signals (RSI), count separately but don't skip
        elif phase == MarketPhase.BULLISH_TREND:
            stats["phase_bullish"] += 1
        elif phase == MarketPhase.BEARISH_TREND:
            stats["phase_bearish"] += 1

        # Step 2: ATR filter
        if conditions.atr_pct > cfg.max_atr_filter_pct:
            stats["atr_blocked"] += 1
            continue

        # Step 3: generate signal via entry_logic
        try:
            signal = tf.entry_logic.analyze_entry(df_slice)
        except Exception as e:
            print(f"  entry_logic ERROR bar {i}: {e}")
            continue

        if signal is not None:
            stats["signal_generated"] += 1

    print("\n═══ TREND FOLLOWER DIAGNOSTIC ═══")
    print(f"  Total bars processed : {stats['total_bars']}")
    print(f"  Phase UNKNOWN        : {stats['phase_unknown']}  "
          f"({stats['phase_unknown']/max(stats['total_bars'],1):.1%})")
    print(f"  Phase SIDEWAYS       : {stats['phase_sideways']}  "
          f"({stats['phase_sideways']/max(stats['total_bars'],1):.1%})")
    print(f"  Phase BULLISH_TREND  : {stats['phase_bullish']}  "
          f"({stats['phase_bullish']/max(stats['total_bars'],1):.1%})")
    print(f"  Phase BEARISH_TREND  : {stats['phase_bearish']}  "
          f"({stats['phase_bearish']/max(stats['total_bars'],1):.1%})")

    if stats["ema_divergence_values"]:
        import statistics
        print(f"  EMA divergence pct   : "
              f"min={min(stats['ema_divergence_values']):.5f}  "
              f"median={statistics.median(stats['ema_divergence_values']):.5f}  "
              f"max={max(stats['ema_divergence_values']):.5f}  "
              f"threshold=0.001")
        above = sum(1 for v in stats["ema_divergence_values"] if v >= 0.001)
        print(f"  EMA div >= threshold : {above}  ({above/max(len(stats['ema_divergence_values']),1):.1%})")

    if stats["atr_pct_values"]:
        import statistics
        above15 = sum(1 for v in stats["atr_pct_values"] if v > 0.15)
        print(f"  ATR blocked (>15%)   : {stats['atr_blocked']}  "
              f"({above15/max(len(stats['atr_pct_values']),1):.1%} > 15%)")
        print(f"  ATR pct stats        : "
              f"min={min(stats['atr_pct_values']):.4f}  "
              f"median={statistics.median(stats['atr_pct_values']):.4f}  "
              f"max={max(stats['atr_pct_values']):.4f}")

    print(f"  ✅ Signals generated  : {stats['signal_generated']}")


# ─── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="data/historical/ETHUSDT_5m.csv")
    parser.add_argument("--bars", type=int, default=5000)
    parser.add_argument("--strategy", choices=["grid", "tf", "both"], default="both")
    args = parser.parse_args()

    df = load_csv(args.csv, args.bars)

    if args.strategy in ("grid", "both"):
        diagnose_grid(df)

    if args.strategy in ("tf", "both"):
        diagnose_tf(df)


if __name__ == "__main__":
    main()
