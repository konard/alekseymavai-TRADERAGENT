# TRADERAGENT — План развития V2.0 [АРХИВ]

> ⚠️ **УСТАРЕЛО**: Этот документ заменён актуальным [plan.md](plan.md) (v2.1.0, 2026-03-11).
> Оставлен как архив для истории разработки.

> Дата: 2026-03-07 · Версия: v2.0.0
> На основе: [Анализ проекта](analysis.md) | [Архитектура](architecture.md) | [plan.md](plan.md)
> Концепция: «Идеальный криптотрейдер» — SMK + трёхрежимная система + адаптивный риск-менеджмент

---

## Исполнительное резюме

TRADERAGENT V2.0 — платформа алгоритмической торговли с 4 стратегиями, работающая на 5 производственных ботах с балансом ~$102k. Текущая версия содержит критические расхождения между live-ботом и бэктестом, которые делают результаты Phase 1 частично недостоверными для TF/SMC стратегий.

**Реализованные улучшения в этом PR (P0-фиксы):**

| Фикс | Файл | Статус |
|------|------|--------|
| `force_close_all()` для TrendFollowerAdapter | `bot/strategies/trend_follower_adapter.py` | ✅ Реализован |
| `force_close_all()` для SMCStrategyAdapter | `bot/strategies/smc_adapter.py` | ✅ Реализован |
| `strat_trades` считает закрытые сделки вместо сигналов | `bot/tests/backtesting/orchestrator_engine.py` | ✅ Исправлен |
| `router_cooldown_bars=2` (синхронизация с live-ботом) | `bot/tests/backtesting/orchestrator_engine.py`, `configs/backtest_phase1.yaml` | ✅ Исправлен |

---

## Концепция: «Идеальный криптотрейдер» в коде

Концепция Smart Money Concept (SMK) из постановки задачи напрямую отражается в архитектуре бота:

| Принцип SMK | Реализация в TRADERAGENT |
|-------------|--------------------------|
| Психология + дисциплина | Risk Manager: `max_daily_loss_pct=6%`, cooldown между сделками |
| Адаптивный риск 1-2% на сделку | `risk_per_trade_pct=0.01` в TF/SMC, позиционирование через RiskManager |
| SMK как основной инструмент | SMCStrategyAdapter: BOS/CHoCH, Order Blocks, FVG (H1+M5) |
| Индикаторы как подтверждение | TrendFollower: EMA/ATR/RSI как подтверждающие фильтры |
| Три режима торговли | Intradey→SMC+TF, Swing→Grid+DCA, Hold→отдельно |
| Адаптация к фазе рынка | MarketRegimeDetector: 6 режимов + StrategyRouter |

---

## Обзор направлений развития

| # | Направление | Цель | Горизонт | Приоритет |
|---|-------------|------|---------|-----------|
| **A** | P0-фиксы бэктеста | Достоверный P&L для всех стратегий | Неделя 1 | 🔴 КРИТИЧНО |
| **B** | Оптимизация параметров Phase 2 | Оптимальные параметры для топ-10 пар | Неделя 2–3 | 🔴 ВЫСОКИЙ |
| **C** | Live↔Backtest синхронизация | Единая логика routing в live и backtest | Неделя 1–2 | 🔴 ВЫСОКИЙ |
| **D** | Portfolio Backtest Phase 3 | Multi-pair портфельный бэктест | Неделя 4 | 🟡 СРЕДНИЙ |
| **E** | Production Deploy | Обновить live-боты с оптимальными параметрами | Неделя 5 | 🟡 СРЕДНИЙ |
| **F** | AdaptiveRecoveryGrid | Каскад Grid→DCA при пробое нижней границы | Неделя 6+ | 🟠 FUTURE |
| **G** | Web UI улучшения | Equity curves, live dashboard, сравнение бэктестов | Параллельно | 🟠 FUTURE |
| **H** | SMC продвинутые алгоритмы | On-chain метрики, VWAP, Order Flow | Неделя 6+ | 🟠 FUTURE |

---

## A. P0-фиксы бэктеста ✅ РЕАЛИЗОВАНЫ

### A1. force_close_all() для TF и SMC ✅

