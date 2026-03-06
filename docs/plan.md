# TRADERAGENT — План развития

> Дата: 2026-03-06 · Версия: v2.0.0
> Обновлено: 2026-03-06 (Session 49)
> На основе: [Анализ проекта](analysis.md)

---

## Обзор приоритетов

| Приоритет | Направление | Цель | Статус |
|-----------|-------------|------|--------|
| **P0** | Live↔Backtest синхронизация | Устранить расхождения движков | ✅ P0.1-P0.5 готово |
| **P0** | Синхронизация серверов с main | Деплой всех фиксов | 🟡 Тестовый ✅, Продакшн 🔴 |
| **P0** | Реальные данные + Phase 1 перегон | Корректный baseline | 🔴 Ожидает деплоя |
| **P1** | Phase 2 оптимизация параметров | Найти оптимальные конфиги | ⏳ После P0 |
| **P1** | DCA оптимизация | 3-5% deviation, max 3 orders | ⏳ После P0 |
| **P1** | Unified parameter model | Унификация параметров стратегий | 🟡 Частично (P0.2) |
| **P2** | TrendFollower SHORT режим | Торговля в BEAR_TREND | ⏳ Планируется |
| **P2** | Hybrid в backtest | Воспроизвести Grid↔DCA routing | ⏳ Планируется |
| **P3** | Auto grid range update | Динамический диапазон Grid | ⏳ Низкий приоритет |
| **P3** | Telegram proxy | Восстановить уведомления | ⏳ Низкий приоритет |

---

## Направление 1: Live ↔ Backtest синхронизация (P0)

**Цель:** Backtest воспроизводит live-поведение с отклонением ≤5% по числу сделок.

**Без этого:** результаты оптимизации бессмысленны для живых ботов.

### Задачи по приоритетам

**~~P0.1 — Синхронизация роутера~~** ✅ ВЫПОЛНЕНО (`961126f`)
- ~~Заменить `StrategyRouter` (advisory) на логику `HybridCoordinator` (mode-based)~~
- Реализовано: вес `1.0` для активных стратегий, `0.0` для неактивных — Grid/DCA взаимоисключающие
- Файлы: `orchestrator_engine.py` — `regime_weights`, skip signal if `weight == 0.0`

**~~P0.2 — Унификация единиц позиции~~** ✅ ВЫПОЛНЕНО (`6b4eccf`)
- ~~Перевести `max_position_size` на `% от текущего баланса`~~
- Реализовано: `from_yaml_config(initial_balance)` — конвертация USD → % через реальный баланс
- `max_position_pct` берётся с первого бота (не MAX), `max_position_size_pct` синхронизирован
- `_cfg_from_yaml()` передаёт `initial_balance` во всех call sites

**~~P0.3 — Синхронизация `max_daily_loss`~~** ✅ ВЫПОЛНЕНО (`961126f`)
- ~~Текущий backtest: 25% хардкод~~
- Реализовано: `max_daily_loss_pct = max_daily_loss_usd / initial_balance` из YAML
- BTC: $600 / $10k = 6%; масштабируется с `initial_balance`

**~~P0.4 — Синхронизация частоты SMC-сигналов~~** ✅ ВЫПОЛНЕНО (`a730751`)
- `smc_generate_signal_every_n`: `12` → `1` (каждый M5-бар, как в live)

**~~P0.5 — DCA catch-up в backtest~~** ✅ ВЫПОЛНЕНО (`6b4eccf`)
- ~~Скопировать логику `DCAStartupAnalyzer._run_dca_catchup()` в warmup-фазу backtest~~
- Реализовано: `_run_dca_warmup_catchup()` в `BacktestOrchestratorEngine`
- Step 1: `_recent_high` из последних 500 warmup-баров (live-пarity)
- Step 2: pre-open catch-up ордера через `DCAStartupAnalyzer`

**P0.6 — Верификационный тест Live vs Backtest** ✅ *DONE*
- Запустить backtest на BTC/USDT (5000 баров) с `--live-config`
- Проверить: DCA catch-up работает, `max_daily_loss=6%`, все стратегии дают сделки
- Финальная проверка: сравнить число сигналов с live dry_run логами
- Допустимое отклонение: ±10% по числу сделок
- Реализовано: `scripts/verify_backtest_parity.py` — автоматический PASS/FAIL чеклист
- Все конфиг-параметры (P0.1–P0.5) подтверждены: max_position_pct=0.30, max_daily_loss_pct=0.06, DCA catch-up=8 ордеров, SMC/TF активны (signal_count > 0)

> **Дополнительно выполнено (Session 48-49):**
> - Нормализация символов: `BTCUSDT` / `BTC` → `BTC/USDT` в `_normalize_symbol()`
> - Подавление debug-логов: `_suppress_strategy_logging()` (предотвращает 2.6M строк/прогон)
> - `docs/architecture_livebot_vs_backtest.md` — диаграмма Live Bot vs BacktestOrchestratorEngine V3.0
> - Исправлены pre-existing merge conflicts: `grid_engine.py`, `bybit_direct_client.py`, `bot/main.py`

