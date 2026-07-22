import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.core.bootstrap import Bootstrap
from api.paper_trading.position_manager import (
    PaperPositionManager,
)
from api.leverage_risk_engine import LeverageRiskEngine
from api.unified_market_context import build_unified_market_context

from ai_observer.filter_adapter import run_shadow_filter
from ai_observer.opportunity_review import (
    build_daily_opportunity_summary,
    review_closed_candle,
)
from ai_observer.telegram_notifier import TelegramNotifier
from live_paper_run import build_live_context


POLL_INTERVAL_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 60 * 60
PAPER_CAPITAL_USD = 1000.0

DATA_DIRECTORY = Path("data")
JOURNAL_FILE = DATA_DIRECTORY / "paper_runs.jsonl"
RUNTIME_STATE_FILE = (
    DATA_DIRECTORY / "paper_runtime_state.json"
)

TELEGRAM_NOTIFIER = TelegramNotifier()


def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def append_journal(
    record: dict[str, Any],
) -> None:
    DATA_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    with JOURNAL_FILE.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write(
            json.dumps(
                record,
                ensure_ascii=False,
                default=str,
            )
        )
        file.write("\n")


def calculate_closed_balance() -> float:
    """
    РЎС‡РёС‚Р°РµС‚ Р·Р°РєСЂС‹С‚С‹Р№ PAPER-Р±Р°Р»Р°РЅСЃ РїРѕ Р¶СѓСЂРЅР°Р»Сѓ.

    РћС€РёР±РєР° С‡С‚РµРЅРёСЏ Р¶СѓСЂРЅР°Р»Р° РЅРµ РґРѕР»Р¶РЅР° РѕСЃС‚Р°РЅР°РІР»РёРІР°С‚СЊ С‚РѕСЂРіРѕРІР»СЋ.
    """
    realized_pnl = 0.0

    if not JOURNAL_FILE.exists():
        return PAPER_CAPITAL_USD

    try:
        with JOURNAL_FILE.open(
            "r",
            encoding="utf-8-sig",
        ) as file:
            for raw_line in file:
                line = raw_line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(record, dict):
                    continue

                position_event = record.get(
                    "position_event"
                )

                if not isinstance(
                    position_event,
                    dict,
                ):
                    continue

                if (
                    position_event.get("event")
                    != "POSITION_CLOSED"
                ):
                    continue

                position = position_event.get(
                    "position"
                )

                if not isinstance(position, dict):
                    continue

                try:
                    realized_pnl += float(
                        position.get(
                            "realized_pnl",
                            0.0,
                        )
                    )
                except (TypeError, ValueError):
                    continue

    except OSError:
        return PAPER_CAPITAL_USD

    return PAPER_CAPITAL_USD + realized_pnl


def find_entry_ai_filter(
    position: dict[str, Any],
) -> dict[str, Any]:
    """
    РќР°С…РѕРґРёС‚ AI Filter, РєРѕС‚РѕСЂС‹Р№ РѕС†РµРЅРёРІР°Р» РїРѕР·РёС†РёСЋ РїСЂРё РІС…РѕРґРµ.
    """
    opened_at_utc = position.get(
        "opened_at_utc"
    )

    if not opened_at_utc:
        return {}

    if not JOURNAL_FILE.exists():
        return {}

    try:
        lines = JOURNAL_FILE.read_text(
            encoding="utf-8-sig"
        ).splitlines()

    except OSError:
        return {}

    for line in reversed(lines):
        line = line.strip()

        if not line:
            continue

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(record, dict):
            continue

        position_event = record.get(
            "position_event"
        )

        if not isinstance(
            position_event,
            dict,
        ):
            continue

        if (
            position_event.get("event")
            != "POSITION_OPENED"
        ):
            continue

        recorded_position = (
            position_event.get("position")
        )

        if not isinstance(
            recorded_position,
            dict,
        ):
            continue

        if (
            recorded_position.get(
                "opened_at_utc"
            )
            != opened_at_utc
        ):
            continue

        pipeline = record.get("pipeline")

        if not isinstance(pipeline, dict):
            return {}

        ai_filter = pipeline.get(
            "ai_filter_shadow"
        )

        if isinstance(ai_filter, dict):
            return ai_filter

        return {}

    return {}


