"""
Adaptive ORB Configuration

Все параметры являются начальными.
Окончательные значения будут определяться
по статистике Backtesting Engine.
"""

# Минимальная длина Opening Range
MIN_ORB_MINUTES = 5

# Максимальная длина Opening Range
MAX_ORB_MINUTES = 15

# Шаг изменения
ORB_STEP = 5

# ATR
ATR_LOW = 0.8
ATR_HIGH = 1.5

# Volume
VOLUME_LOW = 0.8
VOLUME_HIGH = 1.2

# Liquidity
MIN_LIQUIDITY_SCORE = 70

# Минимальная уверенность Strategy Engine
MIN_CONFIDENCE = 0.75

# Минимальный Profit Factor,
# при котором настройки считаются рабочими
MIN_PROFIT_FACTOR = 1.50

# Максимальная просадка
MAX_DRAWDOWN = 8.0