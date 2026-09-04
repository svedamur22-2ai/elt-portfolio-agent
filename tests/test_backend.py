"""Tests for app/backend.py's pure data-access functions — the ones that
don't require a running Streamlit server. `st.cache_data`/`st.cache_resource`
work fine outside `streamlit run` ("bare mode"); only widget rendering
(`st.sidebar.multiselect` etc.) needs a real script run context, which is
what `streamlit.testing.v1.AppTest` (exercised manually against every page
while building this phase) is for instead.
"""

from datetime import date

from app import backend
from src.models.common import RAGStatus, RiskSeverity
from src.models.issue import JiraIssue
from src.models.project import Project
from src.models.risk import Risk


def _project(project_id, name="Test", pm="PM1", bo="BO1", rag=RAGStatus.RED, approved=100.0) -> Project:
    return Project(
        project_id=project_id, project_key=project_id, project_name=name,
        project_manager=pm, business_owner=bo, technical_owner="TO1",
        project_status="Active", risk_status=rag, approved_budget=approved,
        actual_spend=50.0, remaining_budget=50.0, budget_consumption_pct=50.0,
        delivery_progress_pct=40.0,
    )


class TestRunPortfolioQuery:
    def test_returns_real_pipeline_result_for_the_full_portfolio(self):
        result = backend.run_portfolio_query(date(2026, 9, 15))
        assert len(result["unified_projects"]) == 7
        assert result["confidence"] in ("HIGH CONFIDENCE", "MEDIUM CONFIDENCE", "LOW CONFIDENCE")

    def test_cached_by_date_returns_consistent_results(self):
        first = backend.run_portfolio_query(date(2026, 9, 15))
        second = backend.run_portfolio_query(date(2026, 9, 15))
        assert first["confidence"] == second["confidence"]
        assert len(first["unified_projects"]) == len(second["unified_projects"])


class TestAskQuestion:
    def test_scoped_question_returns_chat_answer_format(self):
        result = backend.ask_question("Why is Phoenix Platform Modernization at risk?", date(2026, 9, 15))
        assert "Answer:" in result["final_answer"]
        assert len(result["unified_projects"]) == 1

    def test_unscoped_question_returns_full_report_format(self):
        result = backend.ask_question("What needs my attention this week?", date(2026, 9, 15))
        assert "# Weekly ELT Portfolio Report" in result["final_answer"]


class TestProjectsDataframe:
    def test_shape_and_columns(self):
        df = backend.projects_dataframe([_project("P1"), _project("P2")])
        assert len(df) == 2
        for col in ["project_id", "Project", "PM", "RAG", "Approved Budget"]:
            assert col in df.columns

    def test_financial_status_defaults_to_ok_when_none(self):
        p = Project(project_id="P1", project_key="P1", project_name="X", project_manager="a", business_owner="b", technical_owner="c", project_status="Active")
        df = backend.projects_dataframe([p])
        assert df.iloc[0]["Financial Status"] == "OK"

    def test_real_portfolio_data_produces_seven_rows(self):
        result = backend.run_portfolio_query(date(2026, 9, 15))
        df = backend.projects_dataframe(result["unified_projects"])
        assert len(df) == 7
        assert set(df["RAG"]) <= {"GREEN", "AMBER", "RED", "UNKNOWN"}


class TestRisksDataframe:
    def test_shape_and_columns(self):
        risks = [Risk(risk_id="1", project_id="P1", category="Delivery", severity=RiskSeverity.HIGH, description="d", evidence=["e1", "e2"], reason_codes=["AGED_BLOCKER"])]
        df = backend.risks_dataframe(risks)
        assert df.iloc[0]["Reason Codes"] == "AGED_BLOCKER"
        assert df.iloc[0]["Evidence"] == "e1 | e2"


class TestBlockedIssuesDataframe:
    def test_only_blocked_issues_included(self):
        issues = [
            JiraIssue(issue_key="A-1", project_id="1", summary="s", issue_type="Task", status="BLOCKED", blocked=True, assignee="Alice", blocker_age_days=5),
            JiraIssue(issue_key="A-2", project_id="1", summary="s", issue_type="Task", status="DONE", blocked=False),
        ]
        df = backend.blocked_issues_dataframe(issues)
        assert len(df) == 1
        assert df.iloc[0]["Issue"] == "A-1"

    def test_missing_assignee_shown_as_not_available_not_blank(self):
        issues = [JiraIssue(issue_key="A-1", project_id="1", summary="s", issue_type="Task", status="BLOCKED", blocked=True, assignee=None)]
        df = backend.blocked_issues_dataframe(issues)
        assert df.iloc[0]["Assignee"] == "NOT AVAILABLE"

    def test_unresolved_sprint_count_blocked_shown_as_needs_history(self):
        issues = [JiraIssue(issue_key="A-1", project_id="1", summary="s", issue_type="Task", status="BLOCKED", blocked=True, sprint_count_blocked=None)]
        df = backend.blocked_issues_dataframe(issues)
        assert df.iloc[0]["Sprints Blocked"] == "N/A (needs history)"


class TestApplyProjectFilters:
    def setup_method(self):
        self.df = backend.projects_dataframe(
            [
                _project("P1", name="Alpha", pm="Ann", rag=RAGStatus.RED),
                _project("P2", name="Beta", pm="Bob", rag=RAGStatus.GREEN),
            ]
        )

    def test_no_filters_returns_everything(self):
        assert len(backend.apply_project_filters(self.df)) == 2

    def test_filters_by_project_id(self):
        result = backend.apply_project_filters(self.df, project_ids=["P1"])
        assert result["Project"].tolist() == ["Alpha"]

    def test_filters_by_pm(self):
        result = backend.apply_project_filters(self.df, pms=["Bob"])
        assert result["Project"].tolist() == ["Beta"]

    def test_filters_by_rag(self):
        result = backend.apply_project_filters(self.df, rag=["RED"])
        assert result["Project"].tolist() == ["Alpha"]

    def test_combined_filters_are_intersected_not_unioned(self):
        result = backend.apply_project_filters(self.df, pms=["Ann"], rag=["GREEN"])
        assert result.empty  # Ann's project is RED, not GREEN


class TestStatusColors:
    def test_every_rag_value_has_a_color(self):
        for rag in ["GREEN", "AMBER", "RED", "UNKNOWN"]:
            assert rag in backend.STATUS_COLORS

    def test_every_severity_value_has_a_color(self):
        for severity in ["LOW", "MEDIUM", "HIGH"]:
            assert severity in backend.STATUS_COLORS

    def test_status_colors_are_disjoint_from_categorical_palette(self):
        """Status colors must never double as a generic categorical hue —
        the whole point of reserving them."""
        assert not set(backend.STATUS_COLORS.values()) & set(backend.CATEGORICAL_PALETTE)
