"""Ask the ELT Agent (Sections 16, 17, 18) — free-form questions, answered
with evidence, trend, confidence, and sources."""

import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

from app.backend import ask_question

st.set_page_config(page_title="Ask the ELT Agent", page_icon="💬", layout="wide")
st.title("Ask the ELT Agent")
st.caption(
    "Answers are assembled entirely from already-computed facts (Section 13: the agent explains, it never "
    "calculates) — the same pipeline every other page reads from, run fresh for your question's scope."
)

as_of = st.sidebar.date_input("As of date", value=date.today(), key="as_of_date")

st.markdown("**Example questions:**")
examples = [
    "What needs my attention this week?",
    "Why is Phoenix Platform Modernization at risk?",
    "Show me ORCA's blockers",
    "What is the status of our portfolio?",
]
cols = st.columns(len(examples))
for col, example in zip(cols, examples):
    if col.button(example, width="stretch"):
        st.session_state["chat_question"] = example

question = st.text_input("Your question", key="chat_question", placeholder="e.g. Why is Titan Infra Automation RED?")

if question:
    result = ask_question(question, as_of)
    st.markdown("---")
    st.markdown(f"**Scope:** {'Single project' if result.get('project_filter') else 'Whole portfolio'}")
    st.code(result["final_answer"], language="markdown")

    if not result.get("validation_passed", True):
        st.error("Validation did not pass this run — treat this answer with extra caution.")
    else:
        confidence_icon = {"HIGH CONFIDENCE": "🟢", "MEDIUM CONFIDENCE": "🟡", "LOW CONFIDENCE": "🔴"}
        st.caption(f"{confidence_icon.get(result['confidence'], '')} {result['confidence']}")
else:
    st.info("Type a question above, or click one of the examples.")
