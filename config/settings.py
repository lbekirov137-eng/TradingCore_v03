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

# ==========================
# Режим работы
# ==========================

PAPER_TRADING = True
LIVE_TRADING = False

# ==========================
# Exchange Router
# ==========================

AUTO_SELECT_EXCHANGE = True
ENABLE_LATENCY_CHECK = True
ENABLE_LIQUIDITY_CHECK = False
ENABLE_SPREAD_CHECK = False
ENABLE_OPEN_INTEREST_CHECK = False
ENABLE_FUNDING_CHECK = False