import {expect,it} from 'vitest'
import {nextReviewId,pendingReview,reviewScope,type ReviewItem} from './reviewQueue'
const row=(id:string,extra:Partial<ReviewItem>={}):ReviewItem=>({id,exam_id:'exam-a',state:'completed',...extra})
it('continues in displayed order, wraps once and skips confirmed/running/discarded papers',()=>{
 const items=[row('first'),row('saved',{confirmed_at:'saved'}),row('running',{state:'grading'}),row('discarded',{photos_deleted:true}),row('last')]
 expect(nextReviewId(items,'first','exam-a')).toBe('last')
 expect(nextReviewId(items,'last','exam-a')).toBe('first')
 expect(nextReviewId([row('first')],'first','exam-a')).toBe('')
})
it('never advances across formal exams or between formal and anonymous records',()=>{
 const items=[row('a'),row('b',{exam_id:'exam-b'}),row('anonymous',{exam_id:null})]
 expect(nextReviewId(items,'a')).toBe('')
 expect(nextReviewId(items,'anonymous')).toBe('')
 expect(nextReviewId(items,'missing','exam-a')).toBe('')
 expect(reviewScope(items,'a','exam-b').map(s=>s.id)).toEqual([])
})
it('keeps reconfirmation and legitimate confirmed zero distinct from pending scores',()=>{
 expect(pendingReview(row('confirmed-zero',{confirmed_at:'saved'}))).toBe(false)
 expect(pendingReview(row('reconfirm',{confirmed_at:'saved',teacher_reconfirmation_required:true}))).toBe(true)
 expect(nextReviewId([row('first',{confirmed_at:'saved'}),row('second')],'','exam-a')).toBe('second')
})
