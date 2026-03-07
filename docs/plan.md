# TRADERAGENT — План развития

> Дата: 2026-03-07 · Версия: v2.0.0
> На основе: [Анализ проекта](analysis.md) | [Архитектура](architecture.md)

---

## Обзор направлений

| Направление | Цель | Горизонт |
|-------------|------|---------|
| **A. Backtest Fix** | Достоверный P&L для TF и SMC | Неделя 1 |
| **B. Phase 2 Оптимизация** | Оптимальные параметры для топ-10 пар | Неделя 2–3 |
| **C. Live↔Backtest Sync** | Единая логика routing в live и backtest | Неделя 1–2 |
| **D. Phase 3 Портфель** | Multi-pair portfolio backtest | Неделя 4 |
| **E. Prod Deploy** | Обновить live-боты с оптимальными параметрами | Неделя 5 |
| **F. AdaptiveRecoveryGrid** | Grid→DCA cascade при пробое нижней границы | Неделя 6+ |
| **G. Web UI** | Equity curves, live dashboard | Параллельно |

---

## Направление A: Backtest Fix (P0 — блокирует всё остальное)

### Задачи по приоритету

**A1. force_close_all() для TF и SMC** 🔴 ПЕРВЫЙ
- Файлы: `bot/strategies/trend_follower_adapter.py`, `bot/strategies/smc_adapter.py`
- Логика: при деактивации роутером закрыть все открытые позиции по текущей цене
- Аналог: `grid_adapter.py:force_close_all()`
- Тест: добавить тест `test_force_close_all_tf_smc` в `test_multi_strategy_backtesting.py`
- Ожидаемый результат: TF/SMC realized_pnl ≠ 0

**A2. strat_trades — closed round-trips вместо сигналов** 🔴
- Файл: `bot/tests/backtesting/orchestrator_engine.py:636`
- Изменение: `strat_trades[strat_name] += 1` переместить в `_handle_exits()`
- Ожидаемый результат: BATUSDT SMC trades: 4971 → ~50 (реалистично)

**A3. router_cooldown_bars=2** 🟡
- Файл: `bot/tests/backtesting/orchestrator_engine.py` (дефолт) + `configs/backtest_phase1.yaml`
- Изменение: `router_cooldown_bars: 120` → `router_cooldown_bars: 2` (600 сек / 300 сек = 2 бара M5)
- Обоснование: live-бот cooldown = 600 сек = 2 M5 бара

**A4. Унифицировать routing: exclusive в live-боте** 🟡
- Файл: `bot/orchestrator/bot_orchestrator.py:675–695`
- Изменение: заменить аддитивную логику (TF+SMC добавляются) на exclusive (один режим → одна стратегия)
- Синхронизировать с `StrategyRouter._compute_target_strategies()`
- Риск: может изменить поведение live-ботов — тестировать в dry_run

**A5. win_rate — per-trade расчёт** 🟠
- Файл: `bot/tests/backtesting/orchestrator_engine.py:_calculate_win_rate_from_pnl`
- Изменение: трекать winning/losing трейды в `_handle_exits()` по-стратегийно

---

## Направление B: Phase 2 — Оптимизация параметров

**Требует**: A1, A2 завершены

### Задачи по приоритету

**B1. Определить топ-10 пар для оптимизации** 🔴
- Из Phase 1 results: LDOUSDT, SANDUSDT, BCHUSDT, XEMUSDT, BATUSDT, ZILUSDT, SOLUSDT, LTCUSDT, BTCUSDT, HBARUSDT
- Критерии: Grid Sharpe > 1.5 ИЛИ DCA Sharpe > 2.5

**B2. Grid search — Grid стратегия** 🔴
- Параметры: `num_levels` ∈ [4, 6, 8, 10], `profit_per_grid` ∈ [0.008, 0.012, 0.016, 0.02]
- Запуск: `--mode multi --phases 2` через `run_backtest_v2.py`
- Метрика оптимизации: Sharpe при `bars_active > 500`

**B3. Grid search — DCA стратегия** 🔴
- Параметры: `trigger_pct` ∈ [0.03, 0.04, 0.05], `max_steps` ∈ [3, 4, 5], `take_profit_pct` ∈ [0.06, 0.08, 0.10]

**B4. Grid search — TrendFollower** 🟡
- Параметры: `ema_fast` ∈ [10, 20, 30], `ema_slow` ∈ [40, 50, 60], `risk_per_trade_pct` ∈ [0.005, 0.01, 0.02]
- Требует: A1 завершён

**B5. Grid search — SMC** 🟡
- Параметры: `swing_length` ∈ [5, 10, 20], `min_risk_reward` ∈ [1.5, 2.0, 2.5]
- Требует: A1 завершён

