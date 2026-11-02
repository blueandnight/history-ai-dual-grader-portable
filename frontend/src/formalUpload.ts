import { request } from './api'
export type FormalAssignment = { id: string; name: string; submission_count?:number;objective_submission_count?:number;created_at?:string; formal_layout_required?: boolean; exam_id?: string; layout_exams: {exam_id:string;created_at?:string}[]; original_photo_exams?: {exam_id:string;created_at?:string}[]; sub_questions: {id:string;label:string}[];answer_region_profile?:{page_count:number;regions:import('./privacyEditing').AnswerRegion[]}|null }
export type Group = { label: string; files: File[] }
export type Layout = { exam_id:string;assignment_id:string;answer_regions_required:boolean;layout_profile_id:string;profile_invalid?:boolean;profile_hash_mismatch?:boolean;pdf_hash_mismatch?:boolean;subjective_question_ids:string[];layout_profile: {regions:{region_type:string;question_id:string}[]} | null }
export type CropRun = { status:string;failure_code?:string;alignment_assisted?:boolean;exam_id:string;layout_profile_id:string;alignment_run_id:string;partial_processing?:boolean;crops:{question_id:string;file_name:string;padding_validation_file_name?:string}[];accepted_pages?:{page_number:number}[] }
export type Accepted = { question_id:string;layout_profile_id:string;data_url:string;attestation:string;provenance:{exam_id:string} }
export type Region = {region_id:string;profile_id:string;photo_index:number;x:number;y:number;width:number;height:number;sub_question_ids:string[];coordinate_system:string;rotation_ccw:number;invocation_scope:string}
export type Photo = { name:string;page_sequence:number;data_url:string;original_data_url:string;image_role:string;answer_region_attestation:string;ai_redactions:unknown[];corners?:unknown }
export type Preview = { blocked?:boolean;preview_token:string;preview_fingerprint:string;prompt:string;region_prompts?:{label:string;prompt:string}[];images:{data_url:string;image_kind?:string;sub_question_ids:string[]}[];answer_regions:Region[];provider?:string;model?:string }
export type GradingInputMode = 'auto_crop'|'original_photo'
export function anonymousExamCandidates(a:FormalAssignment,mode:GradingInputMode){
  return (mode==='original_photo'?a.original_photo_exams:a.layout_exams)||[]
}
export type Approval = { photos:Photo[];regions:Region[];filenames:string[];preview:Preview;gradingInputMode?:GradingInputMode;examId?:string;settings?:import('./privacyEditing').PrivacySettings }
export const privacyStatuses = ['pre_anonymized','redacted','no_identity_visible']
export function bulkCropChecks(checks:boolean[],value:boolean):boolean[]{
  return checks.map(()=>value)
}
export function applyPendingPrivacy(statuses:string[],value:string):string[]{
  return privacyStatuses.includes(value)?statuses.map(s=>s==='unchecked'?value:s):[...statuses]
}
export function groupsFromFiles(files:File[], pages:number, _retiredIdentityMode=false):Group[] {
  if (!files.length || files.some(f=>!f.size || f.size>25*1024*1024 || !/\.(png|jpe?g)$/i.test(f.name))) throw new Error('请选择非空PNG/JPEG照片，单张不超过25MiB。')
  let groups:Group[]=[]
  if(!Number.isInteger(pages)||pages<1||pages>10||files.length%pages)throw new Error('每份需1—10张照片，且文件总数须能按页数完整分组。')
  for(let i=0;i<files.length;i+=pages)groups.push({label:`第${groups.length+1}份`,files:files.slice(i,i+pages)})
  if(groups.length>100||groups.some(g=>g.files.length>10))throw new Error('每批最多100份，每份最多10页。')
  return groups
}
export function assertLayout(c:Layout,assignmentId:string){
  if(c.assignment_id!==assignmentId||!c.answer_regions_required||!c.layout_profile||!c.layout_profile_id||c.profile_invalid||c.profile_hash_mismatch||c.pdf_hash_mismatch)throw new Error('正式考试缺少有效答题区，请到答题区设置完善布局后重新开始。')
  const configured=new Set(c.layout_profile.regions.filter(r=>r.region_type==='subjective_answer').map(r=>r.question_id))
  if(!c.subjective_question_ids.length||c.subjective_question_ids.some(id=>!configured.has(id)))throw new Error('答题区尚未覆盖全部主观题。')
}
export function assertOriginalExam(c:Layout,assignmentId:string,examId:string){if(c.assignment_id!==assignmentId||c.exam_id!==examId)throw new Error('所选考试与当前作业不匹配。')}
export function assertRuns(runs:CropRun[],c:Layout){
  const seen=new Set<string>()
  for(const run of runs){
    if(run.status==='alignment_failed')throw new Error(run.failure_code==='page_layout_mismatch'
      ? '照片与参考页的整页布局无法可靠对应。请核对是否为同一版答题卡（含信息栏、表格和小题位置），使用实际印发版空白PDF设置答题区；也请检查照片是否缺边或变形。本批未发送模型。'
      : '没有足够且分布可靠的页面匹配点。请核对单页/双页模式及参考页，并上传完整、清晰的答题卡照片。本批未发送模型。')
    if(run.status!=='cropped'||run.partial_processing||run.exam_id!==c.exam_id||run.layout_profile_id!==c.layout_profile_id||!run.crops?.length||!/^[a-f0-9]{32}$/.test(run.alignment_run_id))throw new Error('配准或裁切未通过，不能上传。')
    for(const crop of run.crops){if(!c.subjective_question_ids.includes(crop.question_id)||seen.has(crop.question_id))throw new Error('小题重复或不属于当前考试，请核对分组。');seen.add(crop.question_id);artifact(run.alignment_run_id,crop.padding_validation_file_name||crop.file_name)}
  }
  if(seen.size!==c.subjective_question_ids.length||seen.size>10)throw new Error('照片没有完整覆盖本次主观题。')
}
export function artifact(run:string,file:string){
  if(!/^[a-f0-9]{32}$/.test(run)||!/^(?:padding_validation\/)?[A-Za-z0-9_-][A-Za-z0-9_.-]*\.png$/.test(file))throw new Error('裁切预览位置无效。')
  return `/api/student-auto-crop-artifact?run_id=${run}&file=${encodeURIComponent(file)}`
}
export function imageUrl(url:string){if(!/^data:image\/(?:jpeg|png);base64,[A-Za-z0-9+/=]+$/.test(url))throw new Error('外发预览图无效。');return url}
export function acceptedPayload(crops:Accepted[],a:FormalAssignment,c:Layout){
  const wanted=new Set(a.sub_questions.map(q=>q.id)),seen=new Set(crops.map(p=>p.question_id))
  if(crops.length!==wanted.size||seen.size!==wanted.size||[...wanted].some(id=>!seen.has(id))||crops.some(p=>!p.attestation||p.provenance?.exam_id!==c.exam_id||p.layout_profile_id!==c.layout_profile_id))throw new Error('正式裁切凭据不完整或与考试不一致。')
  const photos:Photo[]=crops.map((p,i)=>({name:`crop-${i+1}.png`,page_sequence:i+1,data_url:imageUrl(p.data_url),original_data_url:p.data_url,image_role:'answer_region',answer_region_attestation:p.attestation,ai_redactions:[]}))
  const regions:Region[]=crops.map((p,i)=>({region_id:`accepted:${p.question_id}`,profile_id:p.layout_profile_id,photo_index:i+1,x:0,y:0,width:1,height:1,sub_question_ids:[p.question_id],coordinate_system:'perspective_corrected_page_normalized',rotation_ccw:0,invocation_scope:'answer_region'}))
  return {photos,regions}
}
export function assertPreview(p:Preview,count:number,gradingInputMode:GradingInputMode='auto_crop'){
  const original=gradingInputMode==='original_photo'
  if(p.blocked||!p.preview_token||!p.preview_fingerprint||p.images?.length!==count||(original?p.answer_regions?.length!==0||p.images.some(i=>i.image_kind!=='full_page'):p.answer_regions?.length!==count||p.images.some(i=>i.image_kind==='full_page')||p.answer_regions.some(r=>r.invocation_scope!=='answer_region')))throw new Error('正式外发预览未通过，不能上传。')
  p.images.forEach(i=>imageUrl(i.data_url))
}
export function canSend(statuses:string[],count:number,consent:boolean){return consent && count>0 && statuses.length===count && statuses.every(s=>privacyStatuses.includes(s))}
export async function uploadReservedUntilStopped<T>(items:T[],startIndex:number,upload:(item:T,index:number)=>Promise<unknown>,shouldStop:()=>boolean,onUploaded:(index:number)=>void):Promise<number>{
  let index=startIndex
  while(index<items.length&&!shouldStop()){
    await upload(items[index]!,index)
    onUploaded(index)
    index++
  }
  return index
}
export function uploadBody(a:FormalAssignment,exam:string,className:string,approval:Approval,status:string,batch:{batch_id:string;items:{submission_id:string;sequence?:number}[]},index:number){
  if(!privacyStatuses.includes(status)||!batch.items[index]?.submission_id)throw new Error('尚未确认隐私或批次记录无效。')
  const gradingInputMode=approval.gradingInputMode||'auto_crop'
  assertPreview(approval.preview,approval.photos.length,gradingInputMode)
  return {assignment_id:a.id,exam_id:gradingInputMode==='original_photo'?approval.examId||'':exam,class_name:className,student_label:`第${index+1}份`,answer_text:'',dual_review_enabled:true,grading_input_mode:gradingInputMode,
    batch_id:batch.batch_id,submission_id:batch.items[index]!.submission_id,sequence:index+1,idempotency_key:`${batch.batch_id}:${index+1}`,
    ai_privacy_confirmed:true,privacy_status:status,ai_privacy_preview_token:approval.preview.preview_token,ai_privacy_fingerprint:approval.preview.preview_fingerprint,
    answer_regions:approval.preview.answer_regions,photos:approval.photos,}
}
export function originalPhotoPayload(approval:import('./privacyEditing').PrivacyApproval,files:File[],examId:string,filenames:string[]=[]):Approval{
  if(approval.dataUrls.length!==files.length||approval.preview.images.length!==files.length||approval.preview.answer_regions.length)throw new Error('原照片外发预览与所选照片不一致。')
  const photos:Photo[]=files.map((_,i)=>({name:`page-${i+1}.jpg`,page_sequence:i+1,data_url:imageUrl(approval.dataUrls[i]!),original_data_url:approval.dataUrls[i]!,image_role:'source_page',answer_region_attestation:'',ai_redactions:approval.settings.redactions[i]||[],corners:approval.settings.corners[i]||null}))
  const preview:Preview={...approval.preview,images:approval.preview.images.map(image=>({data_url:image.data_url,image_kind:image.image_kind,sub_question_ids:image.sub_question_ids})),answer_regions:[]}
  return {photos,regions:[],filenames,preview,gradingInputMode:'original_photo',examId,settings:approval.settings}
}
export async function cropFile(file:File,exam:string,mode:string){const form=new FormData();form.append('metadata',JSON.stringify({exam_id:exam,input_mode:mode}));form.append('student_image',file,file.name);return request<CropRun>('/api/student-auto-crop','POST',form)}
