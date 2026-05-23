#!/usr/bin/env node
import fs from 'fs';
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';

import { createMysqlConnectionFromEnv } from '../db.mjs';

const MODULE_PATH = fileURLToPath(import.meta.url);
const ROOT = path.dirname(path.dirname(MODULE_PATH));
const WEB_DIR = path.join(ROOT, 'web');
const PORT = Number.parseInt(process.env.DASHBOARD_PORT || '4173', 10);

const SOURCE_LABELS = {
  amd: 'AMD',
  arm: 'Arm',
  intel: 'Intel',
  nvidia: 'NVIDIA',
};

const MIME_TYPES = {
  '.css': 'text/css; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
};

function setLocalMysqlDefaults() {
  process.env.MYSQL_USER ||= 'root';
  process.env.MYSQL_SOCKET_PATH ||= '/tmp/mysql.sock';
  process.env.MYSQL_DATABASE ||= 'nvidia_jobs_monitor';
}

function sendJson(res, status, data) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(data));
}

function sendFile(res, filePath) {
  if (!filePath.startsWith(WEB_DIR)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }
  fs.readFile(filePath, (error, data) => {
    if (error) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }
    res.writeHead(200, { 'Content-Type': MIME_TYPES[path.extname(filePath)] || 'application/octet-stream' });
    res.end(data);
  });
}

function sourceLabel(source) {
  return SOURCE_LABELS[source] || String(source || 'unknown')
    .split(/[-_]+/)
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(' ');
}

function numberValue(value) {
  return Number(value || 0);
}

function emptyFitBucket(date) {
  return {
    date,
    strongFit: 0,
    goodFit: 0,
    possibleStretch: 0,
    lowFit: 0,
  };
}

function groupBySource(runs) {
  const bySource = new Map();
  for (const row of runs) {
    if (!bySource.has(row.source)) bySource.set(row.source, []);
    bySource.get(row.source).push(row);
  }
  return bySource;
}

