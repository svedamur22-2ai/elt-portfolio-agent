# Sample Expected ELT Output (Section 15 / 25)

> **What this file is**: a hand-computed example of what `generate_response`
> (Phase 9) should produce once every intermediate phase is built, run
> against the real numbers in `data/sample/`. It exists so later phases have
> a ground truth to test against — every figure below was computed with
> plain Python over the CSVs (see the calculation notes at the bottom), not
> written first and reverse-justified.
>
> **As-of date**: this report is framed as if generated on **2026-03-22**,
> one week after the latest sprint in the sample data closes (TITAN/LYNX
> Sprint 5 ends 2026-03-15). Financial figures use the **Jan-Feb 2026
> cumulative** close, since a March close would not plausibly be final by
> March 22 — this mismatch in "how current" each source is is intentional
> and is exactly what the Data Freshness section below is for.

---

# Weekly ELT Portfolio Report

**Reporting Period:** March 16 – March 22, 2026

## Data Freshness

| Source | Last refreshed | Status |
|---|---|---|
| Jira — TITAN, LYNX | 2 days ago (2026-03-20) | Within tolerance |
| Jira — PHX, ORCA, NOVA | 30 days ago (2026-02-20) | **STALE** — no board activity since Sprint 3 closed |
| Jira — QSR | 8 days ago (2026-03-14) | Within tolerance |
| Finance — all projects | Jan-Feb 2026 close (finalized ~2026-03-05) | Within tolerance for a monthly close |
| Historical memory | No prior snapshot exists | **N/A — first report for this portfolio** |

PHX, ORCA, and NOVA data is stale enough that any per-issue claim about
those three projects (blocker ages, current sprint state) should be treated
as **as of Feb 20**, not as of this report's date. This is stated explicitly
rather than silently treated as current, per Accuracy Check 2.

## Executive Summary

6 tracked projects (5 with financial data mapped, 1 unmapped).

- **0 GREEN**
- **1 AMBER** (QSR — delivery is on the better end of the portfolio, but its
  combined risk is capped at AMBER because it has no financial mapping, not
  because delivery is bad)
- **5 RED** (PHX, ORCA, NOVA, TITAN, LYNX)

**Portfolio budget (5 mapped projects, Jan-Feb 2026):** $626,668.57 approved
**Spend to date:** $643,223.82 actual
**Available funding:** **-$714,234.82** (negative — the portfolio is
collectively over-committed against approved funding for the period; see the
Financial Watchlist below for the per-project breakdown and the data-quality
note underneath it)

**Data quality note — read before treating this as a five-alarm fire:**
Every mapped project lands RED, and every mapped project's raw
`committed_cost` figure is large enough to push `remaining_budget` negative
even where `actual_spend` alone is under budget (ORCA, TITAN). When 100% of
projects hit the top severity tier on both dimensions, that is itself a
signal to check thresholds and source data before escalating five projects
at once — which is what Accuracy Check 4 (Financial Reconciliation) is
for. This dataset also has at least one confirmed source inconsistency:
**PHX-4** carries `blocker_status = Blocked` while its Jira workflow
`status = In Review` — a genuine conflict in the source record, surfaced
here rather than silently resolved one way or the other.

**Projects requiring executive attention:** PHX, ORCA, NOVA, TITAN, LYNX (all
RED); QSR's missing finance mapping (blocks any financial oversight of that
project).

## Project-Level RAG Status

| Project | Delivery Risk | Financial Risk | Combined | Confidence |
|---|---|---|---|---|
| PHX (Phoenix Platform Modernization) | HIGH | HIGH | 🔴 RED | LOW |
| ORCA (Orca Payments Gateway) | HIGH | HIGH | 🔴 RED | LOW |
| NOVA (Nova Customer Portal) | HIGH | HIGH | 🔴 RED | LOW |
| TITAN (Titan Infra Automation) | HIGH | HIGH | 🔴 RED | MEDIUM |
| LYNX (Lynx Data Analytics) | HIGH | HIGH | 🔴 RED | MEDIUM |
| QSR (Quasar Self-Service Analytics) | MEDIUM | UNKNOWN | 🟡 AMBER | LOW |

TITAN/LYNX sit at MEDIUM rather than LOW confidence (vs. PHX/ORCA/NOVA)
because their Jira data is fresh — the only confidence-reducing factor left
for them is the financial reconciliation anomaly shared by every mapped
project.

## Sprint Delivery Status (most recent sprint per project)

| Project | Sprint | Committed SP | Completed SP | Completion % | Carryover SP |
|---|---|---|---|---|---|
| PHX | PHX-SPR-3 | 35.0 | 28.0 | 80.0% | 7.0 |
| ORCA | ORCA-SPR-3 | 36.0 | 14.0 | 38.9% | 22.0 |
| NOVA | NOVA-SPR-3 | 63.0 | 30.0 | 47.6% | 33.0 |
| TITAN | TITAN-SPR-5 | 69.0 | 20.0 | 29.0% | 49.0 |
| LYNX | LYNX-SPR-5 | 67.0 | 27.0 | 40.3% | 40.0 |
| QSR | QSR-SPR-1 | 14.0 | 9.0 | 64.3% | 5.0 |

