"""Unified Project model — the join point between Jira and Finance (Section 3).

Populated by the normalization layer, never constructed by hand from raw
source dicts. `risk_status` and every `*_pct` field are filled in later by
services/sprint_metrics.py, services/financial_metrics.py, and
services/risk_engine.py (Accuracy Check 5) — the connectors only ever
produce the raw, unmapped `*_status = None` fields.
"""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from src.utils.time import utcnow

from .common import RAGStatus, SourceProvenance


class Project(BaseModel):
    project_id: str
    project_key: str
    project_name: str
    project_manager: str
    business_owner: str
    technical_owner: str
    project_status: str

    start_date: Optional[date] = None
    target_end_date: Optional[date] = None

    jira_project_key: Optional[str] = None
    """None when this project has no Jira mapping (Section 6)."""

    approved_budget: Optional[float] = None
    actual_spend: Optional[float] = None
    committed_spend: Optional[float] = None
    forecast_spend: Optional[float] = None
    remaining_budget: Optional[float] = None
    currency: str = "USD"

    budget_consumption_pct: Optional[float] = None
    delivery_progress_pct: Optional[float] = None

    risk_status: RAGStatus = RAGStatus.UNKNOWN

    financial_status: Optional[str] = None
    """"UNKNOWN" when finance_project_id has no mapping — set explicitly by
    the normalizer, distinct from risk_status."""

    delivery_status: Optional[str] = None
    """"UNKNOWN" when jira_project_key has no mapping."""

    last_updated: datetime = Field(default_factory=utcnow)

    provenance: list[SourceProvenance] = Field(default_factory=list)

    @field_validator("approved_budget", "actual_spend", "committed_spend")
    @classmethod
    def _non_negative(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0:
            raise ValueError("financial amounts must be >= 0 (Accuracy Check 4)")
        return v
