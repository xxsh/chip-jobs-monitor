"""Score job postings against the cached candidate profile.

Reads new_jobs.json (the diff output of daily.py), calls codex once
per job with the score schema, writes scored.json.

Usage:
  python -m scorer.score --jobs new_jobs.json --out scored.json
  python -m scorer.score --jobs new_jobs.json --out scored.json --profile path.json
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
import time
from pathlib import Path
from typing import Any

from scorer.llm import LLMError, call_codex

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).parent / "schemas" / "score.schema.json"
DEFAULT_PROFILE = ROOT / "cache" / "profile_latest.json"

PROMPT_TEMPLATE = """\
You are scoring a single job posting against a candidate profile to
decide whether the candidate should apply.

Be honest and concrete. Use the candidate's actual background — match
strengths, point out real gaps. Avoid generic praise. Do not invent
skills the candidate does not have.

Scoring guidance (0-100):
- 80-100 Strong fit: candidate's primary domains map directly to the role's core requirements; minimal gaps.
- 60-79  Good fit: most requirements match; some gaps but learnable.
- 40-59  Possible stretch: partial overlap; meaningful gaps in core areas.
- 0-39   Low fit: role's core requirements fall outside candidate's experience.

Match `suitability` to the score band. `recommendation`: "Apply" for Strong fit, "Maybe" for Good fit or Possible stretch, "Skip" for Low fit.

For `matchedReasons` and `gapReasons`, cite specific things from the resume profile and the job posting (e.g., "candidate's ATE production test experience matches the silicon characterization scope" rather than "good technical match"). Each reason should be a single sentence.

`verdict` is one paragraph (2-3 sentences) the candidate reads at a glance.

<candidate_profile>
{profile}
</candidate_profile>

<job_posting>
title: {title}
job_id: {jr}
locations: {locations}
department: {department}
work_mode: {work_mode}
employment_type: {employment_type}
posted: {posted}
link: {link}

description:
{description}

responsibilities:
{responsibilities}

requirements:
{requirements}

preferred:
{preferred}
</job_posting>
"""


def _format_list(items: list[str] | None) -> str:
    if not items:
        return "(none listed)"
    return "\n".join(f"- {x}" for x in items)


def _job_payload(job: dict[str, Any], profile: dict[str, Any]) -> str:
    return PROMPT_TEMPLATE.format(
        profile=json.dumps(profile, ensure_ascii=False, indent=2),
        title=job.get("name") or job.get("title") or "(unknown)",
        jr=job.get("jr") or "(unknown)",
        locations=", ".join(job.get("locations") or []),
        department=job.get("department") or "(unknown)",
        work_mode=job.get("workLocationOption") or "(unknown)",
        employment_type=job.get("employmentType") or "(unknown)",
        posted=job.get("datePosted") or "(unknown)",
        link=job.get("link") or "(unknown)",
        description=(job.get("description") or job.get("summary") or "(no description)")[:6000],
        responsibilities=_format_list(job.get("responsibilities")),
        requirements=_format_list(job.get("requirements")),
        preferred=_format_list(job.get("preferred")),
    )


def score_job(job: dict[str, Any], profile: dict[str, Any], *, timeout: int = 120, attempts: int = 1) -> dict[str, Any]:
    """Score one job. On LLM failure returns a result with `error` set."""
    prompt = _job_payload(job, profile)
    last_err: str | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return call_codex(prompt, SCHEMA_PATH, timeout=timeout)
        except LLMError as exc:
            last_err = str(exc)
            if attempt < attempts:
                time.sleep(2)
                continue
    return {
        "score": 0,
        "suitability": "Low fit",
        "recommendation": "Skip",
        "matchedReasons": [],
        "gapReasons": [],
        "verdict": f"Scoring failed: {last_err}",
        "error": last_err,
    }


def load_profile(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    # profile_latest.json wraps the profile under {"profile": ...}; raw cache files are bare.
    if isinstance(data, dict) and "profile" in data and isinstance(data["profile"], dict):
        return data["profile"]
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score new NVIDIA jobs against cached resume profile")
    parser.add_argument("--jobs", required=True, type=Path, help="Path to new_jobs JSON (array of job objects)")
    parser.add_argument("--out", required=True, type=Path, help="Path to write scored JSON")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="Profile JSON (default: cache/profile_latest.json)")
    parser.add_argument("--limit", type=int, default=0, help="Score only the first N jobs (0=all)")
    parser.add_argument("--concurrency", type=int, default=3, help="Maximum concurrent scoring calls (capped at 3)")
    parser.add_argument("--timeout", type=int, default=120, help="Per-job codex timeout in seconds")
    parser.add_argument("--attempts", type=int, default=1, help="Per-job scoring attempts")
    args = parser.parse_args(argv)

    if not args.profile.exists():
        print(f"profile not found: {args.profile}\nRun: python -m scorer.profile --resume <path>", file=sys.stderr)
        return 2
    if not args.jobs.exists():
        print(f"jobs file not found: {args.jobs}", file=sys.stderr)
        return 2

    profile = load_profile(args.profile)
    jobs = json.loads(args.jobs.read_text(encoding="utf-8"))
    if not isinstance(jobs, list):
        print("jobs file must be a JSON array", file=sys.stderr)
        return 2
    if args.limit > 0:
        jobs = jobs[: args.limit]

    timeout = max(15, min(600, args.timeout))
    attempts = max(1, min(3, args.attempts))

    def score_one(index: int, job: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        title = job.get("name") or job.get("title") or "(unknown)"
        jr = job.get("jr") or "(unknown)"
        print(f"  [{index}/{len(jobs)}] scoring {jr} — {title}", file=sys.stderr, flush=True)
        result = score_job(job, profile, timeout=timeout, attempts=attempts)
        # Carry forward identifying fields so the daily.py report can render without re-joining.
        return index, {
            "jr": job.get("jr"),
            "id": job.get("id"),
            "title": title,
            "link": job.get("link"),
            "locations": job.get("locations"),
            "department": job.get("department"),
            "posted": job.get("datePosted") or job.get("postedTs"),
            **result,
        }

    concurrency = max(1, min(3, args.concurrency, len(jobs) or 1))
    scored_slots: list[dict[str, Any] | None] = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(score_one, i, job) for i, job in enumerate(jobs, 1)]
        for future in as_completed(futures):
            index, merged = future.result()
            scored_slots[index - 1] = merged
            jr = merged.get("jr") or "(unknown)"
            print(f"  [{index}/{len(jobs)}] done {jr}", file=sys.stderr, flush=True)

    scored = [job for job in scored_slots if job is not None]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(scored)} scored jobs -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
