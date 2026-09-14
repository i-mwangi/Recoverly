from __future__ import annotations

import logging

from src.config import settings, validate_runtime_config
from src.llm.provider import provider_info


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    print(f"Recoverly runtime configuration")
    print(f"  demo_mode: {settings.demo_mode}")
    print(f"  log_level: {settings.log_level}")
    print(f"  operator_timezone: {settings.operator_timezone}")
    print(f"  sweet spot: ${settings.sweet_spot_min_usd:,}-${settings.sweet_spot_max_usd:,}")
    print(f"  per-case cost cap: ${settings.per_case_cost_cap_usd}")
    print()

    info = provider_info("diplomat")
    print(f"LLM provider: {info['provider']} tier {info['tier']}")
    print(f"  base URL: {info['base_url']}")
    print(f"  diplomat model: {info['model']}")
    print(f"  fallback chain: {', '.join(info['fallback_models'])}")
    print(f"  API key present: {info['api_key_present']}")
    print()

    issues = validate_runtime_config()
    if not issues:
        print("no configuration issues")
        return 0

    errors = 0
    for issue in issues:
        print(issue)
        if issue.startswith("[ERROR]"):
            errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
