"""LangGraph state object (Section 7).

A TypedDict (not a Pydantic BaseModel) because LangGraph's `StateGraph`
applies per-key reducers on partial updates between nodes — TypedDict is the
documented, supported shape for that. The *values* inside each key are still
the Pydantic models from src/models, so every write into state is still
type-checked at the point a node constructs it.

Hard rule enforced by convention (and checked in the validation node,
Phase 6/11): a node may only ADD keys or APPEND to list-typed keys it owns.
No node is allowed to mutate a fact another node already wrote — e.g.
analyze_financial_risk must not edit `financial_data`, only read it and
append to `risks`. This is what Section 7 means by "Do not allow agents to
silently overwrite factual values retrieved from source systems."

One documented exception: `analyze_cross_domain_risk` rewrites
`unified_projects` to fill in `Project.risk_status`. That field is
deliberately left `UNKNOWN` by `unify_projects` (Phase 5's project_unifier
never judges risk, only assembles facts) specifically so a later node can
finalize it — this is completing a field its owner left blank on purpose,
not overwriting a fact another node claimed as final.

Phase 1 sketched this graph before `project_unifier.py` (Phase 5) existed,
so two things changed from that original sketch once the real modules were
built: `jira_projects: list[Project]` became `unified_projects: list[Project]`
(the cross-source join, not a raw Jira project list — nothing in this
codebase actually needs Jira's raw project inventory downstream), and a
`unify_projects` node was added between the fetch/validate stages and
`retrieve_historical_memory`.
"""

from datetime import datetime
from operator import add
from typing import Annotated, Optional, TypedDict

from src.models import FinancialRecord, JiraIssue, Project, Risk, Sprint
from src.models.snapshot import ProjectSnapshot


class Citation(TypedDict):
    source_system: str  # "Jira" | "Finance" | "Historical Snapshot"
    source_record_id: str
    retrieved_timestamp: str


class ValidationResult(TypedDict):
    check_name: str
    passed: bool
    detail: str


class AgentState(TypedDict, total=False):
    # ---- input --------------------------------------------------------
    user_question: str
    project_filter: Optional[list[str]]
    """config/project_mapping.yaml keys (e.g. "PROJECT-10001"), NOT Jira
    keys or finance ids — the mapping key is the one identifier guaranteed
    to exist regardless of which side(s) a project is mapped on.
    None/empty = whole portfolio."""
    request_id: str
    requested_at: datetime

    # ---- classify_request ---------------------------------------------
    intent: str
    """"portfolio_status" | "project_deep_dive" for now — a deterministic,
    keyword-based placeholder (see nodes.make_classify_request) standing in
    for Phase 9's real LLM-based classifier. Since this graph is currently
    linear (no conditional routing yet), `intent` doesn't change which
    nodes run — only `project_filter`, which it also sets, actually
    affects execution."""

    # ---- fetch/validate delivery ---------------------------------------
    jira_issues: list[JiraIssue]
    jira_sprints: list[Sprint]
    jira_data_freshness: Optional[datetime]
    jira_fetch_partial_failure: bool
    """Set by fetch_delivery_data (a fact about its OWN fetch outcome, not
    a judgment about the data — that distinction is why this doesn't
    violate the ownership rule above). validate_delivery_data reads it and
    turns it into a pass/fail + a DATA_FETCH_INCOMPLETE risk if true."""
    delivery_validation: Annotated[list[ValidationResult], add]

    # ---- fetch/validate financial ---------------------------------------
    financial_data: list[FinancialRecord]
    """Already-calculated records (`with_calculated_fields()` applied by
    fetch_financial_data) — Section 5's formulas are pure arithmetic, not a
    judgment call, so computing them at fetch time doesn't blur the
    fetch/validate/analyze boundary the way a risk classification would."""
    financial_data_freshness: Optional[datetime]
    financial_fetch_partial_failure: bool
    """Mirrors `jira_fetch_partial_failure` (Phase 12 hardening) — a
    `FinancialDataSource` exception for one project no longer crashes the
    whole graph; it's caught, that project's financial data stays absent
    (never fabricated), and this flag lets validate_financial_data turn it
    into a proper DATA_FETCH_INCOMPLETE finding instead of an unhandled
    exception taking down every project's report along with it."""
    financial_validation: Annotated[list[ValidationResult], add]

    # ---- unify_projects (Phase 5, wired into the graph in Phase 8) --------
    unified_projects: list[Project]

    # ---- retrieve_historical_memory -------------------------------------
    historical_context: dict[str, list[ProjectSnapshot]]
    """Keyed by project_id -> list of prior ProjectSnapshots from Mem0
    (Section 11). Empty list, not a missing key, when no history exists yet
    — callers must not distinguish "not fetched" from "fetched and empty"
    any other way."""
    memory_freshness: Optional[datetime]

    # ---- calculate_metrics ------------------------------------------------
    calculated_metrics: dict
    """Keyed by project_id -> dict of every Python-computed metric (Accuracy
    Check 5): sprint completion, budget consumption, spend_to_progress_ratio,
    blocker ages, etc. This is the ONLY place these numbers live — nodes and
    the final LLM narration both read from here, never recompute."""

    # ---- risk analysis (each node only appends) ---------------------------
    risks: Annotated[list[Risk], add]
    """Every finding from every stage lands here — Delivery, Financial, and
    Data Quality (mapping gaps, staleness, reconciliation failures, partial
    fetches) alike, distinguished by `Risk.category`. One shared list rather
    than one key per category, matching Section 13's evidence requirement:
    a reader assembling "why is this project RED" wants every contributing
    finding in one place, not scattered across parallel state keys."""

    # ---- validate_findings ------------------------------------------------
    validation_passed: bool
    """False only for a structural failure (currently: any
    DATA_FETCH_INCOMPLETE finding) — missing/stale/inconsistent data
    downgrades `confidence` instead of failing validation outright, per
    Accuracy Check 10 ("downgrade confidence and explain why")."""
    validation_results: Annotated[list[ValidationResult], add]
    confidence: str  # ConfidenceLevel value

    # ---- generate_response --------------------------------------------
    citations: Annotated[list[Citation], add]
    final_answer: str
    """Phase 8 populates this with a deterministic, template-based summary;
    Phase 9 narrates the executive summary via an optional LLM call over the
    same `calculated_metrics`/`risks`/`historical_context` inputs — the
    inputs and the "explain, never calculate" boundary don't change, only
    what writes that one paragraph's prose."""
    current_snapshots: dict[str, ProjectSnapshot]
    """This run's own would-be-persisted `ProjectSnapshot` per project,
    built by generate_response (which needs it to compare against
    `historical_context`'s PRIOR snapshots before this run's own facts are
    written anywhere). Exposed as a first-class key — not just an internal
    rendering detail — so persist_snapshot doesn't reconstruct it a second
    time, and so any other consumer (the Streamlit Trends page, a future
    node) needing "this week's facts in snapshot form" doesn't have to
    either."""

    # ---- persist_snapshot -----------------------------------------------
    snapshot_persisted: bool
    snapshots_written: list[str]
    """Memory ids returned by MemoryStore.add_snapshot, one per project —
    kept for audit (Section 2), not otherwise consumed."""
