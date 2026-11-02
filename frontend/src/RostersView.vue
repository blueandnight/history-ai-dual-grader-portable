<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useReadonly } from './useReadonly'
import { fetchClasses, fetchStudents, type RosterClass, type Student } from './records'
import RosterManager from './RosterManager.vue'
const manage=ref(false)
const classes = useReadonly<RosterClass[]>(), students = useReadonly<Student[]>(), selected = ref('')
const { data, busy, error } = classes
const { data: members, busy: memberBusy, error: memberError } = students
function refresh() { selected.value = ''; students.clear(); return classes.load(fetchClasses) }
function selectClass() { students.clear(); if (selected.value) return students.load(() => fetchStudents(selected.value)) }
onMounted(refresh)
</script>
<template><section><h2>班级与花名册</h2><p>花名册可选，保留学生资料和历史成绩归属；匿名阅卷无需导入名单。</p>
  <button @click="refresh" :disabled="busy">刷新班级</button><p v-if="busy" aria-live="polite">正在加载…</p><p v-if="error" role="alert">{{ error }}</p><p v-if="data && !data.length">尚无班级。</p>
  <template v-if="data?.length"><label for="roster-class">选择班级</label><select id="roster-class" v-model="selected" @change="selectClass"><option value="">请选择</option><option v-for="c in data" :key="c.class_id || c.class_name" :value="c.class_name">{{ c.class_name }}（{{ c.student_count }}人）</option></select></template>
  <p v-if="memberBusy" aria-live="polite">正在加载花名册…</p><p v-if="memberError" role="alert">{{ memberError }}</p><p v-if="members && !members.length">该班暂无学生。</p>
  <div v-if="members?.length" class="table-wrap" tabindex="0" aria-label="班级花名册表格"><table><thead><tr><th>学生</th><th>学号</th><th>状态</th></tr></thead><tbody><tr v-for="s in members" :key="s.student_id"><td>{{ s.display_name }}</td><td>{{ s.student_no || '未录入' }}</td><td>{{ s.enabled ? '启用' : '停用' }}</td></tr></tbody></table></div>
  <p><button @click="manage=!manage">{{ manage?'收起花名册管理':'展开花名册管理' }}</button></p>
  <RosterManager v-if="manage" @changed="refresh" />
</section></template>
