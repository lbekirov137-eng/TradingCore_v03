"""
Загрузка исторических свечей с пагинацией и сохранение в JSON.

Только публичные read-only эндпоинты. Ключи не используются.

Пример:
    python scripts/fetch_history.py --symbol BTCUSDT --interval 5m --candles 8000
"""

import argparse
import json
import os
import time

import requests

BINANCE_URL = "https://api.binance.com/api/v3/klines"

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}


def fetch(symbol: str, interval: str, total_candles: int) -> dict:

    step_ms = INTERVAL_MS[interval]
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - total_candles * step_ms

    timestamps, opens, highs, lows, closes, volumes = [], [], [], [], [], []

    cursor = start_ms

    while len(timestamps) < total_candles:

        response = requests.get(BINANCE_URL, params={
            "symbol": symbol, "interval": interval,
            "startTime": cursor, "limit": 1000,
        }, timeout=20)
        response.raise_for_status()

        batch = response.json()

        if not batch:
            break

        for candle in batch:
            open_time = int(candle[0])
            if timestamps and open_time <= timestamps[-1]:
                continue
            timestamps.append(open_time)
            opens.append(float(candle[1]))
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
            closes.append(float(candle[4]))
            volumes.append(float(candle[5]))

        cursor = timestamps[-1] + step_ms

        if cursor >= now_ms:
            break

        time.sleep(0.25)  # be polite to the public endpoint

    # Отбрасываем последнюю свечу, если она ещё не закрыта.
    if timestamps and timestamps[-1] + step_ms > now_ms:
        for series in (timestamps, opens, highs, lows, closes, volumes):
            series.pop()

    return {
        "symbol": symbol, "interval": interval,
        "timestamps": timestamps, "opens": opens, "highs": highs,
        "lows": lows, "closes": closes, "volumes": volumes,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--candles", type=int, default=5000)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    data = fetch(args.symbol, args.interval, args.candles)

    out = args.out or os.path.join("data", f"{args.symbol}_{args.interval}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f)

    print(f"saved {len(data['timestamps'])} candles -> {out}")


if __name__ == "__main__":
    main()
