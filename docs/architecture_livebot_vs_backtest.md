# Архитектура: Live Bot vs BacktestOrchestratorEngine V3.0

> Дата: 2026-03-06 · Версия: v2.0.0
> Файлы: `bot/orchestrator/bot_orchestrator.py`, `bot/tests/backtesting/orchestrator_engine.py`

---

## 1. Высокоуровневая архитектура системы

```
┌─────────────────────────────────────────────────────────────────────┐
│                         TRADERAGENT v2.0                            │
│                                                                     │
│  ┌──────────────────────┐         ┌───────────────────────────┐    │
│  │     LIVE BOT          │         │  BACKTEST ENGINE V3.0     │    │
│  │  BotOrchestrator      │         │  BacktestOrchestratorEngine│   │
│  │                       │         │                           │    │
│  │  Data: Bybit REST/WS  │ ←─────→ │  Data: CSV files (local)  │    │
│  │  Exec: Real orders    │  same   │  Exec: MarketSimulator    │    │
│  │  State: PostgreSQL    │  logic  │  State: In-memory         │    │
│  │  Loop:  1s real-time  │         │  Loop:  M5 bar iteration  │    │
│  └──────────────────────┘         └───────────────────────────┘    │
│                                                                     │
│  Shared modules (identical):                                        │
│  ├── MarketRegimeDetector    (bot/orchestrator/market_regime.py)    │
│  ├── StrategyRouter          (bot/tests/backtesting/strategy_router)│
│  ├── Grid/DCA/TF/SMC adapters (bot/strategies/)                    │
│  ├── RiskManager             (bot/core/portfolio_risk_manager.py)   │
│  └── HybridCoordinator       (bot/core/trading_core/)              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Live Bot — алгоритм работы

### 2.1 Инициализация (однократно при старте)

```
BotApplication.run()
    └── BotOrchestrator.initialize()
          ├── Подключение к Bybit (ByBitDirectClient → api-demo.bybit.com)
          ├── Загрузка credentials из PostgreSQL
          ├── Инициализация стратегий (по config):
          │     ├── GridEngine          (если strategy=hybrid|grid)
          │     ├── DCAEngine           (если strategy=hybrid|dca)
          │     ├── TrendFollowerStrategy (если strategy=trend_follower)
          │     ├── SMCStrategyAdapter  (если strategy=smc)
          │     └── HybridStrategy      (если strategy=hybrid)
          ├── HybridCoordinator (TradingCore)
          ├── RiskManager (max_position=$3000, max_daily_loss=$600)
          ├── DCAStartupAnalyzer → _run_dca_catchup() (если catch_up_enabled)
          └── load_state() из PostgreSQL
