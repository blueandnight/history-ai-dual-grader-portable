const PRIVACY_TEMPLATE_VERSION = 2;
const PRIVACY_TEMPLATE_COORDINATE_SYSTEM = 'perspective_corrected_page_normalized';

function copyPrivacySettings(settings, pageCount) {
  const source = settings || {};
  const rectangles = (values) => (Array.isArray(values) ? values : []).map((item) => ({
    x: Number(item.x), y: Number(item.y), width: Number(item.width), height: Number(item.height),
  })).filter((item) => [item.x, item.y, item.width, item.height].every(Number.isFinite)
    && item.x >= 0 && item.y >= 0 && item.width > 0 && item.height > 0
    && item.x + item.width <= 1.00001 && item.y + item.height <= 1.00001);
  return {
    answerRegions: (Array.isArray(source.answerRegions) ? source.answerRegions : []).map((item) => ({
      region_id: String(item.region_id || ''),
      photo_index: Number(item.photo_index), x: Number(item.x), y: Number(item.y),
      width: Number(item.width), height: Number(item.height),
      sub_question_ids: Array.isArray(item.sub_question_ids) ? item.sub_question_ids.map(String) : [],
      coordinate_system: String(item.coordinate_system || PRIVACY_TEMPLATE_COORDINATE_SYSTEM),
      rotation_ccw: Number(item.rotation_ccw || 0),
      invocation_scope: String(item.invocation_scope || 'whole_paper'),
      profile_id: String(item.profile_id || ''),
      approved_prompt_sha256: String(item.approved_prompt_sha256 || ''),
    })).filter((item) => Number.isInteger(item.photo_index) && item.photo_index >= 1 && item.photo_index <= pageCount
      && [item.x, item.y, item.width, item.height].every(Number.isFinite)
      && item.x >= 0 && item.y >= 0 && item.width > 0 && item.height > 0
      && item.x + item.width <= 1.00001 && item.y + item.height <= 1.00001
      && item.sub_question_ids.length > 0),
    redactions: Array.from({ length: pageCount }, (_, index) => rectangles(source.redactions?.[index])),
    corners: Array.from({ length: pageCount }, (_, index) => {
      const value = source.corners?.[index];
      return Array.isArray(value) && value.length === 4
        ? value.map((point) => [Number(point[0]), Number(point[1])])
        : null;
    }),
  };
}

async function readPrivacyTemplates(assignment, pageCounts) {
  const response = await api(`/api/privacy-templates?assignment_id=${encodeURIComponent(assignment.id)}`);
  const templates = new Map();
  const unitIds = new Set((assignment.sub_questions || []).map((item) => String(item.id)));
  for (const pageCount of pageCounts) {
    const saved = response.templates?.[String(pageCount)];
    if (!saved || saved.version !== PRIVACY_TEMPLATE_VERSION
      || saved.coordinate_system !== PRIVACY_TEMPLATE_COORDINATE_SYSTEM
      || saved.page_count !== pageCount) continue;
    const settings = copyPrivacySettings({ answerRegions: saved.answer_regions, redactions: saved.redactions }, pageCount);
    if (settings.answerRegions.some((region) =>
      region.sub_question_ids.some((id) => !unitIds.has(id)))) continue;
    templates.set(pageCount, {
      answerRegions: settings.answerRegions,
      redactions: settings.redactions,
      corners: Array(pageCount).fill(null),
      coordinate_system: PRIVACY_TEMPLATE_COORDINATE_SYSTEM,
    });
  }
  return templates;
}

async function writePrivacyTemplate(assignmentId, pageCount, settings) {
  await api('/api/privacy-templates', {
    method: 'POST',
    body: JSON.stringify({
      assignment_id: assignmentId,
      page_count: pageCount,
      version: PRIVACY_TEMPLATE_VERSION,
      coordinate_system: PRIVACY_TEMPLATE_COORDINATE_SYSTEM,
      answer_regions: settings.answerRegions,
      redactions: settings.redactions,
    }),
  });
}

async function readCompressedPrivacyPages(group) {
  const files = [...(group?.files || [])];
  if (!files.length) throw new Error('隐私预览没有图片，已阻止提交');
  if (files.length > 10) throw new Error('每份答卷最多处理10页');
  const dataUrls = [];
  for (const file of files) {
    if (file.size > 25 * 1024 * 1024) throw new Error(`图片“${file.name}”超过25MiB`);
    dataUrls.push(await compressImage(file, await readFileDataUrl(file)));
  }
  return dataUrls;
}

async function preparePrivacyPages(assignmentId, dataUrls, corners, options = {}) {
  const prepared = await api('/api/ai-payload-preview', {
    method: 'POST',
    body: JSON.stringify({
      preview_stage: 'prepare',
      assignment_id: assignmentId,
      ...(Object.prototype.hasOwnProperty.call(options, 'gradingInputMode')
        ? { grading_input_mode: options.gradingInputMode } : {}),
      ...(Object.prototype.hasOwnProperty.call(options, 'examId') ? { exam_id: options.examId } : {}),
      photos: dataUrls.map((dataUrl, index) => ({
        data_url: dataUrl,
        corners: corners[index] || null,
        ai_redactions: [],
      })),
    }),
  });
  if (prepared.stage !== 'prepared_pages' || prepared.prepared_pages?.length !== dataUrls.length
    || prepared.model_request_sent !== false) throw new Error('服务端处理页面不完整，未发送任何模型请求');
  return prepared.prepared_pages.map((item) => item.data_url);
}

function loadPrivacyImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('无法读取图片，已阻止发送'));
    image.src = dataUrl;
  });
}

