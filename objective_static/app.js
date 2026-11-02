const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const objectiveBase = document.documentElement.dataset.objectiveBase || window.OBJECTIVE_BASE || "";
const objectivePath = (path) => path.startsWith("/api/") ? `${objectiveBase}${path}` : path;
const workspaceQuery=new URLSearchParams(location.search);
const embedded=workspaceQuery.get('embedded')==='1';
const workspaceExam=workspaceQuery.get('exam_id')||'';
const workspaceQuick=workspaceQuery.get('workspace_mode')==='quick';
let contextFailed=false,taskContextReady=false;
function notifyWorkspace(){if(embedded&&parent!==window)parent.postMessage({type:'objective-workspace',contextReady:taskContextReady,height:Math.ceil(document.body.getBoundingClientRect().height),busy:uploadInProgress||sourceBusy||deleteBusy,jobId:lastJobId,dirty:Boolean($("files")?.files?.length),assessmentId},location.origin)}
let assessmentId = "";
let lastJobId = "";
let deletion=null,deletePreview=null,deleteBusy=false,deleteUncertain=false;
let bulkDeletion=null,bulkPreview=null,bulkUncertain=false;
let selectedTasks=new Map(),availableTasks=new Map();
function forgetCurrentTask(id){if(lastJobId!==id)return;pollRevision++;lastJobId='';currentTaskIds=new Set();defaultResultScope();persistTask();$('task-current').textContent=currentExam()?'当前任务已删除；下面仍显示本考试全部客观题成绩。':'当前任务已删除或不存在；下面显示当前答案键全部答卷。'}
async function showTaskDeletion(id,key,exam){
 if(uploadInProgress||sourceBusy||deleteBusy||deletion||bulkDeletion||contextFailed)return;
 if(!/^[a-f0-9]{32}$/.test(id||'')||!/^[a-f0-9]{32}$/.test(key||'')||(exam&&!/^[a-f0-9]{32}$/.test(exam)))return;
 deletion={id,key,exam:exam||''};$('job-delete-panel').hidden=false;await loadTaskDeletion();$('job-delete-panel').scrollIntoView({block:'center'});$('job-delete-panel').focus({preventScroll:true});
}
async function loadTaskDeletion(){
 if(!deletion||deleteBusy)return;const chosen=deletion;deleteBusy=true;deletePreview=null;deleteUncertain=false;$('job-delete-consent').checked=false;$('job-delete-error').textContent='';$('job-delete-preview').textContent='正在读取实际删除范围…';updateModeUi();
 try{const p=await api('/api/jobs/'+chosen.id+'/delete-preview?'+new URLSearchParams({assessment_id:chosen.key,exam_id:chosen.exam}));if(deletion!==chosen)return;if(p.job_id!==chosen.id||p.assessment_id!==chosen.key||p.exam_id!==chosen.exam||!/^[a-f0-9]{64}$/.test(p.fingerprint))throw new Error('删除预览归属不一致');deletePreview=p;$('job-delete-preview').textContent=`${p.title} · ${p.created_at} · 任务 ${p.job_id.slice(0,8)} · 答案键版本 ${p.answer_version}；答卷 ${p.submissions} 份，有分数 ${p.scored} 份；照片 ${p.photos} 张；复核记录 ${p.reviews} 条；关联 ${p.links} 条。`;
 if(p.blocked)$('job-delete-error').textContent='任务仍在排队或识别，暂不能删除；请等待当前组结束后重新读取。';
 }catch(e){$('job-delete-preview').textContent='尚未取得可确认的删除范围。';$('job-delete-error').textContent='任务可能已删除、归属不符或读取失败；请关闭后刷新任务核实。'+e.message}
 finally{deleteBusy=false;updateModeUi()}
}
function closeTaskDeletion(){if(deleteBusy)return;deletion=null;deletePreview=null;deleteUncertain=false;$('job-delete-panel').hidden=true;$('job-delete-consent').checked=false;updateModeUi()}
async function removeObjectiveTask(){
 if(deleteBusy||deleteUncertain||!deletion||!deletePreview||deletePreview.blocked||!$('job-delete-consent').checked)return;
 const chosen={...deletion},p=deletePreview;let committed=false,cleanupWarning='';deleteBusy=true;$('job-delete-error').textContent='';updateModeUi();
 try{const r=await api('/api/jobs/'+chosen.id+'/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({assessment_id:chosen.key,exam_id:chosen.exam,expected_fingerprint:p.fingerprint,confirmed:true})});if(!r.ok||r.job_id!==chosen.id||r.assessment_id!==chosen.key)throw new Error('删除响应未能确认');committed=true;cleanupWarning=r.warning||'';forgetCurrentTask(chosen.id);deletion=null;deletePreview=null;$('job-delete-panel').hidden=true;$('job-delete-consent').checked=false;$('task-delete-message').textContent='客观题任务 '+chosen.id.slice(0,8)+' 已删除；考试、答案键、主观题和其他任务保留。';await refreshTasks();await refreshResults();if(cleanupWarning)$('task-error').textContent=cleanupWarning+' '+$('task-error').textContent;
 }catch(e){if(committed){$('task-error').textContent=(cleanupWarning+' 删除已完成，但列表刷新失败，请重新刷新核实。').trim()}else{deleteUncertain=true;$('job-delete-consent').checked=false;$('job-delete-error').textContent='删除结果未能确认、范围有变化或任务正在执行。不会自动重试，请重新读取或关闭后刷新任务核实。'+e.message}}
 finally{deleteBusy=false;updateModeUi()}
}

function selectedItems(){return [...selectedTasks.values()].map(j=>({job_id:j.job_id,assessment_id:j.assessment_id,exam_id:j.source_exam_id||''}))}
async function showBulkDeletion(){
 if(deleteBusy||deletion||bulkDeletion||uploadInProgress||sourceBusy||contextFailed||!selectedTasks.size)return;
 bulkDeletion=selectedItems();$('jobs-delete-panel').hidden=false;await loadBulkDeletion();$('jobs-delete-panel').scrollIntoView({block:'center'});$('jobs-delete-panel').focus({preventScroll:true});
}
async function loadBulkDeletion(){
 if(!bulkDeletion||deleteBusy)return;const items=bulkDeletion;deleteBusy=true;bulkPreview=null;bulkUncertain=false;$('jobs-delete-consent').checked=false;$('jobs-delete-error').textContent='';$('jobs-delete-preview').textContent='正在读取整批实际删除范围…';updateModeUi();
 try{const p=await api('/api/jobs/batch-delete-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items})});
 if(items!==bulkDeletion)return;
 const expected=items.map(i=>i.job_id).sort().join(','),actual=(p.items||[]).map(i=>i.job_id).sort().join(',');
 if(actual!==expected||!/^[a-f0-9]{64}$/.test(p.fingerprint))throw new Error('整批删除预览归属不一致');
 bulkPreview=p;$('jobs-delete-preview').textContent=`所选 ${p.jobs} 个任务；答卷 ${p.submissions} 份，有分数 ${p.scored} 份；照片 ${p.photos} 张；复核 ${p.reviews} 条；关联 ${p.links} 条。任务：${p.items.map(i=>i.job_id.slice(0,8)).join('、')}。`;
 if(p.blocked)$('jobs-delete-error').textContent='选择中仍有正在执行的任务，整批不能删除；请取消后刷新列表重新选择。';
 }catch(e){$('jobs-delete-preview').textContent='尚未取得完整可确认范围。';$('jobs-delete-error').textContent='读取失败或任务归属已变化，请刷新核实。'+e.message}
 finally{deleteBusy=false;updateModeUi()}
}
function closeBulkDeletion(){if(deleteBusy)return;bulkDeletion=null;bulkPreview=null;bulkUncertain=false;$('jobs-delete-panel').hidden=true;$('jobs-delete-consent').checked=false;updateModeUi()}
async function removeBulkTasks(){
 if(deleteBusy||bulkUncertain||!bulkDeletion||!bulkPreview||bulkPreview.blocked||!$('jobs-delete-consent').checked)return;
 const items=bulkDeletion,p=bulkPreview;let committed=false,warning='';deleteBusy=true;$('jobs-delete-error').textContent='';updateModeUi();
 try{const r=await api('/api/jobs/batch-delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items,expected_fingerprint:p.fingerprint,confirmed:true})});
 if(!r.ok||r.deleted_job_ids?.slice().sort().join(',')!==items.map(i=>i.job_id).sort().join(','))throw new Error('删除响应未能完整确认');
 committed=true;warning=r.warning||'';for(const id of r.deleted_job_ids)forgetCurrentTask(id);selectedTasks.clear();bulkDeletion=null;bulkPreview=null;$('jobs-delete-panel').hidden=true;$('jobs-delete-consent').checked=false;
 $('task-delete-message').textContent=`已批量删除 ${r.jobs} 个客观题任务；未选任务、共享答案键、考试及主观题保留。`;
 await refreshTasks();await refreshResults();if(warning)$('task-error').textContent=warning;
 }catch(e){if(committed)$('task-error').textContent=(warning+' 删除已完成，列表刷新失败，请重新刷新核实。').trim();else{bulkUncertain=true;$('jobs-delete-consent').checked=false;$('jobs-delete-error').textContent='整批删除结果未能确认或范围已变化；不会自动重试，请重新读取或关闭后刷新。'+e.message}}
 finally{deleteBusy=false;updateModeUi()}
}

let stopUploads=false,chainActive=false,pollRevision=0,currentTaskIds=new Set();
const terminalJob=status=>["completed","needs_review","recoverable","failed"].includes(status);
const taskLabel=status=>({queued:'排队中',processing:'处理中',completed:'已完成',needs_review:'待复核',recoverable:'处理异常，可恢复',failed:'处理失败'}[status]||'状态待核实');
function persistTask(){const q=new URLSearchParams(location.search);if(assessmentId)q.set('assessment_id',assessmentId);if(lastJobId)q.set('job_id',lastJobId);else q.delete('job_id');history.replaceState(null,'',location.pathname+'?'+q);notifyWorkspace()}
function currentExam(){return workspaceExam||$('source-exam').value||''}
function defaultResultScope(){$('result-scope').value=currentExam()?'exam':'assessment'}
function taskScope(){return $('result-scope').value==='job'&&!!lastJobId}
async function refreshTasks(){
 const exam=workspaceExam||$('source-exam').value;const query=exam?'records=all&exam_id='+encodeURIComponent(exam):assessmentId?'assessment_id='+encodeURIComponent(assessmentId):'';
 if(!query){$('task-list').textContent='尚无答案键，请先选择考试或保存答案键。';return}
 try{const d=await api('/api/jobs?'+query);$('task-error').textContent='';availableTasks=new Map(d.jobs.map(j=>[j.job_id,j]));for(const id of selectedTasks.keys())if(!availableTasks.get(id)?.deletable)selectedTasks.delete(id);$('task-list').innerHTML=d.jobs.length?d.jobs.map(j=>`<article class="task-row"><label><input type="checkbox" class="select-task" data-job="${escapeHtml(j.job_id)}" data-deletable="${j.deletable===true}" aria-label="选择客观题任务 ${escapeHtml(j.job_id.slice(0,8))}" ${selectedTasks.has(j.job_id)?'checked':''} ${j.deletable?'':'disabled'}>选择</label><span>${j.job_id===lastJobId?'当前 · ':''}任务 ${escapeHtml(j.job_id.slice(0,8))} · ${escapeHtml(j.created_at)} · ${escapeHtml(taskLabel(j.status))} · 已处理 ${j.processed_submissions}/${j.total_submissions} 份 · 待复核 ${j.review_submissions} · 异常 ${j.error_submissions} · 答案键版本 ${j.answer_version}</span><button class="quiet open-task" data-job="${escapeHtml(j.job_id)}" data-assessment="${escapeHtml(j.assessment_id)}" ${uploadInProgress||sourceBusy||deletion||deleteBusy?'disabled':''}>查看本次任务</button><button class="danger delete-task" data-deletable="${j.deletable===true}" data-job="${escapeHtml(j.job_id)}" data-assessment="${escapeHtml(j.assessment_id)}" data-exam="${escapeHtml(j.source_exam_id||'')}" ${uploadInProgress||sourceBusy||deletion||deleteBusy?'disabled':''}>删除任务</button></article>`).join(''):'还没有已接收任务，请选择照片上传。';$('task-list').querySelectorAll('.select-task').forEach(b=>b.addEventListener('change',()=>{if(b.checked&&availableTasks.get(b.dataset.job)?.deletable)selectedTasks.set(b.dataset.job,availableTasks.get(b.dataset.job));else selectedTasks.delete(b.dataset.job);updateModeUi()}));$('task-list').querySelectorAll('.open-task').forEach(b=>b.addEventListener('click',()=>void openTask(b.dataset.job,b.dataset.assessment)));$('task-list').querySelectorAll('.delete-task').forEach(b=>b.addEventListener('click',()=>void showTaskDeletion(b.dataset.job,b.dataset.assessment,b.dataset.exam)));updateModeUi()}catch(e){$('task-error').textContent='任务列表读取失败；已有成绩保留。'+e.message}
}
async function openTask(id,key){
 if(uploadInProgress||sourceBusy)return;
 if($('files').files.length&&!window.confirm('切换历史任务会清空尚未上传的照片选择，确认切换？'))return;
 const q=new URLSearchParams(location.search);q.set('assessment_id',key);q.set('job_id',id);location.assign(location.pathname+'?'+q)
}
async function restoreTask(id){
 if(!/^[a-f0-9]{32}$/.test(id))throw new Error('任务编号无效，请从历史任务重新打开');
 const d=await api('/api/jobs/'+id+'?assessment_id='+encodeURIComponent(assessmentId));
 if(d.job.assessment_id!==assessmentId)throw new Error('任务与当前答案键不一致，未切换到其他任务');
 lastJobId=id;currentTaskIds=new Set(d.submissions.map(s=>s.submission_id));persistTask();
 if(!terminalJob(d.job.status)){uploadInProgress=true;updateModeUi();void pollJob(id).catch(e=>{$('task-error').textContent='跟踪中断，请点击刷新任务核实。'+e.message}).finally(()=>{uploadInProgress=false;updateModeUi()})}
 else{$('task-current').textContent=`当前任务 ${id.slice(0,8)} · ${taskLabel(d.job.status)} · 已处理 ${d.job.processed_submissions}/${d.job.total_submissions} 份`;await refreshResults()}
}

let templates = [];
let csrfToken = "";
let uploadInProgress = false;
let sourceRevision=0,sourceBusy=false;
function resetAssessment(){selectedTasks.clear();availableTasks.clear();pollRevision++;assessmentId="";lastJobId="";currentTaskIds=new Set();$("task-current").textContent="尚未选择当前任务，可从历史任务打开。";$("export").disabled=true;$("export-xlsx").disabled=true;$("results").textContent="尚未上传本考试答卷。";$("review-queue").textContent="当前没有异常。";$("review-count").textContent="0";$("assessment-state").textContent="尚未选择答案键";defaultResultScope();$('exam-summary').textContent='';$('version-note').textContent='';$('objective-analysis').hidden=true;updateModeUi()}
async function loadSourceExams(){
 if(uploadInProgress||sourceBusy)return;
 const selected=$("source-exam").value,token=sourceRevision;
 try{const data=await api("/api/source-exams");if(token!==sourceRevision)return;$("source-exam").innerHTML='<option value="">独立客观题（保留原方式）</option>'+data.exams.map(e=>`<option value="${escapeHtml(e.exam_id)}">${escapeHtml(e.title)} · ${escapeHtml(e.exam_id.slice(0,8))}${e.ready?'':' · '+escapeHtml(e.reason)}</option>`).join('');$("source-exam").value=selected;if($("source-exam").value!==selected)await selectSourceExam()}
 catch(e){$("source-status").textContent=e.message}
}
// Display only: read the saved key's explicit points; never infer a default or alter grading.
function displayKeyMax(d){if(typeof d.max_score==='number'&&Number.isFinite(d.max_score))return d.max_score;const key=d.answer_key||[];return key.length&&key.every(k=>typeof k.points==='number'&&Number.isFinite(k.points)&&k.points>0)?Number(key.reduce((n,k)=>n+k.points,0).toFixed(6)):'未记录'}
function renderSource(d){const back=document.getElementById('workbench-entry');if(back&&/^[0-9a-f]{12}$/.test(d.assignment_id||'')&&/^[0-9a-f]{32}$/.test(d.exam_id||d.source_exam_id||''))back.href='/next/#/history?assignment='+encodeURIComponent(d.assignment_id)+'&exam='+encodeURIComponent(d.exam_id||d.source_exam_id);$("source-preview").innerHTML=`<p>客观题满分 ${escapeHtml(displayKeyMax(d))}；答案和分值只读，请在主线确认。</p>`+d.answer_key.map(k=>{const q=d.questions.find(q=>q.question_id===k.original_question_id);return `<article><strong>原题${escapeHtml(k.original_question_id)} → 识别题${escapeHtml(k.question_no)}</strong><p>${escapeHtml(q?.stem)}</p><p>${Object.entries(q?.options||{}).map(([a,t])=>escapeHtml(a)+'. '+escapeHtml(t)).join(' · ')}</p><p>答案 ${escapeHtml(k.correct_option)} · ${escapeHtml(k.points)}分</p></article>`}).join('')}
async function selectSourceExam(){
 const token=++sourceRevision,id=$("source-exam").value;resetAssessment();$("source-preview").textContent='';$("use-source-exam").disabled=true;$("use-source-exam").hidden=!id;$("independent-key").hidden=!!id;$("source-status").textContent='';const back=document.getElementById('workbench-entry');if(back)back.href='/next/#/history';if(!id)return;
 try{const d=await api('/api/source-exams/'+encodeURIComponent(id));if(token!==sourceRevision)return;renderSource(d);$("use-source-exam").disabled=false;await refreshResults();await refreshTasks()}
 catch(e){if(token===sourceRevision)$("source-status").textContent=e.message}
}
async function useSourceExam(){
 if(contextFailed||sourceBusy||uploadInProgress||!$("source-exam").value||(embedded&&$("source-exam").value!==workspaceExam))return;
 const id=$("source-exam").value,token=sourceRevision;sourceBusy=true;resetAssessment();updateModeUi();$("use-source-exam").disabled=true;
 try{const d=await api('/api/assessments/from-exam',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({exam_id:id})});if(token!==sourceRevision)return;assessmentId=d.assessment_id;renderSource(d);$("assessment-state").textContent=`已关联正式考试 · 只读答案键版本${d.answer_version} · 满分${d.max_score} · ${d.reused?'复用快照':'生成快照'}`;$("source-status").textContent='后续上传使用此不可变快照；主线修改不会重评已有答卷。';await refreshResults();await refreshTasks();persistTask()}
 catch(e){if(token===sourceRevision)$("source-status").textContent='选择结果未确认，请重新选择同一考试核实；不会自动重试。'+e.message}
 finally{sourceBusy=false;$("use-source-exam").disabled=false;updateModeUi()}
}

function updateModeUi() {
  const locked=uploadInProgress||sourceBusy||!!deletion||!!bulkDeletion||deleteBusy;
  $("source-exam").disabled=embedded||locked;
  $("refresh-source-exams").disabled=locked;
  $("independent-key").disabled=locked||!!$("source-exam").value;
  document.querySelectorAll('.open-task').forEach(b=>b.disabled=locked);
  document.querySelectorAll('.delete-task').forEach(b=>b.disabled=locked||contextFailed||b.dataset.deletable!=='true');
  document.querySelectorAll('.select-task').forEach(b=>b.disabled=locked||contextFailed||b.dataset.deletable!=='true');
  $('task-selection-count').textContent=`已选择${selectedTasks.size}个任务`;
  $('task-select-ended').disabled=locked||contextFailed||![...availableTasks.values()].some(j=>j.deletable);$('task-select-clear').disabled=locked||!selectedTasks.size;
  $('jobs-delete-open').disabled=locked||contextFailed||!selectedTasks.size;
  $('jobs-delete-consent').disabled=deleteBusy||bulkUncertain||!bulkPreview||bulkPreview.blocked;
  $('jobs-delete-submit').disabled=deleteBusy||bulkUncertain||!bulkPreview||bulkPreview.blocked||!$('jobs-delete-consent').checked;
  $('jobs-delete-close').disabled=deleteBusy;$('jobs-delete-reload').disabled=deleteBusy;
  $('delete-current-task').disabled=locked||contextFailed||!lastJobId;
  $('refresh-tasks').disabled=deleteBusy||!!deletion||!!bulkDeletion;
  $('job-delete-consent').disabled=deleteBusy||deleteUncertain||!deletePreview||deletePreview.blocked;
  $('job-delete-submit').disabled=deleteBusy||deleteUncertain||!deletePreview||deletePreview.blocked||!$('job-delete-consent').checked;
  $('job-delete-close').disabled=deleteBusy;$('job-delete-reload').disabled=deleteBusy;
  $('result-scope').disabled=deleteBusy||!!deletion||!!bulkDeletion;for(const o of $('result-scope').options)o.disabled=o.value==='job'?!lastJobId:o.value==='exam'?!currentExam():!assessmentId;if($('result-scope').selectedOptions[0]?.disabled)defaultResultScope();
  $('stop-uploads').disabled=!chainActive||stopUploads;$('stop-uploads').textContent=stopUploads?'已请求停止后续上传':'停止后续上传';
  for(const id of ['mode','template','privacy-status','files']) $(id).disabled=locked;
  document.querySelectorAll('input[name="grouping"]').forEach(input=>input.disabled=locked);
  const vision = $("mode").value === "vision";
  $("template-wrap").hidden = $("mode").value !== "omr";
  $("privacy-wrap").hidden = !vision;
  $("vision-note").hidden = !vision;
  const filesReady = Boolean($("files").files?.length);
  const privacyReady = !vision || $("privacy-status").value !== "unchecked";
  notifyWorkspace();
  $("upload").disabled = contextFailed || locked || !$("mode").value || !assessmentId || !filesReady || !privacyReady;
}

function buildKeyRows() {
  const count = Math.max(1, Math.min(100, Number($("question-count").value) || 20));
  $("question-count").value = count;
  $("answer-key").innerHTML = Array.from({length: count}, (_, index) => `<div class="answer-row"><span class="q-badge">第 ${index + 1} 题</span><select aria-label="第 ${index + 1} 题标准答案" data-key-option="${index + 1}"><option>A</option><option>B</option><option>C</option><option>D</option></select><input aria-label="第 ${index + 1} 题分值" type="number" min="0.01" max="100" step="0.01" value="1" data-key-points="${index + 1}"></div>`).join("");
}

function readKey() {
  return [...document.querySelectorAll("[data-key-option]")].map((select) => ({
    question_no: Number(select.dataset.keyOption), correct_option: select.value,
    points: Number(document.querySelector(`[data-key-points="${select.dataset.keyOption}"]`).value),
  }));
}

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if(embedded&&String(options.method||"GET").toUpperCase()==="POST"){if(!headers.has("X-Workspace-Exam-ID"))headers.set("X-Workspace-Exam-ID",workspaceExam);}
  if (String(options.method || "GET").toUpperCase() === "POST" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(objectivePath(url), {...options, headers, credentials: "same-origin"});
  const type = response.headers.get("content-type") || "";
  const body = type.includes("application/json") ? await response.json() : await response.text();
  if(response.status===401&&embedded)parent.postMessage({type:'objective-workspace',sessionExpired:true},location.origin);
  if (!response.ok) {const error=new Error(typeof body === "object" ? (body.error || `HTTP ${response.status}`) : `HTTP ${response.status}`);error.status=response.status;throw error;}
  return body;
}

async function loadTemplates() {
  const data = await api("/api/templates");
  templates = data.templates;
  $("template").innerHTML = `<option value="auto">自动匹配所有已确认版式</option>` + templates.map((item) => `<option value="${escapeHtml(item.template_id)}">${escapeHtml(item.name)}（${item.config.question_count}题）</option>`).join("");
  $("template").value = "auto";
  return templates;
}

function groupingValue() { return document.querySelector('input[name="grouping"]:checked')?.value || "one_per_image"; }

function setProgress(message, percent = null) {
  const node = $("progress");
  node.classList.remove("hidden");
  node.innerHTML = `${escapeHtml(message)}${percent === null ? "" : `<div class="progressbar"><i style="width:${Math.max(0, Math.min(100, percent))}%"></i></div>`}`;
}

async function refreshResults() {
  if (!assessmentId&&!currentExam()) return;
  const exam=currentExam(),token=sourceRevision,key=assessmentId;
  let data;
  if(exam){
    const [projection,report]=await Promise.all([api('/api/unified-scores?records=all&exam_id='+encodeURIComponent(exam)),api('/api/unified-scores/report?records=all&exam_id='+encodeURIComponent(exam))]);
    if(token!==sourceRevision||key!==assessmentId||exam!==currentExam())return;
    const unique=new Map();
    for(const row of projection.rows){const side=row.objective;if(!side||unique.has(side.id))continue;unique.set(side.id,{...side,submission_id:side.id,anonymous_code:side.anonymous_code||side.id,score:side.valid?side.score:null,eligible:side.valid&&side.complete,historical:side.valid&&!side.complete})}
    data={submissions:[...unique.values()].sort((a,b)=>String(a.created_at).localeCompare(String(b.created_at))||(a.source_order-b.source_order))};
    const waiting=data.submissions.filter(r=>['queued','processing'].includes(r.state)).length;
    const review=data.submissions.filter(r=>!r.valid&&!['queued','processing'].includes(r.state)).length;
    const historical=data.submissions.filter(r=>r.historical).length;
    $('exam-summary').textContent=`本考试来源共 ${data.submissions.length} 份客观题 · 当前版本有效 ${report.objective_summary.count} 份 · 处理中 ${waiting} 份 · 待处理异常 ${review} 份 · 历史版本 ${historical} 份`;
    $('version-note').textContent=`统计使用 ${report.objective_summary.count} 份当前版本有效答卷；待复核、异常及历史版本不混入正确率。客观题平均分 ${report.objective_summary.mean??'—'}；不要求主观题终评；同一考试来源的阅卷记录合并查看，统计按当前评分口径。`;
    $('objective-analysis').hidden=false;
    $('objective-statistics').innerHTML=Object.keys(report.objective_questions).length?'<div class="objective-table-wrap"><table><thead><tr><th>题号</th><th>有效 / 作答人数</th><th>正确率</th><th>平均分</th><th>得分率</th></tr></thead><tbody>'+Object.entries(report.objective_questions).map(([q,v])=>`<tr><td>${escapeHtml(q)}</td><td>${v.count} / ${v.answered_count}</td><td>${(v.correct_rate*100).toFixed(1)}%</td><td>${v.mean.toFixed(2)}</td><td>${v.score_rate===null?'—':(v.score_rate*100).toFixed(1)+'%'}</td></tr>`).join('')+'</tbody></table></div>':'尚无当前版本有效客观题成绩；完成批改后自动统计。';
    if($('result-scope').value==='assessment')data.submissions=data.submissions.filter(r=>r.assessment_id===assessmentId);
    if(taskScope())data.submissions=data.submissions.filter(r=>r.job_id===lastJobId);
  }else{
    data=await api(`/api/submissions?assessment_id=${encodeURIComponent(assessmentId)}`);
    if(token!==sourceRevision||key!==assessmentId)return;
    if(taskScope())data.submissions=data.submissions.filter(row=>currentTaskIds.has(row.submission_id));
    $('exam-summary').textContent=`当前独立答案键共 ${data.submissions.length} 份答卷；不同上传任务共用此列表。`;
    $('objective-analysis').hidden=true;
  }
  $('results-title').textContent=exam?($('result-scope').value==='exam'?'3. 本考试全部客观题成绩':$('result-scope').value==='job'?'3. 当前任务成绩（高级筛选）':'3. 当前答案键成绩（高级筛选）'):'3. 当前独立答案键的客观题成绩';
  if(exam&&$('result-scope').value!=='exam')$('version-note').textContent+=' 当前列表使用高级筛选；统计及导出仍覆盖本考试。';
  $('export-scope-wrap').hidden=!exam;$('export-xlsx').hidden=!exam;
  $('export-xlsx').disabled=data.submissions.length===0;
  const results = $("results");
  if (!data.submissions.length) {
    results.className = "results empty";
    results.textContent = "完成一次上传后显示匿名答卷及各份分数。";
  } else {
    results.className = "results";
    if(exam){
      results.innerHTML='<div class="objective-table-wrap"><table><thead><tr><th>答卷标识</th><th>客观题成绩</th><th>状态与答案键版本</th><th>详情</th></tr></thead><tbody>'+data.submissions.map(row=>`<tr data-submission="${escapeHtml(row.submission_id)}"><td>${escapeHtml(row.anonymous_code)}<small>答卷 ${escapeHtml(row.submission_id.slice(0,8))}</small></td><td>${row.score===null?'—':`${Number(row.score)} / ${Number(row.max_score)}`}</td><td>${escapeHtml(row.historical?'历史版本，不计入当前统计':row.eligible?'已计分':taskLabel(row.state))}<small>答案键版本 ${escapeHtml(row.answer_version)} · ${escapeHtml(row.reason)}</small></td><td><details class="answer-details" data-submission="${escapeHtml(row.submission_id)}" data-mode="${escapeHtml(row.mode)}"><summary>查看逐题记录</summary><div class="answer-details-body muted">展开后读取原始识别及评分记录。</div></details></td></tr>`).join('')+'</tbody></table></div>';
    }else results.innerHTML = data.submissions.map((row) => `<article class="result-card"><div class="result-line"><span class="code">${escapeHtml(row.anonymous_code)}</span><span class="state ${row.state === "completed" ? "" : "review"}">${row.state === "completed" ? (row.mode === "vision" ? "双模型一致，已计分" : "已批改") : row.state === "queued" || row.state === "processing" ? "处理中" : row.state === "recoverable" ? "可重试" : row.state === "failed" ? "失败" : "待复核"}</span><span class="score">${row.score === null ? "—" : `${Number(row.score).toLocaleString()} / ${Number(row.max_score).toLocaleString()} 分`}</span></div><p class="muted">${row.page_count} 页 · 只显示匿名编号</p>${row.mode === "vision" ? `<details class="answer-details" data-submission="${escapeHtml(row.submission_id)}"><summary>查看双模型逐题识别</summary><div class="answer-details-body muted">展开后读取本地识别记录。</div></details>` : ""}</article>`).join("");
    results.querySelectorAll(".answer-details").forEach((details) => details.addEventListener("toggle", async () => {
      if (!details.open || details.dataset.loaded === "true") return;
      const body = details.querySelector(".answer-details-body");
      try {
        const data = await api(`/api/submissions/${encodeURIComponent(details.dataset.submission)}`);
        const answerStatus = (decision) => {
          if (!decision) return "无结果";
          if (decision.status === "answered") return escapeHtml(decision.option || "选项不明");
          if (decision.status === "blank") return "未作答";
          if (decision.status === "question_unmatched") return "未找到题号";
          return "不确定，需复核";
        };
        body.innerHTML = data.answers.map((answer) => `<div class="answer-detail-row"><strong>第 ${answer.question_no} 题</strong>${details.dataset.mode&&details.dataset.mode!=='vision'?'':`<span>AI-A：${answerStatus(answer.model_decisions?.A)}</span><span>AI-B：${answerStatus(answer.model_decisions?.B)}</span>`}<span>最终：${escapeHtml(answer.final_option || "空白/未确认")}</span><span>${answer.awarded_points === null ? "待复核" : `${Number(answer.awarded_points).toLocaleString()} / ${Number(answer.max_points).toLocaleString()} 分`}</span></div>`).join("") || "当前没有逐题记录。";
        details.dataset.loaded = "true";
      } catch (error) { body.textContent = `读取识别记录失败：${error.message}`; }
    }));
  }
  $("export").disabled = data.submissions.length === 0;
  await refreshReviewQueue();
}

async function refreshReviewQueue() {
  const exam=currentExam(),token=sourceRevision,key=assessmentId;
  const data = await api('/api/review-queue?'+(exam?'records=all&exam_id='+encodeURIComponent(exam):'assessment_id='+encodeURIComponent(assessmentId)));
  if(token!==sourceRevision||key!==assessmentId)return;
  if(taskScope())data.items=data.items.filter(row=>currentTaskIds.has(row.submission_id));
  if(exam&&$('result-scope').value==='assessment')data.items=data.items.filter(row=>row.assessment_id===assessmentId);
  $("review-count").textContent = data.items.length;
  const queue = $("review-queue");
  if (!data.items.length) {
    queue.className = "review-queue empty";
    queue.textContent = "当前没有异常。";
    return;
  }
  queue.className = "review-queue";
  queue.innerHTML = data.items.map((item, index) => `<article class="review-card" data-review="${index}">
    <div class="result-line"><strong>${escapeHtml(item.anonymous_code)} · ${item.question_no ? `第 ${item.question_no} 题` : "整份答卷处理异常"}</strong><span class="state review">需要复核${exam?` · 答案键版本 ${escapeHtml(item.answer_version)}`:""}</span></div>
    <p class="muted">${escapeHtml(({model_disagreement:"两模型识别不同",model_uncertain:"至少一个模型无法确定",model_question_unmatched:"至少一个模型未找到该题",model_run_failed:"模型调用失败"})[item.review_reason] || item.review_reason || item.reason || "识别结果需确认")} · ${escapeHtml(item.error_type || item.source || "本地处理")} · ${item.page_count} 页</p>
    ${item.question_no && item.model_decisions ? `<div class="model-decisions"><span>AI-A：${item.model_decisions.A?.status === "answered" ? escapeHtml(item.model_decisions.A.option) : item.model_decisions.A?.status === "blank" ? "未作答" : "不确定/未识别"}${item.model_decisions.A?.mark_evidence ? ` · ${escapeHtml(item.model_decisions.A.mark_evidence)}` : ""}</span><span>AI-B：${item.model_decisions.B?.status === "answered" ? escapeHtml(item.model_decisions.B.option) : item.model_decisions.B?.status === "blank" ? "未作答" : "不确定/未识别"}${item.model_decisions.B?.mark_evidence ? ` · ${escapeHtml(item.model_decisions.B.mark_evidence)}` : ""}</span></div>` : ""}
    ${item.page_no ? `<details><summary>查看第 ${item.page_no} 页原图</summary><img loading="lazy" alt="匿名答卷整页，仅异常复核" src="${objectivePath(`/api/submissions/${encodeURIComponent(item.submission_id)}/pages/${item.page_no}`)}"></details>` : ""}
    ${item.question_no ? `<div class="review-actions"><label class="field">教师确认答案<select class="option">${item.candidate_option ? "" : "<option value='' selected disabled>核对原图后选择</option>"}<option value="__blank">确认空白 / 未作答</option>${["A","B","C","D"].map(option => `<option value="${option}" ${option === item.candidate_option ? "selected" : ""}>${option}${option === item.candidate_option ? "（模型一致）" : ""}</option>`).join("")}</select></label><button class="quiet save-review" data-submission="${escapeHtml(item.submission_id)}" data-question="${item.question_no}" data-exam="${escapeHtml(item.exam_id||'')}">保存确认</button></div>` : `<button class="quiet retry-submission" data-key="${escapeHtml(item.assessment_id)}" data-exam="${escapeHtml(item.exam_id||'')}" data-submission="${escapeHtml(item.submission_id)}">重试此答卷</button>`}
  </article>`).join("");
  queue.querySelectorAll(".save-review").forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest(".review-card");
    const selectedValue = card.querySelector(".option").value;
    if (!selectedValue) { card.querySelector(".option").focus(); return; }
    button.disabled = true;
    try {
      await api(`/api/submissions/${encodeURIComponent(button.dataset.submission)}/answers/${button.dataset.question}/review`, {method:"POST", headers:{"Content-Type":"application/json","X-Workspace-Exam-ID":button.dataset.exam||workspaceExam}, body:JSON.stringify({option:selectedValue === "__blank" ? "" : selectedValue})});
      await refreshResults();
    } catch (error) { alert(error.message); button.disabled = false; }
  }));
  queue.querySelectorAll(".retry-submission").forEach((button) => button.addEventListener("click", async () => {
    if(uploadInProgress||sourceBusy)return;
    if(!window.confirm('确认重试此答卷？视觉模型重试会再次调用服务并可能产生费用。'))return;
    uploadInProgress=true;updateModeUi();button.disabled = true;
    try {
      const result = await api(`/api/submissions/${encodeURIComponent(button.dataset.submission)}/retry`, {method:"POST",headers:{"Content-Type":"application/json","X-Workspace-Exam-ID":button.dataset.exam||workspaceExam},body:"{}"});
      await pollJob(result.job_id,{key:button.dataset.key||assessmentId});
    } catch (error) { alert(error.message); button.disabled = false; }
    finally{uploadInProgress=false;updateModeUi();}
  }));
}

