import { request } from './api'

export type ReportCriterion = {
  label?: string | null
  max?: number | null
  attempts?: number | null
  credited?: number | null
  missed?: number | null
  rate?: number | null
}
export type ReportQuestion = {
  label?: string | null
  max?: number | null
  answered?: number | null
  teacher_scored?: number | null
  ai_scored?: number | null
  missing_score?: number | null
  unanswered?: number | null
  average?: number | null
  score_rate?: number | null
  criteria?: ReportCriterion[] | null
}
export type CommonMiss = { question?: string | null; label?: string | null; missed?: number | null; rate?: number | null }
export type ExamReport = {
  assignment_id: string
  assignment_name?: string | null
  exam_id?: string | null
  batch_id?: string | null
  student_count?: number | null
  confirmed_count?: number | null
  unconfirmed_count?: number | null
  blocked_count?: number | null
  max_total?: number | null
  scope?: string | null
  average?: number | null
  score_rate?: number | null
  distribution?: Record<string, number | null> | null
  questions?: ReportQuestion[] | null
  common_misses?: CommonMiss[] | null
  warnings?: string[] | null
  coverage?: unknown
  mixed_scopes?: unknown
  objective?: { available?: boolean; reason?: string | null } | null
  [key: string]: unknown
}
export type ExamReportAI = { report: ExamReport; markdown?: string | null; model?: string | null; objectives?: string | null }

export function reportNumber(value: unknown, suffix = ''): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value}${suffix}` : '—'
}

export async function fetchExamReport(assignmentId: string, examId = '') {
  const body: { assignment_id: string; exam_id?: string } = { assignment_id: assignmentId }
  if (examId) body.exam_id = examId
  return request<{ report: ExamReport }>('/api/exam-report', 'POST', body)
}

export async function fetchExamReportAI(assignmentId: string, examId: string, objectives: string) {
  return request<ExamReportAI>('/api/exam-report/ai', 'POST', {
    assignment_id: assignmentId,
    ...(examId ? { exam_id: examId } : {}),
    objectives,
    confirm_send: true,
  })
}

function safeCell(value: unknown): string {
  return String(value ?? '—').replaceAll('|', '\\|').replaceAll('\n', ' ')
}

export function reportMarkdown(report: ExamReport, aiMarkdown = ''): string {
  const questions = report.questions || []
  const lines = [
    `# ${report.assignment_name || '试卷'} · 试卷分析报告`,
    '',
    '> 本地统计由已完成记录生成；AI 分析为可选内容，不重新评分。',
    '',
    '## 数据口径',
    '',
    `- 统计人数：${reportNumber(report.student_count)}`,
    `- 已确认成绩：${reportNumber(report.confirmed_count)}；未确认成绩：${reportNumber(report.unconfirmed_count)}；受阻记录：${reportNumber(report.blocked_count)}`,
    `- 满分：${reportNumber(report.max_total)}；平均分：${reportNumber(report.average)}；平均得分率：${reportNumber(report.score_rate, '%')}`,
    `- 评分范围：${report.scope || '—'}${report.mixed_scopes ? '；包含不同评分范围，各范围分母分别统计' : ''}`,
    '',
    '## 分数段分布',
    '',
    '| 分数段 | 人数 |', '|---|---:|',
    ...Object.entries(report.distribution || {}).map(([label, count]) => `| ${safeCell(label)} | ${reportNumber(count)} |`),
    '',
    '## 分题统计',
    '',
    '| 小题 | 满分 | 已评人数 | 未作答候选 | 平均分 | 得分率 |', '|---|---:|---:|---:|---:|---:|',
    ...(questions.length ? questions.map(q => `| ${safeCell(q.label)} | ${reportNumber(q.max)} | ${reportNumber(q.answered)} | ${reportNumber(q.unanswered)} | ${reportNumber(q.average)} | ${reportNumber(q.score_rate, '%')} |`) : ['| — | — | — | — | — | — |']),
    '',
    '## 评分点与共性问题',
    '',
    ...(report.common_misses?.length ? report.common_misses.map(item => `- ${safeCell(item.question)} · ${safeCell(item.label)}：未命中 ${reportNumber(item.missed)} 次，命中率 ${reportNumber(item.rate, '%')}`) : ['- 暂无可统计的共性问题。']),
    '',
    '## 教学分析',
    '',
    report.objective?.reason ? `> ${report.objective.reason}` : '',
    aiMarkdown || '（未生成 AI 分析。）',
    '',
    ...(report.warnings || []).map(warning => `> 口径提示：${warning}`),
    '',
  ]
  return lines.join('\n')
}
