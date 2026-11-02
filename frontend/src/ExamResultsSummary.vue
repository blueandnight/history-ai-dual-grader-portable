<script setup lang="ts">
import {ref,watch} from 'vue'
import {request} from './api'
import UnifiedScores from './UnifiedScores.vue'
const props=defineProps<{examId:string;assignmentId:string;refreshKey?:string|number}>()
const counts=ref<{objective_summary:{count:number};subjective_summary:{count:number};complete_count:number;incomplete_count:number}>(),error=ref(''),opened=ref(false)
let revision=0
async function load(){const token=++revision;error.value='';try{const data=await request<typeof counts.value>('/objective/api/unified-scores/report?records=all&exam_id='+encodeURIComponent(props.examId));if(token===revision)counts.value=data}catch{if(token===revision)error.value='成绩概览暂未读取，可刷新重试；已保存的答卷不受影响。'}}
watch(()=>[props.examId,props.refreshKey],()=>{counts.value=undefined;void load()},{immediate:true})
</script>
<template><section aria-label="本考试成绩闭环" class="notice"><div class="context-actions"><h3>本考试成绩与分析</h3><a class="button primary" :href="'/objective/api/unified-scores/export.xlsx?records=all&scope=all&exam_id='+encodeURIComponent(examId)">导出 Excel（全部可用结果）</a><button @click="load">刷新概览</button><button @click="opened=!opened">{{opened?'收起成绩与分析':'查看成绩与各题正确率'}}</button></div><p v-if="counts">客观题有效 {{counts.objective_summary.count}} 份 · 主观题已确认 {{counts.subjective_summary.count}} 份 · 整卷完整 {{counts.complete_count}} 份 · 尚有 {{counts.incomplete_count}} 条缺失、未关联或待核对记录。</p><p>未处理的异常不影响其他成绩导出；AI参考与正式成绩在表内分列，缺失留空。</p><p v-if="error" role="status">{{error}}</p><UnifiedScores v-if="opened" :key="examId" :refresh-key="refreshKey" :assignment-id="assignmentId" :selected-exam-id="examId"/></section></template>