async function detectPerspectiveAnomaly(dataUrl) {
  try {
    const image = await loadPrivacyImage(dataUrl);
    const width = 180;
    const height = Math.max(180, Math.round(width * image.naturalHeight / image.naturalWidth));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    context.drawImage(image, 0, 0, width, height);
    const pixels = context.getImageData(0, 0, width, height).data;
    const scanBand = (fraction) => {
      const center = Math.round(height * fraction);
      const radius = Math.max(3, Math.round(height * 0.025));
      const brightColumns = new Array(width).fill(0);
      let samples = 0;
      for (let y = Math.max(0, center - radius); y <= Math.min(height - 1, center + radius); y += 1) {
        samples += 1;
        for (let x = 0; x < width; x += 1) {
          const offset = (y * width + x) * 4;
          const luminance = 0.2126 * pixels[offset] + 0.7152 * pixels[offset + 1] + 0.0722 * pixels[offset + 2];
          if (luminance >= 205) brightColumns[x] += 1;
        }
      }
      const threshold = Math.ceil(samples * 0.58);
      const valid = brightColumns.map((count) => count >= threshold);
      let best = null;
      let start = -1;
      for (let x = 0; x <= width; x += 1) {
        if (x < width && valid[x]) { if (start < 0) start = x; continue; }
        if (start >= 0) {
          const end = x - 1;
          if (end - start + 1 >= width * 0.45 && (!best || end - start > best.end - best.start)) best = { start, end };
          start = -1;
        }
      }
      return best;
    };
    const upper = scanBand(0.20);
    const lower = scanBand(0.80);
    if (!upper || !lower) return false;
    const leftShift = Math.abs(upper.start - lower.start) / width;
    const rightShift = Math.abs(upper.end - lower.end) / width;
    return Math.max(leftShift, rightShift) >= 0.075;
  } catch (_) { return false; }
}

