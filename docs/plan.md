# TRADERAGENT — План развития

> Дата: 2026-03-06 · Версия: v2.0.0  
> На основе: [Анализ проекта](analysis.md)

---

## Обзор приоритетов

| Приоритет | Направление | Цель | Статус |
|-----------|-------------|------|--------|
| **P0** | Live↔Backtest синхронизация | Устранить расхождения движков | 🟡 В процессе |
| **P0** | Синхронизация серверов с main | SMC-фикс на тестовом и продакшн | 🔴 Открыт |
| **P0** | Реальные данные + Phase 1 перегон | Корректный baseline (данные готовы!) | 🔴 Готов к запуску |
| **P1** | Phase 2 оптимизация параметров | Найти оптимальные конфиги | ⏳ После P0 |
| **P1** | DCA оптимизация | 3-5% deviation, max 3 orders | ⏳ После P0 |
| **P1** | Unified parameter model | Унификация параметров стратегий | ⏳ После P0 |
| **P2** | TrendFollower SHORT режим | Торговля в BEAR_TREND | ⏳ Планируется |
| **P2** | Hybrid в backtest | Воспроизвести Grid↔DCA routing | ⏳ Планируется |
| **P3** | Auto grid range update | Динамический диапазон Grid | ⏳ Низкий приоритет |
| **P3** | Telegram proxy | Восстановить уведомления | ⏳ Низкий приоритет |

---

## Направление 1: Live ↔ Backtest синхронизация (P0)

**Цель:** Backtest воспроизводит live-поведение с отклонением ≤5% по числу сделок.

**Без этого:** результаты оптимизации бессмысленны для живых ботов.

### Задачи по приоритетам

**P0.1 — Синхронизация роутера** *(критично)*
- Заменить `StrategyRouter` (advisory) на логику `HybridCoordinator` (mode-based)
- В backtest: Grid и DCA не могут работать одновременно, только одна активна
- Файл: `bot/tests/backtesting/strategy_router.py`, `orchestrator_engine.py`
- Тест: количество одновременно активных стратегий ≤ 1 для Hybrid пар

**P0.2 — Унификация единиц позиции** *(критично)*
- Перевести `max_position_size` на `% от текущего баланса` как универсальную единицу
- Backtest: `position_value = current_balance × max_position_pct`
- Live: `max_position_size` USD → конвертировать через текущий баланс при старте
- Файлы: `OrchestratorBacktestConfig`, `bot_orchestrator.py`, `from_yaml_config()`

**P0.3 — Синхронизация `max_daily_loss`** *(критично)*
- Текущий backtest: `max_daily_loss = 25% × $10k = $2,500` (слишком мягко)
- Live: `max_daily_loss = $600` (6% от баланса)
- Решение: `max_daily_loss_pct = 0.006` (0.6% баланса), не хардкоженный %

**~~P0.4 — Синхронизация частоты SMC-сигналов~~** ✅ ВЫПОЛНЕНО
- `smc_generate_signal_every_n`: `12` → `1` (каждый M5-бар, как в live)

**P0.5 — DCA catch-up в backtest**
- Скопировать логику `DCAStartupAnalyzer._run_dca_catchup()` в warmup-фазу backtest
- Файл: `bot/tests/backtesting/orchestrator_engine.py`

**P0.6 — Верификационный тест Live vs Backtest**
- Запустить live бота в dry_run на отрезке, для которого есть CSV-данные
- Сравнить: количество сигналов, сделок, P&L
- Допустимое отклонение: ±10% по числу сделок

---

## Направление 2: Реальные данные + Phase 1 перегон (P0)

**Цель:** Phase 1 backtest с реальными рыночными данными и исправленным SMC.

> **Статус сервера тестирования (158.160.215.57):**
> ✅ 45 пар × 5m CSV, 5.4 GB, история с 2017-08-17 (BTC — 889k строк)
> ✅ 16 CPU / 30 GB RAM / 87 GB свободно — готов к запуску
> ⚠️ Git отстаёт от main на 7 коммитов (сессии 46-47) — нужно синхронизировать

### Задачи

**P0.0 — Синхронизация серверов с main** *(сделать первым)*
- Тестовый сервер (158.160.215.57): git pull origin main (отстаёт на 7 коммитов)
- Продакшн сервер (185.233.200.13): git pull + tar xzf деплой (отстаёт на 8 коммитов)
- Без этого SMC-баг (исправлен в сессии 47) останется на серверах

**~~P0.1 — Сбор исторических данных~~** ✅ ВЫПОЛНЕНО
- ~~Скачать 12 месяцев M5-данных для 37+ пар~~ — уже есть!
- 45 пар × 5m CSV на тестовом сервере (5.4 GB, с 2017), путь: `data/historical/`
- Дополнительная загрузка не требуется

**P0.2 — Запустить Phase 1 с исправленным движком**
- SMC баг исправлен (сессия 47) — нужен новый прогон после P0.0
- Ожидаемый результат: SMC должен дать сделки на трендовых парах
- Команда: `python scripts/run_backtest_v2.py --mode multi --live-config configs/phase7_demo.yaml --workers 14`
- Использовать реальные CSV через адаптер `MultiTimeframeDataLoader`

**P0.3 — Анализ Phase 1 результатов**
- Какие стратегии прибыльны на каких режимах рынка?
- Какие пары ведут себя хорошо с Grid, DCA, TF, SMC?
- Выявить аномальные пары (как FTTUSDT в предыдущем прогоне)

---

## Направление 3: Оптимизация параметров (P1)

**Цель:** Найти оптимальные параметры для каждой стратегии и каждого режима рынка.

**Зависимость:** требует завершения Направлений 1 и 2.

### Задачи

