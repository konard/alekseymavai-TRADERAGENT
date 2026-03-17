# План реализации: EpisodeManager + Bidirectional Strategies

## Концепция

Система переходит от «бесконечного цикла с per-position стопами» к
**эпизодической торговле**:

```
ЭПИЗОД = SMC анализ → запуск Grid L+S + DCA L+S + TF + SMC
       → мониторинг combined_pnl
       → частичный выход при пробое уровня + отскок +1%
       → реконфигурация на основе нового SMC анализа
       → следующий эпизод
```

Ключевые свойства:
- Grid и DCA держат LONG + SHORT **одновременно** (хедж)
- При пробое поддержки: закрываем LONG, SHORT продолжает до следующего уровня
- Grid не «лочится» в SHORT-only — GridConfigurator строит новую сетку по SMC
- TF — подтверждающий сигнал для confidence score, не основная стратегия
- Риск считается от текущего баланса при каждой реконфигурации

---

## Фазы реализации

### ФАЗА 1 — SMC Signal Enhancement
**Цель**: SMC возвращает структурные события, не только entry сигналы.

**Файлы**:
- `bot/core/smc/models.py` — расширить `SMCSignal`
- `bot/core/smc/structural_detector.py` — добавить детектор пробоев
- `bot/strategies/smc/smc_strategy.py` — новый метод `detect_level_break()`

**Изменения**:
```python
# bot/core/smc/models.py
@dataclass
class SMCSignal:
    # существующие поля
    direction: SignalDirection
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal

    # новые поля
    event_type: str = "entry"
    # "entry" | "support_break" | "resistance_break"

    broken_level: Decimal | None = None
    # уровень который пробили (поддержка или сопротивление)

    break_extreme: Decimal | None = None
    # break_low (при support_break) или break_high (при resistance_break)
    # +1% от этого уровня = триггер частичного закрытия

    next_level: Decimal | None = None
    # следующий уровень SMC: цель для оставшихся позиций
    # support_break → следующая поддержка (цель SHORT)
    # resistance_break → следующее сопротивление (цель LONG)

    structure_confidence: float = 0.0
    # 0.0–1.0: уверенность в сигнале (OB качество, объём, CHoCH/BOS)
```

```python
# bot/strategies/smc/smc_strategy.py — новый метод
def detect_level_break(
    self,
    df_m5: pd.DataFrame,
    current_price: Decimal,
) -> SMCSignal | None:
    """
    Проверяет: пробита ли ключевая поддержка или сопротивление.
    Возвращает SMCSignal с event_type="support_break"/"resistance_break"
    или None если пробоя нет.

    Логика:
    1. Получить текущие ключевые уровни (OB, FVG, swing highs/lows)
    2. Если цена закрылась ниже поддержки → support_break
       broken_level = поддержка
       break_extreme = текущий минимум
       next_level = следующий OB/FVG ниже
    3. Если цена закрылась выше сопротивления → resistance_break
       (зеркально)
    4. structure_confidence = f(OB_quality, volume_confirmation, CHoCH)
    """
```

**Тесты**: `tests/strategies/smc/test_level_break_detection.py`

---

### ФАЗА 2 — Bidirectional GridEngine (LONG + SHORT одновременно)
**Цель**: Grid держит оба направления одновременно. Убрать exclusive set_direction().

**Файлы**:
- `bot/core/grid_engine.py` — двунаправленный режим
- `bot/strategies/grid_adapter.py` — то же для бэктеста