**Проблема:** При деактивации роутером TrendFollower и SMC не закрывают открытые позиции — они «зависают», не генерируют PnL, блокируют капитал.

**Решение:** Добавить метод `force_close_all()` возвращающий список `(pos_id, ExitReason.MANUAL)` для всех открытых позиций.

```python
# TrendFollowerAdapter — реализовано
def force_close_all(self) -> list[tuple[str, BaseExitReason]]:
    position_ids = list(self._strategy.position_manager.active_positions.keys())
    return [(pos_id, BaseExitReason.MANUAL) for pos_id in position_ids]

# SMCStrategyAdapter — реализовано
def force_close_all(self) -> list[tuple[str, ExitReason]]:
    return [(pos_id, ExitReason.MANUAL) for pos_id in list(self._positions.keys())]
```

**Ожидаемый результат:** TF/SMC `realized_pnl ≠ 0` на 80%+ пар.

---

### A2. strat_trades — закрытые сделки вместо сигналов ✅

**Проблема:** `strat_trades[strat_name] += 1` вызывался при каждом сигнале → BATUSDT SMC=4971 (каждый бар), вместо реальных ~50 сделок.

**Решение:** Считать `strat_trades` только при успешном закрытии позиции:

```python
# orchestrator_engine.py — реализовано
if exits:
    pnl_delta = await self._handle_exits(...)
    per_strategy_pnl[strat_name] += pnl_delta
    strat_trades[strat_name] += len(exits)  # ← закрытые round-trips
```

**Ожидаемый результат:** BATUSDT SMC trades: 4971 → ~50 (реалистично).

---

### A3. router_cooldown_bars=2 ✅

**Проблема:** Дефолт `router_cooldown_bars=120` (10 часов между переключениями) вместо 2 баров (10 минут, как в live-боте).

**Решение:** Исправить расчёт: 600 сек / 300 сек на бар M5 = 2 бара.

**Ожидаемый результат:** Частота переключений стратегий в бэктесте приближается к live-боту.

---

## B. Phase 2 — Оптимизация параметров (требует A1, A2)

### B1. Целевые пары для оптимизации 🔴

**Топ-10 пар по Phase 1 (Grid Sharpe > 1.5 OR DCA Sharpe > 2.5):**

| Пара | Grid Sharpe | DCA Sharpe | Приоритет оптимизации |
|------|-------------|------------|----------------------|
| LDOUSDT | 4.93 | 6.36 | Grid + DCA |
| SANDUSDT | 5.13 | 5.75 | Grid + DCA |
| BCHUSDT | 4.66 | 2.99 | Grid |
| XEMUSDT | 4.61 | 3.02 | Grid + DCA |
| BATUSDT | 3.64 | 3.16 | Grid + DCA |
| ZILUSDT | 2.48 | 3.26 | DCA |
| SOLUSDT | 2.05 | 3.13 | DCA |
| LTCUSDT | 1.83 | 3.06 | DCA |
| HBARUSDT | 0.26 | 3.28 | DCA |
| BTCUSDT | -0.10 | 2.97 | DCA |

### B2. Grid search — Grid стратегия 🔴

**Параметры для перебора:**
- `num_levels` ∈ [4, 6, 8, 10]
- `profit_per_grid` ∈ [0.008, 0.010, 0.012, 0.016, 0.020]
- Итого: 4 × 5 = **20 комбинаций** × 10 пар = 200 прогонов

**Критерий:** Sharpe при `bars_active > 500`, минимум 5 закрытых сделок.

### B3. Grid search — DCA стратегия 🔴

**Параметры для перебора:**
- `trigger_pct` ∈ [0.03, 0.04, 0.05, 0.06]
- `max_steps` ∈ [3, 4, 5]
- `take_profit_pct` ∈ [0.06, 0.08, 0.10, 0.12]
- Итого: 4 × 3 × 4 = **48 комбинаций** × 10 пар = 480 прогонов

### B4. Grid search — TrendFollower 🟡 (требует A1)

**Параметры для перебора:**
- `ema_fast_period` ∈ [10, 20, 30]
- `ema_slow_period` ∈ [40, 50, 60]
- `risk_per_trade_pct` ∈ [0.005, 0.010, 0.015, 0.020]
- Итого: 3 × 3 × 4 = **36 комбинаций** × 10 пар = 360 прогонов

