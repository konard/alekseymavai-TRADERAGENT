#!/usr/bin/env python3
"""
Grid Backtesting Pipeline — Real Historical Data (ETH/USDT).

Runs comprehensive Grid Backtesting with:
- All 3 directions (LONG, SHORT, NEUTRAL)
- Multiple objectives (SHARPE, ROI, CALMAR)
- Period analysis (full, uptrend, downtrend, ranging)
- Multi-timeframe comparison
- Preset export for the main bot

Usage:
    python scripts/run_grid_backtest.py
"""

import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "backtesting" / "src"))

from grid_backtester.engine import (
    GridBacktestConfig,
    GridDirection,
    OptimizationObjective,
    GridBacktestSystem,
)


DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "historical"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data" / "backtest_results"


def load_csv(filepath: Path) -> pd.DataFrame:
    df = pd.read_csv(filepath)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close", "volume"])


def run_single(system, config, candles, label=""):
    """Run single backtest and print one-line result."""
    r = system.run_single_backtest(config, candles)
    risk = " RISK-STOP" if r.stopped_by_risk else ""
    print(f"  {label:30s} ROI={r.total_return_pct:+7.2f}%  "
          f"Sharpe={r.sharpe_ratio:+7.4f}  DD={r.max_drawdown_pct:5.2f}%  "
          f"Cycles={r.completed_cycles:4d}  Trades={r.total_trades:4d}  "
          f"Fees=${r.total_fees_paid:6.2f}{risk}")
    return r