```

### 2.2 Параллельные корутины (asyncio)

```
asyncio.gather(
    _main_loop(),           ← основная торговая логика (1s итерации)
    _price_monitor(),       ← подписка на WebSocket price feed
    _regime_monitor_loop(), ← периодическое обновление режима (60s)
    telegram_bot.polling()  ← обработка команд Telegram
)
```

### 2.3 Главный цикл `_main_loop()` — каждую секунду

```
┌─────────────────────────────────────────────────────────────────┐
│  ИТЕРАЦИЯ (каждую 1 секунду)                                    │
│                                                                 │
│  1. DAILY LOSS RESET                                            │
│     if UTC_date != last_reset_date:                             │
│         risk_manager.reset_daily_loss()                         │
│                                                                 │
│  2. КЭШИРОВАТЬ БАЛАНС                                           │
│     _cached_balance = await exchange.fetch_balance()            │
│                                                                 │
│  3. ОБНОВИТЬ АКТИВНЫЕ СТРАТЕГИИ (раз в 60s)                     │
│     └── detect_market_regime() → H1 OHLCV → MarketRegimeDetector│
│           ├── Если нет данных → все стратегии активны           │
│           ├── _REGIME_TO_STRATEGIES[recommendation]:            │
│           │     GRID         → {grid}                           │
│           │     DCA          → {dca}                            │
│           │     HYBRID       → {grid, dca}                      │
│           │     SMC          → {smc}                            │
│           │     HOLD/REDUCE  → {} (торговля остановлена)        │
│           ├── + trend_follower при BULL_TREND / BEAR_TREND      │
│           ├── + smc при BULL/BEAR_TREND, VOLATILE, ACCUM/DISTR  │
│           ├── Cooldown guard: не чаще чем раз в 600s            │
│           ├── Confidence gate: только при confidence ≥ 0.3      │
│           └── Volatility guard: блок при ATR > 3% цены          │
│                                                                 │
│  4. GRID + DCA ЛОГИКА                                           │
│     if grid_active AND dca_active AND hybrid:                   │
│         HybridCoordinator.evaluate(adx):                        │
│             ADX < 25 → GRID_ONLY  → _process_grid_orders()      │
│             ADX ≥ 25 → DCA_ACTIVE → _process_dca_logic()        │
│     elif grid_active: _process_grid_orders()                    │
│     elif dca_active:  _process_dca_logic()                      │
│                                                                 │
│  5. TREND FOLLOWER ЛОГИКА (если is_strategy_active("tf"))       │
│     ├── fetch H1 OHLCV (100 баров)                              │
│     ├── analyze_market() → MarketConditions                     │
│     ├── check_entry_signal() → Signal + position_size           │
│     ├── risk_manager.check_trade() → allowed?                   │
│     └── execute_entry/exit via Bybit API                        │
│                                                                 │
│  6. SMC ЛОГИКА (если is_strategy_active("smc"))                 │
│     ├── КАЖДЫЙ ТИК: update_positions() → TP/SL проверка         │
│     └── РАЗДЗ В 5 МИН (_smc_analysis_interval):                 │
│           ├── fetch D1/H4/H1/M5 OHLCV (параллельно)             │
│           ├── smc_strategy.generate_signals_m5(df_h1, df_m5)    │
│           │     ├── H1: analyze() → current_trend               │
│           │     ├── M5: MarketStructureAnalyzer + ConfluenceZones│
│           │     └── EntrySignalGenerator → SMCSignal[]           │
│           ├── risk_manager.check_trade()                         │
│           └── execute_entry via Bybit API (если не dry_run)      │
│                                                                 │
│  7. ОБНОВИТЬ RISK MANAGER                                        │
│     risk_manager.update_balance(current_balance)                │
│                                                                 │
│  8. ПЕРИОДИЧЕСКОЕ СОХРАНЕНИЕ СОСТОЯНИЯ (каждые 300s)            │
│     save_state() → PostgreSQL                                   │
│                                                                 │
│  await asyncio.sleep(1)                                         │
└─────────────────────────────────────────────────────────────────┘
```

### 2.4 Маппинг Режим → Стратегии (Live)

```
Режим рынка          │ Grid │ DCA  │ TF   │ SMC  │ Hybrid
─────────────────────┼──────┼──────┼──────┼──────┼────────
BULL_TREND           │  ✗   │  ✗   │  ✓   │  ✓   │   –
BEAR_TREND           │  ✗   │  ✗   │  ✓   │  ✓   │   –
TIGHT_RANGE          │  ✓   │  ✗   │  ✗   │  ✗   │  Grid
WIDE_RANGE           │  ✗   │  ✓   │  ✗   │  ✗   │  DCA
QUIET_TRANSITION     │  ✓   │  ✗   │  ✗   │  ✗   │  Grid
VOLATILE_TRANSITION  │  ✗   │  ✓   │  ✗   │  ✓   │  DCA
(нет данных)         │  ✓   │  ✓   │  ✓   │  ✓   │  Hybrid
```

---

## 3. BacktestOrchestratorEngine V3.0 — алгоритм работы

### 3.1 Инициализация (однократно)

```
engine.run(data, config)
    ├── _build_strategies(config)
    │     ├── SMCStrategyAdapter  (из зарегистрированной factory)
    │     ├── GridAdapter
    │     ├── DCAAdapter
    │     └── TrendFollowerAdapter
    ├── MarketSimulator(initial_balance, maker_fee=0.02%, taker_fee=0.055%)
    ├── MarketRegimeDetector()
    ├── StrategyRouter(cooldown_bars=120)
    └── RiskManager(
              max_position = balance × max_position_size_pct (25%),
              max_daily_loss = balance × max_daily_loss_pct (6% после P0.3)
          )
```

### 3.2 Загрузка данных

```
MultiTimeframeDataLoader.load_csv("data/historical/BTCUSDT_5m.csv")
    └── Возвращает MultiTimeframeData:
          ├── .m5  → исходные M5 бары (до 50,000)
          ├── .m15 → resample M5→M15
          ├── .h1  → resample M5→H1
          ├── .h4  → resample M5→H4
          └── .d1  → resample M5→D1
