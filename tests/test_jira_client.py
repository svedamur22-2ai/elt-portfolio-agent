from datetime import date

import pytest

from src.connectors.jira_client import (
    CSVJiraSource,
    JiraClient,
    JiraDataSource,
    JiraPage,
    RetryPolicy,
    build_default_jira_client,
)
from src.services import jira_normalizer as norm
from src.utils.time import utcnow

SAMPLE_CSV = "data/sample/jira_mock_data.csv"


def _client(**kwargs) -> JiraClient:
    return build_default_jira_client(csv_path=SAMPLE_CSV, **kwargs)


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


class TestPagination:
    def test_csv_source_pages_cover_every_row_exactly_once(self):
        source = CSVJiraSource(SAMPLE_CSV)
        total = source.fetch_issue_page(0, 1_000_000).total

        seen_keys = []
        start_at = 0
        page_size = 17  # deliberately not a divisor of `total`
        pages = 0
        while True:
            page = source.fetch_issue_page(start_at, page_size)
            pages += 1
            seen_keys.extend(row["issue_key"] for row in page.values)
            assert page.total == total
            if page.is_last:
                break
            start_at += page_size

        assert len(seen_keys) == total
        assert len(set(seen_keys)) == total  # no duplicates across pages
        assert pages == -(-total // page_size)  # ceil division

    def test_page_size_larger_than_total_returns_single_last_page(self):
        source = CSVJiraSource(SAMPLE_CSV)
        page = source.fetch_issue_page(0, 10_000)
        assert page.is_last is True
        assert len(page.values) == page.total

    def test_page_size_exact_multiple_of_total_ends_cleanly(self):
        source = CSVJiraSource(SAMPLE_CSV)
        total = source.fetch_issue_page(0, 1).total
        page_size = total  # exactly one page
        page = source.fetch_issue_page(0, page_size)
        assert page.is_last is True
        assert len(page.values) == total

    def test_project_filter_is_pushed_down_before_pagination(self):
        source = CSVJiraSource(SAMPLE_CSV)
        page = source.fetch_issue_page(0, 5, project_key="PHX")
        assert page.total == 37  # known PHX issue count
        assert all(row["project_key"] == "PHX" for row in page.values)

    def test_client_fetch_all_pages_matches_direct_csv_read(self):
        client = _client(page_size=13)
        raw_rows, meta = client._fetch_all_pages(project_key="ORCA")
        assert len(raw_rows) == 43
        assert meta.pages_fetched == -(-43 // 13)
        assert meta.partial_failure is False

    def test_get_project_issues_end_to_end_pagination(self):
        client = _client(page_size=6)
        result = client.get_project_issues("QSR")
        assert len(result.records) == 5
        assert result.partial_failure is False
        assert {i.issue_key for i in result.records} == {"QSR-1", "QSR-2", "QSR-3", "QSR-4", "QSR-5"}


# --------------------------------------------------------------------------
# Partial API failure detection
# --------------------------------------------------------------------------


class _FailAfterNPagesSource(JiraDataSource):
    """Test double: succeeds for `fail_after` pages, then raises forever."""

    def __init__(self, rows: list[dict], fail_after: int):
        self._rows = rows
        self.fail_after = fail_after
        self.calls = 0

    def fetch_issue_page(self, start_at, max_results, project_key=None, sprint_id=None) -> JiraPage:
        self.calls += 1
        if self.calls > self.fail_after:
            raise ConnectionError("simulated Jira API outage")
        total = len(self._rows)
        chunk = self._rows[start_at : start_at + max_results]
        return JiraPage(values=chunk, start_at=start_at, max_results=max_results, total=total, is_last=False)

    def fetch_issue_by_key(self, issue_key):
        raise NotImplementedError


class _FailThenRecoverSource(JiraDataSource):
    """Fails exactly `fail_count` times total, then behaves like CSVJiraSource."""

    def __init__(self, delegate: CSVJiraSource, fail_count: int):
        self.delegate = delegate
        self.fail_count = fail_count
        self.attempts = 0

    def fetch_issue_page(self, start_at, max_results, project_key=None, sprint_id=None) -> JiraPage:
        self.attempts += 1
        if self.attempts <= self.fail_count:
            raise TimeoutError("simulated transient timeout")
        return self.delegate.fetch_issue_page(start_at, max_results, project_key, sprint_id)

    def fetch_issue_by_key(self, issue_key):
        return self.delegate.fetch_issue_by_key(issue_key)


class TestPartialFailureDetection:
    def test_failure_mid_pagination_marks_partial_failure_and_keeps_progress(self):
        rows = [{"issue_key": f"X-{i}", "project_key": "X"} for i in range(30)]
        source = _FailAfterNPagesSource(rows, fail_after=2)
        client = JiraClient(source=source, field_map={"issue_key": "issue_key"}, status_cfg=_minimal_status_cfg(), page_size=10)

        raw_rows, meta = client._fetch_all_pages()

        assert meta.partial_failure is True
        assert "simulated Jira API outage" in meta.error
        assert meta.pages_fetched == 2
        assert len(raw_rows) == 20  # first two pages' worth, not discarded

    def test_get_project_issues_surfaces_partial_failure_to_caller(self):
        rows = [
            {
                "issue_key": f"X-{i}",
                "project_key": "X",
                "project_id": "1",
                "status": "To Do",
                "issue_type": "Task",
                "blocker_status": "Not Blocked",
            }
            for i in range(5)
        ]
        source = _FailAfterNPagesSource(rows, fail_after=0)
        client = JiraClient(source=source, field_map=norm.load_field_mapping(), status_cfg=norm.load_status_mapping(), page_size=2)

        result = client.get_project_issues("X")

        assert result.partial_failure is True
        assert result.error is not None
        assert result.records == []  # failed on the very first page

    def test_retry_policy_recovers_from_transient_failures(self):
        delegate = CSVJiraSource(SAMPLE_CSV)
        flaky = _FailThenRecoverSource(delegate, fail_count=2)
        client = JiraClient(
            source=flaky,
            field_map=norm.load_field_mapping(),
            status_cfg=norm.load_status_mapping(),
            page_size=1000,
            retry_policy=RetryPolicy(max_retries=2, backoff_seconds=0),
        )

        raw_rows, meta = client._fetch_all_pages(project_key="QSR")

        assert meta.partial_failure is False
        assert len(raw_rows) == 5

    def test_retry_policy_exhausted_still_marks_partial_failure(self):
        delegate = CSVJiraSource(SAMPLE_CSV)
        flaky = _FailThenRecoverSource(delegate, fail_count=5)
        client = JiraClient(
            source=flaky,
            field_map=norm.load_field_mapping(),
            status_cfg=norm.load_status_mapping(),
            page_size=1000,
            retry_policy=RetryPolicy(max_retries=2, backoff_seconds=0),
        )

        raw_rows, meta = client._fetch_all_pages(project_key="QSR")

        assert meta.partial_failure is True
        assert raw_rows == []


def _minimal_status_cfg() -> norm.StatusMappingConfig:
    return norm.StatusMappingConfig(
        status_map={"To Do": "TODO"},
        blocker_status_map={"": "NOT_BLOCKED"},
        resolved_statuses={"DONE"},
        default_unmapped_status="UNKNOWN",
    )


# --------------------------------------------------------------------------
# Blocked-issue identification (status OR explicit blocker field)
# --------------------------------------------------------------------------


class TestBlockedIssueIdentification:
    def test_blocked_via_workflow_status_alone(self):
        client = _client()
        result = client.get_blocked_issues("PHX", as_of=date(2026, 3, 22))
        keys = {i.issue_key for i in result.records}
        # PHX-15, PHX-17, PHX-26 carry workflow status "Blocked" in the CSV
        assert {"PHX-15", "PHX-17", "PHX-26"} <= keys

    def test_blocked_via_explicit_blocker_field_when_workflow_status_differs(self):
        client = _client()
        result = client.get_blocked_issues("PHX", as_of=date(2026, 3, 22))
        phx4 = next(i for i in result.records if i.issue_key == "PHX-4")
        # Known source inconsistency: workflow status is "In Review" but
        # blocker_status flag is "Blocked" — union logic must still catch it.
        assert phx4.raw_status == "In Review"
        assert phx4.status == "IN_REVIEW"
        assert phx4.blocked is True

    def test_non_blocked_issue_is_excluded(self):
        client = _client()
        result = client.get_blocked_issues("QSR")
        assert result.records == []  # QSR has zero blocked issues by design


# --------------------------------------------------------------------------
# Accuracy requirements: never infer, never assume, preserve identity
# --------------------------------------------------------------------------


class TestAccuracyRules:
    def test_missing_assignee_is_none_not_a_placeholder(self):
        client = _client()
        result = client.get_project_issues("ORCA")
        orca3 = next(i for i in result.records if i.issue_key == "ORCA-3")
        assert orca3.assignee is None  # CSV has this cell blank

    def test_issue_key_is_preserved_verbatim(self):
        client = _client()
        result = client.get_project_issues("TITAN")
        assert all(i.issue_key.startswith("TITAN-") for i in result.records)
        assert len({i.issue_key for i in result.records}) == len(result.records)

    def test_unmapped_raw_status_falls_back_to_configured_sentinel_not_a_guess(self):
        raw_rows = [
            {
                "issue_key": "X-1",
                "project_id": "1",
                "project_key": "X",
                "project_name": "X",
                "issue_type": "Task",
                "status": "Some New Custom Status Nobody Configured",
                "blocker_status": "",
                "story_points": "",
            }
        ]
        report = norm.NormalizationReport()
        issue = norm.normalize_issue(raw_rows[0], norm.load_field_mapping(), norm.load_status_mapping(), utcnow(), report)
        assert issue.status == "UNKNOWN"  # default_unmapped_status, never guessed as e.g. "IN_PROGRESS"
        assert issue.raw_status == "Some New Custom Status Nobody Configured"  # preserved untouched
        assert report.unmapped_statuses == {"Some New Custom Status Nobody Configured": 1}

    def test_row_with_no_issue_key_is_skipped_not_crashed_on(self):
        raw_rows = [{"issue_key": "", "project_key": "X", "status": "To Do"}]
        report = norm.NormalizationReport()
        issue = norm.normalize_issue(raw_rows[0], norm.load_field_mapping(), norm.load_status_mapping(), utcnow(), report)
        assert issue is None
        assert len(report.rows_skipped) == 1

    def test_retrieval_timestamp_recorded_on_every_issue(self):
        client = _client()
        result = client.get_project_issues("PHX")
        assert all(i.retrieved_timestamp == result.retrieved_timestamp for i in result.records)


# --------------------------------------------------------------------------
# get_issue_history: no fabricated changelog
# --------------------------------------------------------------------------


class TestIssueHistory:
    def test_history_only_contains_known_facts_with_limitation_disclosed(self):
        client = _client()
        events = client.get_issue_history("PHX-4")
        assert len(events) >= 1
        for event in events:
            assert "source_limitation" in event
        # must never claim a status transition it wasn't given
        assert not any(e["event"] == "status_changed" for e in events)

    def test_unknown_issue_key_returns_empty_not_an_exception(self):
        client = _client()
        assert client.get_issue_history("NOPE-999") == []


# --------------------------------------------------------------------------
# Custom-field mapping configurability
# --------------------------------------------------------------------------


class TestCustomFieldMapping:
    def test_normalizer_honors_an_alternate_field_map(self):
        """Simulates a live Jira instance where story points live in a
        cryptic custom field id instead of a friendly column name."""
        alternate_map = dict(norm.load_field_mapping())
        alternate_map["story_points"] = "customfield_10016"

        raw = {
            "issue_key": "Y-1",
            "project_id": "9",
            "project_key": "Y",
            "project_name": "Y Project",
            "issue_type": "Story",
            "status": "To Do",
            "blocker_status": "Not Blocked",
            "customfield_10016": "8",  # NOT under the key "story_points"
        }
        report = norm.NormalizationReport()
        issue = norm.normalize_issue(raw, alternate_map, norm.load_status_mapping(), utcnow(), report)

        assert issue.story_points == 8.0

    def test_field_not_present_in_mapping_is_treated_as_absent(self):
        stripped_map = dict(norm.load_field_mapping())
        del stripped_map["story_points"]

        raw = {"issue_key": "Y-2", "project_id": "9", "issue_type": "Story", "status": "To Do", "blocker_status": ""}
        report = norm.NormalizationReport()
        issue = norm.normalize_issue(raw, stripped_map, norm.load_status_mapping(), utcnow(), report)

        assert issue.story_points is None
        assert any(m.issue_key == "Y-2" and "story_points" in m.missing_fields for m in report.missing_field_reports)


# --------------------------------------------------------------------------
# Current / previous sprint selection
# --------------------------------------------------------------------------


class TestSprintSelection:
    def test_current_sprint_is_the_one_active_as_of_the_given_date(self):
        client = _client()
        sprint = client.get_current_sprint("PHX", as_of=date(2026, 1, 10))
        assert sprint.sprint_id == "PHX-SPR-1"
        assert sprint.sprint_status == "ACTIVE"

    def test_no_active_sprint_returns_none_rather_than_the_nearest_one(self):
        client = _client()
        # Well after every PHX sprint has closed
        sprint = client.get_current_sprint("PHX", as_of=date(2026, 6, 1))
        assert sprint is None

    def test_previous_sprints_ordered_most_recent_first(self):
        client = _client()
        sprints = client.get_previous_sprints("PHX", 2, as_of=date(2026, 3, 22))
        assert [s.sprint_id for s in sprints] == ["PHX-SPR-3", "PHX-SPR-2"]
        assert all(s.sprint_status == "CLOSED" for s in sprints)
