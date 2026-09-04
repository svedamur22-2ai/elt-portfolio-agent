from datetime import date

from src.connectors.jira_client import build_default_jira_client
from src.models.common import RiskSeverity
from src.models.issue import JiraIssue
from src.models.sprint import Sprint
from src.services import delivery_metrics

SAMPLE_CSV = "data/sample/jira_mock_data.csv"
AS_OF = date(2026, 3, 22)


def _issue(key, status="TODO", story_points=None, blocked=False, blocker_age_days=None,
           deps=None, sprint_count_blocked=None) -> JiraIssue:
    return JiraIssue(
        issue_key=key,
        project_id="1",
        summary="s",
        issue_type="Story",
        status=status,
        story_points=story_points,
        blocked=blocked,
        blocker_age_days=blocker_age_days,
        linked_dependencies=deps or [],
        sprint_count_blocked=sprint_count_blocked,
    )


def _sprint(completion_pct) -> Sprint:
    return Sprint(
        sprint_id="S1", sprint_name="Sprint 1", project_id="1",
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 14),
        sprint_status="CLOSED",
        committed_story_points=10, completed_story_points=5,
        completion_pct=completion_pct, carryover_story_points=5,
    )


# --------------------------------------------------------------------------
# Individual detectors
# --------------------------------------------------------------------------


