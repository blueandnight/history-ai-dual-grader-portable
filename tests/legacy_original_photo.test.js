const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const privacyFlowSource = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'privacy-flow.js'),
  'utf8',
);

function createHarness({
  examId = '',
  choices = [{ action: 'confirm', privacyStatuses: ['no_identity_visible'] }],
  previewResponse = {},
} = {}) {
  const requests = [];
  const formalCalls = [];
  const editorOptions = [];
  const pendingChoices = [...choices];
  let busyDialog = null;
  const assignment = {
    id: 'assignment-7',
    formal_layout_required: true,
    sub_questions: [{ id: 'q-1', label: '17.1' }],
    answer_region_profile: {
      page_count: 1,
      regions: [{ photo_index: 1, x: 0.1, y: 0.2, width: 0.5, height: 0.3, sub_question_ids: ['q-1'] }],
    },
  };
  const context = {
    state: { assignments: [assignment] },
    document: {
      querySelector() { return busyDialog; },
      createElement() {
        busyDialog = { className: '', innerHTML: '', showModal() {}, remove() { busyDialog = null; } };
        return busyDialog;
      },
      body: { appendChild() {} },
    },
    api: async (endpoint, request = {}) => {
      const body = request.body ? JSON.parse(request.body) : {};
      requests.push({ endpoint, body });
      if (body.preview_stage === 'prepare') {
        return {
          stage: 'prepared_pages',
          prepared_pages: body.photos.map((photo) => ({ data_url: photo.data_url })),
          model_request_sent: false,
        };
      }
      if (endpoint === '/api/ai-payload-preview') {
        return {
          preview_token: `token-${requests.length}`,
          preview_fingerprint: `fingerprint-${requests.length}`,
          prompt: 'score the full-page photo',
          provider: 'mock-provider',
          model: 'mock-model',
          images: Array.from({ length: previewResponse.imageCount ?? body.photos.length }, (_, index) => ({
            image_kind: previewResponse.imageKind || 'full_page',
            source_page: index + 1,
            data_url: body.photos[index]?.data_url || 'data:image/jpeg;base64,extra',
          })),
          answer_regions: Object.prototype.hasOwnProperty.call(previewResponse, 'answerRegions')
            ? previewResponse.answerRegions : [],
        };
      }
      throw new Error(`unexpected API endpoint: ${endpoint}`);
    },
    reviewFormalPrivacyBatch: async (...args) => {
      formalCalls.push(args);
      return null;
    },
    storedMobileToken: () => '',
    escapeHtml: (value) => String(value),
  };
  vm.createContext(context);
  vm.runInContext(privacyFlowSource, context, { filename: 'privacy-flow.js' });
  context.readPrivacyTemplates = async () => new Map();
  context.readCompressedPrivacyPages = async (group) => group.files.map(() => 'data:image/jpeg;base64,original');
  context.detectPerspectiveAnomaly = async () => false;
  context.showBatchPrivacyPayloadReview = async () => pendingChoices.shift() || {
    action: 'confirm',
    privacyStatuses: ['no_identity_visible'],
  };
  context.editPrivacyGroup = async (_assignmentId, _group, _settings, options) => {
    editorOptions.push(options);
    return {
      answerRegions: [{ region_id: 'editor-crop', sub_question_ids: ['q-1'] }],
      redactions: [[{ x: 0.1, y: 0.1, width: 0.2, height: 0.1 }]],
      corners: [null],
    };
  };
  return { context, requests, formalCalls, editorOptions };
}

function sampleGroups() {
  return [{ studentLabel: '第1份', files: [{ name: '01.jpg', size: 10 }] }];
}

