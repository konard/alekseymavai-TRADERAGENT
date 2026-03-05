# TRADERAGENT — Анализ проекта

> Дата: 2026-03-05 · Версия: v2.0.0 + BacktestOrchestratorEngine V3.0
> Кодовая база: ~83K+ LOC · 1352+ тестов · Production: 5 ботов на Bybit demo

---

## 1. Обзор проекта

TRADERAGENT — платформа алгоритмической торговли криптовалютами на Python/asyncio. Работает в продакшене на демо-аккаунте Bybit (api-demo.bybit.com) с 5 активными ботами. Поддерживает 4 независимые торговые стратегии с единым интерфейсом и системой бэктестирования V3.0.

### Производственные боты

| Бот | Стратегия | Пара |
|-----|-----------|------|
| demo_btc_hybrid | Grid + DCA | BTC/USDT |
| demo_eth_grid | Grid | ETH/USDT |
| demo_sol_dca | DCA | SOL/USDT |
| demo_btc_trend | TrendFollower | BTC/USDT |
| demo_btc_smc | SMC | BTC/USDT |

---

## 2. Архитектура (текущее состояние)

### Активные компоненты

```
bot/
├── orchestrator/
│   ├── bot_orchestrator.py          # Главный оркестратор (2639 LOC)
│   ├── market_regime.py              # 6-режимный детектор рынка
│   ├── strategy_selector.py          # Роутер стратегий
│   └── state_persistence.py          # Сохранение состояния
├── strategies/
│   ├── base.py                       # Единый интерфейс BaseStrategy
│   ├── grid/ + grid_adapter.py       # Grid-стратегия
│   ├── dca/ + dca_adapter.py         # DCA-стратегия
│   ├── trend_follower/ + trend_follower_adapter.py
│   └── smc/ + smc_adapter.py         # SMC с M5/H1 мультитаймфреймом
├── tests/backtesting/
│   ├── orchestrator_engine.py        # BacktestOrchestratorEngine V3.0
│   ├── multi_tf_data_loader.py       # Загрузчик данных (O(log n))
│   └── strategy_router.py            # Advisory-роутер для бэктеста
└── api/
    └── bybit_direct_client.py        # Клиент Bybit (demo/live)
```

### Заброшенные / устаревшие компоненты

```
services/backtesting/      # Старая REST-API система бэктеста (не используется)
backtesting-module/        # Отдельный пакет (не используется)
web/                       # Веб-дашборд (не поддерживается активно)
```

---

## 3. Сильные стороны

### 3.1 Архитектурные

- **Единый интерфейс BaseStrategy** (`bot/strategies/base.py`) — все стратегии реализуют один контракт: `analyze_market()`, `generate_signal()`, `open_position()`, `close_position()`, `update_positions()`
- **Адаптерный паттерн** — каждая стратегия имеет два слоя: внутреннюю реализацию и адаптер для бэктеста и живого бота
- **MarketRegimeDetector** — 6 режимов (TIGHT_RANGE, WIDE_RANGE, QUIET/VOLATILE_TRANSITION, BULL/BEAR_TREND) с ADX-гистерезисом
- **BacktestOrchestratorEngine V3.0** — параллельный запуск всех 4 стратегий на одном баре, реальный P&L расчёт
- **O(log n) поиск данных** — `searchsorted` в `multi_tf_data_loader.py`

### 3.2 Качество кода

- 1352+ тестов, ruff + black
- structlog для структурированного логирования
- Decimal для финансовых вычислений (нет float-погрешностей)
- Asyncio-архитектура без блокировок
- Pydantic-схемы для конфигурации

### 3.3 Инфраструктура

- Docker Compose на продакшн-сервере
- Bybit demo API (безрисковое тестирование)
- TimescaleDB для хранения OHLCV
- State persistence (сохранение состояния между рестартами)

---

## 4. Слабые стороны

### 4.1 Критические баги

| Баг | Статус | Описание |
|-----|--------|----------|
| TrendFollower infinite exit loop | ✅ Исправлен (d817507, 2026-03-05) | `close_position()` не вызывался после stop_loss |
| SMC = 0 сделок в бэктесте | 🔴 Открыт | Throttle + risk manager блокируют все сигналы |

### 4.2 Архитектурные проблемы

**MarketRegimeDetector не подключён к торговому циклу**
- Детектор создаётся и обновляется каждые 60 сек (строка 173 bot_orchestrator.py)
- Результат НЕ передаётся в strategy_selector для выбора активных стратегий в live
- В бэктесте используется корректно через `strategy_router.py`

**Telegram недоступен**
- Сервер (185.233.200.13) не может подключиться к api.telegram.org
- Попытка 72+ → бот продолжает работать, но уведомления не доходят

### 4.3 DCA в даунтренде

- DCA накапливает убыточные позиции бесконечно при падении цены
- `max_steps=5` не ограничивает потери достаточно — при 2% deviation и 5 шагах убыток может быть -50%
- Phase 1 результат: DCA avg -$10k/pair на медвежьем рынке (Авг 2025 — Фев 2026)

---

## 5. Расхождения: Live Bot vs BacktestOrchestratorEngine V3.0

**Ключевой вывод:** Пока параметры бэктеста не синхронизированы с параметрами live-бота, результаты бэктеста нельзя использовать для подбора оптимальных параметров. Это главный блокер для оптимизации стратегий.

