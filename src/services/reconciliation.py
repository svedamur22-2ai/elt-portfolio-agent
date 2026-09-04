"""Data-quality layer for the financial pipeline (Accuracy Checks 2, 3, 4, 8).

Four concerns live here, distinct from `financial_metrics.py`'s health
assessment (is this project financially risky?):

1. Is the data even here at all (`detect_missing_financial_record`)?
2. Is it current — both the financial source (`detect_stale_reporting_period`)
   and the historical memory (`detect_stale_memory`, Phase 11)?
3. Does it agree with itself (`detect_reconciliation_failure`)?

These produce `category="Data Quality"` Risks, kept separate from
`category="Financial"` Risks — a project can be perfectly healthy AND have
stale data (we just can't be sure it's still healthy), which is a different
statement than "this project is financially risky".

`evaluate_financial_record` is the single entry point notebooks/tests should
prefer: it wires a `FinancialDataSource` together with `financial_metrics`
and everything in this module into one `FinancialEvaluation`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from src.connectors.financial_client import FinancialDataSource
from src.models.common import ConfidenceLevel, DisplaySentinel, RiskSeverity
from src.models.finance import FinancialRecord, period_end_date
from src.models.risk import Risk
from src.services import financial_metrics
from src.utils.time import utcnow

DEFAULT_RULES_PATH = "config/risk_rules.yaml"
RECONCILIATION_TOLERANCE = 1.00  # dollars — beneath this, treat as float noise, not a real discrepancy


def load_finance_freshness_threshold_hours(path: str = DEFAULT_RULES_PATH) -> int:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["freshness_thresholds_hours"]["finance"]


def render_for_display(value, reason: str) -> str:
    """reason in {"unmapped", "missing", "not_yet_calculated"} -> the
    matching DisplaySentinel; otherwise returns str(value) unchanged. See
    src/models/common.py's DisplaySentinel docstring for why this stays a
    presentation-layer function rather than a value ever stored on a model."""
    if value is not None:
        return str(value)
    return {
        "unmapped": DisplaySentinel.NOT_MAPPED,
        "missing": DisplaySentinel.NOT_AVAILABLE,
    }.get(reason, DisplaySentinel.UNKNOWN)


def _risk_id(project_id: str, reporting_period: str, suffix: str) -> str:
    return f"DQ-{project_id}-{reporting_period}-{suffix}-{uuid4().hex[:8]}"


def detect_missing_financial_record(
    project_id: str, reporting_period: str, record: Optional[FinancialRecord]
) -> Optional[Risk]:
    """HIGH: with no financial record at all, no financial oversight of this
    project is possible — that's a decision-blocking gap, not a minor
    footnote."""
    if record is not None:
        return None
    return Risk(
        risk_id=_risk_id(project_id, reporting_period, "MISSING"),
        project_id=project_id,
        category="Data Quality",
        severity=RiskSeverity.HIGH,
        description=f"No financial record found for {reporting_period}",
        evidence=[f"project_id={project_id}", f"reporting_period={reporting_period}: NOT AVAILABLE"],
        recommended_action="Confirm this project is mapped in the financial source and that the period has closed.",
        reason_codes=["DATA_COMPLETENESS"],
    )


def detect_stale_reporting_period(
    project_id: str,
    latest_available_period: Optional[str],
    as_of: date,
    max_age_hours: Optional[int] = None,
) -> Optional[Risk]:
    """Compares the SOURCE's latest available period (from
    `FinancialDataSource.get_latest_reporting_period`) against `as_of` — not
    the period of whatever record a caller happened to request. A caller
    deliberately looking at last quarter's numbers isn't "stale"; a source
    that hasn't produced a new period in months is."""
    if latest_available_period is None:
        return None  # detect_missing_financial_record already covers "nothing at all"

    max_age_hours = max_age_hours if max_age_hours is not None else load_finance_freshness_threshold_hours()
    age_days = (as_of - period_end_date(latest_available_period)).days
    if age_days * 24 <= max_age_hours:
        return None

    return Risk(
        risk_id=_risk_id(project_id, latest_available_period, "STALE"),
        project_id=project_id,
        category="Data Quality",
        severity=RiskSeverity.MEDIUM,
        description=f"Latest available financial data ({latest_available_period}) is {age_days} days old",
        evidence=[
            f"latest_available_period={latest_available_period}",
            f"as_of={as_of.isoformat()}",
            f"age_days={age_days}",
            f"threshold_hours={max_age_hours}",
        ],
        recommended_action="Confirm the finance feed is still running for this project; do not treat this period's numbers as current.",
        reason_codes=["DATA_STALE"],
    )


