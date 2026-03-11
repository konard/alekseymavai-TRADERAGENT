#!/usr/bin/env python3
"""
XRP/USDT Grid Backtest — Full historical data, $100K deposit.

Runs: classify → optimize → stress test → export preset → save to DB.

NOTE: 7.8 years of XRP data spans $0.12–$3.65 (3000%+ price range).
Risk params are set very wide to let the grid strategy operate without
premature stop-loss triggers. The optimizer will find optimal grid params
within this wide risk envelope.
"""

import asyncio
import json
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

# Paths inside Docker container
PROJECT_ROOT = Path("/app")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "services" / "backtesting" / "src"))

from grid_backtester.engine import (
    GridBacktestConfig,
    GridBacktestSystem,
    GridDirection,
    OptimizationObjective,
)
from grid_backtester.persistence.preset_store import PresetStore

# ─── Config ──────────────────────────────────────────────────────────────
SYMBOL = "XRPUSDT"
DATA_FILE = Path("/data/historical/XRPUSDT_1h.csv")
OUTPUT_DIR = Path("/data/backtest_results")
PRESETS_DB = Path("/data/presets.db")

INITIAL_BALANCE = Decimal("100000")  # $100K USDT
MAKER_FEE = Decimal("0.001")        # 0.1% Bybit
TAKER_FEE = Decimal("0.001")

# Wide risk envelope for multi-year data (XRP moves 3000%+ over 7.8 years)
STOP_LOSS_PCT = Decimal("0.99")      # 99% — effectively disabled
MAX_DRAWDOWN_PCT = Decimal("0.90")   # 90% — very permissive


