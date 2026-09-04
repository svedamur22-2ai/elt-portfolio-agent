"""Project Portfolio (Section 16) — every project, sortable and filterable."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from app.backend import apply_project_filters, projects_dataframe, render_sidebar_filters, run_portfolio_query

st.set_page_config(page_title="Project Portfolio", page_icon="📁", layout="wide")
st.title("Project Portfolio")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered = apply_project_filters(projects_df, **filters)

st.caption(f"{len(filtered)} of {len(projects_df)} project(s) shown.")

st.dataframe(
    filtered.drop(columns=["project_id"]),
    width="stretch",
    hide_index=True,
    column_config={
        "Approved Budget": st.column_config.NumberColumn(format="$%,.2f"),
        "Actual Spend": st.column_config.NumberColumn(format="$%,.2f"),
        "Remaining Budget": st.column_config.NumberColumn(format="$%,.2f"),
        "Delivery Progress %": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.1f%%"),
        "Budget Consumption %": st.column_config.NumberColumn(format="%.1f%%"),
    },
)

st.subheader("Project detail")
selected = st.selectbox("Select a project", filtered["Project"].tolist()) if not filtered.empty else None
if selected:
    row = filtered[filtered["Project"] == selected].iloc[0]
    c1, c2, c3 = st.columns(3)
    c1.metric("RAG", row["RAG"])
    c2.metric("Delivery progress", f"{row['Delivery Progress %']:.1f}%" if row["Delivery Progress %"] == row["Delivery Progress %"] else "N/A")
    c3.metric("Budget consumed", f"{row['Budget Consumption %']:.1f}%" if row["Budget Consumption %"] == row["Budget Consumption %"] else "N/A")
    st.write(f"**PM:** {row['PM']}  |  **Business Owner:** {row['Business Owner']}  |  **Technical Owner:** {row['Technical Owner']}")
    if row["Financial Status"] != "OK":
        st.warning(f"Financial status: {row['Financial Status']} — this project has no financial mapping.")
    if row["Delivery Status"] != "OK":
        st.warning(f"Delivery status: {row['Delivery Status']} — this project has no Jira mapping.")

    project_risks = [r for r in result["risks"] if r.project_id == row["project_id"]]
    if project_risks:
        st.write("**Risk findings:**")
        for r in project_risks:
            with st.expander(f"[{r.category}] {r.severity.value} — {r.description}"):
                st.write("Evidence:")
                for line in r.evidence:
                    st.write(f"- {line}")
                if r.recommended_action:
                    st.write(f"**Recommended action:** {r.recommended_action}")
