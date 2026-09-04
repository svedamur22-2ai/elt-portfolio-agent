"""Executive Overview (Section 16) — headline portfolio health."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.backend import STATUS_COLORS, apply_project_filters, projects_dataframe, render_sidebar_filters, run_portfolio_query

st.set_page_config(page_title="Executive Overview", page_icon="📊", layout="wide")
st.title("Executive Overview")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered = apply_project_filters(projects_df, **filters)

rag_counts = filtered["RAG"].value_counts()

st.subheader("Portfolio at a glance")
cols = st.columns(5)
cols[0].metric("Total projects", len(filtered))
cols[1].metric("🟢 GREEN", int(rag_counts.get("GREEN", 0)))
cols[2].metric("🟡 AMBER", int(rag_counts.get("AMBER", 0)))
cols[3].metric("🔴 RED", int(rag_counts.get("RED", 0)))
cols[4].metric("⚪ UNKNOWN", int(rag_counts.get("UNKNOWN", 0)))

st.subheader("Portfolio finances")
approved = filtered["Approved Budget"].sum(skipna=True)
spend = filtered["Actual Spend"].sum(skipna=True)
remaining = filtered["Remaining Budget"].sum(skipna=True)
mapped_count = filtered["Approved Budget"].notna().sum()
cols2 = st.columns(3)
cols2[0].metric("Approved budget", f"${approved:,.0f}" if mapped_count else "N/A")
cols2[1].metric("Actual spend", f"${spend:,.0f}" if mapped_count else "N/A")
cols2[2].metric("Available", f"${remaining:,.0f}" if mapped_count else "N/A", delta_color="inverse")
if mapped_count < len(filtered):
    st.caption(f"{len(filtered) - mapped_count} project(s) excluded from these totals — no financial mapping.")

st.subheader("RAG distribution")
rag_order = ["RED", "AMBER", "GREEN", "UNKNOWN"]
counts = [int(rag_counts.get(r, 0)) for r in rag_order]
fig = go.Figure(
    go.Bar(
        x=counts,
        y=rag_order,
        orientation="h",
        marker_color=[STATUS_COLORS[r] for r in rag_order],
        text=counts,
        textposition="outside",
    )
)
fig.update_layout(
    xaxis_title="Number of projects",
    yaxis_title=None,
    height=280,
    margin=dict(l=10, r=10, t=10, b=10),
    showlegend=False,
)
st.plotly_chart(fig, width="stretch")

st.subheader("Projects requiring executive attention")
attention = filtered[filtered["RAG"].isin(["RED", "AMBER"])].sort_values("RAG")
if attention.empty:
    st.success("No projects currently RED or AMBER.")
else:
    st.dataframe(
        attention[["Project", "RAG", "PM", "Delivery Progress %", "Budget Consumption %"]],
        width="stretch",
        hide_index=True,
    )

high_risks = [r for r in result["risks"] if r.severity.value == "HIGH" and r.project_id in filtered["project_id"].tolist()]
critical_blockers = sum(1 for r in high_risks if "AGED_BLOCKER" in r.reason_codes or "BLOCKED_MULTIPLE_SPRINTS" in r.reason_codes)
st.metric("HIGH-severity findings needing attention", len(high_risks))
st.caption(f"{critical_blockers} of which involve a blocker. See Risk & Blockers for full evidence.")