def load_csv(filepath: Path) -> pd.DataFrame:
    """Load and normalize OHLCV CSV (handles Binance column format)."""
    df = pd.read_csv(filepath)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    if "open_time" in df.columns and "timestamp" not in df.columns:
        df.rename(columns={"open_time": "timestamp"}, inplace=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    return df


def print_header(candles: pd.DataFrame):
    """Print data summary."""
    start_price = float(candles.iloc[0]["close"])
    end_price = float(candles.iloc[-1]["close"])
    pct = (end_price - start_price) / start_price * 100
    ts_start = candles.iloc[0].get("timestamp", "?")
    ts_end = candles.iloc[-1].get("timestamp", "?")

    print("=" * 80)
    print(f"  XRP/USDT Grid Backtest — Full Pipeline")
    print("=" * 80)
    print(f"  Data:      {len(candles)} 1h candles")
    print(f"  Period:    {ts_start} → {ts_end}")
    print(f"  Price:     ${start_price:.4f} → ${end_price:.4f} ({pct:+.1f}%)")
    print(f"  Range:     ${candles['close'].min():.4f} — ${candles['close'].max():.4f}")
    print(f"  Deposit:   ${INITIAL_BALANCE:,.0f} USDT")
    print(f"  Fees:      {MAKER_FEE*100}% maker / {TAKER_FEE*100}% taker")
    print(f"  Risk:      SL={STOP_LOSS_PCT*100}% / MaxDD={MAX_DRAWDOWN_PCT*100}%")
    print("=" * 80)
    print()


def run_direction_scan(system, candles):
    """Quick scan: 3 directions with default params on full data."""
    print("[1] Direction scan (default params, full period)...")
    print("-" * 80)

    results = {}
    for direction in [GridDirection.NEUTRAL, GridDirection.LONG, GridDirection.SHORT]:
        config = GridBacktestConfig(
            symbol=SYMBOL,
            direction=direction,
            num_levels=15,
            profit_per_grid=Decimal("0.008"),
            amount_per_grid=Decimal("1000"),  # $1K per grid level
            initial_balance=INITIAL_BALANCE,
            maker_fee=MAKER_FEE,
            taker_fee=TAKER_FEE,
            stop_loss_pct=STOP_LOSS_PCT,
            max_drawdown_pct=MAX_DRAWDOWN_PCT,
        )
        r = system.run_single_backtest(config, candles)
        risk = " RISK-STOP" if r.stopped_by_risk else ""
        print(f"  {direction.value:8s}  ROI={r.total_return_pct:+8.2f}%  "
              f"Sharpe={r.sharpe_ratio:+7.4f}  MaxDD={r.max_drawdown_pct:5.2f}%  "
              f"Cycles={r.completed_cycles:5d}  Trades={r.total_trades:5d}  "
              f"Fees=${r.total_fees_paid:8.2f}{risk}")
        results[direction.value] = r

    best_dir = max(results.items(), key=lambda x: x[1].sharpe_ratio)
    print(f"\n  Best direction by Sharpe: {best_dir[0].upper()}")
    print()
    return results


def run_optimization(system, candles):
    """Full optimization pipeline: classify → optimize → stress test."""
    print("[2] Full Optimization Pipeline (coarse=4, fine=4)...")
    print("-" * 80)

    base = GridBacktestConfig(
        symbol=SYMBOL,
        initial_balance=INITIAL_BALANCE,
        maker_fee=MAKER_FEE,
        taker_fee=TAKER_FEE,
        stop_loss_pct=STOP_LOSS_PCT,
        max_drawdown_pct=MAX_DRAWDOWN_PCT,
    )

    t0 = time.perf_counter()
    report = system.run_full_pipeline(
        symbols=[SYMBOL],
        candles_map={SYMBOL: candles},
        base_config=base,
        objective=OptimizationObjective.SHARPE,
        coarse_steps=4,
        fine_steps=4,
    )
    elapsed = time.perf_counter() - t0

    sym_data = report.get("per_symbol", {}).get(SYMBOL, {})
    profile = sym_data.get("profile", {})
    opt = sym_data.get("optimization", {})
    best_result = opt.get("best_result", {})
    best_config = opt.get("best_config", {})
    stress = sym_data.get("stress_test", {})
    preset_yaml = sym_data.get("preset_yaml", "")

    print(f"\n  Classification:")
    print(f"    Cluster:       {profile.get('cluster', '?')}")
    print(f"    ATR%:          {profile.get('atr_pct', 0):.4f}")
    print(f"    Volatility:    {profile.get('volatility_score', 0):.4f}")

    print(f"\n  Optimization ({elapsed:.0f}s, {opt.get('total_trials', 0)} trials):")
    print(f"    Best ROI:      {best_result.get('total_return_pct', 0):+.2f}%")
    print(f"    Sharpe:        {best_result.get('sharpe_ratio', 0):+.4f}")
    print(f"    Max DD:        {best_result.get('max_drawdown_pct', 0):.2f}%")
    print(f"    Cycles:        {best_result.get('completed_cycles', 0)}")
    print(f"    Win Rate:      {best_result.get('win_rate', 0):.2%}")
    print(f"    Profit Factor: {best_result.get('profit_factor', 0):.2f}")
    print(f"    Fees Paid:     ${best_result.get('total_fees_paid', 0):,.2f}")

    print(f"\n  Optimal Params:")
    print(f"    Levels:        {best_config.get('num_levels', '?')}")
    print(f"    Spacing:       {best_config.get('spacing', '?')}")
    print(f"    Profit/Grid:   {best_config.get('profit_per_grid', '?')}")
    print(f"    Direction:     {best_config.get('direction', '?')}")

    # Top 5
    top5 = opt.get("top_5", [])
    if top5:
        print(f"\n  Top 5 Configurations:")
        for i, t in enumerate(top5[:5], 1):
            print(f"    #{i}: ROI={t.get('total_return_pct', 0):+.2f}%  "
                  f"Sharpe={t.get('sharpe_ratio', 0):+.4f}  "
                  f"DD={t.get('max_drawdown_pct', 0):.2f}%  "
                  f"Cycles={t.get('completed_cycles', 0)}")

    # Stress test
    stress_results = stress.get("results", [])
    if stress_results:
        print(f"\n  Stress Test ({stress.get('periods_tested', 0)} volatile periods):")
        for j, sr in enumerate(stress_results):
            print(f"    P{j+1}: ROI={sr.get('total_return_pct', 0):+.2f}%  "
                  f"DD={sr.get('max_drawdown_pct', 0):.2f}%  "
                  f"Risk-stop={sr.get('stopped_by_risk', False)}")

    print()
    return report, sym_data, preset_yaml


def save_results(report, sym_data, preset_yaml, candles):
    """Save JSON report + YAML preset + SQLite preset."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # JSON report
    report_path = OUTPUT_DIR / f"XRPUSDT_backtest_{ts}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  Report:  {report_path}")

    # YAML preset
    if preset_yaml:
        preset_path = OUTPUT_DIR / f"XRPUSDT_preset_{ts}.yaml"
        with open(preset_path, "w") as f:
            f.write(preset_yaml)
        print(f"  Preset:  {preset_path}")

    # SQLite preset store
    opt = sym_data.get("optimization", {})
    best_result = opt.get("best_result", {})
    cluster = sym_data.get("profile", {}).get("cluster", "unknown")

    async def save_to_db():
        store = PresetStore(db_path=str(PRESETS_DB))
        await store.initialize()
        pid = await store.create(
            symbol=SYMBOL,
            config_yaml=preset_yaml or "# no preset generated",
            cluster=cluster,
            metrics=best_result,
        )
        presets = await store.list_presets()
        await store.close()
        return pid, presets

    pid, presets = asyncio.run(save_to_db())
    print(f"  DB:      {PRESETS_DB} (preset_id={pid})")
    print(f"  Library: {len(presets)} preset(s) total")


def main():
    # Load data
    candles = load_csv(DATA_FILE)
    print_header(candles)

    system = GridBacktestSystem(max_workers=2)

    # Step 1: Quick direction scan
    direction_results = run_direction_scan(system, candles)

    # Step 2: Full optimization
    report, sym_data, preset_yaml = run_optimization(system, candles)

    # Step 3: Save everything
    print("[3] Saving results...")
    print("-" * 80)
    save_results(report, sym_data, preset_yaml, candles)

    print()
    print("=" * 80)
    print("  DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