def send_event_notification(
    record: dict[str, Any],
) -> bool:
    """
    РћС‚РїСЂР°РІР»СЏРµС‚ Telegram С‚РѕР»СЊРєРѕ РґР»СЏ Р·РЅР°С‡РёРјС‹С… СЃРѕР±С‹С‚РёР№.

    Р›СЋР±Р°СЏ РѕС€РёР±РєР° Telegram Р±РµР·РѕРїР°СЃРЅРѕ РёРіРЅРѕСЂРёСЂСѓРµС‚СЃСЏ.
    """
    try:
        position_event = record.get(
            "position_event",
            {},
        )

        if not isinstance(
            position_event,
            dict,
        ):
            return False

        event = str(
            position_event.get(
                "event",
                "",
            )
        ).upper()

        position = position_event.get(
            "position"
        )

        if not isinstance(position, dict):
            return False

        pipeline = record.get("pipeline")

        if not isinstance(pipeline, dict):
            pipeline = {}

        ai_filter = pipeline.get(
            "ai_filter_shadow"
        )

        if not isinstance(ai_filter, dict):
            ai_filter = {}

        if event == "POSITION_OPENED":
            return (
                TELEGRAM_NOTIFIER
                .notify_position_opened(
                    position=position,
                    ai_filter=ai_filter,
                )
            )

        if event == "TAKE_PROFIT_1_REACHED":
            return (
                TELEGRAM_NOTIFIER
                .notify_tp1(
                    position=position,
                )
            )

        if event == "POSITION_CLOSED":
            entry_ai_filter = (
                find_entry_ai_filter(
                    position
                )
            )

            return (
                TELEGRAM_NOTIFIER
                .notify_position_closed(
                    position=position,
                    closed_balance=(
                        calculate_closed_balance()
                    ),
                    ai_filter=entry_ai_filter,
                )
            )

        return False

    except Exception:
        return False


def load_runtime_state() -> dict[str, Any]:
    if not RUNTIME_STATE_FILE.exists():
        return {
            "last_processed_close_time_ms": None,
        }

    try:
        with RUNTIME_STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            state = json.load(file)

    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Paper runtime state contains invalid JSON"
        ) from error

    except OSError as error:
        raise RuntimeError(
            "Unable to read paper runtime state"
        ) from error

    if not isinstance(state, dict):
        raise TypeError(
            "Paper runtime state must be a dictionary"
        )

    close_time_ms = state.get(
        "last_processed_close_time_ms"
    )

    if close_time_ms is not None:
        if (
            isinstance(close_time_ms, bool)
            or not isinstance(close_time_ms, int)
        ):
            raise TypeError(
                "Last processed close time must be int"
            )

    return state


def save_runtime_state(
    close_time_ms: int,
) -> None:
    DATA_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    state = {
        "last_processed_close_time_ms": (
            close_time_ms
        ),
        "updated_at_utc": utc_now(),
        "real_order_sent": False,
    }

    temporary_file = (
        RUNTIME_STATE_FILE.with_suffix(".tmp")
    )

    try:
        with temporary_file.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                state,
                file,
                indent=2,
                ensure_ascii=False,
            )

        temporary_file.replace(
            RUNTIME_STATE_FILE
        )

    except OSError as error:
        raise RuntimeError(
            "Unable to save paper runtime state"
        ) from error


def extract_market_snapshot(
    context: Any,
) -> dict[str, Any]:
    market = context.market

    if not isinstance(market, dict):
        raise TypeError(
            "Market data must be a dictionary"
        )

    close_time_ms = market.get(
        "last_close_time_ms"
    )

    if (
        isinstance(close_time_ms, bool)
        or not isinstance(close_time_ms, int)
    ):
        raise ValueError(
            "Market close time must be int"
        )

    highs = market.get("highs")
    lows = market.get("lows")

    if (
        not isinstance(highs, list)
        or not highs
    ):
        raise ValueError(
            "Market highs must be a non-empty list"
        )

    if (
        not isinstance(lows, list)
        or not lows
    ):
        raise ValueError(
            "Market lows must be a non-empty list"
        )

    price = float(market["price"])
    candle_high = float(highs[-1])
    candle_low = float(lows[-1])

    if not (
        candle_low
        <= price
        <= candle_high
    ):
        raise ValueError(
            "Market price must be between "
            "the candle low and high"
        )

    return {
        "close_time_ms": close_time_ms,
        "price": price,
        "candle_high": candle_high,
        "candle_low": candle_low,
    }


