"""Daily NVIDIA jobs pipeline: fetch -> diff -> score -> render -> persist.

Python port of the former daily.mjs (Phase 3 of the JS->Python migration; the
Node entrypoint was deleted 2026-05-23). Collapses the two old subprocess seams:
it imports the scorer (scorer.score) and the MySQL sidecar (db.py) in-process
instead of spawning them.

Pure functions (partition_diff, build_report, render_markdown,
render_telegram_digest, ...) are importable and covered by daily_test.py.

Known intentional difference from the original Node renderer: it sorted tied
scores / canceled jobs with String.localeCompare (ICU collation). That isn't
reproducible in stdlib Python, so ties use a casefold key here — display order
of equally-scored or canceled jobs may differ slightly; all other output is
identical.
"""

from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

__dirname = os.path.dirname(os.path.abspath(__file__))

LOCATION = os.environ.get("NVIDIA_LOCATION") or "Shanghai, China"
SNAPSHOT_LABEL = os.environ.get("NVIDIA_SNAPSHOT_LABEL") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
SKIP_FETCH = os.environ.get("NVIDIA_SKIP_FETCH") == "1"
SNAPSHOT_DIR = os.path.join(__dirname, "snapshots")
REPORT_DIR = os.path.join(__dirname, "reports")
SCORER_DIR = os.path.join(__dirname, "scorer")
PROFILE_CACHE = os.path.join(SCORER_DIR, "cache", "profile_latest.json")
SCORER_PYTHON = os.environ.get("SCORER_PYTHON") or os.path.join(SCORER_DIR, ".venv", "bin", "python")
LOCK_DIR = os.path.join(__dirname, "logs", "daily.lock")
RESUME_PATH = os.path.abspath(
    os.path.join(__dirname, os.environ.get("RESUME_PATH") or "../resume.md")
)


def bounded_int(value, fallback, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = fallback
    return max(lo, min(hi, n))


SCORER_CONCURRENCY = max(1, min(3, bounded_int(os.environ.get("NVIDIA_SCORER_CONCURRENCY"), 3, 1, 3)))
SCORER_JOB_TIMEOUT_SECONDS = bounded_int(os.environ.get("NVIDIA_SCORER_JOB_TIMEOUT_SECONDS"), 90, 15, 600)
SCORER_ATTEMPTS = bounded_int(os.environ.get("NVIDIA_SCORER_ATTEMPTS"), 1, 1, 3)
MAX_SCORING_JOBS_PER_RUN = bounded_int(os.environ.get("NVIDIA_MAX_SCORING_JOBS_PER_RUN"), 10, 0, 100)


def resolve_scorer_codex_home():
    if os.environ.get("NVIDIA_SCORER_CODEX_HOME"):
        return os.environ["NVIDIA_SCORER_CODEX_HOME"]
    user_names = [u for u in (os.environ.get("USER"), os.environ.get("LOGNAME")) if u]
    candidates = []
    if os.environ.get("HOME"):
        candidates.append(os.path.join(os.environ["HOME"], ".codex"))
    candidates.extend(os.path.join("/Users", u, ".codex") for u in user_names)
    candidates.append(os.path.join(os.path.expanduser("~"), ".codex"))
    for candidate in candidates:
        if os.path.exists(os.path.join(candidate, "auth.json")):
            return candidate
    return os.path.join(os.path.expanduser("~"), ".codex")


SCORER_CODEX_HOME = resolve_scorer_codex_home()


# ---------------------------------------------------------------- helpers
def slugify(value):
    s = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return re.sub(r"^-|-$", "", s)


def is_date_label(value):
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", value))


def ensure_dir(directory):
    os.makedirs(directory, exist_ok=True)


def _title_key(value):
    # Approximates JS String.localeCompare for tie-breaking (casefold, then raw).
    s = "" if value is None else str(value)
    return (s.casefold(), s)


def process_is_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_run_lock():
    ensure_dir(os.path.dirname(LOCK_DIR))
    for _ in range(2):
        try:
            os.mkdir(LOCK_DIR)
            with open(os.path.join(LOCK_DIR, "pid"), "w") as f:
                f.write(f"{os.getpid()}\n")
            with open(os.path.join(LOCK_DIR, "started_at"), "w") as f:
                f.write(datetime.now(timezone.utc).isoformat() + "\n")

            def release():
                try:
                    with open(os.path.join(LOCK_DIR, "pid")) as f:
                        lock_pid = f.read().strip()
                    if lock_pid == str(os.getpid()):
                        _rmtree(LOCK_DIR)
                except OSError:
                    pass

            return release
        except FileExistsError:
            pid = None
            try:
                with open(os.path.join(LOCK_DIR, "pid")) as f:
                    pid = int(f.read().strip())
            except (OSError, ValueError):
                pid = None
            if pid is not None and process_is_alive(pid):
                # The holder is alive. Normally we back off — but a scheduled run
                # can be abandoned by the gateway while daily.py keeps running
                # detached, and a wedged run can outlive run-daily.sh's watchdog.
                # If the lock is older than the stale threshold, presume the
                # holder is stuck, terminate it, and take over instead of
                # FATAL-ing this run.
                age = lock_age_seconds()
                stale_after = float(os.environ.get("DAILY_LOCK_STALE_SECONDS", "1500"))
                if age is None or age < stale_after:
                    raise RuntimeError(
                        f"NVIDIA daily monitor is already running with pid {pid}"
                        + (f" (lock age {int(age)}s)" if age is not None else "")
                    )
                import signal
                import time

                print(
                    f"daily: reclaiming stale lock from pid {pid} "
                    f"(age {int(age)}s >= {int(stale_after)}s); terminating it",
                    file=sys.stderr,
                )
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                    for _ in range(10):
                        if not process_is_alive(pid):
                            break
                        time.sleep(1)
                    if not process_is_alive(pid):
                        break
            _rmtree(LOCK_DIR)
    raise RuntimeError(f"Could not acquire NVIDIA daily monitor lock at {LOCK_DIR}")


def lock_age_seconds():
    """Seconds since the current lock was acquired, or None if unknown."""
    try:
        with open(os.path.join(LOCK_DIR, "started_at")) as f:
            started = datetime.fromisoformat(f.read().strip())
    except (OSError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - started).total_seconds()


def _rmtree(path):
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _source_infix(source):
    # NVIDIA keeps the original {label}_{slug} naming (history continuity); other
    # sources get a {label}_{source}_{slug} infix.
    return "" if source in (None, "nvidia") else f"{source}_"


def snapshot_filename(label, source=None):
    return f"{label}_{_source_infix(source)}{slugify(LOCATION)}.json"


def job_key(job):
    if job.get("id") is not None:
        return str(job["id"])
    if job.get("jr") is not None:
        return str(job["jr"])
    name = job.get("name")
    if name is None:
        name = job.get("title")
    if name is None:
        name = ""
    posted = job.get("postedTs")
    if posted is None:
        posted = job.get("datePosted")
    if posted is None:
        posted = job.get("posted")
    if posted is None:
        posted = ""
    return f"{name}|{posted}"


def _load_json(file):
    import json

    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)


