from datetime import date

from src.agents import report_agent
from src.models.common import RAGStatus, RiskSeverity, TrendDirection
from src.models.issue import JiraIssue
from src.models.project import Project
from src.models.risk import Risk
from src.models.snapshot import ProjectSnapshot


def _project(project_id, name="Test Project", risk_status=RAGStatus.RED, approved=100_000.0, actual=90_000.0, remaining=5_000.0, delivery_pct=50.0) -> Project:
    return Project(
        project_id=project_id,
        project_key=project_id,
        project_name=name,
        project_manager="PM",
        business_owner="BO",
        technical_owner="TO",
        project_status="Active",
        risk_status=risk_status,
        approved_budget=approved,
        actual_spend=actual,
        remaining_budget=remaining,
        budget_consumption_pct=(actual / approved * 100) if approved else None,
        delivery_progress_pct=delivery_pct,
    )


def _risk(project_id, category, severity, reason_codes=None, evidence=None, recommended_action=None) -> Risk:
    return Risk(
        risk_id=f"{project_id}-{category}",
        project_id=project_id,
        category=category,
        severity=severity,
        description="d",
        evidence=evidence or ["e1"],
        recommended_action=recommended_action,
        reason_codes=reason_codes or [],
    )


def _snapshot(project_id, d, rag=RAGStatus.RED, completion=50.0, consumption=60.0, blocked=2, major_blockers=None) -> ProjectSnapshot:
    return ProjectSnapshot(
        snapshot_date=d,
        project_id=project_id,
        rag_status=rag,
        sprint_completion_pct=completion,
        budget_consumption_pct=consumption,
        blocked_issues=blocked,
        major_blockers=major_blockers or [],
    )


def _issue(key, assignee="Alice", blocker_age_days=10) -> JiraIssue:
    return JiraIssue(
        issue_key=key, project_id="1", summary="s", issue_type="Task", status="BLOCKED",
        blocked=True, assignee=assignee, blocker_age_days=blocker_age_days,
    )


# --------------------------------------------------------------------------
# compute_portfolio_totals
# --------------------------------------------------------------------------


class TestComputePortfolioTotals:
    def test_sums_present_values(self):
        projects = [_project("P1", approved=100_000, actual=50_000, remaining=50_000), _project("P2", approved=200_000, actual=100_000, remaining=100_000)]
        totals = report_agent.compute_portfolio_totals(projects)
        assert totals["approved_budget"] == 300_000
        assert totals["actual_spend"] == 150_000
        assert totals["remaining_budget"] == 150_000

    def test_none_when_no_project_has_financial_data(self):
        p = Project(project_id="P1", project_key="P1", project_name="X", project_manager="a", business_owner="b", technical_owner="c", project_status="Active")
        totals = report_agent.compute_portfolio_totals([p])
        assert totals["approved_budget"] is None

    def test_rag_counts(self):
        projects = [_project("P1", risk_status=RAGStatus.RED), _project("P2", risk_status=RAGStatus.RED), _project("P3", risk_status=RAGStatus.AMBER)]
        totals = report_agent.compute_portfolio_totals(projects)
        assert totals["rag_counts"]["RED"] == 2
        assert totals["rag_counts"]["AMBER"] == 1


# --------------------------------------------------------------------------
# render_weekly_report
# --------------------------------------------------------------------------