**Ключевые изменения `GridEngine`**:
```python
class GridMode(str, Enum):
    LONG_ONLY   = "long_only"    # только покупки (текущее поведение)
    SHORT_ONLY  = "short_only"   # только продажи
    BIDIRECTIONAL = "bidirectional"  # оба направления одновременно

class GridEngine:
    def __init__(self, ..., mode: GridMode = GridMode.BIDIRECTIONAL):
        self._long_orders: dict[str, GridOrder] = {}   # LONG уровни
        self._short_orders: dict[str, GridOrder] = {}  # SHORT уровни
        self._mode = mode

    def initialize_grid(self, current_price: Decimal, config: GridBiConfig):
        """
        BIDIRECTIONAL:
          LONG уровни:  current_price - 1×step, -2×step, ..., -N×step
                        каждый LONG уровень: buy limit → при fill: sell above
          SHORT уровни: current_price + 1×step, +2×step, ..., +N×step
                        каждый SHORT уровень: sell limit → при fill: buy below

        long_levels_count  = config.long_levels   (вниз от цены)
        short_levels_count = config.short_levels  (вверх от цены)
        Соотношение задаётся GridConfigurator (50/50, 60/40, etc.)
        """

    def get_combined_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Суммарный unrealized PnL всех LONG + SHORT позиций"""

    def close_direction(self, direction: GridDirection) -> list[str]:
        """
        Закрыть все ордера одного направления.
        Возвращает список order_id для отмены на бирже.
        Используется EpisodeManager при частичном выходе.
        """

    # set_direction() — удалить (заменено на GridConfigurator + reconfigure())
    def reconfigure(self, new_config: GridBiConfig) -> list[str]:
        """
        Полная реконфигурация: закрыть всё, переинициализировать.
        Вызывается EpisodeManager после SMC re-analyze.
        Возвращает order_id для отмены.
        """
```

**Новый dataclass**:
```python
@dataclass
class GridBiConfig:
    """Конфигурация двунаправленного грида от GridConfigurator"""
    lower_bound: Decimal        # нижняя граница (LONG зона)
    upper_bound: Decimal        # верхняя граница (SHORT зона)
    current_price: Decimal      # центр сетки
    step_pct: Decimal           # шаг между уровнями (% от цены)
    long_levels: int            # количество LONG уровней (вниз)
    short_levels: int           # количество SHORT уровней (вверх)
    order_size_quote: Decimal   # размер одного ордера в USDT
    # long_levels / short_levels = bias (50/50, 60/40 SHORT, etc.)
```

**Тесты**: `tests/strategies/test_grid_bidirectional.py`

---

### ФАЗА 3 — Bidirectional DCAEngine (LONG + SHORT стеки)
**Цель**: DCA держит независимые стеки LONG и SHORT уровней.

**Файлы**:
- `bot/core/dca_engine.py`
- `bot/strategies/dca/dca_engine.py`
- `bot/strategies/dca_adapter.py`

**Логика**:
```python
class DCAEngine:
    # Существующее (LONG стек — не меняется):
    _long_levels: list[DCALevel]    # усредняемся вниз при падении

    # Новое (SHORT стек — зеркальная логика):
    _short_levels: list[DCALevel]   # усредняемся вверх при росте

    # SHORT DCA логика:
    # - Вход: цена выросла на dca_step_pct от последнего SHORT входа
    # - TP: цена вернулась к short_avg_entry - tp_pct
    # - Не добавляем SHORT если цена падает (только при росте)

    def get_combined_pnl(self, current_price: Decimal) -> Decimal:
        return self._long_pnl(current_price) + self._short_pnl(current_price)

    def close_long_stack(self) -> list[CloseOrder]:
        """Закрыть все LONG DCA позиции. Используется EpisodeManager."""

    def close_short_stack(self) -> list[CloseOrder]:
        """Закрыть все SHORT DCA позиции."""
```

**Тесты**: `tests/strategies/dca/test_dca_bidirectional.py`

---

### ФАЗА 4 — GridConfigurator
**Цель**: На основе SMC анализа строить оптимальную конфигурацию Grid.

**Файл**: `bot/core/grid_configurator.py` (новый)

