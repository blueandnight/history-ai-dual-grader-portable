<script setup lang="ts">
import { computed,onMounted,ref } from 'vue'
import BatchDeleteConfirm from './BatchDeleteConfirm.vue'
import type {BatchDeleted} from './batchDeletion'
import {hasUnsavedTask} from './taskGuard'
import RecoveryManager from './RecoveryManager.vue'
import BatchGradingControls from './BatchGradingControls.vue'
import { useReadonly } from './useReadonly'
import { type Batch } from './records'
import {batchHref,batchTime,batchState,fetchBatchRecords} from './batchRecords'
import {examHref,type ExamContext} from './navigation'
const props=defineProps<{context?:ExamContext}>()
const { data, busy, error, load } = useReadonly<Batch[]>()
const manage=ref(false),deleting=ref<Batch>(),message=ref('')
async function deleted(r:BatchDeleted){deleting.value=undefined;message.value=r.warning||'本批答卷、照片、成绩与关联已删除，考试及其他批次保留。';if(r.shared_crop_runs_preserved)message.value+=' 被其他答卷复用的裁切缓存已保留。';await refresh()}
const visible=computed(()=>data.value?.filter(b=>(!props.context?.assignmentId||b.assignment_id===props.context.assignmentId)&&(!props.context?.examId||b.exam_id===props.context.examId)))
const refresh=()=>load(()=>fetchBatchRecords(props.context))
onMounted(refresh)
</script>
<template><section><h2>批改进度与恢复</h2><p>显示最近100批，包含已完成的主观题批次。点击本批成绩继续查看、采纳或终评；异常恢复仍需明确确认。</p><p class="context-actions"><a :href="examHref('objective',context||{assignmentId:'',examId:''})">客观题进度与异常复核</a><a v-if="context?.assignmentId||context?.examId" href="#/batches?mode=formal">查看全部历史任务（清除考试筛选）</a></p><button :disabled="!!deleting" @click="manage=!manage">{{manage?'关闭异常恢复操作':'打开异常恢复操作'}}</button>
  <button @click="refresh" :disabled="busy||!!deleting">刷新批次</button><p v-if="busy" aria-live="polite">正在加载…</p><p v-if="error" role="alert">{{ error }}</p>
<p v-if="message" role="status">{{message}}</p><BatchDeleteConfirm v-if="deleting" :key="deleting.batch_id" :batch="deleting" @close="deleting=undefined" @deleted="deleted"/>
  <p v-if="visible && !visible.length">当前没有上传批次。</p>
  <div v-if="!manage && visible?.length" class="table-wrap" tabindex="0" aria-label="批次表格"><table><thead><tr><th>考试 / 上传时间</th><th>班级</th><th>状态</th><th>总份数</th><th>处理进度</th><th>操作</th></tr></thead><tbody>
    <tr v-for="b in visible" :key="b.batch_id" :data-batch-id="b.batch_id"><td><strong>{{b.assignment_name}}</strong><p>{{batchTime(b.created_at)}}</p></td><td>{{ b.class_name || '不关联花名册' }}</td><td>{{batchState(b)}}</td><td>{{ b.total_sequences }}</td><td>已接收{{b.uploaded_count}}份 · 已处理{{b.processed_count}}份</td><td><a class="button secondary" :href="batchHref(b,context)">{{b.status==='paused'?'继续处理本批':'本批成绩'}}</a><button class="danger" :disabled="busy||!!deleting||hasUnsavedTask" @click="deleting=b">删除本批次</button></td></tr>
  </tbody></table></div><p><a :href="examHref('history',context||{assignmentId:'',examId:''})">查看已完成答卷与历史记录</a></p>
<BatchGradingControls :assignment-id="context?.assignmentId" :exam-id="context?.examId" @changed="refresh" />
<RecoveryManager v-if="manage" :context="context" @changed="refresh"/></section></template>
