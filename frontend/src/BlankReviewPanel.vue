<script setup lang="ts">
import {computed,onUnmounted,ref} from 'vue'
import {fetchDetail,type Detail} from './records'
import {confirmBlank,originalUrl,reviewable} from './teacherReview'
import {useTaskGuard} from './taskGuard'
const props=defineProps<{ids:string[];assignmentId:string;examId?:string;batchId?:string}>()
const emit=defineEmits<{changed:[];close:[]}>()
const details=ref<Detail[]>([]),busy=ref(false),opened=ref(false),seen=ref<string[]>([]),selected=ref<string[]>([]),confirmed=ref(false),error=ref(''),message=ref(''),uncertain=ref(false)
let alive=true
onUnmounted(()=>{alive=false})
useTaskGuard(computed(()=>busy.value||selected.value.length>0))
const key=(id:string,q:string)=>JSON.stringify([id,q])
const candidates=(d:Detail)=>d.result.sub_questions?.filter(q=>q.answer_status==='unanswered_candidate')||[]
const loaded=(d:Detail)=>!!d.photos?.length&&d.photos.every((_,i)=>seen.value.includes(key(d.id,String(i))))
const count=computed(()=>details.value.reduce((n,d)=>n+candidates(d).length,0))
function assertScope(d:Detail){if(d.assignment_id!==props.assignmentId||(props.examId&&d.exam_id!==props.examId)||(props.batchId&&d.batch_id!==props.batchId)||d.confirmed_at||!reviewable(d)||!d.photos?.length||d.photos_deleted||d.result.photos_discarded_unconfirmed)throw new Error('答卷归属、状态或原图已变化，请刷新后单独核对。')}
async function load(){if(busy.value)return;busy.value=true;error.value='';selected.value=[];confirmed.value=false;seen.value=[];opened.value=false;uncertain.value=false;details.value=[];try{const rows:Detail[]=[];for(const id of props.ids){const d=await fetchDetail(id);assertScope(d);if(candidates(d).length)rows.push(d)}if(alive)details.value=rows}catch(e){if(alive)error.value=(e as Error).message}finally{if(alive)busy.value=false}}
function chooseLoaded(){selected.value=details.value.filter(loaded).flatMap(d=>candidates(d).map(q=>key(d.id,q.sub_question_id)));confirmed.value=false}
async function save(){if(busy.value||uncertain.value||!confirmed.value||!selected.value.length)return;busy.value=true;error.value='';let saved=0;try{for(const d of details.value){for(const q of candidates(d)){if(!alive)throw new Error('核对页面已关闭，后续已停止。');if(!selected.value.includes(key(d.id,q.sub_question_id)))continue;if(!loaded(d))throw new Error('请先打开并核对该份全部原图。');const fresh=await fetchDetail(d.id);assertScope(fresh);if(!alive||fresh.exam_id!==d.exam_id||fresh.batch_id!==d.batch_id)throw new Error('答卷上下文已变化，已停止。');if(JSON.stringify(fresh.photos)!==JSON.stringify(d.photos)||!candidates(fresh).some(x=>x.sub_question_id===q.sub_question_id))throw new Error('原图或小题状态已变化，请重新读取。');await confirmBlank(d.id,q.sub_question_id);saved++;q.answer_status='blank_confirmed';selected.value=selected.value.filter(k=>k!==key(d.id,q.sub_question_id))}}message.value=`已记录 ${saved} 道空白题核对。尚未保存正式成绩；其他评分点仍按原规则处理。`;confirmed.value=false;emit('changed')}catch(e){uncertain.value=true;confirmed.value=false;error.value=`已确认返回成功 ${saved} 道；后续已停止。${(e as Error).message} 请重新读取服务器记录，不会自动重试。`;emit('changed')}finally{busy.value=false}}
function close(){if(!busy.value&&(!selected.value.length||window.confirm('放弃尚未提交的空白题选择？已保存的核对记录会保留。')))emit('close')}
void load()
</script>
<template><section class="blank-panel" aria-label="集中核对疑似空白"><h4>集中核对疑似空白（{{count}}道）</h4><p>核对各份原图，选择确实未作答的小题。只处理主评分结果中的疑似空白；两模型判断冲突或有作答的小题仍需单独复核。不会自动生成正式成绩。</p>
<div class="context-actions"><button :disabled="busy" @click="load">重新读取</button><button :disabled="busy||!details.length" @click="opened=true">展开本组原图</button><button :disabled="busy||uncertain" @click="chooseLoaded">选中已打开原图的小题</button><button :disabled="busy" @click="close">关闭集中核对</button></div>
<p v-if="busy" role="status">正在处理，请勿离开…</p><p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p>
<article v-for="d in details" :key="d.id"><h5>{{d.marking_code||'答卷 '+d.id.slice(0,6)}}</h5><div v-if="opened" class="blank-photos"><a v-for="(_,i) in d.photos" :key="i" :href="originalUrl(d.id,i+1)" target="_blank" rel="noopener"><img loading="lazy" :src="originalUrl(d.id,i+1)" :alt="'本份原图第'+(i+1)+'张，点击放大'" @load="seen.includes(key(d.id,String(i)))||seen.push(key(d.id,String(i)))" @error="seen=seen.filter(k=>k!==key(d.id,String(i)));error='原图读取失败，该份不可确认。'"></a></div><p v-if="!loaded(d)" class="muted">全部原图打开后可选择；图片较小时请点击放大核对。</p><label v-for="q in candidates(d)" :key="q.sub_question_id" class="check"><input type="checkbox" v-model="selected" :value="key(d.id,q.sub_question_id)" :disabled="busy||uncertain||!loaded(d)" @change="confirmed=false">确认 {{q.sub_question_id}} 确实未作答</label></article>
<label class="check"><input type="checkbox" v-model="confirmed" :disabled="busy||uncertain||!selected.length">我已逐张核对所选答卷全部原图，确认选中的 {{selected.length}} 道小题确实为空白，按空白题记录为0分；这不是正式终评。</label><button class="primary" :disabled="busy||uncertain||!confirmed||!selected.length" @click="save">保存所选空白题核对</button></section></template>
<style scoped>.blank-panel{border:1px solid #cbd5e1;padding:16px;margin:16px 0;background:#f8fafc}.blank-panel article{border-top:1px solid #cbd5e1;padding:12px 0}.blank-photos{display:flex;gap:12px;flex-wrap:wrap}.blank-photos a{flex:1 1 260px;max-width:480px}.blank-photos img{width:100%;height:auto}.blank-panel button{white-space:nowrap}.blank-panel h5{font-size:14px;margin:8px 0}</style>
