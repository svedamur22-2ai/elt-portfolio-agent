from datetime import datetime, timezone

import pytest

from src.connectors.financial_client import CSVFinancialDataSource, FinancialDataSource
from src.connectors.jira_client import JiraClient, JiraDataSource, JiraPage, build_default_jira_client
from src.graph import nodes
from src.graph.workflow import build_graph
from src.models.common import RAGStatus
from src.services import project_unifier
from src.services.memory_store import FileMemoryStore, MemoryScope

REQUESTED_AT = datetime(2026, 9, 15, tzinfo=timezone.utc)


class _BrokenFinancialSource(FinancialDataSource):
    """Simulates a totally unreachable live financial source (SQL/Snowflake/
    REST timeout, auth failure, etc.) — every method raises."""

    def get_project_finances(self, project_id, reporting_period):
        raise ConnectionError("simulated financial DB outage")

    def get_portfolio_finances(self, reporting_period):
        raise ConnectionError("simulated financial DB outage")

    def get_latest_reporting_period(self, project_id):
        raise ConnectionError("simulated financial DB outage")


def _deps(tmp_path, jira_client=None):
    return nodes.NodeDeps(
        jira_client=jira_client or build_default_jira_client(),
        financial_source=CSVFinancialDataSource(),
        memory_store=FileMemoryStore(base_dir=tmp_path),
        mapping=project_unifier.load_project_mapping(),
    )


def _initial_state(question="What is the status of our portfolio?", requested_at=REQUESTED_AT):
    return {"user_question": question, "request_id": "req-test", "requested_at": requested_at}


# --------------------------------------------------------------------------
# classify_request
# --------------------------------------------------------------------------


class TestClassifyRequest:
    def test_no_match_scopes_to_whole_portfolio(self, tmp_path):
        node = nodes.make_classify_request(_deps(tmp_path))
        result = node(_initial_state("What needs my attention this week?"))
        assert result["project_filter"] is None
        assert result["intent"] == "portfolio_status"

    def test_matches_project_name_case_insensitively(self, tmp_path):
        node = nodes.make_classify_request(_deps(tmp_path))
        result = node(_initial_state("why is phoenix platform modernization red?"))
        assert result["project_filter"] == ["PROJECT-10001"]
        assert result["intent"] == "project_deep_dive"

    def test_matches_jira_key(self, tmp_path):
        node = nodes.make_classify_request(_deps(tmp_path))
        result = node(_initial_state("show me ORCA's blockers"))
        assert result["project_filter"] == ["PROJECT-10002"]

    def test_matches_mapping_key_directly(self, tmp_path):
        node = nodes.make_classify_request(_deps(tmp_path))
        result = node(_initial_state("give me details on PROJECT-10007"))
        assert result["project_filter"] == ["PROJECT-10007"]

    def test_does_not_fetch_any_data(self, tmp_path):
        """classify_request must be pure — no state keys beyond intent/project_filter."""
        node = nodes.make_classify_request(_deps(tmp_path))
        result = node(_initial_state())
        assert set(result.keys()) == {"intent", "project_filter"}

    def test_explicit_project_filter_is_never_overwritten_by_a_non_matching_question(self, tmp_path):
        """Regression: a caller that already knows which project(s) it
        wants (a notebook, a UI page with its own selector) was having
        that explicit answer silently discarded and replaced with None the
        moment the free-text question didn't happen to mention the project
        by name — even though the caller never asked for text-based
        inference at all."""
        node = nodes.make_classify_request(_deps(tmp_path))
        state = {**_initial_state("status update"), "project_filter": ["PROJECT-10001", "PROJECT-10004"]}
        result = node(state)
        assert result["project_filter"] == ["PROJECT-10001", "PROJECT-10004"]

    def test_explicit_empty_list_project_filter_is_also_respected(self, tmp_path):
        node = nodes.make_classify_request(_deps(tmp_path))
        state = {**_initial_state("status update"), "project_filter": []}
        result = node(state)
        assert result["project_filter"] == []

    def test_still_infers_from_text_when_caller_provides_no_filter_at_all(self, tmp_path):
        node = nodes.make_classify_request(_deps(tmp_path))
        state = _initial_state("why is phoenix platform modernization red?")
        assert "project_filter" not in state
        result = node(state)
        assert result["project_filter"] == ["PROJECT-10001"]