async function editPrivacyGroup(assignmentId, group, initialSettings, options = {}) {
  const files = [...(group?.files || [])];
  if (!files.length) throw new Error('隐私预览没有图片，已阻止提交');
  const pageCount = files.length;
  const assignment = state.assignments.find((item) => String(item.id) === String(assignmentId));
  const cropUnits = assignment?.sub_questions || [];
  const dataUrls = options.dataUrls || await readCompressedPrivacyPages(group);
  const settings = copyPrivacySettings(initialSettings, pageCount);
  let redactions = settings.redactions;
  let answerRegions = options.gradingInputMode === 'original_photo' ? [] : settings.answerRegions;
  const cropToolsEnabled = options.gradingInputMode !== 'original_photo' && !options.identityTemplateOnly;
  let corners = settings.corners;
  let processedPages = options.initialPreparedPages || [];
  const autoPages = Array.isArray(options.forcePerspectivePages)
    ? options.forcePerspectivePages
    : (Array.isArray(options.autoPerspectivePages) ? options.autoPerspectivePages : []);
  let pageIndex = autoPages.length ? autoPages[0] : 0;
  let editingPerspective = autoPages.includes(pageIndex) && !corners[pageIndex];
  let pendingCorners = editingPerspective ? [] : null;
  let previousCorners = null;
  let toolMode = editingPerspective ? 'corners' : 'none';
  let currentImage = null;
  let dragStart = null;
  let dragRect = null;
  let settled = false;
  const dialog = document.createElement('dialog');
  dialog.className = 'ai-privacy-dialog privacy-editor-dialog';
  document.body.appendChild(dialog);

  return new Promise((resolve, reject) => {
    const closeWith = (error, value = null) => {
      if (settled) return;
      settled = true;
      try { dialog.close(); } catch (_) { /* dialog may not have opened */ }
      dialog.remove();
      if (error) reject(error); else resolve(value);
    };
    const pageRegions = () => answerRegions.filter((item) => item.photo_index === pageIndex + 1);
    const selectedUnitIds = () => [...dialog.querySelectorAll('.privacy-crop-unit:checked')].map((input) => input.value);
    const point = (event, canvas) => {
      const bounds = canvas.getBoundingClientRect();
      return {
        x: Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)),
        y: Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height)),
      };
    };
    const redraw = (canvas, image, active = null) => {
      if (editingPerspective) {
        const context = canvas.getContext('2d');
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.drawImage(image, 0, 0, canvas.width, canvas.height);
        (pendingCorners || []).forEach((item, index) => {
          const colors = ['#ef4444', '#f59e0b', '#22c55e', '#3b82f6'];
          const x = item[0] * canvas.width;
          const y = item[1] * canvas.height;
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
        return;
      }
      drawAiRedactions(canvas, image, redactions[pageIndex], active, pageRegions(), toolMode);
    };
    const render = async () => {
          const imageUrl = editingPerspective ? dataUrls[pageIndex] : processedPages[pageIndex];
      const title = options.title || (options.templateMode ? '设置本题身份排除模板' : '修正本份答卷');
      dialog.innerHTML = `
        <section class="ai-privacy-card privacy-editor-card">
          <header><div><span class="eyebrow">${escapeHtml(title)}</span><h2>第 ${pageIndex + 1} / ${pageCount} 页</h2></div><button type="button" class="icon-button privacy-editor-cancel" aria-label="取消">×</button></header>
          <p>${editingPerspective
            ? '检测到页面边缘可能倾斜，请按顺时针点击答卷四角（左上、右上、右下、左下）。这一步只对当前异常图片执行。'
            : (options.templateMode
            ? '先逐页点选四角，将页面登记到校正后的标准页面；然后可选设置身份遮挡。模板只在每份答卷完成四角定位后套用。'
          : (options.gradingInputMode === 'original_photo'
            ? '按上传照片批改。需要时可遮挡身份信息或调整页面透视；不会按题目裁切。'
            : '默认使用整页照片，需要时可手动裁切、遮挡或调整透视；最终图片会集中预览，确认后才发送。'))}</p>
          <div class="ai-privacy-canvas-wrap"><canvas class="ai-privacy-canvas" aria-label="答卷页面"></canvas></div>
          ${editingPerspective || !cropToolsEnabled || !cropUnits.length ? '' : `<div class="ai-crop-units"><strong>新答案裁切关联到</strong>${cropUnits.map((unit, index) => `<label><input class="privacy-crop-unit" type="checkbox" value="${escapeHtml(unit.id)}" ${index === 0 ? 'checked' : ''}>${escapeHtml(unit.label)}</label>`).join('')}</div>`}
          <div class="ai-privacy-tools">
            ${editingPerspective
              ? `<button type="button" class="button secondary privacy-apply-corners" ${pendingCorners?.length === 4 ? '' : 'disabled'}>应用四角校正（${pendingCorners?.length || 0}/4）</button><button type="button" class="button secondary privacy-cancel-corners">取消校正</button>`
              : `<button type="button" class="button secondary privacy-manual-corners">${options.forcePerspectivePages?.includes(pageIndex) ? '页面四角定位中' : '手动调整透视'}</button><button type="button" class="button secondary privacy-tool-none" aria-pressed="${toolMode === 'none'}">整页</button>${cropToolsEnabled ? `<button type="button" class="button secondary privacy-tool-crop" aria-pressed="${toolMode === 'crop'}">可选答案裁切</button>` : ''}<button type="button" class="button secondary privacy-tool-redact" aria-pressed="${toolMode === 'redact'}">可选身份遮挡</button>${cropToolsEnabled ? '<button type="button" class="button secondary privacy-clear-crops">清空本页裁切</button>' : ''}<button type="button" class="button secondary privacy-clear-redactions">清空本页遮挡</button><button type="button" class="button secondary privacy-undo">撤销上一框</button><span>${toolMode === 'crop' ? '拖动绿色框选答案' : (toolMode === 'redact' ? '拖动黑框遮挡身份信息' : '当前按整页预览，不增加裁切或遮挡')}</span>`}
          </div>
          <footer><button type="button" class="button secondary privacy-editor-back" ${pageIndex === 0 ? 'disabled' : ''}>上一页</button><button type="button" class="button secondary privacy-editor-next">${pageIndex < pageCount - 1 ? '下一页' : '完成设置'}</button></footer>
        </section>`;
      const canvas = dialog.querySelector('.ai-privacy-canvas');
      const image = new Image();
      currentImage = image;
      image.onload = () => {
        canvas.width = image.naturalWidth;
        canvas.height = image.naturalHeight;
        redraw(canvas, image);
      };
      image.onerror = () => closeWith(new Error('无法解码本页图片，已阻止发送'));
      image.src = imageUrl;
      dialog.querySelector('.privacy-editor-cancel').addEventListener('click', () => closeWith(new Error('已取消隐私图片设置，本次没有发送模型请求')));
      dialog.querySelector('.privacy-editor-back').addEventListener('click', () => {
        if (pageIndex > 0) { pageIndex -= 1; editingPerspective = false; pendingCorners = null; render(); }
      });
      dialog.querySelector('.privacy-editor-next').addEventListener('click', () => {
        if (pageIndex < pageCount - 1) { pageIndex += 1; editingPerspective = autoPages.includes(pageIndex) && !corners[pageIndex]; pendingCorners = editingPerspective ? [] : null; toolMode = editingPerspective ? 'corners' : 'none'; render(); return; }
        if (options.requirePerspectiveForAllPages && corners.some((item) => !item)) { alert('模板必须为每一页完成四角定位，才能保存到标准页面坐标'); return; }
        if (answerRegions.length) {
          const coveredUnits = new Set(answerRegions.flatMap((item) => item.sub_question_ids || []));
          const missing = cropUnits.filter((unit) => !coveredUnits.has(String(unit.id)));
          if (missing.length) { alert(`裁切模式下还没有为以下小题设置答案区域：${missing.map((unit) => unit.label).join('、')}`); return; }
        }
        closeWith(null, { answerRegions: answerRegions.map((item) => ({ ...item, sub_question_ids: [...item.sub_question_ids] })), redactions: redactions.map((items) => items.map((item) => ({ ...item }))), corners: corners.map((item) => item ? item.map((pointValue) => [...pointValue]) : null) });
      });
      dialog.querySelector('.privacy-tool-none')?.addEventListener('click', () => { toolMode = 'none'; render(); });
      dialog.querySelector('.privacy-tool-crop')?.addEventListener('click', () => { toolMode = 'crop'; render(); });
      dialog.querySelector('.privacy-tool-redact')?.addEventListener('click', () => { toolMode = 'redact'; render(); });
      dialog.querySelector('.privacy-manual-corners')?.addEventListener('click', () => {
        previousCorners = corners[pageIndex] ? corners[pageIndex].map((item) => [...item]) : null;
        pendingCorners = [];
        editingPerspective = true;
        toolMode = 'corners';
        render();
      });
      dialog.querySelector('.privacy-cancel-corners')?.addEventListener('click', () => {
        corners[pageIndex] = previousCorners;
        editingPerspective = false;
        pendingCorners = null;
        toolMode = 'none';
        render();
      });
      dialog.querySelector('.privacy-apply-corners')?.addEventListener('click', async (event) => {
        if (pendingCorners?.length !== 4) return;
        event.currentTarget.disabled = true;
        try {
          const prepared = await preparePrivacyPages(assignmentId, [dataUrls[pageIndex]], [pendingCorners], options);
          processedPages[pageIndex] = prepared[0];
          corners[pageIndex] = pendingCorners.map((item) => [...item]);
          if (!options.preserveTemplateGeometryAfterPerspective) {
            answerRegions = answerRegions.filter((item) => item.photo_index !== pageIndex + 1);
            redactions[pageIndex] = [];
          }
          editingPerspective = false;
          pendingCorners = null;
          previousCorners = null;
          toolMode = 'none';
          render();
        } catch (error) { alert(error.message || '透视校正失败'); event.currentTarget.disabled = false; }
      });
      dialog.querySelector('.privacy-clear-crops')?.addEventListener('click', () => {
        answerRegions = answerRegions.filter((item) => item.photo_index !== pageIndex + 1);
        redraw(canvas, image);
      });
      dialog.querySelector('.privacy-clear-redactions')?.addEventListener('click', () => { redactions[pageIndex] = []; redraw(canvas, image); });
      dialog.querySelector('.privacy-undo')?.addEventListener('click', () => {
        if (toolMode === 'redact') redactions[pageIndex].pop();
        else if (toolMode === 'crop') {
          const lastIndex = answerRegions.map((item) => item.photo_index).lastIndexOf(pageIndex + 1);
          if (lastIndex >= 0) answerRegions.splice(lastIndex, 1);
        }
        redraw(canvas, image);
      });
      canvas.addEventListener('pointerdown', (event) => {
        if (!canvas.width) return;
        if (!editingPerspective && toolMode === 'none') return;
        event.preventDefault();
        const position = point(event, canvas);
        if (editingPerspective) {
          if (pendingCorners.length >= 4) return;
          pendingCorners.push([position.x, position.y]);
          const apply = dialog.querySelector('.privacy-apply-corners');
          if (apply) { apply.disabled = pendingCorners.length !== 4; apply.textContent = `应用四角校正（${pendingCorners.length}/4）`; }
          if (currentImage?.complete) redraw(canvas, currentImage);
          return;
        }
        canvas.setPointerCapture(event.pointerId);
        dragStart = position;
        dragRect = { x: position.x, y: position.y, width: 0, height: 0 };
      });
      canvas.addEventListener('pointermove', (event) => {
        if (!dragStart || editingPerspective) return;
        const end = point(event, canvas);
        dragRect = { x: Math.min(dragStart.x, end.x), y: Math.min(dragStart.y, end.y), width: Math.abs(dragStart.x - end.x), height: Math.abs(dragStart.y - end.y) };
        redraw(canvas, image, dragRect);
      });
      canvas.addEventListener('pointerup', () => {
        if (!dragStart || !dragRect || editingPerspective) return;
        if (dragRect.width >= 0.006 && dragRect.height >= 0.006) {
          if (toolMode === 'redact') redactions[pageIndex].push(dragRect);
          else {
            const ids = selectedUnitIds();
            if (!ids.length) { alert('先选择答案所属的小题'); dragStart = null; dragRect = null; redraw(canvas, image); return; }
            answerRegions.push({ photo_index: pageIndex + 1, ...dragRect, sub_question_ids: ids });
          }
        }
        dragStart = null;
        dragRect = null;
        redraw(canvas, image);
      });
    };
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); closeWith(new Error('已取消隐私图片设置，本次没有发送模型请求')); });
    try { dialog.showModal(); } catch (_) { closeWith(new Error('当前浏览器无法安全显示图片隐私预览')); return; }
    (processedPages.length === pageCount ? Promise.resolve(processedPages) : preparePrivacyPages(assignmentId, dataUrls, corners, options))
      .then((pages) => { processedPages = pages; return render(); })
      .catch((error) => { dialog.innerHTML = `<section class="ai-privacy-card"><h2>本机图片准备失败</h2><p role="alert">${escapeHtml(error.message)}</p><button type="button" class="button secondary privacy-editor-cancel">关闭</button></section>`; dialog.querySelector('.privacy-editor-cancel')?.addEventListener('click', () => closeWith(error)); });
  });
}

