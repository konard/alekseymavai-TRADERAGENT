#!/usr/bin/env python3
"""Bear market grid search — runs run_backtest_v2.py as subprocess for reliability."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Parameter grid: (label, yaml_overrides)
# We'll create temp YAML files for each config
GRID = [
    ("rr5_sig24_sw12", {"smc_min_risk_reward": 5.0, "smc_signal_every_n": 24, "smc_swing_length": 12, "smc_max_positions": 1, "smc_risk_per_trade": 0.02}),
    ("rr5_sig12_sw10", {"smc_min_risk_reward": 5.0, "smc_signal_every_n": 12, "smc_swing_length": 10, "smc_max_positions": 2, "smc_risk_per_trade": 0.02}),
    ("rr45_sig24_sw12", {"smc_min_risk_reward": 4.5, "smc_signal_every_n": 24, "smc_swing_length": 12, "smc_max_positions": 2, "smc_risk_per_trade": 0.02}),
    ("rr45_sig36_sw12", {"smc_min_risk_reward": 4.5, "smc_signal_every_n": 36, "smc_swing_length": 12, "smc_max_positions": 1, "smc_risk_per_trade": 0.02}),
    ("rr4_sig36_sw15", {"smc_min_risk_reward": 4.0, "smc_signal_every_n": 36, "smc_swing_length": 15, "smc_max_positions": 1, "smc_risk_per_trade": 0.025}),
    ("rr4_sig48_sw12", {"smc_min_risk_reward": 4.0, "smc_signal_every_n": 48, "smc_swing_length": 12, "smc_max_positions": 1, "smc_risk_per_trade": 0.03}),
    ("rr5_sig36_sw10", {"smc_min_risk_reward": 5.0, "smc_signal_every_n": 36, "smc_swing_length": 10, "smc_max_positions": 1, "smc_risk_per_trade": 0.025}),
    ("rr6_sig24_sw12", {"smc_min_risk_reward": 6.0, "smc_signal_every_n": 24, "smc_swing_length": 12, "smc_max_positions": 1, "smc_risk_per_trade": 0.02}),
    ("rr6_sig12_sw10", {"smc_min_risk_reward": 6.0, "smc_signal_every_n": 12, "smc_swing_length": 10, "smc_max_positions": 2, "smc_risk_per_trade": 0.015}),
    ("rr35_sig48_sw15", {"smc_min_risk_reward": 3.5, "smc_signal_every_n": 48, "smc_swing_length": 15, "smc_max_positions": 1, "smc_risk_per_trade": 0.03}),
    ("rr5_sig48_sw15", {"smc_min_risk_reward": 5.0, "smc_signal_every_n": 48, "smc_swing_length": 15, "smc_max_positions": 1, "smc_risk_per_trade": 0.02}),
    ("rr5_sig48_sw8", {"smc_min_risk_reward": 5.0, "smc_signal_every_n": 48, "smc_swing_length": 8, "smc_max_positions": 1, "smc_risk_per_trade": 0.025}),
]

SYMBOLS = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]
YAML_TEMPLATE = """
backtest:
  initial_balance: 1000
  warmup_bars: 500
  max_bars: 50000
  vpm_max_grid_loss_pct: 0.25
  routing_yaml: configs/strategy_routing_bear.yaml
orchestrator:
  enable_strategy_router: true
  router_cooldown_bars: 2
  regime_check_every_n: 12
  analyze_every_n: 1
  smc_generate_signal_every_n: {smc_signal_every_n}
risk:
  max_position_pct: 0.20
  max_position_pct_per_trade: 0.10
  max_daily_loss_pct: 0.12
  min_order_size: 5
grid:
  enabled: false
dca:
  enabled: false
trend_follower:
  enabled: false
smc:
  enabled: true
  swing_length: {smc_swing_length}
  risk_per_trade: {smc_risk_per_trade}
  min_risk_reward: {smc_min_risk_reward}
  max_positions: {smc_max_positions}
  require_volume_confirmation: false
  debug_mode: false
  log_all_signals: false
