<script setup lang="ts">
import ProjectRubricEditor from './ProjectRubricEditor.vue'
import {examHref,type ExamContext} from './navigation'
import { computed, onUnmounted, ref } from 'vue'
import {reorder,newSub,moveSub,mergeNext,splitSub,type Major,type Sub, blockers, conflictEvidence, conflictIds, createExam, editDraft, filePayload, parseExam, parseAiExam, previewAiExam, preserveAiExamEdits, references, scoreTotals, sumWarning, type AiPreview, type AiSummary, type Confirmation, type Created, type Draft } from './examImport'
const emit=defineEmits<{created:[value:ExamContext]}>()
const docx = ref<File>(), pdf = ref<File>(), draft = ref<Draft>(), edit = ref<Confirmation>()
const draftId = ref(''), busy = ref(false), error = ref(''), created = ref<Created>(), ambiguous = ref(false)
const aiPreview=ref<AiPreview>(),aiConsent=ref(false),aiSummary=ref<AiSummary>()
const bulkObjectivePoints=ref<number|string>('')
const totals=computed(()=>edit.value?scoreTotals(edit.value):null)
function applyObjectivePoints(){const value=Number(bulkObjectivePoints.value);if(!edit.value||!draft.value||!Number.isFinite(value)||value<=0||value>100)return;for(const q of draft.value.objective_questions)edit.value.objective_points[q.question_id]=value;edit.value.teacher_confirmed=false}
function structural(action:()=>void){try{action();revised()}catch(e){error.value=e instanceof Error?e.message:String(e)}}
function renameMajor(m:Major,label:string){const old=m.label;m.label=label;for(const q of m.sub_questions)if(q.question_id.startsWith(old+'.'))q.question_id=label+q.question_id.slice(old.length);revised()}
function switchMode(q:Sub){if(q.scoring_mode==='holistic'&&!q.holistic)q.holistic={levels:[],dimensions:'',notes:''};revised()}
let revision = 0
const problems = computed(() => draft.value && edit.value ? blockers(draft.value,edit.value) : [])
const sourceConflictEvidence = computed(() => draft.value ? conflictEvidence(draft.value) : [])
function questionIssues(id:string,objective=false){return problems.value.filter(p=>objective?p.includes('客观题'+id)&&!/^\d/.test(p.split('客观题'+id)[1]||''):p.startsWith(id+'的')||p.startsWith(id+'需要')||p.startsWith(id+'封顶')||p.startsWith(id+'候选'))}
function focusIssue(issue:string){const nodes=[...document.querySelectorAll<HTMLElement>('[data-question]')];const node=nodes.find(el=>questionIssues(el.dataset.question||'',el.dataset.kind==='objective').includes(issue));const target=(node?.dataset.kind==='objective'?node.querySelector<HTMLElement>(issue.includes('分值')?'input[id^="exam-points-"]':'select[id^="exam-answer-"]'):node?.querySelector<HTMLElement>('input,select,textarea'))||document.querySelector<HTMLElement>('[name="teacher-confirmed"]');target?.focus();target?.scrollIntoView({block:'center'})}
function sourceLabel(value?:string){return ({not_applicable:'单一文件来源',matched:'来源已匹配',needs_teacher_confirmation:'需要核对两份文件来源',mismatch:'来源不一致'} as Record<string,string>)[value||'']||'按导入提示核对来源'}
function warningLabel(value:string){if(value.startsWith('layout_source_missing:'))return '尚无最终印发版 PDF，无法自动配准裁切；可继续使用原照片批改。';if(value.startsWith('rubric_needs_teacher_review:'))return '评分点为导入草稿，请逐题核对计分方式、文字和分值。';return value.replace(/^[a-z_]+:\s*/,'').replaceAll('rubric','评分标准').replaceAll('reference','参考页')}
function reset() { revision++; draft.value=undefined; edit.value=undefined; draftId.value=''; created.value=undefined; error.value=''; ambiguous.value=false;aiPreview.value=undefined;aiConsent.value=false;aiSummary.value=undefined }
function selectFile(event: Event, kind: 'docx'|'pdf') {
  reset(); const file = (event.target as HTMLInputElement).files?.[0]
  if (kind === 'docx') docx.value=file; else pdf.value=file
}
function revised(event?: Event) {
  const name = (event?.target as HTMLInputElement|undefined)?.name
  if (edit.value && name !== 'teacher-confirmed' && name !== 'source-confirmed') edit.value.teacher_confirmed=false
}
async function parse() {
  if (busy.value) return
  reset(); const token=revision; busy.value=true
  try {
    const payload = { ...(docx.value ? await filePayload(docx.value,'docx') : {}), ...(pdf.value ? await filePayload(pdf.value,'pdf') : {}) }
    if (!docx.value && !pdf.value) throw new Error('请选择Word或PDF。')
    const result=await parseExam(payload)
    if(token!==revision)return
    draft.value=result.draft; edit.value=editDraft(result.draft); draftId.value=result.draft_id
  } catch { if(token===revision)error.value='文件解析未完成。请检查文件格式、大小及是否含可提取文字；如仍失败，可在系统辅助工具中手动录入评分标准。' }
  finally { if(token===revision)busy.value=false }
}
async function confirm() {
  if(busy.value || created.value || ambiguous.value || !edit.value || !draft.value || problems.value.length)return
  busy.value=true; error.value=''; const token=revision
  // One explicit create request. Never retry an uncertain write outcome.
  try { const result=await createExam(draftId.value,structuredClone(JSON.parse(JSON.stringify(edit.value)))); if(token===revision){created.value=result;emit('created',{assignmentId:result.assignment.id,examId:result.exam_id})} }
  catch { if(token===revision){ambiguous.value=true;error.value='创建结果未能确认，已停止重复提交。请先到历史记录核实是否已创建；不要立即重新导入创建。'} }
  finally { if(token===revision)busy.value=false }
}
async function previewAi(){
  if(busy.value||!draftId.value||!draft.value||created.value||ambiguous.value)return
  const token=revision,id=draftId.value;busy.value=true;error.value='';aiPreview.value=undefined;aiConsent.value=false
  try{const p=await previewAiExam(id);if(token!==revision)return;if(p.draft_id!==id||!p.preview_sha256||!p.model||!p.destination_host)throw new Error('invalid preview');aiPreview.value=p}
  catch{if(token===revision)error.value='AI解析预览未完成。请检查AI-A配置、访问权限及原文件是否含身份信息；未发送模型。'}
  finally{if(token===revision)busy.value=false}
}
async function runAi(){
  if(busy.value||!aiConsent.value||!aiPreview.value||!draft.value||!edit.value||created.value||ambiguous.value)return
  const token=revision,id=draftId.value,previous=draft.value,p=aiPreview.value
  if(p.draft_id!==id)return
  busy.value=true;error.value='';edit.value.teacher_confirmed=false
  try{const result=await parseAiExam(id,p.preview_sha256);if(token!==revision||draftId.value!==id)return;const preserved=preserveAiExamEdits(result.draft,previous,edit.value!);draft.value=result.draft;edit.value=preserved;aiSummary.value=result.summary}
  catch{if(token===revision)error.value='AI解析结果未能确认，未自动重试或创建考试。当前人工编辑保留；请核实草稿，继续前需重新预览并确认。'}
  finally{if(token===revision){busy.value=false;aiPreview.value=undefined;aiConsent.value=false}}
}
onUnmounted(() => { revision++; docx.value=undefined; pdf.value=undefined; draft.value=undefined; edit.value=undefined })
</script>
<template>
  <section class="exam-create"><h2>新建考试</h2><ol class="workflow-steps" aria-label="创建考试步骤"><li :aria-current="!draft&&!created?'step':undefined">1 导入试题</li><li :aria-current="draft&&!created?'step':undefined">2 核对答案与分值</li><li :aria-current="created?'step':undefined">3 考试准备完成</li></ol><p>导入文件后逐项核对答案与分值，再确认创建。普通解析不调用模型；原照片批改无需配置答题区。</p>
    <details class="import-source" :open="!draft&&!created"><summary>试题文件{{docx?' · '+docx.name:pdf?' · '+pdf.name:' · Word / PDF'}}</summary><fieldset :disabled="busy || ambiguous || !!created"><legend>试题文件</legend>
      <div class="file-fields"><label>Word试题（.docx，最多10MB）<input type="file" accept=".docx" @change="selectFile($event,'docx')"></label>
      <label>PDF试题（.pdf，最多26MB）<input type="file" accept=".pdf" @change="selectFile($event,'pdf')"></label></div>
      <button class="primary" @click="parse" :disabled="!docx && !pdf">{{ busy ? '正在处理…' : '解析为草稿' }}</button>
    </fieldset></details>
    <p v-if="error" role="alert">{{ error }}</p>
    <article v-if="created" aria-live="polite"><h3>考试已创建</h3><p>{{ created.assignment.name }} · 客观题{{ created.assignment.objective_count }} · 主观题{{ created.assignment.subjective_count }}</p><p>考试编号：{{ created.exam_id }}</p><p v-if="created.assignment.score_totals">客观题满分{{created.assignment.score_totals.objective}} · 主观题满分{{created.assignment.score_totals.subjective}} · 整卷满分{{created.assignment.score_totals.paper}}</p><div class="context-actions"><a v-if="created.assignment.subjective_count" class="button" :href="examHref('upload',{assignmentId:created.assignment.id,examId:created.exam_id})">上传本考试主观题答卷</a><a v-if="created.assignment.objective_count" :href="examHref('objective',{assignmentId:created.assignment.id,examId:created.exam_id})">使用本考试批改客观题</a></div><p v-for="warning in created.warnings" :key="warning">{{ warning }}</p><a :href="examHref('history',{assignmentId:created.assignment.id,examId:created.exam_id})">查看本考试终评与成绩</a></article>
    <a v-if="ambiguous" href="#/history">先检查历史记录</a>
    <details v-if="draft && edit && !created && !ambiguous" class="ai-tools"><summary>可选：AI辅助解析题号与答案</summary><section aria-label="AI题号答案解析">
      <h3>AI解析题号与答案（可选）</h3><p>使用系统管理中已配置的AI-A，仅发送Word/PDF提取文字，不发送页面图片。AI只补题号、答案和参考答案草稿，不生成评分点、不自动创建考试；冲突与评分规则仍需教师核对。</p>
      <button :disabled="busy" @click="previewAi">AI 解析题号/答案</button>
      <section v-if="aiPreview" aria-label="AI文字外发确认"><h4>确认发送试卷文字</h4><p>{{aiPreview.source_label}} · 约{{aiPreview.text_chars}}字符 · {{aiPreview.provider}} / {{aiPreview.model}} · {{aiPreview.destination_host}}</p><p>{{aiPreview.note}}</p><p>不会发送页面图片。请确认原文不含姓名、学号等个人信息；模型调用可能产生费用。</p><label class="check"><input type="checkbox" v-model="aiConsent" :disabled="busy">我已核对文字来源和模型，确认原文无个人信息，同意发送文字解析</label><button :disabled="busy||!aiConsent" @click="runAi">确认发送文字并解析</button> <button :disabled="busy" @click="aiPreview=undefined;aiConsent=false">取消AI解析</button></section>
      <p v-if="aiSummary" role="status">AI识别客观题{{aiSummary.objective.ai_count}}道，新增{{aiSummary.objective.added.length}}道；主观题{{aiSummary.subjective.ai_count}}道，新增{{aiSummary.subjective.added.length}}道；参考答案冲突{{aiSummary.objective.answer_conflicts.length+aiSummary.subjective.answer_conflicts.length}}处。人工编辑已保留，请重新核对并勾选教师确认。</p>
    </section></details>
    <form v-if="draft && edit && !created" @submit.prevent="confirm" @input="revised" @change="revised">
      <fieldset :disabled="busy || ambiguous"><legend>教师核对草稿</legend>
        <p>草稿尚未创建考试 · {{sourceLabel(draft.source.source_pair_status)}}</p>
        <details v-if="draft.warnings?.length" class="import-notes"><summary>导入说明（{{draft.warnings.length}}条）· 查看需注意事项</summary><p v-for="(warning,i) in draft.warnings" :key="i">{{warningLabel(warning)}}</p></details>
        <ul v-if="sourceConflictEvidence.length" class="exam-source-conflicts"><li v-for="(evidence,i) in sourceConflictEvidence" :key="i">{{ evidence }}</li></ul>
        <details v-if="references(draft,draftId).length"><summary>查看试题PDF参考页（不含答案页）</summary><figure v-for="page in references(draft,draftId)" :key="page.page"><figcaption>第{{ page.page }}页</figcaption><img :src="page.url" :alt="'试题参考第'+page.page+'页'" loading="lazy" style="max-width:100%;height:auto"></figure></details>
        <p v-if="draft.source.source_pair_status==='mismatch'" role="alert">两个来源不一致。请清除其中一个文件并重新解析，不能通过勾选绕过。</p>
        <label>考试名称<input v-model="edit.exam_name" required></label>
        <dl v-if="totals" class="score-totals"><div><dt>客观题满分</dt><dd>{{totals.objective??'待确认'}}</dd></div><div><dt>主观题满分</dt><dd>{{totals.subjective??'待确认'}}</dd></div><div><dt>整卷满分</dt><dd>{{totals.paper??'待确认'}}</dd></div></dl>