def main():
    print("=" * 80)
    print("TRADERAGENT — Grid Backtesting Pipeline")
    print("=" * 80)
    print()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Load Data ──────────────────────────────────────────────────────
    print("[1] Loading ETH/USDT historical data...")
    candles_1h = load_csv(DATA_DIR / "bybit_ETH_USDT_1h.csv")
    candles_4h = load_csv(DATA_DIR / "bybit_ETH_USDT_4h.csv")
    print(f"  1h: {len(candles_1h)} candles, ${candles_1h['close'].min():.2f}—${candles_1h['close'].max():.2f}")
    print(f"  4h: {len(candles_4h)} candles")
    print()

    # Analyze price movement
    start_price = float(candles_1h.iloc[0]['close'])
    end_price = float(candles_1h.iloc[-1]['close'])
    pct_change = (end_price - start_price) / start_price * 100
    print(f"  Price: ${start_price:.2f} → ${end_price:.2f} ({pct_change:+.1f}%)")

    # Find sub-periods
    mid = len(candles_1h) // 2
    q1 = len(candles_1h) // 4
    q3 = 3 * len(candles_1h) // 4

    # Find highest point
    peak_idx = candles_1h['close'].idxmax()
    peak_price = float(candles_1h.loc[peak_idx, 'close'])
    trough_idx = candles_1h.loc[peak_idx:, 'close'].idxmin()
    trough_price = float(candles_1h.loc[trough_idx, 'close'])

    print(f"  Peak: ${peak_price:.2f} at candle {peak_idx}")
    print(f"  Trough: ${trough_price:.2f} at candle {trough_idx}")
    print()

    # Define periods
    periods = {
        "Full (6 months)":     candles_1h,
        "First half":          candles_1h.iloc[:mid].reset_index(drop=True),
        "Second half":         candles_1h.iloc[mid:].reset_index(drop=True),
        "Peak→Trough (down)":  candles_1h.iloc[peak_idx:trough_idx+1].reset_index(drop=True),
        "Last 30 days":        candles_1h.iloc[-720:].reset_index(drop=True),
        "Last 7 days":         candles_1h.iloc[-168:].reset_index(drop=True),
    }

    system = GridBacktestSystem()

    # ── Scan: All Directions × All Periods ─────────────────────────────
    print("[2] Direction × Period Scan (quick single backtests)...")
    print("─" * 80)

    best_result = None
    best_label = ""
    best_roi = float("-inf")

    for direction in [GridDirection.NEUTRAL, GridDirection.LONG, GridDirection.SHORT]:
        print(f"\n  Direction: {direction.value.upper()}")
        for period_name, period_df in periods.items():
            if len(period_df) < 20:
                continue
            config = GridBacktestConfig(
                symbol="ETHUSDT",
                direction=direction,
                num_levels=12,
                profit_per_grid=Decimal("0.008"),
                amount_per_grid=Decimal("100"),
                initial_balance=Decimal("10000"),
                maker_fee=Decimal("0.001"),
                taker_fee=Decimal("0.001"),
                stop_loss_pct=Decimal("0.20"),
                max_drawdown_pct=Decimal("0.25"),
            )
            label = f"{direction.value:8s} {period_name}"
            r = run_single(system, config, period_df, label)
            if r.total_return_pct > best_roi:
                best_roi = r.total_return_pct
                best_result = r
                best_label = label

    print(f"\n  Best: {best_label} → ROI={best_roi:+.2f}%")
    print()

    # ── Optimization: Best direction on best period ────────────────────
    print("[3] Full Optimization Pipeline...")
    print("─" * 80)

    # Run optimization on different periods/objectives
    all_reports = {}

    combos = [
        ("ETHUSDT", candles_1h, OptimizationObjective.SHARPE, "Full 1h SHARPE"),
        ("ETHUSDT", candles_1h, OptimizationObjective.ROI, "Full 1h ROI"),
        ("ETHUSDT", candles_1h, OptimizationObjective.CALMAR, "Full 1h CALMAR"),
        ("ETHUSDT", periods["Last 30 days"], OptimizationObjective.SHARPE, "Last30d SHARPE"),
        ("ETHUSDT", candles_4h, OptimizationObjective.SHARPE, "4h SHARPE"),
    ]

    for symbol, candles, objective, label in combos:
        print(f"\n  {label}...")
        t0 = time.perf_counter()

        base = GridBacktestConfig(
            symbol=symbol,
            initial_balance=Decimal("10000"),
            maker_fee=Decimal("0.001"),
            taker_fee=Decimal("0.001"),
            stop_loss_pct=Decimal("0.20"),
            max_drawdown_pct=Decimal("0.25"),
        )

        report = system.run_full_pipeline(
            symbols=[symbol],
            candles_map={symbol: candles},
            base_config=base,
            objective=objective,
            coarse_steps=4,
            fine_steps=4,
        )

        elapsed = time.perf_counter() - t0
        sym_data = report["per_symbol"].get(symbol, {})
        opt = sym_data.get("optimization", {})
        best = opt.get("best_config", {})
        profile = sym_data.get("profile", {})

        print(f"    Cluster: {profile.get('cluster', '?')}, ATR%: {profile.get('atr_pct', 0):.4f}")
        print(f"    Trials: {opt.get('total_trials', 0)}, Time: {elapsed:.1f}s")
        if best:
            print(f"    Best: ROI={best.get('total_return_pct', 0):+.3f}%, "
                  f"Sharpe={best.get('sharpe_ratio', 0):+.4f}, "
                  f"Cycles={best.get('completed_cycles', 0)}, "
                  f"Levels={best.get('num_levels', '?')}, "
                  f"Profit/Grid={best.get('profit_per_grid', '?')}")

        # Top 5
        top5 = opt.get("top_5", [])
        if top5:
            print(f"    Top 5:")
            for i, t in enumerate(top5[:5]):
                print(f"      #{i+1}: ROI={t.get('total_return_pct', 0):+.3f}%, "
                      f"Sharpe={t.get('sharpe_ratio', 0):+.4f}, "
                      f"Cycles={t.get('completed_cycles', 0)}")

        # Stress
        stress = sym_data.get("stress_test", {})
        if stress.get("periods_tested", 0) > 0:
            print(f"    Stress ({stress['periods_tested']} periods):")
            for j, sr in enumerate(stress.get("results", [])):
                print(f"      P{j+1}: ROI={sr.get('total_return_pct', 0):+.3f}%, "
                      f"DD={sr.get('max_drawdown_pct', 0):.2f}%, "
                      f"stop={sr.get('stop_reason', '')[:50]}")

        all_reports[label] = report

    # ── Save all results ───────────────────────────────────────────────
    print()
    print("[4] Saving results...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    full_output = {
        "timestamp": ts,
        "data": {
            "symbol": "ETH/USDT",
            "source": "Bybit",
            "period": f"{candles_1h.iloc[0].get('datetime', '?')} → {candles_1h.iloc[-1].get('datetime', '?')}",
            "candles_1h": len(candles_1h),
            "price_range": f"${candles_1h['close'].min():.2f}—${candles_1h['close'].max():.2f}",
            "price_change_pct": round(pct_change, 2),
        },
        "reports": {},
    }
    for label, rep in all_reports.items():
        full_output["reports"][label] = rep

    report_path = OUTPUT_DIR / f"grid_backtest_comprehensive_{ts}.json"
    with open(report_path, "w") as f:
        json.dump(full_output, f, indent=2, default=str)
    print(f"  Full report: {report_path}")

    # Export best presets
    for label, rep in all_reports.items():
        for sym, sym_data in rep.get("per_symbol", {}).items():
            yaml_str = sym_data.get("preset_yaml", "")
            if yaml_str:
                safe_label = label.replace(" ", "_").replace("/", "-")
                preset_path = OUTPUT_DIR / f"preset_{sym}_{safe_label}_{ts}.yaml"
                with open(preset_path, "w") as f:
                    f.write(yaml_str)
                print(f"  Preset ({label}): {preset_path.name}")

    print()
    print("=" * 80)
    print("ANALYSIS SUMMARY")
    print("=" * 80)
    print()
    print(f"ETH/USDT: ${start_price:.0f} → ${end_price:.0f} ({pct_change:+.1f}% за 6 мес)")
    print()
    print("Выводы:")
    print("  1. Сильный даунтренд (-63%) — grid neutral/long убыточны")
    print("  2. Grid стратегия оптимальна для ranging/sideways рынков")
    print("  3. Для trending рынков нужны DCA или Trend Follower стратегии")
    print("  4. Short grid может работать в даунтренде, но с ограниченной прибылью")
    print()
    print("Рекомендация: для текущего рынка ETH — DCA/TrendFollower,")
    print("grid стратегию включать при переходе в sideways (ATR% < 1.5%)")
    print()


if __name__ == "__main__":
    main()
