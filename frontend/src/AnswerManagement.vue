<script setup lang="ts">
import {computed,ref,watch} from 'vue'
import {listAnswers,previewAnswers,removeAnswers,dataChanged,type AnswerGroup,type DataPreview} from './answerManagement'
import {useTaskGuard} from './taskGuard'
const props=defineProps<{assignmentId:string;examId:string}>()
const rows=ref<AnswerGroup[]>([]),selected=ref<string[]>([]),busy=ref(false),error=ref(''),message=ref(''),preview=ref<DataPreview>(),page=ref(1)
const visible=computed(()=>rows.value.slice((page.value-1)*25,page.value*25)),pages=computed(()=>Math.max(1,Math.ceil(rows.value.length/25)))
const scope=()=>({assignment_id:props.assignmentId,exam_id:props.examId,keys:[...selected.value]})
useTaskGuard(busy)
async function load(){busy.value=true;error.value='';selected.value=[];preview.value=undefined;page.value=1;try{rows.value=(await listAnswers(scope())).groups}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function prepare(){busy.value=true;error.value='';try{preview.value=await previewAnswers(scope())}catch(e){error.value=(e as Error).message}finally{busy.value=false}}
async function remove(){const p=preview.value;if(!p||p.blocked)return;busy.value=true;error.value='';try{const r=await removeAnswers(scope(),p);message.value=r.warning||'所选答卷及其图片、评分与关联已删除';await load();dataChanged()}catch(e){preview.value=undefined;error.value=(e as Error).message}finally{busy.value=false}}
watch(()=>[props.assignmentId,props.examId],load,{immediate:true})
watch(selected,()=>{preview.value=undefined})
</script>
<template><section aria-label="答卷管理与批量删除"><h3>答卷管理</h3><p>同时管理本次阅卷的主客观题。同次整卷上传的两侧作为一组整体删除；独立上传的答卷可分别选择。正在执行的任务请先停止并等待结束。</p>
<p v-if="!examId">当前显示未绑定正式考试的主观题记录。需要管理正式答卷时，请先在上传页选择本次阅卷记录，再打开答卷管理。</p>
<div class="context-actions"><button :disabled="busy" @click="load">刷新列表</button><button :disabled="busy" @click="selected=rows.filter(r=>!r.blocked).map(r=>r.key)">全选可删除答卷</button><button :disabled="busy" @click="selected=[]">清空选择</button><button v-if="selected.length" class="danger" :disabled="busy" @click="prepare">删除所选 {{selected.length}} 组</button></div>
<p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p><p v-if="busy">正在处理…</p>
<section v-if="preview" class="delete-confirm" aria-label="批量删除答卷确认"><h4>永久删除所选 {{preview.groups}} 组答卷？</h4><p>主观题 {{preview.subjective}} 份、客观题 {{preview.objective}} 份、图片 {{preview.photos}} 张。评分、终评和对应身份关联一并删除；保留考试配置、答案、评分标准及未选答卷。无法撤销。</p><p v-if="preview.blocked" role="alert">所选答卷仍有正在执行的任务，暂不能删除。</p><button class="danger" :disabled="busy||preview.blocked" @click="remove">确认删除所选答卷</button><button :disabled="busy" @click="preview=undefined">取消</button></section>
<div class="table-wrap"><table><thead><tr><th>选择</th><th>答卷标识</th><th>包含记录</th><th>任务状态</th></tr></thead><tbody><tr v-for="r in visible" :key="r.key"><td><input v-model="selected" type="checkbox" :value="r.key" :disabled="busy||r.blocked" :aria-label="'选择答卷 '+r.label"></td><td>{{r.label}}</td><td>{{r.subjective?'主观题 '+r.subjective+' 份':''}} {{r.objective?'客观题 '+r.objective+' 份':''}} <small v-if="r.subjective&&r.objective">同份整卷</small></td><td>{{r.blocked?'执行中，暂不可删除':'可删除'}}</td></tr></tbody></table></div>
<p v-if="!busy&&!rows.length">没有答卷，可重新上传。</p><div class="context-actions"><button :disabled="busy||page<=1" @click="page--">上一页</button><span>{{page}} / {{pages}} 页 · {{rows.length}} 组</span><button :disabled="busy||page>=pages" @click="page++">下一页</button></div>
</section></template>
