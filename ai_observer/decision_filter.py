from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# SECURITY MODE
# ============================================================
# Этот модуль работает только как наблюдатель.
# Он НЕ открывает, НЕ отменяет и НЕ блокирует сделки.
SHADOW_MODE = True
REAL_ORDERS_ENABLED = False


# ============================================================
# FILES AND THRESHOLDS
# ============================================================
DEFAULT_INPUT_FILES = [
    Path("/app/pipeline_last.json"),
    Path("/app/data/pipeline_last.json"),
    Path("pipeline_last.json"),
    Path("data/pipeline_last.json"),
]

DEFAULT_OUTPUT_RAILWAY = Path(
    "/app/data/ai_decision_filter.jsonl"
)
DEFAULT_OUTPUT_LOCAL = Path(
    "data/ai_decision_filter.jsonl"
)

MIN_APPROVE_SCORE = float(
    os.getenv("AI_FILTER_APPROVE_SCORE", "70")
)

MIN_REVIEW_SCORE = float(
    os.getenv("AI_FILTER_REVIEW_SCORE", "50")
)


@dataclass
class FilterResult:
    created_at_utc: str
    mode: str
    decision: str
    score: float
    confidence: str
    symbol: str
    side: str
    market_regime: str
    trend: str
    structure: str
    rsi: float | None
    atr: float | None
    risk_reward: float | None
    source_signal_score: float | None
    reasons: list[str]
    warnings: list[str]
    real_order_sent: bool
    source_file: str


# ============================================================
# BASIC HELPERS
# ============================================================
def safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
    try:
        if value is None:
            return default

        return float(value)

    except (TypeError, ValueError):
        return default


def safe_text(
    value: Any,
    default: str = "UNKNOWN",
) -> str:
    if value is None:
        return default

    if isinstance(value, (dict, list, tuple, set)):
        return default

    text = str(value).strip()

    return text if text else default


def get_nested(
    data: dict[str, Any],
    paths: list[list[str]],
) -> Any:
    for path in paths:
        current: Any = data
        found = True

        for key in path:
            if not isinstance(current, dict):
                found = False
                break

            if key not in current:
                found = False
                break

            current = current[key]

        if found and current is not None:
            return current

    return None


