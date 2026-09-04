"""Assembles a `ProjectSnapshot` from a single run's already-computed facts
— Phase 5's unified `Project`, Phase 6's `RiskAssessment`, and the raw Jira
issues/sprint they were computed from. This is what `persist_snapshot`
(src/graph/nodes.py, Phase 8) will call once the graph exists; for now it's
what notebooks/09_mem0_memory.ipynb and its tests call directly.

`major_blockers` stores EVERY currently-blocked issue_key for the project,
not a curated top-N — that completeness is what makes
`trend_engine.compute_sprint_count_blocked` exact for any issue later, not
just ones that happened to be flagged as "major" at the time.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.models.finance import FinancialRecord
from src.models.issue import JiraIssue
from src.models.project import Project
from src.models.snapshot import ProjectSnapshot
from src.models.sprint import Sprint
from src.services.risk_engine import RiskAssessment, compute_risk_score

RESOLVED_STATUSES = {"DONE"}


def build_snapshot(
    project: Project,
    assessment: RiskAssessment,
    jira_issues: Optional[list[JiraIssue]],
    latest_sprint: Optional[Sprint],
    financial_record: Optional[FinancialRecord],
    snapshot_date: date,
) -> ProjectSnapshot:
    """`financial_record` is taken as an explicit parameter — the same
    already-calculated record `assess_project_risk` was given — rather than
    read off `project.approved_budget` etc. `Project` (Phase 5) always
    reflects whatever the financial source's CURRENT latest period is
    (`get_latest_reporting_period`, no `as_of`), which is exactly right for
    a live weekly run but wrong for replaying a past period: two snapshots
    built from the same `Project` object would silently carry identical
    financial figures even when they're meant to represent different
    months, hiding real week-over-week financial movement instead of
    surfacing it. Taking the record explicitly makes both cases correct
    from the same function.

    `jira_issues=None` (delivery-unmapped project, e.g. Helios) leaves
    `open_issues`/`blocked_issues`/`major_blockers` at their honest
    None/empty defaults rather than fabricating zeros — a project with no
    Jira mapping has "not tracked", not "zero issues". Same reasoning for
    `financial_record=None` (delivery-only project, e.g. QSR) leaving the
    budget fields at None.
    """
    open_issues: Optional[int] = None
    blocked_issues: Optional[int] = None
    major_blockers: list[str] = []

    if jira_issues is not None:
        open_issues = sum(1 for i in jira_issues if i.status not in RESOLVED_STATUSES)
        blocked = [i for i in jira_issues if i.blocked]
        blocked_issues = len(blocked)
        major_blockers = sorted(i.issue_key for i in blocked)

    risk_score = compute_risk_score(
        assessment.delivery_risk.severity if assessment.delivery_risk else None,
        assessment.financial_risk.severity if assessment.financial_risk else None,
    )

    return ProjectSnapshot(
        snapshot_date=snapshot_date,
        project_id=project.project_id,
        sprint_id=latest_sprint.sprint_id if latest_sprint else None,
        open_issues=open_issues,
        blocked_issues=blocked_issues,
        overdue_issues=None,  # no due-date field exists anywhere in this codebase (see ProjectSnapshot docstring)
        sprint_completion_pct=latest_sprint.completion_pct if latest_sprint else None,
        delivery_progress_pct=project.delivery_progress_pct,
        approved_budget=financial_record.approved_budget if financial_record else None,
        actual_spend=financial_record.actual_spend if financial_record else None,
        remaining_budget=financial_record.remaining_budget if financial_record else None,
        budget_consumption_pct=financial_record.budget_consumption_pct if financial_record else None,
        risk_score=risk_score,
        rag_status=assessment.combined_rag,
        major_blockers=major_blockers,
    )