PHX's 80% completion is the best raw number in the portfolio, but is
overridden to HIGH delivery risk by 4 aged blockers (see below) — a good
example of why the risk engine checks blockers independently of completion
%, not as a tiebreaker.

## Financial Status (Jan-Feb 2026 cumulative)

| Project | Approved | Actual | Committed | Remaining | Consumption % | Forecast Variance |
|---|---|---|---|---|---|---|
| PHX | $121,122.84 | $135,416.92 | $142,187.59 | -$156,481.67 | 111.8% | -$14,311.24 |
| ORCA | $151,047.45 | $141,706.98 | $154,206.47 | -$144,866.00 | 93.8% | +$3,351.96 |
| NOVA | $121,676.05 | $134,595.87 | $144,707.13 | -$157,626.95 | 110.6% | -$12,283.24 |
| TITAN | $151,345.85 | $145,524.95 | $161,532.33 | -$155,711.43 | 96.2% | -$1,730.06 |
| LYNX | $81,476.38 | $85,979.10 | $95,046.05 | -$99,548.77 | 105.5% | -$8,196.68 |
| QSR | NOT MAPPED | NOT MAPPED | NOT MAPPED | NOT MAPPED | NOT MAPPED | NOT MAPPED |

ORCA has the healthiest forecast trajectory in the portfolio (the only
positive forecast variance) despite currently reading HIGH financial risk —
worth distinguishing "financially unhealthy right now" from "trending worse"
when these get escalated.

## Major Blockers

| Issue | Project | Owner | Days Blocked (as of source freshness) | Dependency |
|---|---|---|---|---|
| TITAN-3 | TITAN | Elena Petrova | 85 | TITAN-1, TITAN-2 |
| NOVA-8 | NOVA | Rahul Mehta | 80 | — |
| ORCA-2 | ORCA | Jamal Carter | 76 | — |
| NOVA-11 | NOVA | Marcus Johnson | 75 | — |
| TITAN-4 | TITAN | Alicia Chen | 67 | — |
| PHX-17 | PHX | Carlos Diaz | 63 | — |
| ORCA-11 | ORCA | Jamal Carter | 63 | — |
| NOVA-10 | NOVA | Priya Raman | 62 | — |
| ORCA-22 | ORCA | Nathan Brooks | 60 | ORCA-9 |
| PHX-4 | PHX | David Kim | 58 | — (see data-quality note above) |

**"Stuck for more than one sprint" (Section 12):** cannot be determined yet
for any issue. Accuracy Check 6 requires ≥2 stored `ProjectSnapshot` records
per issue/project; this is the first report for this portfolio, so
`sprint_count_blocked = None` (not 0, not 1) for every issue above. Once two
weekly snapshots exist, this table will add a "Sprints Blocked" column and
this section will name issues that cross the ≥2-sprint threshold.

## Week-over-Week Changes

No prior snapshot exists for this portfolio (`historical_context` returned
empty for all 6 projects). Every project is reported as **BASELINE**, not
IMPROVING/STABLE/DETERIORATING — Accuracy Check 6 forbids inferring a trend
from a single data point. This section will populate starting with next
week's report, once `persist_snapshot` (Phase 7) has written this week's
`ProjectSnapshot` for each project.

## Key Risks

1. **[HIGH] PHX — Aged blockers despite strong sprint velocity.**
   Evidence: PHX-SPR-3 completion = 80.0%; PHX-4 (58 days), PHX-15 (47 days),
   PHX-17 (63 days), PHX-26 blocked, all exceeding the 7-day threshold.
   Reason codes: `AGED_BLOCKER`.
2. **[HIGH] ORCA/NOVA/TITAN/LYNX — Sprint completion below 60%.**
   Evidence: completion 38.9% / 47.6% / 29.0% / 40.3% respectively, each with
   8-10 open blockers. Reason codes: `LOW_SPRINT_COMPLETION`,
   `AGED_BLOCKER`.
3. **[HIGH] All 5 mapped projects — spend running ahead of delivery
   progress.** Evidence: spend-to-progress ratios range 1.96x (TITAN) to
   3.09x (ORCA) against a 1.3x threshold. Reason codes:
   `SPEND_AHEAD_OF_PROGRESS`, `BUDGET_EXHAUSTION`.
4. **[MEDIUM] QSR — Unmapped to any financial system.**
   Evidence: `config/project_mapping.yaml` has `finance_project_id: null`
   for PROJECT-10006. Reason code: `DATA_MAPPING`.
