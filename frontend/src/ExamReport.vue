<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { fetchExamReport, fetchExamReportAI, reportMarkdown, reportNumber, type ExamReport } from './examReport'
const props = defineProps<{ assignmentId: string; examId?: string; assignmentName?: string }>()
const emit = defineEmits<{ close: [] }>()
const data = ref<ExamReport | null>(null), markdown = ref(''), objectives = ref(''), error = ref('')
const busy = ref(false), aiBusy = ref(false)
let revision = 0
watch(() => [props.assignmentId, props.examId], async () => {
  const token = ++revision
  data.value = null; markdown.value = ''; objectives.value = ''; error.value = ''; busy.value = true; aiBusy.value = false
  try { const response = await fetchExamReport(props.assignmentId, props.examId || ''); if (token === revision) data.value = response.report }
  catch (e) { if (token === revision) error.value = e instanceof Error ? e.message : '报告读取失败' }
  finally { if (token === revision) busy.value = false }
}, { immediate: true })
onBeforeUnmount(() => { revision++ })
async function analyze() {
  if (!data.value || aiBusy.value) return
  if (!window.confirm('将把当前匿名聚合统计和学习目标发送给已配置的 AI 生成分析，会产生模型费用。继续？')) return
  const token = revision
  aiBusy.value = true; markdown.value = ''; error.value = ''
  try {
    const response = await fetchExamReportAI(props.assignmentId, props.examId || '', objectives.value)
    if (token === revision) { data.value = response.report; markdown.value = response.markdown || '' }
  } catch (e) { if (token === revision) error.value = e instanceof Error ? e.message : 'AI 分析失败，本地报告仍可导出' }
  finally { if (token === revision) aiBusy.value = false }
}
function download() {
  if (!data.value) return
  const url = URL.createObjectURL(new Blob([reportMarkdown(data.value, markdown.value)], { type: 'text/markdown;charset=utf-8' }))
  const a = document.createElement('a'); a.href = url; a.download = `${props.assignmentName || '试卷'}-分析报告.md`; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function printReport() {
  if (!data.value) return
  const w = window.open('', '_blank')
  if (!w) { error.value = '请允许打印弹窗'; return }
  w.document.title = '试卷分析报告'
  const pre = w.document.createElement('pre'); pre.style.whiteSpace = 'pre-wrap'; pre.style.fontFamily = 'sans-serif'
  pre.textContent = reportMarkdown(data.value, markdown.value); w.document.body.appendChild(pre); w.document.close(); w.focus(); w.print()
}
</script>
<template>
  <section class="exam-report" role="region" aria-label="试卷分析报告">
    <h3>{{ assignmentName || '试卷' }} · 试卷分析报告</h3>
    <button type="button" @click="emit('close')">关闭报告</button>
    <p v-if="busy" aria-live="polite">正在读取本地统计…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="data">
      <p>统计 {{ reportNumber(data.student_count) }} 份；教师确认 {{ reportNumber(data.confirmed_count) }} 份；未确认 {{ reportNumber(data.unconfirmed_count) }} 份按 AI 建议分统计。</p>
      <p>平均分 {{ reportNumber(data.average) }} / {{ reportNumber(data.max_total) }}；平均得分率 {{ reportNumber(data.score_rate, '%') }}</p>
      <p v-for="warning in data.warnings || []" :key="warning">{{ warning }}</p>
      <p>{{ data.objective?.reason }}</p>
      <div class="table-wrap"><table><thead><tr><th>小题</th><th>满分</th><th>已评</th><th>确认分 / AI分人数</th><th>缺失</th><th>均分</th><th>得分率</th></tr></thead>
        <tbody><tr v-for="(q, i) in data.questions || []" :key="i"><td>{{ q.label }}</td><td>{{ reportNumber(q.max) }}</td><td>{{ reportNumber(q.answered) }}</td><td>{{ reportNumber(q.teacher_scored) }} / {{ reportNumber(q.ai_scored) }}</td><td>{{ reportNumber(q.missing_score) }}</td><td>{{ reportNumber(q.average) }}</td><td>{{ reportNumber(q.score_rate, '%') }}</td></tr></tbody></table></div>
      <details><summary>查看实际聚合统计</summary><pre>{{ JSON.stringify(data, null, 2) }}</pre></details>
      <button type="button" @click="download">下载报告</button><button type="button" @click="printReport">打印报告</button>
      <label for="report-objectives">学习目标（可选，不填写学生身份信息）</label><textarea id="report-objectives" v-model="objectives" maxlength="4000" />
      <button type="button" :disabled="aiBusy || !data.student_count" @click="analyze">{{ aiBusy ? 'AI 分析中…' : '生成 AI 分析' }}</button>
      <pre v-if="markdown" class="analysis-text">{{ markdown }}</pre>
    </template>
  </section>
</template>
<style scoped>
.exam-report{border:1px solid #b7c6da;border-radius:12px;padding:16px;margin:16px 0;min-width:0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:36rem;overflow:auto}textarea{display:block;width:100%;box-sizing:border-box;min-height:5rem}button{margin:4px}
</style>
