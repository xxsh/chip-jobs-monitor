# Score-ledger refactor plan

Make MySQL the single source of truth for "what's been scored," so re-running
the daily pipeline (whether on cron or via `openclaw cron run <id>`) produces a
stable, cumulative digest instead of one that only reflects the most recent
batch.

## Problem

Running the NVIDIA jobs cron multiple times the same day returns different jobs
and different scores in the Telegram digest. Two distinct causes:

1. **Set-of-jobs changes** — `daily.py` caps scoring at 3 jobs per run
   (`MAX_SCORING_JOBS_PER_RUN`, `daily.py:54`) and dedups against
   already-scored keys via `collect_successful_score_keys`
   (`daily.py:421-449`), which reads only the dated JSON report files.
   `write_source_report` overwrites that file on every run with only this
   run's scored set, so the dedup memory is one-run-deep. Same-day re-runs
   forget earlier batches and the digest only shows the most recent 3.
2. **Score-of-same-job changes** — `scorer/src/scorer/llm.py:30-110` calls
   `codex exec` with no temperature/seed pinning, so re-scoring the same job
   sample drift (JR2018202 has been recorded at 0, 58, 83, 93, 78 across
   runs).

This plan fixes (1). Fix for (2) is a follow-up (pin sampling + cache by
`hash(profile)+hash(JD)`).

## Single entry point

Both modes go through `openclaw cron run <id>` against the NVIDIA cron
(`f7695530-cccf-48ff-bf1f-327d453d9fa2`). Cron-scheduled and manual runs
execute the same `daily.py main()`. No new CLI surface; the change is
internal layering.

## Architecture

### Current (tangled)

```
fetch → diff vs yesterday → score (cap 3) → write_report(JSON) → persist(MySQL)
                                                  │
                                                  └── grouped_report → Telegram
```

- JSON report = ledger + deliverable, conflated.
- MySQL persistence is best-effort (`daily.py:921-924` swallows errors).
- Dedup reads JSON, not MySQL.
- `rankedJobs` in the report contains only this run's scored set.

### Target (layered)

```
fetch → diff → score (read MySQL, write MySQL, cap N)
              │
              └── project (read MySQL → ReportModel) → render (files) → announce (openclaw)
```

| Layer | Reads | Writes | Pure? |
|---|---|---|---|
| L1 ingest | careers site | snapshot file | no |
| L2 diff | snapshots | — | yes |
| L3 score | snapshot, MySQL | MySQL | no |
| L4 project | snapshot, MySQL | — | yes |
| L5 render | ReportModel | JSON/MD/Telegram files | yes |
| L6 announce | rendered files | Telegram | handled by openclaw cron's `delivery:` |

Properties:
- L3 is idempotent on `(job_key, profile_hash)`: same key, already scored → skip.
- L4 returns every active job's latest score from MySQL, not just this run's.
- L5 output becomes a stable function of MySQL state.
- Re-running mid-day produces a digest that is a strict superset of the prior
  run's digest (new scores added, existing ones unchanged).

## Phases

### Phase 0 — Verify the schema (read-only)

Open `db.py` / `db.mjs`. Confirm L4 can rebuild the report from MySQL alone:

- `scores` table has: `job_key`, `profile_hash`, `score`, `suitability`,
  `recommendation`, `matched_reasons`, `gap_reasons`, `verdict`,
  `first_seen_date`, `scored_at`.
- `job_snapshots` (or equivalent) has per-day active-job rows: `source`,
  `date`, `job_key`, `title`, `link`, `locations`, `posted`.
- A query exists for: *for source S, date D, profile P, return every active
  job LEFT JOIN its latest score under P*.

Gate: if anything is missing, add a small `ALTER TABLE` / view before Phase 1.

### Phase 1 — L3 reads MySQL for dedup

Files: `daily.py`, `db.py`.

- Add `db.scored_keys(profile_hash, source) -> set[str]`.
- In `daily.py:884-886`, replace `collect_successful_score_keys(load_dated_reports(...), profile_hash)` with `db.scored_keys(profile_hash, source)`.
- Delete `collect_successful_score_keys` (`daily.py:421-449`) and
  `load_dated_reports` (`daily.py:226-247`) once no callers remain.
- Promote MySQL persistence to required: remove the `except Exception` /
  `NVIDIA_DB_REQUIRED` swallow at `daily.py:921-924`. L3 must raise on DB
  write failure.

### Phase 2 — L4 projects from MySQL

Files: `daily.py`, `db.py`, new `projection.py` (optional split).

