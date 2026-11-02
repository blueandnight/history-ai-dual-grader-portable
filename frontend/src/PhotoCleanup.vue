<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import type {Submission} from './history'
import {cleanupPhotos,photoCleanupReason} from './examMaintenance'
import {useTaskGuard} from './taskGuard'
const props=defineProps<{assignmentId:string;examId:string;submissions:Submission[]}>()
const emit=defineEmits<{cleaned:[ids:string[]]}>()
const selected=ref<string[]>([]),busy=ref(false),confirmed=ref(false),error=ref(''),results=ref<string[]>([]),stopped=ref(false)
let alive=true
onUnmounted(()=>{alive=false})
useTaskGuard(busy)
const rows=computed(()=>props.submissions.filter(s=>!props.examId||s.exam_id===props.examId))
const eligible=computed(()=>rows.value.filter(s=>!photoCleanupReason(s)))
const chosen=computed(()=>eligible.value.filter(s=>selected.value.includes(s.id)))
const pages=computed(()=>chosen.value.reduce((n,s)=>n+(s.photos?.length||0),0))
watch(selected,()=>{confirmed.value=false},{deep:true})
watch(()=>[props.assignmentId,props.examId],()=>{selected.value=[];confirmed.value=false;results.value=[];error.value='';stopped.value=false})
async function clean(){
  if(busy.value||stopped.value||!confirmed.value||!chosen.value.length)return
  const items=[...chosen.value];busy.value=true;results.value=[];error.value=''
  try{for(const s of items){
    if(!alive)break
    try{await cleanupPhotos(s,props.assignmentId);if(!alive)break;results.value.push((s.marking_code||s.id)+'：已清理照片，成绩保留');emit('cleaned',[s.id])}
    catch{stopped.value=true;error.value='已停止后续清理：本份操作结果未能确认或服务器拒绝。请刷新历史核对；不会自动重试。';break}
  }}finally{busy.value=false;selected.value=[];confirmed.value=false}
}
</script>
<template><details class="photo-cleanup"><summary>照片管理 · 批量清理主观题照片（含未终评答卷）</summary>
<p>只清理本列表中的主观题原图、处理图与证据裁切图，保留成绩、评语、答卷及关联记录。客观题照片和已有备份不在此次清理范围内。</p>
<fieldset :disabled="busy||stopped"><legend>选择需要清理的答卷</legend>
<button type="button" @click="selected=eligible.map(s=>s.id)" :disabled="!eligible.length">选择全部可清理答卷</button> <button type="button" @click="selected=[]">清空选择</button>
<p v-if="!rows.length">当前没有主观题答卷。</p>
<label class="check" v-for="s in rows" :key="s.id"><input type="checkbox" v-model="selected" :value="s.id" :disabled="!!photoCleanupReason(s)">{{s.marking_code||'答卷 '+s.id.slice(0,6)}} · {{photoCleanupReason(s)||(s.photos?.length+'张原始照片')}}<span v-if="!photoCleanupReason(s)&&(!s.confirmed_at||s.teacher_reconfirmation_required)"> · 未终评，可弃用清理</span></label>
<p aria-live="polite">已选{{chosen.length}}份答卷，{{pages}}张原始照片；派生图片将一并清理。</p>
<label class="check"><input type="checkbox" v-model="confirmed" :disabled="!chosen.length">我已核对所选答卷，确认不再需要原图。清理无法撤销；未终评答卷将无法继续原图复核和正式终评，需要重新上传。已有成绩保留。</label>
<button type="button" class="danger" @click="clean" :disabled="!confirmed||!chosen.length">{{busy?'正在逐份清理…':'清理所选照片'}}</button>
</fieldset><p v-if="error" role="alert">{{error}}</p><ul v-if="results.length" aria-live="polite"><li v-for="(r,i) in results" :key="i">{{r}}</li></ul>
</details></template>
