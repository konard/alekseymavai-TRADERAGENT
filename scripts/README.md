# Scripts

## Активные скрипты

| Скрипт | Назначение |
|--------|-----------|
| `run_backtest_v2.py` | **Основной** — 4-phase pipeline: baseline → optimize → portfolio → robustness |
| `smoke_smc.py` | Диагностика SMC стратегии (single pair, verbose logging) |
| `backfill_history.py` | Backfill исторических OHLCV данных (H1+M5) в TimescaleDB |
| `verify_backtest_parity.py` | Проверка live↔backtest соответствия параметров |
| `add_bybit_credentials.py` | Добавление API ключей в зашифрованное хранилище (PostgreSQL) |
| `test_bybit_connection.py` | Проверка подключения к Bybit (demo или mainnet) |
| `test_demo_trading.py` | Тест demo-режима (api-demo.bybit.com) |
| `validate_demo.py` | Pre-deployment валидация конфигурации |
| `verify_deployment.sh` | Проверка статуса деплоя на production-сервере |
| `backup_db.sh` | Бэкап базы данных PostgreSQL |
| `download_historical_data.py` | Скачивание исторических данных из Bybit |

## Команды

```bash
# Smoke-тест одной пары
python scripts/run_backtest_v2.py --mode single --symbol BTC/USDT --max-bars 3000

# Phase 1: Baseline 43 пар
python scripts/run_backtest_v2.py --mode multi --workers 4

# Phase 2: Оптимизация топ-10 пар
python scripts/run_backtest_v2.py --mode multi --phase optimize \
  --config configs/backtest_phase2.yaml --workers 8

# SMC диагностика
python scripts/smoke_smc.py --symbol BTC/USDT --bars 3000 --balance 10000 --verbose
```

## deprecated/

Устаревшие скрипты (V1.0 era, заменены `run_backtest_v2.py`):
- `run_grid_backtest.py` — Single-pair Grid backtest (v1)
- `run_grid_backtest_all.py` — Multi-pair Grid backtest (v1)
- `run_dca_tf_smc_pipeline.py` — Old DCA+TF+SMC pipeline (v1)
- `run_multi_strategy_backtest.py` — Old multi-strategy runner
- `run_xrp_backtest.py` — XRP-specific backtest (v1)
- `run_replay.py` — Replay backtest (superseded by orchestrator_engine)
- Прочие вспомогательные скрипты