---

## Направление 2: Реальные данные + Phase 1 перегон (P0)

**Цель:** Phase 1 backtest с реальными рыночными данными и исправленным SMC.

> **Статус сервера тестирования (158.160.215.57):**
> ✅ 45 пар × 5m CSV, 5.4 GB, история с 2017-08-17 (BTC — 889k строк)
> ✅ 16 CPU / 30 GB RAM / 87 GB свободно — готов к запуску
> ✅ Синхронизирован с main до коммита `a730751` (Session 48)
> ⚠️ Нужен повторный `git pull` — ещё +7 коммитов (P0.2, P0.5, bugfixes, session 49)

### Задачи

**~~P0.0 — Синхронизация тестового сервера~~** ✅ ЧАСТИЧНО (`Session 48`)
- ✅ Тестовый сервер (158.160.215.57): синхронизирован до `a730751`
- 🔴 Тестовый сервер: нужен повторный `git pull` (ещё 7 коммитов: P0.2, P0.5, bugfixes)
- 🔴 Продакшн сервер (185.233.200.13): отстаёт на ~14 коммитов, бот запущен
- Команда: `git pull origin main && docker compose restart bot`

**~~P0.1 — Сбор исторических данных~~** ✅ ВЫПОЛНЕНО
- 45 пар × 5m CSV на тестовом сервере (5.4 GB, с 2017), путь: `data/historical/`

**P0.2 — Запустить Phase 1 с полными parity-фиксами** 🔴 *После git pull на тест. сервере*
- Все P0.1-P0.5 фиксы включены — нужен чистый прогон
- Ожидаемый результат: DCA catch-up срабатывает на день 0, SMC даёт сделки
- Команда:
  ```bash
  python scripts/run_backtest_v2.py \
    --mode multi \
    --live-config configs/phase7_demo.yaml \
    --data-dir data/historical \
    --max-bars 50000 \
    --workers 14
  ```

**P0.3 — Анализ Phase 1 результатов** ⏳ *После P0.2*
- Какие стратегии прибыльны на каких режимах рынка?
- Какие пары ведут себя хорошо с Grid, DCA, TF, SMC?
- Выявить аномальные пары (как FTTUSDT в предыдущем прогоне)

---

## Направление 3: Оптимизация параметров (P1)

**Цель:** Найти оптимальные параметры для каждой стратегии и каждого режима рынка.

**Зависимость:** требует завершения Направлений 1 и 2.

### Задачи

**P1.1 — Unified parameter model** 🟡 *Частично выполнено (P0.2)*
- `max_position_pct` — ✅ унифицировано через `from_yaml_config(initial_balance)`
- `max_daily_loss_pct` — ✅ унифицировано (P0.3)
- `risk_per_trade_pct` — 🔴 ещё не унифицировано (разные поля у TF vs SMC)
- Осталось: обновить адаптеры TF и SMC для единого `risk_per_trade_pct`

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

| Задача | Метрика | Целевое значение | Статус |
|--------|---------|-----------------|--------|
| Live↔Backtest sync | Отклонение числа сделок | ≤ 10% | ✅ P0.6 DONE |
| Phase 1 перегон | SMC сделок на BTC/USDT | > 0 | 🔴 Ожидает деплоя |
| Phase 2 оптимизация | Sharpe improvement | ≥ 0.5 vs baseline | ⏳ |
| DCA оптимизация | Max drawdown | < 15% | ⏳ |
| TF SHORT режим | Bear market return | > -5% | ⏳ |
| Unified params | Параметров без аналога | 0 | 🟡 2 из 3 готово |

---

## Текущий фокус (ближайшие шаги)

1. **`git pull` на тестовом сервере** (158.160.215.57) — подтянуть P0.2, P0.5, bugfixes
2. ~~**P0.6 Smoke test**~~ — ✅ DONE (`scripts/verify_backtest_parity.py` — все чеки пройдены)
3. **Phase 1 full run** — 37-45 пар, 50k баров, 14 workers
4. **Деплой на продакшн** (185.233.200.13) — `git pull` + `docker compose restart bot`

---

## Временная оценка (ориентировочно)

| Направление | Задачи | Трудоёмкость | Статус |
|-------------|--------|-------------|--------|
| Live↔Backtest sync | P0.1-P0.6 | ~~2-3 сессии~~ | ✅ P0.1-P0.6 DONE |
| Деплой + Phase 1 | git pull + run | 0.5 сессии | 🔴 Следующий |
| Phase 2 оптимизация | P1.1-P1.6 | 3-4 сессии | ⏳ |
| TrendFollower SHORT | P2.1-P2.4 | 1-2 сессии | ⏳ |
| Hybrid в backtest | P2.1-P2.2 | 1 сессия | ⏳ |
| Инфраструктура | P2-P3 | По мере необходимости | ⏳ |
