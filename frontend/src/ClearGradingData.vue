<script setup lang="ts">
import {ref,watch} from 'vue'
import {previewAnswers,removeAnswers,dataChanged,type DataPreview} from './answerManagement'
import {useTaskGuard} from './taskGuard'
const props=defineProps<{assignmentId:string;examId?:string}>(),emit=defineEmits<{close:[];cleared:[message:string]}>()
const busy=ref(false),error=ref(''),preview=ref<DataPreview>()
useTaskGuard(busy)
const scope=()=>({assignment_id:props.assignmentId,...(props.examId!==undefined?{exam_id:props.examId}:{})})
async function load(){busy.value=true;error.value='';preview.value=undefined;try{preview.value=await previewAnswers(scope(),true)}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function clear(){if(!preview.value||preview.value.blocked)return;busy.value=true;error.value='';try{const r=await removeAnswers(scope(),preview.value,true);busy.value=false;emit('cleared',r.warning||'本场阅卷数据已清空，考试配置和花名册保留，可立即重新上传');dataChanged()}catch(e){preview.value=undefined;error.value=(e as Error).message}finally{busy.value=false}}
watch(()=>[props.assignmentId,props.examId],load,{immediate:true})
</script>
<template><section class="delete-confirm" aria-label="清空阅卷数据确认"><h3>清空本场阅卷数据</h3><p v-if="busy">正在处理…</p><p v-if="error" role="alert">{{error}} <button @click="load" :disabled="busy">重新读取范围</button></p><template v-if="preview"><p>{{preview.name}}：主观题 {{preview.subjective}} 份、客观题 {{preview.objective}} 份、图片 {{preview.photos}} 张；{{preview.batches}} 个主观题批次、{{preview.jobs}} 个客观题任务。</p><p>将清空{{examId?'当前这次正式阅卷记录':'该考试下全部阅卷记录'}}的答卷、图片、评分、终评、复核和身份关联。保留考试配置、题目、答案、评分标准、模型设置、班级及花名册。此操作无法撤销。</p><p v-if="preview.blocked" role="alert">仍有正在执行的任务，请先停止并等待当前调用结束，再重新读取范围。</p><button class="danger" :disabled="busy||preview.blocked" @click="clear">确认清空阅卷数据</button><button v-if="preview.blocked" :disabled="busy" @click="load">重新读取范围</button></template> <button :disabled="busy" @click="emit('close')">取消</button></section></template>