def recursive_find_first(
    data: Any,
    wanted_keys: set[str],
) -> Any:
    """
    Ищет первое скалярное значение по ключу во вложенном JSON.
    Словари и списки как итоговое значение не возвращаются.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                key.lower() in wanted_keys
                and not isinstance(value, (dict, list))
                and value is not None
            ):
                return value

        for value in data.values():
            result = recursive_find_first(
                value,
                wanted_keys,
            )

            if result is not None:
                return result

    elif isinstance(data, list):
        for item in data:
            result = recursive_find_first(
                item,
                wanted_keys,
            )

            if result is not None:
                return result

    return None


def normalize_side(value: Any) -> str:
    side = safe_text(value).upper()

    aliases = {
        "LONG": "BUY",
        "BULLISH": "BUY",
        "UP": "BUY",
        "SHORT": "SELL",
        "BEARISH": "SELL",
        "DOWN": "SELL",
    }

    return aliases.get(side, side)


# ============================================================
# FILE HANDLING
# ============================================================
def find_input_file() -> Path:
    custom_path = os.getenv("AI_FILTER_INPUT_FILE")

    if custom_path:
        path = Path(custom_path)

        if not path.exists():
            raise FileNotFoundError(
                f"AI filter input file not found: {path}"
            )

        return path

    for path in DEFAULT_INPUT_FILES:
        if path.exists():
            return path

    searched = ", ".join(
        str(path) for path in DEFAULT_INPUT_FILES
    )

    raise FileNotFoundError(
        "Не найден файл последнего анализа. "
        f"Проверены пути: {searched}"
    )


def resolve_output_file() -> Path:
    custom_path = os.getenv("AI_FILTER_OUTPUT_FILE")

    if custom_path:
        return Path(custom_path)

    if Path("/app/data").exists():
        return DEFAULT_OUTPUT_RAILWAY

    return DEFAULT_OUTPUT_LOCAL


def load_input(path: Path) -> dict[str, Any]:
    with path.open(
        "r",
        encoding="utf-8-sig",
    ) as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(
            "Входной JSON должен содержать объект."
        )

    return data


# ============================================================
# PIPELINE VALUE EXTRACTION
# ============================================================
def extract_symbol(
    data: dict[str, Any],
) -> str:
    value = get_nested(
        data,
        [
            ["symbol"],
            ["market", "symbol"],
            ["instrument"],
            ["trade_plan", "symbol"],
            ["context", "symbol"],
            ["context", "market", "symbol"],
            ["pipeline", "market", "symbol"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"symbol", "instrument"},
        )

    return safe_text(value, "BTCUSDT").upper()


def extract_side(
    data: dict[str, Any],
) -> str:
    # Сначала ищем конкретное поле signal/side,
    # а не весь объект decision.
    value = get_nested(
        data,
        [
            ["side"],
            ["signal"],
            ["decision", "signal"],
            ["decision", "side"],
            ["decision", "direction"],
            ["trade_plan", "side"],
            ["trade_plan", "signal"],
            ["strategy", "side"],
            ["strategy", "signal"],
            ["context", "decision", "signal"],
            ["context", "decision", "side"],
            ["context", "signal"],
            ["pipeline", "decision", "signal"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"signal", "side", "direction"},
        )

    return normalize_side(value)


def extract_market_regime(
    data: dict[str, Any],
) -> str:
    value = get_nested(
        data,
        [
            ["market_regime"],
            ["regime"],
            ["market", "regime"],
            ["context", "market_regime"],
            ["context", "market", "regime"],
            ["analysis", "market_regime"],
            ["analysis", "regime"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"market_regime", "regime"},
        )

    return safe_text(value).upper()


def extract_trend(
    data: dict[str, Any],
) -> str:
    value = get_nested(
        data,
        [
            ["trend"],
            ["indicators", "ema", "trend"],
            ["indicators", "trend"],
            ["market", "trend"],
            ["context", "indicators", "ema", "trend"],
            ["context", "indicators", "trend"],
            ["analysis", "trend"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"trend", "ema_trend"},
        )

    return safe_text(value).upper()


def extract_structure(
    data: dict[str, Any],
) -> str:
    value = get_nested(
        data,
        [
            ["structure"],
            ["market_structure"],
            ["indicators", "market_structure"],
            ["indicators", "structure"],
            ["context", "market_structure"],
            ["context", "indicators", "market_structure"],
            ["analysis", "structure"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"structure", "market_structure"},
        )

    return safe_text(value).upper()


def extract_rsi(
    data: dict[str, Any],
) -> float | None:
    value = get_nested(
        data,
        [
            ["rsi"],
            ["indicators", "rsi"],
            ["indicators", "rsi", "value"],
            ["context", "indicators", "rsi"],
            ["context", "indicators", "rsi", "value"],
            ["analysis", "rsi"],
        ],
    )

    if isinstance(value, dict):
        value = value.get("value")

    if value is None:
        value = recursive_find_first(
            data,
            {"rsi", "rsi_value"},
        )

    return safe_float(value)


def extract_atr(
    data: dict[str, Any],
) -> float | None:
    value = get_nested(
        data,
        [
            ["atr"],
            ["indicators", "atr"],
            ["indicators", "atr", "value"],
            ["context", "indicators", "atr"],
            ["context", "indicators", "atr", "value"],
            ["analysis", "atr"],
        ],
    )

    if isinstance(value, dict):
        value = value.get("value")

    if value is None:
        value = recursive_find_first(
            data,
            {"atr", "atr_value"},
        )

    return safe_float(value)


def extract_price(
    data: dict[str, Any],
) -> float | None:
    value = get_nested(
        data,
        [
            ["price"],
            ["market_price"],
            ["entry"],
            ["trade_plan", "entry"],
            ["trade_plan", "entry_price"],
            ["market", "price"],
            ["market", "last_price"],
            ["context", "market", "price"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {
                "entry",
                "entry_price",
                "market_price",
                "last_price",
                "price",
            },
        )

    return safe_float(value)


def extract_stop_loss(
    data: dict[str, Any],
) -> float | None:
    value = get_nested(
        data,
        [
            ["stop_loss"],
            ["sl"],
            ["trade_plan", "stop_loss"],
            ["trade_plan", "sl"],
            ["risk", "stop_loss"],
            ["risk", "sl"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"stop_loss", "sl", "stop"},
        )

    return safe_float(value)


def extract_take_profit(
    data: dict[str, Any],
) -> float | None:
    value = get_nested(
        data,
        [
            ["take_profit"],
            ["tp"],
            ["take_profit_2"],
            ["tp2"],
            ["trade_plan", "take_profit"],
            ["trade_plan", "take_profit_2"],
            ["trade_plan", "tp"],
            ["trade_plan", "tp2"],
            ["risk", "take_profit"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {
                "take_profit",
                "take_profit_2",
                "tp",
                "tp2",
            },
        )

    return safe_float(value)


def extract_signal_quality(
    data: dict[str, Any],
) -> float | None:
    value = get_nested(
        data,
        [
            ["signal_quality"],
            ["quality_score"],
            ["confidence_score"],
            ["decision", "score"],
            ["strategy", "score"],
            ["analysis", "score"],
            ["context", "decision", "score"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {
                "signal_quality",
                "quality_score",
                "confidence_score",
                "score",
            },
        )

    score = safe_float(value)

    if score is None:
        return None

    if 0 <= score <= 1:
        return score * 100

    return score


def extract_engine_decision(
    data: dict[str, Any],
) -> str:
    value = get_nested(
        data,
        [
            ["engine_decision"],
            ["decision", "engine_decision"],
            ["decision", "decision"],
            ["context", "decision", "engine_decision"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"engine_decision"},
        )

    return safe_text(value).upper()


def extract_risk_allowed(
    data: dict[str, Any],
) -> bool | None:
    value = get_nested(
        data,
        [
            ["risk_allowed"],
            ["decision", "risk_allowed"],
            ["risk", "allowed"],
            ["context", "decision", "risk_allowed"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"risk_allowed"},
        )

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {"true", "yes", "1"}:
            return True

        if normalized in {"false", "no", "0"}:
            return False

    return None


def extract_trade_plan_allowed(
    data: dict[str, Any],
) -> bool | None:
    value = get_nested(
        data,
        [
            ["trade_plan_allowed"],
            ["decision", "trade_plan_allowed"],
            ["trade_plan", "allowed"],
            ["context", "decision", "trade_plan_allowed"],
        ],
    )

    if value is None:
        value = recursive_find_first(
            data,
            {"trade_plan_allowed"},
        )

    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {"true", "yes", "1"}:
            return True

        if normalized in {"false", "no", "0"}:
            return False

    return None


# ============================================================
# SCORING
# ============================================================
def calculate_rr(
    side: str,
    entry: float | None,
    stop_loss: float | None,
    take_profit: float | None,
) -> float | None:
    if (
        entry is None
        or stop_loss is None
        or take_profit is None
    ):
        return None

    if side == "BUY":
        risk = entry - stop_loss
        reward = take_profit - entry

    elif side == "SELL":
        risk = stop_loss - entry
        reward = entry - take_profit

    else:
        return None

    if risk <= 0 or reward <= 0:
        return None

    return reward / risk


def evaluate_candidate(
    data: dict[str, Any],
    source_file: Path,
) -> FilterResult:
    score = 50.0
    reasons: list[str] = []
    warnings: list[str] = []

    symbol = extract_symbol(data)
    side = extract_side(data)
    regime = extract_market_regime(data)
    trend = extract_trend(data)
    structure = extract_structure(data)
    rsi = extract_rsi(data)
    atr = extract_atr(data)
    price = extract_price(data)
    stop_loss = extract_stop_loss(data)
    take_profit = extract_take_profit(data)
    signal_quality = extract_signal_quality(data)
    engine_decision = extract_engine_decision(data)
    risk_allowed = extract_risk_allowed(data)
    trade_plan_allowed = extract_trade_plan_allowed(data)

    if side not in {"BUY", "SELL"}:
        score -= 40
        warnings.append(
            "Нет корректного направления BUY или SELL."
        )
    else:
        reasons.append(
            f"Направление сигнала распознано: {side}."
        )

    if engine_decision == "TRADE":
        score += 8
        reasons.append(
            "Основное ядро разрешило торговый кандидат."
        )
    elif engine_decision in {
        "NO_TRADE",
        "REJECT",
        "WAIT",
    }:
        score -= 30
        warnings.append(
            f"Основное ядро выдало {engine_decision}."
        )

    if risk_allowed is True:
        score += 6
        reasons.append(
            "Risk Engine разрешил кандидат."
        )
    elif risk_allowed is False:
        score -= 35
        warnings.append(
            "Risk Engine запретил кандидат."
        )

    if trade_plan_allowed is True:
        score += 6
        reasons.append(
            "Trade Plan прошёл проверку."
        )
    elif trade_plan_allowed is False:
        score -= 25
        warnings.append(
            "Trade Plan не прошёл проверку."
        )

    if trend in {"BULLISH", "UP", "UPTREND"}:
        if side == "BUY":
            score += 12
            reasons.append(
                "BUY совпадает с восходящим трендом."
            )
        elif side == "SELL":
            score -= 15
            warnings.append(
                "SELL направлен против восходящего тренда."
            )

    elif trend in {
        "BEARISH",
        "DOWN",
        "DOWNTREND",
    }:
        if side == "SELL":
            score += 12
            reasons.append(
                "SELL совпадает с нисходящим трендом."
            )
        elif side == "BUY":
            score -= 15
            warnings.append(
                "BUY направлен против нисходящего тренда."
            )

    else:
        score -= 5
        warnings.append(
            "Тренд не определён или нейтрален."
        )

    bullish_structure = {
        "BULLISH",
        "HIGHER_HIGH",
        "HIGHER_LOW",
        "HH_HL",
        "UPTREND",
    }

    bearish_structure = {
        "BEARISH",
        "LOWER_HIGH",
        "LOWER_LOW",
        "LH_LL",
        "DOWNTREND",
    }

    if structure in bullish_structure:
        if side == "BUY":
            score += 10
            reasons.append(
                "Рыночная структура подтверждает BUY."
            )
        elif side == "SELL":
            score -= 10
            warnings.append(
                "Структура не подтверждает SELL."
            )

    elif structure in bearish_structure:
        if side == "SELL":
            score += 10
            reasons.append(
                "Рыночная структура подтверждает SELL."
            )
        elif side == "BUY":
            score -= 10
            warnings.append(
                "Структура не подтверждает BUY."
            )

    else:
        warnings.append(
            "Рыночная структура не подтверждена."
        )

    if rsi is not None:
        if side == "BUY":
            if 40 <= rsi <= 65:
                score += 8
                reasons.append(
                    f"RSI {rsi:.1f} подходит для BUY."
                )
            elif rsi >= 75:
                score -= 15
                warnings.append(
                    f"RSI {rsi:.1f}: рынок перекуплен."
                )
            elif rsi <= 25:
                score -= 5
                warnings.append(
                    f"RSI {rsi:.1f}: повышенная нестабильность."
                )

        elif side == "SELL":
            if 35 <= rsi <= 60:
                score += 8
                reasons.append(
                    f"RSI {rsi:.1f} подходит для SELL."
                )
            elif rsi <= 25:
                score -= 15
                warnings.append(
                    f"RSI {rsi:.1f}: рынок перепродан."
                )
            elif rsi >= 75:
                score -= 5
                warnings.append(
                    f"RSI {rsi:.1f}: повышенная нестабильность."
                )
    else:
        score -= 3
        warnings.append(
            "RSI отсутствует."
        )

    if regime in {
        "TREND",
        "TRENDING",
        "BULL_TREND",
        "BEAR_TREND",
    }:
        score += 8
        reasons.append(
            "Рыночный режим трендовый."
        )

    elif regime in {
        "RANGE",
        "SIDEWAYS",
        "CHOPPY",
        "UNCERTAIN",
    }:
        score -= 12
        warnings.append(
            f"Режим рынка {regime}: риск ложного входа."
        )

    elif regime == "UNKNOWN":
        score -= 5
        warnings.append(
            "Рыночный режим неизвестен."
        )

    rr = calculate_rr(
        side=side,
        entry=price,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )

    if rr is None:
        score -= 12
        warnings.append(
            "Невозможно проверить Risk/Reward."
        )

    elif rr >= 3:
        score += 15
        reasons.append(
            f"Risk/Reward хороший: 1:{rr:.2f}."
        )

    elif rr >= 2:
        score += 10
        reasons.append(
            f"Risk/Reward приемлемый: 1:{rr:.2f}."
        )

    else:
        score -= 20
        warnings.append(
            f"Risk/Reward слишком низкий: 1:{rr:.2f}."
        )

    if signal_quality is not None:
        if signal_quality >= 80:
            score += 10
            reasons.append(
                f"Высокое качество исходного сигнала: "
                f"{signal_quality:.1f}/100."
            )
        elif signal_quality >= 60:
            score += 4
            reasons.append(
                f"Среднее качество исходного сигнала: "
                f"{signal_quality:.1f}/100."
            )
        else:
            score -= 12
            warnings.append(
                f"Низкое качество исходного сигнала: "
                f"{signal_quality:.1f}/100."
            )
    else:
        warnings.append(
            "Исходная оценка качества сигнала отсутствует."
        )

    if atr is None or atr <= 0:
        score -= 5
        warnings.append(
            "ATR отсутствует или некорректен."
        )

    score = max(0.0, min(100.0, score))

    if score >= MIN_APPROVE_SCORE:
        decision = "APPROVE_FOR_PAPER"
        confidence = "HIGH"

    elif score >= MIN_REVIEW_SCORE:
        decision = "REVIEW"
        confidence = "MEDIUM"

    else:
        decision = "REJECT"
        confidence = "LOW"

    if not reasons:
        reasons.append(
            "Подтверждающих факторов недостаточно."
        )

    return FilterResult(
        created_at_utc=datetime.now(
            timezone.utc
        ).isoformat(),
        mode="SHADOW_MODE",
        decision=decision,
        score=round(score, 2),
        confidence=confidence,
        symbol=symbol,
        side=side,
        market_regime=regime,
        trend=trend,
        structure=structure,
        rsi=rsi,
        atr=atr,
        risk_reward=(
            round(rr, 3)
            if rr is not None
            else None
        ),
        source_signal_score=(
            round(signal_quality, 2)
            if signal_quality is not None
            else None
        ),
        reasons=reasons,
        warnings=warnings,
        real_order_sent=False,
        source_file=str(source_file),
    )


# ============================================================
# OUTPUT
# ============================================================
def save_result(
    output_path: Path,
    result: FilterResult,
) -> None:
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "a",
        encoding="utf-8",
    ) as file:
        file.write(
            json.dumps(
                asdict(result),
                ensure_ascii=False,
            )
            + "\n"
        )


def format_report(
    result: FilterResult,
) -> str:
    lines = [
        "=" * 70,
        "AI DECISION FILTER — SHADOW MODE",
        "",
        f"Инструмент: {result.symbol}",
        f"Направление: {result.side}",
        f"Режим рынка: {result.market_regime}",
        f"Тренд: {result.trend}",
        f"Структура: {result.structure}",
        f"RSI: {result.rsi}",
        f"ATR: {result.atr}",
        f"Risk/Reward: {result.risk_reward}",
        (
            "Исходная оценка сигнала: "
            f"{result.source_signal_score}"
        ),
        "",
        f"Оценка фильтра: {result.score:.1f}/100",
        f"Уверенность: {result.confidence}",
        f"Рекомендация: {result.decision}",
        "",
        "ПОДТВЕРЖДАЮЩИЕ ФАКТОРЫ:",
    ]

    for reason in result.reasons:
        lines.append(f"• {reason}")

    lines.append("")
    lines.append("ПРЕДУПРЕЖДЕНИЯ:")

    if result.warnings:
        for warning in result.warnings:
            lines.append(f"• {warning}")
    else:
        lines.append(
            "• Критических предупреждений нет."
        )

    lines.extend(
        [
            "",
            "ВАЖНО:",
            "Фильтр пока не влияет на торговое ядро.",
            "Он не открывает и не блокирует сделки.",
            "Результаты только записываются для сравнения.",
            "",
            "REAL ORDERS: DISABLED",
            "REAL ORDER SENT: False",
            "=" * 70,
        ]
    )

    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================
def main() -> int:
    if not SHADOW_MODE:
        print(
            "SECURITY ERROR: SHADOW_MODE must remain enabled."
        )
        return 1

    if REAL_ORDERS_ENABLED:
        print(
            "SECURITY ERROR: real orders must remain disabled."
        )
        return 1

    try:
        input_path = find_input_file()
        data = load_input(input_path)

        result = evaluate_candidate(
            data=data,
            source_file=input_path,
        )

        output_path = resolve_output_file()
        save_result(output_path, result)

    except Exception as exc:
        print(
            "AI DECISION FILTER ERROR: "
            f"{type(exc).__name__}: {exc}"
        )
        print("REAL ORDER SENT: False")
        return 1

    print(format_report(result))
    print(f"\nРезультат записан: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())