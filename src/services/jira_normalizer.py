"""Raw Jira dict -> typed Pydantic models (Section 4/6/7/8 of the master
spec; this phase implements the Jira-only slice of it).

Everything here is pure and side-effect free: given raw rows + config, it
returns typed data plus a data-quality report. No network, no file I/O
beyond the config loaders. That's what makes it independently unit-testable
from `src/connectors/jira_client.py`, which owns pagination/retrieval and
calls into this module per page.

Hard rules this module enforces (Section 13's accuracy framework):

- A raw status/blocker value with no entry in status_mapping.yaml never gets
  coerced to the "closest" canonical bucket — it falls back to the
  configured `default_unmapped_status` and is recorded in
  `NormalizationReport.unmapped_statuses` / `unmapped_blocker_values` so it
  surfaces instead of silently vanishing.
- Empty-string source values become `None`, never a fabricated default
  (`assignee` is the sharpest example: "" -> unassigned is a fact worth
  keeping, not "Unassigned" as a string).
- `issue_key` passes through untouched — it is the only identity used
  anywhere downstream (see sprint_metrics.detect_carryover_issues).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

from src.models.issue import JiraIssue
from src.utils.time import utcnow

DEFAULT_FIELD_MAPPING_PATH = "config/jira_field_mapping.yaml"
DEFAULT_STATUS_MAPPING_PATH = "config/status_mapping.yaml"

# Fields we flag when absent. story_points is checked separately since its
# absence is structurally normal for some issue types (see below) rather
# than a data-quality problem.
FIELDS_TO_SURFACE_IF_MISSING = ("assignee", "priority")
STORY_POINT_EXEMPT_TYPES = {"Epic", "Sub-task"}


def load_field_mapping(path: str = DEFAULT_FIELD_MAPPING_PATH) -> dict[str, str]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["fields"]


@dataclass
class StatusMappingConfig:
    status_map: dict[str, str]
    blocker_status_map: dict[str, str]
    resolved_statuses: set[str]
    default_unmapped_status: str


def load_status_mapping(path: str = DEFAULT_STATUS_MAPPING_PATH) -> StatusMappingConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return StatusMappingConfig(
        status_map=data["status_map"],
        blocker_status_map=data["blocker_status_map"],
        resolved_statuses=set(data["resolved_statuses"]),
        default_unmapped_status=data["default_unmapped_status"],
    )


class MissingFieldReport(BaseModel):
    issue_key: str
    missing_fields: list[str]


class NormalizationReport(BaseModel):
    missing_field_reports: list[MissingFieldReport] = Field(default_factory=list)
    unmapped_statuses: dict[str, int] = Field(default_factory=dict)
    """raw status value -> count of issues seen with it, unresolvable via
    status_mapping.yaml. Non-empty means the config is out of date, not that
    the issues are broken."""
    unmapped_blocker_values: dict[str, int] = Field(default_factory=dict)
    rows_skipped: list[str] = Field(default_factory=list)
    """issue_key (or "row N" if issue_key itself was missing) for rows that
    could not be normalized at all, with the reason appended."""


class SprintMeta(BaseModel):
    sprint_id: str
    project_id: str
    sprint_name: str
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class NormalizedJiraData(BaseModel):
    issues: list[JiraIssue]
    sprint_meta: dict[str, SprintMeta]
    """sprint_id -> metadata. Populated from whichever raw rows mention that
    sprint — every row for a given sprint_id carries the same
    name/start/end in this dataset, so the first one seen wins."""
    project_summaries: dict[str, "JiraProjectSummary"]
    report: NormalizationReport
    retrieved_timestamp: datetime


class JiraProjectSummary(BaseModel):
    """Jira-only project facts. Deliberately NOT the cross-source `Project`
    model (Section 3) — that model requires project_manager/business_owner/
    technical_owner/budget, none of which Jira provides. Building a `Project`
    from Jira alone would mean fabricating those fields, which this phase
    must not do. The join into `Project` happens in Phase 5's normalization
    layer, using config/project_mapping.yaml."""

    project_id: str
    project_key: str
    project_name: str
    issue_count: int


def _clean(value: Optional[str]) -> Optional[str]:
    """"" -> None. Never invents a value; just stops treating an empty
    string as if it were meaningfully different from "not provided"."""
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _parse_date(value: Optional[str]) -> Optional[date]:
    value = _clean(value)
    if value is None:
        return None
    return date.fromisoformat(value)


def _parse_float(value: Optional[str]) -> Optional[float]:
    value = _clean(value)
    if value is None:
        return None
    return float(value)


def _parse_dependencies(value: Optional[str]) -> list[str]:
    value = _clean(value)
    if value is None:
        return []
    return [dep.strip() for dep in value.split(";") if dep.strip()]


def normalize_issue(
    raw: dict,
    field_map: dict[str, str],
    status_cfg: StatusMappingConfig,
    retrieved_timestamp: datetime,
    report: NormalizationReport,
) -> Optional[JiraIssue]:
    """Returns None (and records into `report.rows_skipped`) only when the
    row is missing its identity field (`issue_key`) — everything else is
    normalized as best-effort with gaps surfaced, never dropped."""

    def raw_get(semantic: str) -> Optional[str]:
        col = field_map.get(semantic)
        return raw.get(col) if col else None

    issue_key = _clean(raw_get("issue_key"))
    if not issue_key:
        report.rows_skipped.append("row with no issue_key (cannot establish identity)")
        return None

    project_id = _clean(raw_get("project_id")) or ""
    raw_status = _clean(raw_get("status"))
    raw_blocker = _clean(raw_get("blocker_status"))

    canonical_status = status_cfg.status_map.get(raw_status) if raw_status else None
    if canonical_status is None:
        canonical_status = status_cfg.default_unmapped_status
        if raw_status is not None:
            report.unmapped_statuses[raw_status] = report.unmapped_statuses.get(raw_status, 0) + 1

    canonical_blocker = status_cfg.blocker_status_map.get(raw_blocker or "")
    if canonical_blocker is None:
        canonical_blocker = status_cfg.default_unmapped_status
        report.unmapped_blocker_values[raw_blocker or ""] = (
            report.unmapped_blocker_values.get(raw_blocker or "", 0) + 1
        )

    blocked = canonical_status == "BLOCKED" or canonical_blocker == "BLOCKED"
    dependencies = _parse_dependencies(raw_get("dependencies"))

    blocker_reason: Optional[str] = None
    if blocked:
        if dependencies:
            blocker_reason = f"Unresolved dependency: {', '.join(dependencies)}"
        elif canonical_blocker == "BLOCKED":
            blocker_reason = "Flagged blocked via blocker_status field (source provided no reason text)"
        else:
            blocker_reason = "Workflow status is Blocked (no explicit blocker field set)"

    issue_type = _clean(raw_get("issue_type")) or "Unknown"
    assignee = _clean(raw_get("assignee"))
    priority = _clean(raw_get("priority"))
    story_points = _parse_float(raw_get("story_points"))

    missing_fields = [f for f in FIELDS_TO_SURFACE_IF_MISSING if _clean(raw_get(f)) is None]
    if story_points is None and issue_type not in STORY_POINT_EXEMPT_TYPES:
        missing_fields.append("story_points")
    if missing_fields:
        report.missing_field_reports.append(
            MissingFieldReport(issue_key=issue_key, missing_fields=missing_fields)
        )

    return JiraIssue(
        issue_key=issue_key,
        project_id=project_id,
        sprint_id=_clean(raw_get("sprint_id")),
        summary=_clean(raw_get("summary")) or "",
        issue_type=issue_type,
        status=canonical_status,
        raw_status=raw_status,
        priority=priority,
        assignee=assignee,
        created_date=_parse_date(raw_get("issue_created_date")),
        updated_date=_parse_date(raw_get("issue_updated_date")),
        resolution_date=_parse_date(raw_get("resolution_date")),
        story_points=story_points,
        blocked=blocked,
        blocker_reason=blocker_reason,
        blocker_age_days=None,  # filled in by sprint_metrics.apply_blocker_ages, needs an as_of date
        sprint_count_blocked=None,  # requires >=2 historical snapshots (Accuracy Check 6) — out of scope here
        linked_dependencies=dependencies,
        retrieved_timestamp=retrieved_timestamp,
    )


def normalize_all(
    raw_rows: list[dict],
    field_map: dict[str, str],
    status_cfg: StatusMappingConfig,
    retrieved_timestamp: Optional[datetime] = None,
) -> NormalizedJiraData:
    retrieved_timestamp = retrieved_timestamp or utcnow()
    report = NormalizationReport()
    issues: list[JiraIssue] = []
    sprint_meta: dict[str, SprintMeta] = {}
    project_counts: dict[str, JiraProjectSummary] = {}

    for raw in raw_rows:
        issue = normalize_issue(raw, field_map, status_cfg, retrieved_timestamp, report)
        if issue is None:
            continue
        issues.append(issue)

        if issue.sprint_id and issue.sprint_id not in sprint_meta:
            sprint_meta[issue.sprint_id] = SprintMeta(
                sprint_id=issue.sprint_id,
                project_id=issue.project_id,
                sprint_name=_clean(raw.get(field_map.get("sprint_name", ""))) or issue.sprint_id,
                start_date=_parse_date(raw.get(field_map.get("sprint_start_date", ""))),
                end_date=_parse_date(raw.get(field_map.get("sprint_end_date", ""))),
            )

        pid = issue.project_id
        if pid not in project_counts:
            project_counts[pid] = JiraProjectSummary(
                project_id=pid,
                project_key=_clean(raw.get(field_map.get("project_key", ""))) or "",
                project_name=_clean(raw.get(field_map.get("project_name", ""))) or "",
                issue_count=0,
            )
        project_counts[pid] = project_counts[pid].model_copy(
            update={"issue_count": project_counts[pid].issue_count + 1}
        )

    return NormalizedJiraData(
        issues=issues,
        sprint_meta=sprint_meta,
        project_summaries=project_counts,
        report=report,
        retrieved_timestamp=retrieved_timestamp,
    )
