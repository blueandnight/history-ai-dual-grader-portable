const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const start = source.indexOf('async function runBatchUploadWorkers(');
const end = source.indexOf('async function processBatchSubmissions(', start);
assert.ok(start >= 0 && end > start, 'batch upload worker helper should exist');
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

(async () => {
  let stop = false;
  const started = [];
  const completed = [];
  let release;
  const inFlight = new Promise((resolve) => { release = resolve; });
  const run = context.runBatchUploadWorkers(5, 2, () => stop, async (index) => {
    started.push(index + 1);
    await inFlight;
    completed.push(index + 1);
  });
  await Promise.resolve();
  assert.deepEqual(started, [1, 2]);
  stop = true;
  release();
  assert.equal(await run, 2);
  assert.deepEqual(completed, [1, 2]);

  const retried = [];
  const nextIndex = await context.runBatchUploadWorkers(5, 2, () => false, async (index) => retried.push(index + 1), 2);
  assert.equal(nextIndex, 5);
  assert.deepEqual(retried, [3, 4, 5]);

  const retryStart = source.indexOf('async function retryBatchUpload(');
  const retryEnd = source.indexOf('function stopBatchUploads(', retryStart);
  const retryCalls = [];
  const retryButton = { disabled: false, textContent: '' };
  let removed = false;
  const batch = {
    batchId: 'batch-1', nextDisplaySequence: 3, examId: 'exam-1', gradingInputMode: 'original_photo',
    assignmentId: 'assignment-1', className: '', dualReviewEnabled: true,
    items: [{ submission_id: 's1' }, { submission_id: 's2' }, { submission_id: 's3' }],
    groups: [{ studentLabel: '甲', files: ['a.png'] }, { studentLabel: '乙', files: ['b.png'] }, { studentLabel: '丙', files: ['c.png'] }],
    privacyApprovals: [], uploadResults: new Array(3),
  };
  const retryContext = {
    state: { activeBatch: batch },
    alert: (message) => { throw new Error(message); },
    reviewAiPrivacyBatch: async (_assignment, groups) => { retryCalls.push(['privacy', groups[0].studentLabel]); return [{}]; },
    buildSubmissionBody: async (fields) => { retryCalls.push(['body', fields.sequence, fields.submission_id]); return fields; },
    api: async (path, options) => { retryCalls.push(['api', path, options.body.sequence, options.body.submission_id]); },
    displayBatchResultsFrom: async (sequence) => { retryCalls.push(['display', sequence]); },
    syncPendingBatchUploadControls: () => {},
  };
  vm.createContext(retryContext);
  vm.runInContext(source.slice(retryStart, retryEnd), retryContext);
  const row = { querySelector: () => retryButton, remove: () => { removed = true; } };
  await retryContext.retryBatchUpload(3, row);
  assert.deepEqual(retryCalls, [['privacy', '丙'], ['body', 3, 's3'], ['api', '/api/submissions', 3, 's3'], ['display', 3]]);
  assert.equal(batch.uploadResults[2].ok, true);
  assert.equal(removed, true);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