**P1.1 — Unified parameter model**
- Ввести нормализованный уровень риск-параметров:
  - `risk_per_trade_pct` — единица для всех стратегий
  - `max_position_pct` — единица позиции
  - `max_daily_loss_pct` — единица дневного лимита
- Обновить все адаптеры и `from_yaml_config()` для поддержки новой модели

**P1.2 — Grid оптимизация**
- Параметрическая сетка: `profit_per_grid ∈ [0.5%, 2.0%]`, `num_levels ∈ [4, 12]`
- Разные диапазоны для BTC vs ETH
- Оценить: лучший profit_per_grid для каждого volatility-режима

**P1.3 — DCA оптимизация**
- Параметрическая сетка: `trigger_pct ∈ [2%, 6%]`, `max_steps ∈ [2, 6]`, `tp ∈ [5%, 15%]`
- Тест гипотезы: `trigger_pct=3-5%, max_steps=3` лучше текущего `4%/4 steps`
- Отдельная оптимизация для BULL vs BEAR рынка

**P1.4 — TrendFollower оптимизация**
- EMA периоды: fast ∈ [10, 20, 30], slow ∈ [40, 50, 100]
- ATR multiplier TP: ∈ [1.5, 2.0, 2.5, 3.0]
- Тест: добавить SHORT-режим в BEAR_TREND

**P1.5 — SMC оптимизация**
- `swing_length ∈ [5, 10, 20, 35]` для H1
- `swing_length_m5 ∈ [10, 20, 35]` для M5
- `min_risk_reward ∈ [1.5, 2.0, 2.5, 3.0]`
- Тест: M5 vs H1/M15 сигналы по Sharpe

**P1.6 — Режим-зависимая оптимизация**
- Для каждой комбинации {стратегия × режим_рынка} — свои оптимальные параметры
- Пример: Grid optimal params в TIGHT_RANGE ≠ BULL_TREND

---

## Направление 4: TrendFollower SHORT режим (P2)

**Цель:** Торговля в SHORT при BEAR_TREND, BEAR_MARKET режимах.

### Задачи

**P2.1 — SHORT сигналы в TrendFollower**
- Добавить `direction = SHORT` при `ema_fast < ema_slow` + RSI > 60
- Файл: `bot/strategies/trend_follower/entry_logic.py`

**P2.2 — Адаптер для SHORT позиций**
- `TrendFollowerAdapter.open_position()` должен поддерживать `direction=SHORT`
- Управление позицией: `update_positions()` с инверсной логикой TP/SL

**P2.3 — Bybit futures SHORT**
- Убедиться, что `ByBitDirectClient` корректно открывает SHORT на linear futures
- Тест в dry_run режиме перед деплоем

**P2.4 — Backtest SHORT режима**
- Запустить TF-only backtest на bear рынке (Aug 2025 – Feb 2026)
- Сравнить LONG-only vs LONG+SHORT Sharpe

---

## Направление 5: Hybrid в backtest (P2)

**Цель:** Backtest Hybrid (BTC/USDT) воспроизводит поведение `demo_btc_hybrid`.

### Задачи

**P2.1 — HybridCoordinator в BacktestOrchestratorEngine**
- Заменить независимый запуск Grid+DCA на `HybridCoordinator.evaluate(adx)`
- Файл: `bot/tests/backtesting/orchestrator_engine.py`

**P2.2 — Тест: Live vs Backtest Hybrid**
- Запустить `demo_btc_hybrid` dry_run на 30-дневном CSV
- Сравнить количество переключений Grid↔DCA

---

## Направление 6: Инфраструктура (P2-P3)

### P2: Portfolio-level stop-loss в backtest
- Подключить `PortfolioRiskManager` к `BacktestOrchestratorEngine`
- Добавить portfolio-level daily loss halt

### P2: Автоматическое применение результатов оптимизации
- Инструмент: `scripts/apply_optimization.py --results results/phase2/ --config configs/phase7_demo.yaml`
- Обновляет YAML параметры на основе Phase 2 результатов
- Restart бота после обновления

### P3: Динамический Grid-диапазон
- `GridAdapter` должен пересчитывать `upper_price`/`lower_price` при выходе цены за границы
- Параметр: `range_pct = 0.08` (±8% от текущей цены)

### P3: Telegram через proxy
- Настроить HTTP proxy или использовать Telegram Bot API через webhook
- Альтернатива: push уведомления через самохостинг (Ntfy/Gotify)

### P3: Веб-дашборд
- Восстановить/дописать React дашборд в `web/`
- Подключить к `bot/api/` для real-time данных

---

## Метрики успеха

| Задача | Метрика | Целевое значение |
|--------|---------|-----------------|
| Live↔Backtest sync | Отклонение числа сделок | ≤ 10% |
| Phase 1 перегон | SMC сделок на BTC/USDT | > 0 |
| Phase 2 оптимизация | Sharpe improvement | ≥ 0.5 vs baseline |
| DCA оптимизация | Max drawdown | < 15% |
| TF SHORT режим | Bear market return | > -5% |
| Unified params | Параметров без аналога | 0 |

---

## Временная оценка (ориентировочно)

| Направление | Задачи | Трудоёмкость |
|-------------|--------|-------------|
| Live↔Backtest sync | P0.1-P0.6 | 2-3 сессии |
| Реальные данные + Phase 1 | P0.1-P0.3 | 1-2 сессии + время загрузки |
| Phase 2 оптимизация | P1.1-P1.6 | 3-4 сессии |
| TrendFollower SHORT | P2.1-P2.4 | 1-2 сессии |
| Hybrid в backtest | P2.1-P2.2 | 1 сессия |
| Инфраструктура | P2-P3 | По мере необходимости |
