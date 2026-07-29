"""
Профили комиссий бирж. Только исследование — config не меняется.

ЗАЧЕМ. Cost lab показал, что VWAP выходит в безубыток при maker 0.05% и
в плюс при 0.02%. Прежде чем строить что-либо на этом, нужно ответить на
вопрос, который нельзя решить моделированием: КАКАЯ комиссия реально
доступна нашему аккаунту.

Ключевое правило модуля: рекламный минимальный тариф — НЕ достижимый
тариф. Каждый профиль несёт требования и уровень доверия, а профили,
требующие VIP-объёма, помечены как недостижимые по умолчанию. Считать
0.02% доступным «потому что биржа его публикует» — это ровно та ошибка,
из-за которой стратегия выглядит прибыльной на бумаге.

Ничего здесь не читает ключи, не ходит на биржу и не меняет .env.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SPOT = "SPOT"

# Уровень доверия к значению.
VERIFIED_BY_OWNER = "VERIFIED_BY_OWNER"   # владелец подтвердил из кабинета
PUBLISHED_BASE = "PUBLISHED_BASE"          # публичная базовая ставка
PUBLISHED_TIER = "PUBLISHED_TIER"          # публичный тариф с условиями
ASSUMED = "ASSUMED"                        # допущение модели


@dataclass(frozen=True)
class FeeProfile:
    """
    Неизменяемый профиль комиссий.

    achievable_now отражает, доступен ли тариф БЕЗ выполнения условий,
    которых у нас нет. Профиль VIP-уровня может быть корректным и при этом
    недостижимым — эти два свойства разделены намеренно.
    """

    exchange: str
    market_type: str
    maker_fee: float
    taker_fee: float

    confidence: str
    source_reference: str
    source_date: str

    achievable_now: bool
    requirements: str = "none"
    discount_source: str | None = None
    vip_tier: str = "base"
    effective_from: str | None = None
    verified_by_owner: bool = False
    notes: str = ""

    @property
    def round_trip_fee_rate(self) -> float:
        """Maker вход + taker выход — базовый сценарий исполнения."""
        return self.maker_fee + self.taker_fee

    @property
    def taker_round_trip(self) -> float:
        return self.taker_fee * 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "market_type": self.market_type,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
            "vip_tier": self.vip_tier,
            "achievable_now": self.achievable_now,
            "requirements": self.requirements,
            "discount_source": self.discount_source,
            "confidence": self.confidence,
            "source_reference": self.source_reference,
            "source_date": self.source_date,
            "verified_by_owner": self.verified_by_owner,
            "effective_from": self.effective_from,
            "notes": self.notes,
        }


SOURCE_DATE = "2026-07-29"

# --------------------------------------------------------------- профили
#
# Все значения — публичные спотовые тарифы, снятые 2026-07-29.
# Ни один не подтверждён кабинетом владельца, поэтому verified_by_owner
# везде False.

BINANCE_BASE = FeeProfile(
    exchange="binance", market_type=SPOT,
    maker_fee=0.0010, taker_fee=0.0010,
    vip_tier="VIP 0 (regular)",
    achievable_now=True, requirements="none",
    confidence=PUBLISHED_BASE,
    source_reference="binance.com/en/fee/schedule",
    source_date=SOURCE_DATE,
    notes="Base spot rate for a regular account.",
)

BINANCE_BNB = FeeProfile(
    exchange="binance", market_type=SPOT,
    maker_fee=0.00075, taker_fee=0.00075,
    vip_tier="VIP 0 + BNB discount",
    achievable_now=True,
    requirements="hold BNB and enable 'pay fees with BNB'",
    discount_source="BNB 25% fee discount",
    confidence=PUBLISHED_BASE,
    source_reference="binance.com/en/fee/schedule",
    source_date=SOURCE_DATE,
    notes="Requires holding BNB; introduces a separate asset exposure.",
)

BINANCE_VIP1 = FeeProfile(
    exchange="binance", market_type=SPOT,
    maker_fee=0.0009, taker_fee=0.0010,
    vip_tier="VIP 1",
    achievable_now=False,
    requirements="30-day volume >= $1,000,000 AND >= 5 BNB",
    confidence=PUBLISHED_TIER,
    source_reference="binance.com/en/fee/schedule",
    source_date=SOURCE_DATE,
    notes="NOT achievable at paper-scale volume.",
)

BINANCE_VIP3 = FeeProfile(
    exchange="binance", market_type=SPOT,
    maker_fee=0.0004, taker_fee=0.0006,
    vip_tier="VIP 3",
    achievable_now=False,
    requirements="30-day volume >= $20,000,000 AND >= 100 BNB",
    confidence=PUBLISHED_TIER,
    source_reference="binance.com/en/fee/schedule",
    source_date=SOURCE_DATE,
    notes="Reference only. Far beyond our volume.",
)

BYBIT_BASE = FeeProfile(
    exchange="bybit", market_type=SPOT,
    maker_fee=0.0010, taker_fee=0.0010,
    vip_tier="non-VIP",
    achievable_now=True, requirements="none",
    confidence=PUBLISHED_BASE,
    source_reference="bybit.com fee-rate announcement / help centre",
    source_date=SOURCE_DATE,
    notes="Base spot rate. Crypto-fiat pairs are charged differently.",
)

OKX_LV1 = FeeProfile(
    exchange="okx", market_type=SPOT,
    maker_fee=0.0008, taker_fee=0.0010,
    vip_tier="Lv1 (regular, post-KYC)",
    achievable_now=True, requirements="KYC only",
    confidence=PUBLISHED_BASE,
    source_reference="okx.com/fees (via published tier summaries)",
    source_date=SOURCE_DATE,
    notes=(
        "Lowest maker fee reachable with NO volume requirement among the "
        "three venues: 0.08% maker."
    ),
)

# Гипотетические профили для чувствительности. НЕ достижимы сейчас.
HYPOTHETICAL_MAKER_005 = FeeProfile(
    exchange="hypothetical", market_type=SPOT,
    maker_fee=0.0005, taker_fee=0.0010,
    vip_tier="hypothetical",
    achievable_now=False,
    requirements="would need a VIP tier we do not qualify for",
    confidence=ASSUMED,
    source_reference="cost-lab sensitivity point",
    source_date=SOURCE_DATE,
    notes="Break-even point identified by the cost lab. NOT available.",
)

HYPOTHETICAL_MAKER_002 = FeeProfile(
    exchange="hypothetical", market_type=SPOT,
    maker_fee=0.0002, taker_fee=0.0010,
    vip_tier="hypothetical",
    achievable_now=False,
    requirements="Binance VIP 6+ equivalent (>= $400M 30-day volume)",
    confidence=ASSUMED,
    source_reference="cost-lab sensitivity point",
    source_date=SOURCE_DATE,
    notes="Frequently quoted as 'the maker fee'. NOT retail-achievable.",
)

# Профиль, который использует текущий config (для сравнения).
CONFIGURED = FeeProfile(
    exchange="config", market_type=SPOT,
    maker_fee=0.0010, taker_fee=0.0010,
    vip_tier="project config",
    achievable_now=True, requirements="none",
    confidence=PUBLISHED_BASE,
    source_reference="api/paper_trading/cost_model.py defaults",
    source_date=SOURCE_DATE,
    notes="What the project currently assumes.",
)


ALL_PROFILES = (
    CONFIGURED, BINANCE_BASE, BINANCE_BNB, BINANCE_VIP1, BINANCE_VIP3,
    BYBIT_BASE, OKX_LV1, HYPOTHETICAL_MAKER_005, HYPOTHETICAL_MAKER_002,
)


def achievable_profiles() -> tuple[FeeProfile, ...]:
    """Только то, что доступно нашему аккаунту без VIP-условий."""
    return tuple(p for p in ALL_PROFILES if p.achievable_now)


def best_achievable_maker() -> FeeProfile:
    """
    Лучший РЕАЛЬНО доступный maker-тариф.

    Именно он, а не рекламный минимум, должен использоваться в оценке
    жизнеспособности стратегии.
    """
    return min(achievable_profiles(), key=lambda p: p.maker_fee)


def profiles_snapshot() -> list[dict[str, Any]]:
    return [p.to_dict() for p in ALL_PROFILES]
