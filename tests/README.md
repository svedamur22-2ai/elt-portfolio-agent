# Tests (Section 21)

Test suite is built out starting Phase 6 (metrics/risk engine — the first
modules with real logic to test) and hardened in Phase 12. Phase 1 only
fixes where tests will live:

- `tests/test_models.py` — Pydantic validation (Section 4 negative-amount
  guard, sprint story-point guard, etc.)
- `tests/test_metrics.py` — sprint completion, blocker age, budget math
- `tests/test_risk_engine.py` — delivery/financial/combined risk
  classification against config/risk_rules.yaml fixtures
- `tests/test_reconciliation.py` — Accuracy Checks 3/4/6/8/9
- `tests/test_trend_engine.py` — IMPROVING/STABLE/DETERIORATING/BASELINE
- `tests/test_project_mapping.py` — missing-mapping -> UNKNOWN, never guessed
- `tests/test_memory_agent.py` — Mem0 retrieval scoping and the >=2-snapshot
  rule for "stuck for more than one sprint" claims

Scenario coverage tracks Section 21's five worked examples exactly (blocked
across 3 sprints, 92%/55% -> HIGH financial risk, finance source down ->
UNKNOWN + lowered confidence, missing mapping -> DATA_MAPPING risk, 78%->55%
completion -> DETERIORATING).
