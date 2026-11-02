<script setup lang="ts">
import {onMounted,onUnmounted,ref,watch} from 'vue'
import {batchHref,batchTime,batchState,fetchBatchRecords} from './batchRecords'
import {examHref,type ExamContext} from './navigation'
import type {Batch} from './records'
const props=defineProps<{context?:ExamContext}>(),rows=ref<Batch[]>([]),error=ref('')
let revision=0
async function load(){const n=++revision;error.value='';try{const r=await fetchBatchRecords(props.context);if(n===revision)rows.value=r.slice(0,3)}catch{if(n===revision){rows.value=[];error.value='最近批次暂不可用'}}}
watch(()=>[props.context?.assignmentId,props.context?.examId],load,{immediate:true});onMounted(()=>window.addEventListener('batch-created',load));onUnmounted(()=>{revision++;window.removeEventListener('batch-created',load)})
</script>
<template><section class="recent-batches" aria-label="最近批次"><div class="context-actions"><strong>最近批次</strong><a :href="examHref('batches',context||{assignmentId:'',examId:''})">批次列表</a><button @click="load">刷新批次</button></div><p v-if="error" role="alert">{{error}}</p><div class="recent-list"><a v-for="b in rows" :key="b.batch_id" :href="batchHref(b,context)"><span>{{batchTime(b.created_at)}} · {{b.total_sequences}}份</span><small>{{context?.assignmentId?batchState(b):b.assignment_name}} · 打开本批成绩</small></a></div><p v-if="!rows.length&&!error" class="muted">上传后，可从这里重新打开本批成绩。</p></section></template>
<style scoped>.recent-batches{margin:12px 0 20px;padding:12px 16px;border:1px solid #dce3ec;background:#fff}.recent-batches p{margin:6px 0;font-size:13px}.recent-list{display:flex;gap:12px;flex-wrap:wrap}.recent-list a{display:grid;gap:2px;padding:6px 14px 6px 0;min-width:0;max-width:100%;flex:0 1 230px;overflow-wrap:anywhere}.recent-list small{font-size:12px;color:#64748b}</style>
