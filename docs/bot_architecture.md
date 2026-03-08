# Архитектура живого бота — блок-схемы и алгоритмы

> Актуально на: 2026-03-07 · Версия: v2.0.0 · Коммит: `d10aff7`

---

## Содержание

1. [Обзор системы](#1-обзор-системы)
2. [Главный цикл](#2-главный-цикл)
3. [Определение режима рынка](#3-определение-режима-рынка)
4. [Маршрутизация стратегий](#4-маршрутизация-стратегий)
5. [Grid-стратегия](#5-grid-стратегия)
6. [DCA-стратегия](#6-dca-стратегия)
7. [TrendFollower-стратегия](#7-trendfollower-стратегия)
8. [SMC-стратегия](#8-smc-стратегия)
9. [Hybrid-режим (Grid↔DCA)](#9-hybrid-режим-griddca)
10. [Risk Management](#10-risk-management)
11. [Graceful Transition](#11-graceful-transition)

---

## 1. Обзор системы

```mermaid
graph TD
    YAML[configs/phase7_demo.yaml] --> APP[BotApplication]
    APP --> |"создаёт N ботов"| ORCH[BotOrchestrator × N]

    ORCH --> EX[Exchange\nBybit Demo API]
    ORCH --> MRD[MarketRegimeDetector]
    ORCH --> SS[StrategySelector]
    ORCH --> RM[RiskManager]

    ORCH --> GRID[GridEngine]
    ORCH --> DCA[DCAEngine]
    ORCH --> TF[TrendFollower]
    ORCH --> SMC[SMCAdapter]
    ORCH --> HYB[HybridStrategy\nGrid↔DCA coordinator]

    MRD --> |"RegimeAnalysis"| SS
    SS --> |"SelectionResult"| ORCH

    EX --> |"price / OHLCV / orders"| ORCH
    ORCH --> |"create/cancel orders"| EX

    DB[(PostgreSQL)] --> |"restore state"| ORCH
    ORCH --> |"save state"| DB
    TG[Telegram Bot] --> |"команды"| APP
    ORCH --> |"уведомления"| TG
```

> **Комментарий:** ___

---

## 2. Главный цикл

Каждые **1 секунду** (`asyncio.sleep(1)`):

```mermaid
flowchart TD
    START([Tick]) --> PAUSE{Bot paused?}
    PAUSE --> |да| SLEEP([sleep 1s])
    PAUSE --> |нет| DAILY[Сбросить daily loss\nна смене UTC-дня]
    DAILY --> BAL[Получить баланс\ncached_balance]
    BAL --> REGIME{Прошло ≥ 60с\nс последней проверки\nрежима?}
    REGIME --> |да| UPD[_update_active_strategies\n→ StrategySelector]
    REGIME --> |нет| PROC
    UPD --> PROC

    PROC --> GRID_CHK{grid active\n+ dca active\n+ hybrid?}
    GRID_CHK --> |да| HYB[_process_hybrid_logic]
    GRID_CHK --> |нет| IND[Независимые стратегии]
    IND --> |grid active| PGRID[_process_grid_orders]
    IND --> |dca active| PDCA[_process_dca_logic]

    HYB --> TF_CHK
    PGRID --> TF_CHK
    PDCA --> TF_CHK

    TF_CHK{trend_follower\nactive?} --> |да| PTF[_process_trend_follower_logic]
    TF_CHK --> |нет| SMC_CHK
    PTF --> SMC_CHK

    SMC_CHK{smc active?} --> |да| PSMC[_process_smc_logic]
    SMC_CHK --> |нет| RISK
    PSMC --> RISK

    RISK[_update_risk_manager] --> SAVE{30с прошло?}
    SAVE --> |да| SAVEDB[save_state → DB]
    SAVE --> |нет| SLEEP2([sleep 1s])
    SAVEDB --> SLEEP2
```

> **Комментарий:** ___

---

## 3. Определение режима рынка

Запускается каждые **60 секунд** (`regime_check_interval_seconds`).

### 3.1 Двухуровневая классификация (v3.0)

Режим определяется в два приоритетных уровня:

1. **Приоритет 1 — SMC-фаза** (`SMCStructureAnalyzer`, TTL-кэш 5 мин):
   Если `SMCContext.phase == ACCUMULATION` или `DISTRIBUTION` и `warmup_complete=True` → режим устанавливается немедленно, без проверки индикаторов.
   Дополнительно: **freeze** — пока SMC-структура активна, шум ADX/EMA не может сменить режим. Заморозка снимается только при явном CHoCH в противоположном направлении.

2. **Приоритет 2 — Индикаторы** (ADX/EMA/ATR с гистерезисом):
   Используется как fallback для всех остальных режимов.
   ATR уточняет волатильность в любом режиме.

```mermaid
flowchart TD
    OHLCV[OHLCV H1\n100 свечей] --> SMC_SA[SMCStructureAnalyzer\nТТL-кэш 5 мин]
    OHLCV --> IND[Расчёт индикаторов]

    SMC_SA --> SMC_CTX[SMCContext\nphase / trend_bias\nconfidence / structural_levels]

    SMC_CTX --> P1{Приоритет 1\nSMC фаза?\nwarmup_complete?}

    P1 --> |"phase=ACCUMULATION\nwarmup=True"| ACCUM_FR[FREEZE → ACCUMULATION]
    P1 --> |"phase=DISTRIBUTION\nwarmup=True"| DIST_FR[FREEZE → DISTRIBUTION]
    P1 --> |"freeze активна?\n(нет нового CHoCH)"| FREEZE[Удерживать режим\nАкк./Дист.]
    P1 --> |"CHoCH ← противоп.направление"| FREEZE_OFF[Снять freeze\n→ Приоритет 2]
    P1 --> |"нет SMC-сигнала"| IND

    IND --> EMA[EMA 20 / EMA 50]
    IND --> ADX[ADX 14]
    IND --> ATR[ATR 14 %]

    EMA --> REGIME
    ADX --> REGIME
    ATR --> REGIME

    REGIME{Приоритет 2\nКлассификация\nс гистерезисом}

    REGIME --> |"ADX ≥ 32\nEMA20 > EMA50"| BULL[BULL_TREND]
    REGIME --> |"ADX ≥ 32\nEMA20 < EMA50"| BEAR[BEAR_TREND]
    REGIME --> |"ADX < 18\nATR < 1%"| TIGHT[TIGHT_RANGE]
    REGIME --> |"ADX < 18\nATR ≥ 1%"| WIDE[WIDE_RANGE]
    REGIME --> |"ADX 22-32\nATR < 2%"| QUIET[QUIET_TRANSITION]
    REGIME --> |"ADX 22-32\nATR ≥ 2%"| VOLAT[VOLATILE_TRANSITION]

    ACCUM_FR --> REC_SMC[→ SMC]
    DIST_FR --> REC_SMC
    FREEZE --> REC_SMC

    BULL --> CONF{confluence ≥ 0.7?}
    CONF --> |да| REC_HYB[→ HYBRID]
    CONF --> |нет| REC_DCA[→ DCA]
    BEAR --> REC_DCA2[→ DCA]
    TIGHT --> REC_GRID[→ GRID]
    WIDE --> REC_GRID
    QUIET --> REC_GRID2[→ GRID]
    VOLAT --> REC_RED[→ REDUCE_EXPOSURE]
```

### Параметры детектора

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `ema_fast` | 20 | Быстрая EMA | |
| `ema_slow` | 50 | Медленная EMA | |
| `adx_period` | 14 | Период ADX | |
| `atr_period` | 14 | Период ATR | |
| `rsi_period` | 14 | Период RSI | |
| `bb_period` | 20 | Период Bollinger Bands | |
| `adx_enter_trending` | 32.0 | Порог входа в тренд | |
| `adx_exit_trending` | 25.0 | Порог выхода из тренда (гистерезис) | |
| `adx_enter_ranging` | 18.0 | Порог входа в диапазон | |
| `adx_exit_ranging` | 22.0 | Порог выхода из диапазона (гистерезис) | |
| `atr_wide_threshold` | 1.0% | Граница tight/wide range | |
| `atr_volatile_threshold` | 2.0% | Граница quiet/volatile transition | |
| `confluence_threshold` | 0.7 | Порог для HYBRID-режима | |

### SMCStructureAnalyzer параметры

| Параметр | Значение | Описание |
|----------|----------|----------|
| `ttl_seconds` | 300 (5 мин) | TTL кэша SMCContext на символ |
| `swing_strength` | 5 | Баров для подтверждения свинга |
| `min_warmup_bars` | 200 | Минимум свечей для валидного контекста |

> **Комментарий:** Версия v3.0. `SMCStructureAnalyzer` — независимый от SMC-стратегии сервис, работает всегда. `SMCContext.confidence` и `structural_levels` добавлены для downstream-компонентов.

---

## 4. Маршрутизация стратегий

```mermaid
flowchart TD
    ANALYSIS[RegimeAnalysis] --> SS[StrategySelector.select]

    SS --> VOL_CHK{ATR > 3%?\nвысокая волатильность}
    VOL_CHK --> |да, блокировать расширение| KEEP[Оставить текущие стратегии]
    VOL_CHK --> |нет| MAP

    MAP[DEFAULT_REGIME_STRATEGIES] --> RESULT[SelectionResult\nto_start / to_stop / to_keep]

    RESULT --> CD_CHK{Cooldown 600с\nактивен?}
    CD_CHK --> |да| BLOCK[Заблокировать переход]
    CD_CHK --> |нет| CONF_CHK{confidence < 0.3?}
    CONF_CHK --> |да| BLOCK
    CONF_CHK --> |нет| DUR_CHK{Режим < 120с?}
    DUR_CHK --> |да| BLOCK
    DUR_CHK --> |нет| TRANS[execute_transition\n→ graceful_transition]

    TRANS --> ACTIVE[_active_strategies обновлён]
```

### Маппинг режим → стратегии

| Режим | Стратегии | Веса | Комментарий |
|-------|-----------|------|-------------|
| `TIGHT_RANGE` | Grid | 1.0 | |
| `WIDE_RANGE` | Grid | 1.0 | |
| `QUIET_TRANSITION` | Grid | 0.7 | |
| `VOLATILE_TRANSITION` | SMC | 1.0 | |
| `BULL_TREND` | TrendFollower + DCA | 0.7 + 0.3 | |
| `BULL_TREND` + confluence ≥ 0.7 | DCA + Grid + TF | 0.5 + 0.3 + 0.2 | HYBRID |
| `BEAR_TREND` | DCA | 1.0 | |
| `ACCUMULATION` | SMC | 1.0 | |
| `DISTRIBUTION` | SMC | 1.0 | |
| `REDUCE_EXPOSURE` | — | — | все выключены |

### Параметры переключения

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `strategy_switch_cooldown_seconds` | 600 с | Минимум между переключениями | |
| `regime_check_interval_seconds` | 60 с | Частота проверки режима | |
| `_MIN_REGIME_CONFIDENCE` | 0.3 | Мин. уверенность для переключения | |
| `_MIN_REGIME_DURATION_SECONDS` | 120 с | Мин. возраст режима | |
| `_MAX_VOLATILITY_ATR_PCT` | 3.0% | Блок расширения при волатильности | |

> **Комментарий:** ___

---

## 5. Grid-стратегия

```mermaid
flowchart TD
    START([Запуск Grid]) --> RANGE[Определить диапазон\nнижняя / верхняя граница]
    RANGE --> LEVELS[Рассчитать N уровней\nс шагом profit_per_grid %]
    LEVELS --> PLACE[Разместить ордера:\nBUY ниже цены\nSELL выше цены]

    PLACE --> MONITOR([Мониторинг каждый тик])
    MONITOR --> FILL{Ордер исполнен?}

    FILL --> |BUY filled| PLACE_SELL[Разместить SELL\nна уровень выше]
    FILL --> |SELL filled| PLACE_BUY[Разместить BUY\nна уровень ниже]
    FILL --> |нет| SL_CHK

    PLACE_SELL --> SL_CHK
    PLACE_BUY --> SL_CHK

    SL_CHK{Цена < нижняя\nграница - SL%?} --> |да| CLOSE[Закрыть всё\nforce_close_all]
    SL_CHK --> |нет| MONITOR
    CLOSE --> STOP([Grid остановлен])
```

### Параметры Grid (BTC demo)

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `grid_levels` | 6 | Количество уровней сетки | |
| `amount_per_grid` | $150 | Объём на каждый уровень | |
| `profit_per_grid` | 1.2% | Шаг между уровнями | |
| `stop_loss_percentage` | 12% | SL от нижней границы | |
| `min_order_size` | $150 | Минимальный размер ордера | |
| `capital_allocation` | 60% | Доля капитала (Hybrid-режим) | |

> **Комментарий:** ___

---

## 6. DCA-стратегия

```mermaid
flowchart TD
    START([Запуск DCA]) --> WAIT([Ожидание триггера])
    WAIT --> DROP{Цена упала на\ntrigger% от\nточки входа?}
    DROP --> |нет| WAIT
    DROP --> |да, шаг 1| BUY1[Купить step_amount\nШаг 1]
    BUY1 --> TP_CHK1{Цена ≥\nentry + TP%?}
    TP_CHK1 --> |да| CLOSE_ALL[Закрыть всю позицию\nFIX PROFIT]
    TP_CHK1 --> |нет| DROP2

    DROP2{Ещё упала\nна trigger%?} --> |да, шаг 2..N| BUYN[Купить step_amount × multiplier\nШаг N]
    DROP2 --> |нет| TP_CHK1
    BUYN --> MAX_CHK{N = max_steps?}
    MAX_CHK --> |нет| TP_CHK1
    MAX_CHK --> |да| HOLD[Удерживать позицию\nдо TP или закрытия]
    HOLD --> TP_CHK1
    CLOSE_ALL --> START
```

### Параметры DCA (BTC demo)

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `trigger_percentage` | 4% | Падение для первого входа | |
| `max_steps` | 4 | Максимум шагов усреднения | |
| `take_profit_percentage` | 8% | TP от средней цены входа | |
| `step_multiplier` | 1.0 | Множитель объёма на каждый шаг | |
| `capital_allocation` | 30% | Доля капитала (Hybrid-режим) | |
| `catch_up_enabled` | false | Умный старт при запуске бота | |
| `max_daily_loss` | $600 | Дневной лимит убытка | |

> **Комментарий:** ___

---

## 7. TrendFollower-стратегия

```mermaid
flowchart TD
    START([Тик]) --> OHLCV[Получить OHLCV H1\n100 свечей]
    OHLCV --> PHASE[Определить фазу\nbearish/bullish/sideways]
    PHASE --> BULL_CHK{Фаза = bullish_trend?}

    BULL_CHK --> |нет| EXIT_CHK
    BULL_CHK --> |да| EMA_CHK{EMA20 > EMA50?}
    EMA_CHK --> |нет| EXIT_CHK
    EMA_CHK --> |да| RSI_CHK{RSI > 50?}
    RSI_CHK --> |нет| EXIT_CHK
    RSI_CHK --> |да| VOL_CHK{Volume > avg\n× 1.2?}
    VOL_CHK --> |нет| EXIT_CHK
    VOL_CHK --> |да| POS_CHK{Позиция уже\nоткрыта?}
    POS_CHK --> |да| EXIT_CHK
    POS_CHK --> |нет| SIZE[Рассчитать размер\nрisk_per_trade × balance / ATR]
    SIZE --> ENTRY[Открыть LONG\nmarket order]
    ENTRY --> SL[Выставить SL:\nentry - 2×ATR]
    SL --> TP[Выставить TP:\nentry + 3×ATR\nR:R = 1.5]

    EXIT_CHK{Открыта позиция?} --> |нет| DONE([Конец тика])
    EXIT_CHK --> |да| TRAIL{Trailing stop\nсрабатывает?}
    TRAIL --> |да| CLOSE[Закрыть позицию]
    TRAIL --> |нет| EMA_CROSS{EMA20 < EMA50\nили RSI < 40?}
    EMA_CROSS --> |да| CLOSE
    EMA_CROSS --> |нет| DONE
    CLOSE --> DONE
```

### Параметры TrendFollower (BTC demo)

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `ema_fast_period` | 20 | Период быстрой EMA | |
| `ema_slow_period` | 50 | Период медленной EMA | |
| `atr_period` | 14 | Период ATR для SL/TP | |
| `rsi_period` | 14 | Период RSI | |
| `risk_per_trade_pct` | 1% | Риск на сделку от баланса | |
| `max_positions` | 2 | Макс. одновременных позиций | |
| `trailing_stop_atr_mult` | 2.0× | Множитель ATR для трейлинг-стопа | |
| `take_profit_atr_mult` | 3.0× | Множитель ATR для тейк-профита | |
| `active_regimes` | bull_trend | Только в бычьем тренде | |

> **Комментарий:** ___

---

## 8. SMC-стратегия

```mermaid
flowchart TD
    START([Тик, каждые 5 мин]) --> H1[Получить OHLCV H1\n200 свечей]
    H1 --> STRUCT[SMC анализ H1:\nBOS / CHoCH\nOrder Blocks\nFVG]

    STRUCT --> PHASE{SMC фаза?}
    PHASE --> |ACCUMULATION| LONG_SETUP[Искать long-вход]
    PHASE --> |DISTRIBUTION| SHORT_SETUP[Искать short-вход]
    PHASE --> |нет CHoCH| MANAGE[Управлять\nоткрытыми позициями]

    LONG_SETUP --> OB{Order Block\nниже цены?}
    OB --> |нет| MANAGE
    OB --> |да| FVG{FVG\nподтверждение?}
    FVG --> |нет| MANAGE
    FVG --> |да| RR_CHK{R:R ≥ 2.0?}
    RR_CHK --> |нет| MANAGE
    RR_CHK --> |да| MAX_CHK{positions < max_positions?}
    MAX_CHK --> |нет| MANAGE
    MAX_CHK --> |да| ENTRY[Открыть позицию\nSL = под Order Block\nTP = следующий уровень H1]

    ENTRY --> MANAGE
    MANAGE --> EXITS[update_positions:\nпроверить SL / TP]
    EXITS --> DONE([Конец тика])
```

### Параметры SMC (BTC demo, H1 режим)

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `swing_length` | 10 | Длина свинга для структуры H1 | |
| `min_risk_reward` | 2.0 | Минимальный R:R для входа | |
| `max_positions` | 2 | Макс. одновременных позиций | |
| `risk_per_trade_pct` | 1% | Риск на сделку | |
| `require_volume_confirmation` | false | Подтверждение объёмом | |
| `active_regimes` | accumulation, distribution | Режимы для входа | |

### Параметры SMC (M5 режим, если включён)

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `swing_length_h1` | 10 | Свинг для H1-структуры | |
| `swing_length_m5` | 20 | Свинг для M5-входа | |
| `h1_limit` | 200 | Свечей H1 для анализа структуры | |
| `m5_limit` | 1000 | Свечей M5 (~3.5 дня) | |
| `min_risk_reward` | 2.0 | Мин. R:R для M5-входа | |
| `max_positions` | 2 | Макс. позиций в M5-режиме | |

> **Комментарий:** ___

---

## 9. Hybrid-режим (Grid↔DCA)

Активируется когда **Grid и DCA работают одновременно**. Координатором является `HybridStrategy`.

```mermaid
flowchart TD
    START([Тик]) --> ADX[Получить ADX\nиз текущего RegimeAnalysis]
    ADX --> MODE{Текущий режим?}

    MODE --> |GRID_ONLY| GRID_EVAL{ADX > 25\nили тренд?}
    GRID_EVAL --> |нет| RUN_GRID[Работает только Grid]
    GRID_EVAL --> |да| TRANSIT1[Переключить в DCA_ACTIVE]

    MODE --> |DCA_ACTIVE| DCA_EVAL{ADX < 20\nи DCA закрыт?}
    DCA_EVAL --> |нет| RUN_BOTH[DCA активна\nGrid приостановлен]
    DCA_EVAL --> |да| TRANSIT2[Переключить в GRID_ONLY]

    TRANSIT1 --> RUN_BOTH
    TRANSIT2 --> RUN_GRID
    RUN_GRID --> DONE([Конец тика])
    RUN_BOTH --> DONE
```

### Параметры Hybrid

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `grid_capital_pct` | 60% | Доля капитала для Grid | |
| `dca_capital_pct` | 30% | Доля капитала для DCA | |
| `initial_mode` | GRID_ONLY | Стартовый режим | |

> **Комментарий:** ___

---

## 10. Risk Management

```mermaid
flowchart TD
    TICK([Каждый тик]) --> BAL[Получить текущий баланс]
    BAL --> DAILY_CHK{daily_loss >\nmax_daily_loss?}

    DAILY_CHK --> |да| HALT[HALT: остановить все стратегии\nотменить все ордера]
    DAILY_CHK --> |нет| POS_CHK{Размер позиции >\nmax_position_size?}

    POS_CHK --> |да| BLOCK_ENTRY[Заблокировать новые входы]
    POS_CHK --> |нет| OK[Торговля разрешена]

    HALT --> ALERT[Уведомление в Telegram]
    DAILY_RESET([UTC 00:00]) --> RESET[Сбросить daily_loss]
```

### Параметры риска (BTC demo)

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `max_daily_loss` | $600 | Макс. дневной убыток | |
| `max_position_size` | $3,000 | Макс. размер позиции | |
| `max_daily_loss_pct` | 6% | Дневной лимит в % от баланса | |
| `risk_per_trade_pct` | 1% | Риск на одну сделку | |
| `daily_reset` | UTC 00:00 | Сброс счётчика убытка | |

> **Комментарий:** ___

---

## 11. Graceful Transition

Срабатывает когда StrategySelector решает **деактивировать** стратегию.

```mermaid
flowchart TD
    DEACT[Стратегия деактивируется] --> CLOSE_CFG{close_positions_on_switch\n= true?}

    CLOSE_CFG --> |нет| CANCEL_ONLY[Только отменить ордера Grid]
    CLOSE_CFG --> |да| FULL

    FULL --> GRID_D{Grid\nдеактивируется?}
    GRID_D --> |да| CANCEL_ORDERS[cancel_all_orders]
    GRID_D --> |нет| DCA_D

    CANCEL_ORDERS --> DCA_D{DCA\nдеактивируется?}
    DCA_D --> |да| CLOSE_DCA[_close_dca_position]
    DCA_D --> |нет| TF_D

    CLOSE_DCA --> TF_D{TF\nдеактивируется?}
    TF_D --> |да| CLOSE_TF[Закрыть все TF позиции\nmarket order reduceOnly]
    TF_D --> |нет| SMC_D

    CLOSE_TF --> SMC_D{SMC\nдеактивируется?}
    SMC_D --> |да| CLOSE_SMC[Закрыть позиции\nиз adapter._positions]
    SMC_D --> |нет| DONE

    CLOSE_SMC --> DONE
    CANCEL_ONLY --> DONE
    DONE([Transition завершён])
```

### Параметры перехода

| Параметр | Значение | Описание | Комментарий |
|----------|----------|----------|-------------|
| `close_positions_on_switch` | false | Закрывать позиции при деактивации | |
| `strategy_switch_cooldown_seconds` | 600 с | Cooldown между переключениями | |

> **Комментарий:** ___

---

## Сводная таблица: режим → стратегия → ожидаемое поведение

| Режим рынка | ADX | ATR | Активные стратегии | Ожидаемое действие | Комментарий |
|-------------|-----|-----|-------------------|-------------------|-------------|
| TIGHT_RANGE | < 18 | < 1% | Grid | Сетка в узком диапазоне | |
| WIDE_RANGE | < 18 | ≥ 1% | Grid | Сетка с широкими уровнями | |
| QUIET_TRANSITION | 22–32 | < 2% | Grid | Сетка в переходной зоне | |
| VOLATILE_TRANSITION | 22–32 | ≥ 2% | SMC | Ловля разворотов | |
| BULL_TREND (low conf.) | > 32 | — | TF + DCA | Тренд + усреднение | |
| BULL_TREND (high conf.) | > 32 | — | TF + DCA + Grid | Гибридный режим | |
| BEAR_TREND | > 32 | — | DCA | Усреднение при падении | |
| ACCUMULATION | любой | — | SMC | CHoCH вверх → long | |
| DISTRIBUTION | любой | — | SMC | CHoCH вниз → short | |

> **Общий комментарий:** ___

---

*Связанные документы: [analysis.md](analysis.md) | [plan.md](plan.md) | [SESSION_CONTEXT.md](SESSION_CONTEXT.md)*
