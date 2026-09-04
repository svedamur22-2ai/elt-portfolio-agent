"""ELT report generation (Sections 15, 17, 18) — Phase 9.

Two layers, deliberately separated:

1. **Deterministic rendering** (`render_weekly_report`, `render_chat_answer`)
   — assembles every number and fact from already-computed state
   (`calculated_metrics`, `risks`, `unified_projects`, `historical_context`)
   into Section 15's/17's exact format. Fully tested, no LLM required —
   this is what the graph uses whenever no chat model is configured, which
   is the normal case in this environment (no `OPENAI_API_KEY` is set
   here).

2. **LLM narration** (`narrate_executive_summary`) — an OPTIONAL polish
   pass that turns the deterministic report's raw facts into an
   executive-readable paragraph, via any LangChain `BaseChatModel`. The
   prompt hands the model ONLY already-computed facts, as text, and
   instructs it never to state a number that wasn't given — Section 13's
   "explain, never calculate" boundary enforced by what the prompt
   contains, not merely by instruction. `build_default_chat_model()`
   constructs a real `ChatOpenAI` (needs `OPENAI_API_KEY`) — untested in
   this environment, same status as `JiraCloudRESTSource` and
   `Mem0MemoryStore`. The narration function itself IS tested, against
   LangChain's own `FakeListChatModel` test double, which proves the
   prompt-construction and response-parsing logic is correct independent
   of which real model eventually runs it.
"""

from __future__ import annotations

from collections import Counter
from typing import Optional

from src.graph.state import Citation
from src.models.common import TrendDirection
from src.models.issue import JiraIssue
from src.models.project import Project
from src.models.risk import Risk
from src.models.snapshot import ProjectSnapshot
from src.services import trend_engine

# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def _money(value: Optional[float]) -> str:
    return f"${value:,.2f}" if value is not None else "NOT AVAILABLE"


def _pct(value: Optional[float]) -> str:
    return f"{value:.1f}%" if value is not None else "N/A"


def _sum_or_none(values) -> Optional[float]:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _severity_label(risk: Optional[Risk]) -> str:
    return risk.severity.value if risk is not None else "N/A"


# --------------------------------------------------------------------------
# Portfolio aggregates (Section 15's Executive Summary numbers)
# --------------------------------------------------------------------------


def compute_portfolio_totals(projects: list[Project]) -> dict:
    return {
        "total_projects": len(projects),
        "rag_counts": Counter(p.risk_status.value for p in projects),
        "approved_budget": _sum_or_none(p.approved_budget for p in projects),
        "actual_spend": _sum_or_none(p.actual_spend for p in projects),
        "remaining_budget": _sum_or_none(p.remaining_budget for p in projects),
    }


# --------------------------------------------------------------------------
# Section 15 — full weekly report
# --------------------------------------------------------------------------


