#!/usr/bin/env python3
"""Import manual re-score results into resume_scores.

This updates the profile-keyed score overlay used by the dashboard. It does not
touch the run-keyed scores history.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROFILE_CACHE = ROOT / "scorer" / "cache" / "profile_latest.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import create_mysql_connection_from_env, upsert_resume_score  # noqa: E402


def set_local_mysql_defaults() -> None:
    os.environ.setdefault("MYSQL_USER", "root")
    os.environ.setdefault("MYSQL_SOCKET_PATH", "/tmp/mysql.sock")
    os.environ.setdefault("MYSQL_DATABASE", "nvidia_jobs_monitor")


def read_json(path: str) -> tuple[Any, str | None]:
    if path == "-":
        return json.load(sys.stdin), None
    file_path = Path(path).resolve()
    return json.loads(file_path.read_text(encoding="utf-8")), str(file_path)


def load_profile(path: Path) -> dict[str, Any]:
    profile = json.loads(path.read_text(encoding="utf-8"))
    if not profile.get("resumeHash"):
        raise ValueError(f"profile cache missing resumeHash: {path}")
    return profile


def coalesce(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def flatten_score_row(item: dict[str, Any], default_source: str) -> dict[str, Any]:
    """Accept either a direct row or {jr,title,result:{score...}} scorer output."""
    if isinstance(item.get("result"), dict):
        result = dict(item["result"])
        result.setdefault("jr", item.get("jr"))
        result.setdefault("id", item.get("id"))
        result.setdefault("title", item.get("title"))
        result.setdefault("link", item.get("link"))
        result.setdefault("source", item.get("source"))
        return flatten_score_row(result, default_source)

    row = dict(item)
    row.setdefault("source", default_source)

    if "matched_reasons" in row and "matchedReasons" not in row:
        row["matchedReasons"] = row["matched_reasons"]
    if "gap_reasons" in row and "gapReasons" not in row:
        row["gapReasons"] = row["gap_reasons"]
    if "first_seen_date" in row and "firstSeenDate" not in row:
        row["firstSeenDate"] = row["first_seen_date"]
    if "matched_keywords" in row and "matchedKeywords" not in row:
        row["matchedKeywords"] = row["matched_keywords"]
    if "selection_reasons" in row and "selectionReasons" not in row:
        row["selectionReasons"] = row["selection_reasons"]

    return row


def rows_from_payload(payload: Any, default_source: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        items = payload["results"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = [payload]
    else:
        raise ValueError("input must be a JSON object, JSON array, or object with results[]")

    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"score row must be an object, got {type(item).__name__}")
        rows.append(flatten_score_row(item, default_source))
    return rows


def find_job(conn, row: dict[str, Any]) -> dict[str, Any] | None:
    source = coalesce(row.get("source"))
    jr = coalesce(row.get("jr"))
    external_id = coalesce(row.get("id"))
    job_key = coalesce(row.get("jobKey"), row.get("job_key"), external_id)
    if not source:
        return None

    sql = """
      SELECT id, jr, title, link, first_seen_date
      FROM jobs
      WHERE source = %s
        AND (
          (%s IS NOT NULL AND jr = %s)
          OR (%s IS NOT NULL AND external_id = %s)
          OR (%s IS NOT NULL AND job_key = %s)
        )
      ORDER BY last_seen_date DESC, id DESC
      LIMIT 1
    """
    with conn.cursor() as cur:
        cur.execute(sql, [source, jr, jr, external_id, external_id, job_key, job_key])
        found = cur.fetchone()
    if not found:
        return None

    first_seen = found[4]
    return {
        "id": found[0],
        "jr": found[1],
        "title": found[2],
        "link": found[3],
        "firstSeenDate": first_seen.isoformat() if hasattr(first_seen, "isoformat") else first_seen,
    }


def previous_resume_score(conn, *, profile_hash: str, job_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
              SELECT score, suitability, recommendation
              FROM resume_scores
              WHERE profile_hash = %s AND job_id = %s
              LIMIT 1
            """,
            [profile_hash, job_id],
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"score": row[0], "suitability": row[1], "recommendation": row[2]}


