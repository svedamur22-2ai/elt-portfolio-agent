from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field

from src.utils.time import utcnow


class JiraIssue(BaseModel):
    issue_key: str
    project_id: str
    sprint_id: Optional[str] = None

    summary: str
    issue_type: str
    status: str
    """Canonical status per config/status_mapping.yaml (Section 4)."""
    raw_status: Optional[str] = None
    """Status exactly as returned by Jira, kept for audit trail (Section 2)."""

    priority: Optional[str] = None
    assignee: Optional[str] = None

    created_date: Optional[date] = None
    updated_date: Optional[date] = None
    resolution_date: Optional[date] = None

    story_points: Optional[float] = None

    blocked: bool = False
    blocker_reason: Optional[str] = None
    blocker_age_days: Optional[int] = None
    """Calculated as (report_date - updated_date) for the currently-blocked
    flag. This is a proxy until Phase 3's get_issue_history() supplies a real
    changelog-derived "became blocked at" timestamp — documented here so the
    approximation is never mistaken for ground truth."""

    sprint_count_blocked: Optional[int] = None
    """Requires >= 2 historical ProjectSnapshots to compute (Accuracy Check
    6). None (not 0 or 1) until enough history exists — the graph must not
    infer multi-sprint persistence from a single snapshot."""

    linked_dependencies: list[str] = Field(default_factory=list)

    retrieved_timestamp: datetime = Field(default_factory=utcnow)
