# TRADERAGENT — План развития

> Дата: 2026-03-05 · Версия: v2.0.0
> На основе: [Анализ проекта](analysis.md)

---

## Приоритеты (overview)

| Приоритет | Направление | Статус |
|-----------|-------------|--------|
| P0 | Синхронизация Live↔Backtest параметров | 🔴 Блокер |
| P0 | Расследование SMC = 0 сделок | 🔴 Открыт |
| P1 | Оптимизация DCA (Phase 2) | ⏳ После P0 |
| P1 | Подключение MarketRegimeDetector к live | ⏳ После P0 |
| P2 | TrendFollower SHORT режим | ⏳ Планируется |
| P2 | Telegram восстановление | 🟡 Инфра |
| P3 | Веб-дашборд | ⏳ Низкий приоритет |

---

## Направление 1: Синхронизация Live↔Backtest (P0 — Блокер)

**Цель:** Бэктест должен воспроизводить live-поведение идентично, иначе результаты оптимизации бессмысленны.

**Принцип:** Единый конфигурационный файл (`phase7_demo.yaml`) должен использоваться и для live, и для запуска бэктеста.

### Задачи:

**P0.1** Передавать параметры из YAML в `BacktestOrchestratorEngineConfig`
- Файл: `bot/tests/backtesting/orchestrator_engine.py`
- Убрать hardcoded дефолты (`profit_per_grid=0.005`, `take_profit_pct=0.015`, `min_order_size=10`)
- Добавить метод `from_yaml_config(path: str) -> BacktestOrchestratorEngineConfig`

**P0.2** Синхронизировать дефолты adapter'ов с live конфигом
- `bot/strategies/grid_adapter.py`: `profit_per_grid=0.012`, фиксированные границы из конфига
- `bot/strategies/dca_adapter.py`: `take_profit_pct=0.10`, `max_positions=5`
- `bot/strategies/trend_follower_adapter.py`: `max_positions=2`
- `bot/strategies/smc_adapter.py`: `require_volume_confirmation=True`

**P0.3** Унификация `max_position_size` — единица измерения (USD vs % баланса)
- Live Risk Manager: абсолютный USD
- Backtest Risk Manager: % от баланса
- Выбрать один формат и синхронизировать

**P0.4** Реализовать DCA catch-up в бэктесте
- Скопировать логику `_run_dca_catchup()` из `bot_orchestrator.py` в backtest engine

**P0.5** Написать верификационный тест
- Запустить live бота в dry_run и backtest на одном отрезке данных
- Сравнить количество ордеров, P&L, активные позиции

---

## Направление 2: Расследование SMC = 0 сделок (P0)

**Проблема:** SMC не открыл ни одной позиции на 37 парах × 50k баров в Phase 1.

**Гипотезы:**
1. Throttle `smc_generate_signal_every_n=12` + warmup 100 баров → сигналов мало, risk manager блокирует все
2. `min_risk_reward=2.0` слишком высокое для текущего рынка
3. `require_volume_confirmation=False` в backtest, но расчёт объёма некорректен

### Задачи:

**P0.1** Smoke-тест: изолированный запуск только SMC
```bash
# Одна пара, 500 баров, только SMC, без других стратегий
# enable_dca=False, enable_grid=False, enable_trend_follower=False
```

**P0.2** Добавить verbose logging в SMC в backtest
- Логировать каждый отклонённый сигнал: причину (`rr_too_low`, `no_volume`, `risk_blocked`)
- Логировать количество warmup баров vs сигнальных

**P0.3** Параметрический эксперимент
- Снизить `smc_generate_signal_every_n` с 12 до 3
- Снизить `min_risk_reward` с 2.0 до 1.5
- Отключить `require_volume_confirmation`

**P0.4** Проверить корректность M5 данных в бэктесте
- Убедиться что `df_m5` содержит корректные OHLCV для SMC signal generation

---

## Направление 3: Оптимизация DCA (P1, Phase 2)

**Проблема:** DCA накапливает убытки бесконечно при даунтренде. Avg -$10k/pair на Phase 1.

**Стратегия:** Ограничить потери + добавить защитные механизмы.

### Задачи:

**P1.1** Ограничить `max_safety_orders` до 3 (было 5)
- Быстрое тестирование влияния на Phase 1 результаты

**P1.2** Увеличить `price_deviation_pct` до 3-5%
- При deviation=2% бот накапливает слишком частые позиции
- При deviation=5% ждёт более значимых откатов

**P1.3** Добавить DCA SHORT режим при BEAR_TREND
- Когда MarketRegimeDetector даёт BEAR_TREND → DCA продаёт вместо покупки
- Файлы: `bot/strategies/dca/engine.py`, `bot/orchestrator/bot_orchestrator.py`

