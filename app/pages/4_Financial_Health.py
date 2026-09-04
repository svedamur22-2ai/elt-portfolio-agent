"""Financial Health (Section 16) — budget consumption vs. delivery progress."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import plotly.graph_objects as go
import streamlit as st

from app.backend import CATEGORICAL_PALETTE, apply_project_filters, projects_dataframe, render_sidebar_filters, run_portfolio_query

st.set_page_config(page_title="Financial Health", page_icon="💰", layout="wide")
st.title("Financial Health")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered = apply_project_filters(projects_df, **filters)

financial = filtered[filtered["Approved Budget"].notna()].sort_values("Budget Consumption %")
unmapped = filtered[filtered["Approved Budget"].isna()]

if unmapped.shape[0]:
    st.warning(
        f"{unmapped.shape[0]} project(s) excluded below — no financial mapping: {', '.join(unmapped['Project'])}. "
        "See Data Quality / Audit."
    )

st.subheader("Portfolio budget summary")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Approved", f"${financial['Approved Budget'].sum():,.0f}")
c2.metric("Spent", f"${financial['Actual Spend'].sum():,.0f}")
c3.metric("Available", f"${financial['Remaining Budget'].sum():,.0f}")
over_budget = int((financial["Budget Consumption %"] > 100).sum())
c4.metric("Projects over 100% consumed", over_budget)

st.subheader("Budget consumption vs. delivery progress")
st.caption("Both are percentages on the same 0-100 scale — a single shared axis, not a dual-axis chart.")
fig = go.Figure()
fig.add_trace(
    go.Bar(
        name="Budget Consumption %",
        x=financial["Project"],
        y=financial["Budget Consumption %"],
        marker_color=CATEGORICAL_PALETTE[0],
    )
)
fig.add_trace(
    go.Bar(
        name="Delivery Progress %",
        x=financial["Project"],
        y=financial["Delivery Progress %"],
        marker_color=CATEGORICAL_PALETTE[1],
    )
)
fig.add_hline(y=100, line_dash="dot", line_color="#8a8a86")
fig.update_layout(barmode="group", yaxis_title="%", xaxis_title=None, legend=dict(orientation="h", y=1.1), height=420, margin=dict(l=10, r=10, t=30, b=10))
st.plotly_chart(fig, width="stretch")
st.caption("A project where the blue bar (spend) towers over the orange bar (progress) is spending faster than it's delivering — see Risk & Blockers for the exact ratio and threshold.")

st.subheader("Financial Watchlist")
watchlist = financial[["Project", "RAG", "Approved Budget", "Actual Spend", "Remaining Budget", "Budget Consumption %", "Delivery Progress %"]]
st.dataframe(
    watchlist,
    width="stretch",
    hide_index=True,
    column_config={
        "Approved Budget": st.column_config.NumberColumn(format="$%,.2f"),
        "Actual Spend": st.column_config.NumberColumn(format="$%,.2f"),
        "Remaining Budget": st.column_config.NumberColumn(format="$%,.2f"),
        "Budget Consumption %": st.column_config.NumberColumn(format="%.1f%%"),
        "Delivery Progress %": st.column_config.NumberColumn(format="%.1f%%"),
    },
)

financial_risks = [r for r in result["risks"] if r.category == "Financial" and r.project_id in filtered["project_id"].tolist()]
if financial_risks:
    st.subheader("Financial risk findings")
    for r in sorted(financial_risks, key=lambda r: r.severity.value):
        name = projects_df.loc[projects_df["project_id"] == r.project_id, "Project"].iloc[0]
        with st.expander(f"{name} — {r.severity.value} — {', '.join(r.reason_codes)}"):
            for line in r.evidence:
                st.write(f"- {line}")
            if r.recommended_action:
                st.write(f"**Recommended action:** {r.recommended_action}")
