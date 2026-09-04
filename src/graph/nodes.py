"""Node implementations for the LangGraph workflow (Section 7).

Each `make_*` function is a factory: it closes over `NodeDeps` (the live
connectors/services a node needs) and returns the actual `(state) ->
partial_state` callable LangGraph calls. Dependencies are injected this way
— rather than read from state or imported as module globals — so the same
node logic runs against `CSVFinancialDataSource`/`FileMemoryStore` in tests
and notebooks today, and against a live Jira/SQL/Mem0-platform stack later,
with zero changes to the functions below.

`as_of` is deliberately NOT baked into the closure — every node derives it
from `state["requested_at"]`, so one compiled graph is reusable across many
requests instead of needing to be rebuilt per invocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

import yaml

from src.agents import memory_agent, report_agent
from src.connectors.financial_client import FinancialDataSource
from src.connectors.jira_client import JiraClient
from src.graph.state import AgentState, Citation, ValidationResult
from src.models.common import RiskSeverity, SourceSystem, TrendDirection
from src.models.finance import FinancialRecord
from src.models.issue import JiraIssue
from src.models.project import Project
from src.models.risk import Risk
from src.models.snapshot import ProjectSnapshot
from src.models.sprint import Sprint
from src.services import delivery_metrics, financial_metrics, project_unifier, reconciliation, risk_engine, snapshot_builder, sprint_metrics, trend_engine
from src.services.memory_store import MemoryScope, MemoryStore
from src.services.project_unifier import ProjectMappingConfig, ProjectMappingEntry
from src.utils.logging import log_event
from src.utils.time import utcnow

DEFAULT_RULES_PATH = "config/risk_rules.yaml"

PORTFOLIO_SENTINEL = "PORTFOLIO"
"""`Risk.project_id` for findings that aren't about any one project (e.g. a
Jira-wide fetch failure) — a required str field has to hold something, and
inventing a fake project is worse than an explicit, documented sentinel."""


def _load_jira_freshness_threshold_hours(path: str = DEFAULT_RULES_PATH) -> int:
    with open(path) as f:
        return yaml.safe_load(f)["freshness_thresholds_hours"]["jira"]


@dataclass
class NodeDeps:
    jira_client: JiraClient
    financial_source: FinancialDataSource
    memory_store: MemoryStore
    mapping: ProjectMappingConfig
    delivery_rules: dict = field(default_factory=delivery_metrics.load_delivery_risk_rules)
    financial_rules: dict = field(default_factory=financial_metrics.load_financial_risk_rules)
    combined_matrix: dict = field(default_factory=risk_engine.load_combined_risk_matrix)
    jira_freshness_threshold_hours: int = field(default_factory=_load_jira_freshness_threshold_hours)
    history_limit: Optional[int] = 12
    """Weeks of ProjectSnapshot history to retrieve per project. 12 is an
    arbitrary but reasonable quarter-ish window — trend_engine.py only ever
    needs the most recent one or two anyway."""
    chat_model: Optional[Any] = None
    """Any LangChain `BaseChatModel` (e.g. `report_agent.build_default_chat_model()`).
    None (the default — and the only option this environment can actually
    run, with no OPENAI_API_KEY configured) means generate_response uses
    the deterministic executive summary; report_agent.render_weekly_report
    produces every other section identically either way."""


# --------------------------------------------------------------------------
# Shared helpers — translating between the mapping key (Section 6's
# canonical id) and the raw ids each source actually uses.
# --------------------------------------------------------------------------


def _resolve_scope(state: AgentState, mapping: ProjectMappingConfig) -> list[ProjectMappingEntry]:
    filter_ids = state.get("project_filter")
    if not filter_ids:
        return list(mapping.entries.values())
    return [mapping.entries[pid] for pid in filter_ids if pid in mapping.entries]


def _issues_for(entry: ProjectMappingEntry, issues: list[JiraIssue]) -> list[JiraIssue]:
    if not entry.jira_project_id:
        return []
    return [i for i in issues if i.project_id == entry.jira_project_id]


def _sprints_for(entry: ProjectMappingEntry, sprints: list[Sprint]) -> list[Sprint]:
    if not entry.jira_project_id:
        return []
    return [s for s in sprints if s.project_id == entry.jira_project_id]


def _latest_sprint_for(entry: ProjectMappingEntry, sprints: list[Sprint]) -> Optional[Sprint]:
    """Prefers the latest CLOSED sprint — matching the convention every
    other phase already used (`get_previous_sprints(key, 1, as_of)`), not
    just "whichever sprint in the fetched set has the latest end_date".
    That distinction matters because `jira_sprints` includes the currently
    ACTIVE sprint too (fetch_delivery_data fetches both): this dataset gives
    each issue a fixed final status regardless of `as_of`, so an in-progress
    sprint's `completion_pct` is already fully baked in and would otherwise
    silently outrank a genuinely-completed, earlier-ending sprint just for
    having a later end_date. Falls back to whatever's available (e.g. an
    ACTIVE or TODO sprint) only when no CLOSED sprint exists yet."""
    project_sprints = _sprints_for(entry, sprints)
    closed = [s for s in project_sprints if s.sprint_status == "CLOSED"]
    pool = closed or project_sprints
    return max(pool, key=lambda s: s.end_date) if pool else None


def _financial_record_for(entry: ProjectMappingEntry, records: list[FinancialRecord]) -> Optional[FinancialRecord]:
    if not entry.finance_project_id:
        return None
    matches = [r for r in records if r.project_id == entry.finance_project_id]
    return matches[0] if matches else None


def _build_snapshot_for_project(entry: ProjectMappingEntry, project: Project, state: AgentState, snapshot_date) -> ProjectSnapshot:
    """Shared by generate_response (to compare this run's own facts against
    prior history before they're persisted) and persist_snapshot (to
    actually write them) — both need the identical `ProjectSnapshot` built
    from the identical inputs, just at different graph stages, so this is
    the one place that construction happens."""
    issues = state.get("jira_issues", [])
    sprints = state.get("jira_sprints", [])
    records = state.get("financial_data", [])
    risks = state.get("risks", [])

    project_risks = {r.category: r for r in risks if r.project_id == project.project_id}
    assessment = risk_engine.RiskAssessment(
        project_id=project.project_id,
        delivery_risk=project_risks.get("Delivery"),
        financial_risk=project_risks.get("Financial"),
        combined_rag=project.risk_status,
        reason_codes=sorted({code for r in project_risks.values() for code in r.reason_codes}),
        assessed_at=utcnow().isoformat(),
    )

    project_issues = _issues_for(entry, issues) or None
    latest_sprint = _latest_sprint_for(entry, sprints)
    financial_record = _financial_record_for(entry, records)

    return snapshot_builder.build_snapshot(project, assessment, project_issues, latest_sprint, financial_record, snapshot_date)


# --------------------------------------------------------------------------
# classify_request
# --------------------------------------------------------------------------


def make_classify_request(deps: NodeDeps):
    def classify_request(state: AgentState) -> AgentState:
        """Deterministic, keyword-based placeholder for Phase 9's real
        LLM-based intent classifier — matches the question text against
        known jira_key/project_name/mapping-key strings from
        config/project_mapping.yaml. Never fetches data (Section 7).

        Only infers `project_filter` from the question when the caller
        didn't already supply one. A caller that already knows which
        project(s) it wants (a notebook replaying history, a UI page with
        its own project selector) is providing ground truth; overwriting
        that with a guess parsed from free text — even to `None` — would
        silently discard a correct, explicit answer in favor of a worse
        inferred one."""
        if "project_filter" in state:
            project_filter = state["project_filter"]
        else:
            question = (state.get("user_question") or "").lower()
            matched = sorted(
                {
                    project_id
                    for project_id, entry in deps.mapping.entries.items()
                    if (entry.jira_key and entry.jira_key.lower() in question)
                    or entry.project_name.lower() in question
                    or project_id.lower() in question
                }
            )
            project_filter = matched or None

        intent = "project_deep_dive" if project_filter else "portfolio_status"
        return {"intent": intent, "project_filter": project_filter}

    return classify_request


# --------------------------------------------------------------------------
# fetch/validate delivery
# --------------------------------------------------------------------------


def make_fetch_delivery_data(deps: NodeDeps):
    def fetch_delivery_data(state: AgentState) -> AgentState:
        as_of = state["requested_at"].date()
        entries = _resolve_scope(state, deps.mapping)

        issues: list[JiraIssue] = []
        sprints: list[Sprint] = []
        seen_sprint_ids: set[str] = set()
        freshest = None
        partial_failure = False

        for entry in entries:
            if not entry.jira_key:
                continue
            result = deps.jira_client.get_project_issues(entry.jira_key, as_of=as_of)
            issues.extend(result.records)
            partial_failure = partial_failure or result.partial_failure
            freshest = result.retrieved_timestamp if freshest is None else max(freshest, result.retrieved_timestamp)

            previous = deps.jira_client.get_previous_sprints(entry.jira_key, count=100, as_of=as_of)
            current = deps.jira_client.get_current_sprint(entry.jira_key, as_of=as_of)
            for sprint in previous + ([current] if current else []):
                if sprint.sprint_id not in seen_sprint_ids:
                    sprints.append(sprint)
                    seen_sprint_ids.add(sprint.sprint_id)

        log_event(
            "records_retrieved",
            state.get("request_id", "UNKNOWN"),
            source="Jira",
            issue_count=len(issues),
            sprint_count=len(sprints),
            partial_failure=partial_failure,
        )
        return {
            "jira_issues": issues,
            "jira_sprints": sprints,
            "jira_data_freshness": freshest,
            "jira_fetch_partial_failure": partial_failure,
        }

    return fetch_delivery_data


def make_validate_delivery_data(deps: NodeDeps):
    def validate_delivery_data(state: AgentState) -> AgentState:
        requested_at = state["requested_at"]
        freshness = state.get("jira_data_freshness")
        partial_failure = state.get("jira_fetch_partial_failure", False)

        results: list[ValidationResult] = []
        risks: list[Risk] = []

        if freshness is not None:
            age_hours = (requested_at - freshness).total_seconds() / 3600
            fresh_enough = age_hours <= deps.jira_freshness_threshold_hours
            results.append(
                {
                    "check_name": "jira_freshness",
                    "passed": fresh_enough,
                    "detail": f"age={age_hours:.1f}h threshold={deps.jira_freshness_threshold_hours}h",
                }
            )
            if not fresh_enough:
                risks.append(
                    Risk(
                        risk_id=f"DQ-{PORTFOLIO_SENTINEL}-JIRA-STALE-{uuid4().hex[:8]}",
                        project_id=PORTFOLIO_SENTINEL,
                        category="Data Quality",
                        severity=RiskSeverity.MEDIUM,
                        description=f"Jira data is {age_hours:.1f} hours old, exceeding the {deps.jira_freshness_threshold_hours}h threshold",
                        evidence=[f"jira_data_freshness={freshness.isoformat()}", f"requested_at={requested_at.isoformat()}"],
                        reason_codes=["DATA_STALE"],
                    )
                )

        results.append(
            {"check_name": "jira_fetch_complete", "passed": not partial_failure, "detail": f"partial_failure={partial_failure}"}
        )
        if partial_failure:
            risks.append(
                Risk(
                    risk_id=f"DQ-{PORTFOLIO_SENTINEL}-JIRA-PARTIAL-{uuid4().hex[:8]}",
                    project_id=PORTFOLIO_SENTINEL,
                    category="Data Quality",
                    severity=RiskSeverity.HIGH,
                    description="Jira pagination did not complete for one or more projects",
                    evidence=["jira_fetch_partial_failure=True"],
                    recommended_action="Retry the fetch — do not treat this run's delivery figures as complete.",
                    reason_codes=["DATA_FETCH_INCOMPLETE"],
                )
            )

        return {"delivery_validation": results, "risks": risks}

    return validate_delivery_data


# --------------------------------------------------------------------------
# fetch/validate financial
# --------------------------------------------------------------------------


def make_fetch_financial_data(deps: NodeDeps):
    def fetch_financial_data(state: AgentState) -> AgentState:
        """Phase 12 hardening: a `FinancialDataSource` exception for one
        project (a live SQL/Snowflake/REST source timing out, an auth
        failure, whatever) is caught per-project — it no longer crashes
        the whole graph the way an unhandled exception here used to. That
        project's financial data stays absent (never fabricated to keep
        going), the fetch continues for every OTHER project, and
        `financial_fetch_partial_failure` records that this happened, same
        pattern `fetch_delivery_data` already used for Jira outages."""
        entries = _resolve_scope(state, deps.mapping)
        records: list[FinancialRecord] = []
        freshest = None
        partial_failure = False

        for entry in entries:
            if not entry.finance_project_id:
                continue
            try:
                latest_period = deps.financial_source.get_latest_reporting_period(entry.finance_project_id)
                if latest_period is None:
                    continue
                raw = deps.financial_source.get_project_finances(entry.finance_project_id, latest_period)
            except Exception:  # noqa: BLE001 - deliberately broad: any source failure degrades gracefully, never crashes the graph
                partial_failure = True
                continue
            if raw is None:
                continue
            calculated = raw.with_calculated_fields()
            records.append(calculated)
            freshest = calculated.retrieved_timestamp if freshest is None else max(freshest, calculated.retrieved_timestamp)

        log_event(
            "records_retrieved",
            state.get("request_id", "UNKNOWN"),
            source="Finance",
            record_count=len(records),
            partial_failure=partial_failure,
        )
        return {
            "financial_data": records,
            "financial_data_freshness": freshest,
            "financial_fetch_partial_failure": partial_failure,
        }

    return fetch_financial_data


def make_validate_financial_data(deps: NodeDeps):
    def validate_financial_data(state: AgentState) -> AgentState:
        results: list[ValidationResult] = []
        risks: list[Risk] = []

        if state.get("financial_fetch_partial_failure"):
            results.append(
                {"check_name": "financial_fetch_complete", "passed": False, "detail": "financial_fetch_partial_failure=True"}
            )
            risks.append(
                Risk(
                    risk_id=f"DQ-{PORTFOLIO_SENTINEL}-FIN-PARTIAL-{uuid4().hex[:8]}",
                    project_id=PORTFOLIO_SENTINEL,
                    category="Data Quality",
                    severity=RiskSeverity.HIGH,
                    description="Financial data source failed for one or more projects",
                    evidence=["financial_fetch_partial_failure=True"],
                    recommended_action="Retry the fetch — do not treat this run's financial figures as complete.",
                    reason_codes=["DATA_FETCH_INCOMPLETE"],
                )
            )
            # Per-project checks below need the source to still be reachable
            # (they re-derive latest_period); with a known source failure,
            # trying again would just risk the same crash this hardening
            # exists to prevent, for no additional signal.
            return {"financial_validation": results, "risks": risks}

        entries = _resolve_scope(state, deps.mapping)
        records = state.get("financial_data", [])
        as_of = state["requested_at"].date()

        for entry in entries:
            if not entry.finance_project_id:
                continue  # not mapped at all — unify_projects/detect_mapping_risks covers this, not Accuracy Check 3

            latest_period = deps.financial_source.get_latest_reporting_period(entry.finance_project_id)
            record = _financial_record_for(entry, records)

            missing = reconciliation.detect_missing_financial_record(
                entry.project_id, latest_period or "no-period-available", record
            )
            results.append(
                {
                    "check_name": f"{entry.project_id}_financial_completeness",
                    "passed": missing is None,
                    "detail": missing.description if missing else "record present",
                }
            )
            if missing:
                risks.append(missing)
                continue  # nothing further to check without a record

            stale = reconciliation.detect_stale_reporting_period(entry.project_id, latest_period, as_of)
            results.append(
                {
                    "check_name": f"{entry.project_id}_financial_freshness",
                    "passed": stale is None,
                    "detail": stale.description if stale else "within threshold",
                }
            )
            if stale:
                risks.append(stale)

            reconcile = reconciliation.detect_reconciliation_failure(record, project_id=entry.project_id)
            results.append(
                {
                    "check_name": f"{entry.project_id}_financial_reconciliation",
                    "passed": reconcile is None,
                    "detail": reconcile.description if reconcile else "consistent with source",
                }
            )
            if reconcile:
                risks.append(reconcile)

        return {"financial_validation": results, "risks": risks}

    return validate_financial_data


# --------------------------------------------------------------------------
# unify_projects — Phase 5, wired into the graph in Phase 8
# --------------------------------------------------------------------------


def make_unify_projects(deps: NodeDeps):
    def unify_projects(state: AgentState) -> AgentState:
        """Re-queries `jira_client`/`financial_source` directly (via
        `project_unifier.build_unified_project`) rather than reusing
        `jira_issues`/`financial_data` already sitting in state — a known,
        accepted inefficiency against these cheap in-memory CSV sources.
        A real remote source would want `project_unifier` refactored to
        accept pre-fetched data instead of re-querying; not needed yet."""
        as_of = state["requested_at"].date()
        entries = _resolve_scope(state, deps.mapping)

        projects = []
        risks: list[Risk] = []
        for entry in entries:
            project = project_unifier.build_unified_project(entry, deps.jira_client, deps.financial_source, as_of)
            projects.append(project)
            risks.extend(project_unifier.detect_mapping_risks(project))

        return {"unified_projects": projects, "risks": risks}

    return unify_projects


# --------------------------------------------------------------------------
# retrieve_historical_memory
# --------------------------------------------------------------------------


def make_retrieve_historical_memory(deps: NodeDeps):
    def retrieve_historical_memory(state: AgentState) -> AgentState:
        scope = MemoryScope(deps.mapping.organization_id, deps.mapping.portfolio_id)
        projects = state.get("unified_projects", [])
        context = memory_agent.retrieve_historical_context(
            deps.memory_store, scope, [p.project_id for p in projects], limit=deps.history_limit
        )
        log_event(
            "memory_retrieved",
            state.get("request_id", "UNKNOWN"),
            project_count=len(context),
            snapshot_count=sum(len(v) for v in context.values()),
        )
        return {"historical_context": context, "memory_freshness": utcnow()}

    return retrieve_historical_memory


# --------------------------------------------------------------------------
# calculate_metrics
# --------------------------------------------------------------------------


def make_calculate_metrics(deps: NodeDeps):
    def calculate_metrics(state: AgentState) -> AgentState:
        entries = _resolve_scope(state, deps.mapping)
        issues = state.get("jira_issues", [])
        sprints = state.get("jira_sprints", [])
        records = state.get("financial_data", [])

        metrics: dict[str, dict] = {}
        for entry in entries:
            project_issues = _issues_for(entry, issues)
            latest_sprint = _latest_sprint_for(entry, sprints)
            record = _financial_record_for(entry, records)
            blocked = [i for i in project_issues if i.blocked]
            blocker_ages = [i.blocker_age_days for i in blocked if i.blocker_age_days is not None]

            metrics[entry.project_id] = {
                "latest_sprint_id": latest_sprint.sprint_id if latest_sprint else None,
                "sprint_completion_pct": latest_sprint.completion_pct if latest_sprint else None,
                "overall_delivery_progress_pct": sprint_metrics.overall_delivery_progress_pct(project_issues) if project_issues else None,
                "blocked_issue_count": len(blocked) if project_issues else None,
                "oldest_blocker_age_days": max(blocker_ages) if blocker_ages else None,
                "reporting_period": record.reporting_period if record else None,
                "approved_budget": record.approved_budget if record else None,
                "remaining_budget": record.remaining_budget if record else None,
                "budget_consumption_pct": record.budget_consumption_pct if record else None,
                "forecast_variance": record.forecast_variance if record else None,
            }

        return {"calculated_metrics": metrics}

    return calculate_metrics


# --------------------------------------------------------------------------
# risk analysis — three separate nodes calling the same primitives
# risk_engine.assess_project_risk bundles for non-graph callers, kept
# separate here for Section 2's per-stage audit trail.
# --------------------------------------------------------------------------


def make_analyze_delivery_risk(deps: NodeDeps):
    def analyze_delivery_risk(state: AgentState) -> AgentState:
        entries = _resolve_scope(state, deps.mapping)
        issues = state.get("jira_issues", [])
        sprints = state.get("jira_sprints", [])
        projects_by_id = {p.project_id: p for p in state.get("unified_projects", [])}

        risks: list[Risk] = []
        for entry in entries:
            project = projects_by_id.get(entry.project_id)
            if project is None or project.delivery_status == "UNKNOWN":
                continue
            risks.append(
                delivery_metrics.classify_delivery_risk(
                    entry.project_id, _issues_for(entry, issues), _latest_sprint_for(entry, sprints), deps.delivery_rules
                )
            )
        return {"risks": risks}

    return analyze_delivery_risk


def make_analyze_financial_risk(deps: NodeDeps):
    def analyze_financial_risk(state: AgentState) -> AgentState:
        entries = _resolve_scope(state, deps.mapping)
        records = state.get("financial_data", [])
        projects_by_id = {p.project_id: p for p in state.get("unified_projects", [])}

        risks: list[Risk] = []
        for entry in entries:
            project = projects_by_id.get(entry.project_id)
            if project is None or project.financial_status == "UNKNOWN":
                continue
            record = _financial_record_for(entry, records)
            if record is None:
                continue  # mapped but source had nothing this period — already flagged by validate_financial_data
            risks.append(
                financial_metrics.classify_financial_risk(
                    record, project.delivery_progress_pct, deps.financial_rules, project_id=entry.project_id
                )
            )
        return {"risks": risks}

    return analyze_financial_risk


def make_analyze_cross_domain_risk(deps: NodeDeps):
    def analyze_cross_domain_risk(state: AgentState) -> AgentState:
        """The one node allowed to rewrite `unified_projects` (see
        src/graph/state.py's docstring) — it's finalizing `risk_status`,
        the one field `unify_projects` deliberately left `UNKNOWN`."""
        risks_by_project: dict[str, dict[str, Risk]] = {}
        for r in state.get("risks", []):
            risks_by_project.setdefault(r.project_id, {})[r.category] = r

        updated_projects = []
        for project in state.get("unified_projects", []):
            project_risks = risks_by_project.get(project.project_id, {})
            delivery_risk = project_risks.get("Delivery")
            financial_risk = project_risks.get("Financial")
            combined_rag = risk_engine.combine_risk(
                delivery_risk.severity if delivery_risk else None,
                financial_risk.severity if financial_risk else None,
                deps.combined_matrix,
            )
            updated_projects.append(project.model_copy(update={"risk_status": combined_rag}))

        return {"unified_projects": updated_projects}

    return analyze_cross_domain_risk


# --------------------------------------------------------------------------
# validate_findings
# --------------------------------------------------------------------------

_CONFIDENCE_DEGRADING_REASON_CODES = {
    "DATA_STALE",
    "DATA_INCONSISTENT",
    "DATA_COMPLETENESS",
    "DATA_MAPPING",
    "DATA_FETCH_INCOMPLETE",
}


def make_validate_findings(deps: NodeDeps):
    def validate_findings(state: AgentState) -> AgentState:
        """Accuracy Check 10 — the final gate, re-checking rather than
        merely trusting earlier nodes. Three things happen here:

        1. Memory freshness (Phase 11): `freshness_thresholds_hours.memory`
           existed in config since Phase 1 but nothing ever read it until
           now — `reconciliation.detect_stale_memory` per project, a soft
           confidence degradation exactly like Jira/Finance staleness.
        2. Evidence completeness (Phase 11, Accuracy Check 7 made active
           rather than a coding convention): every HIGH-severity risk must
           carry evidence. Every detector in this codebase already does
           this by construction, so this should never fire — but "should
           never" is exactly what a final validation gate exists to catch
           if it's ever wrong, and a Risk with no evidence reaching the
           final report would be a direct violation of the whole accuracy
           framework's central promise.
        3. Orphan project references (Phase 11): every `Risk.project_id` in
           `state["risks"]` must resolve to either the portfolio sentinel or
           an actual `unified_projects` entry — a risk attributed to a
           project that doesn't exist would be indistinguishable from a
           hallucinated finding once it reached the rendered report.

        (1) only ever downgrades `confidence`. (2) and (3) are structural
        contract violations, not data-quality gaps — they flip
        `validation_passed=False`, same tier as `DATA_FETCH_INCOMPLETE`.
        """
        risks = state.get("risks", [])
        projects = state.get("unified_projects", [])
        as_of = state["requested_at"].date()
        historical_context = state.get("historical_context", {})

        memory_staleness_risks = [
            r
            for p in projects
            if (r := reconciliation.detect_stale_memory(p.project_id, historical_context.get(p.project_id, []), as_of))
        ]

        data_quality_risks = [r for r in risks if r.category == "Data Quality"] + memory_staleness_risks
        degradation_signals = sum(
            1 for r in data_quality_risks if set(r.reason_codes) & _CONFIDENCE_DEGRADING_REASON_CODES
        )

        if degradation_signals >= 2:
            confidence = "LOW CONFIDENCE"
        elif degradation_signals == 1:
            confidence = "MEDIUM CONFIDENCE"
        else:
            confidence = "HIGH CONFIDENCE"

        results: list[ValidationResult] = []
        structural_failures: list[str] = []

        missing_evidence = [r for r in risks if r.severity.value == "HIGH" and not r.evidence]
        results.append(
            {
                "check_name": "evidence_completeness",
                "passed": not missing_evidence,
                "detail": (
                    "every HIGH-severity risk carries evidence"
                    if not missing_evidence
                    else f"{len(missing_evidence)} HIGH-severity risk(s) with no evidence: {[r.risk_id for r in missing_evidence]}"
                ),
            }
        )
        if missing_evidence:
            structural_failures.append("evidence_completeness")

        known_project_ids = {p.project_id for p in projects} | {PORTFOLIO_SENTINEL}
        orphan_risks = [r for r in risks if r.project_id not in known_project_ids]
        results.append(
            {
                "check_name": "project_reference_integrity",
                "passed": not orphan_risks,
                "detail": (
                    "every risk references a real project"
                    if not orphan_risks
                    else f"{len(orphan_risks)} risk(s) reference an unknown project_id: {[r.risk_id for r in orphan_risks]}"
                ),
            }
        )
        if orphan_risks:
            structural_failures.append("project_reference_integrity")

        if any("DATA_FETCH_INCOMPLETE" in r.reason_codes for r in data_quality_risks):
            structural_failures.append("data_fetch_incomplete")

        validation_passed = not structural_failures
        results.insert(
            0,
            {
                "check_name": "overall_validation",
                "passed": validation_passed,
                "detail": (
                    f"{len(data_quality_risks)} data-quality finding(s); confidence={confidence}"
                    if validation_passed
                    else f"structural failure(s): {', '.join(structural_failures)}"
                ),
            },
        )

        return {
            "validation_passed": validation_passed,
            "validation_results": results,
            "confidence": confidence,
            "risks": memory_staleness_risks,
        }

    return validate_findings


# --------------------------------------------------------------------------
# generate_response — deterministic formatter (Phase 8); Phase 9 swaps this
# for a real LLM call over the same inputs.
# --------------------------------------------------------------------------


def make_generate_response(deps: NodeDeps):
    def generate_response(state: AgentState) -> AgentState:
        """Phase 8 built this as a one-line-per-project placeholder,
        documented as standing in for Phase 9's real report. Phase 9:
        `report_agent.render_weekly_report` (Section 15's full format) for
        portfolio-wide requests, `report_agent.render_chat_answer`
        (Section 17's Answer/Evidence/Trend/... format) when
        `project_filter` scopes to exactly one project. The deterministic
        backbone is identical either way; `deps.chat_model` (None by
        default — no OPENAI_API_KEY in this environment) only ever
        replaces the Executive Summary paragraph, and a narration failure
        falls back to the deterministic one rather than breaking the run.
        """
        entries = _resolve_scope(state, deps.mapping)
        entries_by_id = {e.project_id: e for e in entries}
        projects = state.get("unified_projects", [])
        risks = state.get("risks", [])
        confidence = state.get("confidence", "LOW CONFIDENCE")
        snapshot_date = state["requested_at"].date()

        current_snapshots: dict[str, ProjectSnapshot] = {}
        for project in projects:
            entry = entries_by_id.get(project.project_id)
            if entry is not None:
                current_snapshots[project.project_id] = _build_snapshot_for_project(entry, project, state, snapshot_date)

        citations: list[Citation] = [
            {
                "source_system": provenance.source_system.value,
                "source_record_id": provenance.source_record_id,
                "retrieved_timestamp": provenance.retrieved_timestamp.isoformat(),
            }
            for project in projects
            for provenance in project.provenance
        ]

        if len(projects) == 1:
            project = projects[0]
            project_risks = [r for r in risks if r.project_id == project.project_id]
            history = state.get("historical_context", {}).get(project.project_id, [])
            previous = history[-1] if history else None
            trend = trend_engine.classify_trend(previous, current_snapshots.get(project.project_id))
            final_answer = report_agent.render_chat_answer(
                state.get("user_question", ""), project, project_risks, trend, confidence, citations
            )
        else:
            executive_summary_override = None
            if deps.chat_model is not None:
                try:
                    executive_summary_override = report_agent.narrate_executive_summary(deps.chat_model, projects, risks)
                except Exception:
                    executive_summary_override = None  # fall back to the deterministic summary — never break the run over narration

            requested_at = state["requested_at"]
            jira_freshness = state.get("jira_data_freshness")
            financial_freshness = state.get("financial_data_freshness")
            final_answer = report_agent.render_weekly_report(
                projects,
                risks,
                state.get("jira_issues", []),
                state.get("historical_context", {}),
                current_snapshots,
                confidence,
                jira_freshness_hours=(requested_at - jira_freshness).total_seconds() / 3600 if jira_freshness else None,
                financial_freshness_hours=(requested_at - financial_freshness).total_seconds() / 3600 if financial_freshness else None,
                executive_summary_override=executive_summary_override,
            )

        if not state.get("validation_passed", True):
            final_answer += "\n\nNOTE: validation did not pass this run — see validation_results for what failed."

        return {"final_answer": final_answer, "citations": citations, "current_snapshots": current_snapshots}

    return generate_response


# --------------------------------------------------------------------------
# persist_snapshot
# --------------------------------------------------------------------------


def make_persist_snapshot(deps: NodeDeps):
    def persist_snapshot(state: AgentState) -> AgentState:
        """Reuses `state["current_snapshots"]` — generate_response already
        built exactly these to compare this run's facts against prior
        history, so persist_snapshot just writes them rather than
        reconstructing a second time. Falls back to building them itself
        only if that key is somehow absent (e.g. a future conditional route
        that skips generate_response), so this node still works standalone."""
        scope = MemoryScope(deps.mapping.organization_id, deps.mapping.portfolio_id)
        snapshot_date = state["requested_at"].date()
        entries = _resolve_scope(state, deps.mapping)
        entries_by_id = {e.project_id: e for e in entries}

        current_snapshots = state.get("current_snapshots") or {}

        written_ids: list[str] = []
        for project in state.get("unified_projects", []):
            entry = entries_by_id.get(project.project_id)
            if entry is None:
                continue
            snapshot = current_snapshots.get(project.project_id) or _build_snapshot_for_project(
                entry, project, state, snapshot_date
            )
            written_ids.append(memory_agent.persist_project_snapshot(deps.memory_store, scope, snapshot))

        log_event(
            "datasource_access",
            state.get("request_id", "UNKNOWN"),
            source="Memory",
            operation="persist_snapshot",
            written_count=len(written_ids),
        )
        return {"snapshot_persisted": True, "snapshots_written": written_ids}

    return persist_snapshot
