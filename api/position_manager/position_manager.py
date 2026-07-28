"""
Хранит состояние единственной открытой paper-позиции.

До первого исправления has_open_position() был захардкожен на False —
т.е. правило TRADE_LIFECYCLE "одновременно только одна позиция" не
проверялось нигде. Это было исправлено, но состояние оставалось только
в памяти процесса — рестарт терял открытую позицию (см. известное
ограничение F17 в AUTOTRADING_RISK_REGISTER.md). Здесь это исправлено:
состояние сохраняется на диск (JSON) после каждой мутации, и при
следующем запуске процесса (или создании нового экземпляра) состояние
восстанавливается — это то, что делает возможным "restart recovery"
для exit-монитора (см. api/execution/exit_monitor.py).
"""

import json
import os

DEFAULT_STATE_PATH = os.path.join("state", "position_manager.json")


class PositionManager:

    _position = None
    _last_signature = None
    _last_session_key = None
    _state_path = DEFAULT_STATE_PATH
    _loaded = False

    @classmethod
    def _ensure_loaded(cls):
        if cls._loaded:
            return
        cls._load()
        cls._loaded = True

    @classmethod
    def _load(cls):
        if not os.path.exists(cls._state_path):
            return
        try:
            with open(cls._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, TypeError):
            # Повреждённое состояние -> безопасный холодный старт без позиции,
            # а не падение процесса (см. Red Team: "checkpoint повреждён").
            return

        cls._position = data.get("position")
        signature = data.get("last_signature")
        session_key = data.get("last_session_key")
        cls._last_signature = tuple(signature) if isinstance(signature, list) else signature
        cls._last_session_key = tuple(session_key) if isinstance(session_key, list) else session_key

    @classmethod
    def _persist(cls):
        os.makedirs(os.path.dirname(cls._state_path) or ".", exist_ok=True)
        tmp = cls._state_path + ".tmp"
        payload = {
            "position": cls._position,
            "last_signature": list(cls._last_signature) if isinstance(cls._last_signature, tuple) else cls._last_signature,
            "last_session_key": list(cls._last_session_key) if isinstance(cls._last_session_key, tuple) else cls._last_session_key,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, cls._state_path)

    @classmethod
    def has_open_position(cls) -> bool:
        cls._ensure_loaded()
        return cls._position is not None

    @classmethod
    def current_position(cls):
        cls._ensure_loaded()
        return cls._position

    @classmethod
    def is_duplicate_signature(cls, signature) -> bool:
        """Блокирует повторную отправку идентичного ордера (idempotency)."""
        cls._ensure_loaded()
        return signature is not None and cls._last_signature == signature

    @classmethod
    def is_duplicate_session(cls, session_key) -> bool:
        """Блокирует повторный вход в рамках уже отторгованной сессии."""
        cls._ensure_loaded()
        return session_key is not None and cls._last_session_key == session_key

    @classmethod
    def open_position(cls, position: dict, signature=None, session_key=None):
        cls._ensure_loaded()

        if cls._position is not None:
            raise RuntimeError(
                "Попытка открыть позицию при уже открытой позиции."
            )

        cls._position = position
        cls._last_signature = signature

        if session_key is not None:
            cls._last_session_key = session_key

        cls._persist()

    @classmethod
    def close_position(cls, reason: str = ""):
        cls._ensure_loaded()
        closed = cls._position
        cls._position = None
        cls._persist()
        return closed

    @classmethod
    def reset(cls, state_path: str = None):
        """Только для тестов и безопасного холодного рестарта."""
        if state_path is not None:
            cls._state_path = state_path

        cls._position = None
        cls._last_signature = None
        cls._last_session_key = None
        cls._loaded = True

        if os.path.exists(cls._state_path):
            os.remove(cls._state_path)
