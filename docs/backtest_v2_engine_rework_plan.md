# Plan: BacktestOrchestratorEngine Rework

**Статус:** Требует переработки
**Приоритет:** P0 — без этого бэктест не отражает реального поведения бота
**Файл:** `bot/tests/backtesting/orchestrator_engine.py` (611 строк)
**Обнаружено:** Session 44 (2026-03-03)

---

## Корневые причины (из анализа 28 пар)

| # | Проблема | Симптом | Файл / Строка |
|---|----------|---------|---------------|
| 1 | PnL = заглушка `× 0.001` | Все сделки = ~$2.5 | `_handle_exits` L~460 |
| 2 | Стратегии по очереди, не параллельно | 1–4 сделки за 163 дня | `run()` L~249 |
| 3 | SMC кэш устаревший + фильтр объёма | SMC = 0 на всех 28 парах | `smc_adapter.py` |
| 4 | Позиция не закрывается при смене стратегии | DD > 100% | `run()` L~255 |
| 5 | Cooldown блокирует вход после каждого switch | ~50% времени без торговли | `run()` L~236 |

---

## Архитектурная разница: живой бот vs текущий движок

### Живой бот (`BotOrchestrator._main_loop`)
```
Grid ──────────────────────────────────────────────► всегда активен
DCA ───────────────────────────────────────────────► всегда активен
TrendFollower ─────────────────────────────────────► всегда активен
SMC ───────────────────────────────────────────────► всегда активен
                          │
                    RegimeDetector
                          │ (advisory: усиливает один из них)
                          ▼
              Router приоритизирует стратегию,
              остальные продолжают работать
```

### Текущий BacktestOrchestratorEngine
```
Grid ──────[active]──[inactive]──[inactive]──[active]──...
DCA ───────[inactive]──[active]──[inactive]──[inactive]─...
TF ────────[inactive]──[inactive]──[active]──[inactive]─...
SMC ───────[inactive]──[inactive]──[inactive]──[active]─...
                ↑         ↑          ↑          ↑
             switch    switch     switch      switch
             (cooldown 120 bars каждый раз)
```

**Стратегии работают по очереди вместо параллельно.**

---

## Что нужно исправить

### Fix 1 — Реальный расчёт PnL (P0, 1 час)

**Где:** `orchestrator_engine.py`, метод `_handle_exits`

```python
# БЫЛО (заглушка):
pnl_delta += amount * current_price * Decimal("0.001")

# СТАЛО (реальный PnL):
# Нужно знать entry_price для каждой позиции
# position_amounts хранит amount, нужно добавить position_entry_prices

# В _handle_signal при открытии:
position_entry_prices[strat_name][pos_id] = current_price

# В _handle_exits при закрытии:
entry_price = position_entry_prices[strat_name].pop(pos_id, current_price)
if direction == SignalDirection.LONG:
    pnl_delta += (current_price - entry_price) * amount
else:
    pnl_delta += (entry_price - current_price) * amount
```

**Что изменится:** `per_strategy_pnl` будет показывать реальный P&L вместо $2.5.

---

### Fix 2 — Параллельная работа стратегий (P0, 3-4 часа)

**Главное изменение:** Убрать `active_set` из логики выполнения.
Все стратегии всегда обрабатываются, Router используется только как
**вес/приоритет**, но не блокирует остальных.

```python
# Новая логика в run():

# 2. Strategy routing (advisory only)
regime_weights: dict[str, float] = {}
if config.enable_strategy_router:
    router_event = router.on_bar(current_regime, i)
    regime_weights = router_event.strategy_weights  # {'dca': 1.0, 'tf': 0.5, ...}

# 3. Все стратегии работают параллельно
for strat_name, strategy in strategies.items():
    # analyze_market — всегда, независимо от режима
    if _n == 0 or bars_since_warmup % _n == 0:
        strategy.analyze_market(df_d1, df_h4, df_h1, df_m15, df_m5)

    # generate_signal — всегда
    signal = strategy.generate_signal(df_m5, balance)

    if signal is not None:
        # Масштабируем позицию по весу режима
        weight = regime_weights.get(strat_name, 0.5)
        await self._handle_signal(
            ...,
            position_weight=weight,  # 1.0 = полный размер, 0.5 = половина
        )

    # update_positions — всегда
    exits = strategy.update_positions(current_price, df_m5)
    if exits:
        pnl_delta = await self._handle_exits(...)
        per_strategy_pnl[strat_name] += pnl_delta
```

**Что изменится:**
- Все 4 стратегии работают одновременно
- Каждая стратегия управляет своими позициями независимо
- Router усиливает/ослабляет позиции через `weight`, не блокирует
- Cooldown убирается или используется только для `weight=0`

---

### Fix 3 — SMC: актуальные данные (P1, 2 часа)

**Проблема:** SMC кэширует данные в `analyze_market()` раз в 60 баров,
а `generate_signal()` использует устаревший кэш.

**Решение A** (быстрое): Передавать текущий `df_m5` в `generate_signal()`:
```python
# В orchestrator_engine.py, в цикле:
signal = strategy.generate_signal(df_m5, balance)  # уже передаём df_m5

# В smc_adapter.py, generate_signal():
def generate_signal(self, df: pd.DataFrame, current_balance: Decimal):
    # Используем df как актуальные M5 данные (не кэш!)
    df_h1 = self._cached_dfs.get("h1", df)  # H1 из кэша OK (медленнее меняется)
    df_m5 = df  # M5 — всегда свежие!
```

