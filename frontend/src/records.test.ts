import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { setSession } from './api'
import { bytes, decisionLabel, detailFinalLabel, fetchDetail, fetchStudents, fetchSystem, savedPointRows, type Detail } from './records'
beforeEach(() => {
  setSession({ required: true, authenticated: true, role: 'teacher' })
  vi.stubGlobal('window', { location: { origin: 'http://localhost' }, dispatchEvent: vi.fn() })
  vi.stubGlobal('fetch', vi.fn())
})
afterEach(() => vi.unstubAllGlobals())
function detail(): Detail {
  return { id:'a'.repeat(12),state:'completed',final_total:6,confirmed_at:'synthetic',
    teacher_scoring_questions:[{id:'Q1',max_score:8,criteria:[{id:'C1',score:2,label:'要点'}]}],
    result:{ suggested_total:12,max_total:30,grading_scope:{scoped_max_score:8},
      dual_review:{results:{A:{suggested_total:4,sub_questions:[{sub_question_id:'Q1',criteria:[{criterion_id:'C1',decision:'full',suggested_score:2,student_evidence:'合成A引文'}]}]},B:{suggested_total:2,sub_questions:[{sub_question_id:'Q1',criteria:[{criterion_id:'C1',decision:'none',suggested_score:0}]}]}}} } }
}
it('preserves separate model judgments and does not invent teacher point truth from total', () => {
  const d=detail(),p=savedPointRows(d)[0]
  expect([p.aScore,p.bScore]).toEqual([2,0])
  expect(decisionLabel(p.teacher)).toBe('无记录')
  expect(detailFinalLabel(d)).toBe('6/8')
  expect(detailFinalLabel({...d,confirmed_at:null})).toBe('尚未确认')
  expect(detailFinalLabel({...d,teacher_reconfirmation_required:true})).toBe('待重新确认')
})
it('does not fill missing/failed model sides from the root result or stale comparisons', () => {
  const d=detail();delete d.result.dual_review!.results!.B
  d.result.dual_review!.comparison={point_comparisons:[{question_id:'Q1',rubric_point_id:'C1',score_a:null,score_b:9,decision_b:'full',evidence_b:'stale'}]}
  const p=savedPointRows(d)[0]
  expect(p.aScore).toBeNull();expect(p.bScore).toBeUndefined();expect(p.bEvidence).toBeUndefined()
  d.result.dual_review!.results!.B={suggested_total:2}
  d.result.dual_review!.models={B:{status:'error'}}
  expect(savedPointRows(d)[0].bScore).toBeUndefined()
})
it('uses only saved teacher decisions, including explicit not-hit and uncertain', () => {
  const d=detail();d.result.dual_review!.teacher_final={point_decisions:[{question_id:'Q1',rubric_point_id:'C1',decision:'none'}]}
  expect(decisionLabel(savedPointRows(d)[0].teacher)).toBe('不给分')
  expect(decisionLabel('uncertain')).toBe('不确定')
})
it('teacher cannot issue admin reads by calling the system helper directly', async () => {
  await expect(fetchSystem()).rejects.toThrow('仅管理员')
  expect(fetch).not.toHaveBeenCalled()
})
it('invalid details/classes cannot trigger a request; class names are URL-encoded', async () => {
  await expect(fetchDetail('../private')).rejects.toThrow()
  await expect(fetchStudents('')).rejects.toThrow();expect(fetch).not.toHaveBeenCalled()
  vi.mocked(fetch).mockResolvedValue(new Response('{"students":[]}',{status:200}))
  await fetchStudents('合成班&其他')
  expect(fetch).toHaveBeenCalledWith('/api/roster-students?class_name='+encodeURIComponent('合成班&其他'),expect.anything())
  expect(bytes(NaN)).toBe('—');expect(bytes(1024**3)).toBe('1.0 GiB')
})
