# TRADERAGENT — Анализ проекта

> Дата: 2026-03-07 · Версия: v2.0.0
> Кодовая база: ~100K+ LOC · 2197 тестов · Production: 5 ботов на Bybit Demo

---

## 1. Текущее состояние

TRADERAGENT — платформа алгоритмической торговли (Python 3.12, asyncio, PostgreSQL). Работает на демо-аккаунте Bybit с 5 активными ботами. Поддерживает 4 стратегии через единый `BaseStrategy` интерфейс.

### Производственные боты

| Бот | Стратегия | Пара | Статус |
|-----|-----------|------|--------|
| demo_btc_hybrid | Grid + DCA (Hybrid) | BTC/USDT | ✅ Running |
| demo_eth_grid | Grid | ETH/USDT | ✅ Running |
| demo_sol_dca | DCA | SOL/USDT | ✅ Running |
| demo_btc_trend | TrendFollower | BTC/USDT | ✅ Running |
| demo_btc_smc | SMC (H1+M5) | BTC/USDT | ✅ Running |

### Серверная инфраструктура

| Сервер | IP | Роль |
|--------|----|------|
| Production | 185.233.200.13 | Live-боты, Docker |
| Testing | 158.160.215.57 | pytest, backtest (.venv) |

---

## 2. Сильные стороны

### 2.1 Архитектура

- **Единый `BaseStrategy` интерфейс** — `analyze_market()`, `generate_signal()`, `update_positions()`, `open_position()`, `close_position()`. Замена стратегии не требует изменения оркестратора.
- **Адаптерный слой** (`*_adapter.py`) — полная изоляция внутренней логики стратегий. Один адаптер работает в live и backtest.
- **6-режимный детектор рынка** (`market_regime.py`) — bull/bear/tight_range/volatile/accumulation/distribution с ADX/EMA/BB/SMC-фазами.
- **TradingCore** (`bot/core/trading_core/`) — единое ядро конфигурации синхронизирует live и backtest параметры.
- **BacktestOrchestratorEngine V3.0** — оркестрированный бэктест: 4 стратегии в одном прогоне с per-strategy metrics.

### 2.2 Функциональность стратегий

- **Grid** — полная реализация: уровни, SL на границах сетки, `force_close_all` при деактивации. Phase 1 Sharpe до 5.13 (SANDUSDT).
- **DCA** — DCAStartupAnalyzer для catch-up, trailing stop, multi-step averaging. Phase 1 Sharpe до 6.36 (LDOUSDT).
- **SMC** — H1 структура (BOS/CHoCH, Order Blocks, FVG) + M5 вход. Исправлен критический баг (Session 47).
- **TrendFollower** — EMA/ATR/RSI с adaptive TP multipliers, partial close, trailing stop.

### 2.3 Тестирование и инфраструктура

- **2197 тестов** на тестовом сервере, 0 провалов
- **Phase 1 завершён** — 43/43 пар, 50k баров, `strategy_score_matrix.json`
- **`configs/backtest_phase1.yaml`** — standalone конфиг не зависит от live YAML
- `from_yaml_config()` читает параметры из live-конфига для backtest (P0.3)

---

## 3. Слабые стороны и технические долги

### 3.1 КРИТИЧЕСКИЙ: Расхождение роутинга Live ↔ Backtest

**Это главное препятствие для достоверного бэктеста.**

#### Live Bot (`bot_orchestrator.py:675–695`) — АДДИТИВНАЯ логика:
```
bull_trend  → Grid + TrendFollower + SMC (3 стратегии одновременно)
bear_trend  → Grid + DCA + TrendFollower + SMC (все 4)
tight_range → Grid
volatile    → Grid + SMC
```

#### Backtest StrategyRouter — ЭКСКЛЮЗИВНАЯ логика:
```
bull_trend  → TrendFollower ТОЛЬКО
bear_trend  → DCA ТОЛЬКО
volatile    → SMC ТОЛЬКО
остальное   → Grid
```

**Следствие**: Phase 1 результаты не отражают поведение живого бота. Выбрать единую модель:
- **Exclusive** (проще оптимизировать, текущий backtest) — рекомендуется для Phase 2
- **Additive** (реалистичнее для live) — сложнее анализировать per-strategy

**Рекомендация**: перевести live-бот на exclusive routing, синхронизировав с backtest.

---

### 3.2 КРИТИЧЕСКИЙ: TF и SMC — realized_pnl=0 в backtest