class TestRenderWeeklyReport:
    def test_contains_all_required_sections(self):
        projects = [_project("P1")]
        report = report_agent.render_weekly_report(projects, [], [], {}, {}, "HIGH CONFIDENCE")
        for section in [
            "# Weekly ELT Portfolio Report", "## Data Freshness", "## Executive Summary",
            "## Project-Level RAG Status", "## Top Risks", "## Projects Requiring Decisions",
            "## Week-over-Week Changes", "## Persistent Blockers", "## Financial Watchlist",
            "## Executive Actions",
        ]:
            assert section in report

    def test_decisions_section_merges_delivery_and_financial_into_one_block_per_project(self):
        """Regression: this used to print two separate '**Project**' header
        blocks for the same project — one per HIGH-severity risk category —
        instead of one merged decision."""
        projects = [_project("P1", name="Alpha")]
        risks = [
            _risk("P1", "Delivery", RiskSeverity.HIGH, recommended_action="Fix delivery."),
            _risk("P1", "Financial", RiskSeverity.HIGH, recommended_action="Fix budget."),
        ]
        report = report_agent.render_weekly_report(projects, risks, [], {}, {}, "LOW CONFIDENCE")
        assert report.count("**Alpha**") == 1
        assert "Fix delivery." in report
        assert "Fix budget." in report

    def test_executive_actions_capped_at_five_distinct_projects(self):
        projects = [_project(f"P{i}", name=f"Project {i}") for i in range(8)]
        risks = [_risk(f"P{i}", "Delivery", RiskSeverity.HIGH, recommended_action=f"Action {i}") for i in range(8)]
        report = report_agent.render_weekly_report(projects, risks, [], {}, {}, "LOW CONFIDENCE")
        actions_section = report.split("## Executive Actions")[1]
        assert actions_section.count("Action ") <= 8  # sanity: text present
        numbered_lines = [line for line in actions_section.splitlines() if line[:2].strip().rstrip(".").isdigit()]
        assert len(numbered_lines) == 5

    def test_executive_actions_prioritizes_red_over_amber(self):
        projects = [_project("P1", name="RedProject", risk_status=RAGStatus.RED), _project("P2", name="AmberProject", risk_status=RAGStatus.AMBER)]
        risks = [
            _risk("P2", "Delivery", RiskSeverity.HIGH, recommended_action="Amber action"),
            _risk("P1", "Delivery", RiskSeverity.HIGH, recommended_action="Red action"),
        ]
        report = report_agent.render_weekly_report(projects, risks, [], {}, {}, "LOW CONFIDENCE")
        actions_section = report.split("## Executive Actions")[1]
        assert actions_section.index("RedProject") < actions_section.index("AmberProject")

    def test_no_decisions_when_no_high_severity_risks(self):
        projects = [_project("P1", risk_status=RAGStatus.GREEN)]
        report = report_agent.render_weekly_report(projects, [], [], {}, {}, "HIGH CONFIDENCE")
        decisions_section = report.split("## Projects Requiring Decisions")[1].split("## Week-over-Week")[0]
        assert "None this period." in decisions_section

    def test_week_over_week_buckets_correctly(self):
        projects = [
            _project("P1", name="Improved"), _project("P2", name="Deteriorated"),
            _project("P3", name="Stable"), _project("P4", name="Fresh"),
        ]
        historical = {
            "P1": [_snapshot("P1", date(2026, 1, 1), rag=RAGStatus.RED)],
            "P2": [_snapshot("P2", date(2026, 1, 1), rag=RAGStatus.GREEN)],
            "P3": [_snapshot("P3", date(2026, 1, 1), rag=RAGStatus.AMBER)],
        }
        current = {
            "P1": _snapshot("P1", date(2026, 1, 8), rag=RAGStatus.AMBER),
            "P2": _snapshot("P2", date(2026, 1, 8), rag=RAGStatus.RED),
            "P3": _snapshot("P3", date(2026, 1, 8), rag=RAGStatus.AMBER),
            "P4": _snapshot("P4", date(2026, 1, 8), rag=RAGStatus.AMBER),
        }
        report = report_agent.render_weekly_report(projects, [], [], historical, current, "HIGH CONFIDENCE")
        wow_section = report.split("## Week-over-Week Changes")[1].split("## Persistent Blockers")[0]
        assert "Improved" in wow_section.split("Improved:")[1].split("\n")[0]
        assert "Deteriorated" in wow_section.split("Deteriorated:")[1].split("\n")[0]
        assert "Stable" in wow_section.split("No material change:")[1].split("\n")[0]
        assert "Fresh" in wow_section.split("baseline):")[1].split("\n")[0]

    def test_persistent_blockers_populated_with_multi_snapshot_history(self):
        projects = [_project("P1")]
        historical = {"P1": [_snapshot("P1", date(2026, 1, 1), major_blockers=["X-1"])]}
        current = {"P1": _snapshot("P1", date(2026, 1, 8), major_blockers=["X-1"])}
        issues = [_issue("X-1", assignee="Bob", blocker_age_days=15)]
        report = report_agent.render_weekly_report(projects, [], issues, historical, current, "HIGH CONFIDENCE")
        blockers_section = report.split("## Persistent Blockers")[1].split("## Financial Watchlist")[0]
        assert "X-1" in blockers_section
        assert "Bob" in blockers_section

    def test_persistent_blockers_empty_with_only_one_snapshot(self):
        """Accuracy Check 6 — a project's very first report has no prior
        history, so nothing can be called 'persistent' yet."""
        projects = [_project("P1")]
        current = {"P1": _snapshot("P1", date(2026, 1, 1), major_blockers=["X-1"])}
        report = report_agent.render_weekly_report(projects, [], [], {}, current, "HIGH CONFIDENCE")
        blockers_section = report.split("## Persistent Blockers")[1].split("## Financial Watchlist")[0]
        assert "X-1" not in blockers_section

    def test_financial_watchlist_sorted_by_consumption_ascending(self):
        projects = [
            _project("P1", name="High", approved=100, actual=95),
            _project("P2", name="Low", approved=100, actual=40),
        ]
        report = report_agent.render_weekly_report(projects, [], [], {}, {}, "HIGH CONFIDENCE")
        watchlist = report.split("## Financial Watchlist")[1].split("## Executive Actions")[0]
        assert watchlist.index("Low") < watchlist.index("High")

    def test_financial_watchlist_excludes_unmapped_projects(self):
        mapped = _project("P1", approved=100, actual=50)
        unmapped = Project(project_id="P2", project_key="P2", project_name="Unmapped", project_manager="a", business_owner="b", technical_owner="c", project_status="Active", financial_status="UNKNOWN")
        report = report_agent.render_weekly_report([mapped, unmapped], [], [], {}, {}, "HIGH CONFIDENCE")
        watchlist = report.split("## Financial Watchlist")[1].split("## Executive Actions")[0]
        assert "Unmapped" not in watchlist

    def test_executive_summary_override_replaces_deterministic_text(self):
        projects = [_project("P1")]
        report = report_agent.render_weekly_report(projects, [], [], {}, {}, "HIGH CONFIDENCE", executive_summary_override="Custom narrated summary.")
        summary_section = report.split("## Executive Summary")[1].split("## Project-Level RAG")[0]
        assert "Custom narrated summary." in summary_section
        assert "tracked project" not in summary_section  # deterministic phrasing replaced, not appended


