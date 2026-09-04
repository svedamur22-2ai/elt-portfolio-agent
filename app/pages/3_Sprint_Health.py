"""Sprint Health (Section 16) — delivery velocity per project."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import plotly.graph_objects as go
import streamlit as st

from app.backend import STATUS_COLORS, apply_project_filters, projects_dataframe, render_sidebar_filters, run_portfolio_query, sprints_dataframe

st.set_page_config(page_title="Sprint Health", page_icon="🏃", layout="wide")
st.title("Sprint Health")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered_projects = apply_project_filters(projects_df, **filters)

metrics = result["calculated_metrics"]
rows = []
for _, p in filtered_projects.iterrows():
    m = metrics.get(p["project_id"], {})
    if m.get("sprint_completion_pct") is None:
        continue
    rows.append(
        {
            "Project": p["Project"],
            "RAG": p["RAG"],
            "Latest Sprint": m.get("latest_sprint_id"),
            "Completion %": m.get("sprint_completion_pct"),
            "Blocked Issues": m.get("blocked_issue_count"),
            "Oldest Blocker (days)": m.get("oldest_blocker_age_days"),
        }
    )

if not rows:
    st.info("No sprint data available for the current filter — try clearing Project/RAG filters, or this selection has no Jira mapping.")
else:
    import pandas as pd

    sprint_df = pd.DataFrame(rows).sort_values("Completion %")

    st.subheader("Sprint completion by project")
    fig = go.Figure(
        go.Bar(
            x=sprint_df["Completion %"],
            y=sprint_df["Project"],
            orientation="h",
            marker_color=[STATUS_COLORS[r] for r in sprint_df["RAG"]],
            text=[f"{v:.0f}%" for v in sprint_df["Completion %"]],
            textposition="outside",
        )
    )
    fig.add_vline(x=60, line_dash="dot", line_color="#8a8a86", annotation_text="HIGH-risk threshold (60%)")
    fig.add_vline(x=80, line_dash="dot", line_color="#8a8a86", annotation_text="Healthy threshold (80%)")
    fig.update_layout(xaxis_title="Sprint completion %", xaxis_range=[0, 105], yaxis_title=None, height=90 + 40 * len(sprint_df), margin=dict(l=10, r=10, t=30, b=10))
    st.plotly_chart(fig, width="stretch")
    st.caption("Bar color reflects each project's overall combined RAG status, not sprint completion alone — a high completion % can still sit next to aged blockers (see Risk & Blockers).")

    st.dataframe(sprint_df, width="stretch", hide_index=True)

st.subheader("All sprints in scope")
project_ids = filtered_projects["project_id"].tolist()
sprints_df = sprints_dataframe(result["jira_sprints"])
sprints_df = sprints_df[sprints_df["project_id"].isin(project_ids)] if project_ids else sprints_df
name_by_id = dict(zip(projects_df["project_id"], projects_df["Project"]))
if not sprints_df.empty:
    sprints_df = sprints_df.copy()
    sprints_df.insert(0, "Project", sprints_df["project_id"].map(name_by_id))
    st.dataframe(sprints_df.drop(columns=["project_id"]).sort_values(["Project", "Start"]), width="stretch", hide_index=True)
else:
    st.info("No sprints found for the current filter.")
