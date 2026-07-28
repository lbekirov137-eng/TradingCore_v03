"""
Глобальные настройки TradingCore
"""

# ==========================
# Биржи
# ==========================

DEFAULT_DATA_EXCHANGE = "auto"
DEFAULT_EXECUTION_EXCHANGE = "bybit"

SUPPORTED_EXCHANGES = [
    "binance",
    "bybit",
    "okx",
]

# ==========================
# Торговый инструмент
# ==========================

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "5m"
DEFAULT_CANDLE_LIMIT = 300

# ==========================
# Риск
# ==========================

DEFAULT_BALANCE = 1000.0
DEFAULT_RISK_PERCENT = 0.1

# Минимальное соотношение прибыль/риск для одобрения сделки
MIN_RISK_REWARD = 2.0

# Ограничения на количество сделок и риск в течение суток (UTC)
MAX_DAILY_TRADES = 3
MAX_DAILY_RISK_PERCENT = 1.0

# Дополнительные лимиты безопасности (Phase 7)
MAX_CONSECUTIVE_LOSSES = 3
MAX_DRAWDOWN_PERCENT = 5.0

# Лимит РЕАЛИЗОВАННОГО убытка за календарные сутки (UTC), отдельно от
# MAX_DAILY_RISK_PERCENT (который лимитирует планируемый риск при входе).
MAX_DAILY_LOSS_PERCENT = 2.0

# ==========================
# Исполнение (комиссии/проскальзывание/точность биржи)
# ==========================

# Комиссия на сторону сделки (0.1% — типичная spot-комиссия Bybit/Binance)
DEFAULT_FEE_RATE = 0.001

# Допущение по проскальзыванию в базисных пунктах на сторону сделки
DEFAULT_SLIPPAGE_BPS = 5.0

# Точность цены/количества по умолчанию для BTCUSDT (переопределяется per-symbol)
DEFAULT_TICK_SIZE = 0.01
DEFAULT_LOT_SIZE = 0.000001
DEFAULT_MIN_NOTIONAL = 5.0

# Без плеча: позиция никогда не может превышать 100% доступного баланса
MAX_POSITION_PERCENT_OF_BALANCE = 100.0

# ==========================
# Фильтры режима и ликвидности (Phase 4)
# ==========================

# Максимально допустимый спред в процентах
MAX_SPREAD_PERCENT = 0.1

# Минимальное отношение текущего объёма к среднему за 20 свечей
MIN_VOLUME_RATIO = 0.5

# Границы ATR в процентах от цены: вне их режим считается непригодным
MIN_ATR_PERCENT = 0.05
MAX_ATR_PERCENT = 3.0

# Максимальный возраст последней закрытой свечи (секунды) до пометки stale
MAX_DATA_AGE_SECONDS = 900

# Движение одной свечи выше этого процента считается аномалией/шоком
MAX_CANDLE_MOVE_PERCENT = 5.0

# Пауза после убыточной сделки (секунды)
COOLDOWN_AFTER_LOSS_SECONDS = 3600

# Максимум сделок за одну торговую сессию
MAX_TRADES_PER_SESSION = 1

# ==========================
# Режим работы
# ==========================

from config.startup_safety import get_paper_trading_flag, get_live_trading_flag, get_demo_only_flag

# Значения по умолчанию (True/False) сохранены как безопасный fallback;
# переменные окружения могут их переопределить (для облачного запуска),
# но никогда не могут включить LIVE_TRADING без явного "true" в окружении.
PAPER_TRADING = get_paper_trading_flag()
LIVE_TRADING = get_live_trading_flag()
DEMO_ONLY = get_demo_only_flag()

# ==========================
# Exchange Router
# ==========================

AUTO_SELECT_EXCHANGE = True
ENABLE_LATENCY_CHECK = True
ENABLE_LIQUIDITY_CHECK = False
ENABLE_SPREAD_CHECK = False
ENABLE_OPEN_INTEREST_CHECK = False
ENABLE_FUNDING_CHECK = False