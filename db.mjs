import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import mysql from 'mysql2/promise';

const MODULE_PATH = fileURLToPath(import.meta.url);
const __dirname = path.dirname(MODULE_PATH);
const DEFAULT_DATABASE = 'nvidia_jobs_monitor';
const DEFAULT_SOURCE = 'nvidia';

function quoteIdentifier(value) {
  if (!/^[A-Za-z0-9_]+$/.test(value)) {
    throw new Error(`Unsafe MySQL identifier: ${value}`);
  }
  return `\`${value}\``;
}

function parsePort(value) {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 3306;
}

function decodeUrlPart(value) {
  return value ? decodeURIComponent(value) : undefined;
}

export function mysqlConfigFromEnv(env = process.env, { includeDatabase = true, multipleStatements = false } = {}) {
  const database = env.MYSQL_DATABASE || DEFAULT_DATABASE;

  if (env.MYSQL_DSN) {
    const url = new URL(env.MYSQL_DSN);
    const dsnDatabase = url.pathname ? decodeURIComponent(url.pathname.replace(/^\//, '')) : '';
    const socketPath = url.searchParams.get('socketPath') || url.searchParams.get('socket');
    const config = {
      user: decodeUrlPart(url.username),
      password: decodeUrlPart(url.password),
      multipleStatements,
      charset: 'utf8mb4',
    };
    if (socketPath) {
      config.socketPath = socketPath;
    } else {
      config.host = url.hostname || 'localhost';
      config.port = url.port ? parsePort(url.port) : 3306;
    }
    if (includeDatabase) config.database = dsnDatabase || database;
    return { configured: true, database: dsnDatabase || database, config };
  }

  const configured = Boolean(
    env.MYSQL_USER ||
      env.MYSQL_PASSWORD ||
      env.MYSQL_HOST ||
      env.MYSQL_PORT ||
      env.MYSQL_SOCKET_PATH ||
      env.MYSQL_DATABASE,
  );
  if (!configured) return { configured: false, database, config: null };

  const config = {
    user: env.MYSQL_USER || 'root',
    password: env.MYSQL_PASSWORD || undefined,
    multipleStatements,
    charset: 'utf8mb4',
  };
  if (env.MYSQL_SOCKET_PATH) {
    config.socketPath = env.MYSQL_SOCKET_PATH;
  } else {
    config.host = env.MYSQL_HOST || 'localhost';
    config.port = parsePort(env.MYSQL_PORT);
  }
  if (includeDatabase) config.database = database;
  return { configured: true, database, config };
}

export function hasMysqlConfig(env = process.env) {
  return mysqlConfigFromEnv(env).configured;
}

export async function createMysqlConnectionFromEnv(options = {}) {
  const { configured, config } = mysqlConfigFromEnv(process.env, options);
  if (!configured) throw new Error('MySQL is not configured. Set MYSQL_USER/MYSQL_SOCKET_PATH or MYSQL_DSN.');
  return mysql.createConnection(config);
}

export async function ensureDatabaseAndSchemaFromEnv({
  schemaPath = path.join(__dirname, 'db', 'schema.sql'),
} = {}) {
  const base = mysqlConfigFromEnv(process.env, { includeDatabase: false });
  if (!base.configured) throw new Error('MySQL is not configured. Set MYSQL_USER/MYSQL_SOCKET_PATH or MYSQL_DSN.');

  const admin = await mysql.createConnection(base.config);
  try {
    await admin.query(
      `CREATE DATABASE IF NOT EXISTS ${quoteIdentifier(base.database)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci`,
    );
  } finally {
    await admin.end();
  }

  const db = await mysql.createConnection(mysqlConfigFromEnv(process.env, { multipleStatements: true }).config);
  try {
    await db.query(fs.readFileSync(schemaPath, 'utf8'));
  } finally {
    await db.end();
  }
}

export function slugify(value) {
  return String(value).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
}

export function isDateLabel(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(String(value));
}

export function jobKey(job) {
  return String(job.id ?? job.jr ?? `${job.name ?? job.title ?? ''}|${job.postedTs ?? job.datePosted ?? job.posted ?? ''}`);
}

function normalizeForJson(value) {
  if (Array.isArray(value)) return value.map(normalizeForJson);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map((key) => [key, normalizeForJson(value[key])]),
    );
  }
  return value ?? null;
}

export function stableStringify(value) {
  return JSON.stringify(normalizeForJson(value));
}

export function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

export function jobContentForHash(job) {
  return {
    id: job.id ?? null,
    jr: job.jr ?? null,
    name: job.name ?? job.title ?? null,
    locations: job.locations ?? [],
    department: job.department ?? null,
    workLocationOption: job.workLocationOption ?? null,
    postedTs: job.postedTs ?? null,
    creationTs: job.creationTs ?? null,
    link: job.link ?? null,
    datePosted: job.datePosted ?? job.posted ?? null,
    validThrough: job.validThrough ?? null,
    employmentType: job.employmentType ?? null,
    description: job.description ?? '',
    summary: job.summary ?? '',
    responsibilities: job.responsibilities ?? [],
    requirements: job.requirements ?? [],
    preferred: job.preferred ?? [],
    detailError: job.detailError ?? null,
  };
}

