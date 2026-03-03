# TRADERAGENT — Сравнение архитектур: Live Bot vs Backtesting

> Версия: v3.1 | Дата: 2026-03-03
> Предыдущая версия: 2026-02-28 (не включала Сессии 42-43 и SMC M5 вход)
> Подробности: [architecture_bot.md](architecture_bot.md) | [architecture_backtest.md](architecture_backtest.md)

---

## 1. Краткое сравнение

| Аспект                    | Live Bot                              | Backtest (V1 Pipeline)                    | Backtest (V2.0 Orchestrator)              |
|---------------------------|---------------------------------------|-------------------------------------------|-------------------------------------------|
| **Цель**                  | Реальная торговля 24/7                | Оптимизация параметров, 45 пар            | Валидация стратегии, портфельный анализ   |
| **Движок**                | `BotOrchestrator`                     | `MultiTFBacktestEngine`                   | `OrchestratorBacktestEngine`              |
| **Стратегии**             | Grid, DCA, TF, SMC, Hybrid            | DCA, TF, SMC (без Grid, без Hybrid)       | Grid, DCA, TF, SMC (через StrategyRouter) |
| **Время**                 | Реальное (asyncio, 1s цикл)           | Ускоренное (итерация по свечам)           | Ускоренное (итерация по свечам)           |
| **Анализ рынка (SMC)**    | каждые 300 сек (~60 свечей M5)        | каждые 24 свечи M5 (2 часа) ❌           | каждые 12 свечей M5 (1 час) ⚠️           |
| **Режимы рынка**          | `MarketRegimeDetector` (каждые 60s)   | опционально (`enable_regime_filter`)      | `StrategyRouter` + `MarketRegimeDetector` |
| **Источник данных**       | REST API биржи                        | CSV-файлы (45 пар × 10 TF)               | CSV-файлы                                 |
| **Исполнение ордеров**    | Биржа (реальное / demo)               | `MarketSimulator` (in-memory)             | `MarketSimulator` (in-memory)             |
| **Параллелизм**           | asyncio (I/O-bound)                   | `ProcessPoolExecutor` (CPU-bound)         | asyncio.gather (CPU-bound)                |
| **Скрипт**                | `bot/main.py`                         | `scripts/run_dca_tf_smc_pipeline.py`      | `scripts/run_backtest_v2.py`              |

---

## 2. Текущий статус деплоя (2026-03-03)

### Активные боты (Bybit Demo, 185.233.200.13)

```yaml
# configs/phase7_demo.yaml — актуальное состояние после Сессий 42-43
bots:
  - name: demo_btc_hybrid          # BTC/USDT, Grid 64k-74k, auto_start: true
  - name: demo_eth_grid            # ETH/USDT, Grid, $30/уровень, auto_start: true
  - name: demo_sol_dca             # SOL/USDT, DCA, trigger=2%, catch_up_enabled: true
  - name: demo_btc_trend           # BTC/USDT, Trend Follower, auto_start: true
  - name: demo_btc_smc             # BTC/USDT, SMC, swing_length=10, min_risk_reward=2.0, dry_run=false
```

### Ключевые изменения параметров (Сессии 42-43)

| Параметр | Было | Стало | Сессия |
|----------|------|-------|--------|
| `SMC.min_risk_reward` | 2.5 | **2.0** | 43 |
| `SMC.dry_run` | true | **false** | 43 |
| `SMC.swing_length` | 50 | **10** | 42 |
| `ETH Grid.amount_per_grid` | $20 | **$30** | 42 |
| `BTC Hybrid.upper_price` | 69k | **74k** | 42 |
| `BTC Hybrid.lower_price` | 62k | **64k** | 42 |
| `SOL DCA.catch_up_enabled` | false | **true** | 42 |
| `TF.auto_start` | false | **true** | 43 |

---

## 3. Алгоритмические изменения (Сессии 42-43)

### 3.1 SMC: двойной таймфрейм M5+H1 (Сессия 42)

```python
# bot/strategies/smc_adapter.py — НОВЫЙ КОД
def analyze_market(self, *df_list):
    m5_explicitly_provided = len(df_list) >= 5  # флаг предотвращает ложный путь при padding

    if m5_explicitly_provided and df_m5 не пустой:
        # НОВЫЙ ПУТЬ: H1 структура + M5 точный вход
        signals = self.strategy.generate_signals_m5(df_h1, df_m5)
    else:
        # СТАРЫЙ ПУТЬ: H1 структура + M15 вход (обратная совместимость)
        signals = self.strategy.generate_signals(df_h1, df_m15)
```

