# TRADERAGENT — Анализ проекта

> Дата: 2026-03-06 · Версия: v2.0.0 + BacktestOrchestratorEngine V3.0  
> Кодовая база: ~100K+ LOC · 1687+ тестов · Production: 5 ботов на Bybit Demo

---

## 1. Текущее состояние

TRADERAGENT — платформа алгоритмической торговли криптовалютами (Python 3.12, asyncio). Работает в продакшене на демо-аккаунте Bybit (`api-demo.bybit.com`) с 5 активными ботами. Поддерживает 4 независимые торговые стратегии с единым интерфейсом `BaseStrategy`.

### Производственные боты (март 2026)

| Бот | Стратегия | Пара | Статус |
|-----|-----------|------|--------|
| demo_btc_hybrid | Grid + DCA (Hybrid) | BTC/USDT | ✅ Running |
| demo_eth_grid | Grid | ETH/USDT | ✅ Running |
| demo_sol_dca | DCA | SOL/USDT | ✅ Running |
| demo_btc_trend | TrendFollower | BTC/USDT | ✅ Running |
| demo_btc_smc | SMC (H1+M5) | BTC/USDT | ✅ Running |

**Баланс:** ~$102,415 (demo). **Сервер:** 185.233.200.13.

---

## 2. Архитектура

### 2.1 Активные компоненты

```
bot/
├── orchestrator/           # Главный цикл (bot_orchestrator.py ~2600 LOC)
│   ├── market_regime.py    # 6-режимный детектор (ADX + SMC overlay)
│   └── strategy_selector.py
├── strategies/             # 4 стратегии + адаптеры
│   ├── base.py             # Единый интерфейс BaseStrategy
│   ├── grid/ + grid_adapter.py
│   ├── dca/ + dca_adapter.py
│   ├── trend_follower/ + trend_follower_adapter.py
│   └── smc/ + smc_adapter.py   # H1 структура + M5 вход
├── core/
│   ├── smc/                # Внутренний SMC-модуль (O(n) swing, BOS/CHoCH)
│   ├── trading_core/       # TradingCore + HybridCoordinator
│   └── portfolio_risk_manager.py
├── tests/backtesting/
│   ├── orchestrator_engine.py   # BacktestOrchestratorEngine V3.0
│   ├── multi_tf_data_loader.py  # O(log n) многотаймфреймовый загрузчик
│   └── strategy_router.py       # Advisory-роутер для бэктеста
└── api/bybit_direct_client.py   # Bybit V5 API (demo/live)
```

### 2.2 Устаревшие / неиспользуемые компоненты

| Компонент | Статус |
|-----------|--------|
| `services/backtesting/` | Старая REST-API система, не используется |
| `backtesting-module/` | Отдельный пакет, не интегрирован |
| `web/` | React-дашборд, не поддерживается |
| `bot/replay/` | Replay-движок, не используется |
| `bot/tests/backtesting/multi_tf_engine.py` | Предшественник V3.0, частично устарел |
| `bot/tests/backtesting/backtesting_engine.py` | V1.0, заменён |

---

## 3. Сильные стороны

### 3.1 Архитектурные

- **Единый интерфейс BaseStrategy** — все 4 стратегии реализуют один контракт. Добавление новой стратегии = 1 файл + адаптер.
- **Адаптерный паттерн** — одна реализация работает и в live, и в бэктесте без модификации стратегий.
- **MarketRegimeDetector** — 6 режимов с ADX-гистерезисом и SMC overlay (ACCUMULATION/DISTRIBUTION). Внутренний модуль `bot/core/smc/` без внешних зависимостей.
- **O(log n) поиск данных** через `searchsorted` в multi_tf_data_loader (было O(n²)).
- **BacktestOrchestratorEngine V3.0** — параллельный запуск всех 4 стратегий на каждом баре, реальный P&L: `(exit_price - entry_price) × amount`.
- **from_yaml_config()** — единый YAML как источник параметров для live и backtest.

### 3.2 Качество кода

- 1687+ тестов, ruff + black
- `Decimal` для всех финансовых вычислений
- structlog (JSON-логирование)
- Pydantic-схемы для конфигурации
- Asyncio без блокирующих вызовов

### 3.3 Инфраструктура

- Docker Compose, volume mount без rebuild
- Bybit Demo API (реальные цены, нулевой риск)
- Encrypted credentials в PostgreSQL
- GitHub Actions workflow для автоматизации

