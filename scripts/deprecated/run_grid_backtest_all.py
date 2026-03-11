#!/usr/bin/env python3
"""
Grid Backtesting Pipeline — All 45 Pairs.

Runs Grid Backtesting System full pipeline on all available USDT pairs:
  classify → optimize → stress test → export presets

Output: data/backtest_results/batch_<timestamp>/
  - Full JSON report per symbol
  - YAML presets for profitable configurations
  - Summary CSV ranking all pairs

Usage:
    python scripts/run_grid_backtest_all.py [--symbols BTC,ETH,SOL] [--last-candles 4320]
    python scripts/run_grid_backtest_all.py --data-dir /app/data/historical  # Docker
"""

import argparse
import csv
import gc
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
    OptimizationObjective,
    GridBacktestSystem,
)

# ─── Paths ─────────────────────────────────────────────────────────────────
# Default data dir — overridden by --data-dir
HIST_DIR = Path("/home/hive/btc/data/historical")
OUTPUT_BASE = Path(__file__).resolve().parent.parent / "data" / "backtest_results"

# ─── Default base config (Bybit linear futures fees) ───────────────────────
BASE_CONFIG = GridBacktestConfig(
    initial_balance=Decimal("10000"),
    maker_fee=Decimal("0.001"),   # 0.1%
    taker_fee=Decimal("0.001"),   # 0.1%
    stop_loss_pct=Decimal("0.15"),
    max_drawdown_pct=Decimal("0.25"),
)


def discover_symbols() -> list[str]:
    """Find all symbols with 1h data in HIST_DIR."""
    symbols = []
    for f in sorted(HIST_DIR.glob("*_1h.csv")):
        sym = f.stem.replace("_1h", "")
        symbols.append(sym)
    return symbols


def load_candles(symbol: str, timeframe: str = "1h", last_n: int = 0) -> pd.DataFrame:
    """Load OHLCV from CSV, optionally trimming to last N candles."""
    path = HIST_DIR / f"{symbol}_{timeframe}.csv"
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path)
    # Normalize column names (handle both cases)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
    # Rename 'open_time' if present
    if "open_time" in df.columns and "timestamp" not in df.columns:
        df.rename(columns={"open_time": "timestamp"}, inplace=True)

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            return pd.DataFrame()
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"])

    if last_n > 0 and len(df) > last_n:
        df = df.iloc[-last_n:].reset_index(drop=True)

    return df


