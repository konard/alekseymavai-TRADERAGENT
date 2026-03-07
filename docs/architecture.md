# TRADERAGENT — Архитектура

> Дата: 2026-03-07 · Версия: v2.0.1 (P0-фиксы: force_close_all TF/SMC, strat_trades, cooldown)

---

## 1. Общая архитектура Live Bot

```
┌─────────────────────────────────────────────────────────────────┐
│                     BotApplication (bot/main.py)                │
│  asyncio event loop · PostgreSQL · Redis · Telegram · Config    │
└─────────────────┬───────────────────────────────────────────────┘
                  │ N ботов
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                  BotOrchestrator (~2400 LOC)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ _main_loop   │  │ TradingCore  │  │ MarketRegimeDetector   │ │
│  │ (60s tick)   │  │ HybridCoord. │  │ ADX/EMA/BB/SMC-phase   │ │
│  └──────┬───────┘  └──────────────┘  └────────────────────────┘ │
│         │                                                         │
│  ┌──────▼────────────────────────────────────────────────────┐   │
│  │         _update_active_strategies()                        │   │
│  │  RegimeAnalysis → active_strategies: set[str]             │   │
│  │  АДДИТИВНАЯ логика (bug: TF+SMC в bull+bear одновременно) │   │
│  └──────┬────────────────────────────────────────────────────┘   │
│         │                                                         │
│  ┌──────▼────────┐  ┌──────────┐  ┌───────────┐  ┌──────────┐   │
│  │ GridEngine    │  │ DCAEngine│  │TrendFollow│  │ SMCAdapt │   │
│  │ (bot/core/    │  │ (core/)  │  │ (adapter) │  │ (adapter)│   │
│  │  grid_engine) │  │          │  │           │  │          │   │
│  └───────────────┘  └──────────┘  └───────────┘  └──────────┘   │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐   │
│  │ RiskManager · ByBitDirectClient · EventBus · Telegram      │   │
│  └────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Жизненный цикл тика (live bot)

```
1. _main_loop() ─► await asyncio.sleep(regime_check_interval=60s)
2. _update_active_strategies() ─► MarketRegimeDetector.analyze(H1 OHLCV)
3. Для каждой активной стратегии:
   a. strategy.process(current_price) ─► Exchange API
   b. Grid: check filled orders, place new levels
   c. DCA: check trigger, place safety orders
   d. TF: check TP/SL, trail stop
   e. SMC: analyze H1 structure, enter M5 signal
