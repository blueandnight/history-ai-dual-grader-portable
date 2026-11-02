import { request } from './api'
import { savedPointRows, type Detail } from './records'
export type Choices = Record<string,string>
export const pointKey=(q:string,p:string)=>JSON.stringify([q,p])
export function savedChoices(d:Detail):Choices {
  const points=d.result.dual_review?.teacher_final?.point_decisions||d.result.teacher_final?.point_decisions||[]
  return Object.fromEntries(points.map(p=>[pointKey(p.question_id,p.rubric_point_id),({full:'teacher_full',none:'teacher_none',uncertain:'teacher_uncertain',hit:'teacher_full',not_hit:'teacher_none'} as Record<string,string>)[p.decision||p.teacher_decision||'']||'']))
}
export function resolvedChoice(choice:string,row:ReturnType<typeof savedPointRows>[number]){
  if(choice==='model_a')return row.aDecision
  if(choice==='model_b')return row.bDecision
  return ({teacher_full:'full',teacher_none:'none',teacher_uncertain:'uncertain'} as Record<string,string>)[choice]
}
export function completeChoices(d:Detail,choices:Choices){const rows=savedPointRows(d);return rows.length>0&&rows.every(r=>['full','none'].includes(resolvedChoice(choices[pointKey(r.questionId,r.pointId)]||'',r)||''))}
export function reviewable(d:Detail){return d.state==='completed'&&d.result.mode==='vision-model'&&!d.result.simulation_model}
export function unresolved(d:Detail){return !!d.result.unmapped_answers?.length||(d.result.sub_questions||[]).some(q=>['unanswered_candidate','question_unmatched','image_unreadable'].includes(q.answer_status||''))}
export function confirmationAllowed(d:Detail,choices:Choices){return !d.result.photos_discarded_unconfirmed&&reviewable(d)&&!unresolved(d)&&(!d.result.blocked||d.manual_reviewed===true)&&completeChoices(d,choices)}
export function confirmationBody(d:Detail,choices:Choices,note:string,expanded:boolean){
  if(!confirmationAllowed(d,choices))throw new Error('请完成所有逐点判断并处理原图复核/空白或题号问题。')
  return {submission_id:d.id,...(d.exam_id?{exam_id:d.exam_id}:{}),teacher_note:note.slice(0,2000),expanded_full_answer:expanded,teacher_point_decisions:savedPointRows(d).map(r=>({question_id:r.questionId,rubric_point_id:r.pointId,choice:choices[pointKey(r.questionId,r.pointId)]}))}
}
export function originalUrl(id:string,index:number){if(!/^[a-f0-9]{12}$/.test(id)||!Number.isInteger(index)||index<1)throw new Error('原图参数无效');return `/api/submission-photo?submission_id=${id}&photo_index=${index}`}
export function confirmTeacher(d:Detail,choices:Choices,note:string,expanded:boolean){return request<{final_total:number;max_total:number}>('/api/confirm','POST',confirmationBody(d,choices,note,expanded))}
export function recordReview(id:string,note:string){return request('/api/review','POST',{submission_id:id,confirm_original:true,review_note:note.slice(0,2000)})}
export function confirmBlank(id:string,q:string){return request('/api/confirm-blank-subquestion','POST',{submission_id:id,sub_question_id:q})}
