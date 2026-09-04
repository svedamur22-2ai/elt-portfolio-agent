from datetime import date

from src.connectors.financial_client import CSVFinancialDataSource
from src.connectors.jira_client import build_default_jira_client
from src.models.common import RAGStatus
from src.services import project_unifier, risk_engine, snapshot_builder

AS_OF = date(2026, 9, 15)


def _clients():
    return build_default_jira_client(), CSVFinancialDataSource()


class TestBuildSnapshotFullyMapped:
    def test_phx_snapshot_matches_computed_figures(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        entry = mapping.entries["PROJECT-10001"]
        project = project_unifier.build_unified_project(entry, jira_client, fin_source, AS_OF)

        issues = jira_client.get_project_issues("PHX", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("PHX", 1, as_of=AS_OF)[0]
        latest_period = fin_source.get_latest_reporting_period("10001")
        financial_record = fin_source.get_project_finances("10001", latest_period).with_calculated_fields()
        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record)

        snapshot = snapshot_builder.build_snapshot(project, assessment, issues, latest_sprint, financial_record, AS_OF)

        assert snapshot.project_id == "PROJECT-10001"
        assert snapshot.sprint_id == "PHX-SPR-3"
        assert snapshot.sprint_completion_pct == 80.0
        assert snapshot.blocked_issues == 4
        assert set(snapshot.major_blockers) == {"PHX-4", "PHX-15", "PHX-17", "PHX-26"}
        assert snapshot.approved_budget == financial_record.approved_budget
        assert snapshot.rag_status == RAGStatus.RED
        assert snapshot.risk_score == 2.0  # HIGH + HIGH

    def test_overdue_issues_is_always_none(self):
        """No due-date field exists anywhere in this codebase — this must
        never be silently reported as 0."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        entry = mapping.entries["PROJECT-10001"]
        project = project_unifier.build_unified_project(entry, jira_client, fin_source, AS_OF)
        issues = jira_client.get_project_issues("PHX", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("PHX", 1, as_of=AS_OF)[0]
        latest_period = fin_source.get_latest_reporting_period("10001")
        financial_record = fin_source.get_project_finances("10001", latest_period).with_calculated_fields()
        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record)

        snapshot = snapshot_builder.build_snapshot(project, assessment, issues, latest_sprint, financial_record, AS_OF)
        assert snapshot.overdue_issues is None


class TestBuildSnapshotMappingGaps:
    def test_jira_only_project_has_none_financial_fields(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        entry = mapping.entries["PROJECT-10006"]  # QSR
        project = project_unifier.build_unified_project(entry, jira_client, fin_source, AS_OF)
        issues = jira_client.get_project_issues("QSR", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("QSR", 1, as_of=AS_OF)[0]
        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record=None)

        snapshot = snapshot_builder.build_snapshot(project, assessment, issues, latest_sprint, None, AS_OF)

        assert snapshot.approved_budget is None
        assert snapshot.budget_consumption_pct is None
        assert snapshot.blocked_issues == 0  # QSR genuinely has zero blocked issues — a real 0, not a gap
        assert snapshot.risk_score == 1.0  # MEDIUM delivery only

    def test_finance_only_project_has_none_delivery_fields(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        entry = mapping.entries["PROJECT-10007"]  # Helios
        project = project_unifier.build_unified_project(entry, jira_client, fin_source, AS_OF)
        latest_period = fin_source.get_latest_reporting_period("10007")
        financial_record = fin_source.get_project_finances("10007", latest_period).with_calculated_fields()
        assessment = risk_engine.assess_project_risk(project, jira_issues=None, latest_sprint=None, financial_record=financial_record)

        snapshot = snapshot_builder.build_snapshot(project, assessment, None, None, financial_record, AS_OF)

        assert snapshot.open_issues is None
        assert snapshot.blocked_issues is None
        assert snapshot.major_blockers == []
        assert snapshot.sprint_completion_pct is None
        assert snapshot.approved_budget is not None  # financial side still populated
        assert snapshot.risk_score == 0.0  # LOW financial only


class TestBuildSnapshotHistoricalReplay:
    def test_financial_record_reflects_the_period_passed_in_not_the_projects_latest(self):
        """Regression guard for the bug caught while building this phase:
        Project always reflects the financial source's global latest
        period; passing an explicit historical financial_record must
        override that, not be silently ignored."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        entry = mapping.entries["PROJECT-10001"]
        project = project_unifier.build_unified_project(entry, jira_client, fin_source, AS_OF)

        issues = jira_client.get_project_issues("PHX", as_of=date(2026, 2, 5)).records
        jan_record = fin_source.get_project_finances("10001", "2026-01").with_calculated_fields()
        assessment = risk_engine.assess_project_risk(project, issues, None, jan_record)

        snapshot = snapshot_builder.build_snapshot(project, assessment, issues, None, jan_record, date(2026, 2, 5))

        assert snapshot.approved_budget == jan_record.approved_budget
        assert snapshot.approved_budget != project.approved_budget  # project reflects Aug (global latest), not Jan