# --------------------------------------------------------------------------
# fetch/validate delivery
# --------------------------------------------------------------------------


class TestFetchValidateDelivery:
    def test_fetch_scoped_to_project_filter(self, tmp_path):
        deps = _deps(tmp_path)
        node = nodes.make_fetch_delivery_data(deps)
        state = {**_initial_state(), "project_filter": ["PROJECT-10001"]}
        result = node(state)
        assert len(result["jira_issues"]) == 37  # known PHX issue count
        assert all(i.project_id == "10001" for i in result["jira_issues"])
        assert result["jira_fetch_partial_failure"] is False

    def test_fetch_whole_portfolio_when_no_filter(self, tmp_path):
        deps = _deps(tmp_path)
        node = nodes.make_fetch_delivery_data(deps)
        result = node({**_initial_state(), "project_filter": None})
        assert len(result["jira_issues"]) == 270  # full sample dataset

    def test_partial_failure_is_surfaced_as_a_data_quality_risk(self, tmp_path):
        class BrokenSource(JiraDataSource):
            def fetch_issue_page(self, start_at, max_results, project_key=None, sprint_id=None):
                raise ConnectionError("simulated outage")

            def fetch_issue_by_key(self, issue_key):
                return None

        broken_client = JiraClient(source=BrokenSource(), field_map=build_default_jira_client().field_map, status_cfg=build_default_jira_client().status_cfg)
        deps = _deps(tmp_path, jira_client=broken_client)

        fetch_result = nodes.make_fetch_delivery_data(deps)({**_initial_state(), "project_filter": ["PROJECT-10001"]})
        assert fetch_result["jira_fetch_partial_failure"] is True

        validate_result = nodes.make_validate_delivery_data(deps)(
            {**_initial_state(), "jira_fetch_partial_failure": True, "jira_data_freshness": None}
        )
        assert any("DATA_FETCH_INCOMPLETE" in r.reason_codes for r in validate_result["risks"])
        assert any(not check["passed"] for check in validate_result["delivery_validation"])

    def test_stale_jira_data_produces_a_medium_risk(self, tmp_path):
        deps = _deps(tmp_path)
        ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
        result = nodes.make_validate_delivery_data(deps)(
            {**_initial_state(), "jira_data_freshness": ancient, "jira_fetch_partial_failure": False}
        )
        stale_risks = [r for r in result["risks"] if "DATA_STALE" in r.reason_codes]
        assert len(stale_risks) == 1
        assert stale_risks[0].severity.value == "MEDIUM"


# --------------------------------------------------------------------------
# fetch/validate financial
# --------------------------------------------------------------------------


class TestFetchValidateFinancial:
    def test_fetch_returns_calculated_records(self, tmp_path):
        deps = _deps(tmp_path)
        result = nodes.make_fetch_financial_data(deps)({**_initial_state(), "project_filter": ["PROJECT-10001"]})
        assert len(result["financial_data"]) == 1
        record = result["financial_data"][0]
        assert record.remaining_budget is not None  # calculated, not raw

    def test_unmapped_project_produces_no_financial_record(self, tmp_path):
        deps = _deps(tmp_path)
        result = nodes.make_fetch_financial_data(deps)({**_initial_state(), "project_filter": ["PROJECT-10006"]})
        assert result["financial_data"] == []

    def test_validate_flags_known_reconciliation_mismatch(self, tmp_path):
        deps = _deps(tmp_path)
        fetch_result = nodes.make_fetch_financial_data(deps)({**_initial_state(), "project_filter": ["PROJECT-10001"]})
        state = {**_initial_state(), "project_filter": ["PROJECT-10001"], "financial_data": fetch_result["financial_data"]}
        validate_result = nodes.make_validate_financial_data(deps)(state)
        assert any("DATA_INCONSISTENT" in r.reason_codes for r in validate_result["risks"])

    def test_validate_skips_projects_with_no_finance_mapping(self, tmp_path):
        deps = _deps(tmp_path)
        state = {**_initial_state(), "project_filter": ["PROJECT-10006"], "financial_data": []}
        result = nodes.make_validate_financial_data(deps)(state)
        assert result["financial_validation"] == []
        assert result["risks"] == []

    def test_total_financial_source_failure_is_caught_not_crashed(self, tmp_path):
        """Regression: a live FinancialDataSource exception used to
        propagate uncaught and crash the entire graph invocation — no
        analogue existed to fetch_delivery_data's Jira partial-failure
        handling. Fixed by catching per-project in fetch_financial_data."""
        broken_source = _BrokenFinancialSource()
        deps = nodes.NodeDeps(
            jira_client=build_default_jira_client(),
            financial_source=broken_source,
            memory_store=FileMemoryStore(base_dir=tmp_path),
            mapping=project_unifier.load_project_mapping(),
        )
        result = nodes.make_fetch_financial_data(deps)({**_initial_state(), "project_filter": ["PROJECT-10001"]})
        assert result["financial_data"] == []
        assert result["financial_fetch_partial_failure"] is True

    def test_validate_financial_data_surfaces_the_fetch_failure_as_a_structural_risk(self, tmp_path):
        deps = _deps(tmp_path)
        state = {**_initial_state(), "financial_fetch_partial_failure": True}
        result = nodes.make_validate_financial_data(deps)(state)
        assert any("DATA_FETCH_INCOMPLETE" in r.reason_codes for r in result["risks"])
        assert any(not check["passed"] for check in result["financial_validation"])


