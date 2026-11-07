<script setup lang="ts">
import HolisticReview from './HolisticReview.vue'
import { computed, watch } from 'vue'
import { useReadonly } from './useReadonly'
import { fetchDetail, savedPointRows, decisionLabel, detailFinalLabel, type Detail, type ModelResult } from './records'
import { score } from './history'
const props = defineProps<{ id: string; inTeacherReview?: boolean }>()
const { data, busy, error, load } = useReadonly<Detail>()
const points = computed(() => data.value ? savedPointRows(data.value) : [])
const maximum = computed(() => data.value?.result.grading_scope?.scoped_max_score ?? data.value?.result.max_total)
function displayModelScore(model?: ModelResult) {
  const questions = model?.sub_questions || []
  if (questions.some(q => q.scoring_mode === 'holistic') && questions.every(q => q.suggested_total == null)) return '未获得有效分数'
  return score(model?.suggested_total, maximum.value)
}
watch(() => props.id, () => load(() => fetchDetail(props.id)), { immediate: true })
</script>
<template>
  <section aria-label="答卷只读详情"><h3>答卷只读详情</h3>
    <button @click="load(() => fetchDetail(id))" :disabled="busy">刷新详情</button>
    <p v-if="busy" aria-live="polite">正在加载详情…</p><p v-if="error" role="alert">{{ error }}</p>
    <template v-if="data"><p>{{ data.marking_code || data.student_label || data.id }} · {{data.confirmation_method==='automatic_blank_zero'?'自动确认':'教师终评'}}：<strong>{{ detailFinalLabel(data) }}</strong></p>
      <p v-if="data.confirmation_method==='automatic_blank_zero'">双模型独立确认全部答题区域完整清晰且未作答，已自动确认0分；需要调整时可进入快捷总分终评。</p>
      <p v-if="data.result.blocked">该记录存在阻断：{{ (data.result.block_reasons || []).join('；') || '请核对原卷后确认最终总分或处理异常' }}</p>
      <details v-if="data.result.dual_review?.raw_results"><summary>自动计零的原始空白证据</summary><template v-for="(model,side) in data.result.dual_review.raw_results" :key="side"><p v-for="q in model?.sub_questions?.filter(q=>q.blank_observation)" :key="q.sub_question_id">{{side}} · {{q.sub_question_id}}：{{q.blank_observation?.summary || '空白证据保存在原始记录中'}}（置信度 {{q.blank_observation?.confidence??'未提供'}}）</p></template></details>
      <template v-if="data.result.dual_review">
        <p data-testid="dual-a">AI-A · {{ data.result.dual_review.models?.A?.label || '模型A' }}：{{ data.result.dual_review.models?.A?.status === 'error' ? '调用失败' : displayModelScore(data.result.dual_review.results?.A) }}</p>
        <p data-testid="dual-b">AI-B · {{ data.result.dual_review.models?.B?.label || '模型B' }}：{{ data.result.dual_review.models?.B?.status === 'error' ? '调用失败' : displayModelScore(data.result.dual_review.results?.B) }}</p>
        <p>风险：{{ data.result.dual_review.comparison?.risk_level || '未记录' }}；{{ (data.result.dual_review.comparison?.risk_reasons || []).join('、') || '无已保存原因' }}</p>
      </template>
      <p v-else>AI建议：{{ displayModelScore(data.result) }}；无双评记录。</p>
      <p v-if="data.confirmation_method==='teacher_total_confirmation'">当前正式成绩来自教师总分确认；没有教师确认的小题或评分点成绩，历史逐点记录保留在原始记录中。</p><p>以下为已保存的模型判断和引文，不代表已核实的原图事实。缺失教师逐点记录时显示“无记录”，不从总分倒推。</p>
      <HolisticReview :detail="data"/><p v-if="!points.length&&!data.result.sub_questions?.some(q=>q.scoring_mode==='holistic')">没有可展示的已保存逐点评分细则，请检查原始评分记录。</p>
      <details :open="!data.result.dual_review?.raw_results"><summary>评分点与空白证据详情</summary><p v-if="data.confirmation_method==='automatic_blank_zero'">以下零分评分点由明确空白规则生成，不代表教师逐点评阅；原始双侧判断保存在答卷记录中。</p><article v-for="p in points" :key="p.questionId + ':' + p.pointId" class="point-record">
        <h4>{{ p.questionId }} / {{ p.pointId }} · {{ p.label }}（{{ p.maximum }}分）</h4>
        <p>AI-A：{{ decisionLabel(p.aDecision) }} · {{ score(p.aScore, p.maximum) }}；模型引文：{{ p.aEvidence || '未提供引文' }}</p>
        <p>AI-B：{{ decisionLabel(p.bDecision) }} · {{ score(p.bScore, p.maximum) }}；模型引文：{{ p.bEvidence || '未提供引文' }}</p>
        <p>教师逐点记录：{{ decisionLabel(p.teacher) }}</p>
      </article></details>
      <p v-if="inTeacherReview">以上是已保存的只读评分摘要。请在快捷终评区查看原图并确认最终总分，逐点终评为可选操作；证据框可在兼容详情中查看。</p><p v-else>此详情区只展示已保存结果，不请求原图或改分。如需查看原图并终评，请在历史记录中选择答卷进入教师终评；证据框可在兼容详情中查看。</p><details><summary>证据框与兼容详情</summary><a href="/legacy#view-history">打开证据框查看工具</a><p>此辅助工具沿用原有记录和权限；请选择对应考试与答卷。</p></details>
    </template>
  </section>
</template>
