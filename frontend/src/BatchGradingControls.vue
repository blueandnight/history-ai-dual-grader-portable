<script setup lang="ts">
import {onUnmounted,ref,watch} from 'vue'
import {request} from './api'
import {fetchBatches,type Batch} from './records'
const props=defineProps<{assignmentId?:string;examId?:string;batchId?:string}>()
const emit=defineEmits<{changed:[];stopped:[]}>()
const batches=ref<Batch[]>([]),busy=ref(false),error=ref(''),message=ref(''),confirming=ref('')
let revision=0
async function load(){const token=++revision;busy.value=true;error.value='';confirming.value='';try{const rows=await fetchBatches();if(token===revision)batches.value=rows.filter(b=>(!props.assignmentId||b.assignment_id===props.assignmentId)&&(!props.examId||b.exam_id===props.examId)&&(!props.batchId||b.batch_id===props.batchId))}catch{if(token===revision){batches.value=[];error.value='批改控制状态读取失败，请刷新后重试。'}}finally{if(token===revision)busy.value=false}}
async function change(b:Batch){if(busy.value)return;busy.value=true;error.value='';const token=revision;const action=b.can_continue_confirmed?'continue_confirmed':b.grading_stopped?'resume':'stop';try{const result=await request<{running:number;waiting:number}>('/api/batches/grading-control','POST',{batch_id:b.batch_id,assignment_id:b.assignment_id,exam_id:b.exam_id||'',action,confirmed:true,expected_sequence:b.next_sequence});if(token!==revision)return;if(action==='stop')emit('stopped');await load();message.value=action==='stop'?`已停止后续批改；${result.waiting}份已上传答卷不会开始评分。${result.running?'当前已开始的答卷仍会完成评分并保存，可能继续产生费用。':'已完成结果和原图保留。'}`:action==='continue_confirmed'?'已保留当前答卷的确认成绩，并继续后续批改。':'已提交继续请求；仅继续尚未开始的任务，异常项仍需原位处理。';emit('changed')}catch{if(token===revision){confirming.value='';error.value='操作未能确认。请刷新核实状态，不会自动重试；当前请求未结束时不能继续。'}}finally{if(token===revision)busy.value=false}}
watch(()=>[props.assignmentId,props.examId,props.batchId],()=>{message.value='';void load()},{immediate:true})
onUnmounted(()=>{revision++})
</script>
<template><section aria-label="批改停止与继续" class="batch-grading-controls">
<p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p>
<div v-for="b in batches" :key="b.batch_id" class="context-actions"><span>批次 {{b.batch_id.slice(0,8)}} · {{b.grading_stopped?'已停止后续批改':b.status==='paused'?'异常暂停':'进行中'}}</span>
<button :disabled="busy" :class="b.grading_stopped||b.can_continue_confirmed?'':'danger'" @click="confirming=b.batch_id">{{b.can_continue_confirmed?'保留已确认成绩，继续批改':b.grading_stopped?'继续本批剩余批改':'停止本批剩余批改'}}</button>
<div v-if="confirming===b.batch_id" role="group" aria-label="确认批改控制">
<p>{{b.can_continue_confirmed?'当前答卷已确认成绩，不会重新评分。继续会启动后续答卷的模型调用，可能产生费用。':b.grading_stopped?'继续会启动尚未开始的任务，模型调用可能产生费用；已完成答卷不会重新评分。已开始的请求尚未结束时，请稍后刷新再继续。':'停止后不再启动本批后续任务，也不接受本批新增上传。当前已开始的答卷会继续完成评分，可能继续产生模型费用；原图和已完成成绩保留。'}}</p>
<button :disabled="busy" @click="change(b)">{{b.grading_stopped||b.can_continue_confirmed?'确认继续批改':'确认停止剩余批改'}}</button> <button :disabled="busy" @click="confirming=''">取消</button>
</div></div><button v-if="batches.length||error" :disabled="busy" @click="load">刷新停止状态</button>
</section></template>
