<script setup lang="ts">
import {computed,nextTick,ref,watch,onUnmounted} from 'vue'
import {request} from './api'
import {fetchDetail,type Detail} from './records'
import {originalUrl} from './teacherReview'
import {useTaskGuard} from './taskGuard'
import SubmissionDetail from './SubmissionDetail.vue'
type Preparation={exam_id:string;max_score:number;grading_scope:{graded_question_ids:string[]};suggestions:{A:number|null;B:number|null};expected_fingerprint:string}
const props=defineProps<{id:string;expectedExamId?:string}>(),emit=defineEmits<{saved:[detail:Detail];confirmed:[detail:Detail]}>()
const root=ref<HTMLElement>()
const data=ref<Detail>(),prepared=ref<Preparation>(),busy=ref(false),error=ref(''),message=ref(''),source=ref<'A'|'B'|'teacher'>('teacher'),score=ref<number|string>(''),confirmed=ref(false),seen=ref<number[]>([]),uncertain=ref(false),dirty=ref(false)
let revision=0
useTaskGuard(computed(()=>busy.value||dirty.value))
const valid=computed(()=>!!prepared.value&&!busy.value&&!uncertain.value&&confirmed.value&&seen.value.length===(data.value?.photos?.length||0)&&score.value!==''&&Number.isFinite(Number(score.value))&&Number(score.value)>=0&&Number(score.value)<=prepared.value.max_score)
async function load(){const n=++revision;busy.value=true;prepared.value=undefined;data.value=undefined;error.value='';message.value='';dirty.value=false;confirmed.value=false;seen.value=[];uncertain.value=false;score.value='';source.value='teacher';try{const d=await fetchDetail(props.id);if(n!==revision)return;if(props.expectedExamId&&d.exam_id!==props.expectedExamId)throw new Error('答卷不属于当前考试');data.value=d;const p=await request<Preparation>('/api/total-confirmation?submission_id='+encodeURIComponent(props.id));if(n!==revision)return;prepared.value=p;score.value=d.confirmed_at&&d.final_total!==null?d.final_total:''}catch(e){if(n===revision)error.value=(e as Error).message}finally{if(n===revision){busy.value=false;await nextTick();root.value?.scrollIntoView({block:'start'});root.value?.focus({preventScroll:true})}}}
function choose(s:'A'|'B'){const v=prepared.value?.suggestions[s];if(v===null||v===undefined)return;source.value=s;score.value=v;dirty.value=true;confirmed.value=false}
async function save(){
 if(!valid.value||!prepared.value)return
 const n=revision;busy.value=true;error.value=''
 try{
  await request('/api/confirm-total','POST',{submission_id:props.id,exam_id:prepared.value.exam_id,expected_fingerprint:prepared.value.expected_fingerprint,source:source.value,final_total:Number(score.value),confirmed:true})
  if(n!==revision)return
  const d=await fetchDetail(props.id);if(n!==revision)return
  data.value=d;dirty.value=false;confirmed.value=false;uncertain.value=true;busy.value=false
  message.value='最终总分已保存，未生成教师小题或逐点评分。重新改分请先读取最新记录。'
  emit('saved',d);emit('confirmed',d)
 }catch(e){if(n===revision){uncertain.value=true;error.value=(e as Error).message+'；请重新读取记录核实，不会自动重复保存。'}}
 finally{if(n===revision)busy.value=false}
}
watch(()=>props.id,load,{immediate:true});onUnmounted(()=>revision++)
</script>
<template><section ref="root" tabindex="-1" class="teacher-review quick-total" aria-label="快捷总分终评"><div class="context-actions"><h3>确认最终总分</h3><button :disabled="busy" @click="load">重新读取记录</button></div><p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p><p v-if="busy" role="status">正在读取或保存…</p><template v-if="data"><p>匿名答卷：<strong>{{data.id}}</strong> · {{data.confirmed_at?'已有正式确认成绩':'待教师确认'}}<span v-if="prepared"> · 本次题目 {{prepared.grading_scope.graded_question_ids.join('、')}}</span></p><div class="review-grid"><section class="review-original"><h4>原始答卷</h4><figure v-for="(_,i) in data.photos" :key="id+':'+i"><a :href="originalUrl(id,i+1)" target="_blank" rel="noopener">放大第{{i+1}}页</a><img class="upload-preview" :src="originalUrl(id,i+1)" :alt="'原卷第'+(i+1)+'页'" @load="seen.includes(i)||seen.push(i)" @error="error='原图未能读取，请恢复图片后再确认'"/></figure></section><section class="review-decisions"><template v-if="prepared"><div class="score-summary"><p>AI-A：<strong>{{prepared.suggestions.A??'结果不完整'}}</strong></p><p>AI-B：<strong>{{prepared.suggestions.B??'结果不完整'}}</strong></p><p>满分：<strong>{{prepared.max_score}}</strong></p><p v-if="prepared.suggestions.A!==null&&prepared.suggestions.B!==null">相差 {{Math.abs(prepared.suggestions.A-prepared.suggestions.B)}} 分</p></div><p>{{data.result.block_reasons?.join('；')||'请核对原卷；模型风险和分歧供教师参考。'}}</p><div class="context-actions"><button :disabled="busy||prepared.suggestions.A===null" @click="choose('A')">采用 AI-A 分数</button><button :disabled="busy||prepared.suggestions.B===null" @click="choose('B')">采用 AI-B 分数</button></div><label>教师最终确认分数<input type="number" min="0" :max="prepared.max_score" step="any" v-model="score" :disabled="busy||uncertain" @input="source='teacher';dirty=true;confirmed=false"/></label><p>确认方式：{{source==='teacher'?'教师自定总分':'采用 AI-'+source}}。仅确认总分，不代表逐点或逐小题核对。</p><label class="check"><input type="checkbox" v-model="confirmed" :disabled="busy||uncertain||seen.length!==data.photos?.length"/>我已查看原卷并确认本次总分</label><button class="primary" :disabled="!valid" @click="save">保存最终总分并继续</button></template><details><summary>评分点、模型证据与历史终评</summary><SubmissionDetail :id="id" in-teacher-review/></details></section></div></template></section></template>