### 5.1 Сравнительная таблица параметров

| Параметр | Live (phase7_demo.yaml) | Backtest (adapter defaults) | Критичность |
|----------|------------------------|------------------------------|-------------|
| **Grid** | | | |
| `profit_per_grid` | 0.012 (1.2%) | 0.005 (0.5%) | 🔴 Высокая — 2.4× разница |
| Границы сетки | Фиксированные из конфига (64k–74k) | Динамические ±5% от цены | 🟡 Средняя |
| **DCA** | | | |
| `take_profit_percentage` | 0.10 (10%) | 0.015 (1.5%) | 🔴 Критическая — 6.7× разница |
| `trigger_percentage` | 0.02 (2%) | 0.02 (2%) | ✅ Совпадает |
| `catch_up_enabled` | true (есть) | Отсутствует | 🔴 Функция отсутствует |
| **TrendFollower** | | | |
| `max_positions` | 2 | 20 (default) | 🔴 Высокая — 10× разница |
| `ema_fast/slow` | 20/50 | 20/50 | ✅ Совпадает |
| `risk_per_trade_pct` | 0.01 | 0.01 | ✅ Совпадает |
| **SMC** | | | |
| `require_volume_confirmation` | true | false (backtest) | 🟡 Средняя |
| Signal throttle (M5) | каждые 300 сек (5 мин) | каждые 12 M5 (1 час) | 🟡 Средняя |
| `min_risk_reward` | 2.0 | 2.0 | ✅ Совпадает |
| **Risk Manager** | | | |
| `min_order_size` | из конфига | hardcoded `Decimal("10")` | 🟡 Средняя |
| `max_position_size` | абсолютный USD | % от баланса | 🟡 Средняя |

### 5.2 Расхождения в логике

**Параллельное выполнение стратегий:**
- **Live**: стратегии выполняются последовательно в отдельных методах, каждая в своем `if`-блоке
- **Backtest V3.0**: все 4 стратегии выполняются на каждом баре с advisory weights от роутера

**Market Regime обновление:**
- **Live**: каждые 60 секунд реального времени, результат НЕ влияет на выбор стратегии
- **Backtest**: каждые 12 M5 баров (1 час), результат через `strategy_router.py` даёт advisory веса (1.0 / 0.5)

**DCA catch-up:**
- **Live**: `_run_dca_catchup()` при старте если `catch_up_enabled=true` — создаёт усредняющие ордера
- **Backtest**: catch-up отсутствует, первый день работы DCA в бэктесте принципиально другой

**SMC мультитаймфреймовость:**
- **Live**: H1 структура + M5 вход через `SMCStrategyAdapter`
- **Backtest**: явная передача `df_h1, df_m5` через `smc_adapter.py`, throttle `smc_generate_signal_every_n=12`

### 5.3 Централизованность учёта позиций

- **Live**: каждый движок (GridEngine, DCAEngine, TrendFollowerStrategy) хранит свои позиции внутри
- **Backtest V3.0**: централизованные dict'ы `position_amounts`, `position_entry_prices`, `per_strategy_pnl`

Это создаёт риск расхождения в P&L расчётах между средами.

---

## 6. Phase 1 Backtest — Ключевые результаты

**Параметры запуска:** 37 пар × 50k M5 баров × 14 рабочих = 35 минут (2026-03-04)

| Метрика | Значение |
|---------|---------|
| Прибыльных пар | 5/37 (13.5%) |
| Средний доход | -11.68% |
| Средний Sharpe | -1.545 |
| Средний MaxDD | 15.4% |

**Прибыльные пары:** BATUSDT +3.8%, ETCUSDT +1.85%, BCHUSDT +1.21%, BNBUSDT +1.09%, UNIUSDT +0.25%

**Причины слабых результатов:**
1. Медвежий рынок Авг 2025 — Фев 2026 — DCA накапливает убытки
2. SMC = 0 сделок везде: throttle + risk manager блокируют все входы
3. Grid + DCA: 77-80% win rate, но отрицательный суммарный P&L

---

## 7. Стратегия тестирования и оптимизации

**Ключевой принцип:** Нельзя подобрать оптимальные параметры для живых ботов, пока бэктест не воспроизводит live-поведение идентично. Сначала синхронизация, потом оптимизация.

### Этапы:

1. **Синхронизация параметров** (текущий блокер)
   - Передавать `phase7_demo.yaml` параметры напрямую в `orchestrator_engine.py`
   - Убрать hardcoded дефолты в adapter'ах

2. **Верификация бэктеста** (smoke tests)
   - Тест: один бот, одна стратегия, 100 баров → совпадение количества ордеров с live логами
   - Тест: SMC isolation — только SMC без других стратегий

3. **Оптимизация параметров** (Phase 2)
   - DCA: `price_deviation=3-5%`, `max_safety_orders=3`, SHORT режим для TrendFollower
   - Grid: адаптивные границы по режиму рынка

---

## 8. Связанные материалы

- [Отчёт Phase 1 бэктеста](backtest_v2_phase1_results.md)
- [Архитектура бэктеста](backtest_v2_engine_rework_plan.md)
- [Архитектура живого бота](architecture_bot.md)
- [Case Study: TrendFollower Infinite Exit Loop](case-studies/issue-tf-infinite-exit-loop/analysis.md)
- [Plan.md](plan.md)
