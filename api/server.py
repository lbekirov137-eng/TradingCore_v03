import os
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from api.app_safety import (
    enforce_startup_safety,
    paper_monitor_lifespan,
    register_safety_routes,
)

# ---------------------------------------------------------------------
# STARTUP SAFETY GATE — общий для всех entrypoint (api/app_safety.py).
# Выполняется при импорте модуля, ДО создания приложения и до того, как
# uvicorn начнёт слушать порт. Небезопасная или нераспознанная
# конфигурация -> процесс не стартует.
#
# Реализация НЕ дублируется: api/main.py вызывает ровно те же функции,
# поэтому обойти защиту сменой entrypoint невозможно.
# ---------------------------------------------------------------------
_STARTUP_SUMMARY = enforce_startup_safety()

from api.analyzer import MarketAnalyzer

# Лёгкий импорт: пакет читает только json/os/pathlib и НЕ тянет Bootstrap,
# провайдеров и сеть, поэтому веб-процесс стартует с прежней скоростью
# независимо от того, включён ли торговый цикл.
from api.paper_analytics import (
    build_observation,
    build_report,
    journal_path,
    load_records,
    render_report_text,
)

from api.paper_storage import data_health

# Супервизор так же лёгок: чистые функции и литеральный реестр, без
# сети, диска и Bootstrap.
from api.strategy_supervisor import (
    DEFAULT_STRATEGY_ID,
    StrategyRegistryError,
    all_strategies,
    build_change_report,
    evaluate_active_strategy,
    plan_switch,
    supervisor_status,
    thresholds_snapshot,
)

app = FastAPI(
    title="TradingCore API",
    version="0.1",
    lifespan=paper_monitor_lifespan,
)

# Общие safety-эндпоинты: /health, /ready, /safety, /monitor/status.
# Регистрируются из api/app_safety, а не описываются здесь повторно.
register_safety_routes(app)


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "TradingCore API",
        "mode": "PAPER",
    }


@app.get("/analyze")
def analyze():
    return MarketAnalyzer.analyze()


@app.get("/performance")
def performance(
    format: str = "json",
    limit: int = 0,
):
    """
    Оценка paper-результатов по журналу цикла.

    Только чтение: эндпоинт не открывает и не закрывает позиции, не влияет
    на сигнал и риск. Отдаёт метрики, размер выборки и защитный статус
    SAFE / WARNING / STOP.

    format=text возвращает тот же отчёт, что печатается в Deploy Logs.
    limit>0 ограничивает разбор последними N записями журнала.

    ВАЖНО про долговечность: в контейнере `data/` эфемерна (см. Dockerfile
    и .dockerignore), поэтому после рестарта журнал пуст, и отчёт честно
    покажет INSUFFICIENT_SAMPLE с нулём сделок. Это не сбой эндпоинта —
    это отсутствие подключённого volume. Долговечный журнал в облаке —
    stdout (строки PAPER_DECISION).
    """
    records = load_records(
        journal_path(),
        limit=limit if limit > 0 else None,
    )

    report = build_report(records)

    if format == "text":
        return PlainTextResponse(
            render_report_text(
                report,
                title="PAPER PERFORMANCE REPORT",
            )
        )

    return report






@app.get("/performance/data-health")
def performance_data_health():
    """
    Здоровье хранилища: переживут ли данные рестарт и целы ли они.

    Существует ровно затем, чтобы «монитор работал, сделок не было» и
    «история потеряна при redeploy» нельзя было перепутать: в отчёте о
    результатах оба случая выглядят как ноль сделок.

    Только чтение.
    """
    return data_health()


def _supervisor_observations(limit: int = 0):
    """Наблюдения из журнала цикла для супервизора."""
    records = load_records(
        journal_path(),
        limit=limit if limit > 0 else None,
    )

    return [
        build_observation(record)
        for record in records
        if isinstance(record, dict) and not record.get("__unreadable__")
    ]


