# TRADERAGENT v2.0

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL%202.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 1352+ passing](https://img.shields.io/badge/tests-1352%20passing-brightgreen.svg)]()
[![Version: 2.0.0](https://img.shields.io/badge/version-2.0.0-blue.svg)]()

Платформа алгоритмической торговли криптовалютами с 4 стратегиями, мультитаймфреймовым анализом и BacktestOrchestratorEngine V3.0.

Algorithmic cryptocurrency trading platform with 4 strategies, multi-timeframe analysis and BacktestOrchestratorEngine V3.0.

---

## Содержание / Table of Contents

- [Статус / Status](#статус--status)
- [Стратегии / Strategies](#стратегии--strategies)
- [Архитектура / Architecture](#архитектура--architecture)
- [Быстрый старт / Quick Start](#быстрый-старт--quick-start)
- [Конфигурация / Configuration](#конфигурация--configuration)
- [Бэктестирование / Backtesting](#бэктестирование--backtesting)
- [Документация / Documentation](#документация--documentation)

---

## Статус / Status

| Компонент | Состояние |
|-----------|-----------|
| Production боты (5 шт.) | ✅ Работают — 185.233.200.13 |
| BacktestOrchestratorEngine | ✅ V3.0 (Phase 1: 37 пар, 50k баров) |
| Тестовый сервер | ⏸️ Выключен |
| Telegram уведомления | ❌ Сеть недоступна на продакшн-сервере |

**Phase 1 Backtest Results (2026-03-04):** 5/37 pairs profitable, avg return -11.68% (bearish market Aug 2025 – Feb 2026)

---

## Стратегии / Strategies

| Стратегия | Описание | Статус |
|-----------|----------|--------|
| **Grid** | Сетка limit-ордеров в диапазоне цен | ✅ Production |
| **DCA** | Усреднение позиции при падении цены | ✅ Production |
| **Trend Follower** | EMA/ATR/RSI trend-following с trailing stop | ✅ Production |
| **SMC (Smart Money Concepts)** | H1 структура + M5 вход, Order Blocks | ✅ Production |

Все стратегии реализуют единый интерфейс `BaseStrategy` и работают как в live-боте, так и в бэктесте.

All strategies implement a unified `BaseStrategy` interface and work both in the live bot and backtesting engine.

---

## Архитектура / Architecture

```
bot/
├── orchestrator/
│   ├── bot_orchestrator.py          # Live bot orchestrator
│   ├── market_regime.py             # 6-mode market regime detector
│   └── strategy_selector.py         # Strategy routing
├── strategies/
│   ├── base.py                      # Unified BaseStrategy interface
│   ├── grid/ + grid_adapter.py
│   ├── dca/ + dca_adapter.py
│   ├── trend_follower/ + trend_follower_adapter.py
│   └── smc/ + smc_adapter.py        # Multi-timeframe H1+M5
└── tests/backtesting/
    ├── orchestrator_engine.py        # BacktestOrchestratorEngine V3.0
    ├── multi_tf_data_loader.py       # O(log n) data loader
    └── strategy_router.py            # Advisory regime-based routing
```

Подробнее: [Анализ проекта](docs/analysis.md) · [План развития](docs/plan.md)

For details: [Project Analysis](docs/analysis.md) · [Development Plan](docs/plan.md)

---

## Быстрый старт / Quick Start

```bash
# 1. Clone and install
git clone https://github.com/alekseymavai/TRADERAGENT.git
cd TRADERAGENT
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp configs/example.yaml configs/my_config.yaml
# Edit: api_key, api_secret, strategy params

# 3. Run (demo mode)
python -m bot.main --config configs/my_config.yaml
```

### Docker (Production)

```bash
docker compose up -d
docker compose logs -f bot
```

---

## Конфигурация / Configuration

Пример конфига: [`configs/phase7_demo.yaml`](configs/phase7_demo.yaml)

Example config: [`configs/phase7_demo.yaml`](configs/phase7_demo.yaml)

```yaml
name: demo_btc_hybrid
exchange: bybit
sandbox: true              # api-demo.bybit.com
symbol: BTC/USDT
strategy: hybrid           # grid + dca

grid:
  upper_price: "74000"
  lower_price: "64000"
  grid_levels: 6
  amount_per_grid: "150"
  profit_per_grid: "0.012"

dca:
  trigger_percentage: "0.02"
  amount_per_step: "150"
  max_steps: 5
  take_profit_percentage: "0.10"
```

---

## Бэктестирование / Backtesting

### Phase 1 (завершён / completed)

37 пар × 50k M5 баров × 14 CPU = 35 минут

```bash
cd bot/tests/backtesting
python run_multi_pair.py --config phase1.yaml --pairs all --workers 14
```

### Ключевые находки / Key Findings

- **DCA катастрофичен при даунтренде** — avg -$10k/pair
- **SMC = 0 сделок** — требует расследования (throttle + risk manager)
- **Grid + DCA**: 77-80% win rate, но отрицательный итог

### BacktestOrchestratorEngine V3.0

- Параллельный запуск всех 4 стратегий на каждом баре
- Реальный P&L: `(exit - entry) × amount`
- Advisory routing по режиму рынка (MarketRegimeDetector)
- O(log n) поиск данных в multi-TF loader

---

## Документация / Documentation

| Документ | Описание |
|----------|----------|
| [docs/analysis.md](docs/analysis.md) | Анализ проекта: сильные/слабые стороны, расхождения Live vs Backtest |
| [docs/plan.md](docs/plan.md) | План развития по приоритетам (P0-P3) |
| [docs/SESSION_CONTEXT.md](docs/SESSION_CONTEXT.md) | Контекст последних сессий разработки |
| [docs/architecture_bot.md](docs/architecture_bot.md) | Архитектура живого бота |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Справочник по конфигурации |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Деплой на сервер |
| [docs/case-studies/](docs/case-studies/) | Разбор критических багов |

---

## Тестирование / Testing

```bash
# Все тесты
pytest bot/tests/ -v

# Только unit
pytest bot/tests/unit/ -v

# Только интеграционные
pytest bot/tests/integration/ -v

# Backtest smoke test (один бот, 3000 баров)
pytest bot/tests/backtesting/test_orchestrator_engine.py -v
```

---

## Инфраструктура / Infrastructure

| Сервер | IP | Роль | Статус |
|--------|----|------|--------|
| Production | 185.233.200.13 | 5 live ботов | ✅ Работает |
| Testing | 158.160.215.57 | Бэктесты | ⏸️ Выключен |

SSH: `ssh ai-agent@185.233.200.13`

Docker: `docker compose restart bot`

---

## Лицензия / License

[Mozilla Public License 2.0](LICENSE)
