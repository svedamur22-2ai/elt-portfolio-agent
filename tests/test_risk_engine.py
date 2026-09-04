from datetime import date

from src.connectors.financial_client import CSVFinancialDataSource
from src.connectors.jira_client import build_default_jira_client
from src.models.common import RAGStatus, RiskSeverity
from src.services import project_unifier, risk_engine

AS_OF = date(2026, 9, 15)


def _clients():
    return build_default_jira_client(), CSVFinancialDataSource()


class TestComputeRiskScore:
    def test_high_high_is_2(self):
        assert risk_engine.compute_risk_score(RiskSeverity.HIGH, RiskSeverity.HIGH) == 2.0

    def test_low_low_is_0(self):
        assert risk_engine.compute_risk_score(RiskSeverity.LOW, RiskSeverity.LOW) == 0.0

    def test_high_low_averages_to_1(self):
        assert risk_engine.compute_risk_score(RiskSeverity.HIGH, RiskSeverity.LOW) == 1.0

    def test_distinguishes_high_low_from_high_high_unlike_the_rag_matrix(self):
        """Both HIGH+LOW and HIGH+HIGH can combine to different RAGs
        depending on the matrix, but risk_score must never conflate them —
        that's the whole reason this function exists alongside combine_risk."""
        mixed = risk_engine.compute_risk_score(RiskSeverity.HIGH, RiskSeverity.LOW)
        both_high = risk_engine.compute_risk_score(RiskSeverity.HIGH, RiskSeverity.HIGH)
        assert mixed != both_high

    def test_uses_whichever_single_side_is_available(self):
        assert risk_engine.compute_risk_score(RiskSeverity.HIGH, None) == 2.0
        assert risk_engine.compute_risk_score(None, RiskSeverity.LOW) == 0.0

    def test_none_when_neither_side_available(self):
        assert risk_engine.compute_risk_score(None, None) is None


class TestCombineRisk:
    def test_high_high_is_red(self):
        assert risk_engine.combine_risk(RiskSeverity.HIGH, RiskSeverity.HIGH) == RAGStatus.RED

    def test_low_low_is_green(self):
        assert risk_engine.combine_risk(RiskSeverity.LOW, RiskSeverity.LOW) == RAGStatus.GREEN

    def test_medium_medium_is_amber(self):
        assert risk_engine.combine_risk(RiskSeverity.MEDIUM, RiskSeverity.MEDIUM) == RAGStatus.AMBER

    def test_high_low_is_amber(self):
        assert risk_engine.combine_risk(RiskSeverity.HIGH, RiskSeverity.LOW) == RAGStatus.AMBER

    def test_medium_high_is_red(self):
        assert risk_engine.combine_risk(RiskSeverity.MEDIUM, RiskSeverity.HIGH) == RAGStatus.RED

    def test_either_side_none_is_amber_never_green(self):
        assert risk_engine.combine_risk(None, RiskSeverity.LOW) == RAGStatus.AMBER
        assert risk_engine.combine_risk(RiskSeverity.LOW, None) == RAGStatus.AMBER
        assert risk_engine.combine_risk(None, None) == RAGStatus.AMBER

    def test_both_none_never_resolves_to_red_either(self):
        """Missing data means "can't tell", not "assume the worst" either —
        AMBER is a deliberately distinct signal from both GREEN and RED."""
        assert risk_engine.combine_risk(None, None) != RAGStatus.RED
        assert risk_engine.combine_risk(None, None) != RAGStatus.GREEN