```python
@dataclass
class SMCContext:
    """Входные данные от SMC анализа для GridConfigurator"""
    current_price: Decimal
    nearest_support: Decimal      # ближайшая поддержка SMC
    nearest_resistance: Decimal   # ближайшее сопротивление SMC
    trend_bias: str               # "bullish" | "bearish" | "neutral"
    structure_confidence: float   # 0.0–1.0
    atr: Decimal                  # Average True Range (для sizing)
    current_balance: Decimal      # для расчёта размера позиций

class GridConfigurator:
    def build(self, ctx: SMCContext) -> GridBiConfig | None:
        """
        Логика построения конфигурации:

        1. Если structure_confidence < 0.3:
           → return None (не открывать Grid, рынок неясный)

        2. Определить bias (соотношение LONG/SHORT уровней):
           strong_bearish (bias < -0.6): long=2, short=8
           weak_bearish   (bias < -0.2): long=4, short=6
           neutral                     : long=5, short=5
           weak_bullish   (bias > +0.2): long=6, short=4
           strong_bullish (bias > +0.6): long=8, short=2

        3. Границы сетки:
           lower_bound = nearest_support × 0.995  (чуть ниже поддержки)
           upper_bound = nearest_resistance × 1.005

        4. Шаг сетки:
           step_pct = max(atr / current_price × 1.5, min_step_pct)

        5. Размер ордера:
           risk_per_episode = current_balance × 0.02  (2% баланса)
           order_size = risk_per_episode / (long_levels + short_levels)

        6. Если upper_bound - lower_bound < atr × 2:
           → диапазон слишком узкий, Grid не запускать
        """
```

**Тесты**: `tests/core/test_grid_configurator.py`

---

### ФАЗА 5 — EpisodeManager
**Цель**: Оркестратор эпизодической торговли.

**Файл**: `bot/orchestrator/episode_manager.py` (новый)

```python
class EpisodeState(str, Enum):
    IDLE              = "idle"
    RUNNING           = "running"
    PENDING_LONG_EXIT = "pending_long_exit"   # поддержка пробита, ждём +1%
    PENDING_SHORT_EXIT = "pending_short_exit" # сопротивление пробито, ждём -1%
    RECONFIGURING     = "reconfiguring"       # закрыли, делаем SMC re-analyze

@dataclass
class EpisodeContext:
    episode_id: int
    start_time: datetime
    state: EpisodeState
    break_extreme: Decimal | None   # break_low или break_high
    trigger_price: Decimal | None   # break_extreme ± 1% (цена для частичного выхода)
    surviving_positions: str | None # "shorts" | "longs" | None
    next_smc_target: Decimal | None # цель для surviving positions

class EpisodeManager:
    """
    Главный цикл одного торгового эпизода.
    Взаимодействует с: SMCStrategy, GridEngine, DCAEngine, TFStrategy.
    """

    async def run_episode(self):
        # 1. SMC анализ → GridConfigurator → запуск стратегий
        ctx = await self._analyze_and_configure()
        if ctx is None:
            await asyncio.sleep(self._cooldown)
            return  # неясная структура, ждём

        await self._start_strategies(ctx)
        self._state = EpisodeState.RUNNING

        # 2. Мониторинг
        async for price, df in self._price_feed:
            await self._update_positions(price)

            # Проверка SMC сигнала пробоя
            break_signal = await self._smc.detect_level_break(df, price)

            if break_signal and self._state == EpisodeState.RUNNING:
                await self._handle_break(break_signal)

            # Проверка триггера частичного выхода
            elif self._state in (PENDING_LONG_EXIT, PENDING_SHORT_EXIT):
                await self._check_exit_trigger(price)

    async def _handle_break(self, signal: SMCSignal):
        """Пробой уровня → устанавливаем флаг и trigger_price"""
        if signal.event_type == "support_break":
            self._state = EpisodeState.PENDING_LONG_EXIT
            self._break_extreme = signal.break_extreme
            self._trigger_price = signal.break_extreme * Decimal("1.01")  # +1%
            self._next_target = signal.next_level
            self._surviving = "shorts"

        elif signal.event_type == "resistance_break":
            self._state = EpisodeState.PENDING_SHORT_EXIT
            self._break_extreme = signal.break_extreme
            self._trigger_price = signal.break_extreme * Decimal("0.99")  # -1%
            self._next_target = signal.next_level
            self._surviving = "longs"

    async def _check_exit_trigger(self, price: Decimal):
        """Ждём отскока до trigger_price → частичный выход"""
        if self._state == EpisodeState.PENDING_LONG_EXIT:
            if price >= self._trigger_price:
                await self._partial_close("longs")   # закрыть LONG
                # SHORT позиции продолжают, цель = next_target

        elif self._state == EpisodeState.PENDING_SHORT_EXIT:
            if price <= self._trigger_price:
                await self._partial_close("shorts")  # закрыть SHORT
                # LONG позиции продолжают, цель = next_target

    async def _partial_close(self, direction: str):
        """Закрыть только один стек позиций"""
        if direction == "longs":
            await self._grid.close_direction(GridDirection.LONG)
            await self._dca.close_long_stack()
            # TF LONG позиции
            await self._tf.close_positions_by_direction(SignalDirection.LONG)
            # SMC LONG позиции
            await self._smc_adapter.close_positions_by_direction(SignalDirection.LONG)

        elif direction == "shorts":
            # зеркально

        # Переключаемся: оставшиеся позиции ведут к next_target
        self._state = EpisodeState.RUNNING  # продолжаем без одного стека

    async def _on_next_target_reached(self, price: Decimal):
        """Surviving positions достигли цели → закрыть всё → реконфигурация"""
        await self._close_all_remaining()
        self._state = EpisodeState.RECONFIGURING
        await asyncio.sleep(self._reconfigure_cooldown)  # 1-3 бара
        # Следующий эпизод
        await self.run_episode()

    def _get_combined_pnl(self, price: Decimal) -> Decimal:
        """Суммарный unrealized PnL всех открытых позиций"""
        return (
            self._grid.get_combined_unrealized_pnl(price)
            + self._dca.get_combined_pnl(price)
            + self._tf.get_open_pnl(price)
            + self._smc_adapter.get_open_pnl(price)
        )
```