**Симптом**: Phase 1 — trades=1–159 для TF, trades=360–4971 для SMC, но `realized_pnl=0`.

**Корневая причина**: Нет `force_close_all()` для TF и SMC при деактивации роутером.

**Механизм**:
1. Роутер активирует TF в `bull_trend` → TF открывает позиции
2. Режим меняется → роутер деактивирует TF (`weight=0`)
3. TF-позиции остаются в `position_amounts[strat_name]`
4. `update_positions()` пропускается (weight=0) → позиции никогда не закрываются
5. Открытые позиции "съедают" баланс → DAILY LOSS LIMIT
6. `per_strategy_pnl["trend_follower"] = 0` (нет exits → нет pnl_delta)

**Только Grid** реализует `force_close_all()`. TF и SMC — нет.

**Фикс**: добавить `force_close_all()` в `TrendFollowerAdapter` и `SMCStrategyAdapter`.

---

### 3.3 ВАЖНЫЙ: `strat_trades` считает сигналы, не закрытые позиции

```python
# orchestrator_engine.py:636
if signal is not None:
    strat_trades[strat_name] += 1  # СИГНАЛ, не завершённая сделка
```

Результат: BATUSDT SMC trades=4971 (каждый бар сигнал), BNBUSDT grid=1983. Реальных закрытых сделок может быть в 10–100 раз меньше.

**Фикс**: считать `strat_trades` только при успешном закрытии позиции в `_handle_exits`.

---

### 3.4 ВАЖНЫЙ: win_rate — приближение, не реальный показатель

```python
def _calculate_win_rate_from_pnl(self, pnl: float, trades: int) -> float:
    return 100.0 if pnl > 0 else 0.0
```

100% win_rate означает только `sum(pnl) > 0`, не процент прибыльных сделок.

---

### 3.5 ВАЖНЫЙ: Cooldown расхождение

| Параметр | Live Bot | backtest_phase1.yaml | Дефолт OrchestratorBacktestConfig |
|---------|----------|---------------------|----------------------------------|
| cooldown | 600 сек = **2 бара M5** | 120 баров = **600 мин** | 120 баров ❌ |

Бэктест использует cooldown в 300 раз больше, чем live-бот. Это сильно искажает частоту переключений стратегий.

**Фикс**: `router_cooldown_bars=2` в `OrchestratorBacktestConfig` по умолчанию.

---

### 3.6 ВАЖНЫЙ: DCA DAILY LOSS LIMIT на 60% пар

Phase 1: `DAILY LOSS LIMIT REACHED` на ~26 из 43 пар. Причина: `$1000 × 6% = $60/день`. DCA открывает 4 позиции по $50 → $200 экспозиции. При любом дневном движении лимит достигается.

Live-бот: `$10,000 × 6% = $600` — тот же процент, но DCA работает корректно.

**Фикс**: для Phase 2 увеличить `initial_balance` до $10,000 или `max_daily_loss_pct` до 25%.

---

### 3.7 УМЕРЕННЫЙ: SMC параметры не унифицированы

| Параметр | YAML (live) | backtest_phase1.yaml |
|---------|-------------|---------------------|
| swing_length | 10 | 10 ✅ |
| min_risk_reward | 2.0 | не задан (дефолт 2.5) ❌ |
| require_volume | false | false ✅ |

---

### 3.8 УМЕРЕННЫЙ: HybridCoordinator не в backtest

`demo_btc_hybrid` (live) использует `HybridCoordinator` (Grid↔DCA через ADX). В бэктесте Grid и DCA работают как независимые стратегии через StrategyRouter. Результаты не сопоставимы с hybrid-ботом.

---

### 3.9 УМЕРЕННЫЙ: SMC генерирует сигнал каждый бар

Без throttling SMC создаёт тысячи сигналов за прогон, многие из которых дублируются. Нужен `generate_signal_every_n ≥ 12` для SMC.

---

### 3.10 МАЛЫЙ: Дублирование SMC реализаций

`bot/strategies/smc_adapter.py` использует `smartmoneyconcepts` библиотеку. `bot/core/smc/` — собственная реализация (BOS/CHoCH, swing detector). Они не связаны.

---

## 4. Конфликты Logic: Live Bot vs Backtest V2.0

