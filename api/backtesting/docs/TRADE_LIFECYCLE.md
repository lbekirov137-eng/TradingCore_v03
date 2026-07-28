# Trade Lifecycle v1

## Состояния сделки

WAIT_SIGNAL
    ↓
OPENING_RANGE
    ↓
BREAKOUT
    ↓
RETEST
    ↓
CONFIRMATION
    ↓
ENTRY
    ↓
POSITION_OPEN
    ↓
TP1_HIT
    ↓
TP2_HIT
    ↓
STOP_LOSS
    ↓
POSITION_CLOSED

---

## Правила

1. Одновременно только одна позиция.

2. Новая сделка невозможна,
   пока предыдущая не закрыта.

3. Все действия записываются в журнал.

4. Любая сделка проходит Backtesting
   раньше, чем допускается к реальной торговле.

5. Decision Engine никогда
   не открывает сделку напрямую.

6. Исполнение всегда проходит
   через Risk Engine.