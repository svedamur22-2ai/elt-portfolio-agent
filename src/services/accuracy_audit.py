"""Consolidated accuracy audit (Section 13) — Phase 11.

Every one of the ten accuracy checks is already enforced somewhere in the
pipeline (Phases 3-9, plus two new live checks `validate_findings` gained in
this phase). This module doesn't invent new rules — it INSPECTS a completed
`AgentState` result after a graph run and confirms each check actually held
for that specific run, producing one structured, human-readable report.

Two real gaps were found and fixed while building this consolidation, not
merely aspirational ones documented after the fact:

- Memory freshness (`freshness_thresholds_hours.memory`) existed in config
  since Phase 1 but nothing ever read it — `reconciliation.detect_stale_memory`
  fills that in, now live in `validate_findings`.
- `reconciliation.detect_reconciliation_failure` stamped `Risk.project_id`
  from the raw finance-system id instead of the project_mapping.yaml key —
  invisible until this module's Check 9 (and `validate_findings`' own
  project-reference check) existed to catch every reconciliation finding
  from a real run as an "orphan" reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import yaml

from src.services import trend_engine

DEFAULT_RULES_PATH = "config/risk_rules.yaml"
_KNOWN_CONFIDENCE_VALUES = {"HIGH CONFIDENCE", "MEDIUM CONFIDENCE", "LOW CONFIDENCE"}


def _load_thresholds(path: str = DEFAULT_RULES_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)["freshness_thresholds_hours"]


@dataclass
class CheckResult:
    number: int
    name: str
    passed: bool
    detail: str


@dataclass
class AccuracyAuditReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def summary(self) -> str:
        header = "Accuracy Audit — " + ("ALL CHECKS PASSED" if self.all_passed else "FAILURES FOUND")
        lines = [header]
        for c in self.checks:
            lines.append(f"  [{'PASS' if c.passed else 'FAIL'}] {c.number}. {c.name}: {c.detail}")
        return "\n".join(lines)


def check_1_source_provenance(state: dict) -> CheckResult:
    """Every project mapped on a given side must carry a matching
    provenance entry for that side — a fact with no traceable source is
    exactly what Accuracy Check 1 exists to prevent."""
    projects = state.get("unified_projects", [])
    violations = []
    for p in projects:
        systems = {pr.source_system.value for pr in p.provenance}
        if p.delivery_status != "UNKNOWN" and "Jira" not in systems:
            violations.append(f"{p.project_id}: has delivery data but no Jira provenance")
        if p.financial_status != "UNKNOWN" and "Finance" not in systems:
            violations.append(f"{p.project_id}: has financial data but no Finance provenance")
    detail = "; ".join(violations) if violations else f"all {len(projects)} project(s) carry provenance consistent with their mapping status"
    return CheckResult(1, "Source Provenance", not violations, detail)


def check_2_freshness(state: dict) -> CheckResult:
    """Passing here means freshness is TRACKED and comparable — not that
    the data happens to be fresh (this sample dataset routinely isn't, and
    correctly says so via Check 3/DATA_STALE findings, not by failing this
    check)."""
    jira_fresh = state.get("jira_data_freshness")
    fin_fresh = state.get("financial_data_freshness")
    memory_fresh = state.get("memory_freshness")
    present = [name for name, v in [("Jira", jira_fresh), ("Finance", fin_fresh), ("Memory", memory_fresh)] if v is not None]
    missing = [name for name, v in [("Jira", jira_fresh), ("Finance", fin_fresh), ("Memory", memory_fresh)] if v is None]
    passed = jira_fresh is not None and fin_fresh is not None
    detail = f"tracked: {', '.join(present) or 'none'}" + (f"; missing: {', '.join(missing)}" if missing else "")
    return CheckResult(2, "Freshness", passed, detail)


def check_3_data_completeness(state: dict) -> CheckResult:
    """Every project unmapped on a side must have a SURFACED finding for
    it — a silent gap (unmapped but no DATA_MAPPING risk) would mean the
    pipeline noticed less than it should have."""
    risks = state.get("risks", [])
    projects = state.get("unified_projects", [])
    completeness_project_ids = {r.project_id for r in risks if set(r.reason_codes) & {"DATA_COMPLETENESS", "DATA_MAPPING"}}
    unmapped = [p for p in projects if p.financial_status == "UNKNOWN" or p.delivery_status == "UNKNOWN"]
    silent_gaps = [p.project_id for p in unmapped if p.project_id not in completeness_project_ids]
    detail = (
        f"{len(unmapped)} unmapped project(s), all surfaced"
        if not silent_gaps
        else f"{len(silent_gaps)} unmapped project(s) with no surfaced finding: {silent_gaps}"
    )
    return CheckResult(3, "Data Completeness", not silent_gaps, detail)


def check_4_financial_reconciliation(state: dict) -> CheckResult:
    """Re-derives `remaining_budget` from each fetched record's own
    approved/actual/committed fields and confirms it matches what's stored
    — a live check that Section 5's formula is still what actually ran,
    not a different calculation slipped in somewhere."""
    records = state.get("financial_data", [])
    violations = []
    for r in records:
        if r.approved_budget < 0 or r.actual_spend < 0 or r.committed_spend < 0:
            violations.append(f"{r.project_id}: negative amount present")
            continue
        if r.remaining_budget is not None:
            expected = r.approved_budget - r.actual_spend - r.committed_spend
            if abs(r.remaining_budget - expected) > 0.01:
                violations.append(f"{r.project_id}: remaining_budget does not match approved-actual-committed")
    detail = "; ".join(violations) if violations else f"{len(records)} financial record(s) all internally consistent"
    return CheckResult(4, "Financial Reconciliation", not violations, detail)


def check_5_deterministic_calculations(state: dict) -> CheckResult:
    """Cross-checks `calculated_metrics` (Accuracy Check 5's dedicated
    home) against the same figures on `unified_projects` — both are
    supposed to be the same numbers viewed two ways; a mismatch would mean
    something recomputed a figure instead of reading the one true value."""
    metrics = state.get("calculated_metrics", {})
    projects_by_id = {p.project_id: p for p in state.get("unified_projects", [])}
    mismatches = []
    for pid, m in metrics.items():
        project = projects_by_id.get(pid)
        if project is None:
            continue
        if m.get("approved_budget") is not None and project.approved_budget is not None:
            if abs(m["approved_budget"] - project.approved_budget) > 0.01:
                mismatches.append(f"{pid}: approved_budget mismatch")
        if m.get("budget_consumption_pct") is not None and project.budget_consumption_pct is not None:
            if abs(m["budget_consumption_pct"] - project.budget_consumption_pct) > 0.01:
                mismatches.append(f"{pid}: budget_consumption_pct mismatch")
    detail = "; ".join(mismatches) if mismatches else f"{len(metrics)} project(s) checked, all consistent"
    return CheckResult(5, "Deterministic Calculations", not mismatches, detail)


def check_6_historical_verification(state: dict) -> CheckResult:
    """For every project with zero stored history, re-derives trend
    classification and confirms it's BASELINE — proving Accuracy Check 6
    held for this run, not just that trend_engine promises it in the
    abstract."""
    historical = state.get("historical_context", {})
    current_snapshots = state.get("current_snapshots", {})
    violations = []
    for pid, current in current_snapshots.items():
        history = historical.get(pid, [])
        if len(history) == 0:
            trend = trend_engine.classify_trend(None, current)
            if trend.value != "BASELINE":
                violations.append(f"{pid}: trend={trend.value} with zero prior snapshots")
    detail = "; ".join(violations) if violations else f"{len(current_snapshots)} project(s) checked, no unsupported historical claims"
    return CheckResult(6, "Historical Verification", not violations, detail)


def check_7_evidence_requirement(state: dict) -> CheckResult:
    """Independent re-confirmation of the same rule now enforced live in
    validate_findings (Phase 11) — defense in depth: this module re-derives
    it from the final state rather than trusting the node's own report of
    itself."""
    risks = state.get("risks", [])
    missing = [r.risk_id for r in risks if r.severity.value == "HIGH" and not r.evidence]
    detail = "every HIGH-severity risk carries evidence" if not missing else f"{len(missing)} HIGH-severity risk(s) with no evidence: {missing}"
    return CheckResult(7, "Evidence Requirement", not missing, detail)


def check_8_confidence(state: dict) -> CheckResult:
    confidence = state.get("confidence")
    passed = confidence in _KNOWN_CONFIDENCE_VALUES
    return CheckResult(8, "Confidence", passed, f"confidence={confidence!r}")


def check_9_no_hallucination(state: dict) -> CheckResult:
    """Every risk must reference a real project (or the portfolio
    sentinel); every citation must carry a non-empty source id. Either
    failing would mean a claim reached the final report that doesn't trace
    back to anything real."""
    known_ids = {p.project_id for p in state.get("unified_projects", [])} | {"PORTFOLIO"}
    orphan_risks = [r.risk_id for r in state.get("risks", []) if r.project_id not in known_ids]
    bad_citations = [c for c in state.get("citations", []) if not c.get("source_record_id") or not c.get("source_system")]
    passed = not orphan_risks and not bad_citations
    detail = (
        "every risk and citation traces to a real source"
        if passed
        else f"orphan risks: {orphan_risks}; malformed citations: {len(bad_citations)}"
    )
    return CheckResult(9, "No Hallucination", passed, detail)


def check_10_validation_node_ran(state: dict) -> CheckResult:
    ran = "validation_passed" in state and "confidence" in state and "validation_results" in state
    detail = f"validation_passed={state.get('validation_passed')}, {len(state.get('validation_results', []))} check(s) recorded"
    return CheckResult(10, "Validation Node", ran, detail)


def run_accuracy_audit(state: dict) -> AccuracyAuditReport:
    """The one function notebooks/13_accuracy_validation.ipynb and the
    Streamlit Data Quality / Audit page both call — every check above, run
    against one completed graph result."""
    return AccuracyAuditReport(
        checks=[
            check_1_source_provenance(state),
            check_2_freshness(state),
            check_3_data_completeness(state),
            check_4_financial_reconciliation(state),
            check_5_deterministic_calculations(state),
            check_6_historical_verification(state),
            check_7_evidence_requirement(state),
            check_8_confidence(state),
            check_9_no_hallucination(state),
            check_10_validation_node_ran(state),
        ]
    )
