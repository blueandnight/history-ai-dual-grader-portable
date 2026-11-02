import {request} from './api'
import {examHref,type ExamContext} from './navigation'
import type {Batch} from './records'
export function batchHref(b:Pick<Batch,'batch_id'|'assignment_id'|'exam_id'>,context?:ExamContext){return examHref('batch',{assignmentId:b.assignment_id,examId:context?.mode==='quick'?'':b.exam_id||'',mode:context?.mode||'formal'})+'&batch='+encodeURIComponent(b.batch_id)}
export async function fetchBatchRecords(context?:ExamContext,batchId?:string){
 const p=new URLSearchParams({include_completed:'1',summary:'1'})
 for(const [key,value,re] of [['assignment_id',context?.assignmentId,/^[a-f0-9]{12}$/],['exam_id',context?.examId,/^[a-f0-9]{32}$/],['batch_id',batchId,/^[a-f0-9]{12}$/]] as const){if(value){if(!re.test(value))throw Error('批次范围无效');p.set(key,value)}}
 return (await request<{batches:Batch[]}>('/api/batches?'+p)).batches
}
export function batchTime(value?:string){if(!value)return '时间未记录';const d=new Date(value);return Number.isNaN(d.getTime())?'时间未记录':d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}
export function batchState(b:Batch){return b.grading_stopped?'已停止':b.status==='paused'?'待处理':b.status==='completed'?'处理结束':'进行中'}
