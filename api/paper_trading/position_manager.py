import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.paper_trading.cost_model import (
    TradingCostConfig,
    compute_trade_costs,
)


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
        cost_config: TradingCostConfig | None = None,
    ) -> None:
        self.state_file = Path(
            state_file
            if state_file is not None
            else self.DEFAULT_STATE_FILE
        )

        # Издержки по умолчанию берутся из окружения с консервативными
        # значениями. Явно нулевую модель нужно передать осознанно —
        # случайно остаться без комиссий нельзя.
        self.cost_config = (
            cost_config
            if cost_config is not None
            else TradingCostConfig.from_env()
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

    # Тот же порог, что и в TradePlanStep. Дублируется намеренно: это
    # защита в глубину, а не единственная проверка. Менеджер позиций не
    # должен принимать недостижимый филл даже если выше по конвейеру
    # проверку обошли, отключили или забыли применить к новой стратегии.
    ENTRY_DRIFT_TOLERANCE_R = 0.5

    def open_position(
        self,
        paper_order: dict[str, Any],
        opened_at_utc: str | None = None,
        market_price: float | None = None,
    ) -> dict[str, Any]:
        """
        Открывает виртуальную позицию.

        market_price — фактическая рыночная цена на момент входа. Если она
        передана, вход по недостижимому уровню отвергается: филл по цене,
        которой на рынке уже нет, порождает фиктивную прибыль (наблюдался
        предопределённый результат +3R на каждой свече). Параметр
        необязательный ради обратной совместимости с существующими
        вызовами и записями, но рабочий цикл обязан его передавать.
        """
        if self.has_open_position():
            return {
                "event": "POSITION_ALREADY_OPEN",
                "position": self.get_position(),
                "real_order_sent": False,
            }

        self._validate_paper_order(paper_order)

        drift_reason = self._entry_drift_reason(
            paper_order=paper_order,
            market_price=market_price,
        )

        if drift_reason is not None:
            return {
                "event": "NO_POSITION_OPENED",
                "reason": drift_reason,
                "position": None,
                "real_order_sent": False,
            }

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

    def _entry_drift_reason(
        self,
        *,
        paper_order: dict[str, Any],
        market_price: float | None,
    ) -> str | None:
        """
        Отвергает вход по уровню, до которого рынок уже не дотягивается.

        Если market_price не передан, проверка пропускается — иначе
        сломались бы существующие вызовы, не знающие про этот параметр.
        Но когда цена передана, отказ строгий и fail-closed.
        """
        if market_price is None:
            return None

        price = self._finite(market_price)

        if price is None:
            return (
                "ENTRY_LEVEL_UNVERIFIABLE: market price is not a finite number"
            )

        entry = self._finite(paper_order.get("entry"))
        stop = self._finite(paper_order.get("stop"))

        if entry is None or stop is None:
            return (
                "ENTRY_LEVEL_UNVERIFIABLE: entry/stop are not finite numbers"
            )

        risk_per_unit = abs(entry - stop)

        if risk_per_unit <= 0:
            return "ENTRY_LEVEL_UNVERIFIABLE: entry and stop coincide"

        drift = abs(price - entry)

        if drift > self.ENTRY_DRIFT_TOLERANCE_R * risk_per_unit:
            return (
                "ENTRY_LEVEL_NO_LONGER_VALID: market price "
                f"{price} deviates from entry {entry} by {drift:.2f} "
                f"({drift / risk_per_unit:.2f}R), which exceeds the allowed "
                f"{self.ENTRY_DRIFT_TOLERANCE_R}R"
            )

        return None

    @staticmethod
    def _finite(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None

        number = float(value)

        return number if math.isfinite(number) else None

    def _close_position(
        self,
        position: dict[str, Any],
        exit_price: float,
        exit_reason: str,
        closed_at_utc: str,
    ) -> dict[str, Any]:
        entry = float(position["entry"])
        quantity = float(position["quantity"])

        # Издержки считаются ОДИН раз, в момент закрытия, и раскладываются
        # на составляющие. gross_pnl — идеальный результат по сырым ценам,
        # net_pnl — после проскальзывания и обеих комиссий.
        costs = compute_trade_costs(
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
            side=str(position.get("side", "LONG")).upper(),
            config=self.cost_config,
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

        # realized_pnl — ФАКТИЧЕСКИЙ результат, то есть net.
        # Идеальный результат без трения остаётся доступен как gross_pnl,
        # чтобы разницу можно было увидеть и проверить.
        position["realized_pnl"] = costs["net_pnl"]

        position["entry_price_raw"] = costs["entry_price_raw"]
        position["entry_price_effective"] = costs["entry_price_effective"]
        position["exit_price_raw"] = costs["exit_price_raw"]
        position["exit_price_effective"] = costs["exit_price_effective"]
        position["gross_pnl"] = costs["gross_pnl"]
        position["entry_fee"] = costs["entry_fee"]
        position["exit_fee"] = costs["exit_fee"]
        position["total_fees"] = costs["total_fees"]
        position["slippage_cost"] = costs["slippage_cost"]
        position["net_pnl"] = costs["net_pnl"]
        position["fee_model_version"] = costs["fee_model_version"]
        position["cost_config"] = costs["cost_config"]

        position["real_order_sent"] = False

        self.reset_position()

        return {
            "event": "POSITION_CLOSED",
            "exit_reason": exit_reason,
            "exit_price": exit_price,
            # realized_pnl == net_pnl: фактический результат после издержек.
            "realized_pnl": costs["net_pnl"],
            "gross_pnl": costs["gross_pnl"],
            "net_pnl": costs["net_pnl"],
            "total_fees": costs["total_fees"],
            "slippage_cost": costs["slippage_cost"],
            "fee_model_version": costs["fee_model_version"],
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

        # Контракт paper_order изменился, а эта проверка отстала.
        #
        # Было (старый контракт): "side" переиспользовалось под значение
        # ТОРГОВОГО СИГНАЛА и содержало "BUY"/"SELL".
        # Стало (текущий контракт pipeline_v2): поля разделены —
        #   signal = "BUY"/"SELL"   (торговый сигнал)
        #   side   = "LONG"/"SHORT" (направление позиции,
        #                            PaperExecutionStep._side)
        #
        # Из-за этого условие `side != "BUY"` отвергало КАЖДЫЙ валидный
        # длинный ордер (side == "LONG"), и paper-цикл не мог открыть ни
        # одной позиции: любая свеча с решением TRADE заканчивалась
        # ValueError и записью FAILED_SAFELY.
        #
        # Принимаем оба контракта: приоритет у signal, fallback на side.
        # Это сохраняет совместимость с уже существующими вызовами и
        # записями, где side == "BUY". Торговая семантика не меняется:
        # как и раньше, допускается только вход в лонг (SPOT_LONG_ONLY),
        # SELL/SHORT по-прежнему отвергается.
        entry_direction = paper_order.get("signal")

        if entry_direction is None:
            entry_direction = paper_order.get("side")

        if str(entry_direction).upper() not in (
            "BUY",
            "LONG",
        ):
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