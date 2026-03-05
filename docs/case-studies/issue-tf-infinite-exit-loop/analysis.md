# Case Study: TrendFollower Infinite Exit Loop

> **Issue ID:** tf-infinite-exit-loop
> **Дата обнаружения:** 2026-03-05
> **Дата исправления:** 2026-03-05 (коммит d817507)
> **Серьёзность:** Критическая — бесконечный цикл рыночных ордеров, утечка баланса

---

## 1. Описание проблемы

После срабатывания stop_loss у стратегии TrendFollower бот бесконечно отправлял рыночные ордера Sell по одной и той же позиции. Баланс демо-счёта уменьшался каждую секунду. Проблема обнаружена при просмотре логов контейнера.

---

## 2. Временная шкала событий

```
15:25:44 - trend_follower_position_closed  position_id=f4fe28b5  exit_reason=stop_loss  exit_price=71424.2
15:25:44 - Created market order  order_id=52a76ee7  side=sell  amount=0.012
15:25:45 - trend_follower_position_closed  position_id=f4fe28b5  exit_reason=stop_loss  exit_price=71424.2  ← ПОВТОР!
15:25:45 - Created market order  order_id=a7c82104  side=sell  amount=0.012  ← ВТОРОЙ ОРДЕР
15:25:46 - trend_follower_position_closed  position_id=f4fe28b5  exit_reason=stop_loss  exit_price=71424.2  ← ПОВТОР!
15:25:46 - Created market order  order_id=65700f96  side=sell  amount=0.012  ← ТРЕТИЙ ОРДЕР
15:25:47 - trend_follower_position_closed  position_id=f4fe28b5  ← ПОВТОР!
...
balance: 99905.94 → 99905.47 → 99904.53 → 99904.00 → ...  ← УТЕЧКА
```

Цикл повторялся ~каждую секунду. Бот остановлен вручную.

---

## 3. Root Cause Analysis

### 3.1 Прямая причина

В методе `_process_trend_follower_logic()` (`bot/orchestrator/bot_orchestrator.py`) при срабатывании `exit_reason` выполнялось:
1. ✅ Отправка рыночного ордера Sell через `_execute_trend_follower_exit()`
2. ✅ Публикация события `ORDER_FILLED`
3. ✅ Логирование `trend_follower_position_closed`
4. ❌ **НЕ вызывался** `self.trend_follower_strategy.close_position(position_id, ...)`

### 3.2 Механизм бесконечного цикла

```
Тик N:   update_position(P1) → stop_loss → execute_exit() → [P1 НЕ удалён из active_positions]
Тик N+1: active_positions = {P1: ...}  ← позиция всё ещё там
         update_position(P1) → price ≤ sl → stop_loss → execute_exit() → [P1 НЕ удалён]
Тик N+2: [повторяется]
...бесконечно
```

### 3.3 Почему `close_position()` критичен

```python
# bot/strategies/trend_follower/position_manager.py:263-277
def close_position(self, position_id: str, reason: ExitReason) -> None:
    if position_id in self.active_positions:
        ...
        del self.active_positions[position_id]  # ← ЕДИНСТВЕННОЕ место удаления позиции
```

Без вызова этого метода позиция остаётся в `active_positions` вечно.

---

## 4. Код до и после исправления

### До (строки 1541-1557 до коммита d817507)

```python
if exit_reason:
    position = self.trend_follower_strategy.position_manager.active_positions.get(
        position_id
    )
    if not self.config.dry_run and position:
        await self._execute_trend_follower_exit(position)   # ✅ ордер отправлен
    # ❌ НЕТ вызова close_position() — позиция не удаляется!
    await self._publish_event(EventType.ORDER_FILLED, {...})
    logger.info("trend_follower_position_closed", ...)
```

### После (коммит d817507)

```python
if exit_reason:
    position = self.trend_follower_strategy.position_manager.active_positions.get(
        position_id
    )
    if not self.config.dry_run and position:
        await self._execute_trend_follower_exit(position)   # ✅ ордер отправлен

    self.trend_follower_strategy.close_position(            # ✅ ДОБАВЛЕНО
        position_id, exit_reason, self.current_price
    )

    await self._publish_event(EventType.ORDER_FILLED, {...})
    logger.info("trend_follower_position_closed", ...)
```

---

## 5. Корректный паттерн (SMC как эталон)

