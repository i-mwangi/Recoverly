from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
FALSE_VALUES = frozenset({"false", "0", "no", "off"})


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


def env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    return Path(value) if value else default


@dataclass(frozen=True, slots=True)
class DataSources:
    customer_history_json: Path = field(
        default_factory=lambda: (
            PROJECT_ROOT / "samples" / "seed_data" / "customer_history.json"
        )
    )
    external_data_json: Path = field(
        default_factory=lambda: (
            PROJECT_ROOT / "samples" / "seed_data" / "external_data.json"
        )
    )
    cases_json: Path = field(
        default_factory=lambda: PROJECT_ROOT / "samples" / "seed_data" / "cases.json"
    )
    payment_methods_json: Path = field(
        default_factory=lambda: (
            PROJECT_ROOT / "src" / "payments" / "payment_methods.json"
        )
    )
    attorney_referrals_yaml: Path = field(
        default_factory=lambda: PROJECT_ROOT / "config" / "attorney_referrals.yaml"
    )
    audit_trail_jsonl: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "audit_trail.jsonl"
    )
    case_state_dir: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "cases"
    )
    attachments_dir: Path = field(
        default_factory=lambda: PROJECT_ROOT / "data" / "attachments"
    )
    chroma_persist_dir: Path = field(
        default_factory=lambda: env_path(
            "CHROMA_PERSIST_DIR", PROJECT_ROOT / "data" / "chroma"
        )
    )
    postgres_url: str = field(default_factory=lambda: os.getenv("POSTGRES_URL", ""))
    redis_url: str = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )


@dataclass(frozen=True, slots=True)
class ApiKeys:
    qwen: str = field(
        default_factory=lambda: (
            os.getenv("RECOVERLY_QWEN_API_KEY", "")
            or os.getenv("QWEN_API_KEY", "")
            or os.getenv("DASHSCOPE_API_KEY", "")
        )
    )
    proxycurl: str = field(default_factory=lambda: os.getenv("PROXYCURL_API_KEY", ""))
    opencorporates: str = field(
        default_factory=lambda: os.getenv("OPENCORPORATES_API_KEY", "")
    )
    courtlistener: str = field(
        default_factory=lambda: os.getenv("COURTLISTENER_API_TOKEN", "")
    )
    paystack: str = field(default_factory=lambda: os.getenv("PAYSTACK_SECRET_KEY", ""))
    hedera_operator_id: str = field(
        default_factory=lambda: os.getenv("HEDERA_OPERATOR_ID", "")
    )
    hedera_operator_key: str = field(
        default_factory=lambda: os.getenv("HEDERA_OPERATOR_KEY", "")
    )
    hedera_network: str = field(
        default_factory=lambda: os.getenv("HEDERA_NETWORK", "testnet")
    )
    resend: str = field(default_factory=lambda: os.getenv("RESEND_API_KEY", ""))
    twilio_account_sid: str = field(
        default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", "")
    )
    twilio_auth_token: str = field(
        default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", "")
    )
    fish_audio: str = field(default_factory=lambda: os.getenv("FISH_API_KEY", ""))
    slack_bot_token: str = field(
        default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", "")
    )
    slack_signing_secret: str = field(
        default_factory=lambda: os.getenv("SLACK_SIGNING_SECRET", "")
    )
    slack_webhook_url: str = field(
        default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", "")
    )