"""


def run_backtest(symbol: str, label: str, params: dict) -> dict:
    """Run backtest via subprocess."""
    # Create temp YAML
    yaml_content = YAML_TEMPLATE.format(**params)
    yaml_path = Path(f"/tmp/bear_gs_{label}.yaml")
    yaml_path.write_text(yaml_content)

    out_dir = f"results/bear_gs/{label}_{symbol}"

    cmd = [
        sys.executable, "scripts/run_backtest_v2.py",
        "--mode", "single",
        "--symbol", symbol,
        "--config", str(yaml_path),
        "--data-dir", "data/historical",
        "--max-bars", "50000",
        "--initial-balance", "1000",
        "--phases", "1",
        "--output-dir", out_dir,
    ]

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return {
            "symbol": symbol, "label": label,
            "return_pct": 0.0, "sharpe": 0.0, "trades": 0,
            "elapsed_s": round(time.time() - t0, 1),
            "error": "TIMEOUT",
            **params,
        }
    elapsed = time.time() - t0

    # Parse result from stdout
    output = proc.stdout + proc.stderr
    m = re.search(r"return=(-?\d+\.?\d*)%.*sharpe=(-?\d+\.?\d*).*trades=(\d+)", output)
    if m:
        return {
            "symbol": symbol,
            "label": label,
            "return_pct": float(m.group(1)),
            "sharpe": float(m.group(2)),
            "trades": int(m.group(3)),
            "elapsed_s": round(elapsed, 1),
            **params,
        }
    return {
        "symbol": symbol,
        "label": label,
        "return_pct": 0.0,
        "sharpe": 0.0,
        "trades": 0,
        "elapsed_s": round(elapsed, 1),
        "error": output[-500:] if proc.returncode else "no match",
        **params,
    }


def main():
    Path("results/bear_gs").mkdir(parents=True, exist_ok=True)
    results = []
    total = len(GRID) * len(SYMBOLS)
    done = 0

    for label, params in GRID:
        for sym in SYMBOLS:
            done += 1
            t0 = time.time()
            r = run_backtest(sym, label, params)
            results.append(r)
            sign = "+" if r["return_pct"] > 0 else ""
            err = r.get("error", "")
            status = f"ERR:{err[:50]}" if err else f"ret={sign}{r['return_pct']:.2f}% sharpe={r['sharpe']:.3f} trades={r['trades']}"
            print(f"  [{done}/{total}] {sym:8s} {label:25s} {status} ({r['elapsed_s']:.0f}s)", flush=True)

    # Save
    with open("results/bear_gs/grid_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Summary
    print("\n" + "=" * 110, flush=True)
    print("SUMMARY — sorted by average return", flush=True)
    print("=" * 110, flush=True)

    by_label = {}
    for r in results:
        if not r.get("error"):
            by_label.setdefault(r["label"], []).append(r)

    ranked = []
    for label, runs in by_label.items():
        if len(runs) < 2:
            continue
        avg_ret = sum(r["return_pct"] for r in runs) / len(runs)
        avg_sharpe = sum(r["sharpe"] for r in runs) / len(runs)
        total_trades = sum(r["trades"] for r in runs)
        ranked.append((avg_ret, avg_sharpe, total_trades, label, runs))

    ranked.sort(key=lambda x: x[0], reverse=True)

    print(f"{'Label':30s} {'Avg Ret':>8s} {'Sharpe':>8s} {'Trades':>7s}  Details", flush=True)
    print("-" * 110, flush=True)
    for avg_ret, avg_sharpe, total_trades, label, runs in ranked:
        sign = "+" if avg_ret > 0 else ""
        details = " | ".join(f"{r['symbol'][:3]}:{'+' if r['return_pct']>0 else ''}{r['return_pct']:.2f}%" for r in runs)
        print(f"{label:30s} {sign}{avg_ret:7.2f}% {avg_sharpe:7.3f} {total_trades:7d}  {details}", flush=True)

    if ranked:
        best = ranked[0]
        print(f"\nBEST: {best[3]} → avg return {best[0]:+.2f}%, Sharpe {best[1]:.3f}", flush=True)


if __name__ == "__main__":
    main()