```python
# bot/strategies/smc/smc_strategy.py — НОВЫЙ МЕТОД
def generate_signals_m5(self, df_h1, df_m5) -> list[BaseSignal]:
    # H1: обнаружить BOS/CHoCH, определить структуру рынка
    # M5: точный вход в ордерный блок / FVG с R:R >= 2.0

# bot/strategies/smc/config.py — НОВЫЕ ПОЛЯ
swing_length_m5: int = 20     # для M5-анализа
swing_length_h1: int = 10     # для H1-структуры
m5_limit: int = 1000          # глубина истории M5
h1_limit: int = 200           # глубина истории H1
warmup_bars: int              # auto: max(swing_length * 4, 100)
```

**Таймфреймы в live-боте (SMC):**
```
D1 + H4 → макро-структура (BOS/CHoCH)
H1      → рабочий таймфрейм (ордерные блоки, FVG)
M5      → точный вход (новый путь, Сессия 42)
M15     → резервный вход (старый путь, если M5 недоступен)
```

### 3.2 DCA: режим догона (Сессия 42)

```python
# bot/orchestrator/bot_orchestrator.py
async def _run_dca_catchup(self):
    """Автоматически дооткрыть DCA-позицию после перезапуска бота,
    если цена значительно упала от точки последнего ордера."""
    # Активируется при catch_up_enabled: true в конфиге
    # SOL/USDT: включён для компенсации "Qty invalid" ошибок
```

### 3.3 Исправление точности ByBit LinearFutures (Сессия 43)

**Проблема:** Bybit возвращает несколько инструментов для одной пары:
- `SOLUSDT` (LinearPerpetual): `qtyStep=0.1` → precision=1 → валидный qty=0.2
- `SOLUSDT-06MAR26` (LinearFutures): `qtyStep=0.01` → precision=2 → невалидный qty=0.24
- + 3 более датированных фьючерса

`fetch_markets()` использовал `f"{base}/{quote}"` как ключ словаря — датированные фьючерсы перезаписывали точность perpetual → ошибки "Qty invalid" в SOL DCA.

**Исправление (commit 9d53210):**
```python
# bot/api/bybit_direct_client.py
for instrument in instruments:
    if instrument.get("contractType") != "LinearPerpetual":
        continue  # пропускать датированные фьючерсы
```

---

## 4. Жизненный цикл торгового цикла

### Live Bot (каждые 1 сек)

```
Ticker API (5s)
      │
      ▼
Проверить режим рынка (60s) ← MarketRegimeDetector
      │
      ▼
Для каждой активной стратегии:
  ├─ [Grid]  сверить ордера с биржей
  ├─ [DCA]   check trigger/TP по цене
  ├─ [TF]    fetch_ohlcv(1h) → analyze → signal?
  └─ [SMC]   каждые 300 сек:
              fetch_ohlcv(1d, 4h, 1h, m5) → analyze_m5 → signal?
              каждую секунду: check TP/SL активных позиций
      │
      ▼ (если signal)
RiskManager.check_trade()
      │
      ▼
exchange.create_order() ──► БИРЖА (demo: api-demo.bybit.com)
      │
      ▼
save_state() (30s) → PostgreSQL
      │
      ▼
sleep(1)
```

### Backtest V1 (MultiTFBacktestEngine, каждая M5 свеча)

```
CSV → DataFrame
      │
      ▼
for каждой M5 свечи:
  ├─ [каждые 24 свечи M5 = 2 часа]  ← РАСХОЖДЕНИЕ (в боте каждые ~60 свечей)
  │   strategy.analyze_market(df_d1, df_h4, df_h1, df_m15)  ← M5 НЕ передаётся
  │
  ├─ strategy.generate_signal(df_m15, balance) → signal?
  ├─ MarketSimulator.fill_orders()   ← мгновенный матчинг
  └─ update_positions(price)

return BacktestResult {metrics, trade_history, equity_curve}
```

### Backtest V2.0 (OrchestratorBacktestEngine, каждая M5 свеча)

```
CSV → MultiTimeframeData {m5, m15, h1, h4, d1}
      │
      ▼
for каждой M5 свечи (после warmup_bars=14400):
  ├─ [каждые 12 свечей M5 = 1 час]
  │   MarketRegimeDetector.analyze(df_h1) → regime
  │   StrategyRouter.on_bar(regime) → active_strategies
  │
  ├─ Для каждой активной стратегии:
  │   generate_signal() → risk_check → open_position()
  │   update_positions(price)
  │
  └─ equity_curve.append()

return OrchestratorBacktestResult {per_strategy_pnl, strategy_switches, ...}
```

