"""Data Quality / Audit (Section 16) — freshness, confidence, and
validation results (Sections 2, 13)."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import streamlit as st

from app.backend import apply_project_filters, inject_theme_css, projects_dataframe, render_sidebar_filters, risks_dataframe, run_portfolio_query
from src.services import accuracy_audit

st.set_page_config(page_title="Data Quality / Audit", page_icon="🔍", layout="wide")
inject_theme_css()
st.title("Data Quality / Audit")

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")
result = run_portfolio_query(as_of)
projects_df = projects_dataframe(result["unified_projects"])
filters = render_sidebar_filters(projects_df)
filtered_ids = apply_project_filters(projects_df, **filters)["project_id"].tolist()

st.subheader("Overall confidence")
confidence_icon = {"HIGH CONFIDENCE": "🟢", "MEDIUM CONFIDENCE": "🟡", "LOW CONFIDENCE": "🔴"}
c1, c2 = st.columns(2)
c1.metric("Confidence", f"{confidence_icon.get(result['confidence'], '')} {result['confidence']}")
c2.metric("Validation passed", "Yes" if result["validation_passed"] else "No")

st.subheader("Source freshness")
requested_at = result["requested_at"] if "requested_at" in result else None
jira_freshness = result.get("jira_data_freshness")
financial_freshness = result.get("financial_data_freshness")
memory_freshness = result.get("memory_freshness")


def _age(ts):
    if ts is None:
        return "NOT AVAILABLE"
    from src.utils.time import utcnow

    reference = requested_at or utcnow()
    hours = (reference - ts).total_seconds() / 3600
    return f"{hours:,.1f} hours old"


st.write(f"**Jira:** {_age(jira_freshness)}")
st.write(f"**Finance:** {_age(financial_freshness)}")
st.write(f"**Memory:** {_age(memory_freshness)}")
st.caption("config/risk_rules.yaml's freshness_thresholds_hours defines what counts as stale for each source.")

st.subheader("Validation checks this run")
validation_rows = list(result.get("delivery_validation", [])) + list(result.get("financial_validation", [])) + list(result.get("validation_results", []))
if validation_rows:
    st.dataframe(pd.DataFrame(validation_rows), width="stretch", hide_index=True)
else:
    st.info("No validation checks recorded.")

st.subheader("Data quality findings")
risks_df = risks_dataframe(result["risks"])
dq = risks_df[risks_df["Category"] == "Data Quality"]
dq = dq[dq["project_id"].isin(filtered_ids)] if filtered_ids else dq
if dq.empty:
    st.success("No data-quality findings for the current scope.")
else:
    name_by_id = dict(zip(projects_df["project_id"], projects_df["Project"]))
    display = dq.copy()
    display.insert(0, "Project", display["project_id"].map(lambda pid: name_by_id.get(pid, pid) if pid != "PORTFOLIO" else "Portfolio-wide"))
    st.dataframe(display.drop(columns=["project_id", "Evidence"]), width="stretch", hide_index=True)

st.subheader("Cross-source mapping gaps (Section 6)")
mapping_gaps = projects_df[(projects_df["Financial Status"] != "OK") | (projects_df["Delivery Status"] != "OK")]
if mapping_gaps.empty:
    st.success("Every tracked project is mapped on both sides.")
else:
    st.dataframe(mapping_gaps[["Project", "Financial Status", "Delivery Status"]], width="stretch", hide_index=True)
    st.caption("A mapping gap always caps combined RAG at AMBER (never GREEN) — see config/risk_rules.yaml's unknown_default.")

st.subheader("Consolidated accuracy audit (Section 13's ten checks)")
st.caption(
    "Each check below re-inspects this run's own result independently of the nodes that produced it — "
    "defense in depth, not a repeat of the same computation."
)
audit_report = accuracy_audit.run_accuracy_audit(result)
st.metric("Checks passed", f"{sum(c.passed for c in audit_report.checks)} / {len(audit_report.checks)}")
audit_df = pd.DataFrame(
    [{"#": c.number, "Check": c.name, "Result": "✅ PASS" if c.passed else "❌ FAIL", "Detail": c.detail} for c in audit_report.checks]
)
st.dataframe(audit_df, width="stretch", hide_index=True)
if not audit_report.all_passed:
    st.error("One or more accuracy checks failed for this run — treat the report above with extra caution.")

st.subheader("Audit trail note")
st.caption(
    "Every number above traces back through a fixed pipeline: source data -> normalized data -> "
    "calculations -> rules triggered -> memory retrieved -> final response (Section 2). "
    "Full evidence for any specific finding is on the Risk & Blockers page."
)
