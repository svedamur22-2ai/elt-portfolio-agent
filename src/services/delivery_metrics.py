"""Deterministic delivery risk detection (Section 8). Pure Python throughout
— mirrors financial_metrics.py's shape exactly: individual detectors return
`Optional[Risk]`, `classify_delivery_risk` combines whichever fired into one
overall Risk with the highest severity among them.

Three of config/risk_rules.yaml's `delivery_risk` conditions are NOT
implemented here, on purpose, because this codebase has no data source for
them yet — implementing them would mean fabricating a signal:

- `milestone_overdue` / `milestones_on_schedule`: no model anywhere (Project,
  Sprint, JiraIssue) carries a milestone concept. Section 3 never defined
  one, so there's nothing to check against.
- `sprint_count_blocked_at_least`: requires >=2 historical ProjectSnapshots
  per Accuracy Check 6 (Section 12) — Mem0 doesn't exist until Phase 7.
  `detect_blocked_multiple_sprints` below is written against
  `JiraIssue.sprint_count_blocked` so it activates automatically the moment
  that field starts being populated, without this module changing.
- `repeated_carryover_sprints_at_least`: same limitation — carryover
  persistence needs history, not a single snapshot.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

import yaml

from src.models.common import RiskSeverity
from src.models.issue import JiraIssue
from src.models.risk import Risk
from src.models.sprint import Sprint

DEFAULT_RULES_PATH = "config/risk_rules.yaml"

RESOLVED_STATUSES = {"DONE"}


def load_delivery_risk_rules(path: str = DEFAULT_RULES_PATH) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["delivery_risk"]


def _risk_id(project_id: str, suffix: str) -> str:
    return f"DEL-{project_id}-{suffix}-{uuid4().hex[:8]}"


def detect_low_sprint_completion(project_id: str, sprint: Optional[Sprint], rules: dict) -> Optional[Risk]:
    """HIGH below `high.sprint_completion_pct_below`, MEDIUM within
    `medium.sprint_completion_pct_between`, else None. `sprint=None` (no
    closed/active sprint found for this project) is a data-availability gap,
    not a health signal — returns None rather than treating "no sprint" as
    "0% complete"."""
    if sprint is None or sprint.completion_pct is None:
        return None

    pct = sprint.completion_pct
    high_below = rules["high"]["sprint_completion_pct_below"]
    medium_range = rules["medium"]["sprint_completion_pct_between"]

    if pct < high_below:
        severity = RiskSeverity.HIGH
    elif medium_range[0] <= pct < medium_range[1]:
        severity = RiskSeverity.MEDIUM
    else:
        return None

    return Risk(
        risk_id=_risk_id(project_id, "COMPLETION"),
        project_id=project_id,
        category="Delivery",
        severity=severity,
        description=f"Sprint completion ({pct:.1f}%) for {sprint.sprint_id} is below the healthy threshold",
        evidence=[
            f"sprint_id={sprint.sprint_id}",
            f"completion_pct={pct:.1f}%",
            f"committed_story_points={sprint.committed_story_points}",
            f"completed_story_points={sprint.completed_story_points}",
        ],
        recommended_action="Review sprint scope and team capacity before committing similar scope next sprint.",
        reason_codes=["LOW_SPRINT_COMPLETION"],
    )


def detect_aged_blocker(project_id: str, issues: list[JiraIssue], rules: dict) -> Optional[Risk]:
    """HIGH if any blocked issue's age exceeds
    `high.critical_blocker_open_days_above`; MEDIUM if the oldest falls
    within `medium.blocker_open_days_between`; else None. Uses the MAX age
    among blocked issues — one sufficiently old blocker is enough to raise
    the whole project's delivery risk, regardless of how many other
    blockers exist."""
    aged = [i for i in issues if i.blocked and i.blocker_age_days is not None]
    if not aged:
        return None

    high_threshold = rules["high"]["critical_blocker_open_days_above"]
    medium_low, medium_high = rules["medium"]["blocker_open_days_between"]

    oldest = max(aged, key=lambda i: i.blocker_age_days)
    if oldest.blocker_age_days > high_threshold:
        severity = RiskSeverity.HIGH
    elif medium_low <= oldest.blocker_age_days <= medium_high:
        severity = RiskSeverity.MEDIUM
    else:
        return None

    culprits = sorted(aged, key=lambda i: -i.blocker_age_days)[:5]
    return Risk(
        risk_id=_risk_id(project_id, "BLOCKER"),
        project_id=project_id,
        category="Delivery",
        severity=severity,
        description=f"{len(aged)} blocked issue(s), oldest open {oldest.blocker_age_days} days ({oldest.issue_key})",
        evidence=[f"{i.issue_key}: blocked {i.blocker_age_days} days ({i.blocker_reason})" for i in culprits],
        recommended_action="Escalate the oldest blockers for owner/dependency resolution this week.",
        reason_codes=["AGED_BLOCKER"],
    )


def detect_unresolved_dependencies(project_id: str, issues: list[JiraIssue], rules: dict) -> Optional[Risk]:
    """MEDIUM when an active (non-Done) issue depends on another issue that
    is itself not yet Done. Looks the dependency up among the same issue
    set fetched for this project — a dependency on an issue outside that set
    (a cross-project dependency) can't be resolved here and is reported
    separately as "unverifiable", never assumed resolved."""
    by_key = {i.issue_key: i for i in issues}
    findings: list[tuple[JiraIssue, str, str]] = []  # (issue, dep_key, dep_state)

    for issue in issues:
        if issue.status in RESOLVED_STATUSES or not issue.linked_dependencies:
            continue
        for dep_key in issue.linked_dependencies:
            dep = by_key.get(dep_key)
            if dep is None:
                findings.append((issue, dep_key, "unverifiable (not in fetched issue set)"))
            elif dep.status not in RESOLVED_STATUSES:
                findings.append((issue, dep_key, f"not done (status={dep.status})"))

    if not findings:
        return None

    return Risk(
        risk_id=_risk_id(project_id, "DEPENDENCY"),
        project_id=project_id,
        category="Delivery",
        severity=RiskSeverity.MEDIUM,
        description=f"{len(findings)} active issue(s) blocked on an unresolved dependency",
        evidence=[f"{issue.issue_key} depends on {dep_key}: {state}" for issue, dep_key, state in findings[:5]],
        recommended_action="Sequence or reprioritize the depended-upon issues before their dependents can progress.",
        reason_codes=["DEPENDENCY_RISK"],
    )


def detect_blocked_multiple_sprints(project_id: str, issues: list[JiraIssue], rules: dict) -> Optional[Risk]:
    """HIGH when `JiraIssue.sprint_count_blocked >= high.sprint_count_blocked_at_least`.
    Currently a no-op on every issue in this codebase (the field is always
    `None` until Phase 7's Mem0 history exists — see this module's
    docstring) — written now so classify_delivery_risk doesn't need to
    change when that field starts being populated."""
    threshold = rules["high"]["sprint_count_blocked_at_least"]
    culprits = [i for i in issues if i.sprint_count_blocked is not None and i.sprint_count_blocked >= threshold]
    if not culprits:
        return None
    return Risk(
        risk_id=_risk_id(project_id, "MULTISPRINT"),
        project_id=project_id,
        category="Delivery",
        severity=RiskSeverity.HIGH,
        description=f"{len(culprits)} issue(s) blocked across {threshold}+ sprints",
        evidence=[f"{i.issue_key}: blocked across {i.sprint_count_blocked} sprints" for i in culprits],
        recommended_action="Escalate to ELT — a blocker persisting across multiple sprints usually needs authority this team doesn't have.",
        reason_codes=["BLOCKED_MULTIPLE_SPRINTS"],
    )


_SEVERITY_ORDER = {RiskSeverity.LOW: 0, RiskSeverity.MEDIUM: 1, RiskSeverity.HIGH: 2}


def classify_delivery_risk(
    project_id: str,
    issues: list[JiraIssue],
    latest_sprint: Optional[Sprint],
    rules: Optional[dict] = None,
) -> Risk:
    """Combines every detector above into one overall Delivery Risk, same
    pattern as financial_metrics.classify_financial_risk: severity = the
    highest among whichever fired, LOW with no evidence when none did."""
    rules = rules or load_delivery_risk_rules()

    findings = [
        f
        for f in (
            detect_low_sprint_completion(project_id, latest_sprint, rules),
            detect_aged_blocker(project_id, issues, rules),
            detect_unresolved_dependencies(project_id, issues, rules),
            detect_blocked_multiple_sprints(project_id, issues, rules),
        )
        if f is not None
    ]

    if not findings:
        blocked_count = sum(1 for i in issues if i.blocked)
        return Risk(
            risk_id=_risk_id(project_id, "OVERALL"),
            project_id=project_id,
            category="Delivery",
            severity=RiskSeverity.LOW,
            description="No delivery risk thresholds triggered",
            evidence=[
                f"completion_pct={latest_sprint.completion_pct:.1f}%" if latest_sprint and latest_sprint.completion_pct is not None else "completion_pct=N/A",
                f"blocked_issue_count={blocked_count}",
            ],
            reason_codes=[],
        )

    overall_severity = max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])
    combined_evidence = [line for f in findings for line in f.evidence]
    combined_reasons = sorted({code for f in findings for code in f.reason_codes})
    combined_actions = "; ".join(sorted({f.recommended_action for f in findings if f.recommended_action}))

    return Risk(
        risk_id=_risk_id(project_id, "OVERALL"),
        project_id=project_id,
        category="Delivery",
        severity=overall_severity,
        description=f"{len(findings)} delivery risk condition(s) triggered: " + ", ".join(combined_reasons),
        evidence=combined_evidence,
        recommended_action=combined_actions or None,
        reason_codes=combined_reasons,
    )