# --------------------------------------------------------------------------
# unify_projects
# --------------------------------------------------------------------------


class TestUnifyProjects:
    def test_mapping_gaps_produce_risks(self, tmp_path):
        deps = _deps(tmp_path)
        result = nodes.make_unify_projects(deps)({**_initial_state(), "project_filter": ["PROJECT-10006", "PROJECT-10007"]})
        assert len(result["unified_projects"]) == 2
        assert len(result["risks"]) == 2
        assert all("DATA_MAPPING" in r.reason_codes for r in result["risks"])

    def test_fully_mapped_project_has_no_mapping_risk(self, tmp_path):
        deps = _deps(tmp_path)
        result = nodes.make_unify_projects(deps)({**_initial_state(), "project_filter": ["PROJECT-10001"]})
        assert result["risks"] == []


# --------------------------------------------------------------------------
# calculate_metrics
# --------------------------------------------------------------------------


class TestCalculateMetrics:
    def test_phx_metrics_match_known_figures(self, tmp_path):
        deps = _deps(tmp_path)
        state = {**_initial_state(), "project_filter": ["PROJECT-10001"]}
        state.update(nodes.make_fetch_delivery_data(deps)(state))
        state.update(nodes.make_fetch_financial_data(deps)(state))

        result = nodes.make_calculate_metrics(deps)(state)
        m = result["calculated_metrics"]["PROJECT-10001"]
        assert m["latest_sprint_id"] == "PHX-SPR-3"
        assert m["sprint_completion_pct"] == 80.0
        assert m["blocked_issue_count"] == 4

    def test_latest_sprint_prefers_closed_over_a_later_but_still_active_sprint(self, tmp_path):
        """Regression: PHX-SPR-3 (2026-02-02 to 2026-02-15) is ACTIVE as of
        2026-02-05, with PHX-SPR-2 (ends 2026-02-01) the latest CLOSED
        sprint at that point. `_latest_sprint_for` must report PHX-SPR-2's
        25% completion, not PHX-SPR-3's already-fully-baked-in 80% just
        because it ends later — this dataset gives every issue a fixed
        final status regardless of `as_of`, so an in-progress sprint's
        completion_pct is not a meaningful "as of this date" answer."""
        from datetime import datetime, timezone

        deps = _deps(tmp_path)
        state = {"project_filter": ["PROJECT-10001"], "requested_at": datetime(2026, 2, 5, tzinfo=timezone.utc)}
        state.update(nodes.make_fetch_delivery_data(deps)(state))
        state.update(nodes.make_fetch_financial_data(deps)(state))

        result = nodes.make_calculate_metrics(deps)(state)
        m = result["calculated_metrics"]["PROJECT-10001"]
        assert m["latest_sprint_id"] == "PHX-SPR-2"
        assert m["sprint_completion_pct"] == 25.0


# --------------------------------------------------------------------------
# risk analysis nodes
# --------------------------------------------------------------------------


