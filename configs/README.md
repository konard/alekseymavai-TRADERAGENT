# Configs

## Активные конфигурации

| Файл | Назначение |
|------|-----------|
| `phase7_demo.yaml` | **Основной** — 5 production-ботов (Bybit Demo, $102k) |
| `strategy_routing.yaml` | **Единый** — правила маршрутизации (live + backtest) |
| `backtest_phase1.yaml` | Phase 1 baseline backtest конфиг (43 пары, $1k) |
| `smc.yaml` | SMC стратегия: swing_length, min_risk_reward, FVG/OB параметры |
| `trend_follower_production.yaml` | TrendFollower: EMA периоды, ATR multipliers |

## Ключевые параметры

### phase7_demo.yaml — production боты

```yaml
exchange:
  sandbox: true          # → api-demo.bybit.com (не testnet!)
  credentials_name: bybit_demo

risk_management:
  max_position_size: "3000"   # USD per strategy
  max_daily_loss: "600"       # USD (6% of $10k)

smc:
  swing_length: 10            # H1 swing detection
  min_risk_reward: 2.0        # Minimum R:R

dca:
  trigger_percentage: "0.04"  # -4% от текущей цены
  max_steps: 4
  take_profit_percentage: "0.08"  # +8%
```

### strategy_routing.yaml — маршрутизация

Используется и в `StrategySelector` (live), и в `StrategyRouter` (backtest):

```yaml
# Изменение здесь применяется в обоих местах
routes:
  - regime: bull_trend
    confluence_high: true
    strategies: [{name: trend_follower, weight: 0.7}, {name: dca, weight: 0.3}]
  - regime: bear_trend
    strategies: [{name: dca, weight: 1.0}]
  ...
```

## deprecated/

Устаревшие конфиги (заменены `phase7_demo.yaml`):
- `demo_trading.yaml` — старый demo конфиг (pre-v2.0)
- `example.yaml` — пример конфига v1
- `bybit_example.yaml` — пример Bybit конфига v1