export function jobContentHash(job) {
  return sha256(stableStringify(jobContentForHash(job)));
}

export function cancellationKey(canceled) {
  return sha256(stableStringify({ jr: canceled.jr ?? null, title: canceled.title ?? null }));
}

export function toMysqlDate(value) {
  if (!value) return null;
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const match = text.match(/^(\d{4}-\d{2}-\d{2})/);
  return match ? match[1] : null;
}

export function toMysqlDateTime(value) {
  if (!value) return null;
  const text = String(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return `${text} 00:00:00`;
  const match = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})/);
  return match ? `${match[1]} ${match[2]}` : null;
}

function epochToMysqlDate(value) {
  if (value == null || value === '') return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) return null;
  const ms = number > 10_000_000_000 ? number : number * 1000;
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString().slice(0, 10);
}

export function resolveJobPostedDate(job = {}, fallbackDate = null) {
  return (
    toMysqlDate(job.datePosted ?? job.posted) ??
    epochToMysqlDate(job.posted) ??
    epochToMysqlDate(job.postedTs) ??
    epochToMysqlDate(job.creationTs) ??
    toMysqlDate(fallbackDate)
  );
}

export function resolveJobPostedDateTime(job = {}, fallbackDate = null) {
  const direct = toMysqlDateTime(job.datePosted ?? job.posted);
  if (direct) return direct;
  const postedDate = resolveJobPostedDate(job, fallbackDate);
  return postedDate ? `${postedDate} 00:00:00` : null;
}

function jsonParam(value) {
  return JSON.stringify(value ?? null);
}