def load_memory_freshness_threshold_hours(path: str = DEFAULT_RULES_PATH) -> int:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["freshness_thresholds_hours"]["memory"]


def detect_stale_memory(
    project_id: str,
    history: list,
    as_of: date,
    max_age_hours: Optional[int] = None,
) -> Optional[Risk]:
    """Section 13's "Historical memory last updated" freshness check —
    Accuracy Check 2 covers Jira/Finance/Memory alike, but until Phase 11
    only the first two were ever actually compared against a threshold;
    `freshness_thresholds_hours.memory` sat in config unread. Flags when a
    project's most recent STORED snapshot is older than that threshold —
    the process hasn't been re-run for this project recently, not that the
    retrieval itself was slow.

    `history=[]` (no snapshots at all) returns None on purpose: that's
    Accuracy Check 6's "first report" / BASELINE case, a different fact
    than "we used to report on this and stopped." `history` is typed as
    `list` rather than `list[ProjectSnapshot]` to avoid importing
    src.models.snapshot purely for a type hint in a module that otherwise
    has no reason to depend on it."""
    if not history:
        return None
    max_age_hours = max_age_hours if max_age_hours is not None else load_memory_freshness_threshold_hours()
    latest = max(history, key=lambda s: s.snapshot_date)
    age_days = (as_of - latest.snapshot_date).days
    if age_days * 24 <= max_age_hours:
        return None
    return Risk(
        risk_id=_risk_id(project_id, latest.snapshot_date.isoformat(), "MEMORY_STALE"),
        project_id=project_id,
        category="Data Quality",
        severity=RiskSeverity.MEDIUM,
        description=f"Most recent stored snapshot for this project ({latest.snapshot_date.isoformat()}) is {age_days} days old",
        evidence=[
            f"latest_snapshot_date={latest.snapshot_date.isoformat()}",
            f"as_of={as_of.isoformat()}",
            f"age_days={age_days}",
            f"threshold_hours={max_age_hours}",
        ],
        recommended_action="Confirm the weekly pipeline is still running for this project; trend/persistence claims may be based on outdated history.",
        reason_codes=["DATA_STALE"],
    )


def detect_reconciliation_failure(
    record: FinancialRecord, tolerance: float = RECONCILIATION_TOLERANCE, project_id: Optional[str] = None
) -> Optional[Risk]:
    """Compares `record.remaining_budget` (this pipeline's authoritative
    figure — approved minus actual minus committed) against
    `record.source_reported_remaining_budget`, when the source provided one.

    On data/sample/financial_mock_data.csv this fires for every row: the
    source's own `remaining_budget` column turns out to equal
    `approved_budget - actual_cost` only — it doesn't subtract
    committed_cost at all, so it systematically overstates remaining funding
    versus Section 5's formula. That's a real, verifiable discrepancy in the
    sample data, not a synthetic example.

    `project_id` defaults to `record.project_id` (the raw finance-system id)
    when omitted — same reasoning, and the same real bug once caught by
    Phase 11's `validate_findings` project-reference check, as
    `financial_metrics.py`'s detectors: the graph always passes the
    project_mapping.yaml key explicitly instead, since that's the one id
    scheme every `Risk` in `state["risks"]` shares regardless of category."""
    if record.remaining_budget is None or record.source_reported_remaining_budget is None:
        return None
    diff = record.remaining_budget - record.source_reported_remaining_budget
    if abs(diff) <= tolerance:
        return None
    project_id = project_id or record.project_id
    return Risk(
        risk_id=_risk_id(project_id, record.reporting_period, "RECONCILE"),
        project_id=project_id,
        category="Data Quality",
        severity=RiskSeverity.MEDIUM,
        description="Source-reported remaining budget does not match this pipeline's computed figure",
        evidence=[
            f"computed_remaining_budget (approved - actual - committed) = {record.remaining_budget:,.2f}",
            f"source_reported_remaining_budget = {record.source_reported_remaining_budget:,.2f}",
            f"difference = {diff:,.2f}",
        ],
        recommended_action="Confirm with Finance which formula is authoritative before citing 'remaining budget' externally — this pipeline uses approved - actual - committed.",
        reason_codes=["DATA_INCONSISTENT"],
    )


