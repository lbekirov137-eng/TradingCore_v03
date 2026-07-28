"""
Единый источник истины для live_paper_run.

Дефект: корневой live_paper_run.py и api/providers/live_paper_run.py были
побайтово идентичными копиями одной реализации. К моменту исправления они
УЖЕ разошлись — предупреждение о единицах RISK_PERCENT (0.1 означает 0.1%,
а не 10%) попало только в корневой файл. Две копии торгового кода с
расходящимися пояснениями к риск-параметру — прямой путь к ошибке
в сто раз.

Канонической признана корневая реализация: её импортирует рабочий цикл
(paper_live_loop.py). Второй путь оставлен как тонкий re-export, чтобы не
ломать возможные внешние импорты.
"""

import live_paper_run as canonical
from api.providers import live_paper_run as compat


PUBLIC_NAMES = (
    "SYMBOL",
    "TIMEFRAME",
    "CANDLE_LIMIT",
    "PAPER_BALANCE",
    "RISK_PERCENT",
    "build_live_context",
    "build_report",
    "main",
)


class TestBothImportPathsAreTheSameObject:

    def test_functions_are_identical_objects(self):
        """
        Не «равны по поведению», а буквально ОДИН объект. Копия не может
        разойтись с оригиналом, если это тот же самый объект в памяти.
        """
        assert compat.build_live_context is canonical.build_live_context
        assert compat.build_report is canonical.build_report
        assert compat.main is canonical.main

    def test_constants_are_identical(self):
        for name in ("SYMBOL", "TIMEFRAME", "CANDLE_LIMIT", "PAPER_BALANCE"):
            assert getattr(compat, name) == getattr(canonical, name), name

    def test_risk_percent_matches_and_is_the_percent_convention(self):
        """
        Именно это значение разошлось между копиями по смыслу.
        0.1 читается api/risk_engine.py как ПРОЦЕНТЫ (делится на 100),
        то есть 0.1% капитала. Замена на 0.001 уменьшила бы риск в 100 раз.
        """
        assert compat.RISK_PERCENT == canonical.RISK_PERCENT
        assert canonical.RISK_PERCENT == 0.1

    def test_every_public_name_is_reexported(self):
        for name in PUBLIC_NAMES:
            assert hasattr(compat, name), name


class TestCompatModuleHasNoOwnLogic:
    """Тонкий wrapper не должен обрастать собственной реализацией."""

    def test_compat_module_defines_nothing_of_its_own(self):
        import inspect

        for name in PUBLIC_NAMES:
            obj = getattr(compat, name)

            if inspect.isfunction(obj):
                assert obj.__module__ == "live_paper_run", (
                    f"{name} определён в компат-модуле вместо канонического"
                )

    def test_compat_module_is_small(self):
        """
        Косвенная, но полезная проверка: если кто-то снова скопирует сюда
        реализацию, файл резко вырастет и тест это заметит.
        """
        import pathlib

        source = pathlib.Path(compat.__file__).read_text(encoding="utf-8")
        code_lines = [
            line
            for line in source.splitlines()
            if line.strip()
            and not line.strip().startswith("#")
        ]

        assert len(code_lines) < 60, (
            "компат-модуль слишком велик — похоже, в него вернули логику"
        )


class TestCanonicalBehaviourIsUnchanged:

    def test_public_market_provider_is_used(self):
        """Данные берутся только из публичного провайдера, без ключей."""
        import inspect

        source = inspect.getsource(canonical.build_live_context)

        assert "BinancePublicMarketProvider" in source
        assert "api_key" not in source.lower()

    def test_real_orders_stay_disabled(self):
        import inspect

        source = inspect.getsource(canonical.build_live_context)

        assert '"real_orders_enabled": False' in source
