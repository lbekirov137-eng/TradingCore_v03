# Strategy Implementation Contracts

Зафиксировано ДО написания логики. Источник каждого параметра указан явно.
Ни одно значение здесь не выбрано по результатам прогона на данных: файл
создан раньше, чем существовали реализации.

**Разрешение.** Реализация трёх недостающих стратегий выполняется по уже
существующим письменным спецификациям проекта. Новая торговая идея не
придумывается, параметры под историю не оптимизируются.

---

## Общие инварианты (все три стратегии)

| Свойство | Значение | Источник |
|---|---|---|
| Market | BTCUSDT | `config/settings.py: DEFAULT_SYMBOL` |
| Timeframe | 5m | `config/settings.py: DEFAULT_INTERVAL` |
| Направление | LONG only | `SPOT_LONG_ONLY`, `PaperPositionManager._validate_paper_order` |
| Риск на сделку | 0.1% | `config/settings.py: DEFAULT_RISK_PERCENT` |
| Плечо | 1x | `config/startup_safety.py` |
| Averaging / пирамидинг | запрещён | одна позиция, `PaperPositionManager.has_open_position` |
| Решения | только по ЗАКРЫТОЙ свече | `paper_live_loop`: обработка по `close_time_ms` |
| Будущие свечи | запрещены | контракт `evaluate_closed_candle(candles, index)` |
| Stop | `entry − 1×ATR` (= 1R) | `api/trade_plan.py: TradePlan.build` |
| TP1 | `entry + 2×ATR` (= 2R) | `api/trade_plan.py: TradePlan.build` |
| TP2 | `entry + 3×ATR` (= 3R) | `api/trade_plan.py: TradePlan.build` |
| Минимальный R:R | 2.0 | `StrategySpec.min_risk_reward`, согласуется с TP1=2R |
| Комиссия | taker 0.10% обе стороны | `api/paper_trading/cost_model.py` |
| Проскальзывание | 5 bps, против сделки | `api/paper_trading/cost_model.py` |
| Реальные ордера | невозможны | `real_order_sent=False` на всех путях |

### Расхождение, которое нужно знать

`api/strategy_supervisor/registry.py` в `exit_criteria` описывает
«TP1 at 1R, TP2 at 2R». Каноническая реализация проекта
(`api/trade_plan.py`) использует **2R / 3R**. Реализации следуют
`TradePlan.build`, потому что это исполняемая спецификация, а не
комментарий. Текст в реестре требует приведения в соответствие —
**не менял его в этом заходе**, так как реестр уже закоммичен и его правка
изменила бы `version` стратегий.

---

## 1. SESSION_VWAP_TREND_PULLBACK v1.0.0

| Пункт | Значение | Источник |
|---|---|---|
| Session / TZ | CRYPTO 00:00–23:59 UTC, session VWAP сбрасывается в 00:00 UTC | `config/trading_sessions.py` |
| Индикаторы | session VWAP, EMA20/EMA50, ATR14, market structure | `api/ema.py`, `api/atr.py`, `api/market_structure.py` |
| Разрешённый режим | TREND | `registry.allowed_regimes` |
| Warm-up | 60 баров | см. таблицу неопределённостей |
| Вход | EMA20 > EMA50 (направление старшего ТФ) И цена ≥ VWAP И подтверждённый pullback к зоне VWAP И закрытие подтверждающей свечи выше её открытия и выше VWAP | `registry.entry_criteria` |
| Запрет | боковик: `atr_percent` вне [0.8, 1.5] ⇒ NO_TRADE | `config/adaptive_orb.py: ATR_LOW/ATR_HIGH` |
| Инвалидация | закрытие ниже VWAP; закрытие ниже последнего higher-low | `registry.exit_criteria` |
| Stop | `entry − 1×ATR` | `TradePlan.build` |
| Цели | 2R / 3R | `TradePlan.build` |
| Сделок за сессию | 1 | см. таблицу неопределённостей |

## 2. LONDON_SESSION_BREAKOUT_RETEST v1.0.0

| Пункт | Значение | Источник |
|---|---|---|
| Session / TZ | **London 07:00–16:00 UTC** (проектное расписание, уже в UTC — перевод TZ не требуется и не выполняется) | `config/trading_sessions.py: TRADING_SESSIONS["LONDON"]` |
| Opening range | 07:00–07:30 UTC (30 мин = 6 свечей по 5m) | `registry.parameters: opening_range_minutes=30` |
| Индикаторы | ATR14, диапазон сессии | `api/atr.py` |
| Разрешённые режимы | BREAKOUT, TREND | `registry.allowed_regimes` |
| Warm-up | 60 баров | см. таблицу неопределённостей |
| Вход | **закрытая** свеча выше `range_high` (breakout) → затем retest: свеча касается зоны `range_high ± 0.25×ATR` и **закрывается выше** `range_high` | `registry.entry_criteria`, `retest_tolerance_atr=0.25` |
| Запрет chase | вход без состоявшегося retest запрещён структурно (машина состояний) | `registry.entry_criteria` |
| Инвалидация | закрытие обратно ВНУТРИ диапазона ⇒ сетап сброшен | `registry.exit_criteria` |
| Stop | `entry − 1×ATR` | `TradePlan.build` |
| Цели | 2R / 3R | `TradePlan.build` |
| Сделок за сессию | 1 | `registry.parameters: max_trades_per_session=1` |
| Окно входов | до 16:00 UTC | `TRADING_SESSIONS["LONDON"].end` |

