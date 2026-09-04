from datetime import date

from src.connectors.financial_client import (
    CSVFinancialDataSource,
    MockFinancialDataSource,
    _raw_to_record,
    FinancialFetchReport,
    load_financial_field_mapping,
)
from src.models.common import ConfidenceLevel
from src.models.finance import FinancialRecord, period_end_date
from src.services import reconciliation
from src.utils.time import utcnow

SAMPLE_CSV = "data/sample/financial_mock_data.csv"


def _calculated(**overrides) -> FinancialRecord:
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
# period_end_date helper
# --------------------------------------------------------------------------


class TestPeriodEndDate:
    def test_31_day_month(self):
        assert period_end_date("2026-01") == date(2026, 1, 31)

    def test_30_day_month(self):
        assert period_end_date("2026-04") == date(2026, 4, 30)

    def test_leap_year_february(self):
        assert period_end_date("2028-02") == date(2028, 2, 29)

    def test_non_leap_year_february(self):
        assert period_end_date("2026-02") == date(2026, 2, 28)


# --------------------------------------------------------------------------
# Missing financial record
# --------------------------------------------------------------------------


class TestDetectMissingFinancialRecord:
    def test_fires_when_record_is_none(self):
        finding = reconciliation.detect_missing_financial_record("X", "2026-06", None)
        assert finding is not None
        assert finding.category == "Data Quality"
        assert "DATA_COMPLETENESS" in finding.reason_codes

    def test_silent_when_record_exists(self):
        r = _calculated()
        assert reconciliation.detect_missing_financial_record("X", "2026-06", r) is None


# --------------------------------------------------------------------------
# Stale reporting period
# --------------------------------------------------------------------------


class TestDetectStaleReportingPeriod:
    def test_silent_when_within_threshold(self):
        # August close (ends 2026-08-31), checked 2 weeks later — well within 45 days
        finding = reconciliation.detect_stale_reporting_period("X", "2026-08", as_of=date(2026, 9, 14))
        assert finding is None

    def test_fires_when_beyond_threshold(self):
        finding = reconciliation.detect_stale_reporting_period("X", "2026-08", as_of=date(2027, 3, 1))
        assert finding is not None
        assert finding.category == "Data Quality"
        assert "DATA_STALE" in finding.reason_codes

    def test_none_when_no_period_available_at_all(self):
        """Absence-of-any-data is detect_missing_financial_record's job, not
        this one's — avoids double-reporting the same underlying gap."""
        assert reconciliation.detect_stale_reporting_period("X", None, as_of=date(2026, 1, 1)) is None

    def test_boundary_exactly_at_threshold_does_not_fire(self):
        # threshold is 1080 hours = 45 days
        finding = reconciliation.detect_stale_reporting_period(
            "X", "2026-01", as_of=date(2026, 1, 31) , max_age_hours=1080
        )
        assert finding is None

    def test_custom_threshold_is_honored(self):
        finding = reconciliation.detect_stale_reporting_period(
            "X", "2026-01", as_of=date(2026, 2, 5), max_age_hours=24
        )
        assert finding is not None


# --------------------------------------------------------------------------
# Reconciliation failure — proven against real data, not synthetic
# --------------------------------------------------------------------------


