from datetime import date

from src.connectors.jira_client import build_default_jira_client
from src.models.issue import JiraIssue
from src.services import sprint_metrics

SAMPLE_CSV = "data/sample/jira_mock_data.csv"


def _issue(key, sprint_id, story_points, status, summary="s", blocked=False, updated=None) -> JiraIssue:
    return JiraIssue(
        issue_key=key,
        project_id="1",
        sprint_id=sprint_id,
        summary=summary,
        issue_type="Story",
        status=status,
        story_points=story_points,
        blocked=blocked,
        updated_date=updated,
    )


class TestDeriveSprintStatus:
    def test_active_when_as_of_within_range(self):
        assert sprint_metrics.derive_sprint_status(date(2026, 1, 5), date(2026, 1, 18), date(2026, 1, 10)) == "ACTIVE"

    def test_active_boundary_inclusive_on_start_and_end(self):
        assert sprint_metrics.derive_sprint_status(date(2026, 1, 5), date(2026, 1, 18), date(2026, 1, 5)) == "ACTIVE"
        assert sprint_metrics.derive_sprint_status(date(2026, 1, 5), date(2026, 1, 18), date(2026, 1, 18)) == "ACTIVE"

    def test_closed_after_end_date(self):
        assert sprint_metrics.derive_sprint_status(date(2026, 1, 5), date(2026, 1, 18), date(2026, 2, 1)) == "CLOSED"

    def test_todo_before_start_date(self):
        assert sprint_metrics.derive_sprint_status(date(2026, 1, 5), date(2026, 1, 18), date(2025, 12, 1)) == "TODO"

    def test_unknown_when_dates_missing(self):
        assert sprint_metrics.derive_sprint_status(None, None, date(2026, 1, 1)) == "UNKNOWN"


class TestBlockerAge:
    def test_calculates_days_between_updated_and_as_of(self):
        assert sprint_metrics.calculate_blocker_age_days(date(2026, 1, 1), date(2026, 1, 8)) == 7

    def test_none_when_updated_date_missing(self):
        assert sprint_metrics.calculate_blocker_age_days(None, date(2026, 1, 8)) is None

    def test_apply_blocker_ages_only_touches_blocked_issues_and_does_not_mutate_input(self):
        blocked = _issue("X-1", "S1", 3, "BLOCKED", blocked=True, updated=date(2026, 1, 1))
        not_blocked = _issue("X-2", "S1", 2, "DONE", blocked=False, updated=date(2026, 1, 1))
        original_blocked_age = blocked.blocker_age_days

        result = sprint_metrics.apply_blocker_ages([blocked, not_blocked], as_of=date(2026, 1, 15))

        assert original_blocked_age is None  # input untouched
        result_by_key = {i.issue_key: i for i in result}
        assert result_by_key["X-1"].blocker_age_days == 14
        assert result_by_key["X-2"].blocker_age_days is None


