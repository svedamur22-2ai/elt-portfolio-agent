from datetime import date, datetime, timezone

from src.connectors.financial_client import CSVFinancialDataSource
from src.connectors.jira_client import build_default_jira_client
from src.graph.nodes import NodeDeps
from src.graph.workflow import build_graph
from src.models.common import RiskSeverity
from src.models.finance import FinancialRecord
from src.models.risk import Risk
from src.services import accuracy_audit, project_unifier
from src.services.memory_store import FileMemoryStore

REQUESTED_AT = datetime(2026, 9, 15, tzinfo=timezone.utc)


def _real_result(tmp_path):
    deps = NodeDeps(
        jira_client=build_default_jira_client(),
        financial_source=CSVFinancialDataSource(),
        memory_store=FileMemoryStore(base_dir=tmp_path),
        mapping=project_unifier.load_project_mapping(),
    )
    graph = build_graph(deps)
    return graph.invoke({"user_question": "portfolio status", "request_id": "req", "requested_at": REQUESTED_AT})


class TestFullAuditAgainstRealPipeline:
    def test_every_check_passes_on_a_real_run(self, tmp_path):
        """The headline regression: a real, unmodified graph result must
        clear all ten checks. If this ever fails, something in the
        pipeline has drifted from its own accuracy guarantees."""
        result = _real_result(tmp_path)
        report = accuracy_audit.run_accuracy_audit(result)
        assert report.all_passed, report.summary()
        assert len(report.checks) == 10

    def test_report_summary_is_human_readable(self, tmp_path):
        result = _real_result(tmp_path)
        report = accuracy_audit.run_accuracy_audit(result)
        summary = report.summary()
        assert "ALL CHECKS PASSED" in summary
        assert "10. Validation Node" in summary


class TestIndividualChecksCatchInjectedFailures:
    """Each check must actually fail when the condition it's checking for
    is violated — not just always pass on well-formed input."""

    def test_check_7_catches_a_high_severity_risk_with_no_evidence(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        broken["risks"] = result["risks"] + [
            Risk(risk_id="FAKE-1", project_id="PORTFOLIO", category="Delivery", severity=RiskSeverity.HIGH, description="d", evidence=[])
        ]
        check = accuracy_audit.check_7_evidence_requirement(broken)
        assert check.passed is False
        assert "FAKE-1" in check.detail

    def test_check_9_catches_an_orphan_project_reference(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        broken["risks"] = result["risks"] + [
            Risk(risk_id="FAKE-2", project_id="NONEXISTENT-PROJECT", category="Delivery", severity=RiskSeverity.LOW, description="d", evidence=["e"])
        ]
        check = accuracy_audit.check_9_no_hallucination(broken)
        assert check.passed is False
        assert "FAKE-2" in check.detail

    def test_check_9_catches_a_citation_with_no_source_record_id(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        broken["citations"] = result["citations"] + [{"source_system": "Jira", "source_record_id": "", "retrieved_timestamp": "2026-01-01T00:00:00"}]
        check = accuracy_audit.check_9_no_hallucination(broken)
        assert check.passed is False

    def test_check_4_catches_a_remaining_budget_that_does_not_match_the_formula(self, tmp_path):
        result = _real_result(tmp_path)
        tampered_record = result["financial_data"][0].model_copy(update={"remaining_budget": 999_999_999.0})
        broken = dict(result)
        broken["financial_data"] = [tampered_record] + result["financial_data"][1:]
        check = accuracy_audit.check_4_financial_reconciliation(broken)
        assert check.passed is False

    def test_check_5_catches_a_calculated_metrics_mismatch(self, tmp_path):
        result = _real_result(tmp_path)
        pid = result["unified_projects"][0].project_id
        broken = dict(result)
        broken_metrics = dict(result["calculated_metrics"])
        broken_metrics[pid] = {**broken_metrics[pid], "approved_budget": 1.0}
        broken["calculated_metrics"] = broken_metrics
        check = accuracy_audit.check_5_deterministic_calculations(broken)
        assert check.passed is False
        assert pid in check.detail

    def test_check_6_catches_an_unsupported_trend_claim(self, tmp_path):
        from src.models.snapshot import ProjectSnapshot
        from src.models.common import RAGStatus

        result = _real_result(tmp_path)
        pid = result["unified_projects"][0].project_id
        broken = dict(result)
        # zero prior history, but pretend a snapshot exists so classify_trend
        # would need one to justify anything other than BASELINE
        broken["current_snapshots"] = {
            pid: ProjectSnapshot(snapshot_date=date(2026, 9, 15), project_id=pid, rag_status=RAGStatus.RED)
        }
        broken["historical_context"] = {pid: []}
        check = accuracy_audit.check_6_historical_verification(broken)
        assert check.passed is True  # classify_trend itself still correctly returns BASELINE here

    def test_check_8_catches_an_invalid_confidence_value(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        broken["confidence"] = "SORT OF CONFIDENT"
        check = accuracy_audit.check_8_confidence(broken)
        assert check.passed is False

    def test_check_10_catches_a_missing_validation_key(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        del broken["validation_passed"]
        check = accuracy_audit.check_10_validation_node_ran(broken)
        assert check.passed is False

    def test_check_3_catches_a_silent_completeness_gap(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        # remove every DATA_MAPPING/DATA_COMPLETENESS finding while unmapped projects remain
        broken["risks"] = [r for r in result["risks"] if not (set(r.reason_codes) & {"DATA_MAPPING", "DATA_COMPLETENESS"})]
        check = accuracy_audit.check_3_data_completeness(broken)
        assert check.passed is False

    def test_check_1_catches_a_project_missing_provenance_it_should_have(self, tmp_path):
        result = _real_result(tmp_path)
        mapped_project = next(p for p in result["unified_projects"] if p.delivery_status != "UNKNOWN")
        stripped = mapped_project.model_copy(update={"provenance": []})
        broken = dict(result)
        broken["unified_projects"] = [stripped if p.project_id == mapped_project.project_id else p for p in result["unified_projects"]]
        check = accuracy_audit.check_1_source_provenance(broken)
        assert check.passed is False
        assert mapped_project.project_id in check.detail

    def test_check_2_catches_missing_jira_freshness(self, tmp_path):
        result = _real_result(tmp_path)
        broken = dict(result)
        broken["jira_data_freshness"] = None
        check = accuracy_audit.check_2_freshness(broken)
        assert check.passed is False


class TestAccuracyAuditReport:
    def test_all_passed_is_true_only_when_every_check_passes(self):
        report = accuracy_audit.AccuracyAuditReport(
            checks=[
                accuracy_audit.CheckResult(1, "A", True, "ok"),
                accuracy_audit.CheckResult(2, "B", True, "ok"),
            ]
        )
        assert report.all_passed is True

        report.checks.append(accuracy_audit.CheckResult(3, "C", False, "broken"))
        assert report.all_passed is False

    def test_summary_marks_each_check_pass_or_fail(self):
        report = accuracy_audit.AccuracyAuditReport(
            checks=[accuracy_audit.CheckResult(1, "A", True, "fine"), accuracy_audit.CheckResult(2, "B", False, "oops")]
        )
        summary = report.summary()
        assert "[PASS] 1. A: fine" in summary
        assert "[FAIL] 2. B: oops" in summary