class TestRiskAnalysisNodes:
    def _run_up_to_unify(self, deps, project_filter):
        state = {**_initial_state(), "project_filter": project_filter}
        state.update(nodes.make_fetch_delivery_data(deps)(state))
        state.update(nodes.make_fetch_financial_data(deps)(state))
        state.update(nodes.make_unify_projects(deps)(state))
        return state

    def test_delivery_and_financial_risk_share_the_project_id_scheme(self, tmp_path):
        deps = _deps(tmp_path)
        state = self._run_up_to_unify(deps, ["PROJECT-10001"])

        delivery_result = nodes.make_analyze_delivery_risk(deps)(state)
        state["risks"] = delivery_result["risks"]
        financial_result = nodes.make_analyze_financial_risk(deps)(state)

        assert delivery_result["risks"][0].project_id == "PROJECT-10001"
        assert financial_result["risks"][0].project_id == "PROJECT-10001"

    def test_cross_domain_sets_risk_status_to_red_for_phx(self, tmp_path):
        deps = _deps(tmp_path)
        state = self._run_up_to_unify(deps, ["PROJECT-10001"])
        state["risks"] = []
        state["risks"] = nodes.make_analyze_delivery_risk(deps)(state)["risks"]
        state["risks"] = state["risks"] + nodes.make_analyze_financial_risk(deps)(state)["risks"]

        result = nodes.make_analyze_cross_domain_risk(deps)(state)
        assert result["unified_projects"][0].risk_status == RAGStatus.RED

    def test_unmapped_project_gets_amber_never_green(self, tmp_path):
        deps = _deps(tmp_path)
        state = self._run_up_to_unify(deps, ["PROJECT-10006"])
        state["risks"] = nodes.make_analyze_delivery_risk(deps)(state)["risks"]

        result = nodes.make_analyze_cross_domain_risk(deps)(state)
        assert result["unified_projects"][0].risk_status == RAGStatus.AMBER


# --------------------------------------------------------------------------
# validate_findings
# --------------------------------------------------------------------------


class TestValidateFindings:
    def test_high_confidence_with_no_data_quality_risks(self, tmp_path):
        deps = _deps(tmp_path)
        result = nodes.make_validate_findings(deps)({**_initial_state(), "risks": []})
        assert result["confidence"] == "HIGH CONFIDENCE"
        assert result["validation_passed"] is True

    def test_partial_failure_fails_validation(self, tmp_path):
        from src.models.risk import Risk
        from src.models.common import RiskSeverity

        deps = _deps(tmp_path)
        risk = Risk(
            risk_id="x", project_id="PORTFOLIO", category="Data Quality", severity=RiskSeverity.HIGH,
            description="d", evidence=["e"], reason_codes=["DATA_FETCH_INCOMPLETE"],
        )
        result = nodes.make_validate_findings(deps)({**_initial_state(), "risks": [risk]})
        assert result["validation_passed"] is False

    def test_two_degradation_signals_yield_low_confidence(self, tmp_path):
        from src.models.risk import Risk
        from src.models.common import RiskSeverity
        from src.models.project import Project

        deps = _deps(tmp_path)
        stub_projects = [
            Project(project_id="P1", project_key="P1", project_name="P1", project_manager="a", business_owner="b", technical_owner="c", project_status="Active"),
            Project(project_id="P2", project_key="P2", project_name="P2", project_manager="a", business_owner="b", technical_owner="c", project_status="Active"),
        ]
        risks = [
            Risk(risk_id="1", project_id="P1", category="Data Quality", severity=RiskSeverity.MEDIUM, description="d", evidence=["e"], reason_codes=["DATA_STALE"]),
            Risk(risk_id="2", project_id="P2", category="Data Quality", severity=RiskSeverity.MEDIUM, description="d", evidence=["e"], reason_codes=["DATA_INCONSISTENT"]),
        ]
        result = nodes.make_validate_findings(deps)({**_initial_state(), "risks": risks, "unified_projects": stub_projects})
        assert result["confidence"] == "LOW CONFIDENCE"
        assert result["validation_passed"] is True  # neither is a structural fetch failure

    def test_evidence_completeness_check_fails_validation_when_violated(self, tmp_path):
        """Regression guard for the Phase 11 enforcement: a HIGH-severity
        risk with no evidence should never reach a final report undetected."""
        from src.models.risk import Risk
        from src.models.common import RiskSeverity

        deps = _deps(tmp_path)
        risk = Risk(risk_id="1", project_id="PORTFOLIO", category="Delivery", severity=RiskSeverity.HIGH, description="d", evidence=[])
        result = nodes.make_validate_findings(deps)({**_initial_state(), "risks": [risk]})
        assert result["validation_passed"] is False
        evidence_check = next(r for r in result["validation_results"] if r["check_name"] == "evidence_completeness")
        assert evidence_check["passed"] is False

    def test_orphan_project_reference_fails_validation(self, tmp_path):
        from src.models.risk import Risk
        from src.models.common import RiskSeverity

        deps = _deps(tmp_path)
        risk = Risk(risk_id="1", project_id="NONEXISTENT-PROJECT", category="Delivery", severity=RiskSeverity.LOW, description="d", evidence=["e"])
        result = nodes.make_validate_findings(deps)({**_initial_state(), "risks": [risk], "unified_projects": []})
        assert result["validation_passed"] is False
        ref_check = next(r for r in result["validation_results"] if r["check_name"] == "project_reference_integrity")
        assert ref_check["passed"] is False

    def test_memory_staleness_degrades_confidence(self, tmp_path):
        from src.models.project import Project
        from src.models.snapshot import ProjectSnapshot
        from src.models.common import RAGStatus
        from datetime import date, datetime, timezone

        deps = _deps(tmp_path)
        project = Project(project_id="P1", project_key="P1", project_name="P1", project_manager="a", business_owner="b", technical_owner="c", project_status="Active")
        old_snapshot = ProjectSnapshot(snapshot_date=date(2025, 1, 1), project_id="P1", rag_status=RAGStatus.RED)

        result = nodes.make_validate_findings(deps)(
            {
                **_initial_state(),
                "risks": [],
                "unified_projects": [project],
                "historical_context": {"P1": [old_snapshot]},
                "requested_at": datetime(2026, 9, 15, tzinfo=timezone.utc),
            }
        )
        assert result["confidence"] != "HIGH CONFIDENCE"
        assert any("DATA_STALE" in r.reason_codes for r in result["risks"])


