<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref} from 'vue'
import {useTaskGuard} from './taskGuard'
import {request} from './api'
import {examHref,type ExamContext} from './navigation'
const props=defineProps<{context:ExamContext}>()
const emit=defineEmits<{context:[value:ExamContext];busy:[value:boolean]}>()
const pending=ref(false)
useTaskGuard(pending)
const frame=ref<HTMLIFrameElement>(),height=ref(1200),error=ref('')
const exams=ref<{exam_id:string;assignment_id:string;title:string;ready:boolean;reason?:string}[]>([]),selected=ref('')
let verifiedAssessment='',verifiedJob='',revision=0
const assessment=new URLSearchParams(location.hash.split('?')[1]||'').get('assessment')||''
const restoredJob=new URLSearchParams(location.hash.split('?')[1]||'').get('job')||''
const src=computed(()=>'/objective/?'+new URLSearchParams({embedded:'1',workspace_mode:props.context.mode||'formal',...(props.context.examId?{exam_id:props.context.examId}:{}),...(assessment?{assessment_id:assessment}:{}),...(restoredJob?{job_id:restoredJob}:{})}))
async function choose(){if(!exams.value.some(e=>e.exam_id===selected.value&&e.assignment_id===props.context.assignmentId))return;location.hash=examHref('objective',{...props.context,examId:selected.value})}
async function message(event:MessageEvent){
 if(event.origin!==location.origin||event.source!==frame.value?.contentWindow||event.data?.type!=='objective-workspace')return
 if(event.data.sessionExpired===true){window.dispatchEvent(new Event('session-expired'));return}
 if(Number.isFinite(event.data.height))height.value=Math.max(600,Math.min(30000,event.data.height))
 pending.value=event.data.busy===true||event.data.dirty===true
 emit('busy',event.data.busy===true)
 if(event.data.contextReady===false)return
 const id=event.data.assessmentId
 const job=event.data.jobId||''
 if(!/^[a-f0-9]{32}$/.test(id||'')||(job&&!/^[a-f0-9]{32}$/.test(job))||(id===verifiedAssessment&&job===verifiedJob))return
 const n=++revision
 try{const a=await request<{source_exam_id?:string|null}>('/objective/api/assessments/'+id)
  if(n!==revision)return
  if((a.source_exam_id||'')!==props.context.examId)throw new Error('答案键与当前考试不一致')
  if(job){const j=await request<{job:{assessment_id:string}}>('/objective/api/jobs/'+job+'?assessment_id='+id);if(n!==revision)return;if(j.job.assessment_id!==id)throw new Error('任务与当前答案键不一致')}
  verifiedAssessment=id;verifiedJob=job
  const p=new URLSearchParams(location.hash.split('?')[1]||'');p.set('assessment',id);if(job)p.set('job',job);else p.delete('job');history.replaceState(null,'',location.hash.split('?')[0]+'?'+p)
 }catch(e){error.value=(e as Error).message}
}
onMounted(async()=>{window.addEventListener('message',message);if(props.context.mode!=='quick'&&!props.context.examId&&props.context.assignmentId){try{exams.value=(await request<{exams:typeof exams.value}>('/objective/api/source-exams')).exams.filter(e=>e.assignment_id===props.context.assignmentId);if(exams.value.length===1){selected.value=exams.value[0]!.exam_id;await choose()}}catch(e){error.value=(e as Error).message}}})
onUnmounted(()=>{revision++;window.removeEventListener('message',message);emit('busy',false)})
</script>
<template><section><h2>客观题批改与复核</h2><p v-if="error" role="alert">{{error}}</p>
 <template v-if="context.mode!=='quick'&&!context.examId"><p>请先选择考试及本次正式阅卷记录。此操作只读取候选，不会创建答案键。</p><label>本次阅卷记录<select v-model="selected"><option value="">请选择</option><option v-for="e in exams" :key="e.exam_id" :value="e.exam_id">{{e.title}} · {{e.exam_id.slice(0,8)}}{{e.ready?'':' · '+e.reason}}</option></select></label><button :disabled="!selected" @click="choose">使用本次阅卷记录</button></template>
 <iframe v-else ref="frame" title="客观题上传与复核" class="objective-frame" :src="src" :style="{height:height+'px'}"></iframe>

</section></template>
