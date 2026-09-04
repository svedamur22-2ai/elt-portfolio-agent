# Notebook Plan (Section 19)

Notebooks are created as each phase lands — not stubbed out empty in Phase 1,
since an empty `.ipynb` with no real cells to run would just be noise. Each
one, when created, will follow the fixed structure: Objective, Dependencies,
Configuration, Implementation, Sample output, Validation checks, Error
handling, Testing, Next step.

| # | Notebook | Lands in | Status |
|---|----------|----------|--------|
| 01 | `01_environment_setup.ipynb` | Phase 1 | ✅ Done |
| 02 | `02_jira_connection.ipynb` | Phase 3 | ✅ Done |
| 03 | `03_jira_exploration.ipynb` | Phase 3 | ✅ Done |
| 04 | `04_financial_source.ipynb` | Phase 4 | ✅ Done |
| 05 | `05_project_mapping.ipynb` | Phase 5 | ✅ Done |
| 06 | `06_data_normalization.ipynb` | Phase 5 | ✅ Done |
| 07 | `07_metrics_engine.ipynb` | Phase 6 | ✅ Done |
| 08 | `08_risk_engine.ipynb` | Phase 6 | ✅ Done |
| 09 | `09_mem0_memory.ipynb` | Phase 7 | ✅ Done |
| 10 | `10_langgraph_workflow.ipynb` | Phase 8 | ✅ Done |
| 11 | `11_historical_analysis.ipynb` | Phase 8 | ✅ Done |
| 12 | `12_elt_report_generation.ipynb` | Phase 9 | ✅ Done |
| 13 | `13_accuracy_validation.ipynb` | Phase 11 | ✅ Done |
| 14 | `14_end_to_end_testing.ipynb` | Phase 12 | ✅ Done |
| 15 | `15_streamlit_backend_test.ipynb` | Phase 10 | ✅ Done |

Note: notebook 15 originally landed in this table under "Phase 12" — a
labeling slip from Phase 1, when the notebook-number-to-phase mapping was
first sketched. It's obviously Phase 10 (Streamlit dashboard) content;
corrected once we actually got there.

Every "Done" notebook executes cleanly end-to-end (`jupyter nbconvert --execute`)
and its numbers are cross-checked against the others (e.g. 06 recomputes 02/03/04's
figures directly and asserts equality, not just similarity).
