const state = {
  assignments: [],
  rosterClasses: [],
  rosterStudents: [],
  modelPresets: [],
  batchDraft: null,
  batchPreviewUrls: [],
  cameraSession: { files: [], stream: null, sequence: 0, previewUrls: [] },
  continuousCompleted: 0,
  batchReview: null,
  mobileNetworkAddresses: [],
  mobileNetworkInterfaces: [],
  mobileServicePort: 8765,
  mobileAccessUrl: '',
  recoverySubmissions: [],
  examMappings: [],
  evaluationSummary: null,
  dualReviewAvailableModels: [],
  examImportDraftId: '',
  examImportDraft: null,
  examLayoutProfile: null,
  studentAutoCropResult: null,
};

const mobileTokenParam = new URLSearchParams(window.location.search).get('access_token');
if (mobileTokenParam) {
  try { sessionStorage.setItem('history-grader-mobile-token', mobileTokenParam); } catch (error) { /* private mode */ }
  const cleanUrl = new URL(window.location.href);
  cleanUrl.searchParams.delete('access_token');
  window.history.replaceState({}, document.title, cleanUrl.toString());
}

let fallbackIdempotencyCounter = 0;
const SINGLE_PHOTO_INPUTS = ['#photos', '#photos-album'];
const BATCH_PHOTO_INPUTS = ['#batch-photos', '#batch-photos-album'];

function createIdempotencyKey() {
  const browserCrypto = window.crypto || window.msCrypto;
  if (browserCrypto && typeof browserCrypto.randomUUID === 'function') {
    return browserCrypto.randomUUID();
  }
  if (browserCrypto && typeof browserCrypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(16);
    browserCrypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => ('0' + value.toString(16)).slice(-2)).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  fallbackIdempotencyCounter += 1;
  return `client-${Date.now().toString(36)}-${fallbackIdempotencyCounter.toString(36)}`;
}

const $ = (selector) => document.querySelector(selector);

function filesFromInputs(selectors) {
  const files = selectors.flatMap((selector) => {
    const input = $(selector);
    return input ? [...input.files] : [];
  });
  const seen = new Set();
  return files.filter((file) => {
    const key = `${file.name}\u0000${file.size}\u0000${file.lastModified || 0}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function clearFileInputs(selectors) {
  selectors.forEach((selector) => {
    const input = $(selector);
    if (input) input.value = '';
  });
}

function setReadiness(selector, text, stateName = '') {
  const element = $(selector);
  if (!element) return;
  element.textContent = text;
  element.className = stateName ? `readiness-${stateName}` : '';
}

function isLocalPage() {
  if (window.HISTORY_GRADER_AUTH?.cloudMode) return Boolean(window.HISTORY_GRADER_AUTH.authenticated);
  return ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);
}

function isCloudRuntime() {
  return Boolean(window.HISTORY_GRADER_AUTH?.cloudMode);
}

function applyRuntimeUiCopy() {
  if (!isCloudRuntime()) return;
  const copies = {
    '#assignment-section .section-kicker': '手动录题 / 细则维护',
    '#view-management > .page-heading .section-kicker': '服务器管理',
    '#view-management > .page-heading h3': '共享工作空间配置',
    '#view-management > .page-heading h3 + p': '花名册在共享工作空间中使用；模型密钥由服务器保存。',
    '#history-section h2': '考试与批改记录',
    '#recovery-section > p': '服务重启后，未确定或失败的任务保留在这里。重试前请确认服务商是否已计费；记录保存在服务器。',
    '#evaluation-section > p': '这里只读取服务器明确配置的匿名评测清单，不扫描正式答卷，也不会调用模型。',
    '#audit-summary > p': '正在校验服务器审计链…',
    '#student-auto-crop-test-panel > p': '服务器内定位与裁切测试，不调用模型、不评分。请选择测试照片检查页面匹配和答案图。',
    '#model-settings .section-heading > div > p': 'AI-A 与 AI-B 独立评阅。已保存的Key不会回传浏览器；管理员可在下方选择双评模型、替换Key。',
    '#model-settings .model-settings-hint': '双评由 AI-A 与 AI-B 独立完成；此处配置只影响双评。AI生成评分点仍沿用服务器主模型配置，单模型设置保留给旧单评流程。',
    '.dual-review-option > span': '使用服务器已配置的 AI-A 与 AI-B 独立评阅；手机和电脑均可使用，敏感模型配置由管理员维护。',
    '.batch-dual-review-option > span': '每份答卷使用服务器已配置的 AI-A 与 AI-B 独立评阅；教师仍需逐份确认最终成绩。',
  };
  for (const [selector, text] of Object.entries(copies)) document.querySelectorAll(selector).forEach(el => { el.textContent = text; });
}

function showGradingAssignmentContext(assignmentId) {
  const assignment = state.assignments.find(item => String(item.id) === String(assignmentId));
  const context = $('#grading-assignment-context');
  if (context) context.textContent = assignment ? `当前考试 / 题目：${assignment.name}` : '请先选择考试或题目。';
}

function enterGradingForAssignment(assignment) {
  if (!assignment?.id || (state.batchReview && !closeBatchReview())) return;
  $('#assignment-select').value = assignment.id;
  $('#batch-assignment-select').value = assignment.id;
  loadClassExams().then(() => {
    const select = $('#batch-exam-select');
    if ([...select.options].some(option => option.value === assignment.exam_id)) select.value = assignment.exam_id;
  }).catch(error => setStatus(error.message, true));
  showGradingAssignmentContext(assignment.id);
  $('#main-tab-grading').click();
  $('#submission-result').className = 'result-area empty';
  $('#submission-result').textContent = `已选定“${assignment.name}”，请上传学生答卷开始批改。`;
  window.latestSubmissionId = null;
  $('#grading-section').scrollIntoView({ block: 'start' });
}

function isCloudAdmin() {
  return Boolean(window.HISTORY_GRADER_AUTH?.cloudMode && window.HISTORY_GRADER_AUTH?.authenticated && window.HISTORY_GRADER_AUTH?.role === 'admin');
}

function storedMobileToken() {
  try { return sessionStorage.getItem('history-grader-mobile-token') || ''; } catch (error) { return ''; }
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}

const dualReviewReasonLabels = Object.freeze({
  evidence_difference: '两模型引用的作答证据不同',
  evidence_missing: '至少一个模型没有提供作答证据',
  score_disagreement: '两模型对该评分点给出的分数不同',
  total_score_disagreement: '两模型建议总分不同',
  total_score_unavailable: '至少一个模型没有给出有效总分',
  rubric_point_missing: '评分点结果不完整',
  rubric_decision_disagreement: '两模型对评分点的判断不一致',
  rubric_echo_suspected: '引文与评分细则文字相似，请核对是否为学生手写答案',
  same_score_different_rubric: '总分相同，但对应的评分点判断不同',
  model_result_requires_review: '模型结果需要教师复核',
  dual_review_degraded: '双模型评审未完整完成',
  canonical_input_mismatch: '两模型收到的评分输入不一致',
});

function dualReviewReasonLabel(value) {
  const reason = String(value ?? '').trim();
  return dualReviewReasonLabels[reason] || '其他需要复核的项目';
}

function renderMobileNetwork(data) {
  const validAddress = (value) => /^[0-9]{1,3}(?:\.[0-9]{1,3}){3}$/.test(String(value));
  const records = Array.isArray(data.network_interfaces)
    ? data.network_interfaces
      .filter((item) => item && validAddress(item.address))
      .map((item) => ({
        address: String(item.address),
        connectionLabel: String(item.connection_label || '局域网'),
        recommended: Boolean(item.recommended),
      }))
    : (Array.isArray(data.network_addresses) ? data.network_addresses.filter(validAddress).map((address, index) => ({
      address: String(address), connectionLabel: '局域网', recommended: index === 0,
    })) : []);
  state.mobileNetworkInterfaces = records;
  state.mobileNetworkAddresses = records.map((item) => item.address);
  state.mobileServicePort = Number(data.service_port) || 8765;
  const container = $('#mobile-network-addresses');
  const note = $('#mobile-network-note');
  if (!container || !note) return;
  if (!state.mobileNetworkAddresses.length) {
    note.textContent = '未检测到可用于手机访问的 IPv4 地址';
    container.innerHTML = '<span class="muted">请连接 Wi‑Fi、网线或开启手机“USB 网络共享”，然后点击“刷新地址”。</span>';
    setReadiness('#readiness-connection', '等待连接', 'pending');
    return;
  }
  const hasUsb = records.some((item) => item.connectionLabel === 'USB 数据线');
  note.textContent = hasUsb
    ? '已检测到 USB 网络共享；服务端口 ' + state.mobileServicePort
    : '手机和电脑需连接同一 Wi‑Fi/局域网；服务端口 ' + state.mobileServicePort;
  setReadiness('#readiness-connection', hasUsb ? 'USB 已识别' : '网络已识别', 'ready');
  container.innerHTML = state.mobileNetworkAddresses
    .map((address, index) => {
      const item = records[index];
      const recommended = item.recommended ? '<span class="mobile-network-recommended">推荐</span>' : '';
      return '<div class="mobile-network-address-row"><span class="mobile-network-type">'
        + escapeHtml(item.connectionLabel) + recommended + '</span><code class="mobile-network-address">http://'
        + escapeHtml(address) + ':' + state.mobileServicePort + '/</code></div>';
    })
    .join('');
}

function renderMobileAccessUrl(token = '') {
  const output = $('#mobile-access-url');
  const copyButton = $('#copy-mobile-url');
  if (!output || !copyButton) return;
  state.mobileAccessUrl = token && isCloudAdmin()
    ? `${window.location.origin}/?access_token=${encodeURIComponent(token)}`
    : token && state.mobileNetworkAddresses.length
      ? state.mobileNetworkAddresses.map((address) => 'http://' + address + ':' + state.mobileServicePort + '/?access_token=' + encodeURIComponent(token)).join('\n')
      : '';
  if (!state.mobileAccessUrl) {
    output.textContent = token ? '未检测到可用的手机访问地址，请检查服务器连接' : '生成访问码后显示完整手机地址';
    copyButton.disabled = true;
    return;
  }
  output.textContent = state.mobileAccessUrl;
  copyButton.disabled = false;
}

function applyCloudManagementCopy(cloudAdmin) {
  if (!cloudAdmin) return;
  const page = $('#view-management');
  const panel = $('#mobile-access-panel');
  if (!page || !panel) return;

  const headings = page.querySelectorAll(':scope > .page-heading');
  if (headings[0]) {
    headings[0].querySelector('.section-kicker').textContent = '云端管理';
    headings[0].querySelector('p').textContent = '模型与数据保存在服务器端；本页提供当前账户获准的管理操作。';
  }
  if (headings[1]) {
    headings[1].querySelector('.section-kicker').textContent = '服务器设置';
    headings[1].querySelector('h3').textContent = '模型与人员配置';
    headings[1].querySelector('p').textContent = '花名册可按任课老师自行导入；模型 API Key 只保存在服务器端，不会回传到浏览器。';
  }

  panel.classList.add('cloud-mobile-access');
  const mobileHeading = panel.querySelector('.section-heading');
  mobileHeading.querySelector('.section-kicker').textContent = '手机拍照与批改';
  mobileHeading.querySelector('h2').textContent = '手机批改入口';
  mobileHeading.querySelector('p').textContent = '可生成 45 分钟临时授权，让手机上传答卷、查看手机提交记录的证据并确认成绩；无需共享管理员密码。';

  const stepCopy = [
    ['打开云端地址', '手机浏览器打开当前 HTTPS 域名即可使用，无需与服务器处于同一 Wi-Fi，也无需 USB 网络共享。'],
    ['生成临时授权', '访问码无需管理员密码；只可处理手机提交的答卷，不能读取花名册或服务器设置。'],
    ['批改并确认', '可查看评分证据并确认手机提交记录的成绩；45 分钟后、服务重启或主动撤销后失效。'],
  ];
  panel.querySelectorAll('.connection-steps li').forEach((item, index) => {
    const copy = stepCopy[index];
    if (!copy) return;
    item.querySelector('strong').textContent = copy[0];
    item.querySelector('small').textContent = copy[1];
  });

  const note = $('#mobile-token-note');
  if (note) note.textContent = '生成 45 分钟临时码：可上传答卷、查看手机提交记录并确认成绩，也可教师确认跳过手机批次当前失败项；不开放模型重试、花名册、模型设置或备份权限。';
}

async function copyMobileAccessUrl() {
  if (!state.mobileAccessUrl) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(state.mobileAccessUrl);
    } else {
      const textarea = document.createElement('textarea');
      textarea.value = state.mobileAccessUrl;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      document.execCommand('copy');
      textarea.remove();
    }
    $('#copy-mobile-url').textContent = '已复制';
    setTimeout(() => { $('#copy-mobile-url').textContent = '复制手机地址'; }, 1600);
  } catch (error) {
    alert('复制失败，请手动复制上方手机地址');
  }
}

async function api(url, options = {}) {
  const isMultipart = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const headers = { ...(isMultipart ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) };
  const token = storedMobileToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(url, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

// Image navigation does not send Authorization headers, so mobile-token image
// previews must be fetched explicitly rather than embedding the token in URLs.
document.addEventListener('click', async (event) => {
  const link = event.target.closest('a[data-protected-image]');
  const token = storedMobileToken();
  if (!link || !token) return;
  event.preventDefault();
  const preview = window.open('about:blank', '_blank');
  if (!preview) {
    alert('浏览器拦截了新窗口，请允许此页面打开原图。');
    return;
  }
  try {
    const response = await fetch(link.href, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `原图读取失败（${response.status}）`);
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    preview.location.replace(objectUrl);
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  } catch (error) {
    preview.close();
    alert(error.message);
  }
});

async function waitForSubmission(submissionId, progressCallback = null) {
  for (let attempt = 0; attempt < 720; attempt += 1) {
    const data = await api(`/api/submission?submission_id=${encodeURIComponent(submissionId)}`);
    const submission = data.submission;
    if (typeof progressCallback === 'function') progressCallback(submission);
    if (['completed', 'failed'].includes(submission.state)) return submission;
    await wait(500);
  }
  throw new Error(isCloudRuntime() ? '服务器批改队列等待超时；记录仍保留，可稍后刷新查看。' : '本机批改队列等待超时；记录仍保留在本地，可稍后刷新查看。');
}

function normalizeMajorNumber(value) {
  return String(value || '').trim().replace(/^第\s*/, '').replace(/\s*题$/, '').trim();
}

function normalizeMinorNumber(value, fallback) {
  return String(value || '').trim()
    .replace(/^第\s*/, '')
    .replace(/\s*(?:小题|问)$/, '')
    .replace(/^[（(]\s*/, '')
    .replace(/\s*[）)]$/, '')
    .trim() || String(fallback);
}

function composeSubQuestionLabel(majorValue, minorValue, fallback) {
  const major = normalizeMajorNumber(majorValue);
  const minor = normalizeMinorNumber(minorValue, fallback);
  if (!major) return minor;
  if (!minor) return major;
  if (minor.startsWith(`${major}.`)) return minor;
  return `${major}.${minor}`;
}

function addMajorQuestion(values = {}) {
  const list = $('#major-questions-list');
  const usedMajorNumbers = new Set([...list.querySelectorAll('.major-label')]
    .map(input => normalizeMajorNumber(input.value)).filter(Boolean));
  let nextMajorNumber = 17;
  while (usedMajorNumbers.has(String(nextMajorNumber))) nextMajorNumber += 1;
  const fragment = $('#major-question-template').content.cloneNode(true);
  const card = fragment.querySelector('.major-question-card');
  const children = card.querySelector('.subquestions-list');
  card.querySelector('.major-label').value = values.label || String(nextMajorNumber);
  card.querySelector('.major-prompt').value = values.prompt || '';
  card.querySelector('.remove-major-question').addEventListener('click', () => {
    card.remove();
    updateAssignmentTotal();
  });
  card.querySelector('.add-major-subquestion').addEventListener('click', () => addSubQuestion({}, children));
  list.appendChild(fragment);
  if (Array.isArray(values.sub_questions) && values.sub_questions.length) {
    values.sub_questions.forEach(item => addSubQuestion(item, children));
  } else {
    addSubQuestion({ label: '1' }, children);
  }
  updateAssignmentTotal();
}

function addSubQuestion(values = {}, targetList = null) {
  const fragment = $('#subquestion-template').content.cloneNode(true);
  const card = fragment.querySelector('.subquestion-card');
  const list = targetList || $('#major-questions-list .major-question-card:last-child .subquestions-list');
  if (!list) return;
  const usedLabels = new Set([...list.querySelectorAll('.subq-label')].map(input => normalizeMinorNumber(input.value, '')));
  let nextLabel = 1;
  while (usedLabels.has(String(nextLabel))) nextLabel += 1;
  card.querySelector('.subq-label').value = values.label || String(nextLabel);
  card.querySelector('.subq-question').value = values.question || '';
  card.querySelector('.subq-answer').value = values.standard_answer || '';
  card.querySelector('.subq-max-score').value = values.max_score || '';
  card.querySelector('.subq-scoring-mode').value = values.scoring_mode || 'point_bank';
  card.querySelector('.subq-point-value').value = values.point_value || 2;
  card.querySelector('.subq-criteria').value = values.criteriaText || '';
  card.querySelector('.remove-subquestion').addEventListener('click', () => {
    card.remove();
    updateAssignmentTotal();
  });
  card.querySelector('.subq-scoring-mode').addEventListener('change', () => updateAssignmentTotal());
  list.appendChild(fragment);
  updateAssignmentTotal();
}

function buildAssignmentQuestion() {
  const general = $('#question').value.trim();
  const groups = [...document.querySelectorAll('.major-question-card')].map((card) => {
    const major = normalizeMajorNumber(card.querySelector('.major-label').value);
    const prompt = card.querySelector('.major-prompt').value.trim();
    const hasQuestions = [...card.querySelectorAll('.subquestion-card')].some(hasSmallQuestionInput);
    if (!prompt && !hasQuestions) return '';
    return `${major ? `【第${major}题】` : '【大题材料】'}${prompt ? `\n${prompt}` : ''}`;
  });
  return [
    general ? `【全卷背景/总说明】\n${general}` : '',
    ...groups,
  ].filter(Boolean).join('\n\n');
}

function collectQuestionEntries() {
  return [...document.querySelectorAll('.major-question-card')].flatMap((group) =>
    [...group.querySelectorAll('.subquestion-card')].map(card => ({ group, card })));
}

function hasSmallQuestionInput(card) {
  return Boolean(
    card.querySelector('.subq-question').value.trim()
    || card.querySelector('.subq-answer').value.trim()
    || card.querySelector('.subq-criteria').value.trim()
    || card.querySelector('.subq-max-score').value.trim(),
  );
}

function parseCriteriaText(text, defaultScore = 0) {
  return text.split('\n').map((line, index) => {
    const parts = line.split('|').map(x => x.trim());
    const score = parts[1] ? Number(parts[1]) : Number(defaultScore);
    return {
      id: `C${index + 1}`,
      label: parts[0] || '',
      score: Number.isFinite(score) ? score : 0,
      keywords: (parts[2] || '').split(/[,，、]/).map(x => x.trim()).filter(Boolean),
      note: parts[3] || '',
    };
  }).filter(item => item.label && item.score > 0);
}

function inferMaxScore(question) {
  const match = String(question).match(/[（(]\s*(\d+(?:\.\d+)?)\s*分\s*[）)]|(?:^|\s)(\d+(?:\.\d+)?)\s*分/);
  return match ? Number(match[1] || match[2]) : null;
}

function buildSubQuestionDraft(card, index) {
  const group = card.closest('.major-question-card');
  const majorLabel = group?.querySelector('.major-label').value.trim() || '';
  const majorNumber = normalizeMajorNumber(majorLabel);
  const majorPrompt = group?.querySelector('.major-prompt').value.trim() || '';
  const childQuestion = card.querySelector('.subq-question').value.trim();
  const label = composeSubQuestionLabel(majorLabel, card.querySelector('.subq-label').value, index + 1);
  const question = [
    majorPrompt ? `所属大题材料/共同题干（第${majorNumber || majorLabel}题）：\n${majorPrompt}` : '',
    `小题题干（${label}）：\n${childQuestion}`,
  ].filter(Boolean).join('\n\n');
  const explicitMax = Number(card.querySelector('.subq-max-score').value);
  const scoringMode = card.querySelector('.subq-scoring-mode').value || 'point_bank';
  const pointValue = Number(card.querySelector('.subq-point-value').value) || 2;
  const inferredMax = Number.isFinite(explicitMax) && explicitMax > 0 ? explicitMax : inferMaxScore(childQuestion);
  return {
    id: `Q${index + 1}`,
    label,
    question,
    standard_answer: card.querySelector('.subq-answer').value.trim(),
    max_score: scoringMode === 'point_bank' ? inferredMax : null,
    scoring_mode: scoringMode,
    point_value: pointValue,
    criteria: parseCriteriaText(card.querySelector('.subq-criteria').value, scoringMode === 'point_bank' ? pointValue : 0),
  };
}

function collectDraftSubQuestions() {
  return collectQuestionEntries().map(({ card }, index) => buildSubQuestionDraft(card, index));
}

function collectSubQuestions() {
  return collectDraftSubQuestions().filter(item => item.question && item.standard_answer && item.criteria.length);
}

function criteriaToText(criteria) {
  return (criteria || []).map(item => [
    item.label || item.criterion || '',
    item.score ?? item.max_score ?? 0,
    Array.isArray(item.keywords) ? item.keywords.join(',') : (item.keywords || ''),
    item.note || '',
  ].join(' | ')).join('\n');
}

function generatedForDraft(draft, index, allGenerated) {
  const byId = Object.fromEntries((allGenerated || []).map(item => [item.id || item.sub_question_id, item]));
  return byId[draft.id] || null;
}

async function generateRubricForCard(card) {
  const cards = collectQuestionEntries().map(item => item.card);
  const index = cards.indexOf(card);
  const draft = buildSubQuestionDraft(card, index);
  if (!card.querySelector('.subq-question').value.trim() || !draft.standard_answer) return alert('请先填写本小题题目和参考答案');
  if (draft.scoring_mode === 'point_bank' && !draft.max_score) return alert('按得分点封顶时，请填写本小题满分；如果题干中有“（6分）”等标注，也可以直接录入在小题题目中。');
  const button = card.querySelector('.generate-rubric');
  button.disabled = true;
  button.textContent = '生成中…';
  try {
    const data = await previewAndGenerateRubric(buildAssignmentQuestion(), [draft]);
    const generated = data.sub_questions?.[0];
    if (generated?.id !== draft.id || !generated?.criteria?.length) throw new Error('模型返回的小题 ID 缺失或不匹配，评分点未应用；请重试或手动录入。');
    card.querySelector('.subq-criteria').value = criteriaToText(generated.criteria);
    updateAssignmentTotal();
    button.textContent = '重新生成本小题评分点';
  } catch (error) {
    alert(error.message);
    button.textContent = 'AI生成本小题评分点';
  } finally { button.disabled = false; }
}

async function generateAllRubric() {
  const allEntries = collectQuestionEntries();
  const entries = allEntries.filter(({ card }) => hasSmallQuestionInput(card));
  const drafts = entries.map(({ card }) => buildSubQuestionDraft(
    card,
    allEntries.findIndex(item => item.card === card),
  ));
  if (!drafts.length || entries.some(({ card }) => !card.querySelector('.subq-question').value.trim() || !card.querySelector('.subq-answer').value.trim())) {
    return alert('请先填写每道小题的题目和参考答案');
  }
  if (drafts.some(item => item.scoring_mode === 'point_bank' && !item.max_score)) {
    return alert('按得分点封顶的小题需要填写满分；也可以把“（6分）”等标注录入在小题题目中。');
  }
  const button = $('#generate-all-rubric');
  button.disabled = true;
  button.textContent = '生成中…';
  try {
    const data = await previewAndGenerateRubric(buildAssignmentQuestion(), drafts);
    const generated = data.sub_questions || [];
    const mapped = drafts.map(draft => generatedForDraft(draft, 0, generated));
    if (mapped.length !== drafts.length || mapped.some(item => !item?.criteria?.length)) {
      throw new Error('模型返回的小题 ID 缺失或不匹配；没有应用任何草拟评分点，请重试或手动录入。');
    }
    entries.forEach(({ card }, index) => {
      card.querySelector('.subq-criteria').value = criteriaToText(mapped[index].criteria);
    });
    updateAssignmentTotal();
    button.textContent = '已生成，可审核后保存';
  } catch (error) {
    alert(error.message);
    button.textContent = 'AI生成全部评分点';
  } finally { button.disabled = false; }
}

async function previewAndGenerateRubric(question, subQuestions) {
  const request = { question, sub_questions: subQuestions };
  const preview = await api('/api/rubric-payload-preview', {
    method: 'POST',
    body: JSON.stringify(request),
  });
  if (preview.blocked || !preview.preview_token || !preview.payload) {
    throw new Error(preview.error || '服务端无法生成评分细则 Payload 预览');
  }
  const dialog = document.createElement('dialog');
  dialog.className = 'ai-privacy-dialog';
  dialog.innerHTML = `
    <section class="ai-privacy-card ai-payload-final">
      <header><div><span class="eyebrow">评分细则外发检查</span><h2>核对即将发送的完整模型请求</h2></div><button type="button" class="icon-button rubric-preview-cancel" aria-label="取消">×</button></header>
      <p>模型会收到下方完整请求正文，其中包含题干和参考答案。请确认不含学生姓名、学号、花名册或其他不应外发的身份信息。服务端检测到花名册姓名或学号时会直接拦截。</p>
      <div class="ai-payload-meta">用途：${escapeHtml(preview.model_role || '模型')}生成评分点草稿 · 服务商：${escapeHtml(preview.provider)} · 模型：${escapeHtml(preview.model)} · 目标主机：${escapeHtml(preview.destination_host)} · 指纹：${escapeHtml(preview.preview_fingerprint.slice(0, 16))}…</div>
      <details open><summary>实际发送的完整 JSON Payload</summary><pre class="ai-payload-prompt">${escapeHtml(JSON.stringify(preview.payload, null, 2))}</pre></details>
      <label class="ai-privacy-check"><input class="rubric-payload-confirm" type="checkbox">我已核对完整 Payload，确认可将其中内容发送给显示的模型服务商</label>
      <footer><button type="button" class="button secondary rubric-preview-cancel">取消</button><button type="button" class="button primary rubric-payload-send" disabled>确认并发送</button></footer>
    </section>`;
  document.body.appendChild(dialog);
  try { dialog.showModal(); } catch (_) { dialog.remove(); throw new Error('当前浏览器无法安全显示模型请求预览'); }
  const confirmed = await new Promise((resolve) => {
    const finish = (value) => { try { dialog.close(); } catch (_) {} dialog.remove(); resolve(value); };
    dialog.querySelectorAll('.rubric-preview-cancel').forEach((button) => button.addEventListener('click', () => finish(false)));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(false); }, { once: true });
    const checkbox = dialog.querySelector('.rubric-payload-confirm');
    const send = dialog.querySelector('.rubric-payload-send');
    checkbox.addEventListener('change', () => { send.disabled = !checkbox.checked; });
    send.addEventListener('click', () => finish(true));
  });
  if (!confirmed) throw new Error('已取消评分细则模型请求，没有发送 Payload');
  return api('/api/generate-rubric', {
    method: 'POST',
    body: JSON.stringify({ ...request, rubric_preview_token: preview.preview_token }),
  });
}

function updateAssignmentTotal() {
  const groups = [...document.querySelectorAll('.major-question-card')];
  const entries = collectQuestionEntries();
  const cards = entries.map(item => item.card);
  let total = 0;
  let missingPointBankMax = false;
  let insufficientPointCandidates = false;
  groups.forEach((group) => {
    let groupTotal = 0;
    [...group.querySelectorAll('.subquestion-card')].forEach((card) => {
      const index = cards.indexOf(card);
      const draft = buildSubQuestionDraft(card, index);
      const active = hasSmallQuestionInput(card);
      card.querySelector('.subq-full-label').textContent = draft.label ? `第${draft.label}题` : '未编号';
      const criteriaTotal = draft.criteria.reduce((sum, item) => sum + item.score, 0);
      const bankMode = draft.scoring_mode === 'point_bank';
      card.querySelectorAll('.subq-point-bank-field').forEach((field) => { field.hidden = !bankMode; });
      const guide = card.querySelector('.subq-scoring-guide');
      if (!active) {
        guide.textContent = '填写小题内容、答案或评分点后自动计算。';
        guide.classList.remove('is-warning');
        return;
      }
      if (bankMode) {
        if (!draft.max_score) {
          missingPointBankMax = true;
          guide.textContent = '请填写本小题满分，系统才能知道答中几个不同要点后封顶。';
          guide.classList.add('is-warning');
        } else {
          const requiredPoints = Math.ceil(draft.max_score / draft.point_value);
          const underfilled = draft.criteria.length < requiredPoints;
          if (underfilled) insufficientPointCandidates = true;
          guide.textContent = `${underfilled ? `候选不足：目前 ${draft.criteria.length} 个，至少需要 ${requiredPoints} 个才能达到满分。` : ''}按不同有效要点计分：每点 ${draft.point_value} 分，满分 ${draft.max_score} 分；当前列有 ${draft.criteria.length} 个候选点。同义表达应合并到同一点；候选可多于满分所需。需要各评分项不同分值时，切换到“按评分项分值累计”。`;
          guide.classList.toggle('is-warning', underfilled);
          groupTotal += draft.max_score;
        }
      } else {
        guide.textContent = `自定义累计：每行按填写的分值计分；当前评分项合计 ${criteriaTotal} 分。`;
        guide.classList.remove('is-warning');
        groupTotal += criteriaTotal;
      }
    });
    total += groupTotal;
    const formattedGroupTotal = Number.isInteger(groupTotal) ? groupTotal : groupTotal.toFixed(1);
    group.querySelector('.major-total').textContent = `本大题小题分值合计：${formattedGroupTotal}`;
  });
  const formatted = Number.isInteger(total) ? total : total.toFixed(1);
  $('#assignment-total').textContent = `全卷满分合计：${formatted}${missingPointBankMax ? '（请补全小题满分）' : insufficientPointCandidates ? '（候选点不足以覆盖满分）' : ''}`;
}

function setStatus(text, error = false) {
  $('#status').textContent = text;
  $('#status').style.background = error ? '#fff0f0' : '';
  $('#status').style.color = error ? '#a23c3c' : '';
}

function modelStatusText(data) {
  if (data.model_is_simulation) return '模拟模型已连接 · 结果仅用于流程验收，不是真实 AI 评分';
  if (isCloudAdmin() && !data.model_configured) return '服务器模型未配置完整，当前不能进行真实 AI 批改';
  if (!data.model_configured) return isCloudRuntime() ? '服务器模型尚未配置，请联系管理员' : '本地服务正常 · 当前为规则演示模式';
  if (data.api_key_error) return `接口已配置 · ${isCloudRuntime() ? '服务器密钥需要检查' : '本机 Key 无法解密'} · ${data.model}`;
  if (!data.api_key_configured) return `接口已配置 · ${isCloudRuntime() ? '请联系管理员配置服务器密钥' : '请在电脑本机输入 Key'} · ${data.model}`;
  return `视觉模型已配置 · ${data.model}`;
}

function modelPresetById(presetId) {
  return state.modelPresets.find((item) => item.id === presetId)
    || state.modelPresets.find((item) => item.id === 'custom');
}

function isUserModelPreset(preset) {
  return Boolean(preset && preset.user_defined);
}

function syncCustomPresetEditor(preset = modelPresetById($('#model-preset').value)) {
  const userPreset = isUserModelPreset(preset);
  $('#custom-preset-label').value = userPreset ? (preset.label || '') : '';
  $('#custom-preset-note').value = userPreset ? (preset.note || '') : '';
  $('#delete-custom-preset').disabled = !userPreset;
  $('#custom-preset-status').textContent = userPreset
    ? '正在编辑此自定义预设；修改后点击“保存为自定义预设”。'
    : '选择或填写接口后，可保存为自定义预设。';
}

function renderModelPresets(selectedId = 'custom') {
  const select = $('#model-preset');
  if (!state.modelPresets.length) {
    if (isCloudAdmin()) {
      select.innerHTML = '<option value="">服务器尚未配置可选模型</option>';
      select.value = '';
      return;
    }
    select.innerHTML = '<option value="custom">自定义 OpenAI 兼容视觉模型</option>';
    select.value = 'custom';
    return;
  }
  select.innerHTML = state.modelPresets
    .map((preset) => `<option value="${escapeHtml(preset.id)}">${escapeHtml(preset.label)}</option>`)
    .join('');
  select.value = state.modelPresets.some((item) => item.id === selectedId) ? selectedId : 'custom';
}

function applyModelPreset(presetId, preserveCustomValues = false) {
  const preset = modelPresetById(presetId);
  if (!preset) return;
  const custom = preset.id === 'custom';
  const editable = !isCloudAdmin() && (custom || isUserModelPreset(preset));
  if (!custom) {
    $('#model-base-url').value = preset.base_url || '';
    $('#model-id').value = preset.model || '';
  } else if (!preserveCustomValues) {
    $('#model-base-url').value = '';
    $('#model-id').value = '';
  }
  $('#model-base-url').readOnly = !editable;
  $('#model-id').readOnly = !editable && !preset.experimental;
  $('#api-key-label').textContent = `${preset.provider_label || preset.provider || '当前服务商'} API Key`;
  $('#model-preset-note').textContent = preset.note || '';
  syncCustomPresetEditor(preset);
}

async function loadStatus() {
  try {
    await window.HISTORY_GRADER_AUTH_READY;
    applyRuntimeUiCopy();
    if (window.HISTORY_GRADER_AUTH?.role === 'teacher') {
      const health = await api('/api/health');
      setStatus(health.ok ? '服务正常' : '服务需要检查', !health.ok);
      await loadDualReviewReadiness();
      setReadiness('#readiness-connection', '已登录教师账户', 'ready');
      return;
    }
    const data = await api('/api/status');
    const localPage = isLocalPage();
    const cloudAdmin = isCloudAdmin();
    state.modelPresets = data.model_presets || [];
    renderModelPresets(data.model_preset || 'custom');
    $('#model-base-url').value = data.base_url || '';
    $('#model-id').value = data.model || '';
    applyModelPreset(data.model_preset || 'custom', true);
    if (cloudAdmin) {
      state.mobileNetworkInterfaces = [];
      state.mobileNetworkAddresses = [];
      state.mobileServicePort = 0;
      applyCloudManagementCopy(true);
      setReadiness('#readiness-connection', window.location.protocol === 'https:' ? 'HTTPS 可用' : '需要 HTTPS', window.location.protocol === 'https:' ? 'ready' : 'error');
    } else {
      renderMobileNetwork(data);
    }
    setStatus(modelStatusText(data), Boolean(data.api_key_error));
    await loadDualReviewReadiness();
    setReadiness(
      '#readiness-model',
      data.model_is_simulation ? '仅模拟' : data.api_key_error ? 'Key 需要处理' : (data.model_configured && data.api_key_configured ? '已配置' : '待配置'),
      data.model_is_simulation ? 'error' : data.api_key_error ? 'error' : (data.model_configured && data.api_key_configured ? 'ready' : 'pending'),
    );
    const keyInput = $('#api-key');
    const keyButton = $('#settings-form button[type="submit"]');
    const serviceButtons = [$('#restart-service'), $('#shutdown-service')];
    const mobileButtons = [$('#generate-mobile-token'), $('#revoke-mobile-token'), $('#refresh-mobile-network')];
    const configControls = [$('#model-preset'), $('#model-base-url'), $('#model-id')];
    const customPresetControls = [$('#fetch-custom-models'), $('#custom-model-list'), $('#custom-preset-label'), $('#custom-preset-note'), $('#save-custom-preset'), $('#delete-custom-preset')];
    const keyStorageControls = [...document.querySelectorAll('input[name="key-storage"]')];
    if (cloudAdmin) {
      $('#api-key').value = '';
      $('#api-key').disabled = true;
      $('#model-preset').disabled = !data.server_model_selection_enabled || state.modelPresets.length === 0;
      $('#model-base-url').readOnly = true;
      $('#model-id').readOnly = true;
      $('#settings-form button[type="submit"]').disabled = state.modelPresets.length < 2;
      [...customPresetControls, ...keyStorageControls, ...serviceButtons].forEach((control) => { control.disabled = true; });
      mobileButtons.forEach((control) => { control.disabled = control.id === 'refresh-mobile-network'; });
      $('#settings-note').textContent = data.model_is_simulation
        ? '当前服务器使用模拟模型；结果不能作为真实 AI 评分或确认成绩。'
        : '模型 Key 保存在服务器；网页只提交已配置的模型预设编号。';
      $('#mobile-token-note').textContent = data.mobile_token_active
        ? '已有有效临时码；重新生成会使旧码失效，权限仅限手机提交的批改记录'
        : '生成45分钟临时码，可处理手机提交的答卷、查看证据并确认成绩';
      renderMobileAccessUrl('');
    } else if (!localPage) {
      [keyInput, keyButton, ...configControls, ...customPresetControls, ...keyStorageControls, ...serviceButtons, ...mobileButtons].forEach((control) => { control.disabled = true; });
      $('#settings-note').textContent = '请在电脑打开 http://127.0.0.1:8765 保存 Key';
      $('#mobile-token-note').textContent = '请在运行程序的电脑本机生成临时访问码';
    } else {
      [...configControls, ...customPresetControls, ...keyStorageControls].forEach((control) => { control.disabled = false; });
      mobileButtons.forEach((control) => { control.disabled = false; });
      $('#mobile-token-note').textContent = data.mobile_token_active
        ? '已有有效访问码；重新生成会立即使旧访问码失效'
        : '尚未生成手机访问码';
      renderMobileAccessUrl('');
      $('#settings-note').textContent = data.api_key_error
        || (data.api_key_session_only
          ? 'Key 仅在本次运行中有效；未更新本机已保存的 Key。'
          : data.api_key_persisted
          ? 'Key 已保存在本机，不会写入 U 盘，也不会回显。'
          : data.api_key_configured
            ? 'Key 仅在本次运行中有效，未写入磁盘。'
            : '本机尚未保存当前服务商 Key。');
    }
  } catch (error) { setStatus(error.message, true); }
}

async function loadDualReviewReadiness() {
  const controls = [
    { input: $('#dual-review-enabled'), note: $('#dual-review-readiness') },
    { input: $('#batch-dual-review-enabled'), note: $('#batch-dual-review-readiness') },
  ].filter((item) => item.input && item.note);
  try {
    const teacherAccount = window.HISTORY_GRADER_AUTH?.role === 'teacher';
    const mobileClient = Boolean(storedMobileToken()) || teacherAccount;
    const data = await api(mobileClient
      ? '/api/mobile/dual-review-readiness'
      : '/api/dual-review/status');
    state.dualReviewAvailableModels = data.available_models || [];
    const ready = Boolean(data.configured_a && data.configured_b && data.pair_distinct !== false);
    const modelNames = [data.label_a, data.label_b].filter(Boolean);
    const pairLabel = modelNames.length === 2 ? `${modelNames[0]} + ${modelNames[1]}` : '所选双模型';
    if (teacherAccount) setReadiness('#readiness-model', ready ? '双评已就绪' : '请联系管理员配置', ready ? 'ready' : 'pending');
    const message = ready
      ? teacherAccount
        ? `双评已就绪：${pairLabel}。模型配置由管理员维护。`
        : mobileClient
        ? (isCloudRuntime() ? `服务器双评已就绪：${pairLabel}；手机可直接使用。敏感模型配置由管理员维护。` : `电脑端已配置就绪：${pairLabel}；手机可以使用此配置独立双评。更换模型或 Key 请在电脑设置。`)
        : `已就绪：${pairLabel}；开启后每份答卷会产生两次独立模型请求。`
      : data.pair_distinct === false
        ? '暂不可用：AI-A 与 AI-B 选择了相同模型。请为两个独立评分位选择不同模型。'
        : `暂不可用：AI-A（${data.label_a || '未选择'}）Key${data.configured_a ? '已配置' : '未配置'}，AI-B（${data.label_b || '未选择'}）Key${data.configured_b ? '已配置' : '未配置'}。请在模型设置中分别选择模型并配置 Key。`;
    [
      { side: 'a', configured: Boolean(data.configured_a), preset: data.preset_a, label: data.label_a, provider: data.provider_label_a, model: data.model_a },
      { side: 'b', configured: Boolean(data.configured_b), preset: data.preset_b, label: data.label_b, provider: data.provider_label_b, model: data.model_b },
    ].forEach(({ side, configured }) => {
      const state = $(`#dual-model-${side}-state`);
      const note = $(`#dual-model-${side}-note`);
      const input = $(`#dual-model-${side}-key`);
      const form = $(`#dual-model-${side}-form`);
      const button = form?.querySelector('button[type="submit"]');
      if (state) {
        state.textContent = configured ? 'Key 已配置' : '待配置';
        state.classList.toggle('is-ready', configured);
        state.classList.toggle('is-missing', !configured);
      }
      if (note) note.textContent = isCloudRuntime()
        ? (configured ? '服务器Key已配置；留空沿用所选服务商Key，输入新Key可替换。' : '请选择模型并输入对应服务商Key。')
        : configured
        ? '已配置。重新输入并保存可替换本机加密 Key。'
        : '尚未配置；保存后会使用 Windows 本机 DPAPI 加密。';
      const select = $(`#dual-model-${side}-preset`);
      if (select && Array.isArray(data.available_models)) {
        const current = side === 'a' ? data.preset_a : data.preset_b;
        select.innerHTML = data.available_models.map((item) =>
          `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`
        ).join('');
        select.value = current || '';
      }
      const selectedModel = data.available_models?.find((item) => item.id === (side === 'a' ? data.preset_a : data.preset_b));
      const title = $(`#dual-model-${side}-title`);
      const meta = $(`#dual-model-${side}-meta`);
      const keyLabel = $(`#dual-model-${side}-key-label`);
      if (title) title.textContent = selectedModel?.label || '请选择视觉模型';
      if (meta) meta.innerHTML = selectedModel
        ? `${escapeHtml(selectedModel.provider_label)} · <code>${escapeHtml(selectedModel.model)}</code>`
        : '选择此侧视觉模型';
      if (keyLabel && selectedModel) {
        keyLabel.firstChild.textContent = `${selectedModel.provider_label} API Key`;
        const keyInput = keyLabel.querySelector('input');
        if (keyInput) keyInput.placeholder = isCloudRuntime() ? '留空沿用所选服务商Key；输入新Key可替换，不回显' : `可留空并沿用本机 ${selectedModel.provider_label} Key；输入不会回显`;
      }
      if (input) input.disabled = isCloudRuntime() ? !isCloudAdmin() : !isLocalPage();
      if (select) select.disabled = isCloudRuntime() ? !isCloudAdmin() : !isLocalPage();
      if (button) button.disabled = isCloudRuntime() ? !isCloudAdmin() : !isLocalPage();
    });
    controls.forEach(({ input, note }) => {
      note.textContent = teacherAccount && !ready ? '双评暂不可用，请联系管理员检查服务器模型配置。' : message;
      input.disabled = !ready;
      if (!ready) input.checked = false;
    });
  } catch (error) {
    ['a', 'b'].forEach((side) => {
      const state = $(`#dual-model-${side}-state`);
      const note = $(`#dual-model-${side}-note`);
      if (state) { state.textContent = '状态不可用'; state.classList.remove('is-ready', 'is-missing'); }
      if (note) note.textContent = `无法读取${isCloudRuntime() ? '服务器' : '本机'}配置：${error.message}`;
      const input = $(`#dual-model-${side}-key`);
      const button = $(`#dual-model-${side}-form button[type="submit"]`);
      if (input) input.disabled = true;
      if (button) button.disabled = true;
    });
    controls.forEach(({ input, note }) => {
      input.disabled = true;
      input.checked = false;
      note.textContent = `双评配置检查失败：${error.message}`;
    });
  }
}