function intParam(value, fallback = 0) {
  const parsed = Number.parseInt(value ?? '', 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function loadProfileHash(profileCachePath) {
  if (!profileCachePath || !fs.existsSync(profileCachePath)) return null;
  try {
    const profile = JSON.parse(fs.readFileSync(profileCachePath, 'utf8'));
    return profile.resumeHash ?? null;
  } catch {
    return null;
  }
}

async function upsertRun(connection, { source, location, runDate, snapshotFile, reportFile, report }) {
  const locationSlug = slugify(location);
  const [result] = await connection.execute(
    `
      INSERT INTO runs (
        source, location, location_slug, run_date, snapshot_file, report_file, previous_snapshot,
        resume_path, baseline_created, status, current_job_count, added_count, canceled_count,
        ranked_job_count, backlog_count, deferred_score_count, score_error_count,
        profile_highlights, fit_summary, scored_dates, deferred_dates, raw_report
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), CAST(? AS JSON), CAST(? AS JSON), CAST(? AS JSON))
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
    `,
    [
      source,
      location,
      locationSlug,
      runDate,
      snapshotFile ? path.basename(snapshotFile) : null,
      reportFile ? path.basename(reportFile) : null,
      report.previousSnapshot ?? null,
      report.resumePath ?? null,
      Boolean(report.baselineCreated),
      'imported',
      intParam(report.currentJobCount),
      intParam(report.addedCount),
      intParam(report.canceledCount),
      intParam(report.rankedJobCount),
      intParam(report.backlogCount),
      intParam(report.deferredScoreCount),
      intParam(report.scoreErrorCount),
      jsonParam(report.profileHighlights ?? []),
      jsonParam(report.fitSummary ?? {}),
      jsonParam(report.scoredDates ?? []),
      jsonParam(report.deferredDates ?? []),
      jsonParam(report),
    ],
  );
  return result.insertId;
}

async function upsertJob(connection, { source, runId, runDate, job, titleOverride = null }) {
  const key = jobKey(job);
  const hash = job.description || job.summary || job.requirements || job.responsibilities
    ? jobContentHash(job)
    : null;
  const title = titleOverride ?? job.name ?? job.title ?? null;
  const firstSeenDate = resolveJobPostedDate(job, runDate);
  const [result] = await connection.execute(
    `
      INSERT INTO jobs (
        source, job_key, external_id, jr, title, link, department,
        first_seen_date, last_seen_date, latest_content_hash, latest_seen_run_id
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    `,
    [
      source,
      key,
      job.id != null ? String(job.id) : null,
      job.jr ?? null,
      title,
      job.link ?? null,
      job.department ?? null,
      firstSeenDate,
      runDate,
      hash,
      runId,
    ],
  );
  return result.insertId;
}

async function upsertJobSnapshot(connection, { source, runId, jobId, job }) {
  const key = jobKey(job);
  const hash = jobContentHash(job);
  await connection.execute(
    `
      INSERT INTO job_snapshots (
        run_id, job_id, source, job_key, title, jr, external_id, link, locations,
        department, work_location_option, posted_ts, creation_ts, date_posted, valid_through,
        employment_type, description, summary, responsibilities, requirements_json, preferred,
        detail_error, content_hash, raw_json, active
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), CAST(? AS JSON), ?, ?, CAST(? AS JSON), ?)
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
    `,
    [
      runId,
      jobId,
      source,
      key,
      job.name ?? job.title ?? null,
      job.jr ?? null,
      job.id != null ? String(job.id) : null,
      job.link ?? null,
      jsonParam(job.locations ?? []),
      job.department ?? null,
      job.workLocationOption ?? null,
      job.postedTs ?? null,
      job.creationTs ?? null,
      resolveJobPostedDateTime(job),
      toMysqlDateTime(job.validThrough),
      job.employmentType ?? null,
      job.description ?? null,
      job.summary ?? null,
      jsonParam(job.responsibilities ?? []),
      jsonParam(job.requirements ?? []),
      jsonParam(job.preferred ?? []),
      job.detailError ?? null,
      hash,
      jsonParam(job),
      true,
    ],
  );
}

async function findJobIdByJr(connection, { source, jr }) {
  if (!jr) return null;
  const [rows] = await connection.execute(
    'SELECT id FROM jobs WHERE source = ? AND jr = ? ORDER BY last_seen_date DESC, id DESC LIMIT 1',
    [source, jr],
  );
  return rows[0]?.id ?? null;
}

async function upsertScore(connection, { source, runId, runDate, score, profileHash, resumePath }) {
  const fallbackDate = toMysqlDate(score.firstSeenDate) ?? runDate;
  const scoreFirstSeenDate = resolveJobPostedDate(score, fallbackDate);
  const jobId = await upsertJob(connection, {
    source,
    runId,
    runDate: fallbackDate,
    job: score,
    titleOverride: score.title,
  });
  await connection.execute(
    `
      INSERT INTO scores (
        run_id, job_id, score, suitability, recommendation, matched_reasons, gap_reasons,
        verdict, first_seen_date, skipped_reason, error, profile_hash, resume_path, raw_score
      )
      VALUES (?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), ?, ?, ?, ?, ?, ?, CAST(? AS JSON))
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
    `,
    [
      runId,
      jobId,
      score.score ?? null,
      score.suitability ?? null,
      score.recommendation ?? null,
      jsonParam(score.matchedReasons ?? []),
      jsonParam(score.gapReasons ?? []),
      score.verdict ?? null,
      scoreFirstSeenDate,
      score.skippedReason ?? null,
      score.error ?? null,
      profileHash ?? null,
      resumePath ?? null,
      jsonParam(score),
    ],
  );
}

async function upsertCancellation(connection, { source, locationSlug, runId, canceled }) {
  const jobId = await findJobIdByJr(connection, { source, jr: canceled.jr });
  await connection.execute(
    `
      INSERT INTO cancellations (run_id, job_id, source, location_slug, canceled_key, jr, title)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON DUPLICATE KEY UPDATE
        job_id = VALUES(job_id),
        jr = VALUES(jr),
        title = VALUES(title)
    `,
    [runId, jobId, source, locationSlug, cancellationKey(canceled), canceled.jr ?? null, canceled.title ?? null],
  );
}

export async function persistDailyRun(connection, {
  source = DEFAULT_SOURCE,
  location,
  runDate,
  currentJobs,
  report,
  snapshotFile = null,
  reportFile = null,
  profileCachePath = null,
}) {
  if (!isDateLabel(runDate)) throw new Error(`MySQL persistence requires YYYY-MM-DD runDate, got: ${runDate}`);
  const profileHash = loadProfileHash(profileCachePath);
  const locationSlug = slugify(location);

  await connection.beginTransaction();
  try {
    const runId = await upsertRun(connection, { source, location, runDate, snapshotFile, reportFile, report });
    for (const job of currentJobs ?? []) {
      const jobId = await upsertJob(connection, { source, runId, runDate, job });
      await upsertJobSnapshot(connection, { source, runId, jobId, job });
    }
    for (const score of report.rankedJobs ?? []) {
      await upsertScore(connection, {
        source,
        runId,
        runDate,
        score,
        profileHash,
        resumePath: report.resumePath ?? null,
      });
    }
    for (const canceled of report.canceledJobs ?? []) {
      await upsertCancellation(connection, { source, locationSlug, runId, canceled });
    }
    await connection.commit();
    return { runId, jobSnapshots: currentJobs?.length ?? 0, scores: report.rankedJobs?.length ?? 0, cancellations: report.canceledJobs?.length ?? 0 };
  } catch (error) {
    await connection.rollback();
    throw error;
  }
}

export async function persistDailyRunFromEnv(options) {
  if (!hasMysqlConfig()) return { skipped: true, reason: 'mysql-not-configured' };
  await ensureDatabaseAndSchemaFromEnv();
  const connection = await createMysqlConnectionFromEnv();
  try {
    return await persistDailyRun(connection, options);
  } finally {
    await connection.end();
  }
}