@dataclass(frozen=True, slots=True)
class Settings:
    demo_mode: bool = field(
        default_factory=lambda: env_bool("DEMO_MODE", default=False)
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    operator_timezone: str = field(
        default_factory=lambda: os.getenv("OPERATOR_TIMEZONE", "Africa/Nairobi")
    )
    daily_digest_hour: int = field(
        default_factory=lambda: env_int("DAILY_DIGEST_HOUR", 9)
    )
    daily_digest_minute: int = field(
        default_factory=lambda: env_int("DAILY_DIGEST_MINUTE", 0)
    )

    lead_time_email_hours: int = 1
    lead_time_voice_hours: int = 4
    lead_time_demand_letter_hours: int = 8

    tier1a_cache_ttl_seconds: int = 3_600
    tier1b_cache_ttl_seconds: int = 86_400
    tier2_cache_ttl_seconds: int = 14_400

    max_partial_subcycles_per_invoice: int = 2

    sweet_spot_min_usd: int = 3_000
    sweet_spot_max_usd: int = 40_000

    per_case_cost_cap_usd: float = field(
        default_factory=lambda: float(os.getenv("PER_CASE_COST_CAP_USD", "0.50"))
    )
    hitl_sla_seconds: int = field(
        default_factory=lambda: env_int("HITL_SLA_SECONDS", 14_400)
    )

    data: DataSources = field(default_factory=DataSources)
    api_keys: ApiKeys = field(default_factory=ApiKeys)


settings = Settings()

DataSourceKind = Literal[
    "customers",
    "invoices",
    "customer_replies",
    "cases",
    "external_scans",
    "deep_dives",
    "excuse_library",
    "payment_methods",
    "attorney_referrals",
    "audit_trail",
]

POSTGRES_BACKED_KINDS: frozenset[str] = frozenset(
    {
        "customers",
        "invoices",
        "customer_replies",
        "cases",
        "external_scans",
        "deep_dives",
    }
)

STATIC_CONFIG_KINDS: frozenset[str] = frozenset(
    {"payment_methods", "attorney_referrals"}
)


def _fixture_path(kind: DataSourceKind) -> Path:
    paths: dict[str, Path] = {
        "customers": settings.data.customer_history_json,
        "invoices": settings.data.customer_history_json,
        "customer_replies": settings.data.customer_history_json,
        "cases": settings.data.cases_json,
        "external_scans": settings.data.external_data_json,
        "deep_dives": settings.data.external_data_json,
        "payment_methods": settings.data.payment_methods_json,
        "attorney_referrals": settings.data.attorney_referrals_yaml,
        "audit_trail": settings.data.audit_trail_jsonl,
        "excuse_library": settings.data.chroma_persist_dir,
    }
    try:
        return paths[kind]
    except KeyError:
        raise ValueError(f"unknown data source kind: {kind!r}") from None


def get_data_source(kind: DataSourceKind) -> dict[str, object]:
    path = _fixture_path(kind)

    if kind == "audit_trail":
        return {"mode": "append_log", "path": path}

    if kind == "excuse_library":
        return {"mode": "chroma", "persist_dir": path}

    if settings.demo_mode:
        return {"mode": "fixture", "path": path}

    if kind in POSTGRES_BACKED_KINDS:
        return {
            "mode": "production",
            "connection": settings.data.postgres_url,
            "table": kind,
        }

    if kind in STATIC_CONFIG_KINDS:
        return {"mode": "static_config", "path": path}

    raise ValueError(f"unknown data source kind: {kind!r}")


def validate_runtime_config(strict: bool = False) -> list[str]:
    issues: list[str] = []
    if sys.version_info < (3, 11):
        issues.append("[ERROR] Python 3.11+ is required")
    for module in ("flask", "httpx", "resend", "pypdf", "tzdata", "strands"):
        if importlib.util.find_spec(module) is None:
            issues.append(
                f"[ERROR] runtime dependency missing: {module}; install requirements.txt"
            )

    if settings.demo_mode:
        for label, path in (
            ("customer_history", settings.data.customer_history_json),
            ("external_data", settings.data.external_data_json),
            ("cases", settings.data.cases_json),
        ):
            if not path.exists():
                issues.append(
                    f"[ERROR] DEMO_MODE=true but fixture missing: {label} -> {path}"
                )
    else:
        issues.append(
            "[WARN] Case persistence currently uses local JSON files; POSTGRES_URL does not enable a database-backed workflow"
        )


    if not settings.api_keys.qwen:
        issues.append("[WARN] no Qwen API key set; every LLM-backed agent will fail")
    if not os.getenv("PAYSTACK_SECRET_KEY"):
        issues.append(
            "[WARN] PAYSTACK_SECRET_KEY is missing; card checkout and settlement are unavailable"
        )
    if not os.getenv("RECOVERLY_PAYLINK_BASE"):
        issues.append(
            "[WARN] RECOVERLY_PAYLINK_BASE is missing; email payment links use a placeholder domain"
        )
    if not os.getenv("RESEND_API_KEY") and not os.getenv("SMTP_APP_PASSWORD"):
        issues.append("[WARN] no email delivery credentials are configured")

    if strict:
        errors = [issue for issue in issues if issue.startswith("[ERROR]")]
        if errors:
            raise RuntimeError("config validation failed:\n  " + "\n  ".join(errors))

    return issues