```

### 3.3 Главный цикл — каждый M5 бар

```
┌─────────────────────────────────────────────────────────────────┐
│  ИТЕРАЦИЯ (каждый M5 бар, от warmup_bars до конца данных)       │
│                                                                 │
│  КОНТЕКСТ: get_context_at(data, i, lookback=100)                │
│  → df_d1[100], df_h4[100], df_h1[100], df_m15[100], df_m5[100] │
│                                                                 │
│  ШАГ 1: ОБНАРУЖЕНИЕ РЕЖИМА (раз в 12 баров = 1 час)            │
│     if bar % 12 == 0 and len(df_h1) >= 60:                      │
│         current_regime = MarketRegimeDetector.analyze(df_h1)    │
│         → RegimeAnalysis(regime, confidence, recommended_strat) │
│                                                                 │
│  ШАГ 2: РОУТИНГ СТРАТЕГИЙ                                       │
│     router.on_bar(current_regime, bar_index)                    │
│     → RouterEvent(active_strategies, cooldown_remaining)        │
│     regime_weights = {                                          │
│         strat: 1.0 if strat in active_strategies else 0.0       │
│     }                                                           │
│     (cooldown: 120 баров = 600s, идентично live 600s)           │
│                                                                 │
│  ШАГ 3: ГЕНЕРАЦИЯ И ИСПОЛНЕНИЕ СИГНАЛОВ                        │
│     for each strategy:                                          │
│         if weight == 0.0: skip  ← P0.1 fix                     │
│                                                                 │
│         analyze_market (throttled):                             │
│             SMC:  каждые 60 баров (= 300s = 5 мин)             │
│             Grid/DCA/TF: каждый бар                             │
│                                                                 │
│         generate_signal:                                        │
│             SMC:  каждый бар (P0.4 fix, было каждые 12)         │
│             Grid/DCA/TF: каждый бар                             │
│                                                                 │
│         _handle_signal(signal, weight):                         │
│             ├── risk_manager.check_trade() → allowed?           │
│             ├── simulator.open_position(price, amount)          │
│             └── per_strategy_pnl[strat] += realized_pnl         │
│                                                                 │
│  ШАГ 4: УПРАВЛЕНИЕ ВЫХОДАМИ (_handle_exits)                     │
│     for each open position:                                     │
│         if price >= tp or price <= sl:                          │
│             simulator.close_position()                          │
│             per_strategy_pnl[strat] += pnl                     │
│     (аналог live SMC update_positions() каждый тик)             │
│                                                                 │
│  ШАГ 5: EQUITY CURVE                                            │
│     equity_curve.append({timestamp, price, portfolio_value,     │
│                           regime, active_strategies})            │
│                                                                 │
│  ШАГ 6: RISK MANAGER                                            │
│     risk_manager.update_balance(portfolio_value)                │
│     if bar % 288 == 0:  ← 288 × 5min = 1440min = 1 день        │
│         risk_manager.reset_daily_loss()                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Сравнительная таблица Live vs Backtest

| Компонент | Live Bot | Backtest V3.0 | Статус |
|-----------|----------|---------------|--------|
| **Данные** | Bybit REST + WebSocket | CSV файлы (resample) | ✅ Эквивалент |
| **Исполнение** | Bybit API (реальные ордера) | MarketSimulator | ✅ Эквивалент |
| **Комиссии** | Bybit VIP0: 0.02%/0.055% | maker=0.02%, taker=0.055% | ✅ Совпадает |
| **Скользкость** | Рыночная | slippage=0.03% | ✅ Аппроксимация |
| **Цикл** | 1 секунда (реальное время) | M5 бар (5 минут) | ✅ Ускорение |
| **Режим рынка** | MarketRegimeDetector (H1, 60s) | MarketRegimeDetector (H1, 12 баров) | ✅ Идентично |
| **Роутинг стратегий** | `_REGIME_TO_STRATEGIES` + guards | StrategyRouter (те же правила) | ✅ Совпадает |
| **Grid/DCA exclusive** | HybridCoordinator (ADX) | weight=0.0 по режиму | ✅ P0.1 |
| **Cooldown переключения** | 600s | 120 баров × 5min = 600s | ✅ Совпадает |
| **max_daily_loss** | $600 (BTC) | 6% × $10k = $600 | ✅ P0.3 |
| **Сброс daily_loss** | UTC-смена дня | каждые 288 баров = 1440min | ✅ Совпадает |
| **SMC частота сигналов** | Каждые 5 мин (\_smc\_analysis\_interval) | Каждый M5 бар | ✅ P0.4 |
| **SMC TP/SL** | Каждую секунду | Каждый M5 бар (5 мин) | 🟡 Допущение |
| **Symbol format** | BTC/USDT | BTCUSDT → BTC/USDT | ✅ P0.4 |
| **Live params из YAML** | phase7_demo.yaml | from_yaml_config() | ✅ P0.3 |
| **State persistence** | PostgreSQL (save/load) | In-memory (нет) | 🟡 Не нужен |
| **DCA catch-up** | DCAStartupAnalyzer | Нет (warmup заменяет) | 🟡 Допущение |
| **Confidence gate** | ≥ 0.3 + duration ≥ 120s | ≥ 0.3 (нет duration gate) | 🟡 Незначительно |