**B6. initial_balance=$10,000 для Phase 2** 🟡
- Избежать ложных DAILY LOSS LIMIT срабатываний ($600 при $10k vs $60 при $1k)

---

## Направление C: Live↔Backtest Synchronization

**Задачи**

**C1. Exclusive routing в live-боте** 🔴
- Зависит от: A4

**C2. from_yaml_config() полная синхронизация** 🟡
- Добавить: `smc.min_risk_reward`, `tf.warmup_bars` в синхронизацию

**C3. Automated parity tests** 🟠
- `tests/integration/test_live_backtest_parity.py`
- Проверяет: идентичность параметров TradingCore и OrchestratorBacktestConfig

---

## Направление D: Phase 3 — Portfolio Backtest

**Требует**: B завершён

**D1. Выбор топ-5 пар для портфеля** 🔴
- После B: выбрать пары с лучшим Sharpe per strategy
- Цель: Grid × 3 пары + DCA × 2 пары, или TF × 2 + Grid × 3

**D2. Запустить PortfolioBacktestEngine** 🔴
- Файл: `bot/tests/backtesting/portfolio_engine.py`
- SharedCapitalPool: $10,000 / 5 пар = $2,000 на пару

**D3. Корреляционный анализ** 🟡
- Пары с низкой корреляцией лучше для портфеля
- Уже реализовано в `PortfolioBacktestEngine.run_correlation_analysis()`

**D4. Drawdown и portfolio Sharpe** 🟡
- Max portfolio drawdown должен быть < 20%
- Portfolio Sharpe > 1.0

---

## Направление E: Production Deploy

**Требует**: C, D завершены

**E1. Обновить configs/phase7_demo.yaml** 🔴
- Применить оптимальные параметры из Phase 2
- Exclusive routing в live-боте

**E2. Docker rebuild и deploy** 🔴
- `docker build -t traderagent-bot .` на 185.233.200.13
- Smoke test перед переключением

**E3. Мониторинг после деплоя** 🟡
- Сравнить live metrics с backtest предсказаниями
- KPI: win_rate > 55%, max_drawdown < 10%

---

## Направление F: AdaptiveRecoveryGrid (Future Feature)

**Концепция**: Grid достигает нижней границы → запускается DCA cascade до уровня поддержки SMC → TP при совокупной позиции +1%.

**Задачи**

**F1. SMC.get_nearest_support()** 🟠
- Добавить метод в `SMCStrategyAdapter`
- Возвращает: ближайший Order Block ниже текущей цены

**F2. CombinedPositionManager** 🟠
- Отслеживает суммарную позицию Grid+DCA
- Вычисляет breakeven и динамический TP

**F3. Grid→DCA event bus** 🟠
- При `grid_lower_boundary_hit` событие передаётся в DCA
- DCA запускает cascade до SMC support level

**F4. Restart Grid после TP** 🟠
- После достижения combined TP: закрыть DCA, перезапустить Grid

---

## Направление G: Web UI

**Задачи**

**G1. Equity curves** 🟠
- lightweight-charts для визуализации backtest results
- Уже есть: `web/frontend/node_modules/lightweight-charts/`

**G2. Live dashboard** 🟠
- Текущие позиции, PnL, режим рынка
- FastAPI backend уже реализован (`web/`)

**G3. Backtest comparison UI** 🟠
- Сравнение Phase 1/2/3 результатов по парам и стратегиям

---

## Дорожная карта (Timeline)

```
Неделя 1:  A1, A2, A3 (force_close_all + trades counter + cooldown fix)
Неделя 2:  A4 (routing sync), B1, B2 (Phase 2 Grid optimization)
Неделя 3:  B3, B4, B5 (Phase 2 DCA/TF/SMC optimization)
Неделя 4:  D1, D2, D3 (Phase 3 portfolio)
Неделя 5:  E1, E2, E3 (production deploy)
Неделя 6+: F (AdaptiveRecoveryGrid), G (Web UI)
```

---

## Критерии успеха

| Milestone | Критерий |
|-----------|---------|
| Phase 2 готов | TF/SMC pnl ≠ 0 на 80%+ пар |
| Оптимизация завершена | Sharpe > 1.5 для топ-5 пар |
| Phase 3 портфель | Portfolio Sharpe > 1.0, drawdown < 20% |
| Production deploy | Live метрики ≈ backtest предсказания ±30% |
| AdaptiveRecoveryGrid | Тест на BTC: меньше стоп-лоссов vs чистый Grid |

---

*Связанные документы: [analysis.md](analysis.md) | [architecture.md](architecture.md)*