---

## 5. Системы бэктестинга на тест-машине (158.160.215.57)

| Скрипт | Размер | Дата | Движок | Стратегии | Охват | Ключевые особенности |
|--------|--------|------|--------|-----------|-------|---------------------|
| `run_backtest_v2.py` | 22K | 28 фев | `OrchestratorBacktestEngine` | Grid+DCA+TF+SMC | Портфель, несколько пар | Точно повторяет логику live-бота, StrategyRouter, walk-forward, Monte Carlo, 5 исправлений движка |
| `run_dca_tf_smc_pipeline.py` | 43K | 3 мар | `MultiTFBacktestEngine` | DCA+TF+SMC | 45 пар × 3 стратегии (135 задач) | 5-фазный пайплайн, ProcessPoolExecutor, прогресс в Telegram, min_risk_reward=2.0 |
| `run_grid_backtest_all.py` | 15K | 23 фев | Кастомный Grid | Только Grid | 45 пар батчем | Классификация → оптимизация → стресс-тест → YAML пресеты |
| `run_grid_backtest.py` | 11K | 23 фев | Кастомный Grid | Только Grid | Одна пара | Интерактивный бэктест одной пары |
| `run_multi_strategy_backtest.py` | 8.4K | 27 фев | Смешанный | DCA+TF+SMC | Сравнение | HTML-отчёты, сравнение стратегий рядом |
| `run_replay.py` | 15K | 23 фев | Replay | Любая | Одна пара | Пошаговый прогон с визуализацией |
| `run_xrp_backtest.py` | 9.5K | 23 фев | Кастомный | Смешанный | Только XRP | Захардкожен под пару XRP |

---

## 6. Обработка ордеров: ключевые отличия

| Аспект                      | Live Bot                                      | Backtest                                        |
|-----------------------------|-----------------------------------------------|-------------------------------------------------|
| **Где исполняется**         | Bybit / другая биржа (HTTP)                   | `MarketSimulator` (память)                      |
| **Как определяется fill**   | polling `fetch_open_orders()` + сравнение ID  | price crossing limit price (мгновенно)          |
| **Гранулярность цены**      | Одна цена в момент polling                    | 4 цены на свечу (O/L/H/C)                       |
| **Скольжение (slippage)**   | Реальное (market impact, bid-ask spread)      | Нет (идеальное исполнение)                      |
| **Комиссии**                | Реальные (maker/taker из конфига биржи)       | Симулированные (maker_fee, taker_fee)           |
| **Книга ордеров**           | Реальная биржевая                             | Виртуальная (только свои ордера)                |
| **Контр-ордера (grid)**     | После подтверждения fill с биржи              | Сразу при crossed price                         |

---

## 7. Архитектурный долг и расхождения (актуально на 2026-03-03)

### ❌ Проблема 1: analyze_every_n=24 в V1 Pipeline (Сессия 44)

```
Live Bot:    SMC analyze_market() вызывается каждые 300 сек = ~60 M5 свечей
V1 Pipeline: analyze_every_n=24 → analyze_market() каждые 24 M5 свечи = 2 часа

РАСХОЖДЕНИЕ: бот анализирует рынок в 2.5 раза чаще, чем пайплайн.
V2.0 Pipeline: regime_check_every_n=12 → каждые 12 свечей M5 = 1 час. Ближе, но не 60.
```

### ❌ Проблема 2: Двойной прогрев SMC → 0 сделок в бэктесте (Сессия 44)

```
MultiTFBacktestEngine.warmup_bars = 100    ← движок пропускает первые 100 баров
SMCStrategy._generate_call_count < warmup_bars → skip  ← стратегия ещё 100 вызовов

Итого: ~200 баров × 5 мин = ~16 часов данных пропускается
Симптом: SMC выдаёт 0 сделок на любой паре в V1 Pipeline.
Подтверждено: Phase 1 завис на 80/135 — оставшиеся 55 задач SMC зависли.

Решение: убрать дублирование warmup (стратегия vs движок).
```

### ❌ Проблема 3: M5 данные не передаются в V1 Pipeline

