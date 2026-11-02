<script setup lang="ts">
import {computed,onUnmounted,ref,shallowRef,watch} from 'vue'
import ConsensusScores from './ConsensusScores.vue'
import ReviewQueue from './ReviewQueue.vue'
import SubmissionDetail from './SubmissionDetail.vue'
import {fetchDetail,type Detail} from './records'
const props=defineProps<{assignmentId:string;examId?:string;batchId:string;refreshKey:number;recoveryInline?:boolean}>()
const emit=defineEmits<{changed:[];recover:[];reviewed:[detail:Detail]}>()
const reviewId=ref(''),detailId=ref(''),error=ref(''),opening=ref(false),reviewItems=shallowRef<Detail[]>([])
const expectedExam=computed(()=>props.examId||reviewItems.value.find(d=>d.id===reviewId.value)?.exam_id)
let revision=0
function reset(){revision++;reviewId.value='';detailId.value='';error.value='';opening.value=false;reviewItems.value=[]}
watch(()=>[props.assignmentId,props.examId,props.batchId],reset)
onUnmounted(()=>revision++)
async function open(id:string,ids:string[],detail=false){
 if(opening.value)return
 const n=++revision;opening.value=true;error.value=''
 try{
  const current=await fetchDetail(id);if(n!==revision)return
  if(current.assignment_id!==props.assignmentId||current.batch_id!==props.batchId||(props.examId&&current.exam_id!==props.examId))throw Error('答卷不属于当前批次，请刷新核实。')
  const available=await Promise.all(ids.map(sid=>sid===id?current:fetchDetail(sid)))
  if(n!==revision)return
  reviewItems.value=available.filter(d=>d.assignment_id===props.assignmentId&&d.batch_id===props.batchId&&d.exam_id===current.exam_id)
  if(detail)detailId.value=id;else reviewId.value=id
 }catch(e){if(n===revision)error.value=(e as Error).message}finally{if(n===revision)opening.value=false}
}
function saved(d:Detail){reviewItems.value=reviewItems.value.map(r=>r.id===d.id?d:r);emit('reviewed',d);emit('changed')}
</script>
<template><section class="batch-scores" aria-label="本批双评成绩">
<p v-if="error" role="alert">{{error}}</p><p v-if="opening" role="status">正在打开本批答卷…</p>
<ReviewQueue v-if="reviewId" v-model:id="reviewId" :items="reviewItems" :expected-exam-id="expectedExam" back-label="返回本批成绩" @saved="saved"/>
<template v-else-if="detailId"><button @click="detailId=''">返回本批成绩</button><SubmissionDetail :id="detailId"/></template>
<ConsensusScores v-else :assignment-id="assignmentId" :exam-id="examId" :batch-id="batchId" auto-refresh :refresh-key="refreshKey" :recovery-inline="recoveryInline" @recover="emit('recover')" @review="open" @detail="id=>open(id,[id],true)" @changed="emit('changed')"/>
</section></template>
