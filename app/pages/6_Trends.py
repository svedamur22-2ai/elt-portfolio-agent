"""Trends (Section 16) — week-over-week movement (Section 14)."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from app.backend import STATUS_COLORS, apply_project_filters, projects_dataframe, render_sidebar_filters, run_portfolio_query
from src.services import trend_engine

st.set_page_config(page_title="Trends", page_icon="📈", layout="wide")
st.title("Trends")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered = apply_project_filters(projects_df, **filters)

historical_context = result.get("historical_context", {})
current_snapshots = result.get("current_snapshots", {})

rows = []
for _, p in filtered.iterrows():
    history = historical_context.get(p["project_id"], [])
    previous = history[-1] if history else None
    current = current_snapshots.get(p["project_id"])
    trend = trend_engine.classify_trend(previous, current) if current else None
    diff = trend_engine.diff_snapshots(previous, current) if previous and current else None
    rows.append(
        {
            "Project": p["Project"],
            "RAG": p["RAG"],
            "Trend": trend.value if trend else "N/A",
            "Sprint Completion Δ": diff.sprint_completion_delta if diff else None,
            "Budget Consumption Δ": diff.budget_consumption_delta if diff else None,
            "Blocked Issues Δ": diff.blocked_issue_delta if diff else None,
            "RAG Change": diff.rag_status_change if diff else None,
            "Prior Snapshots": len(history),
        }
    )

import pandas as pd

trend_df = pd.DataFrame(rows)

st.subheader("Week-over-week summary")
if trend_df.empty:
    st.info("No projects match the current filter.")
else:
    trend_icon = {"IMPROVING": "🟢 IMPROVING", "DETERIORATING": "🔴 DETERIORATING", "STABLE": "⚪ STABLE", "BASELINE": "🆕 BASELINE", "N/A": "N/A"}
    display = trend_df.copy()
    display["Trend"] = display["Trend"].map(trend_icon).fillna(display["Trend"])
    st.dataframe(display, width="stretch", hide_index=True)

baseline_count = int((trend_df["Trend"] == "BASELINE").sum()) if not trend_df.empty else 0
if baseline_count:
    st.caption(
        f"{baseline_count} project(s) show BASELINE — this is their first stored snapshot for this scope. "
        "Trend direction requires >=2 historical snapshots (Accuracy Check 6); it is never inferred from one data point."
    )

st.subheader("Historical snapshot count per project")
st.caption("More weekly reports run for a project (and persisted) means more reliable trend and 'stuck for N sprints' answers over time.")
history_counts = pd.DataFrame(
    [{"Project": p["Project"], "Stored Snapshots": len(historical_context.get(p["project_id"], []))} for _, p in filtered.iterrows()]
)
if not history_counts.empty:
    st.bar_chart(history_counts.set_index("Project"), color=STATUS_COLORS["UNKNOWN"])
