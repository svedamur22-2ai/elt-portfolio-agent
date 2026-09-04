"""Financial record + deterministic arithmetic (Section 5).

These functions are the ONLY place remaining_budget / consumption_pct /
forecast_variance get computed. The LLM must never be asked to recompute
these — nodes pass the already-calculated FinancialRecord into the graph
state and only narrate it.
"""

import calendar
import re
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from src.utils.time import utcnow

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class FinancialRecord(BaseModel):
    project_id: str
    reporting_period: str
    """"YYYY-MM" — a monthly close, not an arbitrary date. Real financial
    sources report in discrete periods; forcing this to a `date` would
    silently invite the question "which day of the month?" that the source
    never actually answers."""

    approved_budget: float
    actual_spend: float
    committed_spend: float
    forecast_spend: float
    currency: str = "USD"

    remaining_budget: Optional[float] = None
    budget_consumption_pct: Optional[float] = None
    forecast_variance: Optional[float] = None

    source_reported_remaining_budget: Optional[float] = None
    """The source system's OWN pre-computed remaining-budget figure, when it
    provides one — kept purely for reconciliation (services/reconciliation.py
    detect_reconciliation_failure). Never used as the authoritative
    remaining_budget: some source systems compute it with a different,
    incomplete formula (e.g. ignoring committed_spend), and the whole point
    of Section 5 is that THIS pipeline's formula is the one used everywhere
    downstream, with any disagreement surfaced as a finding, not silently
    preferred one way or the other."""

    retrieved_timestamp: datetime = Field(default_factory=utcnow)

    @field_validator("reporting_period")
    @classmethod
    def _valid_period_format(cls, v: str) -> str:
        if not _PERIOD_RE.match(v):
            raise ValueError(f"reporting_period must be 'YYYY-MM', got {v!r}")
        return v

    @field_validator("approved_budget", "actual_spend", "committed_spend")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("financial amounts must be >= 0 (Accuracy Check 4)")
        return v

    def with_calculated_fields(self) -> "FinancialRecord":
        """Returns a copy with remaining_budget / consumption_pct / variance
        filled in. Kept as an explicit, callable step (rather than a
        validator) so the audit trail can show "raw record" vs
        "post-calculation record" as two distinct stages (Section 2)."""
        remaining = self.approved_budget - self.actual_spend - self.committed_spend
        consumption_pct = (
            (self.actual_spend / self.approved_budget) * 100.0
            if self.approved_budget > 0
            else None  # divide-by-zero guard — never coerced to 0 or 100
        )
        variance = self.approved_budget - self.forecast_spend
        return self.model_copy(
            update={
                "remaining_budget": remaining,
                "budget_consumption_pct": consumption_pct,
                "forecast_variance": variance,
            }
        )


def period_end_date(reporting_period: str) -> date:
    """"2026-01" -> date(2026, 1, 31). Used for freshness/staleness
    comparisons, which need an actual date, not a "YYYY-MM" string."""
    year, month = (int(part) for part in reporting_period.split("-"))
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def spend_to_progress_ratio(
    budget_consumption_pct: Optional[float], delivery_progress_pct: Optional[float]
) -> Optional[float]:
    """spend_to_progress_ratio = budget_consumption_pct / delivery_progress_pct

    Returns None when delivery_progress_pct is 0 or unavailable — a project
    with 0% delivery progress and any spend at all is already a HIGH
    financial risk by the `forecast_exceeds_approved` / consumption rules;
    this function must not raise or silently return infinity."""
    if not delivery_progress_pct or budget_consumption_pct is None:
        return None
    return budget_consumption_pct / delivery_progress_pct