test('original-photo mode skips formal crop and never sends answer regions', async () => {
  for (const examId of ['', 'exam-existing']) {
    const harness = createHarness({ examId });
    const approvals = await harness.context.reviewAiPrivacyBatch(
      'assignment-7', sampleGroups(), 'class-1', false,
      { gradingInputMode: 'original_photo', examId },
    );

    assert.equal(harness.formalCalls.length, 0);
    assert.equal(harness.requests.some(({ endpoint }) => endpoint.includes('student-auto-crop')), false);
    const previewRequests = harness.requests.filter(({ endpoint, body }) => endpoint === '/api/ai-payload-preview' && !body.preview_stage);
    assert.equal(previewRequests.length, 1);
    assert.equal(previewRequests[0].body.grading_input_mode, 'original_photo');
    assert.equal(previewRequests[0].body.exam_id, examId);
    assert.deepEqual(previewRequests[0].body.answer_regions, []);
    for (const { body } of harness.requests.filter(({ endpoint }) => endpoint === '/api/ai-payload-preview')) {
      assert.equal(body.grading_input_mode, 'original_photo');
      assert.equal(body.exam_id, examId);
    }
    assert.equal(approvals.length, 1);
    assert.equal(approvals[0].gradingInputMode, 'original_photo');
    assert.equal(approvals[0].examId, examId);
    assert.equal(approvals[0].answerRegions.length, 0);
    assert.equal(approvals[0].privacy_status, 'no_identity_visible');
  }
});

test('canceling the batch privacy confirmation rejects the approval', async () => {
  const harness = createHarness({ choices: [{ action: 'cancel' }] });
  await assert.rejects(
    harness.context.reviewAiPrivacyBatch(
      'assignment-7', sampleGroups(), '', false,
      { gradingInputMode: 'original_photo', examId: '' },
    ),
    /已取消本批隐私确认/,
  );
  assert.equal(harness.requests.some(({ body }) => !body.preview_stage), true);
});

test('editing redactions regenerates the preview with the same mode and exam binding', async () => {
  const harness = createHarness({
    examId: '',
    choices: [{ action: 'edit-group', index: 0 }, { action: 'confirm', privacyStatuses: ['redacted'] }],
  });
  const approvals = await harness.context.reviewAiPrivacyBatch(
    'assignment-7', sampleGroups(), '', false,
    { gradingInputMode: 'original_photo', examId: '' },
  );

  const previewRequests = harness.requests.filter(({ endpoint, body }) => endpoint === '/api/ai-payload-preview' && !body.preview_stage);
  assert.equal(previewRequests.length, 2);
  for (const { body } of previewRequests) {
    assert.equal(body.grading_input_mode, 'original_photo');
    assert.equal(body.exam_id, '');
    assert.deepEqual(body.answer_regions, []);
  }
  assert.equal(harness.editorOptions.length, 1);
  assert.equal(harness.editorOptions[0].gradingInputMode, 'original_photo');
  assert.equal(approvals[0].gradingInputMode, 'original_photo');
  assert.equal(approvals[0].examId, '');
  assert.equal(approvals[0].answerRegions.length, 0);
  assert.equal(approvals[0].privacy_status, 'redacted');
});

test('original-photo mode rejects a signed preview containing answer regions', async () => {
  for (const answerRegions of [[{ region_id: 'unexpected-region' }], null]) {
    const harness = createHarness({ previewResponse: { answerRegions } });
    await assert.rejects(
      harness.context.reviewAiPrivacyBatch(
        'assignment-7', sampleGroups(), '', false,
        { gradingInputMode: 'original_photo', examId: '' },
      ),
      /预览返回了答题区裁切/,
    );
  }
});

test('original-photo mode rejects crops or an incomplete full-page preview', async (t) => {
  for (const [name, previewResponse] of [
    ['crop image', { imageKind: 'answer_region' }],
    ['missing page image', { imageCount: 0 }],
    ['extra page image', { imageCount: 2 }],
  ]) {
    await t.test(name, async () => {
      const harness = createHarness({ previewResponse });
      await assert.rejects(
        harness.context.reviewAiPrivacyBatch(
          'assignment-7', sampleGroups(), '', false,
          { gradingInputMode: 'original_photo', examId: '' },
        ),
        /未完整保留每张整页照片/,
      );
    });
  }
});
