import assert from 'node:assert/strict';
import test from 'node:test';

import { attachApplications } from './application-history.mjs';

test('application outcomes match company and requisition without changing job availability or score', () => {
  const rows = [
    { source: 'nvidia', jr: 'JR100', title: 'Renamed AI role', status: 'valid', score: 74 },
    { source: 'amd', jr: 'JR100', title: 'AI role', status: 'valid', score: 69 },
    { source: 'nvidia', jr: 'JR101', title: 'AI role', status: 'valid', score: 72 },
    { source: 'nvidia', jr: null, title: 'AI role', status: 'valid', score: 71 },
  ];
  const applications = [{
    source: 'NVIDIA', jr: 'jr100', title: 'AI role',
    applicationStatus: 'declined', applicationSubmittedDate: '2026-01-01',
  }];
  const result = attachApplications(rows, applications);
  assert.equal(result[0].applicationStatus, 'declined');
  assert.equal(result[0].applicationSubmittedDate, '2026-01-01');
  assert.equal(result[0].status, 'valid');
  assert.equal(result[0].score, 74);
  assert.equal(result[0].title, 'Renamed AI role');
  assert.ok(result.slice(1).every((row) => row.applicationStatus === null));
  assert.equal(rows[0].applicationStatus, undefined);
});

test('a later application supersedes a prior decline regardless of input order', () => {
  const rows = [{ source: 'nvidia', jr: 'JR100' }];
  const applications = [
    { source: 'nvidia', jr: 'JR100', applicationStatus: 'declined', applicationSubmittedDate: '2026-01-01' },
    { source: 'nvidia', jr: 'JR100', applicationStatus: 'applied', applicationSubmittedDate: '2026-02-01' },
  ];
  for (const history of [applications, [...applications].reverse()]) {
    const [row] = attachApplications(rows, history);
    assert.equal(row.applicationStatus, 'applied');
    assert.equal(row.applicationSubmittedDate, '2026-02-01');
  }
});

test('history for closed or untracked jobs survives enrichment without becoming an active role', () => {
  const applications = [{
    source: 'nvidia', jr: 'JR100', title: 'Historical role',
    applicationStatus: 'declined', applicationSubmittedDate: '2026-01-01',
  }];
  assert.deepEqual(attachApplications([], applications), []);
  assert.equal(applications.length, 1);
  assert.equal(applications[0].applicationStatus, 'declined');
});