async function saveDualReviewConfiguration(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const side = form.dataset.side;
  const sideKey = side.toLowerCase();
  const input = $(`#dual-model-${sideKey}-key`);
  const select = $(`#dual-model-${sideKey}-preset`);
  const note = $(`#dual-model-${sideKey}-note`);
  const button = form.querySelector('button[type="submit"]');
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    const result = await api('/api/dual-review/configuration', {
      method: 'POST',
      body: JSON.stringify({ side, preset_id: select.value, api_key: input.value }),
    });
    input.value = '';
    note.classList.remove('error');
    note.textContent = result.message || '模型配置已保存。';
    await loadDualReviewReadiness();
  } catch (error) {
    note.textContent = error.message;
    note.classList.add('error');
  } finally {
    button.textContent = originalText;
    input.value = '';
    button.disabled = isCloudRuntime() ? !isCloudAdmin() : !isLocalPage();
  }
}

function previewDualReviewModel(side) {
  const sideKey = side.toLowerCase();
  const select = $(`#dual-model-${sideKey}-preset`);
  const selected = state.dualReviewAvailableModels.find((item) => item.id === select?.value);
  if (!selected) return;
  $(`#dual-model-${sideKey}-title`).textContent = selected.label;
  $(`#dual-model-${sideKey}-meta`).innerHTML = `${escapeHtml(selected.provider_label)} · <code>${escapeHtml(selected.model)}</code>`;
  const keyLabel = $(`#dual-model-${sideKey}-key-label`);
  keyLabel.firstChild.textContent = `${selected.provider_label} API Key`;
  const keyInput = keyLabel.querySelector('input');
  if (keyInput) keyInput.placeholder = isCloudRuntime() ? '留空沿用所选服务商Key；输入新Key可替换，不回显' : `可留空并沿用本机 ${selected.provider_label} Key；输入不会回显`;
  const note = $(`#dual-model-${sideKey}-note`);
  note.textContent = '模型选择尚未保存；提交此卡片后生效。';
  note.classList.remove('error');
  const status = $(`#dual-model-${sideKey}-state`);
  status.textContent = '待保存选择';
  status.classList.remove('is-ready', 'is-missing');
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function renderBackups(backups) {
  const select = $('#backup-select');
  if (!backups.length) {
    select.innerHTML = '<option value="">暂无备份</option>';
    $('#restore-backup').disabled = true;
    return;
  }
  select.innerHTML = backups.map(item => `<option value="${escapeHtml(item.filename)}">${escapeHtml(item.created_at)} · ${escapeHtml(formatBytes(item.bytes))}</option>`).join('');
  $('#restore-backup').disabled = false;
}

async function loadStorage() {
  try {
    await window.HISTORY_GRADER_AUTH_READY;
    if (window.HISTORY_GRADER_AUTH?.role === 'teacher') return;
    const data = await api('/api/storage');
    $('#storage-summary').textContent = [
      `数据目录：${data.data_dir}`,
      `数据占用：${formatBytes(data.data_bytes)} · 备份占用：${formatBytes(data.backup_bytes)}`,
      `当前磁盘剩余：${formatBytes(data.disk_free_bytes)} / ${formatBytes(data.disk_total_bytes)}`,
    ].join('\n');
    if (!isLocalPage()) {
      $('#create-backup').disabled = true;
      $('#refresh-backups').disabled = true;
      $('#restore-backup').disabled = true;
      $('#backup-note').textContent = '备份和恢复只允许在运行程序的电脑本机执行';
      return;
    }
    const backups = await api('/api/backups');
    renderBackups(backups.backups || []);
    $('#backup-note').textContent = backups.backups?.length ? '恢复前会自动生成当前数据的安全备份' : '当前还没有备份';
    await loadRetentionReport();
  } catch (error) {
    $('#storage-summary').textContent = error.message;
    $('#backup-note').textContent = error.message;
  }
}

async function loadRetentionReport() {
  const data = await api('/api/retention');
  $('#retention-days').value = data.retention_days;
  $('#retention-summary').textContent = [
    `原图：${data.total_original_images} 张 · ${formatBytes(data.total_original_bytes)}`,
    `达到期限且已教师确认：${data.hypothetical_eligible_images} 张 · ${formatBytes(data.hypothetical_eligible_bytes)}`,
    `仍保留：${data.held_images} 张 · ${formatBytes(data.held_bytes)}`,
    '当前为 dry-run；未删除任何内容。',
  ].join('\n');
}

async function previewRetention() {
  const button = $('#preview-retention');
  button.disabled = true;
  try {
    const retentionDays = Number($('#retention-days').value);
    const data = await api('/api/retention/preview', {
      method: 'POST',
      body: JSON.stringify({ retention_days: retentionDays, dry_run: true }),
    });
    $('#retention-summary').textContent = [
      `计划期限：${data.retention_days} 天${data.cutoff ? ` · 截止 ${data.cutoff}` : ' · 未设置期限'}`,
      `原图总量：${data.total_original_images} 张 · ${formatBytes(data.total_original_bytes)}`,
      `模拟候选：${data.hypothetical_eligible_images} 张 · ${formatBytes(data.hypothetical_eligible_bytes)}`,
      `保留：${data.held_images} 张 · ${formatBytes(data.held_bytes)}`,
      `操作结果：${data.deletion_performed ? '异常：执行了删除' : 'dry-run，未删除任何数据'}`,
      data.held_reason,
    ].join('\n');
  } catch (error) {
    $('#retention-summary').textContent = error.message;
  } finally { button.disabled = false; }
}

async function createMobileToken() {
  const button = $('#generate-mobile-token');
  button.disabled = true;
  try {
    const data = await api('/api/mobile-token', { method: 'POST', body: '{}' });
    $('#mobile-token-value').textContent = data.token;
    $('#mobile-token-note').textContent = '访问码有效 45 分钟；重启或撤销后失效。可直接复制下方完整手机地址。';
    setReadiness('#readiness-connection', '访问码已生成', 'ready');
    renderMobileAccessUrl(data.token);
  } catch (error) {
    alert(error.message);
  } finally { button.disabled = false; }
}

async function revokeMobileToken() {
  try {
    await api('/api/mobile-token/revoke', { method: 'POST', body: '{}' });
    $('#mobile-token-value').textContent = '—';
    $('#mobile-token-note').textContent = '手机临时访问码已撤销';
    setReadiness('#readiness-connection', state.mobileNetworkAddresses.length ? '网络已识别' : '可选', state.mobileNetworkAddresses.length ? 'ready' : '');
    renderMobileAccessUrl('');
  } catch (error) { alert(error.message); }
}

async function refreshMobileNetwork() {
  if (isCloudAdmin()) return;
  const button = $('#refresh-mobile-network');
  if (!button) return;
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = '检测中…';
  try {
    const data = await api('/api/status?refresh=1');
    renderMobileNetwork(data);
    const token = $('#mobile-token-value')?.textContent?.trim() || '';
    if (token && token !== '—') renderMobileAccessUrl(token);
  } catch (error) {
    $('#mobile-network-note').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function createBackup() {
  const button = $('#create-backup');
  button.disabled = true;
  button.textContent = '备份中…';
  try {
    const data = await api('/api/backup/create', { method: 'POST', body: '{}' });
    $('#backup-note').textContent = `备份完成：${data.filename}`;
    await loadStorage();
  } catch (error) {
    $('#backup-note').textContent = error.message;
    alert(error.message);
  } finally { button.disabled = false; button.textContent = '立即备份'; }
}

async function restoreBackup() {
  const filename = $('#backup-select').value;
  if (!filename) return alert('请先选择一个备份');
  if (!window.confirm(`恢复“${filename}”会替换当前数据库和照片；程序会先自动备份当前数据。确认继续吗？`)) return;
  const button = $('#restore-backup');
  button.disabled = true;
  button.textContent = '恢复中…';
  try {
    const data = await api('/api/backup/restore', { method: 'POST', body: JSON.stringify({ filename }) });
    $('#backup-note').textContent = `${data.message} 恢复前备份：${data.safety_backup}`;
    await loadStorage();
    alert(`${data.message}\n恢复前安全备份：${data.safety_backup}`);
  } catch (error) {
    $('#backup-note').textContent = error.message;
    alert(error.message);
  } finally { button.disabled = false; button.textContent = '恢复选中的备份'; }
}

function rosterClassOptions() {
  return '<option value="">不关联花名册</option>' + state.rosterClasses
    .map(item => `<option value="${escapeHtml(item.class_name)}">${escapeHtml(item.class_name)}（${escapeHtml(item.student_count)}人）</option>`)
    .join('');
}

async function loadRosterClasses(preferredClass = '') {
  const data = await api('/api/rosters');
  state.rosterClasses = data.classes || [];
  ['#class-select', '#batch-class-select'].forEach(selector => {
    const select = $(selector);
    const oldValue = preferredClass || select.value;
    select.innerHTML = rosterClassOptions();
    if (state.rosterClasses.some(item => item.class_name === oldValue)) select.value = oldValue;
  });
  $('#roster-classes').textContent = state.rosterClasses.length
    ? state.rosterClasses.map(item => `${item.class_name}：${item.student_count}人`).join('　')
    : '当前没有班级名单';
  setReadiness('#readiness-roster', state.rosterClasses.length ? `${state.rosterClasses.length} 个班级` : '可选', state.rosterClasses.length ? 'ready' : '');
  await loadRosterEditor(preferredClass);
  await loadClassExams();
}

async function loadRosterStudents(className) {
  if (!className) {
    state.rosterStudents = [];
    return;
  }
  const data = await api(`/api/roster-students?class_name=${encodeURIComponent(className)}`);
  state.rosterStudents = (data.students || []).filter(student => student.enabled !== 0);
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function importRoster(event) {
  event.preventDefault();
  const file = $('#roster-file').files[0];
  if (!file) return alert('请选择 CSV、TSV 或 XLSX 花名册');
  const button = event.submitter;
  button.disabled = true;
  button.textContent = '导入中…';
  try {
    const importPayload = { filename: file.name, data_url: await fileToDataUrl(file), default_class_name: $('#roster-default-class').value };
    const preview = await api('/api/import-roster', { method: 'POST', body: JSON.stringify({ ...importPayload, preview_only: true }) });
    const p = preview.preview;
    $('#roster-note').textContent = `导入预览：新增 ${p.new_count}，已有 ${p.existing_count}，重复名单记录 ${p.duplicate_count}，缺失或无效 ${p.missing_count}，姓名冲突 ${p.conflict_count}。`;
    if (!p.new_count || !window.confirm(`${$('#roster-note').textContent}\n只新增有效记录，已有学生ID和历史绑定保留。是否导入？`)) return;
    const data = await api('/api/import-roster', {
      method: 'POST',
      body: JSON.stringify(importPayload),
    });
    $('#roster-note').textContent = data.message;
    $('#roster-file').value = '';
    await loadRosterClasses(data.classes?.length === 1 ? data.classes[0] : '');
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = '导入花名册';
  }
}

function wait(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function restartService() {
  if (!window.confirm('重启只会重启本地服务，不会删除题目、照片或批改记录。继续吗？')) return;
  const button = $('#restart-service');
  button.disabled = true;
  button.textContent = '重启中…';
  try {
    await api('/api/restart', { method: 'POST' });
    setStatus('服务正在重启…');
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await wait(500);
      try {
        const data = await api('/api/status');
        setStatus(modelStatusText(data), Boolean(data.api_key_error));
        window.location.reload();
        return;
      } catch (error) { /* The old process may still be releasing port 8765. */ }
    }
    throw new Error('服务重启后暂时无法连接，请检查运行窗口');
  } catch (error) {
    setStatus(error.message, true);
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = '重启服务';
  }
}

async function shutdownService() {
  if (!window.confirm('安全退出只会关闭本地服务，不会删除题目、照片或批改记录。确认退出吗？')) return;
  const button = $('#shutdown-service');
  button.disabled = true;
  button.textContent = '退出中…';
  try {
    await api('/api/shutdown', { method: 'POST' });
    setStatus('服务已请求安全退出，请拔出 U 盘前确认页面无法连接');
  } catch (error) {
    setStatus(error.message, true);
    alert(error.message);
    button.disabled = false;
    button.textContent = '安全退出';
  }
}

async function fetchCustomModelList() {
  if (!isLocalPage() || window.HISTORY_GRADER_AUTH?.required || isCloudAdmin()) return;
  const base = $('#model-base-url').value.trim();
  const button = $('#fetch-custom-models');
  button.disabled = true;
  $('#custom-model-list').replaceChildren();
  $('#custom-model-list').hidden = true;
  $('#custom-model-status').textContent = '正在查询所填接口…';
  try {
    const data = await api('/api/custom-models/list', {method:'POST', body:JSON.stringify({base_url:base,api_key:$('#api-key').value.trim(),confirm_send:true})});
    if (base !== $('#model-base-url').value.trim()) return;
    const select = $('#custom-model-list');
    const blank = document.createElement('option');blank.value='';blank.textContent='选择模型ID';select.append(blank);
    (data.models || []).forEach(id=>{const option=document.createElement('option');option.value=id;option.textContent=id;select.append(option)});
    select.hidden = false;
    $('#custom-model-status').textContent = `读取 ${data.models.length} 个模型；列表不代表具备图片批改能力，请核对服务商说明。`;
  } catch(error) { $('#custom-model-status').textContent = error.message; }
  finally { $('#api-key').value='';button.disabled=false; }
}
$('#fetch-custom-models')?.addEventListener('click', fetchCustomModelList);
$('#custom-model-list')?.addEventListener('change', event=>{if(event.target.value)$('#model-id').value=event.target.value});

async function saveCustomModelPreset() {
  if (!isLocalPage() || isCloudAdmin()) {
    alert('自定义模型预设只能在电脑本机保存');
    return;
  }
  const selected = modelPresetById($('#model-preset').value);
  const existingId = isUserModelPreset(selected) ? selected.id : '';
  const label = $('#custom-preset-label').value.trim();
  if (!label) {
    alert('请填写自定义模型预设名称');
    $('#custom-preset-label').focus();
    return;
  }
  const button = $('#save-custom-preset');
  button.disabled = true;
  button.textContent = existingId ? '更新中…' : '保存中…';
  try {
    const data = await api('/api/custom-models/save', {
      method: 'POST',
      body: JSON.stringify({
        preset_id: existingId,
        label,
        base_url: $('#model-base-url').value.trim(),
        model: $('#model-id').value.trim(),
        note: $('#custom-preset-note').value.trim(),
      }),
    });
    state.modelPresets = data.model_presets || state.modelPresets;
    renderModelPresets(data.preset.id);
    applyModelPreset(data.preset.id, true);
    $('#custom-preset-status').textContent = data.message || '自定义模型预设已保存。';
    setStatus('自定义预设已保存；如需启用，请点击“保存模型配置”');
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
    button.textContent = '保存为自定义预设';
    syncCustomPresetEditor(modelPresetById($('#model-preset').value));
  }
}

async function deleteCustomModelPreset() {
  if (!isLocalPage() || isCloudAdmin()) {
    alert('自定义模型预设只能在电脑本机删除');
    return;
  }
  const preset = modelPresetById($('#model-preset').value);
  if (!isUserModelPreset(preset)) return;
  if (!window.confirm(`确定删除自定义预设“${preset.label}”吗？不会删除本机 Key 或批改数据。`)) return;
  const button = $('#delete-custom-preset');
  button.disabled = true;
  try {
    await api('/api/custom-models/delete', {
      method: 'POST',
      body: JSON.stringify({ preset_id: preset.id }),
    });
    await loadStatus();
    $('#custom-preset-status').textContent = '自定义模型预设已删除；本机 Key 和历史数据未受影响。';
  } catch (error) {
    alert(error.message);
    syncCustomPresetEditor(preset);
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const input = $('#api-key');
  const button = event.submitter;
  button.disabled = true;
  button.textContent = '保存中…';
  try {
    const settingsPayload = isCloudAdmin()
      ? { preset_id: $('#model-preset').value }
      : {
        preset_id: $('#model-preset').value,
        base_url: $('#model-base-url').value,
        model: $('#model-id').value,
        api_key: input.value,
        remember_key: $('input[name="key-storage"]:checked')?.value !== 'session',
      };
    const data = await api('/api/settings', { method: 'POST', body: JSON.stringify(settingsPayload) });
    input.value = '';
    $('#settings-note').textContent = data.message || '模型配置已保存。';
    setStatus('模型配置已保存，可以直接开始批改');
    await loadStatus();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; button.textContent = '保存模型配置'; }
}

let examReportData = null;
let examReportMarkdown = '';
let examReportGeneration = 0;
let examReportContext = null;

function examReportNumber(value, suffix = '') {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}${suffix}` : '—';
}

function examReportMarkdownDocument(data, markdown = '') {
  const lines = [
    `# ${data.assignment_name || '试卷'} · 试卷分析报告`,
    '',
    '> 本报告由确定性统计和（可选的）AI 教学分析组成；AI 不重新评分，不接收学生姓名或照片。',
    '',
    '## 一、数据口径',
    '',
    `- 统计人数：${examReportNumber(data.student_count)}`,
    `- 已确认成绩：${examReportNumber(data.confirmed_count)}；未确认（使用 AI 建议分）：${examReportNumber(data.unconfirmed_count)}；受阻记录：${examReportNumber(data.blocked_count)}`,
    `- 满分：${examReportNumber(data.max_total)}；平均分：${examReportNumber(data.average)}；平均得分率：${examReportNumber(data.score_rate, '%')}`,
    ...(data.warnings || []).map(warning => `> 口径提示：${warning}`),
    '',
    '## 二、总体表现',
    '',
    '| 分数段 | 人数 |',
    '|---|---:|',
    ...Object.entries(data.distribution || {}).map(([label, count]) => `| ${label} | ${count} |`),
    '',
    '## 三、分题统计',
    '',
    '| 小题 | 满分 | 已评人数 | 未作答候选 | 平均分 | 得分率 |',
    '|---|---:|---:|---:|---:|---:|',
    ...(data.questions || []).map((q) => `| ${q.label ?? '—'} | ${examReportNumber(q.max)} | ${examReportNumber(q.answered)} | ${examReportNumber(q.unanswered)} | ${examReportNumber(q.average)} | ${examReportNumber(q.score_rate, '%')} |`),
    '',
    '## 四、评分点命中与共性问题',
    '',
    ...(data.common_misses || []).length
      ? (data.common_misses || []).map((item) => `- ${item.question ?? '—'} · ${item.label ?? '—'}：未命中 ${examReportNumber(item.missed)} 次，命中率 ${examReportNumber(item.rate, '%')}`)
      : ['- 暂无可统计的评分点缺口。'],
    '',
    '## 五、教学评一体化分析',
    '',
    data.objective?.available ? '' : `> ${data.objective?.reason || '客观题数据未接入。'}`,
    '',
    markdown || '（尚未生成 AI 分析。请填写学习目标后点击“生成 AI 分析”。）',
    '',
    '---',
    '报告只基于当前已完成记录；未确认成绩仍使用 AI 建议分，请以教师最终确认为准。',
  ];
  return lines.join('\n');
}

function resetExamReportView() {
  examReportData = null;
  examReportMarkdown = '';
  $('#exam-report-title').textContent = '试卷分析报告';
  $('#exam-report-status').textContent = '';
  $('#exam-report-body').innerHTML = '';
  $('#exam-report-ai-box').hidden = true;
  $('#exam-report-objectives').value = '';
  $('#exam-report-ai-output').textContent = '';
  $('#exam-report-ai-output').hidden = true;
  $('#download-exam-report').disabled = true;
  $('#print-exam-report').disabled = true;
  const button = $('#generate-exam-report-ai');
  button.disabled = false;
  button.textContent = '生成 AI 分析';
}

function isCurrentExamReport(generation, key) {
  return generation === examReportGeneration && examReportContext?.key === key && $('#exam-report-dialog').open;
}

function renderExamReport(data) {
  examReportData = data;
  const questions = Array.isArray(data.questions) ? data.questions : [];
  const rows = questions.map((q) => `<tr><td>${escapeHtml(q.label ?? '—')}</td><td>${escapeHtml(examReportNumber(q.max))}</td><td>${escapeHtml(examReportNumber(q.answered))}</td><td>${escapeHtml(examReportNumber(q.unanswered))}</td><td>${escapeHtml(examReportNumber(q.average))}</td><td>${escapeHtml(examReportNumber(q.score_rate, '%'))}</td></tr>`).join('');
  const criteriaRows = questions.map((q) => {
    const points = (Array.isArray(q.criteria) ? q.criteria : []).map((c) => `<li>${escapeHtml(c.label ?? '—')}：命中 ${escapeHtml(examReportNumber(c.credited))}/${escapeHtml(examReportNumber(c.scored))}（${escapeHtml(examReportNumber(c.rate, '%'))}）；缺失 ${escapeHtml(examReportNumber(c.missing))}</li>`).join('');
    return points ? `<details><summary>${escapeHtml(q.label ?? '—')} · 评分点命中</summary><ul>${points}</ul></details>` : '';
  }).join('');
  const commonMisses = Array.isArray(data.common_misses) ? data.common_misses : [];
  const misses = commonMisses.length
    ? `<ul>${commonMisses.map((item) => `<li>${escapeHtml(item.question ?? '—')} · ${escapeHtml(item.label ?? '—')}：未命中 ${escapeHtml(examReportNumber(item.missed))} 次，命中率 ${escapeHtml(examReportNumber(item.rate, '%'))}</li>`).join('')}</ul>`
    : '<p class="muted">暂无可统计的评分点缺口。</p>';
  const distribution = Object.entries(data.distribution || {}).map(([label, count]) => `<tr><td>${escapeHtml(label)}</td><td>${escapeHtml(count)}</td></tr>`).join('');
  $('#exam-report-body').innerHTML = `
    <div class="exam-report-summary">
      <article><span>统计人数</span><strong>${escapeHtml(examReportNumber(data.student_count))}</strong></article>
      <article><span>平均分</span><strong>${escapeHtml(examReportNumber(data.average))} / ${escapeHtml(examReportNumber(data.max_total))}</strong></article>
      <article><span>平均得分率</span><strong>${escapeHtml(examReportNumber(data.score_rate, '%'))}</strong></article>
      <article><span>未确认成绩</span><strong>${escapeHtml(examReportNumber(data.unconfirmed_count))}</strong></article>
    </div>
    <p class="hint">已确认 ${escapeHtml(examReportNumber(data.confirmed_count))} 份；未确认记录使用 AI 建议分；受阻记录 ${escapeHtml(examReportNumber(data.blocked_count))} 份。</p>
    ${(data.warnings || []).map(warning => `<p class="hint">${escapeHtml(warning)}</p>`).join('')}
    <h3>分数段分布</h3>
    <table class="report-table"><thead><tr><th>分数段</th><th>人数</th></tr></thead><tbody>${distribution || '<tr><td colspan="2">暂无可统计的分数段数据。</td></tr>'}</tbody></table>
    <h3>分题统计</h3>
    <table class="report-table"><thead><tr><th>小题</th><th>满分</th><th>已评</th><th>未作答候选</th><th>平均分</th><th>得分率</th></tr></thead><tbody>${rows || '<tr><td colspan="6">暂无可统计的已完成答卷。</td></tr>'}</tbody></table>
    <h3>评分点命中</h3>${criteriaRows || '<p class="muted">暂无可统计的评分点。</p>'}
    <h3>共性问题</h3>${misses}
    <div class="exam-report-objective-note"><strong>客观题统计：</strong>${escapeHtml(data.objective?.reason || '当前批次未接入客观题批改数据。')}</div>`;
  $('#exam-report-ai-box').hidden = false;
  $('#exam-report-ai-output').hidden = true;
  $('#download-exam-report').disabled = false;
  $('#print-exam-report').disabled = false;
  examReportMarkdown = '';
  $('#exam-report-status').textContent = '本地统计已生成，可直接下载或打印；填写学习目标后才需要调用 AI。';
}

async function openExamReport(assignmentId, examId = '') {
  const dialog = $('#exam-report-dialog');
  const generation = ++examReportGeneration;
  const key = `${String(assignmentId)}\u0000${String(examId || '')}`;
  examReportContext = { key, assignmentId, examId: examId || '' };
  resetExamReportView();
  $('#exam-report-status').textContent = '正在读取统计…';
  if (!dialog.open) dialog.showModal();
  try {
    const data = await api('/api/exam-report', {
      method: 'POST',
      body: JSON.stringify({ assignment_id: assignmentId, exam_id: examId || '' }),
    });
    if (!isCurrentExamReport(generation, key)) return;
    if (!data || !data.report || typeof data.report !== 'object') throw new Error('报告统计数据缺失，无法生成报告。');
    $('#exam-report-title').textContent = `${data.report.assignment_name || '试卷'} · 试卷分析报告`;
    renderExamReport(data.report);
  } catch (error) {
    if (isCurrentExamReport(generation, key)) $('#exam-report-status').textContent = `无法读取试卷统计：${error.message}`;
  }
}

async function generateExamReportAi() {
  const context = examReportContext;
  if (!examReportData || !context) return;
  const generation = examReportGeneration;
  const key = context.key;
  const objectives = $('#exam-report-objectives').value.trim();
  if (!window.confirm('将把匿名聚合统计和学习目标发送给已配置的 AI-A 生成分析，不包含学生姓名或照片；会产生模型费用。继续？')) return;
  if (!isCurrentExamReport(generation, key)) return;
  const button = $('#generate-exam-report-ai');
  button.disabled = true;
  button.textContent = 'AI 分析中…';
  $('#exam-report-ai-output').textContent = '';
  $('#exam-report-ai-output').hidden = true;
  examReportMarkdown = '';
  $('#exam-report-status').textContent = 'AI 正在根据统计生成教学评一体化分析…';
  try {
    const data = await api('/api/exam-report/ai', {
      method: 'POST',
      body: JSON.stringify({
        assignment_id: context.assignmentId,
        exam_id: context.examId,
        objectives,
        confirm_send: true,
      }),
    });
    if (!isCurrentExamReport(generation, key)) return;
    if (!data || !data.report || typeof data.report !== 'object' || typeof data.markdown !== 'string') throw new Error('AI 报告数据缺失。');
    examReportData = data.report;
    examReportMarkdown = data.markdown;
    $('#exam-report-ai-output').textContent = examReportMarkdown;
    $('#exam-report-ai-output').hidden = false;
    $('#download-exam-report').disabled = false;
    $('#print-exam-report').disabled = false;
    $('#exam-report-status').textContent = `AI 分析已生成 · 模型：${data.model || 'AI-A'}。导出前请核对数据与结论。`;
  } catch (error) {
    if (isCurrentExamReport(generation, key)) $('#exam-report-status').textContent = `AI 分析失败：${error.message}`;
  } finally {
    if (isCurrentExamReport(generation, key)) {
      button.disabled = false;
      button.textContent = '生成 AI 分析';
    }
  }
}

function downloadExamReport() {
  if (!examReportData) return;
  const markdown = examReportMarkdownDocument(examReportData, examReportMarkdown);
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${examReportData.assignment_name || '试卷报告'}-教学评一体化报告.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function printExamReport() {
  if (!examReportData) return;
  const markdown = examReportMarkdownDocument(examReportData, examReportMarkdown);
  const win = window.open('', '_blank');
  if (!win) return alert('浏览器阻止了打印窗口，请允许弹窗后重试。');
  win.document.write(`<!doctype html><html><head><meta charset="utf-8"><title>试卷分析报告</title><style>body{font-family:"Microsoft YaHei",sans-serif;margin:32px;line-height:1.75;color:#17202a}pre{white-space:pre-wrap;font-family:inherit}h1{font-size:22px}@media print{body{margin:12mm}}</style></head><body><pre>${escapeHtml(markdown)}</pre></body></html>`);
  win.document.close();
  win.focus();
  setTimeout(() => win.print(), 300);
}

$('#close-exam-report')?.addEventListener('click', () => $('#exam-report-dialog').close());
$('#exam-report-dialog')?.addEventListener('close', () => {
  examReportGeneration += 1;
  examReportContext = null;
  resetExamReportView();
});
$('#generate-exam-report-ai')?.addEventListener('click', generateExamReportAi);
$('#download-exam-report')?.addEventListener('click', downloadExamReport);
$('#print-exam-report')?.addEventListener('click', printExamReport);

async function loadAssignments() {
  const data = await api('/api/assignments');
  state.assignments = data.assignments || [];
  setReadiness('#readiness-assignment', state.assignments.length ? `${state.assignments.length} 道题` : '待录入', state.assignments.length ? 'ready' : 'pending');
  const selectedAssignmentIds = [$('#assignment-select')?.value || '', $('#batch-assignment-select')?.value || ''];
  const options = state.assignments.length
    ? state.assignments.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('')
    : '<option value="">请先保存一道题</option>';
  [$('#assignment-select'), $('#batch-assignment-select')].forEach((select, index) => {
    select.innerHTML = options;
    const preferred = selectedAssignmentIds[index];
    if (preferred && state.assignments.some(item => String(item.id) === preferred)) select.value = preferred;
  });
  showGradingAssignmentContext($('#batch-grading-pane')?.hidden ? $('#assignment-select')?.value : $('#batch-assignment-select')?.value);
  const history = $('#assignment-history');
  history.innerHTML = state.assignments.length ? state.assignments.map(item => `
    <div class="history-item">
      <div><h3>${escapeHtml(item.name)}</h3><p>${escapeHtml(item.question.slice(0, 90))} · 已提交 ${item.submission_count} 份</p></div>
      <div class="history-actions">${isLocalPage() ? `<a class="button secondary" href="/api/export.csv?assignment_id=${encodeURIComponent(item.id)}">导出实名成绩</a><a class="button secondary" href="/api/export-anonymous.csv?assignment_id=${encodeURIComponent(item.id)}">导出匿名成绩</a><button class="button secondary exam-report-button" type="button" data-assignment-id="${escapeHtml(item.id)}" data-exam-id="${escapeHtml(item.exam_id || '')}">试卷报告</button>` : ''}${isLocalPage() && item.exam_id ? `<button class="button secondary open-exam-layout-profile" type="button" data-exam-id="${escapeHtml(item.exam_id)}">答题区设置</button>` : ''}${isLocalPage() ? `<button class="button secondary view-submissions" type="button" data-id="${escapeHtml(item.id)}">查看批改记录</button>` : ''}<button class="button danger delete-assignment" type="button" data-id="${escapeHtml(item.id)}" data-name="${escapeHtml(item.name)}">删除整题</button></div>
      ${isLocalPage() ? `<div class="history-submissions" data-assignment-id="${escapeHtml(item.id)}" hidden></div>` : ''}
    </div>`).join('') : '<p class="muted">暂无记录</p>';
  history.querySelectorAll('.delete-assignment').forEach(button => {
    button.addEventListener('click', () => deleteAssignment(button.dataset.id, button.dataset.name));
  });
  history.querySelectorAll('.view-submissions').forEach(button => {
    button.addEventListener('click', () => toggleAssignmentSubmissions(button));
  });
  history.querySelectorAll('.exam-report-button').forEach(button => {
    button.addEventListener('click', () => openExamReport(button.dataset.assignmentId, button.dataset.examId || ''));
  });
  history.querySelectorAll('.open-exam-layout-profile').forEach(button => {
    button.addEventListener('click', () => openExamLayoutProfile(button.dataset.examId));
  });
}

const normalizedCoordinate = (value) => Math.max(0, Math.min(1, value));

function layoutProfileStatusLabel(status) {
  return ({ missing: '未设置', partial: '部分设置', complete: '已完成' })[status] || '未设置';
}

function objectiveQuestionNumber(value) {
  const match = String(value ?? '').match(/^Q?(\d+)$/i);
  return match ? String(Number(match[1])) : '';
}

function layoutProfileProgressText(data, regions = []) {
  const expected = data?.subjective_question_ids || [];
  const configured = new Set(regions
    .filter(region => !region.region_type || region.region_type === 'subjective_answer')
    .map(region => String(region.question_id || '')).filter(Boolean));
  const count = expected.filter(questionId => configured.has(questionId)).length;
  const status = count === expected.length ? 'complete' : count === 0 ? 'missing' : 'partial';
  const objectiveExpected = (data?.objective_question_ids || []).map(String);
  const objectiveConfigured = new Set();
  regions.forEach(region => {
    if (!region.region_type || region.region_type === 'subjective_answer') return;
    (region.question_mapping || []).forEach(value => {
      const number = objectiveQuestionNumber(value);
      if (number) objectiveConfigured.add(number);
    });
  });
  const objectiveCount = objectiveExpected.filter(questionId => objectiveConfigured.has(objectiveQuestionNumber(questionId))).length;
  const objectiveTotal = objectiveExpected.length;
  const objectiveStatus = objectiveTotal
    ? (objectiveCount === objectiveTotal ? 'complete' : objectiveCount === 0 ? 'missing' : 'partial')
    : 'not_available';
  return { count, total: expected.length, status, label: layoutProfileStatusLabel(status), objectiveCount, objectiveTotal, objectiveStatus };
}

async function openExamLayoutProfile(examId) {
  if (!isLocalPage()) return;
  $('#main-tab-history')?.click();
  const panel = $('#exam-layout-profile-panel');
  panel.hidden = false;
  $('#exam-layout-profile-workspace').hidden = true;
  $('#exam-layout-profile-summary').innerHTML = '<p class="muted">正在读取正式考试和 参考试卷页…</p>';
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    const data = await api(`/api/exam-layout-profile?exam_id=${encodeURIComponent(examId)}`);
    state.studentAutoCropResult = null;
    $('#student-auto-crop-result').innerHTML = '';
    state.examLayoutProfile = {
      examId: data.exam_id,
      data,
      regions: (data.layout_profile?.regions || []).map(region => structuredClone(region)),
      selectedQuestionId: '',
      objectiveActive: false,
      selectedObjectiveRegionId: '',
      pageNumber: data.layout_profile?.regions?.[0]?.page_number ?? data.references?.[0]?.page_number ?? null,
      dirty: false,
      dragging: null,
    };
    renderExamLayoutProfile();
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    $('#exam-layout-profile-summary').innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`;
  }
}

function renderExamLayoutProfile() {
  const model = state.examLayoutProfile;
  if (!model) return;
  const data = model.data;
  const panel = $('#exam-layout-profile-panel');
  $('#exam-layout-profile-title').textContent = `答题区设置 · ${data.exam_name}`;
  const progress = layoutProfileProgressText(data, model.regions);
  const pdfLine = data.has_pdf_reference
    ? `参考试卷页：已加载 ${data.references.length} 页（${data.references.map(page => `第 ${page.page_number} 页${page.page_role === 'answer_sheet' ? '（答题卡）' : ''}`).join('、')}）`
    : '参考试卷页：未加载';
  let warning = '';
  if (!data.has_pdf_reference) {
    warning = '<div class="exam-layout-notice">当前考试没有打印 参考试卷页，无法设置基于 PDF 坐标的答题区。请返回考试列表，之后可继续使用现有批改流程。</div>';
  } else if (data.pdf_hash_mismatch) {
    warning = '<div class="exam-layout-notice is-warning">正式考试记录中的 PDF 与当前保存的 PDF 文件 hash 不一致。请先核对或重新导入 PDF，本页暂不允许保存。</div>';
  } else if (data.profile_hash_mismatch) {
    warning = '<div class="exam-layout-notice is-warning">当前 PDF 与旧答题区模板来源不一致，旧 ROI 已停用。请在当前 PDF 上重新框选并保存。</div>';
  } else if (data.profile_invalid) {
    warning = '<div class="exam-layout-notice is-warning">已保存的答题区数据无法通过现有 layout_profile 校验，请重新框选需要的答题区。</div>';
  }
  const objectiveSummary = progress.objectiveTotal
    ? `<div><strong>客观题答题区</strong><span>${progress.objectiveCount} / ${progress.objectiveTotal} · ${escapeHtml(layoutProfileStatusLabel(progress.objectiveStatus))}</span></div>`
    : '';
  $('#exam-layout-profile-summary').innerHTML = `<div class="exam-layout-summary-card"><div><strong>考试名称</strong><span>${escapeHtml(data.exam_name)}</span></div><div><strong>参考试卷页</strong><span>${escapeHtml(pdfLine)}</span></div><div><strong>主观题答题区</strong><span>${progress.count} / ${progress.total} · ${escapeHtml(progress.label)}</span></div>${objectiveSummary}</div>${warning}`;
  const workspace = $('#exam-layout-profile-workspace');
  workspace.hidden = !data.has_pdf_reference || data.pdf_hash_mismatch || !data.references.length;
  const cropTestPanel = $('#student-auto-crop-test-panel');
  cropTestPanel.hidden = !data.has_pdf_reference;
  $('#student-auto-crop-run').disabled = Boolean(model.dirty);
  $('#student-auto-crop-note').textContent = model.dirty
    ? '请先保存当前答题区修改，再测试学生答卷。'
    : `照片只在${isCloudRuntime() ? '服务器内' : '本机'}处理，不调用模型；未通过质量检查的页面会停止处理，其他通过的页面仍可生成答案图。`;
  if (!data.has_pdf_reference || !data.references.length) return;
  const pageSelect = $('#exam-layout-profile-page');
  const previousPage = model.pageNumber;
  pageSelect.innerHTML = data.references.map(page => `<option value="${page.page_number}">第 ${page.page_number} 页</option>`).join('');
  if (!data.references.some(page => Number(page.page_number) === Number(previousPage))) model.pageNumber = data.references[0].page_number;
  pageSelect.value = String(model.pageNumber);
  const questions = data.subjective_question_ids || [];
  $('#exam-layout-profile-question-list').innerHTML = questions.length ? questions.map(questionId => {
    const region = model.regions.find(item => String(item.question_id) === questionId);
    const selected = model.selectedQuestionId === questionId;
    return `<button type="button" class="exam-layout-question${selected ? ' is-selected' : ''}${region ? ' is-configured' : ''}" data-question-id="${escapeHtml(questionId)}" aria-pressed="${selected}"><strong>${escapeHtml(questionId)}</strong><small>${region ? `已设置 · PDF第 ${region.page_number} 页` : '未设置'}</small></button>`;
  }).join('') : '<p class="muted">本场考试没有识别到需要配置答题区的主观小题。</p>';
  const objectiveIds = (data.objective_question_ids || []).map(String);
  renderExamLayoutObjectiveControls();
  const objectiveStart = objectiveIds.length ? String($('#exam-layout-objective-start').value || '') : '';
  const objectiveEnd = objectiveIds.length ? String($('#exam-layout-objective-end').value || '') : '';
  const selectedRegion = model.regions.find(item => String(item.question_id) === model.selectedQuestionId);
  $('#exam-layout-profile-selection-label').textContent = model.objectiveActive
    ? `客观题：第 ${objectiveStart}–${objectiveEnd} 题 · 在答题卡上拖出这一行答案区域。`
    : model.selectedQuestionId
      ? selectedRegion ? `${model.selectedQuestionId} · 当前绑定 PDF 第 ${selectedRegion.page_number} 页；重新拖动即可覆盖。` : `当前小题：${model.selectedQuestionId} · 请选择正确试卷页并拖出答题区。`
      : '先选择主观题小题或客观题起止题号，再在页面上拖出答题区。';
  $('#delete-exam-layout-region').disabled = !selectedRegion;
  $('#exam-layout-profile-note').textContent = model.dirty
    ? '有未保存修改。框选、删除仅影响本页草稿，点击“保存答题区设置”后才写入正式考试。'
    : '在 PDF 页上拖动鼠标框选；保存后，本场考试的其他学生答卷可复用这套版式坐标。';
  const objectivePart = objectiveIds.length ? ` · 客观题 ${progress.objectiveCount} / ${progress.objectiveTotal}` : '';
  $('#exam-layout-profile-progress').textContent = `已配置 ${progress.count} / ${progress.total} 个主观小题${objectivePart} · ${progress.label}`;
  const saveButton = $('#save-exam-layout-profile');
  saveButton.disabled = data.pdf_hash_mismatch || (!questions.length && !objectiveIds.length);
  saveButton.textContent = model.dirty ? '保存答题区设置' : '保存答题区设置';
  const activePage = data.references.find(page => Number(page.page_number) === Number(model.pageNumber));
  const image = $('#exam-layout-reference-image');
  if (activePage && (image.dataset.pageNumber !== String(activePage.page_number) || image.dataset.examId !== model.examId)) {
    image.dataset.pageNumber = String(activePage.page_number);
    image.dataset.examId = model.examId;
    image.src = `${activePage.preview_url}&v=${encodeURIComponent(activePage.reference_sha256 || model.data.source_pdf_sha256)}`;
    image.alt = `${data.exam_name} PDF 第 ${activePage.page_number} 页 reference`;
  }
  renderExamLayoutOverlays();
  panel.dataset.status = progress.status;
}

function renderExamLayoutObjectiveControls() {
  const model = state.examLayoutProfile;
  const section = $('#exam-layout-profile-objective');
  if (!model || !section) return;
  const ids = (model.data.objective_question_ids || []).map(String);
  section.hidden = !ids.length;
  if (!ids.length) return;
  const startSelect = $('#exam-layout-objective-start');
  const endSelect = $('#exam-layout-objective-end');
  const previousStart = String(startSelect.value || '');
  const previousEnd = String(endSelect.value || '');
  startSelect.innerHTML = ids.map(id => `<option value="${escapeHtml(id)}">第 ${escapeHtml(id)} 题</option>`).join('');
  const startValue = ids.includes(previousStart) ? previousStart : ids[0];
  startSelect.value = startValue;
  const startIndex = ids.indexOf(startValue);
  const endOptions = ids.slice(startIndex);
  endSelect.innerHTML = endOptions.map(id => `<option value="${escapeHtml(id)}">第 ${escapeHtml(id)} 题</option>`).join('');
  const previousEndIndex = ids.indexOf(previousEnd);
  endSelect.value = previousEndIndex >= startIndex ? previousEnd : endOptions[endOptions.length - 1];
  const drawButton = $('#exam-layout-objective-draw');
  drawButton.classList.toggle('is-active', Boolean(model.objectiveActive));
  drawButton.textContent = model.objectiveActive ? '取消框选' : '框选答案行';
  const objectiveRegions = model.regions.filter(region => region.region_type && region.region_type !== 'subjective_answer');
  $('#exam-layout-objective-list').innerHTML = objectiveRegions.length
    ? objectiveRegions.map(region => {
      const mapping = (region.question_mapping || []).map(objectiveQuestionNumber).filter(Boolean);
      const label = mapping.length ? `第 ${mapping[0]}–${mapping[mapping.length - 1]} 题` : '客观题区域';
      return `<div class="exam-layout-objective-item"><span>${escapeHtml(label)} · PDF第 ${Number(region.page_number)} 页 · ${Number(region.rows) || 1} 行 × ${Number(region.cols) || mapping.length} 列</span><button type="button" class="button secondary exam-layout-objective-delete" data-region-id="${escapeHtml(region.region_id)}">删除</button></div>`;
    }).join('')
    : '<p class="muted">尚未设置客观题答案行区域。</p>';
}

function renderExamLayoutOverlays() {
  const model = state.examLayoutProfile;
  const overlays = $('#exam-layout-region-overlays');
  const dragRect = $('#exam-layout-drag-rect');
  if (!model || !overlays) return;
  overlays.innerHTML = model.regions.filter(region => Number(region.page_number) === Number(model.pageNumber)).map(region => {
    const bbox = region.bbox || {};
    const x1 = Number(bbox.x1); const y1 = Number(bbox.y1); const x2 = Number(bbox.x2); const y2 = Number(bbox.y2);
    const objective = Boolean(region.region_type && region.region_type !== 'subjective_answer');
    const mapping = (region.question_mapping || []).map(objectiveQuestionNumber).filter(Boolean);
    const label = objective
      ? `客观 ${mapping[0] ?? ''}–${mapping[mapping.length - 1] ?? ''}`
      : String(region.question_id || '');
    const active = objective ? model.selectedObjectiveRegionId === region.region_id : model.selectedQuestionId === region.question_id;
    return `<div class="exam-layout-region-box${active ? ' is-active' : ''}" style="left:${x1 * 100}%;top:${y1 * 100}%;width:${(x2 - x1) * 100}%;height:${(y2 - y1) * 100}%"><span>${escapeHtml(label)}</span></div>`;
  }).join('');
  if (!model.dragging) dragRect.hidden = true;
}

const autoCropStatusLabels = {
  uploaded: '已接收照片',
  page_matched: '页面已匹配',
  alignment_passed: '配准通过',
  cropped: '已生成 crop',
  alignment_failed: '页面配准失败',
  page_match_failed: '页面匹配失败',
  page_match_ambiguous: '页面匹配不明确',
  layout_profile_missing: '缺少有效答题区模板',
  layout_profile_stale: '答题区模板与当前 PDF 不一致',
  roi_missing: '该页没有答题区',
};

function formatAutoCropMetric(value, asPercent = false) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return asPercent ? `${(Number(value) * 100).toFixed(2)}%` : String(value);
}

function renderStudentAutoCropResult(result) {
  const container = $('#student-auto-crop-result');
  if (!result) { container.innerHTML = ''; return; }
  const runId = String(result.alignment_run_id || '');
  const artifactUrls = result.artifact_urls || {};
  const getImg = (file) => artifactUrls[file] ? `<figure><figcaption>${escapeHtml(file)}</figcaption><img loading="lazy" src="${escapeHtml(artifactUrls[file])}" alt="${escapeHtml(file)}"></figure>` : '';
  const formatCropBox = (box) => box ? Object.entries(box).map(([key, value]) => `${key}=${Number(value).toFixed(4)}`).join(', ') : '—';
  const candidateCards = (result.page_candidates || []).map(candidate => {
    const matchRows = (candidate.matches || []).map(match => {
      const m = match.metrics || {};
      return `<tr><td>PDF 第 ${Number(match.page_number)} 页</td><td>${formatAutoCropMetric(m.good_matches)}</td><td>${formatAutoCropMetric(m.inliers)}</td><td>${formatAutoCropMetric(m.inlier_ratio, true)}</td><td>${formatAutoCropMetric(m.reference_coverage, true)}</td><td>${m.quality_gate === 'passed' ? '通过' : '未通过'}</td><td>${escapeHtml(m.failure_reason || '—')}</td></tr>`;
    }).join('');
    return `<article class="student-auto-crop-result-card"><strong>${escapeHtml(candidate.candidate_id)}候选页</strong> · ${escapeHtml(autoCropStatusLabels[candidate.status] || candidate.status)}${candidate.matched_page_number ? ` · 匹配 PDF 第 ${Number(candidate.matched_page_number)} 页` : ''}<div class="student-auto-crop-candidate"><table><thead><tr><th>Reference</th><th>Good matches</th><th>Inliers</th><th>Inlier ratio</th><th>Coverage</th><th>质量门</th><th>结果</th></tr></thead><tbody>${matchRows}</tbody></table></div><div class="student-auto-crop-image-grid">${getImg(`candidate_${candidate.candidate_id}.png`)}</div></article>`;
  }).join('');
  const acceptedCards = (result.accepted_pages || []).map(page => `<article class="student-auto-crop-result-card"><strong>${escapeHtml(page.candidate_id)} → PDF 第 ${Number(page.page_number)} 页</strong><div class="student-auto-crop-image-grid">${getImg(`aligned_page_${Number(page.page_number)}.png`)}${getImg(`roi_overlay_page_${Number(page.page_number)}.png`)}</div></article>`).join('');
  const crops = (result.crops || []).map(crop => `<div>${getImg(crop.file_name)}<small>Configured ROI: ${escapeHtml(formatCropBox(crop.configured_bbox))}<br>Effective crop: ${escapeHtml(formatCropBox(crop.effective_crop_bbox))}</small></div>`).join('');
  const paddingImages = Object.keys(artifactUrls).filter(file => file.startsWith('padding_validation/') && file.endsWith('.png')).map(getImg).join('');
  const paddingReportLink = artifactUrls['padding_validation/PADDING_VALIDATION.md'] ? `<a href="${escapeHtml(artifactUrls['padding_validation/PADDING_VALIDATION.md'])}" target="_blank" rel="noopener">查看 padding 验收记录</a>` : '';
  const warnings = (result.warnings || []).map(item => `<li>${escapeHtml(item)}</li>`).join('');
  const reportLink = artifactUrls['AUTO_CROP_REPORT.md'] ? `<a href="${escapeHtml(artifactUrls['AUTO_CROP_REPORT.md'])}" target="_blank" rel="noopener">查看诊断报告</a>` : '';
  const manifestLink = artifactUrls['run.json'] ? `<a href="${escapeHtml(artifactUrls['run.json'])}" target="_blank" rel="noopener">查看 run.json</a>` : '';
  const review = runId && (result.crops || []).length
    ? `<div class="student-auto-crop-review"><span>裁切人工验收：${result.human_review === 'passed' ? '通过' : result.human_review === 'failed' ? '失败' : '待查看'}</span><button class="button secondary student-auto-crop-review-action" type="button" data-run-id="${escapeHtml(runId)}" data-verdict="passed">通过</button><button class="button secondary student-auto-crop-review-action" type="button" data-run-id="${escapeHtml(runId)}" data-verdict="failed">失败</button></div>`
    : '';
  container.innerHTML = `<div class="student-auto-crop-result-card"><div class="student-auto-crop-status">结果：${escapeHtml(autoCropStatusLabels[result.status] || result.status || '未知')}</div><p>exam_id：${escapeHtml(result.exam_id || '')}${result.layout_profile_id ? ` · layout_profile_id：${escapeHtml(result.layout_profile_id)}` : ''}${result.input_mode ? ` · ${result.input_mode === 'double_spread' ? '双页展开' : '单页'}` : ''}</p>${result.partial_processing ? '<p>部分页面未通过质量门或没有答题区；仅展示有效页面的 crop。</p>' : ''}${warnings ? `<ul>${warnings}</ul>` : ''}${reportLink} ${manifestLink} ${paddingReportLink}${review}</div>${candidateCards}${acceptedCards}${crops ? `<article class="student-auto-crop-result-card"><strong>自动生成的小题 crop</strong><div class="student-auto-crop-image-grid">${crops}</div></article>` : ''}${paddingImages ? `<article class="student-auto-crop-result-card"><strong>Safety padding 诊断图（待人工验收）</strong><div class="student-auto-crop-image-grid">${paddingImages}</div></article>` : ''}`;
}

function layoutPointerPoint(event) {
  const image = $('#exam-layout-reference-image');
  const rect = image.getBoundingClientRect();
  if (!rect.width || !rect.height || !image.complete || !image.naturalWidth) return null;
  return {
    x: normalizedCoordinate((event.clientX - rect.left) / rect.width),
    y: normalizedCoordinate((event.clientY - rect.top) / rect.height),
  };
}

function updateLayoutDragRect() {
  const model = state.examLayoutProfile;
  const node = $('#exam-layout-drag-rect');
  if (!model?.dragging || !node) return;
  const { start, end } = model.dragging;
  node.hidden = false;
  node.style.left = `${Math.min(start.x, end.x) * 100}%`;
  node.style.top = `${Math.min(start.y, end.y) * 100}%`;
  node.style.width = `${Math.abs(end.x - start.x) * 100}%`;
  node.style.height = `${Math.abs(end.y - start.y) * 100}%`;
}

$('#exam-layout-profile-question-list').addEventListener('click', event => {
  const button = event.target.closest('.exam-layout-question');
  const model = state.examLayoutProfile;
  if (!button || !model) return;
  model.objectiveActive = false;
  model.selectedObjectiveRegionId = '';
  model.selectedQuestionId = button.dataset.questionId;
  const existing = model.regions.find(region => String(region.question_id) === model.selectedQuestionId);
  if (existing) {
    model.pageNumber = Number(existing.page_number);
    $('#exam-layout-profile-page').value = String(model.pageNumber);
  }
  renderExamLayoutProfile();
});

$('#exam-layout-profile-page').addEventListener('change', event => {
  const model = state.examLayoutProfile;
  if (!model) return;
  model.pageNumber = Number(event.target.value);
  renderExamLayoutProfile();
});

$('#exam-layout-objective-start').addEventListener('change', () => {
  renderExamLayoutProfile();
});
$('#exam-layout-objective-end').addEventListener('change', () => {
  renderExamLayoutProfile();
});
$('#exam-layout-objective-draw').addEventListener('click', () => {
  const model = state.examLayoutProfile;
  if (!model) return;
  model.objectiveActive = !model.objectiveActive;
  if (model.objectiveActive) {
    model.selectedQuestionId = '';
    model.selectedObjectiveRegionId = '';
  }
  renderExamLayoutProfile();
  if (model.objectiveActive) {
    $('#exam-layout-profile-note').textContent = '客观题模式：选择起止题号后，在答题卡上拖出这一行答案区域。';
  }
});
$('#exam-layout-objective-list').addEventListener('click', event => {
  const button = event.target.closest('.exam-layout-objective-delete');
  const model = state.examLayoutProfile;
  if (!button || !model) return;
  model.regions = model.regions.filter(region => region.region_id !== button.dataset.regionId);
  model.selectedObjectiveRegionId = '';
  model.dirty = true;
  renderExamLayoutProfile();
});

$('#exam-layout-reference-frame').addEventListener('pointerdown', event => {
  const model = state.examLayoutProfile;
  if (!model || event.button !== 0 || (!model.selectedQuestionId && !model.objectiveActive)) return;
  const point = layoutPointerPoint(event);
  if (!point) return;
  model.dragging = { pointerId: event.pointerId, start: point, end: point };
  event.currentTarget.setPointerCapture(event.pointerId);
  updateLayoutDragRect();
  event.preventDefault();
});
$('#exam-layout-reference-frame').addEventListener('pointermove', event => {
  const model = state.examLayoutProfile;
  if (!model?.dragging || model.dragging.pointerId !== event.pointerId) return;
  const point = layoutPointerPoint(event);
  if (!point) return;
  model.dragging.end = point;
  updateLayoutDragRect();
});
$('#exam-layout-reference-frame').addEventListener('pointerup', event => {
  const model = state.examLayoutProfile;
  if (!model?.dragging || model.dragging.pointerId !== event.pointerId) return;
  const { start, end } = model.dragging;
  const x1 = Math.min(start.x, end.x); const y1 = Math.min(start.y, end.y);
  const x2 = Math.max(start.x, end.x); const y2 = Math.max(start.y, end.y);
  model.dragging = null;
  $('#exam-layout-drag-rect').hidden = true;
  if (x2 - x1 < 0.004 || y2 - y1 < 0.004) return;
  if (model.objectiveActive) {
    const ids = (model.data.objective_question_ids || []).map(String);
    const startId = String($('#exam-layout-objective-start').value || '');
    const endId = String($('#exam-layout-objective-end').value || '');
    const startIndex = ids.indexOf(startId);
    const endIndex = ids.indexOf(endId);
    if (startIndex < 0 || endIndex < startIndex) {
      $('#exam-layout-profile-note').textContent = '请选择有效的客观题起止题号。';
      return;
    }
    const mapping = ids.slice(startIndex, endIndex + 1);
    const mappingSet = new Set(mapping);
    const region = {
      region_id: `objective-${Number(model.pageNumber)}-${mapping[0]}-${mapping[mapping.length - 1]}`,
      region_type: 'handwritten_abcd',
      page_number: Number(model.pageNumber),
      bbox: { x1, y1, x2, y2 },
      rows: 1,
      cols: mapping.length,
      question_mapping: mapping,
    };
    model.regions = [
      ...model.regions.filter(item => {
        if (!item.region_type || item.region_type === 'subjective_answer') return true;
        return !(item.question_mapping || []).some(value => mappingSet.has(objectiveQuestionNumber(value)));
      }),
      region,
    ];
    model.dirty = true;
    model.objectiveActive = false;
    renderExamLayoutProfile();
    $('#exam-layout-profile-note').textContent = `已添加客观题第 ${mapping[0]}–${mapping[mapping.length - 1]} 题答案行区域（尚未保存）。`;
    return;
  }
  const region = {
    region_id: `subjective-${model.selectedQuestionId}`,
    region_type: 'subjective_answer',
    question_id: model.selectedQuestionId,
    page_number: Number(model.pageNumber),
    bbox: { x1, y1, x2, y2 },
  };
  model.regions = [
    ...model.regions.filter(item => item.question_id !== model.selectedQuestionId),
    region,
  ];
  model.dirty = true;
  renderExamLayoutProfile();
});

$('#delete-exam-layout-region').addEventListener('click', () => {
  const model = state.examLayoutProfile;
  if (!model?.selectedQuestionId) return;
  model.regions = model.regions.filter(item => item.question_id !== model.selectedQuestionId);
  model.dirty = true;
  renderExamLayoutProfile();
});

$('#save-exam-layout-profile').addEventListener('click', async event => {
  const model = state.examLayoutProfile;
  if (!model) return;
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const saved = await api('/api/exam-layout-profile', {
      method: 'POST',
      body: JSON.stringify({ exam_id: model.examId, regions: model.regions }),
    });
    model.data = saved;
    model.regions = (saved.layout_profile?.regions || []).map(region => structuredClone(region));
    model.objectiveActive = false;
    model.selectedObjectiveRegionId = '';
    model.dirty = false;
    renderExamLayoutProfile();
    $('#exam-layout-profile-note').textContent = '答题区设置已保存。离开后重新打开，可以继续查看已保存的页码和框选。';
    await loadAssignments();
  } catch (error) {
    $('#exam-layout-profile-note').textContent = error.message;
    $('#exam-layout-profile-note').classList.add('error');
  } finally {
    button.disabled = false;
  }
});

$('#student-auto-crop-run').addEventListener('click', async event => {
  const model = state.examLayoutProfile;
  const button = event.currentTarget;
  const note = $('#student-auto-crop-note');
  const file = $('#student-auto-crop-file').files?.[0];
  if (!model || model.dirty) {
    note.textContent = '请先保存答题区设置，再运行自动裁切。';
    return;
  }
  if (!file) {
    note.textContent = '请先选择一张学生答卷照片。';
    return;
  }
  const inputMode = document.querySelector('input[name="student-auto-crop-mode"]:checked')?.value || 'single_page';
  const form = new FormData();
  form.append('metadata', JSON.stringify({ exam_id: model.examId, input_mode: inputMode }));
  form.append('student_image', file, file.name);
  button.disabled = true;
  note.textContent = `正在${isCloudRuntime() ? '服务器内' : '本机'}匹配 PDF 页面并检查图像质量…`;
  $('#student-auto-crop-result').innerHTML = '';
  try {
    const result = await api('/api/student-auto-crop', { method: 'POST', body: form });
    state.studentAutoCropResult = result;
    renderStudentAutoCropResult(result);
    note.textContent = result.status === 'cropped'
      ? '自动处理完成。请检查页面匹配、ROI overlay 和每个小题 crop。'
      : '未生成可用 crop。请查看下方失败状态和配准指标。';
  } catch (error) {
    note.textContent = error.message;
    note.classList.add('error');
  } finally {
    button.disabled = Boolean(state.examLayoutProfile?.dirty);
  }
});

$('#student-auto-crop-result').addEventListener('click', async event => {
  const button = event.target.closest('.student-auto-crop-review-action');
  if (!button || !state.studentAutoCropResult) return;
  button.disabled = true;
  try {
    const result = await api('/api/student-auto-crop-review', {
      method: 'POST',
      body: JSON.stringify({ alignment_run_id: button.dataset.runId, verdict: button.dataset.verdict }),
    });
    state.studentAutoCropResult.human_review = result.human_review;
    renderStudentAutoCropResult(state.studentAutoCropResult);
  } catch (error) {
    $('#student-auto-crop-note').textContent = error.message;
    $('#student-auto-crop-note').classList.add('error');
  }
});

$('#close-exam-layout-profile').addEventListener('click', () => {
  const model = state.examLayoutProfile;
  if (model?.dirty && !window.confirm('当前有未保存的答题区修改，仍要返回吗？')) return;
  $('#exam-layout-profile-panel').hidden = true;
  state.examLayoutProfile = null;
});

function formatSubmissionHistoryState(submission) {
  if (submission.teacher_reconfirmation_required) {
    return '评分范围已修正，待教师重新确认最终分';
  }
  if (submission.state === 'completed') {
    return submission.final_total === null || submission.final_total === undefined
      ? '已完成，尚未保存最终分数'
      : `已确认 ${submission.final_total} 分`;
  }
  if (submission.state === 'failed') return '处理失败，可查看待恢复任务';
  if (submission.state === 'recovery_pending') return '服务中断，待人工处理';
  if (submission.state === 'grading') return '正在批改';
  return '排队中';
}

function renderAssignmentSubmissions(container, submissions) {
  if (!submissions.length) {
    container.innerHTML = '<p class="muted">这道题还没有学生批改记录。</p>';
    return;
  }
  container.innerHTML = submissions.map(submission => {
    const student = submission.student_name
      ? `${submission.student_name}${submission.student_no ? `（${submission.student_no}）` : ''}`
      : (submission.student_label || '未命名学生');
    const previousFinal = submission.previous_teacher_final || {};
    const score = submission.teacher_reconfirmation_required
      ? `历史终分 ${previousFinal.final_total ?? submission.final_total ?? '—'} / ${previousFinal.max_total ?? '—'}（待重新确认）`
      : submission.final_total !== null && submission.final_total !== undefined
      ? `最终 ${submission.final_total} / ${submission.max_total ?? '—'}`
      : `AI建议 ${submission.suggested_total ?? '—'} / ${submission.max_total ?? '—'}`;
    const photoAction = submission.photos?.length
      ? `<button class="button danger history-delete-photos" type="button" data-submission-id="${escapeHtml(submission.id)}">删除照片缓存</button>`
      : `<span class="muted">${submission.photos_deleted ? '照片缓存已清理' : '无照片缓存'}</span>`;
    const reviewAction = submission.has_dual_review && submission.state === 'completed'
      && (submission.teacher_reconfirmation_required || submission.final_total === null || submission.final_total === undefined)
      ? `<button class="button secondary history-open-review" type="button" data-submission-id="${escapeHtml(submission.id)}">${submission.teacher_reconfirmation_required ? '重新确认最终分' : '打开逐点评审'}</button>`
      : submission.state === 'completed' && submission.final_total != null
        ? `<button class="button secondary history-open-review" type="button" data-read-only="true" data-submission-id="${escapeHtml(submission.id)}">查看评审详情</button>` : '';
    return `<div class="history-submission-item" data-submission-id="${escapeHtml(submission.id)}">
      <div><strong>${escapeHtml(student)}</strong><small>${escapeHtml(formatSubmissionHistoryState(submission))} · ${escapeHtml(score)}</small></div>
      <div class="history-submission-actions">${reviewAction}${photoAction}</div>
    </div>`;
  }).join('');
  container.querySelectorAll('.history-delete-photos').forEach(button => {
    button.addEventListener('click', () => deleteHistorySubmissionPhotos(button));
  });
  container.querySelectorAll('.history-open-review').forEach(button => {
    button.addEventListener('click', () => openHistorySubmissionForReview(button));
  });
}

async function openHistorySubmissionForReview(button) {
  const submissionId = button?.dataset.submissionId;
  if (!submissionId) return;
  button.disabled = true;
  try {
    const data = await api(`/api/submission?submission_id=${encodeURIComponent(submissionId)}`);
    document.querySelector('#main-tab-grading')?.click();
    if (!selectGradingMode('single')) return;
    renderResult(data.submission, null, { readOnly: button.dataset.readOnly === 'true' });
    document.querySelector('#submission-result')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    alert(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function toggleAssignmentSubmissions(button) {
  const container = button.closest('.history-item')?.querySelector('.history-submissions');
  if (!container) return;
  if (!container.hidden) {
    container.hidden = true;
    button.textContent = '查看学生记录';
    return;
  }
  button.disabled = true;
  try {
    const data = await api(`/api/assignment-submissions?assignment_id=${encodeURIComponent(button.dataset.id)}`);
    renderAssignmentSubmissions(container, data.submissions || []);
    container.hidden = false;
    button.textContent = '收起学生记录';
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
}

function recoveryStudentLabel(submission) {
  if (submission.identity_status === 'matched' && submission.student_name) {
    return `${submission.student_name}${submission.student_no ? `（${submission.student_no}）` : ''}`;
  }
  return submission.student_label || '未填写学生标识';
}

function recoveryStateLabel(submission) {
  if (submission.recovery_action === 'skip_unmatched_batch_sequence') return '批次暂停，题号未匹配';
  if (submission.recovery_action === 'skip_failed_upload') return '批次暂停，上传失败';
  if (submission.state === 'recovery_pending') return '服务中断，结果未确定';
  if (submission.result?.mode === 'vision-model-error') return '模型调用失败';
  return '任务处理失败';
}

function renderRecoverySubmissions(submissions, batches = []) {
  const container = $('#recovery-list');
  if (!container) return;
  const activeBatches = batches.filter((batch) => batch.status === 'active');
  const pausedMissingUploads = batches.flatMap((batch) => {
    if (batch.status !== 'paused') return [];
    return (batch.items || [])
      .filter((item) => Number(item.sequence) === Number(batch.pause_sequence) && !item.submission_state)
      .map((item) => ({ ...item, batch_id: batch.batch_id, pause_reason: batch.pause_reason }));
  });
  if (!submissions.length && !pausedMissingUploads.length && !activeBatches.length) {
    container.innerHTML = '<p class="muted">暂无待恢复任务。</p>';
    return;
  }
  container.innerHTML = [...submissions.map((submission) => {
    const waitSeconds = Number(submission.retry_wait_seconds || 0);
    const unmatchedPaused = submission.recovery_action === 'skip_unmatched_batch_sequence';
    const retryAction = unmatchedPaused
      ? '<span class="recovery-no-retry">本份题号无法可靠对应，不计入成绩；跳过后只继续后续答卷，原批改记录会保留。</span>'
      : submission.retryable && isLocalPage()
      ? `<button class="button secondary recovery-retry-button" type="button" data-submission-id="${escapeHtml(submission.id)}"${waitSeconds > 0 ? ' disabled' : ''}>${waitSeconds > 0 ? `请等待约 ${waitSeconds} 秒` : '人工确认后重试'}</button>`
      : submission.retryable && storedMobileToken()
        ? '<span class="recovery-no-retry">手机端不重试模型调用，避免重复计费；可确认跳过当前项。</span>'
      : `<span class="recovery-no-retry">暂不建议重试：${escapeHtml(submission.retry_reason || '请先修正配置或重新提交照片')}</span>`;
    const skipAction = submission.batch_id
      ? `<button class="button danger recovery-sequence-skip" type="button" data-batch-id="${escapeHtml(submission.batch_id)}" data-submission-id="${escapeHtml(submission.id)}" data-sequence="${escapeHtml(submission.sequence)}">教师确认跳过第${escapeHtml(submission.sequence)}份并继续</button>`
      : '';
    return `
    <article class="recovery-item">
      <div class="recovery-item-main">
        <strong>${escapeHtml(recoveryStudentLabel(submission))}</strong>
        <small>${escapeHtml(submission.assignment_name || '未命名作业')} · ${escapeHtml(recoveryStateLabel(submission))}</small>
        <p>${escapeHtml(submission.result?.summary || '该任务需要人工确认后重试。')}</p>
      </div>
      <div class="recovery-item-actions">
        ${originalPhotoLink(submission, '打开原图')}
        ${unmatchedPaused ? '<small>跳过不会删除本份模型建议；必须明确确认才会开始处理下一份。</small>' : ''}
        ${retryAction}
        ${skipAction}
      </div>
    </article>`;
  }), ...pausedMissingUploads.map((item) => `
    <article class="recovery-item">
      <div class="recovery-item-main"><strong>第 ${escapeHtml(item.sequence)} 份 · ${escapeHtml(item.student_label)}</strong>
        <small>批次 ${escapeHtml(item.batch_id)} · 上传中断，照片未完整保存</small>
        <p>${escapeHtml(item.pause_reason || '请原位重传照片；若无法恢复，可确认跳过。')}</p></div>
      <div class="recovery-item-actions"><button class="button danger recovery-batch-skip" type="button" data-batch-id="${escapeHtml(item.batch_id)}" data-submission-id="${escapeHtml(item.submission_id)}" data-sequence="${escapeHtml(item.sequence)}">教师确认跳过</button></div>
    </article>`), ...activeBatches.map((batch) => {
      const sequence = Number(batch.next_sequence || 1);
      const total = Number(batch.total_sequences || 0);
      const label = (batch.items || []).find((item) => Number(item.sequence) === sequence)?.student_label || '';
      return `<article class="recovery-item active-batch-recovery">
        <div class="recovery-item-main"><strong>批次已恢复 · ${Math.max(0, sequence - 1)} / ${escapeHtml(total)} 份已越过</strong>
          <small>当前第 ${escapeHtml(sequence)} / ${escapeHtml(total)} 份${label ? ` · ${escapeHtml(label)}` : ''}</small>
          <p>服务端正按原顺序继续处理。后续模型调用可能产生费用；可点“刷新”查看状态。</p></div>
      </article>`;
    })].join('');
  container.querySelectorAll('.recovery-retry-button').forEach((button) => {
    button.addEventListener('click', () => retryRecoverySubmission(button.dataset.submissionId, button));
  });
  container.querySelectorAll('.recovery-batch-skip').forEach((button) => {
    button.addEventListener('click', () => skipBatchSequence({
      batchId: button.dataset.batchId,
      submissionId: button.dataset.submissionId,
      sequence: Number(button.dataset.sequence),
      row: button.closest('.recovery-item'),
    }));
  });
  container.querySelectorAll('.recovery-sequence-skip').forEach((button) => {
    button.addEventListener('click', () => skipBatchSequence({
      batchId: button.dataset.batchId,
      submissionId: button.dataset.submissionId,
      sequence: Number(button.dataset.sequence),
      row: button.closest('.recovery-item'),
    }));
  });
}

async function loadRecoverySubmissions() {
  const data = await api('/api/recovery-submissions');
  state.recoverySubmissions = Array.isArray(data.submissions) ? data.submissions : [];
  const batchData = isLocalPage()
    ? await api('/api/batches')
    : { batches: Array.isArray(data.batches) ? data.batches : [] };
  renderRecoverySubmissions(state.recoverySubmissions, Array.isArray(batchData.batches) ? batchData.batches : []);
}

function evaluationPercent(value) {
  return value === null || value === undefined ? '—' : `${(Number(value) * 100).toFixed(1)}%`;
}

function evaluationNumber(value) {
  return value === null || value === undefined ? '—' : Number(value).toFixed(2);
}

function renderEvaluationSummary(data) {
  const container = $('#evaluation-summary');
  if (!container) return;
  state.evaluationSummary = data;
  const manifest = data.manifest || {};
  const metrics = data.metrics;
  if (!manifest.configured) {
    container.innerHTML = `
      <div class="evaluation-empty">
        <strong>尚未配置评测集</strong>
        <p>请复制程序目录下的 <code>evaluation/manifest.example.json</code> 为 <code>evaluation/manifest.json</code>，再填入匿名 case 和教师金标准分。</p>
        <small>评测清单只允许保存匿名编号和分数，不要放入学生姓名、答案、照片路径或 API Key。</small>
      </div>`;
    return;
  }
  if (!metrics) {
    container.innerHTML = '<p class="muted">评测清单为空，暂时没有可统计的样本。</p>';
    return;
  }
  const contexts = Array.isArray(data.version_contexts) ? data.version_contexts : [];
  const contextText = contexts.length
    ? contexts.map((item) => `${escapeHtml(item.app_version || '旧记录')} · ${escapeHtml(item.grading_rules_version || '旧规则')} · ${escapeHtml(item.prompt_version || '无提示词')}`).join('<br>')
    : '尚无带版本信息的已完成样本';
  container.innerHTML = `
    <div class="evaluation-heading"><div><strong>${escapeHtml(manifest.name || '匿名评测集')}</strong><span>清单 ${escapeHtml(manifest.relative_path || 'evaluation/manifest.json')} · 共 ${escapeHtml(manifest.case_count ?? 0)} 个样本</span></div><span class="evaluation-threshold">严重误差阈值：${escapeHtml(manifest.severe_error_threshold ?? '—')} 分</span></div>
    <div class="evaluation-metrics">
      <div class="evaluation-metric"><span>±1 分一致率</span><strong>${evaluationPercent(metrics.within_one_rate)}</strong><small>${metrics.within_one_count ?? 0} / ${metrics.scored_cases ?? 0} 个有效样本</small></div>
      <div class="evaluation-metric"><span>严重误差率</span><strong>${evaluationPercent(metrics.severe_error_rate)}</strong><small>${metrics.severe_error_count ?? 0} / ${metrics.scored_cases ?? 0} 个有效样本</small></div>
      <div class="evaluation-metric"><span>平均绝对误差</span><strong>${evaluationNumber(metrics.average_absolute_error)}</strong><small>分</small></div>
      <div class="evaluation-metric"><span>失败率</span><strong>${evaluationPercent(metrics.failure_rate)}</strong><small>${metrics.failed_cases ?? 0} / ${metrics.matched_cases ?? 0} 个已找到样本</small></div>
    </div>
    <div class="evaluation-breakdown"><span>已匹配 ${metrics.matched_cases ?? 0}</span><span>已评分 ${metrics.scored_cases ?? 0}</span><span>缺失 ${metrics.missing_cases ?? 0}</span><span>未纳入统计 ${metrics.excluded_cases ?? 0}</span></div>
    <details class="evaluation-versions"><summary>查看本批样本的版本信息</summary><p>${contextText}</p></details>`;
}

function renderAuditSummary(data) {
  const container = $('#audit-summary');
  if (!container) return;
  const events = Array.isArray(data.events) ? data.events : [];
  const chainValid = data.chain_valid === true;
  const statusClass = chainValid ? 'audit-valid' : 'audit-invalid';
  const statusText = chainValid ? '完整性校验通过' : '完整性校验失败，请停止使用并保留数据副本';
  const eventRows = events.length
    ? events.slice(0, 8).map((event) => `<li><span>${escapeHtml(event.event_type || 'unknown')}</span><small>${escapeHtml(event.created_at || '')}</small></li>`).join('')
    : '<li><span>暂无审计事件</span></li>';
  container.innerHTML = `
    <div class="audit-heading"><div><strong>${isCloudRuntime() ? '服务器审计记录' : '本地版本化审计链'}</strong><span>记录评分、复核、确认和配置变更的脱敏事件，不包含 API Key、答案或照片内容。</span></div><span class="audit-status ${statusClass}">${statusText}</span></div>
    <div class="audit-meta"><span>记录格式：${escapeHtml(data.audit_record_version || 'legacy')}</span><span>事件数：${escapeHtml(data.event_count ?? 0)}</span></div>
    <details class="audit-events"><summary>查看最近事件（最多8条）</summary><ul>${eventRows}</ul></details>`;
}

async function loadEvaluationSummary() {
  if (!isLocalPage()) return;
  try {
    const data = await api('/api/evaluation/summary');
    renderEvaluationSummary(data);
    renderAuditSummary(data.audit || {});
  } catch (error) {
    const evaluation = $('#evaluation-summary');
    const audit = $('#audit-summary');
    if (evaluation) evaluation.innerHTML = `<p class="error">评测清单读取失败：${escapeHtml(error.message)}</p>`;
    if (audit) audit.innerHTML = '<p class="muted">评测清单无效，本次未显示审计摘要。</p>';
  }
}

async function retryRecoverySubmission(submissionId, button) {
  if (!submissionId || !button) return;
  button.disabled = true;
  button.textContent = '正在重试…';
  try {
    await api('/api/retry', { method: 'POST', body: JSON.stringify({ submission_id: submissionId }) });
    const completed = await waitForSubmission(submissionId);
    renderResult(completed);
    await Promise.all([loadRecoverySubmissions(), loadAssignments()]);
  } catch (error) {
    alert(error.message);
    button.disabled = false;
    button.textContent = '人工确认后重试';
  }
}

async function deleteAssignment(assignmentId, assignmentName) {
  const confirmed = window.confirm(`确定删除“${assignmentName}”吗？\n\n这会同时删除该题的所有学生批改记录和照片，且无法恢复。`);
  if (!confirmed) return;
  try {
    const data = await api('/api/delete-assignment', {
      method: 'POST',
      body: JSON.stringify({ assignment_id: assignmentId }),
    });
    await loadAssignments();
    alert(data.message);
  } catch (error) { alert(error.message); }
}

function deleteSubmissionPhotosMessage() {
  return '确定删除本份学生的照片缓存吗？\n\n将删除原图、处理图和证据裁切图，但保留题目、成绩、评语、花名册关联和数据库记录。删除后无法打开原图；如果本份任务失败，也将无法再用这些照片重试。\n\n已生成的备份文件不会被删除。';
}

async function deleteSubmissionPhotosRequest(submissionId) {
  if (!window.confirm(deleteSubmissionPhotosMessage())) return null;
  return api('/api/delete-submission-photos', {
    method: 'POST',
    body: JSON.stringify({ submission_id: submissionId }),
  });
}

async function deleteHistorySubmissionPhotos(button) {
  if (!button?.dataset.submissionId) return;
  button.disabled = true;
  try {
    const data = await deleteSubmissionPhotosRequest(button.dataset.submissionId);
    if (!data) return;
    const row = button.closest('.history-submission-item');
    button.replaceWith(document.createTextNode('照片缓存已清理'));
    if (row) row.classList.add('photos-cleaned');
    alert(data.message);
  } catch (error) {
    alert(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function createAssignment(event) {
  event.preventDefault();
  const button = event.submitter;
  const allEntries = collectQuestionEntries();
  const entries = allEntries.filter(({ card }) => hasSmallQuestionInput(card));
  const drafts = entries.map(({ card }) => buildSubQuestionDraft(
    card,
    allEntries.findIndex(item => item.card === card),
  ));
  if (!drafts.length) return alert('请至少填写一道小题、参考答案和一个有效评分点。');
  if (entries.some(({ group, card }) => !normalizeMajorNumber(group.querySelector('.major-label').value)
    || !normalizeMinorNumber(card.querySelector('.subq-label').value, ''))) {
    return alert('请为已填写的小题补全大题编号和小题序号，例如 17 和 1。');
  }
  if (drafts.some((item, index) => !entries[index].card.querySelector('.subq-question').value.trim()
    || !item.standard_answer)) {
    return alert('每道已开始填写的小题都需要填写题目和参考答案。');
  }
  const labels = drafts.map(item => item.label);
  if (new Set(labels).size !== labels.length) return alert('小题编号重复，请检查大题编号和小题序号。');
  if (drafts.some((item, index) => entries[index].card.querySelector('.subq-question').value.trim()
    && item.scoring_mode === 'point_bank' && !item.max_score)) {
    return alert('按得分点封顶的小题必须填写满分；也可以把“（6分）”等标注录入在小题题目中。');
  }
  if (drafts.some(item => !item.criteria.length)) {
    return alert('每道已填写题目和参考答案的小题都要录入或生成至少一个评分点。');
  }
  const insufficient = drafts.filter(item => item.scoring_mode === 'point_bank'
    && item.criteria.length < Math.ceil(item.max_score / item.point_value));
  if (insufficient.length && !window.confirm(`以下小题的候选点数量不足以达到满分：\n${insufficient.map(item => `${item.label}：${item.criteria.length} 个候选点，至少需要 ${Math.ceil(item.max_score / item.point_value)} 个`).join('\n')}\n\n仍要保存题目吗？保存后请补充评分点或调整规则。`)) return;
  button.disabled = true;
  try {
    const data = await api('/api/assignments', { method: 'POST', body: JSON.stringify({
      name: $('#assignment-name').value,
      question: buildAssignmentQuestion(),
      sub_questions: drafts,
    }) });
    await loadAssignments();
    $('#assignment-select').value = data.assignment.id;
    $('#batch-assignment-select').value = data.assignment.id;
    const warningText = data.assignment.scoring_warnings?.length
      ? `\n\n评分规则提醒：\n${data.assignment.scoring_warnings.join('\n')}`
      : '';
    alert(`已保存：${data.assignment.name}，满分 ${data.assignment.max_score}${warningText}`);
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
}

function readFileDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function compressImage(file, sourceDataUrl) {
  return new Promise((resolve, reject) => {
    if (file.size > 25 * 1024 * 1024) {
      reject(new Error(`图片“${file.name}”超过25MiB，请先压缩后再上传`));
      return;
    }
    resolve(sourceDataUrl);
  }).then((dataUrl) => {
    if (!String(file.type).startsWith('image/')) return dataUrl;
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        if (Math.min(image.naturalWidth, image.naturalHeight) < 700) {
          reject(new Error(`图片“${file.name}”短边低于700px，请使用手机原图而不是聊天软件缩略图`));
          return;
        }
        if (image.naturalWidth * image.naturalHeight > 40_000_000) {
          reject(new Error(`图片“${file.name}”解码后超过4000万像素，请先缩小`));
          return;
        }
        const longest = Math.max(image.naturalWidth, image.naturalHeight);
        const scale = Math.min(1, 3000 / longest);
        const canvas = document.createElement('canvas');
        canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
        canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
        canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
        resolve(canvas.toDataURL('image/jpeg', 0.86));
      };
      image.onerror = reject;
      image.src = dataUrl;
    });
  });
}

function filesToDataUrls(files) {
  return Promise.all([...files].map(async (file, index) => {
    if (file.size > 25 * 1024 * 1024) {
      throw new Error(`图片“${file.name}”超过25MiB，请先压缩后再上传`);
    }
    const originalDataUrl = await readFileDataUrl(file);
    return {
      name: file.name,
      page_sequence: index + 1,
      data_url: await compressImage(file, originalDataUrl),
      original_data_url: originalDataUrl,
    };
  }));
}

function dataUrlToBlob(dataUrl) {
  const [header, encoded] = String(dataUrl).split(',', 2);
  if (!header || !encoded) throw new Error('图片编码失败，请重试');
  const match = header.match(/^data:([^;]+);base64$/i);
  if (!match) throw new Error('图片格式不受支持，请重试');
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return new Blob([bytes], { type: match[1] });
}

async function buildSubmissionBody(fields, files, privacyApproval = null) {
  const selectedFiles = [...(files || [])];
  if (!selectedFiles.length) return JSON.stringify({ ...fields, photos: [] });
  if (!privacyApproval?.token || !privacyApproval?.redactionsByFile || !Array.isArray(privacyApproval.answerRegions)) {
    throw new Error('缺少服务端签名的裁切图 AI Payload 预览，已阻止上传');
  }
  if (!['pre_anonymized', 'redacted', 'no_identity_visible'].includes(privacyApproval.privacy_status)) {
    throw new Error('尚未确认本份答卷的隐私状态，已阻止上传');
  }
  const privacyFields = {
    ai_privacy_confirmed: true,
    ai_privacy_preview_token: privacyApproval.token,
    ai_privacy_fingerprint: privacyApproval.fingerprint || '',
    privacy_status: privacyApproval.privacy_status,
    answer_regions: privacyApproval.answerRegions,
    ...(Object.prototype.hasOwnProperty.call(privacyApproval, 'gradingInputMode')
      ? { grading_input_mode: privacyApproval.gradingInputMode } : {}),
    ...(Object.prototype.hasOwnProperty.call(privacyApproval, 'examId')
      ? { exam_id: privacyApproval.examId } : {}),
  };
  if (privacyApproval.acceptedPhotos) {
    const photos = privacyApproval.acceptedPhotos;
    if (!privacyApproval.examId || photos.length !== privacyApproval.answerRegions.length
      || photos.some(photo => photo.image_role !== 'answer_region' || !photo.answer_region_attestation)) {
      throw new Error('正式裁切凭据不完整，已阻止上传');
    }
    return JSON.stringify({ ...fields, ...privacyFields, exam_id: privacyApproval.examId, photos });
  }
  if (typeof FormData === 'undefined') {
    const photos = await filesToDataUrls(selectedFiles);
    photos.forEach((photo, index) => {
      const file = selectedFiles[index];
      photo.name = `page_${String(index + 1).padStart(2, '0')}.jpg`;
      const approvedDataUrl = privacyApproval.processedDataUrlsByFile?.get(file);
      if (!approvedDataUrl) throw new Error('缺少教师预览确认过的处理页，已阻止上传');
      photo.data_url = approvedDataUrl;
      photo.ai_redactions = privacyApproval.redactionsByFile.get(file) || [];
      photo.corners = privacyApproval.cornersByFile?.get(file) || null;
    });
    return JSON.stringify({ ...fields, ...privacyFields, photos });
  }
  const parts = await Promise.all(selectedFiles.map(async (file, index) => {
    if (file.size > 25 * 1024 * 1024) {
      throw new Error(`图片“${file.name}”超过25MiB，请先压缩后再上传`);
    }
    const processedDataUrl = privacyApproval.processedDataUrlsByFile?.get(file);
    if (!processedDataUrl) throw new Error('缺少教师预览确认过的处理页，已阻止上传');
    return {
      name: `page_${String(index + 1).padStart(2, '0')}.jpg`,
      pageSequence: index + 1,
      aiRedactions: privacyApproval.redactionsByFile.get(file) || [],
      corners: privacyApproval.cornersByFile?.get(file) || null,
      processedBlob: dataUrlToBlob(processedDataUrl),
      originalFile: file,
    };
  }));
  const form = new FormData();
  form.append('metadata', JSON.stringify({
    ...fields,
    ...privacyFields,
    photos: parts.map((part) => ({ name: part.name, corners: part.corners, ai_redactions: part.aiRedactions, page_sequence: part.pageSequence })),
  }));
  parts.forEach((part, index) => {
    form.append(`processed_${index}`, part.processedBlob, `${part.name}.processed.jpg`);
    form.append(`original_${index}`, part.originalFile, part.name);
  });
  return form;
}

function drawAiRedactions(canvas, image, rectangles, activeRect = null, cropRegions = [], activeMode = 'redact') {
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  context.fillStyle = '#000';
  rectangles.forEach((item) => {
    context.fillRect(
      Math.floor(item.x * canvas.width),
      Math.floor(item.y * canvas.height),
      Math.max(1, Math.ceil(item.width * canvas.width)),
      Math.max(1, Math.ceil(item.height * canvas.height)),
    );
  });
  context.lineWidth = Math.max(2, canvas.width / 700);
  cropRegions.forEach((item, index) => {
    context.strokeStyle = '#16835d';
    context.strokeRect(item.x * canvas.width, item.y * canvas.height, item.width * canvas.width, item.height * canvas.height);
    context.fillStyle = '#16835d';
    context.font = `${Math.max(14, canvas.width / 55)}px sans-serif`;
    context.fillText(`裁切${index + 1}`, item.x * canvas.width + 4, item.y * canvas.height + 20);
  });
  if (activeRect) {
    context.strokeStyle = activeMode === 'redact' ? '#e14545' : '#16835d';
    if (activeMode === 'redact') {
      context.fillStyle = '#000';
      context.fillRect(activeRect.x * canvas.width, activeRect.y * canvas.height, activeRect.width * canvas.width, activeRect.height * canvas.height);
    }
    context.strokeRect(
      activeRect.x * canvas.width, activeRect.y * canvas.height,
      activeRect.width * canvas.width, activeRect.height * canvas.height,
    );
  }
}

function drawPerspectiveCorners(canvas, points) {
  const context = canvas.getContext('2d');
  const colors = ['#ef4444', '#f59e0b', '#22c55e', '#3b82f6'];
  (points || []).forEach((point, index) => {
    const x = point[0] * canvas.width;
    const y = point[1] * canvas.height;
    context.beginPath();
    context.arc(x, y, Math.max(10, canvas.width * 0.008), 0, Math.PI * 2);
    context.fillStyle = colors[index];
    context.fill();
    context.lineWidth = Math.max(2, canvas.width * 0.002);
    context.strokeStyle = '#fff';
    context.stroke();
    context.fillStyle = '#fff';
    context.font = `bold ${Math.max(14, canvas.width * 0.018)}px sans-serif`;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(String(index + 1), x, y);
  });
}

function formatElapsed(milliseconds) {
  if (!Number.isFinite(milliseconds)) return '—';
  return `${(milliseconds / 1000).toFixed(1)} 秒`;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value < 1024) return `${Math.round(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function renderDiagnostics(result) {
  const diagnostic = result?.diagnostics || {};
  const hasDiagnostics = Object.keys(diagnostic).length > 0;
  if (!hasDiagnostics) return '';
  const status = diagnostic.http_status ? `HTTP ${escapeHtml(diagnostic.http_status)}` : '—';
  const category = diagnostic.error_category ? escapeHtml(diagnostic.error_category) : '正常完成';
  const retryable = diagnostic.error_category
    ? (diagnostic.retryable ? '可能适合人工重试' : '不建议直接重试')
    : '—';
  const timing = [
    ['上传与入队', diagnostic.submit_ms],
    ['整条处理链', diagnostic.pipeline_ms],
    ['模型请求', diagnostic.model_call_ms],
    ['照片质量检查', diagnostic.quality_gate_ms],
    ['证据裁切', diagnostic.evidence_ms],
  ].filter(([, value]) => Number.isFinite(Number(value)));
  const size = [
    ['照片张数', diagnostic.photo_count],
    ['上传原图', formatBytes(diagnostic.uploaded_original_bytes)],
    ['模型请求体', formatBytes(diagnostic.request_body_bytes)],
    ['批改图大小', formatBytes(diagnostic.processed_photo_bytes)],
    ['模型图片大小', formatBytes(diagnostic.model_photo_bytes)],
    ['原图大小', formatBytes(diagnostic.original_photo_bytes)],
    ['模型图片长边', diagnostic.model_image_long_edge ? `${diagnostic.model_image_long_edge} px` : undefined],
  ].filter(([, value]) => value !== undefined && value !== null && value !== '—');
  return `<details class="diagnostic-box"><summary>查看性能与失败诊断</summary>
    <div class="diagnostic-grid">
      <span>处理状态</span><strong>${category}</strong>
      <span>服务商状态</span><strong>${status}</strong>
      <span>处理建议</span><strong>${escapeHtml(retryable)}</strong>
      ${diagnostic.message ? `<span>诊断结论</span><strong>${escapeHtml(diagnostic.message)}</strong>` : ''}
      ${diagnostic.exception_type ? `<span>异常类型</span><strong>${escapeHtml(diagnostic.exception_type)}</strong>` : ''}
    </div>
    ${timing.length ? `<div class="diagnostic-subtitle">耗时</div><div class="diagnostic-chips">${timing.map(([label, value]) => `<span>${escapeHtml(label)}：${formatElapsed(Number(value))}</span>`).join('')}</div>` : ''}
    ${size.length ? `<div class="diagnostic-subtitle">请求规模</div><div class="diagnostic-chips">${size.map(([label, value]) => `<span>${escapeHtml(label)}：${escapeHtml(value)}</span>`).join('')}</div>` : ''}
    <small>这里只显示脱敏后的类型、状态、耗时和大小，不包含 API Key、学生答案或照片内容。</small>
  </details>`;
}

function isContinuousMode() {
  return Boolean($('#continuous-mode')?.checked);
}

function selectGradingMode(mode, moveFocus = false) {
  if (state.batchReview && !closeBatchReview()) return false;
  const normalized = ['single', 'continuous', 'batch'].includes(mode) ? mode : 'single';
  const singlePane = $('#single-grading-pane');
  const batchPane = $('#batch-grading-pane');
  const continuous = $('#continuous-mode');
  if (singlePane) singlePane.hidden = normalized === 'batch';
  if (batchPane) batchPane.hidden = normalized !== 'batch';
  if (continuous && normalized !== 'batch') continuous.checked = normalized === 'continuous';
  document.querySelectorAll('[data-grading-mode]').forEach((button) => {
    const active = button.dataset.gradingMode === normalized;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  const title = $('#single-mode-title');
  const description = $('#single-mode-description');
  if (title) title.textContent = normalized === 'continuous' ? '连续批改' : '单份批改';
  if (description) description.textContent = normalized === 'continuous'
    ? '确认一份后保留题目和班级，自动清空本份照片并准备下一名学生。'
    : '上传一名学生的答卷，立即查看建议分和证据，再由教师确认。';
  showGradingAssignmentContext($(normalized === 'batch' ? '#batch-assignment-select' : '#assignment-select')?.value);
  if (normalized !== 'batch') updateContinuousStatus();
  if (moveFocus) (normalized === 'batch' ? batchPane : singlePane)?.focus?.({ preventScroll: true });
  return true;
}

function setupGradingModes() {
  const tabs = [...document.querySelectorAll('[data-grading-mode]')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectGradingMode(tab.dataset.gradingMode));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const offset = event.key === 'ArrowRight' ? 1 : -1;
      const next = tabs[(index + offset + tabs.length) % tabs.length];
      next.focus();
      selectGradingMode(next.dataset.gradingMode);
    });
  });
  selectGradingMode(isContinuousMode() ? 'continuous' : 'single');
}

function updateContinuousStatus(message = '') {
  const status = $('#continuous-status');
  const count = $('#continuous-count');
  if (count) count.textContent = String(state.continuousCompleted);
  if (!status) return;
  if (message) {
    status.textContent = message;
    return;
  }
  status.textContent = isContinuousMode()
    ? `连续模式运行中；保存后自动进入下一份。`
    : (state.continuousCompleted
      ? `本次连续批改已确认 ${state.continuousCompleted} 份；当前改为单份模式。`
      : '单份模式：保存后可手动开始下一份。');
}

function updateSinglePhotoCount() {
  const count = filesFromInputs(SINGLE_PHOTO_INPUTS).length;
  $('#photo-count').textContent = count
    ? `已选择 ${count} 张照片${count > 10 ? '；超过单份最多10张' : ''}`
    : '尚未选择照片；单张原图≤25MiB、短边≥700px，最多10张';
}

function updateBatchPhotoCount() {
  const count = filesFromInputs(BATCH_PHOTO_INPUTS).length;
  $('#batch-photo-count').textContent = count
    ? `已选择 ${count} 张照片；每名学生的每份作业最多10张`
    : '尚未选择照片；单张原图≤25MiB、每份最多10张';
}

function clearSubmissionDraft() {
  $('#student-label').value = '';
  $('#answer-text').value = '';
  clearFileInputs(SINGLE_PHOTO_INPUTS);
  updateSinglePhotoCount();
}

function prepareNextSubmission(message = '') {
  clearSubmissionDraft();
  $('#submission-result').className = 'result-area empty';
  $('#submission-result').innerHTML = `<div class="next-ready-state"><span aria-hidden="true">✓</span><div><strong>${escapeHtml(message || `第 ${state.continuousCompleted} 份已保存`)}</strong><p>题目和班级已保留，请输入下一名学生标识并上传答卷。</p></div></div>`;
  updateContinuousStatus();
  window.requestAnimationFrame(() => $('#student-label')?.focus());
}

function originalPhotoLink(submission, label = '打开原图') {
  if (!submission.photos?.length) return '';
  return `<a data-protected-image href="/api/submission-photo?submission_id=${encodeURIComponent(submission.id)}&photo_index=1" target="_blank" rel="noopener">${escapeHtml(label)}</a>`;
}

function modelProviderDisplayName(meta, side = '') {
  const provider = String(meta?.provider || '').toLowerCase();
  const known = { qwen: 'Qwen', deepseek: 'DeepSeek', mimo: 'MiMo', openai: 'OpenAI', google: 'Gemini', zhipu: '智谱' };
  return known[provider] || meta?.provider_label || meta?.model_id || `AI-${side}`;
}

function dualReviewModelNames(dual) {
  const models = dual?.models || {};
  return { A: modelProviderDisplayName(models.A, 'A'), B: modelProviderDisplayName(models.B, 'B') };
}

function dualPointModelsAgree(item) {
  const decisionA = String(item?.decision_a || '');
  const decisionB = String(item?.decision_b || '');
  const scoreA = item?.score_a;
  const scoreB = item?.score_b;
  return Boolean(decisionA)
    && decisionA === decisionB
    && !['missing', 'conflict'].includes(decisionA)
    && scoreA !== null && scoreA !== undefined && scoreA !== ''
    && scoreB !== null && scoreB !== undefined && scoreB !== ''
    && Number.isFinite(Number(scoreA)) && Number.isFinite(Number(scoreB))
    && Math.abs(Number(scoreA) - Number(scoreB)) <= 1e-9;
}

function dualPointSupportBothLabel(item) {
  const labels = {
    full: '都判给分',
    none: '都判不给分',
    partial: '都判部分给分',
    uncertain: '都判不确定',
  };
  const decision = String(item?.decision_a || '');
  return `支持 AI-A 和 AI-B ${labels[decision] || '的一致判断'}`;
}

function savedTeacherPointChoice(teacherFinal, item) {
  const saved = (teacherFinal?.point_decisions || []).find((point) =>
    point.question_id === item.question_id && point.rubric_point_id === item.rubric_point_id,
  );
  if (!saved) return '';
  if (['support_A', 'support_B', 'support_both'].includes(saved.resolution)) return saved.resolution;
  const byDecision = {
    hit: 'full', not_hit: 'none', uncertain: 'uncertain', partial: 'partial',
  };
  const decision = saved.decision || byDecision[saved.teacher_decision] || 'uncertain';
  if (saved.resolution === 'both_wrong_custom') {
    return ({ full: 'both_wrong_custom_hit', none: 'both_wrong_custom_not_hit', uncertain: 'both_wrong_custom_uncertain' })[decision] || '';
  }
  if (saved.resolution === 'custom') {
    return ({ full: 'teacher_full', none: 'teacher_none', partial: 'teacher_partial', uncertain: 'teacher_uncertain' })[decision] || '';
  }
  return '';
}

function teacherPointTitle(result, item) {
  const sources = [result, result.dual_review?.results?.A, result.dual_review?.results?.B];
  for (const source of sources) {
    const question = source?.sub_questions?.find(q => q.sub_question_id === item.question_id);
    const point = question?.criteria?.find(c => (c.criterion_id || c.id) === item.rubric_point_id);
    if (point) return `<small>${escapeHtml(item.question_id)} · ${escapeHtml(item.rubric_point_id)}</small><strong>${escapeHtml(point.criterion || point.label || '评分点内容未记录')} · ${escapeHtml(point.max_score ?? point.score ?? '—')}分</strong>`;
  }
  return `<small>${escapeHtml(item.question_id)} · ${escapeHtml(item.rubric_point_id)}</small><strong>评分点内容未记录</strong>`;
}

function teacherPointControls(item, savedChoice = '', className = 'dual-point-final-choice', readOnly = false) {
  const options = [['', '未单独记录'], ...(dualPointModelsAgree(item) ? [['support_both', dualPointSupportBothLabel(item)]] : []),
    ['support_A', '支持 AI-A 判断'], ['support_B', '支持 AI-B 判断'],
    ['both_wrong_custom_hit', '两者都不对：给分'], ['both_wrong_custom_not_hit', '两者都不对：不给分'], ['both_wrong_custom_uncertain', '两者都不对：不确定'],
    ['teacher_full', '教师独立判断：给分'], ['teacher_none', '教师独立判断：不给分'], ['teacher_partial', '教师判部分给分'], ['teacher_uncertain', '教师独立判断：不确定']];
  if (readOnly) return `<p class="teacher-saved-choice">教师判断：${escapeHtml(options.find(o => o[0] === savedChoice)?.[1] || '未单独记录')}</p>`;
  return `<div class="teacher-point-controls"><div class="teacher-quick-choices" role="group" aria-label="教师快捷判断">${[['teacher_full','给分'],['teacher_none','不给分'],['teacher_uncertain','不确定']].map(([value,label]) => `<button type="button" class="button secondary teacher-quick-choice" data-choice="${value}" aria-pressed="${savedChoice === value}">${label}</button>`).join('')}</div><details class="teacher-choice-details" ${savedChoice && !savedChoice.startsWith('teacher_') ? 'open' : ''}><summary>判断依据 / 特殊情况</summary><label>记录教师判断<select class="${className}" data-question-id="${escapeHtml(item.question_id)}" data-rubric-point-id="${escapeHtml(item.rubric_point_id)}">${options.map(([value,label]) => `<option value="${value}" ${value === savedChoice ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('')}</select></label></details></div>`;
}

// Quick choices write the same canonical select consumed by the existing save code.
document.addEventListener('click', event => {
  const button = event.target.closest('.teacher-quick-choice');
  if (!button) return;
  const select = button.closest('.teacher-point-controls').querySelector('select');
  if (select.disabled) return;
  select.value = button.dataset.choice;
  select.dispatchEvent(new Event('change', { bubbles: true }));
});
document.addEventListener('change', event => {
  const select = event.target.closest('.teacher-point-controls select');
  if (!select) return;
  const controls = select.closest('.teacher-point-controls');
  controls.querySelectorAll('.teacher-quick-choice').forEach(button => button.setAttribute('aria-pressed', String(select.value === button.dataset.choice)));
  updateTeacherScorePreview(select.closest('#submission-result'));
});

function updateTeacherScorePreview(root) {
  if (!root) return;
  const input = root.querySelector('#final-score');
  const button = root.querySelector('#confirm-result');
  const summary = root.querySelector('#teacher-score-summary');
  if (!input || !button || !summary) return;
  const questions = root._teacherQuestions || [];
  const result = root._teacherResult || {};
  let pending = 0, total = 0;
  const subtotals = questions.map(q => {
    let subtotal = 0, unresolved = 0;
    for (const c of q.criteria) {
      const select = [...root.querySelectorAll('.dual-point-final-choice')].find(x => x.dataset.questionId === q.id && x.dataset.rubricPointId === c.id);
      const choice = select?.value || '';
      let decision = ({ teacher_full: 'full', teacher_none: 'none', both_wrong_custom_hit: 'full', both_wrong_custom_not_hit: 'none' })[choice];
      if (['support_A', 'support_B', 'support_both'].includes(choice)) {
        const side = choice === 'support_B' ? 'B' : 'A';
        const source = result.dual_review?.results?.[side] || (side === 'A' ? result : {});
        decision = source.sub_questions?.find(x => x.sub_question_id === q.id)?.criteria?.find(x => x.criterion_id === c.id)?.decision;
      }
      if (!['full', 'none'].includes(decision)) { unresolved++; pending++; }
      if (decision === 'full') subtotal += Math.min(Number(c.score), Math.max(0, Number(q.max_score) - subtotal));
    }
    total += subtotal;
    return `${q.id}：${unresolved ? '待确定' : subtotal}`;
  });
  const complete = questions.length > 0 && pending === 0;
  input.value = complete ? total : '';
  button.disabled = !complete;
  summary.textContent = `教师小计：${subtotals.join('；')}。${complete ? `最终总分：${total}` : `终评未完成，${pending}个评分点待确定。`}`;
}

function teacherScopeBanner(result) {
  const ids = result.grading_scope?.graded_question_ids;
  return `<div class="teacher-scope-banner"><strong>本次评分范围：${escapeHtml(ids?.length ? ids.map(id => id.replace(/^Q/, '')).join('、') : '整场考试')}</strong><strong>本次满分：${escapeHtml(result.grading_scope?.scoped_max_score ?? result.max_total)} 分</strong></div>`;
}

function teacherQuestionCrop(submission, questionId) {
  if (submission.photos_deleted) return '<p class="muted">答案图片缓存已清理。</p>';
  const photoIndex = (submission.photos || []).findIndex(photo => photo.image_role === 'answer_region' && photo.answer_region_provenance?.question_id === questionId);
  if (photoIndex < 0) return '<p class="muted">此记录未保存本小题答案图，可查看原图核对。</p>';
  const url = `/api/submission-photo?submission_id=${encodeURIComponent(submission.id)}&photo_index=${photoIndex + 1}&preview=answer_region`;
  return `<figure class="teacher-answer-crop"><button type="button" class="teacher-crop-zoom" aria-label="放大 ${escapeHtml(questionId)} 学生答案"><img data-teacher-crop-src="${escapeHtml(url)}" alt="${escapeHtml(questionId)} 学生答案" loading="lazy"></button><figcaption>学生答案 · 点击放大</figcaption></figure>`;
}

async function loadTeacherCrops(root) {
  for (const img of root.querySelectorAll('img[data-teacher-crop-src]')) {
    const token = storedMobileToken();
    try {
      if (token) {
        const response = await fetch(img.dataset.teacherCropSrc, { headers: { Authorization: `Bearer ${token}` } });
        if (!response.ok) throw new Error('图片不可用');
        const blob = await response.blob();
        img.src = await new Promise((resolve, reject) => {
          const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(blob);
        });
      } else img.src = img.dataset.teacherCropSrc;
      img.addEventListener('error', () => { img.closest('figure').textContent = '答案图片不可用，请查看原图。'; }, { once: true });
    } catch { img.closest('figure').textContent = '答案图片不可用，请查看原图。'; }
  }
}
document.addEventListener('click', event => {
  const button = event.target.closest('.teacher-crop-zoom');
  if (!button) return;
  const source = button.querySelector('img');
  if (!source?.complete || !source.naturalWidth) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'teacher-crop-dialog';
  const close = document.createElement('button'); close.type = 'button'; close.className = 'button secondary'; close.textContent = '关闭放大图';
  const image = source.cloneNode(); image.removeAttribute('data-teacher-crop-src'); image.removeAttribute('loading');
  const imageScroll = document.createElement('div'); imageScroll.className = 'teacher-crop-image-scroll'; imageScroll.append(image);
  dialog.append(close, imageScroll); document.body.append(dialog);
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  dialog.showModal();
});

function renderResult(submission, elapsedMs = null, { readOnly = false } = {}) {
  window.latestSubmissionId = submission.id;
  const result = submission.result;
  const teacherReconfirmation = result.teacher_reconfirmation_required === true;
  const previousTeacherFinal = result.previous_teacher_final || {};
  const gradingScopeIds = result.grading_scope?.graded_question_ids || [];
  const savedTeacherFinal = result.dual_review?.teacher_final || result.teacher_final || {};
  const dualBaseModel = modelProviderDisplayName(
    result.dual_review?.models?.[result.dual_review?.base_side || 'A'],
    result.dual_review?.base_side || 'A',
  );
  const subQuestions = result.sub_questions || [];
  const unresolvedStatuses = new Set(['unanswered_candidate', 'question_unmatched', 'image_unreadable']);
  const unresolvedAnswerReview = subQuestions.some((item) => unresolvedStatuses.has(item.answer_status))
    || Boolean(result.unmapped_answers?.length);
  const recoveryPending = submission.state === 'recovery_pending';
  const hardBlocked = result.simulation_model || unresolvedAnswerReview || ['vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error', 'queued'].includes(result.mode);
  const reviewOverride = Boolean(submission.manual_reviewed) && result.mode === 'vision-model';
  const gradingBlocked = Boolean(result.blocked) || hardBlocked;
  const canConfirm = !readOnly && !hardBlocked && (!result.blocked || reviewOverride);
  const canReview = !readOnly && gradingBlocked && !unresolvedAnswerReview && !reviewOverride && !result.simulation_model && result.mode === 'vision-model';
  const canRetry = !readOnly && (!state.batchReview || isLocalPage()) && !submission.photos_deleted && (
    result.mode === 'processing-error'
    || (result.mode === 'vision-model-error' && result.diagnostics?.retryable === true)
    || (recoveryPending && result.mode === 'queued')
  );
  const retryBlockedDetail = result.mode === 'vision-model-error'
    ? `不建议直接重试：${escapeHtml(result.diagnostics?.message || '请先修正模型配置')}`
    : result.mode === 'image-quality-error'
      ? '照片质量不合格，未调用模型，请重新拍摄。'
      : '当前处理未通过安全门禁，请重新准备答卷。';
  const displayScoresBlocked = hardBlocked || (Boolean(result.blocked) && !reviewOverride);
  const scoreText = teacherReconfirmation
    ? '待教师重新确认'
    : unresolvedAnswerReview
    ? '待人工核对'
    : result.simulation_model
    ? '仅流程模拟'
    : canConfirm || canReview
    ? escapeHtml(result.suggested_total)
    : (canRetry ? '待重试' : '无法判分');
  const scoreLabel = teacherReconfirmation
    ? '本次评分范围终分'
    : result.simulation_model
    ? '模拟模型输出（不可作为成绩）'
    : canConfirm
    ? (reviewOverride ? (result.dual_review ? `${dualBaseModel}建议分（双评已完成原图复核）` : 'AI建议分（已完成原图复核）') : result.dual_review ? `${dualBaseModel}建议分（双评主视图）` : 'AI建议分')
    : canReview
      ? result.dual_review ? `${dualBaseModel}建议分（双评尚未核对）` : 'AI建议分（尚未核对）'
      : '当前结果';
  const statusPresentation = teacherReconfirmation
    ? { className: 'result-status-review', icon: '↻', title: '评分范围已修正 · 待教师重新确认', detail: '旧终分按整场考试满分保存，仅作为历史记录；请按当前小题范围重新确认最终分。' }
    : result.simulation_model
    ? { className: 'result-status-failed', icon: '!', title: '模拟模型结果', detail: '仅用于验证流程，不是真实 AI 评分，不能确认或导出为正式成绩。' }
    : canConfirm
    ? reviewOverride
      ? { className: 'result-status-reviewed', icon: '✓', title: '已完成原图复核 · 待教师确认', detail: '下方分数仍是 AI 建议，请按原图修改后保存最终分数。' }
      : { className: 'result-status-review', icon: '1', title: 'AI 已生成建议 · 待教师确认', detail: '请核对下方证据，教师确认后才会计入成绩。' }
      : canReview
      ? { className: 'result-status-review', icon: '1', title: '需要教师核对原图', detail: '当前分数只是 AI 建议，尚未计入成绩。' }
      : unresolvedAnswerReview
        ? { className: 'result-status-review', icon: '!', title: '答案与题号需要核对', detail: '未对应或无法读取的小题没有可确认分数；只有教师核实确为空白后才能按0分计。' }
        : canRetry
        ? { className: 'result-status-retry', icon: '↻', title: '本次没有得到可靠结果', detail: '请确认服务商记录后，再决定是否人工重试。' }
        : { className: 'result-status-failed', icon: '!', title: '暂时无法判分', detail: retryBlockedDetail };
  const reviewSteps = (canConfirm || canReview)
    ? `<div class="result-review-steps" aria-label="批改确认进度">
        <div class="review-step done"><span>✓</span><div><strong>AI 分析</strong><small>已完成</small></div></div>
        <div class="review-step ${reviewOverride ? 'done' : 'current'}"><span>${reviewOverride ? '✓' : '2'}</span><div><strong>核对证据</strong><small>${reviewOverride ? '已核对原图' : '当前步骤'}</small></div></div>
        <div class="review-step ${reviewOverride ? 'current' : 'upcoming'}"><span>3</span><div><strong>保存成绩</strong><small>${reviewOverride ? '等待教师确认' : '核对后完成'}</small></div></div>
      </div>`
    : '';
  const canDeletePhotoCache = !readOnly && submission.photos?.length && submission.confirmed_at && (isLocalPage() || submission.source_channel === 'mobile');
  const photoCacheControl = canDeletePhotoCache
    ? `<div class="photo-cache-control"><strong>本份照片缓存</strong><span>确认不再需要原图后，可只清理本份学生照片，不影响题目、成绩或评语。</span><button class="button danger" type="button" id="delete-submission-photos">删除本份照片缓存</button></div>`
    : '';
  const reconfirmationNotice = teacherReconfirmation
    ? `<div class="quality-warning"><strong>请重新确认本次最终分</strong><span>当前评分范围：${escapeHtml(gradingScopeIds.join(' + ') || '未记录')}，满分 ${escapeHtml(result.max_total)} 分。旧终分 ${escapeHtml(previousTeacherFinal.final_total ?? '—')} / ${escapeHtml(previousTeacherFinal.max_total ?? '—')} 已保留为历史记录，不会自动换算。已保存的逐点评判会继续保留；保存新终分后，系统按当前范围更新完整度。</span></div>`
    : '';
  const confirmationBox = readOnly
    ? `<div class="result-saved"><strong>已保存最终分：${escapeHtml(submission.final_total ?? '—')} / ${escapeHtml(result.grading_scope?.scoped_max_score ?? result.max_total)} 分</strong><span>评审详情仅供查看。</span></div>`
    : result.simulation_model
    ? '<div class="action-guide"><strong>当前为流程验收</strong><span>模拟模型结果不能确认、计入成绩或导出。</span></div>'
    : canConfirm
    ? `${reconfirmationNotice}<p class="hint final-score-guidance">逐点确定给分/不给分后自动计算；服务器校验并保存最终成绩。</p><div id="teacher-score-summary" role="status"></div><label>教师最终确认分（自动计算）<input id="final-score" type="number" min="0" max="${escapeHtml(result.max_total)}" readonly value=""></label><button class="button primary" type="button" id="confirm-result" disabled>确认最终成绩</button>`
    : canReview
      ? `<div class="action-guide"><strong>请按 3 步完成</strong><ol><li>${originalPhotoLink(submission, '打开原图')}，确认整页和答案清晰完整</li><li>对照下方“证据”和“判断”</li><li>确认无误后点击解锁确认</li></ol></div><button class="button secondary" type="button" id="review-result">已核对原图，解锁确认</button>`
      : canRetry
        ? `<div class="action-guide"><strong>请先确认是否需要重试</strong><span>${recoveryPending ? '上次服务中断，模型请求结果不确定。' : '本次模型调用未得到可靠结果。'}若服务商可能已计费，请先确认服务商记录。</span></div><button class="button secondary" type="button" id="retry-result">人工确认后重试</button>`
        : `<div class="action-guide"><strong>下一步：重新准备答卷</strong><span>${retryBlockedDetail}</span></div>`;
  $('#submission-result').classList.remove('empty');
  const blockReasons = (result.block_reasons || result.quality_issues || [])
    .map((item) => escapeHtml(item))
    .filter(Boolean);
  const blockNotice = blockReasons.length
    ? `<div class="quality-warning"><strong>${canReview ? '为什么需要你复核：' : '处理未完成原因：'}</strong><ul>${blockReasons.map((item) => '<li>' + item + '</li>').join('')}</ul></div>`
    : '';
  const questionRemapPanel = !readOnly && (result.unmapped_answers || []).length
    ? `<section class="question-remap-review"><h3>题号无法对应</h3><p>查看原图中的答案位置并指定题号。系统会再次展示将发送的整页图片和提示词，只有你确认后才会重新处理。</p>${result.unmapped_answers.map((item) => `
        <label class="question-remap-item">答案片段 ${escapeHtml(item.answer_id)} · 原图第${escapeHtml(item.photo_index)}页
          <a data-protected-image href="/api/submission-photo?submission_id=${encodeURIComponent(submission.id)}&photo_index=${encodeURIComponent(item.photo_index)}" target="_blank" rel="noopener">查看对应原图</a>
          <select data-question-remap-answer="${escapeHtml(item.answer_id)}"><option value="">选择正确小题</option>${subQuestions.map((sub) => `<option value="${escapeHtml(sub.sub_question_id)}">${escapeHtml(sub.label)} ${escapeHtml(sub.question || '')}</option>`).join('')}</select>
        </label>`).join('')}<button class="button secondary" id="reprocess-question-mapping" type="button">预览后按指定题号重新处理</button></section>`
    : !readOnly && result.requires_question_mapping
      ? '<div class="quality-warning"><strong>没有可供安全映射的答案位置。</strong><span>系统无法可靠定位答案片段；请重新拍摄清晰完整的答卷，或在批次中人工处理该份，不会把它当作零分。</span></div>'
      : '';
  const markingMapping = state.examMappings.find((item) => item.submission_id === submission.id);
  const anonymousRecord = Boolean(submission.marking_code_id);
  const mappingResolved = anonymousRecord && markingMapping?.binding_status === 'matched' && markingMapping.student_name;
  const identityNote='匿名答卷 '+submission.id;
  const identityBindingControl='';
  const teacherNoteField = (canConfirm || canReview)
    ? '<label class="teacher-note-field">教师评语 / 复核说明<textarea id="teacher-note" placeholder="可选：记录需要保留的判断说明"></textarea></label>'
    : '';
  const actionTitle = canConfirm ? '确认最终成绩' : canReview ? '完成原图复核' : canRetry ? '恢复本次任务' : '后续处理';
  const actionHint = canConfirm
    ? '只有保存确认后，本份结果才计入成绩。'
    : canReview
      ? '完成原图核对后，才会解锁最终分保存。'
      : canRetry
        ? '重试可能产生新的模型请求，请先确认服务商记录。'
        : '请根据上方原因重新准备答卷，当前结果不会计入成绩。';
  const subQuestionScoreOverview = subQuestions.length
    ? `<section class="subquestion-score-overview" aria-label="各小题评分">
        <div class="subquestion-score-overview-head"><strong>${result.simulation_model ? '各小题模拟分' : `各小题${result.dual_review ? `${dualBaseModel}建议分` : 'AI建议分'}`}</strong><small>${result.simulation_model ? '流程模拟不可计入成绩。' : 'AI建议仅供对照；教师终评按逐点决定计算。'}</small></div>
        <div class="subquestion-score-list">${subQuestions.map((sub) => {
          const needsReview = displayScoresBlocked
            || unresolvedStatuses.has(sub.answer_status)
            || sub.suggested_total == null;
          const subScoreText = result.simulation_model
            ? '模拟'
            : needsReview
              ? '待核对'
              : `${escapeHtml(sub.suggested_total)} / ${escapeHtml(sub.max_total)}`;
          return `<div class="subquestion-score-chip${needsReview || result.simulation_model ? ' is-pending' : ''}"><span>${escapeHtml(sub.label)}</span><strong>${subScoreText}</strong></div>`;
        }).join('')}</div>
      </section>`
    : '';
  const dualReview = result.dual_review;
  const dualRiskLabels = { HIGH: '高风险 · 建议优先复核', MEDIUM: '建议核对证据', LOW: '轻微差异', NO_CONFLICT: '未发现额外分歧' };
  const dualDecisionLabel = (value) => ({ full: '给分', none: '不给分', partial: '部分给分', uncertain: '不确定', missing: '缺失', conflict: '内部冲突' }[value] || value || '未知');
  const dualReviewPanel = dualReview ? (() => {
    const comparison = dualReview.comparison || {};
    const results = dualReview.results || {};
    const modelMeta = dualReview.models || {};
    const modelNames = dualReviewModelNames(dualReview);
    const risk = comparison.risk_level || (dualReview.status === 'complete' ? 'NO_CONFLICT' : 'HIGH');
    const scoreMeta = (side) => {
      const meta = modelMeta[side] || {};
      const modelResult = results[side] || {};
      const usage = meta.token_usage || {};
      const scopedMaximum = Number(result.grading_scope?.scoped_max_score ?? result.max_total);
      const modelTotal = Number(modelResult.suggested_total);
      const scopedModelTotal = Number.isFinite(modelTotal) && modelTotal >= 0 && modelTotal <= scopedMaximum ? modelTotal : '—';
      return `<div class="dual-model-summary"><strong>AI-${side} · ${escapeHtml(modelNames[side])}</strong><span>${meta.status === 'error' ? `调用失败（${escapeHtml(meta.error_category || 'unknown')}）` : `建议 ${escapeHtml(scopedModelTotal)} / ${escapeHtml(scopedMaximum)}`}</span><details><summary>技术详情</summary><small>${escapeHtml(meta.model_id || '')} · ${escapeHtml(meta.latency_ms ?? '—')} ms · ${escapeHtml(usage.total_tokens ?? '—')} tokens</small></details></div>`;
    };
    const rows = Array.isArray(comparison.point_comparisons) ? comparison.point_comparisons : [];
    return `<section class="dual-review-panel" aria-label="双模型对照结果">
      <div class="dual-review-head"><div><span class="section-kicker">本份双评对照</span><h3>${escapeHtml(modelNames.A)} × ${escapeHtml(modelNames.B)}</h3></div><strong class="dual-risk risk-${escapeHtml(risk.toLowerCase())}">${escapeHtml(dualRiskLabels[risk] || '需查看')}</strong></div>
      <p>${dualReview.status === 'complete' ? `两模型使用同一题目、评分细则、提示词和图片输入独立评分。${readOnly ? '本页展示已保存的教师终评；风险标签是当时的复核提示。' : '教师仍需确认最终分；风险标签只是复核提示。'}` : '双评未完整成功，当前结果已标记为受限状态；不能将单侧结果视作无冲突。'}</p>
      <div class="dual-model-summaries">${scoreMeta('A')}${scoreMeta('B')}</div>
      ${Array.isArray(comparison.review_reasons) && comparison.review_reasons.length ? `<div class="dual-reasons">复核原因：${comparison.review_reasons.map(dualReviewReasonLabel).map(escapeHtml).join('、')}</div>` : ''}
      ${rows.length ? `<details class="dual-point-details" open><summary>逐评分点对照（${rows.length}）</summary><div class="dual-point-list">${[...new Set(rows.map(item => item.question_id))].map(questionId => `<section class="teacher-question-group"><h4>小题 ${escapeHtml(questionId.replace(/^Q/, ''))}</h4>${teacherQuestionCrop(submission, questionId)}${rows.filter(item => item.question_id === questionId).map(item => `<article class="dual-point-row"><header><div class="teacher-point-title">${teacherPointTitle(result, item)}${item.rubric_echo_suspected ? '<span class="review-badge">引文疑似复述评分细则</span>' : ''}</div><span>A ${escapeHtml(item.score_a ?? '—')} 分 / B ${escapeHtml(item.score_b ?? '—')} 分</span></header><div class="dual-point-columns"><div><strong>AI-A · ${escapeHtml(dualDecisionLabel(item.decision_a))}</strong><p>${escapeHtml(item.evidence_a || '未提供引文')}</p><small>${escapeHtml(item.reason_a || '')}</small></div><div><strong>AI-B · ${escapeHtml(dualDecisionLabel(item.decision_b))}</strong><p>${escapeHtml(item.evidence_b || '未提供引文')}</p><small>${escapeHtml(item.reason_b || '')}</small></div></div>${teacherPointControls(item, savedTeacherPointChoice(savedTeacherFinal, item), 'dual-point-final-choice', readOnly)}</article>`).join('')}</section>`).join('')}</div><small>逐点保存给分、不计分或不确定，以及支持的模型；两模型判断和分数均一致时可记录为同时支持双方。所有评分点确定为给分/不给分后，才能确认最终成绩。</small></details>` : '<p class="muted">没有可供对照的完整评分点结果。</p>'}
    </section>`;
  })() : '';
  $('#submission-result').innerHTML = `
    <div class="result-card">
      <div class="result-status ${statusPresentation.className}"><span class="result-status-icon" aria-hidden="true">${statusPresentation.icon}</span><div><strong>${readOnly ? '已保存教师最终分 · 只读查看' : statusPresentation.title}</strong><span>${readOnly ? '评分点判断和最终分均来自已保存记录。' : statusPresentation.detail}</span></div></div>
      ${teacherScopeBanner(result)}
      ${readOnly ? '<p class="teacher-readonly-notice">已确认记录 · 只读评审详情</p>' : '<a class="button secondary" href="#teacher-final-confirmation">跳到最终确认</a>'}
      ${reviewSteps}
      ${dualReviewPanel}
      ${!dualReview ? `<section class="teacher-single-points"><h3>教师逐点终评</h3>${(submission.teacher_scoring_questions || []).map(q => `<section class="teacher-question-group"><h4>${escapeHtml(q.label || q.id)}</h4>${q.criteria.map(c => `<article class="dual-point-row"><strong>${escapeHtml(c.label)} · ${escapeHtml(c.score)}分</strong>${teacherPointControls({ question_id: q.id, rubric_point_id: c.id }, savedTeacherPointChoice(savedTeacherFinal, { question_id: q.id, rubric_point_id: c.id }), 'dual-point-final-choice', readOnly)}</article>`).join('')}</section>`).join('')}</section>` : ''}
      <div class="result-summary">
        <div class="score-block"><div class="score-label">${readOnly ? '教师最终分' : scoreLabel}</div><div class="score">${readOnly ? escapeHtml(submission.final_total ?? '—') : scoreText} <small>/ ${escapeHtml(result.max_total)}</small></div><strong>${escapeHtml(submission.student_label)}</strong><div class="identity-note">${escapeHtml(identityNote)}</div>${Number.isFinite(elapsedMs) ? `<div class="elapsed-note">本次处理耗时：${formatElapsed(elapsedMs)}</div>` : ''}</div>
        <div class="result-glance"><span>建议操作</span><strong>${readOnly ? '查看已保存的评审记录' : canConfirm ? '先核对证据，再保存最终分' : canReview ? '先打开原图核对答案' : canRetry ? '检查失败原因后决定是否重试' : '重新准备清晰完整的答卷'}</strong>${submission.photos?.length ? `<span>${originalPhotoLink(submission, '在新窗口打开第1页原图')}</span>` : ''}</div>
        ${subQuestionScoreOverview}
      </div>
      ${identityBindingControl}
      ${blockNotice}
      ${questionRemapPanel}
      <details id="full-answer-details" class="result-evidence" data-expanded-by-user="false" ><summary><span>评分证据与逐题判断</span><small>逐项核对学生答案、判断与建议分</small></summary><div class="subquestion-results">${subQuestions.map(sub => `
        <section class="subquestion-result">
          <div class="subquestion-result-head"><div><strong>${escapeHtml(sub.label)}</strong><details><summary>查看题干与共同材料</summary><p>${escapeHtml(sub.question || '')}</p></details>${sub.scoring_mode === 'point_bank' ? `<small class="subq-result-scoring-note">${escapeHtml(sub.point_value || 2)} 分/个不同要点 · ${Math.ceil(Number(sub.max_total || 0) / Number(sub.point_value || 2))} 个点满分 · 候选 ${Number(sub.criteria?.length || 0)} 点，满分封顶</small>` : ''}</div><strong>${displayScoresBlocked || sub.suggested_total == null ? '待核对' : escapeHtml(sub.suggested_total)} / ${escapeHtml(sub.max_total)}</strong></div>
          ${!readOnly && sub.answer_status === 'unanswered_candidate' ? `<div class="quality-warning"><strong>AI 只标记为“可能未作答”。</strong><span>核对原图后，只有确认留白才可按0分计。</span><button class="button secondary confirm-blank-subquestion" type="button" data-sub-question-id="${escapeHtml(sub.sub_question_id)}">我已核对原图，确认本题未作答</button></div>` : ''}
          ${sub.answer_status === 'question_unmatched' ? '<div class="quality-warning"><strong>题号无法对应。</strong><span>本题没有分数，不能直接确认；请指定对应题号或重新拍摄。</span></div>' : ''}
          ${sub.answer_status === 'image_unreadable' ? '<div class="quality-warning"><strong>图像无法读取。</strong><span>本题没有分数，不能当作未作答；请检查对应页面或重新拍摄。</span></div>' : ''}
          <div class="criteria-result">${(sub.criteria || []).map(item => `
            <div class="result-item">
              <div><strong>${escapeHtml(item.criterion)} ${item.needs_review ? '<span class="review-badge">需教师复核</span>' : ''} ${item.rubric_echo_suspected ? '<span class="review-badge">引文疑似复述评分细则</span>' : ''}</strong>
              <div class="evidence">证据：${escapeHtml(item.student_evidence || '未提取到')}${item.evidence?.photo_index ? ` · <a data-protected-image href="/api/submission-photo?submission_id=${encodeURIComponent(submission.id)}&photo_index=${encodeURIComponent(item.evidence.photo_index)}" target="_blank" rel="noopener">查看原图第${escapeHtml(item.evidence.photo_index)}页</a>` : ''}</div>
              <div class="reason">判断：${escapeHtml(item.reason || '—')}</div></div>
              <div class="item-score ${item.credit_status === 'capped' ? 'item-score-capped' : ''}">${displayScoresBlocked ? '待核对' : item.credit_status === 'capped' ? `已答出 · 满分封顶（实际计入 ${escapeHtml(item.suggested_score)} / ${escapeHtml(item.max_score)} 分）` : item.credit_status === 'duplicate_evidence' ? '证据重复 · 不重复计分' : `${escapeHtml(item.suggested_score)} / ${escapeHtml(item.max_score)}`}</div>
            </div>`).join('')}</div>
        </section>`).join('')}</div></details>
      <section class="result-confirmation" id="teacher-final-confirmation"><div class="result-confirmation-head"><span class="section-kicker">教师决定</span><h3>${readOnly ? '教师最终分' : actionTitle}</h3><p>${readOnly ? '只读查看已保存成绩。' : actionHint}</p></div><div class="result-primary-action">${teacherNoteField}<div class="confirm-box">${confirmationBox}</div></div></section>
      <div class="result-technical"><div class="mode-note"><details class="model-summary"><summary>查看模型说明</summary><p>${escapeHtml(result.summary || '未提供处理说明。')}</p></details><details><summary>查看技术状态</summary><span>${escapeHtml(result.mode)}</span></details>${renderDiagnostics(result)}</div>${photoCacheControl}</div>
    </div>`;
  if (readOnly) {
    $('#submission-result').querySelectorAll('a[data-protected-image]').forEach(link => {
      const url = new URL(link.href);
      if (url.pathname === '/api/submission-photo') { url.searchParams.set('preview', 'readonly'); link.href = url.href; }
    });
  }
  const scoreRoot = $('#submission-result');
  scoreRoot._teacherQuestions = submission.teacher_scoring_questions || [];
  scoreRoot._teacherResult = result;
  if (!readOnly) updateTeacherScorePreview(scoreRoot);
  else {
    const tf = result.dual_review?.teacher_final || result.teacher_final;
    if (tf?.score_source === 'teacher_points_deterministic_v1') {
      $('#teacher-final-confirmation').insertAdjacentHTML('afterbegin', `<p class="teacher-saved-subtotals">教师小计：${tf.question_subtotals.map(q => `${escapeHtml(q.question_id)} = ${escapeHtml(q.subtotal)}`).join('；')}；教师终评：${escapeHtml(tf.final_total)}</p>`);
    }
  }
  loadTeacherCrops(scoreRoot);
  const fullAnswerDetails = $('#full-answer-details');
  fullAnswerDetails?.querySelector('summary')?.addEventListener('click', () => {
    if (!fullAnswerDetails.open) fullAnswerDetails.dataset.expandedByUser = 'true';
  });
  if (canConfirm) $('#confirm-result').addEventListener('click', confirmResult);
  if (canReview) $('#review-result').addEventListener('click', reviewSubmission);
  if (canRetry) $('#retry-result').addEventListener('click', retrySubmission);
  $('#reprocess-question-mapping')?.addEventListener('click', reprocessQuestionMapping);
  document.querySelectorAll('.confirm-blank-subquestion').forEach((button) => button.addEventListener('click', () => confirmBlankSubQuestion(button)));
  const deletePhotosButton = $('#delete-submission-photos');
  if (deletePhotosButton) deletePhotosButton.addEventListener('click', deleteCurrentSubmissionPhotos);
  if (Number.isFinite(elapsedMs) && window.matchMedia?.('(max-width: 680px)').matches) {
    requestAnimationFrame(() => {
      const resultElement = $('#submission-result');
      if (resultElement) {
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        resultElement.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth', block: 'start' });
      }
    });
  }
}

async function confirmBlankSubQuestion(button) {
  const submissionId = window.latestSubmissionId;
  const subQuestionId = button?.dataset.subQuestionId;
  if (!submissionId || !subQuestionId) return;
  if (!window.confirm('请确认你已查看原图，并确认该小题确实没有作答。确认后该小题才按0分计；AI 的空白判断本身不会变成成绩。')) return;
  button.disabled = true;
  try {
    await api('/api/confirm-blank-subquestion', {
      method: 'POST',
      body: JSON.stringify({ submission_id: submissionId, sub_question_id: subQuestionId }),
    });
    const refreshed = await api(`/api/submission?submission_id=${encodeURIComponent(submissionId)}`);
    renderResult(refreshed.submission);
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

function confirmQuestionRemapPreview(preview) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'ai-privacy-dialog question-remap-preview-dialog';
    const card = document.createElement('section');
    card.className = 'ai-privacy-card';
    const title = document.createElement('h2');
    title.textContent = '确认重新处理的最终模型输入';
    const note = document.createElement('p');
    note.textContent = `服务商：${preview.provider || '未配置'} · 模型：${preview.model || '未配置'} · 指纹：${String(preview.preview_fingerprint || '').slice(0, 16)}…`;
    const imageList = document.createElement('div');
    imageList.className = 'question-remap-preview-images';
    for (const item of preview.images || []) {
      const figure = document.createElement('figure');
      const caption = document.createElement('figcaption');
      caption.textContent = `实际外发整页图 ${item.page} · SHA-256 ${String(item.sha256 || '').slice(0, 16)}…`;
      const image = document.createElement('img');
      image.src = item.data_url;
      image.alt = `即将外发的第 ${item.page} 页最终图片`;
      figure.append(caption, image);
      imageList.append(figure);
    }
    const promptDetails = document.createElement('details');
    promptDetails.open = true;
    const summary = document.createElement('summary');
    summary.textContent = '查看实际提示词';
    const prompt = document.createElement('pre');
    prompt.className = 'ai-payload-prompt';
    prompt.textContent = preview.prompt || '';
    promptDetails.append(summary, prompt);
    const footer = document.createElement('footer');
    const cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'button secondary'; cancel.textContent = '取消';
    const send = document.createElement('button');
    send.type = 'button'; send.className = 'button primary'; send.textContent = '确认以上图片并重新处理';
    footer.append(cancel, send);
    card.append(title, note, imageList, promptDetails, footer);
    dialog.append(card);
    document.body.append(dialog);
    const finish = (approved) => {
      dialog.close();
      dialog.remove();
      resolve(approved);
    };
    cancel.addEventListener('click', () => finish(false));
    send.addEventListener('click', () => finish(true));
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); finish(false); }, { once: true });
    try { dialog.showModal(); } catch (_) { dialog.remove(); resolve(false); }
  });
}

async function reprocessQuestionMapping() {
  const submissionId = window.latestSubmissionId;
  const button = $('#reprocess-question-mapping');
  if (!submissionId || !button) return;
  const selects = [...document.querySelectorAll('[data-question-remap-answer]')];
  if (!selects.length || selects.some((select) => !select.value)) return alert('请为每个未匹配答案片段指定小题');
  const questionMapping = selects.map((select) => ({
    answer_id: select.dataset.questionRemapAnswer,
    sub_question_id: select.value,
  }));
  button.disabled = true;
  try {
    const preview = await api('/api/question-remap-preview', {
      method: 'POST',
      body: JSON.stringify({ submission_id: submissionId, question_mapping: questionMapping }),
    });
    if (preview.blocked || !preview.preview_token || !preview.images?.length) {
      throw new Error(preview.error || '无法生成重新处理的最终图片预览');
    }
    if (!await confirmQuestionRemapPreview(preview)) return;
    await api('/api/reprocess-question-mapping', {
      method: 'POST',
      body: JSON.stringify({
        submission_id: submissionId,
        question_mapping: questionMapping,
        ai_privacy_confirmed: true,
        ai_privacy_preview_token: preview.preview_token,
        ai_privacy_fingerprint: preview.preview_fingerprint,
      }),
    });
    const completed = await waitForSubmission(submissionId);
    renderResult(completed);
  } catch (error) {
    alert(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function deleteCurrentSubmissionPhotos() {
  const button = $('#delete-submission-photos');
  if (!button || !window.latestSubmissionId) return;
  button.disabled = true;
  try {
    const data = await deleteSubmissionPhotosRequest(window.latestSubmissionId);
    if (!data) return;
    renderResult(data.submission);
    alert(data.message);
  } catch (error) {
    alert(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function reviewSubmission() {
  const button = $('#review-result');
  const reviewNote = $('#teacher-note')?.value || '';
  button.disabled = true;
  try {
    await api('/api/review', {
      method: 'POST',
      body: JSON.stringify({
        submission_id: window.latestSubmissionId,
        confirm_original: true,
        review_note: reviewNote,
      }),
    });
    const data = await api(`/api/submission?submission_id=${encodeURIComponent(window.latestSubmissionId)}`);
    renderResult(data.submission);
    if ($('#teacher-note')) $('#teacher-note').value = reviewNote;
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

async function retrySubmission() {
  const button = $('#retry-result');
  button.disabled = true;
  try {
    await api('/api/retry', { method: 'POST', body: JSON.stringify({ submission_id: window.latestSubmissionId }) });
    const completed = await waitForSubmission(window.latestSubmissionId);
    renderResult(completed);
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

async function confirmResult() {
  const button = $('#confirm-result');
  const scoreInput = $('#final-score');
  const finalTotal = Number(scoreInput?.value);
  const maximum = Number(scoreInput?.max);
  if (!Number.isFinite(finalTotal) || finalTotal < 0 || (Number.isFinite(maximum) && finalTotal > maximum)) {
    alert(`请输入 0 至 ${Number.isFinite(maximum) ? maximum : '题目满分'} 之间的最终分。`);
    scoreInput?.focus();
    return;
  }
  button.disabled = true;
  const batchContext = state.batchReview;
  if (batchContext) batchContext.saving = true;
  const continueAfterConfirm = !batchContext && isContinuousMode();
  try {
    await api('/api/confirm', { method: 'POST', body: JSON.stringify({
      submission_id: window.latestSubmissionId,
      teacher_note: $('#teacher-note')?.value || '',
      expanded_full_answer: $('#full-answer-details')?.dataset.expandedByUser === 'true',
      teacher_point_decisions: [...document.querySelectorAll('.dual-point-final-choice')]
        .filter((select) => select.value)
        .map((select) => ({ question_id: select.dataset.questionId, rubric_point_id: select.dataset.rubricPointId, choice: select.value })),
    }) });
    await loadAssignments();
    document.querySelectorAll('.dual-point-final-choice').forEach((select) => { select.disabled = true; select.closest('.teacher-point-controls')?.querySelectorAll('button').forEach(button => { button.disabled = true; }); });
    if (batchContext) {
      batchContext.dirty = false;
      batchContext.saving = false;
      const refreshed = await api(`/api/submission?submission_id=${encodeURIComponent(batchContext.submissionId)}`);
      batchReviewRows().find(row => row.dataset.submissionId === batchContext.submissionId)?.remove();
      renderBatchRow(refreshed.submission);
      await openBatchSubmissionForReview(batchContext.submissionId);
    } else if (continueAfterConfirm) {
      state.continuousCompleted += 1;
      prepareNextSubmission(`第 ${state.continuousCompleted} 份已保存，下一份已准备`);
    } else {
      if ($('#teacher-note')) $('#teacher-note').disabled = true;
      const box = button.closest('.confirm-box');
      if (box) {
        box.innerHTML = '<div class="result-saved"><span aria-hidden="true">✓</span><div><strong>最终成绩已保存</strong><small>本份结果已计入作业记录。</small></div></div><button class="button secondary" type="button" id="prepare-next-result">批改下一份</button>';
        $('#prepare-next-result')?.addEventListener('click', () => prepareNextSubmission('上一份已保存，可开始下一份'));
      }
      updateContinuousStatus();
    }
  } catch (error) { alert(error.message); button.disabled = false; }
  finally { if (batchContext) batchContext.saving = false; }
}

async function createSubmission(event) {
  event.preventDefault();
  const button = event.submitter;
  const files = filesFromInputs(SINGLE_PHOTO_INPUTS);
  if (!$('#assignment-select').value) return alert('请先保存并选择一道题');
  if (!files.length && !$('#answer-text').value.trim()) return alert('请上传照片，或填写答案文字用于演示');
  button.disabled = true;
  button.textContent = '正在处理…';
  const startedAt = performance.now();
  try {
    const submissionFields = {
      assignment_id: $('#assignment-select').value,
      class_name: $('#class-select').value,
      student_label: $('#student-label').value,
      answer_text: $('#answer-text').value,
      dual_review_enabled: $('#dual-review-enabled')?.checked === true,
      idempotency_key: createIdempotencyKey(),
    };
    if (submissionFields.class_name) await loadRosterStudents(submissionFields.class_name);
    const privacyApprovals = files.length
      ? await reviewAiPrivacyBatch(submissionFields.assignment_id, [{ studentLabel: submissionFields.student_label, files: [...files] }], submissionFields.class_name, submissionFields.dual_review_enabled)
      : [];
    const privacyApproval = privacyApprovals[0] || null;
    const body = await buildSubmissionBody(submissionFields, files, privacyApproval);
    const data = await api('/api/submissions', { method: 'POST', body });
    const completed = await waitForSubmission(data.submission.id, (submission) => {
      setStatus(submission.state === 'grading' ? `${isCloudRuntime() ? '服务器' : '本机'}正在批改…` : `已进入${isCloudRuntime() ? '服务器' : '本机'}批改队列…`);
    });
    renderResult(completed, performance.now() - startedAt);
    await loadAssignments();
    await loadRecoverySubmissions();
    clearSubmissionDraft();
    updateContinuousStatus(data.submission.result?.mode === 'vision-model-error' || data.submission.result?.dual_review?.status !== undefined && data.submission.result?.dual_review?.status !== 'complete'
      ? '本份双评未完整成功；请先查看失败一侧并按原图核对，完成确认后才会进入下一份。'
      : '请核对本份批改结果；修改最终分数后点击“保存确认”，再进入下一份。');
  } catch (error) {
    $('#submission-result').innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
  } finally { button.disabled = false; button.textContent = '开始辅助批改'; }
}

function fileStem(file) {
  return file.name.replace(/\.[^.]+$/, '') || file.name;
}

function batchGroupLabel(file, mode) {
  const stem = fileStem(file);
  if (mode === 'each') return stem;
  const match = stem.match(/^(.+?)[-_—–\s](?:第?\d+(?:页)?|p(?:age)?\d+)$/i);
  return match ? match[1] : stem;
}

function groupBatchFiles(files,mode){return mode==='all'?[{studentLabel:'第1份',files:[...files]}]:files.map((file,i)=>({studentLabel:`第${i+1}份`,files:[file]}))}

const MAX_CAMERA_PHOTOS = 20;
// Keep provider pressure bounded while allowing a batch to make progress.
const BATCH_CONCURRENCY = 2;

function cameraStreamSupported() {
  return Boolean(window.isSecureContext && navigator.mediaDevices && typeof navigator.mediaDevices.getUserMedia === 'function');
}

function clearCameraPreviewUrls() {
  state.cameraSession.previewUrls.forEach((url) => URL.revokeObjectURL(url));
  state.cameraSession.previewUrls = [];
}

function stopCameraStream() {
  if (state.cameraSession.stream) {
    state.cameraSession.stream.getTracks().forEach((track) => track.stop());
    state.cameraSession.stream = null;
  }
  const video = $('#camera-preview');
  if (video) video.srcObject = null;
}

function renderCameraQueue() {
  const container = $('#camera-thumbs');
  if (!container) return;
  clearCameraPreviewUrls();
  container.innerHTML = state.cameraSession.files.map((file, index) => {
    const previewUrl = URL.createObjectURL(file);
    state.cameraSession.previewUrls.push(previewUrl);
    return `<figure class="camera-photo">
      <img src="${escapeHtml(previewUrl)}" alt="第${index + 1}张待上传照片">
      <figcaption title="第${index + 1}张">第${index + 1}张</figcaption>
      <button class="icon-button remove-camera-photo" type="button" data-camera-index="${index}" title="删除并重拍">×</button>
    </figure>`;
  }).join('');
  updateCameraSessionControls();
}

function updateCameraSessionControls() {
  const count = state.cameraSession.files.length;
  const countElement = $('#camera-session-count');
  if (countElement) countElement.textContent = `已加入 ${count} / ${MAX_CAMERA_PHOTOS} 张`;
  const captureButton = $('#camera-capture');
  if (captureButton) captureButton.disabled = count >= MAX_CAMERA_PHOTOS || !state.cameraSession.stream;
  ['#camera-fallback-input', '#camera-album-input'].forEach((selector) => {
    const input = $(selector);
    if (input) input.disabled = count >= MAX_CAMERA_PHOTOS;
  });
  const finishButton = $('#camera-finish');
  if (finishButton) finishButton.disabled = count === 0;
}

function setCameraSessionNote(message, isError = false) {
  const note = $('#camera-session-note');
  if (!note) return;
  note.textContent = message;
  note.classList.toggle('error', isError);
}

async function startCameraStream() {
  const video = $('#camera-preview');
  const placeholder = $('#camera-preview-placeholder');
  if (!video) return;
  stopCameraStream();
  if (!cameraStreamSupported()) {
    if (placeholder) {
      placeholder.hidden = false;
      placeholder.textContent = '当前地址是 HTTP 局域网页面，浏览器通常不允许网页直接调用相机';
    }
    setCameraSessionNote('当前为 HTTP 局域网访问，请使用“系统相机逐张添加”；照片仍会进入同一队列，拍完后统一上传。');
    updateCameraSessionControls();
    return;
  }
  setCameraSessionNote('正在请求相机权限；允许后可连续点击“拍一张”。');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      audio: false,
    });
    state.cameraSession.stream = stream;
    video.srcObject = stream;
    video.onloadedmetadata = () => {
      if (placeholder) placeholder.hidden = true;
      updateCameraSessionControls();
    };
    await video.play().catch(() => {});
    if (placeholder && video.videoWidth) placeholder.hidden = true;
    setCameraSessionNote('相机已开启。每拍一名学生点击一次“拍一张”；拍错可在缩略图右上角删除重拍。');
    updateCameraSessionControls();
  } catch (error) {
    if (placeholder) {
      placeholder.hidden = false;
      placeholder.textContent = '网页相机不可用，请使用下方系统相机逐张添加';
    }
    setCameraSessionNote('网页相机未能开启（可能是权限、HTTP 地址或浏览器限制），可改用“系统相机逐张添加”。', true);
    updateCameraSessionControls();
  }
}

async function openCameraSession() {
  if (state.batchDraft) return alert('当前已有批量预览，请先完成或取消它，再开始新的连续拍摄。');
  const panel = $('#camera-session');
  if (!panel) return;
  panel.hidden = false;
  renderCameraQueue();
  await startCameraStream();
}

function makeCameraFile(blob, name) {
  try {
    return new File([blob], name, { type: 'image/jpeg', lastModified: Date.now() });
  } catch (error) {
    blob.name = name;
    blob.lastModified = Date.now();
    return blob;
  }
}

function captureCameraPhoto() {
  if (!state.cameraSession.stream || state.cameraSession.files.length >= MAX_CAMERA_PHOTOS) return;
  const video = $('#camera-preview');
  const canvas = $('#camera-canvas');
  if (!video || !canvas || !video.videoWidth || !video.videoHeight) {
    return alert('相机画面尚未准备好，请稍等片刻后再拍。');
  }
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  const context = canvas.getContext('2d');
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  canvas.toBlob((blob) => {
    if (!blob) return alert('照片采集失败，请重试。');
    state.cameraSession.sequence += 1;
    const name = `camera-${String(state.cameraSession.sequence).padStart(3, '0')}.jpg`;
    state.cameraSession.files.push(makeCameraFile(blob, name));
    renderCameraQueue();
    setCameraSessionNote(state.cameraSession.files.length >= MAX_CAMERA_PHOTOS
      ? '已达到20张上限，请点击“完成拍摄并预览”。'
      : '已加入队列；可以继续拍下一名学生。');
  }, 'image/jpeg', 0.92);
}

function addCameraFallbackPhotos(event) {
  const files = [...(event.target.files || [])];
  event.target.value = '';
  if (!files.length) return;
  const remaining = MAX_CAMERA_PHOTOS - state.cameraSession.files.length;
  if (remaining <= 0) return alert('连续拍摄队列最多20张照片。');
  const accepted = files.slice(0, remaining);
  state.cameraSession.files.push(...accepted);
  state.cameraSession.sequence += accepted.length;
  renderCameraQueue();
  if (files.length > accepted.length) alert(`连续拍摄队列最多20张照片，本次仅加入前${accepted.length}张。`);
  setCameraSessionNote(state.cameraSession.files.length >= MAX_CAMERA_PHOTOS
    ? '已达到20张上限，请点击“完成拍摄并预览”。'
    : '照片已加入队列；可继续从系统相机拍摄，或从相册补充。');
}

function finishCameraSession() {
  if (!state.cameraSession.files.length) return alert('请至少拍摄一张照片。');
  const groups = state.cameraSession.files.map((file, index) => ({ studentLabel: `第${index + 1}名`, files: [file] }));
  stopCameraStream();
  clearCameraPreviewUrls();
  state.cameraSession.files = [];
  state.cameraSession.sequence = 0;
  const panel = $('#camera-session');
  if (panel) panel.hidden = true;
  $('#batch-group-mode').value = 'each';
  renderCameraQueue();
  renderBatchPreview(groups);
  $('#batch-form button[type="submit"]').textContent = '确认分组并开始批改';
  $('#batch-photo-count').textContent = `连续拍摄队列已准备 ${groups.length} 名学生；请先核对缩略图和学生标识`;
  $('#batch-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function cancelCameraSession() {
  stopCameraStream();
  clearCameraPreviewUrls();
  state.cameraSession.files = [];
  state.cameraSession.sequence = 0;
  const panel = $('#camera-session');
  if (panel) panel.hidden = true;
  renderCameraQueue();
  setCameraSessionNote('已取消本次连续拍摄。');
}

function clearBatchPreviewUrls() {
  state.batchPreviewUrls.forEach((url) => URL.revokeObjectURL(url));
  state.batchPreviewUrls = [];
}

function resetBatchDraft(clearInput = false, restoreButton = true) {
  clearBatchPreviewUrls();
  state.batchDraft = null;
  if (clearInput) {
    clearFileInputs(BATCH_PHOTO_INPUTS);
    updateBatchPhotoCount();
  }
  if (restoreButton) $('#batch-form button[type="submit"]').textContent = '上传并预览分组';
}

function syncBatchDraftLabels() {
  if (!state.batchDraft) return;
  document.querySelectorAll('.batch-preview-group').forEach((card) => {
    const index = Number(card.dataset.groupIndex);
    const draft = state.batchDraft[index];
    if (!draft) return;
    draft.studentLabel = card.querySelector('.batch-preview-label').value.trim() || `第${index + 1}组`;
  });
}

function renderBatchPreview(groups) {
  clearBatchPreviewUrls();
  state.batchDraft = groups.map((group, index) => ({
    studentLabel: group.studentLabel || `第${index + 1}组`,
    files: [...group.files],
  }));
  const totalFiles = state.batchDraft.reduce((sum, group) => sum + group.files.length, 0);
  $('#batch-result').classList.remove('empty');
  $('#batch-result').innerHTML = `
    <div class="batch-preview">
      <div class="batch-preview-head">
        <div>
          <strong>已暂存 ${state.batchDraft.length} 名学生，共 ${totalFiles} 页</strong>
          <small>当前只是预览，不会调用模型。请核对分组、页数和学生标识，确认后才开始批改。</small>
        </div>
        <div class="batch-preview-actions">
          <button id="batch-cancel-preview" class="button secondary" type="button">取消预览</button>
        </div>
      </div>
      <div class="batch-preview-list">
        ${state.batchDraft.map((group, groupIndex) => `
          <section class="batch-preview-group" data-group-index="${groupIndex}">
            <div class="batch-preview-group-head">
              <label>学生标识
                <input class="batch-preview-label" value="${escapeHtml(group.studentLabel)}" placeholder="如：张三或0101">
              </label>
              <div class="batch-order-controls"><strong>${group.files.length} 页</strong>
                <button class="button secondary move-batch-group" type="button" data-direction="-1" data-group-index="${groupIndex}" ${groupIndex === 0 ? 'disabled' : ''} aria-label="上移第${groupIndex + 1}份">↑</button>
                <button class="button secondary move-batch-group" type="button" data-direction="1" data-group-index="${groupIndex}" ${groupIndex === state.batchDraft.length - 1 ? 'disabled' : ''} aria-label="下移第${groupIndex + 1}份">↓</button>
              </div>
            </div>
            <div class="batch-preview-thumbs">
              ${group.files.map((file, fileIndex) => {
                const previewUrl = URL.createObjectURL(file);
                state.batchPreviewUrls.push(previewUrl);
                return `<figure class="batch-preview-photo" data-file-index="${fileIndex}">
                  <img src="${escapeHtml(previewUrl)}" alt="${escapeHtml(file.name)}">
                  <figcaption title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</figcaption>
                  <div class="batch-page-controls">
                    <button class="icon-button move-batch-page" type="button" data-direction="-1" data-group-index="${groupIndex}" data-file-index="${fileIndex}" ${fileIndex === 0 ? 'disabled' : ''} aria-label="前移此页">↑</button>
                    <button class="icon-button move-batch-page" type="button" data-direction="1" data-group-index="${groupIndex}" data-file-index="${fileIndex}" ${fileIndex === group.files.length - 1 ? 'disabled' : ''} aria-label="后移此页">↓</button>
                    <button class="icon-button remove-batch-photo" type="button" data-group-index="${groupIndex}" data-file-index="${fileIndex}" title="移除照片">×</button>
                  </div>
                </figure>`;
              }).join('')}
            </div>
          </section>`).join('')}
      </div>
    </div>`;
  $('#batch-cancel-preview').addEventListener('click', () => {
    resetBatchDraft(true);
    $('#batch-result').className = 'result-area empty';
    $('#batch-result').textContent = '批量结果会显示在这里';
  });
  document.querySelectorAll('.remove-batch-photo').forEach((button) => {
    button.addEventListener('click', () => {
      syncBatchDraftLabels();
      const groupIndex = Number(button.dataset.groupIndex);
      const fileIndex = Number(button.dataset.fileIndex);
      const group = state.batchDraft[groupIndex];
      if (!group) return;
      group.files.splice(fileIndex, 1);
      if (!group.files.length) state.batchDraft.splice(groupIndex, 1);
      if (!state.batchDraft.length) {
        resetBatchDraft(true);
        $('#batch-result').className = 'result-area empty';
        $('#batch-result').textContent = '批量结果会显示在这里';
        return;
      }
      renderBatchPreview(state.batchDraft);
    });
  });
  document.querySelectorAll('.move-batch-group').forEach((button) => {
    button.addEventListener('click', () => {
      syncBatchDraftLabels();
      const index = Number(button.dataset.groupIndex);
      const target = index + Number(button.dataset.direction);
      if (target < 0 || target >= state.batchDraft.length) return;
      [state.batchDraft[index], state.batchDraft[target]] = [state.batchDraft[target], state.batchDraft[index]];
      renderBatchPreview(state.batchDraft);
    });
  });
  document.querySelectorAll('.move-batch-page').forEach((button) => {
    button.addEventListener('click', () => {
      syncBatchDraftLabels();
      const group = state.batchDraft[Number(button.dataset.groupIndex)];
      if (!group) return;
      const index = Number(button.dataset.fileIndex);
      const target = index + Number(button.dataset.direction);
      if (target < 0 || target >= group.files.length) return;
      [group.files[index], group.files[target]] = [group.files[target], group.files[index]];
      renderBatchPreview(state.batchDraft);
    });
  });
}

function updateBatchCompletionSummary(batch = state.activeBatch) {
  const target = $('#batch-completion-summary');
  if (!target || !batch) return;
  const items = batch.items || [];
  const total = items.length;
  const count = (status) => items.filter((item) => item.model_status === status).length;
  const processed = count('processed');
  const partial = count('partial');
  const failed = count('failed');
  const skipped = count('skipped');
  const finished = processed + partial + failed + skipped;
  const eligible = items.filter((item) => item.teacher_review_required === true);
  const confirmed = eligible.filter((item) => item.teacher_confirmed === true || Boolean(item.confirmed_at)).length;
  const pendingFinal = Math.max(0, eligible.length - confirmed);
  const processingLabel = finished === total && total > 0 ? '模型处理已结束' : '模型处理';
  const finalLabel = pendingFinal ? `${pendingFinal} 份待教师确认` : '待确认 0 份';
  target.textContent = `${processingLabel}：${processed + partial}/${total} 份已返回结果（${partial} 份双评未完整），${failed} 份失败，${skipped} 份跳过，${Math.max(0, total - finished)} 份待处理。教师终评：${confirmed}/${eligible.length} 完成，${finalLabel}。`;
  if (finished === total && processed === total && pendingFinal === 0 && total > 0) {
    target.textContent += ' 本批模型处理与教师终评均已完成。';
  }
}

function syncBatchSubmissionStatus(submission) {
  const batch = state.activeBatch;
  if (!batch || submission?.batch_id !== batch.batchId) return;
  const item = (batch.items || []).find((entry) => entry.submission_id === submission.id);
  if (!item) return;
  const result = submission.result || {};
  const failedModes = ['vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error', 'queued'];
  if (submission.state === 'failed' || failedModes.includes(result.mode) || result.simulation_model) {
    item.model_status = 'failed';
    item.teacher_review_required = false;
  } else if (submission.state === 'completed') {
    item.model_status = result.dual_review?.status === 'degraded' ? 'partial' : 'processed';
    item.teacher_review_required = true;
  } else {
    item.model_status = 'pending';
    item.teacher_review_required = false;
  }
  item.confirmed_at = submission.confirmed_at || item.confirmed_at || null;
  item.teacher_confirmed = Boolean(item.confirmed_at);
  updateBatchCompletionSummary(batch);
}

function batchReviewRows() {
  return [...document.querySelectorAll('#batch-results-body tr[data-submission-id]')];
}

function nextPendingBatchReview() {
  const rows = batchReviewRows();
  const current = rows.findIndex(row => row.dataset.submissionId === state.batchReview?.submissionId);
  return [...rows.slice(current + 1), ...rows.slice(0, Math.max(current, 0))].find(row => row.dataset.reviewPending === 'true');
}

function refreshBatchReviewNavigation() {
  if (!state.batchReview) return;
  const next = nextPendingBatchReview();
  document.querySelectorAll('#next-batch-review, [data-batch-next]').forEach(button => {
    button.disabled = !next || state.batchReview.saving;
    button.textContent = next ? '下一份待评审' : '暂无下一份待评审';
  });
  const rows = batchReviewRows();
  const current = rows.findIndex(row => row.dataset.submissionId === state.batchReview.submissionId);
  const counter = $('#batch-review-navigation small');
  if (counter) counter.textContent = `批次评审 · 第 ${current + 1} / ${rows.length} 份已返回结果`;
}

function batchReviewCanLeave() {
  if (state.batchReview?.saving) return false;
  return !state.batchReview?.dirty || window.confirm('本份评审有尚未保存的修改，是否离开？');
}

function closeBatchReview() {
  if (!batchReviewCanLeave()) return false;
  if (!state.batchReview) return true;
  $('#single-grading-pane').appendChild($('#submission-result'));
  $('#submission-result').className = 'result-area empty';
  $('#submission-result').textContent = '上传答卷后，批改结果会显示在这里';
  window.latestSubmissionId = null;
  $('#batch-review-workspace').hidden = true;
  $('#batch-grading-pane .batch-section').hidden = false;
  state.batchReview = null;
  $('#submission-result').oninput = null;
  $('#submission-result').onchange = null;
  return true;
}

async function openBatchSubmissionForReview(submissionId) {
  if (!batchReviewCanLeave()) return;
  const navigation = $('#batch-review-navigation');
  try {
    const data = await api(`/api/submission?submission_id=${encodeURIComponent(submissionId)}`);
    const submission = data.submission;
    if (!state.batchReview && !selectGradingMode('batch')) return;
    state.batchReview = { submissionId, dirty: false, saving: false };
    $('#batch-grading-pane .batch-section').hidden = true;
    $('#batch-review-workspace').hidden = false;
    $('#batch-review-workspace').appendChild($('#submission-result'));
    const rows = batchReviewRows();
    const current = rows.findIndex(row => row.dataset.submissionId === submissionId);
    const next = nextPendingBatchReview();
    navigation.innerHTML = `<div><strong>${escapeHtml(submission.student_label || '本份答卷')}</strong><small>批次评审 · 第 ${current + 1} / ${rows.length} 份已返回结果</small></div><div class="batch-review-navigation-actions"><button class="button secondary" type="button" id="return-to-batch">返回批次</button><button class="button secondary" type="button" id="next-batch-review" ${next ? '' : 'disabled'}>${next ? '下一份待评审' : '暂无下一份待评审'}</button></div>`;
    $('#return-to-batch').addEventListener('click', () => {
      if (closeBatchReview()) $('#batch-result').scrollIntoView({ block: 'start' });
    });
    $('#next-batch-review').addEventListener('click', () => {
      const next = nextPendingBatchReview();
      if (next) openBatchSubmissionForReview(next.dataset.submissionId);
    });
    renderResult(submission, null, { readOnly: submission.final_total != null && !submission.result?.teacher_reconfirmation_required });
    const footerNavigation = document.createElement('div');
    footerNavigation.className = 'batch-review-navigation-actions';
    footerNavigation.innerHTML = `<button class="button secondary" type="button" data-batch-return>返回批次</button><button class="button secondary" type="button" data-batch-next ${next ? '' : 'disabled'}>${next ? '下一份待评审' : '暂无下一份待评审'}</button>`;
    $('#teacher-final-confirmation').appendChild(footerNavigation);
    footerNavigation.querySelector('[data-batch-return]').addEventListener('click', () => $('#return-to-batch').click());
    footerNavigation.querySelector('[data-batch-next]').addEventListener('click', () => $('#next-batch-review').click());
    $('#submission-result').oninput = () => { if (state.batchReview) state.batchReview.dirty = true; };
    $('#submission-result').onchange = () => { if (state.batchReview) state.batchReview.dirty = true; };
    $('#batch-review-workspace').scrollIntoView({ block: 'start' });
  } catch (error) { alert(error.message); }
}

function renderBatchShell(total) {
  $('#batch-result').classList.remove('empty');
  $('#batch-result').innerHTML = `
    <div id="batch-progress" class="batch-progress">准备上传 ${total} 份答卷；照片可并发上传，模型将严格按序号逐份批改。</div>
    <p><a class="button primary" href="/next/#/history?assignment=${encodeURIComponent($('#batch-assignment-select')?.value || '')}&exam=${encodeURIComponent($('#batch-exam-select')?.value || '')}">双评成绩、批量采纳与异常复核</a></p>
    <div id="batch-completion-summary" class="batch-progress batch-completion-summary">模型处理：0/${total} 份已返回结果。教师终评：0/0 完成。</div>
    <p class="hint">先查看整批进度，再点击“打开评审”；答案、双评对照和最终分确认与单份批改共用同一页面。</p>
    <p><button id="batch-stop-remaining" class="button secondary" type="button" hidden>停止后续上传</button></p>
    <div id="batch-timing" class="batch-timing">平均耗时：— · 整批耗时：—</div>
    <div class="batch-table-wrap"><table class="batch-table">
      <thead><tr><th>原序号 / 学生</th><th>AI建议总分</th><th>分题分数</th><th>模式</th><th>本次耗时</th><th>教师确认</th></tr></thead>
      <tbody id="batch-results-body"></tbody>
    </table></div>`;
}

function renderBatchDualReview(result, submission) {
  const dual = result?.dual_review;
  if (!dual) return '';
  const comparison = dual.comparison || {};
  const results = dual.results || {};
  const names = dualReviewModelNames(dual);
  const labels = { HIGH: '高风险 · 建议优先复核', MEDIUM: '核对证据', LOW: '轻微差异', NO_CONFLICT: '无额外冲突' };
  const rows = comparison.point_comparisons || [];
  return `<details class="batch-dual-review"><summary>双评对照：${escapeHtml(labels[comparison.risk_level] || '需教师核对')} · ${escapeHtml(names.A)} ${escapeHtml(results.A?.suggested_total ?? '—')} / ${escapeHtml(names.B)} ${escapeHtml(results.B?.suggested_total ?? '—')}</summary>
    ${teacherScopeBanner(result)}
    <small>${dual.status === 'complete' ? escapeHtml(labels[comparison.risk_level] || '请教师核对') : '双评未完整成功，不能按无冲突处理。'}</small>
    ${(comparison.review_reasons || []).length ? `<small>原因：${comparison.review_reasons.map(dualReviewReasonLabel).map(escapeHtml).join('、')}</small>` : ''}
    ${[...new Set(rows.map(item => item.question_id))].map(questionId => `<section class="teacher-question-group"><h4>小题 ${escapeHtml(questionId.replace(/^Q/, ''))}</h4>${teacherQuestionCrop(submission, questionId)}${rows.filter(item => item.question_id === questionId).map(item => `<div class="batch-dual-point"><div class="teacher-point-title">${teacherPointTitle(result, item)}</div><span>A ${escapeHtml(item.score_a ?? '—')} / B ${escapeHtml(item.score_b ?? '—')}</span><span>A：${escapeHtml(item.evidence_a || '未提供引文')} ${escapeHtml(item.reason_a || '')}</span><span>B：${escapeHtml(item.evidence_b || '未提供引文')} ${escapeHtml(item.reason_b || '')}</span>${teacherPointControls(item, savedTeacherPointChoice(dual.teacher_final, item), 'batch-dual-point-choice')}</div>`).join('')}</section>`).join('')}
  </details>`;
}

function batchScoreSummary(result, modelNames) {
  const dual = result.dual_review;
  const maximum = escapeHtml(result.max_total ?? '—');
  if (dual?.status === 'degraded') return `双评缺侧 · 建议分 — / ${maximum}`;
  if (!dual) return `${escapeHtml(result.suggested_total ?? '—')} / ${maximum}`;
  return `${escapeHtml(modelNames.A)} ${escapeHtml(dual.results?.A?.suggested_total ?? '—')} · ${escapeHtml(modelNames.B)} ${escapeHtml(dual.results?.B?.suggested_total ?? '—')} / ${maximum}`;
}

function batchSubScoreSummary(result, modelNames) {
  const dual = result.dual_review;
  const questions = result.sub_questions || dual?.results?.A?.sub_questions || dual?.results?.B?.sub_questions || [];
  const rows = questions.map(q => {
    const id = q.sub_question_id ?? q.id;
    const find = side => id == null ? undefined : (dual?.results?.[side]?.sub_questions || []).find(p => String(p.sub_question_id ?? p.id) === String(id));
    const a = dual?.status === 'degraded' ? null : dual ? find('A')?.suggested_total : q.suggested_total;
    const b = dual?.status === 'degraded' ? null : dual ? find('B')?.suggested_total : null;
    const max = escapeHtml(q.max_total ?? '—');
    const diff = a == null || b == null ? '结果不完整' : Number(a) === Number(b) ? '一致' : `相差 ${Math.abs(Number(a)-Number(b))} 分`;
    return `<tr><td>${escapeHtml(q.label ?? id ?? '—')}</td><td>${escapeHtml(a ?? '—')} / ${max}</td><td>${escapeHtml(b ?? '—')} / ${max}</td><td>${escapeHtml(diff)}</td></tr>`;
  }).join('');
  return rows ? `<div style="overflow:auto"><table class="batch-subscore-grid"><thead><tr><th>小题</th><th>${escapeHtml(modelNames.A || 'AI-A')}</th><th>${escapeHtml(modelNames.B || 'AI-B')}</th><th>差异</th></tr></thead><tbody>${rows}</tbody></table></div>` : '—';
}

function renderBatchRow(submission, elapsedMs = null) {
  const result = submission.result || {};
  const modelNames = dualReviewModelNames(result.dual_review);
  const unmatchedQuestionMapping = Boolean(result.requires_question_mapping) && (
    Boolean(result.unmapped_answers?.length)
    || (result.sub_questions || []).some((item) => item.answer_status === 'question_unmatched')
  );
  const recoveryPending = submission.state === 'recovery_pending';
  const hardBlocked = result.simulation_model || ['vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error', 'queued'].includes(result.mode);
  const gradingBlocked = Boolean(result.blocked) || hardBlocked || unmatchedQuestionMapping;
  const canConfirm = !hardBlocked && !unmatchedQuestionMapping && (!result.blocked || submission.manual_reviewed);
  const canRetry = !submission.photos_deleted && (
    result.mode === 'processing-error'
    || (result.mode === 'vision-model-error' && result.diagnostics?.retryable === true)
    || (recoveryPending && result.mode === 'queued')
  );
  const subScores = batchSubScoreSummary(result, modelNames);
  const row = document.createElement('tr');
  row.dataset.batchSequence = submission.sequence || '';
  row.dataset.submissionId = submission.id;
  row.dataset.reviewPending = String(submission.state === 'completed' && !hardBlocked && (submission.final_total == null || result.teacher_reconfirmation_required === true));
  if (submission.batch_id) row.dataset.batchId = submission.batch_id;
  const mapping = state.examMappings.find((item) => item.id === submission.marking_code_id || item.submission_id === submission.id);
  const hasMarkingCode = Boolean(submission.marking_code_id || submission.marking_code);
  const mappingResolved = hasMarkingCode && mapping?.binding_status === 'matched' && mapping.student_name;
  const identity=submission.student_name||submission.id;
  const identityNote='';
  const identityControl='';
  const photoCacheAction = isLocalPage() && submission.photos?.length && submission.confirmed_at
    ? `<button class="button danger batch-delete-photos-button" type="button">删除照片缓存</button>`
    : '';
  row.innerHTML = `
    <td class="identity-cell"><strong>${submission.sequence ? `第 ${escapeHtml(submission.sequence)} 份 · ` : ''}${escapeHtml(identity)}</strong><small>${escapeHtml(identityNote)}</small>${identityControl}</td>
    <td><strong class="batch-score">${batchScoreSummary(result, modelNames)}</strong><small>${escapeHtml(result.summary || '')}</small>${result.dual_review ? `<small>${escapeHtml(({ HIGH: '高风险 · 建议优先复核', MEDIUM: '建议核对证据', LOW: '轻微差异', NO_CONFLICT: '未发现额外分歧' })[result.dual_review.comparison?.risk_level] || '需教师核对')}</small>` : ''}${result.diagnostics?.message ? `<small class="batch-diagnostic">诊断：${escapeHtml(result.diagnostics.message)}${result.diagnostics.http_status ? `（HTTP ${escapeHtml(result.diagnostics.http_status)}）` : ''}</small>` : ''}</td>
    <td>${subScores}</td>
    <td>${escapeHtml(result.simulation_model ? '模拟模型' : result.mode || '')}</td>
    <td class="batch-elapsed">${formatElapsed(elapsedMs)}</td>
    <td><div class="batch-confirm">${submission.final_total != null && !result.teacher_reconfirmation_required ? `<strong>${result.confirmation_method === 'teacher_batch_adoption' ? '批量采纳' : '最终'} ${escapeHtml(submission.final_total)} / ${escapeHtml(result.grading_scope?.scoped_max_score ?? result.max_total)} 分</strong>` : '<span>待教师终评</span>'}</div><button class="button primary batch-open-review" type="button">${submission.final_total != null && !result.teacher_reconfirmation_required ? '查看评审详情' : '打开评审'}</button><div class="batch-photo-cache">${photoCacheAction}</div></td>`;
  if (!canConfirm) {
    row.querySelector('.batch-score').textContent = result.simulation_model
      ? '仅模拟结果，不可确认'
      : result.mode === 'vision-model'
      ? 'AI建议 ' + result.suggested_total + ' / ' + result.max_total
      : '无法判分 / ' + result.max_total;
    row.querySelector('.batch-confirm').innerHTML = result.simulation_model
      ? '<span class="batch-blocked">模拟结果仅用于流程验收，不能确认为成绩</span>'
      : result.mode === 'vision-model'
      ? unmatchedQuestionMapping
        ? `<div class="batch-action-guide">${originalPhotoLink(submission, '打开原图')}<small>答案题号无法可靠对应，本份不能确认为成绩。跳过后保留本条模型记录，并继续下一份。</small></div><button class="button danger batch-skip-button" type="button">跳过本份并继续</button>`
        : `<div class="batch-action-guide">${originalPhotoLink(submission, '打开原图')}<small>核对证据后再解锁确认</small></div><button class="button secondary batch-review-button" type="button">核对后解锁确认</button>`
      : canRetry && isLocalPage()
        ? `<span class="batch-blocked">${recoveryPending ? '上次中断，需人工决定是否重试' : '模型调用失败，需人工重试'}</span><button class="button secondary batch-retry-button" type="button">人工确认后重试</button>`
      : canRetry && storedMobileToken() && state.activeBatch?.batchId === submission.batch_id
        ? '<span class="batch-blocked">手机端暂不重试模型调用；可教师确认跳过本份并继续</span>'
        : `<span class="batch-blocked">${result.mode === 'vision-model-error' ? `暂不建议重试：${escapeHtml(result.diagnostics?.message || '请先修正模型配置')}` : '安全门禁拦截<br>请重新拍摄或核对证据'}</span>`;
    const mobileOwnBatch = Boolean(storedMobileToken() && state.activeBatch?.batchId === submission.batch_id);
    const skipEligibleFailure = ['vision-model-error', 'image-quality-error', 'processing-error'].includes(result.mode)
      || (isLocalPage() && result.mode === 'rules-demo');
    if (submission.batch_id && (isLocalPage() || mobileOwnBatch) && skipEligibleFailure) {
      row.querySelector('.batch-confirm').insertAdjacentHTML('beforeend', '<button class="button danger batch-skip-button" type="button">确认跳过本份</button>');
    }
  }
  row.querySelector('.batch-open-review').addEventListener('click', () => openBatchSubmissionForReview(submission.id));
  const confirmButton = row.querySelector('.batch-confirm-button');
  if (confirmButton) confirmButton.addEventListener('click', () => confirmBatchRow(submission.id, row));
  const reviewButton = row.querySelector('.batch-review-button');
  if (reviewButton) reviewButton.addEventListener('click', () => openBatchSubmissionForReview(submission.id));
  const retryButton = row.querySelector('.batch-retry-button');
  if (retryButton) retryButton.addEventListener('click', () => retryBatchRow(submission.id, row));
  const deletePhotosButton = row.querySelector('.batch-delete-photos-button');
  if (deletePhotosButton) deletePhotosButton.addEventListener('click', () => deleteBatchSubmissionPhotos(deletePhotosButton, row, submission.id));
  const skipButton = row.querySelector('.batch-skip-button');
  if (skipButton) skipButton.addEventListener('click', () => skipBatchSequence({
    batchId: submission.batch_id,
    submissionId: submission.id,
    sequence: submission.sequence,
    row,
  }));
  loadTeacherCrops(row);
  insertBatchRowInSequence(row);
  syncBatchSubmissionStatus(submission);
  refreshBatchReviewNavigation();
}

function insertBatchRowInSequence(row) {
  const body = $('#batch-results-body');
  const sequence = Number(row.dataset.batchSequence || 0);
  if (!sequence) { body.appendChild(row); return; }
  const before = [...body.children].find((item) => {
    const itemSequence = Number(item.dataset.batchSequence || 0);
    return itemSequence && itemSequence > sequence;
  });
  body.insertBefore(row, before || null);
}

async function skipBatchSequence({ batchId, submissionId, sequence, row }) {
  const confirmed = window.confirm(`确认跳过第 ${sequence} 份并继续后续答卷？本份不计入成绩；如已有模型结果会保留，并记录跳过审计。继续后会处理下一份，可能调用模型并产生费用。`);
  if (!confirmed) return;
  const button = row?.querySelector('.batch-skip-button');
  if (button) button.disabled = true;
  try {
    await api('/api/batches/skip', {
      method: 'POST',
      body: JSON.stringify({ batch_id: batchId, submission_id: submissionId, sequence, confirm_skip: true, reason: '教师确认跳过当前批次序号并继续' }),
    });
    const activeItem = (state.activeBatch?.items || []).find((item) => item.submission_id === submissionId)
      || state.activeBatch?.items?.[Number(sequence) - 1];
    if (activeItem) {
      activeItem.model_status = 'skipped';
      activeItem.teacher_review_required = false;
      updateBatchCompletionSummary();
    }
    if (button) button.replaceWith(document.createTextNode('已确认跳过；后续序号继续处理'));
    const progress = $('#batch-progress');
    if (progress) progress.textContent = `第 ${sequence} 份已由教师确认跳过；后续答卷按原序号继续。`;
    await loadRecoverySubmissions();
    await resumeActiveBatchAfter(sequence);
  } catch (error) {
    alert(error.message);
    if (button) button.disabled = false;
  }
}

async function deleteBatchSubmissionPhotos(button, row, submissionId) {
  if (!button || !submissionId) return;
  button.disabled = true;
  try {
    const data = await deleteSubmissionPhotosRequest(submissionId);
    if (!data) return;
    button.replaceWith(document.createTextNode('照片缓存已清理'));
    row.classList.add('photos-cleaned');
    row.querySelectorAll('a[href*="/api/submission-photo"]').forEach(link => link.remove());
    alert(data.message);
  } catch (error) {
    alert(error.message);
  } finally {
    if (button.isConnected) button.disabled = false;
  }
}

async function retryBatchRow(submissionId, row) {
  const button = row.querySelector('.batch-retry-button');
  button.disabled = true;
  try {
    await api('/api/retry', { method: 'POST', body: JSON.stringify({ submission_id: submissionId }) });
    const completed = await waitForSubmission(submissionId);
    row.remove();
    renderBatchRow(completed);
    if (completed.batch_id && completed.sequence && completed.state === 'completed'
      && !completed.result?.simulation_model
      && !['vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error'].includes(completed.result?.mode)) {
      await resumeActiveBatchAfter(completed.sequence);
    }
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

async function reviewBatchRow(submissionId, row) {
  const button = row.querySelector('.batch-review-button');
  button.disabled = true;
  try {
    await api('/api/review', {
      method: 'POST',
      body: JSON.stringify({ submission_id: submissionId, confirm_original: true, review_note: '批量阅卷中教师已核对原图证据' }),
    });
    const data = await api(`/api/submission?submission_id=${encodeURIComponent(submissionId)}`);
    row.remove();
    renderBatchRow(data.submission);
  } catch (error) {
    alert(error.message);
    button.disabled = false;
  }
}

function renderBatchError(studentLabel, error, elapsedMs = null, submissionId = '', canRetry = true, batchId = '', sequence = 0, uploadFailure = false) {
  const row = document.createElement('tr');
  row.className = 'batch-error-row';
  row.dataset.batchSequence = sequence || '';
  if (batchId) row.dataset.batchId = batchId;
  const mobileOwnBatch = Boolean(storedMobileToken() && state.activeBatch?.batchId === batchId);
  const retryButton = uploadFailure
    ? '<small>上传失败可原位重传，或由教师确认跳过</small>'
    : submissionId && canRetry && isLocalPage()
    ? `<button class="button secondary batch-detached-retry-button" type="button">人工确认后重试</button>`
    : submissionId && canRetry && mobileOwnBatch
      ? '<small>手机端暂不重试模型调用；可教师确认跳过本份并继续</small>'
    : submissionId
      ? '<small>本份失败类型不建议重复调用，请先修正配置或重新提交照片</small>'
      : '<small>可在下方“待恢复任务”中查看是否需要重试</small>';
  const skipButton = batchId && (isLocalPage() || mobileOwnBatch)
    ? '<button class="button danger batch-skip-button" type="button">教师确认跳过本份并继续</button>'
    : '';
  row.innerHTML = `<td><strong>${sequence ? `第 ${escapeHtml(sequence)} 份 · ` : ''}${escapeHtml(studentLabel)}</strong></td><td colspan="4">处理失败：${escapeHtml(error.message || error)}<div class="batch-error-actions">${retryButton}${skipButton}</div></td><td class="batch-elapsed">${formatElapsed(elapsedMs)}</td>`;
  const button = row.querySelector('.batch-detached-retry-button');
  if (button) button.addEventListener('click', () => retryDetachedBatchRow(submissionId, row));
  const skip = row.querySelector('.batch-skip-button');
  if (skip) skip.addEventListener('click', () => skipBatchSequence({ batchId, submissionId, sequence, row }));
  insertBatchRowInSequence(row);
  return row;
}

function renderPendingBatchUpload(sequence, batch) {
  const group = batch.groups[sequence - 1];
  const item = batch.items[sequence - 1];
  if (!group || !item || document.querySelector(`#batch-results-body tr[data-batch-sequence="${sequence}"]`)) return;
  const row = document.createElement('tr');
  row.className = 'batch-error-row batch-upload-pending-row';
  row.dataset.batchSequence = sequence;
  row.dataset.batchId = batch.batchId;
  const ready = sequence === batch.nextDisplaySequence;
  row.innerHTML = `<td><strong>第 ${sequence} 份 · ${escapeHtml(group.studentLabel)}</strong></td><td colspan="4">尚未提交；请原位重传，或由教师确认跳过。<div class="batch-error-actions"><button class="button secondary batch-upload-retry-button" type="button" ${ready ? '' : 'disabled'}>原位上传此份</button><button class="button danger batch-skip-button" type="button" ${ready ? '' : 'disabled'}>教师确认跳过</button></div></td><td class="batch-elapsed">—</td>`;
  row.querySelector('.batch-upload-retry-button').addEventListener('click', () => retryBatchUpload(sequence, row));
  row.querySelector('.batch-skip-button').addEventListener('click', () => skipBatchSequence({ batchId: batch.batchId, submissionId: item.submission_id, sequence, row }));
  insertBatchRowInSequence(row);
}

function syncPendingBatchUploadControls(batch = state.activeBatch) {
  if (!batch) return;
  document.querySelectorAll('#batch-results-body .batch-upload-pending-row').forEach((row) => {
    const ready = Number(row.dataset.batchSequence) === batch.nextDisplaySequence;
    row.querySelectorAll('.batch-upload-retry-button, .batch-skip-button').forEach((button) => { button.disabled = !ready; });
  });
}

async function retryBatchUpload(sequence, row) {
  const batch = state.activeBatch;
  if (!batch || !batch.batchId || sequence !== batch.nextDisplaySequence) {
    alert('当前浏览器没有保留这份答卷照片，请重新提交此批次或由教师确认跳过。');
    return;
  }
  const button = row?.querySelector('.batch-upload-retry-button');
  if (button) { button.disabled = true; button.textContent = '正在原位重传…'; }
  try {
    const item = batch.items[sequence - 1];
    const group = batch.groups[sequence - 1];
    const previousApproval = batch.privacyApprovals?.[sequence - 1];
    const privacyApproval = (await reviewAiPrivacyBatch(batch.assignmentId, [{
      ...group,
      studentLabel: group.studentLabel,
      files: [...group.files],
    }], batch.className, batch.dualReviewEnabled === true, {
      gradingInputMode: batch.gradingInputMode,
      examId: Object.prototype.hasOwnProperty.call(previousApproval || {}, 'examId')
        ? previousApproval.examId : batch.examId,
    }))[0];
    const body = await buildSubmissionBody({
      assignment_id: batch.assignmentId,
      class_name: batch.className,
      student_label: group.studentLabel,
      answer_text: '',
      batch_id: batch.batchId,
      submission_id: item.submission_id,
      sequence,
      idempotency_key: `${batch.batchId}:${sequence}`,
      dual_review_enabled: batch.dualReviewEnabled === true,
    }, group.files, privacyApproval);
    await api('/api/submissions', { method: 'POST', body });
    batch.uploadResults[sequence - 1] = { ok: true, item };
    row?.remove();
    await displayBatchResultsFrom(sequence);
    syncPendingBatchUploadControls(batch);
  } catch (error) {
    alert(error.message || '原位重传失败');
    if (button) { button.disabled = false; button.textContent = '原位重传照片'; }
  }
}

function stopBatchUploads(batchId) {
  const batch = state.activeBatch;
  if (!batch || batch.batchId !== batchId) return;
  batch.uploadStopRequested = true;
  const button = $('#batch-stop-remaining');
  if (button) {
    button.disabled = true;
    button.textContent = '正在等待已开始的上传完成…';
  }
  const progress = $('#batch-progress');
  if (progress) progress.textContent = '已请求停止后续上传；正在等待已开始的上传完成。';
}

async function runBatchUploadWorkers(total, concurrency, shouldStop, uploadOne, startIndex = 0) {
  let nextIndex = startIndex;
  const worker = async () => {
    while (nextIndex < total && !shouldStop()) {
      const index = nextIndex++;
      await uploadOne(index);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, total) }, () => worker()));
  return nextIndex;
}

async function resumeActiveBatchAfter(sequence) {
  const batch = state.activeBatch;
  if (batch && batch.nextDisplaySequence === sequence) {
    batch.nextDisplaySequence = sequence + 1;
    syncPendingBatchUploadControls(batch);
    await displayBatchResultsFrom(sequence + 1);
    syncPendingBatchUploadControls(batch);
  }
}

async function retryDetachedBatchRow(submissionId, row) {
  const button = row.querySelector('.batch-detached-retry-button');
  if (!button) return;
  button.disabled = true;
  button.textContent = '正在重试…';
  try {
    await api('/api/retry', { method: 'POST', body: JSON.stringify({ submission_id: submissionId }) });
    const completed = await waitForSubmission(submissionId);
    row.remove();
    renderBatchRow(completed);
    if (completed.batch_id && completed.sequence && completed.state === 'completed'
      && !completed.result?.simulation_model
      && !['vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error'].includes(completed.result?.mode)) {
      await resumeActiveBatchAfter(completed.sequence);
    }
    await loadRecoverySubmissions();
  } catch (error) {
    alert(error.message);
    button.disabled = false;
    button.textContent = '人工确认后重试';
  }
}

async function confirmBatchRow(submissionId, row) {
  const button = row.querySelector('.batch-confirm-button');
  button.disabled = true;
  try {
    await api('/api/confirm', { method: 'POST', body: JSON.stringify({
      submission_id: submissionId,
      teacher_note: row.querySelector('.batch-note').value,
      teacher_point_decisions: [...row.querySelectorAll('.batch-dual-point-choice')]
        .filter((select) => select.value)
        .map((select) => ({ question_id: select.dataset.questionId, rubric_point_id: select.dataset.rubricPointId, choice: select.value })),
    }) });
    button.textContent = '已确认';
    row.querySelector('.batch-final-score').disabled = true;
    row.querySelector('.batch-note').disabled = true;
    row.querySelectorAll('.batch-dual-point-choice').forEach((select) => { select.disabled = true; select.closest('.teacher-point-controls')?.querySelectorAll('button').forEach(button => { button.disabled = true; }); });
    const activeItem = (state.activeBatch?.items || []).find((item) => item.submission_id === submissionId);
    if (activeItem) {
      activeItem.teacher_confirmed = true;
      activeItem.teacher_review_required = true;
      updateBatchCompletionSummary();
    }
    await loadAssignments();
  } catch (error) { alert(error.message); button.disabled = false; }
}

async function processBatchSubmissions(button) {
  syncBatchDraftLabels();
  const groups = state.batchDraft ? state.batchDraft.map((group) => ({ ...group, files: [...group.files] })) : [];
  const assignmentId = $('#batch-assignment-select').value;
  if (!assignmentId) return alert('请先保存并选择一道题');
  if (!groups.length) return alert('请先选择一批学生作业照片');
  if (groups.length > 100) return alert('单批最多处理100名学生，请分批提交');
  try {
    await loadRosterStudents($('#batch-class-select').value);
  } catch (error) {
    return alert(error.message);
  }
  button.disabled = true;
  const inputModeSelect = $('#batch-grading-input-mode');
  if (inputModeSelect) inputModeSelect.disabled = true;
  const batchStartedAt = performance.now();
  const className = $('#batch-class-select').value;
  const gradingInputMode = inputModeSelect?.value || 'original_photo';
  const examId = $('#batch-exam-select').value || '';
  try {
    const dualReviewEnabled = $('#batch-dual-review-enabled')?.checked === true;
    const privacyApprovals = await reviewAiPrivacyBatch(assignmentId, groups, className, dualReviewEnabled, {
      gradingInputMode,
      examId,
    });
    renderBatchShell(groups.length);
    const created = await api('/api/batches', {
      method: 'POST',
      body: JSON.stringify({
        assignment_id: assignmentId,
        class_name: className,
        grading_input_mode: gradingInputMode,
        exam_id: Object.prototype.hasOwnProperty.call(privacyApprovals[0] || {}, 'examId')
          ? privacyApprovals[0].examId : (className ? examId : ''),
        batch_request_id: createIdempotencyKey(),
        items: groups.map((group, index) => ({
          page_count: privacyApprovals[index]?.acceptedPhotos?.length || group.files.length,
        })),
      }),
    });
    const batchId = created.batch_id;
    const items = created.items || [];
    items.forEach((item) => { item.model_status = 'pending'; item.teacher_review_required = false; item.teacher_confirmed = false; });
    state.activeBatch = { batchId, examId: created.exam_id || '', gradingInputMode, items, groups, assignmentId, className, privacyApprovals, dualReviewEnabled, nextDisplaySequence: 1, displaying: false, uploadStopRequested: false, uploadStartedCount: 0 };
    const stopButton = $('#batch-stop-remaining');
    if (stopButton) {
      stopButton.hidden = false;
      stopButton.addEventListener('click', () => stopBatchUploads(batchId), { once: true });
    }
    updateBatchCompletionSummary();
    const uploadResults = new Array(groups.length);
    let uploadedCount = 0;
    const uploadOne = async (index) => {
      const group = groups[index];
      const item = items[index];
      const sequence = index + 1;
      try {
        const body = await buildSubmissionBody({
          assignment_id: assignmentId,
          class_name: className,
          student_label: group.studentLabel,
          answer_text: '',
          batch_id: batchId,
          submission_id: item.submission_id,
          sequence,
          idempotency_key: `${batchId}:${sequence}`,
          dual_review_enabled: state.activeBatch.dualReviewEnabled,
        }, group.files, privacyApprovals[index] || null);
        await api('/api/submissions', { method: 'POST', body });
        uploadResults[index] = { ok: true, item };
      } catch (error) {
        try {
          await api('/api/batches/upload-failed', {
            method: 'POST',
            body: JSON.stringify({ batch_id: batchId, sequence, error: error.message || '照片上传失败' }),
          });
        } catch (_) { /* The batch remains safely blocked by its reserved sequence. */ }
        uploadResults[index] = { ok: false, item, error };
        item.model_status = 'failed';
        updateBatchCompletionSummary();
      } finally {
        uploadedCount += 1;
        const progress = $('#batch-progress');
        if (progress) progress.textContent = `照片上传 ${uploadedCount} / ${groups.length} 份；模型按教师确认的序号串行批改。`;
      }
    };
    const nextIndex = await runBatchUploadWorkers(groups.length, BATCH_CONCURRENCY,
      () => state.activeBatch?.batchId !== batchId || state.activeBatch.uploadStopRequested,
      async (index) => {
        state.activeBatch.uploadStartedCount = Math.max(state.activeBatch.uploadStartedCount, index + 1);
        await uploadOne(index);
      });
    const stopped = state.activeBatch?.batchId === batchId && state.activeBatch.uploadStopRequested;
    if (stopButton) stopButton.hidden = true;
    state.activeBatch.uploadResults = uploadResults;
    if (stopped && state.activeBatch?.batchId === batchId) {
      for (let sequence = nextIndex + 1; sequence <= groups.length; sequence += 1) renderPendingBatchUpload(sequence, state.activeBatch);
      syncPendingBatchUploadControls(state.activeBatch);
      const progress = $('#batch-progress');
      if (progress) progress.textContent = `已停止后续上传；已启动的 ${nextIndex} 份均已结束。其余 ${groups.length - nextIndex} 份保留原序号，可原位上传或由教师逐份确认跳过。`;
    }
    await displayBatchResultsFrom(1, batchStartedAt);
    syncPendingBatchUploadControls(state.activeBatch);
    const hasFailure = uploadResults.some((item) => !item?.ok);
    if (!hasFailure && state.activeBatch?.batchId === batchId && state.activeBatch.nextDisplaySequence > groups.length) {
      const batchItems = state.activeBatch.items || [];
      const pendingFinals = batchItems.filter((item) => item.teacher_review_required && !item.teacher_confirmed).length;
      const partial = batchItems.filter((item) => item.model_status === 'partial').length;
      const failed = batchItems.filter((item) => item.model_status === 'failed').length;
      const skipped = batchItems.filter((item) => item.model_status === 'skipped').length;
      button.textContent = pendingFinals
        ? `模型处理已结束；${pendingFinals} 份待教师终评`
        : (partial || failed || skipped)
          ? `模型处理队列已结束；双评不完整 ${partial}，失败 ${failed}，跳过 ${skipped}`
          : '批次模型处理与教师终评均完成，可继续提交';
      clearBatchPreviewUrls();
      state.batchDraft = null;
      clearFileInputs(BATCH_PHOTO_INPUTS);
      updateBatchPhotoCount();
    } else {
      button.textContent = '批次已暂停；可原位重试或确认跳过';
    }
    const timing = $('#batch-timing');
    if (timing) timing.textContent = `整批耗时：${formatElapsed(performance.now() - batchStartedAt)} · 上传可并发，模型调用按序进行`;
    clearBatchPreviewUrls();
    await loadAssignments();
    await loadRecoverySubmissions();
  } catch (error) {
    alert(error.message || '批次提交失败');
  } finally {
    button.disabled = false;
    if (inputModeSelect) inputModeSelect.disabled = false;
  }
}

async function displayBatchResultsFrom(startSequence, batchStartedAt = performance.now()) {
  const batch = state.activeBatch;
  if (!batch || batch.displaying) return;
  batch.displaying = true;
  try {
    for (let sequence = startSequence; sequence <= batch.items.length; sequence += 1) {
      const item = batch.items[sequence - 1];
      const group = batch.groups[sequence - 1];
      const upload = batch.uploadResults?.[sequence - 1];
      if (!upload) {
        batch.nextDisplaySequence = sequence;
        break;
      }
      if (upload && !upload.ok) {
        item.model_status = 'failed';
        updateBatchCompletionSummary(batch);
        const row = renderBatchError(group.studentLabel, upload.error, null, item.submission_id, false, batch.batchId, sequence, true);
        const action = row.querySelector('.batch-error-actions');
        if (action) action.insertAdjacentHTML('afterbegin', '<button class="button secondary batch-upload-retry-button" type="button">原位重传照片</button>');
        row.querySelector('.batch-upload-retry-button')?.addEventListener('click', () => retryBatchUpload(sequence, row));
        batch.nextDisplaySequence = sequence;
        $('#batch-progress').textContent = `第 ${sequence} 份上传失败，批次已暂停；可原位重传或经教师确认后跳过。`;
        break;
      }
      const startedAt = performance.now();
      try {
        const completed = await waitForSubmission(item.submission_id, (submission) => {
          const progress = $('#batch-progress');
          if (progress) progress.textContent = `按序处理中：第 ${sequence} / ${batch.items.length} 份（${group.studentLabel}）— ${submission.state}`;
        });
        if (completed.state === 'failed') {
          item.model_status = 'failed';
          updateBatchCompletionSummary(batch);
          renderBatchError(group.studentLabel, new Error(completed.result?.summary || '本份处理失败'), performance.now() - startedAt, item.submission_id, false, batch.batchId, sequence);
          batch.nextDisplaySequence = sequence;
          break;
        }
        completed.batch_id = batch.batchId;
        completed.sequence = sequence;
        const gradingFailed = completed.result?.simulation_model
          || ['vision-model-error', 'image-quality-error', 'rules-demo', 'processing-error'].includes(completed.result?.mode);
        const questionMappingBlocked = completed.result?.requires_question_mapping === true;
        renderBatchRow(completed, performance.now() - startedAt);
        if (gradingFailed || questionMappingBlocked) {
          batch.nextDisplaySequence = sequence;
          $('#batch-progress').textContent = completed.result?.simulation_model
            ? `第 ${sequence} 份返回模拟模型结果，批次已暂停；该结果不能确认为正式成绩。`
            : questionMappingBlocked && !gradingFailed
              ? `第 ${sequence} 份的答案题号无法可靠对应，批次已暂停；可核对原图，或确认跳过后继续。`
              : `第 ${sequence} 份批改失败，队列已在原位暂停；请选择重试或教师确认跳过。`;
          break;
        }
        batch.nextDisplaySequence = sequence + 1;
      } catch (error) {
        item.model_status = 'failed';
        updateBatchCompletionSummary(batch);
        renderBatchError(group.studentLabel, error, performance.now() - startedAt, item.submission_id, false, batch.batchId, sequence);
        batch.nextDisplaySequence = sequence;
        break;
      }
    }
    if (batch.nextDisplaySequence > batch.items.length) {
      $('#batch-progress').textContent = '模型处理队列已结束；下方分别显示模型处理与教师终评状态。';
      updateBatchCompletionSummary(batch);
    }
  } finally {
    batch.displaying = false;
  }
}

async function createBatchSubmissions(event) {
  event.preventDefault();
  const button = event.submitter;
  if (state.batchDraft) {
    await processBatchSubmissions(button);
    return;
  }
  const assignmentId = $('#batch-assignment-select').value;
  const files = filesFromInputs(BATCH_PHOTO_INPUTS);
  if (!assignmentId) return alert('请先保存并选择一道题');
  if (!files.length) return alert('请选择一批学生作业照片');
  const groups = groupBatchFiles(files,$('#batch-group-mode').value);
  if (groups.length > 100) return alert('单批最多处理100名学生，请分批提交');
  renderBatchPreview(groups);
  button.textContent = '确认分组并开始批改';
}

$('#add-major-question').addEventListener('click', () => addMajorQuestion());
$('#generate-all-rubric').addEventListener('click', generateAllRubric);
$('#restart-service').addEventListener('click', restartService);
$('#shutdown-service').addEventListener('click', shutdownService);
$('#generate-mobile-token').addEventListener('click', createMobileToken);
$('#revoke-mobile-token').addEventListener('click', revokeMobileToken);
$('#copy-mobile-url').addEventListener('click', copyMobileAccessUrl);
$('#refresh-mobile-network').addEventListener('click', refreshMobileNetwork);
$('#create-backup').addEventListener('click', createBackup);
$('#refresh-backups').addEventListener('click', () => loadStorage().catch(error => alert(error.message)));
$('#restore-backup').addEventListener('click', restoreBackup);
$('#preview-retention').addEventListener('click', previewRetention);
$('#settings-form').addEventListener('submit', saveSettings);
$('#dual-model-a-form').addEventListener('submit', saveDualReviewConfiguration);
$('#dual-model-b-form').addEventListener('submit', saveDualReviewConfiguration);
$('#dual-model-a-preset').addEventListener('change', () => previewDualReviewModel('A'));
$('#dual-model-b-preset').addEventListener('change', () => previewDualReviewModel('B'));
$('#model-preset').addEventListener('change', (event) => applyModelPreset(event.target.value));
$('#save-custom-preset').addEventListener('click', saveCustomModelPreset);
$('#delete-custom-preset').addEventListener('click', deleteCustomModelPreset);
$('#roster-form').addEventListener('submit', importRoster);
initializeRosterManagement();
$('#assignment-form').addEventListener('submit', createAssignment);
$('#submission-form').addEventListener('submit', createSubmission);
$('#batch-form').addEventListener('submit', createBatchSubmissions);
$('#open-camera-session').addEventListener('click', () => openCameraSession().catch((error) => alert(error.message)));
$('#camera-start').addEventListener('click', () => startCameraStream().catch((error) => alert(error.message)));
$('#camera-capture').addEventListener('click', captureCameraPhoto);
$('#camera-finish').addEventListener('click', finishCameraSession);
$('#camera-cancel').addEventListener('click', cancelCameraSession);
$('#camera-fallback-input').addEventListener('change', addCameraFallbackPhotos);
$('#camera-album-input').addEventListener('change', addCameraFallbackPhotos);
$('#camera-thumbs').addEventListener('click', (event) => {
  const button = event.target.closest('.remove-camera-photo');
  if (!button) return;
  const index = Number(button.dataset.cameraIndex);
  if (!Number.isInteger(index) || !state.cameraSession.files[index]) return;
  state.cameraSession.files.splice(index, 1);
  renderCameraQueue();
  setCameraSessionNote('已删除这张照片，可以继续拍摄或用系统相机补拍。');
});
$('#continuous-mode').addEventListener('change', () => selectGradingMode(isContinuousMode() ? 'continuous' : 'single'));
$('#major-questions-list').addEventListener('input', updateAssignmentTotal);
$('#major-questions-list').addEventListener('click', (event) => {
  const button = event.target.closest('.generate-rubric');
  if (button) generateRubricForCard(button.closest('.subquestion-card'));
});
$('#refresh').addEventListener('click', () => loadAssignments().catch(error => alert(error.message)));
$('#refresh-recovery').addEventListener('click', () => loadRecoverySubmissions().catch(error => alert(error.message)));
$('#refresh-evaluation').addEventListener('click', () => loadEvaluationSummary().catch(error => alert(error.message)));
SINGLE_PHOTO_INPUTS.forEach((selector) => {
  $(selector).addEventListener('change', updateSinglePhotoCount);
});
BATCH_PHOTO_INPUTS.forEach((selector) => {
  $(selector).addEventListener('change', () => {
    if (state.batchDraft) {
      resetBatchDraft(false);
      $('#batch-result').className = 'result-area empty';
      $('#batch-result').textContent = '批量结果会显示在这里';
    }
    updateBatchPhotoCount();
  });
});
$('#batch-grading-input-mode')?.addEventListener('change', () => {
  const layoutField=$('#batch-page-layout-field');
  if(layoutField)layoutField.hidden=$('#batch-grading-input-mode').value!=='auto_crop';
  if (!state.batchDraft) return;
  resetBatchDraft(false);
  $('#batch-result').className = 'result-area empty';
  $('#batch-result').textContent = '图片处理方式已更改，请重新确认分组预览。';
});

function bindTextFile(fileSelector, targetSelector) {
  $(fileSelector).addEventListener('change', (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { $(targetSelector).value = reader.result; };
    reader.readAsText(file, 'UTF-8');
  });
}

bindTextFile('#question-file', '#question');

async function importQuestionDocx() {
  const input = $('#question-docx-file');
  const file = input?.files?.[0];
  const status = $('#question-docx-status');
  if (!file) return alert('请先选择一份 .docx 试卷');
  if (!file.name.toLowerCase().endsWith('.docx')) return alert('目前只支持 .docx；旧版 .doc 暂不支持');
  if (file.size > 10 * 1024 * 1024) return alert('Word 文件不能超过10MiB');
  const button = $('#import-question-docx');
  button.disabled = true;
  status.textContent = `正在${isCloudRuntime() ? '服务器' : '本机'}解析 Word 结构，不会调用模型……`;
  try {
    const dataUrl = await readFileDataUrl(file);
    const data = await api('/api/import-docx', {
      method: 'POST',
      body: JSON.stringify({ filename: file.name, data_base64: String(dataUrl).split(',')[1] || '' }),
    });
    const imported = data.imported || {};
    const questions = Array.isArray(imported.material_questions) ? imported.material_questions : [];
    if (!questions.length) throw new Error('没有识别出编号材料题小题；原有录入内容未清除。');
    const subQuestionCount = questions.reduce((count, question) => count + (question.sub_questions || []).length, 0);
    if (!window.confirm(`识别到 ${questions.length} 道材料题、${subQuestionCount} 个小题。\n\n继续会用这些草稿替换当前尚未保存的大题录入卡片；不会覆盖已经保存的作业或答卷。`)) {
      status.textContent = '已取消导入，当前录入内容保持不变。';
      return;
    }
    const list = $('#major-questions-list');
    list.replaceChildren();
    for (const question of questions) {
      addMajorQuestion({
        label: question.label,
        prompt: question.prompt,
        sub_questions: (question.sub_questions || []).map((sub) => ({
          label: sub.label,
          question: sub.question,
          standard_answer: sub.standard_answer,
          max_score: sub.max_score ?? '',
          scoring_mode: 'point_bank',
          point_value: 2,
          criteriaText: '',
        })),
      });
    }
    const assignmentName = $('#assignment-name');
    if (imported.title && (!assignmentName.value.trim() || assignmentName.value.trim() === '历史材料题练习')) {
      assignmentName.value = imported.title;
    }
    const objectiveCount = Object.keys(imported.objective_answer_key || {}).length;
    const warnings = Array.isArray(imported.warnings) ? imported.warnings : [];
    status.textContent = `已生成可编辑草稿：${questions.length} 道材料题、${subQuestionCount} 个小题；识别到 ${objectiveCount} 个客观题答案。评分点留空，请核对题目、参考答案和分值，再手动补充或选择“AI生成全部评分点”。${warnings.length ? `\n${warnings.join('\n')}` : ''}`;
    list.scrollIntoView({ behavior: 'smooth', block: 'start' });
    updateAssignmentTotal();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$('#import-question-docx').addEventListener('click', () => importQuestionDocx());

const importWarningLabels = {
  docx_parse_incomplete: 'Word 中有部分题目结构未能完整识别，请逐题核对。',
  pdf_text_unavailable: 'PDF 没有可用文本层，不能从 PDF 生成语义草稿。',
  source_pair_mismatch: 'Word 与 PDF 题号或内容明显不一致。',
  source_pair_uncertain: 'Word 与 PDF 是否属于同一份试卷尚不能自动确认。',
  question_count_mismatch: 'Word 与 PDF 识别到的题目数量不同。',
  question_id_mismatch: 'Word 与 PDF 的题号集合不同。',
  answer_key_conflict: 'Word 与 PDF 的答案键存在冲突，请逐题选择最终答案。',
  answer_missing: '有客观题尚未识别到正确答案。',
  max_score_unknown: '有主观小题尚未确认满分。',
  rubric_needs_teacher_review: '评分点为候选草稿，需要教师逐项核对。',
  layout_source_missing: '尚未上传打印 PDF；后续不能使用 PDF 页面 reference。',
  layout_reference_unavailable: 'PDF 已加载，但当前电脑没有生成页面 reference。',
  pdf_semantic_fallback: '当前仅使用 PDF 文字解析，题目与评分草稿需要重点核对。',
};

function getImportWarningMessage(warning) {
  const text = String(warning || '');
  const code = text.split(/[：:]/, 1)[0];
  return importWarningLabels[code] || text;
}

const examSourceConflictSectionLabels = Object.freeze({
  objective_questions: '客观题',
  subjective_subquestions: '主观题',
});

const examSourceConflictTypeLabels = Object.freeze({
  question_text_mismatch: '题干文本不一致',
  question_id_mismatch: '题号不一致',
  answer_key_conflict: '答案冲突',
});

function examSourceConflictListText(values) {
  return (Array.isArray(values) ? values : [])
    .map(value => String(value ?? '').trim())
    .filter(Boolean)
    .join('、');
}

function examSourceConflictText(conflict) {
  const item = conflict && typeof conflict === 'object' ? conflict : {};
  const type = String(item.type || '');
  if (type === 'question_text_mismatch') {
    const compared = Number(item.compared);
    const matched = Number(item.matched);
    if (item.compared != null && item.matched != null && Number.isFinite(compared) && Number.isFinite(matched)) {
      return `题干文本抽样：已比较 ${compared} 题，一致 ${matched} 题`;
    }
    return examSourceConflictTypeLabels[type];
  }
  if (type === 'question_id_mismatch') {
    const section = examSourceConflictSectionLabels[item.section] || String(item.section || '题目');
    const evidence = [];
    const docxOnly = examSourceConflictListText(item.docx_only);
    const pdfOnly = examSourceConflictListText(item.pdf_only);
    if (docxOnly) evidence.push(`Word 独有：${docxOnly}`);
    if (pdfOnly) evidence.push(`PDF 独有：${pdfOnly}`);
    return `${section}题号不一致${evidence.length ? `：${evidence.join('；')}` : ''}`;
  }
  if (type === 'answer_key_conflict') {
    const questionId = String(item.question_id ?? '').trim();
    const docxAnswer = String(item.docx_answer ?? '').trim() || '无记录';
    const pdfAnswer = String(item.pdf_answer ?? '').trim() || '无记录';
    return `${questionId ? `第 ${questionId} 题` : ''}答案冲突：Word ${docxAnswer}；PDF ${pdfAnswer}`;
  }
  return String(item.label || item.type || item.section || '来源冲突').trim() || '来源冲突';
}

function examSourceStateMarkup(draft) {
  const source = draft.source || {};
  const content = source.content_source || {};
  const layout = source.layout_source || {};
  const status = source.source_pair_status || 'not_applicable';
  const contentLabel = content.type === 'pdf_semantic_fallback' ? 'PDF fallback' : content.type === 'docx' ? 'DOCX' : '未识别';
  const layoutLabel = layout.type === 'pdf' || layout.source_pdf_file_name ? `PDF · ${Number(layout.page_count || 0)} 页` : '未上传';
  const matchLabels = {
    matched: ['已确认一致', 'is-good'],
    needs_teacher_confirmation: ['需要教师确认', 'is-warning'],
    mismatch: ['发现明显不一致', 'is-bad'],
    not_applicable: ['单一来源', ''],
  };
  const [matchLabel, matchClass] = matchLabels[status] || ['需要教师确认', 'is-warning'];
  let fallbackNote = '';
  if (draft.pdf_semantic_fallback) fallbackNote = '<div class="exam-fallback-note">当前仅使用 PDF 导入。PDF 文本解析容易受排版影响，请重点核对题目、答案和评分草稿。</div>';
  else if (!layout.source_pdf_file_name && content.type === 'docx') fallbackNote = '<div class="exam-fallback-note">尚未上传最终打印 PDF。当前仍可创建考试，但没有 PDF 页面 reference，后续无法使用基于 PDF 的自动页面定位。</div>';
  const pageRoles = draft.layout_reference?.page_roles || [];
  const examPages = pageRoles.filter(item => item.page_role === 'exam').map(item => item.page_number);
  const answerSheetPages = pageRoles.filter(item => item.page_role === 'answer_sheet').map(item => item.page_number);
  const answerPages = pageRoles.filter(item => item.page_role === 'answer').map(item => item.page_number);
  const referenceStatus = draft.layout_reference?.render_status === 'generated'
    ? `已生成 ${draft.layout_reference.pages?.length || 0} 页`
    : layout.source_pdf_file_name ? '尚未生成' : '无 PDF';
  const sourceConflicts = Array.isArray(source.source_conflicts) ? source.source_conflicts : [];
  const details = sourceConflicts.map(item => `<li>${escapeHtml(examSourceConflictText(item))}</li>`).join('');
  return `<div class="exam-source-state-row"><span><strong>内容源：</strong>${escapeHtml(contentLabel)}</span><span><strong>版式源：</strong>${escapeHtml(layoutLabel)}</span><span class="exam-source-match ${matchClass}"><strong>来源匹配：</strong>${escapeHtml(matchLabel)}</span></div>
    ${layout.source_pdf_file_name ? `<div class="exam-layout-summary">试卷页：${examPages.length ? escapeHtml(examPages.join('、')) : '未能识别'}　·　答题卡页：${answerSheetPages.length ? escapeHtml(answerSheetPages.join('、')) : '未能识别'}　·　答案页：${answerPages.length ? escapeHtml(answerPages.join('、')) : '未能识别'}　·　Reference：${escapeHtml(referenceStatus)}</div>` : ''}
    ${details ? `<ul class="exam-source-conflicts">${details}</ul>` : ''}${fallbackNote}`;
}

function renderExamObjectiveQuestions(draft) {
  const list = $('#exam-objective-list');
  const questions = draft.objective_questions || [];
  if (!questions.length) {
    list.innerHTML = '<p class="muted">没有识别到客观题。</p>';
    return;
  }
  const docxKey = draft.answer_key_crosscheck?.docx_answer_key || {};
  const pdfKey = draft.answer_key_crosscheck?.pdf_answer_key || {};
  const draftKey = draft.objective_answer_key || {};
  list.innerHTML = questions.map((question) => {
    const id = String(question.question_id);
    const conflicting = docxKey[id] && pdfKey[id] && docxKey[id] !== pdfKey[id];
    const currentAnswer = draftKey[id] || '';
    const options = question.options || {};
    return `<article class="exam-objective-card" data-exam-objective-id="${escapeHtml(id)}"><div class="exam-objective-top"><details><summary>第 ${escapeHtml(id)} 题 · 查看题干与选项</summary><p>${escapeHtml(question.stem || '题干未完整识别')}</p><ul>${Object.entries(options).map(([key, value]) => `<li>${escapeHtml(key)}．${escapeHtml(value)}</li>`).join('')}</ul></details><label class="exam-answer-field">正确答案<select data-exam-answer data-question-id="${escapeHtml(id)}"><option value="">请选择</option>${['A', 'B', 'C', 'D'].map(answer => `<option value="${answer}" ${currentAnswer === answer ? 'selected' : ''}>${answer}</option>`).join('')}</select></label><label>分值<input type="number" min="0" max="100" step="any" data-exam-objective-points data-question-id="${escapeHtml(id)}" value="${escapeHtml(draft.objective_points?.[id]??'')}"></label></div>${conflicting ? `<p class="exam-answer-conflict">答案冲突　Word：${escapeHtml(docxKey[id])}　·　PDF：${escapeHtml(pdfKey[id])}　·　请选择最终答案</p>` : ''}</article>`;
  }).join('');
}

function renderExamSubjectiveQuestions(draft) {
  const list = $('#exam-subjective-list');
  const majors = draft.material_questions || [];
  if (!majors.length) {
    list.innerHTML = '<p class="muted">没有识别到主观题小题。</p>';
    return;
  }
  list.innerHTML = majors.map((major) => {
    const majorId = String(major.label || '');
    const subs = major.sub_questions || [];
    const subMarkup = subs.map((sub) => {
      const qid = String(sub.question_id || `${majorId}.${sub.label || ''}`);
      const points = sub.rubric_draft || [];
      const maxValue = sub.max_score == null ? '' : sub.max_score;
      const scoreLabel = sub.max_score == null ? '未确认满分' : `满分 ${escapeHtml(maxValue)}`;
      return `<section class="exam-subq-editor" data-exam-subq-id="${escapeHtml(qid)}"><div class="exam-subq-head"><h4>${escapeHtml(qid)}</h4><span class="exam-subq-state" data-subq-state>${escapeHtml(scoreLabel)}</span></div>
        <div class="exam-subjective-fields"><label>设问<textarea data-subq-question>${escapeHtml(sub.question || '')}</textarea></label><label>参考答案<textarea data-subq-reference>${escapeHtml(sub.reference_answer || sub.standard_answer || '')}</textarea></label><label class="exam-max-score-field">本小题满分<input data-subq-max-score type="number" min="0.5" step="0.5" value="${escapeHtml(maxValue)}" placeholder="未识别到本题分值，请填写"></label></div>
        <div class="exam-subjective-fields"><label>评分方式<select data-subq-scoring-mode aria-label="${escapeHtml(qid)} 评分方式"><option value="weighted" ${sub.scoring_mode !== 'point_bank' ? 'selected' : ''}>按评分项分值累计</option><option value="point_bank" ${sub.scoring_mode === 'point_bank' ? 'selected' : ''}>按得分点封顶</option></select></label><label>封顶模式每点分值<input data-subq-point-value aria-label="${escapeHtml(qid)} 每点分值" type="number" min="0.5" step="0.5" value="${escapeHtml(sub.point_value ?? 2)}"></label></div>
        <p>按得分点封顶：候选点可以多于满分所需，答中足够的不同要点即可满分；各评分点分值须与每点分值一致，不自动改写。按评分项累计：各项分值合计应等于满分。</p>
        <div><strong>评分点草稿</strong><div class="exam-point-list">${points.map((point, index) => examRubricPointMarkup(qid, index, point)).join('')}</div><button type="button" class="button secondary exam-add-point" data-add-exam-point="${escapeHtml(qid)}">＋添加评分点</button></div>
        <div class="exam-score-warning" data-score-warning></div></section>`;
    }).join('');
    const unknownCount = subs.filter(sub => sub.max_score == null).length;
    return `<details class="exam-subjective-card" data-exam-major-id="${escapeHtml(majorId)}"><summary><span>第 ${escapeHtml(majorId)} 题 · ${subs.length} 个小题</span><small>${unknownCount ? `${unknownCount} 个满分待填写` : '满分已填写'}</small></summary><label class="exam-major-prompt">大题材料 / 共同题干<textarea data-major-prompt>${escapeHtml(major.prompt || '')}</textarea></label>${subMarkup}</details>`;
  }).join('');
}

function examRubricPointMarkup(qid, index, point = {}) {
  const label = point.label || '';
  const score = point.score == null ? '' : point.score;
  return `<div class="exam-point-row" data-exam-point data-question-id="${escapeHtml(qid)}"><input type="text" data-point-label value="${escapeHtml(label)}" aria-label="${escapeHtml(qid)} 评分点文字" placeholder="评分点内容"><input type="number" data-point-score min="0.5" step="0.5" value="${escapeHtml(score)}" aria-label="评分点分值" placeholder="分值"><button type="button" class="button danger compact-button" data-remove-exam-point aria-label="删除评分点">删除</button></div>`;
}

function renderExamWarnings(draft) {
  const status = draft.source?.source_pair_status || 'not_applicable';
  const blockers = [];
  const confirmable = [];
  if (status === 'mismatch') blockers.push('Word 与 PDF 不一致；请移除其中一个来源并重新生成草稿。');
  if (status === 'needs_teacher_confirmation' && !$('#exam-source-confirm').checked) blockers.push('请确认 Word 与 PDF 属于同一份试卷。');
  if (!$('#exam-teacher-confirm').checked) blockers.push('请勾选已核对题目、答案、参考答案和评分点。');
  const objectiveQuestions = draft.objective_questions || [];
  if (!objectiveQuestions.length && !(draft.material_questions || []).some(major => (major.sub_questions || []).length)) blockers.push('草稿中没有可创建的题目。');
  objectiveQuestions.forEach((question) => {
    const id = String(question.question_id);
    const select = document.querySelector(`[data-exam-answer][data-question-id="${CSS.escape(id)}"]`);
    const points=Number(document.querySelector(`[data-exam-objective-points][data-question-id="${CSS.escape(id)}"]`)?.value);
    if(!Number.isFinite(points)||points<=0||points>100)blockers.push(`客观题 ${id} 尚未确认有效分值。`);
    if (!select?.value) blockers.push(`客观题 ${id} 尚未确认答案。`);
    if (question.needs_teacher_review) blockers.push(`客观题 ${id} 结构未完整识别，请重新整理 Word 后导入。`);
  });
  document.querySelectorAll('.exam-subq-editor').forEach((editor) => {
    const id = editor.dataset.examSubqId;
    const maxScore = Number(editor.querySelector('[data-subq-max-score]')?.value);
    if (!Number.isFinite(maxScore) || maxScore <= 0) blockers.push(`${id} 未填写有效满分。`);
    if (!editor.querySelector('[data-subq-question]')?.value.trim()) blockers.push(`${id} 设问不能为空。`);
    if (!editor.querySelector('[data-subq-reference]')?.value.trim()) blockers.push(`${id} 参考答案不能为空。`);
    const points = [...editor.querySelectorAll('[data-exam-point]')];
    if (!points.length) blockers.push(`${id} 至少需要一个评分点。`);
    points.forEach((point, index) => {
      const label = point.querySelector('[data-point-label]')?.value.trim();
      const score = Number(point.querySelector('[data-point-score]')?.value);
      if (!label || !Number.isFinite(score) || score <= 0) blockers.push(`${id} 第 ${index + 1} 个评分点需要填写内容和分值。`);
    });
    if (Number.isFinite(maxScore) && maxScore > 0 && points.length && points.every(point => Number(point.querySelector('[data-point-score]')?.value) > 0)) {
      const sum = points.reduce((total, point) => total + Number(point.querySelector('[data-point-score]').value), 0);
      const warning = editor.querySelector('[data-score-warning]');
      warning.textContent = Math.abs(sum - maxScore) > 0.000001 ? `评分点分值合计 ${sum.toFixed(2).replace(/\.00$/, '')}，本题满分 ${maxScore.toFixed(2).replace(/\.00$/, '')}，请确认。` : '';
      if (editor.querySelector('[data-subq-scoring-mode]').value === 'point_bank') {
        const value = Number(editor.querySelector('[data-subq-point-value]').value);
        if (!Number.isFinite(value) || value <= 0 || points.some(p => Number(p.querySelector('[data-point-score]').value) !== value)) {
          blockers.push(`${id} 封顶模式的每个评分点须与每点分值一致，请逐点核对；系统不会自动改分。`);
        } else if (sum < maxScore - 0.000001) {
          blockers.push(`${id} 候选评分点不足以达到满分。`);
        }
        warning.textContent = `每个不同有效要点 ${value} 分，${Math.ceil(maxScore / value)} 个点可达满分 ${maxScore}；候选 ${points.length} 个，候选池合计 ${sum} 分，最终不超过满分。`;
      }
    }
  });
  for (const warning of (draft.warnings || [])) {
    const message = getImportWarningMessage(warning);
    if (!confirmable.includes(message) && !blockers.some(item => item.startsWith(message))) confirmable.push(message);
  }
  if (status === 'needs_teacher_confirmation' && $('#exam-source-confirm').checked) {
    const message = '教师已确认 Word 与 PDF 属于同一份试卷。';
    const index = blockers.findIndex(item => item.includes('确认 Word 与 PDF'));
    if (index >= 0) blockers.splice(index, 1);
    confirmable.push(message);
  }
  const renderList = (title, items, klass) => `<div class="exam-warning-card ${klass}"><strong>${title}</strong>${items.length ? `<ul>${items.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '<p class="muted">无</p>'}</div>`;
  $('#exam-warning-groups').innerHTML = `<div class="exam-warning-card blocking"><strong>必须处理后才能创建</strong>${blockers.length ? `<ul>${blockers.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>` : '<p class="muted">当前必要项目已填写。</p>'}</div>${renderList('核对后可继续', [...new Set(confirmable)], 'confirmable')}`;
  $('#exam-create-blockers').textContent = blockers.length ? `还有 ${blockers.length} 项需要处理。` : '';
  return blockers;
}

function renderExamDraft(draft, draftId) {
  state.examImportDraftId = draftId;
  state.examImportDraft = draft;
  $('#exam-draft-panel').hidden = false;
  $('#exam-created-result').hidden = true;
  $('#exam-create-name').value = draft.title || String(draft.source?.content_source?.file_name || '').replace(/\.[^.]+$/, '') || '新建考试';
  $('#exam-source-summary').innerHTML = examSourceStateMarkup(draft);
  const status = draft.source?.source_pair_status || 'not_applicable';
  $('#exam-pair-confirm-row').hidden = status !== 'needs_teacher_confirmation';
  const actions = $('#exam-source-actions');
  if (status === 'mismatch') {
    actions.hidden = false;
    actions.innerHTML = '<span class="exam-source-action-note">可返回重传，也可移除一个来源后重新解析：</span><button type="button" class="button secondary" data-exam-rebuild="docx_only">只用 Word 重建草稿</button><button type="button" class="button secondary" data-exam-rebuild="pdf_only">只用 PDF fallback 重建</button>';
  } else actions.hidden = true;
  $('#exam-ai-parse-box').hidden = false;
  $('#ai-parse-exam-draft').disabled = false;
  $('#ai-parse-exam-draft').textContent = 'AI 解析题号/答案';
  $('#exam-ai-parse-status').classList.remove('error');
  $('#exam-ai-parse-status').textContent = '发送 Word/PDF 提取的文字（不发送图片）；结果并入草稿后仍需核对。';
  $('#exam-ai-parse-summary').hidden = true;
  $('#exam-ai-parse-summary').innerHTML = '';
  renderExamObjectiveQuestions(draft);
  renderExamSubjectiveQuestions(draft);
  const refs = draft.layout_reference?.pages || [];
  const referenceDetails = $('#exam-reference-details');
  referenceDetails.hidden = !refs.length;
  $('#exam-reference-list').innerHTML = refs.map(page => `<figure><img loading="lazy" src="${escapeHtml(page.preview_url || '')}" alt="PDF 第 ${escapeHtml(page.page_number)} 页页面 reference"><figcaption>第 ${escapeHtml(page.page_number)} 页 · ${escapeHtml(page.reference_width)} × ${escapeHtml(page.reference_height)} · ${escapeHtml(page.render_dpi)} DPI</figcaption></figure>`).join('');
  renderExamWarnings(draft);
  $('#exam-import-status').textContent = '已生成考试草稿。解析结果尚未创建正式考试，请核对并完成教师确认。';
  $('#exam-draft-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function examAiParseSummaryMarkup(summary) {
  if (!summary) return '';
  const objective = summary.objective || {};
  const subjective = summary.subjective || {};
  const pieces = [
    `客观题：AI 识别 ${objective.ai_count || 0} 道，新增 ${(objective.added || []).length} 道，答案冲突 ${(objective.answer_conflicts || []).length} 处`,
    `主观题：AI 识别 ${subjective.ai_count || 0} 道，新增 ${(subjective.added || []).length} 道，参考答案冲突 ${(subjective.answer_conflicts || []).length} 处`,
  ];
  if ((summary.issues || []).length) pieces.push(`解析提示 ${summary.issues.length} 条`);
  return `<div class="exam-ai-parse-card"><strong>AI 解析结果（仅草稿，未创建考试）</strong><p>${pieces.map(escapeHtml).join(' · ')}</p><p class="muted">冲突项没有自动覆盖原答案；题目、答案、参考答案和评分点仍需教师逐项核对。</p></div>`;
}

function preserveExamImportEdits(incoming, previous, edits) {
  // Compare against the last server draft, so repeated AI passes and edits made
  // while a request is pending remain protected. Unedited blanks can be filled.
  const merged = structuredClone(incoming);
  const value = v => String(v ?? '');
  const points = list => (list || []).map(p => ({ label: value(p.label), score: value(p.score) }));
  const oldMajors = new Map((previous.material_questions || []).map(m => [String(m.label), m]));
  const editedMajors = new Map((edits.material_questions || []).map(m => [String(m.label), m]));
  const oldName = previous.title || String(previous.source?.content_source?.file_name || '').replace(/\.[^.]+$/, '') || '新建考试';
  if (edits.exam_name !== oldName) merged.title = edits.exam_name;
  for (const q of merged.objective_questions || []) {
    const id = String(q.question_id);
    if (Object.hasOwn(edits.objective_answer_key, id)
      && value(edits.objective_answer_key[id]) !== value(previous.objective_answer_key?.[id])) {
      merged.objective_answer_key ||= {};
      merged.objective_answer_key[id] = edits.objective_answer_key[id];
    }
  }
  for (const major of merged.material_questions || []) {
    const old = oldMajors.get(String(major.label)), edited = editedMajors.get(String(major.label));
    if (!old || !edited) continue;
    if (value(edited.prompt) !== value(old.prompt)) major.prompt = edited.prompt;
    const oldSubs = new Map((old.sub_questions || []).map(s => [String(s.question_id), s]));
    const editedSubs = new Map((edited.sub_questions || []).map(s => [String(s.question_id), s]));
    for (const sub of major.sub_questions || []) {
      const oldSub = oldSubs.get(String(sub.question_id)), edit = editedSubs.get(String(sub.question_id));
      if (!oldSub || !edit) continue;
      if (value(edit.question) !== value(oldSub.question)) sub.question = edit.question;
      if (value(edit.reference_answer) !== value(oldSub.reference_answer || oldSub.standard_answer)) {
        sub.reference_answer = edit.reference_answer;
        sub.standard_answer = edit.reference_answer;
      }
      if (value(edit.max_score) !== value(oldSub.max_score)) sub.max_score = edit.max_score;
      if (value(edit.scoring_mode) !== value(oldSub.scoring_mode || 'weighted')) sub.scoring_mode = edit.scoring_mode;
      if (value(edit.point_value) !== value(oldSub.point_value ?? 2)) sub.point_value = edit.point_value;
      if (JSON.stringify(points(edit.rubric_draft)) !== JSON.stringify(points(oldSub.rubric_draft))) {
        sub.rubric_draft = edit.rubric_draft;
      }
    }
  }
  return merged;
}

async function aiParseExamDraft() {
  const draftId = state.examImportDraftId;
  if (!draftId) return;
  const previous = state.examImportDraft;
  const button = $('#ai-parse-exam-draft');
  const status = $('#exam-ai-parse-status');
  status.classList.remove('error');
  button.disabled = true;
  const originalText = button.textContent;
  button.textContent = 'AI 解析中…';
  try {
    const preview = await api('/api/exam-import-ai-preview', { method: 'POST', body: JSON.stringify({ draft_id: draftId }) });
    if (state.examImportDraftId !== draftId) return;
    const message = `将把 ${preview.source_label}（约 ${preview.text_chars} 字符）发送给 ${preview.provider} / ${preview.model}（${preview.destination_host}）用于识别题号、选项、答案和参考答案。\n\n不会发送页面图片，也不会自动创建考试。已执行身份风险检查，请确认原文不含个人信息。是否继续？`;
    if (!window.confirm(message)) {
      status.textContent = '已取消 AI 解析。';
      return;
    }
    button.textContent = 'AI 解析中…';
    const response = await api('/api/exam-import-ai-parse', {
      method: 'POST',
      body: JSON.stringify({ draft_id: draftId, confirm_send_text: true, preview_sha256: preview.preview_sha256 }),
    });
    if (state.examImportDraftId !== draftId) return;
    const edited = preserveExamImportEdits(response.draft || {}, previous, collectExamConfirmation());
    renderExamDraft(edited, draftId);
    // Keep the unedited server baseline for comparison on the next AI pass.
    state.examImportDraft = response.draft || {};
    $('#exam-teacher-confirm').checked = false;
    refreshExamImportWarnings();
    $('#exam-ai-parse-summary').innerHTML = examAiParseSummaryMarkup(response.summary || {});
    $('#exam-ai-parse-summary').hidden = false;
    $('#exam-ai-parse-status').textContent = 'AI 解析已并入草稿；请先核对，再创建正式考试。';
    $('#exam-import-status').textContent = 'AI 解析已完成并写入草稿（未创建考试）。请核对题号、答案和参考答案。';
  } catch (error) {
    status.textContent = error.message;
    status.classList.add('error');
  } finally {
    button.disabled = false;
    button.textContent = originalText;
  }
}

async function parseExamImportDraft() {
  const docx = $('#exam-docx-file').files[0] || null;
  const pdf = $('#exam-pdf-file').files[0] || null;
  if (!docx && !pdf) return alert('请至少选择一份 Word / DOCX 或 PDF 文件。');
  const button = $('#parse-exam-draft');
  button.disabled = true;
  button.textContent = '正在解析…';
  state.examImportDraft = null;
  state.examImportDraftId = '';
  $('#exam-draft-panel').hidden = true;
  $('#exam-import-status').textContent = '正在读取文件并生成可编辑草稿；此过程不会创建正式考试。';
  try {
    const payload = {};
    if (docx) {
      payload.docx_filename = docx.name;
      payload.docx_data_base64 = String(await readFileDataUrl(docx)).split(',')[1] || '';
    }
    if (pdf) {
      payload.pdf_filename = pdf.name;
      payload.pdf_data_base64 = String(await readFileDataUrl(pdf)).split(',')[1] || '';
    }
    const response = await api('/api/import-exam-draft', { method: 'POST', body: JSON.stringify(payload) });
    renderExamDraft(response.draft || {}, response.draft_id);
  } catch (error) {
    $('#exam-import-status').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = '解析并生成草稿';
  }
}

function collectExamConfirmation() {
  const draft = state.examImportDraft || {};
  const objectiveAnswerKey = {};
  document.querySelectorAll('[data-exam-answer]').forEach(select => { objectiveAnswerKey[select.dataset.questionId] = select.value; });
  const answerConflictResolutions = {};
  const docxKey = draft.answer_key_crosscheck?.docx_answer_key || {};
  const pdfKey = draft.answer_key_crosscheck?.pdf_answer_key || {};
  Object.keys(docxKey).forEach((questionId) => {
    if (pdfKey[questionId] && docxKey[questionId] !== pdfKey[questionId]) answerConflictResolutions[questionId] = objectiveAnswerKey[questionId] || '';
  });
  const materialQuestions = [...document.querySelectorAll('[data-exam-major-id]')].map(major => ({
    label: major.dataset.examMajorId,
    prompt: major.querySelector('[data-major-prompt]')?.value || '',
    sub_questions: [...major.querySelectorAll('[data-exam-subq-id]')].map(editor => ({
      question_id: editor.dataset.examSubqId,
      question: editor.querySelector('[data-subq-question]')?.value || '',
      reference_answer: editor.querySelector('[data-subq-reference]')?.value || '',
      max_score: editor.querySelector('[data-subq-max-score]')?.value || '',
      scoring_mode: editor.querySelector('[data-subq-scoring-mode]')?.value || 'weighted',
      point_value: editor.querySelector('[data-subq-point-value]')?.value || '',
      rubric_draft: [...editor.querySelectorAll('[data-exam-point]')].map(point => ({
        label: point.querySelector('[data-point-label]')?.value || '',
        score: point.querySelector('[data-point-score]')?.value || '',
      })),
    })),
  }));
  return {
    exam_name: $('#exam-create-name').value,
    objective_answer_key: objectiveAnswerKey,
    objective_points:Object.fromEntries([...document.querySelectorAll('[data-exam-objective-points]')].map(input=>[input.dataset.questionId,input.value])),
    answer_conflict_resolutions: answerConflictResolutions,
    material_questions: materialQuestions,
    teacher_confirmed: $('#exam-teacher-confirm').checked,
    source_pair_confirmed: $('#exam-source-confirm').checked,
  };
}

function refreshExamImportWarnings() {
  if (state.examImportDraft) renderExamWarnings(state.examImportDraft);
}

async function createImportedExam() {
  const draft = state.examImportDraft;
  if (!draft || !state.examImportDraftId) return;
  const blockers = renderExamWarnings(draft);
  if (blockers.length) {
    $('#exam-create-status').textContent = '暂不能创建，请先处理上方标出的项目。';
    const firstSub = document.querySelector('.exam-subq-editor');
    if (firstSub && blockers.some(item => item.includes(firstSub.dataset.examSubqId))) {
      firstSub.closest('details')?.setAttribute('open', '');
      firstSub.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else $('#exam-warning-groups').scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  const button = $('#exam-create-button');
  button.disabled = true;
  $('#exam-create-status').textContent = '正在保存正式考试…';
  try {
    const response = await api('/api/create-imported-exam', {
      method: 'POST',
      body: JSON.stringify({ draft_id: state.examImportDraftId, confirmation: collectExamConfirmation() }),
    });
    await loadAssignments();
    $('#assignment-select').value = response.assignment.id;
    $('#batch-assignment-select').value = response.assignment.id;
    $('#exam-draft-panel').hidden = true;
    const extra = response.warnings?.length ? `<p>${response.warnings.map(escapeHtml).join('<br>')}</p>` : '';
    $('#exam-created-result').innerHTML = `<strong>考试已创建</strong><span>${escapeHtml(response.assignment.name)} · ${escapeHtml(response.assignment.objective_count)} 道客观题 · ${escapeHtml(response.assignment.subjective_count)} 道主观小题</span><p>正式考试编号：${escapeHtml(response.exam_id)}。导入来源与 reference 已随考试保存。</p>${extra}<div class="exam-created-actions"><button type="button" class="button secondary" id="open-created-exam-layout">设置答题区</button><button type="button" class="button secondary" data-app-view-target="grading">去批改工作台</button></div>`;
    $('#exam-created-result').hidden = false;
    $('#exam-created-result [data-app-view-target="grading"]').addEventListener('click', () => enterGradingForAssignment(response.assignment));
    $('#open-created-exam-layout').addEventListener('click', () => openExamLayoutProfile(response.exam_id));
    $('#exam-create-status').textContent = '';
  } catch (error) {
    $('#exam-create-status').textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

$('#parse-exam-draft').addEventListener('click', parseExamImportDraft);
$('#ai-parse-exam-draft').addEventListener('click', aiParseExamDraft);
$('#exam-create-button').addEventListener('click', createImportedExam);
$('#exam-draft-panel').addEventListener('input', refreshExamImportWarnings);
$('#exam-draft-panel').addEventListener('change', refreshExamImportWarnings);
$('#exam-draft-panel').addEventListener('click', (event) => {
  const addButton = event.target.closest('[data-add-exam-point]');
  if (addButton) {
    const editor = document.querySelector(`[data-exam-subq-id="${CSS.escape(addButton.dataset.addExamPoint)}"]`);
    const points = editor?.querySelector('.exam-point-list');
    if (editor && points) {
      points.insertAdjacentHTML('beforeend', examRubricPointMarkup(editor.dataset.examSubqId, points.children.length, {}));
      points.lastElementChild?.querySelector('[data-point-label]')?.focus();
      refreshExamImportWarnings();
    }
  }
  const removeButton = event.target.closest('[data-remove-exam-point]');
  if (removeButton) {
    removeButton.closest('[data-exam-point]')?.remove();
    refreshExamImportWarnings();
  }
  const rebuildButton = event.target.closest('[data-exam-rebuild]');
  if (rebuildButton) {
    if (rebuildButton.dataset.examRebuild === 'docx_only') $('#exam-pdf-file').value = '';
    if (rebuildButton.dataset.examRebuild === 'pdf_only') $('#exam-docx-file').value = '';
    parseExamImportDraft();
  }
});

function setupAppNavigation() {
  const views = [...document.querySelectorAll('[data-app-view]')];
  const controls = [...document.querySelectorAll('[data-app-view-target]')];
  const viewFor = (name) => views.find((view) => view.dataset.appView === name);
  const activate = (name, updateHash = true) => {
    if (window.HISTORY_GRADER_AUTH?.role === 'teacher' && ['management', 'users'].includes(name)) return false;
    const activeView = viewFor(name);
    if (!activeView) return false;
    views.forEach((view) => {
      const selected = view === activeView;
      view.hidden = !selected;
      view.classList.toggle('active', selected);
    });
    controls.forEach((control) => {
      const selected = control.dataset.appViewTarget === name;
      control.classList.toggle('active', selected);
      if (control.getAttribute('role') === 'tab') {
        control.setAttribute('aria-selected', String(selected));
        control.tabIndex = selected ? 0 : -1;
      } else {
        control.setAttribute('aria-pressed', String(selected));
      }
    });
    document.body.dataset.appView = name;
    if (updateHash) history.replaceState(null, '', `#${activeView.id}`);
    return true;
  };

  controls.forEach((control) => control.addEventListener('click', () => activate(control.dataset.appViewTarget)));
  document.querySelector('.main-nav')?.addEventListener('keydown', (event) => {
    const tabs = [...document.querySelectorAll('.main-nav [role="tab"]')].filter(tab => !tab.hidden);
    const currentIndex = tabs.indexOf(event.target.closest('[role="tab"]'));
    if (currentIndex < 0) return;
    let nextIndex = currentIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = tabs.length - 1;
    else return;
    event.preventDefault();
    activate(tabs[nextIndex].dataset.appViewTarget);
    tabs[nextIndex].focus();
  });

  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    const targetId = link.getAttribute('href').slice(1);
    const target = document.getElementById(targetId);
    const view = target?.closest('[data-app-view]');
    if (!target || !view) return;
    event.preventDefault();
    activate(view.dataset.appView, false);
    history.replaceState(null, '', `#${target.id}`);
    requestAnimationFrame(() => target.scrollIntoView({ behavior: 'smooth', block: 'start' }));
  });

  const initialTarget = document.getElementById(window.location.hash.slice(1));
  const initialView = initialTarget?.closest('[data-app-view]') || (window.location.pathname === '/users' ? viewFor('users') : viewFor('grading'));
  activate(initialView?.dataset.appView || 'grading', false);
  if (initialTarget && initialTarget !== initialView) {
    requestAnimationFrame(() => initialTarget.scrollIntoView({ behavior: 'auto', block: 'start' }));
  }
}

window.addEventListener('beforeunload', stopCameraStream);

addMajorQuestion({ label: '17', sub_questions: [{ label: '1' }] });
setupAppNavigation();
setupGradingModes();
updateContinuousStatus();
loadStatus();
loadStorage();
loadRecoverySubmissions().catch(error => { if (isLocalPage() || storedMobileToken()) $('#recovery-list').innerHTML = `<p class="error">${escapeHtml(error.message)}</p>`; });
loadEvaluationSummary();
loadRosterClasses().catch(error => { $('#roster-note').textContent = error.message; });
loadAssignments().catch(error => setStatus(error.message, true));

$('#start-grading-from-layout').addEventListener('click', async () => {
  const model = state.examLayoutProfile;
  if (!model) return;
  if (model.dirty) { alert('请先保存答题区设置，再开始批改。'); return; }
  await loadAssignments();
  const assignment = state.assignments.find(item => item.id === model.data.assignment_id || item.exam_id === model.examId);
  if (!assignment) { alert('未找到当前考试，请刷新考试列表。'); return; }
  enterGradingForAssignment({ ...assignment, exam_id: model.examId });
});

['#assignment-select', '#batch-assignment-select'].forEach(selector => {
  $(selector).addEventListener('change', event => showGradingAssignmentContext(event.target.value));
});
