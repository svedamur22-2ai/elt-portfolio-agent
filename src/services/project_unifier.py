"""Cross-source project unification (Section 6 / Phase 5: "Unified project
model"). Joins a Jira-only view (src/connectors/jira_client.py +
src/services/sprint_metrics.py) and a Finance-only view
(src/connectors/financial_client.py) into one `Project` per
config/project_mapping.yaml entry.

Scope boundary: this module assembles FACTS (budget figures, delivery
progress, ownership) — it deliberately does not compute `risk_status`. That
stays `RAGStatus.UNKNOWN` here; Phase 6's risk engine is what turns these
facts into a judgment, using financial_metrics.classify_financial_risk and a
symmetric delivery-risk classifier. Keeping that boundary means this module
never has to be re-litigated when risk thresholds change.

Never guess a mapping (Section 6): every Project this produces comes from an
explicit config/project_mapping.yaml entry. A Jira project or finance
project with no entry here simply never becomes a `Project` — it would show
up as an orphan in a full audit, not as a silently-invented unification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional
from uuid import uuid4

import yaml
from pydantic import BaseModel

from src.connectors.financial_client import FinancialDataSource
from src.connectors.jira_client import JiraClient
from src.models.common import RiskSeverity, SourceProvenance, SourceSystem
from src.models.project import Project
from src.models.risk import Risk
from src.services import sprint_metrics

DEFAULT_MAPPING_PATH = "config/project_mapping.yaml"


class ProjectMappingEntry(BaseModel):
    project_id: str
    """The mapping key itself (e.g. "PROJECT-10001") — the canonical,
    source-independent identifier. Stable even when one side is unmapped."""
    jira_key: Optional[str] = None
    jira_project_id: Optional[str] = None
    finance_project_id: Optional[str] = None
    project_name: str
    project_manager: str
    business_owner: str
    technical_owner: str
    project_status: str


@dataclass
class ProjectMappingConfig:
    organization_id: str
    portfolio_id: str
    entries: dict[str, ProjectMappingEntry]  # keyed by project_id


def load_project_mapping(path: str = DEFAULT_MAPPING_PATH) -> ProjectMappingConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    entries = {
        project_id: ProjectMappingEntry(project_id=project_id, **fields)
        for project_id, fields in data["projects"].items()
    }
    return ProjectMappingConfig(
        organization_id=data["organization_id"],
        portfolio_id=data["portfolio_id"],
        entries=entries,
    )


def _display_key(entry: ProjectMappingEntry) -> str:
    """A short, human-facing code for display — distinct from
    `jira_project_key` on the Project model, which stays None when unmapped
    (see that field's own docstring). Falls back to a finance-derived code
    so a finance-only project (Helios) still gets something readable rather
    than an empty string."""
    if entry.jira_key:
        return entry.jira_key
    if entry.finance_project_id:
        return f"FIN-{entry.finance_project_id}"
    return entry.project_id


def build_unified_project(
    entry: ProjectMappingEntry,
    jira_client: Optional[JiraClient],
    financial_source: Optional[FinancialDataSource],
    as_of: date,
) -> Project:
    """One project. `jira_client`/`financial_source` may be None (e.g. a
    caller only wants to unify the financial side for a quick check) — in
    that case that side is treated exactly like "no mapping", which is the
    correct, conservative behavior: no client means no verified data, which
    is indistinguishable from "not mapped" as far as what we're willing to
    assert.

    Phase 12 hardening: an exception from either client (a live source
    down, timing out, whatever) is caught here and treated the SAME
    conservative way — no fabricated data, `*_status = "UNKNOWN"`. This
    module doesn't try to distinguish "not mapped" from "mapped but the
    source just failed" in `Project` itself; that distinction is already
    captured elsewhere, as a dedicated HIGH-severity `DATA_FETCH_INCOMPLETE`
    finding from `validate_delivery_data`/`validate_financial_data`, which
    run before this node in the graph. Duplicating that distinction here
    would mean two places could disagree about it.
    """
    provenance: list[SourceProvenance] = []

    delivery_progress_pct: Optional[float] = None
    delivery_status: Optional[str] = None
    if entry.jira_project_id and entry.jira_key and jira_client is not None:
        try:
            issues_result = jira_client.get_project_issues(entry.jira_key, as_of=as_of)
        except Exception:  # noqa: BLE001 - any source failure degrades to UNKNOWN, never crashes or fabricates
            delivery_status = "UNKNOWN"
        else:
            if issues_result.partial_failure:
                # JiraClient already retried and gave up (Phase 3) rather than
                # raising — but computing delivery_progress_pct from a known-
                # incomplete fetch would be misleading (some records may be
                # missing, not zero), so this counts as unmapped-for-this-run
                # exactly like an outright exception, not a lesser case.
                delivery_status = "UNKNOWN"
            else:
                delivery_progress_pct = sprint_metrics.overall_delivery_progress_pct(issues_result.records)
                provenance.append(
                    SourceProvenance(
                        source_system=SourceSystem.JIRA,
                        source_record_id=entry.jira_key,
                        retrieved_timestamp=issues_result.retrieved_timestamp,
                    )
                )
    else:
        delivery_status = "UNKNOWN"

    approved_budget = actual_spend = committed_spend = forecast_spend = remaining_budget = None
    budget_consumption_pct = None
    currency = "USD"
    financial_status: Optional[str] = None
    if entry.finance_project_id and financial_source is not None:
        try:
            latest_period = financial_source.get_latest_reporting_period(entry.finance_project_id)
            raw_record = (
                financial_source.get_project_finances(entry.finance_project_id, latest_period)
                if latest_period
                else None
            )
        except Exception:  # noqa: BLE001 - any source failure degrades to UNKNOWN, never crashes or fabricates
            raw_record = None
            financial_status = "UNKNOWN"
        if raw_record is not None:
            calculated = raw_record.with_calculated_fields()
            approved_budget = calculated.approved_budget
            actual_spend = calculated.actual_spend
            committed_spend = calculated.committed_spend
            forecast_spend = calculated.forecast_spend
            remaining_budget = calculated.remaining_budget
            budget_consumption_pct = calculated.budget_consumption_pct
            currency = calculated.currency
            provenance.append(
                SourceProvenance(
                    source_system=SourceSystem.FINANCE,
                    source_record_id=f"{entry.finance_project_id}/{latest_period}",
                    retrieved_timestamp=calculated.retrieved_timestamp,
                )
            )
        else:
            financial_status = "UNKNOWN"  # mapped, but source genuinely has no record
    else:
        financial_status = "UNKNOWN"  # not mapped at all

    return Project(
        project_id=entry.project_id,
        project_key=_display_key(entry),
        project_name=entry.project_name,
        project_manager=entry.project_manager,
        business_owner=entry.business_owner,
        technical_owner=entry.technical_owner,
        project_status=entry.project_status,
        jira_project_key=entry.jira_key if entry.jira_project_id else None,
        approved_budget=approved_budget,
        actual_spend=actual_spend,
        committed_spend=committed_spend,
        forecast_spend=forecast_spend,
        remaining_budget=remaining_budget,
        currency=currency,
        budget_consumption_pct=budget_consumption_pct,
        delivery_progress_pct=delivery_progress_pct,
        financial_status=financial_status,
        delivery_status=delivery_status,
        provenance=provenance,
    )


def build_unified_portfolio(
    mapping: ProjectMappingConfig,
    jira_client: Optional[JiraClient],
    financial_source: Optional[FinancialDataSource],
    as_of: date,
) -> list[Project]:
    return [
        build_unified_project(entry, jira_client, financial_source, as_of)
        for entry in mapping.entries.values()
    ]


def detect_mapping_risks(project: Project) -> list[Risk]:
    """Section 6: surface the discrepancy rather than silently proceeding.
    A project can trigger 0, 1, or (in principle) both of these — they're
    independent per-side checks."""
    risks = []
    if project.financial_status == "UNKNOWN":
        risks.append(
            Risk(
                risk_id=f"MAP-{project.project_id}-FIN-{uuid4().hex[:8]}",
                project_id=project.project_id,
                category="Data Quality",
                severity=RiskSeverity.MEDIUM,
                description=f"{project.project_name} has no financial system mapping",
                evidence=[f"config/project_mapping.yaml: finance_project_id is null for {project.project_id}"],
                recommended_action="Assign a finance_project_id in config/project_mapping.yaml to enable financial oversight.",
                reason_codes=["DATA_MAPPING"],
            )
        )
    if project.delivery_status == "UNKNOWN":
        risks.append(
            Risk(
                risk_id=f"MAP-{project.project_id}-JIRA-{uuid4().hex[:8]}",
                project_id=project.project_id,
                category="Data Quality",
                severity=RiskSeverity.MEDIUM,
                description=f"{project.project_name} has no Jira project mapping",
                evidence=[f"config/project_mapping.yaml: jira_project_id is null for {project.project_id}"],
                recommended_action="Assign a jira_project_id in config/project_mapping.yaml to enable delivery oversight.",
                reason_codes=["DATA_MAPPING"],
            )
        )
    return risks