```
Live Bot:    smc_adapter.analyze_market(df_d1, df_h4, df_h1, df_m15, df_m5)
             → m5_explicitly_provided=True → generate_signals_m5(df_h1, df_m5)

V1 Pipeline: analyze_market(df_d1, df_h4, df_h1, df_m15)  ← только 4 таймфрейма
             → m5_explicitly_provided=False → generate_signals(df_h1, df_m15)
             → старый путь M15 (алгоритм Сессии 41)

Следствие: бэктест использует устаревший алгоритм SMC-входа.
```

### ⚠️ Проблема 4: MarketRegimeDetector не подключён в live-боте

```
Live Bot:  _regime_monitor_loop() → MarketRegimeDetector.detect_market_regime()
           НО: результат НЕ читается в _main_loop
           _update_active_strategies() вызывается, но переключение не активно

Backtest V2.0: StrategyRouter.on_bar(regime) → активирует/деактивирует стратегии

Следствие: live-бот не адаптируется к рынку, хотя детектор работает.
```

### ⚠️ Проблема 5: Слипаж не моделируется в бэктесте

```
Backtest: цена исполнения = цена в книге ордеров (ideal fill)
Live Bot: цена исполнения ≠ запрошенная (bid-ask spread, market impact)

Следствие: реальные результаты хуже бэктестовых,
особенно для рыночных ордеров и низколиквидных пар.
```

### ⚠️ Проблема 6: Расхождение warmup_bars (V1 vs V2)

```
V1 MultiTFBacktestEngine.warmup_bars = 100
V2 OrchestratorBacktestConfig.warmup_bars = 14400  (= 50 дней M5 данных)

V2 намеренно использует длинный прогрев, чтобы режимный фильтр
имел достаточно истории для определения тренда.
```

---

## 8. Параметры бэктестирования vs живой бот

| Параметр | Live Bot | V1 Pipeline | V2 Pipeline | Статус |
|----------|----------|-------------|-------------|--------|
| SMC `swing_length` | 10 | 10 | 10 | ✅ |
| SMC `min_risk_reward` | **2.0** | **2.0** (исправлено Сессия 44) | 2.0 | ✅ |
| SMC `risk_per_trade` | 2% | 2% | 2% | ✅ |
| SMC M5 вход | **Да** (Сессия 42) | **Нет** (M15 только) | **Нет** | ❌ |
| DCA `price_deviation_pct` | 2% | 2% | 2% | ✅ |
| TF `ema_fast_period` | 20 | 20 | 20 | ✅ |
| TF `ema_slow_period` | 50 | 50 | 50 | ✅ |
| TF `max_atr_filter_pct` | 5% | 5% | 5% | ✅ |
| Режимы рынка | Детектируется, не применяется | опционально | StrategyRouter | ⚠️ |
| Grid стратегия | Да | Нет | Да | ⚠️ |

---

## 9. Результаты Phase 1 (частичные, Сессия 44)

Попытка запуска Phase 1 на тест-машине (158.160.215.57):
- 45 пар × 3 стратегии = 135 задач, 14 воркеров, загрузка CPU 97%
- **Прогресс: 80/135 задач → зависание ~1.5 часа**

```
Хронология:
17:39 — старт
18:43 — прогресс 80/135 → задачи перестали завершаться
20:16 — мониторинг убит, процесс остановлен

Задачи 1-80:   DCA и TF → завершились нормально
Задачи 81-135: SMC     → зависли (двойной прогрев + 0 сделок)
```

---

## 10. Что нужно синхронизировать (Roadmap)

| Задача | Приоритет | Статус |
|--------|-----------|--------|
| Исправить двойной прогрев SMC в V1 Pipeline | 🔴 ВЫСОКИЙ | ❌ Не сделано |
| Передавать M5 данные в V1 Pipeline | 🔴 ВЫСОКИЙ | ❌ Не сделано |
| Выбрать V1 или V2 как основную систему | 🔴 ВЫСОКИЙ | ❌ Ожидает решения |
| Подключить MarketRegimeDetector к `_main_loop` | 🟡 СРЕДНИЙ | ❌ Не сделано |
| Добавить slippage model в backtest | 🟡 СРЕДНИЙ | ❌ Не сделано |
| Исправить analyze_every_n (V1: 24→60) | 🟡 СРЕДНИЙ | ❌ Не сделано |
| Завершить Phase 1 после исправления SMC | 🟡 СРЕДНИЙ | ❌ Заблокировано |
| Запустить Phase 2 оптимизацию | 🟢 НИЗКИЙ | ❌ Заблокировано |
| Скачать 7 недостающих пар (NEAR, APT, PEPE, WIF, BONK, SUI, SEI) | 🟢 НИЗКИЙ | ❌ Не сделано |
| Кэш индикаторов в Parquet | 🟢 НИЗКИЙ | ❌ Не сделано |

