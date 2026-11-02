<script setup lang="ts">
import {nextTick,onMounted,ref} from 'vue'
import {request} from './api'
import {useTaskGuard} from './taskGuard'
const props=defineProps<{assignment:{id:string;name:string}}>()
const emit=defineEmits<{close:[];created:[value:{assignmentId:string;examId:string;message:string}]}>()
const name=ref(props.assignment.name+'（副本）'),busy=ref(false),error=ref(''),input=ref<HTMLInputElement>()
useTaskGuard(busy)
onMounted(async()=>{await nextTick();input.value?.focus();input.value?.select()})
async function create(){
 if(busy.value||!name.value.trim())return
 busy.value=true;error.value=''
 try{
  const r=await request<{assignment:{id:string;name:string};exam_id:string;warnings:string[]}>('/api/copy-exam','POST',{assignment_id:props.assignment.id,name:name.value.trim()})
  emit('created',{assignmentId:r.assignment.id,examId:r.exam_id,message:'已创建 '+r.assignment.name+'，答卷与成绩独立。'+r.warnings.join('；')})
 }catch{error.value='复制未能确认，请刷新考试列表核对后再重试。'}finally{busy.value=false}
}
</script>
<template><section class="delete-confirm" aria-label="复制为新考试"><h3>复制为新考试</h3><p>来源：{{assignment.name}}。复用题目、答案、分值、评分标准及可用版式，不复制答卷、成绩或任务。</p><form @submit.prevent="create"><label>新考试名称<input ref="input" v-model="name" maxlength="200" required :disabled="busy"></label><p v-if="error" role="alert">{{error}}</p><button class="primary" type="submit" :disabled="busy||!name.trim()">{{busy?'正在创建…':'创建新考试'}}</button> <button type="button" :disabled="busy" @click="emit('close')">取消</button></form></section></template>
