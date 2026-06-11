# Scoring Model Review Summary and Improvement Plan

Date: 2026-06-02

## Implementation Status (updated 2026-06-02)

Steps 1, 2, 3, 4, and 6 are implemented on branch `resume-scoring-unification` (working tree, not yet committed). Step 5 is deferred.

| Step | Status | Notes |
|---|---|---|
| 1. Normalize JD sections | DONE | Real root cause was a regex gap in the existing `fetch.py` parser — headers like `## What you'll be doing:` weren't matched, collapsing the JD into `summary`. Fixed there (not a new extractor in `score.py`). Also changed `(none listed)` → `(see description)`. 325 jobs across 33 local goldens re-derived. |
| 2. Requirement-first reasoning | DONE | Prompt classifies archetype → builds coverageMatrix → lists criticalGaps → then scores. Live-validated below. |
| 3. Score caps | DONE (scoped) | Kept the two general rules (≥2 missing → <80; Strong needs direct core evidence). Dropped the per-archetype magic numbers as over-fit. Added a deterministic backstop in `score_job` enforcing the ≥2-missing rule from the model's own coverageMatrix. |
| 4. Extend schema | DONE | Added `roleArchetype`, `coverageMatrix`, `criticalGaps`, `confidence`, `scoreRationale` (all required; stored in `raw_score`; no DB migration). |
| 5. Multi-run aggregation | DEFERRED | Step 2 already cut variance (JR2017301 spread 6→2). Re-measure before paying 3× codex cost. `call_codex` exposes no temperature knob, so multi-run is the only variance lever. |
| 6. Regression tests | DONE | Deterministic unit tests (prompt/schema/cap) + opt-in live smoke (`RUN_SCORING_SMOKE=1`). The "JR2017301 stays Strong" target was revised — see calibration finding. |

### Calibration finding (decided: keep strict)

Live re-scores under the new scorer (current profile `7e1958ce04d4a6f9`):

| JR | Old | New | Plan re-run avg | Result |
|---|---:|---:|---:|---|
| JR2016323 | 88 | 58 | 60.0 | Demoted to Possible stretch (criterion 2 met) |
| JR2014734 | 82 | 57 | 65.7 | Demoted to Possible stretch (criterion 3 met) |
| JR2017301 | 87 | 74–76 | 85.3 | Now Good fit, not Strong |