def find_snapshot_by_label(label, source=None):
    file = os.path.join(SNAPSHOT_DIR, snapshot_filename(label, source))
    return file if os.path.exists(file) else None


def list_dated_snapshot_files(current_label=None, source=None):
    if current_label is None:
        current_label = SNAPSHOT_LABEL
    suffix = f"_{_source_infix(source)}{slugify(LOCATION)}.json"
    if not os.path.exists(SNAPSHOT_DIR):
        return []
    entries = []
    for file in os.listdir(SNAPSHOT_DIR):
        if not file.endswith(suffix):
            continue
        label = file[: -len(suffix)]
        if not is_date_label(label):
            continue
        if is_date_label(current_label) and label > current_label:
            continue
        entries.append({"label": label, "file": os.path.join(SNAPSHOT_DIR, file)})
    entries.sort(key=lambda e: e["label"])
    return entries


def find_previous_snapshot(current_label, source=None):
    files = [e for e in list_dated_snapshot_files(current_label, source) if e["label"] != current_label]
    return files[-1]["file"] if files else None


def load_snapshot(file):
    return _load_json(file)


def load_snapshot_history(current_label=None, source=None):
    if current_label is None:
        current_label = SNAPSHOT_LABEL
    return [
        {"label": e["label"], "file": e["file"], "jobs": load_snapshot(e["file"])}
        for e in list_dated_snapshot_files(current_label, source)
    ]


def load_profile_highlights():
    if not os.path.exists(PROFILE_CACHE):
        return []
    try:
        data = _load_json(PROFILE_CACHE)
        profile = data.get("profile") if isinstance(data, dict) and "profile" in data else data
        domains = profile.get("primaryDomains")
        return domains if isinstance(domains, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def load_profile_hash():
    """Current profile identity (resumeHash from profile_latest.json), or None.

    Used to make backlog convergence profile-aware: a job scored under a
    different resume is re-scored under the current one. Same source of truth
    db.py reads for scores.profile_hash.
    """
    if not os.path.exists(PROFILE_CACHE):
        return None
    try:
        data = _load_json(PROFILE_CACHE)
        return data.get("resumeHash") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def partition_diff(current_jobs, previous_jobs):
    previous_ids = {job_key(job) for job in previous_jobs}
    current_ids = {job_key(job) for job in current_jobs}
    new_jobs = [job for job in current_jobs if job_key(job) not in previous_ids]
    canceled = []
    for job in previous_jobs:
        if job_key(job) in current_ids:
            continue
        removed = {"jr": job.get("jr"), "title": job.get("name")}
        if job.get("link"):
            removed["link"] = job["link"]
        canceled.append(removed)
    canceled.sort(key=lambda c: _title_key(c["title"]))
    return {"newJobs": new_jobs, "canceledJobs": canceled}


def is_intern_job(job):
    text = " ".join(
        str(x)
        for x in (
            job.get("name"),
            job.get("title"),
        )
        if x
    )
    return bool(re.search(r"\b(intern|internship)\b", text, re.I)) or "实习" in text


def build_skipped_intern_score(job):
    title = job.get("name") or job.get("title") or "(unknown)"
    return {
        "jr": job.get("jr"),
        "id": job.get("id"),
        "title": title,
        "link": job.get("link"),
        "locations": job.get("locations"),
        "department": job.get("department"),
        "posted": job.get("datePosted") or job.get("postedTs"),
        "score": 0,
        "suitability": "Low fit",
        "recommendation": "Skip",
        "matchedReasons": [],
        "gapReasons": [
            "Internship roles are skipped because the experience level is too far below the target profile."
        ],
        "verdict": "Skipped automatically: internship-level role.",
        "skippedReason": "intern",
        "firstSeenDate": job.get("firstSeenDate"),
    }


def ensure_profile_cache():
    if not os.path.exists(PROFILE_CACHE):
        raise RuntimeError(
            f"Profile cache not found at {PROFILE_CACHE}.\n"
            "Run once before the first scoring:\n"
            f"  cd {SCORER_DIR}\n"
            f'  PYTHONPATH=src .venv/bin/python -m scorer.profile --resume "{RESUME_PATH}"'
        )


def run_scorer(jobs):
    """In-process scorer: replaces the old `python -m scorer.score` subprocess.

    Mirrors scorer/score.py main(): per-job codex call, bounded concurrency,
    identifying fields carried forward, original order preserved.
    """
    if not jobs:
        return []
    ensure_profile_cache()

    scorer_src = os.path.join(SCORER_DIR, "src")
    if scorer_src not in sys.path:
        sys.path.insert(0, scorer_src)
    os.environ["CODEX_HOME"] = SCORER_CODEX_HOME
    from scorer.score import load_profile, score_job  # noqa: E402

    profile = load_profile(Path(PROFILE_CACHE))
    timeout = SCORER_JOB_TIMEOUT_SECONDS
    attempts = SCORER_ATTEMPTS
    concurrency = max(1, min(3, SCORER_CONCURRENCY, len(jobs) or 1))

    def score_one(index, job):
        title = job.get("name") or job.get("title") or "(unknown)"
        jr = job.get("jr") or "(unknown)"
        print(f"  [{index + 1}/{len(jobs)}] scoring {jr} — {title}", file=sys.stderr, flush=True)
        result = score_job(job, profile, timeout=timeout, attempts=attempts)
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

    slots = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(score_one, i, job) for i, job in enumerate(jobs)]
        for future in as_completed(futures):
            index, merged = future.result()
            slots[index] = merged
            jr = merged.get("jr") or "(unknown)"
            print(f"  [{index + 1}/{len(jobs)}] done {jr}", file=sys.stderr, flush=True)
    return slots