4. RiskManager.check_daily_loss()
5. EventBus.emit() ─► Telegram notifications
```

---

## 2. Общая архитектура BacktestOrchestratorEngine V3.0

```
┌───────────────────────────────────────────────────────────────────┐
│           run_backtest_v2.py ─── BacktestOrchestratorEngine       │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  run_multi() — параллельный запуск через ProcessPoolExecutor │  │
│  │  43 символа × 4 воркера = Phase 1 за ~58 мин               │  │
│  └────────────────────────┬────────────────────────────────────┘  │
│                           │ per symbol                             │
│  ┌────────────────────────▼───────────────────────────────────┐   │
│  │  _load_data() — tail-read CSV (50k баров, OOM-защита)      │   │
│  │  MultiTimeframeData: M5 / M15 / H1 / H4 / D1              │   │
│  └────────────────────────┬───────────────────────────────────┘   │
│                           │                                        │
│  ┌────────────────────────▼───────────────────────────────────┐   │
│  │  BacktestOrchestratorEngine.run()                           │   │
│  │                                                             │   │
│  │  for i in range(warmup_bars, total_bars):                   │   │
│  │    ┌──────────────────────────────────────────────────┐     │   │
│  │    │ 1. Regime detect (каждые 12 баров)                │     │   │
│  │    │ 2. StrategyRouter.on_bar(regime) ─► weights       │     │   │
│  │    │    ЭКСКЛЮЗИВНО: bull→TF, bear→DCA, volatile→SMC   │     │   │
│  │    │ 3. force_close_all(deactivated) ─► pnl_delta      │     │   │
│  │    │    ✅ Grid + TF + SMC (P0-фикс: force_close реализован) │   │
│  │    │ 4. Для каждой стратегии с weight>0:               │     │   │
│  │    │    a. analyze_market(df_d1..df_m5)                │     │   │
│  │    │    b. generate_signal(df_m5) ─► signal?           │     │   │
│  │    │    c. _handle_signal() ─► simulator.buy()         │     │   │
│  │    │    d. update_positions() ─► exits?                │     │   │
│  │    │    e. _handle_exits() ─► pnl_delta                │     │   │
│  │    │ 5. Record equity_curve, strat_bars_active         │     │   │
│  │    │ 6. RiskManager.check_daily_loss()                 │     │   │
│  │    └──────────────────────────────────────────────────┘     │   │
│  │                                                             │   │
│  │  _compute_per_strategy_metrics()                            │   │
│  │  Sharpe · win_rate · trades · realized_pnl per strategy    │   │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  strategy_score_matrix.json / .csv                                │
└───────────────────────────────────────────────────────────────────┘
```

---

## 3. Стратегии — Адаптерная архитектура

```
┌───────────────────────────────────────────────────────────────────┐
│                      BaseStrategy (base.py)                       │
│  analyze_market() · generate_signal() · update_positions()       │
│  open_position()  · close_position()  · get_active_positions()   │
└───────────┬───────────────┬───────────────┬────────────┬──────────┘
            │               │               │            │
     ┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼────┐ ┌────▼────────┐
     │GridAdapter  │ │DCAAdapter   │ │TFAdapter │ │SMCAdapter   │
     │             │ │             │ │          │ │             │
     │ ✅force_close│ │             │ │✅ fixed  │ │✅ fixed     │
     └──────┬──────┘ └──────┬──────┘ └─────┬────┘ └────┬────────┘
            │               │               │            │
     ┌──────▼──────┐ ┌──────▼──────┐ ┌─────▼────┐ ┌────▼────────┐
     │GridEngine   │ │DCAEngine    │ │TFStrategy│ │SMCStrategy  │
     │(bot/core/)  │ │(bot/core/)  │ │(tf/      │ │+ lib SMC    │
     │CalculatorRisk│ │StartupAna.  │ │ position │ │ H1+M5 logic │
     └─────────────┘ └─────────────┘ └──────────┘ └─────────────┘
```

---

## 4. Сравнительная таблица: Live Bot vs BacktestOrchestratorEngine

| Аспект | Live Bot (bot_orchestrator.py) | Backtest V3.0 (orchestrator_engine.py) |
|--------|--------------------------------|----------------------------------------|
| **Тик** | asyncio 60s | M5 бар (300s) |
| **Routing** | 🔴 Аддитивный: TF+SMC добавляются к базовым | ✅ Эксклюзивный: один режим = одна стратегия |
| **Cooldown** | 600 сек = 2 бара M5 | ✅ 2 бара M5 (P0-фикс) |
| **Regime check** | каждые 60 сек | каждые 12 баров = 60 мин ✅ |
| **force_close** | cancel_orders() (нет force_close) | ✅ Grid + TF + SMC (P0-фикс) |
| **Data** | WebSocket real-time | CSV (tail-read, 50k баров) |
| **Capital** | $10,000+ реальных | $1,000 симулятор |
| **Fees** | Bybit VIP0 (0.02%/0.055%) | TradingCoreConfig ✅ |
| **Daily loss** | $600 при $10k | 🔴 $60 при $1k (ложные стопы) |
| **win_rate** | N/A | 🔴 sum(pnl)>0 → 100% (приближение) |
| **trades** | N/A | ✅ закрытые round-trips (P0-фикс) |
| **Hybrid Grid↔DCA** | ✅ HybridCoordinator (ADX) | ❌ не реализован |
| **SMC throttle** | 5-min interval | ❌ каждый бар |
| **DCA catch-up** | ✅ DCAStartupAnalyzer | ✅ P0.5 warmup catchup |
| **Config source** | phase7_demo.yaml | backtest_phase1.yaml + from_yaml_config() |

---

## 5. Конфликты в Routing Logic

### Live Bot — аддитивная схема

```
Режим          Active strategies
───────────────────────────────────────────
bull_trend  → Grid + TF + SMC  (3 стратегии)
bear_trend  → Grid + DCA + TF + SMC  (4!)
tight_range → Grid
volatile    → Grid + SMC
accumul.    → Grid + SMC
distribut.  → Grid + SMC
NO_REGIME   → Grid + DCA + TF + SMC (все)
```

### Backtest StrategyRouter — эксклюзивная схема

```
Режим          Active strategies
───────────────────────────────────────────
bull_trend  → TF (если enable_tf=True)
bear_trend  → DCA
volatile    → SMC (если enable_smc=True)
breakout    → SMC (если enable_smc=True)
tight_range → Grid (через GRID recommendation)
NO_REGIME   → {} (пусто, не все стратегии!)
```

### Последствия расхождения

1. Live-бот: в bull_trend одновременно открываются Grid позиции + TF позиции + SMC позиции
2. Backtest: в bull_trend только TF, Grid и SMC не работают совсем
3. **Реальные результаты Grid в bull-рынке backtest = 0 PnL** (стратегия деактивирована)
4. **Реальные результаты TF в live-боте включают конкуренцию за капитал с Grid/SMC**

---

## 6. Потоки данных

```
Phase 1 Backtest Pipeline:

data/historical/XXXUSDT_5m.csv
         │
         ▼ tail-read (50k строк)
_load_data() ─► MultiTimeframeData (M5/M15/H1/H4/D1)
         │
         ▼ ProcessPoolExecutor (4 воркера)
run_single_symbol(symbol, data, config)
         │
         ▼
BacktestOrchestratorEngine.run(data, config)
         │
         ▼
OrchestratorBacktestResult
  ├── per_strategy_metrics {grid, dca, tf, smc}
  │     ├── bars_active
  │     ├── trades  ✅ (закрытые round-trips, P0-фикс)
  │     ├── realized_pnl  ✅ (TF/SMC≠0 после P0-фикса force_close_all)
  │     ├── sharpe
  │     └── win_rate  ⚠️ (100% если pnl>0)
  └── equity_curve
         │
         ▼
strategy_score_matrix.json / .csv
```

---

## 7. Адаптер — BaseStrategy интерфейс

```python
class BaseStrategy(ABC):
    def analyze_market(self, df_d1, df_h4, df_h1, df_m15, df_m5) -> BaseMarketAnalysis:
        """Обновить внутренний рыночный контекст (не генерирует сигнал)"""

    def generate_signal(self, df_m5, balance) -> Optional[BaseSignal]:
        """Возвратить сигнал на открытие позиции или None"""

    def update_positions(self, current_price, df_m5) -> list[tuple[str, ExitReason]]:
        """Проверить TP/SL, вернуть список (pos_id, reason) для закрытия"""

    def open_position(self, signal, amount_usd) -> str:
        """Открыть позицию, вернуть pos_id"""

    def close_position(self, pos_id, reason, exit_price) -> None:
        """Закрыть позицию по pos_id"""

    def force_close_all(self) -> list[tuple[str, ExitReason]]:
        """Немедленно закрыть все позиции (при деактивации роутером)"""
        # ✅ Реализован во всех адаптерах: Grid, TF, SMC (P0-фикс)
```

---

## 8. TradingCore — Единое ядро конфигурации

```
TradingCoreConfig
├── symbol, initial_balance
├── cooldown_seconds = 600   ─► cooldown_bars = 2 (M5)
├── regime_check_interval_seconds = 3600 ─► regime_check_bars = 12
├── max_daily_loss_pct = 0.06  (6%)
├── maker_fee = 0.0002, taker_fee = 0.00055  (Bybit VIP0)
├── enable_grid/dca/trend_follower/smc
└── analyze_every_n_bars = 4

trading_core_to_backtest_config(core) ─► OrchestratorBacktestConfig
  ├── cooldown_bars = core.cooldown_bars(bar_duration=300)  # = 2
  ├── regime_check_every_n = core.regime_check_bars(300)  # = 12
  └── default_analyze_every_n = core.config.analyze_every_n_bars  # = 4
```

---

*Связанные документы: [analysis.md](analysis.md) | [plan.md](plan.md)*
