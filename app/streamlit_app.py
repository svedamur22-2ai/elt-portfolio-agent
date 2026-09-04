"""Streamlit entrypoint (Section 16).

Sets page config, runs the one full-portfolio pipeline invocation every
other page reads from, and renders the shared sidebar filters (persisted in
`st.session_state` so they survive navigating to another page). Pages live
in `app/pages/` following Streamlit's multipage convention; none of them
call the pipeline a second time for portfolio-wide data — see
`app/backend.py`'s `run_portfolio_query` caching.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date

import streamlit as st

from app.backend import projects_dataframe, render_sidebar_filters, run_portfolio_query

st.set_page_config(page_title="ELT Portfolio Intelligence", page_icon="📊", layout="wide")

st.title("ELT Portfolio Intelligence Agent")
st.caption("Executive Leadership Team dashboard — Jira delivery + financial risk, unified and evidenced.")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
render_sidebar_filters(projects_df)

st.markdown(
    """
Use the sidebar to navigate:

- **Executive Overview** — headline portfolio health
- **Project Portfolio** — every project, sortable and filterable
- **Sprint Health** — delivery velocity and blockers
- **Financial Health** — budget consumption and forecast
- **Risk & Blockers** — every risk finding with evidence
- **Trends** — week-over-week movement
- **Ask the ELT Agent** — free-form questions, answered with evidence and sources
- **Data Quality / Audit** — freshness, confidence, and validation results
"""
)

col1, col2, col3 = st.columns(3)
col1.metric("Tracked projects", len(projects_df))
col2.metric("Confidence this run", result["confidence"].replace(" CONFIDENCE", ""))
col3.metric("Validation passed", "Yes" if result["validation_passed"] else "No")

if not result["validation_passed"]:
    st.error("Validation did not pass this run — see Data Quality / Audit for what failed.")
