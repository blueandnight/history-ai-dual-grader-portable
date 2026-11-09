import { afterEach, expect, it, vi } from 'vitest'
import { blockers, conflictEvidence, conflictIds, createExam, editDraft, filePayload, preserveAiExamEdits, references, scoreTotals, sumWarning, type Draft } from './examImport'
import { setSession } from './api'
const draft = (): Draft => ({ title:'合成考试',objective_questions:[{question_id:'1',stem:'合成题',options:{A:'甲',B:'乙',C:'丙',D:'丁'}}],objective_answer_key:{'1':'A'},objective_points:{'1':3},material_questions:[{label:'9',prompt:'合成材料',sub_questions:[{question_id:'9.1',question:'设问',reference_answer:'参考',max_score:4,rubric_draft:[{label:'甲',score:1},{label:'乙',score:3}]}]}],source:{source_pair_status:'not_applicable'} })
afterEach(() => { vi.unstubAllGlobals(); setSession(null) })
it('keeps a teacher answer even when both server drafts have no answer key entry',()=>{
 const old=draft();old.objective_answer_key={};const current=editDraft(old);current.objective_answer_key['1']='D'
 const merged=preserveAiExamEdits(structuredClone(old),old,current)
 expect(merged.objective_answer_key['1']).toBe('D');expect(merged.teacher_confirmed).toBe(false)
})
it('AI merge protects manual fields and rules across repeated parses while filling untouched blanks',()=>{
 const old=draft(),current=editDraft(old);current.exam_name='教师标题';current.teacher_confirmed=true
 current.objective_answer_key['1']='D';const q=current.material_questions[0].sub_questions[0]
 q.question='教师设问';q.reference_answer='教师答案';q.max_score=8;q.scoring_mode='point_bank';q.point_value=2;q.rubric_draft=[{label:'教师点',score:2}]
 old.material_questions[0].sub_questions.push({question_id:'9.2',question:'未编辑',reference_answer:'',max_score:null,rubric_draft:[]})
 current.material_questions[0].sub_questions.push({...old.material_questions[0].sub_questions[1]})
 const incoming=structuredClone(old);incoming.material_questions[0].sub_questions[1].reference_answer='AI补空白';incoming.material_questions[0].sub_questions[1].max_score=6
 incoming.objective_answer_key['2']='B'
 const merged=preserveAiExamEdits(incoming,old,current)
 expect(merged.exam_name).toBe('教师标题');expect(merged.objective_answer_key).toEqual({'1':'D','2':'B'})
 expect(merged.material_questions[0].sub_questions[0]).toMatchObject(q)
 expect(merged.material_questions[0].sub_questions[1]).toMatchObject({reference_answer:'AI补空白',max_score:6})
 expect(merged.teacher_confirmed).toBe(false)
 expect(preserveAiExamEdits(incoming,incoming,merged).material_questions[0].sub_questions[0]).toMatchObject(q)
})
it('reference links include exam and blank answer-sheet pages, excluding unknown/answer pages', () => {
  const d=draft();d.layout_reference={pages:[{page_number:3,page_role:'exam'},{page_number:4,page_role:'answer_sheet'},{page_number:5,page_role:'answer'},{page_number:6}],page_roles:[{page_number:4,page_role:'answer_sheet'},{page_number:5,page_role:'answer'}]}
  expect(references(d,'a'.repeat(32))).toEqual([
    {page:3,url:'/api/exam-import-reference?draft_id='+ 'a'.repeat(32)+'&page_number=3'},
    {page:4,url:'/api/exam-import-reference?draft_id='+ 'a'.repeat(32)+'&page_number=4'},
  ]);expect(references(d,'../secret')).toEqual([])
})
it('keeps a teacher rubric visible when AI proposes a different scoring mode',()=>{
 const old=draft(),current=editDraft(old),incoming=structuredClone(old)
 const manual=current.material_questions[0].sub_questions[0],ai=incoming.material_questions[0].sub_questions[0]
 manual.rubric_draft[0].label='教师修改的评分点'
 Object.assign(ai,{scoring_mode:'holistic',rubric_draft:[],holistic:{levels:[],dimensions:'论证',notes:'AI提取'}})
 const result=preserveAiExamEdits(incoming,old,current).material_questions[0].sub_questions[0]
 expect(result.scoring_mode).toBe('weighted');expect(result.holistic).toBeUndefined()
 expect(result.rubric_draft).toEqual(manual.rubric_draft)
})
it('keeps unequal weights, IDs and missing scores, requiring explicit confirmation', () => {
  const d=draft(), c=editDraft(d); expect(c.teacher_confirmed).toBe(false)
  c.teacher_confirmed=true; expect(blockers(d,c)).toEqual([])
  c.material_questions[0].sub_questions[0].rubric_draft[0].score=null
  expect(blockers(d,c).join()).toContain('正分值'); expect(d.material_questions[0].sub_questions[0].rubric_draft[0].score).toBe(1)
})
it('does not allow a checkbox to override a mismatched source pair', () => {
  const d=draft();d.source.source_pair_status='mismatch';const c=editDraft(d);c.source_pair_confirmed=c.teacher_confirmed=true
  expect(blockers(d,c).join()).toContain('不一致')
  d.source.source_pair_status='needs_teacher_confirmation';c.source_pair_confirmed=false
  expect(blockers(d,c).join()).toContain('同一份')
})
it('requires a separate matching conflict decision, never preselects it', () => {
  const d=draft();d.answer_key_crosscheck={status:'conflict'};d.source.source_conflicts=[{type:'answer_key_conflict',question_id:'1'}]
  const c=editDraft(d);c.teacher_confirmed=true
  expect(conflictIds(d)).toEqual(['1']);expect(blockers(d,c).join()).toContain('裁决')
  c.answer_conflict_resolutions['1']='B';expect(blockers(d,c).join()).toContain('裁决')
  c.answer_conflict_resolutions['1']='A';expect(blockers(d,c)).toEqual([])
})
it('renders question text sampling as compared/matched counts, never phantom Word/PDF evidence', () => {
  const d=draft();d.source.source_pair_status='mismatch'
  d.source.source_conflicts=[{type:'question_text_mismatch',compared:16,matched:0}]
  expect(conflictEvidence(d)).toEqual(['题干文本抽样：已比较 16 题，一致 0 题'])
  const text=conflictEvidence(d).join()
  expect(text).not.toContain('—');expect(text).not.toContain('Word');expect(text).not.toContain('PDF')
  d.source.source_conflicts=[{type:'question_text_mismatch'}]
  expect(conflictEvidence(d)).toEqual(['题干文本不一致'])
})
it('shows only the non-empty Word/PDF id lists for question id mismatches', () => {
  const d=draft()
  d.source.source_conflicts=[
    {type:'question_id_mismatch',section:'objective_questions',docx_only:['7','8'],pdf_only:[]},
    {type:'question_id_mismatch',section:'subjective_subquestions',docx_only:[],pdf_only:['17.2']},
  ]
  const [objective,subjective]=conflictEvidence(d)
  expect(objective).toBe('客观题题号不一致：Word 独有：7、8');expect(objective).not.toContain('PDF 独有');expect(objective).not.toContain('—')
  expect(subjective).toBe('主观题题号不一致：PDF 独有：17.2');expect(subjective).not.toContain('Word 独有');expect(subjective).not.toContain('—')
})
it('keeps both source answers visible for answer key conflicts', () => {
  const d=draft();d.source.source_conflicts=[{type:'answer_key_conflict',question_id:'1',docx_answer:'A',pdf_answer:'B'}]
  expect(conflictEvidence(d)).toEqual(['第 1 题答案冲突：Word A；PDF B'])
})
it('never fabricates source evidence for unknown conflict types', () => {
  expect(conflictEvidence(draft())).toEqual([])
  const d=draft();d.source.source_conflicts=[{type:'future_conflict',docx_only:['1']}]
  expect(conflictEvidence(d)).toEqual(['future_conflict'])
})
it('blocks incomplete objective structure, empty references and invalid point values', () => {
  const d=draft();d.objective_questions[0].needs_teacher_review=true;const c=editDraft(d);c.teacher_confirmed=true
  const q=c.material_questions[0].sub_questions[0];q.reference_answer='';q.max_score=Infinity;q.rubric_draft[0].score=-1
  expect(blockers(d,c)).toHaveLength(4)
})
it('warns about score sums without silently rewriting weights or max', () => {
  const q=draft().material_questions[0].sub_questions[0];q.max_score=8
  expect(sumWarning(q)).toContain('合计4');expect(q.rubric_draft.map(p=>p.score)).toEqual([1,3]);expect(q.max_score).toBe(8)
})
it('explicit candidate bank supports five two-point options capped at eight and protects custom weights',()=>{
 const d=draft(),c=editDraft(d);c.teacher_confirmed=true
 const q=c.material_questions[0].sub_questions[0]
 expect(q.scoring_mode).toBe('weighted')
 q.scoring_mode='point_bank';q.point_value=2;q.max_score=8;q.rubric_draft=Array.from({length:5},(_,i)=>({label:'独立要点'+i,score:2}))
 expect(blockers(d,c)).toEqual([]);expect(sumWarning(q)).toBe('')
 q.rubric_draft[0].score=1;expect(blockers(d,c).join()).toContain('分值一致');expect(q.rubric_draft[0].score).toBe(1)
 q.rubric_draft[0].score=2;q.max_score=12;expect(blockers(d,c).join()).toContain('不足')
})
it('reparsing never carries a prior confirmation or conflict resolution', () => {
  const c=editDraft(draft());c.teacher_confirmed=c.source_pair_confirmed=true;c.answer_conflict_resolutions['1']='A'
  expect(editDraft(draft())).toMatchObject({teacher_confirmed:false,source_pair_confirmed:false,answer_conflict_resolutions:{}})
})
it('rejects wrong suffix, empty and oversized files before reading', async () => {
  for(const f of [{name:'x.doc',size:5},{name:'x.docx',size:0},{name:'x.docx',size:11*1024*1024}]) await expect(filePayload(f as File,'docx')).rejects.toThrow('10MB')
  const f={name:'x.docx',size:3,arrayBuffer:async()=>new Uint8Array([0,128,255]).buffer} as File
  expect(await filePayload(f,'docx')).toEqual({docx_filename:'x.docx',docx_data_base64:'AID/'})
})
it('uses session CSRF for creation and sends only one request on failure', async () => {
  vi.stubGlobal('window',{location:{origin:'http://127.0.0.1:1234'},dispatchEvent:vi.fn()});vi.stubGlobal('fetch',vi.fn().mockRejectedValue(new Error('offline')))
  setSession({required:true,authenticated:true,csrf_token:'synthetic'})
  await expect(createExam('a'.repeat(32),editDraft(draft()))).rejects.toThrow()
  expect(fetch).toHaveBeenCalledTimes(1);expect(fetch).toHaveBeenCalledWith('/api/create-imported-exam',expect.objectContaining({headers:expect.objectContaining({'X-CSRF-Token':'synthetic'})}))
})

it('requires explicit objective scores and keeps paper totals separate',()=>{
 const d=draft();delete d.objective_points;const c=editDraft(d);c.teacher_confirmed=true
 expect(c.objective_points['1']).toBe(null);expect(blockers(d,c).join()).toContain('分值')
 expect(scoreTotals(c)).toEqual({objective:null,subjective:4,paper:null})
 c.objective_points['1']=3;expect(scoreTotals(c)).toEqual({objective:3,subjective:4,paper:7});expect(blockers(d,c)).toEqual([])
 for(const bad of ['',0,-1,Infinity,101]){c.objective_points['1']=bad;expect(blockers(d,c).join()).toContain('分值')}
 c.objective_points['1']=2.5;expect(preserveAiExamEdits(d,d,c).objective_points['1']).toBe(2.5)
 d.objective_questions[0]!.question_id='3';expect(blockers(d,c).join()).toContain('连续编号')
})