class TestApplySprintCountBlocked:
    def test_populates_only_blocked_issues(self):
        from src.models.snapshot import ProjectSnapshot
        from src.models.common import RAGStatus

        snaps = [
            ProjectSnapshot(snapshot_date=date(2026, 1, 1), project_id="1", rag_status=RAGStatus.RED, major_blockers=["X-1"]),
            ProjectSnapshot(snapshot_date=date(2026, 1, 8), project_id="1", rag_status=RAGStatus.RED, major_blockers=["X-1"]),
        ]
        blocked = _issue("X-1", "S1", 3, "BLOCKED", blocked=True)
        not_blocked = _issue("X-2", "S1", 2, "DONE", blocked=False)

        result = sprint_metrics.apply_sprint_count_blocked([blocked, not_blocked], snaps)
        by_key = {i.issue_key: i for i in result}
        assert by_key["X-1"].sprint_count_blocked == 2
        assert by_key["X-2"].sprint_count_blocked is None

    def test_none_with_fewer_than_two_snapshots(self):
        from src.models.snapshot import ProjectSnapshot
        from src.models.common import RAGStatus

        snaps = [ProjectSnapshot(snapshot_date=date(2026, 1, 1), project_id="1", rag_status=RAGStatus.RED, major_blockers=["X-1"])]
        blocked = _issue("X-1", "S1", 3, "BLOCKED", blocked=True)
        result = sprint_metrics.apply_sprint_count_blocked([blocked], snaps)
        assert result[0].sprint_count_blocked is None

    def test_does_not_mutate_input(self):
        from src.models.snapshot import ProjectSnapshot
        from src.models.common import RAGStatus

        snaps = [
            ProjectSnapshot(snapshot_date=date(2026, 1, 1), project_id="1", rag_status=RAGStatus.RED, major_blockers=["X-1"]),
            ProjectSnapshot(snapshot_date=date(2026, 1, 8), project_id="1", rag_status=RAGStatus.RED, major_blockers=["X-1"]),
        ]
        blocked = _issue("X-1", "S1", 3, "BLOCKED", blocked=True)
        sprint_metrics.apply_sprint_count_blocked([blocked], snaps)
        assert blocked.sprint_count_blocked is None  # original untouched

    def test_real_phx_data_matches_manual_computation(self):
        """Cross-check against the same 3-week real-data scenario verified
        by hand while building this phase: PHX-17 blocked across 3
        consecutive weekly snapshots."""
        from src.connectors.jira_client import build_default_jira_client
        from src.connectors.financial_client import CSVFinancialDataSource
        from src.services import project_unifier, risk_engine, snapshot_builder

        jira_client = build_default_jira_client(csv_path=SAMPLE_CSV)
        fin_source = CSVFinancialDataSource()
        mapping = project_unifier.load_project_mapping()
        entry = mapping.entries["PROJECT-10001"]

        def make_snapshot(as_of, period, snap_date):
            issues = jira_client.get_project_issues("PHX", as_of=as_of).records
            sprint = jira_client.get_previous_sprints("PHX", 1, as_of=as_of)[0]
            record = fin_source.get_project_finances("10001", period).with_calculated_fields()
            project = project_unifier.build_unified_project(entry, jira_client, fin_source, as_of)
            assessment = risk_engine.assess_project_risk(project, issues, sprint, record)
            return snapshot_builder.build_snapshot(project, assessment, issues, sprint, record, snap_date)

        snaps = [
            make_snapshot(date(2026, 3, 1), "2026-01", date(2026, 3, 1)),
            make_snapshot(date(2026, 3, 8), "2026-02", date(2026, 3, 8)),
            make_snapshot(date(2026, 3, 15), "2026-03", date(2026, 3, 15)),
        ]

        issues = jira_client.get_project_issues("PHX", as_of=date(2026, 3, 15)).records
        updated = sprint_metrics.apply_sprint_count_blocked(issues, snaps)
        phx17 = next(i for i in updated if i.issue_key == "PHX-17")
        assert phx17.sprint_count_blocked == 3


class TestBuildSprint:
    def test_completion_pct_and_carryover_from_story_points(self):
        issues = [
            _issue("A-1", "S1", 5, "DONE"),
            _issue("A-2", "S1", 3, "IN_PROGRESS"),
            _issue("A-3", "S1", 2, "BLOCKED", blocked=True),
        ]
        sprint = sprint_metrics.build_sprint(
            sprint_id="S1",
            project_id="1",
            sprint_name="Sprint 1",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
            issues=issues,
            as_of=date(2026, 1, 20),
        )
        assert sprint.committed_story_points == 10
        assert sprint.completed_story_points == 5
        assert sprint.carryover_story_points == 5
        assert sprint.completion_pct == 50.0
        assert sprint.sprint_status == "CLOSED"

    def test_issues_without_story_points_are_excluded_from_totals(self):
        issues = [
            _issue("A-1", "S1", 5, "DONE"),
            _issue("A-2", "S1", None, "DONE"),  # e.g. a Sub-task with no points
        ]
        sprint = sprint_metrics.build_sprint(
            sprint_id="S1", project_id="1", sprint_name="Sprint 1",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 14),
            issues=issues, as_of=date(2026, 1, 20),
        )
        assert sprint.committed_story_points == 5
        assert sprint.completed_story_points == 5

    def test_zero_committed_points_gives_none_completion_pct_not_zero_division(self):
        sprint = sprint_metrics.build_sprint(
            sprint_id="S1", project_id="1", sprint_name="Sprint 1",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 14),
            issues=[], as_of=date(2026, 1, 20),
        )
        assert sprint.completion_pct is None


