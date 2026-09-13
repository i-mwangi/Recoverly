from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from src.config import settings

Mode = Literal["in_spot", "lite", "attorney_recommended"]

ATTORNEY_DEMAND_LETTER_USD: Final = 480
LITE_MODE_CARD: Final = "09_lite_mode_2button.json"
ATTORNEY_PATH_CARD: Final = "10_attorney_path_2button.json"


@dataclass(frozen=True, slots=True)
class PathDecision:
    mode: Mode
    outstanding_balance_usd: float
    gross_amount_usd: float
    deposit_pct: float
    band_label: str
    requires_concierge_approval: bool
    concierge_card_template: str | None
    cost_pct_of_outstanding: float | None
    rationale: str


def derive_outstanding(gross_amount_usd: float, deposit_pct: float) -> float:
    if gross_amount_usd <= 0:
        raise ValueError(f"gross_amount_usd must be positive, got {gross_amount_usd}")
    if not 0.0 <= deposit_pct < 1.0:
        raise ValueError(f"deposit_pct must be in [0, 1), got {deposit_pct}")
    return round(gross_amount_usd * (1.0 - deposit_pct), 2)


def _amounts_line(outstanding: float, gross: float, deposit_pct: float) -> str:
    return (
        f"Outstanding ${outstanding:,.0f} "
        f"(gross ${gross:,.0f}, deposit {deposit_pct:.0%})."
    )


def _lite_decision(outstanding: float, gross: float, deposit_pct: float, floor: float) -> PathDecision:
    cost_pct = round((ATTORNEY_DEMAND_LETTER_USD / outstanding) * 100, 1)
    return PathDecision(
        mode="lite",
        outstanding_balance_usd=outstanding,
        gross_amount_usd=gross,
        deposit_pct=deposit_pct,
        band_label=f"< ${floor:,.0f} outstanding (below sweet spot)",
        requires_concierge_approval=True,
        concierge_card_template=LITE_MODE_CARD,
        cost_pct_of_outstanding=cost_pct,
        rationale=(
            f"{_amounts_line(outstanding, gross, deposit_pct)} "
            f"A single ${ATTORNEY_DEMAND_LETTER_USD} attorney demand letter would consume "
            f"{cost_pct:.0f}% of the outstanding. Recommend Lite Mode (cadence only) "
            f"or write off with a template notice."
        ),
    )


def _attorney_decision(outstanding: float, gross: float, deposit_pct: float, ceiling: float) -> PathDecision:
    return PathDecision(
        mode="attorney_recommended",
        outstanding_balance_usd=outstanding,
        gross_amount_usd=gross,
        deposit_pct=deposit_pct,
        band_label=f"> ${ceiling:,.0f} outstanding (above sweet spot)",
        requires_concierge_approval=True,
        concierge_card_template=ATTORNEY_PATH_CARD,
        cost_pct_of_outstanding=None,
        rationale=(
            f"{_amounts_line(outstanding, gross, deposit_pct)} "
            f"At this size a contingency attorney's fee typically pays for itself given "
            f"cross-border enforcement complexity. Recommend either Run+Tag or Get Referrals."
        ),
    )


def _in_spot_decision(
    outstanding: float, gross: float, deposit_pct: float, floor: float, ceiling: float
) -> PathDecision:
    return PathDecision(
        mode="in_spot",
        outstanding_balance_usd=outstanding,
        gross_amount_usd=gross,
        deposit_pct=deposit_pct,
        band_label=f"${floor:,.0f}-${ceiling:,.0f} outstanding (sweet spot)",
        requires_concierge_approval=False,
        concierge_card_template=None,
        cost_pct_of_outstanding=None,
        rationale=(
            f"{_amounts_line(outstanding, gross, deposit_pct)} "
            f"This falls in the sweet spot, so the full agent flow runs at zero commission "
            f"and no operator approval is required to start."
        ),
    )


def select_path(
    outstanding_balance_usd: float,
    *,
    gross_amount_usd: float | None = None,
    deposit_pct: float = 0.0,
    sweet_spot_min: float | None = None,
    sweet_spot_max: float | None = None,
) -> PathDecision:
    if outstanding_balance_usd <= 0:
        raise ValueError(
            f"outstanding_balance_usd must be positive, got {outstanding_balance_usd}"
        )

    floor = float(sweet_spot_min if sweet_spot_min is not None else settings.sweet_spot_min_usd)
    ceiling = float(sweet_spot_max if sweet_spot_max is not None else settings.sweet_spot_max_usd)
    if floor > ceiling:
        raise ValueError(f"sweet spot bounds inverted: {floor} > {ceiling}")

    gross = outstanding_balance_usd if gross_amount_usd is None else gross_amount_usd

    if outstanding_balance_usd < floor:
        return _lite_decision(outstanding_balance_usd, gross, deposit_pct, floor)
    if outstanding_balance_usd > ceiling:
        return _attorney_decision(outstanding_balance_usd, gross, deposit_pct, ceiling)
    return _in_spot_decision(outstanding_balance_usd, gross, deposit_pct, floor, ceiling)


def select_path_from_gross(
    gross_amount_usd: float,
    deposit_pct: float = 0.0,
    **kwargs: float | None,
) -> PathDecision:
    outstanding = derive_outstanding(gross_amount_usd, deposit_pct)
    return select_path(
        outstanding,
        gross_amount_usd=gross_amount_usd,
        deposit_pct=deposit_pct,
        **kwargs,
    )
