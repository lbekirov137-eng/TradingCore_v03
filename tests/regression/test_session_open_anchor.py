"""
CRITICAL confirmed finding: SessionOpen.find_first_candle для сессии
CRYPTO раньше безусловно возвращал индекс 0 — то есть Opening Range
строился из первых свечей ЛЮБОГО загруженного окна (например, самых
старых из последних 300 свечей скользящего запроса), а не из начала
реальных суток. Так как DataEngine каждый раз запрашивает скользящееся
окно "последние N свечей", это делало Opening Range не привязанным ни к
какой фактической границе сессии — вопреки ORB_BASELINE.md.

Тест воспроизводит именно этот сценарий: свечи начинаются ДО полуночи
UTC, поэтому "индекс 0" и "первая свеча после полуночи UTC" — это разные
индексы. Правильное поведение обязано выбрать вторую.
"""

from api.contracts.context import LiveContext
from api.strategy_engine.strategies.orb.opening_range import OpeningRange

from tests.conftest import make_snapshot, QUIET_UTC_MIDNIGHT_MS, FIVE_MIN_MS


def test_crypto_opening_range_anchors_to_utc_midnight_not_array_start():

    # 3 свечи ДО полуночи (предыдущие сутки), затем >=5 свечей ПОСЛЕ полуночи.
    pre_midnight_start = QUIET_UTC_MIDNIGHT_MS - 3 * FIVE_MIN_MS

    closes = [90.0, 91.0, 92.0] + [100.0, 100.0, 100.0, 100.0, 100.0]
    highs = [90.2, 91.2, 92.2] + [100.2, 100.2, 100.2, 100.2, 100.2]
    lows = [89.8, 90.8, 91.8] + [99.8, 99.8, 99.8, 99.8, 99.8]

    snapshot = make_snapshot(
        closes, highs=highs, lows=lows, start_ms=pre_midnight_start,
    )

    ctx = LiveContext(exchange="binance", symbol="BTCUSDT", interval="5m", limit=300)
    ctx.market = snapshot

    opening_range = OpeningRange.calculate(ctx)

    assert opening_range is not None
    # Диапазон должен строиться из свечей ПОСЛЕ полуночи (high/low = 100.2/99.8),
    # а не из первых трёх свечей окна (high/low = 92.2/89.8 - предыдущие сутки).
    assert opening_range["start_index"] == 3
    assert opening_range["high"] == 100.2
    assert opening_range["low"] == 99.8