def normalize_row(row: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out.setdefault("jr", job.get("jr"))
    out.setdefault("title", job.get("title"))
    out.setdefault("link", job.get("link"))
    out.setdefault("firstSeenDate", job.get("firstSeenDate"))
    out.setdefault("matchedReasons", [])
    out.setdefault("gapReasons", [])
    out.setdefault("matchedKeywords", [])
    out.setdefault("selectionReasons", [])
    return out


def skip_reason(row: dict[str, Any]) -> str | None:
    if row.get("error"):
        return f"error: {row.get('error')}"
    if row.get("score") is None:
        return "missing score"
    return None


def validate_row(row: dict[str, Any]) -> None:
    score = int(row["score"])
    if score < 0 or score > 100:
        raise ValueError(f"{row.get('source')} {row.get('jr')}: score out of range: {score}")
    row["score"] = score
    for key in ("suitability", "recommendation"):
        if not row.get(key):
            raise ValueError(f"{row.get('source')} {row.get('jr')}: missing {key}")


def import_rows(
    *,
    rows: list[dict[str, Any]],
    profile: dict[str, Any],
    report_file: str | None,
    dry_run: bool,
    allow_missing: bool,
) -> dict[str, Any]:
    conn = create_mysql_connection_from_env()
    imported: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    try:
        conn.begin()
        for original in rows:
            row = dict(original)
            reason = skip_reason(row)
            if reason:
                skipped.append({
                    "source": row.get("source"),
                    "jr": row.get("jr"),
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "reason": reason,
                })
                continue
            validate_row(row)
            job = find_job(conn, row)
            if not job:
                missing.append({
                    "source": row.get("source"),
                    "jr": row.get("jr"),
                    "id": row.get("id"),
                    "title": row.get("title"),
                })
                continue

            normalized = normalize_row(row, job)
            previous = previous_resume_score(conn, profile_hash=profile["resumeHash"], job_id=job["id"])
            if not dry_run:
                upsert_resume_score(
                    conn,
                    source=normalized["source"],
                    job_id=job["id"],
                    run_date=normalized.get("firstSeenDate"),
                    score=normalized,
                    profile_hash=profile["resumeHash"],
                    resume_path=profile.get("resumePath"),
                    report_file=report_file,
                )
            imported.append({
                "source": normalized.get("source"),
                "jr": normalized.get("jr"),
                "title": normalized.get("title"),
                "jobId": job["id"],
                "previous": previous,
                "new": {
                    "score": int(normalized["score"]),
                    "suitability": normalized.get("suitability"),
                    "recommendation": normalized.get("recommendation"),
                },
            })

        if missing and not allow_missing:
            raise ValueError(f"could not match {len(missing)} job(s): {missing[:8]}")

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "dryRun": dry_run,
        "profileHash": profile["resumeHash"],
        "imported": imported,
        "missing": missing,
        "skipped": skipped,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upsert manual re-score results into resume_scores for the current resume profile.",
    )
    parser.add_argument(
        "report",
        help="JSON file to import, or '-' for stdin. Accepts {results:[...]}, a list, a direct row, or {jr,title,result:{...}}.",
    )
    parser.add_argument("--profile", type=Path, default=PROFILE_CACHE, help=f"profile cache path (default: {PROFILE_CACHE})")
    parser.add_argument("--source", default="nvidia", help="default source for rows that omit source (default: nvidia)")
    parser.add_argument("--dry-run", action="store_true", help="validate and print planned updates without committing")
    parser.add_argument("--allow-missing", action="store_true", help="skip rows that cannot be matched to jobs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    set_local_mysql_defaults()
    payload, report_file = read_json(args.report)
    profile = load_profile(args.profile)
    rows = rows_from_payload(payload, args.source)
    result = import_rows(
        rows=rows,
        profile=profile,
        report_file=report_file,
        dry_run=args.dry_run,
        allow_missing=args.allow_missing,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
