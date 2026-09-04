from datetime import date

from src.models.common import RAGStatus, TrendDirection
from src.models.snapshot import ProjectSnapshot
from src.services import trend_engine

THRESHOLDS = {"significant_completion_delta_pct": 5, "significant_consumption_delta_pct": 5}


def _snapshot(
    d=date(2026, 1, 1),
    completion=70.0,
    blocked=2,
    consumption=60.0,
    remaining=1000.0,
    risk_score=1.0,
    open_issues=10,
    rag=RAGStatus.AMBER,
    major_blockers=None,
) -> ProjectSnapshot:
    return ProjectSnapshot(
        snapshot_date=d,
        project_id="P1",
        open_issues=open_issues,
        blocked_issues=blocked,
        sprint_completion_pct=completion,
        budget_consumption_pct=consumption,
        remaining_budget=remaining,
        risk_score=risk_score,
        rag_status=rag,
        major_blockers=major_blockers or [],
    )


# --------------------------------------------------------------------------
# diff_snapshots
# --------------------------------------------------------------------------


class TestDiffSnapshots:
    def test_computes_all_deltas(self):
        prev = _snapshot(completion=72, blocked=2, consumption=61, remaining=5000, risk_score=1.0, open_issues=20, rag=RAGStatus.AMBER)
        curr = _snapshot(completion=55, blocked=5, consumption=70, remaining=3000, risk_score=2.0, open_issues=18, rag=RAGStatus.RED)
        diff = trend_engine.diff_snapshots(prev, curr)

        assert diff.sprint_completion_delta == -17
        assert diff.blocked_issue_delta == 3
        assert diff.budget_consumption_delta == 9
        assert diff.remaining_budget_delta == -2000
        assert diff.risk_score_delta == 1.0
        assert diff.open_issue_delta == -2
        assert diff.rag_status_change == "AMBER -> RED"

    def test_none_when_a_field_is_missing_on_either_side(self):
        prev = ProjectSnapshot(snapshot_date=date(2026, 1, 1), project_id="P1", rag_status=RAGStatus.UNKNOWN)
        curr = _snapshot()
        diff = trend_engine.diff_snapshots(prev, curr)
        assert diff.sprint_completion_delta is None
        assert diff.budget_consumption_delta is None

    def test_no_rag_change_reported_when_unchanged(self):
        prev = _snapshot(rag=RAGStatus.RED)
        curr = _snapshot(rag=RAGStatus.RED)
        assert trend_engine.diff_snapshots(prev, curr).rag_status_change is None


# --------------------------------------------------------------------------
# classify_trend
# --------------------------------------------------------------------------


class TestClassifyTrend:
    def test_baseline_when_no_previous_snapshot(self):
        """Accuracy Check 6: never infer a trend from a single data point,
        no matter how the current snapshot looks."""
        curr = _snapshot(rag=RAGStatus.RED)
        assert trend_engine.classify_trend(None, curr) == TrendDirection.BASELINE

    def test_deteriorating_when_rag_gets_worse(self):
        prev = _snapshot(rag=RAGStatus.AMBER)
        curr = _snapshot(rag=RAGStatus.RED)
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.DETERIORATING

    def test_improving_when_rag_gets_better(self):
        prev = _snapshot(rag=RAGStatus.RED)
        curr = _snapshot(rag=RAGStatus.AMBER)
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.IMPROVING

    def test_section_14_worked_example_is_deteriorating(self):
        """Reproduces Section 14's exact example: completion 72->55,
        blocked 2->5, consumption 61->70, AMBER->RED."""
        prev = _snapshot(completion=72, blocked=2, consumption=61, rag=RAGStatus.AMBER)
        curr = _snapshot(completion=55, blocked=5, consumption=70, rag=RAGStatus.RED)
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.DETERIORATING

    def test_stable_rag_falls_through_to_secondary_signals_deteriorating(self):
        prev = _snapshot(completion=80, consumption=50, blocked=1, rag=RAGStatus.RED)
        curr = _snapshot(completion=60, consumption=60, blocked=3, rag=RAGStatus.RED)  # RAG unchanged, everything else worse
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.DETERIORATING

    def test_stable_rag_falls_through_to_secondary_signals_improving(self):
        prev = _snapshot(completion=40, consumption=90, blocked=5, rag=RAGStatus.RED)
        curr = _snapshot(completion=80, consumption=70, blocked=1, rag=RAGStatus.RED)  # still RED, but clearly better underneath
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.IMPROVING

    def test_small_moves_below_threshold_are_stable_not_noise(self):
        prev = _snapshot(completion=70, consumption=60, blocked=2, rag=RAGStatus.AMBER)
        curr = _snapshot(completion=72, consumption=61, blocked=2, rag=RAGStatus.AMBER)  # tiny wobble
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.STABLE

    def test_mixed_signals_that_cancel_out_are_stable(self):
        prev = _snapshot(completion=50, consumption=50, blocked=2, rag=RAGStatus.AMBER)
        curr = _snapshot(completion=70, consumption=70, blocked=2, rag=RAGStatus.AMBER)  # completion up (good), consumption up (bad)
        assert trend_engine.classify_trend(prev, curr, THRESHOLDS) == TrendDirection.STABLE