**Тесты**: `tests/orchestrator/test_episode_manager.py`

---

### ФАЗА 6 — TF как сигнал уверенности
**Цель**: TF trend direction влияет на GridConfigurator bias и confidence.

**Файлы**:
- `bot/strategies/trend_follower/market_analyzer.py` — добавить `get_trend_bias()`
- `bot/core/grid_configurator.py` — принимает TF bias как входной параметр

```python
# GridConfigurator.build() расширяется:
@dataclass
class SMCContext:
    ...
    tf_trend_bias: float   # -1.0 (strong bear) → 0.0 (neutral) → +1.0 (strong bull)
    tf_confidence: float   # уверенность TF в своём сигнале (ADX value / 100)

# В build():
combined_bias = smc_bias * 0.6 + tf_trend_bias * 0.4  # SMC главный, TF подтверждает
combined_confidence = smc_confidence * 0.7 + tf_confidence * 0.3
```

**При конфликте SMC vs TF**:
```
SMC: support_break (bearish) + TF: BULLISH_TREND
→ combined_confidence снижается
→ GridConfigurator: order_size × 0.5 (торгуем осторожнее)
→ trigger_price для LONG exit: +0.5% вместо +1% (выходим быстрее)
```

---

### ФАЗА 7 — BotOrchestrator интеграция
**Цель**: Orchestrator делегирует управление эпизодами в EpisodeManager.

**Файл**: `bot/orchestrator/bot_orchestrator.py`

**Изменения**:
```python
class BotOrchestrator:
    def __init__(self, ...):
        ...
        self._episode_manager = EpisodeManager(
            smc=self.smc_strategy,
            grid=self.grid_engine,
            dca=self.dca_engine,
            tf=self.trend_follower_strategy,
            configurator=GridConfigurator(),
            price_feed=self._price_feed(),
        )

    async def _main_loop(self):
        # Вместо прямого управления стратегиями:
        await self._episode_manager.run_episode()
        # EpisodeManager сам управляет циклом и реконфигурациями
```