`JR2017301` no longer scores Strong: its coverage matrix shows the candidate's Applied-AI *production ownership* (the role's core) is **adjacent, not direct** — the same honesty that demotes `JR2016323` (same team's role family). The plan's criterion 4 assumed the stable 84–87 was correct, but stability is not correctness; that score carried the same keyword-overlap optimism this review set out to fix. **Decision: keep the strict calibration** (`JR2017301` = Good fit / Maybe). Criterion 4 is restated accordingly.

### Not yet applied to the backlog

Convergence keys on `profile_hash`, which a prompt/schema change does not move, so the daily pipeline uses the new scorer only for newly-added jobs. The dashboard still shows the **old** backlog scores (`JR2016323`=88, `JR2014734`=82) until a forced full rescore (`scorer.score` over the backlog → `scripts/import-rescore-report.py`). That rescore would also overwrite the two manual corrections listed at the end of this doc.

## Context

This project scores active NVIDIA jobs against the current candidate profile and stores the current-profile score in `resume_scores`. The dashboard reads `resume_scores` as the authoritative overlay for the current resume profile.

Current profile hash:

```text
7e1958ce04d4a6f9
```

During manual review of the NVIDIA top-ranked jobs, several high scores looked unstable or overly optimistic. We re-ran selected jobs multiple times without writing to the database, then compared the new results against existing `resume_scores`.

## What Happened

Two jobs showed large score differences between the existing dashboard score and repeated re-scores:

| JR | Title | Existing DB Score | Re-run Scores | Re-run Average |
|---|---|---:|---:|---:|
| JR2016323 | Senior Platform AI Engineer - Silicon Co-Design Group | 88 | 58, 56, 66 | 60.0 |
| JR2014734 | Senior Silicon Validation Methodology Engineer | 82 | 65, 64, 68 | 65.7 |

The issue was not that the old scorer had no access to the full JD. For `JR2016323`, the snapshot had a complete `description` of 5604 characters, and `score.py` includes up to 6000 characters in the prompt. The full sections were present in `description`, including:

- `What you'll be doing`
- `What we need to see`
- `Ways to stand out from the crowd`

However, the structured fields were empty:

```text
responsibilities: []
requirements: []
preferred: []
```

This caused the prompt to contain a full `description`, but also misleading structured sections such as:

```text
requirements:
(none listed)
preferred:
(none listed)
```

The old high score for `JR2016323` also had this gap reason:

```text
Posting excerpt is incomplete, so some requirements such as exact production AI infrastructure stack, security, reliability, or cloud expectations are unknown.
```

So the old score appears to have under-weighted hard requirements that were present in the description but not parsed into structured fields.

## Root Causes

### 1. The scorer over-relies on broad keyword overlap

Old `JR2016323` score: `88 / Strong fit / Apply`.

The old reasoning heavily weighted:

- AI-driven platform
- agents and skills
- silicon engineering workflows
- staff-level roadmap/platform ownership

But the full JD has hard requirements that are not clearly supported by the profile:

- 12+ years designing and operating production-grade backend/platform infrastructure
- 5+ years direct ML infrastructure experience
- ownership of model serving or latency-sensitive backend services
- job queues and sandboxed execution such as Kubernetes Jobs, Celery, Temporal, or container runtimes
- security, reliability, observability, SLOs, graceful degradation, and sustained production operation

The re-runs correctly treated the role as a production ML/platform-infrastructure leadership job, not just an AI-agent semiconductor workflow job.

### 2. Empty structured JD fields mislead the model

For jobs where requirements are embedded in `description`, the current prompt still says `requirements: (none listed)`. This creates an inconsistent prompt:

- the complete requirements are present in prose
- the structured requirements section says none are listed

That makes the scorer vulnerable to missing important hard requirements.

### 3. Strong-fit scoring lacks explicit hard-requirement gates

The current scoring prompt gives general bands:

- 80-100: Strong fit
- 60-79: Good fit
- 40-59: Possible stretch
- 0-39: Low fit

But it does not enforce gates such as:

- if two or more core hard requirements are missing, cap score below 80
- if a platform-infra role lacks production ops / model serving / SLO evidence, cap score below 70
- if a silicon validation role lacks GPU/SOC architecture or pre-silicon/emulation/FPGA evidence, cap score below 72

### 4. The model does not first classify the role archetype

For `JR2014734`, the old score treated the job as a strong silicon validation + AI workflow match.

The re-runs treated it more accurately as a GPU/SOC system validation methodology role requiring:

- deep GPU/SOC system-level architecture
- active and low-power feature knowledge
- boot, binning, PVT sensitivity, platform component losses
- HW/SW feature-interaction debug
- pre-silicon N-1, emulation, and FPGA validation flow bring-up

The candidate has relevant ATE, EVB correlation, yield analytics, bring-up, RMA, and AI workflow automation experience, but not direct evidence for those GPU/SOC architecture and pre-silicon validation requirements. So `Good fit` is more defensible than `Strong fit`.

### 5. Single-run LLM scores are too noisy for high-confidence ranking

Top NVIDIA jobs re-run three times:

| JR | Existing Score | Re-run Scores | Average |
|---|---:|---:|---:|
| JR2015041 | 89 | 84, 78, 78 | 80.0 |
| JR2016323 | 88 | 58, 56, 66 | 60.0 |
| JR2017301 | 87 | 86, 86, 84 | 85.3 |
| JR2017063 | 86 | 76, 76, 74 | 75.3 |
| JR2018202 | 84 | 78, 78, 85 | 80.3 |
| JR2017783 | 84 | 74, 74, 72 | 73.3 |
| JR2017785 | 82 | 74, 76, 74 | 74.7 |
| JR2014734 | 82 | 65, 64, 68 | 65.7 |
| JR2014732 | 78 | 74, 77, 76 | 75.7 |
| JR2018196 | 76 | 70, 72, 68 | 70.0 |

`JR2017301` is stable. `JR2016323` and `JR2014734` were not.

## Recommended Fix Plan

### Step 1: Normalize JD sections before scoring

> **Status: DONE** — implemented in `fetch.py` (the existing `parse_description_sections`), not `score.py`; the bug was an anchored regex that missed `##`-prefixed headers. See Implementation Status.

Update `scorer/src/scorer/score.py` to extract structured sections from `description` when `responsibilities`, `requirements`, or `preferred` are empty.

Target sections:

- `What you'll be doing`
- `What we need to see`
- `Ways to stand out from the crowd`

Goal:

- avoid showing `(none listed)` when the content exists inside `description`
- make the prompt consistently expose hard requirements

Suggested helper:

```text
extract_job_sections(job) -> {
  responsibilities: [...],
  requirements: [...],
  preferred: [...]
}
```

### Step 2: Add requirement-first scoring instructions

> **Status: DONE** — see Implementation Status.

Revise the scoring prompt so the model must:

1. classify the role archetype
2. identify the top 5-8 hard requirements
3. map each hard requirement to candidate evidence as `direct`, `adjacent`, or `missing`
4. list critical gaps
5. only then assign score

This reduces keyword-driven over-scoring.

### Step 3: Add explicit score caps

> **Status: DONE (scoped).** Rule 1 (≥2 missing → <80) and the "direct evidence for core work" rule are in the prompt; the two **per-archetype** caps below were **dropped** as over-fit to two jobs. A deterministic backstop in `score_job` (`_enforce_score_caps`) enforces the ≥2-missing rule from the model's own coverageMatrix so it can't be ignored.

Add prompt rules such as:

```text
If two or more core hard requirements are missing, the score must be below 80.
For production ML/platform infrastructure roles, if model serving, production operations, SLO/reliability, or sandboxed execution evidence is missing, the score should normally be below 70.
For GPU/SOC validation methodology roles, if GPU/SOC architecture and pre-silicon/emulation/FPGA evidence is missing, the score should normally be below 72.
Strong fit requires direct evidence for the role's core work, not just adjacent domain overlap.
```

### Step 4: Extend the score schema

> **Status: DONE** — see Implementation Status. All new fields are required in the schema (codex strict mode) and land in `raw_score` with no DB migration.

Update `scorer/src/scorer/schemas/score.schema.json` to include diagnostic fields:

- `roleArchetype`
- `hardRequirements`
- `coverageMatrix`
- `criticalGaps`
- `confidence`
- `scoreRationale`

This makes dashboard/debugging easier and helps review future scoring mistakes.

### Step 5: Add multi-run aggregation for high scores

> **Status: DEFERRED** — Step 2 already reduced variance (JR2017301 spread 6→2). Revisit only if a fresh spread measurement justifies the 3× codex cost. Note: there is no temperature knob in `call_codex`, so multi-run is the only available variance lever.

For jobs initially scored `>= 80`, run the scorer three times and store an aggregate:

- median score, or trimmed mean
- min/max score
- score spread
- confidence flag

Suggested rule:

```text
If score spread > 12, mark low confidence and do not automatically trust the single-run high score.
```

This would have caught:

- `JR2016323`: old 88, re-runs 58/56/66
- `JR2014734`: old 82, re-runs 65/64/68

### Step 6: Add regression tests with known problem jobs

> **Status: DONE** — `score_test.py` adds deterministic tests (prompt anchors, schema shape, the cap backstop) plus an opt-in live smoke (`RUN_SCORING_SMOKE=1`, fixtures in `fixtures/scoring_smoke_jobs.json`). The "`JR2017301` should remain Strong fit" target was **revised** to "remains a solid Good fit (≥60)" — see the calibration finding.

Add tests or fixtures for:

- `JR2016323`: should not score as Strong fit unless production ML infra / model serving / SLO evidence is explicitly present
- `JR2014734`: should remain Good fit unless GPU/SOC architecture and pre-silicon/emulation/FPGA evidence is present
- `JR2017301`: should remain Strong fit because repeated scores were stable at 84-86

## Files To Review

Primary files:

- `scorer/src/scorer/score.py`
- `scorer/src/scorer/schemas/score.schema.json`

Useful supporting files:

- `db.py`
- `scripts/import-rescore-report.py`
- `scripts/dashboard-server.mjs`
- `web/dashboard.js`

## Current Manual Database Corrections Already Made

These were manually updated in `resume_scores`, not `scores`:

| JR | New Score | Fit | Recommendation |
|---|---:|---|---|
| JR2018202 | 84 | Strong fit | Apply |
| JR2014732 | 78 | Good fit | Maybe |

No database update was made for `JR2016323` or `JR2014734` yet. Under the new scorer they re-score to 58 and 57 respectively, but a forced backlog rescore is still required to surface that in the dashboard (see "Not yet applied to the backlog") — and that rescore would overwrite the two manual corrections above.

## Acceptance Criteria

Status as of 2026-06-02 (verified by live re-score; **not yet reflected in the dashboard** — see "Not yet applied to the backlog"):

1. **[DONE]** Jobs with embedded requirements no longer show `(none listed)` — the prompt emits `(see description)` and the fixed parser fills the structured lists.
2. **[DONE]** `JR2016323` scores 58 (Possible stretch), not Strong.
3. **[DONE]** `JR2014734` scores 57 (Possible stretch), not Strong.
4. **[REVISED]** `JR2017301` is now a Good fit (74–76), not Strong — by design (its core Applied-AI production ownership is adjacent, not direct). Restated as "remains a solid Good fit (≥60), not demoted to stretch/low," which holds and is asserted by the smoke test.
5. **[PARTIAL]** A per-score `confidence` signal is emitted and the ≥2-missing cap is enforced deterministically; the empirical multi-run stability check (Step 5) is deferred.

## Suggested Review Questions For Claude Code

1. Is section extraction best implemented in `score.py`, or should it live in the NVIDIA fetch/parser layer?
2. Should score caps be enforced only in prompt text, or should there be deterministic post-processing?
3. How should the expanded schema be migrated without breaking existing reports and dashboard code?
4. Should multi-run aggregation be part of daily scoring, manual rescore only, or only for `>=80` candidates?
5. What is the smallest safe patch that prevents another `JR2016323`-style false Strong fit?
