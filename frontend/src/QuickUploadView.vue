<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,shallowRef} from 'vue'
import {useTaskGuard} from './taskGuard'
import {examHref,type ExamContext} from './navigation'
import BatchScores from './BatchScores.vue'
import {batchHref} from './batchRecords'
import {request} from './api'
import {applyPendingPrivacy,groupsFromFiles,type FormalAssignment,type Group} from './formalUpload'
import {fetchDetail,type Detail} from './records'
import {prepareQuickPrivacyPreview,quickUploadBody,type PrivacyApproval} from './privacyEditing'
import type {ResumeUpload} from './recovery'
import PrivacyEditor from './PrivacyEditor.vue'
const props=defineProps<{resume?:ResumeUpload;initialContext?:ExamContext}>()
const emit=defineEmits<{context:[value:ExamContext]}>()
const bulkPrivacy=ref(''),progress=ref('')
const assignments=ref<FormalAssignment[]>([]),assignmentId=ref(''),files=shallowRef<File[]>([]),pages=ref(1),dual=ref(true),groupConsent=ref(false),consent=ref(false)
const groups=shallowRef<Group[]>([]),approvals=shallowRef<(PrivacyApproval|undefined)[]>([]),statuses=ref<string[]>([]),editing=ref(-1),busy=ref(false),error=ref(''),phase=ref<'select'|'privacy'|'sending'|'done'|'uncertain'>('select')
const batch=shallowRef<{batch_id:string;exam_id?:string;items:{submission_id:string;sequence?:number}[]}>(),uploaded=ref<string[]>([]),details=shallowRef<Detail[]>([])
useTaskGuard(computed(()=>!['done','uncertain'].includes(phase.value)&&files.value.length>0))
const contextHref=(view:'history'|'batches')=>examHref(view,{assignmentId:assignmentId.value,examId:'',mode:'quick'})
function changeAssignment(){reset();emit('context',{assignmentId:assignmentId.value,examId:'',mode:'quick'})}
let alive=true,revision=0
const assignment=computed(()=>assignments.value.find(a=>a.id===assignmentId.value))
const grouped=computed(()=>{try{return {groups:groupsFromFiles(files.value,props.resume?files.value.length:pages.value,false),error:''}}catch(e){return {groups:[],error:(e as Error).message}}})
const ready=computed(()=>approvals.value.length>0&&approvals.value.every(Boolean)&&statuses.value.every(v=>['pre_anonymized','redacted','no_identity_visible'].includes(v))&&consent.value&&editing.value<0)
function reset(){revision++;busy.value=false;bulkPrivacy.value='';progress.value='';groups.value=[];approvals.value=[];statuses.value=[];editing.value=-1;phase.value='select';consent.value=false;groupConsent.value=false;error.value='';batch.value=undefined;uploaded.value=[];details.value=[]}
function choose(event:Event){reset();files.value=[...(event.target as HTMLInputElement).files||[]]}
async function begin(){
 if(busy.value||!assignment.value||assignment.value.formal_layout_required||!groupConsent.value||grouped.value.error)return
 if(props.resume&&(grouped.value.groups.length!==1||files.value.length!==props.resume.item.page_count)){error.value='原位重传必须与预留照片页数一致';return}
 const token=revision,a=assignment.value;groups.value=grouped.value.groups;approvals.value=groups.value.map(()=>undefined);statuses.value=groups.value.map(()=>'unchecked');phase.value='privacy';editing.value=-1;consent.value=false;error.value='';busy.value=true
 try{
  for(let i=0;i<groups.value.length;i++){
   check(token);progress.value=`正在准备第${i+1}/${groups.value.length}份实际外发预览；尚未调用模型。`
   const approval=await prepareQuickPrivacyPreview(a.id,label(i),groups.value[i]!.files,dual.value,()=>check(token));check(token)
   approvals.value=approvals.value.map((old,n)=>n===i?approval:old)
  }
  progress.value='本批外发预览已准备。请查看图片、设置隐私状态并确认本批提交。'
 }catch(e){if(alive&&token===revision){error.value=(e as Error).message;progress.value='预览未完整准备，尚未上传或评分。可单独修正未完成的答卷，或重新选择。'}}finally{if(alive&&token===revision)busy.value=false}
}
function applyBulkPrivacy(){if(busy.value||editing.value>=0||phase.value!=='privacy'||!approvals.value.every(Boolean))return;statuses.value=applyPendingPrivacy(statuses.value,bulkPrivacy.value);consent.value=false}
function label(i:number){return `第${props.resume?.item.sequence||i+1}份`}
function invalidate(i:number){approvals.value=approvals.value.map((a,n)=>n===i?undefined:a);statuses.value[i]='unchecked';consent.value=false}
function approve(i:number,a:PrivacyApproval){approvals.value=approvals.value.map((old,n)=>n===i?a:old);editing.value=-1;consent.value=false}
function check(token:number){if(!alive||revision!==token)throw new Error('上传上下文已变化')}
async function send(){if(!ready.value||busy.value||!assignment.value||phase.value!=='privacy')return;const token=revision;busy.value=true;phase.value='sending';error.value='';let currentSequence=0;try{const fresh=(await request<{assignments:FormalAssignment[]}>('/api/assignments')).assignments.find(a=>a.id===assignmentId.value);check(token);if(!fresh||fresh.formal_layout_required)throw new Error('正式布局状态已改变，请走正式上传')
  const reserved=props.resume?{batch_id:props.resume.batch.batch_id,items:[props.resume.item]}:await request<NonNullable<typeof batch.value>>('/api/batches','POST',{assignment_id:assignmentId.value,class_name:'',batch_request_id:crypto.randomUUID(),items:groups.value.map(g=>({page_count:g.files.length}))});check(token);batch.value=reserved;window.dispatchEvent(new Event('batch-created'));if(reserved.items.length!==groups.value.length)throw new Error('预留份数不一致')
  for(let i=0;i<groups.value.length;i++){const item=reserved.items[i]!,sequence=props.resume?.item.sequence||i+1;currentSequence=sequence;const body=quickUploadBody(assignmentId.value,label(i),groups.value[i]!.files,approvals.value[i]!,statuses.value[i]!,true,dual.value,{batch_id:reserved.batch_id,submission_id:item.submission_id,sequence,idempotency_key:`${reserved.batch_id}:${sequence}`});await request('/api/submissions','POST',body);check(token);uploaded.value.push(item.submission_id);currentSequence=0}phase.value='done'
 }catch{if(alive&&revision===token){if(batch.value&&currentSequence){try{await request('/api/batches/upload-failed','POST',{batch_id:batch.value.batch_id,sequence:currentSequence})}catch{/* Original reservation stays blocked; no hidden retry. */}}phase.value='uncertain';error.value='上传结果未能完整确认，已停止且不会自动重试。请到批次记录核实，再原位重传或明确跳过；勿重建同一批次。'}}finally{if(alive&&revision===token)busy.value=false}}