def analyze_price(df: pd.DataFrame) -> dict:
    """Quick price analysis for a symbol's candles."""
    if df.empty:
        return {}
    start = float(df.iloc[0]["close"])
    end = float(df.iloc[-1]["close"])
    high = float(df["close"].max())
    low = float(df["close"].min())
    change_pct = (end - start) / start * 100 if start else 0
    return {
        "start_price": round(start, 4),
        "end_price": round(end, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "change_pct": round(change_pct, 2),
        "candles": len(df),
    }


def main():
    parser = argparse.ArgumentParser(description="Grid Backtest — All 45 Pairs")
    parser.add_argument("--data-dir", type=str, default="",
                        help="Path to historical CSV directory")
    parser.add_argument("--output-dir", type=str, default="",
                        help="Path to output directory")
    parser.add_argument("--symbols", type=str, default="",
                        help="Comma-separated symbols (default: all)")
    parser.add_argument("--last-candles", type=int, default=4320,
                        help="Use last N 1h candles (default: 4320 = ~6 months)")
    parser.add_argument("--objective", type=str, default="sharpe",
                        choices=["sharpe", "roi", "calmar", "profit_factor"],
                        help="Optimization objective (default: sharpe)")
    parser.add_argument("--coarse-steps", type=int, default=3,
                        help="Coarse optimization steps (default: 3)")
    parser.add_argument("--fine-steps", type=int, default=3,
                        help="Fine optimization steps (default: 3)")
    args = parser.parse_args()

    global HIST_DIR, OUTPUT_BASE
    if args.data_dir:
        HIST_DIR = Path(args.data_dir)
    if args.output_dir:
        OUTPUT_BASE = Path(args.output_dir)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = OUTPUT_BASE / f"batch_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    presets_dir = out_dir / "presets"
    presets_dir.mkdir(exist_ok=True)

    obj_map = {
        "sharpe": OptimizationObjective.SHARPE,
        "roi": OptimizationObjective.ROI,
        "calmar": OptimizationObjective.CALMAR,
        "profit_factor": OptimizationObjective.PROFIT_FACTOR,
    }
    objective = obj_map[args.objective]

    # ── Discover symbols ───────────────────────────────────────────────
    all_symbols = discover_symbols()
    if args.symbols:
        requested = [s.strip().upper() + "USDT" if "USDT" not in s.strip().upper()
                     else s.strip().upper() for s in args.symbols.split(",")]
        symbols = [s for s in requested if s in all_symbols]
        if not symbols:
            print(f"ERROR: None of {requested} found. Available: {all_symbols[:10]}...")
            sys.exit(1)
    else:
        symbols = all_symbols

    print("=" * 80)
    print("TRADERAGENT — Grid Backtesting Pipeline (Batch)")
    print("=" * 80)
    print(f"  Symbols:    {len(symbols)} pairs")
    print(f"  Timeframe:  1h (last {args.last_candles} candles ≈ {args.last_candles // 720} months)")
    print(f"  Objective:  {args.objective.upper()}")
    print(f"  Opt steps:  coarse={args.coarse_steps}, fine={args.fine_steps}")
    print(f"  Output:     {out_dir}")
    print(f"  Started:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    system = GridBacktestSystem()
    summary_rows = []
    total_start = time.perf_counter()

    for idx, symbol in enumerate(symbols, 1):
        print(f"[{idx:2d}/{len(symbols)}] {symbol}...", end=" ", flush=True)
        t0 = time.perf_counter()

        # Load data
        candles = load_candles(symbol, "1h", last_n=args.last_candles)
        if len(candles) < 100:
            print(f"SKIP (only {len(candles)} candles)")
            summary_rows.append({
                "symbol": symbol, "status": "skipped",
                "reason": f"only {len(candles)} candles",
            })
            continue

        price_info = analyze_price(candles)

        # Run full pipeline
        try:
            report = system.run_full_pipeline(
                symbols=[symbol],
                candles_map={symbol: candles},
                base_config=GridBacktestConfig(
                    symbol=symbol,
                    initial_balance=BASE_CONFIG.initial_balance,
                    maker_fee=BASE_CONFIG.maker_fee,
                    taker_fee=BASE_CONFIG.taker_fee,
                    stop_loss_pct=BASE_CONFIG.stop_loss_pct,
                    max_drawdown_pct=BASE_CONFIG.max_drawdown_pct,
                ),
                objective=objective,
                coarse_steps=args.coarse_steps,
                fine_steps=args.fine_steps,
            )
        except Exception as e:
            elapsed = time.perf_counter() - t0
            print(f"ERROR ({elapsed:.1f}s): {e}")
            summary_rows.append({
                "symbol": symbol, "status": "error", "reason": str(e)[:100],
            })
            continue

        elapsed = time.perf_counter() - t0
        sym_data = report.get("per_symbol", {}).get(symbol, {})

        if "error" in sym_data:
            print(f"ERROR: {sym_data['error']} ({elapsed:.1f}s)")
            summary_rows.append({
                "symbol": symbol, "status": "error", "reason": sym_data["error"],
            })
            continue

        profile = sym_data.get("profile", {})
        opt = sym_data.get("optimization", {})
        best_result = opt.get("best_result", {})
        best_config = opt.get("best_config", {})
        stress = sym_data.get("stress_test", {})

        roi = best_result.get("total_return_pct", 0)
        sharpe = best_result.get("sharpe_ratio", 0)
        dd = best_result.get("max_drawdown_pct", 0)
        cycles = best_result.get("completed_cycles", 0)
        trades = best_result.get("total_trades", 0)
        risk_stop = best_result.get("stopped_by_risk", False)

        # Stress test summary
        stress_results = stress.get("results", [])
        stress_avg_roi = 0
        if stress_results:
            stress_avg_roi = sum(r.get("total_return_pct", 0) for r in stress_results) / len(stress_results)

        status_icon = "+" if roi > 0 else "-"
        risk_flag = " RISK-STOP" if risk_stop else ""
        print(f"[{status_icon}] ROI={roi:+7.2f}%  Sharpe={sharpe:+7.3f}  "
              f"DD={dd:5.2f}%  Cycles={cycles:4d}  "
              f"Cluster={profile.get('cluster', '?'):12s}  "
              f"({elapsed:.1f}s){risk_flag}")

        # Save individual report
        report_path = out_dir / f"{symbol}_report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        # Save preset if exists
        preset_yaml = sym_data.get("preset_yaml", "")
        if preset_yaml:
            preset_path = presets_dir / f"{symbol}.yaml"
            with open(preset_path, "w") as f:
                f.write(preset_yaml)

        # Free memory between symbols
        del candles, report
        gc.collect()

        # Append to summary
        summary_rows.append({
            "symbol": symbol,
            "status": "ok",
            "cluster": profile.get("cluster", ""),
            "atr_pct": profile.get("atr_pct", 0),
            "candles": price_info.get("candles", 0),
            "price_change_pct": price_info.get("change_pct", 0),
            "roi_pct": round(roi, 4),
            "sharpe": round(sharpe, 4),
            "max_dd_pct": round(dd, 4),
            "cycles": cycles,
            "trades": trades,
            "risk_stop": risk_stop,
            "levels": best_config.get("num_levels", 0),
            "profit_per_grid": best_config.get("profit_per_grid", 0),
            "spacing": best_config.get("spacing", ""),
            "stress_avg_roi": round(stress_avg_roi, 4),
            "stress_periods": stress.get("periods_tested", 0),
            "opt_trials": opt.get("total_trials", 0),
            "time_s": round(elapsed, 1),
        })

    total_elapsed = time.perf_counter() - total_start

    # ── Summary CSV ────────────────────────────────────────────────────
    csv_path = out_dir / "summary.csv"
    ok_rows = [r for r in summary_rows if r.get("status") == "ok"]
    if ok_rows:
        fieldnames = list(ok_rows[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            # Sort by ROI descending
            for row in sorted(ok_rows, key=lambda r: r.get("roi_pct", 0), reverse=True):
                writer.writerow(row)

    # ── Print Final Summary ────────────────────────────────────────────
    print()
    print("=" * 80)
    print("BATCH BACKTEST SUMMARY")
    print("=" * 80)
    print(f"  Total time:       {total_elapsed:.0f}s ({total_elapsed/60:.1f} min)")
    print(f"  Symbols tested:   {len(ok_rows)}/{len(symbols)}")
    print(f"  Skipped/errors:   {len(summary_rows) - len(ok_rows)}")
    print()

    if ok_rows:
        profitable = [r for r in ok_rows if r.get("roi_pct", 0) > 0]
        unprofitable = [r for r in ok_rows if r.get("roi_pct", 0) <= 0]

        print(f"  Profitable:       {len(profitable)}/{len(ok_rows)} "
              f"({100*len(profitable)/len(ok_rows):.0f}%)")
        print()

        # Top 10 by ROI
        by_roi = sorted(ok_rows, key=lambda r: r.get("roi_pct", 0), reverse=True)
        print("  TOP 10 by ROI:")
        print(f"  {'#':>3s} {'Symbol':12s} {'Cluster':12s} {'ROI%':>8s} {'Sharpe':>8s} "
              f"{'MaxDD%':>7s} {'Cycles':>6s} {'RiskStop':>8s}")
        print("  " + "─" * 70)
        for i, r in enumerate(by_roi[:10], 1):
            print(f"  {i:3d} {r['symbol']:12s} {r['cluster']:12s} "
                  f"{r['roi_pct']:+8.2f} {r['sharpe']:+8.3f} "
                  f"{r['max_dd_pct']:7.2f} {r['cycles']:6d} "
                  f"{'YES' if r['risk_stop'] else 'no':>8s}")

        # Bottom 5
        print()
        print("  BOTTOM 5 by ROI:")
        for i, r in enumerate(by_roi[-5:], 1):
            print(f"  {i:3d} {r['symbol']:12s} {r['cluster']:12s} "
                  f"{r['roi_pct']:+8.2f} {r['sharpe']:+8.3f} "
                  f"{r['max_dd_pct']:7.2f}")

        # By cluster
        print()
        print("  BY CLUSTER:")
        clusters = {}
        for r in ok_rows:
            c = r.get("cluster", "unknown")
            clusters.setdefault(c, []).append(r)
        for cluster, rows in sorted(clusters.items()):
            avg_roi = sum(r["roi_pct"] for r in rows) / len(rows)
            avg_sharpe = sum(r["sharpe"] for r in rows) / len(rows)
            prof_count = sum(1 for r in rows if r["roi_pct"] > 0)
            print(f"    {cluster:12s}: {len(rows):2d} pairs, "
                  f"avg ROI={avg_roi:+.2f}%, avg Sharpe={avg_sharpe:+.3f}, "
                  f"{prof_count}/{len(rows)} profitable")

    print()
    print(f"  Output directory: {out_dir}")
    print(f"  Summary CSV:      {csv_path}")
    print(f"  Presets:          {presets_dir}/ ({len(list(presets_dir.glob('*.yaml')))} files)")
    print()


if __name__ == "__main__":
    main()
