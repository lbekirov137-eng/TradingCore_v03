"""
Исследовательский конвейер поверх BacktestEngine: минимальный объём
выборки, сегментация по рыночному режиму, сравнение с бенчмарками,
анализ устойчивости параметров и Monte Carlo по порядку сделок.

Ничего здесь не оптимизирует параметры на максимум прибыли — это
инструменты ПРОВЕРКИ, а не подгонки (см. явное требование не
"манипулировать порогами ради красивого бэктеста").
"""

import random
import statistics
from dataclasses import replace

from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig
from api.backtesting.backtest_context import BacktestContext
from api.strategy_engine.filters.regime import classify_regime, Regime
from api.ema import EMAEngine
from api.atr import ATREngine
from api.market_structure import MarketStructure


def min_sample_size_check(summary: dict, min_trades: int = 30) -> dict:
    """
    Флагует результат как статистически недостаточный, если сделок
    меньше min_trades. Не блокирует расчёт метрик — только явно
    помечает их как ненадёжные для вывода о прибыльности.
    """

    trades = summary.get("total_trades", 0)

    return {
        "trades": trades,
        "min_required": min_trades,
        "sufficient": trades >= min_trades,
        "note": (
            f"{trades} сделок недостаточно для статистически значимого вывода "
            f"(минимум {min_trades})." if trades < min_trades else
            f"{trades} сделок — минимальный порог выборки пройден."
        ),
    }


def benchmark_buy_and_hold(market, fee_rate: float = 0.001) -> dict:
    """Простой бенчмарк: купить на первой свече, держать до последней."""

    if len(market.closes) < 2:
        return {"net_pnl": 0.0, "return_percent": 0.0, "note": "Недостаточно данных."}

    entry = market.opens[0]
    exit_price = market.closes[-1]

    qty = 1000.0 / entry  # notional identical to strategy's default starting balance
    gross = (exit_price - entry) * qty
    fees = (entry + exit_price) * qty * fee_rate
    net = gross - fees

    return {
        "net_pnl": round(net, 8),
        "return_percent": round(net / 1000.0 * 100, 4),
        "entry": entry,
        "exit": exit_price,
        "note": "Buy-and-hold от первой до последней свечи периода, с комиссией на вход и выход.",
    }


def benchmark_no_trade() -> dict:
    """Тривиальный бенчмарк: не делать ничего. net_pnl всегда 0."""
    return {"net_pnl": 0.0, "return_percent": 0.0, "note": "NO_TRADE — эталон нулевого риска и нулевого результата."}


def regime_segmentation(strategy, market, config: BacktestConfig = None) -> dict:
    """
    Запускает бэктест один раз, затем классифицирует режим рынка в точке
    ВХОДА каждой сделки и группирует net_pnl по режиму. Позволяет увидеть,
    работает ли стратегия только в одном режиме (типичный overfitting
    к конкретному отрезку истории).
    """

    config = config or BacktestConfig()
    engine = BacktestEngine(strategy, config)
    report = engine.run(market)

    buckets = {}

    for trade in report["trades"]:
        if trade["result"] not in ("WIN", "LOSS", "BREAKEVEN"):
            continue

        index = trade["entry_index"]
        regime = _classify_regime_at(market, index)

        bucket = buckets.setdefault(regime, {"trades": 0, "net_pnl": 0.0, "wins": 0})
        bucket["trades"] += 1
        bucket["net_pnl"] += trade["net_pnl"]
        if trade["result"] == "WIN":
            bucket["wins"] += 1

    for regime, bucket in buckets.items():
        bucket["net_pnl"] = round(bucket["net_pnl"], 8)
        bucket["win_rate_percent"] = round(bucket["wins"] / bucket["trades"] * 100, 2) if bucket["trades"] else 0.0

    return {"by_regime": buckets, "overall_summary": report["summary"]}


