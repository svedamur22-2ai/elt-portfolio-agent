from src.connectors.financial_client import (
    CSVFinancialDataSource,
    MockFinancialDataSource,
)
from src.models.common import RiskSeverity
from src.models.finance import FinancialRecord
from src.services import financial_metrics

SAMPLE_CSV = "data/sample/financial_mock_data.csv"


def _record(**overrides) -> FinancialRecord:
    defaults = dict(
        project_id="X",
        reporting_period="2026-06",
        approved_budget=200_000.0,
        actual_spend=100_000.0,
        committed_spend=20_000.0,
        forecast_spend=195_000.0,
    )
    defaults.update(overrides)
    return FinancialRecord(**defaults).with_calculated_fields()


# --------------------------------------------------------------------------
# Deterministic arithmetic (Section 5) — the pipeline's only source of truth
# --------------------------------------------------------------------------


class TestCalculatedFields:
    def test_remaining_budget_formula(self):
        r = _record(approved_budget=200_000, actual_spend=100_000, committed_spend=20_000)
        assert r.remaining_budget == 80_000.0

    def test_consumption_pct_formula(self):
        r = _record(approved_budget=200_000, actual_spend=50_000)
        assert r.budget_consumption_pct == 25.0

    def test_forecast_variance_formula(self):
        r = _record(approved_budget=200_000, forecast_spend=210_000)
        assert r.forecast_variance == -10_000.0

    def test_zero_approved_budget_does_not_divide_by_zero(self):
        r = FinancialRecord(
            project_id="X", reporting_period="2026-06",
            approved_budget=0, actual_spend=0, committed_spend=0, forecast_spend=0,
        ).with_calculated_fields()
        assert r.budget_consumption_pct is None

    def test_negative_amounts_rejected(self):
        import pytest
        with pytest.raises(Exception):
            FinancialRecord(
                project_id="X", reporting_period="2026-06",
                approved_budget=-1, actual_spend=0, committed_spend=0, forecast_spend=0,
            )

    def test_invalid_reporting_period_format_rejected(self):
        import pytest
        with pytest.raises(Exception):
            FinancialRecord(
                project_id="X", reporting_period="March 2026",
                approved_budget=1, actual_spend=0, committed_spend=0, forecast_spend=0,
            )


# --------------------------------------------------------------------------
# Individual detectors
# --------------------------------------------------------------------------


class TestProjectIdOverride:
    """The graph (Phase 8) needs every Risk it collects to share one id
    scheme (config/project_mapping.yaml's key) regardless of category —
    these confirm the override is honored and the default preserves old
    behavior for every pre-Phase-8 call site."""

    def test_defaults_to_record_project_id_when_not_given(self):
        r = _record(project_id="10001", approved_budget=100, actual_spend=95)
        risk = financial_metrics.classify_financial_risk(r)
        assert risk.project_id == "10001"

    def test_explicit_project_id_overrides_the_record_scheme(self):
        r = _record(project_id="10001", approved_budget=100, actual_spend=95)
        risk = financial_metrics.classify_financial_risk(r, project_id="PROJECT-10001")
        assert risk.project_id == "PROJECT-10001"

    def test_override_propagates_to_individual_detectors_too(self):
        r = _record(project_id="10001", approved_budget=100_000, forecast_spend=150_000)
        finding = financial_metrics.detect_forecast_overrun(r, project_id="PROJECT-10001")
        assert finding.project_id == "PROJECT-10001"

    def test_no_findings_branch_also_honors_the_override(self):
        r = _record(project_id="10001", approved_budget=100_000, actual_spend=10_000, forecast_spend=20_000)
        risk = financial_metrics.classify_financial_risk(r, project_id="PROJECT-10001")
        assert risk.severity == RiskSeverity.LOW
        assert risk.project_id == "PROJECT-10001"


