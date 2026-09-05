"""Risk & Blockers (Section 16) — every Risk finding with evidence."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from app.backend import apply_project_filters, blocked_issues_dataframe, inject_theme_css, projects_dataframe, render_sidebar_filters, risks_dataframe, run_portfolio_query

st.set_page_config(page_title="Risk & Blockers", page_icon="⚠️", layout="wide")
inject_theme_css()
st.title("Risk & Blockers")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered_projects = apply_project_filters(projects_df, **filters)
project_ids = filtered_projects["project_id"].tolist()
name_by_id = dict(zip(projects_df["project_id"], projects_df["Project"]))

risks_df = risks_dataframe(result["risks"])
risks_df = risks_df[risks_df["project_id"].isin(project_ids)] if project_ids else risks_df

severity_filter = st.multiselect("Severity", ["HIGH", "MEDIUM", "LOW"], default=["HIGH", "MEDIUM"])
category_filter = st.multiselect("Category", ["Delivery", "Financial", "Data Quality"], default=["Delivery", "Financial", "Data Quality"])
scoped = risks_df[risks_df["Severity"].isin(severity_filter) & risks_df["Category"].isin(category_filter)] if not risks_df.empty else risks_df

st.subheader(f"Risk findings ({len(scoped)})")
if scoped.empty:
    st.info("No risk findings match the current filters.")
else:
    display = scoped.copy()
    display.insert(0, "Project", display["project_id"].map(lambda pid: name_by_id.get(pid, pid)))
    st.dataframe(
        display.drop(columns=["project_id", "Evidence"]),
        width="stretch",
        hide_index=True,
    )
    st.caption("Every HIGH-severity finding carries evidence (Accuracy Check 7) — expand below.")
    for _, row in display.iterrows():
        with st.expander(f"[{row['Category']}] {row['Project']} — {row['Severity']} — {row['Description']}"):
            for line in row["Evidence"].split(" | "):
                if line:
                    st.write(f"- {line}")
            if row["Recommended Action"]:
                st.write(f"**Recommended action:** {row['Recommended Action']}")

st.subheader("Blocked issues")
blocked_df = blocked_issues_dataframe(result["jira_issues"])
blocked_df = blocked_df[blocked_df["project_id"].isin(project_ids)] if project_ids else blocked_df
if blocked_df.empty:
    st.success("No blocked issues in the current scope.")
else:
    display = blocked_df.copy()
    display.insert(0, "Project", display["project_id"].map(lambda pid: name_by_id.get(pid, pid)))
    st.dataframe(
        display.drop(columns=["project_id"]).sort_values("Age (days)", ascending=False),
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "\"Sprints Blocked\" requires >=2 stored historical snapshots (Accuracy Check 6) — "
        "'N/A (needs history)' means this is the first report for that issue, not that it's unblocked."
    )