async function requestPrivacyPayloadPreview(assignmentId, group, className, dataUrls, settings, dualReviewEnabled = false, options = {}) {
  const files = [...group.files];
  const examId = Object.prototype.hasOwnProperty.call(options, 'examId')
    ? options.examId
    : (document.querySelector('#batch-class-select')?.value === className
      ? (document.querySelector('#batch-exam-select')?.value || '') : '');
  const answerRegions = options.gradingInputMode === 'original_photo' ? [] : settings.answerRegions;
  const payload = await api('/api/ai-payload-preview', {
    method: 'POST',
    body: JSON.stringify({
      assignment_id: assignmentId,
      class_name: className || '',
      exam_id: examId,
      ...(Object.prototype.hasOwnProperty.call(options, 'gradingInputMode')
        ? { grading_input_mode: options.gradingInputMode } : {}),
      student_label: group.studentLabel || '',
      dual_review_enabled: dualReviewEnabled === true,
      photos: dataUrls.map((dataUrl, index) => ({
        data_url: dataUrl,
        corners: settings.corners[index] || null,
        ai_redactions: settings.redactions[index] || [],
      })),
      answer_regions: answerRegions,
    }),
  });
  if (payload.blocked || !payload.preview_token || !Array.isArray(payload.images)
    || (options.gradingInputMode !== 'original_photo' && !payload.images.length)) {
    throw new Error(payload.error || `第 ${group.studentLabel || ''} 份没有生成可核对的外发图片`);
  }
  if (options.gradingInputMode === 'original_photo'
    && (!Array.isArray(payload.answer_regions) || payload.answer_regions.length !== 0)) {
    throw new Error('原照片模式预览返回了答题区裁切，已阻止上传；请重新生成预览');
  }
  if (options.gradingInputMode === 'original_photo'
    && (payload.images.length !== files.length || payload.images.some((image) => image.image_kind !== 'full_page'))) {
    throw new Error('原照片模式预览未完整保留每张整页照片，已阻止上传；请重新生成预览');
  }
  return {
    publicPreview: {
      prompt: payload.prompt,
      regionPrompts: Array.isArray(payload.region_prompts) ? payload.region_prompts : [],
      provider: payload.provider,
      model: payload.model,
      fingerprint: payload.preview_fingerprint,
      images: payload.images,
    },
    approval: {
      token: payload.preview_token,
      fingerprint: payload.preview_fingerprint,
      redactionsByFile: new Map(files.map((file, index) => [file, settings.redactions[index] || []])),
      processedDataUrlsByFile: new Map(files.map((file, index) => [file, dataUrls[index]])),
      cornersByFile: new Map(files.map((file, index) => [file, settings.corners[index] || null])),
      answerRegions: (Array.isArray(payload.answer_regions) ? payload.answer_regions : settings.answerRegions)
        .map((item) => ({ ...item, sub_question_ids: [...item.sub_question_ids] })),
      ...(Object.prototype.hasOwnProperty.call(options, 'gradingInputMode')
        ? { gradingInputMode: options.gradingInputMode } : {}),
      ...(Object.prototype.hasOwnProperty.call(options, 'examId') ? { examId } : {}),
    },
  };
}

