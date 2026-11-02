const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'static', 'app.js'), 'utf8');
const start = source.indexOf('let examReportData = null;');
const end = source.indexOf('async function loadAssignments()', start);
assert.ok(start >= 0 && end > start, 'legacy exam report block should exist before assignment loading');

const elements = new Map();
const element = (selector) => {
  if (!elements.has(selector)) elements.set(selector, {
    textContent: '', innerHTML: '', hidden: false, disabled: false, value: '', open: false,
    dataset: {}, handlers: {},
    addEventListener(name, handler) { this.handlers[name] = handler; },
    showModal() { this.open = true; },
    close() { this.open = false; this.handlers.close?.(); },
  });
  return elements.get(selector);
};

const pending = [];
const calls = [];
const context = {
  console,
  window: { confirm: () => true },
  document: {},
  URL: {},
  Blob,
  setTimeout,
  escapeHtml: (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  $: element,
  api(path, options) {
    calls.push({ path, options });
    return new Promise((resolve, reject) => pending.push({ resolve, reject }));
  },
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);

const report = (name, assignmentId) => ({
  assignment_name: name, assignment_id: assignmentId, exam_id: 'exam-1',
  student_count: 2, confirmed_count: 1, unconfirmed_count: 1, blocked_count: 0,
  max_total: 10, average: 7, score_rate: 70, distribution: { '60–79': 2 },
  questions: [{ label: 'Q1', max: 10, answered: 2, unanswered: 0, average: 7, score_rate: 70, criteria: [] }],
  common_misses: [], objective: { reason: '客观题统计缺失' },
});

(async () => {
  const first = context.openExamReport('a1', 'exam-1');
  const second = context.openExamReport('a2', 'exam-1');
  pending[1].resolve({ report: report('考试 B', 'a2') });
  await second;
  pending[0].resolve({ report: report('迟到的考试 A', 'a1') });
  await first;
  assert.match(element('#exam-report-title').textContent, /^考试 B/);
  assert.equal(element('#download-exam-report').disabled, false);
  assert.match(element('#exam-report-body').innerHTML, /客观题统计缺失/);

  const ai = context.generateExamReportAi();
  const aiRequest = calls.at(-1);
  assert.equal(aiRequest.path, '/api/exam-report/ai');
  const aiPayload = JSON.parse(aiRequest.options.body);
  assert.equal(aiPayload.assignment_id, 'a2');
  assert.equal(aiPayload.confirm_send, true);
  pending[2].resolve({ report: report('考试 B', 'a2'), markdown: 'AI 教学分析', model: 'mock-model', objectives: '目标' });
  await ai;
  assert.equal(element('#exam-report-ai-output').textContent, 'AI 教学分析');

  const staleAi = context.generateExamReportAi();
  element('#exam-report-dialog').close();
  assert.equal(element('#download-exam-report').disabled, true);
  const third = context.openExamReport('a3', 'exam-3');
  pending[4].resolve({ report: report('考试 C', 'a3') });
  await third;
  pending[3].resolve({ report: report('过期 AI', 'a2'), markdown: '不应覆盖', model: 'mock-model' });
  await staleAi;
  assert.match(element('#exam-report-title').textContent, /^考试 C/);
  assert.notEqual(element('#exam-report-ai-output').textContent, '不应覆盖');
  assert.equal(element('#download-exam-report').disabled, false);

  const missing = context.openExamReport('a4', 'exam-4');
  pending[5].resolve({});
  await missing;
  assert.match(element('#exam-report-status').textContent, /统计数据缺失/);
  assert.equal(element('#download-exam-report').disabled, true);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