---

## 11. Диаграмма общей архитектуры (актуально на 2026-03-03)

```
                        TRADERAGENT v2.3
                              │
          ┌───────────────────┴───────────────────┐
          │                                       │
     LIVE BOT                              BACKTEST SYSTEMS
     (185.233.200.13)                      (158.160.215.57)
     bot/main.py                                  │
          │                         ┌─────────────┼─────────────┐
     BotOrchestrator ×5             │             │             │
     ├─ demo_btc_hybrid             V1 Pipeline   V2 Pipeline   Grid
     ├─ demo_eth_grid               (43K, 5-фаз)  (22K, Orch)   (15K, batch)
     ├─ demo_sol_dca (catch_up✓)
     ├─ demo_btc_trend              MultiTF       Orchestrator
     └─ demo_btc_smc (M5 вход✓)    Engine        Engine
          │                         analyze_24M5  analyze_12M5
     SMC адаптер                    warmup=100×2  warmup=14400
     ├─ H1+M5 путь (новый)              ↑ ПРОБЛЕМА ❌
     └─ H1+M15 путь (старый)
                                    SMC использует старый путь M15 ❌
     Стратегии (общий код)          M5 данные не передаются ❌
     ├── GridEngine
     ├── DCAEngine                  Обе системы используют один
     ├── TrendFollower              и тот же код стратегий
     └── SMCStrategy
         ├─ generate_signals()      (M15 путь — старый)
         └─ generate_signals_m5()  (M5 путь — только live-бот)

     Инфраструктура
     ├── PostgreSQL (state)
     ├── Redis (events)
     ├── Prometheus (metrics)
     └── Telegram (alerts + control)
```

---

## Changelog

### v3.1 (2026-03-03, Сессии 42-44)

| Изменение | Детали |
|-----------|--------|
| **SMC M5 двойной таймфрейм** | `smc_adapter.py` маршрутизирует на `generate_signals_m5(df_h1, df_m5)` при наличии M5. Флаг `m5_explicitly_provided` предотвращает ложный путь при padding. |
| **SMC min_risk_reward=2.0** | Изменён с 2.5 на 2.0 в live-боте и обоих пайплайнах (V1 исправлен в Сессии 44) |
| **SMC config.py новые поля** | `swing_length_m5=20`, `swing_length_h1=10`, `m5_limit=1000`, `h1_limit=200`, `warmup_bars=auto` |
| **DCA catch-up режим** | `_run_dca_catchup()` в оркестраторе, включён для SOL/USDT (`catch_up_enabled: true`) |
| **ByBit LinearFutures исправление** | `fetch_markets()` пропускает `contractType != LinearPerpetual` (commit 9d53210) |
| **ByBit qtyStep** | Используется `qtyStep` вместо `basePrecision` для точности количества (commit 7793d2b) |
| **Конфиг BTC Hybrid** | Переcentrирован: 64k-74k (было 62k-69k) |
| **Конфиг ETH Grid** | $30/уровень (было $20, ниже минимума Bybit 0.01 ETH) |
| **Все 5 ботов активны** | SMC: `dry_run=false`, TF: `auto_start=true` |
| **V2.0 Pipeline задокументирован** | `run_backtest_v2.py` добавлен в таблицу систем |
| **Проблема двойного прогрева SMC** | Задокументирована: engine 100 + strategy 100 = 0 сделок |
| **Результаты Phase 1** | 80/135 DCA+TF выполнено, SMC завис — задокументировано |
| **Системы тестирования** | Таблица 7 скриптов с особенностями добавлена |
| **Roadmap обновлён** | Приоритеты для SMC бэктест-исправлений |

### v3.0 (2026-02-28, Сессии 38-41)

| Изменение | Детали |
|-----------|--------|
| **V2.0 Orchestrator Engine** | `OrchestratorBacktestEngine` + `StrategyRouter` + `PortfolioBacktestEngine` |
| **Режимный фильтр в бэктесте** | opt-in `enable_regime_filter=True` |
| **PortfolioBacktestEngine** | Одновременный бэктест N пар, корреляционная матрица |
| **5 ботов активны** | Hybrid, Grid, DCA, Trend Follower, SMC (dry_run) |
| **Phase 1 V1 baseline** | 135 задач, результаты в `phase1_baseline.json` |

---

> **Последнее обновление:** 2026-03-03 | **Сессия:** 44
> **Co-Authored:** Claude Sonnet 4.6