---

## 4. Слабые стороны

### 4.1 Критические (блокируют оптимизацию)

**А. Нет реальных торговых данных**  
Без CSV-данных за 12+ месяцев невозможна осмысленная оптимизация. Синтетика не воспроизводит реальную микроструктуру рынка.

**Б. Параметры не унифицированы** (см. раздел 5)  
`max_position_size` в USD vs % баланса, `cooldown` в секундах vs барах, `max_daily_loss` в $ vs % — вычисления не совпадают между live и backtest.

**В. Hybrid не воспроизводится в backtest**  
`HybridStrategy` (Grid↔DCA coordinator) в live переключает стратегии через `HybridCoordinator`. В backtest Grid и DCA работают независимо — поведение принципиально разное.

### 4.2 Архитектурные

**Г. Два параллельных роутера**  
Live: `HybridCoordinator` (mode-based: GRID_ONLY / DCA_ACTIVE).  
Backtest: `StrategyRouter` (advisory weights: 1.0 / 0.5).  
Разные алгоритмы → backtest ≠ live по определению.

**Д. bot_orchestrator.py перегружен (2600+ LOC)**  
Main loop + risk management + routing + Telegram + WebSocket + DCA catch-up в одном файле. Сложно тестировать.

**Е. Дублирование SMC-логики**  
`bot/core/smc/` (swing/BOS/CHoCH) + `bot/strategies/smc/` (market_structure, signal_generator) — два слоя с частичным дублированием. `structural_detector.py` и `market_structure.py` делают похожую работу.

**Ж. Нет автоматического применения результатов оптимизации**  
Нет инструмента: «взять оптимальные параметры из backtest → записать в YAML → перезапустить».

### 4.3 Функциональные

**З. TrendFollower без SHORT-режима** — теряется 50% возможностей в BEAR_TREND.

**И. DCA catch-up не реализован в backtest** — поведение при старте различается.

**К. Portfolio-level stop-loss не подключён** — `PortfolioRiskManager` реализован, но не интегрирован в BacktestOrchestratorEngine.

**Л. Grid-диапазон не пересчитывается автоматически** — при движении BTC от $69k к $100k сетка выходит за диапазон.

### 4.4 Инфраструктурные

**М. Telegram недоступен на продакшн-сервере** (сетевые ограничения).

**Н. Нет CI/CD для backtest** — `weekly_optimization.yml` не запускается без тестового сервера.

**О. 26 веб-тестов всегда провалены** — pre-existing, засоряют отчёт.

---

## 5. Конфликты: Live ↔ Backtest V3.0

> **Главный принцип**: нельзя оптимизировать то, что не воспроизводишь.

### 5.1 Таблица расхождений параметров

| Параметр | Live (bot_orchestrator) | Backtest (orchestrator_engine) | Статус |
|----------|------------------------|-------------------------------|--------|
| `max_position_size` | Абсолютный USD (3000) | % баланса (25% × $10k = $2500) | 🔴 Разные единицы |
| `max_daily_loss` | Абсолютный USD (600) | % баланса (25% × $10k = $2500) | 🔴 В 4× мягче в backtest |
| `cooldown` | 600 сек (wall-clock) | 120 баров × 300 сек = 600 сек | ✅ Эквивалентно |
| `regime_check_interval` | 60 сек (каждый тик) | 12 баров = 60 мин | 🔴 60× реже |
| `maker_fee` | 0.02% | 0.0002 | ✅ Совпадает |
| `taker_fee` | 0.055% | 0.00055 | ✅ Совпадает |
| `slippage` | Нет (реальный ордербук) | 0.03% фиксированный | ⚠️ Только backtest |
| `risk_per_trade` | 2% | 2% | ✅ Совпадает |
| `require_volume_confirmation` | True (SMC) | False (хардкод) | ⚠️ Намеренно |
| `warmup_bars` | N/A | 14 400 баров (50 дней) | ⚠️ Только backtest |
| `smc_generate_signal_every_n` | Каждый тик | 12 баров (60 мин) | 🔴 12× реже |

### 5.2 Конфликт роутера (критический)

**Live** (`HybridCoordinator`):
```
GRID_ONLY  → Grid активен, DCA пассивен
DCA_ACTIVE → DCA активен, Grid пассивен
Переключение: ADX-порог или SMC-сигнал
```