### B5. Grid search — SMC 🟡 (требует A1)

**Параметры для перебора:**
- `swing_length` ∈ [5, 10, 15, 20]
- `min_risk_reward` ∈ [1.5, 2.0, 2.5, 3.0]
- Итого: 4 × 4 = **16 комбинаций** × 10 пар = 160 прогонов

### B6. Начальный баланс $10,000 для Phase 2 🟡

Для избежания ложных DAILY LOSS LIMIT (60% пар в Phase 1).
`$1,000 × 6% = $60` → лимит срабатывает при первом же дневном колебании.
`$10,000 × 6% = $600` → реалистичный лимит.

### B7. Параметры по фазам рынка 🟡

Разные оптимальные параметры для разных рыночных условий:

| Режим | Grid | DCA | TrendFollower | SMC |
|-------|------|-----|---------------|-----|
| **bull_trend** | `profit_per_grid=0.016` | — | `ema_fast=10, ema_slow=40` | `min_rr=2.0` |
| **bear_trend** | `profit_per_grid=0.012` | `trigger=0.04, steps=5` | `ema_fast=20, ema_slow=60` | — |
| **tight_range** | `profit_per_grid=0.008, levels=10` | — | — | — |
| **volatile** | — | — | — | `swing=5, min_rr=1.5` |

---

## C. Live↔Backtest синхронизация

### C1. Exclusive routing в live-боте 🔴

**Текущая проблема:** Live-бот использует аддитивную логику (TF+SMC+Grid одновременно), бэктест — эксклюзивную (один режим = одна стратегия).

**Решение:** Синхронизировать `_update_active_strategies()` в `bot_orchestrator.py` с `StrategyRouter._compute_target_strategies()`:

```python
# Целевое поведение (exclusive routing):
bull_trend   → TrendFollower (+ Grid как базовая защита)
bear_trend   → DCA (+ Grid как базовая защита)
volatile     → SMC
tight_range  → Grid
```

**Риск:** Изменяет поведение live-ботов → тестировать в dry_run режиме.

### C2. SMC параметры в backtest config 🟡

Добавить `smc.min_risk_reward=2.0` в `from_yaml_config()` (сейчас дефолт 2.5 в коде).

### C3. Автоматические parity-тесты 🟠

Файл: `tests/integration/test_live_backtest_parity.py`

Проверяет идентичность параметров `TradingCore` и `OrchestratorBacktestConfig`:
- cooldown_bars == 2
- regime_check_every_n == 12
- max_daily_loss_pct идентичны

---

## D. Phase 3 — Portfolio Backtest

### D1. Выбор топ-5 пар 🔴

После Phase 2: выбрать пары с лучшим Sharpe per strategy.
Цель: Grid × 2 пары + DCA × 2 пары + TF/SMC × 1 пара = 5 пар.

### D2. PortfolioBacktestEngine 🔴

Файл: `bot/tests/backtesting/portfolio_engine.py`
SharedCapitalPool: $50,000 / 5 пар = $10,000 на пару.

### D3. Корреляционный анализ 🟡

Пары с низкой корреляцией ↓ совокупный drawdown.
Уже реализовано в `PortfolioBacktestEngine.run_correlation_analysis()`.

### D4. Portfolio KPI 🟡

- Max portfolio drawdown < 20%
- Portfolio Sharpe > 1.0
- Win rate > 55%

---

## E. Production Deploy

### E1. Обновление конфигов 🔴

После Phase 2: создать `configs/phase8_optimized.yaml` с оптимальными параметрами из Phase 2:
- Grid: оптимальные `num_levels`, `profit_per_grid` для каждой пары
- DCA: оптимальные `trigger_pct`, `max_steps`, `take_profit_pct`
- Risk: `cooldown_seconds=600` (синхронизировано)

### E2. Docker rebuild и deploy 🔴

```bash
docker build -t traderagent-bot:v2.1 . --on 185.233.200.13
docker-compose up -d --force-recreate
```

### E3. Мониторинг после деплоя 🟡

KPI: win_rate > 55%, max_drawdown < 10%, live metrics ≈ backtest ± 30%.

---

## F. AdaptiveRecoveryGrid (Future)

