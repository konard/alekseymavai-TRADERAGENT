# TRADERAGENT v2.1

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL%202.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 2197+ passing](https://img.shields.io/badge/tests-2197%20passing-brightgreen.svg)]()
[![Version: 2.1.0](https://img.shields.io/badge/version-2.1.0-blue.svg)]()

Платформа алгоритмической торговли криптовалютами с 4 стратегиями, адаптивным переключением по режиму рынка и BacktestOrchestratorEngine V3.0.

Algorithmic cryptocurrency trading platform with 4 strategies, market-regime adaptive switching, and BacktestOrchestratorEngine V3.0.

---

## Содержание / Table of Contents

- [Статус](#статус--status)
- [Стратегии](#стратегии--strategies)
- [Архитектура](#архитектура--architecture)
- [Быстрый старт](#быстрый-старт--quick-start)
- [Конфигурация](#конфигурация--configuration)
- [Бэктестирование](#бэктестирование--backtesting)
- [Тестирование](#тестирование--testing)
- [Документация](#документация--documentation)
- [Инфраструктура](#инфраструктура--infrastructure)

---

## Статус / Status

| Компонент | Состояние |
|-----------|-----------|
| Production боты (5 шт.) | ✅ Running — 185.233.200.13, баланс ~$102k |
| BacktestOrchestratorEngine V3.0 | ✅ Phase 1 завершён (43/43 пар, 50k баров) |
| TradingCore (unified config) | ✅ Завершён — cooldown/fees/risk синхронизированы |
| force_close_all в TF/SMC | ✅ Реализован (v2.1) |
| strat_trades — закрытые сделки | ✅ Исправлен (v2.1) |
| router_cooldown_bars=2 | ✅ Синхронизирован с live (v2.1) |
| Единый strategy_routing.yaml | ✅ Live + Backtest используют один конфиг |
| Live↔Backtest routing | 🔴 additive vs exclusive (см. [docs/plan.md](docs/plan.md) → C1) |
| SMC = 0 сделок в backtest | 🔴 Диагностика (см. [docs/plan.md](docs/plan.md) → A1) |
| Phase 2 оптимизация | ⏳ Не запущена |

**Текущий фокус:** диагностика SMC, синхронизация routing, Phase 2 оптимизация.

---

## Стратегии / Strategies

| Стратегия | Описание | Live | Backtest |
|-----------|----------|------|---------|
| **Grid** | Сетка limit-ордеров в ценовом диапазоне | ✅ | ✅ |
| **DCA** | Усреднение позиции при падении цены, catch-up при старте | ✅ | ✅ |
| **Trend Follower** | EMA/ATR/RSI trend-following с trailing stop и partial close | ✅ | ✅ |
| **SMC** | H1 структура (BOS/CHoCH, OB, FVG) + M5 вход | ✅ | ✅ |
| **Hybrid (Grid+DCA)** | HybridCoordinator: ADX-based Grid↔DCA переключение | ✅ | 🟡 |

Все стратегии реализуют единый интерфейс `BaseStrategy` и подключаются через адаптер к live-боту и бэктесту.

---

## Архитектура / Architecture

```
bot/
├── orchestrator/
│   ├── bot_orchestrator.py          # Live bot main loop (~2600 LOC)
│   ├── market_regime.py             # 6-mode market regime detector (ADX/EMA/SMC)
│   ├── strategy_selector.py         # Regime → strategy routing (RoutingConfig)
│   ├── strategy_conductor.py        # StrategyConductor: directives to strategies
│   └── routing_config.py            # Loads configs/strategy_routing.yaml
├── strategies/
│   ├── base.py                      # Unified BaseStrategy interface
│   ├── grid/ + grid_adapter.py      # Grid strategy + force_close_all
│   ├── dca/ + dca_adapter.py        # DCA strategy + DCAStartupAnalyzer
│   ├── trend_follower/ + adapter    # TrendFollower + force_close_all
│   └── smc/ + smc_adapter.py        # SMC: H1 structure + M5 entry + force_close_all
├── core/
│   ├── smc/                         # Internal SMC module (O(n) swing, BOS/CHoCH)
│   ├── trading_core/                # TradingCore + HybridCoordinator (unified config)
│   └── portfolio_risk_manager.py    # Cross-pair risk + SharedCapitalPool
└── tests/backtesting/
    ├── orchestrator_engine.py        # BacktestOrchestratorEngine V3.0
    ├── unified_engine.py             # TradingCore → OrchestratorBacktestConfig
    ├── strategy_router.py            # Mirrors live StrategySelector (same YAML)
    ├── portfolio_engine.py           # Multi-pair portfolio backtest
    └── optimization.py              # Grid search with ProcessPoolExecutor
```

### Алгоритм маршрутизации (единый конфиг)

`configs/strategy_routing.yaml` используется и в live-боте, и в бэктесте:

```
bull_trend + confluence_high  → {TF: 0.7, DCA: 0.3}
bull_trend (normal)           → {TF: 1.0}
bear_trend                    → {DCA: 1.0}
tight_range / wide_range      → {Grid: 1.0}
volatile_transition           → {SMC: 1.0}
accumulation / distribution   → {SMC: 1.0}
fallback                      → {Grid: 0.25, DCA: 0.25, TF: 0.25, SMC: 0.25}
```

### Детекция режима рынка

```
ADX ≥ 32 + EMA↑  → BULL_TREND     → TrendFollower
ADX ≥ 32 + EMA↓  → BEAR_TREND     → DCA
ADX 18–32        → TRANSITION     → SMC (volatile) / Grid (quiet)
ADX < 18         → RANGE          → Grid
SMC ACCUMULATION → ACCUMULATION   → SMC
SMC DISTRIBUTION → DISTRIBUTION   → SMC
```

Подробные блок-схемы (Mermaid): **[docs/analysis.md](docs/analysis.md#6-архитектурные-блок-схемы)**

Сравнительные таблицы Live vs Backtest: **[docs/architecture_v2.md](docs/architecture_v2.md)**

---

## Быстрый старт / Quick Start

```bash
# 1. Клонирование и установка
git clone https://github.com/alekseymavai/TRADERAGENT.git
cd TRADERAGENT
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Настройка
cp configs/phase7_demo.yaml configs/my_config.yaml
# Отредактировать: credentials_name, параметры стратегий

# 3. Запуск в demo-режиме (Bybit Demo)
python -m bot.main --config configs/my_config.yaml
```

### Docker (Production)

```bash
docker compose up -d
docker compose logs -f bot
```

**Требования**: Python 3.12+, PostgreSQL 15, Redis 7, Docker Compose.

---

## Конфигурация / Configuration

| Файл | Назначение |
|------|-----------|
| `configs/phase7_demo.yaml` | Основной конфиг 5 production-ботов |
| `configs/strategy_routing.yaml` | Правила маршрутизации (live + backtest) |
| `configs/backtest_phase1.yaml` | Конфиг Phase 1 baseline backtest |

**Важно**: `sandbox: true` → использует `api-demo.bybit.com` (demo trading), не testnet.

```yaml
bots:
  - name: demo_btc_hybrid
    symbol: BTC/USDT
    strategy: hybrid

    exchange:
      exchange_id: bybit
      credentials_name: bybit_demo
      sandbox: true              # → api-demo.bybit.com

    grid:
      grid_levels: 6
      amount_per_grid: "150"
      profit_per_grid: "0.012"  # 1.2% profit per level

    dca:
      trigger_percentage: "0.04"       # Enter DCA at -4%
      max_steps: 4
      take_profit_percentage: "0.08"   # TP at +8%

    smc:
      swing_length: 10                 # H1 swing detection
      min_risk_reward: 2.0             # Minimum R:R ratio

    risk_management:
      max_position_size: "3000"        # USD
      max_daily_loss: "600"            # USD (= 6% of $10k)
```

---

## Бэктестирование / Backtesting

### Запуск

```bash
# Smoke-тест одной пары
python scripts/run_backtest_v2.py --mode single --symbol BTC/USDT --max-bars 3000

# Phase 1: Baseline всех пар (43 пары параллельно)
python scripts/run_backtest_v2.py --mode multi --workers 4

# Phase 2: Оптимизация топ-10 пар
python scripts/run_backtest_v2.py --mode multi --phase optimize \
  --config configs/backtest_phase2.yaml --workers 8

# SMC диагностика
python scripts/smoke_smc.py --symbol BTC/USDT --bars 3000 --balance 10000 --verbose
```

### Phase 1 результаты (43/43 пар, ~173 дня, 50k M5 баров)

| Метрика | Значение |
|---------|---------|
| Пар обработано | **43 / 43** |
| Время прогона | ~58 мин, 4 workers |
| Grid лучшие (Sharpe) | SANDUSDT 5.13, LDOUSDT 4.93, BCHUSDT 4.66 |
| DCA лучшие (Sharpe) | LDOUSDT 6.36, SANDUSDT 5.75, ZILUSDT 3.26 |
| TF/SMC | Требуют диагностики (см. [docs/plan.md](docs/plan.md)) |

### Архитектура BacktestOrchestratorEngine V3.0

```
FOR каждый M5 бар:
  IF бар % 12 == 0:
    MarketRegimeDetector.analyze(df_h1)  # Режим каждый час
    StrategyRouter.on_bar()              # active_strategies set
  FOR каждая активная стратегия:
    generate_signal() → CapitalArbiter → RiskManager → MarketSimulator
  check_exits() → force_close_all() при деактивации
  snapshot equity_curve
```

---

## Тестирование / Testing

```bash
# Все тесты (тестовый сервер 158.160.215.57)
.venv/bin/python -m pytest tests/ --ignore=tests/loadtest --ignore=tests/integration -q

# По группам
python -m pytest tests/strategies/ -q                            # 743 теста
python -m pytest tests/orchestrator/ -q                          # 143 теста
python -m pytest tests/backtesting/ -q                           # backtest тесты

# SMC (без flaky market_structure)
python -m pytest tests/strategies/smc/ \
  --ignore=tests/strategies/smc/test_market_structure.py -v
```

**Статус тестов**: 2197 passing, ~26 pre-existing failures (web auth, flaky SMC market_structure).

> **Flaky тесты SMC** (`test_uptrend_detection`, `test_downtrend_detection`, `test_order_block_detection_bullish`) — случайные сбои из-за рандомизированных синтетических данных. Pre-existing, не регрессия.

---

## Документация / Documentation

| Документ | Описание |
|----------|----------|
| **[docs/analysis.md](docs/analysis.md)** | Комплексный анализ: сильные/слабые стороны, архитектурные схемы, Live↔Backtest конфликты, параметры |
| **[docs/plan.md](docs/plan.md)** | Дорожная карта: 8 направлений (A–H), приоритеты P0–P3, таймлайн |
| **[docs/architecture_v2.md](docs/architecture_v2.md)** | Детальная архитектура v2.2: Mermaid-диаграммы, алгоритмы, Issue #371 |
| **[docs/bot_architecture_v2.md](docs/bot_architecture_v2.md)** | Алгоритмы живого бота v2.1: HealthMonitor, StrategyRegistry, потоки данных |
| **[docs/SESSION_CONTEXT.md](docs/SESSION_CONTEXT.md)** | История разработки по сессиям, текущий фокус |

---

## Инфраструктура / Infrastructure

| Сервер | IP | Роль | Статус |
|--------|----|------|--------|
| Production | 185.233.200.13 | 5 live ботов (Docker Compose) | ✅ Running |
| Testing | 158.160.215.57 | pytest, backtest (.venv, без Docker) | ✅ Active |

```bash
# Деплой кода
tar czf /tmp/sync.tar.gz bot/ scripts/ configs/
scp /tmp/sync.tar.gz ai-agent@185.233.200.13:/tmp/
ssh ai-agent@185.233.200.13 "cd ~/TRADERAGENT && tar xzf /tmp/sync.tar.gz && docker compose restart bot"

# Логи
ssh ai-agent@185.233.200.13 "docker logs traderagent-bot --tail 50 -f"

# Тесты на тестовом сервере
ssh ai-agent@158.160.215.57 "cd ~/TRADERAGENT && .venv/bin/python -m pytest tests/ -x -q"
```

**Stack**: Python 3.12, asyncio, PostgreSQL 15 (TimescaleDB), Redis 7, Docker Compose, Bybit V5 API.

---

## Лицензия / License

[Mozilla Public License 2.0](LICENSE)
