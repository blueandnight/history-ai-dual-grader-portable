import {request} from './api'
import type {Submission} from './history'
export function photoCleanupReason(s:Submission):string {
  if (['queued','grading','recovery_pending'].includes(s.state)) return '任务进行中'
  if (s.photos_deleted || !s.photos?.length) return '无可清理照片'
  return ''
}
export type DeletePreview={assignment_id:string;name:string;fingerprint:string;blocked:boolean;pending_subjective?:number;unfinished_batches?:number;exams:number;subjective:number;objective:number;photos:number;links:number}
export async function previewDeletion(id:string){
  if(!/^[a-f0-9]{12}$/.test(id))throw new Error('无效考试编号')
  return request<DeletePreview>('/api/assignment-delete-preview?assignment_id='+encodeURIComponent(id))
}
export async function deleteExam(id:string,name:string,fingerprint:string){
  if(!/^[a-f0-9]{12}$/.test(id)||!name.trim()||!/^[a-f0-9]{64}$/.test(fingerprint))throw new Error('请先核对删除范围并输入考试名称')
  return request<{warning?:string}>('/api/delete-exam','POST',{assignment_id:id,confirmation_name:name,expected_fingerprint:fingerprint})
}
export async function cleanupPhotos(s:Submission,assignmentId:string){
  if(!/^[a-f0-9]{12}$/.test(s.id)||!/^[a-f0-9]{12}$/.test(assignmentId)||photoCleanupReason(s))throw new Error('答卷暂不可清理照片')
  return request('/api/delete-submission-photos','POST',{submission_id:s.id,expected_assignment_id:assignmentId,expected_exam_id:s.exam_id||'',discard_unconfirmed:!s.confirmed_at||s.teacher_reconfirmation_required===true})
}
