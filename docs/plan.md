# TRADERAGENT — План развития

> Дата: 2026-03-11 · Версия: v2.1.0
> Основан на: [analysis.md](analysis.md) | [SESSION_CONTEXT.md](SESSION_CONTEXT.md)
> Концепция: «Идеальный криптотрейдер» — SMC + трёхрежимная система + адаптивный риск-менеджмент

---

## Исполнительное резюме

TRADERAGENT v2.1 — платформа с 5 живыми ботами (~$102k), BacktestOrchestratorEngine V3.0 (Phase 1: 43/43 пар), и завершёнными P0-фиксами. Ключевая нерешённая задача — расхождение routing (additive live vs exclusive backtest) и отсутствие достоверных данных по TF/SMC стратегиям.

### Выполненные улучшения (v2.0 → v2.1)

| Улучшение | Компонент | Статус |
|-----------|-----------|--------|
| force_close_all() для TF и SMC | `TrendFollowerAdapter`, `SMCStrategyAdapter` | ✅ |
| strat_trades — закрытые сделки | `orchestrator_engine.py` | ✅ |
| router_cooldown_bars=2 | `OrchestratorBacktestConfig` | ✅ |
| Единый strategy_routing.yaml | `RoutingConfig` → live + backtest | ✅ |
| TradingCore (fees/cooldown/risk) | `bot/core/trading_core/` | ✅ |
| «Единый разумный трейдер» (#356-360) | SMCStructureAnalyzer, StrategyConductor и др. | ✅ |
| UnifiedBacktestEngine | `bot/tests/backtesting/unified_engine.py` | ✅ |
| VirtualPositionManager + CapitalArbiter | `orchestrator_engine.py` | ✅ |

---

## Направления развития

| # | Направление | Горизонт | Приоритет |
|---|-------------|---------|-----------|
| **A** | Диагностика и фикс SMC=0 сделок | Неделя 1 | 🔴 P0 |
| **B** | Phase 2: Оптимизация параметров | Неделя 2–3 | 🔴 P1 |
| **C** | Live↔Backtest: синхронизация routing | Неделя 1–2 | 🔴 P1 |
| **D** | Phase 3: Portfolio Backtest | Неделя 4 | 🟡 P2 |
| **E** | Production Deploy (Phase 8) | Неделя 5 | 🟡 P2 |
| **F** | AdaptiveRecoveryGrid | Неделя 6+ | 🟠 P3 |
| **G** | Web UI — equity curves, dashboard | Параллельно | 🟠 P3 |
| **H** | SMC advanced: on-chain, Order Flow | Будущее | 🟠 P3 |

---

## A. Диагностика SMC = 0 сделок 🔴 P0

### A1. Smoke-test SMC-only (приоритет)

**Цель**: выяснить, почему SMC не открывает позиции в backtest Phase 1.

**Гипотезы (проверить по порядку):**
1. `max_positions=3` при `initial_balance=$1k` → CapitalArbiter блокирует входы
2. `min_risk_reward=2.5` (default) слишком высок → мало сигналов
3. CapitalArbiter: `allocation["smc"]=0` в текущих режимах → 0 capital
4. SMC throttle: сигнал каждый бар → дубль → риск-чек падает

```bash
# Smoke-test: одна пара, SMC-only, детальные логи
python scripts/smoke_smc.py --symbol BTC/USDT --bars 3000 --balance 10000 \
  --min-rr 2.0 --swing-length 10 --verbose
```

**Ожидаемый результат**: SMC trades > 0 после фикса.

### A2. Фиксы по результатам диагностики

Вероятные фиксы:
- `smc_params["min_risk_reward"] = 2.0` в `backtest_phase1.yaml`
- `max_positions` → proportional к balance (`min(3, balance/3000)`)
- SMC `generate_signal_every_n=12` (hourly throttle) в конфиге
- CapitalArbiter: убедиться что SMC получает allocation в `volatile_transition` и `accumulation`

### A3. Re-run Phase 1 с фиксами

После A2: повторный прогон 43 пар для получения достоверных TF/SMC метрик.

---

## B. Phase 2 — Оптимизация параметров 🔴 P1

*Требует завершения A1, A2.*

### B1. Конфигурация Phase 2

```yaml
# configs/backtest_phase2.yaml
initial_balance: 10000          # Было $1k → теперь $10k
max_daily_loss_pct: 0.06        # 6% = $600 (реалистично)
router_cooldown_bars: 2         # Синхронизировано с live
regime_check_every_n: 12        # 1 час M5
```

### B2. Топ-10 пар для оптимизации

По Phase 1 (Grid Sharpe > 1.5 OR DCA Sharpe > 2.5):
```
LDOUSDT, SANDUSDT, BCHUSDT, XEMUSDT, BATUSDT,
ZILUSDT, SOLUSDT, LTCUSDT, HBARUSDT, BTCUSDT
```

### B3. Grid search — Grid стратегия

| Параметр | Значения | Комбинаций |
|---------|----------|-----------|
| `num_levels` | [4, 6, 8, 10] | 4 |
| `profit_per_grid` | [0.008, 0.010, 0.012, 0.016, 0.020] | 5 |
| **Итого** | 4 × 5 = **20** × 10 пар | **200 прогонов** |

### B4. Grid search — DCA стратегия

| Параметр | Значения | Комбинаций |
|---------|----------|-----------|
| `trigger_pct` | [0.03, 0.04, 0.05, 0.06] | 4 |
| `max_steps` | [3, 4, 5] | 3 |
| `take_profit_pct` | [0.06, 0.08, 0.10, 0.12] | 4 |
| **Итого** | 4 × 3 × 4 = **48** × 10 пар | **480 прогонов** |

### B5. Grid search — TrendFollower (после A1)

| Параметр | Значения | Комбинаций |
|---------|----------|-----------|
| `ema_fast_period` | [10, 20, 30] | 3 |
| `ema_slow_period` | [40, 50, 60] | 3 |
| `risk_per_trade_pct` | [0.005, 0.010, 0.015, 0.020] | 4 |
| **Итого** | 3 × 3 × 4 = **36** × 10 пар | **360 прогонов** |

### B6. Grid search — SMC (после A1)

| Параметр | Значения | Комбинаций |
|---------|----------|-----------|
| `swing_length` | [5, 10, 15, 20] | 4 |
| `min_risk_reward` | [1.5, 2.0, 2.5, 3.0] | 4 |
| **Итого** | 4 × 4 = **16** × 10 пар | **160 прогонов** |

### B7. Запуск Phase 2

```bash
python scripts/run_backtest_v2.py \
  --mode multi \
  --config configs/backtest_phase2.yaml \
  --pairs "LDOUSDT,SANDUSDT,BCHUSDT,XEMUSDT,BATUSDT,ZILUSDT,SOLUSDT,LTCUSDT,HBARUSDT,BTCUSDT" \
  --workers 8 \
  --phase optimize
```

**Целевые параметры по режимам рынка:**

| Режим | Grid | DCA | TrendFollower | SMC |
|-------|------|-----|---------------|-----|
| `bull_trend` | `profit=0.016` | — | `ema_fast=10, ema_slow=40` | `min_rr=2.0` |
| `bear_trend` | `profit=0.012` | `trigger=0.04, steps=5` | `ema_fast=20, ema_slow=60` | — |
| `tight_range` | `profit=0.008, levels=10` | — | — | — |
| `volatile` | — | — | — | `swing=5, min_rr=1.5` |

---

## C. Live↔Backtest: синхронизация routing 🔴 P1

### C1. Выбор единой модели маршрутизации

**Проблема**: Live — аддитивная логика (несколько стратегий одновременно), backtest — эксклюзивная.

**Решение**: перевести live-бот на exclusive routing:
```
bull_trend   → TrendFollower (основная) + Grid (защита)
bear_trend   → DCA (основная) + Grid (защита)
volatile     → SMC
tight_range  → Grid
accumulation → SMC
```

**Шаги**:
1. Добавить `routing_mode: exclusive` в `strategy_routing.yaml`
2. Обновить `StrategySelector._compute_target_strategies()` для поддержки режима
3. Тестировать `dry_run=true` 48 часов на production-боте

### C2. SMC параметры в backtest config

В `from_yaml_config()` добавить:
```python
smc_params["min_risk_reward"] = float(yaml_smc.get("min_risk_reward", 2.0))
```
Сейчас YAML: 2.0, default: 2.5 — расхождение устраняется.

### C3. Автоматические parity-тесты

Файл: `tests/integration/test_live_backtest_parity.py`

```python
assert backtest_config.router_cooldown_bars == live_config.cooldown_seconds / bar_seconds
assert backtest_config.max_daily_loss_pct == live_config.max_daily_loss_pct
assert backtest_config.maker_fee == live_config.maker_fee
assert backtest_config.smc_params["min_risk_reward"] == live_yaml["smc"]["min_risk_reward"]
```

### C4. regime_check синхронизация

Добавить `regime_check_interval_seconds` в `TradingCore`, из которого `backtest_engine` автоматически вычисляет `regime_check_every_n = interval_seconds / bar_duration_seconds`.

---

## D. Phase 3 — Portfolio Backtest 🟡 P2

*Требует завершения B.*

### D1. Выбор пар для портфеля

После Phase 2: топ-5 пар по Sharpe, с попарной корреляцией < 0.6.
Цель: Grid × 2 + DCA × 2 + TF/SMC × 1.

### D2. Запуск Portfolio Engine

```bash
python scripts/run_backtest_v2.py --mode portfolio \
  --pairs "LDOUSDT,SANDUSDT,BTCUSDT,ETHUSDT,SOLUSDT" \
  --capital 50000
```

SharedCapitalPool: $50k / 5 пар = $10k на пару.

### D3. Portfolio KPI

| Метрика | Целевое значение |
|---------|-----------------|
| Portfolio Sharpe | > 1.0 |
| Max Drawdown | < 20% |
| Win Rate | > 55% |
| Корреляция пар | < 0.6 (попарно) |

---

## E. Production Deploy (Phase 8) 🟡 P2

*Требует завершения D.*

### E1. Создание конфига Phase 8

```bash
cp configs/phase7_demo.yaml configs/phase8_optimized.yaml
# Внести оптимальные параметры из Phase 2
```

### E2. Dry-run перед деплоем

```bash
ssh ai-agent@185.233.200.13 \
  "docker compose exec bot python -m bot.main --config /app/configs/phase8_optimized.yaml --dry-run"
```

### E3. Деплой

```bash
tar czf /tmp/sync.tar.gz bot/ scripts/ configs/
scp /tmp/sync.tar.gz ai-agent@185.233.200.13:/tmp/
ssh ai-agent@185.233.200.13 "cd ~/TRADERAGENT && tar xzf /tmp/sync.tar.gz && docker compose restart bot"
```

### E4. KPI мониторинга

- Win rate > 55%, max daily drawdown < 10%
- Live metrics ≈ backtest прогноз ± 30%
- Нет аномалий в логах (Telegram alerts)

---

## F. AdaptiveRecoveryGrid (Future) 🟠 P3

**Концепция SMK**: Grid достигает нижней границы → DCA cascade до SMC-поддержки → TP при суммарной позиции +1%.

```
GridEngine.on_lower_boundary_hit()
  → SMC.get_nearest_support(current_price)
  → DCAEngine.start_cascade(smc_support)
  → CombinedPositionManager.check_combined_tp()
  → GridEngine.restart(new_range)
```

---

## G. Web UI 🟠 P3

Реализовано (`web/`): FastAPI backend, lightweight-charts.

**Планируемые улучшения**:
- Equity curves из backtest
- Live dashboard (WebSocket)
- Backtest comparison (тепловая карта Sharpe)

---

## H. SMC Advanced 🟠 P3

- On-chain метрики: Glassnode (NUPL, SOPR) для Long Hold позиций
- Volume Profile / VWAP: замена `require_volume=false` реальным подтверждением
- SMC throttling: `generate_signal_every_n=12` (hourly)

---

## Параметры риска по фазам рынка

| Параметр | Тренд | Боковик | Высокая волатильность |
|---------|-------|---------|----------------------|
| `max_position_pct` | 25% | 20% | 15% |
| `risk_per_trade_pct` | 1.0–1.5% | 0.5–1.0% | 0.5% |
| `max_daily_loss_pct` | 6% | 4% | 3% |
| `cooldown_seconds` | 600 | 600 | 1200 |
| `max_positions` | 2 | 4 | 1 |

---

## Дорожная карта

```
Неделя 1:  A1 (smoke-test SMC) → A2 (фиксы) → C2 (min_rr sync)
Неделя 2:  A3 (re-run Phase 1) → C1 (exclusive routing dry-run) → C3 (parity tests)
Неделя 3:  B1-B4 (Phase 2: Grid + DCA optimization, 680 прогонов)
Неделя 4:  B5-B6 (Phase 2: TF + SMC optimization, 520 прогонов)
Неделя 5:  D1-D3 (Phase 3 portfolio backtest)
Неделя 6:  E1-E4 (production deploy: Phase 8 configs)
Неделя 7+: F, G, H
```

---

## Критерии успеха v2.1

| Milestone | Критерий | Текущий статус |
|-----------|---------|----------------|
| SMC диагностика | SMC trades > 0 в smoke-test | 🔴 В работе |
| Phase 2 готов | Sharpe > 1.5 для топ-5 пар | ⏳ Неделя 3–4 |
| Routing синхронизирован | Live ≈ Backtest маршрутизация | ⏳ Неделя 1–2 |
| Phase 3 портфель | Portfolio Sharpe > 1.0, drawdown < 20% | ⏳ Неделя 5 |
| Production deploy | Live ≈ backtest ±30% | ⏳ Неделя 6 |
| AdaptiveRecoveryGrid | Меньше stop-loss vs чистый Grid | ⏳ Будущее |

---

*Связанные документы: [analysis.md](analysis.md) | [architecture_v2.md](architecture_v2.md) | [SESSION_CONTEXT.md](SESSION_CONTEXT.md)*
