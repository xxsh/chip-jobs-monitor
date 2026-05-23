#!/usr/bin/env node
import {
  createMysqlConnectionFromEnv,
  resolveJobPostedDate,
  resolveJobPostedDateTime,
  toMysqlDate,
} from '../db.mjs';

const DRY_RUN = process.argv.includes('--dry-run');

function setLocalMysqlDefaults() {
  process.env.MYSQL_USER ||= 'root';
  process.env.MYSQL_SOCKET_PATH ||= '/tmp/mysql.sock';
  process.env.MYSQL_DATABASE ||= 'nvidia_jobs_monitor';
}

function parseJsonCell(value) {
  if (!value) return {};
  if (typeof value === 'object') return value;
  try {
    return JSON.parse(value);
  } catch {
    return {};
  }
}

function rememberEarliest(map, key, date) {
  if (!date) return;
  const current = map.get(key);
  if (!current || date < current) map.set(key, date);
}

setLocalMysqlDefaults();
const connection = await createMysqlConnectionFromEnv();

let snapshotUpdates = 0;
let jobUpdates = 0;
let scoreUpdates = 0;

try {
  const [snapshots] = await connection.execute(`
    SELECT
      js.id,
      js.run_id AS runId,
      js.job_id AS jobId,
      DATE_FORMAT(r.run_date, '%Y-%m-%d') AS runDate,
      js.raw_json AS rawJson
    FROM job_snapshots js
    JOIN runs r ON r.id = js.run_id
    ORDER BY js.id
  `);

  const jobDates = new Map();
  const snapshotDates = new Map();

  if (!DRY_RUN) await connection.beginTransaction();

  try {
    for (const row of snapshots) {
      const job = parseJsonCell(row.rawJson);
      const postedDate = resolveJobPostedDate(job, row.runDate);
      const postedDateTime = resolveJobPostedDateTime(job, row.runDate);
      if (!postedDate) continue;

      rememberEarliest(jobDates, row.jobId, postedDate);
      snapshotDates.set(`${row.runId}:${row.jobId}`, postedDate);

      if (!DRY_RUN && postedDateTime) {
        const [result] = await connection.execute(
          `
            UPDATE job_snapshots
            SET date_posted = ?
            WHERE id = ? AND (date_posted IS NULL OR DATE(date_posted) <> ?)
          `,
          [postedDateTime, row.id, postedDate],
        );
        snapshotUpdates += result.affectedRows;
      } else if (DRY_RUN) {
        snapshotUpdates += 1;
      }
    }

    for (const [jobId, postedDate] of jobDates.entries()) {
      if (!DRY_RUN) {
        const [result] = await connection.execute(
          `
            UPDATE jobs
            SET first_seen_date = ?
            WHERE id = ? AND (first_seen_date IS NULL OR first_seen_date <> ?)
          `,
          [postedDate, jobId, postedDate],
        );
        jobUpdates += result.affectedRows;
      } else {
        jobUpdates += 1;
      }
    }

    const [scores] = await connection.execute(`
      SELECT
        id,
        run_id AS runId,
        job_id AS jobId,
        DATE_FORMAT(first_seen_date, '%Y-%m-%d') AS firstSeenDate,
        raw_score AS rawScore
      FROM scores
      ORDER BY id
    `);

    for (const score of scores) {
      const rawScore = parseJsonCell(score.rawScore);
      const fallbackDate = toMysqlDate(score.firstSeenDate);
      const postedDate = snapshotDates.get(`${score.runId}:${score.jobId}`)
        || resolveJobPostedDate(rawScore, fallbackDate);
      if (!postedDate) continue;

      if (!DRY_RUN) {
        const [result] = await connection.execute(
          `
            UPDATE scores
            SET first_seen_date = ?
            WHERE id = ? AND (first_seen_date IS NULL OR first_seen_date <> ?)
          `,
          [postedDate, score.id, postedDate],
        );
        scoreUpdates += result.affectedRows;
      } else {
        scoreUpdates += 1;
      }
    }

    if (!DRY_RUN) await connection.commit();
  } catch (error) {
    if (!DRY_RUN) await connection.rollback();
    throw error;
  }
} finally {
  await connection.end();
}

const prefix = DRY_RUN ? 'Dry run' : 'Backfill complete';
console.error(`${prefix}: ${snapshotUpdates} snapshot rows, ${jobUpdates} job rows, ${scoreUpdates} score rows.`);
