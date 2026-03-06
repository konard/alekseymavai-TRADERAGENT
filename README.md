# TRADERAGENT v2.0

[![License: MPL 2.0](https://img.shields.io/badge/License-MPL%202.0-brightgreen.svg)](https://opensource.org/licenses/MPL-2.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests: 1687+ passing](https://img.shields.io/badge/tests-1687%20passing-brightgreen.svg)]()
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
| BacktestOrchestratorEngine V3.0 | ✅ Phase 1 завершён (37 пар, 50k баров) |
| SMC критический баг | ✅ Исправлен (сессия 47) — 0 → 1086 сделок |
| Live↔Backtest sync | 🟡 P0.1-P0.3 done, роутер в процессе |
| Telegram уведомления | ❌ Сеть недоступна на сервере |

**Текущий фокус:** синхронизация Live↔Backtest, сбор реальных данных, Phase 2 оптимизация.

---

## Стратегии / Strategies

| Стратегия | Описание | Live | Backtest |
|-----------|----------|------|---------|
| **Grid** | Сетка limit-ордеров в ценовом диапазоне | ✅ | ✅ |
| **DCA** | Усреднение позиции при падении цены | ✅ | ✅ |
| **Trend Follower** | EMA/ATR/RSI trend-following с trailing stop | ✅ | ✅ |
| **SMC** | H1 структура + M5 вход, Order Blocks, FVG | ✅ | ✅ |
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
│   ├── grid/ + grid_adapter.py      # Grid strategy
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
→ **[docs/analysis.md](docs/analysis.md)**

Detailed architecture analysis, strengths/weaknesses, and Live↔Backtest conflicts:
→ **[docs/analysis.md](docs/analysis.md)**

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

# Phase 1: Baseline 37 пар / 37 pairs baseline
python scripts/run_backtest_v2.py --mode multi --symbols BTC,ETH,SOL --workers 8

# SMC диагностика / SMC diagnostics
python scripts/smoke_smc.py --bars 2000 --trend up --warmup 200
```

### Ключевые особенности / Key features

- Параметры из live YAML: `--live-config configs/phase7_demo.yaml`
- Параллельный запуск: Phase 1 на 14 CPU = 35 мин для 37 пар
- Реальный P&L: `(exit_price - entry_price) × amount`
- Advisory routing по режиму рынка

### Phase 1 результаты (2026-03-03, данные Aug 2025 – Feb 2026)

> ⚠️ SMC = 0 сделок — исправлен баг в сессии 47. Нужен новый прогон.

| Метрика | Значение |
|---------|---------|
| Пар обработано | 28 из 37 |
| Ср. return | +0.35% (артефакт PnL-заглушки) |
| SMC сделок | 0 (баг исправлен) |
| Ср. strategy switches | 199/пару |

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

**Статус тестов**: 1687+ passing, ~36 pre-existing failures (web tests password, flaky SMC market_structure).

---

## Документация / Documentation

| Документ | Описание |
|----------|----------|
| **[docs/analysis.md](docs/analysis.md)** | Анализ проекта: сильные/слабые стороны, конфликты Live↔Backtest, унификация параметров |
| **[docs/plan.md](docs/plan.md)** | План развития: P0–P3 задачи по направлениям |
| [docs/SESSION_CONTEXT.md](docs/SESSION_CONTEXT.md) | Полная история разработки по сессиям |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Деплой на продакшн-сервер |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Справочник по конфигурации |
| [docs/BYBIT_DEMO_TRADING_SOLUTION.md](docs/BYBIT_DEMO_TRADING_SOLUTION.md) | Настройка Bybit Demo API |
| [docs/case-studies/](docs/case-studies/) | Разбор критических багов |

---

## Инфраструктура / Infrastructure

| Сервер | IP | Роль | Статус |
|--------|----|------|--------|
| Production | 185.233.200.13 | 5 live ботов | ✅ Running |
| Testing | 158.160.215.57 | Бэктесты | ⏸️ Выключен |
| Dev | 173.249.2.184 | Разработка | — |

```bash
# Деплой / Deploy
tar czf /tmp/sync.tar.gz bot/ scripts/ configs/
scp /tmp/sync.tar.gz ai-agent@185.233.200.13:/tmp/
ssh ai-agent@185.233.200.13 "cd ~/TRADERAGENT && tar xzf /tmp/sync.tar.gz && docker compose restart bot"

# Логи / Logs
ssh ai-agent@185.233.200.13 "docker logs traderagent-bot --tail 50 -f"
```

**Stack**: Python 3.12, asyncio, PostgreSQL 15, Redis 7, Docker Compose, Bybit V5 API.

---

## Лицензия / License

[Mozilla Public License 2.0](LICENSE)
