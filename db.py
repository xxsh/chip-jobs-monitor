"""MySQL persistence sidecar for the daily run.

Python port of db.mjs (Phase 2 of the JS→Python migration). Uses PyMySQL
(sync) in place of mysql2/promise. Pure helpers (hashing, slugify, date
coercion, env-config parsing) are byte-for-byte compatible with db.mjs so that
content hashes stay consistent across the migration.

db.mjs remains the live path until Phase 3 (daily.py) imports this module
directly; nothing here is wired into the running pipeline yet.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, parse_qs

import pymysql
from pymysql.constants import CLIENT

__dirname = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATABASE = "nvidia_jobs_monitor"
DEFAULT_SOURCE = "nvidia"


def quote_identifier(value):
    if not re.match(r"^[A-Za-z0-9_]+$", value):
        raise ValueError(f"Unsafe MySQL identifier: {value}")
    return f"`{value}`"


def parse_port(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 3306
    return parsed if parsed > 0 else 3306


def decode_url_part(value):
    return unquote(value) if value else None


def mysql_config_from_env(env=None, include_database=True, multiple_statements=False):
    if env is None:
        env = os.environ
    database = env.get("MYSQL_DATABASE") or DEFAULT_DATABASE

    if env.get("MYSQL_DSN"):
        url = urlparse(env["MYSQL_DSN"])
        dsn_database = unquote(url.path.lstrip("/")) if url.path else ""
        query = parse_qs(url.query)
        socket_path = (query.get("socketPath") or query.get("socket") or [None])[0]
        config = {
            "user": decode_url_part(url.username),
            "password": decode_url_part(url.password) or "",
            "charset": "utf8mb4",
        }
        if socket_path:
            config["unix_socket"] = socket_path
        else:
            config["host"] = url.hostname or "localhost"
            config["port"] = parse_port(url.port) if url.port else 3306
        if multiple_statements:
            config["client_flag"] = CLIENT.MULTI_STATEMENTS
        if include_database:
            config["database"] = dsn_database or database
        return {"configured": True, "database": dsn_database or database, "config": config}

    configured = bool(
        env.get("MYSQL_USER")
        or env.get("MYSQL_PASSWORD")
        or env.get("MYSQL_HOST")
        or env.get("MYSQL_PORT")
        or env.get("MYSQL_SOCKET_PATH")
        or env.get("MYSQL_DATABASE")
    )
    if not configured:
        return {"configured": False, "database": database, "config": None}

    config = {
        "user": env.get("MYSQL_USER") or "root",
        "password": env.get("MYSQL_PASSWORD") or "",
        "charset": "utf8mb4",
    }
    if env.get("MYSQL_SOCKET_PATH"):
        config["unix_socket"] = env["MYSQL_SOCKET_PATH"]
    else:
        config["host"] = env.get("MYSQL_HOST") or "localhost"
        config["port"] = parse_port(env.get("MYSQL_PORT"))
    if multiple_statements:
        config["client_flag"] = CLIENT.MULTI_STATEMENTS
    if include_database:
        config["database"] = database
    return {"configured": True, "database": database, "config": config}


def has_mysql_config(env=None):
    return mysql_config_from_env(env)["configured"]


def create_mysql_connection_from_env(include_database=True, multiple_statements=False):
    result = mysql_config_from_env(
        os.environ, include_database=include_database, multiple_statements=multiple_statements
    )
    if not result["configured"]:
        raise RuntimeError("MySQL is not configured. Set MYSQL_USER/MYSQL_SOCKET_PATH or MYSQL_DSN.")
    return pymysql.connect(**result["config"])


def ensure_database_and_schema_from_env(schema_path=None):
    if schema_path is None:
        schema_path = os.path.join(__dirname, "db", "schema.sql")

    base = mysql_config_from_env(os.environ, include_database=False)
    if not base["configured"]:
        raise RuntimeError("MySQL is not configured. Set MYSQL_USER/MYSQL_SOCKET_PATH or MYSQL_DSN.")

    admin = pymysql.connect(**base["config"])
    try:
        with admin.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS {quote_identifier(base['database'])} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        admin.commit()
    finally:
        admin.close()

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    db = create_mysql_connection_from_env(multiple_statements=True)
    try:
        with db.cursor() as cur:
            cur.execute(schema_sql)
            while cur.nextset():
                pass
        db.commit()
    finally:
        db.close()


def slugify(value):
    s = re.sub(r"[^a-z0-9]+", "-", str(value).lower())
    return re.sub(r"^-|-$", "", s)


def is_date_label(value):
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", str(value)))


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


def stable_stringify(value):
    # Matches db.mjs: recursively sorted keys, compact separators, raw unicode.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _coalesce(*values):
    for value in values:
        if value is not None:
            return value
    return None


def job_content_for_hash(job):
    return {
        "id": _coalesce(job.get("id")),
        "jr": _coalesce(job.get("jr")),
        "name": _coalesce(job.get("name"), job.get("title")),
        "locations": _coalesce(job.get("locations"), []),
        "department": _coalesce(job.get("department")),
        "workLocationOption": _coalesce(job.get("workLocationOption")),
        "postedTs": _coalesce(job.get("postedTs")),
        "creationTs": _coalesce(job.get("creationTs")),
        "link": _coalesce(job.get("link")),
        "datePosted": _coalesce(job.get("datePosted"), job.get("posted")),
        "validThrough": _coalesce(job.get("validThrough")),
        "employmentType": _coalesce(job.get("employmentType")),
        "description": _coalesce(job.get("description"), ""),
        "summary": _coalesce(job.get("summary"), ""),
        "responsibilities": _coalesce(job.get("responsibilities"), []),
        "requirements": _coalesce(job.get("requirements"), []),
        "preferred": _coalesce(job.get("preferred"), []),
        "detailError": _coalesce(job.get("detailError")),
    }


def job_content_hash(job):
    return sha256(stable_stringify(job_content_for_hash(job)))


def cancellation_key(canceled):
    return sha256(stable_stringify({"jr": _coalesce(canceled.get("jr")), "title": _coalesce(canceled.get("title"))}))


def to_mysql_date(value):
    if not value:
        return None
    text = str(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


def to_mysql_datetime(value):
    if not value:
        return None
    text = str(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return f"{text} 00:00:00"
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
    return f"{match.group(1)} {match.group(2)}" if match else None


def _epoch_to_mysql_date(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number or number < 0:
        return None
    seconds = number / 1000 if number > 10_000_000_000 else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def resolve_job_posted_date(job, fallback_date=None):
    """Public job posting date, falling back only when source metadata is absent."""
    if job is None:
        job = {}
    return (
        to_mysql_date(_coalesce(job.get("datePosted"), job.get("posted")))
        or _epoch_to_mysql_date(job.get("posted"))
        or _epoch_to_mysql_date(job.get("postedTs"))
        or _epoch_to_mysql_date(job.get("creationTs"))
        or to_mysql_date(fallback_date)
    )


def resolve_job_posted_datetime(job, fallback_date=None):
    if job is None:
        job = {}
    direct = to_mysql_datetime(_coalesce(job.get("datePosted"), job.get("posted")))
    if direct:
        return direct
    posted_date = resolve_job_posted_date(job, fallback_date)
    return f"{posted_date} 00:00:00" if posted_date else None


def json_param(value):
    return json.dumps(value if value is not None else None, ensure_ascii=False)


def int_param(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def load_profile_hash(profile_cache_path):
    if not profile_cache_path or not os.path.exists(profile_cache_path):
        return None
    try:
        with open(profile_cache_path, "r", encoding="utf-8") as f:
            profile = json.load(f)
        return profile.get("resumeHash")
    except (ValueError, OSError):
        return None


def _has_hashable_content(job):
    # Mirror JS truthiness of `description || summary || requirements || responsibilities`,
    # where any list (even empty) is truthy.
    for key in ("description", "summary", "requirements", "responsibilities"):
        value = job.get(key)
        if isinstance(value, (list, dict)):
            return True
        if value:
            return True
    return False


def _str_id(value):
    return str(value) if value is not None else None


def upsert_run(conn, source, location, run_date, snapshot_file, report_file, report):
    location_slug = slugify(location)
    sql = """
      INSERT INTO runs (
        source, location, location_slug, run_date, snapshot_file, report_file, previous_snapshot,
        resume_path, baseline_created, status, current_job_count, added_count, canceled_count,
        ranked_job_count, backlog_count, deferred_score_count, score_error_count,
        profile_highlights, fit_summary, scored_dates, deferred_dates, raw_report
      )
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), CAST(%s AS JSON), CAST(%s AS JSON), CAST(%s AS JSON))
      ON DUPLICATE KEY UPDATE
        id = LAST_INSERT_ID(id),
        location = VALUES(location),
        snapshot_file = VALUES(snapshot_file),
        report_file = VALUES(report_file),
        previous_snapshot = VALUES(previous_snapshot),
        resume_path = VALUES(resume_path),
        baseline_created = VALUES(baseline_created),
        status = VALUES(status),
        current_job_count = VALUES(current_job_count),
        added_count = VALUES(added_count),
        canceled_count = VALUES(canceled_count),
        ranked_job_count = VALUES(ranked_job_count),
        backlog_count = VALUES(backlog_count),
        deferred_score_count = VALUES(deferred_score_count),
        score_error_count = VALUES(score_error_count),
        profile_highlights = VALUES(profile_highlights),
        fit_summary = VALUES(fit_summary),
        scored_dates = VALUES(scored_dates),
        deferred_dates = VALUES(deferred_dates),
        raw_report = VALUES(raw_report)
    """
    params = [
        source,
        location,
        location_slug,
        run_date,
        os.path.basename(snapshot_file) if snapshot_file else None,
        os.path.basename(report_file) if report_file else None,
        _coalesce(report.get("previousSnapshot")),
        _coalesce(report.get("resumePath")),
        bool(report.get("baselineCreated")),
        "imported",
        int_param(report.get("currentJobCount")),
        int_param(report.get("addedCount")),
        int_param(report.get("canceledCount")),
        int_param(report.get("rankedJobCount")),
        int_param(report.get("backlogCount")),
        int_param(report.get("deferredScoreCount")),
        int_param(report.get("scoreErrorCount")),
        json_param(_coalesce(report.get("profileHighlights"), [])),
        json_param(_coalesce(report.get("fitSummary"), {})),
        json_param(_coalesce(report.get("scoredDates"), [])),
        json_param(_coalesce(report.get("deferredDates"), [])),
        json_param(report),
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid


def upsert_job(conn, source, run_id, run_date, job, title_override=None):
    key = job_key(job)
    hash_value = job_content_hash(job) if _has_hashable_content(job) else None
    title = title_override if title_override is not None else _coalesce(job.get("name"), job.get("title"))
    first_seen_date = resolve_job_posted_date(job, run_date)
    sql = """
      INSERT INTO jobs (
        source, job_key, external_id, jr, title, link, department,
        first_seen_date, last_seen_date, latest_content_hash, latest_seen_run_id
      )
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
      ON DUPLICATE KEY UPDATE
        id = LAST_INSERT_ID(id),
        external_id = COALESCE(VALUES(external_id), external_id),
        jr = COALESCE(VALUES(jr), jr),
        title = COALESCE(VALUES(title), title),
        link = COALESCE(VALUES(link), link),
        department = COALESCE(VALUES(department), department),
        first_seen_date = LEAST(COALESCE(first_seen_date, VALUES(first_seen_date)), VALUES(first_seen_date)),
        last_seen_date = GREATEST(COALESCE(last_seen_date, VALUES(last_seen_date)), VALUES(last_seen_date)),
        latest_content_hash = COALESCE(VALUES(latest_content_hash), latest_content_hash),
        latest_seen_run_id = VALUES(latest_seen_run_id)
    """
    params = [
        source,
        key,
        _str_id(job.get("id")),
        _coalesce(job.get("jr")),
        title,
        _coalesce(job.get("link")),
        _coalesce(job.get("department")),
        first_seen_date,
        run_date,
        hash_value,
        run_id,
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid


def upsert_job_snapshot(conn, source, run_id, job_id, job):
    key = job_key(job)
    hash_value = job_content_hash(job)
    sql = """
      INSERT INTO job_snapshots (
        run_id, job_id, source, job_key, title, jr, external_id, link, locations,
        department, work_location_option, posted_ts, creation_ts, date_posted, valid_through,
        employment_type, description, summary, responsibilities, requirements_json, preferred,
        detail_error, content_hash, raw_json, active
      )
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), CAST(%s AS JSON), %s, %s, CAST(%s AS JSON), %s)
      ON DUPLICATE KEY UPDATE
        title = VALUES(title),
        jr = VALUES(jr),
        external_id = VALUES(external_id),
        link = VALUES(link),
        locations = VALUES(locations),
        department = VALUES(department),
        work_location_option = VALUES(work_location_option),
        posted_ts = VALUES(posted_ts),
        creation_ts = VALUES(creation_ts),
        date_posted = VALUES(date_posted),
        valid_through = VALUES(valid_through),
        employment_type = VALUES(employment_type),
        description = VALUES(description),
        summary = VALUES(summary),
        responsibilities = VALUES(responsibilities),
        requirements_json = VALUES(requirements_json),
        preferred = VALUES(preferred),
        detail_error = VALUES(detail_error),
        content_hash = VALUES(content_hash),
        raw_json = VALUES(raw_json),
        active = VALUES(active)
    """
    params = [
        run_id,
        job_id,
        source,
        key,
        _coalesce(job.get("name"), job.get("title")),
        _coalesce(job.get("jr")),
        _str_id(job.get("id")),
        _coalesce(job.get("link")),
        json_param(_coalesce(job.get("locations"), [])),
        _coalesce(job.get("department")),
        _coalesce(job.get("workLocationOption")),
        _coalesce(job.get("postedTs")),
        _coalesce(job.get("creationTs")),
        resolve_job_posted_datetime(job),
        to_mysql_datetime(job.get("validThrough")),
        _coalesce(job.get("employmentType")),
        _coalesce(job.get("description")),
        _coalesce(job.get("summary")),
        json_param(_coalesce(job.get("responsibilities"), [])),
        json_param(_coalesce(job.get("requirements"), [])),
        json_param(_coalesce(job.get("preferred"), [])),
        _coalesce(job.get("detailError")),
        hash_value,
        json_param(job),
        True,
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)


def find_job_id_by_jr(conn, source, jr):
    if not jr:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM jobs WHERE source = %s AND jr = %s ORDER BY last_seen_date DESC, id DESC LIMIT 1",
            [source, jr],
        )
        row = cur.fetchone()
    return row[0] if row else None


def upsert_score(conn, source, run_id, run_date, score, profile_hash, resume_path):
    fallback_date = to_mysql_date(score.get("firstSeenDate")) or run_date
    score_first_seen_date = resolve_job_posted_date(score, fallback_date)
    job_id = upsert_job(
        conn,
        source=source,
        run_id=run_id,
        run_date=fallback_date,
        job=score,
        title_override=score.get("title"),
    )
    sql = """
      INSERT INTO scores (
        run_id, job_id, score, suitability, recommendation, matched_reasons, gap_reasons,
        verdict, first_seen_date, skipped_reason, error, profile_hash, resume_path, raw_score
      )
      VALUES (%s, %s, %s, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), %s, %s, %s, %s, %s, %s, CAST(%s AS JSON))
      ON DUPLICATE KEY UPDATE
        score = VALUES(score),
        suitability = VALUES(suitability),
        recommendation = VALUES(recommendation),
        matched_reasons = VALUES(matched_reasons),
        gap_reasons = VALUES(gap_reasons),
        verdict = VALUES(verdict),
        first_seen_date = VALUES(first_seen_date),
        skipped_reason = VALUES(skipped_reason),
        error = VALUES(error),
        profile_hash = VALUES(profile_hash),
        resume_path = VALUES(resume_path),
        raw_score = VALUES(raw_score)
    """
    params = [
        run_id,
        job_id,
        _coalesce(score.get("score")),
        _coalesce(score.get("suitability")),
        _coalesce(score.get("recommendation")),
        json_param(_coalesce(score.get("matchedReasons"), [])),
        json_param(_coalesce(score.get("gapReasons"), [])),
        _coalesce(score.get("verdict")),
        score_first_seen_date,
        _coalesce(score.get("skippedReason")),
        _coalesce(score.get("error")),
        _coalesce(profile_hash),
        _coalesce(resume_path),
        json_param(score),
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)
    return job_id


def upsert_resume_score(conn, *, source, job_id, run_date, score, profile_hash, resume_path, report_file=None):
    """Upsert into the profile-keyed authoritative view (resume_scores).

    Keyed UNIQUE(profile_hash, job_id): one row per job per resume profile, so
    re-scoring under the same resume updates in place. This is the table the
    dashboard treats as the current-resume score; run-keyed `scores` stays the
    per-run history. db.py writes this on every daily run (db.mjs does NOT — it
    is intentionally outside the verify_db.py parity contract); the manual
    rescore importer writes the same table with the same key.
    """
    fallback_date = to_mysql_date(score.get("firstSeenDate")) or run_date
    score_first_seen_date = resolve_job_posted_date(score, fallback_date)
    sql = """
      INSERT INTO resume_scores (
        job_id, source, profile_hash, resume_path, score, suitability, recommendation,
        matched_reasons, gap_reasons, verdict, first_seen_date, matched_keywords,
        selection_reasons, report_file, raw_score
      )
      VALUES (%s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), %s, CAST(%s AS JSON))
      ON DUPLICATE KEY UPDATE
        source = VALUES(source),
        resume_path = VALUES(resume_path),
        score = VALUES(score),
        suitability = VALUES(suitability),
        recommendation = VALUES(recommendation),
        matched_reasons = VALUES(matched_reasons),
        gap_reasons = VALUES(gap_reasons),
        verdict = VALUES(verdict),
        first_seen_date = VALUES(first_seen_date),
        matched_keywords = VALUES(matched_keywords),
        selection_reasons = VALUES(selection_reasons),
        report_file = VALUES(report_file),
        raw_score = VALUES(raw_score)
    """
    params = [
        job_id,
        source,
        profile_hash,
        _coalesce(resume_path),
        _coalesce(score.get("score")),
        _coalesce(score.get("suitability")),
        _coalesce(score.get("recommendation")),
        json_param(_coalesce(score.get("matchedReasons"), [])),
        json_param(_coalesce(score.get("gapReasons"), [])),
        _coalesce(score.get("verdict")),
        score_first_seen_date,
        json_param(_coalesce(score.get("matchedKeywords"), [])),
        json_param(_coalesce(score.get("selectionReasons"), [])),
        _coalesce(report_file),
        json_param(score),
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)


def upsert_cancellation(conn, source, location_slug, run_id, canceled):
    job_id = find_job_id_by_jr(conn, source=source, jr=canceled.get("jr"))
    sql = """
      INSERT INTO cancellations (run_id, job_id, source, location_slug, canceled_key, jr, title)
      VALUES (%s, %s, %s, %s, %s, %s, %s)
      ON DUPLICATE KEY UPDATE
        job_id = VALUES(job_id),
        jr = VALUES(jr),
        title = VALUES(title)
    """
    params = [
        run_id,
        job_id,
        source,
        location_slug,
        cancellation_key(canceled),
        _coalesce(canceled.get("jr")),
        _coalesce(canceled.get("title")),
    ]
    with conn.cursor() as cur:
        cur.execute(sql, params)


def scored_keys(conn, *, source, profile_hash):
    """job_keys already scored under (profile_hash, source) in resume_scores.

    Single source of truth for "have we scored this job under the current
    resume?" — drives the dedup in daily.py's L3 step. Errored/skipped scores
    only land in resume_scores when they carry a non-null score and no error
    (see persist_daily_run), so a key being present here means "don't re-score".
    """
    if not profile_hash:
        return set()
    sql = """
      SELECT j.job_key
      FROM resume_scores rs
      JOIN jobs j ON j.id = rs.job_id
      WHERE rs.profile_hash = %s AND rs.source = %s
    """
    with conn.cursor() as cur:
        cur.execute(sql, [profile_hash, source])
        rows = cur.fetchall()
    return {row[0] for row in rows}


def fetch_scores_for_keys(conn, *, source, profile_hash, job_keys):
    """Map job_key -> score-shaped dict for keys present in resume_scores.

    Returns the latest scoring detail under the current resume, in the shape the
    renderers expect (matchedReasons, gapReasons, firstSeenDate, etc.). Callers
    merge this with the in-memory current snapshot to assemble rankedJobs for L4.
    """
    if not profile_hash or not job_keys:
        return {}
    placeholders = ",".join(["%s"] * len(job_keys))
    sql = f"""
      SELECT j.job_key, rs.score, rs.suitability, rs.recommendation,
             rs.matched_reasons, rs.gap_reasons, rs.verdict, rs.first_seen_date,
             rs.matched_keywords, rs.selection_reasons, rs.raw_score
      FROM resume_scores rs
      JOIN jobs j ON j.id = rs.job_id
      WHERE rs.profile_hash = %s AND rs.source = %s AND j.job_key IN ({placeholders})
    """
    with conn.cursor() as cur:
        cur.execute(sql, [profile_hash, source, *job_keys])
        rows = cur.fetchall()

    out = {}
    for row in rows:
        key, score, suitability, recommendation, matched, gap, verdict, first_seen, matched_kw, sel_reasons, raw = row
        # raw_score holds the original scoring payload — pull skippedReason / error from there
        # so we don't lose them across the ledger round-trip.
        try:
            raw_obj = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else (raw or {})
        except (ValueError, TypeError):
            raw_obj = {}
        out[key] = {
            "score": score,
            "suitability": suitability,
            "recommendation": recommendation,
            "matchedReasons": json.loads(matched) if isinstance(matched, (str, bytes, bytearray)) else (matched or []),
            "gapReasons": json.loads(gap) if isinstance(gap, (str, bytes, bytearray)) else (gap or []),
            "verdict": verdict,
            "firstSeenDate": first_seen.isoformat() if hasattr(first_seen, "isoformat") else first_seen,
            "matchedKeywords": json.loads(matched_kw) if isinstance(matched_kw, (str, bytes, bytearray)) else (matched_kw or []),
            "selectionReasons": json.loads(sel_reasons) if isinstance(sel_reasons, (str, bytes, bytearray)) else (sel_reasons or []),
            "skippedReason": raw_obj.get("skippedReason"),
        }
    return out


def persist_daily_run(
    conn,
    *,
    source=DEFAULT_SOURCE,
    location,
    run_date,
    current_jobs,
    report,
    snapshot_file=None,
    report_file=None,
    profile_cache_path=None,
):
    if not is_date_label(run_date):
        raise ValueError(f"MySQL persistence requires YYYY-MM-DD runDate, got: {run_date}")
    profile_hash = load_profile_hash(profile_cache_path)
    location_slug = slugify(location)

    conn.begin()
    try:
        run_id = upsert_run(
            conn,
            source=source,
            location=location,
            run_date=run_date,
            snapshot_file=snapshot_file,
            report_file=report_file,
            report=report,
        )
        for job in current_jobs or []:
            job_id = upsert_job(conn, source=source, run_id=run_id, run_date=run_date, job=job)
            upsert_job_snapshot(conn, source=source, run_id=run_id, job_id=job_id, job=job)
        resume_path = _coalesce(report.get("resumePath"))
        resume_score_count = 0
        for score in report.get("rankedJobs") or []:
            job_id = upsert_score(
                conn,
                source=source,
                run_id=run_id,
                run_date=run_date,
                score=score,
                profile_hash=profile_hash,
                resume_path=resume_path,
            )
            # Mirror the daily score into the profile-keyed authoritative view.
            # Only real model scores (numeric, no error) belong there — skip
            # errored rows; require a profile_hash since it's part of the key.
            if profile_hash and score.get("score") is not None and not score.get("error"):
                upsert_resume_score(
                    conn,
                    source=source,
                    job_id=job_id,
                    run_date=run_date,
                    score=score,
                    profile_hash=profile_hash,
                    resume_path=resume_path,
                    report_file=report_file,
                )
                resume_score_count += 1
        for canceled in report.get("canceledJobs") or []:
            upsert_cancellation(conn, source=source, location_slug=location_slug, run_id=run_id, canceled=canceled)
        conn.commit()
        return {
            "run_id": run_id,
            "job_snapshots": len(current_jobs or []),
            "scores": len(report.get("rankedJobs") or []),
            "resume_scores": resume_score_count,
            "cancellations": len(report.get("canceledJobs") or []),
        }
    except Exception:
        conn.rollback()
        raise


def persist_daily_run_from_env(
    *,
    source=DEFAULT_SOURCE,
    location,
    run_date,
    current_jobs,
    report,
    snapshot_file=None,
    report_file=None,
    profile_cache_path=None,
):
    if not has_mysql_config():
        raise RuntimeError(
            "MySQL is required for the daily run (ledger is the source of truth for scored jobs). "
            "Set MYSQL_USER/MYSQL_SOCKET_PATH or MYSQL_DSN."
        )
    ensure_database_and_schema_from_env()
    conn = create_mysql_connection_from_env()
    try:
        return persist_daily_run(
            conn,
            source=source,
            location=location,
            run_date=run_date,
            current_jobs=current_jobs,
            report=report,
            snapshot_file=snapshot_file,
            report_file=report_file,
            profile_cache_path=profile_cache_path,
        )
    finally:
        conn.close()