function openPrivacyZoom(dataUrl, label) {
  const dialog = document.createElement('dialog');
  dialog.className = 'privacy-zoom-dialog';
  dialog.innerHTML = `<button type="button" class="icon-button privacy-zoom-close" aria-label="关闭">×</button><img alt="${escapeHtml(label)}"><p>${escapeHtml(label)}</p>`;
  dialog.querySelector('img').src = dataUrl;
  document.body.appendChild(dialog);
  const close = () => { try { dialog.close(); } catch (_) { /* not open */ } dialog.remove(); };
  dialog.querySelector('.privacy-zoom-close').addEventListener('click', close);
  dialog.addEventListener('click', (event) => { if (event.target === dialog) close(); });
  dialog.addEventListener('cancel', () => dialog.remove());
  dialog.showModal();
}

function showBatchPrivacyPayloadReview(assignment, groups, previews, templatesByPageCount, allowTemplateManagement = true) {
  const formal = previews.every(item => item.acceptedAnswerRegions);
  const originalPhoto = previews.every(item => item.approval.gradingInputMode === 'original_photo');
  const dialog = document.createElement('dialog');
  dialog.className = 'ai-privacy-dialog batch-ai-review-dialog';
  document.body.appendChild(dialog);
  return new Promise((resolve) => {
    const closeWith = (value) => { try { dialog.close(); } catch (_) { /* not open */ } dialog.remove(); resolve(value); };
    const render = () => {
      const count = groups.length;
      const pageCounts = [...new Set(groups.map((group) => group.files.length))];
      const cards = groups.map((group, index) => {
        const item = previews[index];
        const template = templatesByPageCount.get(group.files.length);
        const images = item.publicPreview.images.map((image, imageIndex) => {
          const fullPage = image.image_kind === 'full_page';
          const kind = fullPage ? '整页' : '裁切';
          const sourceDescription = fullPage
            ? '整页外发图 ' + (imageIndex + 1) + ' · 原页 ' + image.source_page
            : '外发裁切图 ' + imageIndex + 1 + ' · 原页 ' + image.source_page + ' · 小题 ' + escapeHtml((image.sub_question_ids || []).join('、'));
          return '<figure class="batch-ai-outbound-image">'
            + '<button type="button" class="privacy-zoom-trigger" data-group="' + index + '" data-image="' + imageIndex + '" aria-label="放大第 ' + (index + 1) + ' 份' + kind + '外发图 ' + (imageIndex + 1) + '">'
            + '<img src="' + escapeHtml(image.data_url) + '" alt="' + kind + '外发图 ' + (imageIndex + 1) + '"></button>'
            + '<figcaption>' + sourceDescription + '</figcaption></figure>';
        }).join('');
        const templateStatus = originalPhoto
          ? '<p class="batch-ai-normal">按上传照片保留整页批改，不按试卷答题区裁切；需要时可单独遮挡身份信息。</p>'
          : item.templateApplied
          ? '<p class="batch-ai-normal">已用本份每页四角定位到标准页面，并应用身份排除模板。</p>'
          : (item.templateLocationFailed
            ? '<p class="batch-ai-warning">身份模板定位未完成，未套用模板；当前仍显示整页外发图。可点“单独修正本份”遮挡，或取消本批后重拍本份。</p>'
            : (template
              ? '<p class="batch-ai-warning">身份模板尚未用于本份。系统不会按页数自动套用；需要时请逐页定位四角后再应用。</p>'
              : '<p class="batch-ai-warning">当前没有身份模板，按整页直接预览。照片若含姓名、学号、座号或二维码，请重拍为无身份照片，或单独遮挡后再确认。</p>'));
        const anomaly = item.anomalyPages.length
          ? '<p class="batch-ai-warning">第 ' + item.anomalyPages.map((page) => page + 1).join('、') + ' 页检测到可能的透视倾斜；请检查实际外发图，可单独调整或重拍。</p>'
          : '';
        const applyButton = !originalPhoto && template && !item.templateApplied
          ? '<button type="button" class="button secondary privacy-apply-template" data-group="' + index + '">四角定位后应用身份模板</button>'
          : '';
        return '<article class="batch-ai-review-item">'
          + '<header><div><strong>第 ' + (index + 1) + ' 份 · ' + escapeHtml(group.studentLabel || ('答卷' + (index + 1))) + '</strong>'
          + '<small>' + group.files.length + ' 页 · ' + item.publicPreview.images.length + ' 张实际外发图片 · '
          + escapeHtml(item.publicPreview.provider || '') + ' / ' + escapeHtml(item.publicPreview.model || '') + '</small></div>'
          + '<div>' + (item.acceptedAnswerRegions ? '<span>正式裁切已验收；如有身份信息请取消并重新处理源图</span>' : '<button type="button" class="button secondary privacy-edit-group" data-group="' + index + '">单独修正本份</button>' + applyButton) + '</div></header>'
          + (item.answerRegionProfileApplied
            ? '<p class="batch-ai-normal">已按本试卷固定版式生成逐小题答题区。下面每张裁切图都是该小题模型实际收到的图片；请确认手写内容完整，并避开大段印刷材料。</p>'
            : templateStatus) + anomaly
          + '<div class="batch-ai-outbound-grid">' + images + '</div>'
          + '<label class="batch-privacy-status">本份隐私状态 <select class="privacy-status-select" data-group="' + index + '">'
          + '<option value="unchecked" ' + ((group.privacyStatus || 'unchecked') === 'unchecked' ? 'selected' : '') + '>尚未确认（不能发送模型）</option>'
          + '<option value="pre_anonymized" ' + (group.privacyStatus === 'pre_anonymized' ? 'selected' : '') + '>导入前已避开身份信息</option>'
          + '<option value="redacted" ' + (group.privacyStatus === 'redacted' ? 'selected' : '') + '>已在预览图中遮挡身份信息</option>'
          + '<option value="no_identity_visible" ' + (group.privacyStatus === 'no_identity_visible' ? 'selected' : '') + '>我确认图中没有可辨认身份信息</option>'
          + '</select></label>'
          + '<details><summary>查看本份实际提示词</summary>'
          + (item.publicPreview.regionPrompts.length
            ? item.publicPreview.regionPrompts.map((entry) => '<h4>' + escapeHtml(entry.label || entry.region_id) + '</h4><pre class="ai-payload-prompt">' + escapeHtml(entry.prompt) + '</pre>').join('')
            : '<pre class="ai-payload-prompt">' + escapeHtml(item.publicPreview.prompt) + '</pre>')
          + '<p class="privacy-fingerprint">预览指纹 ' + escapeHtml(item.publicPreview.fingerprint.slice(0, 16)) + '…</p></details>'
          + '</article>';
      }).join('');
      const templateButtons = originalPhoto
        ? '<span>原照片模式保留整页图片；需要时可单独遮挡身份信息。</span>'
        : formal ? '<span>正式答题区使用服务端验收结果；需要修改时请取消，重新处理源照片和答题区。</span>' : allowTemplateManagement ? pageCounts.map((pageCount) => {
        const existing = templatesByPageCount.has(pageCount);
        return '<button type="button" class="button secondary privacy-edit-template" data-page-count="' + pageCount + '">'
          + (existing ? '修改' : '设置') + pageCount + '页身份排除模板</button>';
      }).join('') : '<span>手机端不读取或修改电脑保存的隐私模板；需要遮挡时可单独修正本批答卷。</span>';
      dialog.innerHTML = '<section class="ai-privacy-card batch-ai-review-card">'
        + '<header><div><span class="eyebrow">批次外发隐私检查</span><h2>' + (count === 1 ? '核对本份最终图片' : '集中核对 ' + count + ' 份答卷')
        + '</h2></div><button type="button" class="icon-button batch-ai-review-cancel" aria-label="取消">×</button></header>'
        + (originalPhoto
          ? '<p>下方展示的就是将要发送给模型的图片。原照片批改会保留整页，不按试卷答题区裁切。请检查身份信息并确认每份隐私状态；“尚未确认”时不会发送模型。</p>'
          : '<p>下方展示的就是将要发送给模型的最终图片。当前固定试卷版式会预设各小题答题区；请检查裁切是否完整并避开身份信息和大段印刷材料。其他题目默认显示整页。请确认每份隐私状态；“尚未确认”时不会发送模型。发现身份信息或裁切错误时请取消并重新处理。</p>')
        + '<div class="batch-ai-template-bar">' + (originalPhoto || formal ? '' : '<span>可选：按页数设置身份排除模板；套用前必须逐页定位四角，并确认版式相同。</span>') + templateButtons + '</div>'
        + (count > 1 ? '<div class="batch-ai-template-bar"><label class="batch-privacy-status">批量设置隐私状态<select class="privacy-bulk-status"><option value="">请选择状态</option><option value="pre_anonymized">导入前已避开身份信息</option><option value="redacted">已在预览图中遮挡身份信息</option><option value="no_identity_visible">我确认图中没有可辨认身份信息</option></select></label><button type="button" class="button secondary privacy-bulk-apply" disabled>应用到尚未确认的答卷</button><span>请先查看本批实际外发图片；已设置的单份状态不会覆盖，应用后仍可单独修改。</span><span class="privacy-bulk-feedback" role="status" aria-live="polite"></span></div>' : '')
        + '<div class="batch-ai-review-list">' + cards + '</div>'
        + '<label class="ai-privacy-check batch-ai-confirm"><input type="checkbox" class="batch-ai-consent">我已逐张查看本批实际外发图片，确认其中不含未排除的身份信息、题目与答案可供本次评分，并同意提交模型批改</label>'
        + '<footer><button type="button" class="button secondary batch-ai-review-cancel">取消本批</button><button type="button" class="button primary batch-ai-confirm-send" disabled>'
        + (count === 1 ? '确认并上传本份' : '确认并上传 ' + count + ' 份') + '</button></footer></section>';
      dialog.querySelectorAll('.privacy-zoom-trigger').forEach((button) => button.addEventListener('click', () => {
        const groupIndex = Number(button.dataset.group);
        const imageIndex = Number(button.dataset.image);
        openPrivacyZoom(previews[groupIndex].publicPreview.images[imageIndex].data_url, '第 ' + (groupIndex + 1) + ' 份实际外发图 ' + (imageIndex + 1));
      }));
      dialog.querySelectorAll('.privacy-edit-group').forEach((button) => button.addEventListener('click', () => closeWith({ action: 'edit-group', index: Number(button.dataset.group) })));
      dialog.querySelectorAll('.privacy-edit-template').forEach((button) => button.addEventListener('click', () => closeWith({ action: 'edit-template', pageCount: Number(button.dataset.pageCount) })));
      dialog.querySelectorAll('.privacy-apply-template').forEach((button) => button.addEventListener('click', () => closeWith({ action: 'apply-template', index: Number(button.dataset.group) })));
      dialog.querySelectorAll('.batch-ai-review-cancel').forEach((button) => button.addEventListener('click', () => closeWith({ action: 'cancel' })));
      const consent = dialog.querySelector('.batch-ai-consent');
      const confirm = dialog.querySelector('.batch-ai-confirm-send');
      const privacyStatuses = [...dialog.querySelectorAll('.privacy-status-select')];
      const isConfirmedStatus = (value) => ['pre_anonymized', 'redacted', 'no_identity_visible'].includes(value);
      const updateConfirmAvailability = () => {
        confirm.disabled = !consent.checked || privacyStatuses.some((select) => !isConfirmedStatus(select.value));
      };
      consent.addEventListener('change', updateConfirmAvailability);
      privacyStatuses.forEach((select) => select.addEventListener('change', () => {
        const index = Number(select.dataset.group);
        if (groups[index]) groups[index].privacyStatus = select.value;
        updateConfirmAvailability();
      }));
      const bulkStatus = dialog.querySelector('.privacy-bulk-status');
      const bulkApply = dialog.querySelector('.privacy-bulk-apply');
      if (bulkStatus && bulkApply) {
        bulkStatus.addEventListener('change', () => { bulkApply.disabled = !isConfirmedStatus(bulkStatus.value); });
        bulkApply.addEventListener('click', () => {
          if (!isConfirmedStatus(bulkStatus.value)) return;
          let changed = 0;
          privacyStatuses.forEach((select) => {
            if (select.value !== 'unchecked') return;
            select.value = bulkStatus.value;
            select.dispatchEvent(new Event('change'));
            changed += 1;
          });
          dialog.querySelector('.privacy-bulk-feedback').textContent = `已设置${changed}份。请核对例外答卷，最后勾选确认并点击上传；此操作不会发送模型。`;
        });
      }
      confirm.addEventListener('click', () => {
        const selectedStatuses = privacyStatuses.map((select) => select.value);
        if (consent.checked && selectedStatuses.length === count && selectedStatuses.every(isConfirmedStatus)) {
          closeWith({ action: 'confirm', privacyStatuses: selectedStatuses });
        }
      });
    };
    render();
    try { dialog.showModal(); } catch (_) { closeWith({ action: 'cancel' }); }
  });
}

