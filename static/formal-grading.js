// Formal images are produced and signed by the server. Never recompress them.
async function confirmFormalCropRuns(groups, runs) {
  if (runs.some(({ run }) => run.crops.some(crop => !run.artifact_urls?.[crop.padding_validation_file_name || crop.file_name]))) {
    throw new Error('裁切预览缺失，不能验收');
  }
  const dialog = document.createElement('dialog');
  dialog.className = 'ai-privacy-dialog batch-ai-review-dialog formal-crop-review-dialog';
  document.body.appendChild(dialog);
  return new Promise((resolve) => {
    const close = (value) => { dialog.close(); dialog.remove(); resolve(value); };
    const cards = runs.map(({ run, groupIndex, fileIndex }) => {
      const images = run.crops.map(crop => {
        const file = crop.padding_validation_file_name || crop.file_name;
        const url = run.artifact_urls?.[file];
        if (!url) throw new Error('裁切预览缺失，不能验收');
        return `<figure class="batch-ai-outbound-image"><a class="privacy-zoom-trigger" href="${escapeHtml(url)}" target="_blank" rel="noopener"><img src="${escapeHtml(url)}" alt="小题 ${escapeHtml(crop.question_id)} 裁切图"></a><figcaption>小题 ${escapeHtml(crop.question_id)}</figcaption><label><input type="checkbox" class="formal-crop-checked">我已核对：题号正确、手写基本完整、未混入材料正文</label></figure>`;
      }).join('');
      const overlays = (run.accepted_pages || []).map(page => {
        const url = run.artifact_urls?.[`roi_overlay_page_${page.page_number}.png`];
        return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">查看原页与裁切边界（第${Number(page.page_number)}页）</a>` : '';
      }).join(' ');
      const warning = run.alignment_assisted ? '<p role="alert">外框辅助对齐：请核对本批预览中的小题位置和答案完整性，可批量验收，异常图片可单独取消。</p>' : '';
      return `<article class="batch-ai-review-item"><h3>第${groupIndex + 1}份 · 源照片${fileIndex + 1}</h3>${warning}<p>${escapeHtml(groups[groupIndex].studentLabel || '')}</p><p>${overlays}</p><div class="batch-ai-outbound-grid">${images}</div></article>`;
    }).join('');
    dialog.innerHTML = `<section class="ai-privacy-card batch-ai-review-card"><h2>先验收正式答题区裁切</h2><p>这里只检查配准和裁切，不会调用模型。请逐张核对；有截字、错页或材料混入时取消，调整答题区或重拍。下一步仍需独立确认外发隐私。</p><div class="batch-ai-review-list" aria-label="正式答区裁切列表">${cards}</div><footer><button type="button" class="button secondary formal-crop-cancel">取消</button><button type="button" class="button primary formal-crop-accept" disabled>裁切验收通过，继续隐私预览</button></footer></section>`;
    const button = dialog.querySelector('.formal-crop-accept');
    const checks = [...dialog.querySelectorAll('.formal-crop-checked')];
    const toolbar = document.createElement('div');
    toolbar.className = 'batch-ai-template-bar';
    toolbar.innerHTML = '<button type="button" class="button secondary formal-crop-check-all">已查看本批预览，全部验收</button><button type="button" class="button secondary formal-crop-uncheck-all">取消全部验收</button><span class="formal-crop-count" role="status" aria-live="polite"></span>';
    dialog.querySelector('.batch-ai-review-list').before(toolbar);
    const update = () => {
      const count = checks.filter(item => item.checked).length;
      button.disabled = !checks.length || count !== checks.length;
      toolbar.querySelector('.formal-crop-count').textContent = `已验收 ${count}/${checks.length} 张；异常图片可单独取消。下一步仍需确认隐私。`;
    };
    checks.forEach(input => input.addEventListener('change', update));
    toolbar.querySelector('.formal-crop-check-all').addEventListener('click', () => { checks.forEach(item => { item.checked = true; }); update(); });
    toolbar.querySelector('.formal-crop-uncheck-all').addEventListener('click', () => { checks.forEach(item => { item.checked = false; }); update(); });
    update();
    button.addEventListener('click', () => close(true));
    dialog.querySelector('.formal-crop-cancel').addEventListener('click', () => close(false));
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(false); });
    dialog.showModal();
  });
}