def summarize_fits(jobs):
    summary = {"strongFit": 0, "goodFit": 0, "possibleStretch": 0, "lowFit": 0}
    for job in jobs:
        suitability = job.get("suitability")
        if suitability == "Strong fit":
            summary["strongFit"] += 1
        elif suitability == "Good fit":
            summary["goodFit"] += 1
        elif suitability == "Possible stretch":
            summary["possibleStretch"] += 1
        else:
            summary["lowFit"] += 1
    return summary


def sort_scored(jobs):
    return sorted(jobs, key=lambda j: (-(j.get("score") or 0), _title_key(j.get("title"))))


def build_first_seen_by_key(snapshot_history):
    first_seen = {}
    if not isinstance(snapshot_history, list) or len(snapshot_history) < 2:
        return first_seen
    previous_jobs = snapshot_history[0].get("jobs") or []
    for entry in snapshot_history[1:]:
        previous_keys = {job_key(job) for job in previous_jobs}
        for job in entry.get("jobs") or []:
            key = job_key(job)
            if key not in previous_keys and key not in first_seen:
                first_seen[key] = entry["label"]
        previous_jobs = entry.get("jobs") or []
    return first_seen


def attach_scoring_dates(scored_jobs, source_jobs):
    source_by_key = {job_key(job): job for job in source_jobs}
    out = []
    for index, scored in enumerate(scored_jobs):
        source = source_by_key.get(job_key(scored))
        if source is None and index < len(source_jobs):
            source = source_jobs[index]
        first_seen = scored.get("firstSeenDate")
        if first_seen is None:
            first_seen = source.get("firstSeenDate") if source else None
        # {**scored, "firstSeenDate": ...} keeps the key in place if present, else appends — matches JS spread.
        out.append({**scored, "firstSeenDate": first_seen})
    return out


def render_markdown(report):
    md = f"# NVIDIA daily jobs — {report['date']}\n\n"
    md += f"- Jobs today: {report['currentJobCount']}\n"
    md += f"- Added: {report['addedCount']}\n"
    md += f"- Scored now: {report['rankedJobCount']}\n"
    if report.get("backlogCount"):
        md += f"- Backfilled: {report['backlogCount']}\n"
    if report.get("deferredScoreCount"):
        md += f"- Deferred unscored: {report['deferredScoreCount']}"
        if report.get("deferredDates"):
            md += f" ({', '.join(report['deferredDates'])})"
        md += "\n"
    if report.get("scoreErrorCount"):
        md += f"- Scoring errors to retry: {report['scoreErrorCount']}\n"
    md += f"- Canceled: {report['canceledCount']}\n"

    if report["profileHighlights"]:
        md += f"- Profile: {', '.join(report['profileHighlights'])}\n"

    if report.get("baselineCreated"):
        md += "\nBaseline established today. Scoring of newly added jobs starts from the next dated run.\n"
        return md

    if not report["rankedJobs"]:
        md += (
            f"\nNo jobs scored in this run; {report['deferredScoreCount']} active unscored jobs remain queued.\n"
            if report.get("deferredScoreCount")
            else "\nNo newly added NVIDIA jobs today.\n"
        )
        if report["canceledJobs"]:
            md += "\n## Canceled\n"
            for j in report["canceledJobs"]:
                md += markdown_job_line(j) + "\n"
        return md

    md += f"- Strong fit: {report['fitSummary']['strongFit']}\n"
    md += f"- Good fit: {report['fitSummary']['goodFit']}\n"
    md += f"- Possible stretch: {report['fitSummary']['possibleStretch']}\n"
    md += f"- Low fit: {report['fitSummary']['lowFit']}\n"

    md += "\n## Jobs Ranked For You\n"
    for index, job in enumerate(report["rankedJobs"]):
        md += f"\n### {index + 1}. [{job['jr']}] {job['title']}\n"
        md += f"- Fit: {job['suitability']} ({job['score']})\n"
        md += f"- Action: {job['recommendation']}\n"
        if job.get("firstSeenDate"):
            md += f"- Added: {job['firstSeenDate']}\n"
        md += f"- Posted: {job.get('posted') if job.get('posted') is not None else ''}\n"
        md += f"- Locations: {'; '.join(job.get('locations') or [])}\n"
        md += f"- Link: {job['link']}\n"
        if job.get("verdict"):
            md += f"- Verdict: {job['verdict']}\n"
        if job.get("matchedReasons"):
            md += "- Matches:\n"
            for r in job["matchedReasons"]:
                md += f"  - {r}\n"
        if job.get("gapReasons"):
            md += "- Gaps:\n"
            for r in job["gapReasons"]:
                md += f"  - {r}\n"
        if job.get("error"):
            md += f"- Note: scoring error — {job['error']}\n"

    if report["canceledJobs"]:
        md += "\n## Canceled\n"
        for j in report["canceledJobs"]:
            md += markdown_job_line(j) + "\n"

    return md