def render_weekly_report(
    projects: list[Project],
    risks: list[Risk],
    jira_issues: list[JiraIssue],
    historical_context: dict[str, list[ProjectSnapshot]],
    current_snapshots: dict[str, ProjectSnapshot],
    confidence: str,
    jira_freshness_hours: Optional[float] = None,
    financial_freshness_hours: Optional[float] = None,
    executive_summary_override: Optional[str] = None,
) -> str:
    """`current_snapshots` (keyed by project_id) is this run's own
    would-be-persisted snapshot — built the same way `persist_snapshot`
    builds it, but generate_response runs before persistence, so it can't
    read its own snapshot back out of `historical_context` (which only ever
    holds PRIOR runs). Passing it in explicitly is what lets week-over-week
    comparison work in the same run that produces the numbers being
    compared. `executive_summary_override` is where `narrate_executive_summary`'s
    LLM output goes when a chat model is configured; the rest of the report
    is identical either way."""
    projects_by_id = {p.project_id: p for p in projects}
    risks_by_project: dict[str, list[Risk]] = {}
    for r in risks:
        risks_by_project.setdefault(r.project_id, []).append(r)

    totals = compute_portfolio_totals(projects)
    lines: list[str] = []

    lines.append("# Weekly ELT Portfolio Report")
    lines.append("")
    lines.append("## Data Freshness")
    lines.append(f"Jira: {jira_freshness_hours:.1f} hours old" if jira_freshness_hours is not None else "Jira: NOT AVAILABLE")
    lines.append(f"Finance: {financial_freshness_hours:.1f} hours old" if financial_freshness_hours is not None else "Finance: NOT AVAILABLE")
    lines.append("")

    lines.append("## Executive Summary")
    if executive_summary_override:
        lines.append(executive_summary_override.strip())
    else:
        rag = totals["rag_counts"]
        lines.append(
            f"{totals['total_projects']} tracked project(s) — "
            f"{rag.get('GREEN', 0)} GREEN, {rag.get('AMBER', 0)} AMBER, {rag.get('RED', 0)} RED, "
            f"{rag.get('UNKNOWN', 0)} UNKNOWN."
        )
        lines.append(
            f"Portfolio budget: {_money(totals['approved_budget'])}  |  "
            f"Spend: {_money(totals['actual_spend'])}  |  "
            f"Available: {_money(totals['remaining_budget'])}"
        )
        attention = sorted((p for p in projects if p.risk_status.value in ("RED", "AMBER")), key=lambda p: p.project_id)
        lines.append(
            "Projects requiring executive attention: " + (", ".join(p.project_name for p in attention) if attention else "none")
        )
    lines.append(f"Confidence: {confidence}")
    lines.append("")

    lines.append("## Project-Level RAG Status")
    lines.append("| Project | Delivery | Financial | Combined |")
    lines.append("|---|---|---|---|")
    for project in sorted(projects, key=lambda p: p.project_id):
        project_risks = {r.category: r for r in risks_by_project.get(project.project_id, [])}
        lines.append(
            f"| {project.project_name} | {_severity_label(project_risks.get('Delivery'))} | "
            f"{_severity_label(project_risks.get('Financial'))} | {project.risk_status.value} |"
        )
    lines.append("")

    lines.append("## Top Risks")
    lines.append("| Project | Delivery | Financial | Overall | Reason |")
    lines.append("|---|---|---|---|---|")
    for project in sorted(projects, key=lambda p: p.project_id):
        if project.risk_status.value not in ("RED", "AMBER"):
            continue
        project_risks = {r.category: r for r in risks_by_project.get(project.project_id, [])}
        reason_codes = sorted({code for r in risks_by_project.get(project.project_id, []) for code in r.reason_codes})
        lines.append(
            f"| {project.project_name} | {_severity_label(project_risks.get('Delivery'))} | "
            f"{_severity_label(project_risks.get('Financial'))} | {project.risk_status.value} | "
            f"{', '.join(reason_codes) or '—'} |"
        )
    lines.append("")

    lines.append("## Projects Requiring Decisions")
    # Grouped by project — a project's Delivery and Financial risks can
    # each independently be HIGH severity, but that's one decision to make
    # about that project, not two separate entries under the same name.
    high_risks_by_project: dict[str, list[Risk]] = {}
    for r in risks:
        if r.severity.value == "HIGH" and r.recommended_action and r.project_id in projects_by_id:
            high_risks_by_project.setdefault(r.project_id, []).append(r)

    if not high_risks_by_project:
        lines.append("None this period.")
    for project_id in sorted(high_risks_by_project, key=lambda pid: projects_by_id[pid].project_id):
        project = projects_by_id[project_id]
        project_high_risks = high_risks_by_project[project_id]
        combined_actions = sorted({r.recommended_action for r in project_high_risks})
        combined_evidence = [line for r in project_high_risks for line in r.evidence]
        lines.append(f"**{project.project_name}**")
        lines.append(f"Decision: {' '.join(combined_actions)}")
        lines.append(f"Evidence: {'; '.join(combined_evidence)}")
        lines.append("")

    lines.append("## Week-over-Week Changes")
    improved, deteriorated, stable, baseline = [], [], [], []
    for project in sorted(projects, key=lambda p: p.project_id):
        history = historical_context.get(project.project_id, [])
        previous = history[-1] if history else None
        current = current_snapshots.get(project.project_id)
        if current is None:
            continue
        trend = trend_engine.classify_trend(previous, current)
        bucket = {
            TrendDirection.IMPROVING: improved,
            TrendDirection.DETERIORATING: deteriorated,
            TrendDirection.STABLE: stable,
            TrendDirection.BASELINE: baseline,
        }[trend]
        bucket.append(project.project_name)
    lines.append(f"Improved: {', '.join(improved) or 'none'}")
    lines.append(f"Deteriorated: {', '.join(deteriorated) or 'none'}")
    lines.append(f"No material change: {', '.join(stable) or 'none'}")
    lines.append(f"No prior data (baseline): {', '.join(baseline) or 'none'}")
    lines.append("")

    lines.append("## Persistent Blockers")
    lines.append("| Issue | Project | Owner | Days Blocked | Sprints Blocked |")
    lines.append("|---|---|---|---|---|")
    issues_by_key = {i.issue_key: i for i in jira_issues}
    for project in sorted(projects, key=lambda p: p.project_id):
        history = historical_context.get(project.project_id, [])
        current = current_snapshots.get(project.project_id)
        all_snapshots = history + ([current] if current else [])
        for issue_key in trend_engine.find_persistent_blockers(all_snapshots):
            issue = issues_by_key.get(issue_key)
            sprints_blocked = trend_engine.compute_sprint_count_blocked(issue_key, all_snapshots)
            lines.append(
                f"| {issue_key} | {project.project_name} | {issue.assignee if issue else 'NOT AVAILABLE'} | "
                f"{issue.blocker_age_days if issue and issue.blocker_age_days is not None else 'N/A'} | {sprints_blocked} |"
            )
    lines.append("")

    lines.append("## Financial Watchlist")
    lines.append("| Project | Approved | Spend | Available | Delivery % | Budget % | Risk |")
    lines.append("|---|---|---|---|---|---|---|")
    financial_projects = sorted(
        (p for p in projects if p.approved_budget is not None), key=lambda p: (p.budget_consumption_pct or 0)
    )
    for project in financial_projects:
        lines.append(
            f"| {project.project_name} | {_money(project.approved_budget)} | {_money(project.actual_spend)} | "
            f"{_money(project.remaining_budget)} | {_pct(project.delivery_progress_pct)} | "
            f"{_pct(project.budget_consumption_pct)} | {project.risk_status.value} |"
        )
    lines.append("")

    lines.append("## Executive Actions")
    # One combined line per project (reusing the grouping above), RED
    # projects first — "top 5 items requiring executive attention" (Section
    # 25's business objectives) means 5 distinct projects/decisions, not 5
    # risk-category fragments of the same 1-2 projects.
    rag_priority = {"RED": 0, "AMBER": 1, "GREEN": 2, "UNKNOWN": 1}
    ordered_project_ids = sorted(
        high_risks_by_project, key=lambda pid: (rag_priority.get(projects_by_id[pid].risk_status.value, 1), pid)
    )
    numbered = 0
    for project_id in ordered_project_ids:
        project = projects_by_id[project_id]
        combined_actions = sorted({r.recommended_action for r in high_risks_by_project[project_id]})
        numbered += 1
        lines.append(f"{numbered}. [{project.project_name}] {' '.join(combined_actions)}")
        if numbered >= 5:
            break
    if numbered == 0:
        lines.append("None this period.")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Section 17 — chat answer format