# --------------------------------------------------------------------------
# compute_sprint_count_blocked / find_persistent_blockers (Section 12)
# --------------------------------------------------------------------------


class TestSprintCountBlocked:
    def test_none_with_fewer_than_two_snapshots(self):
        """Accuracy Check 6."""
        snaps = [_snapshot(d=date(2026, 1, 1), major_blockers=["PAY-142"])]
        assert trend_engine.compute_sprint_count_blocked("PAY-142", snaps) is None

    def test_counts_consecutive_recent_appearances(self):
        """Section 12's exact worked example: PAY-142 blocked in sprints
        21, 22, 23 -> sprint_count_blocked = 3."""
        snaps = [
            _snapshot(d=date(2026, 1, 1), major_blockers=["PAY-142"]),
            _snapshot(d=date(2026, 1, 8), major_blockers=["PAY-142"]),
            _snapshot(d=date(2026, 1, 15), major_blockers=["PAY-142"]),
        ]
        assert trend_engine.compute_sprint_count_blocked("PAY-142", snaps) == 3

    def test_a_gap_breaks_the_streak(self):
        snaps = [
            _snapshot(d=date(2026, 1, 1), major_blockers=["PAY-142"]),
            _snapshot(d=date(2026, 1, 8), major_blockers=[]),  # resolved that week
            _snapshot(d=date(2026, 1, 15), major_blockers=["PAY-142"]),  # blocked again — a NEW incident
        ]
        assert trend_engine.compute_sprint_count_blocked("PAY-142", snaps) == 1

    def test_zero_when_not_currently_blocked(self):
        snaps = [
            _snapshot(d=date(2026, 1, 1), major_blockers=["PAY-142"]),
            _snapshot(d=date(2026, 1, 8), major_blockers=[]),
        ]
        assert trend_engine.compute_sprint_count_blocked("PAY-142", snaps) == 0

    def test_unrelated_issue_never_blocked_is_zero(self):
        snaps = [
            _snapshot(d=date(2026, 1, 1), major_blockers=["A-1"]),
            _snapshot(d=date(2026, 1, 8), major_blockers=["A-1"]),
        ]
        assert trend_engine.compute_sprint_count_blocked("NEVER-BLOCKED", snaps) == 0


class TestFindPersistentBlockers:
    def test_empty_with_fewer_than_two_snapshots(self):
        snaps = [_snapshot(major_blockers=["A-1"])]
        assert trend_engine.find_persistent_blockers(snaps) == []

    def test_finds_issues_at_or_above_the_threshold(self):
        snaps = [
            _snapshot(d=date(2026, 1, 1), major_blockers=["A-1", "A-2"]),
            _snapshot(d=date(2026, 1, 8), major_blockers=["A-1", "A-2"]),
        ]
        assert set(trend_engine.find_persistent_blockers(snaps, min_sprints=2)) == {"A-1", "A-2"}

    def test_excludes_issues_blocked_only_in_the_latest_snapshot(self):
        snaps = [
            _snapshot(d=date(2026, 1, 1), major_blockers=["A-1"]),
            _snapshot(d=date(2026, 1, 8), major_blockers=["A-1", "A-2"]),  # A-2 is new this week
        ]
        assert trend_engine.find_persistent_blockers(snaps, min_sprints=2) == ["A-1"]

    def test_default_threshold_comes_from_config(self):
        threshold = trend_engine.load_blocked_multiple_sprints_threshold()
        assert threshold == 2  # config/risk_rules.yaml delivery_risk.high.sprint_count_blocked_at_least
