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

## Running this project

### 1. Prerequisites

- Python 3.11+ (developed against 3.14)
- No external accounts needed to run against the bundled sample data —
  Jira/financial credentials and an LLM key are only required for the
  optional live-source / LLM-narration paths (step 5 below).

### 2. Clone and set up a virtual environment

```bash
git clone https://github.com/svedamur22-2ai/elt-portfolio-agent.git
cd elt-portfolio-agent
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run the test suite

```bash
pytest tests/ -q
```

This runs entirely against the sample data in `data/sample/` and mock
connectors — no `.env` file needed.

### 4. Explore via the notebooks

```bash
jupyter notebook notebooks/01_environment_setup.ipynb
```

Every notebook in `notebooks/` (01 through 15, see
[`notebooks/README.md`](notebooks/README.md) for what each one covers) runs
standalone end-to-end against `data/sample/` — no external credentials
required. To execute one non-interactively (as CI would):

```bash
jupyter nbconvert --to notebook --execute --output <name>.ipynb notebooks/<name>.ipynb
```

### 5. (Optional) Configure live sources / LLM narration

```bash
cp .env.example .env
```

Fill in `.env` only for the pieces you want live instead of mocked:
`JIRA_URL`/`JIRA_EMAIL`/`JIRA_API_TOKEN` for a real Jira instance,
`FINANCIAL_SOURCE_TYPE` to swap off the bundled CSV, and `OPENAI_API_KEY`
for the optional LLM-narrated executive summary (also required by Mem0's
local `Memory()` class for fact-extraction/embeddings — see the comments in
`.env.example`). Everything runs correctly with none of this set; unmapped
or unavailable data shows up as `UNKNOWN`/`NOT AVAILABLE`, never fabricated.

### 6. Run the Streamlit dashboard

```bash
streamlit run app/ELT_Intelligence_Agent.py
```

Opens on `http://localhost:8501` by default (pass `--server.port <port>` if
that's taken). The sidebar navigates between Executive Overview, Project
Portfolio, Sprint Health, Financial Health, Risk & Blockers, Trends, Ask the
ELT Agent, and Data Quality / Audit — all reading from the same LangGraph
pipeline as the notebooks (see `app/backend.py`). Stop it with `Ctrl+C`, or
`pkill -f "streamlit run app/ELT_Intelligence_Agent.py"` if it's running in
the background.

## Repository layout

See "Repository structure" in [`architecture.md`](architecture.md).