<details v-if="draft.source_blocks?.length"><summary>查看原始提取文字（材料、表格与参考范例）</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{draft.source_blocks.join('\n')}}</pre></details>
<details v-if="draft.source_tables?.length"><summary>查看原始表格行列（只读参考）</summary><div v-for="(table,ti) in draft.source_tables" :key="ti" style="overflow-x:auto"><table><tbody><tr v-for="(row,ri) in table" :key="ri"><td v-for="(cell,ci) in row" :key="ci" style="white-space:pre-wrap">{{cell}}</td></tr></tbody></table></div></details>
        <h3>客观题</h3><div v-if="draft.objective_questions.length"><label>客观题批量分值<input v-model="bulkObjectivePoints" type="number" min="0" max="100" step="any"></label><button type="button" @click="applyObjectivePoints">应用分值到全部客观题</button><p>请明确填写分值；批量设置后仍可逐题调整。</p></div><p v-if="!draft.objective_questions.length">未识别到客观题。</p>
        <div class="objective-draft-grid"><article v-for="q in draft.objective_questions" :key="q.question_id" class="point-record objective-draft" :data-question="q.question_id" data-kind="objective"><h4>客观题{{ q.question_id }}</h4><p v-for="issue in questionIssues(q.question_id,true)" :key="issue" class="field-error">{{issue}}</p><p>{{ q.stem }}</p>
          <p v-for="(text,key) in q.options" :key="key">{{ key }}. {{ text }}</p>
          <label :for="'exam-answer-'+q.question_id">第{{ q.question_id }}题正确答案</label><select :id="'exam-answer-'+q.question_id" v-model="edit.objective_answer_key[q.question_id]"><option value="">待确认</option><option v-for="a in ['A','B','C','D']" :key="a">{{ a }}</option></select>
          <label :for="'exam-points-'+q.question_id">第{{q.question_id}}题分值<input :id="'exam-points-'+q.question_id" v-model="edit.objective_points[q.question_id]" type="number" min="0" max="100" step="any" required></label>
          <label v-if="conflictIds(draft).includes(q.question_id)">第{{ q.question_id }}题来源答案冲突，请独立裁决<select v-model="edit.answer_conflict_resolutions[q.question_id]"><option value="">请选择最终答案</option><option v-for="a in ['A','B','C','D']" :key="a">{{ a }}</option></select></label>
        </article>
        </div><h3>主观题</h3><p v-if="!edit.material_questions.length">未识别到主观题。</p>
        <button type="button" @click="edit.material_questions.push({label:String(Math.max(0,...edit.material_questions.map(m=>Number(m.label)||0))+1),prompt:'',sub_questions:[]});revised()">添加大题</button><p>拆分前请在设问中换行；拆分后须重新分配满分、参考答案和评分标准。合并会保留两题文字并合计满分；跨大题移动会携带原材料。所有操作只影响本草稿。</p><article v-for="(major,mi) in edit.material_questions" :key="mi" class="point-record" :data-major="major.label">
          <button type="button" :disabled="mi===0" @click="structural(()=>reorder(edit!.material_questions,mi,-1))">上移大题</button><button type="button" :disabled="mi===edit.material_questions.length-1" @click="structural(()=>reorder(edit!.material_questions,mi,1))">下移大题</button><label>大题编号<input :value="major.label" @change="renameMajor(major,($event.target as HTMLInputElement).value)"></label><button type="button" @click="major.sub_questions.push(newSub(major.label,major.sub_questions));revised()">添加小题</button><button type="button" @click="edit.material_questions.splice(mi,1);revised()">删除大题</button><label>第{{ major.label }}题材料<textarea v-model="major.prompt" rows="4"></textarea></label>
          <article v-for="(q,qi) in major.sub_questions" :key="qi" class="point-record" :data-question="q.question_id"><h4>小题{{ q.question_id }}</h4><p v-for="issue in questionIssues(q.question_id)" :key="issue" class="field-error">{{issue}}</p>
            <button type="button" :disabled="qi===0" @click="structural(()=>reorder(major.sub_questions,qi,-1))">上移小题</button><button type="button" :disabled="qi===major.sub_questions.length-1" @click="structural(()=>reorder(major.sub_questions,qi,1))">下移小题</button><label>小题编号<input v-model="q.question_id"></label><label>题型<select aria-label="题型" v-model="q.question_type"><option value="subjective">材料题</option><option value="essay">开放性小论文</option><option value="explanation">历史解释题</option></select></label>
            <button type="button" @click="major.sub_questions.splice(qi,1);revised()">删除小题</button><button type="button" @click="structural(()=>splitSub(major,q))">拆分小题</button><button type="button" :disabled="qi===major.sub_questions.length-1" @click="structural(()=>mergeNext(major,qi))">与下一小题合并</button>
            <label>移动到大题<select aria-label="移动到大题" :value="mi" @change="structural(()=>moveSub(major,edit!.material_questions[Number(($event.target as HTMLSelectElement).value)]!,q))"><option v-for="(dest,di) in edit.material_questions" :key="di" :value="di">{{dest.label}}</option></select></label>
            <label>{{ q.question_id }}设问<textarea v-model="q.question" rows="3" required></textarea></label>
            <label>{{ q.question_id }}参考答案<textarea v-model="q.reference_answer" rows="3" :required="q.scoring_mode!=='holistic'"></textarea></label>
            <label>{{ q.question_id }}满分<input v-model="q.max_score" type="number" min="0" step="any" required></label>
            <label :for="'exam-scoring-mode-'+q.question_id">{{q.question_id}}评分方式</label><select :id="'exam-scoring-mode-'+q.question_id" v-model="q.scoring_mode" @change="switchMode(q)"><option value="weighted">按评分项分值累计</option><option value="point_bank">按得分点封顶</option><option value="holistic">开放题评价（整体分档／分项给分）</option></select>
            <ProjectRubricEditor v-if="q.scoring_mode==='holistic'&&q.holistic?.aggregation==='sum_projects'" :config="q.holistic"/><section v-else-if="q.scoring_mode==='holistic'&&q.holistic"><p role="status">两个AI分别按教师确认的标准定档并给出建议分；评价维度不机械累计。</p><label>评分步长<input type="number" min="0.01" step="any" v-model="q.holistic.score_step" required></label><label>评价维度（用顿号或分号分隔）<textarea v-model="q.holistic.dimensions" @input="q.holistic.dimension_definitions=undefined" required></textarea></label><label>评分细则与补充说明<textarea v-model="q.holistic.notes"></textarea></label><article v-for="(level,li) in q.holistic.levels" :key="li"><label>最低分<input v-model="level.min_score" type="number" min="0" step="any"></label><label>最高分<input v-model="level.max_score" type="number" min="0" step="any"></label><label>本档评价标准<textarea v-model="level.description" required></textarea></label><button type="button" @click="q.holistic.levels.splice(li,1);revised()">删除档次</button></article><button type="button" @click="q.holistic.levels.push({min_score:null,max_score:null,description:''});revised()">添加档次</button></section>
            <template v-if="q.scoring_mode!=='holistic'">
            <label v-if="q.scoring_mode==='point_bank'">{{q.question_id}}每点分值<input v-model="q.point_value" type="number" min="0.5" step="any" required></label>
            <p v-if="q.scoring_mode==='point_bank'">按不同有效要点计分，最多{{q.max_score}}分；候选点可以多于满分所需，各评分点分值须等于每点分值。系统不会自动改写已有评分点。</p>
            <p v-else>各评分项分值合计应等于本题满分。若规则为答中任意足够数量的要点即可满分，请明确选择“按得分点封顶”。</p>
            <div v-for="(point,i) in q.rubric_draft" :key="i" class="point-record">
              <label>{{ q.question_id }}评分点{{ i+1 }}文字<textarea v-model="point.label" required></textarea></label>
              <label>{{ q.question_id }}评分点{{ i+1 }}分值<input v-model="point.score" type="number" min="0" step="any" required></label>
              <button class="secondary" type="button" @click="q.rubric_draft.splice(i,1);edit.teacher_confirmed=false">删除此评分点</button>
            </div>
            <button type="button" @click="q.rubric_draft.push({label:'',score:null});edit.teacher_confirmed=false">添加评分点</button>
            </template><p v-if="sumWarning(q)" role="alert">{{ sumWarning(q) }}</p>
          </article>
        </article>
        <details><summary>实时预览（当前编辑内容）</summary><section v-for="(m,i) in edit.material_questions" :key="i"><h4>第{{m.label}}题</h4><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{m.prompt}}</pre><article v-for="(q,j) in m.sub_questions" :key="j"><h5>{{q.question_id}} · {{q.max_score??'待填写'}}分 · {{q.scoring_mode}}</h5><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{q.question}}
参考：{{q.reference_answer}}</pre><pre style="white-space:pre-wrap">{{q.scoring_mode==='holistic'?q.holistic:q.rubric_draft}}</pre></article></section></details>
        <label v-if="draft.source.source_pair_status==='needs_teacher_confirmation'" class="check"><input name="source-confirmed" type="checkbox" v-model="edit.source_pair_confirmed">我已核实Word与PDF属于同一份试卷</label>
        <label class="check"><input name="teacher-confirmed" type="checkbox" v-model="edit.teacher_confirmed">我已核对题目、答案、参考答案、满分和所有评分点</label>
        <details v-if="problems.length" class="validation-summary"><summary>还有{{problems.length}}项需要核对 · 点击定位</summary><ul><li v-for="p in problems" :key="p"><button class="text-button" type="button" @click="focusIssue(p)">{{p}}</button></li></ul></details>
        <button class="primary" :disabled="!!problems.length">{{ busy ? '正在创建…' : '确认创建考试' }}</button>
      </fieldset>
    </form>
  </section>
</template>