class TestOverallDeliveryProgress:
    def test_story_point_weighted_across_all_sprints(self):
        issues = [
            _issue("A-1", "S1", 5, "DONE"),
            _issue("A-2", "S1", 3, "IN_PROGRESS"),
            _issue("A-3", "S2", 8, "DONE"),
            _issue("A-4", "S2", 4, "TODO"),
        ]
        # committed = 20, completed (DONE) = 13
        assert sprint_metrics.overall_delivery_progress_pct(issues) == 65.0

    def test_none_when_no_issue_carries_story_points(self):
        issues = [_issue("A-1", "S1", None, "DONE"), _issue("A-2", "S1", None, "TODO")]
        assert sprint_metrics.overall_delivery_progress_pct(issues) is None

    def test_matches_phase1_hand_computed_figure_for_phx(self):
        """Regression pin against Phase 1's independently hand-computed
        sample report figure (37.9% overall progress for PHX)."""
        client = build_default_jira_client(csv_path=SAMPLE_CSV)
        issues = client.get_project_issues("PHX").records
        pct = sprint_metrics.overall_delivery_progress_pct(issues)
        assert round(pct, 1) == 37.9


class TestCarryoverByIdentity:
    def test_same_issue_key_across_two_sprints_is_detected(self):
        previous = [_issue("A-1", "S1", 3, "IN_PROGRESS"), _issue("A-2", "S1", 2, "DONE")]
        current = [_issue("A-1", "S2", 3, "DONE"), _issue("A-3", "S2", 5, "TODO")]
        assert sprint_metrics.detect_carryover_issues(previous, current) == ["A-1"]

    def test_disjoint_sprints_report_no_carryover(self):
        previous = [_issue("A-1", "S1", 3, "DONE")]
        current = [_issue("A-2", "S2", 3, "TODO")]
        assert sprint_metrics.detect_carryover_issues(previous, current) == []

    def test_shared_summary_text_alone_is_not_carryover(self):
        """The exact failure mode requirement 9 exists to prevent: two
        different tickets with the same template-generated summary must
        NOT be reported as one issue carrying over."""
        previous = [_issue("A-1", "S1", 3, "DONE", summary="Resolve intermittent failure in API gateway")]
        current = [_issue("A-99", "S2", 5, "TODO", summary="Resolve intermittent failure in API gateway")]
        assert sprint_metrics.detect_carryover_issues(previous, current) == []

    def test_naive_summary_matcher_produces_the_false_positive_identity_avoids(self):
        previous = [_issue("A-1", "S1", 3, "DONE", summary="Resolve intermittent failure in API gateway")]
        current = [_issue("A-99", "S2", 5, "TODO", summary="Resolve intermittent failure in API gateway")]
        assert sprint_metrics.naive_summary_carryover(previous, current) == [("A-1", "A-99")]

    def test_real_dataset_phx_sprint1_to_sprint2_has_the_known_summary_collision(self):
        """Ground-truth regression check against the real sample data:
        PHX-5 (Sprint 1) and PHX-23 (Sprint 2) are distinct tickets that
        happen to share a summary. Identity-based detection must report
        zero carryover for this pair; the naive matcher must report exactly
        this false positive, proving why identity is required."""
        client = build_default_jira_client(csv_path=SAMPLE_CSV)
        sprint1 = client.get_sprint_issues("PHX-SPR-1").records
        sprint2 = client.get_sprint_issues("PHX-SPR-2").records

        assert sprint_metrics.detect_carryover_issues(sprint1, sprint2) == []

        naive_matches = sprint_metrics.naive_summary_carryover(sprint1, sprint2)
        assert ("PHX-5", "PHX-23") in naive_matches