async function refresh(){if(busy.value)return;busy.value=true;const token=revision;try{const list=[];for(const id of uploaded.value){list.push(await fetchDetail(id));check(token)}details.value=list}catch(e){if(alive&&token===revision)error.value=(e as Error).message}finally{if(alive&&token===revision)busy.value=false}}
onMounted(async()=>{busy.value=true;try{assignments.value=(await request<{assignments:FormalAssignment[]}>('/api/assignments')).assignments;if(props.resume)assignmentId.value=props.resume.batch.assignment_id;else if(props.initialContext?.assignmentId){if(!assignments.value.some(a=>a.id===props.initialContext?.assignmentId))throw new Error('评分标准不存在或无权访问');assignmentId.value=props.initialContext.assignmentId}}catch(e){if(alive)error.value=(e as Error).message}finally{if(alive)busy.value=false}})
onUnmounted(()=>{alive=false;revision++;files.value=[];groups.value=[];approvals.value=[]})
</script>
<template><section class="quick-upload"><h2>{{resume?'快速匿名原位重传':'快速匿名上传'}}</h2><p>快速匿名模式：无需花名册和预建正式考试。选择已有评分标准，核对照片与隐私后开始批改；符合条件的双评共识可批量采纳，分歧或异常需教师复核。</p><p v-if="resume">沿用批次{{resume.batch.batch_id}}第{{resume.item.sequence}}份，不创建新批次。</p><p v-if="error" role="alert">{{error}}</p>
 <fieldset :disabled="busy||phase!=='select'"><legend>选择与分组</legend><label v-if="!initialContext?.assignmentId" for="quick-assignment">快速匿名作业</label><select v-show="!initialContext?.assignmentId" id="quick-assignment" v-model="assignmentId" :disabled="!!resume" @change="changeAssignment"><option value="">请选择</option><option v-for="a in assignments" :key="a.id" :value="a.id">{{a.name}}{{a.formal_layout_required?'（须正式裁切）':''}}</option></select><p v-if="assignment?.formal_layout_required" role="alert">本作业需要正式布局和合法裁切，不能用快速匿名整页评分。<a href="#/upload">前往正式上传</a></p><label v-if="!resume">快速匿名每份页数<input v-model.number="pages" type="number" min="1" max="10" @change="reset"></label><label>快速匿名照片<input type="file" accept="image/jpeg,image/png" multiple @change="choose"></label><label class="check"><input type="checkbox" v-model="dual" @change="reset">使用异模型双评</label><p v-if="files.length&&grouped.error" role="alert">{{grouped.error}}</p><ol><li v-for="(g,i) in grouped.groups" :key="i">第{{resume?.item.sequence||i+1}}份 · {{g.files.length}}页</li></ol><label class="check"><input type="checkbox" v-model="groupConsent">已核对匿名照片顺序和每份页数</label><button :disabled="!assignment||assignment.formal_layout_required||!groupConsent||!!grouped.error" @click="begin">准备整批隐私预览</button></fieldset>
 <template v-if="phase==='privacy'&&assignment"><p role="status">{{progress}}</p><fieldset v-if="groups.length>1" :disabled="busy||editing>=0||!approvals.every(Boolean)"><legend>批量确认隐私状态</legend><p>查看下方本批实际外发图后，统一设置隐私状态；异常照片可单独遮挡修正。不需要逐份点击采用预览。</p><label for="quick-bulk-privacy">本批隐私状态</label><select id="quick-bulk-privacy" v-model="bulkPrivacy"><option value="">请选择状态</option><option value="pre_anonymized">导入前已避开身份信息</option><option value="redacted">确认预览图中身份信息已遮挡</option><option value="no_identity_visible">确认图中没有可辨认身份信息</option></select><button :disabled="!bulkPrivacy" @click="applyBulkPrivacy">应用到尚未确认的答卷</button></fieldset><article v-for="(g,i) in groups" :key="i" class="point-record"><h3>{{label(i)}} · {{g.files.length}}页</h3><button :disabled="busy||editing===i" @click="editing=i;consent=false">{{approvals[i]?'重新编辑本份（将使旧预览失效）':'编辑本份隐私与外发图'}}</button><PrivacyEditor v-if="editing===i" :key="i" :assignment="assignment" :files="g.files" :student-label="label(i)" :dual="dual" :initial-settings="approvals[i]?.settings" @invalidated="invalidate(i)" @ready="approve(i,$event)"/><p>{{approvals[i]?'实际外发图已准备，请核对':'本份预览未完成，请单独修正'}}</p><template v-if="approvals[i]&&editing!==i"><figure v-for="(image,j) in approvals[i]!.preview.images" :key="j"><img class="upload-preview" :src="image.data_url" :alt="label(i)+'实际外发图'+(j+1)" @error="invalidate(i);error='外发图片未能显示，请重新准备预览。'"><figcaption>{{label(i)}} · 第{{j+1}}页实际外发图</figcaption></figure></template><label :for="'quick-privacy-'+i">{{label(i)}}隐私状态</label><select :id="'quick-privacy-'+i" v-model="statuses[i]" :disabled="busy||editing>=0||!approvals[i]" @change="consent=false"><option value="unchecked">尚未确认</option><option value="pre_anonymized">导入前已避开身份信息</option><option value="redacted">确认身份信息已遮挡</option><option value="no_identity_visible">确认没有可辨认身份信息</option></select></article><label class="check"><input type="checkbox" v-model="consent" :disabled="busy||editing>=0||!approvals.every(Boolean)">我已逐份核对外发图与隐私，确认提交评分；模型调用可能产生费用</label><button :disabled="busy||!ready" @click="send">确认上传快速匿名答卷</button> <button :disabled="busy" @click="reset">取消并重新选择</button></template>
 <section v-if="['sending','done','uncertain'].includes(phase)"><h3>{{phase==='done'?'快速匿名上传完成':phase==='uncertain'?'请核实匿名批次状态':'正在上传匿名答卷'}}</h3><p v-if="batch">批次{{batch.batch_id}}；已确认上传{{uploaded.length}}/{{groups.length}}份。</p><a v-if="batch" :href="batchHref({batch_id:batch.batch_id,assignment_id:assignmentId,exam_id:batch.exam_id},initialContext)">重新打开本批成绩</a> · <a :href="contextHref('batches')">批次与异常恢复</a> · <a :href="contextHref('history')">查看全部匿名成绩</a><button v-if="uploaded.length" :disabled="busy" @click="refresh">只读刷新匿名答卷状态</button><BatchScores v-if="batch&&uploaded.length" :assignment-id="assignmentId" :batch-id="batch.batch_id" :refresh-key="uploaded.length" @changed="refresh" /></section>
</section></template>
