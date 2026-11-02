<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { fetchAssignments, fetchSubmissions, finalLabel, score, exportHref, type Assignment, type Submission } from './history'
import SubmissionDetail from './SubmissionDetail.vue'
import ReviewQueue from './ReviewQueue.vue'
import {nextReviewId} from './reviewQueue'
import ExamReport from './ExamReport.vue'
import AnswerLinks from './AnswerLinks.vue'
import UnifiedScores from './UnifiedScores.vue'
import PhotoCleanup from './PhotoCleanup.vue'
import BatchGradingControls from './BatchGradingControls.vue'
import ConsensusScores from './ConsensusScores.vue'
import {allowLeaving,hasUnsavedTask} from './taskGuard'
import {request} from './api'
import {examHref,type ExamContext} from './navigation'
import type { Detail } from './records'
const props=defineProps<{initialContext?:ExamContext}>()
const emit=defineEmits<{context:[value:ExamContext]}>()
const formalExams=ref<{exam_id:string;assignment_id:string;title:string;created_at?:string}[]>([]),exam=ref(''),sourceError=ref('')
const panel=ref<'records'|'links'|'scores'>('records')
const examChoices=computed(()=>formalExams.value.filter(e=>e.assignment_id===selected.value))
const visibleSubmissions=computed(()=>submissions.value)
const contextHref=(view:'upload'|'objective')=>examHref(view,{assignmentId:selected.value,examId:exam.value,className:props.initialContext?.examId===exam.value?props.initialContext.className:undefined})
const assignments = ref<Assignment[]>([]), submissions = ref<Submission[]>([])
const selected = ref(''), busy = ref(false), error = ref('')
const detailId = ref('')
const reviewId = ref('')
const reviewIds=ref<string[]>([])
const reviewItems=computed(()=>reviewIds.value.length?visibleSubmissions.value.filter(s=>reviewIds.value.includes(s.id)):visibleSubmissions.value)
function reviewConsensus(id:string,ids:string[]){if(!allowLeaving())return;reviewIds.value=ids;reviewId.value=id;detailId.value=''}
const reportOpen = ref(false)
const selectedAssignment = computed(() => assignments.value.find(a => a.id === selected.value))
function reviewed(d: Detail) { const row=submissions.value.find(s=>s.id===d.id); if(row){row.final_total=d.final_total;row.confirmed_at=d.confirmed_at;row.teacher_reconfirmation_required=d.teacher_reconfirmation_required} }
function photosCleaned(ids:string[]){for(const s of submissions.value)if(ids.includes(s.id)){s.photos=[];s.photos_deleted=true}}
let revision = 0
async function load() {
  reviewId.value=''; reportOpen.value=false;panel.value='records'
  const current = ++revision; busy.value = true; error.value = ''; submissions.value = []; detailId.value = ''
  try {
    const items = await fetchAssignments()
    if (current !== revision) return
    assignments.value = items
    sourceError.value=''
    try{formalExams.value=(await request<{exams:typeof formalExams.value}>('/objective/api/source-exams')).exams}catch(e){if(current===revision){formalExams.value=[];sourceError.value='正式考试列表暂不可用，答卷记录仍可查看。请刷新后再进入关联与统一成绩。'}}
    if(current!==revision)return
    if (selected.value && !items.some(a => a.id === selected.value)) selected.value = ''
    if(!selected.value){if(items.some(a=>a.id===props.initialContext?.assignmentId))selected.value=props.initialContext!.assignmentId;else if(props.initialContext?.assignmentId)sourceError.value='传入的考试不存在或无法访问，请重新选择考试。';else if(items.length===1)selected.value=items[0]!.id}
    if(selected.value)await showRecords(exam.value || props.initialContext?.examId)
  } catch (e) { if (current === revision) { assignments.value = []; error.value = e instanceof Error ? e.message : '加载失败' } }
  finally { if (current === revision) busy.value = false }
}
async function showRecords(preferred?:string) {
  exam.value=examChoices.value.some(e=>e.exam_id===preferred)?preferred!:!preferred&&props.initialContext?.mode!=='quick'&&examChoices.value.length>0?examChoices.value[0]!.exam_id:''
  if(preferred&&!exam.value)sourceError.value='传入的阅卷记录不属于所选考试或无法访问，请重新选择记录。'
  selectExam()
  reviewId.value=''; reportOpen.value=false;panel.value='records'
  const current = ++revision; submissions.value = []; error.value = ''; detailId.value = ''
  if (!selected.value) { busy.value = false; return }
  busy.value = true
  try { const rows = await fetchSubmissions(selected.value); if (current === revision){submissions.value = rows;if(!preferred&&rows.some(s=>!s.exam_id)){exam.value='';selectExam()}} }
  catch (e) { if (current === revision) error.value = e instanceof Error ? e.message : '加载失败' }
  finally { if (current === revision) busy.value = false }
}
function changePanel(value:'records'|'links'|'scores'){if(!allowLeaving())return;reviewId.value='';detailId.value='';reportOpen.value=false;panel.value=value;const p=new URLSearchParams(location.hash.split('?')[1]||'');p.set('panel',value);history.replaceState(null,'',location.hash.split('?')[0]+'?'+p)}
function selectExam(){reviewId.value='';detailId.value='';reportOpen.value=false;emit('context',{assignmentId:selected.value,examId:exam.value,className:props.initialContext?.examId===exam.value?props.initialContext.className:undefined,mode:exam.value?'formal':props.initialContext?.mode||'formal'})}
onMounted(async()=>{await load();const p=new URLSearchParams(location.hash.split('?')[1]||'').get('panel');if(p==='links'||p==='scores')changePanel(p)})
onUnmounted(() => { revision++; assignments.value = []; submissions.value = [] })
</script>
<template>
  <section><h2>终评与成绩</h2>
    <p>默认只读查询。点击确认最终总分后才进入复核、改判与保存流程。</p>
    <button @click="load" :disabled="busy||hasUnsavedTask">刷新历史</button>
    <p v-if="error" role="alert">{{ error }}</p><p v-if="sourceError" role="alert">{{sourceError}}</p>
    <p v-if="busy" aria-live="polite">正在加载…</p>
    <p v-else-if="!assignments.length && !error">尚无考试记录。</p>
    <div v-if="assignments.length&&!initialContext?.assignmentId"><label for="history-assignment">选择考试</label><select id="history-assignment" v-model="selected" @change="showRecords()"><option value="">请选择</option><option v-for="a in assignments" :key="a.id" :value="a.id">{{ a.name }}（{{ a.submission_count }}份）</option></select></div>
    <template v-if="selected">
      <details v-if="examChoices.length>1"><summary>高级：本次上传与任务管理范围</summary><label>本次阅卷记录<select v-model="exam" :disabled="hasUnsavedTask" @change="selectExam"><option value="">全部主观题记录（不影响全部成绩合览）</option><option v-for="e in examChoices" :key="e.exam_id" :value="e.exam_id">{{e.created_at || e.title}} · {{e.exam_id.slice(0,8)}}</option></select></label></details>
      <div class="context-actions"><a :href="contextHref('upload')">继续上传本考试主观题</a><a v-if="exam" :href="contextHref('objective')">批改本考试客观题</a></div>
      <div class="panel-tabs" role="group" aria-label="考试记录视图">
        <button :aria-pressed="panel==='records'" @click="changePanel('records')">答卷与终评</button>
        <button :disabled="!exam" :aria-pressed="panel==='links'" @click="changePanel('links')">答卷关联（可选）</button>
        <button :disabled="!exam" :aria-pressed="panel==='scores'" @click="changePanel('scores')">统一成绩</button>
      </div>
      <AnswerLinks v-if="panel==='links' && exam" :key="selected+exam" :assignment-id="selected" :selected-exam-id="exam" />
      <UnifiedScores v-if="panel==='scores' && exam" :key="selected+exam" :assignment-id="selected" :selected-exam-id="exam" />
      <details class="export-tools"><summary>兼容工具：主观题旧导出与报告</summary><div class="exports"><a :href="exportHref(selected, false)">导出实名成绩</a><a :href="exportHref(selected, true)">导出匿名成绩</a><button :disabled="!exam" @click="reportOpen=true">试卷报告</button></div><p>这些兼容工具仅针对主观题。整卷正式成绩和考试评价请使用“统一成绩”页；快速匿名仍可导出已确认主观题成绩。</p></details>
      <div v-if="panel==='records'">
      <BatchGradingControls v-if="!reviewId&&!detailId" :assignment-id="selected" :exam-id="exam" @changed="showRecords(exam)" />
      <PhotoCleanup v-if="!busy&&!reviewId&&!detailId" :key="selected+':'+exam+':'+revision" :assignment-id="selected" :exam-id="''" :submissions="visibleSubmissions" @cleaned="photosCleaned" />
      <p>正式主观题成绩使用已确认的总分；未确认成绩不会自动发布。</p>
      <div v-if="!busy && !visibleSubmissions.length && !error" class="empty-state"><h4>该考试尚无主观题答卷</h4><p>添加学生照片后，可在这里继续教师终评。</p><a :href="contextHref('upload')">上传第一批答卷</a></div>
      <div v-if="!reviewId&&nextReviewId(visibleSubmissions,'','')" class="context-actions"><button class="secondary" @click="allowLeaving()&&(reviewIds=[],reviewId=nextReviewId(visibleSubmissions,'',''),detailId='')">开始连续评阅</button><small>可选：逐份核对全部未确认答卷；正常答卷可直接使用下方批量采纳。</small></div>
      <ConsensusScores v-if="!busy&&!reviewId&&!detailId" :key="selected+':'+exam+':'+revision" :assignment-id="selected" :exam-id="''" @review="reviewConsensus" @detail="detailId=$event" @changed="showRecords(exam)" />
      <details v-if="visibleSubmissions.length&&!reviewId"><summary>原始记录与单份详情（备用）</summary><ul><li v-for="s in visibleSubmissions" :key="s.id">{{s.marking_code||s.student_label||s.id}} · {{finalLabel(s)}} <button @click="detailId=s.id">查看单份详情</button><button @click="reviewConsensus(s.id,[s.id])">确认本份总分</button></li></ul></details>
      </div>
    </template>
    <ExamReport v-if="reportOpen && selectedAssignment" :key="selectedAssignment.id + ':' + exam" :assignment-id="selectedAssignment.id" :exam-id="exam" :assignment-name="selectedAssignment.name" @close="reportOpen = false" />
    <button v-if="detailId" @click="detailId=''">返回双评成绩</button><SubmissionDetail v-if="detailId" :id="detailId" />
    <ReviewQueue v-if="reviewId && visibleSubmissions.some(s=>s.id===reviewId)" v-model:id="reviewId" :items="reviewItems" :expected-exam-id="''" @saved="reviewed" />
  </section>
</template>