SMC-стратегия делала это правильно (`bot/orchestrator/bot_orchestrator.py:1628-1635`):

```python
exits = self.smc_strategy.update_positions(self.current_price, pd.DataFrame())

for position_id, exit_reason in exits:
    self.smc_strategy.close_position(position_id, exit_reason, self.current_price)  # ✅ СНАЧАЛА
    if not self.config.dry_run:
        await self._execute_smc_exit(position_id, exit_reason)
    await self._publish_event(...)
```

**Ключевое отличие:** SMC вызывает `close_position()` **до** отправки ордера, TF вызывал **после** (и вовсе не вызывал).

| Аспект | TF (до фикса) | SMC | TF (после фикса) |
|--------|--------------|-----|-----------------|
| Удаление из dict | ❌ Нет | ✅ `.pop()` | ✅ через `close_position()` |
| Повторный вход | ✅ Возможен | ❌ Невозможен | ❌ Невозможен |
| Биржевых ордеров | ∞ | ровно N | ровно N |

---

## 6. Тесты, которые могли поймать баг

### Отсутствующий тест #1: Идемпотентность exit

```python
async def test_stop_loss_fires_only_once(orchestrator, mock_exchange):
    """После stop_loss позиция должна быть удалена, следующий тик не должен слать ордер."""
    # Открыть позицию
    position_id = orchestrator.trend_follower_strategy.open_position(signal, size)

    # Установить цену ниже stop_loss
    orchestrator.current_price = sl_price

    # Первый тик — должен закрыть позицию
    await orchestrator._process_trend_follower_logic()
    assert mock_exchange.create_order.call_count == 1

    # Второй тик — НЕ должен создавать новый ордер
    await orchestrator._process_trend_follower_logic()
    assert mock_exchange.create_order.call_count == 1  # Без изменений!
```

### Отсутствующий тест #2: Позиция удалена после exit

```python
async def test_position_removed_from_active_after_exit(orchestrator):
    position_id = orchestrator.trend_follower_strategy.open_position(signal, size)
    assert position_id in orchestrator.trend_follower_strategy.position_manager.active_positions

    orchestrator.current_price = sl_price
    await orchestrator._process_trend_follower_logic()

    assert position_id not in orchestrator.trend_follower_strategy.position_manager.active_positions
```

---

## 7. Предложения по предотвращению

### 7.1 Защитное программирование в оркестраторе

Добавить проверку в `_execute_trend_follower_exit()`:
```python
async def _execute_trend_follower_exit(self, position: Any) -> None:
    position_id = getattr(position, 'position_id', None)
    if position_id and position_id not in self.trend_follower_strategy.position_manager.active_positions:
        logger.warning("tf_exit_for_already_closed_position", position_id=position_id)
        return
    ...
```

### 7.2 Унифицировать паттерн exit для всех стратегий

Создать базовый метод в оркестраторе:
```python
async def _execute_strategy_exit(self, strategy, position_id, exit_reason, current_price):
    """Единый exit flow: ордер → close_position → event → log."""
    position = strategy.get_position(position_id)
    if not self.config.dry_run and position:
        await self._execute_exchange_order(position, exit_reason)
    strategy.close_position(position_id, exit_reason, current_price)  # всегда
    await self._publish_event(EventType.ORDER_FILLED, {...})
```

### 7.3 Интеграционные тесты в CI

Добавить в pytest suite:
- `tests/integration/test_exit_patterns.py` — проверка всех 4 стратегий на идемпотентность exit

---

## 8. Влияние на демо-счёт

- Время активности бага: неизвестно (обнаружен при проверке логов)
- Примерное количество ордеров-дублей: 70+ (по `tryings=71` в Telegram polling)
- Потеря баланса: ~$100 (с $99905 до ~$99800)
- Тип счёта: Bybit demo (виртуальные деньги, реальных потерь нет)

---

## 9. Связанные файлы

| Файл | Роль |
|------|------|
| `bot/orchestrator/bot_orchestrator.py:1541-1557` | Место бага и фикса |
| `bot/orchestrator/bot_orchestrator.py:1628-1635` | Эталон — корректная SMC реализация |
| `bot/strategies/trend_follower/trend_follower_strategy.py:291-344` | `close_position()` |
| `bot/strategies/trend_follower/position_manager.py:263-277` | Удаление из `active_positions` |
| `bot/strategies/smc_adapter.py:235-269` | SMC `close_position()` через `.pop()` |
