export type ReviewItem={id:string;exam_id?:string|null;state:string;confirmed_at?:string|null;teacher_reconfirmation_required?:boolean;photos_deleted?:boolean}
export function pendingReview(item:ReviewItem){return ['completed','failed','recovery_pending'].includes(item.state)&&(!item.confirmed_at||item.teacher_reconfirmation_required===true)&&!item.photos_deleted}
// Order is for navigation only. It never establishes student identity or links papers.
export function reviewScope(items:ReviewItem[],currentId:string,expectedExamId?:string){
 const current=items.find(s=>s.id===currentId)
 if(currentId&&!current||expectedExamId&&current&&(current.exam_id||'')!==expectedExamId)return []
 const scope=expectedExamId||(current?(current.exam_id||''):(items.find(pendingReview)?.exam_id||''))
 return items.filter(s=>(s.exam_id||'')===scope)
}
export function nextReviewId(items:ReviewItem[],currentId:string,expectedExamId?:string){
 const scoped=reviewScope(items,currentId,expectedExamId),index=scoped.findIndex(s=>s.id===currentId)
 const ordered=index<0?scoped:[...scoped.slice(index+1),...scoped.slice(0,index)]
 return ordered.find(s=>s.id!==currentId&&pendingReview(s))?.id||''
}