# --------------------------------------------------------------------------


def render_chat_answer(
    question: str,
    project: Project,
    project_risks: list[Risk],
    trend: TrendDirection,
    confidence: str,
    citations: list[Citation],
) -> str:
    delivery_risk = next((r for r in project_risks if r.category == "Delivery"), None)
    financial_risk = next((r for r in project_risks if r.category == "Financial"), None)

    lines = [f"Question: {question}", ""]
    lines.append(f"Answer: {project.project_name} is {project.risk_status.value}.")
    lines.append("")
    lines.append("Evidence:")
    for r in project_risks:
        for line in r.evidence:
            lines.append(f"  - {line}")
    if not project_risks:
        lines.append("  (no risk findings on record for this project)")
    lines.append("")
    lines.append(f"Trend: {trend.value}")
    lines.append("")
    lines.append(
        "Financial Impact: "
        + (financial_risk.description if financial_risk else "No financial risk findings on record.")
    )
    lines.append("")
    actions = sorted({r.recommended_action for r in project_risks if r.recommended_action})
    lines.append("Recommendation: " + ("; ".join(actions) if actions else "None"))
    lines.append("")
    lines.append(f"Confidence: {confidence}")
    lines.append("")
    lines.append("Sources:")
    for c in citations:
        lines.append(f"  - {c['source_system']}: {c['source_record_id']} (retrieved {c['retrieved_timestamp']})")
    if not citations:
        lines.append("  (no sources available)")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# LLM narration (optional) — Section 13's "explain, never calculate"
