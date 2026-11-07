import {expect,it} from 'vitest'
import {reorder,newSub,moveSub,mergeNext,splitSub,editDraft,blockers,preserveAiExamEdits,type Major,type Draft} from './examImport'
const major=():Major=>({label:'19',prompt:'合成材料甲',sub_questions:[{question_id:'19.1',question:'设问甲\n设问乙',reference_answer:'参考甲；参考乙',max_score:12,scoring_mode:'weighted',rubric_draft:[{label:'合成标准',score:12}]}]})
it('split retains reference, clears unknown allocations and supports manual repair',()=>{
 const m=major();splitSub(m,m.sub_questions[0]);expect(m.sub_questions.map(q=>q.question)).toEqual(['设问甲','设问乙'])
 expect(m.sub_questions[0].reference_answer).toBe('参考甲；参考乙');expect(m.sub_questions[1].reference_answer).toBe('')
 expect(m.sub_questions.every(q=>q.max_score===null)).toBe(true)
 m.sub_questions[0].max_score=4;m.sub_questions[1].max_score=8;mergeNext(m,0)
 expect(m.sub_questions).toHaveLength(1);expect(m.sub_questions[0].max_score).toBe(12)
 expect(m.sub_questions[0].question).toContain('设问乙')
})
it('moves source context and answers together using a unique destination number',()=>{
 const m=major(),dest:Major={label:'20',prompt:'合成材料乙',sub_questions:[newSub('20',[])]}
 moveSub(m,dest,m.sub_questions[0]);expect(m.sub_questions).toHaveLength(0)
 const q=dest.sub_questions[1];expect(q.question_id).toBe('20.2');expect(q.question).toContain('合成材料甲');expect(q.reference_answer).toContain('参考乙')
})
it('preserves teacher structure on repeated AI parses and accepts holistic draft without reference',()=>{
 const d:Draft={title:'合成',source:{},objective_questions:[],objective_answer_key:{},material_questions:[major()]}
 const c=editDraft(d);c.material_questions[0].label='21';const q=c.material_questions[0].sub_questions[0];q.question_id='21.1'
 q.scoring_mode='holistic';q.reference_answer='';q.holistic={score_step:1,levels:[{min_score:0,max_score:12,description:'综合标准'}],dimensions:'观点与论证',notes:''};c.teacher_confirmed=true
 expect(blockers(d,c)).toEqual([])
 expect(preserveAiExamEdits(d,d,c).material_questions[0].sub_questions[0].holistic).toEqual(q.holistic)
})
it('merges holistic text without inventing a replacement level scheme',()=>{
 const m=major(),a=m.sub_questions[0];a.scoring_mode='holistic';a.holistic={levels:[{min_score:0,max_score:12,description:'原标准甲'}],dimensions:'观点',notes:''}
 const b=structuredClone(a);b.question_id='19.2';b.holistic!.levels[0].description='原标准乙';m.sub_questions.push(b)
 mergeNext(m,0);expect(a.max_score).toBe(24);expect(a.holistic!.levels).toEqual([])
 expect(a.holistic!.notes).toContain('原标准甲');expect(a.holistic!.notes).toContain('原标准乙')
})
it('keeps point-bank mode when splitting and reorders without reassigning answers',()=>{
 const m=major(),q=m.sub_questions[0];q.scoring_mode='point_bank';q.point_value=2
 splitSub(m,q);const next=m.sub_questions[1]
 expect(next.scoring_mode).toBe('point_bank');expect(next.point_value).toBe(2);expect(next.rubric_draft).toEqual([])
 reorder(m.sub_questions,1,-1);expect(m.sub_questions[1]).toBe(q);expect(q.reference_answer).toBe('参考甲；参考乙')
 reorder(m.sub_questions,0,1);expect(()=>mergeNext(m,0)).not.toThrow();expect(m.sub_questions).toHaveLength(1)
})
