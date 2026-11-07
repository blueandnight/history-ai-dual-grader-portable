<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import {allowLeaving,useTaskGuard} from './taskGuard'
import { request } from './api'
import { fetchClasses, fetchDetail, type RosterClass, type Detail } from './records'
import { acceptedPayload, anonymousExamCandidates, applyPendingPrivacy, bulkCropChecks, artifact, assertLayout, assertOriginalExam, assertPreview, assertRuns, canSend, cropFile, groupsFromFiles, imageUrl, originalPhotoPayload, uploadBody, uploadReservedUntilStopped, type Accepted, type Approval, type CropRun, type FormalAssignment, type Group, type GradingInputMode, type Layout } from './formalUpload'
import { emptyPrivacy, readPrivacyFiles, requestPrivacyPreview, imageBlob, type PrivacyApproval } from './privacyEditing'
import BatchPrivacyGrid from './BatchPrivacyGrid.vue'
import PrivacyEditor from './PrivacyEditor.vue'
import BatchScores from './BatchScores.vue'
import ExamResultsSummary from './ExamResultsSummary.vue'
import {batchHref} from './batchRecords'
import BatchGradingControls from './BatchGradingControls.vue'
import {examHref, type ExamContext} from './navigation'
import type {ResumeUpload} from './recovery'
const props=defineProps<{resume?:ResumeUpload;initialContext?:ExamContext}>()
const emit=defineEmits<{context:[value:ExamContext]}>()
const advanced=ref(false),fileInput=ref<HTMLInputElement|null>(null)
const assignments=ref<FormalAssignment[]>([]),classes=ref<RosterClass[]>([]),exams=ref<{exam_id:string;created_at?:string}[]>([])
const assignmentId=ref(''),examId=ref(''),className=ref(''),pages=ref(1),mode=ref('single_page'),gradingInputMode=ref<GradingInputMode>('original_photo'),files=shallowRef<File[]>([])
const phase=ref<'select'|'crops'|'privacy'|'sending'|'stopped'|'done'|'uncertain'>('select'),busy=ref(false),error=ref(''),progress=ref('')
const runs=shallowRef<CropRun[][]>([]),groups=shallowRef<Group[]>([]),approvals=shallowRef<Approval[]>([]),context=shallowRef<Layout>()
const checked=ref<boolean[]>([]),statuses=ref<string[]>([]),consent=ref(false)
const bulkPrivacy=ref('')
const editingOriginal=ref(-1)
const combined=ref(false),combinedMode=ref('vision'),combinedConsent=ref(false),assessment=ref(''),combinedErrors=ref<{id:string;index:number}[]>([])
async function dispatchObjective(id:string,index:number){const p=approvals.value[index]!;const form=new FormData();form.append('assessment_id',assessment.value);form.append('source_submission_id',id);form.append('combined_confirmed','true');form.append('mode',combinedMode.value);form.append('grouping','multi_page_all');form.append('privacy_status',statuses.value[index]!);p.preview.images.forEach((image,i)=>form.append('files',imageBlob(image.data_url),`page_${i+1}.jpg`));const token=revision;const result=await request<{job_id:string}>('/objective/api/uploads','POST',form);check(token);for(;;){const state=await request<{job:{status:string}}>('/objective/api/jobs/'+result.job_id);check(token);if(!['queued','processing'].includes(state.job.status))break;progress.value=`第${index+1}份：客观题处理中；主观题由原队列处理，可停止后续上传。`;await new Promise(resolve=>setTimeout(resolve,1500));check(token)}}
async function retryObjective(item:{id:string;index:number}){if(busy.value)return;busy.value=true;try{await dispatchObjective(item.id,item.index);combinedErrors.value=combinedErrors.value.filter(v=>v.id!==item.id)}catch{error.value='客观题接收尚未确认，主观题已保留。请核对客观题任务；不会自动重试模型调用。'}finally{busy.value=false}}
async function acceptObjective(id:string,index:number){if(!combined.value)return;try{await dispatchObjective(id,index)}catch{combinedErrors.value.push({id,index})}}
function checkAllCrops(value:boolean){if(!busy.value&&phase.value==='crops')checked.value=bulkCropChecks(checked.value,value)}
function focusPendingPrivacy(){const i=statuses.value.findIndex(v=>v==='unchecked');if(i>=0){const select=document.getElementById('upload-privacy-'+i);select?.scrollIntoView({block:'center'});select?.focus()}}
function applyBulkPrivacy(){if(busy.value||phase.value!=='privacy'||!['pre_anonymized','redacted','no_identity_visible'].includes(bulkPrivacy.value))return;statuses.value=applyPendingPrivacy(statuses.value,bulkPrivacy.value);consent.value=false}
useTaskGuard(computed(()=>!['done','stopped','uncertain'].includes(phase.value)&&files.value.length>0))
const batch=shallowRef<{batch_id:string;exam_id:string;items:{submission_id:string;sequence?:number}[]}>(),uploaded=ref<string[]>([]),details=shallowRef<Detail[]>([]),skipped=ref<number[]>([]),nextSequence=ref(0),stopRequested=ref(false)
const statusBusy=ref(false),statusError=ref('')
let pollTimer:ReturnType<typeof setTimeout>|undefined
function stopPolling(){if(pollTimer!==undefined)clearTimeout(pollTimer);pollTimer=undefined}
function reviewed(d:Detail){details.value=details.value.map(row=>row.id===d.id?d:row)}
let revision=0,alive=true
const assignment=computed(()=>assignments.value.find(a=>a.id===assignmentId.value))
watch([assignmentId,examId,className],()=>emit('context',{assignmentId:assignmentId.value,examId:examId.value,className:className.value}))
const contextHref=(view:'history'|'batches'|'objective'|'layout')=>examHref(view,{assignmentId:assignmentId.value,examId:examId.value,className:className.value})
const previewExamId=computed(()=>props.resume?props.resume.batch.preview_exam_id??examId.value:examId.value)
const grouped=computed(()=>{try{return {groups:groupsFromFiles(files.value,Number(pages.value)),error:''}}catch(e){return {groups:[],error:(e as Error).message}}})
const cropCount=computed(()=>runs.value.flat().reduce((n,r)=>n+r.crops.length,0))
const allCrops=computed(()=>checked.value.length===cropCount.value&&cropCount.value>0&&checked.value.every(Boolean))
const allowed=computed(()=>editingOriginal.value<0&&approvals.value.length>0&&approvals.value.every(Boolean)&&canSend(statuses.value,approvals.value.length,consent.value))
watch(gradingInputMode,v=>{if(v!=='original_photo')combined.value=false})
watch(combinedMode,()=>combinedConsent.value=false)
const locked=computed(()=>busy.value||phase.value!=='select')
function clear(){combinedErrors.value=[];combinedConsent.value=false;assessment.value='';stopPolling();statusError.value='';revision++;runs.value=[];groups.value=[];approvals.value=[];context.value=undefined;checked.value=[];statuses.value=[];consent.value=false;editingOriginal.value=-1;error.value='';progress.value='';phase.value='select';batch.value=undefined;uploaded.value=[];skipped.value=[];nextSequence.value=0;stopRequested.value=false;details.value=[]}
function check(token:number){if(!alive||token!==revision)throw new Error('操作已取消，请重新开始。')}
function groupLabel(i:number){return `第${props.resume?.item.sequence||i+1}份`}
function cancelSelection(){clear();files.value=[];if(fileInput.value)fileInput.value.value=''}
function nextBatch(){if(allowLeaving())cancelSelection()}
function chooseFiles(event:Event){clear();files.value=[...(event.target as HTMLInputElement).files||[]]}
async function chooseExamList(){
  clear();examId.value='';exams.value=[];const token=revision;busy.value=true
  try{
    if(!assignment.value)return
    const list=className.value?(await request<{exams:{exam_id:string;created_at?:string}[]}>('/api/class-exams?class_name='+encodeURIComponent(className.value)+'&assignment_id='+assignmentId.value)).exams:anonymousExamCandidates(assignment.value,gradingInputMode.value)
    check(token);exams.value=list;if(list.length===1)examId.value=list[0]!.exam_id
  }catch(e){if(alive&&token===revision)error.value=(e as Error).message}finally{if(alive&&token===revision)busy.value=false}
}
async function generate(){
  if(busy.value||phase.value!=='select'||!assignment.value||!examId.value||grouped.value.error)return
  const token=revision,a=assignment.value,list=grouped.value.groups;busy.value=true;error.value=''
  try{
    if(props.resume&&(list.length!==1||!props.resume.exam_id||a.id!==props.resume.batch.assignment_id||examId.value!==props.resume.exam_id))throw new Error('原位重传的考试、作业或分组不一致。')
    const c=await request<Layout>('/api/exam-layout-profile?exam_id='+examId.value);check(token)
    if(gradingInputMode.value==='original_photo')assertOriginalExam(c,a.id,examId.value);else assertLayout(c,a.id)
    if(c.exam_id!==examId.value)throw new Error('考试不一致。')
    if(gradingInputMode.value==='original_photo'){
      const output:Approval[]=[]
      for(let i=0;i<list.length;i++){
        check(token);progress.value=`正在准备第${i+1}/${list.length}份原照片外发预览；未调用自动裁切。`
        const dataUrls=await readPrivacyFiles(list[i]!.files);check(token)
        const settings=emptyPrivacy(dataUrls.length)
        const preview=await requestPrivacyPreview(a.id,groupLabel(i),dataUrls,settings,true,{gradingInputMode:'original_photo',examId:previewExamId.value,className:className.value});check(token)
        const payload=originalPhotoPayload({dataUrls,settings,preview,anomalyPages:[],templateApplied:false},list[i]!.files,previewExamId.value,list[i]!.files.map(f=>f.name))
        assertPreview(payload.preview,payload.photos.length,'original_photo');output.push(payload)
      }
      check(token);context.value=c;groups.value=list;runs.value=[];approvals.value=output;statuses.value=list.map(()=>'unchecked');consent.value=false;editingOriginal.value=-1;phase.value='privacy';return
    }
    const output:CropRun[][]=[]
    for(let i=0;i<list.length;i++){
      const groupRuns:CropRun[]=[]
      for(let j=0;j<list[i]!.files.length;j++){
        check(token);progress.value=`正在配准第${i+1}/${list.length}份，照片${j+1}；未调用模型。`
        const run=await cropFile(list[i]!.files[j]!,examId.value,mode.value);check(token);groupRuns.push(run)
        if(run.status!=='cropped'||run.partial_processing){if(run.status==='alignment_failed')assertRuns([run],c);throw new Error(`第${i+1}份配准或裁切失败。请重拍或检查答题区；没有上传评分。`)}
      }
      assertRuns(groupRuns,c);output.push(groupRuns)
    }
    check(token);context.value=c;groups.value=list;runs.value=output;checked.value=Array(output.flat().reduce((n,r)=>n+r.crops.length,0)).fill(false);phase.value='crops'
  }catch(e){if(alive&&token===revision){error.value=(e as Error).message;runs.value=[];approvals.value=[]}}
  finally{if(alive&&token===revision){busy.value=false;progress.value=''}}
}
function cropIndex(g:number,r:number,k:number){return runs.value.slice(0,g).flat().reduce((n,x)=>n+x.crops.length,0)+runs.value[g]!.slice(0,r).reduce((n,x)=>n+x.crops.length,0)+k}
async function accept(){
  if(busy.value||phase.value!=='crops'||!allCrops.value||!context.value||!assignment.value)return
  const token=revision,c=context.value,a=assignment.value;busy.value=true;error.value=''
  try{
    const output:Approval[]=[]
    for(let i=0;i<runs.value.length;i++){
      const accepted:Accepted[]=[],filenames:string[]=[]
      for(let j=0;j<runs.value[i]!.length;j++){
        const run=runs.value[i]![j]!;check(token)
        const review=await request<{human_review:string}>('/api/student-auto-crop-review','POST',{alignment_run_id:run.alignment_run_id,verdict:'passed'});check(token)
        if(review.human_review!=='passed')throw new Error('裁切验收未保存。')
        const result=await request<{answer_regions:Accepted[]}>('/api/student-auto-crop-answer-regions','POST',{alignment_run_id:run.alignment_run_id});check(token)
        if(!result.answer_regions?.length)throw new Error('缺少合法裁切凭据。')
        accepted.push(...result.answer_regions);filenames.push(...result.answer_regions.map(()=>groups.value[i]!.files[j]!.name))
      }
      const payload=acceptedPayload(accepted,a,c)
      if(props.resume&&(payload.photos.length!==props.resume.item.page_count))throw new Error('重传裁切页数与原预留清单不一致。')
      check(token);const preview=await request<Approval['preview']>('/api/ai-payload-preview','POST',{assignment_id:a.id,exam_id:c.exam_id,class_name:className.value,student_label:groupLabel(i),dual_review_enabled:true,photos:payload.photos,answer_regions:payload.regions});check(token);assertPreview(preview,payload.photos.length)
      output.push({...payload,filenames,preview,gradingInputMode:'auto_crop'})
    }
    approvals.value=output;statuses.value=output.map(()=>'unchecked');consent.value=false;phase.value='privacy'
  }catch(e){if(alive&&token===revision){error.value=(e as Error).message;approvals.value=[];checked.value=checked.value.map(()=>false)}}finally{if(alive&&token===revision)busy.value=false}
}
function invalidateOriginal(i:number){if(gradingInputMode.value!=='original_photo')return;approvals.value=approvals.value.map((p,k)=>k===i?undefined as unknown as Approval:p);statuses.value[i]='unchecked';consent.value=false}
function approveOriginal(i:number,value:PrivacyApproval){if(gradingInputMode.value!=='original_photo'||!groups.value[i])return;try{const approval=originalPhotoPayload(value,groups.value[i]!.files,previewExamId.value,groups.value[i]!.files.map(f=>f.name));assertPreview(approval.preview,approval.photos.length,'original_photo');approvals.value=approvals.value.map((p,k)=>k===i?approval:p);statuses.value[i]='unchecked';editingOriginal.value=-1;consent.value=false;error.value=''}catch(e){error.value=(e as Error).message}}
async function send(){
  if(busy.value||phase.value!=='privacy'||!allowed.value||!assignment.value)return
  if(combined.value&&(!combinedConsent.value||gradingInputMode.value!=='original_photo'))return
  const token=revision,a=assignment.value;busy.value=true;phase.value='sending';error.value='';stopRequested.value=false;nextSequence.value=0;skipped.value=[];uploaded.value=[]
  try{
    check(token)
    if(combined.value){const snapshot=await request<{assessment_id:string}>('/objective/api/assessments/from-exam','POST',{exam_id:examId.value});check(token);assessment.value=snapshot.assessment_id}
    const reserved=props.resume?{batch_id:props.resume.batch.batch_id,exam_id:props.resume.exam_id,items:[props.resume.item]}:await request<NonNullable<typeof batch.value>>('/api/batches','POST',{assignment_id:a.id,exam_id:examId.value,class_name:className.value,grading_input_mode:gradingInputMode.value,batch_request_id:crypto.randomUUID(),items:approvals.value.map(p=>({page_count:p.photos.length,}))});check(token)
    batch.value=reserved
    window.dispatchEvent(new Event('batch-created'))
    if(reserved.exam_id!==examId.value||reserved.items.length!==approvals.value.length||(props.resume&&(props.resume.batch.grading_input_mode||'auto_crop')!==gradingInputMode.value))throw new Error('预留批次与确认内容不一致。')
    nextSequence.value=await uploadReservedUntilStopped(approvals.value,0,async(_approval,i)=>{
      check(token);progress.value=`正在上传第${i+1}/${approvals.value.length}份；模型评分由原服务队列处理。`
      const body=uploadBody(a,examId.value,className.value,approvals.value[i]!,statuses.value[i]!,reserved,i)
      if(props.resume){body.sequence=props.resume.item.sequence;body.idempotency_key=`${reserved.batch_id}:${props.resume.item.sequence}`;body.student_label=groupLabel(i)}
      await request('/api/submissions','POST',body);check(token);await acceptObjective(reserved.items[i]!.submission_id,i);check(token)
    },()=>stopRequested.value,(i)=>uploaded.value.push(reserved.items[i]!.submission_id))
    if(nextSequence.value<approvals.value.length){phase.value='stopped';progress.value=`已停止后续上传；已确认上传${uploaded.value.length}份，尚有${approvals.value.length-nextSequence.value}份保留原序号。可逐份原位上传或由教师确认跳过。`}
    else{phase.value='done';progress.value='上传完成，下方查看双评成绩；符合条件可批量采纳，分歧或异常需复核。'}
  }catch{if(alive&&token===revision){phase.value='uncertain';error.value='上传未能完整确认，已停止后续上传且不会自动重试。请先到任务进度与异常恢复中核实已上传和预留的序号，勿重新创建相同批次。'}}finally{if(alive&&token===revision)busy.value=false}
}
function stopSending(){if(phase.value==='sending')stopRequested.value=true}
async function uploadNext(){
  if(busy.value||phase.value!=='stopped'||!batch.value||!assignment.value||nextSequence.value>=approvals.value.length)return
  const token=revision,a=assignment.value,index=nextSequence.value,reserved=batch.value;busy.value=true;error.value=''
  try{
    const completed=await uploadReservedUntilStopped(approvals.value.slice(index,index+1),0,async(_approval,offset)=>{
      const i=index+offset;check(token);progress.value=`正在原位上传第${i+1}/${approvals.value.length}份；模型评分由原服务队列处理。`
      const body=uploadBody(a,examId.value,className.value,approvals.value[i]!,statuses.value[i]!,reserved,i)
      if(props.resume){body.sequence=props.resume.item.sequence;body.idempotency_key=`${reserved.batch_id}:${props.resume.item.sequence}`;body.student_label=groupLabel(i)}
      await request('/api/submissions','POST',body);check(token);await acceptObjective(reserved.items[i]!.submission_id,i);check(token)
    },()=>false,()=>uploaded.value.push(reserved.items[index]!.submission_id))
    nextSequence.value=index+completed
    if(nextSequence.value>=approvals.value.length){phase.value='done';progress.value='上传完成，下方查看双评成绩；符合条件可批量采纳，分歧或异常需复核。'}
    else progress.value=`第${index+1}份已原位上传；其余答卷仍待处理。`
  }catch{if(alive&&token===revision){phase.value='uncertain';error.value='原位上传结果未能确认。请先核实批次状态，不要重复创建批次。'}}finally{if(alive&&token===revision)busy.value=false}
}
async function skipNext(){
  if(busy.value||phase.value!=='stopped'||!batch.value||nextSequence.value>=approvals.value.length)return
  const index=nextSequence.value,item=batch.value.items[index],sequence=props.resume?props.resume.item.sequence:(item?.sequence||index+1)
  if(!item||!window.confirm(`确认跳过第${sequence}份？该份不会上传或评分，并会记录教师跳过。`))return
  const token=revision;busy.value=true;error.value=''
  try{
    await request('/api/batches/skip','POST',{batch_id:batch.value.batch_id,submission_id:item.submission_id,sequence,confirm_skip:true,reason:'教师明确确认跳过未上传答卷'})
    check(token);skipped.value.push(sequence);nextSequence.value++
    if(nextSequence.value>=approvals.value.length){phase.value='done';progress.value='所有序号均已上传或由教师明确跳过。'}
    else progress.value=`已由教师明确跳过第${sequence}份；第${nextSequence.value+1}份仍待处理。`
  }catch(e){if(alive&&token===revision)error.value=(e as Error).message}finally{if(alive&&token===revision)busy.value=false}
}
async function refresh(){
  if(statusBusy.value||!uploaded.value.length)return
  stopPolling();const token=revision;statusBusy.value=true;statusError.value=''
  try{const rows=await Promise.all(uploaded.value.map(fetchDetail));check(token);details.value=rows}
  catch(e){if(alive&&token===revision)statusError.value=(e as Error).message}
  finally{
    statusBusy.value=false
    if(alive&&token===revision&&!statusError.value&&['done','stopped'].includes(phase.value)&&details.value.some(s=>['queued','grading','recovery_pending'].includes(s.state||'')))pollTimer=setTimeout(refresh,3000)
  }
}
watch([phase,()=>uploaded.value.length],()=>{stopPolling();if(['done','stopped'].includes(phase.value)&&uploaded.value.length)void refresh()})
onMounted(async()=>{const token=revision;busy.value=true;try{const [a,c]=await Promise.all([request<{assignments:FormalAssignment[]}>('/api/assignments'),fetchClasses()]);check(token);assignments.value=a.assignments;classes.value=c;if(!props.resume){const initial=props.initialContext;if(initial?.assignmentId&&a.assignments.some(row=>row.id===initial.assignmentId))assignmentId.value=initial.assignmentId;else if(initial?.assignmentId)error.value='传入的考试不存在或无法访问，请重新选择考试。';else if(a.assignments.length===1)assignmentId.value=a.assignments[0]!.id;if(initial?.className&&c.some(row=>row.class_name===initial.className))className.value=initial.className;if(assignmentId.value){await chooseExamList();if(initial?.examId){if(exams.value.some(e=>e.exam_id===initial.examId))examId.value=initial.examId;else{examId.value='';error.value='传入的阅卷记录与当前考试、班级或输入方式不匹配，请明确选择对应班级与阅卷记录。'}}}}if(props.resume){assignmentId.value=props.resume.batch.assignment_id;className.value=props.resume.batch.class_name;examId.value=props.resume.exam_id;gradingInputMode.value=props.resume.batch.grading_input_mode||'auto_crop';exams.value=props.resume.exam_id?[{exam_id:props.resume.exam_id}]:[];if(!examId.value)error.value='原批次考试无法核实，已阻止正式重传。'}}catch(e){if(alive)error.value=(e as Error).message}finally{if(alive)busy.value=false}})
onUnmounted(()=>{stopPolling();alive=false;revision++;files.value=[];runs.value=[];approvals.value=[];groups.value=[];details.value=[]})
</script>
<template>
<section class="formal-upload"><h2>上传与批改</h2><p>原照片可直接批改。添加一份或多份答卷后，先核对分组和隐私，再提交双模型评分。</p>
  <ol class="workflow-steps" aria-label="上传步骤"><li :aria-current="phase==='select'?'step':undefined">1 选择考试与照片</li><li :aria-current="['crops','privacy'].includes(phase)?'step':undefined">2 核对实际外发图</li><li :aria-current="['sending','done','stopped','uncertain'].includes(phase)?'step':undefined">3 评分与教师终评</li></ol>
  <p v-if="resume">沿用批次{{resume.batch.batch_id}}第{{resume.item.sequence}}份，保持原批次模式，不创建新批次。</p>
  <p v-if="error" role="alert">{{ error }}</p><p role="status">{{ progress }}</p>
  <p v-if="phase!=='select'" class="exam-summary">{{assignment?.name}} · {{groups.length}}份 / {{files.length}}张 · {{gradingInputMode==='original_photo'?'原照片整页':'自动裁切'}}{{className?' · '+className:' · 不关联花名册'}}</p>
  <fieldset v-show="phase==='select'" :disabled="locked"><legend>1. 选择考试和照片</legend>
    <label class="photo-picker">学生照片（PNG/JPEG，可多选）<input ref="fileInput" type="file" accept=".png,.jpg,.jpeg" multiple @change="chooseFiles"><small>一份或多份均从这里添加；手机浏览器也可选择拍照。</small></label>
    <label v-if="!initialContext?.assignmentId" for="upload-assignment">选择考试</label><select v-show="!initialContext?.assignmentId" id="upload-assignment" v-model="assignmentId" :disabled="!!resume" @change="chooseExamList"><option value="">请选择</option><option v-for="a in assignments" :key="a.id" :value="a.id">{{ a.name }}</option></select>
    <label for="upload-class">关联班级（可选）</label><select id="upload-class" v-model="className" :disabled="!!resume" @change="chooseExamList"><option value="">不关联花名册</option><option v-for="c in classes" :key="c.class_name">{{ c.class_name }}</option></select>
    <div v-show="exams.length!==1||!examId"><label for="upload-exam">本次阅卷记录</label><select id="upload-exam" v-model="examId" :disabled="!!resume" @change="clear"><option value="">请选择本考试的阅卷记录</option><option v-for="e in exams" :key="e.exam_id" :value="e.exam_id">{{e.created_at || '正式考试'}} · {{e.exam_id.slice(0,8)}}</option></select></div>
    <p v-if="assignment && exams.length===1 && examId" class="muted">已选用本考试的唯一阅卷记录；可直接添加照片。</p>
    <p v-if="assignment && !exams.length" role="status">当前模式下没有可用阅卷记录。请检查班级归属；自动裁切还需配置正式答题区。</p>
    <label v-if="gradingInputMode==='original_photo'&&!resume" class="check"><input type="checkbox" v-model="combined" @change="combinedConsent=false"/>这份答题卡同时包含客观题与主观题（一次选择照片）</label><p v-if="combined">本考试须已有确认的客观题答案与分值。每份多页保持同一来源，自动关联，无需再次配对。</p><label v-if="combined">客观题识别<select v-model="combinedMode"><option value="vision">现有双模型视觉识别（可能收费）</option><option value="omr">本机 OMR（需已有确认模板）</option></select></label>
    <details v-if="examId" class="upload-links"><summary>本考试的其他操作</summary><p class="context-actions"><a :href="contextHref('objective')">批改本考试客观题 →</a> · <a :href="contextHref('history')">本考试终评与成绩 →</a></p></details>
    <details :open="advanced" @toggle="advanced=($event.target as HTMLDetailsElement).open"><summary>更多设置{{gradingInputMode==='auto_crop'?' · 自动裁切':''}}</summary>
      <label for="grading-input-mode">阅卷输入方式</label><select id="grading-input-mode" v-model="gradingInputMode" :disabled="!!resume" @change="chooseExamList"><option value="original_photo">原照片整页批改（默认）</option><option value="auto_crop">自动配准裁切</option></select>
      <template v-if="gradingInputMode==='auto_crop'"><p>需先<a :href="contextHref('layout')">设置正式答题区</a>，并验收裁切预览。</p><label for="upload-layout">拍摄版式</label><select id="upload-layout" v-model="mode" @change="clear"><option value="single_page">单页照片</option><option value="double_spread">双页展开照片</option></select></template>
    </details>

    <div v-if="files.length" class="grouping-panel"><h3>已选择{{files.length}}张照片 · 核对答卷分组</h3>
    <label>每份照片张数<input v-model.number="pages" type="number" min="1" max="10" step="1" @change="clear"></label>

    <p v-if="files.length && grouped.error" role="alert">{{ grouped.error }}</p>
    <ol><li v-for="(g,i) in grouped.groups" :key="i">{{ i+1 }} · {{ g.label }}：{{ g.files.map(f=>f.name).join('、') }}</li></ol>
    </div>
    <button @click="generate" :disabled="!assignmentId||!examId||!files.length||!!grouped.error">{{ gradingInputMode==='original_photo'?'核对分组并编辑原照片隐私':'生成正式裁切预览（不调用模型）' }}</button>
  </fieldset>
  <section v-if="phase==='crops'"><h3>2. 逐张验收正式裁切</h3><p>有截字、错页、材料正文或身份信息时请取消并调整布局/重拍。</p>
    <p><button :disabled="busy" @click="checkAllCrops(true)">已查看本批预览，全部验收</button> <button :disabled="busy" @click="checkAllCrops(false)">取消全部验收</button> <span role="status">已验收 {{checked.filter(Boolean).length}}/{{cropCount}} 张；异常图片可单独取消。下一步仍需确认隐私。</span></p>
    <article v-for="(list,g) in runs" :key="g" class="point-record"><h4>第{{ g+1 }}份</h4><div v-for="(run,r) in list" :key="run.alignment_run_id">
      <p v-if="run.alignment_assisted" role="alert">外框辅助对齐：请核对本批预览中的小题位置和答案完整性，可批量验收，异常图片可单独取消。</p>
      <p v-for="p in run.accepted_pages" :key="p.page_number"><a :href="artifact(run.alignment_run_id,'roi_overlay_page_'+p.page_number+'.png')" target="_blank" rel="noopener">查看原页与裁切边界（第{{ p.page_number }}页）</a></p>
      <figure v-for="(crop,k) in run.crops" :key="crop.question_id"><img class="upload-preview" :src="artifact(run.alignment_run_id,crop.padding_validation_file_name||crop.file_name)" :alt="'小题'+crop.question_id+'裁切图'"><figcaption>小题{{ crop.question_id }}</figcaption><label class="check"><input type="checkbox" v-model="checked[cropIndex(g,r,k)]" :disabled="busy">题号正确、手写基本完整、未混入材料正文</label></figure>
    </div></article>
    <button @click="accept" :disabled="busy||!allCrops">裁切验收通过，继续隐私预览</button> <button @click="cancelSelection" :disabled="busy">取消并重新选择</button>
  </section>
  <section v-if="phase==='privacy'"><h3>{{ gradingInputMode==='original_photo'?'2. 编辑原照片并确认实际外发图片':'3. 确认实际外发图片与隐私' }}</h3><p v-if="gradingInputMode==='original_photo'">模型将接收整页原照片。可逐份遮挡身份信息并核对服务端实际外发预览；原照片模式不调用自动裁切，也不要求有效答题区布局。</p><p v-else>下方是服务端生成的实际模型输入。发现身份、错页或裁切异常时取消，不可直接修改这些已签名图像。</p>
    <section v-if="gradingInputMode==='original_photo'&&editingOriginal>=0&&assignment"><h4>单独修正第{{editingOriginal+1}}份</h4><PrivacyEditor :key="editingOriginal" :assignment="assignment" :files="groups[editingOriginal]!.files" :student-label="groupLabel(editingOriginal)" :dual="true" :grading-input-mode="gradingInputMode" :exam-id="previewExamId" :class-name="className" :initial-settings="approvals[editingOriginal]?.settings" @invalidated="invalidateOriginal(editingOriginal)" @ready="approveOriginal(editingOriginal,$event)"/></section>

    <BatchPrivacyGrid :approvals="approvals" :statuses="statuses" :busy="busy" :editable="gradingInputMode==='original_photo'" @status="(i,v)=>{statuses[i]=v;consent=false}" @edit="i=>{editingOriginal=i;consent=false}"/>
    <fieldset v-if="approvals.length>1" :disabled="busy"><legend>批量设置隐私状态</legend><p>无需逐份点击确认。先查看本批实际外发图片，再选择本批隐私状态并点击应用。只应用到尚未确认的答卷，已设置的单份状态不会覆盖，仍可单独修改。</p><label for="upload-bulk-privacy">本批隐私状态</label><select id="upload-bulk-privacy" v-model="bulkPrivacy"><option value="">请选择状态</option><option value="pre_anonymized">导入前已避开身份信息</option><option value="redacted">确认预览图中身份信息已遮挡</option><option value="no_identity_visible">确认图中没有可辨认身份信息</option></select><button :disabled="!bulkPrivacy" @click="applyBulkPrivacy">应用到尚未确认的答卷</button></fieldset>
    <p v-if="editingOriginal>=0" role="status">正在编辑单份图片，请先重新生成并采用该份预览。</p>
    <p>本批主观题预计模型请求：{{approvals.reduce((n,a)=>n+(a.preview.estimated_model_requests??2),0)}}次。开放题先转写再评分，分区及重试可能增加请求和费用。</p>
    <label class="check"><input type="checkbox" v-model="consent">我已逐张查看实际外发图片，确认没有未排除的身份信息，并同意提交双模型批改</label>
    <label v-if="combined" class="check"><input type="checkbox" v-model="combinedConsent"/>我同意同一批已核对的实际外发图用于两类评分；视觉识别每份另调用两个客观题模型，可能另外收费</label>
    <button class="primary" @click="send" :disabled="busy||!allowed||(combined&&!combinedConsent)">确认并上传本批</button> <button @click="cancelSelection" :disabled="busy">取消本批</button>
  </section>
  <section v-if="combinedErrors.length" role="status"><h3>客观题接收待核实（不影响主观题）</h3><p>已接收的客观题不会重复创建；未确认的上传需明确重试。离开页面后可从客观题任务查看已接收部分。</p><p v-for="item in combinedErrors" :key="item.id">第{{item.index+1}}份 <button :disabled="busy" @click="retryObjective(item)">核实并原位补传客观题</button></p></section>
  <BatchGradingControls v-if="batch" :key="batch.batch_id" :assignment-id="assignmentId" :exam-id="batch.exam_id" :batch-id="batch.batch_id" @stopped="stopSending" @changed="refresh" />
  <section v-if="['sending','stopped','done','uncertain'].includes(phase)" class="task-progress"><div class="progress-facts"><span>上传：{{uploaded.length}} / {{approvals.length}}份已接收</span><span>评分：{{details.filter(d=>d.state==='completed').length}}份完成（已读取状态）</span><span>成绩确认：{{details.filter(d=>d.confirmed_at&&!d.teacher_reconfirmation_required).length}}份已确认</span></div><h3>{{ phase==='done'?'上传完成':phase==='uncertain'?'请核实批次状态':phase==='stopped'?'上传已停止':'正在上传' }}</h3><details v-if="batch"><summary>查看批次编号</summary><p>{{batch.batch_id}}</p></details><p>已确认上传{{ uploaded.length }}份 · 教师明确跳过{{ skipped.length }}份 · 尚未处理{{ Math.max(0,approvals.length-nextSequence) }}份。此处不会自动重试或跳过。</p><button v-if="phase==='sending'" :disabled="stopRequested" @click="stopSending">{{ stopRequested?'正在等待已开始的上传完成…':'停止后续上传' }}</button><template v-if="phase==='stopped'"><button :disabled="busy" @click="uploadNext">原位上传下一份</button> <button class="button danger" :disabled="busy" @click="skipNext">教师确认跳过下一份</button></template> <a v-if="batch" :href="batchHref({batch_id:batch.batch_id,assignment_id:assignmentId,exam_id:batch.exam_id},initialContext)">重新打开本批成绩</a> · <a :href="contextHref('batches')">查看批次记录</a> · <a :href="contextHref('history')">查看考试全部成绩</a> <button v-if="phase==='done'&&!resume" :disabled="busy" @click="nextBatch">上传下一批照片</button><p><button v-if="uploaded.length" :disabled="statusBusy" @click="refresh">只读刷新已上传状态</button></p><p v-if="uploaded.length">本批评分状态自动刷新；上传完成不代表评分完成。下方直接显示总分、小题双评分数，可批量采纳或处理分歧。</p><p v-if="statusError" role="alert">状态读取失败：{{statusError}}。已暂停自动刷新，可点击只读刷新重试。</p><BatchScores v-if="batch&&uploaded.length" :assignment-id="assignmentId" :exam-id="batch.exam_id" :batch-id="batch.batch_id" :refresh-key="uploaded.length" @reviewed="reviewed" @changed="refresh" /></section>
<ExamResultsSummary v-if="examId&&['sending','done','stopped','uncertain'].includes(phase)" :exam-id="examId" :assignment-id="assignmentId" :refresh-key="details.map(d=>d.state+String(d.confirmed_at)).join()"/>
</section>
</template>