**Решение B** (правильное): Вызывать `analyze_market()` для SMC каждый бар,
но с `check_interval` внутри адаптера:
```python
# В smc_adapter.py:
def analyze_market(self, *dfs):
    # Кэшируем H1/H4/D1 для структурного анализа
    # M5 не кэшируем — берём из generate_signal()
    self._cached_dfs["h1"] = dfs[2]
    self._cached_dfs["h4"] = dfs[1]
    self._cached_dfs["d1"] = dfs[0]
    # НЕ кэшируем M5 — всегда свежие в generate_signal
```

**Дополнительно:** Убрать фильтр объёма для исторических данных:
```python
# В SMCConfig:
require_volume_confirmation: bool = False  # для бэктеста
# ИЛИ в orchestrator_engine.py передавать smc_params={"require_volume_confirmation": False}
```

---

### Fix 4 — Закрытие позиций при смене активной стратегии (P1, 2 часа)

При смене доминирующей стратегии (Router переключается), опциональная логика
закрытия позиций предыдущей:

```python
# В StrategyRouter или в run():
if router_event.strategy_changed:
    prev_strategy = router_event.previous_strategy
    if config.close_on_switch and prev_strategy in strategies:
        prev_strat = strategies[prev_strategy]
        for pos_id in list(position_amounts[prev_strategy].keys()):
            prev_strat.close_position(pos_id, ExitReason.STRATEGY_CHANGE, current_price)
            # ... выполнить sell order
```

**Флаг:** `OrchestratorBacktestConfig.close_positions_on_switch: bool = False`
(по умолчанию False = позиции остаются открытыми, как в живом боте)

---

### Fix 5 — Исправление расчёта просадки (P1, 30 мин)

```python
# БЫЛО: берёт portfolio_value с unrealized PnL
dd = peak_value - portfolio_value

# СТАЛО: считаем realized + unrealized отдельно
realized_balance = simulator.balance.quote
unrealized_pnl = sum(
    (current_price - entry_prices[strat][pid]) * pos_amounts[strat][pid]
    for strat in strategies
    for pid in position_amounts[strat]
)
portfolio_value = realized_balance + unrealized_pnl
```

---

## Новая архитектура движка

```
BacktestOrchestratorEngine v3.0
│
├── run()
│   ├── [warmup] — только analyze_market, нет сделок
│   │
│   └── [main loop, каждый M5 бар]
│       ├── 1. regime_detector.analyze(df_h1)   ← раз в N баров
│       ├── 2. router.get_weights(regime)        ← веса стратегий
│       ├── 3. FOR EACH strategy (параллельно):
│       │   ├── analyze_market(df_d1, h4, h1, m15, m5)
│       │   ├── signal = generate_signal(df_m5, balance)
│       │   ├── IF signal: _handle_signal(signal, weight)
│       │   └── exits = update_positions(price, df_m5)
│       │       └── IF exits: _handle_exits(exits)
│       ├── 4. portfolio_value = simulator.get_value()
│       ├── 5. drawdown = peak - portfolio_value
│       └── 6. risk_manager.check(portfolio_value)
│
├── _handle_signal(signal, weight)
│   ├── position_size = balance × max_pct × weight
│   ├── risk_manager.check_trade(size)
│   ├── strategy.open_position(signal, size)
│   ├── simulator.create_order(side, amount)
│   └── entry_prices[strat][pos_id] = current_price  ← НОВОЕ
│
└── _handle_exits(exits)
    ├── strategy.close_position(pos_id, reason, price)
    ├── simulator.create_order(side="sell", amount)
    └── pnl = (exit_price - entry_price) × amount  ← ИСПРАВЛЕНО
```

---

## Этапы выполнения

| Этап | Задача | Сложность | Приоритет |
|------|--------|-----------|-----------|
| 1 | Fix PnL формулу (`× 0.001` → реальный) | Простая | P0 |
| 2 | Добавить `position_entry_prices` tracking | Простая | P0 |
| 3 | Перевести стратегии в параллельный режим | Средняя | P0 |
| 4 | Убрать `active_set` блокировку, ввести `weight` | Средняя | P0 |
| 5 | Fix SMC: актуальные M5 данные в generate_signal | Средняя | P1 |
| 6 | Убрать volume filter для бэктеста (SMC config) | Простая | P1 |
| 7 | Исправить расчёт drawdown | Простая | P1 |
| 8 | Добавить `close_positions_on_switch` флаг | Средняя | P2 |
| 9 | Исключить дефунктные токены (FTT, LUNA) | Простая | P1 |
| 10 | Дымовой тест после каждого этапа | — | P0 |

**Ожидаемый результат после переработки:**
- 50–200+ сделок на пару за 163 дня (вместо 1–4)
- SMC генерирует сделки (ожидается 10–30/пару)
- per_strategy_pnl = реальный P&L по каждой стратегии
- DD ≤ max_drawdown конфига (≤25%)
- Результаты сопоставимы с ожидаемой эффективностью живого бота

---

## Файлы, которые нужно изменить

| Файл | Изменения |
|------|-----------|
| `bot/tests/backtesting/orchestrator_engine.py` | Основная переработка (Fix 1–5) |
| `bot/strategies/smc_adapter.py` | Fix 3 (актуальные данные) |
| `bot/tests/backtesting/orchestrator_engine.py` | `OrchestratorBacktestConfig` — новые поля |
| `scripts/run_backtest_v2.py` | Передать `smc_params={"require_volume_confirmation": False}` |

---

## Исключить из тестирования (дефунктные токены)

| Токен | Причина |
|-------|---------|
| FTTUSDT | FTX Exchange Token (банкрот с Nov 2022, цена = $0) |
| LUNAUSDT | Terra Luna Classic (коллапс May 2022, цена близка к $0) |
| WAVESUSDT | Низкая ликвидность 2025–2026 |

---

*Создан: 2026-03-03 | Session 44*