class TestDetectLowSprintCompletion:
    def test_high_below_60(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        finding = delivery_metrics.detect_low_sprint_completion("X", _sprint(45.0), rules)
        assert finding.severity == RiskSeverity.HIGH

    def test_medium_between_60_and_80(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        finding = delivery_metrics.detect_low_sprint_completion("X", _sprint(70.0), rules)
        assert finding.severity == RiskSeverity.MEDIUM

    def test_none_at_or_above_80(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        assert delivery_metrics.detect_low_sprint_completion("X", _sprint(80.0), rules) is None
        assert delivery_metrics.detect_low_sprint_completion("X", _sprint(95.0), rules) is None

    def test_none_when_no_sprint_available(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        assert delivery_metrics.detect_low_sprint_completion("X", None, rules) is None


class TestDetectAgedBlocker:
    def test_high_when_oldest_exceeds_7_days(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=True, blocker_age_days=10)]
        finding = delivery_metrics.detect_aged_blocker("X", issues, rules)
        assert finding.severity == RiskSeverity.HIGH
        assert "A-1" in finding.evidence[0]

    def test_medium_when_between_3_and_7(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=True, blocker_age_days=5)]
        finding = delivery_metrics.detect_aged_blocker("X", issues, rules)
        assert finding.severity == RiskSeverity.MEDIUM

    def test_none_when_under_3_days(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=True, blocker_age_days=1)]
        assert delivery_metrics.detect_aged_blocker("X", issues, rules) is None

    def test_none_when_no_blocked_issues(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=False)]
        assert delivery_metrics.detect_aged_blocker("X", issues, rules) is None

    def test_uses_the_oldest_blocker_among_several(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [
            _issue("A-1", blocked=True, blocker_age_days=2),
            _issue("A-2", blocked=True, blocker_age_days=20),
        ]
        finding = delivery_metrics.detect_aged_blocker("X", issues, rules)
        assert finding.severity == RiskSeverity.HIGH
        assert "A-2" in finding.description


class TestDetectUnresolvedDependencies:
    def test_fires_when_dependency_not_done(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [
            _issue("A-1", status="IN_PROGRESS", deps=["A-2"]),
            _issue("A-2", status="TODO"),
        ]
        finding = delivery_metrics.detect_unresolved_dependencies("X", issues, rules)
        assert finding is not None
        assert finding.severity == RiskSeverity.MEDIUM
        assert "DEPENDENCY_RISK" in finding.reason_codes

    def test_silent_when_dependency_is_done(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [
            _issue("A-1", status="IN_PROGRESS", deps=["A-2"]),
            _issue("A-2", status="DONE"),
        ]
        assert delivery_metrics.detect_unresolved_dependencies("X", issues, rules) is None

    def test_silent_when_dependent_issue_itself_already_done(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [
            _issue("A-1", status="DONE", deps=["A-2"]),
            _issue("A-2", status="TODO"),
        ]
        assert delivery_metrics.detect_unresolved_dependencies("X", issues, rules) is None

    def test_dependency_outside_fetched_set_is_unverifiable_not_assumed_resolved(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", status="IN_PROGRESS", deps=["OTHER-PROJ-99"])]
        finding = delivery_metrics.detect_unresolved_dependencies("X", issues, rules)
        assert finding is not None
        assert "unverifiable" in finding.evidence[0]


class TestDetectBlockedMultipleSprints:
    def test_none_when_sprint_count_blocked_is_none_everywhere(self):
        """Current real-world state: no history exists yet (Phase 7)."""
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=True, sprint_count_blocked=None)]
        assert delivery_metrics.detect_blocked_multiple_sprints("X", issues, rules) is None

    def test_fires_once_the_field_is_populated(self):
        """Forward-compatibility check: this detector must already work
        correctly once Phase 7 starts populating sprint_count_blocked,
        without any code change here."""
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=True, sprint_count_blocked=3)]
        finding = delivery_metrics.detect_blocked_multiple_sprints("X", issues, rules)
        assert finding is not None
        assert finding.severity == RiskSeverity.HIGH
        assert "BLOCKED_MULTIPLE_SPRINTS" in finding.reason_codes

    def test_below_threshold_does_not_fire(self):
        rules = delivery_metrics.load_delivery_risk_rules()
        issues = [_issue("A-1", blocked=True, sprint_count_blocked=1)]
        assert delivery_metrics.detect_blocked_multiple_sprints("X", issues, rules) is None


# --------------------------------------------------------------------------
# Combined classification
# --------------------------------------------------------------------------


class TestClassifyDeliveryRisk:
    def test_healthy_project_is_low_with_no_findings(self):
        issues = [_issue("A-1", status="DONE", story_points=5)]
        risk = delivery_metrics.classify_delivery_risk("X", issues, _sprint(90.0))
        assert risk.severity == RiskSeverity.LOW
        assert risk.reason_codes == []

    def test_severity_is_the_max_across_multiple_findings(self):
        issues = [_issue("A-1", blocked=True, blocker_age_days=15)]  # HIGH
        risk = delivery_metrics.classify_delivery_risk("X", issues, _sprint(70.0))  # MEDIUM
        assert risk.severity == RiskSeverity.HIGH
        assert {"AGED_BLOCKER", "LOW_SPRINT_COMPLETION"} <= set(risk.reason_codes)

    def test_every_high_severity_risk_carries_evidence(self):
        """Accuracy Check 7."""
        issues = [_issue("A-1", blocked=True, blocker_age_days=15)]
        risk = delivery_metrics.classify_delivery_risk("X", issues, None)
        assert risk.severity == RiskSeverity.HIGH
        assert len(risk.evidence) > 0

    def test_real_phx_data_reproduces_hand_computed_result(self):
        """Regression pin: PHX has 80% sprint completion (no completion
        finding) but 4 aged blockers (>7 days) — should be HIGH via
        AGED_BLOCKER alone, exactly as established in Phase 1's report."""
        client = build_default_jira_client(csv_path=SAMPLE_CSV)
        issues = client.get_project_issues("PHX", as_of=AS_OF).records
        latest_sprint = client.get_previous_sprints("PHX", 1, as_of=AS_OF)[0]

        assert latest_sprint.completion_pct == 80.0
        risk = delivery_metrics.classify_delivery_risk("PHX", issues, latest_sprint)
        assert risk.severity == RiskSeverity.HIGH
        assert "AGED_BLOCKER" in risk.reason_codes
        assert "LOW_SPRINT_COMPLETION" not in risk.reason_codes

    def test_real_qsr_data_lands_medium_via_completion_only(self):
        """QSR: 64.3% completion (medium band), zero blockers, zero
        unresolved dependencies among its 5 issues."""
        client = build_default_jira_client(csv_path=SAMPLE_CSV)
        issues = client.get_project_issues("QSR", as_of=AS_OF).records
        latest_sprint = client.get_previous_sprints("QSR", 1, as_of=AS_OF)[0]

        risk = delivery_metrics.classify_delivery_risk("QSR", issues, latest_sprint)
        assert risk.severity == RiskSeverity.MEDIUM
        assert risk.reason_codes == ["LOW_SPRINT_COMPLETION"]