TELEGRAM_LIMIT = 4096
TELEGRAM_BUDGET = 3800
TELEGRAM_PER_COMPANY = 6  # max actionable jobs shown per company in the grouped digest


def _u16len(s):
    # Length in UTF-16 code units, matching JS String.length (and Telegram's limit).
    return len(s.encode("utf-16-le")) // 2


def _u16slice(s, n):
    # First n UTF-16 code units, matching JS String.prototype.slice(0, n).
    return s.encode("utf-16-le")[: n * 2].decode("utf-16-le", errors="ignore")


def first_sentence(text, max_len=220):
    if not text:
        return ""
    m = re.match(r"[\s\S]*?[.!?。！？](?=\s|$)", str(text))
    s = (m.group(0) if m else str(text)).strip()
    return _u16slice(s, max_len - 1).rstrip() + "…" if _u16len(s) > max_len else s


def fit_icon(suitability):
    return {"Strong fit": "🟢", "Good fit": "🟡", "Possible stretch": "🟠", "Low fit": "⚪️"}.get(
        suitability, "⚪️"
    )


def render_telegram_digest(report):
    lines = []
    ranked_job_count = report.get("rankedJobCount", len(report["rankedJobs"]))
    backlog_count = report.get("backlogCount") or 0
    deferred_score_count = report.get("deferredScoreCount") or 0
    lines.append(f"🦀 NVIDIA {report['location']} — {report['date']}")
    lines.append(
        f"{report['currentJobCount']} active · +{report['addedCount']} added today · "
        f"{ranked_job_count} scored"
        f"{f' ({backlog_count} backfill)' if backlog_count else ''}"
        f"{f' · {deferred_score_count} queued' if deferred_score_count else ''} · "
        f"-{report['canceledCount']} canceled"
    )

    if report.get("baselineCreated"):
        lines.append("")
        lines.append("Baseline established. New-job tracking starts tomorrow.")
        return "\n".join(lines)

    if not report["rankedJobs"]:
        lines.append("")
        if deferred_score_count:
            lines.append(
                f"No jobs scored in this run; {deferred_score_count} active unscored jobs remain queued."
            )
        else:
            lines.append("No new jobs today.")
        if report["canceledJobs"]:
            lines.append("")
            lines.append(f"❌ Canceled ({len(report['canceledJobs'])}):")
            for c in report["canceledJobs"][:10]:
                lines.append(telegram_job_chunk(c).lstrip("\n"))
        return "\n".join(lines)

    f = report["fitSummary"]
    lines.append(
        f"Fit: {f['strongFit']} strong / {f['goodFit']} good / {f['possibleStretch']} stretch / {f['lowFit']} low"
    )
    if deferred_score_count:
        dates = f" from {', '.join(report['deferredDates'])}" if report.get("deferredDates") else ""
        lines.append(f"Queued next: {deferred_score_count} unscored active job(s){dates}")
    lines.append("")
    lines.append(f"➕ SCORED ({ranked_job_count}) ranked by fit:")

    actionable = [j for j in report["rankedJobs"] if j.get("recommendation") != "Skip"]
    skip_pile = [j for j in report["rankedJobs"] if j.get("recommendation") == "Skip"]

    body = {"text": "", "truncated": 0}

    def try_append(chunk):
        if _u16len("\n".join(lines)) + _u16len(body["text"]) + _u16len(chunk) + 1 > TELEGRAM_BUDGET:
            body["truncated"] += 1
            return False
        body["text"] += chunk
        return True

    for i, j in enumerate(actionable):
        head = f"\n\n{i + 1}. {fit_icon(j.get('suitability'))} [{j['jr']}] {j['title']}"
        first_seen = f" · added {j['firstSeenDate']}" if j.get("firstSeenDate") else ""
        meta = f"\n   {j['suitability']} ({j['score']}) · {j['recommendation']}{first_seen}"
        verdict = f"\n   {first_sentence(j['verdict'], 220)}" if j.get("verdict") else ""
        link = f"\n   {j['link']}"
        if not try_append(head + meta + verdict + link):
            continue

    if skip_pile:
        header = f"\n\n⚪️ Skip ({len(skip_pile)}):"
        if try_append(header):
            for j in skip_pile:
                try_append(telegram_job_chunk(j, include_added=True))

    if report["canceledJobs"]:
        header = f"\n\n❌ Canceled ({len(report['canceledJobs'])}):"
        if try_append(header):
            for c in report["canceledJobs"][:8]:
                try_append(telegram_job_chunk(c))

    if body["truncated"] > 0:
        body["text"] += (
            f"\n\n…{body['truncated']} item(s) trimmed; "
            f"see latest_{slugify(report['location'])}.md for full report."
        )

    out = "\n".join(lines) + body["text"]
    return _u16slice(out, TELEGRAM_LIMIT - 1) + "…" if _u16len(out) > TELEGRAM_LIMIT else out


def project_ledger_jobs(current_jobs, key_to_score):
    """Shape MySQL-resident scores into rankedJobs entries using today's snapshot metadata.

    fetch_scores_for_keys only returns score-side fields (score, verdict, …); the
    title/link/locations come from the active snapshot row. Jobs not present in
    the ledger (key_to_score) are skipped — they belong to the L3 scoring path.
    """
    if not key_to_score:
        return []
    out = []
    for job in current_jobs:
        key = job_key(job)
        score_data = key_to_score.get(key)
        if score_data is None:
            continue
        out.append({
            "jr": job.get("jr"),
            "id": job.get("id"),
            "title": job.get("name") or job.get("title"),
            "link": job.get("link"),
            "locations": job.get("locations"),
            "department": job.get("department"),
            "posted": job.get("datePosted") or job.get("postedTs"),
            **score_data,
        })
    return out