**Концепция:** Grid достигает нижней границы → запускается DCA cascade до уровня поддержки SMC → TP при совокупной позиции +1%.

### F1. SMC.get_nearest_support() 🟠

```python
def get_nearest_support(self, current_price: Decimal) -> Optional[Decimal]:
    """Ближайший Order Block ниже текущей цены (SMC support level)."""
```

### F2. CombinedPositionManager 🟠

Отслеживает суммарную позицию Grid+DCA:
- `total_position_value`: sum всех открытых позиций
- `breakeven_price`: средневзвешенная цена входа
- `dynamic_tp`: TP при `combined_pnl >= +1%`

### F3. Grid→DCA event bus 🟠

```
GridEngine.on_lower_boundary_hit() ─► EventBus ─► DCAEngine.start_cascade(smc_support)
```

### F4. Restart Grid после TP 🟠

После `combined_tp_reached`: закрыть DCA cascade, перезапустить Grid с новым диапазоном.

---

## G. Web UI улучшения (Future)

### G1. Equity curves 🟠

lightweight-charts для backtesting reports.
Уже есть: `web/frontend/node_modules/lightweight-charts/`.
API: `GET /api/backtest/results/{run_id}/equity_curve`

### G2. Live dashboard 🟠

FastAPI backend уже реализован (`web/`):
- Текущие позиции, PnL, режим рынка
- WebSocket для real-time обновлений

### G3. Backtest comparison UI 🟠

Сравнение Phase 1/2/3 результатов:
- Тепловая карта Sharpe по парам и стратегиям
- Equity curves с наложением

---

## H. SMC продвинутые алгоритмы (Future)

### H1. On-chain метрики для Long Hold 🟠

Для Long Hold позиций (weekly/monthly charts):
- Glassnode API: NUPL, SOPR, Exchange Flow
- Подтверждение входа: on-chain аккумуляция

### H2. Order Flow / Volume Profile 🟠

Заменить `require_volume_confirmation=false` реальным Volume Profile:
- Volume Point of Control (vPOC) как уровень поддержки
- VWAP отклонение как фильтр для SMC входа

### H3. Throttling SMC сигналов 🟠

Установить `smc_generate_signal_every_n=12` (hourly) вместо каждого бара.
Уже поддерживается конфигурационно.

---

## Параметры стратегий по парам (предварительно, до Phase 2)

На основе Phase 1 результатов и экспертной оценки SMK:

### Grid стратегия — рекомендуемые параметры

| Пара | num_levels | profit_per_grid | Обоснование |
|------|-----------|-----------------|-------------|
| SANDUSDT | 6 | 1.2% | Sharpe 5.13 с текущими параметрами |
| BCHUSDT | 8 | 1.0% | Высокий Sharpe, умеренная волатильность |
| XEMUSDT | 6 | 1.2% | Sharpe 4.61 с текущими параметрами |
| BATUSDT | 8 | 0.8% | Хороший Sharpe при умеренном профите |
| LDOUSDT | 6 | 1.6% | Высокая волатильность, нужен больший профит |

### DCA стратегия — рекомендуемые параметры

| Пара | trigger_pct | max_steps | take_profit_pct | Обоснование |
|------|-------------|-----------|-----------------|-------------|
| LDOUSDT | 4% | 4 | 8% | Sharpe 6.36 с текущими параметрами |
| ZILUSDT | 5% | 5 | 10% | Высокая волатильность — глубже усреднение |
| HBARUSDT | 3% | 4 | 8% | Sharpe 3.28 — консервативный вход |
| BTCUSDT | 3% | 3 | 6% | Низкая волатильность BTC |
| SOLUSDT | 4% | 4 | 8% | Средняя волатильность |

### TrendFollower — рекомендуемые параметры

| Режим рынка | ema_fast | ema_slow | risk_pct | Обоснование |
|-------------|----------|----------|----------|-------------|
| Сильный тренд | 10 | 40 | 1.5% | Быстрый вход, увеличенный риск |
| Умеренный тренд | 20 | 50 | 1.0% | Стандартный режим |
| Слабый тренд | 30 | 60 | 0.5% | Осторожный вход, снижен риск |

### SMC — рекомендуемые параметры

