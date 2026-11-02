<script setup lang="ts">
import {nextTick,onMounted,onUnmounted,ref} from 'vue'
import {useTaskGuard} from './taskGuard'
import type {Batch} from './records'
import {batchTime} from './batchRecords'
import {previewBatchDelete,deleteBatch,type BatchDeletePreview,type BatchDeleted} from './batchDeletion'
const props=defineProps<{batch:Batch}>()
const emit=defineEmits<{close:[];deleted:[result:BatchDeleted]}>()
const preview=ref<BatchDeletePreview>(),busy=ref(false),confirmed=ref(false),uncertain=ref(false),error=ref(''),root=ref<HTMLElement>()
useTaskGuard(busy)
let alive=true
onUnmounted(()=>{alive=false})
async function load(){if(busy.value)return;busy.value=true;confirmed.value=false;uncertain.value=false;preview.value=undefined;error.value='';try{const p=await previewBatchDelete(props.batch);if(alive)preview.value=p}catch{if(alive)error.value='无法读取删除范围。批次可能已删除或归属有变化，请关闭后刷新列表核实。'}finally{if(alive)busy.value=false}}
async function remove(){if(busy.value||uncertain.value||!confirmed.value||!preview.value)return;busy.value=true;error.value='';try{const result=await deleteBatch(props.batch,preview.value,confirmed.value);if(!alive)return;busy.value=false;confirmed.value=false;await nextTick();emit('deleted',result)}catch{if(alive){uncertain.value=true;confirmed.value=false;error.value='删除结果未能确认、范围已变化或有任务正在进行。请重新读取或关闭后刷新列表核实；不会自动重试。'}}finally{if(alive)busy.value=false}}
onMounted(async()=>{await load();await nextTick();root.value?.scrollIntoView({block:'center'});root.value?.focus({preventScroll:true})})
</script>
<template><section ref="root" tabindex="-1" class="delete-confirm" aria-label="删除批次确认"><h3>永久删除本批次</h3><p>{{batch.assignment_name}} · {{batch.class_name||'匿名'}} · {{batchTime(batch.created_at)}} · 批次 {{batch.batch_id}}</p><p>删除本批主观答卷、原图、处理图、证据图、主观成绩和答卷关联，不可撤销。考试、客观答卷与成绩、花名册、其他批次、既有备份和审计记录保留。</p>
<p v-if="busy" role="status">正在核实或提交，请勿离开…</p><p v-if="error" role="alert">{{error}}</p>
<template v-if="preview"><p>本批预约 {{preview.total}} 份；已接收 {{preview.submissions}} 份，其中已有确认成绩 {{preview.confirmed}} 份；照片 {{preview.photos}} 张；关联 {{preview.links}} 条。</p><p v-if="preview.pending||preview.unuploaded">将同时取消 {{preview.pending}} 份未执行任务及 {{preview.unuploaded}} 份未上传预约。</p><p v-if="preview.blocked" role="alert">本批仍有正在执行的评分任务，暂不能删除。请等待请求结束后重新读取。</p><label class="check"><input type="checkbox" v-model="confirmed" :disabled="busy||uncertain||preview.blocked">我确认永久删除这一批及上述数据，知悉已有确认成绩也会删除。</label><button class="danger" :disabled="busy||uncertain||!confirmed||preview.blocked" @click="remove">永久删除本批次</button></template>
<button :disabled="busy" @click="load">重新读取删除范围</button><button :disabled="busy" @click="emit('close')">取消</button></section></template>
<style scoped>.delete-confirm{border:1px solid #e3b5b5;background:#fff9f9;padding:18px;margin:18px 0}.delete-confirm button{margin-right:8px;white-space:nowrap}.delete-confirm p{overflow-wrap:anywhere}.delete-confirm .check{display:flex;align-items:flex-start;gap:8px;margin:16px 0}</style>