async function reviewFormalPrivacyBatch(assignment, groups, className, dualReviewEnabled, options = {}) {
  if (!assignment.formal_layout_required && !className) return null;
  const selected = document.querySelector('#batch-exam-select')?.value;
  if (!options.examId && !className && (assignment.layout_exams || []).length > 1 && !selected) {
    throw new Error('本作业有多套答题区，请明确选择本批对应的正式考试。');
  }
  const examId = options.examId || selected || assignment.exam_id;
  if (!examId) throw new Error('请先选择正式考试，并保存主观题答题区。');
  const context = await api(`/api/exam-layout-profile?exam_id=${encodeURIComponent(examId)}`);
  if (context.assignment_id !== assignment.id) throw new Error('考试与当前作业不一致，请重新选择。');
  if (!context.answer_regions_required) {
    if (className) throw new Error('正式班级评分需要先为该考试配置 PDF 答题区。');
    return null; // Existing layout-free anonymous path.
  }
  if (!context.layout_profile || context.profile_invalid || context.profile_hash_mismatch || context.pdf_hash_mismatch) {
    throw new Error('当前正式考试缺少有效答题区，请打开“答题区设置”保存后再上传。');
  }
  const required = new Set(context.subjective_question_ids || []);
  if (!required.size) throw new Error('未找到本次主观题，已停止裁切。');
  const configured = new Set(context.layout_profile.regions.filter(r => r.region_type === 'subjective_answer').map(r => String(r.question_id)));
  if ([...required].some(id => !configured.has(id))) throw new Error('答题区设置尚未覆盖所有主观题，请先补齐。');
  const runs = [];
  const busy = document.createElement('dialog');
  busy.className = 'ai-privacy-dialog';
  document.body.appendChild(busy);
  try {
    busy.innerHTML = '<section class="ai-privacy-card"><h2>正在生成正式答题区</h2><p role="status">服务器配准与裁切中，不调用模型。</p></section>';
    busy.showModal();
    busy.addEventListener('cancel', event => event.preventDefault());
    for (let groupIndex = 0; groupIndex < groups.length; groupIndex += 1) {
      const group = groups[groupIndex];
      if (!group.files?.length || group.files.length > 10 || group.files.some(file => file.size > 25 * 1024 * 1024)) {
        throw new Error(`第${groupIndex + 1}份需包含1—10张照片，且单张不超过25MiB。`);
      }
      const seen = new Set();
      for (let fileIndex = 0; fileIndex < group.files.length; fileIndex += 1) {
        busy.querySelector('[role="status"]').textContent = `第${groupIndex + 1}/${groups.length}份，照片${fileIndex + 1}：正在配准与裁切，不调用模型。`;
        const form = new FormData();
        form.append('metadata', JSON.stringify({ exam_id: examId, input_mode: document.querySelector('#batch-page-layout')?.value || 'single_page' }));
        form.append('student_image', group.files[fileIndex], group.files[fileIndex].name);
        const run = await api('/api/student-auto-crop', { method: 'POST', body: form });
        if (run.status !== 'cropped' || run.partial_processing || !run.crops?.length || run.exam_id !== examId || run.layout_profile_id !== context.layout_profile_id) {
          const hint = run.status === 'alignment_failed'
            ? (run.failure_code === 'page_layout_mismatch'
              ? '照片与参考页的整页布局无法可靠对应。请核对是否为同一版答题卡（含信息栏、表格和小题位置），使用实际印发版空白PDF设置答题区；也请检查照片是否缺边或变形。'
              : '没有足够且分布可靠的页面匹配点。请核对单页/双页模式及参考页，并上传完整、清晰的答题卡照片。')
            : '请检查拍摄页和答题区。';
          throw new Error(`第${groupIndex + 1}份照片${fileIndex + 1}未通过裁切：${run.status || '未知状态'}。${hint}本批未发送模型。`);
        }
        for (const crop of run.crops) {
          const id = String(crop.question_id);
          if (!required.has(id) || seen.has(id)) throw new Error(`第${groupIndex + 1}份小题${id}重复或不属于当前考试，请核对照片分组。`);
          seen.add(id);
        }
        runs.push({ run, groupIndex, fileIndex });
      }
      if (seen.size !== required.size) throw new Error(`第${groupIndex + 1}份缺少主观题照片，不能作为完整答卷上传。`);
      if (seen.size > 10) throw new Error('本份裁切图超过10张，请按现有小题范围分批处理。');
    }
  } finally { busy.close(); busy.remove(); }
  if (!await confirmFormalCropRuns(groups, runs)) throw new Error('已取消裁切验收，本批没有发送模型。');
  const cropsByGroup = groups.map(() => []);
  for (const item of runs) {
    const id = item.run.alignment_run_id;
    const review = await api('/api/student-auto-crop-review', { method: 'POST', body: JSON.stringify({ alignment_run_id: id, verdict: 'passed' }) });
    if (review.human_review !== 'passed') throw new Error('裁切验收未保存，已阻止上传。');
    const response = await api('/api/student-auto-crop-answer-regions', { method: 'POST', body: JSON.stringify({ alignment_run_id: id }) });
    if (!response.answer_regions?.length) throw new Error('没有合法的裁切凭据，已阻止上传。');
    for (const crop of response.answer_regions) {
      if (crop.provenance?.exam_id !== examId || crop.layout_profile_id !== context.layout_profile_id || !crop.attestation) throw new Error('裁切凭据与考试不一致，已阻止上传。');
      cropsByGroup[item.groupIndex].push({ ...crop, sourceFilename: groups[item.groupIndex].files[item.fileIndex].name });
    }
  }
  const previews = [];
  for (let index = 0; index < groups.length; index += 1) {
    const crops = cropsByGroup[index];
    const questionIds = new Set(crops.map(crop => crop.question_id));
    const wanted = new Set(assignment.sub_questions.map(q => String(q.id)));
    if (crops.length !== wanted.size || questionIds.size !== wanted.size || [...wanted].some(id => !questionIds.has(id))) throw new Error(`第${index + 1}份裁切凭据未完整覆盖评分范围。`);
    const answerRegions = crops.map((crop, i) => ({ region_id: `accepted:${crop.question_id}`, profile_id: crop.layout_profile_id,
      photo_index: i + 1, x: 0, y: 0, width: 1, height: 1, sub_question_ids: [crop.question_id],
      coordinate_system: 'perspective_corrected_page_normalized', rotation_ccw: 0, invocation_scope: 'answer_region' }));
    const photos = crops.map((crop, i) => ({ name: `crop-${i + 1}.png`, page_sequence: i + 1, data_url: crop.data_url,
      original_data_url: crop.data_url, image_role: 'answer_region', answer_region_attestation: crop.attestation, ai_redactions: [] }));
    const preview = await api('/api/ai-payload-preview', { method: 'POST', body: JSON.stringify({ assignment_id: assignment.id,
      exam_id: examId,
      ...(Object.prototype.hasOwnProperty.call(options, 'gradingInputMode')
        ? { grading_input_mode: options.gradingInputMode } : {}),
      class_name: className, student_label: groups[index].studentLabel || '',
      dual_review_enabled: dualReviewEnabled, photos, answer_regions: answerRegions }) });
    if (preview.blocked || !preview.preview_token || preview.images?.length !== photos.length || preview.images.some(image => image.image_kind === 'full_page')) throw new Error('正式答题区未能生成合法的外发预览。');
    previews.push({ acceptedAnswerRegions: true, answerRegionProfileApplied: true, anomalyPages: [],
      publicPreview: { prompt: preview.prompt, regionPrompts: preview.region_prompts || [], images: preview.images,
        provider: preview.provider, model: preview.model, fingerprint: preview.preview_fingerprint },
      approval: { token: preview.preview_token, fingerprint: preview.preview_fingerprint, answerRegions: preview.answer_regions,
        redactionsByFile: new Map(), acceptedPhotos: photos, examId,
        ...(Object.prototype.hasOwnProperty.call(options, 'gradingInputMode')
          ? { gradingInputMode: options.gradingInputMode } : {}),
        identityFilenames: crops.map(crop => crop.sourceFilename) } });
  }
  const choice = await showBatchPrivacyPayloadReview(assignment, groups, previews, new Map(), false);
  if (choice.action !== 'confirm') throw new Error('已取消最终隐私确认，本批没有发送模型。');
  return previews.map((item, index) => ({ ...item.approval, privacy_status: choice.privacyStatuses[index] }));
}
