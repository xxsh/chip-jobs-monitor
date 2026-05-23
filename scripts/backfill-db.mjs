#!/usr/bin/env node
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

import {
  createMysqlConnectionFromEnv,
  ensureDatabaseAndSchemaFromEnv,
  persistDailyRun,
  slugify,
} from '../db.mjs';

const MODULE_PATH = fileURLToPath(import.meta.url);
const ROOT = path.dirname(path.dirname(MODULE_PATH));
const SNAPSHOT_DIR = path.join(ROOT, 'snapshots');
const REPORT_DIR = path.join(ROOT, 'reports');
const PROFILE_CACHE = path.join(ROOT, 'scorer', 'cache', 'profile_latest.json');
const DRY_RUN = process.argv.includes('--dry-run');

function parseDatedSnapshot(file) {
  const match = file.match(/^(\d{4}-\d{2}-\d{2})_(.+)\.json$/);
  if (!match) return null;
  return { date: match[1], slug: match[2], file };
}

function loadJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function minimalReport({ date, slug, jobs, snapshotFile }) {
  return {
    date,
    location: slug,
    currentSnapshot: snapshotFile,
    previousSnapshot: null,
    baselineCreated: true,
    currentJobCount: jobs.length,
    addedCount: 0,
    canceledCount: 0,
    canceledJobs: [],
    fitSummary: { strongFit: 0, goodFit: 0, possibleStretch: 0, lowFit: 0 },
    backlogCount: 0,
    deferredScoreCount: 0,
    scoreErrorCount: 0,
    rankedJobCount: 0,
    rankedJobs: [],
    profileHighlights: [],
    scoredDates: [],
    deferredDates: [],
  };
}

function listEntries() {
  return fs
    .readdirSync(SNAPSHOT_DIR)
    .map(parseDatedSnapshot)
    .filter(Boolean)
    .sort((a, b) => `${a.date}_${a.slug}`.localeCompare(`${b.date}_${b.slug}`));
}

const entries = listEntries();
let snapshotRows = 0;
let scoreRows = 0;
let cancellationRows = 0;

if (DRY_RUN) {
  for (const entry of entries) {
    const jobs = loadJson(path.join(SNAPSHOT_DIR, entry.file));
    const reportFile = path.join(REPORT_DIR, `${entry.date}_${entry.slug}.json`);
    const report = fs.existsSync(reportFile) ? loadJson(reportFile) : minimalReport({ date: entry.date, slug: entry.slug, jobs, snapshotFile: entry.file });
    snapshotRows += jobs.length;
    scoreRows += report.rankedJobs?.length ?? 0;
    cancellationRows += report.canceledJobs?.length ?? 0;
  }
  console.error(`Dry run: ${entries.length} runs, ${snapshotRows} job snapshots, ${scoreRows} scores, ${cancellationRows} cancellations.`);
  process.exit(0);
}

try {
  await ensureDatabaseAndSchemaFromEnv();
  const connection = await createMysqlConnectionFromEnv();
  try {
    for (const entry of entries) {
      const snapshotFile = path.join(SNAPSHOT_DIR, entry.file);
      const jobs = loadJson(snapshotFile);
      const reportFile = path.join(REPORT_DIR, `${entry.date}_${entry.slug}.json`);
      const report = fs.existsSync(reportFile)
        ? loadJson(reportFile)
        : minimalReport({ date: entry.date, slug: entry.slug, jobs, snapshotFile: entry.file });
      const location = report.location || entry.slug;
      const result = await persistDailyRun(connection, {
        location,
        runDate: entry.date,
        currentJobs: jobs,
        report,
        snapshotFile,
        reportFile: fs.existsSync(reportFile) ? reportFile : null,
        profileCachePath: PROFILE_CACHE,
      });
      snapshotRows += result.jobSnapshots;
      scoreRows += result.scores;
      cancellationRows += result.cancellations;
      console.error(`Imported ${entry.date}_${slugify(location)}: ${result.jobSnapshots} snapshots, ${result.scores} scores.`);
    }
  } finally {
    await connection.end();
  }
  console.error(`Backfill complete: ${entries.length} runs, ${snapshotRows} job snapshots, ${scoreRows} scores, ${cancellationRows} cancellations.`);
} catch (error) {
  console.error(`FATAL db:backfill failed: ${error.message}`);
  process.exit(1);
}
