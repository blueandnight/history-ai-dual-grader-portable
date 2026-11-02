import {request} from './api'
export type AnswerGroup={key:string;members:string[];label:string;subjective:number;objective:number;blocked:boolean;states:string[]}
export type DeleteScope={assignment_id:string;exam_id?:string;keys?:string[]}
export type DataPreview={fingerprint:string;name:string;groups:number;subjective:number;objective:number;photos:number;batches:number;jobs:number;blocked:boolean}
export const listAnswers=(scope:DeleteScope)=>request<{groups:AnswerGroup[]}>('/api/answer-management?'+new URLSearchParams({assignment_id:scope.assignment_id,exam_id:scope.exam_id||''}))
export const previewAnswers=(scope:DeleteScope,clear=false)=>request<DataPreview>(clear?'/api/clear-grading-preview':'/api/answer-delete-preview','POST',scope)
export type DeleteResult={ok?:boolean;warning?:string;verified_remaining?:{subjective:number;objective:number;jobs:number;batches:number;identities:number;links:number}|null}
export function requireCleared(result:DeleteResult){
 const remaining=result.verified_remaining
 if(result.ok!==true||!remaining||(['subjective','objective','jobs','batches','identities','links'] as const).some(k=>remaining[k]!==0))throw new Error('尚未核实主客观题和身份关联全部清空，请重新读取范围；不会自动重试')
}
export async function removeAnswers(scope:DeleteScope,p:DataPreview,clear=false){
 const result=await request<DeleteResult>(clear?'/api/clear-grading-data':'/api/delete-answers','POST',{...scope,confirmed:true,expected_fingerprint:p.fingerprint})
 if(clear)requireCleared(result)
 return result
}
export function dataChanged(){window.dispatchEvent(new Event('grading-data-changed'));try{localStorage.setItem('grading-data-revision',String(Date.now()))}catch{/* Local events still refresh this workspace. */}}