async function pollJob(jobId, {keepLocked=false,prefix='',key=assessmentId}={}) {
  const revision=++pollRevision,track=key===assessmentId;if(track){lastJobId=jobId;persistTask();}
  for (;;) {
    if(revision!==pollRevision)return;
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}?assessment_id=${encodeURIComponent(key)}`);
    const job = data.job;
    if(revision!==pollRevision)return;
    if(job.assessment_id&&job.assessment_id!==key)throw new Error('任务不属于当前答案键');
    if(track)currentTaskIds=new Set((data.submissions||[]).map(s=>s.submission_id));
    if(track)$('task-current').textContent=`当前任务 ${jobId.slice(0,8)} · ${taskLabel(job.status)} · 已处理 ${job.processed_submissions}/${job.total_submissions} 份 · 已完成 ${job.complete_submissions} · 待复核 ${job.review_submissions} · 异常 ${job.error_submissions}`;
    const pct = job.total_submissions ? Math.round(100 * job.processed_submissions / job.total_submissions) : 100;
    setProgress(`${prefix}正在批改 ${job.processed_submissions} / ${job.total_submissions} 份 · 已完成 ${job.complete_submissions} · 待复核 ${job.review_submissions} · 处理异常 ${job.error_submissions}`, pct);
    await refreshResults();await refreshTasks();
    if (["completed", "needs_review", "recoverable", "failed"].includes(job.status)) {
      if(!keepLocked)uploadInProgress = false;
      updateModeUi();
      const grouped = job.grouping === "multi_page_all" ? ` ${job.total_files} 页合并为 1 份答卷` : ` ${job.total_files} 张图片分别生成 ${job.total_submissions} 份答卷`;
      const outcome = job.status === "completed" ? "批改完成" : job.status === "recoverable" ? "部分处理异常，可在复核队列重试" : job.status === "failed" ? "处理失败，请检查复核队列" : "处理完成，异常答卷已进入复核队列";
      setProgress(`${prefix}${outcome}：${grouped}；得分按已保存的标准答案和分值计算。`, 100);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

async function saveAssessment() {
  if(contextFailed||(embedded&&!workspaceQuick)||uploadInProgress||sourceBusy||$("source-exam").value)return;
  const button = $("save-key");
  button.disabled = true;sourceBusy=true;updateModeUi();
  $("assessment-state").textContent = "正在保存答案键…";
  try {
    const data = await api("/api/assessments", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({title:$("assessment-title").value, answer_key:readKey()})});
    resetAssessment();assessmentId = data.assessment_id;persistTask();await refreshResults();await refreshTasks();
    $("assessment-state").textContent = `答案键已保存 · ${data.question_count} 题 · 满分 ${data.max_score} · 版本 ${data.answer_version}`;
    updateModeUi();
  } catch (error) {
    $("assessment-state").textContent = error.message;
  } finally { sourceBusy=false;button.disabled = false;updateModeUi(); }
}

// PNG normalization can be much larger than the uploaded JPEG. Bound both
// request bytes and the decoded RGB representation; never resize or split one paper.
function planUploadBatches(items, grouping) {
  const budget=180*1024*1024, batches=[];
  if(!items.length || items.length>100)throw new Error('每次请选择1—100张照片');
  let batch=[],raw=0,normalized=0;
  for(const item of items){
    if(!item.size || item.size>12*1024*1024)throw new Error('每张照片须大于0且不超过12 MiB，请调整后重新选择');
    if(!Number.isInteger(item.width)||!Number.isInteger(item.height)||item.width<100||item.height<100||item.width*item.height>40000000)throw new Error('图片尺寸不符合要求，请使用清晰且不超过4000万像素的照片');
    // RGB PNG uses 3 bytes/pixel; 4 plus 1 MiB leaves room for filters/zlib/metadata.
    const expanded=item.width*item.height*4+1024*1024;
    if(batch.length&&(raw+item.size>budget||normalized+expanded>budget)){
      if(grouping==='multi_page_all')throw new Error('这组多页答卷处理后过大，无法保持同一份答卷上传。请减少本份页数或自行缩小图片后重新核对；系统不会拆成不同考生。');
      batches.push(batch);batch=[];raw=0;normalized=0;
    }
    batch.push(item.file);raw+=item.size;normalized+=expanded;
  }
  if(batch.length)batches.push(batch);
  return batches;
}
async function inspectUpload(file){
  const url=URL.createObjectURL(file),image=new Image();
  try{await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=()=>reject(new Error('有图片无法解码，请检查所选照片'));image.src=url});return {file,size:file.size,width:image.naturalWidth,height:image.naturalHeight}}
  finally{image.src='';URL.revokeObjectURL(url)}
}
function keepRemainingFiles(files){
  const remaining=new DataTransfer();for(const file of files)remaining.items.add(file);
  $("files").files=remaining.files;
  $("file-count").textContent=files.length?`尚有 ${files.length} 张未提交照片保留在选择框中；已接收部分不会重复提交。`:'所有已接收照片均已从选择框移除；请在下方核实结果。';
}
async function uploadFiles() {
  const files = [...$("files").files];
  if (uploadInProgress || !assessmentId || !files.length) return;
  const mode=$("mode").value, privacy=$("privacy-status").value, grouping=groupingValue(), template=$("template").value, assessment=assessmentId;
  if (mode === "vision" && privacy === "unchecked") {
    $("privacy-status").focus();setProgress('请先确认答卷隐私状态；尚未确认时不会将照片发送给模型。');return;
  }
  uploadInProgress = true;chainActive=true;stopUploads=false;$('upload-stop-note').textContent='停止后不再提交剩余组；已接收任务会继续处理，不撤回已发出的模型请求。';updateModeUi();
  let accepted=0,attemptEnd=0,attempting=false,batches=[];
  try {
    setProgress(`正在检查 ${files.length} 张照片大小；原始分辨率保持不变…`);
    const items=[];for(const file of files)items.push(await inspectUpload(file));
    batches=planUploadBatches(items,grouping);
    for(let index=0;index<batches.length;index++){
      if(stopUploads)break;
      const batch=batches[index],prefix=batches.length>1?`第 ${index+1}/${batches.length} 组 · `:'';
      const form = new FormData();
      form.set('assessment_id',assessment);form.set('mode',mode);form.set('grouping',grouping);
      if(mode==='omr')form.set('template_id',template);
      if(mode==='vision')form.set('privacy_status',privacy);
      for(const file of batch)form.append('files',file,file.name);
      setProgress(`${prefix}正在上传 ${batch.length} 张原图；整批共 ${files.length} 张…`);
      attemptEnd=accepted+batch.length;attempting=true;
      const job=await api('/api/uploads',{method:'POST',body:form});
      accepted=attemptEnd;attempting=false;lastJobId=job.job_id;persistTask();
      keepRemainingFiles(files.slice(accepted));
      await pollJob(job.job_id,{keepLocked:true,prefix});
    }
    if(stopUploads){setProgress(`已停止后续上传；已接收 ${accepted} 张，剩余 ${files.length-accepted} 张保留在选择框。当前已接收任务继续处理。`);return}
    $("file-count").textContent=`已上传 ${accepted} 张照片，分 ${batches.length} 组接收；结果与待复核项集中显示在下方。`;
    setProgress(`整批处理结束：已接收 ${accepted} 张照片；请查看下方成绩与待复核项。`,100);
  } catch (error) {
    if(attempting){
      // No upload idempotency on this API. Never resend a possibly accepted chunk.
      keepRemainingFiles(files.slice(attemptEnd));
      setProgress(`本组接收结果未能确认，已停止后续上传且不会自动重试。已确认接收 ${accepted} 张；当前 ${attemptEnd-accepted} 张请先核实下方结果，勿直接重复上传。其余未提交照片保留。${error.message}`);
    }else setProgress(`已确认接收 ${accepted} 张；后续上传已停止。${error.message}。未提交照片仍保留，请核实后继续。`);
  } finally {
    uploadInProgress = false;chainActive=false;updateModeUi();await refreshTasks();
  }
}

async function setupTemplate() {
  const file = $("calibration").files[0];
  if (!file) { $("template-state").textContent = "请先选择校准页。"; return; }
  const form = new FormData();
  form.set("sample", file, file.name);
  form.set("name", $("new-template-name").value);
  form.set("question_count", $("new-template-count").value);
  form.set("columns", $("new-template-columns").value);
  form.set("signature", $("new-template-signature").value);
  form.set("confirm", "true");
  const button = $("confirm-template");
  button.disabled = true;
  $("template-state").textContent = "正在验证定位点和版式签名…";
  try {
    const result = await api("/api/templates/setup", {method:"POST", body:form});
    await loadTemplates();
    $("template").value = result.template_id;
    $("template-state").textContent = `已确认并保存 ${result.name} · 配置哈希 ${result.config_sha256.slice(0,16)}…，之后可直接复用。`;
  } catch (error) { $("template-state").textContent = `未确认：${error.message}`; }
  finally { button.disabled = false; }
}

async function initialize() {
  if(embedded){document.body.classList.add('embedded');document.querySelector('#setup-panel h2').textContent=workspaceQuick?'1. 设置客观题答案键':'1. 本考试已确认答案与分值';new ResizeObserver(notifyWorkspace).observe(document.body);}
  buildKeyRows();
  $("source-exam").addEventListener('change',selectSourceExam);
  $("refresh-source-exams").addEventListener('click',loadSourceExams);
  $("use-source-exam").addEventListener('click',useSourceExam);
  const session = await fetch("/api/auth/session", {credentials:"same-origin", cache:"no-store"});
  if (!session.ok) {if(embedded)parent.postMessage({type:'objective-workspace',sessionExpired:true},location.origin);else window.location.replace('/login');return;}
  const sessionData = await session.json();
  csrfToken = sessionData.csrf_token || "";
  $("build-key").addEventListener("click", buildKeyRows);
  $("save-key").addEventListener("click", saveAssessment);
  $('delete-current-task').addEventListener('click',()=>void showTaskDeletion(lastJobId,assessmentId,workspaceExam||$('source-exam').value||''));
  $('task-select-ended').addEventListener('click',()=>{selectedTasks=new Map([...availableTasks].filter(([,j])=>j.deletable));document.querySelectorAll('.select-task').forEach(b=>b.checked=selectedTasks.has(b.dataset.job));updateModeUi()});
  $('task-select-clear').addEventListener('click',()=>{selectedTasks.clear();document.querySelectorAll('.select-task').forEach(b=>b.checked=false);updateModeUi()});
  $('jobs-delete-open').addEventListener('click',()=>void showBulkDeletion());$('jobs-delete-consent').addEventListener('change',updateModeUi);$('jobs-delete-reload').addEventListener('click',()=>void loadBulkDeletion());$('jobs-delete-close').addEventListener('click',closeBulkDeletion);$('jobs-delete-submit').addEventListener('click',()=>void removeBulkTasks());
  $('job-delete-consent').addEventListener('change',updateModeUi);$('job-delete-reload').addEventListener('click',()=>void loadTaskDeletion());$('job-delete-close').addEventListener('click',closeTaskDeletion);$('job-delete-submit').addEventListener('click',()=>void removeObjectiveTask());
  $('stop-uploads').addEventListener('click',()=>{stopUploads=true;$('upload-stop-note').textContent='已请求停止后续上传。已接收组会继续处理；未提交照片仍保留，请勿刷新。';updateModeUi()});
  $('refresh-tasks').addEventListener('click',async()=>{await refreshTasks();try{if(lastJobId&&!uploadInProgress)await restoreTask(lastJobId);else await refreshResults()}catch(e){if(e.status===404&&lastJobId){forgetCurrentTask(lastJobId);try{await refreshResults();$('task-error').textContent='原任务已不存在或不可访问，已清除失效任务入口；未自动选择其他任务。'}catch{$('task-error').textContent='原任务已不存在，成绩列表刷新失败，请稍后重新读取。'}}else $('task-error').textContent=e.message}});
  $('result-scope').addEventListener('change',()=>void refreshResults().catch(e=>{$('task-error').textContent=e.message}));
  $("upload").addEventListener("click", uploadFiles);
  $("confirm-template").addEventListener("click", setupTemplate);
  $("files").addEventListener("change", () => {
    const files = [...$("files").files];
    $("file-count").textContent = files.length ? `已选 ${files.length} 张照片；${groupingValue() === "one_per_image" ? "默认将每张分成独立答卷。" : "所选照片将合并为一份多页答卷。"}` : "尚未选择照片";
    updateModeUi();
  });
  document.querySelectorAll('input[name="grouping"]').forEach((input) => input.addEventListener("change", () => $("files").dispatchEvent(new Event("change"))));
  $("mode").addEventListener("change", updateModeUi);
  $("privacy-status").addEventListener("change", updateModeUi);
  function exportResults(kind){if(contextFailed)return;const exam=currentExam();window.location.href=objectivePath(exam?'/api/unified-scores/export.'+kind+'?records=all&side=objective&exam_id='+encodeURIComponent(exam)+'&scope='+$('export-scope').value:`/api/export.csv?assessment_id=${encodeURIComponent(assessmentId)}${taskScope()?'&job_id='+encodeURIComponent(lastJobId):''}`)}
  $("export").addEventListener("click",()=>exportResults('csv'));
  $('export-xlsx').addEventListener('click',()=>exportResults('xlsx'));
  $('refresh-results').addEventListener('click',()=>void refreshResults().catch(e=>{$('task-error').textContent=e.message}));
  try {
    const status = await api("/api/status");
    const vision = status.vision_grading || {};
    const visionOption = [...$("mode").options].find((item) => item.value === "vision");
    if (!vision.enabled) {
      visionOption.remove();
      $("mode").value = "omr";
      $("runtime-status").textContent = `${status.recognizer} 可用 · 当前服务未接入视觉双评；可使用已确认 OMR 模板`;
    } else {
      const modelLabels = (vision.models || []).map((model) => `${model.provider} ${model.model}`).join(" + ");
      if (vision.available) {
        visionOption.disabled = false;
        visionOption.textContent = `视觉模型双评 · ${modelLabels}`;
        $("mode").value = "vision";
        $("runtime-status").textContent = `默认双评：${modelLabels}；两侧 Key 已配置`;
      } else {
        visionOption.disabled = true;
        visionOption.textContent = `视觉模型双评 · ${modelLabels || "尚未配置"}（请联系管理员配置模型密钥）`;
        $("mode").value = "";
        $("runtime-status").textContent = "视觉双评已接入，但模型 Key 尚未全部配置；请联系管理员完成配置后刷新页面。";
      }
    }
    await loadTemplates();
    await loadSourceExams();
    const query = new URLSearchParams(window.location.search);
    const requestedExam = query.get('exam_id');
    if(embedded&&!workspaceQuick&&!requestedExam)throw new Error('正式考试上下文缺失');
    if(embedded&&workspaceQuick&&requestedExam)throw new Error('快速匿名模式不能隐式绑定正式考试');
    const savedAssessment = query.get('assessment_id');
    if (!savedAssessment && requestedExam) {
      if (!/^[0-9a-f]{32}$/.test(requestedExam) || !Array.from($("source-exam").options).some(o => o.value === requestedExam)) {
        throw new Error('所选正式考试不存在或无权访问，请重新选择考试');
      }
      $("source-exam").value = requestedExam;
      await selectSourceExam();
    }
    if(savedAssessment&&!/^[0-9a-f]{32}$/.test(savedAssessment))throw new Error('答案键编号无效');
    if (savedAssessment) {
      const saved = await api(`/api/assessments/${savedAssessment}`);
      if((requestedExam&&(saved.source_exam_id||'')!==requestedExam)||(embedded&&(saved.source_exam_id||'')!==workspaceExam))throw new Error('答案键快照与当前考试不一致');
      assessmentId = saved.assessment_id;
      if (saved.source_exam_id) {
        $("source-exam").value = saved.source_exam_id;
        $("independent-key").hidden = true;
        $("use-source-exam").hidden = true;
        renderSource({...saved, questions:[]});
      }
      if(!saved.source_exam_id){
        $("assessment-title").value=saved.title||'';$("question-count").value=saved.answer_key.length;buildKeyRows();
        for(const row of saved.answer_key){document.querySelector(`[data-key-option="${row.question_no}"]`).value=row.correct_option;document.querySelector(`[data-key-points="${row.question_no}"]`).value=row.points;}
      }
      $("assessment-state").textContent = `已打开历史答案键快照 · 版本 ${saved.answer_version} · 满分 ${displayKeyMax(saved)}`;
      defaultResultScope();await refreshResults();
    }
  } catch (error) { contextFailed=true;assessmentId="";$("use-source-exam").disabled=true;$("save-key").disabled=true;$("runtime-status").textContent = `服务状态异常：${error.message}`; }
  if(!contextFailed){await refreshTasks();const job=workspaceQuery.get('job_id');if(job){try{await restoreTask(job)}catch(e){$('task-error').textContent=e.message;contextFailed=true}}}
  taskContextReady=!contextFailed;updateModeUi();
}

document.addEventListener("DOMContentLoaded", initialize);

window.addEventListener('beforeunload',e=>{if(uploadInProgress||sourceBusy||deleteBusy||$('files')?.files?.length){e.preventDefault();e.returnValue='';}});