function aggregateRuns(runs) {
  const byDate = new Map();
  for (const row of runs) {
    if (!byDate.has(row.date)) {
      byDate.set(row.date, {
        date: row.date,
        location: row.location,
        activeJobs: 0,
        added: 0,
        canceled: 0,
        scored: 0,
        queued: 0,
        scoreErrors: 0,
        sourceCount: 0,
        sources: [],
      });
    }
    const bucket = byDate.get(row.date);
    bucket.activeJobs += numberValue(row.activeJobs);
    bucket.added += numberValue(row.added);
    bucket.canceled += numberValue(row.canceled);
    bucket.scored += numberValue(row.scored);
    bucket.queued += numberValue(row.queued);
    bucket.scoreErrors += numberValue(row.scoreErrors);
    bucket.sourceCount += 1;
    bucket.sources.push(row.source);
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function postedCountsBySourceDate(postedAddRows) {
  return new Map(postedAddRows.map((row) => [`${row.source}:${row.date}`, numberValue(row.count)]));
}

function buildSourceSummaries(runs, scoreStats, postedAddRows) {
  const bestScoreBySource = new Map(scoreStats.map((row) => [row.source, numberValue(row.bestScore)]));
  const postedBySourceDate = postedCountsBySourceDate(postedAddRows);
  return [...groupBySource(runs).entries()]
    .map(([source, sourceRuns]) => {
      const latest = sourceRuns.at(-1) ?? {};
      const sourcePostedRows = postedAddRows.filter((row) => row.source === source);
      const previous = sourceRuns.at(-2) ?? null;
      return {
        source,
        display: sourceLabel(source),
        location: latest.location ?? null,
        firstDate: sourceRuns[0]?.date ?? null,
        latestDate: latest.date ?? null,
        daysTracked: sourceRuns.length,
        activeJobs: numberValue(latest.activeJobs),
        activeDelta: previous
          ? numberValue(latest.activeJobs) - numberValue(previous.activeJobs)
          : numberValue(latest.activeJobs),
        latestAdded: postedBySourceDate.get(`${source}:${latest.date}`) || 0,
        latestCanceled: numberValue(latest.canceled),
        latestScored: numberValue(latest.scored),
        queued: numberValue(latest.queued),
        scoreErrors: numberValue(latest.scoreErrors),
        totalAdded: sourcePostedRows.reduce((sum, row) => sum + numberValue(row.count), 0),
        totalCanceled: sourceRuns.reduce((sum, row) => sum + numberValue(row.canceled), 0),
        totalScored: sourceRuns.reduce((sum, row) => sum + numberValue(row.scored), 0),
        bestScore: bestScoreBySource.get(source) || null,
        isNewSource: sourceRuns.length === 1,
      };
    })
    .sort((a, b) => b.activeJobs - a.activeJobs || a.display.localeCompare(b.display));
}

function buildMovementSeries(runs, postedAddRows) {
  const aggregate = aggregateRuns(runs);
  if (!aggregate.length) return [];

  const firstDate = aggregate[0].date;
  const latestDate = aggregate.at(-1).date;
  const runByDate = new Map(aggregate.map((row) => [row.date, row]));
  const postedByDate = new Map();
  for (const row of postedAddRows) {
    if (!row.date || row.date < firstDate || row.date > latestDate) continue;
    postedByDate.set(row.date, (postedByDate.get(row.date) || 0) + numberValue(row.count));
  }

  const dates = [...new Set([...runByDate.keys(), ...postedByDate.keys()])].sort();
  let lastRun = aggregate[0];
  return dates.map((date) => {
    const run = runByDate.get(date);
    if (run) lastRun = run;
    return {
      ...(run || {
        date,
        location: lastRun.location,
        activeJobs: lastRun.activeJobs,
        canceled: 0,
        scored: 0,
        queued: lastRun.queued,
        scoreErrors: 0,
        sourceCount: lastRun.sourceCount,
        sources: lastRun.sources,
      }),
      added: postedByDate.get(date) || 0,
    };
  });
}

function buildFitSeries(fitRows) {
  const dateToFit = new Map();
  for (const row of fitRows) {
    if (!dateToFit.has(row.date)) dateToFit.set(row.date, emptyFitBucket(row.date));
    const bucket = dateToFit.get(row.date);
    if (row.suitability === 'Strong fit') bucket.strongFit += numberValue(row.count);
    else if (row.suitability === 'Good fit') bucket.goodFit += numberValue(row.count);
    else if (row.suitability === 'Possible stretch') bucket.possibleStretch += numberValue(row.count);
    else bucket.lowFit += numberValue(row.count);
  }
  return [...dateToFit.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function decorateRows(rows) {
  return rows.map((row) => ({
    ...row,
    sourceDisplay: sourceLabel(row.source),
  }));
}

async function queryTrends() {
  setLocalMysqlDefaults();
  const connection = await createMysqlConnectionFromEnv();
  try {
    const [runs] = await connection.execute(`
      SELECT
        source,
        DATE_FORMAT(run_date, '%Y-%m-%d') AS date,
        location,
        current_job_count AS activeJobs,
        added_count AS added,
        canceled_count AS canceled,
        ranked_job_count AS scored,
        deferred_score_count AS queued,
        score_error_count AS scoreErrors
      FROM runs
      ORDER BY run_date, source
    `);

    const [postedAddRows] = await connection.execute(`
      SELECT source, DATE_FORMAT(first_seen_date, '%Y-%m-%d') AS date, COUNT(*) AS count
      FROM jobs
      WHERE first_seen_date IS NOT NULL
      GROUP BY source, first_seen_date
      ORDER BY first_seen_date, source
    `);

    const [fitRows] = await connection.execute(`
      SELECT
        source,
        date,
        suitability,
        COUNT(*) AS count
      FROM (
        SELECT
          r.source,
          DATE_FORMAT(COALESCE(s.first_seen_date, j.first_seen_date, r.run_date), '%Y-%m-%d') AS date,
          s.suitability
        FROM scores s
        JOIN runs r ON r.id = s.run_id
        JOIN jobs j ON j.id = s.job_id
        WHERE s.score IS NOT NULL
      ) scored_by_posted_date
      GROUP BY source, date, suitability
      ORDER BY date, source
    `);

    const [scoreStats] = await connection.execute(`
      SELECT r.source, MAX(s.score) AS bestScore, COUNT(s.score) AS scoredRows
      FROM scores s
      JOIN runs r ON r.id = s.run_id
      GROUP BY r.source
    `);

    const [topRoles] = await connection.execute(`
      SELECT
        r.source,
        DATE_FORMAT(COALESCE(s.first_seen_date, j.first_seen_date, r.run_date), '%Y-%m-%d') AS date,
        j.jr,
        j.title,
        j.link,
        j.department,
        s.score,
        s.suitability,
        s.recommendation,
        CASE
          WHEN j.last_seen_date >= lr.latest_run_date THEN 'valid'
          ELSE 'cancelled'
        END AS status
      FROM scores s
      JOIN jobs j ON j.id = s.job_id
      JOIN runs r ON r.id = s.run_id
      JOIN (
        SELECT source, location_slug, MAX(run_date) AS latest_run_date
        FROM runs
        GROUP BY source, location_slug
      ) lr ON lr.source = r.source AND lr.location_slug = r.location_slug
      WHERE s.score IS NOT NULL
      ORDER BY s.score DESC, COALESCE(s.first_seen_date, j.first_seen_date, r.run_date) DESC, j.title
      LIMIT 12
    `);

    const [recentAdds] = await connection.execute(`
      SELECT source, DATE_FORMAT(first_seen_date, '%Y-%m-%d') AS date, jr, title, link, department
      FROM jobs
      WHERE first_seen_date IS NOT NULL
      ORDER BY first_seen_date DESC, id DESC
      LIMIT 18
    `);

    const [recentCancels] = await connection.execute(`
      SELECT c.source, DATE_FORMAT(r.run_date, '%Y-%m-%d') AS date, c.jr, c.title
      FROM cancellations c
      JOIN runs r ON r.id = c.run_id
      ORDER BY r.run_date DESC, c.id DESC
      LIMIT 12
    `);

    const aggregate = aggregateRuns(runs);
    const movementSeries = buildMovementSeries(runs, postedAddRows);
    const sources = buildSourceSummaries(runs, scoreStats, postedAddRows);
    const latest = aggregate.at(-1) ?? {};
    const first = aggregate[0] ?? {};
    const totalAdded = postedAddRows.reduce((sum, row) => sum + numberValue(row.count), 0);
    const totalCanceled = runs.reduce((sum, row) => sum + numberValue(row.canceled), 0);
    const totalScored = runs.reduce((sum, row) => sum + numberValue(row.scored), 0);
    const latestAdded = sources.reduce((sum, row) => sum + numberValue(row.latestAdded), 0);
    const latestCanceled = sources.reduce((sum, row) => sum + numberValue(row.latestCanceled), 0);
    const latestScored = sources.reduce((sum, row) => sum + numberValue(row.latestScored), 0);
    const latestQueued = sources.reduce((sum, row) => sum + numberValue(row.queued), 0);
    const activeJobs = sources.reduce((sum, row) => sum + numberValue(row.activeJobs), 0);
    const activeDelta = sources.reduce((sum, row) => sum + numberValue(row.activeDelta), 0);
    const bestScore = Math.max(0, ...sources.map((row) => numberValue(row.bestScore))) || null;

    return {
      generatedAt: new Date().toISOString(),
      summary: {
        daysTracked: aggregate.length,
        sourceCount: sources.length,
        firstDate: first.date ?? null,
        latestDate: latest.date ?? null,
        location: latest.location ?? null,
        activeJobs,
        activeDelta,
        latestAdded,
        latestCanceled,
        latestScored,
        totalAdded,
        totalCanceled,
        totalScored,
        queued: latestQueued,
        bestScore,
      },
      sources,
      runs: movementSeries,
      runsBySource: Object.fromEntries(groupBySource(runs)),
      fitSeries: buildFitSeries(fitRows),
      topRoles: decorateRows(topRoles),
      recentAdds: decorateRows(recentAdds),
      recentCancels: decorateRows(recentCancels),
    };
  } finally {
    await connection.end();
  }
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname === '/api/trends') {
      sendJson(res, 200, await queryTrends());
      return;
    }

    const routePath = url.pathname === '/' ? '/dashboard.html' : url.pathname;
    const safePath = path.normalize(routePath).replace(/^(\.\.[/\\])+/, '');
    sendFile(res, path.join(WEB_DIR, safePath));
  } catch (error) {
    sendJson(res, 500, { error: error.message });
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.error(`Dashboard ready: http://127.0.0.1:${PORT}/`);
});