def build_report(
    *,
    current_jobs,
    previous_jobs,
    previous_snapshot_file,
    snapshot_history=None,
    successful_score_keys=None,
    report_date=None,
    max_scoring_jobs_per_run=None,
    score_fn=run_scorer,
    source=None,
    profile_hash=None,
    ledger_jobs=None,
    score_filter=None,
):
    if report_date is None:
        report_date = SNAPSHOT_LABEL
    if max_scoring_jobs_per_run is None:
        max_scoring_jobs_per_run = MAX_SCORING_JOBS_PER_RUN
    profile_highlights = load_profile_highlights()

    if not previous_snapshot_file:
        return {
            "date": report_date,
            "location": LOCATION,
            "resumePath": RESUME_PATH,
            "currentSnapshot": snapshot_filename(report_date, source),
            "previousSnapshot": None,
            "baselineCreated": True,
            "profileHighlights": profile_highlights,
            "profileHash": profile_hash,
            "currentJobCount": len(current_jobs),
            "addedCount": 0,
            "canceledCount": 0,
            "canceledJobs": [],
            "fitSummary": summarize_fits([]),
            "backlogCount": 0,
            "deferredScoreCount": 0,
            "scoreErrorCount": 0,
            "remainingUnscoredCount": 0,
            "scoredDates": [],
            "deferredDates": [],
            "rankedJobCount": 0,
            "rankedJobs": [],
            "newJobs": [],
        }

    diff = partition_diff(current_jobs, previous_jobs)
    new_jobs, canceled_jobs = diff["newJobs"], diff["canceledJobs"]
    first_seen_by_key = build_first_seen_by_key(snapshot_history) if snapshot_history else None
    handled_score_keys = successful_score_keys if successful_score_keys is not None else set()
    if first_seen_by_key is not None:
        score_candidates = [
            {**job, "firstSeenDate": first_seen_by_key.get(job_key(job))}
            for job in current_jobs
        ]
        score_candidates = [
            job for job in score_candidates if job["firstSeenDate"] and job_key(job) not in handled_score_keys
        ]
    else:
        score_candidates = [{**job, "firstSeenDate": report_date} for job in new_jobs]

    skipped_intern_jobs = [build_skipped_intern_score(job) for job in score_candidates if is_intern_job(job)]
    model_score_candidates = [job for job in score_candidates if not is_intern_job(job)]
    # Per-source policy filter (e.g. non-semi sources only score AI/data-related titles).
    # Filtered-out jobs are still in the snapshot + MySQL — they just don't reach codex.
    score_filtered_count = 0
    if score_filter is not None:
        before = len(model_score_candidates)
        model_score_candidates = [job for job in model_score_candidates if score_filter(job)]
        score_filtered_count = before - len(model_score_candidates)
    if max_scoring_jobs_per_run > 0:
        jobs_to_score = model_score_candidates[:max_scoring_jobs_per_run]
        deferred_jobs = model_score_candidates[max_scoring_jobs_per_run:]
    else:
        jobs_to_score = model_score_candidates
        deferred_jobs = []

    scored_by_model = attach_scoring_dates(score_fn(jobs_to_score), jobs_to_score)
    fresh_jobs = [*scored_by_model, *skipped_intern_jobs]
    fresh_keys = {job_key(j) for j in fresh_jobs}
    # Digest = today's diff vs yesterday. Restrict both fresh (this run's LLM work) and
    # historical (ledger pre-fills) to firstSeenDate == today. Backlog catch-ups scored
    # this run still advance MySQL and appear in the full markdown report, but they are
    # not "today's change" and don't surface in the Telegram digest — same-day re-runs
    # must produce a stable digest, which requires excluding any fresh-vs-ledger drift
    # on non-today entries.
    todays_fresh = [j for j in fresh_jobs if j.get("firstSeenDate") == report_date]
    todays_ledger = [
        j for j in (ledger_jobs or [])
        if j.get("firstSeenDate") == report_date and job_key(j) not in fresh_keys
    ]
    scored = sort_scored([*todays_fresh, *todays_ledger])
    backlog_count = len([j for j in fresh_jobs if j.get("firstSeenDate") and j["firstSeenDate"] != report_date])
    score_error_count = len([j for j in fresh_jobs if j.get("error")])
    scored_dates = sorted({j["firstSeenDate"] for j in scored if j.get("firstSeenDate")})
    deferred_dates = sorted({j["firstSeenDate"] for j in deferred_jobs if j.get("firstSeenDate")})

    return {
        "date": report_date,
        "location": LOCATION,
        "resumePath": RESUME_PATH,
        "currentSnapshot": snapshot_filename(report_date, source),
        "previousSnapshot": os.path.basename(previous_snapshot_file),
        "baselineCreated": False,
        "profileHighlights": profile_highlights,
        "profileHash": profile_hash,
        "currentJobCount": len(current_jobs),
        "addedCount": len(new_jobs),
        "canceledCount": len(canceled_jobs),
        "canceledJobs": canceled_jobs,
        "fitSummary": summarize_fits(scored),
        "scoreCandidateCount": len(score_candidates),
        "modelScoreCandidateCount": len(model_score_candidates),
        "scoreFilteredCount": score_filtered_count,
        "scoreLimit": max_scoring_jobs_per_run,
        "backlogCount": backlog_count,
        "deferredScoreCount": len(deferred_jobs),
        "scoreErrorCount": score_error_count,
        "remainingUnscoredCount": len(deferred_jobs) + score_error_count,
        "scoredDates": scored_dates,
        "deferredDates": deferred_dates,
        "rankedJobCount": len(scored),
        "rankedJobs": scored,
        "newJobs": scored,
    }


def remove_latest_report_files():
    if not is_date_label(SNAPSHOT_LABEL):
        return
    slug = slugify(LOCATION)
    latest_base = os.path.join(REPORT_DIR, f"latest_{slug}")
    for suffix in (".json", ".md", "_telegram.md"):
        try:
            os.remove(f"{latest_base}{suffix}")
        except OSError:
            pass


