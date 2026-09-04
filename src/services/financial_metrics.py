"""Deterministic financial calculations and risk detection (Accuracy Check 5
/ Sections 9, 13). Pure Python throughout — no LLM call is permitted in this
module. `src/models/finance.py` owns the core formulas
(`with_calculated_fields`, `spend_to_progress_ratio`); this module owns
turning those numbers into individual, evidenced findings and an overall
financial risk classification.

Every detector below returns `Optional[Risk]` — `None` means the condition
did not fire, never "risk is fine" by omission of a check that couldn't run
(e.g. `detect_spend_ahead_of_delivery` with no delivery data returns `None`
and callers must not read that as "spend is fine").

Every detector also takes an optional `project_id` override, defaulting to
`record.project_id` (the raw finance-system id, e.g. "10001") when omitted.
The graph (Phase 8) always passes it explicitly as the project_mapping.yaml
key (e.g. "PROJECT-10001") instead — that's the one id scheme every `Risk`
in `state["risks"]` shares regardless of category, which is what makes
"every risk for this project" a simple filter rather than a three-way
id-scheme lookup. Defaulting to `record.project_id` keeps every pre-Phase-8
call site (tests, notebooks 04/07/08/09) working unchanged.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

import yaml

from src.models.common import RiskSeverity
from src.models.finance import FinancialRecord, spend_to_progress_ratio
from src.models.risk import Risk

DEFAULT_RULES_PATH = "config/risk_rules.yaml"


def load_financial_risk_rules(path: str = DEFAULT_RULES_PATH) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["financial_risk"]


def _risk_id(project_id: str, reporting_period: str, suffix: str) -> str:
    return f"FIN-{project_id}-{reporting_period}-{suffix}-{uuid4().hex[:8]}"


def detect_forecast_overrun(record: FinancialRecord, project_id: Optional[str] = None) -> Optional[Risk]:
    """Fires when forecast_spend > approved_budget (forecast_variance < 0) —
    the *high* tier's `forecast_exceeds_approved` rule. This can fire even
    when current consumption looks fine (see MOCK-OVERRUN): a forecast is a
    forward-looking signal, not a restatement of consumption-to-date."""
    if record.forecast_variance is None or record.forecast_variance >= 0:
        return None
    project_id = project_id or record.project_id
    return Risk(
        risk_id=_risk_id(project_id, record.reporting_period, "FORECAST"),
        project_id=project_id,
        category="Financial",
        severity=RiskSeverity.HIGH,
        description="Forecast spend exceeds approved budget",
        evidence=[
            f"approved_budget={record.approved_budget:,.2f}",
            f"forecast_spend={record.forecast_spend:,.2f}",
            f"forecast_variance={record.forecast_variance:,.2f}",
        ],
        recommended_action="Review forecast assumptions and secure supplemental funding or descope before spend catches up to it.",
        reason_codes=["FORECAST_OVERRUN"],
    )


def detect_high_budget_consumption(record: FinancialRecord, rules: dict, project_id: Optional[str] = None) -> Optional[Risk]:
    """HIGH above `high.budget_consumption_pct_above`, MEDIUM above
    `medium.budget_consumption_pct_above`, else None. Independent of
    forecast/remaining-budget checks — a project can be at 80% consumption
    with a perfectly fine forecast and still warrant a MEDIUM flag purely on
    consumption velocity."""
    pct = record.budget_consumption_pct
    if pct is None:
        return None

    high_threshold = rules["high"]["budget_consumption_pct_above"]
    medium_threshold = rules["medium"]["budget_consumption_pct_above"]

    if pct > high_threshold:
        severity = RiskSeverity.HIGH
        threshold = high_threshold
    elif pct > medium_threshold:
        severity = RiskSeverity.MEDIUM
        threshold = medium_threshold
    else:
        return None

    project_id = project_id or record.project_id
    return Risk(
        risk_id=_risk_id(project_id, record.reporting_period, "CONSUMPTION"),
        project_id=project_id,
        category="Financial",
        severity=severity,
        description=f"Budget consumption ({pct:.1f}%) exceeds the {threshold}% threshold",
        evidence=[
            f"budget_consumption_pct={pct:.1f}%",
            f"actual_spend={record.actual_spend:,.2f}",
            f"approved_budget={record.approved_budget:,.2f}",
        ],
        recommended_action="Confirm remaining scope is funded by remaining budget before the next reporting period.",
        reason_codes=["HIGH_BUDGET_CONSUMPTION"],
    )


def detect_negative_remaining_budget(record: FinancialRecord, project_id: Optional[str] = None) -> Optional[Risk]:
    """Unconditionally HIGH — a negative remaining_budget means approved
    funding has already been exceeded, which is true regardless of any
    configured percentage threshold."""
    if record.remaining_budget is None or record.remaining_budget >= 0:
        return None
    project_id = project_id or record.project_id
    return Risk(
        risk_id=_risk_id(project_id, record.reporting_period, "NEGATIVE"),
        project_id=project_id,
        category="Financial",
        severity=RiskSeverity.HIGH,
        description="Remaining budget is negative",
        evidence=[
            f"remaining_budget={record.remaining_budget:,.2f}",
            f"approved_budget={record.approved_budget:,.2f}",
            f"actual_spend={record.actual_spend:,.2f}",
            f"committed_spend={record.committed_spend:,.2f}",
        ],
        recommended_action="Escalate immediately — approved funding is already exhausted, not just at risk.",
        reason_codes=["NEGATIVE_REMAINING_BUDGET"],
    )


def detect_spend_ahead_of_delivery(
    record: FinancialRecord, delivery_progress_pct: Optional[float], rules: dict, project_id: Optional[str] = None
) -> Optional[Risk]:
    """Requires `delivery_progress_pct` from the Jira/sprint layer — this
    module does not fetch it itself (Sections 8/9's delivery metrics are a
    separate concern). Returns None both when the ratio is within threshold
    AND when delivery_progress_pct wasn't supplied; callers must not treat
    those as equivalent (log/display "not evaluated" separately from "no
    risk" — see notebooks/04_financial_source.ipynb)."""
    if delivery_progress_pct is None:
        return None
    ratio = spend_to_progress_ratio(record.budget_consumption_pct, delivery_progress_pct)
    if ratio is None:
        return None
    threshold = rules["high"]["spend_to_progress_ratio_above"]
    if ratio <= threshold:
        return None
    project_id = project_id or record.project_id
    return Risk(
        risk_id=_risk_id(project_id, record.reporting_period, "SPEND_AHEAD"),
        project_id=project_id,
        category="Financial",
        severity=RiskSeverity.HIGH,
        description=f"Spend-to-progress ratio ({ratio:.2f}x) exceeds the {threshold}x threshold",
        evidence=[
            f"budget_consumption_pct={record.budget_consumption_pct:.1f}%",
            f"delivery_progress_pct={delivery_progress_pct:.1f}%",
            f"spend_to_progress_ratio={ratio:.2f}x",
        ],
        recommended_action="Investigate why spend is outpacing delivery — scope creep, underestimation, or a stalled delivery team.",
        reason_codes=["SPEND_AHEAD_OF_PROGRESS"],
    )


_SEVERITY_ORDER = {RiskSeverity.LOW: 0, RiskSeverity.MEDIUM: 1, RiskSeverity.HIGH: 2}


def classify_financial_risk(
    record: FinancialRecord,
    delivery_progress_pct: Optional[float] = None,
    rules: Optional[dict] = None,
    project_id: Optional[str] = None,
) -> Risk:
    """Combines every detector above into one overall Financial Risk:
    severity = the highest severity among whichever detectors fired, LOW
    (with no evidence) when none did. This is the Risk that would feed
    Section 10's cross-domain combined_risk_matrix once a delivery Risk
    exists alongside it (Phase 6) — deliberately not built here, since this
    task is scoped to the financial layer only."""
    rules = rules or load_financial_risk_rules()
    project_id = project_id or record.project_id

    findings = [
        f
        for f in (
            detect_forecast_overrun(record, project_id),
            detect_high_budget_consumption(record, rules, project_id),
            detect_negative_remaining_budget(record, project_id),
            detect_spend_ahead_of_delivery(record, delivery_progress_pct, rules, project_id),
        )
        if f is not None
    ]

    if not findings:
        return Risk(
            risk_id=_risk_id(project_id, record.reporting_period, "OVERALL"),
            project_id=project_id,
            category="Financial",
            severity=RiskSeverity.LOW,
            description="No financial risk thresholds triggered",
            evidence=[
                f"budget_consumption_pct={record.budget_consumption_pct:.1f}%"
                if record.budget_consumption_pct is not None
                else "budget_consumption_pct=N/A",
                f"remaining_budget={record.remaining_budget:,.2f}" if record.remaining_budget is not None else "remaining_budget=N/A",
                f"forecast_variance={record.forecast_variance:,.2f}" if record.forecast_variance is not None else "forecast_variance=N/A",
            ],
            reason_codes=[],
        )

    overall_severity = max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])
    combined_evidence = [line for f in findings for line in f.evidence]
    combined_reasons = [code for f in findings for code in f.reason_codes]
    combined_actions = "; ".join(sorted({f.recommended_action for f in findings if f.recommended_action}))

    return Risk(
        risk_id=_risk_id(project_id, record.reporting_period, "OVERALL"),
        project_id=project_id,
        category="Financial",
        severity=overall_severity,
        description=f"{len(findings)} financial risk condition(s) triggered: " + ", ".join(sorted(set(combined_reasons))),
        evidence=combined_evidence,
        recommended_action=combined_actions or None,
        reason_codes=sorted(set(combined_reasons)),
    )
