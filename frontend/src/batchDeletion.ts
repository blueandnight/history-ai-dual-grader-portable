import {request} from './api'
import type {Batch} from './records'
export type BatchDeletePreview={batch_id:string;assignment_id:string;exam_id:string;name:string;created_at:string;total:number;submissions:number;confirmed:number;pending:number;unuploaded:number;photos:number;links:number;blocked:boolean;fingerprint:string}
export type BatchDeleted={ok:boolean;warning?:string;shared_crop_runs_preserved?:number}
function scope(b:Batch){if(!/^[a-f0-9]{12}$/.test(b.batch_id)||!/^[a-f0-9]{12}$/.test(b.assignment_id)||(b.exam_id&&!/^[a-f0-9]{32}$/.test(b.exam_id)))throw Error('批次范围无效');return {batch_id:b.batch_id,assignment_id:b.assignment_id,exam_id:b.exam_id||''}}
export async function previewBatchDelete(b:Batch){const q=scope(b);const p=await request<BatchDeletePreview>('/api/batch-delete-preview?'+new URLSearchParams(q));if(p.batch_id!==q.batch_id||p.assignment_id!==q.assignment_id||p.exam_id!==q.exam_id)throw Error('删除预览与当前批次不一致');return p}
export function deleteBatch(b:Batch,p:BatchDeletePreview,confirmed:boolean){const q=scope(b);if(!confirmed||p.blocked||p.batch_id!==q.batch_id||p.assignment_id!==q.assignment_id||p.exam_id!==q.exam_id||!/^[a-f0-9]{64}$/.test(p.fingerprint))throw Error('请先核对本批删除范围并明确确认');return request<BatchDeleted>('/api/delete-batch','POST',{...q,confirmed:true,expected_fingerprint:p.fingerprint})}