def _write_json(path, obj):
    import json

    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=2))


def write_source_report(report, source):
    """Per-source dated report ({label}_{infix}{slug}.json) — feeds score-dedup, DB archive, and grouping."""
    ensure_dir(REPORT_DIR)
    path = os.path.join(REPORT_DIR, f"{SNAPSHOT_LABEL}_{_source_infix(source)}{slugify(LOCATION)}.json")
    _write_json(path, report)
    return path


def strip_proxy_env():
    # codex and Playwright both misbehave through the local proxy here, so the
    # whole run goes proxy-free (matches run-daily.sh and the old runPythonScorer).
    for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(var, None)


def run_fetch():
    strip_proxy_env()
    import asyncio
    import time

    import fetch  # noqa: E402 - lazy: pulls in Playwright only when fetching

    # The NVIDIA scrape drives a real Chromium. In a background/scheduled run macOS
    # can suspend the process's network I/O (App Nap / idle sleep), surfacing as
    # net::ERR_NETWORK_IO_SUSPENDED (and friends) mid-navigation — which is why
    # manual foreground runs succeed but the cron run fails. These are transient,
    # so retry the whole fetch with a fresh browser each attempt. run-daily.sh also
    # holds a `caffeinate` assertion for the run's lifetime to prevent the sleep
    # that triggers this in the first place.
    transient_markers = (
        "ERR_NETWORK_IO_SUSPENDED",
        "ERR_NETWORK_CHANGED",
        "ERR_INTERNET_DISCONNECTED",
        "ERR_CONNECTION",
        "ERR_TIMED_OUT",
        "ERR_NAME_NOT_RESOLVED",
        "Timeout",
        "net::ERR",
    )
    attempts = max(1, int(os.environ.get("NVIDIA_FETCH_ATTEMPTS", "3")))
    for attempt in range(1, attempts + 1):
        try:
            asyncio.run(fetch.main())
            return
        except Exception as error:  # noqa: BLE001 - classify, then re-raise or retry
            message = str(error)
            is_transient = any(marker in message for marker in transient_markers)
            if not is_transient or attempt == attempts:
                raise
            backoff = min(30, 5 * attempt)
            print(
                f"NVIDIA fetch attempt {attempt}/{attempts} hit transient network "
                f"error; retrying in {backoff}s: {message[:160]}",
                file=sys.stderr,
            )
            time.sleep(backoff)


def _registry():
    import sources

    return sources.SOURCES


def parse_source_list(value):
    return [s.strip() for s in value.split(",") if s.strip()]


def enabled_sources():
    env = os.environ.get("MONITOR_SOURCES")
    if env:
        return parse_source_list(env)
    return ["nvidia", *sorted(_registry().keys())]


def source_display(source):
    if source == "nvidia":
        return "NVIDIA"
    registry = _registry()
    return registry[source]["display"] if source in registry else source