class TestDetectReconciliationFailure:
    def test_no_finding_when_source_omits_its_own_figure(self):
        r = _calculated()  # source_reported_remaining_budget defaults to None
        assert reconciliation.detect_reconciliation_failure(r) is None

    def test_no_finding_when_figures_agree_within_tolerance(self):
        r = FinancialRecord(
            project_id="X", reporting_period="2026-06",
            approved_budget=100_000, actual_spend=50_000, committed_spend=10_000, forecast_spend=90_000,
            source_reported_remaining_budget=40_000.50,  # computed = 40_000.00, within $1 tolerance
        ).with_calculated_fields()
        assert reconciliation.detect_reconciliation_failure(r) is None

    def test_fires_on_real_csv_data_because_source_ignores_committed_spend(self):
        """Ground-truth regression: data/sample/financial_mock_data.csv's own
        `remaining_budget` column equals approved_budget - actual_cost only —
        it never subtracts committed_cost. This pipeline's formula does
        subtract it (Section 5), so every row in this file should trip this
        detector. This is a real property of the sample data, not a
        contrived fixture."""
        source = CSVFinancialDataSource(SAMPLE_CSV)
        r = source.get_project_finances("10001", "2026-01").with_calculated_fields()

        assert r.source_reported_remaining_budget == -6846.43  # approved - actual only
        assert round(r.remaining_budget, 2) != round(r.source_reported_remaining_budget, 2)

        finding = reconciliation.detect_reconciliation_failure(r)
        assert finding is not None
        assert finding.category == "Data Quality"
        assert "DATA_INCONSISTENT" in finding.reason_codes
        # the discrepancy should be explainable exactly by committed_spend
        diff = r.remaining_budget - r.source_reported_remaining_budget
        assert round(diff, 2) == round(-r.committed_spend, 2)

    def test_fires_consistently_across_the_whole_real_dataset(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        for project_id in ["10001", "10002", "10003", "10004", "10005"]:
            for period in ["2026-01", "2026-08"]:
                r = source.get_project_finances(project_id, period).with_calculated_fields()
                assert reconciliation.detect_reconciliation_failure(r) is not None


class TestReconciliationFailureProjectIdOverride:
    """Regression: this detector used to stamp Risk.project_id from
    record.project_id (the raw finance-system id, e.g. "10001") unconditionally
    — caught live in Phase 11 when validate_findings's new project-reference
    check flagged every reconciliation finding from a real graph run as an
    orphan, since the graph always resolves projects by the mapping key
    (e.g. "PROJECT-10001"). Mirrors the same fix already applied to
    financial_metrics.py's detectors in Phase 8."""

    def test_defaults_to_record_project_id_when_not_given(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        r = source.get_project_finances("10001", "2026-01").with_calculated_fields()
        finding = reconciliation.detect_reconciliation_failure(r)
        assert finding.project_id == "10001"

    def test_explicit_project_id_overrides_the_record_scheme(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        r = source.get_project_finances("10001", "2026-01").with_calculated_fields()
        finding = reconciliation.detect_reconciliation_failure(r, project_id="PROJECT-10001")
        assert finding.project_id == "PROJECT-10001"


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------


class TestComputeConfidence:
    def test_low_when_record_missing_regardless_of_other_flags(self):
        assert reconciliation.compute_confidence(record_found=False, is_stale=False, has_reconciliation_failure=False) == ConfidenceLevel.LOW

    def test_high_when_present_fresh_and_consistent(self):
        assert reconciliation.compute_confidence(record_found=True, is_stale=False, has_reconciliation_failure=False) == ConfidenceLevel.HIGH

    def test_medium_with_exactly_one_degradation(self):
        assert reconciliation.compute_confidence(record_found=True, is_stale=True, has_reconciliation_failure=False) == ConfidenceLevel.MEDIUM
        assert reconciliation.compute_confidence(record_found=True, is_stale=False, has_reconciliation_failure=True) == ConfidenceLevel.MEDIUM

    def test_low_with_both_degradations(self):
        assert reconciliation.compute_confidence(record_found=True, is_stale=True, has_reconciliation_failure=True) == ConfidenceLevel.LOW


# --------------------------------------------------------------------------
# evaluate_financial_record — full pipeline, matches earlier manual checks
# --------------------------------------------------------------------------


class TestEvaluateFinancialRecord:
    def test_fresh_real_record_is_medium_confidence_due_to_reconciliation_only(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        ev = reconciliation.evaluate_financial_record(source, "10001", "2026-08", as_of=date(2026, 9, 15))
        assert ev.confidence == ConfidenceLevel.MEDIUM
        assert len(ev.data_quality_risks) == 1
        assert "DATA_INCONSISTENT" in ev.data_quality_risks[0].reason_codes
        assert ev.financial_risk is not None

    def test_stale_and_inconsistent_real_record_is_low_confidence(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        ev = reconciliation.evaluate_financial_record(source, "10001", "2026-08", as_of=date(2027, 3, 1))
        assert ev.confidence == ConfidenceLevel.LOW
        reason_codes = {code for r in ev.data_quality_risks for code in r.reason_codes}
        assert {"DATA_STALE", "DATA_INCONSISTENT"} <= reason_codes

    def test_missing_project_short_circuits_to_no_financial_risk(self):
        source = CSVFinancialDataSource(SAMPLE_CSV)
        ev = reconciliation.evaluate_financial_record(source, "10006", "2026-08", as_of=date(2026, 9, 15))
        assert ev.confidence == ConfidenceLevel.LOW
        assert ev.record is None
        assert ev.financial_risk is None
        assert any("DATA_COMPLETENESS" in r.reason_codes for r in ev.data_quality_risks)

    def test_delivery_progress_pct_flows_through_to_spend_ahead_check(self):
        mock = MockFinancialDataSource()
        ev = reconciliation.evaluate_financial_record(
            mock, "MOCK-RED", "2026-06", as_of=date(2026, 6, 20), delivery_progress_pct=20.0
        )
        assert "SPEND_AHEAD_OF_PROGRESS" in ev.financial_risk.reason_codes

    def test_mock_green_project_end_to_end_is_high_confidence_low_risk(self):
        mock = MockFinancialDataSource()
        ev = reconciliation.evaluate_financial_record(mock, "MOCK-GREEN", "2026-06", as_of=date(2026, 6, 20))
        assert ev.confidence == ConfidenceLevel.HIGH
        assert ev.financial_risk.severity.value == "LOW"
        assert ev.data_quality_risks == []


# --------------------------------------------------------------------------
# Malformed / incomplete rows never crash the connector or invent zeros
# --------------------------------------------------------------------------


class TestMalformedRowHandling:
    def test_row_missing_a_required_numeric_field_is_skipped_not_zero_filled(self):
        field_map = load_financial_field_mapping()
        raw = {
            "project_id": "X",
            "financial_period": "2026-06",
            "approved_budget": "100000",
            "actual_cost": "",  # missing
            "committed_cost": "5000",
            "forecast_cost": "90000",
            "currency": "USD",
        }
        report = FinancialFetchReport()
        record = _raw_to_record(raw, field_map, report, utcnow())
        assert record is None
        assert len(report.rows_skipped) == 1

    def test_row_with_unparseable_number_is_skipped(self):
        field_map = load_financial_field_mapping()
        raw = {
            "project_id": "X",
            "financial_period": "2026-06",
            "approved_budget": "not-a-number",
            "actual_cost": "1000",
            "committed_cost": "500",
            "forecast_cost": "900",
            "currency": "USD",
        }
        report = FinancialFetchReport()
        record = _raw_to_record(raw, field_map, report, utcnow())
        assert record is None
        assert "unparseable" in report.rows_skipped[0]

    def test_optional_source_remaining_budget_missing_does_not_invalidate_row(self):
        field_map = load_financial_field_mapping()
        raw = {
            "project_id": "X",
            "financial_period": "2026-06",
            "approved_budget": "100000",
            "actual_cost": "50000",
            "committed_cost": "5000",
            "forecast_cost": "90000",
            "currency": "USD",
            # no "remaining_budget" key at all
        }
        report = FinancialFetchReport()
        record = _raw_to_record(raw, field_map, report, utcnow())
        assert record is not None
        assert record.source_reported_remaining_budget is None
        assert report.rows_skipped == []