class TestAssessProjectRisk:
    def test_fully_mapped_high_risk_project_is_red(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)

        issues = jira_client.get_project_issues("PHX", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("PHX", 1, as_of=AS_OF)[0]
        latest_period = fin_source.get_latest_reporting_period("10001")
        financial_record = fin_source.get_project_finances("10001", latest_period).with_calculated_fields()

        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record)

        assert assessment.delivery_risk.severity == RiskSeverity.HIGH
        assert assessment.financial_risk.severity == RiskSeverity.HIGH
        assert assessment.combined_rag == RAGStatus.RED
        assert "AGED_BLOCKER" in assessment.reason_codes
        assert "HIGH_BUDGET_CONSUMPTION" in assessment.reason_codes

    def test_delivery_and_financial_risk_share_the_same_project_id_scheme(self):
        """Regression: financial_metrics.classify_financial_risk used to
        stamp Risk.project_id from the raw finance-system id (e.g. "10001")
        while delivery_metrics used the mapping key (e.g. "PROJECT-10001").
        Both risks in one RiskAssessment must use the same scheme, or a
        caller filtering `state["risks"]` by project_id (Phase 8) would
        silently miss half of them."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10001"], jira_client, fin_source, AS_OF)

        issues = jira_client.get_project_issues("PHX", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("PHX", 1, as_of=AS_OF)[0]
        latest_period = fin_source.get_latest_reporting_period("10001")
        financial_record = fin_source.get_project_finances("10001", latest_period).with_calculated_fields()

        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record)

        assert assessment.delivery_risk.project_id == "PROJECT-10001"
        assert assessment.financial_risk.project_id == "PROJECT-10001"
        assert assessment.delivery_risk.project_id == assessment.financial_risk.project_id

    def test_jira_only_project_has_no_financial_risk_object(self):
        """QSR — financial_status is UNKNOWN, so financial_risk must be
        None, not a Risk fabricated at some default severity."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10006"], jira_client, fin_source, AS_OF)

        issues = jira_client.get_project_issues("QSR", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("QSR", 1, as_of=AS_OF)[0]

        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record=None)

        assert assessment.financial_risk is None
        assert assessment.delivery_risk is not None
        assert assessment.combined_rag == RAGStatus.AMBER  # capped, never GREEN, per unknown_default

    def test_finance_only_project_has_no_delivery_risk_object(self):
        """Helios — delivery_status is UNKNOWN."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10007"], jira_client, fin_source, AS_OF)

        latest_period = fin_source.get_latest_reporting_period("10007")
        financial_record = fin_source.get_project_finances("10007", latest_period).with_calculated_fields()

        assessment = risk_engine.assess_project_risk(project, jira_issues=None, latest_sprint=None, financial_record=financial_record)

        assert assessment.delivery_risk is None
        assert assessment.financial_risk is not None
        assert assessment.financial_risk.severity == RiskSeverity.LOW  # Helios is a healthy budget
        assert assessment.combined_rag == RAGStatus.AMBER  # still capped despite LOW financial risk

    def test_reason_codes_are_the_union_of_both_sides_deduplicated(self):
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        project = project_unifier.build_unified_project(mapping.entries["PROJECT-10003"], jira_client, fin_source, AS_OF)

        issues = jira_client.get_project_issues("NOVA", as_of=AS_OF).records
        latest_sprint = jira_client.get_previous_sprints("NOVA", 1, as_of=AS_OF)[0]
        latest_period = fin_source.get_latest_reporting_period("10003")
        financial_record = fin_source.get_project_finances("10003", latest_period).with_calculated_fields()

        assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record)
        assert len(assessment.reason_codes) == len(set(assessment.reason_codes))
        assert assessment.reason_codes == sorted(assessment.reason_codes)


class TestPortfolioWideAssessment:
    def test_all_five_mapped_projects_are_red_matching_phase1_report(self):
        """Regression pin against Phase 1's independently hand-computed
        sample report — all 5 fully-mapped projects were found RED."""
        jira_client, fin_source = _clients()
        mapping = project_unifier.load_project_mapping()
        portfolio = project_unifier.build_unified_portfolio(mapping, jira_client, fin_source, AS_OF)

        for project in portfolio:
            if project.project_id in {"PROJECT-10006", "PROJECT-10007"}:
                continue  # the two deliberately-unmapped projects, covered above

            jira_key = project.jira_project_key
            issues = jira_client.get_project_issues(jira_key, as_of=AS_OF).records
            latest_sprint = jira_client.get_previous_sprints(jira_key, 1, as_of=AS_OF)[0]
            finance_pid = mapping.entries[project.project_id].finance_project_id
            latest_period = fin_source.get_latest_reporting_period(finance_pid)
            financial_record = fin_source.get_project_finances(finance_pid, latest_period).with_calculated_fields()

            assessment = risk_engine.assess_project_risk(project, issues, latest_sprint, financial_record)
            assert assessment.combined_rag == RAGStatus.RED, f"{project.project_id} expected RED, got {assessment.combined_rag}"