def fetch_source(source):
    """Fetch + write the current snapshot for one source. NVIDIA uses Playwright (fetch.py);
    HTTP sources use the sources/ adapters and the shared snapshot renderers."""
    if source == "nvidia":
        run_fetch()
        return
    import json

    import fetch as fetchmod
    import sources

    jobs = sources.get_source(source)["fetch"]()
    base = os.path.join(SNAPSHOT_DIR, f"{SNAPSHOT_LABEL}_{_source_infix(source)}{slugify(LOCATION)}")
    header = f"{source_display(source)} — {LOCATION}"
    with open(f"{base}.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(jobs, ensure_ascii=False, indent=2))
    with open(f"{base}.md", "w", encoding="utf-8") as f:
        f.write(fetchmod.render_markdown(jobs, header, SNAPSHOT_LABEL))
    with open(f"{base}.csv", "w", encoding="utf-8") as f:
        f.write(fetchmod.render_csv(jobs))
    print(f"  {source_display(source)}: fetched {len(jobs)} jobs", file=sys.stderr)


def _load_ledger_state(source, current_jobs, profile_hash):
    """Pre-scoring read of the MySQL ledger: (already-scored keys, projected ledger jobs).

    Opens a short-lived connection — we don't hold it across the LLM calls that
    can take minutes per job. Persistence later opens its own connection.
    """
    import db

    if not profile_hash:
        return set(), []
    db.ensure_database_and_schema_from_env()
    conn = db.create_mysql_connection_from_env()
    try:
        successful = db.scored_keys(conn, source=source, profile_hash=profile_hash)
        score_data = db.fetch_scores_for_keys(
            conn,
            source=source,
            profile_hash=profile_hash,
            job_keys=[job_key(job) for job in current_jobs],
        )
    finally:
        conn.close()
    return successful, project_ledger_jobs(current_jobs, score_data)


def run_one_source(source, seed=False):
    """Full per-source pipeline: fetch -> diff -> score -> per-source report -> persist. Returns the report.

    seed=True scores the entire current backlog (treats every current job as new, unlimited) instead of
    baselining — used once when onboarding a new company so its existing openings get ranked immediately.
    """
    display = source_display(source)
    if not SKIP_FETCH:
        fetch_source(source)

    current_file = find_snapshot_by_label(SNAPSHOT_LABEL, source)
    if not current_file:
        raise RuntimeError(f"snapshot not found for {source} ({SNAPSHOT_LABEL})")
    current_jobs = load_snapshot(current_file)
    current_profile_hash = load_profile_hash()
    import sources as _sources
    score_filter = _sources.score_filter_for(source)

    if seed:
        report = build_report(
            current_jobs=current_jobs,
            previous_jobs=[],  # everything counts as new
            previous_snapshot_file=current_file,  # non-None so it's not treated as a baseline run
            snapshot_history=None,
            successful_score_keys=set(),
            report_date=SNAPSHOT_LABEL,
            max_scoring_jobs_per_run=0,  # no per-run cap: score the whole backlog
            source=source,
            profile_hash=current_profile_hash,
            score_filter=score_filter,
        )
        print(f"  {display}: seeding — scoring {report['rankedJobCount']} current openings", file=sys.stderr)
    else:
        previous_file = find_previous_snapshot(SNAPSHOT_LABEL, source)
        previous_jobs = load_snapshot(previous_file) if previous_file else []
        history = load_snapshot_history(SNAPSHOT_LABEL, source)
        if not is_date_label(SNAPSHOT_LABEL) and not any(e["label"] == SNAPSHOT_LABEL for e in history):
            history = [*history, {"label": SNAPSHOT_LABEL, "file": current_file, "jobs": current_jobs}]
        successful, ledger_jobs = _load_ledger_state(source, current_jobs, current_profile_hash)
        report = build_report(
            current_jobs=current_jobs,
            previous_jobs=previous_jobs,
            previous_snapshot_file=previous_file,
            snapshot_history=history,
            successful_score_keys=successful,
            ledger_jobs=ledger_jobs,
            source=source,
            profile_hash=current_profile_hash,
            score_filter=score_filter,
        )
    report_file = write_source_report(report, source)

    if is_date_label(SNAPSHOT_LABEL):
        import db

        persisted = db.persist_daily_run_from_env(
            source=source,
            location=LOCATION,
            run_date=SNAPSHOT_LABEL,
            current_jobs=current_jobs,
            report=report,
            snapshot_file=current_file,
            report_file=report_file,
            profile_cache_path=PROFILE_CACHE,
        )
        print(
            f"  {display}: persisted run {persisted['run_id']} — "
            f"{persisted['job_snapshots']} snapshots, {persisted['scores']} scores "
            f"({persisted.get('resume_scores', 0)} → current resume).",
            file=sys.stderr,
        )
    return report


def build_grouped(results):
    keys = (
        "currentJobCount", "addedCount", "canceledCount", "rankedJobCount",
        "backlogCount", "deferredScoreCount", "scoreErrorCount",
    )
    successful = [r for r in results if r.get("status", "ok") == "ok" and r.get("report") is not None]
    totals = {k: sum((r["report"].get(k) or 0) for r in successful) for k in keys}
    totals["failedSourceCount"] = len([r for r in results if r.get("status") == "error"])
    return {
        "date": SNAPSHOT_LABEL,
        "location": LOCATION,
        "entries": results,
        "sources": successful,
        "totals": totals,
    }


def grouped_entries(grouped):
    return grouped.get("entries") or grouped.get("sources") or []


def short_error(error, limit=180):
    text = re.sub(r"\s+", " ", str(error)).strip()
    return _u16slice(text, limit - 1) + "…" if _u16len(text) > limit else text


def job_display_name(job):
    jr = job.get("jr") or job.get("id") or "unknown"
    title = job.get("title") or job.get("name") or "(untitled)"
    return f"[{jr}] {title}"


def markdown_job_line(job):
    link = job.get("link")
    return f"- {job_display_name(job)}" + (f" — {link}" if link else "")


def telegram_job_chunk(job, include_added=False, include_link=True):
    added = f" · added {job['firstSeenDate']}" if include_added and job.get("firstSeenDate") else ""
    chunk = f"\n• {job_display_name(job)}{added}"
    if include_link and job.get("link"):
        chunk += f"\n  {job['link']}"
    return chunk


def render_grouped_markdown(grouped):
    t = grouped["totals"]
    entries = grouped_entries(grouped)
    failed_count = t.get("failedSourceCount") or len([e for e in entries if e.get("status") == "error"])
    md = f"# Daily jobs — {grouped['location']} — {grouped['date']}\n\n"
    if failed_count:
        md += f"- Companies: {len(entries)} ({len(grouped.get('sources') or [])} succeeded, {failed_count} failed)\n"
    else:
        md += f"- Companies: {len(entries)}\n"
    md += (
        f"- Active: {t['currentJobCount']} · Added: {t['addedCount']} · "
        f"Scored: {t['rankedJobCount']} · Canceled: {t['canceledCount']}\n"
    )
    for entry in entries:
        if entry.get("status") == "error":
            md += f"\n## {entry['display']} (failed)\n"
            md += f"\n_Fetch failed:_ {short_error(entry.get('error', 'unknown error'), 300)}\n"
            continue
        r = entry["report"]
        md += f"\n## {entry['display']} (+{r['addedCount']}, {r['rankedJobCount']} scored, -{r['canceledCount']})\n"
        if r.get("baselineCreated"):
            md += "\nBaseline established today.\n"
            continue
        if not (r["addedCount"] or r["rankedJobCount"] or r["canceledCount"]):
            md += "\n_No changes._\n"
        elif not r["rankedJobs"]:
            md += "\n_No newly scored jobs._\n"
        for i, job in enumerate(r["rankedJobs"]):
            md += f"\n### {i + 1}. [{job['jr']}] {job['title']}\n"
            md += f"- Fit: {job['suitability']} ({job['score']}) · {job['recommendation']}\n"
            if job.get("firstSeenDate"):
                md += f"- Added: {job['firstSeenDate']}\n"
            md += f"- Link: {job['link']}\n"
            if job.get("verdict"):
                md += f"- Verdict: {job['verdict']}\n"
        if r["canceledJobs"]:
            md += "\n**Canceled:**\n"
            for canceled in r["canceledJobs"]:
                md += markdown_job_line(canceled) + "\n"
    return md


def render_grouped_telegram(grouped):
    t = grouped["totals"]
    failed_count = t.get("failedSourceCount") or len([e for e in grouped_entries(grouped) if e.get("status") == "error"])
    stats = (
        f"{t['currentJobCount']} active · +{t['addedCount']} added · "
        f"{t['rankedJobCount']} scored · -{t['canceledCount']} canceled"
    )
    if failed_count:
        stats += f" · {failed_count} failed"
    lines = [
        f"🦀 Jobs — {grouped['location']} — {grouped['date']}",
        stats,
    ]
    body = {"text": "", "truncated": 0}

    def try_append(chunk):
        if _u16len("\n".join(lines)) + _u16len(body["text"]) + _u16len(chunk) + 1 > TELEGRAM_BUDGET:
            body["truncated"] += 1
            return False
        body["text"] += chunk
        return True

    for entry in grouped_entries(grouped):
        if entry.get("status") == "error":
            try_append(f"\n\n=== {entry['display']} ===\n❌ Fetch failed: {short_error(entry.get('error', 'unknown error'))}")
            continue
        r = entry["report"]
        if r.get("baselineCreated"):
            try_append(f"\n\n=== {entry['display']} ===\nBaseline established.")
            continue
        actionable = [j for j in r["rankedJobs"] if j.get("recommendation") != "Skip"]
        skip_pile = [j for j in r["rankedJobs"] if j.get("recommendation") == "Skip"]
        if not (actionable or skip_pile or r["canceledJobs"]):
            if not (r["addedCount"] or r["rankedJobCount"] or r["canceledCount"]):
                try_append(f"\n\n=== {entry['display']} ===\n✅ Success: no changes.")
            else:
                try_append(f"\n\n=== {entry['display']} (+{r['addedCount']}) ===\n✅ Success: no newly scored jobs.")
            continue
        if not try_append(f"\n\n=== {entry['display']} (+{r['addedCount']}) ==="):
            continue
        # Cap actionable items per company so one big day (e.g. a seed) can't crowd out other companies.
        for i, j in enumerate(actionable[:TELEGRAM_PER_COMPANY]):
            first_seen = f" · added {j['firstSeenDate']}" if j.get("firstSeenDate") else ""
            head = f"\n\n{i + 1}. {fit_icon(j.get('suitability'))} [{j['jr']}] {j['title']}"
            meta = f"\n   {j['suitability']} ({j['score']}) · {j['recommendation']}{first_seen}"
            verdict = f"\n   {first_sentence(j['verdict'], 140)}" if j.get("verdict") else ""
            try_append(head + meta + verdict + f"\n   {j['link']}")
        if len(actionable) > TELEGRAM_PER_COMPANY:
            try_append(f"\n   …+{len(actionable) - TELEGRAM_PER_COMPANY} more scored (see full report)")
        if skip_pile:
            try_append(f"\n\n⚪️ Skip ({len(skip_pile)}):")
            for skipped in skip_pile[:8]:
                try_append(telegram_job_chunk(skipped, include_added=True, include_link=False))
        if r["canceledJobs"]:
            try_append(f"\n\n❌ Canceled ({len(r['canceledJobs'])}):")
            for canceled in r["canceledJobs"][:8]:
                try_append(telegram_job_chunk(canceled, include_link=False))

    if body["truncated"] > 0:
        body["text"] += f"\n\n…{body['truncated']} item(s) trimmed; see latest_{slugify(grouped['location'])}.md."
    out = "\n".join(lines) + body["text"]
    return _u16slice(out, TELEGRAM_LIMIT - 1) + "…" if _u16len(out) > TELEGRAM_LIMIT else out


def write_grouped(grouped):
    ensure_dir(REPORT_DIR)
    slug = slugify(LOCATION)
    latest = os.path.join(REPORT_DIR, f"latest_{slug}")
    _write_json(f"{latest}.json", grouped)
    with open(f"{latest}.md", "w", encoding="utf-8") as f:
        f.write(render_grouped_markdown(grouped))
    with open(f"{latest}_telegram.md", "w", encoding="utf-8") as f:
        f.write(render_grouped_telegram(grouped))
    if is_date_label(SNAPSHOT_LABEL):
        _write_json(os.path.join(REPORT_DIR, f"{SNAPSHOT_LABEL}_{slug}_grouped.json"), grouped)


def main():
    strip_proxy_env()  # codex (in-process scorer) and Playwright run proxy-free, even when fetch is skipped
    release_lock = acquire_run_lock()
    try:
        ensure_dir(REPORT_DIR)
        # Prevent cron from delivering stale latest_* reports if this run fails before write_grouped().
        remove_latest_report_files()

        sources_list = enabled_sources()
        seed_set = {s.strip() for s in (os.environ.get("SEED_SOURCES") or "").split(",") if s.strip()}
        print(f'Running daily jobs monitor for "{LOCATION}" — sources: {", ".join(sources_list)}', file=sys.stderr)
        if seed_set:
            print(f"Seed-scoring backlog for: {', '.join(sorted(seed_set))}", file=sys.stderr)
        if SKIP_FETCH:
            print("Skipping fetch because NVIDIA_SKIP_FETCH=1.", file=sys.stderr)

        results = []
        for source in sources_list:
            try:
                report = run_one_source(source, seed=(source in seed_set))
                results.append({"source": source, "display": source_display(source), "status": "ok", "report": report})
            except Exception as error:  # noqa: BLE001 - one source failing must not kill the rest
                print(f"WARN source '{source}' failed: {error}", file=sys.stderr)
                results.append({
                    "source": source,
                    "display": source_display(source),
                    "status": "error",
                    "error": str(error),
                })

        if not results:
            raise RuntimeError("all sources failed")

        grouped = build_grouped(results)
        write_grouped(grouped)
        t = grouped["totals"]
        print(
            f"Wrote grouped report — {len(results)} companies, added {t['addedCount']}, "
            f"scored {t['rankedJobCount']}, canceled {t['canceledCount']}.",
            file=sys.stderr,
        )
    finally:
        release_lock()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001 - mirror JS top-level catch
        print(f"FATAL {error}", file=sys.stderr)
        sys.exit(1)
