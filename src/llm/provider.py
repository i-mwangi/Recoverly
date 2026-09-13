from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

log = logging.getLogger("recoverly.llm.provider")

DEFAULT_BASE_URL: Final = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
DEFAULT_TIMEOUT_SECONDS: Final = 120.0
DEFAULT_MAX_TOKENS: Final = 800
MAX_ATTEMPTS_PER_MODEL: Final = 2
RETRY_BACKOFF_SECONDS: Final = 1.5
RETRYABLE_STATUS_CODES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})

API_KEY_ENV_VARS: Final = (
    "RECOVERLY_QWEN_API_KEY",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
)

TEXT_FAST: Final = "qwen-flash"
TEXT_BALANCED: Final = "qwen-plus"
TEXT_FLAGSHIP: Final = "qwen3-max"
VISION_BALANCED: Final = "qwen-vl-plus"
VISION_FLAGSHIP: Final = "qwen-vl-max"

PROVIDER_NAME: Final = "qwen"

TIER_ROUTING: Final[dict[int, dict[str, str]]] = {
    1: {
        "preflight": VISION_BALANCED,
        "diplomat": TEXT_FAST,
        "investigator": TEXT_FAST,
        "escalator": TEXT_FAST,
        "tone_coach": TEXT_BALANCED,
        "voice": TEXT_FAST,
        "aaa_specialist": TEXT_BALANCED,
        "default": TEXT_FAST,
    },
    2: {
        "preflight": VISION_BALANCED,
        "diplomat": TEXT_BALANCED,
        "investigator": TEXT_BALANCED,
        "escalator": TEXT_BALANCED,
        "tone_coach": TEXT_BALANCED,
        "voice": TEXT_BALANCED,
        "aaa_specialist": TEXT_FLAGSHIP,
        "default": TEXT_BALANCED,
    },
    3: {
        "preflight": VISION_FLAGSHIP,
        "diplomat": TEXT_FLAGSHIP,
        "investigator": TEXT_FLAGSHIP,
        "escalator": TEXT_FLAGSHIP,
        "tone_coach": TEXT_FLAGSHIP,
        "voice": TEXT_FLAGSHIP,
        "aaa_specialist": TEXT_FLAGSHIP,
        "default": TEXT_FLAGSHIP,
    },
}

FALLBACK_MODELS: Final[tuple[str, ...]] = (TEXT_BALANCED, TEXT_FAST)

PRICING_USD_PER_1M_TOKENS: Final[dict[str, tuple[float, float]]] = {
    TEXT_FAST: (0.05, 0.40),
    TEXT_BALANCED: (0.40, 1.20),
    TEXT_FLAGSHIP: (1.20, 6.00),
    VISION_BALANCED: (0.21, 0.63),
    VISION_FLAGSHIP: (0.80, 3.20),
}


class LLMError(RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class Usage:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    elapsed_seconds: float
    cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "elapsed_seconds": self.elapsed_seconds,
            "cost_usd": self.cost_usd,
        }


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    input_rate, output_rate = PRICING_USD_PER_1M_TOKENS.get(model, (0.0, 0.0))
    return round((input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 8)


def api_key() -> str:
    for name in API_KEY_ENV_VARS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def base_url() -> str:
    return os.getenv("RECOVERLY_QWEN_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def active_tier(tier: int | None = None) -> int:
    if tier is not None:
        return tier
    try:
        return int(os.getenv("LLM_TIER", "1"))
    except ValueError:
        return 1


def resolve_model(agent_role: str = "default", tier: int | None = None) -> str:
    override = os.getenv("LLM_MODEL", "").strip()
    if override:
        return override
    routing = TIER_ROUTING.get(active_tier(tier), TIER_ROUTING[1])
    return routing.get(agent_role, routing["default"])


def _model_chain(agent_role: str, tier: int | None) -> list[str]:
    chain = [resolve_model(agent_role, tier)]
    chain.extend(model for model in FALLBACK_MODELS if model not in chain)
    return chain


def _build_messages(system: str, user: str) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    return messages


def _request_body(
    model: str, system: str, user: str, max_tokens: int, temperature: float
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": _build_messages(system, user),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "Recoverly/1.0",
    }


def _parse_response(model: str, payload: dict[str, Any], elapsed: float) -> tuple[str, Usage]:
    choices = payload.get("choices") or []
    if not choices:
        raise LLMError(f"{model} returned no choices")

    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise LLMError(f"{model} returned empty content")

    raw_usage = payload.get("usage") or {}
    input_tokens = int(raw_usage.get("prompt_tokens") or 0)
    output_tokens = int(raw_usage.get("completion_tokens") or 0)

    usage = Usage(
        provider=PROVIDER_NAME,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        elapsed_seconds=round(elapsed, 3),
        cost_usd=estimate_cost_usd(model, input_tokens, output_tokens),
    )
    return text, usage


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in RETRYABLE_STATUS_CODES
    return isinstance(error, (httpx.TimeoutException, httpx.TransportError, LLMError))


def _invoke(client: httpx.Client, model: str, body: dict[str, Any]) -> tuple[str, Usage]:
    started = time.perf_counter()
    response = client.post("/chat/completions", json=body)
    response.raise_for_status()
    return _parse_response(model, response.json(), time.perf_counter() - started)


def complete(
    system: str,
    user: str,
    *,
    agent_role: str = "default",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.3,
    tier: int | None = None,
) -> tuple[str, dict[str, Any]]:
    key = api_key()
    if not key:
        raise LLMConfigurationError(
            "no Qwen API key found; set one of " + ", ".join(API_KEY_ENV_VARS)
        )

    last_error: Exception | None = None

    with httpx.Client(
        base_url=base_url(), headers=_headers(key), timeout=DEFAULT_TIMEOUT_SECONDS
    ) as client:
        for model in _model_chain(agent_role, tier):
            body = _request_body(model, system, user, max_tokens, temperature)
            for attempt in range(1, MAX_ATTEMPTS_PER_MODEL + 1):
                try:
                    text, usage = _invoke(client, model, body)
                except Exception as error:
                    last_error = error
                    retryable = _is_retryable(error)
                    log.warning(
                        "qwen/%s attempt %d/%d failed (%s): %s",
                        model,
                        attempt,
                        MAX_ATTEMPTS_PER_MODEL,
                        "retryable" if retryable else "fatal",
                        error,
                    )
                    if not retryable:
                        break
                    if attempt < MAX_ATTEMPTS_PER_MODEL:
                        time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                else:
                    log.info(
                        "qwen/%s ok role=%s in=%d out=%d %.2fs $%.6f",
                        model,
                        agent_role,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.elapsed_seconds,
                        usage.cost_usd,
                    )
                    return text, usage.as_dict()

    raise LLMError(f"all Qwen models failed for role={agent_role}: {last_error}")


async def complete_async(
    system: str,
    user: str,
    *,
    agent_role: str = "default",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.3,
    tier: int | None = None,
) -> tuple[str, dict[str, Any]]:
    return await asyncio.to_thread(
        complete,
        system,
        user,
        agent_role=agent_role,
        max_tokens=max_tokens,
        temperature=temperature,
        tier=tier,
    )


def provider_info(agent_role: str = "default", tier: int | None = None) -> dict[str, Any]:
    return {
        "provider": PROVIDER_NAME,
        "base_url": base_url(),
        "tier": active_tier(tier),
        "agent_role": agent_role,
        "model": resolve_model(agent_role, tier),
        "fallback_models": list(FALLBACK_MODELS),
        "api_key_present": bool(api_key()),
    }