| Пара | swing_length | min_risk_reward | Обоснование |
|------|-------------|-----------------|-------------|
| BTCUSDT | 10 | 2.5 | Высокая ликвидность, четкая структура |
| ETHUSDT | 10 | 2.0 | Хорошая SMK структура |
| SOLUSDT | 5 | 2.0 | Более частые импульсы |
| Альткоины | 15 | 3.0 | Ложные пробои → нужен высокий RR |

---

## Risk Management параметры

Параметры для работы в разных фазах рынка:

| Параметр | Тренд (bull/bear) | Боковик (tight_range) | Высокая волатильность |
|----------|-------------------|-----------------------|-----------------------|
| `max_position_pct` | 25% | 20% | 15% |
| `risk_per_trade_pct` | 1.0–1.5% | 0.5–1.0% | 0.5% |
| `max_daily_loss_pct` | 6% | 4% | 3% |
| `cooldown_seconds` | 600 | 600 | 1200 |
| `max_positions` | 2 | 4 | 1 |

**Принцип «1:1 с высокой вероятностью»:** В редких случаях при высокой уверенности в сигнале (confluence_score > 0.8 в SMC) допускается увеличение позиции до 3% риска. Реализация: `confidence_based_sizing()` в RiskManager.

---

## Дорожная карта (Timeline)

```
Неделя 1:  ✅ A1-A3 (force_close_all + trades counter + cooldown fix) — ГОТОВО
Неделя 2:  B6 (initial_balance=$10k), B1 (топ-10 пар определены)
           C1 (exclusive routing в live-боте — dry_run тест)
Неделя 3:  B2, B3 (Phase 2 Grid + DCA optimization — 680 прогонов)
Неделя 4:  B4, B5 (Phase 2 TF + SMC optimization — 520 прогонов)
           C2, C3 (parity tests)
Неделя 5:  D1, D2, D3 (Phase 3 portfolio backtest — топ-5 пар)
Неделя 6:  E1, E2, E3 (production deploy с optimized configs)
Неделя 7+: F (AdaptiveRecoveryGrid), G (Web UI), H (SMC advanced)
```

---

## Критерии успеха V2.0

| Milestone | Критерий | Текущий статус |
|-----------|---------|----------------|
| P0-фиксы завершены | TF/SMC pnl ≠ 0 на 80%+ пар | ✅ Реализовано |
| Phase 2 готов | Sharpe > 1.5 для топ-5 пар | 🕐 Неделя 3–4 |
| Phase 3 портфель | Portfolio Sharpe > 1.0, drawdown < 20% | 🕐 Неделя 5 |
| Production deploy | Live метрики ≈ backtest предсказания ±30% | 🕐 Неделя 6 |
| AdaptiveRecoveryGrid | Меньше стоп-лоссов vs чистый Grid на BTC | 🕐 Неделя 7+ |
| SMC On-chain | Long Hold позиции подтверждены on-chain | 🕐 Future |

---

## Связь с принципами «Идеального трейдера»

| Принцип | Текущая реализация | V2 улучшение |
|---------|-------------------|--------------|
| **Эмоциональная стабильность** | Cooldown 600s между сделками | Adaptive cooldown по волатильности |
| **Терпение (только при выравнивании сигналов)** | SMC: min_rr=2.0, confluence_score | SMC: Volume Profile подтверждение |
| **Смирение (принятие убытков)** | force_close_all при деактивации | Фиксированный % риска на сделку |
| **Жёсткий риск ≤1-2%** | risk_per_trade_pct=0.01 | Adaptive sizing по confidence |
| **SMK как основа** | SMCAdapter: BOS/CHoCH, OB, FVG | H1+M5+D1 multi-TF структура |
| **Адаптивность** | 6-режимный MarketRegimeDetector | Phase-specific параметры |
| **Интрадей (M1-H1)** | TF (EMA/ATR/RSI) + SMC (H1+M5) | SMC throttling=hourly |
| **Свинг (DCA+Grid)** | DCA Startup Analyzer, Grid catch-up | AdaptiveRecoveryGrid (F) |
| **Долгосрок** | — | On-chain метрики (H1) |

---

*Связанные документы: [analysis.md](analysis.md) | [plan.md](plan.md) | [architecture.md](architecture.md)*