def build_pipeline_data(
    result: Any,
) -> dict[str, Any]:
    return {
        "market": result.market,
        "indicators": result.indicators,
        "regime": result.regime,
        "strategy": result.strategy,
        "risk": result.risk,
        "trade_plan": result.execution.get(
            "trade_plan"
        ),
        "decision": result.decision,
        "paper_order": result.execution.get(
            "paper_order"
        ),
    }


def build_journal_record(
    context: Any,
    snapshot: dict[str, Any],
    position_event: dict[str, Any],
    pipeline_data: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "recorded_at_utc": utc_now(),
        "exchange": context.exchange,
        "symbol": context.symbol,
        "timeframe": context.timeframe,
        "market_source": context.market.get(
            "source"
        ),
        "last_close_time_ms": snapshot[
            "close_time_ms"
        ],
        "market_price": snapshot["price"],
        "candle_high": snapshot[
            "candle_high"
        ],
        "candle_low": snapshot[
            "candle_low"
        ],
        "pipeline": pipeline_data,
        "position_event": position_event,
        "position": position_event.get(
            "position"
        ),
        "real_order_sent": False,
    }


def print_result(
    record: dict[str, Any],
) -> None:
    pipeline = record.get("pipeline")
    position_event = record["position_event"]
    position = position_event.get("position")

    print("-" * 70)
    print(
        "UTC:",
        record["recorded_at_utc"],
    )
    print(
        "SYMBOL:",
        record["symbol"],
    )
    print(
        "PRICE:",
        record["market_price"],
    )
    print(
        "CANDLE HIGH:",
        record["candle_high"],
    )
    print(
        "CANDLE LOW:",
        record["candle_low"],
    )

    if isinstance(pipeline, dict):
        decision = pipeline.get(
            "decision",
            {},
        ).get(
            "decision",
            "UNKNOWN",
        )

        paper_status = pipeline.get(
            "paper_order",
            {},
        ).get(
            "status",
            "UNKNOWN",
        )

        print(
            "PIPELINE DECISION:",
            decision,
        )
        print(
            "PIPELINE PAPER RESULT:",
            paper_status,
        )

        ai_filter_shadow = pipeline.get(
            "ai_filter_shadow",
            {},
        )

        if isinstance(ai_filter_shadow, dict):
            print(
                "AI FILTER DECISION:",
                ai_filter_shadow.get(
                    "decision",
                    "NOT_AVAILABLE",
                ),
            )
            print(
                "AI FILTER SCORE:",
                ai_filter_shadow.get("score"),
            )
            print(
                "AI FILTER STATUS:",
                ai_filter_shadow.get(
                    "adapter_status",
                    "UNKNOWN",
                ),
            )

        opportunity = pipeline.get(
            "ai_opportunity_review",
            {},
        )
        if isinstance(opportunity, dict):
            print(
                "AI OPPORTUNITY SCORE:",
                opportunity.get("score"),
            )
            print(
                "MARKET REGIME:",
                opportunity.get("market_regime"),
            )
            print(
                "ATR PERCENT:",
                opportunity.get("atr_percent"),
            )
            print(
                "RELATIVE VOLUME:",
                opportunity.get("relative_volume"),
            )
    else:
        print(
            "PIPELINE:",
            "NOT RUN - OPEN POSITION MANAGED",
        )

    print(
        "POSITION EVENT:",
        position_event.get("event"),
    )

    if isinstance(position, dict):
        print(
            "POSITION STATUS:",
            position.get("status"),
        )
        print(
            "ENTRY:",
            position.get("entry"),
        )
        print(
            "STOP:",
            position.get("stop"),
        )
        print(
            "TAKE PROFIT 1:",
            position.get("take_profit_1"),
        )
        print(
            "TAKE PROFIT 2:",
            position.get("take_profit_2"),
        )
        print(
            "TP1 REACHED:",
            position.get("tp1_reached"),
        )
        print(
            "UNREALIZED PNL:",
            position.get("unrealized_pnl"),
        )

        if position.get("status") == "CLOSED":
            print(
                "EXIT REASON:",
                position.get("exit_reason"),
            )
            print(
                "EXIT PRICE:",
                position.get("exit_price"),
            )
            print(
                "REALIZED PNL:",
                position.get("realized_pnl"),
            )

    print("REAL ORDER SENT: False")
    print("-" * 70)

