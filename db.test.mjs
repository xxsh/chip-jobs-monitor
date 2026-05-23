import assert from 'node:assert/strict';
import test from 'node:test';

import {
  cancellationKey,
  jobContentHash,
  jobKey,
  mysqlConfigFromEnv,
  resolveJobPostedDate,
  resolveJobPostedDateTime,
  slugify,
  toMysqlDate,
  toMysqlDateTime,
} from './db.mjs';

test('mysqlConfigFromEnv skips persistence when no MySQL env vars exist', () => {
  const config = mysqlConfigFromEnv({});
  assert.equal(config.configured, false);
  assert.equal(config.database, 'nvidia_jobs_monitor');
});

test('mysqlConfigFromEnv supports local socket configuration', () => {
  const config = mysqlConfigFromEnv({
    MYSQL_USER: 'root',
    MYSQL_SOCKET_PATH: '/tmp/mysql.sock',
    MYSQL_DATABASE: 'nvidia_jobs_monitor',
  });
  assert.equal(config.configured, true);
  assert.equal(config.config.user, 'root');
  assert.equal(config.config.socketPath, '/tmp/mysql.sock');
  assert.equal(config.config.database, 'nvidia_jobs_monitor');
});

test('jobKey matches daily job identity fallback order', () => {
  assert.equal(jobKey({ id: 42, jr: 'JR42', name: 'Role' }), '42');
  assert.equal(jobKey({ jr: 'JR42', name: 'Role' }), 'JR42');
  assert.equal(jobKey({ title: 'Role', posted: '2026-05-20T00:00:00' }), 'Role|2026-05-20T00:00:00');
});

test('jobContentHash is stable for equivalent job content', () => {
  const a = {
    id: 1,
    jr: 'JR1',
    name: 'Role',
    locations: ['China, Shanghai'],
    requirements: ['Python'],
    preferred: [],
  };
  const b = {
    preferred: [],
    requirements: ['Python'],
    locations: ['China, Shanghai'],
    name: 'Role',
    jr: 'JR1',
    id: 1,
  };
  assert.equal(jobContentHash(a), jobContentHash(b));
});

test('date helpers normalize MySQL date values', () => {
  assert.equal(toMysqlDate('2026-05-20T00:00:00'), '2026-05-20');
  assert.equal(toMysqlDateTime('2026-05-20T01:02:03'), '2026-05-20 01:02:03');
  assert.equal(toMysqlDateTime('2026-05-20'), '2026-05-20 00:00:00');
});

test('resolveJobPostedDate prefers public posted date over monitor run date', () => {
  assert.equal(
    resolveJobPostedDate({ datePosted: '2026-05-20', postedTs: 1779408000, creationTs: 1772236800 }, '2026-05-22'),
    '2026-05-20',
  );
  assert.equal(resolveJobPostedDate({ datePosted: '2026-05-20T01:02:03' }, '2026-05-22'), '2026-05-20');
  assert.equal(resolveJobPostedDate({ postedTs: 1779408000 }, '2026-05-22'), '2026-05-22');
  assert.equal(resolveJobPostedDate({ creationTs: 1772236800 }, '2026-05-22'), '2026-02-28');
  assert.equal(resolveJobPostedDate({}, '2026-05-22'), '2026-05-22');
  assert.equal(resolveJobPostedDateTime({ datePosted: '2026-05-20' }), '2026-05-20 00:00:00');
});

test('slug and cancellation keys are deterministic', () => {
  assert.equal(slugify('Shanghai, China'), 'shanghai-china');
  assert.equal(cancellationKey({ jr: 'JR1', title: 'A' }), cancellationKey({ title: 'A', jr: 'JR1' }));
});
