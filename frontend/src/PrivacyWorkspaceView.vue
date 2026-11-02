<script setup lang="ts">
import {computed,onMounted,ref,shallowRef} from 'vue'
import {request} from './api'
import PrivacyEditor from './PrivacyEditor.vue'
import type {FormalAssignment} from './formalUpload'
import type {ExamContext} from './navigation'
const props=defineProps<{initialContext?:ExamContext}>()
const assignments=ref<FormalAssignment[]>([]),assignmentId=ref(''),files=shallowRef<File[]>([]),error=ref(''),ready=ref(false)
const assignment=computed(()=>assignments.value.find(a=>a.id===assignmentId.value))
function choose(event:Event){files.value=[...(event.target as HTMLInputElement).files||[]];ready.value=false}
onMounted(async()=>{try{assignments.value=(await request<{assignments:FormalAssignment[]}>('/api/assignments')).assignments;if(props.initialContext?.assignmentId){if(!assignments.value.some(a=>a.id===props.initialContext?.assignmentId))throw new Error('评分标准不存在或无法访问');assignmentId.value=props.initialContext.assignmentId}}catch(e){error.value=(e as Error).message}})
</script>
<template><section><h2>隐私编辑工具</h2><p>独立核对遮盖、纸张四角与外发预览，不上传评分，也不调用模型。正式考试请使用正式上传的裁切验收链路。</p><p v-if="error" role="alert">{{error}}</p><label v-if="!initialContext?.assignmentId" for="privacy-assignment">选择作业</label><select v-show="!initialContext?.assignmentId" id="privacy-assignment" v-model="assignmentId" @change="ready=false"><option value="">请选择</option><option v-for="a in assignments" :key="a.id" :value="a.id">{{a.name}}</option></select><label>选择待预览照片<input type="file" accept="image/jpeg,image/png" multiple @change="choose"></label><PrivacyEditor v-if="assignment&&files.length" :key="assignment.id" :assignment="assignment" :files="files" student-label="隐私预览" :dual="true" @ready="ready=true" @invalidated="ready=false"/><p v-if="ready" role="status">预览已核对；本工具没有创建答卷或评分任务。</p></section></template>
