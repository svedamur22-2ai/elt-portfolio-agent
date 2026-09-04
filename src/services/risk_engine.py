"""Cross-domain risk combination (Section 10). Delivery and financial risk
are each classified independently — services/delivery_metrics.py and
services/financial_metrics.py — deliberately without knowledge of each
other. This module is the ONLY place they're combined, via
config/risk_rules.yaml's `combined_risk_matrix`, so that matrix stays the
single source of truth for "what does HIGH delivery + MEDIUM financial
actually mean for the RAG status" rather than that judgment call being
duplicated or drifting between callers.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import yaml
from pydantic import BaseModel

from src.models.common import RAGStatus, RiskSeverity
from src.models.finance import FinancialRecord
from src.models.issue import JiraIssue
from src.models.project import Project
from src.models.risk import Risk
from src.models.sprint import Sprint
from src.services import delivery_metrics, financial_metrics
from src.utils.time import utcnow

DEFAULT_RULES_PATH = "config/risk_rules.yaml"


def load_combined_risk_matrix(path: str = DEFAULT_RULES_PATH) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["combined_risk_matrix"]


_SEVERITY_SCORE = {RiskSeverity.LOW: 0.0, RiskSeverity.MEDIUM: 1.0, RiskSeverity.HIGH: 2.0}


def compute_risk_score(
    delivery_severity: Optional[RiskSeverity], financial_severity: Optional[RiskSeverity]
) -> Optional[float]:
    """A continuous 0-2 score (mean of whichever severity ordinals are
    available) for `ProjectSnapshot.risk_score` — week-over-week trend
    granularity the 3-value combined RAG can't show on its own. HIGH+LOW
    and HIGH+HIGH can both land on the same RAG under some matrix
    configurations but are clearly different risk levels; this number is
    what lets trend_engine.py's `risk_score_delta` tell them apart.

    None only when NEITHER side could be assessed — uses whichever side(s)
    are available otherwise (Phase 5/6's unmapped-project handling means
    this is routinely called with just one side present)."""
    scores = [_SEVERITY_SCORE[s] for s in (delivery_severity, financial_severity) if s is not None]
    if not scores:
        return None
    return sum(scores) / len(scores)


def combine_risk(
    delivery_severity: Optional[RiskSeverity],
    financial_severity: Optional[RiskSeverity],
    matrix: Optional[dict] = None,
) -> RAGStatus:
    """`None` on either side means "could not be assessed" (project
    unmapped on that side, per Section 6) — NOT "assume LOW/healthy". Per
    the matrix's `unknown_default`, that always resolves to AMBER, never
    GREEN: missing information about half a project's risk profile is never
    grounds for calling it healthy."""
    matrix = matrix or load_combined_risk_matrix()
    if delivery_severity is None or financial_severity is None:
        return RAGStatus(matrix["unknown_default"])
    key = f"{delivery_severity.value.lower()}_{financial_severity.value.lower()}"
    return RAGStatus(matrix[key])


class RiskAssessment(BaseModel):
    project_id: str
    delivery_risk: Optional[Risk]
    """None when the project has no Jira mapping (Section 6) — not a Risk
    forced to some default severity."""
    financial_risk: Optional[Risk]
    """None when the project has no finance mapping."""
    combined_rag: RAGStatus
    reason_codes: list[str]
    assessed_at: str


def assess_project_risk(
    project: Project,
    jira_issues: Optional[list[JiraIssue]],
    latest_sprint: Optional[Sprint],
    financial_record: Optional[FinancialRecord],
    delivery_rules: Optional[dict] = None,
    financial_rules: Optional[dict] = None,
    combined_matrix: Optional[dict] = None,
) -> RiskAssessment:
    """The Phase 6 entry point: takes a unified `Project` (Phase 5) plus the
    raw per-side data still needed for detector-level evidence (a `Project`
    only carries aggregate figures, not the individual blocked issues or the
    calculated `FinancialRecord` a detector needs to cite) and produces one
    `RiskAssessment`.

    Whether each side even runs is driven by the Project's own
    `delivery_status`/`financial_status` — "UNKNOWN" means Phase 5 already
    determined this project isn't mapped on that side, and this function
    must not second-guess that by trying to assess data that isn't there.
    """
    delivery_risk: Optional[Risk] = None
    if project.delivery_status != "UNKNOWN" and jira_issues is not None:
        delivery_risk = delivery_metrics.classify_delivery_risk(
            project.project_id, jira_issues, latest_sprint, delivery_rules
        )

    financial_risk: Optional[Risk] = None
    if project.financial_status != "UNKNOWN" and financial_record is not None:
        financial_risk = financial_metrics.classify_financial_risk(
            financial_record, project.delivery_progress_pct, financial_rules, project_id=project.project_id
        )

    combined_rag = combine_risk(
        delivery_risk.severity if delivery_risk else None,
        financial_risk.severity if financial_risk else None,
        combined_matrix,
    )

    reason_codes = sorted(
        set((delivery_risk.reason_codes if delivery_risk else []))
        | set((financial_risk.reason_codes if financial_risk else []))
    )

    return RiskAssessment(
        project_id=project.project_id,
        delivery_risk=delivery_risk,
        financial_risk=financial_risk,
        combined_rag=combined_rag,
        reason_codes=reason_codes,
        assessed_at=utcnow().isoformat(),
    )
