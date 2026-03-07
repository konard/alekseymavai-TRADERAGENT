# TRADERAGENT v2.0

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL%202.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 2197+ passing](https://img.shields.io/badge/tests-2197%20passing-brightgreen.svg)]()
[![Version: 2.0.0](https://img.shields.io/badge/version-2.0.0-blue.svg)]()

Платформа алгоритмической торговли криптовалютами с 4 стратегиями, адаптивным переключением по режиму рынка и BacktestOrchestratorEngine V3.0.

Algorithmic cryptocurrency trading platform with 4 strategies, market-regime adaptive switching, and BacktestOrchestratorEngine V3.0.

---

## Содержание / Table of Contents

- [Статус / Status](#статус--status)
- [Стратегии / Strategies](#стратегии--strategies)
- [Архитектура / Architecture](#архитектура--architecture)
- [Быстрый старт / Quick Start](#быстрый-старт--quick-start)
- [Конфигурация / Configuration](#конфигурация--configuration)
- [Бэктестирование / Backtesting](#бэктестирование--backtesting)
- [Тестирование / Testing](#тестирование--testing)
- [Документация / Documentation](#документация--documentation)
- [Инфраструктура / Infrastructure](#инфраструктура--infrastructure)

---

## Статус / Status

| Компонент | Состояние |
|-----------|-----------|
| Production боты (5 шт.) | ✅ Running — 185.233.200.13, баланс ~$102k |
| BacktestOrchestratorEngine V3.0 | ✅ Phase 1 завершён (43/43 пар, 50k баров, 2026-03-07) |
| TradingCore (unified config) | ✅ Завершён — cooldown/fees/risk синхронизированы |
| Live↔Backtest routing sync | 🔴 P0 — additive vs exclusive расхождение (см. plan.md) |
| force_close_all в TF/SMC | 🔴 P0 — TF и SMC не закрывают позиции при деактивации |
| Telegram уведомления | ✅ Работают |

**Текущий фокус:** устранение P0-багов Live↔Backtest, Phase 2 оптимизация.

---

## Стратегии / Strategies

| Стратегия | Описание | Live | Backtest |
|-----------|----------|------|---------|
| **Grid** | Сетка limit-ордеров в ценовом диапазоне | ✅ | ✅ |
| **DCA** | Усреднение позиции при падении цены | ✅ | ✅ |
| **Trend Follower** | EMA/ATR/RSI trend-following с trailing stop | ✅ | ⚠️ force_close_all отсутствует |
| **SMC** | H1 структура + M5 вход, Order Blocks, FVG | ✅ | ⚠️ force_close_all отсутствует |
| **Hybrid (Grid+DCA)** | Координированное переключение Grid↔DCA | ✅ | 🟡 Частично |

Все стратегии реализуют единый интерфейс `BaseStrategy` и подключаются через адаптер к live-боту и бэктесту.

All strategies implement the unified `BaseStrategy` interface and connect via adapters to both the live bot and backtesting engine.

---

## Архитектура / Architecture

```
bot/
├── orchestrator/
│   ├── bot_orchestrator.py          # Live bot main loop (~2600 LOC)
│   ├── market_regime.py             # 6-mode market regime detector
│   └── strategy_selector.py         # Strategy routing (HybridCoordinator)
├── strategies/
│   ├── base.py                      # Unified BaseStrategy interface
│   ├── grid/ + grid_adapter.py      # Grid strategy + force_close_all
│   ├── dca/ + dca_adapter.py        # DCA strategy
│   ├── trend_follower/ + adapter    # TrendFollower (EMA/ATR/RSI)
│   └── smc/ + smc_adapter.py        # SMC: H1 structure + M5 entry
├── core/
│   ├── smc/                         # Internal SMC module (O(n) swing, BOS/CHoCH)
│   ├── trading_core/                # TradingCore + HybridCoordinator
│   └── portfolio_risk_manager.py
└── tests/backtesting/
    ├── orchestrator_engine.py        # BacktestOrchestratorEngine V3.0
    ├── multi_tf_data_loader.py       # O(log n) multi-TF data loader
    └── strategy_router.py            # Advisory regime-based routing
```

Подробный анализ архитектуры, сильных/слабых сторон и расхождений Live↔Backtest:
→ **[docs/analysis.md](docs/analysis.md)** | **[docs/architecture.md](docs/architecture.md)**

Detailed architecture analysis, strengths/weaknesses, and Live↔Backtest conflicts:
→ **[docs/analysis.md](docs/analysis.md)** | **[docs/architecture.md](docs/architecture.md)**

---

## Быстрый старт / Quick Start

```bash
# 1. Клонирование и установка / Clone and install
git clone https://github.com/alekseymavai/TRADERAGENT.git
cd TRADERAGENT
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Настройка / Configure
cp configs/phase7_demo.yaml configs/my_config.yaml
# Отредактировать: api_key, api_secret, параметры стратегий

# 3. Запуск в demo-режиме / Run in demo mode
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

Основной конфиг: [`configs/phase7_demo.yaml`](configs/phase7_demo.yaml)

Main config example: [`configs/phase7_demo.yaml`](configs/phase7_demo.yaml)

```yaml
bots:
  - name: demo_btc_hybrid
    symbol: BTC/USDT
    strategy: hybrid

    exchange:
      exchange_id: bybit
      credentials_name: bybit_demo
      sandbox: true              # api-demo.bybit.com

    grid:
      grid_levels: 6
      amount_per_grid: "150"
      profit_per_grid: "0.012"  # 1.2% profit per level

    dca:
      trigger_percentage: "0.04"       # Enter DCA at -4%
      max_steps: 4
      take_profit_percentage: "0.08"   # TP at +8%

    risk_management:
      max_position_size: "3000"        # USD
      max_daily_loss: "600"            # USD
```

**Важно**: `sandbox: true` → использует `api-demo.bybit.com` (demo trading), не testnet.

**Note**: `sandbox: true` → uses `api-demo.bybit.com` (demo trading), not testnet.

---

## Бэктестирование / Backtesting

### Запуск / Run

```bash
# Smoke-тест одной пары / Single pair smoke test
python scripts/run_backtest_v2.py --mode single --symbol BTC/USDT --max-bars 3000

# Phase 1: Baseline всех пар / All pairs baseline (auto-discover data/historical/)
python scripts/run_backtest_v2.py --mode multi --workers 4

# SMC диагностика / SMC diagnostics
python scripts/smoke_smc.py --bars 2000 --trend up --warmup 200
```

### Phase 1 результаты (2026-03-07, данные ~173 дня, 50k M5 баров)

| Метрика | Значение |
|---------|---------|
| Пар обработано | **43 / 43** |
| Время прогона | ~58 мин, 4 workers |
| Grid лучшие | SANDUSDT +5.13%, LDOUSDT +4.93%, BCHUSDT +4.66% |
| DCA лучшие | LDOUSDT +6.36%, SANDUSDT +5.75% |
| TF/SMC PnL | 0 — см. `force_close_all` баг в [docs/plan.md](docs/plan.md) |

> **P0 баги**: TF/SMC не реализуют `force_close_all()` — позиции не закрываются при деактивации роутером → ложные DAILY_LOSS_LIMIT. Подробнее: [docs/analysis.md](docs/analysis.md).

---

## Тестирование / Testing

```bash
# Все тесты / All tests
python -m pytest tests/ -v

# Unit тесты SMC
python -m pytest bot/tests/unit/smc/ -v

# Backtest config sync (34 теста)
python -m pytest bot/tests/unit/test_backtest_config.py -v

# SMC стратегия (без flaky market_structure)
python -m pytest tests/strategies/smc/ --ignore=tests/strategies/smc/test_market_structure.py -v
```

**Статус тестов**: 2197 passing, ~26 pre-existing failures (web tests password, flaky SMC market_structure).

Тестовый сервер: 158.160.215.57 (Python 3.12, `.venv`, без Docker).

---

## Документация / Documentation

| Документ | Описание |
|----------|----------|
| **[docs/analysis.md](docs/analysis.md)** | Анализ проекта: сильные/слабые стороны, конфликты Live↔Backtest, результаты Phase 1 |
| **[docs/plan.md](docs/plan.md)** | План развития: 7 направлений, приоритеты P0–P3, таймлайн |
| **[docs/planV2.md](docs/planV2.md)** | План V2.0: концепция «Идеального трейдера», P0-фиксы, оптимизация Phase 2, параметры по парам |
| **[docs/architecture.md](docs/architecture.md)** | Блок-схемы, сравнительные таблицы Live vs Backtest |
| [docs/SESSION_CONTEXT.md](docs/SESSION_CONTEXT.md) | Полная история разработки по сессиям |

---

## Инфраструктура / Infrastructure

| Сервер | IP | Роль | Статус |
|--------|----|------|--------|
| Production | 185.233.200.13 | 5 live ботов | ✅ Running |
| Testing | 158.160.215.57 | Бэктесты, тесты | ✅ Active |

```bash
# Деплой / Deploy
tar czf /tmp/sync.tar.gz bot/ scripts/ configs/
scp /tmp/sync.tar.gz ai-agent@185.233.200.13:/tmp/
ssh ai-agent@185.233.200.13 "cd ~/TRADERAGENT && tar xzf /tmp/sync.tar.gz && docker compose restart bot"

# Логи / Logs
ssh ai-agent@185.233.200.13 "docker logs traderagent-bot --tail 50 -f"

# Тестовый сервер / Testing server
ssh ai-agent@158.160.215.57 "cd ~/TRADERAGENT && .venv/bin/python -m pytest tests/ -x -q"
```

**Stack**: Python 3.12, asyncio, PostgreSQL 15, Redis 7, Docker Compose, Bybit V5 API.

---

## Лицензия / License

[Mozilla Public License 2.0](LICENSE)
