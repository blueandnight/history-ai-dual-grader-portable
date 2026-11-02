<script setup lang="ts">
import { onMounted,ref } from 'vue'
import { useReadonly } from './useReadonly'
import { fetchSystem, bytes, type SystemInfo } from './records'
import SystemConfiguration from './SystemConfiguration.vue'
import SystemOperations from './SystemOperations.vue'
const config=ref(false),operations=ref(false)
const { data, busy, error, load } = useReadonly<SystemInfo>()
onMounted(() => load(fetchSystem))
</script>
<template><section><h2>系统与模型设置</h2><p>日常阅卷无需反复设置。先查看就绪状态，需要时再打开模型配置或本机运维。</p>
  <button @click="load(fetchSystem)" :disabled="busy">刷新系统信息</button><p v-if="busy" aria-live="polite">正在加载…</p><p v-if="error" role="alert">{{ error }}</p>
  <dl v-if="data"><dt>服务健康</dt><dd>{{ data.health.ok ? '正常' : '需检查' }}</dd><dt>内置PDF渲染</dt><dd>{{ data.health.pdf_renderer ? '可用' : '不可用' }}</dd><dt>监听端口</dt><dd>{{ data.status.service_port ?? '—' }}</dd><dt>活动任务</dt><dd>{{ data.status.active_task_count ?? '—' }}</dd><dt>AI-A</dt><dd>{{ data.pair.label_a || '未选模型' }} · {{ data.pair.configured_a ? '配置已就绪' : '配置未就绪' }}</dd><dt>AI-B</dt><dd>{{ data.pair.label_b || '未选模型' }} · {{ data.pair.configured_b ? '配置已就绪' : '配置未就绪' }}</dd><dt>数据大小</dt><dd>{{ bytes(data.storage.data_bytes) }}</dd><dt>备份大小</dt><dd>{{ bytes(data.storage.backup_bytes) }}</dd><dt>磁盘可用</dt><dd>{{ bytes(data.storage.disk_free_bytes) }}</dd></dl>
  <p><button @click="config=!config;operations=false">{{ config?'收起模型配置':'展开模型配置' }}</button> <button @click="operations=!operations;config=false">{{ operations?'收起本机运维':'展开本机运维' }}</button></p><SystemConfiguration v-if="config" /><SystemOperations v-if="operations" />
</section></template>
