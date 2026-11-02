import { type Session } from './api'
export type View = 'home' | 'objective' | 'create' | 'upload' | 'history' | 'batches' | 'batch' | 'rosters' | 'system' | 'users' | 'layout' | 'privacy' | 'quick'
export function routeFromHash(hash: string, session: Session | null): View {
  const raw = hash.replace(/^#\/?/, '').split('?')[0]
  const route = ({'view-grading':'upload','grading-section':'upload','view-rubric':'home','view-history':'history','view-management':'system','view-exam-create':'create'} as Record<string,string>)[raw||''] || raw
  if(route==='objective')return 'objective'
  if (route === 'create') return 'create'
  if (route === 'upload') return new URLSearchParams(hash.split('?')[1]||'').get('mode')==='quick'?'quick':'upload'
  if (route === 'layout') return 'layout'
  if (route === 'privacy') return 'privacy'
  if (route === 'quick') return 'quick'
  if (route === 'history') return 'history'
  if (route === 'batches') return 'batches'
  if (route === 'batch') return 'batch'
  if (route === 'rosters') return 'rosters'
  if (route === 'system' && session?.authenticated) return 'system'
  if (route === 'users' && session?.role==='admin') return 'users'
  return 'home'
}

export type ExamContext = { assignmentId: string; examId: string; className?: string; mode?: 'formal'|'quick' }
export function contextFromHash(hash: string): ExamContext {
  const params = new URLSearchParams(hash.split('?')[1] || '')
  const assignmentId = params.get('assignment') || '', examId = params.get('exam') || ''
  return { assignmentId: /^[a-f0-9]{12}$/.test(assignmentId) ? assignmentId : '', examId: /^[a-f0-9]{32}$/.test(examId) ? examId : '', className: (params.get('class') || '').slice(0,100) }
}
export function examHref(view: View, context: ExamContext): string {
  const params = new URLSearchParams()
  if (/^[a-f0-9]{12}$/.test(context.assignmentId)) params.set('assignment', context.assignmentId)
  if (/^[a-f0-9]{32}$/.test(context.examId)) params.set('exam', context.examId)
  if(context.mode)params.set('mode',context.mode)
  if (context.className) params.set('class',context.className.slice(0,100))
  return '#/' + view + (params.size ? '?' + params.toString() : '')
}
