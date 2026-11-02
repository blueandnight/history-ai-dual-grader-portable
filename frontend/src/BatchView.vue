<script setup lang="ts">
import {nextTick,onUnmounted,ref,watch} from 'vue'
import {fetchBatchRecords,batchTime,batchState} from './batchRecords'
import {examHref,type ExamContext} from './navigation'
import type {Batch} from './records'
import BatchDeleteConfirm from './BatchDeleteConfirm.vue'
import type {BatchDeleted} from './batchDeletion'
import {hasUnsavedTask} from './taskGuard'
import BatchScores from './BatchScores.vue'
import BatchGradingControls from './BatchGradingControls.vue'
import RecoveryManager from './RecoveryManager.vue'
const props=defineProps<{context:ExamContext;batchId:string}>()
const batch=ref<Batch>(),busy=ref(false),error=ref(''),manage=ref(false),refreshKey=ref(0)
const deleting=ref(false),deleteMessage=ref('')
function deleted(r:BatchDeleted){batch.value=undefined;deleting.value=false;deleteMessage.value=(r.warning||'本批已删除，考试与其他批次保留。')+(r.shared_crop_runs_preserved?' 被其他答卷复用的裁切缓存已保留。':'')}
const recoveryRoot=ref<HTMLElement>()
async function showRecovery(){manage.value=true;await nextTick();recoveryRoot.value?.scrollIntoView({block:'start'})}
let revision=0
async function load(){const n=++revision;busy.value=true;error.value='';try{if(!props.batchId)throw Error('缺少批次编号');const r=await fetchBatchRecords(props.context,props.batchId);if(n!==revision)return;const b=r.find(b=>b.batch_id===props.batchId);if(!b)throw Error('本批记录不存在或不属于当前考试，请返回批次列表重新选择');batch.value=b;refreshKey.value++}catch(e){if(n===revision){error.value=(e as Error).message}}finally{if(n===revision)busy.value=false}}
watch(()=>[props.batchId,props.context.assignmentId,props.context.examId],load,{immediate:true});onUnmounted(()=>revision++)
</script>
<template><section aria-label="本批答卷工作台"><div class="context-actions"><h2>本批答卷</h2><a :href="examHref('batches',context)">返回批次列表</a></div><p v-if="busy" role="status">正在读取本批记录…</p><p v-if="error" role="alert">{{error}}</p><p v-if="deleteMessage" role="status">{{deleteMessage}}</p><template v-if="batch"><button class="danger" :disabled="busy||hasUnsavedTask||deleting" @click="deleting=true">删除本批次</button><BatchDeleteConfirm v-if="deleting" :batch="batch" @close="deleting=false" @deleted="deleted"/><p>{{batch.assignment_name}} · {{batchTime(batch.created_at)}} · {{batch.total_sequences}}份 · {{batchState(batch)}}</p><p class="muted">已接收{{batch.uploaded_count}}份，处理结束{{batch.processed_count}}份；处理结束不等于成绩已确认。</p><BatchGradingControls v-if="batch.status!=='completed'" :assignment-id="batch.assignment_id" :exam-id="batch.exam_id" :batch-id="batch.batch_id" @changed="load"/><button v-if="batch.status!=='completed'" @click="manage=!manage">{{manage?'收起恢复操作':'处理本批上传与评分异常'}}</button><div v-if="manage" ref="recoveryRoot"><RecoveryManager :context="{assignmentId:batch.assignment_id,examId:batch.exam_id||''}" :batch-id="batch.batch_id" @changed="load"/></div><BatchScores :key="batch.batch_id" :assignment-id="batch.assignment_id" :exam-id="batch.exam_id" :batch-id="batch.batch_id" :refresh-key="refreshKey" recovery-inline @recover="showRecovery" @changed="load"/></template></section></template>
