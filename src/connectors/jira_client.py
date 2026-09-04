"""Jira adapter (Section 4).

Design: `JiraClient` contains all pagination, retry, and partial-failure
handling and is source-agnostic. `JiraDataSource` is the seam — today only
`CSVJiraSource` (backed by data/sample/jira_mock_data.csv) is implemented,
because that's the data available; `JiraCloudRESTSource` is stubbed with the
real endpoint/pagination contract documented so plugging in a live Jira
Cloud instance later means implementing that one class, not touching
`JiraClient` or anything upstream of it.

The public methods deliberately mirror Section 4's original list
(`get_projects`, `get_current_sprint`, ...) as `JiraClient` methods rather
than module-level functions — once pagination/retry/DI enter the picture (a
real requirement here: "unit test pagination", "detect partial API
failures"), a class holding that state is the natural shape; a bare module
function has nowhere to put a swappable source or a retry policy.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Generic, Optional, TypeVar

import csv as csv_module

from pydantic import BaseModel, Field

from src.models.issue import JiraIssue
from src.models.sprint import Sprint
from src.services import jira_normalizer as norm
from src.services import sprint_metrics
from src.utils.time import utcnow

T = TypeVar("T")


# --------------------------------------------------------------------------
# Pagination primitives — shaped to match Jira's real REST pagination
# envelope (startAt / maxResults / total / isLast from
# /rest/api/3/search and /rest/agile/1.0/*), so a live source is a drop-in.
# --------------------------------------------------------------------------


class JiraPage(BaseModel):
    values: list[dict]
    start_at: int
    max_results: int
    total: int
    is_last: bool


class JiraFetchResult(BaseModel, Generic[T]):
    """Every multi-record JiraClient method returns this. `partial_failure`
    is how "detect partial API failures" surfaces: callers get back
    whatever was fetched before the failure PLUS an explicit signal that the
    result is incomplete, instead of either an exception that discards
    partial progress or a silently-truncated list indistinguishable from a
    complete one."""

    records: list[T]
    total_available: Optional[int] = None
    pages_fetched: int = 0
    partial_failure: bool = False
    error: Optional[str] = None
    retrieved_timestamp: datetime = Field(default_factory=utcnow)


@dataclass
class RetryPolicy:
    max_retries: int = 0
    backoff_seconds: float = 0.0


class JiraDataSource(ABC):
    """The only interface `JiraClient` depends on."""

    @abstractmethod
    def fetch_issue_page(
        self,
        start_at: int,
        max_results: int,
        project_key: Optional[str] = None,
        sprint_id: Optional[str] = None,
    ) -> JiraPage:
        """`project_key`/`sprint_id` are pushed-down filters (mirroring a
        real JQL `project = X AND sprint = Y` search) — implementations
        filter before paginating, not after, so `total`/`is_last` describe
        the filtered result set."""

    @abstractmethod
    def fetch_issue_by_key(self, issue_key: str) -> Optional[dict]:
        ...


class CSVJiraSource(JiraDataSource):
    """Reads data/sample/jira_mock_data.csv (or any file with the same
    columns) once, then serves paginated/filtered slices from memory."""

    def __init__(self, csv_path: str | Path = "data/sample/jira_mock_data.csv") -> None:
        self.csv_path = Path(csv_path)
        with open(self.csv_path, newline="") as f:
            self._rows: list[dict] = list(csv_module.DictReader(f))

    def _filtered(self, project_key: Optional[str], sprint_id: Optional[str]) -> list[dict]:
        rows = self._rows
        if project_key is not None:
            rows = [r for r in rows if r.get("project_key") == project_key]
        if sprint_id is not None:
            rows = [r for r in rows if r.get("sprint_id") == sprint_id]
        return rows

    def fetch_issue_page(
        self,
        start_at: int,
        max_results: int,
        project_key: Optional[str] = None,
        sprint_id: Optional[str] = None,
    ) -> JiraPage:
        rows = self._filtered(project_key, sprint_id)
        total = len(rows)
        chunk = rows[start_at : start_at + max_results]
        is_last = start_at + len(chunk) >= total
        return JiraPage(values=chunk, start_at=start_at, max_results=max_results, total=total, is_last=is_last)

    def fetch_issue_by_key(self, issue_key: str) -> Optional[dict]:
        for row in self._rows:
            if row.get("issue_key") == issue_key:
                return row
        return None


class JiraCloudRESTSource(JiraDataSource):
    """Not implemented — no live Jira instance/credentials available in this
    environment. Documented so a future implementation is a known-shape
    task, not a design exercise:

    - `fetch_issue_page` -> GET `{JIRA_URL}/rest/api/3/search` with
      `jql=project="{project_key}" AND sprint={sprint_id}`,
      `startAt={start_at}`, `maxResults={max_results}`; response's
      `startAt`/`maxResults`/`total`/`isLast` map directly onto `JiraPage`.
    - `fetch_issue_by_key` -> GET `{JIRA_URL}/rest/api/3/issue/{issue_key}`.
    - Auth: HTTP Basic with `JIRA_EMAIL` / `JIRA_API_TOKEN` (see
      .env.example) — never logged, never embedded in a URL query string.
    - Rate limiting: Jira Cloud returns HTTP 429 with `Retry-After`; the
      `RetryPolicy` already threaded through `JiraClient` is meant to honor
      that header once this class exists, rather than a fixed backoff.
    """

    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url
        self.email = email
        self.api_token = api_token

    def fetch_issue_page(self, start_at, max_results, project_key=None, sprint_id=None) -> JiraPage:
        raise NotImplementedError("Live Jira Cloud REST source not implemented — see class docstring")

    def fetch_issue_by_key(self, issue_key: str) -> Optional[dict]:
        raise NotImplementedError("Live Jira Cloud REST source not implemented — see class docstring")


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass
class _FetchMeta:
    pages_fetched: int
    total_available: Optional[int]
    partial_failure: bool
    error: Optional[str]


class JiraClient:
    def __init__(
        self,
        source: JiraDataSource,
        field_map: Optional[dict[str, str]] = None,
        status_cfg: Optional[norm.StatusMappingConfig] = None,
        page_size: int = 50,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        self.source = source
        self.field_map = field_map or norm.load_field_mapping()
        self.status_cfg = status_cfg or norm.load_status_mapping()
        self.page_size = page_size
        self.retry_policy = retry_policy or RetryPolicy()

    # -- low-level pagination, shared by every public method --------------

    def _fetch_all_pages(
        self, project_key: Optional[str] = None, sprint_id: Optional[str] = None
    ) -> tuple[list[dict], _FetchMeta]:
        records: list[dict] = []
        start_at = 0
        pages_fetched = 0
        total_available: Optional[int] = None

        while True:
            attempt = 0
            while True:
                try:
                    page = self.source.fetch_issue_page(
                        start_at, self.page_size, project_key=project_key, sprint_id=sprint_id
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - deliberately broad: any source failure is "partial failure"
                    attempt += 1
                    if attempt > self.retry_policy.max_retries:
                        return records, _FetchMeta(
                            pages_fetched=pages_fetched,
                            total_available=total_available,
                            partial_failure=True,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    if self.retry_policy.backoff_seconds:
                        time.sleep(self.retry_policy.backoff_seconds)

            records.extend(page.values)
            pages_fetched += 1
            total_available = page.total
            if page.is_last:
                return records, _FetchMeta(
                    pages_fetched=pages_fetched,
                    total_available=total_available,
                    partial_failure=False,
                    error=None,
                )
            start_at += self.page_size

    def _normalize(self, raw_rows: list[dict]) -> norm.NormalizedJiraData:
        return norm.normalize_all(raw_rows, self.field_map, self.status_cfg)

    # -- public API ---------------------------------------------------------

    def get_projects(self) -> JiraFetchResult[norm.JiraProjectSummary]:
        raw_rows, meta = self._fetch_all_pages()
        normalized = self._normalize(raw_rows)
        return JiraFetchResult(
            records=list(normalized.project_summaries.values()),
            total_available=meta.total_available,
            pages_fetched=meta.pages_fetched,
            partial_failure=meta.partial_failure,
            error=meta.error,
            retrieved_timestamp=normalized.retrieved_timestamp,
        )

    def get_project_issues(self, project_key: str, as_of: Optional[date] = None) -> JiraFetchResult[JiraIssue]:
        raw_rows, meta = self._fetch_all_pages(project_key=project_key)
        normalized = self._normalize(raw_rows)
        issues = sprint_metrics.apply_blocker_ages(normalized.issues, as_of or date.today())
        return JiraFetchResult(
            records=issues,
            total_available=meta.total_available,
            pages_fetched=meta.pages_fetched,
            partial_failure=meta.partial_failure,
            error=meta.error,
            retrieved_timestamp=normalized.retrieved_timestamp,
        )

    def get_sprint_issues(self, sprint_id: str, as_of: Optional[date] = None) -> JiraFetchResult[JiraIssue]:
        raw_rows, meta = self._fetch_all_pages(sprint_id=sprint_id)
        normalized = self._normalize(raw_rows)
        issues = sprint_metrics.apply_blocker_ages(normalized.issues, as_of or date.today())
        return JiraFetchResult(
            records=issues,
            total_available=meta.total_available,
            pages_fetched=meta.pages_fetched,
            partial_failure=meta.partial_failure,
            error=meta.error,
            retrieved_timestamp=normalized.retrieved_timestamp,
        )

    def get_blocked_issues(self, project_key: str, as_of: Optional[date] = None) -> JiraFetchResult[JiraIssue]:
        result = self.get_project_issues(project_key, as_of=as_of)
        blocked = [i for i in result.records if i.blocked]
        return result.model_copy(update={"records": blocked})

    def _all_sprints_for_project(self, project_key: str, as_of: date) -> list[Sprint]:
        raw_rows, _meta = self._fetch_all_pages(project_key=project_key)
        normalized = self._normalize(raw_rows)
        return sprint_metrics.build_sprints(normalized, as_of)

    def get_current_sprint(self, project_key: str, as_of: Optional[date] = None) -> Optional[Sprint]:
        as_of = as_of or date.today()
        sprints = self._all_sprints_for_project(project_key, as_of)
        active = [s for s in sprints if s.sprint_status == "ACTIVE"]
        if active:
            return max(active, key=lambda s: s.start_date)
        return None

    def get_previous_sprints(self, project_key: str, count: int, as_of: Optional[date] = None) -> list[Sprint]:
        as_of = as_of or date.today()
        sprints = self._all_sprints_for_project(project_key, as_of)
        closed = [s for s in sprints if s.sprint_status == "CLOSED"]
        closed.sort(key=lambda s: s.end_date, reverse=True)
        return closed[:count]

    def get_issue_history(self, issue_key: str) -> list[dict]:
        """Returns known timestamped facts only — NOT a real changelog. The
        CSV export carries no transition history, so this must not invent
        intermediate status changes it was never given (the accuracy rule
        this whole layer is built around). Each entry is tagged with
        `source_limitation` so nothing downstream mistakes this for
        Jira's actual `/changelog` data."""
        raw = self.source.fetch_issue_by_key(issue_key)
        if raw is None:
            return []

        report = norm.NormalizationReport()
        issue = norm.normalize_issue(raw, self.field_map, self.status_cfg, utcnow(), report)
        if issue is None:
            return []

        events = []
        if issue.created_date:
            events.append(
                {
                    "observed_at": issue.created_date.isoformat(),
                    "event": "created",
                    "status": None,
                    "source_limitation": "CSV export has no status-at-creation field",
                }
            )
        if issue.updated_date:
            events.append(
                {
                    "observed_at": issue.updated_date.isoformat(),
                    "event": "last_updated",
                    "status": issue.status,
                    "blocked": issue.blocked,
                    "source_limitation": "reflects only the most recent known state, not the transition that produced it",
                }
            )
        if issue.resolution_date:
            events.append(
                {
                    "observed_at": issue.resolution_date.isoformat(),
                    "event": "resolved",
                    "status": issue.status,
                    "source_limitation": None,
                }
            )
        return events


def build_default_jira_client(
    csv_path: str = "data/sample/jira_mock_data.csv",
    field_mapping_path: str = norm.DEFAULT_FIELD_MAPPING_PATH,
    status_mapping_path: str = norm.DEFAULT_STATUS_MAPPING_PATH,
    page_size: int = 50,
) -> JiraClient:
    return JiraClient(
        source=CSVJiraSource(csv_path),
        field_map=norm.load_field_mapping(field_mapping_path),
        status_cfg=norm.load_status_mapping(status_mapping_path),
        page_size=page_size,
    )