| Параметр | Live Bot | Backtest V2.0 | Критичность |
|---------|----------|---------------|-------------|
| **Routing** | Additive (TF+SMC в bull+bear) | Exclusive (один режим → одна стратегия) | 🔴 КРИТИЧНО |
| **TF/SMC деактивация** | cancel_orders() без force_close | force_close_all отсутствует | 🔴 КРИТИЧНО |
| **Cooldown** | 600 сек = 2 бара M5 | 120 баров = 600 мин | 🟡 ВАЖНО |
| **Regime check** | 60 сек = ~0.2 бара | 12 баров = 60 мин | 🟡 ВАЖНО |
| **win_rate** | N/A (нет расчёта) | sum(pnl)>0 → 100% | 🟡 ВАЖНО |
| **trades counter** | N/A | сигналы (x10–100 завышение) | 🟡 ВАЖНО |
| **DCA daily loss** | $600 при $10k | $60 при $1k | 🟡 ВАЖНО |
| **SMC min_rr** | 2.0 (YAML) | 2.5 (hardcoded default) | 🟠 УМЕРЕННО |
| **HybridCoordinator** | Активен | Не реализован | 🟠 УМЕРЕННО |
| **SMC throttling** | Нет | Нет | 🟠 УМЕРЕННО |

---

## 5. Phase 1 Results — Summary

43/43 пар, 50k M5 баров (~173 дня), ~58 минут, тестовый сервер 158.160.215.57.

### Достоверные результаты (Grid + DCA работают корректно)

| Пара | Grid Sharpe | DCA Sharpe | Total PnL |
|------|-------------|------------|-----------|
| LDOUSDT | 4.93 | 6.36 | +$122 |
| SANDUSDT | 5.13 | 5.75 | +$23 |
| BCHUSDT | 4.66 | 2.99 | +$58 |
| XEMUSDT | 4.61 | 3.02 | +$57 |
| BATUSDT | 3.64 | 3.16 | +$20 |
| ZILUSDT | 2.48 | 3.26 | +$45 |
| SOLUSDT | 2.05 | 3.13 | +$48 |
| LTCUSDT | 1.83 | 3.06 | +$69 |
| BTCUSDT | -0.10 | 2.97 | +$68 |
| HBARUSDT | 0.26 | 3.28 | +$71 |

### Недостоверные (TF, SMC — баг force_close_all)

TF и SMC показывают `realized_pnl=0` на большинстве пар из-за незакрытых позиций при деактивации роутером.

---

## 6. Унификация параметров стратегий

| Параметр | Статус | Источник правды |
|---------|--------|-----------------|
| initial_balance | ✅ P0.2 | YAML |
| max_daily_loss_pct | ✅ P0.3 | YAML → backtest |
| maker/taker_fee | ✅ P0 | TradingCoreConfig |
| cooldown_bars | ⚠️ расхождение | backtest_phase1.yaml: 120 баров (неверно) |
| regime_check_every_n | ✅ P0.1 | backtest_phase1.yaml: 12 баров |
| max_position_pct | ✅ P0.2 | YAML |
| grid_params | ✅ | backtest_phase1.yaml |
| dca_params | ✅ | backtest_phase1.yaml |
| tf_params | ✅ | backtest_phase1.yaml |
| smc_params (min_rr) | ❌ расхождение | YAML: 2.0, backtest default: 2.5 |
| routing mode | ❌ расхождение | additive vs exclusive |
| force_close_all | ❌ отсутствует | нет в TF/SMC |
| HybridCoordinator | ❌ отсутствует | только в live |

---

## 7. Выводы и приоритеты

### P0 — Критические фиксы (блокируют Phase 2)

1. `force_close_all()` в TF и SMC адаптерах
2. `strat_trades` считать closed round-trips, не сигналы
3. Унифицировать routing (exclusive и в live-боте)
4. `router_cooldown_bars=2` в backtest (синхронизация с live)

### P1 — Phase 2 оптимизация (после P0)

5. Grid search параметров для топ-10 пар (Grid + DCA)
6. `initial_balance=$10,000` или `max_daily_loss_pct=0.25` для Phase 2

### P2 — Метрики и качество

7. `win_rate` — per-trade расчёт
8. SMC `generate_signal_every_n=12`
9. SMC `min_rr=2.0` в backtest конфиге

### P3 — Архитектурные улучшения

10. HybridCoordinator в backtest
11. Telegram прокси для production
12. Consolidate SMC реализации

---

*Связанные документы: [plan.md](plan.md) | [architecture.md](architecture.md)*
