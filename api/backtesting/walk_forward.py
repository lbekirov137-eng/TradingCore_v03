"""
Train/test split и walk-forward валидация + анализ устойчивости.

Назначение — НЕ найти "лучшие" параметры, а проверить, сохраняется ли
результат вне участка, на котором стратегия рассматривалась. Если
результат исчезает при небольшом изменении параметров или комиссий,
стратегия помечается FRAGILE и не допускается к paper-forward
(см. AUTOTRADING_RELEASE_GATES.md, Gate 3).
"""

from dataclasses import replace

from api.backtesting.backtest_engine import BacktestEngine, BacktestConfig


class MarketSlice:
    """Срез рыночных данных без копирования семантики MarketSnapshot."""

    def __init__(self, market, start: int, end: int):
        self.timestamps = market.timestamps[start:end]
        self.opens = market.opens[start:end]
        self.highs = market.highs[start:end]
        self.lows = market.lows[start:end]
        self.closes = market.closes[start:end]
        self.volumes = market.volumes[start:end]


def train_test_split(market, train_ratio: float = 0.6, validation_ratio: float = 0.2):
    """
    Делит данные на train / validation / out-of-sample test.
    Out-of-sample участок НЕ должен использоваться для выбора параметров.
    """

    total = len(market.timestamps)

    train_end = int(total * train_ratio)
    validation_end = int(total * (train_ratio + validation_ratio))

    return {
        "train": MarketSlice(market, 0, train_end),
        "validation": MarketSlice(market, train_end, validation_end),
        "test": MarketSlice(market, validation_end, total),
    }


def walk_forward(strategy, market, config: BacktestConfig = None,
                  window_size: int = None, step: int = None) -> dict:
    """
    Прогоняет стратегию по последовательным окнам, имитируя движение
    вперёд во времени. Каждое окно оценивается независимо.
    """

    config = config or BacktestConfig()
    total = len(market.timestamps)

    window_size = window_size or max(100, total // 4)
    step = step or max(1, window_size // 2)

    windows = []
    start = 0

    while start + window_size <= total:
        window_market = MarketSlice(market, start, start + window_size)
        engine = BacktestEngine(strategy, config)
        report = engine.run(window_market)

        windows.append({
            "start_index": start,
            "end_index": start + window_size,
            "summary": report["summary"],
        })

        start += step

    profitable_windows = [w for w in windows if w["summary"]["net_pnl"] > 0]
    traded_windows = [w for w in windows if w["summary"]["total_trades"] > 0]

    return {
        "windows": windows,
        "window_count": len(windows),
        "windows_with_trades": len(traded_windows),
        "profitable_windows": len(profitable_windows),
        "consistency_percent": round(
            (len(profitable_windows) / len(traded_windows) * 100) if traded_windows else 0.0, 2
        ),
    }


def sensitivity_analysis(strategy, market, base_config: BacktestConfig = None) -> dict:
    """
    Стресс-тест устойчивости: удвоенные/утроенные издержки и задержка
    входа. Если результат разваливается — стратегия FRAGILE.
    """

    base_config = base_config or BacktestConfig()

    scenarios = {
        "baseline": base_config,
        "fees_x2": replace(base_config, fee_rate=base_config.fee_rate * 2),
        "slippage_x2": replace(base_config, slippage_bps=base_config.slippage_bps * 2),
        "slippage_x3": replace(base_config, slippage_bps=base_config.slippage_bps * 3),
        "spread_x2": replace(base_config, spread_bps=base_config.spread_bps * 2),
        "latency_1_candle": replace(base_config, latency_candles=1),
        "all_costs_x2": replace(
            base_config,
            fee_rate=base_config.fee_rate * 2,
            slippage_bps=base_config.slippage_bps * 2,
            spread_bps=base_config.spread_bps * 2,
        ),
    }

    results = {}

    for name, config in scenarios.items():
        engine = BacktestEngine(strategy, config)
        report = engine.run(market)
        results[name] = report["summary"]

    baseline_pnl = results["baseline"]["net_pnl"]

    stressed = [v["net_pnl"] for k, v in results.items() if k != "baseline"]
    survived = all(pnl > 0 for pnl in stressed) if stressed else False

    verdict = "ROBUST" if (baseline_pnl > 0 and survived) else "FRAGILE"

    if results["baseline"]["total_trades"] == 0:
        verdict = "INSUFFICIENT_DATA"

    return {
        "scenarios": results,
        "verdict": verdict,
        "note": (
            "FRAGILE означает, что результат не переживает удвоение издержек "
            "или задержку входа на одну свечу. Такая стратегия не допускается "
            "к paper-forward (Gate 3)."
        ),
    }
