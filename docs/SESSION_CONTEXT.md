# TRADERAGENT v2.0 — Контекст проекта

> Обновлено: 2026-03-09

---

## Статус

- **Версия:** v2.0.0
- **Тесты:** 2197 passing | ruff PASS | black PASS
- **Бот:** RUNNING — 5 ботов на `185.233.200.13`
  - demo_btc_hybrid, demo_eth_grid, demo_sol_dca, demo_btc_trend, demo_btc_smc
- **Backtest V3.0:** Phase 1 COMPLETE — 43/43 пар

---

## Инфраструктура

| Сервер | IP | Роль |
|--------|----|------|
| Production | 185.233.200.13 | Live-бот (Docker Compose, `docker compose restart bot`) |
| Dev | /home/hive/TRADERAGENT | Локальная разработка |

**Доступ:**
```bash
# SSH на production
sudo -u hive ssh -i /home/hive/.ssh/id_ed25519 ai-agent@185.233.200.13
# Git операции
sudo -u hive git -C /home/hive/TRADERAGENT ...
# Deploy файлов
sudo -u hive scp -i /home/hive/.ssh/id_ed25519 <file> ai-agent@185.233.200.13:/home/ai-agent/TRADERAGENT/<path>
```

---

## Архитектура (текущая, после рефакторинга)

```
BotOrchestrator
├── StrategySelector → RoutingConfig (configs/strategy_routing.yaml)
├── StrategyConductor (RegimeAnalysis → TradingMode → StrategyDirective)
├── SMCStructureAnalyzer (кеширующий SMC-анализатор, 5-мин TTL)
├── MarketRegimeDetector (ADX/EMA/ATR + SMC-first classification)
├── PortfolioRiskManager (per-symbol агрегация, global_stop_loss 2.5%)
├── RiskManager (per-bot лимиты)
└── 4 Strategy Adapters:
    ├── GridAdapter (+ CorePosition)
    ├── DCAAdapter (+ CorePosition)
    ├── TrendFollowerAdapter
    └── SMCAdapter
```

**Маршрутизация (единый конфиг `strategy_routing.yaml`):**
```
bull_trend + high_confluence → {dca:0.5, grid:0.3, tf:0.2}
bull_trend (normal)          → {tf:0.7, dca:0.3}
bear_trend                   → {dca:1.0}
tight_range / wide_range     → {grid:1.0}
volatile_transition          → {smc:1.0}
accumulation / distribution  → {smc:1.0}
fallback                     → {grid:0.25, dca:0.25, tf:0.25, smc:0.25}
```

---

## Выполненные этапы рефакторинга

### «Единый разумный трейдер» (Issues #356–#360) — ВСЕ MERGED

| Issue | Компонент | PR |
|-------|-----------|----|
| #356 | SMCStructureAnalyzer + MarketRegimeDetector SMC-first | PR #361 |
| #357 | StrategyConductor (иерархическое управление) | PR #362 |
| #358 | PortfolioRiskManager (глобальный stop-loss) | PR #363 |
| #359 | CorePosition (DCA+Grid координация) | PR #365 |
| #360 | Two-phase Cooldown с SMC confirmation | PR #364 |

### Epic #1: Унификация маршрутизации (Issues #368–#371) — ВСЕ MERGED

| Issue | Компонент | PR |
|-------|-----------|----|
| #369 | strategy_routing.yaml + RoutingConfig | PR #373 |
| #370 | StrategySelector → RoutingConfig | PR #374 |
| #371 | StrategyRouter (backtest) → RoutingConfig | PR #375 |
| #368 | Интеграционные кросс-тесты | PR #372 |

---

## Phase 1 Backtest — ключевые результаты

- **5 из 37 прибыльных** (BAT +3.8%, ETC +1.85%, BCH +1.21%, BNB +1.09%, UNI +0.25%)
- **SMC = 0 сделок** во всех парах (throttle + risk manager блокируют входы)
- **DCA катастрофичен в даунтренде:** avg -$10k/пара
- **Avg:** -11.68% return, -1.545 Sharpe, 15.4% MaxDD

---

## Оставшиеся расхождения Live ↔ Backtest

| Проблема | Влияние |
|----------|---------|
| Regime check: 60s (live) vs 12 баров/1h (backtest) | Backtest менее реактивен |
| Daily loss: $600@$10k vs $60@$1k | Ложные стопы в backtest |
| win_rate: sum(pnl)>0 → 100% | Нужен per-trade расчёт |
| HybridCoordinator не в backtest | Нет Hybrid-тестирования |
| StrategyConductor, CorePosition не в backtest | Опционально |

---

## Следующие шаги (приоритет)

1. **P0: Диагностика SMC = 0 сделок** — smoke-test одной пары с SMC-only
2. **P1: Масштабирование daily_loss** — пропорциональный лимит вместо абсолютного
3. **P2: Оптимизация DCA** — `price_deviation_pct=3-5%`, `max_safety_orders=3`
4. **P3: Phase 2 backtest** — перезапуск с исправленными параметрами

---

## Ключевые файлы

| Файл | Описание |
|------|----------|
| `docs/bot_architecture_v2.md` | Алгоритмы живого бота (mermaid-диаграммы) |
| `docs/architecture_v2.md` | Сравнение Live vs Backtest |
| `docs/analysis.md` | Анализ проекта, сильные/слабые стороны |
| `docs/plan.md` | 7 направлений развития (A–G) |
| `docs/persona.md` | Роль: криптотрейдер + senior dev |
| `configs/strategy_routing.yaml` | Единый конфиг маршрутизации |
| `configs/phase7_demo.yaml` | Конфиг 5 ботов |
| `bot/orchestrator/strategy_selector.py` | Выбор стратегий (live) |
| `bot/orchestrator/strategy_conductor.py` | Координатор стратегий |
| `bot/orchestrator/routing_config.py` | Загрузчик RoutingConfig |
| `bot/core/smc/structure_analyzer.py` | Кеширующий SMC-анализатор |
| `bot/core/portfolio_risk_manager.py` | Портфельный риск |
| `bot/tests/backtesting/orchestrator_engine.py` | BacktestOrchestratorEngine V3.0 |

---

## Quick Commands

```bash
# Все тесты
python -m pytest bot/tests/ tests/ --ignore=bot/tests/testnet -q

# Тесты по группам
python -m pytest bot/tests/ --ignore=bot/tests/testnet -q       # bot (385)
python -m pytest tests/strategies/ -q                            # strategies (743)
python -m pytest tests/orchestrator/ -q                          # orchestrator (143)

# Production
sudo -u hive ssh -i /home/hive/.ssh/id_ed25519 ai-agent@185.233.200.13
docker compose logs bot --tail=50
docker compose restart bot
```

---

## Ссылки

- **Repo:** https://github.com/alekseymavai/TRADERAGENT
- **Release v2.0.0:** https://github.com/alekseymavai/TRADERAGENT/releases/tag/v2.0.0
