"""
Locks the rule: no paid AI API is connected or called by default.

Two defects were fixed to make this true:

1. `ai_core/openai_provider.py` imported `openai` at module top level, so
   the entire paper path (paper_live_loop -> filter_adapter ->
   openai_provider) raised ModuleNotFoundError when the paid package was
   absent -- including during test collection. The import is now lazy.

2. `AI_OPENAI_SHADOW_ENABLED` defaulted to "true", meaning a cloud PAPER
   deployment would have called the paid API automatically, without an
   explicit decision. It now defaults to "false" (opt-in), and the
   provider object is not even constructed while disabled.
"""

import os

import pytest

from ai_observer.filter_adapter import AIFilterAdapter


@pytest.fixture(autouse=True)
def _clear_ai_env(monkeypatch):
    monkeypatch.delenv("AI_OPENAI_SHADOW_ENABLED", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


class TestPaidAIIsOffByDefault:

    def test_shadow_is_disabled_by_default(self):
        adapter = AIFilterAdapter()
        assert adapter.openai_shadow_enabled is False

    def test_provider_is_not_even_constructed_when_disabled(self):
        """No provider object means no client, no key read, no paid call."""
        adapter = AIFilterAdapter()
        assert adapter.provider is None

    def test_presence_of_an_api_key_alone_does_not_enable_paid_calls(self, monkeypatch):
        """
        A key sitting in the environment must not be sufficient to start
        spending money -- enabling requires the explicit flag.
        """
        monkeypatch.setenv("OPENAI_API_KEY", "sk-not-a-real-key-for-testing")

        adapter = AIFilterAdapter()

        assert adapter.openai_shadow_enabled is False
        assert adapter.provider is None

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", ""])
    def test_falsy_flag_values_keep_it_disabled(self, monkeypatch, value):
        monkeypatch.setenv("AI_OPENAI_SHADOW_ENABLED", value)
        assert AIFilterAdapter().openai_shadow_enabled is False


class TestExplicitOptInStillWorks:
    """The capability is preserved, just gated behind an explicit choice."""

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
    def test_explicit_flag_enables_shadow_mode(self, monkeypatch, value):
        monkeypatch.setenv("AI_OPENAI_SHADOW_ENABLED", value)
        adapter = AIFilterAdapter()
        assert adapter.openai_shadow_enabled is True


class TestOpenAIPackageIsNotRequiredToImport:

    def test_provider_module_imports_without_the_paid_package(self):
        """
        Importing the provider module must never raise, even when the
        `openai` package is not installed in the environment.
        """
        import importlib
        module = importlib.import_module("ai_core.openai_provider")
        assert hasattr(module, "OpenAIProvider")

    def test_provider_without_key_is_not_configured(self):
        from ai_core.openai_provider import OpenAIProvider
        provider = OpenAIProvider()
        assert provider.is_configured() is False