# --------------------------------------------------------------------------
# render_chat_answer
# --------------------------------------------------------------------------


class TestRenderChatAnswer:
    def test_contains_all_required_sections(self):
        project = _project("P1", name="Alpha")
        answer = report_agent.render_chat_answer("Why is Alpha red?", project, [], TrendDirection.BASELINE, "HIGH CONFIDENCE", [])
        for label in ["Question:", "Answer:", "Evidence:", "Trend:", "Financial Impact:", "Recommendation:", "Confidence:", "Sources:"]:
            assert label in answer

    def test_evidence_lists_every_risk_line(self):
        project = _project("P1", name="Alpha")
        risks = [_risk("P1", "Delivery", RiskSeverity.HIGH, evidence=["ev1", "ev2"])]
        answer = report_agent.render_chat_answer("q", project, risks, TrendDirection.STABLE, "HIGH CONFIDENCE", [])
        assert "ev1" in answer and "ev2" in answer

    def test_no_sources_says_so_rather_than_an_empty_section(self):
        project = _project("P1")
        answer = report_agent.render_chat_answer("q", project, [], TrendDirection.BASELINE, "LOW CONFIDENCE", [])
        assert "no sources available" in answer

    def test_sources_rendered_from_citations(self):
        project = _project("P1")
        citations = [{"source_system": "Jira", "source_record_id": "PHX", "retrieved_timestamp": "2026-01-01T00:00:00"}]
        answer = report_agent.render_chat_answer("q", project, [], TrendDirection.BASELINE, "HIGH CONFIDENCE", citations)
        assert "Jira: PHX" in answer


# --------------------------------------------------------------------------
# LLM narration — tested against FakeListChatModel, not a real LLM
# --------------------------------------------------------------------------


class TestNarrateExecutiveSummary:
    def test_returns_the_fake_models_response_verbatim(self):
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        fake = FakeListChatModel(responses=["Narrated summary text."])
        projects = [_project("P1")]
        result = report_agent.narrate_executive_summary(fake, projects, [])
        assert result == "Narrated summary text."

    def test_facts_text_includes_only_at_risk_projects_and_real_numbers(self):
        healthy = _project("P1", name="Healthy", risk_status=RAGStatus.GREEN)
        at_risk = _project("P2", name="AtRisk", risk_status=RAGStatus.RED)
        risks = [_risk("P2", "Delivery", RiskSeverity.HIGH, reason_codes=["AGED_BLOCKER"])]

        facts = report_agent.build_narration_facts_text([healthy, at_risk], risks)
        assert "AtRisk" in facts
        assert "AGED_BLOCKER" in facts
        assert "Healthy" not in facts  # GREEN projects aren't called out individually

    def test_prompt_includes_the_no_invention_system_rules(self):
        """The enforcement mechanism is what's actually in the prompt, not
        just a docstring claim — confirm the rules text is really there."""
        assert "Never state a number" in report_agent.NARRATION_SYSTEM_PROMPT
        assert "Never invent a project name" in report_agent.NARRATION_SYSTEM_PROMPT