@app.get("/strategies/status")
def strategies_status(champion: str = DEFAULT_STRATEGY_ID):
    """
    Статус супервизора, реестра и champion-стратегии.

    champion по умолчанию — политика NO_TRADE в RANGE: пока ни одна
    стратегия не подтверждена OOS-выборкой, безопасный режим это
    «не торгуем», а не «торгуем чем-нибудь».

    Блок research_only сохранён: он фиксирует уже измеренные результаты
    наследуемых реализаций и не должен потеряться при добавлении реестра.
    """
    try:
        status = supervisor_status(
            champion_id=champion,
            observations=_supervisor_observations(),
        )
    except StrategyRegistryError as error:
        # Незарегистрированный champion — ошибка запроса, а не сбой
        # сервиса: реестр намеренно закрыт.
        return {
            "error": "UNREGISTERED_STRATEGY",
            "detail": str(error),
            "registered": [
                spec.strategy_id for spec in all_strategies()
            ],
        }

    status["research_only"] = _research_only_notes()

    return status


def _research_only_notes():
    """
    Уже измеренные результаты наследуемых реализаций.

    Держатся отдельно от реестра супервизора намеренно: это исторический
    факт о конкретных реализациях, а не спецификация кандидата. Кандидат
    с тем же названием обязан проходить OOS-гейты заново.
    """
    return {
        "VLAD_ORB": {
            "status": "RESEARCH_ONLY",
            "production_approved": False,
            "note": "Активная стратегия paper-контура координатора.",
        },
        "ORB_LEGACY": {
            "status": "RESEARCH_ONLY",
            "production_approved": False,
            "note": (
                "Наследуемая реализация в api/strategy_engine/strategies/orb/. "
                "Backtest: 208 сделок за 6 месяцев, net -176.54 (-17.7%), "
                "profit factor 0.205, walk-forward consistency 0.0%."
            ),
        },
        "VWAP_TREND_PULLBACK": {
            "status": "RESEARCH_ONLY",
            "production_approved": False,
            "note": (
                "Backtest: 98 сделок, net -128.37 (-12.8%), "
                "profit factor 0.092, walk-forward consistency 0.0%."
            ),
        },
    }


@app.get("/strategies/performance")
def strategies_performance(champion: str = DEFAULT_STRATEGY_ID):
    """
    Статистика активной стратегии и пороги, по которым её судят.

    Пороги отдаются ВМЕСТЕ с метриками намеренно: число без порога не
    позволяет понять, близка ли стратегия к остановке, а порог,
    известный только коду, невозможно проконтролировать снаружи.
    """
    observations = _supervisor_observations()

    try:
        evaluation = evaluate_active_strategy(champion, observations)
    except StrategyRegistryError as error:
        return {
            "error": "UNREGISTERED_STRATEGY",
            "detail": str(error),
        }

    return {
        "schema_version": "STRATEGY_PERFORMANCE_V1",
        "mode": "PAPER",
        "real_orders_enabled": False,
        "champion": champion,
        "status": evaluation["status"],
        "reasons": evaluation["reasons"],
        "insufficient_sample": evaluation["insufficient_sample"],
        "sample": evaluation["sample"],
        "stats": evaluation["stats"],
        "observed_triggers": evaluation["observed_triggers"],
        "thresholds": thresholds_snapshot(),
    }


@app.get("/strategies/change-report")
def strategies_change_report(champion: str = DEFAULT_STRATEGY_ID):
    """
    Strategy Change Report — почему переключились ИЛИ почему нет.

    Отчёт строится и в случае отказа: «переключения не было, потому что
    выборка мала» — такой же результат работы супервизора, как и само
    переключение, и без него молчание неотличимо от поломки.

    Эндпоинт ничего не переключает: он вызывает ЧИСТУЮ функцию plan_switch
    и показывает план. Исполнение плана — отдельное действие цикла.
    """
    observations = _supervisor_observations()

    try:
        evaluation = evaluate_active_strategy(champion, observations)
    except StrategyRegistryError as error:
        return {
            "error": "UNREGISTERED_STRATEGY",
            "detail": str(error),
        }

    # Валидаций кандидатов пока нет: они появляются только после
    # хронологического walk-forward прогона на накопленных данных.
    # Пустой словарь честно приводит к NO_VALID_STRATEGY, а не к выбору
    # наугад.
    plan = plan_switch(
        active_strategy_id=champion,
        evaluation=evaluation,
        validations={},
        has_open_position=False,
        now_utc=datetime.now(timezone.utc).isoformat(),
    )

    return build_change_report(
        plan=plan,
        evaluation=evaluation,
        now_utc=datetime.now(timezone.utc).isoformat(),
    )
