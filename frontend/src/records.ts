import { getSession, isOperator, request } from './api'
import { finalLabel, type Submission } from './history'
export type PointResult = { criterion_id: string; decision?: string; suggested_score?: number | null; max_score?: number; student_evidence?: string }
export type QuestionResult = { sub_question_id: string; answer_status?: string;blank_observation?:{summary?:string;confidence?:number}; criteria: PointResult[] }
export type ModelResult = { suggested_total?: number | null; max_total?: number; sub_questions?: QuestionResult[] }
export type TeacherPoint = { question_id: string; rubric_point_id: string; decision?: string; teacher_decision?: string }
export type TeacherFinal = { point_decisions?: TeacherPoint[]; teacher_note?: string; final_total?: number; question_subtotals?: {question_id:string;subtotal:number;max_score:number}[] }
export type PointComparison = { question_id: string; rubric_point_id: string; decision_a?: string; decision_b?: string; score_a?: number | null; score_b?: number | null; evidence_a?: string; evidence_b?: string }
export type ReviewResult = ModelResult & {
  grading_scope?: { scoped_max_score?: number }; blocked?: boolean; block_reasons?: string[]; mode?:string;simulation_model?:boolean;unmapped_answers?:unknown[];
  teacher_total_confirmation?: TeacherFinal;confirmation_method?:string;teacher_final?: TeacherFinal;photos_discarded_unconfirmed?:boolean;
  dual_review?: { status?: string; raw_results?: { A?: ModelResult; B?: ModelResult }; results?: { A?: ModelResult; B?: ModelResult };
    models?: { A?: { label?: string; model?: string; status?: string }; B?: { label?: string; model?: string; status?: string } };
    comparison?: { risk_level?: string; risk_reasons?: string[]; point_comparisons?: PointComparison[] };
    teacher_final?: TeacherFinal }
}
export type Detail = Omit<Submission, 'suggested_total' | 'max_total' | 'grading_scope'> & { assignment_id?:string;exam_id?:string;batch_id?:string|null;class_name?:string;result: ReviewResult;photos?:{image_role?:string}[];manual_reviewed?:boolean;review_note?:string;photos_deleted?:boolean; teacher_scoring_questions?: { id: string; label?: string; question?:string;standard_answer?:string;max_score: number; criteria: { id: string; label?: string; score: number }[] }[] }
export type Batch = { created_at?:string;assignment_name?:string;uploaded_count?:number;processed_count?:number; exam_id?:string; batch_id: string; assignment_id: string; class_name: string; grading_stopped?:boolean; grading_input_mode?: 'auto_crop'|'original_photo'; preview_exam_id?: string; status: string; total_sequences: number; next_sequence: number; pause_reason?: string; items?: { state?: string }[] }
export type RosterClass = { class_id?: string | null; class_name: string; student_count: number }
export type Student = { student_id: string; display_name: string; student_no: string; enabled: number }
export type SystemInfo = { status: { model_configured: boolean; api_key_configured: boolean; active_task_count?: number; grade_worker_count?: number; service_port?: number; provider?: string; model?: string }; storage: { data_bytes: number; backup_bytes: number; disk_free_bytes: number }; health: { ok: boolean; pdf_renderer: boolean }; pair: { label_a?: string; label_b?: string; configured_a?: boolean; configured_b?: boolean } }
export async function fetchDetail(id: string) {
  if (!/^[a-f0-9]{12}$/.test(id)) throw new Error('无效答卷编号')
  return (await request<{ submission: Detail }>('/api/submission?submission_id=' + encodeURIComponent(id))).submission
}
export async function fetchBatches() { return (await request<{ batches: Batch[] }>('/api/batches')).batches }
export async function fetchClasses() { return (await request<{ classes: RosterClass[] }>('/api/rosters')).classes }
export async function fetchStudents(name: string) {
  if (!name.trim() || name.length > 300) throw new Error('请选择有效班级')
  return (await request<{ students: Student[] }>('/api/roster-students?class_name=' + encodeURIComponent(name))).students
}
export async function fetchSystem(): Promise<SystemInfo> {
  if (!isOperator(getSession())) throw new Error('仅管理员或本机工作台可查看系统信息')
  const [status, storage, health, pair] = await Promise.all([
    request<SystemInfo['status']>('/api/status'), request<SystemInfo['storage']>('/api/storage'),
    request<SystemInfo['health']>('/api/health'), request<SystemInfo['pair']>('/api/dual-review/status'),
  ])
  return { status, storage, health, pair }
}
export function decisionLabel(value?: string) {
  return ({ full: '给分', hit: '给分', teacher_full: '给分', none: '不给分', not_hit: '不给分', teacher_none: '不给分', partial: '部分给分', uncertain: '不确定', teacher_uncertain: '不确定', missing: '缺失', conflict: '冲突' } as Record<string, string>)[value || ''] || '无记录'
}
export function savedPointRows(detail: Detail) {
  const r = detail.result, dual = r.dual_review
  const teachers = detail.confirmation_method==='teacher_total_confirmation'?[]:dual?.teacher_final?.point_decisions || r.teacher_final?.point_decisions || []
  const models = dual ? dual.results || {} : { A: r }
  const availableA = !dual || (!!models.A && dual.models?.A?.status !== 'error')
  const availableB = !!dual && !!models.B && dual.models?.B?.status !== 'error'
  const points = (model?: ModelResult) => (model?.sub_questions || []).flatMap(q => (q.criteria || []).map(c => ({ ...c, question_id: q.sub_question_id })))
  const a = points(models.A), b = points(models.B)
  const compared = dual?.comparison?.point_comparisons || []
  return (detail.teacher_scoring_questions || []).flatMap(q => q.criteria.map(p => {
    const match = (c: { question_id: string; criterion_id: string }) => c.question_id === q.id && c.criterion_id === p.id
    const pa = a.find(match), pb = b.find(match)
    const row = compared.find(c => c.question_id === q.id && c.rubric_point_id === p.id)
    const teacher = teachers.find(c => c.question_id === q.id && c.rubric_point_id === p.id)
    return { questionId: q.id, pointId: p.id, label: p.label || p.id, maximum: p.score,
      aDecision: availableA ? (row ? row.decision_a : pa?.decision) : undefined, bDecision: availableB ? (row ? row.decision_b : pb?.decision) : undefined,
      aScore: availableA ? (row ? row.score_a : pa?.suggested_score) : undefined, bScore: availableB ? (row ? row.score_b : pb?.suggested_score) : undefined,
      aEvidence: availableA ? (row ? row.evidence_a : pa?.student_evidence) : undefined, bEvidence: availableB ? (row ? row.evidence_b : pb?.student_evidence) : undefined,
      teacher: teacher?.decision ?? teacher?.teacher_decision }
  }))
}
export function detailFinalLabel(detail: Detail) {
  return finalLabel({ ...detail, suggested_total: detail.result.suggested_total ?? null,
    max_total: detail.result.max_total ?? null, grading_scope: detail.result.grading_scope })
}
export function bytes(value: unknown) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return '—'
  return value >= 1024 ** 3 ? (value / 1024 ** 3).toFixed(1) + ' GiB' : (value / 1024 ** 2).toFixed(1) + ' MiB'
}