def process_closed_candle(
    engine: Any,
    position_manager: PaperPositionManager,
    context: Any,
    snapshot: dict[str, Any],
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
]:
    position_was_open = (
        position_manager.has_open_position()
    )

    if position_was_open:
        position_event = (
            position_manager.evaluate_position(
                market_price=snapshot["price"],
                candle_high=snapshot[
                    "candle_high"
                ],
                candle_low=snapshot[
                    "candle_low"
                ],
                observed_at_utc=utc_now(),
            )
        )

        # На свече закрытия позиции новый вход
        # не создаётся. Это защищает от повторного
        # входа внутри одной и той же свечи.
        return position_event, None

    result = engine.execute(context)
    pipeline_data = build_pipeline_data(result)

    unified_market_context = (
        build_unified_market_context(
            context=result,
            snapshot=snapshot,
        )
    )
    pipeline_data[
        "unified_market_context"
    ] = unified_market_context

    opportunity_review = review_closed_candle(
        pipeline_data=pipeline_data,
        snapshot=snapshot,
        symbol=str(getattr(context, "symbol", "BTCUSDT")),
        recorded_at_utc=utc_now(),
    )

    pipeline_data["ai_opportunity_review"] = (
        opportunity_review
    )

    paper_order = result.execution.get(
        "paper_order",
        {},
    )

    if not isinstance(paper_order, dict):
        paper_order = {}

    order_side = str(
        paper_order.get("side", "")
    ).upper()

    candidate_ready = (
        paper_order.get("mode") == "PAPER"
        and paper_order.get("status")
        == "FILLED_SIMULATED"
        and paper_order.get(
            "real_order_sent"
        )
        is False
        and order_side in {"BUY", "LONG"}
        and paper_order.get("entry") is not None
        and paper_order.get("stop") is not None
    )

    if not candidate_ready:
        pipeline_data["leverage_risk"] = {
            "decision": "SKIPPED",
            "allowed": False,
            "reason": "NO_TRADE_CANDIDATE",
            "leverage": 0,
            "position_size": 0.0,
            "position_notional": 0.0,
            "margin_required": 0.0,
            "risk_amount": 0.0,
            "actual_risk_amount": 0.0,
            "real_order_sent": False,
        }

        pipeline_data["ai_filter_shadow"] = {
            "decision": "SKIPPED",
            "score": 0.0,
            "status": "SKIPPED",
            "reason": "NO_TRADE_CANDIDATE",
            "real_order_sent": False,
        }

        position_event = {
            "event": "NO_POSITION_OPENED",
            "reason": (
                paper_order.get("reason")
                or "NO_TRADE_CANDIDATE"
            ),
            "position": (
                position_manager.get_position()
            ),
            "real_order_sent": False,
        }

        return position_event, pipeline_data

    signal_data = pipeline_data.get(
        "signal",
        {},
    )
    market_data = pipeline_data.get(
        "market",
        {},
    )
    decision_data = pipeline_data.get(
        "decision",
        {},
    )

    if not isinstance(signal_data, dict):
        signal_data = {}

    if not isinstance(market_data, dict):
        market_data = {}

    if not isinstance(decision_data, dict):
        decision_data = {}

    signal_quality = signal_data.get(
        "quality",
        signal_data.get(
            "confidence",
            decision_data.get(
                "confidence",
                0.60,
            ),
        ),
    )

    volatility = market_data.get(
        "volatility_score",
        market_data.get(
            "volatility",
            0.90,
        ),
    )

    liquidity = market_data.get(
        "liquidity_score",
        market_data.get(
            "liquidity",
            0.50,
        ),
    )

    market_regime = pipeline_data.get(
        "market_regime",
        market_data.get(
            "regime",
            "UNKNOWN",
        ),
    )

    try:
        order_risk_amount = float(
            paper_order.get(
                "risk_amount",
                1.0,
            )
        )
    except (TypeError, ValueError):
        order_risk_amount = 1.0

    order_risk_percent = min(
        order_risk_amount
        / PAPER_CAPITAL_USD,
        LeverageRiskEngine.MAX_RISK_PERCENT,
    )

    leverage_decision = (
        LeverageRiskEngine.evaluate(
            capital=PAPER_CAPITAL_USD,
            entry_price=paper_order["entry"],
            stop_price=paper_order["stop"],
            side="LONG",
            signal_quality=signal_quality,
            volatility=volatility,
            liquidity=liquidity,
            market_regime=market_regime,
            risk_percent=order_risk_percent,
            data_ok=True,
            system_ok=True,
        )
    )

    pipeline_data["leverage_risk"] = (
        leverage_decision
    )

    if leverage_decision.get(
        "allowed"
    ) is not True:
        paper_order["status"] = (
            "REJECTED_BY_LEVERAGE_ENGINE"
        )
        paper_order["reason"] = (
            leverage_decision.get(
                "reason",
                "LEVERAGE_ENGINE_REJECTED",
            )
        )
        paper_order["quantity"] = 0.0
        paper_order["risk_amount"] = 0.0
        paper_order["leverage"] = 0

        pipeline_data[
            "ai_filter_shadow"
        ] = {
            "decision": "SKIPPED",
            "score": 0.0,
            "status": "SKIPPED",
            "reason": (
                "LEVERAGE_ENGINE_REJECTED"
            ),
            "real_order_sent": False,
        }

        position_event = {
            "event": "NO_POSITION_OPENED",
            "reason": paper_order["reason"],
            "position": (
                position_manager.get_position()
            ),
            "real_order_sent": False,
        }

        return position_event, pipeline_data

    paper_order["quantity"] = (
        leverage_decision["position_size"]
    )
    paper_order["risk_amount"] = (
        leverage_decision[
            "actual_risk_amount"
        ]
    )
    paper_order["leverage"] = (
        leverage_decision["leverage"]
    )
    paper_order["position_notional"] = (
        leverage_decision[
            "position_notional"
        ]
    )
    paper_order["margin_required"] = (
        leverage_decision[
            "margin_required"
        ]
    )
    paper_order["leverage_reason"] = (
        leverage_decision["reason"]
    )

    # AI-фильтр работает только для
    # настоящего кандидата и только в SHADOW MODE.
    # Он не может открыть сделку или снять
    # ограничения безопасности.
    ai_filter_shadow = run_shadow_filter(
        pipeline_data=pipeline_data,
        context=context,
        snapshot=snapshot,
    )

    pipeline_data["ai_filter_shadow"] = (
        ai_filter_shadow
    )

    position_event = (
        position_manager.open_position(
            paper_order=paper_order,
            opened_at_utc=utc_now(),
        )
    )

    return position_event, pipeline_data