## 3. TREND_PULLBACK_EMA_STRUCTURE v1.0.0

| Пункт | Значение | Источник |
|---|---|---|
| Session / TZ | CRYPTO 00:00–23:59 UTC | `config/trading_sessions.py` |
| Индикаторы | EMA20, EMA50, ATR14, market structure | `registry.parameters: fast_ema=20, slow_ema=50` |
| Разрешённый режим | TREND | `registry.allowed_regimes` |
| Warm-up | 60 баров | max(slow_ema=50, ATR14) + запас |
| Вход | EMA20 > EMA50 И подтверждённый higher-low (≥2 подтверждения) И pullback в зону EMA20…EMA50 И continuation: закрытие выше предыдущего swing high | `registry.entry_criteria`, `min_structure_confirmations=2` |
| Запрет | структура и EMA противоречат (EMA вверх, структура даёт lower-low) ⇒ NO_TRADE | `registry.entry_criteria` |
| Инвалидация | закрытие ниже подтверждённого higher-low | `registry.exit_criteria` |
| Stop | `entry − 1×ATR` | `TradePlan.build` |
| Цели | 2R / 3R | `TradePlan.build` |
| Сделок за сессию | 1 | см. таблицу неопределённостей |

---

## Неопределённые параметры и принятые defaults

Все помечены `DEFAULT_NOT_OPTIMIZED`. **Ни один не менялся после просмотра
результатов walk-forward** — значения зафиксированы в этом файле до
первого прогона.

| parameter | source | chosen value | reason | optimization status |
|---|---|---|---|---|
| `warmup_bars` | не задан в проекте | 60 | Покрывает slow_ema=50 и ATR14 с запасом. Меньше — EMA50 не прогрета; больше — теряются ранние свечи выборки. | DEFAULT_NOT_OPTIMIZED |
| `vwap_pullback_zone_atr` | `registry: vwap_band_atr_multiple=0.5` | 0.5×ATR | Взято из реестра напрямую, не подбиралось. | FROM_REGISTRY |
| `max_trades_per_session` (VWAP, EMA) | задан только для London и ORB | 1 | Консервативно: равняется явно заданному значению для двух других стратегий. Больше сделок = больше издержек при неизвестном крае. | DEFAULT_NOT_OPTIMIZED |
| `session_reset_utc` (VWAP) | не задан | 00:00 UTC | Совпадает с началом сессии CRYPTO в проектном расписании. | DEFAULT_NOT_OPTIMIZED |
| `min_structure_confirmations` | `registry` | 2 | Из реестра. | FROM_REGISTRY |
| `structure_lookback_bars` | не задан | 20 | Нужен конечный горизонт для swing-точек. 20 баров = 100 минут на 5m, покрывает внутрисессионную структуру. | DEFAULT_NOT_OPTIMIZED |
| `atr_period` | не задан явно для этих стратегий | 14 | Стандарт, используемый в `api/atr.py`. | PROJECT_STANDARD |
| `range_regime_atr_bounds` | `config/adaptive_orb.py` | [0.8, 1.5] | `ATR_LOW`/`ATR_HIGH` — уже существующие проектные границы «боковик / нормально / слишком волатильно». | FROM_PROJECT_CONFIG |
| `retest_tolerance_atr` | `registry` | 0.25 | Из реестра. | FROM_REGISTRY |
| `opening_range_minutes` | `registry` | 30 | Из реестра. | FROM_REGISTRY |
| `breakout_confirmation` | не задан | закрытие свечи выше границы | Проектный инвариант «только закрытые свечи»; wick-breakout был бы решением по незакрытым данным. | DEFAULT_NOT_OPTIMIZED |
| `holdout_fraction` | не задан | 0.30 | Стандартная доля OOS; выбрана до прогона. | DEFAULT_NOT_OPTIMIZED |
| `walk_forward_windows` | не задан | 4 | Даёт ≥4 независимые точки для robustness ratio при 6 месяцах данных. | DEFAULT_NOT_OPTIMIZED |

### Явные ambiguity, которые НЕ скрыты

1. **TP1/TP2: 1R/2R (текст реестра) против 2R/3R (`TradePlan.build`).**
   Выбран `TradePlan.build` как исполняемая спецификация. Требует
   согласования текста реестра отдельным изменением.
2. **«Higher timeframe direction» для VWAP-стратегии не определён в
   проекте.** На 5m нет загруженного старшего ТФ. Использован
   прокси EMA20>EMA50 на рабочем ТФ — это ослабление исходной формулировки,
   и оно зафиксировано здесь, а не спрятано в коде.
3. **«Liquidity proxy» задан как `MIN_LIQUIDITY_SCORE=70`, но источник
   score для исторических свечей отсутствует.** В backtest используется
   относительный объём; порог ликвидности НЕ применяется, и это отражено в
   diagnostics как `LIQUIDITY_PROXY_UNAVAILABLE`.
4. **`MIN_CONFIDENCE=0.75` и `MIN_PROFIT_FACTOR=1.50`** из
   `adaptive_orb.py` относятся к ORB-контуру. Для gates супервизора
   действует `PROMOTE_MIN_PROFIT_FACTOR=1.15`; расхождение намеренно не
   устранялось — это разные пороги разных контуров.