# --------------------------------------------------------------------------
# Full graph, end to end
# --------------------------------------------------------------------------


class TestFullGraph:
    def test_portfolio_wide_run_matches_prior_phases_regression_pin(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())

        rag_by_id = {p.project_id: p.risk_status for p in result["unified_projects"]}
        assert rag_by_id["PROJECT-10001"] == RAGStatus.RED
        assert rag_by_id["PROJECT-10002"] == RAGStatus.RED
        assert rag_by_id["PROJECT-10003"] == RAGStatus.RED
        assert rag_by_id["PROJECT-10004"] == RAGStatus.RED
        assert rag_by_id["PROJECT-10005"] == RAGStatus.RED
        assert rag_by_id["PROJECT-10006"] == RAGStatus.AMBER
        assert rag_by_id["PROJECT-10007"] == RAGStatus.AMBER

    def test_full_run_never_produces_an_orphan_risk_reference(self, tmp_path):
        """Regression: a full portfolio run used to fail its own
        project_reference_integrity check — every reconciliation-failure
        Risk was stamped with the raw finance id ("10001") instead of the
        mapping key ("PROJECT-10001"), making validate_findings flag them
        all as orphans. Fixed in reconciliation.detect_reconciliation_failure."""
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())

        assert result["validation_passed"] is True
        ref_check = next(r for r in result["validation_results"] if r["check_name"] == "project_reference_integrity")
        assert ref_check["passed"] is True

        known_ids = {p.project_id for p in result["unified_projects"]} | {nodes.PORTFOLIO_SENTINEL}
        assert all(r.project_id in known_ids for r in result["risks"])

    def test_total_financial_outage_degrades_the_whole_graph_gracefully(self, tmp_path):
        """Regression (Phase 12 hardening): an unhandled FinancialDataSource
        exception used to crash the entire graph invocation — first inside
        fetch_financial_data, and even after fixing that, again inside
        unify_projects, which independently re-queries the same source
        without going through fetch_financial_data's try/except at all."""
        deps = nodes.NodeDeps(
            jira_client=build_default_jira_client(),
            financial_source=_BrokenFinancialSource(),
            memory_store=FileMemoryStore(base_dir=tmp_path),
            mapping=project_unifier.load_project_mapping(),
        )
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())  # must not raise

        assert result["financial_data"] == []
        assert result["validation_passed"] is False
        assert result["confidence"] == "LOW CONFIDENCE"
        assert all(p.financial_status == "UNKNOWN" for p in result["unified_projects"] if p.approved_budget is None)
        assert result["final_answer"]  # a report is still produced

    def test_scoped_query_only_processes_the_matched_project(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state("Why is Phoenix Platform Modernization at risk?"))

        assert len(result["unified_projects"]) == 1
        assert result["unified_projects"][0].project_id == "PROJECT-10001"
        assert len(result["jira_issues"]) == 37

    def test_final_answer_and_citations_are_populated(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state("Why is Phoenix Platform Modernization at risk?"))

        assert "Phoenix Platform Modernization" in result["final_answer"]
        assert "RED" in result["final_answer"]
        assert len(result["citations"]) >= 2  # at least Jira + Finance
        assert any(c["source_system"] == "Jira" for c in result["citations"])
        assert any(c["source_system"] == "Finance" for c in result["citations"])

    def test_persist_snapshot_writes_one_snapshot_per_project(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())

        assert result["snapshot_persisted"] is True
        assert len(result["snapshots_written"]) == len(result["unified_projects"])

    def test_current_snapshots_exposed_and_match_what_was_persisted(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())

        assert set(result["current_snapshots"].keys()) == {p.project_id for p in result["unified_projects"]}
        phx_snapshot = result["current_snapshots"]["PROJECT-10001"]
        assert phx_snapshot.sprint_completion_pct == 80.0

        scope = MemoryScope(deps.mapping.organization_id, deps.mapping.portfolio_id)
        stored = deps.memory_store.get_snapshots(scope, "PROJECT-10001")
        assert stored[-1].sprint_completion_pct == phx_snapshot.sprint_completion_pct

    def test_second_run_finds_history_from_the_first(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)

        graph.invoke(_initial_state(requested_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
        second_result = graph.invoke(_initial_state(requested_at=datetime(2026, 9, 8, tzinfo=timezone.utc)))

        assert len(second_result["historical_context"]["PROJECT-10001"]) == 1  # the first run's snapshot

    def test_low_confidence_does_not_block_a_final_answer(self, tmp_path):
        """Accuracy Check 10: low confidence downgrades trust in the
        answer, it doesn't prevent one from being produced."""
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())
        assert result["confidence"] == "LOW CONFIDENCE"  # this dataset is genuinely stale/inconsistent
        assert result["final_answer"]  # still produced

    def test_portfolio_wide_request_produces_the_full_weekly_report_format(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())
        for section in ["# Weekly ELT Portfolio Report", "## Executive Summary", "## Top Risks", "## Executive Actions"]:
            assert section in result["final_answer"]

    def test_scoped_request_produces_the_chat_answer_format_instead(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        result = graph.invoke(_initial_state("Why is Phoenix Platform Modernization at risk?"))
        for label in ["Question:", "Answer:", "Evidence:", "Trend:", "Recommendation:", "Sources:"]:
            assert label in result["final_answer"]
        assert "# Weekly ELT Portfolio Report" not in result["final_answer"]

    def test_llm_narration_replaces_executive_summary_when_chat_model_configured(self, tmp_path):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        deps = _deps(tmp_path)
        deps.chat_model = FakeListChatModel(responses=["This is the narrated executive summary."])
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())
        assert "This is the narrated executive summary." in result["final_answer"]

    def test_narration_failure_falls_back_to_deterministic_summary(self, tmp_path):
        """A broken chat model must never take down the whole report."""
        from langchain_core.runnables import Runnable

        class BrokenChatModel(Runnable):
            def invoke(self, input, config=None, **kwargs):
                raise RuntimeError("simulated LLM outage")

        deps = _deps(tmp_path)
        deps.chat_model = BrokenChatModel()
        graph = build_graph(deps)
        result = graph.invoke(_initial_state())
        assert "tracked project(s)" in result["final_answer"]  # deterministic phrasing, not a crash

    def test_second_run_shows_week_over_week_movement_not_just_baseline(self, tmp_path):
        deps = _deps(tmp_path)
        graph = build_graph(deps)
        graph.invoke(_initial_state(requested_at=datetime(2026, 9, 1, tzinfo=timezone.utc)))
        second = graph.invoke(_initial_state(requested_at=datetime(2026, 9, 8, tzinfo=timezone.utc)))
        wow_section = second["final_answer"].split("## Week-over-Week Changes")[1].split("## Persistent Blockers")[0]
        assert "baseline): none" in wow_section or "No prior data (baseline): none" in wow_section