**P1.4** Добавить максимальный убыток на позицию DCA
- Если суммарный DCA-убыток > X% → закрыть все DCA позиции
- Параметр: `max_dca_loss_pct: 0.05` (5%)

**P1.5** Phase 2 backtest: 37 пар × 50k баров с новыми параметрами
- Сравнить с Phase 1 baseline

---

## Направление 4: Адаптивное переключение стратегий (P1)

**Проблема:** MarketRegimeDetector создан и работает, но результат не используется в live боте.

**Цель:** Живой бот автоматически меняет параметры стратегий в зависимости от режима рынка.

### Задачи:

**P1.1** Подключить MarketRegimeDetector к strategy_selector в live
- Файл: `bot/orchestrator/bot_orchestrator.py`, метод `_regime_monitor_loop()`
- Передавать `current_regime` в `strategy_selector.select_active_strategies()`

**P1.2** Определить правила переключения для каждого режима
```
BULL_TREND  → активировать TrendFollower (weight=1.0), DCA (weight=0.5)
BEAR_TREND  → активировать DCA SHORT, отключить Grid LONG
TIGHT_RANGE → Grid (weight=1.0), DCA (weight=1.0)
WIDE_RANGE  → SMC (weight=1.0), TrendFollower (weight=0.5)
```

**P1.3** Cooldown и гистерезис переключений
- Cooldown уже есть: 600 сек между переключениями
- Добавить: не переключать при волатильности > X%

**P1.4** Live тестирование на демо-счёте
- Включить режим для одного бота (demo_btc_hybrid)
- Мониторинг результатов 2 недели

---

## Направление 5: TrendFollower SHORT режим (P2)

**Проблема:** TrendFollower открывает только LONG позиции, проигрывает на даунтренде.

### Задачи:

**P2.1** Включить SHORT сигналы в TrendFollowerStrategy
- Файл: `bot/strategies/trend_follower/entry_logic.py`
- При `phase=bearish` → SHORT вход при откате к EMA20

**P2.2** Проверить Bybit demo поддержку SHORT на USDT perpetuals
- Тестовый ордер Sell Market 0.001 BTC

**P2.3** Backtest TrendFollower только SHORT на Phase 1 данных
- Сравнить с LONG-only baseline

---

## Направление 6: Инфраструктура (P2)

### Telegram восстановление

**P2.1** Диагностика: почему 185.233.200.13 не может достучаться до api.telegram.org
```bash
# На сервере:
curl -I https://api.telegram.org
nslookup api.telegram.org
ping api.telegram.org
```

**P2.2** Варианты решения:
- Через прокси (HTTP_PROXY в docker-compose)
- Через отдельный VPS как relay
- Замена на Telegram Bot через webhook вместо polling

### Мониторинг балансов

**P2.3** Добавить daily P&L отчёт в файл (если Telegram недоступен)
- Запись в `/home/ai-agent/TRADERAGENT/logs/daily_pnl.jsonl`

### Тестовый сервер

**P2.4** Восстановить или заменить тестовый сервер 158.160.215.57
- Сейчас ВЫКЛЮЧЕН после сессии 45
- Нужен для Phase 2 backtest (35 мин на 37 пар)
- Альтернатива: запустить на production сервере в отдельном container

---

## Направление 7: Качество кода (P3)

**P3.1** Интеграционные тесты для pattern "exit → close_position"
- По каждой стратегии (TF, SMC, Grid, DCA) написать тест на идемпотентность exit
- Предотвратить повторение бага TrendFollower Infinite Exit Loop

**P3.2** Унифицированный конфиг для backtest и live
- Один класс `StrategyConfig` вместо двух (Pydantic + dataclass)
- Единая точка конвертации в `BacktestOrchestratorEngineConfig`

**P3.3** Документирование API бэктеста
- Параметры `BacktestOrchestratorEngineConfig` с пояснениями
- Пример запуска с параметрами из YAML

---

## Временная шкала (ориентировочно)

| Этап | Задачи | Условие перехода |
|------|--------|-----------------|
| 1 | P0.1–P0.5: Синхронизация параметров | Бэктест воспроизводит live ордера |
| 2 | P0.1–P0.4: SMC investigation | SMC генерирует ≥ 1 сделку/100 баров |
| 3 | P1.1–P1.5: DCA оптимизация | Phase 2 backtest: avg return > 0% |
| 4 | P1.1–P1.4: Adaptive switching | Live демо 2 недели без критических багов |
| 5 | P2–P3: Всё остальное | По мере ресурсов |
