"""Streamlit backend (Section 16) — a thin, cached wrapper over the
LangGraph pipeline built in Phases 3-9. No business logic lives here: every
page in `app/pages/` reads from this module, so the dashboard stays a
presentation layer over the already-tested pipeline rather than a second
implementation of it.

All paths are resolved from `PROJECT_ROOT` explicitly, rather than relying
on the process's working directory — `streamlit run` can be invoked from
anywhere, unlike the notebooks (which each `os.chdir` themselves).

Colors come from the dataviz skill's validated reference palette
(status colors reserved for RAG/severity, never reused for a plain
category; categorical slots used only for genuinely-categorical series in
fixed order).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from src.connectors.financial_client import CSVFinancialDataSource
from src.connectors.jira_client import build_default_jira_client
from src.graph.nodes import NodeDeps
from src.graph.workflow import build_graph
from src.services import project_unifier
from src.services.memory_store import FileMemoryStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Status palette (dataviz skill, references/palette.md) — reserved for
# RAG/severity only, never reused as a generic categorical color.
STATUS_COLORS = {
    "GREEN": "#0ca30c",
    "LOW": "#0ca30c",
    "AMBER": "#fab219",
    "MEDIUM": "#fab219",
    "RED": "#d03b3b",
    "HIGH": "#d03b3b",
    "UNKNOWN": "#8a8a86",
    "N/A": "#8a8a86",
}
# Categorical slots 1/2 (blue/orange) — used only for genuinely categorical
# series (e.g. two distinct percentage metrics on one shared 0-100% axis),
# in this fixed order, never reassigned by filter or rank.
CATEGORICAL_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def inject_theme_css() -> None:
    """One small CSS accent layer, called once per page right after
    `st.set_page_config`. Streamlit's dark theme otherwise renders every
    heading in flat white — this gives just the page title (`st.title`,
    an h1) the brand accent color (same blue as `.streamlit/config.toml`'s
    `primaryColor`, kept as one constant here so the two can't drift apart).
    Subheaders stay plain white — no border, no color — so the one accent
    reads as "this is the page title" rather than being spread thin across
    every heading level."""
    st.markdown(
        """
        <style>
        h1 {
            color: #2a78d6 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def get_deps() -> NodeDeps:
    return NodeDeps(
        jira_client=build_default_jira_client(csv_path=str(PROJECT_ROOT / "data/sample/jira_mock_data.csv")),
        financial_source=CSVFinancialDataSource(csv_path=str(PROJECT_ROOT / "data/sample/financial_mock_data.csv")),
        memory_store=FileMemoryStore(base_dir=str(PROJECT_ROOT / "data/snapshots")),
        mapping=project_unifier.load_project_mapping(str(PROJECT_ROOT / "config/project_mapping.yaml")),
    )


@st.cache_resource
def get_graph():
    return build_graph(get_deps())


def _requested_at(as_of: date) -> datetime:
    return datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)


@st.cache_data(show_spinner="Running the ELT pipeline...")
def run_portfolio_query(as_of: date):
    """The one full-portfolio graph invocation every non-chat page reads
    from — cached by `as_of` so switching sidebar filters (which apply
    client-side over the resulting DataFrames) never re-runs the pipeline."""
    graph = get_graph()
    return graph.invoke(
        {
            "user_question": "What is the status of our portfolio?",
            "request_id": f"streamlit-portfolio-{as_of.isoformat()}",
            "requested_at": _requested_at(as_of),
        }
    )


@st.cache_data(show_spinner="Thinking...")
def ask_question(question: str, as_of: date):
    """A fresh invocation per question — `classify_request` derives
    `project_filter` from the text itself (Section 17), so this
    deliberately does NOT take the sidebar's project filter as an input."""
    graph = get_graph()
    return graph.invoke(
        {
            "user_question": question,
            "request_id": f"streamlit-chat-{as_of.isoformat()}-{abs(hash(question))}",
            "requested_at": _requested_at(as_of),
        }
    )


def projects_dataframe(projects) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "project_id": p.project_id,
                "Project": p.project_name,
                "PM": p.project_manager,
                "Business Owner": p.business_owner,
                "Technical Owner": p.technical_owner,
                "RAG": p.risk_status.value,
                "Delivery Progress %": p.delivery_progress_pct,
                "Approved Budget": p.approved_budget,
                "Actual Spend": p.actual_spend,
                "Remaining Budget": p.remaining_budget,
                "Budget Consumption %": p.budget_consumption_pct,
                "Financial Status": p.financial_status or "OK",
                "Delivery Status": p.delivery_status or "OK",
            }
            for p in projects
        ]
    )