async function reviewAiPrivacyBatch(assignmentId, groups, className = '', dualReviewEnabled = false, options = {}) {
  const assignment = state.assignments.find((item) => String(item.id) === String(assignmentId));
  if (!assignment) throw new Error('作业不存在，请重新选择');
  if (!Array.isArray(groups) || !groups.length) throw new Error('没有可核对的答卷');
  const gradingInputMode = options.gradingInputMode;
  const formal = gradingInputMode === 'original_photo'
    ? null
    : await reviewFormalPrivacyBatch(assignment, groups, className, dualReviewEnabled, options);
  if (formal) return formal;
  const pageCounts = [...new Set(groups.map((group) => (group.files || []).length))];
  if (pageCounts.some((count) => count < 1 || count > 10)) throw new Error('每份答卷需要1—10页照片');
  const mobileClient = Boolean(storedMobileToken());
  const templatesByPageCount = mobileClient
    ? new Map()
    : await readPrivacyTemplates(assignment, pageCounts);
  const dirtyPageCounts = new Set();
  const overrides = new Map();
  const templateAppliedGroups = new Set();
  const templateLocationFailedGroups = new Set();
  const previews = new Array(groups.length);
  const dataUrlsByGroup = new Map();
  const previewOptions = {
    ...(Object.prototype.hasOwnProperty.call(options, 'gradingInputMode') ? { gradingInputMode } : {}),
    ...(Object.prototype.hasOwnProperty.call(options, 'examId') ? { examId: options.examId } : {}),
  };
  const setBusy = (message) => {
    let dialog = document.querySelector('.batch-ai-busy-dialog');
    if (!dialog) { dialog = document.createElement('dialog'); dialog.className = 'ai-privacy-dialog batch-ai-busy-dialog'; document.body.appendChild(dialog); dialog.showModal(); }
    dialog.innerHTML = '<section class="ai-privacy-card"><h2>正在准备批次隐私预览…</h2><p>' + escapeHtml(message) + '</p><p>此步骤只生成本地服务端预览，不调用第三方模型。</p></section>';
  };
  const closeBusy = () => document.querySelector('.batch-ai-busy-dialog')?.remove();
  const loadGroup = async (index) => {
    if (!dataUrlsByGroup.has(index)) dataUrlsByGroup.set(index, await readCompressedPrivacyPages(groups[index]));
    return dataUrlsByGroup.get(index);
  };
  const baseFor = (index) => overrides.get(index) || {
    answerRegions: gradingInputMode !== 'original_photo'
      && groups[index].files.length === Number(assignment.answer_region_profile?.page_count || 0)
      ? (assignment.answer_region_profile?.regions || []).map((region) => ({
        ...region, sub_question_ids: [...(region.sub_question_ids || [])],
      }))
      : [],
    redactions: Array(groups[index].files.length).fill(null).map(() => []),
    corners: Array(groups[index].files.length).fill(null),
  };
  const generate = async (index) => {
    setBusy('正在生成第 ' + (index + 1) + ' / ' + groups.length + ' 份最终外发图片预览。');
    const dataUrls = await loadGroup(index);
    const settings = copyPrivacySettings(baseFor(index), dataUrls.length);
    if (gradingInputMode === 'original_photo') settings.answerRegions = [];
    const initialPreparedPages = await preparePrivacyPages(assignmentId, dataUrls, settings.corners, previewOptions);
    const anomalyPages = [];
    for (let page = 0; page < initialPreparedPages.length; page += 1) {
      if (!settings.corners[page] && await detectPerspectiveAnomaly(initialPreparedPages[page])) anomalyPages.push(page);
    }
    const result = await requestPrivacyPayloadPreview(
      assignmentId, groups[index], className, dataUrls, settings, dualReviewEnabled, previewOptions,
    );
    previews[index] = {
      publicPreview: result.publicPreview,
      approval: result.approval,
      settings,
      anomalyPages,
      templateApplied: templateAppliedGroups.has(index),
      answerRegionProfileApplied: !overrides.has(index)
        && gradingInputMode !== 'original_photo'
        && groups[index].files.length === Number(assignment.answer_region_profile?.page_count || 0),
      templateLocationFailed: templateLocationFailedGroups.has(index),
    };
  };
  try {
    for (let index = 0; index < groups.length; index += 1) await generate(index);

    while (true) {
      closeBusy();
      const choice = await showBatchPrivacyPayloadReview(
        assignment, groups, previews, templatesByPageCount, !mobileClient,
      );
      if (choice.action === 'cancel') throw new Error('已取消本批隐私确认；答卷没有上传或发送给模型');
      if (choice.action === 'edit-group') {
        const index = choice.index;
        const dataUrls = await loadGroup(index);
        const edited = await editPrivacyGroup(assignmentId, groups[index], previews[index].settings, {
          dataUrls,
          ...previewOptions,
          initialPreparedPages: await preparePrivacyPages(assignmentId, dataUrls, previews[index].settings.corners, previewOptions),
          title: '第 ' + (index + 1) + ' 份 · 可选遮挡或页面调整',
        });
        overrides.set(index, edited);
        await generate(index);
        continue;
      }
      if (choice.action === 'apply-template') {
        const index = choice.index;
        const pageCount = groups[index].files.length;
        const template = templatesByPageCount.get(pageCount);
        if (!template) throw new Error('身份模板不存在或已失效，请重新预览');
        const dataUrls = await loadGroup(index);
        const initialPreparedPages = await preparePrivacyPages(assignmentId, dataUrls, Array(pageCount).fill(null), previewOptions);
        let applied;
        try {
          applied = await editPrivacyGroup(assignmentId, groups[index], {
            answerRegions: [],
            redactions: template.redactions,
            corners: Array(pageCount).fill(null),
          }, {
            ...previewOptions,
            dataUrls,
            initialPreparedPages,
            forcePerspectivePages: Array.from({ length: pageCount }, (_, page) => page),
            preserveTemplateGeometryAfterPerspective: true,
            requirePerspectiveForAllPages: true,
            identityTemplateOnly: true,
            title: pageCount + '页身份模板 · 逐页定位后应用',
          });
        } catch (error) {
          if (String(error?.message || '').startsWith('已取消隐私图片设置')) {
            templateLocationFailedGroups.add(index);
            previews[index].templateLocationFailed = true;
            continue;
          }
          throw error;
        }
        overrides.set(index, { answerRegions: [], redactions: applied.redactions, corners: applied.corners });
        templateLocationFailedGroups.delete(index);
        templateAppliedGroups.add(index);
        await generate(index);
        continue;
      }
      if (choice.action === 'edit-template') {
        const pageCount = choice.pageCount;
        const index = groups.findIndex((group) => group.files.length === pageCount);
        const dataUrls = await loadGroup(index);
        const previousTemplate = templatesByPageCount.get(pageCount);
        const initial = {
          answerRegions: [],
          redactions: previousTemplate ? previousTemplate.redactions : Array(pageCount).fill(null).map(() => []),
          corners: Array(pageCount).fill(null),
        };
        const edited = await editPrivacyGroup(assignmentId, groups[index], initial, {
          dataUrls,
          ...previewOptions,
          initialPreparedPages: await preparePrivacyPages(assignmentId, dataUrls, Array(pageCount).fill(null), previewOptions),
          forcePerspectivePages: Array.from({ length: pageCount }, (_, page) => page),
          preserveTemplateGeometryAfterPerspective: true,
          requirePerspectiveForAllPages: true,
          identityTemplateOnly: true,
          templateMode: true,
          title: pageCount + '页身份排除模板 · 设置前逐页定位四角',
        });
        const template = {
          answerRegions: [],
          redactions: edited.redactions,
          corners: Array(pageCount).fill(null),
          coordinate_system: PRIVACY_TEMPLATE_COORDINATE_SYSTEM,
        };
        templatesByPageCount.set(pageCount, template);
        dirtyPageCounts.add(pageCount);
        overrides.set(index, { answerRegions: [], redactions: edited.redactions, corners: edited.corners });
        templateAppliedGroups.add(index);
        await generate(index);
        continue;
      }
      if (choice.action !== 'confirm') throw new Error('批次隐私预览状态无效');
      const approvals = previews.map((item, index) => ({
        ...item.approval,
        privacy_status: choice.privacyStatuses[index],
      }));
      for (const count of dirtyPageCounts) await writePrivacyTemplate(assignmentId, count, templatesByPageCount.get(count));
      return approvals;
    }
  } finally { closeBusy(); }
}
