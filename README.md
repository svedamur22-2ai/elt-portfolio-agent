# ELT Portfolio Intelligence Agent

An agent for the Executive Leadership Team that joins Jira delivery data with
financial data, retains historical project state week over week, and
generates an executive weekly status report — with every number traceable
back to its source and every risk claim backed by evidence.

See [`architecture.md`](architecture.md) for the full design (diagram, data
model, LangGraph state, repo layout) and
[`docs/sample_expected_elt_report.md`](docs/sample_expected_elt_report.md)
for a worked example computed from the sample data in `data/sample/`.

## Status

**Phase 12 of 12 — complete.** Jira/financial connectors, cross-source
project unification, the delivery/financial/combined risk engine, Mem0
historical memory, the full LangGraph orchestration, ELT report generation,
the Streamlit dashboard, the consolidated accuracy audit
(`src/services/accuracy_audit.py`, Section 13's ten checks), structured
observability logging (`src/utils/logging.py`, Section 23 — every graph node
emits JSON `graph_node_start`/`graph_node_end`/`error` lines with
credential-shaped fields redacted), and production hardening against total
Jira/Financial source outages (caught and degraded to `UNKNOWN`, never
crashing the graph or fabricating data) are all implemented and tested. See
[`notebooks/README.md`](notebooks/README.md) for per-phase notebook status.

## Getting started

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # only needed for a live Jira/Mem0/LLM instead of the bundled sample data
jupyter notebook notebooks/01_environment_setup.ipynb
```

Every notebook in `notebooks/` runs standalone end-to-end against the sample
data in `data/sample/` — no external credentials required.

## Running the dashboard

```bash
streamlit run app/ELT_Intelligence_Agent.py
```

Opens the Executive Overview, Project Portfolio, Sprint Health, Financial
Health, Risk & Blockers, Trends, Ask the ELT Agent, and Data Quality / Audit
pages (Section 16), all reading from the same LangGraph pipeline as the
notebooks — see `app/backend.py`.

## Repository layout

See "Repository structure" in [`architecture.md`](architecture.md).