def compute_confidence(
    record_found: bool,
    is_stale: bool,
    has_reconciliation_failure: bool,
) -> ConfidenceLevel:
    """Accuracy Check 8. LOW whenever the record is simply missing (nothing
    else matters at that point); LOW when BOTH stale and inconsistent
    compound; MEDIUM for exactly one of those two; HIGH only when the data
    is present, fresh, and self-consistent."""
    if not record_found:
        return ConfidenceLevel.LOW
    degradations = sum([is_stale, has_reconciliation_failure])
    if degradations >= 2:
        return ConfidenceLevel.LOW
    if degradations == 1:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.HIGH


class FinancialEvaluation(BaseModel):
    project_id: str
    reporting_period: str
    record: Optional[FinancialRecord]
    """The CALCULATED record (with_calculated_fields already applied), or
    None if missing — never the raw pre-calculation record."""
    financial_risk: Optional[Risk]
    """None only when `record` is None — with no data there is nothing to
    assess (see `data_quality_risks` for the resulting MISSING finding)."""
    data_quality_risks: list[Risk] = Field(default_factory=list)
    confidence: ConfidenceLevel
    retrieved_timestamp: str


def evaluate_financial_record(
    source: FinancialDataSource,
    project_id: str,
    reporting_period: str,
    as_of: date,
    delivery_progress_pct: Optional[float] = None,
    rules: Optional[dict] = None,
    max_age_hours: Optional[int] = None,
) -> FinancialEvaluation:
    """One-call entry point tying the connector, financial_metrics, and this
    module together — what notebooks/04_financial_source.ipynb and the tests
    both use rather than re-wiring the pieces by hand each time."""
    raw_record = source.get_project_finances(project_id, reporting_period)
    latest_period = source.get_latest_reporting_period(project_id)

    data_quality_risks: list[Risk] = []
    missing = detect_missing_financial_record(project_id, reporting_period, raw_record)
    if missing:
        data_quality_risks.append(missing)

    stale = detect_stale_reporting_period(project_id, latest_period, as_of, max_age_hours)
    if stale:
        data_quality_risks.append(stale)

    calculated_record: Optional[FinancialRecord] = None
    financial_risk: Optional[Risk] = None
    reconciliation_failed = False

    if raw_record is not None:
        calculated_record = raw_record.with_calculated_fields()
        reconciliation_finding = detect_reconciliation_failure(calculated_record, project_id=project_id)
        if reconciliation_finding:
            data_quality_risks.append(reconciliation_finding)
            reconciliation_failed = True
        financial_risk = financial_metrics.classify_financial_risk(calculated_record, delivery_progress_pct, rules)

    confidence = compute_confidence(
        record_found=raw_record is not None,
        is_stale=stale is not None,
        has_reconciliation_failure=reconciliation_failed,
    )

    return FinancialEvaluation(
        project_id=project_id,
        reporting_period=reporting_period,
        record=calculated_record,
        financial_risk=financial_risk,
        data_quality_risks=data_quality_risks,
        confidence=confidence,
        retrieved_timestamp=utcnow().isoformat(),
    )
