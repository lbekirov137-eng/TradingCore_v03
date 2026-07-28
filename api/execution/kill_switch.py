"""
Safe-stop / kill switch.

Поведение при активации (engage):
  - новые входы блокируются немедленно (DecisionEngine должен проверять
    KillSwitch.is_engaged() до одобрения любой сделки — см. wiring в
    decision_engine.py);
  - мониторинг УЖЕ открытых позиций не останавливается — exit monitor
    обязан продолжать работать, чтобы существующий риск не остался без
    присмотра;
  - отложенные (ещё не подтверждённые биржей) входные ордера отменяются
    безопасно через ExchangeAdapter.cancel_order, если он передан;
  - закрытие уже открытых позиций происходит ТОЛЬКО если явно
    сконфигурировано (close_positions_on_engage=True) — по умолчанию
    kill switch не закрывает существующие позиции сам по себе, т.к.
    паническое закрытие по рынку может быть хуже, чем управляемый выход
    по SL/TP.

Состояние — на диске (JSON), чтобы переживать рестарт процесса: если
kill switch был включён до падения процесса, он остаётся включённым
после перезапуска, пока оператор явно не снимет его (disengage).
"""

import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Optional

DEFAULT_STATE_PATH = os.path.join("state", "kill_switch.json")


@dataclass
class KillSwitchState:
    engaged: bool = False
    reason: str = ""
    engaged_at: Optional[float] = None
    engaged_by: str = ""
    close_positions_on_engage: bool = False


class KillSwitch:

    def __init__(self, state_path: str = DEFAULT_STATE_PATH):
        self.state_path = state_path
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        self._state = self._load()

    def _load(self) -> KillSwitchState:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return KillSwitchState(**data)
            except (json.JSONDecodeError, OSError, TypeError):
                # Повреждённый файл состояния трактуется консервативно:
                # лучше считать kill switch включённым (безопасная остановка),
                # чем случайно разрешить торговлю на повреждённом состоянии.
                return KillSwitchState(engaged=True, reason="Состояние kill switch повреждено на диске — безопасная остановка.")
        return KillSwitchState()

    def _persist(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self._state), f)
        os.replace(tmp, self.state_path)

    def engage(self, reason: str, operator: str = "unknown", close_positions: bool = False,
               adapter=None, pending_client_order_ids: Optional[list] = None):

        self._state = KillSwitchState(
            engaged=True, reason=reason, engaged_at=time.time(),
            engaged_by=operator, close_positions_on_engage=close_positions,
        )
        self._persist()

        cancelled = []
        if adapter is not None and pending_client_order_ids:
            for client_order_id in pending_client_order_ids:
                try:
                    adapter.cancel_order(client_order_id)
                    cancelled.append(client_order_id)
                except Exception:
                    # Отмена — best-effort безопасности; ошибка отмены не
                    # должна маскировать сам факт включения kill switch.
                    continue

        return {"engaged": True, "reason": reason, "cancelled_pending_orders": cancelled}

    def disengage(self, operator: str = "unknown"):
        self._state = KillSwitchState(engaged=False, reason="", engaged_at=None, engaged_by=operator)
        self._persist()
        return {"engaged": False}

    def is_engaged(self) -> bool:
        return self._state.engaged

    def should_close_positions(self) -> bool:
        return self._state.engaged and self._state.close_positions_on_engage

    def status(self) -> dict:
        return asdict(self._state)

    def reset(self):
        """Только для тестов."""
        self._state = KillSwitchState()
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
