"""The unit of historical memory (Section 11). One ProjectSnapshot is written
per project per reporting week; the trend engine (Section 14) diffs
consecutive snapshots for the same project_id. Mem0 stores the normalized
facts derived from this model — never the raw Jira/Finance payloads."""

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.utils.time import utcnow

from .common import RAGStatus


class ProjectSnapshot(BaseModel):
    snapshot_date: date
    project_id: str
    sprint_id: Optional[str] = None

    open_issues: Optional[int] = None
    """None when the project has no Jira mapping (Section 6) — not 0.
    `overdue_issues` is None unconditionally: no model anywhere (Project,
    Sprint, JiraIssue) carries a due-date field, so this is a permanent
    data-availability gap, not a per-project mapping gap — see Phase 6's
    delivery_metrics.py docstring for the same reasoning applied to
    `milestone_overdue`."""
    blocked_issues: Optional[int] = None
    overdue_issues: Optional[int] = None

    sprint_completion_pct: Optional[float] = None
    delivery_progress_pct: Optional[float] = None

    approved_budget: Optional[float] = None
    actual_spend: Optional[float] = None
    remaining_budget: Optional[float] = None
    budget_consumption_pct: Optional[float] = None

    risk_score: Optional[float] = None
    rag_status: RAGStatus = RAGStatus.UNKNOWN
    major_blockers: list[str] = Field(default_factory=list)
    """issue_keys, not free text — keeps blocker history traceable/joinable."""

    source_timestamp: datetime = Field(default_factory=utcnow)