# boundary enforced by prompt content, not just instruction.
# --------------------------------------------------------------------------

NARRATION_SYSTEM_PROMPT = (
    "You are writing the executive summary paragraph of a weekly portfolio "
    "status report for an Executive Leadership Team. You will be given "
    "already-computed facts (project counts, RAG statuses, budget figures, "
    "risk reason codes) as plain text below. Write 2-4 sentences explaining "
    "what these facts mean for the business.\n\n"
    "Rules, strictly enforced:\n"
    "- Never state a number that does not appear in the facts given to you.\n"
    "- Never invent a project name, risk, date, or owner not listed below.\n"
    "- If the facts are insufficient to answer something, say so rather than guessing.\n"
    "- Do not recompute or 'correct' any number — restate it exactly as given."
)


def build_narration_facts_text(projects: list[Project], risks: list[Risk]) -> str:
    """The ONLY input `narrate_executive_summary` gives the model — plain
    text built entirely from already-computed values. If a fact isn't in
    this string, the model has no way to know it, which is the actual
    enforcement mechanism behind the system prompt's rules above."""
    totals = compute_portfolio_totals(projects)
    rag = totals["rag_counts"]
    lines = [
        f"total_projects={totals['total_projects']}",
        f"GREEN={rag.get('GREEN', 0)} AMBER={rag.get('AMBER', 0)} RED={rag.get('RED', 0)} UNKNOWN={rag.get('UNKNOWN', 0)}",
        f"approved_budget={_money(totals['approved_budget'])}",
        f"actual_spend={_money(totals['actual_spend'])}",
        f"remaining_budget={_money(totals['remaining_budget'])}",
    ]
    for project in sorted(projects, key=lambda p: p.project_id):
        if project.risk_status.value not in ("RED", "AMBER"):
            continue
        reasons = sorted({code for r in risks if r.project_id == project.project_id for code in r.reason_codes})
        lines.append(f"{project.project_name}: status={project.risk_status.value} reasons={','.join(reasons) or 'none'}")
    return "\n".join(lines)


def narrate_executive_summary(chat_model, projects: list[Project], risks: list[Risk]) -> str:
    """`chat_model` is any LangChain `BaseChatModel` — a real `ChatOpenAI`
    in production, `FakeListChatModel` in tests. Returns the model's raw
    text; callers should catch exceptions and fall back to the
    deterministic summary rather than let a narration failure break the
    whole report (see src/graph/nodes.py's generate_response)."""
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([("system", NARRATION_SYSTEM_PROMPT), ("human", "{facts}")])
    chain = prompt | chat_model
    facts_text = build_narration_facts_text(projects, risks)
    response = chain.invoke({"facts": facts_text})
    return response.content


def build_default_chat_model(model: str = "gpt-4o-mini", temperature: float = 0.0):
    """Real `ChatOpenAI` construction — needs `OPENAI_API_KEY`. Not
    exercised by any test in this environment (that key isn't configured
    here); `narrate_executive_summary` itself is tested against
    `FakeListChatModel` instead, which proves the prompt/response wiring
    independent of which concrete model eventually runs it."""
    from langchain_openai import ChatOpenAI  # deferred: only needed when actually narrating

    return ChatOpenAI(model=model, temperature=temperature)