Статусы: ✅ Совпадает/эквивалент · 🟡 Допустимое расхождение · ❌ Критическое расхождение

---

## 5. Оставшиеся расхождения (допустимые)

### 5.1 SMC TP/SL granularity (🟡)
- **Live**: TP/SL проверяется каждую секунду по WebSocket-цене
- **Backtest**: проверяется раз в 5 минут (конец M5 бара)
- **Влияние**: незначительное для swing-стратегии; TP/SL могут срабатывать с погрешностью ≤5 мин
- **Решение**: принято как архитектурное ограничение (внутри M5 данных нет тиков)

### 5.2 DCA catch-up (🟡)
- **Live**: `DCAStartupAnalyzer` при старте анализирует историю и имитирует пропущенные ордера
- **Backtest**: warmup-период (500 баров) заменяет catch-up — стратегии "разогреваются" без торговли
- **Влияние**: первые 500 баров backtest может иметь меньше открытых DCA-позиций

### 5.3 Confidence + duration gate для переключения (🟡)
- **Live**: дополнительная проверка `MIN_REGIME_DURATION_SECONDS=120` и `MAX_VOLATILITY_ATR_PCT=3.0`
- **Backtest**: только confidence gate (≥0.3), duration gate отсутствует
- **Влияние**: backtest может переключать стратегии чуть чаще в нестабильные периоды

---

## 6. Архитектура данных для Backtest

```
data/historical/
├── BTCUSDT_5m.csv      ← базовые данные (до 889k строк от 2017)
├── BTCUSDT_1h.csv      ← (не используется — backtest ресемплирует из 5m)
├── ETHUSDT_5m.csv
└── ...  (45 пар × 10 TF = 450 файлов, 5.4 GB)

MultiTimeframeDataLoader.load_csv(filepath)
    ├── Читает только *_5m.csv
    └── Ресемплирует:
          5m → 15m (groupby 3 бара)
          5m → 1h  (groupby 12 баров)
          5m → 4h  (groupby 48 баров)
          5m → 1d  (groupby 288 баров)
```

---

## 7. Схема прохождения сигнала (end-to-end)

```
                    LIVE BOT                    BACKTEST V3.0
                    ────────                    ─────────────
Данные      Bybit WebSocket (цена)         CSV → resample → df_m5
            Bybit REST (OHLCV каждые 5m)   get_context_at(i)

Режим       H1 OHLCV → RegimeDetector      H1 slice → RegimeDetector
            → recommended_strategy          → recommended_strategy

Роутинг     _REGIME_TO_STRATEGIES           StrategyRouter.on_bar()
            + cooldown 600s                 + cooldown 120 bars
            + confidence 0.3               + confidence 0.3
            → _active_strategies set        → regime_weights {0.0/1.0}

Стратегия   if is_strategy_active("smc"):   if weight == 1.0:
            _process_smc_logic()              generate_signal(df_m5)

SMC         generate_signals_m5(h1, m5)    generate_signals_m5(h1, m5)
            → SMCSignal(entry, SL, TP)      → SMCSignal(entry, SL, TP)

Риск        RiskManager.check_trade()       RiskManager.check_trade()
            max_daily_loss=$600             max_daily_loss=6%×$10k=$600

Исполнение  Bybit API.place_order()         MarketSimulator.open_position()
            реальный ордер                  виртуальный P&L

TP/SL       WebSocket цена каждую 1s        Close price каждые 5 min
            → smc_strategy.update_positions → _handle_exits()

Результат   PostgreSQL + Telegram           OrchestratorBacktestResult JSON
```

---

## 8. Запуск Backtest V3.0

```bash
# Одна пара (smoke-тест)
python scripts/run_backtest_v2.py \
    --mode single --symbol BTCUSDT \
    --data-dir data/historical --max-bars 5000 \
    --live-config configs/phase7_demo.yaml

# Phase 1: все 45 пар параллельно (14 воркеров)
python scripts/run_backtest_v2.py \
    --mode multi \
    --symbols "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,ADAUSDT,...(45 пар)" \
    --data-dir data/historical --max-bars 50000 \
    --workers 14 \
    --live-config configs/phase7_demo.yaml

# Результаты: results/backtest_v2/multi_YYYYMMDD_HHMMSS/phase1_BTCUSDT.json
```

---

*Создан: 2026-03-06 · Обновить при изменении логики routing или стратегий*
