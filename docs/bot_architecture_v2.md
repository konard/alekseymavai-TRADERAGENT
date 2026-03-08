# Архитектура живого бота — блок-схемы и алгоритмы (v2)

> Актуально на: 2026-03-08 · Версия: v2.1.0 · Коммит: `f3b2a78`
>
> Предыдущая версия: [bot_architecture.md](bot_architecture.md) (v2.0.0)

---

## Содержание

1. [Обзор системы](#1-обзор-системы)
2. [Жизненный цикл бота](#2-жизненный-цикл-бота)
3. [Главный цикл](#3-главный-цикл)
4. [Определение режима рынка](#4-определение-режима-рынка)
5. [SMC-ядро (core/smc)](#5-smc-ядро-coresmc)
6. [Маршрутизация стратегий](#6-маршрутизация-стратегий)
7. [Grid-стратегия](#7-grid-стратегия)
8. [DCA-стратегия](#8-dca-стратегия)
9. [TrendFollower-стратегия](#9-trendfollower-стратегия)
10. [SMC-стратегия](#10-smc-стратегия)
11. [Hybrid-режим (Grid↔DCA)](#11-hybrid-режим-griddca)
12. [Risk Management (3 уровня)](#12-risk-management-3-уровня)
13. [Graceful Transition](#13-graceful-transition)
14. [Health Monitor и автовосстановление](#14-health-monitor-и-автовосстановление)
15. [Система событий](#15-система-событий)
16. [Exchange Client (Bybit V5)](#16-exchange-client-bybit-v5)
17. [Сводная таблица](#17-сводная-таблица)

---

## 1. Обзор системы

```mermaid
graph TD
    YAML[configs/phase7_demo.yaml] --> APP[BotApplication]
    APP --> |"создаёт N ботов"| ORCH[BotOrchestrator × N]

    ORCH --> EX[ByBitDirectClient\nBybit V5 API]
    ORCH --> MRD[MarketRegimeDetector\n8 режимов + гистерезис]
    ORCH --> SS[StrategySelector\nрежим → стратегии]
    ORCH --> SR[StrategyRegistry\nstate machine × 7 состояний]
    ORCH --> RM[RiskManager\nper-bot лимиты]
    ORCH --> HM[HealthMonitor\n30с цикл проверок]
    ORCH --> TC[TradingCore\nshared config + hybrid coord]

    ORCH --> GRID[GridEngine]
    ORCH --> DCA[DCAEngine]
    ORCH --> TF[TrendFollowerStrategy]
    ORCH --> SMC_S[SMCStrategyAdapter]
    ORCH --> HYB[HybridStrategy\nGrid↔DCA координатор]

    MRD --> |"RegimeAnalysis"| SS
    SS --> |"SelectionResult\nto_start / to_stop / to_keep"| ORCH

    EX --> |"price / OHLCV / orders"| ORCH
    ORCH --> |"create/cancel orders"| EX

    SMC_CORE[bot/core/smc/\nSMCAnalyzer] --> |"SMCContext"| MRD
    SMC_CORE --> |"SMCContext"| SMC_S

    PRM[PortfolioRiskManager\noptional, кросс-пар] -.-> ORCH

    DB[(PostgreSQL\nTimescaleDB)] --> |"restore state"| ORCH
    ORCH --> |"save state\nкаждые 30с"| DB

    HM_DB[HistoryManager\nOHLCV cache] -.-> ORCH

    TG[Telegram Bot] --> |"команды"| APP
    ORCH --> |"события → уведомления"| TG
```

### Ключевые компоненты

| Компонент | Файл | Ответственность |
|-----------|------|-----------------|
| `BotOrchestrator` | `bot/orchestrator/bot_orchestrator.py` (~2365 строк) | Главный координатор: цикл, стратегии, ордера |
| `MarketRegimeDetector` | `bot/orchestrator/market_regime.py` | 8-режимный классификатор с ADX-гистерезисом |
| `StrategySelector` | `bot/orchestrator/strategy_selector.py` | Маппинг режим → стратегии, cooldown, transition gates |
| `StrategyRegistry` | `bot/orchestrator/strategy_registry.py` | State machine жизненного цикла стратегий |
| `HealthMonitor` | `bot/orchestrator/health_monitor.py` | Мониторинг здоровья, авторестарт |
| `SMCAnalyzer` | `bot/core/smc/analyzer.py` | Структурный анализ: свинги, BOS/CHoCH, OB, FVG |
| `RiskManager` | `bot/core/risk_manager.py` | Per-bot лимиты: позиция, daily loss, SL |
| `PortfolioRiskManager` | `bot/core/portfolio_risk_manager.py` | Кросс-пар: корреляция, utilization cap, портфельный halt |
| `TradingCore` | `bot/core/trading_core/core.py` | Общий конфиг live↔backtest, HybridCoordinator |
| `ByBitDirectClient` | `bot/api/bybit_direct_client.py` (~1111 строк) | Bybit V5 REST API, подпись HMAC-SHA256, retry |

---

## 2. Жизненный цикл бота

```mermaid
stateDiagram-v2
    [*] --> STOPPED

    STOPPED --> STARTING: start()
    STARTING --> RUNNING: initialize OK

    RUNNING --> PAUSED: pause()
    PAUSED --> RUNNING: resume()

    RUNNING --> STOPPING: stop()
    PAUSED --> STOPPING: stop()
    RUNNING --> EMERGENCY: emergency_stop()

    STOPPING --> STOPPED: cleanup done
    EMERGENCY --> STOPPED: force cleanup
```

### Инициализация (`initialize()`, строки 207-378)

```mermaid
flowchart TD
    START([initialize]) --> REDIS[Redis connect + ping]
    REDIS --> RM_INIT[RiskManager\nfetch balance → init]
    RM_INIT --> GRID_INIT{strategy ∈\ngrid/hybrid?}

    GRID_INIT --> |да| CREATE_GRID[GridEngine\nGridType.STATIC]
    GRID_INIT --> |нет| DCA_INIT

    CREATE_GRID --> DCA_INIT{strategy ∈\ndca/hybrid?}
    DCA_INIT --> |да| CREATE_DCA[DCAEngine]
    DCA_INIT --> |нет| HYB_INIT

    CREATE_DCA --> HYB_INIT{strategy ==\nhybrid?}
    HYB_INIT --> |да + оба engine| CREATE_HYB[HybridStrategy\n+ GridRiskManager]
    HYB_INIT --> |нет| TF_INIT

    CREATE_HYB --> TF_INIT{strategy ==\ntrend_follower?}
    TF_INIT --> |да| CREATE_TF[TrendFollowerStrategy\nconfig → dataclass\ninitial_capital]
    TF_INIT --> |нет| SMC_INIT

    CREATE_TF --> SMC_INIT{strategy == smc?}
    SMC_INIT --> |да| CREATE_SMC[SMCStrategyAdapter\nconfig → dataclass\ninitial_capital]
    SMC_INIT --> |нет| LOAD_STATE

    CREATE_SMC --> LOAD_STATE[load_state\nиз PostgreSQL]
    LOAD_STATE --> DONE([initialize OK])
```

### Запуск (`start()`, строки 380-493)

```mermaid
flowchart TD
    START([start]) --> STATE_CHK{state ==\nSTOPPED?}
    STATE_CHK --> |нет| SKIP([return])
    STATE_CHK --> |да| SET_STARTING[state = STARTING]
    SET_STARTING --> FETCH_PRICE[fetch_ticker\n→ current_price]
    FETCH_PRICE --> LOADED{Состояние\nзагружено из DB?}

    LOADED --> |да| RECONCILE[reconcile_with_exchange\nпроверить ордера на бирже]
    LOADED --> |нет| FRESH_GRID[grid_engine.initialize_grid\nplace orders на бирже]

    RECONCILE --> CATCH_UP
    FRESH_GRID --> CATCH_UP{DCA catch_up\nenabled?}

    CATCH_UP --> |да| RUN_CATCHUP[_run_dca_catchup\nвычислить уровни\nразместить ордера]
    CATCH_UP --> |нет| HISTORY

    RUN_CATCHUP --> HISTORY{SMC / TF\nстратегия?}
    HISTORY --> |да| START_FEED[_start_history_feed\nbackfill + WebSocket]
    HISTORY --> |нет| LAUNCH

    START_FEED --> LAUNCH[state = RUNNING\n_running = True]
    LAUNCH --> TASKS[asyncio.create_task ×3:\n• _main_loop\n• _price_monitor\n• _regime_monitor_loop]
    TASKS --> HM_START[health_monitor.start]
    HM_START --> EVENT[publish BOT_STARTED]
    EVENT --> DONE([Бот запущен])
```

---

## 3. Главный цикл

Каждые **1 секунду** (`asyncio.sleep(1)`):

```mermaid
flowchart TD
    START([Tick]) --> PAUSE{state ==\nPAUSED?}
    PAUSE --> |да| SLEEP([sleep 1s])
    PAUSE --> |нет| DAILY{UTC день\nсменился?}
    DAILY --> |да| RESET_DAILY[daily_loss = 0]
    DAILY --> |нет| BAL
    RESET_DAILY --> BAL

    BAL[Кешировать баланс\n_cached_balance] --> REGIME{Прошло ≥ 60с\nс последней\nпроверки режима?}
    REGIME --> |да| UPD[_update_active_strategies\n→ detect_market_regime\n→ StrategySelector]
    REGIME --> |нет| PROC
    UPD --> PROC

    PROC --> ACTIVE{Какие стратегии\nв _active_strategies?}

    ACTIVE --> |grid + dca + hybrid| HYB_P[_process_hybrid_logic\nHybridCoordinator.evaluate]
    ACTIVE --> |только grid| PGRID[_process_grid_orders\nпроверить fills → rebalance]
    ACTIVE --> |только dca| PDCA[_process_dca_logic\ntrigger / take_profit]

    HYB_P --> TF_CHK
    PGRID --> TF_CHK
    PDCA --> TF_CHK

    TF_CHK{trend_follower\n∈ active?} --> |да| PTF[_process_trend_follower_logic\nH1 100 свечей → вход/выход]
    TF_CHK --> |нет| SMC_CHK
    PTF --> SMC_CHK

    SMC_CHK{smc\n∈ active?} --> |да| PSMC[_process_smc_logic\nTP/SL каждый тик\nполный анализ каждые 5 мин]
    SMC_CHK --> |нет| RISK
    PSMC --> RISK

    RISK[_update_risk_manager\ncheck halt conditions] --> HALT{is_halted?}
    HALT --> |да| EMERGENCY[emergency_stop\nотмена всех ордеров]
    HALT --> |нет| SAVE

    SAVE{Прошло ≥ 30с?} --> |да| SAVEDB[save_state → DB]
    SAVE --> |нет| SLEEP2([sleep 1s])
    SAVEDB --> SLEEP2
```

### Паттерн размещения ордеров (все стратегии)

```mermaid
flowchart LR
    SIGNAL[Сигнал от стратегии] --> RISK_CHK[RiskManager\ncheck_trade]
    RISK_CHK --> |rejected| SKIP([Пропустить])
    RISK_CHK --> |OK| PORT_CHK[PortfolioRiskManager\ncheck_allocation]
    PORT_CHK --> |rejected| SKIP
    PORT_CHK --> |OK| DRY{dry_run?}
    DRY --> |да| LOG([Залогировать])
    DRY --> |нет| PLACE[Разместить ордер\nна бирже]
    PLACE --> |success| ADVANCE[Обновить состояние\nстратегии]
    PLACE --> |fail| NO_ADV([НЕ обновлять\nсостояние])
```

> **Принцип #231:** Ордер размещается на бирже **ДО** обновления состояния стратегии.
> Если ордер не прошёл — состояние не меняется (нет двойного учёта).

---

## 4. Определение режима рынка

Запускается каждые **60 секунд** (`regime_check_interval`).

```mermaid
flowchart TD
    OHLCV[fetch_ohlcv H1\n100 свечей] --> IND[Расчёт индикаторов]

    IND --> EMA["EMA 20 / EMA 50\newm(span, adjust=False)"]
    IND --> ADX["ADX 14\nWilder's smoothing\n+DI / -DI → DX → ADX"]
    IND --> ATR["ATR 14 %\ntrue_range.rolling(14).mean\n/ close × 100"]
    IND --> RSI["RSI 14\nWilder's smoothing"]
    IND --> BB["BB 20\nwidth = (upper-lower)/middle × 100%"]
    IND --> VOL["Volume Ratio\nvolume / volume.rolling(20).mean"]

    EMA --> CLASSIFY
    ADX --> CLASSIFY
    ATR --> CLASSIFY

    subgraph CLASSIFY [Классификация с гистерезисом]
        direction TB
        CUR{Текущий\nрежим?}

        CUR --> |"В ТРЕНДЕ"| TREND_EXIT{"ADX < 25?\n(exit_trending)"}
        TREND_EXIT --> |да| RECLASS[Переклассифицировать\nкак range/transition]
        TREND_EXIT --> |нет| KEEP_TREND[Оставить\nBULL/BEAR_TREND]

        CUR --> |"В RANGE"| RANGE_EXIT{"ADX > 22?\n(exit_ranging)"}
        RANGE_EXIT --> |да| RECLASS
        RANGE_EXIT --> |нет| KEEP_RANGE[Оставить\nTIGHT/WIDE_RANGE]

        CUR --> |"Другой / UNKNOWN"| STD[Стандартные\nпороги]
    end

    CLASSIFY --> SMC_CHK{SMC контекст\nдоступен?\nwarmup завершён?}
    SMC_CHK --> |да| SMC_OVR[analyze_with_smc\nSMC-оверлей]
    SMC_CHK --> |нет| RESULT

    SMC_OVR --> SMC_PHASE{SMCPhase?}
    SMC_PHASE --> |ACCUMULATION| ACC[→ ACCUMULATION\nconfidence = 0.6×base + 0.4]
    SMC_PHASE --> |DISTRIBUTION| DIST[→ DISTRIBUTION\nconfidence = 0.6×base + 0.4]
    SMC_PHASE --> |другой| RESULT

    ACC --> RESULT[RegimeAnalysis]
    DIST --> RESULT
    KEEP_TREND --> RESULT
    KEEP_RANGE --> RESULT
    STD --> RESULT
    RECLASS --> RESULT
```

### ADX-гистерезис (предотвращение осцилляции)

```
              ┌─────── exit_trending = 25 ────────┐
              │                                     │
   RANGING ───┤      TRANSITION ZONE (18-32)       ├─── TRENDING
              │                                     │
              └─────── exit_ranging = 22 ──────────┘

   Вход в ТРЕНД: ADX поднимается выше 32
   Выход из ТРЕНДА: ADX падает ниже 25 (не 32!)
   Вход в RANGE: ADX падает ниже 18
   Выход из RANGE: ADX поднимается выше 22 (не 18!)
```

### Таблица режимов (8 штук)

| Режим | ADX | ATR | EMA | Источник |
|-------|-----|-----|-----|----------|
| `TIGHT_RANGE` | < 18 | < 1% | любой | Индикаторы |
| `WIDE_RANGE` | < 18 | ≥ 1% | любой | Индикаторы |
| `QUIET_TRANSITION` | 22–32 | < 2% | любой | Индикаторы |
| `VOLATILE_TRANSITION` | 22–32 | ≥ 2% | любой | Индикаторы |
| `BULL_TREND` | > 32 | — | EMA20 > EMA50 | Индикаторы |
| `BEAR_TREND` | > 32 | — | EMA20 < EMA50 | Индикаторы |
| `ACCUMULATION` | любой | — | — | **SMC CHoCH ↑** |
| `DISTRIBUTION` | любой | — | — | **SMC CHoCH ↓** |

### Confluence Score (0.0–1.0)

| Компонент | Вес | Формула |
|-----------|-----|---------|
| ADX | 30% | `(ADX - 20) / 20`, clamp 0–1 |
| Тренд | 25% | `|trend_strength|` |
| RSI | 20% | Bull: `(RSI - 50) / 50`; Bear: `(50 - RSI) / 50` |
| Volume | 15% | > 1.5 → 1.0; 1.0–1.5 → linear; < 0.8 → `ratio / 0.8` |
| BB Width | 10% | 2–4% → 1.0; outside → falloff |

### Параметры детектора

| Параметр | Значение | Гистерезис |
|----------|----------|------------|
| `ema_fast` / `ema_slow` | 20 / 50 | — |
| `adx_period` | 14 | enter_trending=32, exit=25 |
| `atr_period` | 14 | enter_ranging=18, exit=22 |
| `rsi_period` | 14 | — |
| `bb_period` | 20 | — |
| `atr_wide_threshold` | 1.0% | tight/wide boundary |
| `atr_volatile_threshold` | 2.0% | quiet/volatile boundary |
| `confluence_threshold` | 0.7 | для HYBRID-рекомендации |
| Минимум данных | 64 свечи H1 | `max(ema_slow + atr_period, ...)` |

---

## 5. SMC-ядро (core/smc)

Модуль `bot/core/smc/` — единственный источник правды для SMC-анализа. Используется и `MarketRegimeDetector` (оверлей), и `SMCStrategyAdapter` (торговля).

### Общая архитектура

```mermaid
flowchart TD
    DF["DataFrame\nOHLCV (H1/M5)"] --> ANALYZER[SMCAnalyzer.analyze]

    ANALYZER --> SW[SwingDetector\nнайти пивоты]
    ANALYZER --> SD[StructuralDetector\nBOS / CHoCH]
    ANALYZER --> IMB[ImbalanceDetector\nFVG + Order Block]
    ANALYZER --> LIQ[SupplyDemandDetector\nEQH/EQL + зоны]

    SW --> |"SwingPoint[]"| SD
    SD --> |"StructureEvent[]"| IMB
    IMB --> |"FVG[] + OB[]"| LIQ
    LIQ --> |"LiquidityLevel[]"| CTX[SMCContext]

    CTX --> PHASE["phase: SMCPhase\nbias: -1.0 … +1.0\nwarmup_complete: bool"]

    style ANALYZER fill:#f9f,stroke:#333,stroke-width:2px
```

### 5.1 Swing Detector

**Алгоритм** (vectorised, O(n)):

```
Swing HIGH в позиции i ⟺ high[i] == max(high[i-strength : i+strength+1])
Swing LOW  в позиции i ⟺ low[i]  == min(low[i-strength : i+strength+1])
```

- `strength = 5` (по умолчанию) — количество баров в каждую сторону
- Бары в пределах `strength` от краёв DataFrame **не подтверждаются**
- Минимум данных: `2 × strength + 1 = 11` баров
- Реализация: `numpy.sliding_window_view` → rolling max/min

### 5.2 Structural Detector (BOS/CHoCH)

State-машина с тремя состояниями: `UNKNOWN → BULL / BEAR`

```mermaid
stateDiagram-v2
    [*] --> UNKNOWN

    UNKNOWN --> BULL: close > prev_SH\n(первый breakout вверх)
    UNKNOWN --> BEAR: close < prev_SL\n(первый breakout вниз)

    BULL --> BULL: close > prev_SH\n→ BOS_BULL\n(продолжение)
    BULL --> BEAR: close < prev_SL\n+ impulse ≥ 0.3×ATR\n→ CHOCH_BEAR\n(разворот!)

    BEAR --> BEAR: close < prev_SL\n→ BOS_BEAR\n(продолжение)
    BEAR --> BULL: close > prev_SH\n+ impulse ≥ 0.3×ATR\n→ CHOCH_BULL\n(разворот!)
```

**Фильтр шума**: `impulse < min_impulse_atr × ATR` → пропустить (не считать за breakout)

| Событие | Тренд ДО | Тренд ПОСЛЕ | Значение |
|---------|----------|-------------|----------|
| `BOS_BULL` | BULL | BULL | Продолжение бычьего тренда |
| `BOS_BEAR` | BEAR | BEAR | Продолжение медвежьего тренда |
| `CHOCH_BULL` | BEAR | BULL | Разворот вверх (накопление) |
| `CHOCH_BEAR` | BULL | BEAR | Разворот вниз (распределение) |

### 5.3 Order Block Detection

```mermaid
flowchart TD
    EVENT[StructureEvent\nна баре N] --> SEARCH["Поиск назад\nmax_ob_lookback=20 баров"]

    SEARCH --> TYPE{Тип события?}

    TYPE --> |"BOS_BULL / CHOCH_BULL"| FIND_BEAR["Найти последнюю\nмедвежью свечу\n(close < open)"]
    TYPE --> |"BOS_BEAR / CHOCH_BEAR"| FIND_BULL["Найти последнюю\nбычью свечу\n(close > open)"]

    FIND_BEAR --> BULL_OB["BULL Order Block\n(зона спроса)\nlow..high медвежьей свечи"]
    FIND_BULL --> BEAR_OB["BEAR Order Block\n(зона предложения)\nlow..high бычьей свечи"]

    BULL_OB --> VALID{Валидация}
    BEAR_OB --> VALID

    VALID --> |"close < OB.low"| INVALID[Invalidated ✗]
    VALID --> |"close ∈ [OB.low, OB.high]"| TOUCHED[Touched ✓]
    VALID --> |"close > OB.high (bull)\nclose < OB.low (bear)"| STILL_VALID[Valid ✓]
```

### 5.4 Fair Value Gap (FVG)

```
BULL FVG: high[i-2] < low[i]     → зона: [high[i-2], low[i]]
BEAR FVG: low[i-2]  > high[i]    → зона: [high[i], low[i-2]]
Фильтр: gap_size ≥ min_fvg_atr × ATR (default 0.2)
Filled: close ∈ [gap_low, gap_high]
```

### 5.5 Supply/Demand Levels

**EQH/EQL кластеризация:**
1. Сортировка swing points по цене
2. Группировка точек в пределах `tolerance_pct = 0.2%`
3. Группы с `touch_count ≥ 2` → `LiquidityLevel`
4. Strength: `min(1.0, touch_count / 5.0)`
5. Swept: EQH — `close > avg × 1.001`; EQL — `close < avg × 0.999`

**Supply/Demand из OB:**
- BULL OB → DEMAND level (strength = 0.7)
- BEAR OB → SUPPLY level (strength = 0.7)

### 5.6 Phase Derivation

```python
recent_events = last 10 structure_events
bull_count = count(is_bullish)
bear_count = len(recent) - bull_count
raw_bias = (bull_count - bear_count) / len(recent)
trend_bias = clip(raw_bias, -1.0, +1.0)

last_event = events[-1]
if last_event == BOS_BULL  → BULL_TREND
if last_event == BOS_BEAR  → BEAR_TREND
if last_event == CHOCH_BULL → ACCUMULATION
if last_event == CHOCH_BEAR → DISTRIBUTION
if no events              → RANGING
```

### Параметры SMC-ядра

| Параметр | Значение | Описание |
|----------|----------|----------|
| `swing_strength` | 5 | Баров в каждую сторону для подтверждения пивота |
| `min_warmup_bars` | 200 | Баров до `warmup_complete = True` |
| `min_impulse_atr` | 0.3 | Минимальный импульс (×ATR) для BOS/CHoCH |
| `min_fvg_atr` | 0.2 | Минимальный размер FVG (×ATR) |
| `tolerance_pct` | 0.002 | Кластеризация EQH/EQL (0.2%) |
| `min_eq_touches` | 2 | Минимум касаний для уровня ликвидности |
| `max_ob_lookback` | 20 | Баров назад для поиска свечи OB |
| `max_ob_count` | 10 | Хранить max 10 последних OB |
| `max_fvg_count` | 10 | Хранить max 10 последних FVG |

---

## 6. Маршрутизация стратегий

### `_update_active_strategies()` — полный flow

```mermaid
flowchart TD
    START([_update_active_strategies]) --> LOCK{strategy_locked?}
    LOCK --> |да| FORCE[force _active_strategies\n= _locked_strategies]
    FORCE --> END([return])

    LOCK --> |нет| FIRST{Первая\nитерация?}
    FIRST --> |да, _last_regime = 0| EAGER[detect_market_regime\nнемедленно]
    FIRST --> |нет| STALE
    EAGER --> STALE

    STALE{age > 2× interval?\nstale данные?} --> |да| WARN[log: stale_regime_data]
    STALE --> |нет| NO_REGIME
    WARN --> NO_REGIME

    NO_REGIME{analysis ==\nNone?} --> |да| FALLBACK["active = {grid, dca,\ntrend_follower, smc}"]
    FALLBACK --> END

    NO_REGIME --> |нет| VOL_GUARD{"ATR > 3.0%?\nвысокая\nволатильность"}
    VOL_GUARD --> |"да + расширение"| BLOCK[Заблокировать\nдобавление стратегий]
    BLOCK --> END
    VOL_GUARD --> |"нет (или сокращение)"| SELECT

    SELECT[StrategySelector.select\nрежим → стратегии] --> NEEDED{transition_needed?}
    NEEDED --> |нет| END
    NEEDED --> |да| DEACT{Есть деактивируемые\nстратегии?}

    DEACT --> |да| GRACE[_graceful_transition\ncancel orders, close positions]
    DEACT --> |нет| EXEC

    GRACE --> EXEC[strategy_selector\n.execute_transition]
    EXEC --> UPDATE["_active_strategies = intended\n_last_strategy_switch = now"]
    UPDATE --> END
```

### StrategySelector — transition gates

```mermaid
flowchart LR
    CHECK[Проверка перехода] --> BOOT{Первый\nпереход?}
    BOOT --> |да| PASS[✅ Пропустить\nвсе проверки]

    BOOT --> |нет| CD{Cooldown\n300с прошёл?}
    CD --> |нет| BLOCKED["🚫 Блокировка\n(cooldown active)"]
    CD --> |да| DUR{Режим\n≥ 120с?}
    DUR --> |нет| BLOCKED2["🚫 Блокировка\n(regime too young)"]
    DUR --> |да| CONF{Confidence\n≥ 0.30?}
    CONF --> |нет| BLOCKED3["🚫 Блокировка\n(low confidence)"]
    CONF --> |да| PASS2[✅ Переход\nразрешён]
```

### Маппинг режим → стратегии

| Режим | Стратегии | Веса | Приоритет |
|-------|-----------|------|-----------|
| `TIGHT_RANGE` | Grid | 1.0 | 1 |
| `WIDE_RANGE` | Grid | 1.0 | 1 |
| `QUIET_TRANSITION` | Grid | 0.7 | 1 |
| `VOLATILE_TRANSITION` | SMC | 1.0 | 1 |
| `BULL_TREND` | TF + DCA | 0.7 + 0.3 | 1, 2 |
| `BULL_TREND` + confluence ≥ 0.7 | DCA + Grid + TF | 0.5 + 0.3 + 0.2 | 1, 2, 3 |
| `BEAR_TREND` | DCA | 1.0 | 1 |
| `ACCUMULATION` | SMC | 1.0 | 1 |
| `DISTRIBUTION` | SMC | 1.0 | 1 |
| `REDUCE_EXPOSURE` | — | — | — |

### Параметры маршрутизации

| Параметр | Значение | Описание |
|----------|----------|----------|
| `strategy_switch_cooldown_seconds` | 300 с | Минимум между переключениями |
| `regime_check_interval_seconds` | 60 с | Частота проверки режима |
| `_MIN_REGIME_CONFIDENCE` | 0.30 | Мин. уверенность для переключения |
| `_MIN_REGIME_DURATION_SECONDS` | 120 с | Мин. возраст режима |
| `_MAX_VOLATILITY_ATR_PCT` | 3.0% | Блок расширения при волатильности |

---

## 7. Grid-стратегия

```mermaid
flowchart TD
    START([Запуск Grid]) --> RANGE["Определить диапазон\nlower_price .. upper_price"]
    RANGE --> LEVELS["Рассчитать N уровней\nлогарифмический шаг\nprofit_per_grid %"]
    LEVELS --> BAL_CHK{"Достаточно\nbase balance\nдля SELL?"}
    BAL_CHK --> |нет| SKIP_SELL["Пропустить SELL\nордера (#230)"]
    BAL_CHK --> |да| PLACE["Разместить ордера:\nBUY ниже цены\nSELL выше цены"]
    SKIP_SELL --> PLACE

    PLACE --> MONITOR([Мониторинг каждый тик])
    MONITOR --> CHECK_ORDERS["Для каждого tracked order:\nесть в open_orders?"]

    CHECK_ORDERS --> |"Исчез из open"| VERIFY["fetch_order\nверифицировать статус"]
    VERIFY --> |"status: closed"| FILLED
    VERIFY --> |"canceled/expired"| REMOVE[Удалить из трекинга]

    CHECK_ORDERS --> |"Всё ещё открыт"| MONITOR

    FILLED["grid_engine.handle_order_filled\n(order_id, filled_price, amount)"]
    FILLED --> REBALANCE{Rebalance\norder?}

    REBALANCE --> |"BUY filled"| PLACE_SELL["Разместить SELL\nна уровень выше\nprice × (1 + profit_per_grid)"]
    REBALANCE --> |"SELL filled"| PLACE_BUY["Разместить BUY\nна уровень ниже\nprice × (1 - profit_per_grid)"]
    REBALANCE --> |"None"| MONITOR

    PLACE_SELL --> MONITOR
    PLACE_BUY --> MONITOR
```

### Position Sizing

```
amount_per_grid = $150 (quote currency)
amount_base = amount_per_grid / level_price
quantized = round_down(amount_base, precision)
```

### Параметры Grid (BTC demo)

| Параметр | Значение | Описание |
|----------|----------|----------|
| `grid_levels` | 6 | Количество уровней сетки |
| `amount_per_grid` | $150 | Объём на каждый уровень |
| `profit_per_grid` | 1.2% | Шаг между уровнями |
| `stop_loss_percentage` | 12% | SL от нижней границы |
| `min_order_size` | $150 | Минимальный размер ордера |
| `capital_allocation` | 60% | Доля капитала (Hybrid-режим) |

---

## 8. DCA-стратегия

### Основной цикл

```mermaid
flowchart TD
    START([Тик]) --> PRICE[dca_engine.update_price\ncurrent_price]
    PRICE --> ACTIONS{dca_actions}

    ACTIONS --> |"dca_triggered: true"| RISK_CHK["RiskManager.check_trade\n+ PortfolioRM.check_allocation"]
    RISK_CHK --> |rejected| SKIP([Пропустить])
    RISK_CHK --> |OK| DRY{dry_run?}
    DRY --> |да| LOG([Залогировать])
    DRY --> |нет| PLACE["_place_dca_order\nmarket buy"]
    PLACE --> |success| ADVANCE["dca_engine.execute_dca_step\nобновить avg_price, total_amount"]
    PLACE --> |fail| NO_ADV([НЕ обновлять])
    ADVANCE --> EVENT[publish DCA_TRIGGERED]

    ACTIONS --> |"tp_triggered: true"| TP_FLOW
    TP_FLOW["dca_engine.close_position\n→ PnL"] --> CLOSE["Закрыть позицию\nmarket sell"]
    CLOSE --> RELEASE["Освободить капитал\nPortfolioRM.release"]
```

### Safety Orders (усреднение)

```mermaid
flowchart TD
    BASE["Base Order\nentry_price, base_volume"] --> DROP1{"Цена упала\nна trigger%\nот entry?"}
    DROP1 --> |да| SO1["Safety Order 1\nvolume = base × multiplier^1"]
    SO1 --> RECALC1["Пересчёт avg_price\nTP = avg × (1 + tp_pct)"]
    RECALC1 --> DROP2{"Ещё упала\nна trigger%?"}
    DROP2 --> |да| SO2["Safety Order 2\nvolume = base × multiplier^2"]
    SO2 --> RECALC2[Пересчёт avg_price]
    RECALC2 --> DROPN{"...до\nmax_steps?"}
    DROPN --> |да| SON["Safety Order N\nvolume = base × multiplier^N"]
    DROPN --> |"N = max_steps"| HOLD["Удерживать позицию\nдо TP или SL"]

    DROP1 --> |нет| TP_CHK{"price ≥ avg\n+ tp_pct?"}
    RECALC1 --> TP_CHK
    TP_CHK --> |да| CLOSE_ALL[Закрыть всю позицию\nFIX PROFIT]
    TP_CHK --> |нет| DROP1
```

### DCA Catch-up (при перезапуске бота)

```mermaid
flowchart TD
    START([_run_dca_catchup]) --> OHLCV["Fetch OHLCV\n+ open_orders"]
    OHLCV --> LEVELS["Вычислить уровни:\nprice_n = base × (1 - trigger% × n)\nдля n = 1..max_steps"]
    LEVELS --> FILTER["Отфильтровать:\n• ниже текущей цены\n• нет открытого ордера\n• amount ≥ min_lot_size"]
    FILTER --> SORT["Сортировка:\nот ближайшего к дальнему"]
    SORT --> LIMIT["Обрезать до\ncatch_up_max_orders (3)"]
    LIMIT --> PLACE["Разместить ордера\nна бирже"]
```

### Параметры DCA

| Параметр | BTC demo | SOL demo | Описание |
|----------|----------|----------|----------|
| `trigger_percentage` | 4% | 2% | Падение для входа/SO |
| `max_steps` | 4 | 4 | Максимум safety orders |
| `take_profit_percentage` | 8% | 8% | TP от средней цены |
| `step_multiplier` | 1.0 | 1.0 | Множитель объёма SO |
| `capital_allocation` | 30% | 100% | Доля капитала |
| `catch_up_enabled` | false | true | Умный старт |
| `max_daily_loss` | $600 | $600 | Дневной лимит |
| `catch_up_max_orders` | 3 | 3 | Макс. catch-up ордеров |

### Signal Generator (v2.0) — Confluence Score

| Компонент | Вес | Условие |
|-----------|-----|---------|
| Тренд | 3 | EMA fast < slow + ADX ≥ 20 |
| Цена | 2 | Расстояние до support ≤ 2% |
| Индикаторы | 2 | RSI < 35 + Volume ≥ 1.2× + Price ≤ BB lower |
| Риск | 1 | Active deals < max + Daily PnL > -$500 |
| Тайминг | 1 | Cooldown ≥ 1 час |
| **Порог** | | **≥ 0.75 (75%)** |

---

## 9. TrendFollower-стратегия

```mermaid
flowchart TD
    START([Тик]) --> OHLCV["Fetch OHLCV H1\n100 свечей\n(HistoryManager или REST)"]
    OHLCV --> ANALYZE["analyze_market(df)\n→ phase, trend_strength, RSI"]

    ANALYZE --> PHASE{Фаза?}

    PHASE --> |bullish_trend| ENTRY_CHK
    PHASE --> |bearish_trend| ENTRY_CHK
    PHASE --> |sideways| ENTRY_CHK

    subgraph ENTRY_CHK [Проверка входа]
        direction TB
        EMA{"EMA20 > EMA50?\n(bull)"}
        EMA --> |да| RSI_CHK{"RSI > 50?"}
        RSI_CHK --> |да| VOL_CHK{"Volume > avg\n× 1.2?"}
        VOL_CHK --> |да| ATR_CHK{"ATR < 5%?\n(не экстрем)"}
        ATR_CHK --> |да| POS_CHK{"Позиция\nне открыта?"}
        POS_CHK --> |да| SIZE["position_size =\n(capital × risk_pct)\n/ (entry - SL)"]
    end

    SIZE --> RISK["RiskManager.check_trade\n+ total_open_exposure"]
    RISK --> |OK| ENTRY["Открыть LONG\nmarket order"]
    ENTRY --> SL_TP["SL: entry - ATR × sl_mult\nTP: entry + ATR × tp_mult"]

    subgraph EXIT_LOGIC [Управление позицией]
        direction TB
        TRAIL{"Trailing stop\nactivation:\nprofit ≥ 1.5×ATR"}
        TRAIL --> |да| TRAIL_STOP["Trail distance\n= 1.0×ATR"]
        BREAKEVEN{"Breakeven:\nprofit ≥ 1.0×ATR"}
        BREAKEVEN --> |да| MOVE_SL["SL → entry\n(breakeven)"]
        PARTIAL{"Partial close:\nprofit ≥ 70% TP"}
        PARTIAL --> |да| CLOSE_50["Закрыть 50%\nпозиции"]
        EMA_CROSS{"EMA20 < EMA50\nили RSI < 40?"}
        EMA_CROSS --> |да| CLOSE_ALL["Закрыть позицию"]
    end

    SL_TP --> EXIT_LOGIC
```

### SL/TP множители (зависят от фазы)

| Фаза | SL множитель ATR | TP множитель ATR | R:R |
|------|------------------|------------------|-----|
| Sideways | 1.0 | 1.0 | 1:1 |
| Weak trend | 2.0 | 1.5 | 1:0.75 |
| Strong trend | 2.5 | 2.5 | 1:1 |

### Risk Manager (TF-специфичный)

| Параметр | Значение | Описание |
|----------|----------|----------|
| `risk_per_trade_pct` | 2% | Риск на сделку от капитала |
| `max_position_size_usd` | 10% капитала | Абсолютный лимит позиции |
| `max_concurrent_positions` | 3 | Макс. одновременных позиций |
| `max_consecutive_losses` | 3 | → снижение размера на 50% |
| `daily_loss_limit` | $500 | Дневной лимит |
| `min_balance_buffer` | 5% | Минимальный остаток |

### Параметры TrendFollower (BTC demo)

| Параметр | Значение | Описание |
|----------|----------|----------|
| `ema_fast_period` | 20 | Период быстрой EMA |
| `ema_slow_period` | 50 | Период медленной EMA |
| `atr_period` | 14 | Период ATR для SL/TP |
| `rsi_period` | 14 | Период RSI |
| `risk_per_trade_pct` | 1% | Риск на сделку от баланса |
| `max_positions` | 2 | Макс. одновременных позиций |
| `trailing_stop_atr_mult` | 2.0× | Множитель ATR для трейлинг-стопа |
| `take_profit_atr_mult` | 3.0× | Множитель ATR для тейк-профита |
| `active_regimes` | bull_trend | Только в бычьем тренде |

---

## 10. SMC-стратегия

### Многотаймфреймный анализ

```mermaid
flowchart TD
    subgraph THROTTLE [Контроль частоты]
        TICK([Тик]) --> QUICK["Быстрый TP/SL чек\nupdate_positions\n— каждый тик"]
        TICK --> INTERVAL{"now - last_analysis\n≥ 300с (5 мин)?"}
        INTERVAL --> |нет| SKIP([Пропустить анализ])
        INTERVAL --> |да| FULL_ANALYSIS
    end

    subgraph FULL_ANALYSIS [Полный анализ каждые 5 мин]
        FETCH["asyncio.gather:\n• D1 (200 свечей)\n• H4 (200 свечей)\n• H1 (200 свечей)\n• M5 (1000 свечей)"]
        FETCH --> ANALYZE["smc_strategy.analyze_market\n(df_d1, df_h4, df_h1, df_m5)"]
        ANALYZE --> SIGNAL["smc_strategy.generate_signal\n(df_m5, balance)"]
    end

    SIGNAL --> STALE_CHK{"|entry - current|\n/ current > 2%?\nstale signal?"}
    STALE_CHK --> |да| REJECT["Отклонить сигнал\n(stale)"]
    STALE_CHK --> |нет| MAX_POS{"positions <\nmax_positions?"}
    MAX_POS --> |нет| REJECT2([Лимит позиций])
    MAX_POS --> |да| SIZE["position_size =\nmin(entry × 0.1,\nmax_position_size)"]
    SIZE --> RISK["RiskManager\ncheck_trade"]
    RISK --> |OK| ENTRY["Открыть позицию\nSL = под/над Order Block\nTP = следующий уровень"]
```

### Поиск входа (M5 precision)

```mermaid
flowchart TD
    STRUCT["H1 структура:\nBOS/CHoCH → тренд"] --> PHASE{SMCPhase?}

    PHASE --> |ACCUMULATION| LONG["Искать LONG"]
    PHASE --> |DISTRIBUTION| SHORT["Искать SHORT"]
    PHASE --> |нет CHoCH| MANAGE([Управлять позициями])

    LONG --> OB{"Order Block\n(demand zone)\nниже цены?"}
    OB --> |нет| MANAGE
    OB --> |да| FVG{"FVG\nподтверждение?"}
    FVG --> |нет| MANAGE
    FVG --> |да| PATTERN{"Price Action\nна M5?\n• Engulfing\n• Pin Bar\n• Inside Bar"}
    PATTERN --> |нет| MANAGE
    PATTERN --> |да| RR{"R:R ≥ 2.0?"}
    RR --> |нет| MANAGE
    RR --> |да| CONFLUENCE["Confluence score:\nOB + FVG + liquidity\n+ trend alignment"]
    CONFLUENCE --> ENTRY["ВХОД"]
```

### Price Action паттерны (M5)

| Паттерн | Условие | SL | TP |
|---------|---------|----|----|
| **Engulfing (bull)** | Body[i] полностью перекрывает body[i-1], close > prev.open | Под low[i-1] + 0.5% | Ближайший OB/FVG/уровень выше |
| **Engulfing (bear)** | Инвертированный | Над high[i-1] + 0.5% | Ближайший OB/FVG/уровень ниже |
| **Pin Bar (hammer)** | Тень > 60% свечи, close у верхней трети | Под low тени | Ближайший resistance |
| **Pin Bar (shooting star)** | Инвертированный | Над high тени | Ближайший support |
| **Inside Bar** | Range ⊂ range[i-1] | Под low[i-1] / над high[i-1] | Ближайший уровень |

### Параметры SMC (BTC demo)

| Параметр | Значение | Описание |
|----------|----------|----------|
| `swing_length` | 10 | Длина свинга для структуры H1 |
| `min_risk_reward` | 2.0 | Минимальный R:R для входа |
| `max_positions` | 2 | Макс. одновременных позиций |
| `risk_per_trade_pct` | 1% | Риск на сделку |
| `require_volume_confirmation` | false | Подтверждение объёмом |
| `active_regimes` | accumulation, distribution | Режимы для входа |
| `_smc_analysis_interval` | 300с (5 мин) | Частота полного анализа |
| Stale threshold | 2% | `|entry - current| / current` |

---

## 11. Hybrid-режим (Grid↔DCA)

### Координатор решений

```mermaid
flowchart TD
    START([Тик]) --> GET_ADX["Извлечь ADX\nиз RegimeAnalysis"]
    GET_ADX --> COORD["TradingCore.hybrid_coordinator\n.evaluate(adx, current_price)"]

    COORD --> ADX_CHK{ADX?}

    ADX_CHK --> |"None\n(нет данных)"| GRID_ONLY[mode: GRID_ONLY\nrun_grid=True, run_dca=False]
    ADX_CHK --> |"ADX ≤ 25"| GRID_ONLY
    ADX_CHK --> |"ADX > 25"| DCA_ACTIVE[mode: DCA_ACTIVE\nrun_grid=False, run_dca=True]
    ADX_CHK --> |"22 ≤ ADX ≤ 28\n+ allow_both"| BOTH[mode: HYBRID\nrun_grid=True, run_dca=True]

    GRID_ONLY --> ROUTE
    DCA_ACTIVE --> ROUTE
    BOTH --> ROUTE

    ROUTE{Маршрутизация}
    ROUTE --> |"run_grid=T, run_dca=T"| EXEC_BOTH["_process_grid_orders\n+ _process_dca_logic"]
    ROUTE --> |"run_grid=T, run_dca=F"| EXEC_GRID["_process_grid_orders"]
    ROUTE --> |"run_grid=F, run_dca=T"| EXEC_DCA["_process_dca_logic"]
    ROUTE --> |"оба False"| HOLD([HOLD — ничего])
```

### Параметры Hybrid

| Параметр | Значение | Описание |
|----------|----------|----------|
| `adx_dca_threshold` | 25.0 | ADX порог переключения Grid→DCA |
| `adx_tolerance` | 3.0 | Зона гистерезиса вокруг порога |
| `allow_both` | false | Одновременная работа |
| `grid_capital_pct` | 60% | Доля капитала для Grid |
| `dca_capital_pct` | 30% | Доля капитала для DCA |
| `initial_mode` | GRID_ONLY | Стартовый режим |

---

## 12. Risk Management (3 уровня)

```mermaid
flowchart TD
    subgraph L1 [Уровень 1: Per-Strategy]
        GRID_SL["Grid: SL = 12%\nот нижней границы"]
        DCA_MAX["DCA: max_daily_loss\n= $600"]
        TF_RISK["TF: risk_per_trade\n= 1-2%, trailing stop"]
        SMC_SL["SMC: SL под Order Block\nR:R ≥ 2.0"]
    end

    subgraph L2 [Уровень 2: Per-Bot RiskManager]
        RM_CHK["Каждый тик:"]
        RM_CHK --> POS{"position_size >\nmax_position_size?"}
        POS --> |да| BLOCK_ENTRY["Заблокировать\nновые входы"]
        POS --> |нет| DAILY{"daily_loss >\nmax_daily_loss?"}
        DAILY --> |да| HALT["HALT: остановить\nвсе стратегии"]
        DAILY --> |нет| DD{"drawdown >\nstop_loss_pct?"}
        DD --> |да| HALT
        DD --> |нет| OK[Торговля разрешена]
    end

    subgraph L3 [Уровень 3: Portfolio optional]
        PRM["PortfolioRiskManager"]
        PRM --> UTIL{"total_allocated >\ncapital × 80%?"}
        UTIL --> |да| REJECT_EXP["REJECTED_EXPOSURE"]
        UTIL --> |нет| PAIR{"pair_exposure >\ncapital × 25%?"}
        PAIR --> |да| REJECT_PAIR["REJECTED_PAIR_LIMIT"]
        PAIR --> |нет| CORR{"Коррелированная\nпара активна?\nBTC/ETH = 0.85"}
        CORR --> |да| REJECT_CORR["REJECTED_CORRELATION"]
        CORR --> |нет| PORT_DD{"portfolio drawdown\n> 15%?"}
        PORT_DD --> |да| PORT_HALT["PORTFOLIO HALTED\nвсе боты"]
        PORT_DD --> |нет| APPROVED["APPROVED ✓"]
    end

    L1 --> L2
    L2 --> L3
```

### Каскад проверки при каждом ордере

```
1. Strategy-level check (Grid SL / DCA max_steps / TF risk_per_trade / SMC R:R)
2. RiskManager.check_trade(order_value, current_position, available_balance)
   ├── check_order_size: value ≥ min_order_size
   ├── check_position_limit: current + additional ≤ max_position_size
   └── check_available_balance: available ≥ required
3. PortfolioRiskManager.check_allocation(bot_name, amount, balance, symbol)
   ├── portfolio halt check
   ├── per-pair cap (25%)
   ├── total exposure cap (80%)
   └── correlation check (BTC/ETH = 0.85)
```

### Параметры риска

| Параметр | Уровень | Значение | Описание |
|----------|---------|----------|----------|
| `max_position_size` | Per-bot | $3,000 | Макс. размер позиции |
| `min_order_size` | Per-bot | $10-150 | Минимальный ордер |
| `max_daily_loss` | Per-bot | $600 | Дневной лимит убытка |
| `max_daily_loss_pct` | Per-bot | 6% | Дневной лимит в % |
| `stop_loss_percentage` | Per-bot | configurable | Портфельный SL |
| `risk_per_trade_pct` | Per-bot | 1-2% | Риск на одну сделку |
| `max_total_exposure_pct` | Portfolio | 80% | Утилизация капитала |
| `max_single_pair_pct` | Portfolio | 25% | Лимит на одну пару |
| `max_correlation_limit` | Portfolio | 0.80 | Блок коррелированных пар |
| `portfolio_stop_loss_pct` | Portfolio | 15% | Drawdown → halt всех ботов |
| `daily_reset` | Per-bot | UTC 00:00 | Сброс daily_loss |

---

## 13. Graceful Transition

Срабатывает когда `StrategySelector` решает **деактивировать** стратегию.

```mermaid
flowchart TD
    DEACT["Стратегии для\nдеактивации:\nset of str"] --> EVENT1["publish\nSTRATEGY_TRANSITION_STARTED"]
    EVENT1 --> CLOSE_CFG{close_positions\n_on_switch\n= true?}

    CLOSE_CFG --> |нет| GRID_ONLY_D
    CLOSE_CFG --> |да| FULL

    subgraph FULL [Полное закрытие]
        direction TB
        GRID_D{"grid ∈\ndeactivated?"} --> |да| CANCEL["exchange.cancel_all_orders\n(symbol)"]
        GRID_D --> |нет| DCA_D

        CANCEL --> DCA_D{"dca ∈\ndeactivated?"}
        DCA_D --> |да| CLOSE_DCA["_close_dca_position\nmarket sell"]
        DCA_D --> |нет| TF_D

        CLOSE_DCA --> TF_D{"trend_follower ∈\ndeactivated?"}
        TF_D --> |да| CLOSE_TF["Для каждой позиции:\nmarket order\nopposite side\nreduceOnly=True"]
        TF_D --> |нет| SMC_D

        CLOSE_TF --> SMC_D{"smc ∈\ndeactivated?"}
        SMC_D --> |да| CLOSE_SMC["Для каждой позиции:\nmarket order\nopposite side\nreduceOnly=True"]
        SMC_D --> |нет| DONE_FULL
        CLOSE_SMC --> DONE_FULL([Закрытие завершено])
    end

    subgraph GRID_ONLY_D [Только отмена Grid]
        CANCEL_ONLY["cancel_all_orders\n(только Grid лимитки)"]
    end

    DONE_FULL --> EVENT2
    CANCEL_ONLY --> EVENT2
    EVENT2["publish\nSTRATEGY_TRANSITION_COMPLETED"]
    EVENT2 --> DONE([Transition завершён])
```

### Параметры перехода

| Параметр | Значение | Описание |
|----------|----------|----------|
| `close_positions_on_switch` | false | Закрывать позиции при деактивации |
| `strategy_switch_cooldown_seconds` | 300 с | Cooldown между переключениями |

---

## 14. Health Monitor и автовосстановление

### Цикл мониторинга (каждые 30 секунд)

```mermaid
flowchart TD
    CHECK([check_all]) --> ITER["Для каждой стратегии\nв StrategyRegistry"]

    ITER --> STATE{state?}
    STATE --> |ERROR| CRITICAL_1["🔴 CRITICAL\n+ error message"]
    STATE --> |"не ACTIVE/PAUSED"| HEALTHY_1["🟢 HEALTHY\n(не мониторится)"]
    STATE --> |"ACTIVE/PAUSED"| CHECKS

    subgraph CHECKS [Проверки]
        ERR_CNT{"error_count\n≥ 10?"} --> |да| UNHEALTHY["🟡 UNHEALTHY"]
        ERR_CNT --> |нет| CONSEC{"consecutive\nerrors ≥ 3?"}
        CONSEC --> |да| CRITICAL_2["🔴 CRITICAL\n(override)"]
        CONSEC --> |нет| SIG_TO{"Нет сигнала\n> 300с?"}
        SIG_TO --> |да| DEGRADED["🟠 DEGRADED"]
        SIG_TO --> |нет| TRADE_TO{"Нет сделок\n> 3600с?"}
        TRADE_TO --> |да| DEGRADED
        TRADE_TO --> |нет| HEALTHY_2["🟢 HEALTHY"]
    end

    CRITICAL_1 --> CALLBACKS
    CRITICAL_2 --> CALLBACKS
    UNHEALTHY --> CALLBACKS
    DEGRADED --> CALLBACKS

    subgraph CALLBACKS [Реакция]
        CB_UNH["on_unhealthy callback"]
        CB_CRIT["on_critical callback"]
        AUTO{"auto_restart\n= true\n+ state = ERROR?"}
        AUTO --> |да| RESTART{"restart_attempts\n< max (3)?"}
        RESTART --> |да| DO_RESTART["reset_strategy\n→ start_strategy"]
        RESTART --> |нет| GIVE_UP["Макс. перезапусков\nдостигнут"]
    end
```

### Overall Status

| Статус | Условие |
|--------|---------|
| 🔴 CRITICAL | Хотя бы одна стратегия CRITICAL |
| 🟡 UNHEALTHY | Хотя бы одна UNHEALTHY (нет CRITICAL) |
| 🟠 DEGRADED | Хотя бы одна DEGRADED |
| 🟢 HEALTHY | Все стратегии OK |

### Пороги

| Порог | Значение | Описание |
|-------|----------|----------|
| `max_error_count` | 10 | Ошибок до UNHEALTHY |
| `max_consecutive_errors` | 3 | Подряд до CRITICAL |
| `signal_timeout_seconds` | 300 | Нет сигнала → DEGRADED |
| `trade_timeout_seconds` | 3600 | Нет сделки → DEGRADED |
| `auto_restart` | true | Авторестарт при ERROR |
| `max_restart_attempts` | 3 | Макс. перезапусков |
| `check_interval` | 30 с | Интервал проверок |

---

## 15. Система событий

### State Machine стратегии (StrategyRegistry)

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> STARTING: register + start

    STARTING --> ACTIVE: init success
    STARTING --> ERROR: init fail

    ACTIVE --> PAUSED: pause()
    PAUSED --> ACTIVE: resume()

    ACTIVE --> STOPPING: stop()
    PAUSED --> STOPPING: stop()

    STOPPING --> STOPPED: cleanup done
    STOPPED --> IDLE: reset()

    ACTIVE --> ERROR: runtime error
    PAUSED --> ERROR: runtime error
    STOPPING --> ERROR: cleanup error

    ERROR --> IDLE: reset()
    ERROR --> STOPPING: force stop
```

### Типы событий (35+)

| Категория | События |
|-----------|---------|
| **Жизненный цикл бота** | BOT_STARTED, BOT_STOPPED, BOT_PAUSED, BOT_RESUMED, BOT_EMERGENCY_STOP |
| **Ордера** | ORDER_PLACED, ORDER_FILLED, ORDER_CANCELLED, ORDER_FAILED |
| **Grid/DCA** | GRID_INITIALIZED, GRID_REBALANCED, DCA_TRIGGERED, TAKE_PROFIT_HIT, GRID_BREAKOUT |
| **Стратегии** | STRATEGY_REGISTERED, STRATEGY_STARTED, STRATEGY_STOPPED, STRATEGY_PAUSED, STRATEGY_RESUMED, STRATEGY_ERROR, STRATEGY_RESTARTED |
| **Режим рынка** | REGIME_DETECTED, REGIME_CHANGED, STRATEGY_SWITCH_RECOMMENDED |
| **Здоровье** | HEALTH_CHECK_COMPLETED, HEALTH_DEGRADED, HEALTH_CRITICAL |
| **Переходы** | STRATEGY_TRANSITION_STARTED, STRATEGY_TRANSITION_COMPLETED |
| **Блокировки** | STRATEGY_LOCKED, STRATEGY_UNLOCKED |
| **Риск** | RISK_LIMIT_HIT, STOP_LOSS_TRIGGERED, POSITION_LIMIT_REACHED |
| **Hybrid** | HYBRID_MODE_ACTIVATED, HYBRID_TRANSITION |
| **Прочие** | PRICE_UPDATED, ERROR_OCCURRED, EXCHANGE_ERROR |

### TradingEvent структура

```python
@dataclass
class TradingEvent:
    event_type: EventType
    bot_name: str
    timestamp: str  # ISO8601
    data: dict[str, Any]  # Decimal → str при сериализации
```

---

## 16. Exchange Client (Bybit V5)

### Архитектура запросов

```mermaid
flowchart LR
    CLIENT[ByBitDirectClient] --> AUTH["HMAC-SHA256 подпись\ntimestamp + api_key\n+ recv_window + params"]
    AUTH --> RETRY["tenacity retry\n3 попытки\nexp backoff 1-10с"]
    RETRY --> |"NetworkError\nRateLimitError"| RETRY
    RETRY --> |"AuthError\nInvalidOrder\nInsufficient"| FAIL[Ошибка]
    RETRY --> |success| PARSE["Парсинг ответа\nnormalize статусов"]
```

### Нормализация статусов ордеров

| Bybit Status | Наш статус |
|-------------|------------|
| `New` | open |
| `PartiallyFilled` | open |
| `Filled` | **closed** |
| `Cancelled` | cancelled |
| `Rejected` | rejected |
| `Deactivated` | cancelled |

### Ключевой фикс: LinearFutures precision (#9d53210)

```
Bybit /v5/market/instruments-info для SOL/USDT:
├── SOLUSDT (LinearPerpetual): qtyStep=0.1 → precision=1 → qty=0.2 ✅
├── SOLUSDT-06MAR26 (LinearFutures): qtyStep=0.01 → precision=2 → qty=0.24 ❌
├── SOLUSDT-13MAR26 ...
└── ...

Фикс: if contractType != "LinearPerpetual": continue
```

### Precision & Rounding

```python
# Из qtyStep (НЕ basePrecision!)
precision['amount'] = abs(Decimal(qtyStep).as_tuple().exponent)
precision['price']  = abs(Decimal(tickSize).as_tuple().exponent)

# Round DOWN для ордеров
quantizer = Decimal(10) ** -precision
rounded = Decimal(str(amount)).quantize(quantizer, rounding="ROUND_DOWN")
```

### Параметры клиента

| Параметр | Значение | Описание |
|----------|----------|----------|
| Base URL (demo) | `api-demo.bybit.com` | Demo Trading API |
| Base URL (live) | `api.bybit.com` | Production API |
| `recv_window` | 10000 мс | Допуск серверного времени |
| Retry | 3 попытки | С exponential backoff 1–10с |
| Category (demo) | `linear` | Demo Trading = только linear |
| `positionIdx` | 0 | One-way mode |
| `timeInForce` | GTC | Good Till Cancel |

---

## 17. Сводная таблица: режим → стратегия → поведение

| Режим рынка | ADX | ATR | Активные стратегии | Ожидаемое действие | Throttle |
|-------------|-----|-----|-------------------|-------------------|----------|
| TIGHT_RANGE | < 18 | < 1% | Grid | Сетка в узком диапазоне | — |
| WIDE_RANGE | < 18 | ≥ 1% | Grid | Сетка с широкими уровнями | — |
| QUIET_TRANSITION | 22–32 | < 2% | Grid (0.7) | Сетка, сниженная аллокация | — |
| VOLATILE_TRANSITION | 22–32 | ≥ 2% | SMC | Ловля разворотов от OB/FVG | 5 мин |
| BULL_TREND (low conf) | > 32 | — | TF (0.7) + DCA (0.3) | Тренд + усреднение | TF: каждый тик, DCA: каждый тик |
| BULL_TREND (high conf) | > 32 | — | DCA + Grid + TF | Гибридный режим (HybridCoordinator) | — |
| BEAR_TREND | > 32 | — | DCA | Усреднение при падении | — |
| ACCUMULATION | любой | — | SMC | CHoCH вверх → long от OB | 5 мин |
| DISTRIBUTION | любой | — | SMC | CHoCH вниз → short от OB | 5 мин |

### Таймлайн одного тика (worst case)

```
0ms     ─── Начало тика
1ms     ─── Проверка pause/daily reset
2ms     ─── Cache balance (может REST call ~100ms)
100ms   ─── _update_active_strategies (каждые 60с):
             ├── detect_market_regime (fetch 100 H1 candles ~200ms)
             ├── StrategySelector.select (< 1ms)
             └── graceful_transition (если нужно, cancel/close ~500ms)
600ms   ─── _process_hybrid_logic / grid / dca
700ms   ─── _process_trend_follower_logic (fetch 100 H1 candles ~200ms, если нет cache)
900ms   ─── _process_smc_logic:
             ├── Quick TP/SL check (< 1ms)
             └── Full analysis (каждые 5 мин): fetch 4 TF ~800ms + analyze ~50ms
1750ms  ─── _update_risk_manager (< 1ms)
1751ms  ─── save_state (каждые 30с, ~50ms)
1800ms  ─── sleep до следующей секунды
```

---

## Appendix A: Ключевые файлы

| Категория | Файл | Строк | Описание |
|-----------|------|-------|----------|
| **Orchestrator** | `bot/orchestrator/bot_orchestrator.py` | ~2365 | Главный координатор |
| | `bot/orchestrator/market_regime.py` | ~809 | 8-режимный классификатор |
| | `bot/orchestrator/strategy_selector.py` | ~483 | Маппинг режим → стратегии |
| | `bot/orchestrator/strategy_registry.py` | ~358 | State machine стратегий |
| | `bot/orchestrator/health_monitor.py` | ~331 | Мониторинг + авторестарт |
| | `bot/orchestrator/events.py` | ~163 | 35+ типов событий |
| **SMC Core** | `bot/core/smc/analyzer.py` | ~200 | Оркестратор SMC-анализа |
| | `bot/core/smc/models.py` | ~250 | SwingPoint, OB, FVG, SMCContext |
| | `bot/core/smc/swing_detector.py` | ~80 | Пивоты (numpy vectorised) |
| | `bot/core/smc/structural_detector.py` | ~120 | BOS/CHoCH state machine |
| | `bot/core/smc/imbalance_detector.py` | ~150 | FVG + Order Block |
| | `bot/core/smc/supply_demand_detector.py` | ~130 | EQH/EQL + Supply/Demand |
| **Strategies** | `bot/strategies/smc/smc_strategy.py` | ~400 | Multi-TF SMC торговля |
| | `bot/strategies/trend_follower/trend_follower_strategy.py` | ~500 | EMA/RSI тренд + trailing |
| | `bot/strategies/dca/dca_signal_generator.py` | ~300 | Confluence-based DCA |
| | `bot/strategies/hybrid/hybrid_strategy.py` | ~250 | Grid↔DCA координация |
| **Core** | `bot/core/grid_engine.py` | ~400 | Сеточный движок |
| | `bot/core/dca_engine.py` | ~300 | DCA движок |
| | `bot/core/risk_manager.py` | ~250 | Per-bot лимиты |
| | `bot/core/portfolio_risk_manager.py` | ~350 | Кросс-пар риск |
| | `bot/core/trading_core/config.py` | ~100 | Shared config live↔backtest |
| | `bot/core/trading_core/hybrid_coordinator.py` | ~120 | Stateless Grid/DCA routing |
| **Exchange** | `bot/api/bybit_direct_client.py` | ~1111 | Bybit V5 REST + HMAC |

---

*Связанные документы: [bot_architecture.md](bot_architecture.md) (v2.0.0) | [SESSION_CONTEXT.md](SESSION_CONTEXT.md) | [persona.md](persona.md)*