- Add `db.scores_for_active_jobs(source, date, profile_hash) -> list[ScoredJob]`.
  Returns active jobs in today's snapshot joined to their latest score under
  the current profile, regardless of which day/run wrote the score.
- Rewrite `build_report` (`daily.py:654-756`) so `rankedJobs` comes from that
  query instead of `scored_by_model + skipped_intern_jobs`.
- L3's per-run scoring side-effect stays (it grows the ledger); L4 is the
  *only* place `rankedJobs` is assembled.
- `canceledJobs` and the diff still come from snapshots — they don't need
  scores.

Acceptance:
- Same MySQL state + same snapshot → identical `rankedJobs` ordering and
  content across runs.

### Phase 3 — Renderers unchanged

`render_markdown`, `render_telegram_digest`, `render_grouped_markdown`,
`render_grouped_telegram`: no API change. They already take a report dict
and are pure. They just receive a fuller `rankedJobs` list.

`write_source_report` and `write_grouped` keep overwriting the same paths
on every run. Safe now: the file content is a function of MySQL state.

### Phase 4 — Tests

File: `daily_test.py`.

- Replace `build_report` fixtures with an in-memory DB shim (sqlite-backed
  is fine for tests).
- New test: two consecutive `daily.main()` calls. Second call scores no new
  jobs (DB sees them all). Both calls produce byte-identical
  `latest_*_telegram.md`.
- New test: three consecutive calls with `cap=3` against a pool of 9
  unscored jobs. Final digest contains all 9, with the score for each job
  appearing exactly once.
- New test: DB write failure in L3 → `daily.main()` raises, no partial
  artifacts left behind that downstream readers would mistake for valid.

### Phase 5 (optional) — Skip duplicate Telegram on no-change runs

Only if duplicate Telegram digests on manual re-runs become annoying.

- After L5, hash the rendered Telegram body.
- Store last-delivered hash in MySQL (`delivery_log` row keyed by cron+date).
- If new hash == last hash → write a sentinel that openclaw's announce can
  see and skip, or have the script emit empty stdout.

Skip this phase unless the duplicate-send actually shows up as a problem.

## File-by-file change summary

| File | Change |
|---|---|
| `db.py` | Add `scored_keys()`, `scores_for_active_jobs()`. Possibly add/extend schema (Phase 0). |
| `daily.py` | L3 dedup reads MySQL; L4 projects from MySQL; remove `collect_successful_score_keys`, `load_dated_reports`; remove silent persistence-failure swallow. |
| `daily_test.py` | Fixture-DB-based tests; new convergence and no-change tests. |
| `scorer/src/scorer/score.py` | Unchanged. (LLM determinism is a separate follow-up.) |
| `scorer/src/scorer/llm.py` | Unchanged. (See follow-up below.) |
| `reports/*.json` | Format unchanged. Still useful as human-readable archive; now a projection rather than the ledger. |

## Acceptance criteria

1. Running the cron once and then running `openclaw cron run <id>` again the
   same day produces a Telegram digest whose actionable+skip lists are a
   superset of the first run's.
2. A job already scored under the current profile is never re-scored within
   the same day or across days, until the profile hash changes.
3. `daily.main()` raises if MySQL is unreachable during scoring or
   projection — no silent partial state.
4. `daily_test.py` covers re-run convergence (Phase 4 tests pass).

## Out of scope (follow-ups)

- **LLM determinism / cache.** Pin `codex` temperature where supported; add
  a `hash(profile)+hash(JD)` cache so even fresh scoring is reproducible.
  Tracked separately; this refactor fixes only the ledger-memory bug.
- **Rubric tightening.** Replace coarse score bands with a checklist scoring
  prompt. Bigger project; defer until LLM determinism is in.
- **JSONL WAL fallback for MySQL.** Only if DB outages become a real problem
  for daily runs.

## Risks

- **MySQL becomes load-bearing.** Phase 1 removes the failure-tolerance
  shim. A DB outage now fails the run loudly. Acceptable because the
  alternative (silent partial scoring + JSON ledger drift) is what got us
  here.
- **Legacy JSON-only scores.** If any scores exist in old report files but
  not in MySQL, they won't appear in the new digest. Mitigation: one-shot
  backfill script (`python -m daily backfill --from-reports`) before
  cut-over; skip if MySQL is already complete.
- **Schema gaps in Phase 0.** If `scores` is missing fields the renderers
  need (verdict, matched/gap reasons), L4 can't reproduce the current
  digest. Add columns before Phase 1; cheap if caught early.