class TestDetectForecastOverrun:
    def test_fires_when_forecast_exceeds_approved(self):
        r = _record(approved_budget=150_000, forecast_spend=175_000)
        finding = financial_metrics.detect_forecast_overrun(r)
        assert finding is not None
        assert finding.severity == RiskSeverity.HIGH
        assert "FORECAST_OVERRUN" in finding.reason_codes

    def test_silent_when_forecast_within_approved(self):
        r = _record(approved_budget=150_000, forecast_spend=140_000)
        assert financial_metrics.detect_forecast_overrun(r) is None

    def test_fires_independently_of_current_consumption(self):
        """The MOCK-OVERRUN scenario: consumption is nowhere near any
        threshold, but forecast alone should still trip this detector."""
        r = _record(approved_budget=150_000, actual_spend=80_000, committed_spend=20_000, forecast_spend=175_000)
        assert r.budget_consumption_pct < 75  # nowhere near a consumption threshold
        finding = financial_metrics.detect_forecast_overrun(r)
        assert finding is not None


class TestDetectHighBudgetConsumption:
    def test_high_tier_above_90(self):
        rules = financial_metrics.load_financial_risk_rules()
        r = _record(approved_budget=100_000, actual_spend=95_000)
        finding = financial_metrics.detect_high_budget_consumption(r, rules)
        assert finding.severity == RiskSeverity.HIGH

    def test_medium_tier_between_75_and_90(self):
        rules = financial_metrics.load_financial_risk_rules()
        r = _record(approved_budget=100_000, actual_spend=80_000)
        finding = financial_metrics.detect_high_budget_consumption(r, rules)
        assert finding.severity == RiskSeverity.MEDIUM

    def test_no_finding_below_75(self):
        rules = financial_metrics.load_financial_risk_rules()
        r = _record(approved_budget=100_000, actual_spend=50_000)
        assert financial_metrics.detect_high_budget_consumption(r, rules) is None


class TestDetectNegativeRemainingBudget:
    def test_fires_when_remaining_is_negative(self):
        r = _record(approved_budget=100_000, actual_spend=90_000, committed_spend=20_000)
        assert r.remaining_budget < 0
        finding = financial_metrics.detect_negative_remaining_budget(r)
        assert finding is not None
        assert finding.severity == RiskSeverity.HIGH

    def test_silent_when_remaining_is_positive(self):
        r = _record(approved_budget=100_000, actual_spend=10_000, committed_spend=5_000)
        assert financial_metrics.detect_negative_remaining_budget(r) is None

    def test_silent_at_exactly_zero(self):
        r = _record(approved_budget=100_000, actual_spend=80_000, committed_spend=20_000)
        assert r.remaining_budget == 0
        assert financial_metrics.detect_negative_remaining_budget(r) is None


class TestDetectSpendAheadOfDelivery:
    def test_none_when_no_delivery_data_provided(self):
        rules = financial_metrics.load_financial_risk_rules()
        r = _record(approved_budget=100_000, actual_spend=90_000)
        assert financial_metrics.detect_spend_ahead_of_delivery(r, None, rules) is None

    def test_fires_when_ratio_exceeds_threshold(self):
        rules = financial_metrics.load_financial_risk_rules()
        r = _record(approved_budget=100_000, actual_spend=90_000, committed_spend=0)  # 90% consumed
        finding = financial_metrics.detect_spend_ahead_of_delivery(r, delivery_progress_pct=30.0, rules=rules)  # ratio = 3.0x
        assert finding is not None
        assert finding.severity == RiskSeverity.HIGH
        assert "SPEND_AHEAD_OF_PROGRESS" in finding.reason_codes

    def test_silent_when_spend_and_delivery_are_aligned(self):
        rules = financial_metrics.load_financial_risk_rules()
        r = _record(approved_budget=100_000, actual_spend=50_000, committed_spend=0)  # 50% consumed
        assert financial_metrics.detect_spend_ahead_of_delivery(r, delivery_progress_pct=55.0, rules=rules) is None