**Backtest** (`StrategyRouter` advisory weights):
```
Все стратегии работают одновременно
weight = 1.0 → полная аллокация капитала
weight = 0.5 → половинная
Cooldown = 120 баров между переключениями
```

**Следствие**: в backtest Grid + DCA торгуют одновременно. В live — только одна активна. Backtest систематически завышает торговую активность.

### 5.3 Конфликт позиционирования

Live: `max_position_size = 3000 USD` — фиксированный USD-лимит.  
Backtest: `max_position_pct = 0.25` → 25% × $10,000 = $2,500.

При балансе $102,415: backtest даёт $2,500, live даёт $3,000 → разница 20%.  
При оптимизации под $10,000 initial_balance результаты нерелевантны для реального баланса $102,415.

### 5.4 Конфликт частоты сигналов SMC

**Live**: `generate_signal` вызывается каждый M5-тик.  
**Backtest**: `smc_generate_signal_every_n = 12` → раз в 60 мин.  
**Следствие**: backtest даёт SMC 12× меньше возможностей для входа.

### 5.5 Исправленный критический баг (сессия 47)

`generate_signals_m5()` использовал неверный ключ: `.get("trend", ...)` вместо `.get("current_trend", ...)`. Словарь `get_current_structure()` возвращает `"current_trend"`, поэтому fallback всегда возвращал `RANGING` → 97.8% вызовов блокировалось.

**Результат до**: SMC = 0 сделок на 37 парах × 50k баров.  
**Результат после**: 0 → 1086 сделок на 2000 баров синтетики (smoke test).

---

## 6. Унификация параметров стратегий

### 6.1 Проблема разнородности

Одна концепция — разные имена параметров:

| Концепция | Grid | DCA | TrendFollower | SMC |
|-----------|------|-----|---------------|-----|
| Размер сделки | `amount_per_grid` | `amount_per_step` | `risk_per_trade_pct` | `risk_per_trade` |
| Макс. позиция | `max_position_size` | `max_position_size` | `max_position_size_usd` | `max_position_size` |
| Тейк-профит | `profit_per_grid` (%) | `take_profit_percentage` (%) | ATR × multiplier | `min_risk_reward` (R:R) |
| Стоп-лосс | нет | нет | ATR-based | implicit in signal |

Кросс-стратегийная оптимизация невозможна без нормализации.

### 6.2 Целевая модель унификации

```
Уровень 1: Универсальные риск-параметры (все стратегии)
  risk_per_trade_pct    = % баланса, рискуемый на одну сделку
  max_position_pct      = % баланса в одной позиции
  max_daily_loss_pct    = % баланса, максимальный дневной убыток

Уровень 2: Стратегия-специфичные параметры
  Grid: num_levels, profit_per_grid_pct, range_pct
  DCA: trigger_pct, safety_order_multiplier, max_safety_orders
  TF: ema_fast, ema_slow, atr_period, tp_atr_mult, sl_atr_mult
  SMC: swing_length, min_risk_reward, confluence_required
```

### 6.3 Почему оптимизация невозможна без backtest

До проведения бэктеста с реальными данными и исправленным движком:
1. Нет данных о распределении сделок по стратегиям
2. Нет данных о влиянии cooldown на P&L
3. Нет данных о корреляции стратегий в разных режимах рынка
4. Нет данных о drawdown на реальных downtrend/uptrend периодах

**Вывод**: сначала исправить Live↔Backtest расхождения → собрать реальные данные → запустить Phase 2 оптимизацию → только тогда можно говорить об оптимальных параметрах.

---

## 7. Статус тестирования

| Набор тестов | Passing | Failing | Причина |
|---|---|---|---|
| SMC unit (`bot/tests/unit/smc/`) | 52 | 0 | ✅ |
| MarketRegime unit | 26 | 0 | ✅ |
| Backtest config sync | 34 | 0 | ✅ |
| SMC strategy tests | 109 | 7 | ⚠️ Pre-existing |
| Web tests | ~800 | 26 | ⚠️ Pre-existing (password) |
| SMC market_structure | нестабильны | 3 | ⚠️ Flaky (randomized data) |
| **Итого** | **~1687** | **~36** | — |

Все 36 failing тестов — pre-existing, не введены текущей разработкой.
