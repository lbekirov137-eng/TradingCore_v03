import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PaperPositionManager:
    """
    Управляет одной виртуальной SPOT LONG позицией.

    Возможности:
    - сохраняет открытую позицию на диск;
    - не допускает повторный вход;
    - отслеживает stop, TP1 и TP2;
    - использует консервативное правило:
      если stop и take-profit коснулись в одной свече,
      первым считается stop;
    - никогда не отправляет реальные ордера.
    """

    NAME = "Paper Position Manager"
    VERSION = "1.0.0"

    DEFAULT_STATE_FILE = Path(
        "data/paper_position.json"
    )

    def __init__(
        self,
        state_file: str | Path | None = None,
    ) -> None:
        self.state_file = Path(
            state_file
            if state_file is not None
            else self.DEFAULT_STATE_FILE
        )

    def has_open_position(self) -> bool:
        position = self.get_position()

        return bool(
            position
            and position.get("status") == "OPEN"
        )

    def get_position(
        self,
    ) -> dict[str, Any] | None:
        if not self.state_file.exists():
            return None

        try:
            with self.state_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                position = json.load(file)

        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Paper position state contains invalid JSON"
            ) from error

        except OSError as error:
            raise RuntimeError(
                "Unable to read paper position state"
            ) from error

        if not isinstance(position, dict):
            raise TypeError(
                "Paper position state must be a dictionary"
            )

        return position

    def open_position(
        self,
        paper_order: dict[str, Any],
        opened_at_utc: str | None = None,
    ) -> dict[str, Any]:
        if self.has_open_position():
            return {
                "event": "POSITION_ALREADY_OPEN",
                "position": self.get_position(),
                "real_order_sent": False,
            }

        self._validate_paper_order(paper_order)

        timestamp = (
            opened_at_utc
            if opened_at_utc is not None
            else self._utc_now()
        )

        position = {
            "status": "OPEN",
            "mode": "PAPER",
            "manager_version": self.VERSION,
            "real_order_sent": False,
            "exchange": paper_order["exchange"],
            "symbol": paper_order["symbol"],
            "timeframe": paper_order["timeframe"],
            "side": paper_order["side"],
            "execution_mode": paper_order.get(
                "execution_mode",
                "SPOT_LONG_ONLY",
            ),
            "entry": float(paper_order["entry"]),
            "quantity": float(
                paper_order["quantity"]
            ),
            "stop": float(paper_order["stop"]),
            "take_profit_1": float(
                paper_order["take_profit_1"]
            ),
            "take_profit_2": float(
                paper_order["take_profit_2"]
            ),
            "risk_amount": float(
                paper_order.get(
                    "risk_amount",
                    0.0,
                )
            ),
            "opened_at_utc": timestamp,
            "last_update_at_utc": timestamp,
            "last_market_price": float(
                paper_order["entry"]
            ),
            "unrealized_pnl": 0.0,
            "tp1_reached": False,
            "tp1_reached_at_utc": None,
            "closed_at_utc": None,
            "exit_price": None,
            "exit_reason": None,
            "realized_pnl": 0.0,
        }

        self._save_position(position)

        return {
            "event": "POSITION_OPENED",
            "position": position,
            "real_order_sent": False,
        }

    def evaluate_position(
        self,
        market_price: float,
        candle_high: float | None = None,
        candle_low: float | None = None,
        observed_at_utc: str | None = None,
    ) -> dict[str, Any]:
        position = self.get_position()

        if (
            position is None
            or position.get("status") != "OPEN"
        ):
            return {
                "event": "NO_OPEN_POSITION",
                "position": position,
                "real_order_sent": False,
            }

        self._validate_market_values(
            market_price=market_price,
            candle_high=candle_high,
            candle_low=candle_low,
        )

        price = float(market_price)

        high = (
            float(candle_high)
            if candle_high is not None
            else price
        )

        low = (
            float(candle_low)
            if candle_low is not None
            else price
        )

        timestamp = (
            observed_at_utc
            if observed_at_utc is not None
            else self._utc_now()
        )

        position["last_update_at_utc"] = timestamp
        position["last_market_price"] = price
        position["unrealized_pnl"] = round(
            (
                price
                - float(position["entry"])
            )
            * float(position["quantity"]),
            8,
        )

        stop = float(position["stop"])
        take_profit_1 = float(
            position["take_profit_1"]
        )
        take_profit_2 = float(
            position["take_profit_2"]
        )

        # Консервативная защита:
        # если одна свеча коснулась и stop, и цели,
        # сначала считается исполненным stop.
        if low <= stop:
            return self._close_position(
                position=position,
                exit_price=stop,
                exit_reason="STOP_LOSS",
                closed_at_utc=timestamp,
            )

        if high >= take_profit_2:
            return self._close_position(
                position=position,
                exit_price=take_profit_2,
                exit_reason="TAKE_PROFIT_2",
                closed_at_utc=timestamp,
            )

        if (
            high >= take_profit_1
            and position.get("tp1_reached")
            is not True
        ):
            position["tp1_reached"] = True
            position["tp1_reached_at_utc"] = (
                timestamp
            )

            self._save_position(position)

            return {
                "event": "TAKE_PROFIT_1_REACHED",
                "position": position,
                "real_order_sent": False,
            }

        self._save_position(position)

        return {
            "event": "POSITION_REMAINS_OPEN",
            "position": position,
            "real_order_sent": False,
        }

    def reset_position(self) -> dict[str, Any]:
        previous_position = self.get_position()

        if self.state_file.exists():
            try:
                self.state_file.unlink()
            except OSError as error:
                raise RuntimeError(
                    "Unable to reset paper position state"
                ) from error

        return {
            "event": "POSITION_STATE_RESET",
            "previous_position": previous_position,
            "real_order_sent": False,
        }

    def _close_position(
        self,
        position: dict[str, Any],
        exit_price: float,
        exit_reason: str,
        closed_at_utc: str,
    ) -> dict[str, Any]:
        entry = float(position["entry"])
        quantity = float(position["quantity"])

        realized_pnl = round(
            (exit_price - entry) * quantity,
            8,
        )

        position["status"] = "CLOSED"
        position["closed_at_utc"] = closed_at_utc
        position["last_update_at_utc"] = (
            closed_at_utc
        )
        position["last_market_price"] = exit_price
        position["exit_price"] = exit_price
        position["exit_reason"] = exit_reason
        position["unrealized_pnl"] = 0.0
        position["realized_pnl"] = realized_pnl
        position["real_order_sent"] = False

        self.reset_position()

        return {
            "event": "POSITION_CLOSED",
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "position": position,
            "real_order_sent": False,
        }

    def _save_position(
        self,
        position: dict[str, Any],
    ) -> None:
        self.state_file.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_file = self.state_file.with_suffix(
            ".tmp"
        )

        try:
            with temporary_file.open(
                "w",
                encoding="utf-8",
            ) as file:
                json.dump(
                    position,
                    file,
                    indent=2,
                    ensure_ascii=False,
                    default=str,
                )

            temporary_file.replace(
                self.state_file
            )

        except OSError as error:
            raise RuntimeError(
                "Unable to save paper position state"
            ) from error

    @classmethod
    def _validate_paper_order(
        cls,
        paper_order: Any,
    ) -> None:
        if not isinstance(paper_order, dict):
            raise TypeError(
                "Paper order must be a dictionary"
            )

        if paper_order.get("mode") != "PAPER":
            raise ValueError(
                "Paper order mode must be PAPER"
            )

        if (
            paper_order.get("status")
            != "FILLED_SIMULATED"
        ):
            raise ValueError(
                "Paper order status must be "
                "FILLED_SIMULATED"
            )

        if paper_order.get(
            "real_order_sent"
        ) is not False:
            raise ValueError(
                "Paper order must not send "
                "a real order"
            )

        if paper_order.get("side") != "BUY":
            raise ValueError(
                "Only BUY paper orders are supported"
            )

        required_text_fields = (
            "exchange",
            "symbol",
            "timeframe",
        )

        for field_name in required_text_fields:
            field_value = paper_order.get(
                field_name
            )

            if (
                not isinstance(field_value, str)
                or not field_value.strip()
            ):
                raise ValueError(
                    f"Paper order {field_name} "
                    "must be a non-empty string"
                )

        required_number_fields = (
            "entry",
            "quantity",
            "stop",
            "take_profit_1",
            "take_profit_2",
        )

        for field_name in required_number_fields:
            field_value = paper_order.get(
                field_name
            )

            if not cls._is_valid_number(
                field_value
            ):
                raise ValueError(
                    f"Paper order {field_name} "
                    "must be a finite number"
                )

            if float(field_value) <= 0:
                raise ValueError(
                    f"Paper order {field_name} "
                    "must be greater than zero"
                )

        entry = float(paper_order["entry"])
        stop = float(paper_order["stop"])
        take_profit_1 = float(
            paper_order["take_profit_1"]
        )
        take_profit_2 = float(
            paper_order["take_profit_2"]
        )

        if not (
            stop
            < entry
            < take_profit_1
            < take_profit_2
        ):
            raise ValueError(
                "BUY paper order levels must satisfy "
                "stop < entry < take_profit_1 "
                "< take_profit_2"
            )

    @classmethod
    def _validate_market_values(
        cls,
        market_price: Any,
        candle_high: Any,
        candle_low: Any,
    ) -> None:
        if not cls._is_valid_number(
            market_price
        ):
            raise ValueError(
                "Market price must be a finite number"
            )

        price = float(market_price)

        if price <= 0:
            raise ValueError(
                "Market price must be greater than zero"
            )

        if candle_high is not None:
            if not cls._is_valid_number(
                candle_high
            ):
                raise ValueError(
                    "Candle high must be "
                    "a finite number"
                )

        if candle_low is not None:
            if not cls._is_valid_number(
                candle_low
            ):
                raise ValueError(
                    "Candle low must be "
                    "a finite number"
                )

        high = (
            float(candle_high)
            if candle_high is not None
            else price
        )

        low = (
            float(candle_low)
            if candle_low is not None
            else price
        )

        if low <= 0 or high <= 0:
            raise ValueError(
                "Candle values must be "
                "greater than zero"
            )

        if low > high:
            raise ValueError(
                "Candle low must not exceed "
                "candle high"
            )

        if not low <= price <= high:
            raise ValueError(
                "Market price must be between "
                "candle low and candle high"
            )

    @staticmethod
    def _is_valid_number(
        value: Any,
    ) -> bool:
        if isinstance(value, bool):
            return False

        if not isinstance(
            value,
            (int, float),
        ):
            return False

        return math.isfinite(float(value))

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(
            timezone.utc
        ).isoformat()