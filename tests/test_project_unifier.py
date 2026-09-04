from datetime import date

from src.connectors.financial_client import CSVFinancialDataSource
from src.connectors.jira_client import build_default_jira_client
from src.services import project_unifier

AS_OF = date(2026, 9, 15)


def _clients():
    return build_default_jira_client(), CSVFinancialDataSource()


class TestLoadProjectMapping:
    def test_loads_all_seven_entries(self):
        mapping = project_unifier.load_project_mapping()
        assert set(mapping.entries.keys()) == {
            "PROJECT-10001", "PROJECT-10002", "PROJECT-10003",
            "PROJECT-10004", "PROJECT-10005", "PROJECT-10006", "PROJECT-10007",
        }

    def test_organization_and_portfolio_ids_present(self):
        mapping = project_unifier.load_project_mapping()
        assert mapping.organization_id == "org-northwind"
        assert mapping.portfolio_id == "portfolio-elt-2026"

    def test_null_mapping_fields_parse_as_none_not_the_string_null(self):
        mapping = project_unifier.load_project_mapping()
        assert mapping.entries["PROJECT-10006"].finance_project_id is None
        assert mapping.entries["PROJECT-10007"].jira_project_id is None
        assert mapping.entries["PROJECT-10007"].jira_key is None


class TestFullyMappedProject:
    def test_phx_has_both_sides_populated(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)

        assert project.project_key == "PHX"
        assert project.jira_project_key == "PHX"
        assert project.financial_status is None
        assert project.delivery_status is None
        assert project.delivery_progress_pct is not None
        assert project.approved_budget is not None

    def test_delivery_progress_matches_direct_sprint_metrics_calculation(self):
        """Cross-check against the same calculation done directly, without
        going through the unifier — proves the unifier isn't quietly using
        a different formula."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)

        from src.services import sprint_metrics
        issues = jira_client.get_project_issues("PHX", as_of=AS_OF).records
        expected = sprint_metrics.overall_delivery_progress_pct(issues)
        assert project.delivery_progress_pct == expected

    def test_financial_figures_match_latest_period_from_source(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)

        latest = fin_source.get_latest_reporting_period("10001")
        expected = fin_source.get_project_finances("10001", latest).with_calculated_fields()
        assert project.approved_budget == expected.approved_budget
        assert project.remaining_budget == expected.remaining_budget

    def test_no_mapping_risks_for_a_fully_mapped_project(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)
        assert project_unifier.detect_mapping_risks(project) == []

    def test_provenance_includes_both_sources(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)
        systems = {p.source_system.value for p in project.provenance}
        assert systems == {"Jira", "Finance"}


class TestJiraOnlyProject:
    """PROJECT-10006 / QSR: exists in Jira, no finance mapping."""

    def test_financial_status_is_unknown(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10006"], jira_client, fin_source, AS_OF)
        assert project.financial_status == "UNKNOWN"

    def test_financial_numeric_fields_stay_none_not_zero(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10006"], jira_client, fin_source, AS_OF)
        assert project.approved_budget is None
        assert project.remaining_budget is None
        assert project.budget_consumption_pct is None

    def test_delivery_side_still_populated(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10006"], jira_client, fin_source, AS_OF)
        assert project.delivery_status is None
        assert project.delivery_progress_pct is not None

    def test_mapping_risk_fires_for_financial_side_only(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10006"], jira_client, fin_source, AS_OF)
        risks = project_unifier.detect_mapping_risks(project)
        assert len(risks) == 1
        assert "DATA_MAPPING" in risks[0].reason_codes
        assert "financial" in risks[0].description.lower()


class TestFinanceOnlyProject:
    """PROJECT-10007 / Helios: exists in Finance, no Jira mapping."""

    def test_delivery_status_is_unknown(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10007"], jira_client, fin_source, AS_OF)
        assert project.delivery_status == "UNKNOWN"

    def test_delivery_progress_stays_none(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10007"], jira_client, fin_source, AS_OF)
        assert project.delivery_progress_pct is None
        assert project.jira_project_key is None

    def test_financial_side_still_populated(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10007"], jira_client, fin_source, AS_OF)
        assert project.financial_status is None
        assert project.approved_budget is not None

    def test_display_key_falls_back_to_finance_derived_code(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10007"], jira_client, fin_source, AS_OF)
        assert project.project_key == "FIN-10007"

    def test_mapping_risk_fires_for_delivery_side_only(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10007"], jira_client, fin_source, AS_OF)
        risks = project_unifier.detect_mapping_risks(project)
        assert len(risks) == 1
        assert "DATA_MAPPING" in risks[0].reason_codes
        assert "jira" in risks[0].description.lower()


class TestBuildUnifiedPortfolio:
    def test_returns_all_seven_projects(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        portfolio = project_unifier.build_unified_portfolio(mapping, jira_client, fin_source, AS_OF)
        assert len(portfolio) == 7
        assert {p.project_id for p in portfolio} == set(mapping.entries.keys())

    def test_exactly_two_projects_carry_a_mapping_gap(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        portfolio = project_unifier.build_unified_portfolio(mapping, jira_client, fin_source, AS_OF)
        flagged = [p for p in portfolio if project_unifier.detect_mapping_risks(p)]
        assert {p.project_id for p in flagged} == {"PROJECT-10006", "PROJECT-10007"}


class TestNoClientProvided:
    def test_none_jira_client_behaves_like_unmapped(self):
        _jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], None, fin_source, AS_OF)
        assert project.delivery_status == "UNKNOWN"
        assert project.delivery_progress_pct is None
        # financial side is unaffected
        assert project.approved_budget is not None

    def test_none_financial_source_behaves_like_unmapped(self):
        jira_client, _fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, None, AS_OF)
        assert project.financial_status == "UNKNOWN"
        assert project.approved_budget is None


class TestSourceFailureResilience:
    """Phase 12 hardening: a client raising mid-call (a live source down)
    must degrade exactly like an absent client (None), never crash or
    fabricate data. Regression for a real bug: this function used to let
    such an exception propagate uncaught, crashing the whole graph via
    unify_projects, which calls this directly."""

    def test_jira_client_exception_degrades_to_unknown_not_a_crash(self):
        from src.connectors.jira_client import JiraClient, JiraDataSource

        class BrokenJiraSource(JiraDataSource):
            def fetch_issue_page(self, start_at, max_results, project_key=None, sprint_id=None):
                raise ConnectionError("simulated Jira outage")

            def fetch_issue_by_key(self, issue_key):
                raise ConnectionError("simulated Jira outage")

        default = build_default_jira_client()
        broken_client = JiraClient(source=BrokenJiraSource(), field_map=default.field_map, status_cfg=default.status_cfg)
        _jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()

        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], broken_client, fin_source, AS_OF)

        assert project.delivery_status == "UNKNOWN"
        assert project.delivery_progress_pct is None
        assert project.approved_budget is not None  # financial side, backed by a working source, is unaffected

    def test_financial_source_exception_degrades_to_unknown_not_a_crash(self):
        from src.connectors.financial_client import FinancialDataSource

        class BrokenFinancialSource(FinancialDataSource):
            def get_project_finances(self, project_id, reporting_period):
                raise ConnectionError("simulated financial DB outage")

            def get_portfolio_finances(self, reporting_period):
                raise ConnectionError("simulated financial DB outage")

            def get_latest_reporting_period(self, project_id):
                raise ConnectionError("simulated financial DB outage")

        jira_client, _fin_source = _clients()
        mapping = project_unifier.load_project_mapping()

        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, BrokenFinancialSource(), AS_OF)

        assert project.financial_status == "UNKNOWN"
        assert project.approved_budget is None
        assert project.delivery_progress_pct is not None  # Jira side, backed by a working client, is unaffected
