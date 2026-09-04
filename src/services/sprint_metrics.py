"""Sprint-level aggregation and cross-sprint identity checks.

Everything here is deterministic Python over already-normalized `JiraIssue`
objects (Accuracy Check 5) — no LLM, no guessing. Two things this module is
specifically responsible for getting right:

1. **Carryover by identity, not text** (Section 9 / master-prompt
   requirement). `detect_carryover_issues` only ever compares `issue_key`
   sets. `naive_summary_carryover` exists purely as a labeled anti-pattern
   reference so tests/notebooks can show *why* text-matching is wrong on
   this dataset, not as something calling code should use.
2. **Blocker age as a disclosed proxy** (see `src/models/issue.py`). This
   dataset has no changelog, so "how long has this been blocked" is
   approximated as `as_of - updated_date`. `calculate_blocker_age_days`
   takes `as_of` explicitly rather than defaulting silently, so every call
   site has to consciously pick a reference date instead of it quietly being
   "whenever this function happened to run."
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.models.issue import JiraIssue
from src.models.sprint import Sprint
from src.services.jira_normalizer import NormalizedJiraData

DEFAULT_RESOLVED_STATUSES = {"DONE"}


def derive_sprint_status(start_date: Optional[date], end_date: Optional[date], as_of: date) -> str:
    """The CSV export has no sprint-state field (a live Jira Agile REST
    pull would return `state: active|closed|future` directly from
    `/rest/agile/1.0/sprint/{id}` — use that instead of this function once
    Phase 3's live source exists). Derived here from dates as a disclosed
    approximation, not presented as ground truth from Jira."""
    if start_date is None or end_date is None:
        return "UNKNOWN"
    if start_date <= as_of <= end_date:
        return "ACTIVE"
    if as_of > end_date:
        return "CLOSED"
    return "TODO"


def calculate_blocker_age_days(updated_date: Optional[date], as_of: date) -> Optional[int]:
    if updated_date is None:
        return None
    return (as_of - updated_date).days


def apply_blocker_ages(issues: list[JiraIssue], as_of: date) -> list[JiraIssue]:
    """Returns a new list — never mutates the input, consistent with the
    graph-state ownership rule (src/graph/state.py)."""
    result = []
    for issue in issues:
        if issue.blocked:
            age = calculate_blocker_age_days(issue.updated_date, as_of)
            issue = issue.model_copy(update={"blocker_age_days": age})
        result.append(issue)
    return result


def apply_sprint_count_blocked(issues: list[JiraIssue], snapshots: list) -> list[JiraIssue]:
    """Populates `JiraIssue.sprint_count_blocked` from historical
    `ProjectSnapshot`s (Phase 7) via `trend_engine.compute_sprint_count_blocked`
    — the field `delivery_metrics.detect_blocked_multiple_sprints` was
    written against back in Phase 6, before any history existed to fill it.
    Only touches currently-blocked issues; non-blocked issues keep
    `sprint_count_blocked=None` since the count is only meaningful for an
    issue that's blocked right now. Returns a new list, never mutates the
    input (same rule as `apply_blocker_ages`).

    `snapshots` is typed as `list` rather than `list[ProjectSnapshot]` to
    avoid importing trend_engine/models.snapshot at module load time purely
    for a type hint — the deferred import below is the actual dependency."""
    from src.services import trend_engine  # deferred: avoids a needless import cycle at module load

    result = []
    for issue in issues:
        if issue.blocked:
            count = trend_engine.compute_sprint_count_blocked(issue.issue_key, snapshots)
            issue = issue.model_copy(update={"sprint_count_blocked": count})
        result.append(issue)
    return result


def build_sprint(
    sprint_id: str,
    project_id: str,
    sprint_name: str,
    start_date: Optional[date],
    end_date: Optional[date],
    issues: list[JiraIssue],
    as_of: date,
    resolved_statuses: set[str] = DEFAULT_RESOLVED_STATUSES,
) -> Sprint:
    committed = sum(i.story_points for i in issues if i.story_points is not None)
    completed = sum(
        i.story_points for i in issues if i.story_points is not None and i.status in resolved_statuses
    )
    carryover = committed - completed
    completion_pct = (completed / committed * 100.0) if committed > 0 else None

    return Sprint(
        sprint_id=sprint_id,
        sprint_name=sprint_name,
        project_id=project_id,
        start_date=start_date or as_of,
        end_date=end_date or as_of,
        sprint_status=derive_sprint_status(start_date, end_date, as_of),
        committed_story_points=committed,
        completed_story_points=completed,
        completion_pct=completion_pct,
        carryover_story_points=carryover,
    )


def build_sprints(normalized: NormalizedJiraData, as_of: date) -> list[Sprint]:
    issues_by_sprint: dict[str, list[JiraIssue]] = {}
    for issue in normalized.issues:
        if issue.sprint_id is None:
            continue
        issues_by_sprint.setdefault(issue.sprint_id, []).append(issue)

    sprints = []
    for sprint_id, issues in issues_by_sprint.items():
        meta = normalized.sprint_meta.get(sprint_id)
        sprints.append(
            build_sprint(
                sprint_id=sprint_id,
                project_id=meta.project_id if meta else (issues[0].project_id if issues else ""),
                sprint_name=meta.sprint_name if meta else sprint_id,
                start_date=meta.start_date if meta else None,
                end_date=meta.end_date if meta else None,
                issues=issues,
                as_of=as_of,
            )
        )
    return sprints


def overall_delivery_progress_pct(
    issues: list[JiraIssue], resolved_statuses: set[str] = DEFAULT_RESOLVED_STATUSES
) -> Optional[float]:
    """Story-point-weighted completion across ALL of a project's issues to
    date (every sprint, not just the most recent one) — the denominator
    Project.delivery_progress_pct and the financial layer's
    spend_to_progress_ratio need, since spend accumulates against the whole
    project, not against whichever sprint happens to be current.

    None when no issue in the project carries story points at all, never 0
    — a project with zero story-pointed work has no measurable progress
    signal, which is a different fact than "measured at 0% complete"."""
    committed = sum(i.story_points for i in issues if i.story_points is not None)
    if committed == 0:
        return None
    completed = sum(
        i.story_points for i in issues if i.story_points is not None and i.status in resolved_statuses
    )
    return completed / committed * 100.0


def detect_carryover_issues(previous_sprint_issues: list[JiraIssue], current_sprint_issues: list[JiraIssue]) -> list[str]:
    """Issue-identity carryover (requirement 9): an issue "carried over"
    only if the exact same `issue_key` appears in both sprints. Deliberately
    ignores `summary` entirely — see `naive_summary_carryover` below for why
    that matters on this dataset.

    In a single flat snapshot (this sample data), no issue_key can appear
    under two different sprint_ids — Jira moves an unresolved issue's sprint
    field forward rather than duplicating the row — so this correctly
    returns [] here. It becomes meaningful once either (a) `get_issue_history`
    (Phase 3 live source) exposes real sprint-field transitions, or (b) two
    successive pulls of the same still-open issue are compared.
    """
    previous_keys = {i.issue_key for i in previous_sprint_issues}
    current_keys = {i.issue_key for i in current_sprint_issues}
    return sorted(previous_keys & current_keys)


def naive_summary_carryover(previous_sprint_issues: list[JiraIssue], current_sprint_issues: list[JiraIssue]) -> list[tuple[str, str]]:
    """ANTI-PATTERN — reference implementation only, never called by
    production code. Matches on `summary` text instead of `issue_key`, which
    is exactly the mistake requirement 9 prohibits. Kept here (tested in
    tests/test_sprint_metrics.py) so the contrast with
    `detect_carryover_issues` is concrete: on this dataset, this function
    reports false positives like (PHX-5, PHX-23) — two distinct tickets that
    happen to share a template-generated summary — while the identity-based
    function correctly reports none.
    """
    previous_by_summary = {i.summary: i.issue_key for i in previous_sprint_issues}
    matches = []
    for issue in current_sprint_issues:
        prior_key = previous_by_summary.get(issue.summary)
        if prior_key is not None and prior_key != issue.issue_key:
            matches.append((prior_key, issue.issue_key))
    return matches
