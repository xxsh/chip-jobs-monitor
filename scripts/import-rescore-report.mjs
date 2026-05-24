#!/usr/bin/env node
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import { createMysqlConnectionFromEnv } from '../db.mjs';

const MODULE_PATH = fileURLToPath(import.meta.url);
const ROOT = path.dirname(path.dirname(MODULE_PATH));
const DEFAULT_REPORT = path.join(ROOT, 'reports', '2026-05-23_expanded-rescore.json');
const PROFILE_CACHE = path.join(ROOT, 'scorer', 'cache', 'profile_latest.json');

function setLocalMysqlDefaults() {
  process.env.MYSQL_USER ||= 'root';
  process.env.MYSQL_SOCKET_PATH ||= '/tmp/mysql.sock';
  process.env.MYSQL_DATABASE ||= 'nvidia_jobs_monitor';
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function jsonParam(value) {
  return JSON.stringify(value ?? null);
}

function stringOrNull(value) {
  if (value === undefined || value === null || value === '') return null;
  return String(value);
}

async function findJobId(connection, row) {
  const source = stringOrNull(row.source);
  const jr = stringOrNull(row.jr);
  const externalId = stringOrNull(row.id);
  if (!source) return null;

  const [rows] = await connection.execute(
    `
      SELECT id
      FROM jobs
      WHERE source = ?
        AND (
          (? IS NOT NULL AND jr = ?)
          OR (? IS NOT NULL AND external_id = ?)
          OR (? IS NOT NULL AND job_key = ?)
        )
      ORDER BY last_seen_date DESC, id DESC
      LIMIT 1
    `,
    [source, jr, jr, externalId, externalId, externalId, externalId],
  );
  return rows[0]?.id ?? null;
}

async function upsertResumeScore(connection, { row, jobId, profile, reportFile }) {
  await connection.execute(
    `
      INSERT INTO resume_scores (
        job_id, source, profile_hash, resume_path, score, suitability, recommendation,
        matched_reasons, gap_reasons, verdict, first_seen_date, matched_keywords,
        selection_reasons, report_file, raw_score
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON), ?, ?, CAST(? AS JSON), CAST(? AS JSON), ?, CAST(? AS JSON))
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
    `,
    [
      jobId,
      stringOrNull(row.source),
      profile.resumeHash,
      stringOrNull(profile.resumePath),
      Number(row.score),
      stringOrNull(row.suitability),
      stringOrNull(row.recommendation),
      jsonParam(row.matchedReasons ?? []),
      jsonParam(row.gapReasons ?? []),
      stringOrNull(row.verdict),
      stringOrNull(row.firstSeenDate),
      jsonParam(row.matchedKeywords ?? []),
      jsonParam(row.selectionReasons ?? []),
      reportFile,
      jsonParam(row),
    ],
  );
}

async function main() {
  setLocalMysqlDefaults();
  const reportFile = path.resolve(process.argv[2] || DEFAULT_REPORT);
  const report = readJson(reportFile);
  const profile = readJson(PROFILE_CACHE);

  if (!profile.resumeHash) {
    throw new Error(`profile cache missing resumeHash: ${PROFILE_CACHE}`);
  }
  if (!Array.isArray(report.results)) {
    throw new Error(`report missing results[]: ${reportFile}`);
  }

  const connection = await createMysqlConnectionFromEnv();
  const missing = [];
  let imported = 0;
  try {
    await connection.beginTransaction();
    for (const row of report.results) {
      if (row.error || row.score === undefined || row.score === null) continue;
      const jobId = await findJobId(connection, row);
      if (!jobId) {
        missing.push({ source: row.source, jr: row.jr, id: row.id, title: row.title });
        continue;
      }
      await upsertResumeScore(connection, { row, jobId, profile, reportFile });
      imported += 1;
    }
    if (missing.length) {
      throw new Error(`could not match ${missing.length} job(s): ${JSON.stringify(missing.slice(0, 8))}`);
    }
    await connection.commit();
  } catch (error) {
    await connection.rollback();
    throw error;
  } finally {
    await connection.end();
  }

  console.error(`Imported ${imported} resume scores for profile ${profile.resumeHash}`);
}

try {
  await main();
} catch (error) {
  console.error(`FATAL import-rescore-report failed: ${error.message}`);
  process.exit(1);
}
