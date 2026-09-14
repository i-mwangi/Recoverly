from __future__ import annotations

import pytest

from src.llm.provider import (
    FALLBACK_MODELS,
    PROVIDER_NAME,
    PRICING_USD_PER_1M_TOKENS,
    TEXT_BALANCED,
    TEXT_FAST,
    TEXT_FLAGSHIP,
    TIER_ROUTING,
    VISION_BALANCED,
    VISION_FLAGSHIP,
    active_tier,
    api_key,
    estimate_cost_usd,
    provider_info,
    resolve_model,
)


class TestRouting:
    def test_provider_is_qwen(self):
        assert PROVIDER_NAME == "qwen"

    def test_every_tier_has_all_roles(self):
        roles = TIER_ROUTING[1].keys()
        for tier in TIER_ROUTING.values():
            assert tier.keys() == roles

    def test_diplomat_tier1_fastest(self):
        assert resolve_model("diplomat", tier=1) == TEXT_FAST

    def test_tone_coach_tier1_balanced(self):
        assert resolve_model("tone_coach", tier=1) == TEXT_BALANCED

    def test_preflight_uses_vision(self):
        assert resolve_model("preflight", tier=1) == VISION_BALANCED
        assert resolve_model("preflight", tier=3) == VISION_FLAGSHIP

    def test_tier3_flagship(self):
        assert resolve_model("diplomat", tier=3) == TEXT_FLAGSHIP

    def test_unknown_role_falls_back_to_default(self):
        assert resolve_model("mystery", tier=1) == TIER_ROUTING[1]["default"]

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "custom-model")
        assert resolve_model("diplomat", tier=1) == "custom-model"


class TestTierSelection:
    def test_default_tier_is_1(self, monkeypatch):
        monkeypatch.delenv("LLM_TIER", raising=False)
        assert active_tier() == 1

    def test_env_tier_used(self, monkeypatch):
        monkeypatch.setenv("LLM_TIER", "2")
        assert active_tier() == 2

    def test_bad_tier_falls_back(self, monkeypatch):
        monkeypatch.setenv("LLM_TIER", "not-a-number")
        assert active_tier() == 1


class TestPricing:
    @pytest.mark.parametrize("model", [TEXT_FAST, TEXT_BALANCED, TEXT_FLAGSHIP, VISION_BALANCED, VISION_FLAGSHIP])
    def test_priced(self, model):
        assert PRICING_USD_PER_1M_TOKENS[model][0] > 0
        assert PRICING_USD_PER_1M_TOKENS[model][1] > 0

    def test_cost_estimate_scales_linearly(self):
        one = estimate_cost_usd(TEXT_BALANCED, 1_000, 500)
        double = estimate_cost_usd(TEXT_BALANCED, 2_000, 1_000)
        assert double == pytest.approx(one * 2)

    def test_cost_unknown_model_is_zero(self):
        assert estimate_cost_usd("mystery-model", 1_000, 500) == 0


class TestApiKey:
    def test_prefers_namespaced_env(self, monkeypatch):
        monkeypatch.setenv("RECOVERLY_QWEN_API_KEY", "primary")
        monkeypatch.setenv("QWEN_API_KEY", "secondary")
        assert api_key() == "primary"

    def test_falls_back_to_generic(self, monkeypatch):
        monkeypatch.delenv("RECOVERLY_QWEN_API_KEY", raising=False)
        monkeypatch.setenv("QWEN_API_KEY", "generic")
        assert api_key() == "generic"

    def test_no_key_returns_empty_string(self, monkeypatch):
        for var in ("RECOVERLY_QWEN_API_KEY", "QWEN_API_KEY", "DASHSCOPE_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        assert api_key() == ""


class TestProviderInfo:
    def test_reports_role_and_model(self):
        info = provider_info("tone_coach", tier=1)
        assert info["provider"] == "qwen"
        assert info["agent_role"] == "tone_coach"
        assert info["model"] == TEXT_BALANCED

    def test_lists_fallback_chain(self):
        info = provider_info("diplomat", tier=1)
        assert set(info["fallback_models"]) == set(FALLBACK_MODELS)