def main() -> None:
    print("=" * 70)
    print(
        "TRADING CORE - LIVE PAPER POSITION MODE"
    )
    print("PUBLIC MARKET DATA ONLY")
    print("API KEY NOT USED")
    print("REAL ORDERS DISABLED")
    print(
        f"POLL INTERVAL: "
        f"{POLL_INTERVAL_SECONDS} seconds"
    )
    print(
        f"JOURNAL: {JOURNAL_FILE}"
    )
    print(
        "POSITION STATE: "
        "/app/data/paper_position.json"
    )
    print("PRESS CTRL+C TO STOP SAFELY")
    print("=" * 70)

    engine = Bootstrap.build()

    position_manager = PaperPositionManager()

    runtime_state = load_runtime_state()

    last_processed_close_time_ms = (
        runtime_state.get(
            "last_processed_close_time_ms"
        )
    )

    # ???? ????????: ????????? ?????? heartbeat
    # ????? ?????? ??????? ???????????? ?????.
    last_heartbeat_monotonic = 0.0
    last_daily_report_date_utc: str | None = None

    try:
        while True:
            try:
                context = build_live_context()

                snapshot = extract_market_snapshot(
                    context
                )

                current_close_time_ms = snapshot[
                    "close_time_ms"
                ]

                if (
                    current_close_time_ms
                    == last_processed_close_time_ms
                ):
                    time.sleep(
                        POLL_INTERVAL_SECONDS
                    )
                    continue

                (
                    position_event,
                    pipeline_data,
                ) = process_closed_candle(
                    engine=engine,
                    position_manager=position_manager,
                    context=context,
                    snapshot=snapshot,
                )

                record = build_journal_record(
                    context=context,
                    snapshot=snapshot,
                    position_event=position_event,
                    pipeline_data=pipeline_data,
                )

                append_journal(record)
                print_result(record)

                telegram_sent = (
                    send_event_notification(
                        record
                    )
                )

                if (
                    record["position_event"].get(
                        "event"
                    )
                    in {
                        "POSITION_OPENED",
                        "TAKE_PROFIT_1_REACHED",
                        "POSITION_CLOSED",
                    }
                ):
                    print(
                        "TELEGRAM EVENT SENT:",
                        telegram_sent,
                    )

                heartbeat_now = time.monotonic()

                if (
                    heartbeat_now
                    - last_heartbeat_monotonic
                    >= HEARTBEAT_INTERVAL_SECONDS
                ):
                    latest_event = str(
                        record.get(
                            "position_event",
                            {},
                        ).get(
                            "event",
                            "UNKNOWN",
                        )
                    )

                    heartbeat_sent = (
                        TELEGRAM_NOTIFIER.notify_health(
                            latest_event=latest_event,
                            latest_price=snapshot.get(
                                "market_price",
                                snapshot.get(
                                    "price",
                                    snapshot.get(
                                        "last_price",
                                        snapshot.get(
                                            "close",
                                            "UNKNOWN",
                                        ),
                                    ),
                                ),
                            ),
                        )
                    )

                    print(
                        "TELEGRAM HEARTBEAT SENT:",
                        heartbeat_sent,
                    )

                    if heartbeat_sent:
                        last_heartbeat_monotonic = (
                            heartbeat_now
                        )

                current_date_utc = (
                    datetime.now(timezone.utc)
                    .date()
                    .isoformat()
                )

                if (
                    current_date_utc
                    != last_daily_report_date_utc
                ):
                    daily_report_text = (
                        build_daily_opportunity_summary(
                            current_date_utc
                        )
                    )

                    daily_report_sent = (
                        TELEGRAM_NOTIFIER.send(
                            daily_report_text
                        )
                    )

                    print(
                        "TELEGRAM DAILY AI REPORT SENT:",
                        daily_report_sent,
                    )

                    if daily_report_sent:
                        last_daily_report_date_utc = (
                            current_date_utc
                        )

                save_runtime_state(
                    current_close_time_ms
                )

                last_processed_close_time_ms = (
                    current_close_time_ms
                )

            except Exception as error:
                error_record = {
                    "recorded_at_utc": utc_now(),
                    "status": "FAILED_SAFELY",
                    "error_type": (
                        type(error).__name__
                    ),
                    "error": str(error),
                    "real_order_sent": False,
                }

                append_journal(error_record)

                telegram_error_sent = (
                    TELEGRAM_NOTIFIER.notify_error(
                        error_type=(
                            error_record[
                                "error_type"
                            ]
                        ),
                        error_message=(
                            error_record["error"]
                        ),
                    )
                )

                print(
                    "TELEGRAM ERROR SENT:",
                    telegram_error_sent,
                )

                print("-" * 70)
                print("RUN FAILED SAFELY")
                print(
                    "ERROR TYPE:",
                    error_record["error_type"],
                )
                print(
                    "ERROR:",
                    error_record["error"],
                )
                print("NO REAL ORDER WAS SENT")
                print("-" * 70)

            time.sleep(
                POLL_INTERVAL_SECONDS
            )

    except KeyboardInterrupt:
        print()
        print("=" * 70)
        print("PAPER MONITOR STOPPED SAFELY")
        print("NO REAL ORDER WAS SENT")
        print("=" * 70)


if __name__ == "__main__":
    main()