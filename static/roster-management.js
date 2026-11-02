async function loadClassExams(){
 const aid=document.querySelector('#batch-assignment-select').value,select=document.querySelector('#batch-exam-select'),a=state.assignments.find(v=>v.id===aid);
 const mode=document.querySelector('#batch-grading-input-mode').value;
 const exams=(mode==='auto_crop'?a?.layout_exams:a?.original_photo_exams)||[];
 select.innerHTML='<option value="">选择考试</option>'+exams.map(e=>`<option value="${escapeHtml(e.exam_id)}">本次阅卷 · ${escapeHtml(e.created_at)}</option>`).join('');select.disabled=!aid;if(exams.length===1)select.value=exams[0].exam_id;
}
async function loadRosterEditor(preferred = '') {
  const select = document.querySelector('#roster-editor-class');
  const value = preferred || select.value;
  select.innerHTML = '<option value="">选择班级查看或修改</option>' + state.rosterClasses.map(c => `<option value="${escapeHtml(c.class_name)}">${escapeHtml(c.class_name)}</option>`).join('');
  if (state.rosterClasses.some(c => c.class_name === value)) select.value = value;
  const host = document.querySelector('#roster-students-editor');
  if (!select.value) { host.innerHTML = ''; return; }
  const data = await api(`/api/roster-students?class_name=${encodeURIComponent(select.value)}`);
  host.innerHTML = (data.students || []).map(s => `<form class="roster-student-edit form-grid" data-student-id="${escapeHtml(s.id)}"><div class="form-row two-columns"><label>学号（可选）<input name="student_no" value="${escapeHtml(s.student_no)}" maxlength="100"></label><label>姓名<input name="display_name" value="${escapeHtml(s.student_name)}" required maxlength="100"></label></div><label><input type="checkbox" name="enabled" ${s.enabled ? 'checked' : ''}> 在当前花名册中启用</label><button class="button secondary" type="submit">保存修改</button></form>`).join('');
  host.querySelectorAll('form').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      await api('/api/roster-student', { method: 'POST', body: JSON.stringify({ student_id: form.dataset.studentId,
        student_no: form.elements.student_no.value, display_name: form.elements.display_name.value,
        enabled: form.elements.enabled.checked }) });
      document.querySelector('#roster-note').textContent = '已保存；学生ID和历史答卷归属保持不变。';
      await loadRosterClasses(select.value);
    } catch (error) { alert(error.message); } finally { button.disabled = false; }
  }));
}

function initializeRosterManagement() {
  document.querySelector('#roster-editor-class').addEventListener('change', () => loadRosterEditor().catch(e => alert(e.message)));
  document.querySelector('#roster-add-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = event.submitter;
    button.disabled = true;
    try {
      await api('/api/roster-student', { method: 'POST', body: JSON.stringify({
        class_name: form.elements.class_name.value, student_no: form.elements.student_no.value,
        display_name: form.elements.display_name.value,
      }) });
      await loadRosterClasses(form.elements.class_name.value);
      document.querySelector('#roster-note').textContent = '已新增学生。';
      form.elements.student_no.value = ''; form.elements.display_name.value = '';
    } catch (error) { alert(error.message); } finally { button.disabled = false; }
  });
  ['#batch-class-select', '#batch-assignment-select'].forEach(selector => document.querySelector(selector).addEventListener('change', () => {
    resetBatchDraft();
    loadClassExams().catch(e => alert(e.message));
  }));
}