5. **[MEDIUM] PHX — Source data inconsistency.**
   Evidence: PHX-4 `blocker_status = Blocked` with workflow `status = In
   Review`. Reason code: `DATA_INCONSISTENT`.

## Executive Decisions Required

**Project:** ORCA (Orca Payments Gateway)
**Decision:** Approve supplemental funding or descope, and unblock
ORCA-2/ORCA-11 (both >60 days blocked, owner Jamal Carter on both).
**Evidence:** 93.8% of approved Jan-Feb budget consumed against only 30.4%
overall delivery progress; 8 open blockers.
**Financial Impact:** at the current spend-to-progress ratio (3.09x),
completing the remaining ~70% of scoped work would require roughly 3x the
funding consumed so far — outside the current approved budget.

**Project:** QSR (Quasar Self-Service Analytics)
**Decision:** Assign a `finance_project_id` in `config/project_mapping.yaml`
so this project can receive any financial oversight at all.
**Evidence:** delivery is otherwise the most stable in the portfolio (64.3%
overall progress, zero open blockers) — the mapping gap, not project health,
is what's blocking sign-off.

## Financial Watchlist

| Project | Approved | Spend | Available | Delivery % | Budget % | Risk |
|---|---|---|---|---|---|---|
| ORCA | $151,047.45 | $141,706.98 | -$144,866.00 | 30.4% | 93.8% | HIGH |
| TITAN | $151,345.85 | $145,524.95 | -$155,711.43 | 49.1% | 96.2% | HIGH |
| LYNX | $81,476.38 | $85,979.10 | -$99,548.77 | 45.3% | 105.5% | HIGH |
| NOVA | $121,676.05 | $134,595.87 | -$157,626.95 | 36.1% | 110.6% | HIGH |
| PHX | $121,122.84 | $135,416.92 | -$156,481.67 | 37.9% | 111.8% | HIGH |

(Sorted by consumption %, ascending — closest-to-plan first.)

## Executive Actions

1. Review the ORCA and PHX blocker backlogs with their respective owners
   (Jamal Carter — ORCA-2, ORCA-11; David Kim/Carlos Diaz — PHX-4, PHX-17)
   this week; both have blockers open >60 days.
2. Decide ORCA's funding path (supplemental budget vs. descope) given its
   3.09x spend-to-progress ratio.
3. Assign QSR a `finance_project_id` so it stops reporting `NOT MAPPED`.
4. Have Finance re-verify the `committed_cost` figures behind the negative
   `remaining_budget` on all 5 mapped projects before this number is
   escalated further — see the data-quality note in the Executive Summary.
5. Investigate why PHX/ORCA/NOVA's Jira boards have had no activity since
   2026-02-20 (30 days) — this is either a reporting-hygiene problem or a
   sign those projects have effectively stalled since Sprint 3.

---

## Sources

**Jira:** PHX-4, PHX-15, PHX-17, PHX-26, ORCA-2, ORCA-11, ORCA-22,
NOVA-8, NOVA-10, NOVA-11, TITAN-3, TITAN-4, TITAN-8, LYNX-26, LYNX-34,
LYNX-37 — `data/sample/jira_mock_data.csv`, retrieved as of the last
`issue_updated_date` per project (see Data Freshness table).

**Finance:** project_id 10001-10005, financial_period 2026-01, 2026-02 —
`data/sample/financial_mock_data.csv`.

**Project Mapping:** `config/project_mapping.yaml` (PROJECT-10001 through
PROJECT-10006).

**Historical Snapshot:** none (first report for this portfolio).

---

## Calculation notes (how the numbers above were derived)

- **Sprint completion %** = sum(story_points) of Done/Closed issues in the
  project's most recent sprint ÷ sum(story_points) of all issues in that
  sprint.
- **Overall delivery progress %** = sum(story_points) of Done/Closed issues
  across *all* sprints to date ÷ sum(story_points) of all issues with story
  points recorded. This is the denominator used for
  `spend_to_progress_ratio`, not the single-sprint completion % above — a
  spend/progress comparison needs cumulative progress, not current-sprint
  velocity.
- **Blocker age (days)** = report date (2026-03-22) − `issue_updated_date`,
  for issues with `blocker_status = Blocked`. This is a proxy for "days
  since it became blocked" (the sample data has no changelog), and is
  labeled as such in `src/models/issue.py` — Phase 3's
  `get_issue_history()` will replace it with a real value.
- **Financial figures** = sums of `approved_budget` / `actual_cost` /
  `committed_cost` / `forecast_cost` across the 2026-01 and 2026-02 rows per
  `project_id`, then `src/models/finance.py`'s
  `FinancialRecord.with_calculated_fields()` formulas.
- **Combined risk** = `config/risk_rules.yaml`'s `combined_risk_matrix`,
  keyed by the delivery/financial risk pair; QSR's financial side is
  `UNKNOWN`, which resolves via `unknown_default: AMBER`.