Убрать из Orchestrator:
- `_switch_grid_direction()` → заменено EpisodeManager._handle_break()
- `_ensure_hedge_mode()` → перенести в EpisodeManager.__init__()
- `_update_active_strategies()` → заменено EpisodeManager._start_strategies()

---

### ФАЗА 8 — Backtest Engine поддержка
**Цель**: `OrchestratorBacktestEngine` поддерживает новую архитектуру.

**Файл**: `bot/tests/backtesting/orchestrator_engine.py`

**Изменения**:
- Добавить `EpisodeBacktestAdapter` — синхронная версия EpisodeManager для бэктеста
- Grid в бэктесте использует `GridBiConfig` (LONG + SHORT уровни одновременно)
- DCA в бэктесте использует двунаправленные стеки
- SMC `detect_level_break()` вызывается каждые N баров

```python
class OrchestratorBacktestConfig:
    ...
    use_episode_manager: bool = True   # включить новую архитектуру
    episode_exit_pct: float = 0.01     # +1% trigger для частичного выхода
    reconfigure_cooldown_bars: int = 3 # баров ожидания перед реконфигурацией
```

---

### ФАЗА 9 — Тесты

| Тест | Файл | Что проверяет |
|------|------|---------------|
| SMC level break | `tests/strategies/smc/test_level_break_detection.py` | support/resistance_break события |
| Grid bidirectional | `tests/strategies/test_grid_bidirectional.py` | LONG+SHORT одновременно, close_direction |
| DCA bidirectional | `tests/strategies/dca/test_dca_bidirectional.py` | SHORT стек, зеркальная логика |
| GridConfigurator | `tests/core/test_grid_configurator.py` | bias → уровни, ATR sizing, confidence gate |
| EpisodeManager | `tests/orchestrator/test_episode_manager.py` | полный цикл: break → partial exit → reconfigure |
| Integration | `tests/orchestrator/test_episode_integration.py` | 2022 bear market сценарий end-to-end |

---

## Порядок реализации (зависимости)

```
Фаза 1 (SMC Signal)
    ↓
Фаза 2 (Grid LONG+SHORT)  ←── параллельно ──→  Фаза 3 (DCA LONG+SHORT)
    ↓                                                    ↓
Фаза 4 (GridConfigurator)  ←── зависит от 1,2,3 ────────┘
    ↓
Фаза 5 (EpisodeManager)  ←── зависит от 1,2,3,4
    ↓
Фаза 6 (TF confidence)   ←── зависит от 5
    ↓
Фаза 7 (BotOrchestrator) ←── зависит от 5,6
    ↓
Фаза 8 (Backtest Engine) ←── зависит от 2,3,5
    ↓
Фаза 9 (Тесты)
```

**Фазы 2 и 3 можно реализовывать параллельно.**
**Фазы 7 и 8 можно реализовывать параллельно.**

---

## Оценка объёма

| Фаза | Новых строк | Изменений | Сложность |
|------|------------|-----------|-----------|
| 1 SMC Signal | ~150 | средние | ★★★☆☆ |
| 2 Grid Bi | ~300 | большие | ★★★★☆ |
| 3 DCA Bi | ~200 | средние | ★★★☆☆ |
| 4 GridConfigurator | ~250 | новый файл | ★★★☆☆ |
| 5 EpisodeManager | ~400 | новый файл | ★★★★★ |
| 6 TF confidence | ~100 | малые | ★★☆☆☆ |
| 7 Orchestrator | ~150 | средние | ★★★☆☆ |
| 8 Backtest | ~300 | средние | ★★★★☆ |
| 9 Тесты | ~600 | новые файлы | ★★★☆☆ |

**Итого**: ~2,450 строк нового/изменённого кода, 9 фаз.

---

## Ветка

`feature/episode-manager` → PR в `main` после прохождения всех тестов.