# --------------------------------------------------------------------------
# Combined classification — GREEN / AMBER / RED via the mock fixture
# --------------------------------------------------------------------------


class TestClassifyFinancialRisk:
    def test_green_project_is_low_with_no_findings(self):
        mock = MockFinancialDataSource()
        r = mock.get_project_finances("MOCK-GREEN", "2026-06").with_calculated_fields()
        risk = financial_metrics.classify_financial_risk(r)
        assert risk.severity == RiskSeverity.LOW
        assert risk.reason_codes == []

    def test_amber_project_is_medium(self):
        mock = MockFinancialDataSource()
        r = mock.get_project_finances("MOCK-AMBER", "2026-06").with_calculated_fields()
        risk = financial_metrics.classify_financial_risk(r)
        assert risk.severity == RiskSeverity.MEDIUM
        assert "HIGH_BUDGET_CONSUMPTION" in risk.reason_codes

    def test_red_project_is_high_with_multiple_reasons(self):
        mock = MockFinancialDataSource()
        r = mock.get_project_finances("MOCK-RED", "2026-06").with_calculated_fields()
        risk = financial_metrics.classify_financial_risk(r)
        assert risk.severity == RiskSeverity.HIGH
        assert {"FORECAST_OVERRUN", "HIGH_BUDGET_CONSUMPTION", "NEGATIVE_REMAINING_BUDGET"} <= set(risk.reason_codes)

    def test_overrun_project_is_high_via_forecast_alone(self):
        mock = MockFinancialDataSource()
        r = mock.get_project_finances("MOCK-OVERRUN", "2026-06").with_calculated_fields()
        assert r.budget_consumption_pct < 75  # confirms consumption isn't what's triggering this
        risk = financial_metrics.classify_financial_risk(r)
        assert risk.severity == RiskSeverity.HIGH
        assert risk.reason_codes == ["FORECAST_OVERRUN"]

    def test_every_high_severity_risk_carries_evidence(self):
        """Accuracy Check 7: no RED/HIGH risk without evidence."""
        mock = MockFinancialDataSource()
        for project_id in ["MOCK-RED", "MOCK-OVERRUN"]:
            r = mock.get_project_finances(project_id, "2026-06").with_calculated_fields()
            risk = financial_metrics.classify_financial_risk(r)
            assert risk.severity == RiskSeverity.HIGH
            assert len(risk.evidence) > 0

    def test_missing_project_returns_none_never_a_fabricated_record(self):
        mock = MockFinancialDataSource()
        assert mock.get_project_finances("MOCK-MISSING", "2026-06") is None


# --------------------------------------------------------------------------
# Real CSV source: values match hand-computed ground truth
# --------------------------------------------------------------------------


class TestAgainstRealCsvData:
    def test_phoenix_january_matches_known_figures(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        r = source.get_project_finances("10001", "2026-01").with_calculated_fields()
        assert r.approved_budget == 60076.96
        assert r.actual_spend == 66923.39
        assert r.committed_spend == 70355.64
        assert round(r.remaining_budget, 2) == round(60076.96 - 66923.39 - 70355.64, 2)
        assert round(r.budget_consumption_pct, 2) == round(66923.39 / 60076.96 * 100, 2)

    def test_currency_defaults_to_usd(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        r = source.get_project_finances("10001", "2026-01")
        assert r.currency == "USD"

    def test_get_portfolio_finances_returns_one_record_per_project_for_period(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        records = source.get_portfolio_finances("2026-01")
        assert {r.project_id for r in records} == {"10001", "10002", "10003", "10004", "10005"}

    def test_missing_project_period_combo_returns_none(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        assert source.get_project_finances("10001", "2099-01") is None
        assert source.get_project_finances("10006", "2026-01") is None  # QSR has no finance mapping at all

    def test_get_latest_reporting_period(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        assert source.get_latest_reporting_period("10001") == "2026-08"
        assert source.get_latest_reporting_period("NOPE") is None
