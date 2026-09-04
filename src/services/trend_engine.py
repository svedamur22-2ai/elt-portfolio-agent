"""Week-over-week comparison and multi-sprint blocker persistence
(Sections 12, 14). Pure Python over `ProjectSnapshot` history — this is
where Accuracy Check 6 ("require >=2 historical snapshots before claiming
persistence") is actually enforced, not just documented.
"""

from __future__ import annotations

from typing import Optional

import yaml
from pydantic import BaseModel

from src.models.common import RAGStatus, TrendDirection
from src.models.snapshot import ProjectSnapshot

DEFAULT_RULES_PATH = "config/risk_rules.yaml"

_RAG_ORDER = {RAGStatus.GREEN: 0, RAGStatus.AMBER: 1, RAGStatus.RED: 2}


def load_trend_thresholds(path: str = DEFAULT_RULES_PATH) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["trend_thresholds"]


def load_blocked_multiple_sprints_threshold(path: str = DEFAULT_RULES_PATH) -> int:
    """Reuses delivery_risk.high.sprint_count_blocked_at_least — the same
    number that decides BLOCKED_MULTIPLE_SPRINTS in delivery_metrics.py, so
    "stuck for more than one sprint" means the same thing everywhere in this
    codebase, not two independently-tuned thresholds."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["delivery_risk"]["high"]["sprint_count_blocked_at_least"]


class SnapshotDiff(BaseModel):
    blocked_issue_delta: Optional[int] = None
    sprint_completion_delta: Optional[float] = None
    budget_consumption_delta: Optional[float] = None
    remaining_budget_delta: Optional[float] = None
    risk_score_delta: Optional[float] = None
    open_issue_delta: Optional[int] = None
    rag_status_change: Optional[str] = None
    """e.g. "AMBER -> RED"; None when unchanged."""


def diff_snapshots(previous: ProjectSnapshot, current: ProjectSnapshot) -> SnapshotDiff:
    def delta(before, after):
        return (after - before) if (before is not None and after is not None) else None

    rag_change = (
        f"{previous.rag_status.value} -> {current.rag_status.value}"
        if previous.rag_status != current.rag_status
        else None
    )

    return SnapshotDiff(
        blocked_issue_delta=delta(previous.blocked_issues, current.blocked_issues),
        sprint_completion_delta=delta(previous.sprint_completion_pct, current.sprint_completion_pct),
        budget_consumption_delta=delta(previous.budget_consumption_pct, current.budget_consumption_pct),
        remaining_budget_delta=delta(previous.remaining_budget, current.remaining_budget),
        risk_score_delta=delta(previous.risk_score, current.risk_score),
        open_issue_delta=delta(previous.open_issues, current.open_issues),
        rag_status_change=rag_change,
    )


def classify_trend(
    previous: Optional[ProjectSnapshot], current: ProjectSnapshot, thresholds: Optional[dict] = None
) -> TrendDirection:
    """BASELINE whenever `previous` is None — Accuracy Check 6 forbids
    inferring a trend from a single data point, no matter how bad or good
    that one snapshot looks.

    Otherwise: a RAG status that got strictly worse or better is decisive
    on its own (it's already the combined judgment of both risk domains).
    Only when RAG is unchanged do the underlying deltas get compared
    against `trend_thresholds` to catch movement a stable RAG can hide —
    e.g. a project that's still RED but whose sprint completion just jumped
    25% -> 80% is meaningfully IMPROVING even though the label didn't
    move."""
    if previous is None:
        return TrendDirection.BASELINE

    thresholds = thresholds or load_trend_thresholds()
    diff = diff_snapshots(previous, current)

    if previous.rag_status in _RAG_ORDER and current.rag_status in _RAG_ORDER:
        rag_delta = _RAG_ORDER[current.rag_status] - _RAG_ORDER[previous.rag_status]
        if rag_delta > 0:
            return TrendDirection.DETERIORATING
        if rag_delta < 0:
            return TrendDirection.IMPROVING

    completion_threshold = thresholds["significant_completion_delta_pct"]
    consumption_threshold = thresholds["significant_consumption_delta_pct"]

    bad_signals = sum(
        [
            diff.sprint_completion_delta is not None and diff.sprint_completion_delta <= -completion_threshold,
            diff.budget_consumption_delta is not None and diff.budget_consumption_delta >= consumption_threshold,
            diff.blocked_issue_delta is not None and diff.blocked_issue_delta > 0,
        ]
    )
    good_signals = sum(
        [
            diff.sprint_completion_delta is not None and diff.sprint_completion_delta >= completion_threshold,
            diff.budget_consumption_delta is not None and diff.budget_consumption_delta <= -consumption_threshold,
            diff.blocked_issue_delta is not None and diff.blocked_issue_delta < 0,
        ]
    )

    if bad_signals > good_signals:
        return TrendDirection.DETERIORATING
    if good_signals > bad_signals:
        return TrendDirection.IMPROVING
    return TrendDirection.STABLE


def compute_sprint_count_blocked(issue_key: str, snapshots: list[ProjectSnapshot]) -> Optional[int]:
    """Accuracy Check 6 / Section 12: requires >=2 snapshots — returns
    `None` (not 0) otherwise, since with fewer than two data points
    "how many sprints has this been blocked" genuinely cannot be answered.

    Counts consecutive most-recent snapshots (working backward from the
    latest) in which `issue_key` appears in `major_blockers`. A gap breaks
    the streak on purpose: an issue blocked, resolved, then blocked again
    later is two separate blocking incidents, not one three-sprint-long
    one."""
    if len(snapshots) < 2:
        return None
    ordered = sorted(snapshots, key=lambda s: s.snapshot_date)
    count = 0
    for snapshot in reversed(ordered):
        if issue_key in snapshot.major_blockers:
            count += 1
        else:
            break
    return count


def find_persistent_blockers(
    snapshots: list[ProjectSnapshot], min_sprints: Optional[int] = None
) -> list[str]:
    """Section 12's "what has been stuck for more than one sprint" query,
    computed only from stored snapshot history — never from the current
    Jira pull alone. Returns `[]` (not an error) when fewer than 2
    snapshots exist, since Accuracy Check 6 makes that an honest "cannot
    determine yet" rather than "nothing is stuck"."""
    if len(snapshots) < 2:
        return []
    min_sprints = min_sprints if min_sprints is not None else load_blocked_multiple_sprints_threshold()
    latest = max(snapshots, key=lambda s: s.snapshot_date)
    return [
        issue_key
        for issue_key in latest.major_blockers
        if (compute_sprint_count_blocked(issue_key, snapshots) or 0) >= min_sprints
    ]