def risks_dataframe(risks) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "project_id": r.project_id,
                "Category": r.category,
                "Severity": r.severity.value,
                "Description": r.description,
                "Reason Codes": ", ".join(r.reason_codes),
                "Recommended Action": r.recommended_action or "",
                "Evidence": " | ".join(r.evidence),
            }
            for r in risks
        ]
    )


def blocked_issues_dataframe(jira_issues) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Issue": i.issue_key,
                "project_id": i.project_id,
                "Assignee": i.assignee or "NOT AVAILABLE",
                "Status": i.status,
                "Age (days)": i.blocker_age_days,
                "Reason": i.blocker_reason,
                "Sprints Blocked": i.sprint_count_blocked if i.sprint_count_blocked is not None else "N/A (needs history)",
                "Dependencies": ", ".join(i.linked_dependencies) or "—",
            }
            for i in jira_issues
            if i.blocked
        ]
    )


def sprints_dataframe(sprints) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "project_id": s.project_id,
                "Sprint": s.sprint_name,
                "Status": s.sprint_status,
                "Start": s.start_date,
                "End": s.end_date,
                "Committed SP": s.committed_story_points,
                "Completed SP": s.completed_story_points,
                "Completion %": s.completion_pct,
                "Carryover SP": s.carryover_story_points,
            }
            for s in sprints
        ]
    )


def apply_project_filters(
    df: pd.DataFrame,
    project_ids: Optional[list[str]] = None,
    pms: Optional[list[str]] = None,
    bos: Optional[list[str]] = None,
    rag: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Applies the shared sidebar filters (Section 16) to any DataFrame
    that carries a `project_id` column — and `PM`/`Business Owner`/`RAG`
    when present. A filter list that's empty or None is a no-op, not an
    "exclude everything" — an unset filter means "don't filter on this
    dimension", matching how every sidebar multiselect defaults to empty."""
    filtered = df
    if project_ids:
        filtered = filtered[filtered["project_id"].isin(project_ids)]
    if pms and "PM" in filtered.columns:
        filtered = filtered[filtered["PM"].isin(pms)]
    if bos and "Business Owner" in filtered.columns:
        filtered = filtered[filtered["Business Owner"].isin(bos)]
    if rag and "RAG" in filtered.columns:
        filtered = filtered[filtered["RAG"].isin(rag)]
    return filtered


def render_sidebar_filters(projects_df: pd.DataFrame) -> dict:
    """Renders the shared sidebar filters and returns the current
    selections. Every widget uses a fixed `key` (no separate `default=`) so
    Streamlit's session state IS the persistence mechanism — the same
    selections survive navigating between pages."""
    st.sidebar.header("Filters")
    project_names = st.sidebar.multiselect(
        "Project", sorted(projects_df["Project"].tolist()), key="filter_projects"
    )
    pms = st.sidebar.multiselect("Project Manager", sorted(projects_df["PM"].unique().tolist()), key="filter_pms")
    bos = st.sidebar.multiselect(
        "Business Owner", sorted(projects_df["Business Owner"].unique().tolist()), key="filter_bos"
    )
    rag = st.sidebar.multiselect("RAG Status", ["GREEN", "AMBER", "RED", "UNKNOWN"], key="filter_rag")

    project_ids = (
        projects_df.loc[projects_df["Project"].isin(project_names), "project_id"].tolist() if project_names else None
    )
    return {"project_ids": project_ids, "pms": pms or None, "bos": bos or None, "rag": rag or None}


def rag_badge(rag: str) -> str:
    """A colored Markdown badge for RAG/severity values — used instead of
    plain text wherever a status appears in a table caption or metric
    label, per the status-color-with-label rule (color is never the only
    signal)."""
    color = STATUS_COLORS.get(rag, STATUS_COLORS["UNKNOWN"])
    return f":{_markdown_color_name(color)}[**{rag}**]"


def _markdown_color_name(hex_color: str) -> str:
    """Streamlit's `:color[text]` markdown syntax only accepts a fixed set
    of named colors, not arbitrary hex — map our reserved status hexes onto
    the closest named equivalent Streamlit supports."""
    return {
        STATUS_COLORS["GREEN"]: "green",
        STATUS_COLORS["AMBER"]: "orange",
        STATUS_COLORS["RED"]: "red",
        STATUS_COLORS["UNKNOWN"]: "gray",
    }.get(hex_color, "gray")