def _classify_regime_at(market, index: int) -> str:

    context = BacktestContext(index=index, market=market, indicators={}, balance=1000.0)
    visible = context.visible_market

    closes = list(visible.closes)
    highs = list(visible.highs)
    lows = list(visible.lows)

    if len(closes) < 20:
        return Regime.UNDETERMINED

    context.indicators["ema"] = EMAEngine.calculate_all(closes)
    context.indicators["atr"] = ATREngine.calculate(highs, lows, closes)

    return classify_regime(context)


def parameter_stability_analysis(strategy_factory, param_variants: list, market, config: BacktestConfig = None) -> dict:
    """
    strategy_factory(params) -> strategy instance/class implementing .generate(context)
    param_variants: список словарей с небольшими вариациями параметров.

    Запускает КАЖДЫЙ вариант отдельно и отдельно репортит результат —
    НЕ выбирает "лучший" вариант и не комбинирует их в одну стратегию.
    Цель — увидеть, устойчив ли знак/порядок результата к небольшим
    изменениям параметров, а не найти оптимум.
    """

    config = config or BacktestConfig()
    results = []

    for params in param_variants:
        strategy = strategy_factory(params)
        report = BacktestEngine(strategy, config).run(market)
        results.append({"params": params, "summary": report["summary"]})

    net_pnls = [r["summary"]["net_pnl"] for r in results if r["summary"]["total_trades"] > 0]

    if len(net_pnls) < 2:
        stability_verdict = "INSUFFICIENT_DATA"
    else:
        signs = {1 if pnl > 0 else (-1 if pnl < 0 else 0) for pnl in net_pnls}
        stability_verdict = "STABLE_SIGN" if len(signs) == 1 else "SIGN_FLIPS_WITH_SMALL_CHANGES"

    return {
        "variants": results,
        "verdict": stability_verdict,
        "note": (
            "SIGN_FLIPS_WITH_SMALL_CHANGES означает, что profit/loss меняет знак "
            "при малых изменениях параметров — явный признак переобучения "
            "на конкретном отрезке истории, а не реального edge."
        ),
    }


def monte_carlo_trade_order(trades: list, iterations: int = 1000, seed: int = 42,
                             initial_balance: float = 1000.0) -> dict:
    """
    Перемешивает ПОРЯДОК уже случившихся net_pnl (не сами исходы), чтобы
    оценить, насколько путь к финальному результату (просадка, риск
    разорения) зависит от конкретной последовательности сделок, которая
    сама по себе могла быть удачным/неудачным совпадением.
    """

    net_pnls = [t["net_pnl"] for t in trades if t.get("result") in ("WIN", "LOSS", "BREAKEVEN")]

    if len(net_pnls) < 2:
        return {"verdict": "INSUFFICIENT_DATA", "iterations": 0}

    rng = random.Random(seed)

    max_drawdowns = []
    final_balances = []
    ruin_count = 0

    for _ in range(iterations):
        shuffled = net_pnls[:]
        rng.shuffle(shuffled)

        balance = initial_balance
        peak = balance
        max_dd = 0.0

        for pnl in shuffled:
            balance += pnl
            peak = max(peak, balance)
            max_dd = max(max_dd, peak - balance)

            if balance <= 0:
                ruin_count += 1
                break

        max_drawdowns.append(max_dd)
        final_balances.append(balance)

    return {
        "iterations": iterations,
        "trades_per_iteration": len(net_pnls),
        "median_max_drawdown": round(statistics.median(max_drawdowns), 4),
        "p95_max_drawdown": round(sorted(max_drawdowns)[int(0.95 * len(max_drawdowns)) - 1], 4),
        "median_final_balance": round(statistics.median(final_balances), 4),
        "p5_final_balance": round(sorted(final_balances)[int(0.05 * len(final_balances))], 4),
        "ruin_probability_percent": round(ruin_count / iterations * 100, 2),
        "note": (
            "Порядок сделок перемешан; набор исходов (net_pnl) фиксирован. "
            "Показывает чувствительность просадки/риска разорения к порядку, "
            "а не к самим сделкам."
        ),
    }
