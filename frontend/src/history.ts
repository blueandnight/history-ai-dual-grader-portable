import { request } from './api'
export type Assignment = { id: string; name: string; submission_count: number; exam_id?: string }
export type Submission = {
  id: string; exam_id?: string | null; student_label?: string; marking_code?: string; state: string; grading_stopped?:boolean;
  suggested_total: number | null; final_total: number | null; max_total: number | null;
  confirmed_at?: string | null; teacher_reconfirmation_required?: boolean; confirmation_method?:string;
  grading_scope?: { scoped_max_score?: number } | null
  photos?:unknown[]; photos_deleted?:boolean
}
export async function fetchAssignments() {
  const data = await request<{ assignments: Assignment[] }>('/api/assignments')
  return data.assignments
}
export async function fetchSubmissions(assignmentId: string) {
  if (!/^[a-f0-9]{12}$/.test(assignmentId)) throw new Error('无效的考试编号')
  const data = await request<{ submissions: Submission[] }>('/api/assignment-submissions?assignment_id=' + encodeURIComponent(assignmentId))
  return data.submissions
}
export function score(value: unknown, maximum: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return typeof maximum === 'number' && Number.isFinite(maximum) && maximum > 0 ? `${value}/${maximum}` : `${value}`
}
export function finalLabel(s: Submission) {
  if (s.teacher_reconfirmation_required) return '待重新确认'
  if (!s.confirmed_at) return '尚未确认'
  return (s.confirmation_method==='automatic_blank_zero'?'明确空白自动计零 · ':s.confirmation_method==='teacher_batch_adoption'?'批量采纳 · ':s.confirmation_method==='teacher_total_confirmation'?'总分终评 · ':'')+score(s.final_total, s.grading_scope?.scoped_max_score ?? s.max_total)
}
export function exportHref(id: string, anonymous: boolean) {
  return '/api/' + (anonymous ? 'export-anonymous.csv' : 'export.csv') + '?assignment_id=' + encodeURIComponent(id)
}
