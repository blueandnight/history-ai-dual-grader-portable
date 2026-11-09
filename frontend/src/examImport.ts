import { request } from './api'
export type Point = { label: string; score: number | string | null }
export type Sub = { question_id: string; question: string; reference_answer: string; ai_reference_alternative?: string; scoring_guide_source?: string; max_score: number | string | null; rubric_draft: Point[]; scoring_mode?: 'weighted'|'point_bank'|'holistic'; question_type?:'subjective'|'essay'|'explanation'; holistic?:{aggregation?:string;projects?:{id:string;label:string;max_score:number;score_step:number;levels:{id:string;min_score:number;max_score:number;description:string}[]}[];score_step?:number|string|null;dimension_definitions?:{id:string;label:string}[];levels:{id?:string;min_score:number|string|null;max_score:number|string|null;description:string}[];dimensions:string;notes:string}; point_value?: number|string|null }
export type Major = { label: string; prompt: string; sub_questions: Sub[] }
export type SourceConflict = {
  type: string; section?: string; label?: string; question_id?: string;
  docx_answer?: string; pdf_answer?: string;
  docx_only?: string[]; pdf_only?: string[]; compared?: number; matched?: number;
}
export type Draft = {
  source_tables?:string[][][]; source_blocks?:string[]; title: string; objective_questions: { question_id: string; stem: string; options: Record<string,string>; needs_teacher_review?: boolean }[];
  objective_answer_key: Record<string,string>; objective_points?: Record<string,number|string|null>; material_questions: Major[];
  source: { source_pair_status?: string; source_conflicts?: SourceConflict[] };
  answer_key_crosscheck?: { status: string }; warnings?: string[];
  layout_reference?: { pages?: { page_number: number; page_role?: string }[]; page_roles?: { page_number: number; page_role: string }[] };
}
export type Confirmation = { exam_name: string; objective_answer_key: Record<string,string>; objective_points: Record<string,number|string|null>; material_questions: Major[]; teacher_confirmed: boolean; source_pair_confirmed: boolean; answer_conflict_resolutions: Record<string,string> }
export type Created = { exam_id: string; assignment: { id: string; name: string; objective_count: number; subjective_count: number; score_totals?: {objective:number;subjective:number;paper:number} }; warnings?: string[] }
export function editDraft(draft: Draft): Confirmation {
  return { exam_name: draft.title || '', objective_answer_key: { ...draft.objective_answer_key }, objective_points:Object.fromEntries(draft.objective_questions.map(q=>[q.question_id,draft.objective_points?.[q.question_id]??null])),
    material_questions: (JSON.parse(JSON.stringify(draft.material_questions || [])) as Major[]).map(m=>({...m,sub_questions:m.sub_questions.map(q=>({...q,scoring_mode:q.scoring_mode||'weighted',point_value:q.point_value??2}))})), teacher_confirmed: false,
    source_pair_confirmed: false, answer_conflict_resolutions: {} }
}
export function conflictIds(d: Draft) {
  return d.answer_key_crosscheck?.status === 'conflict' ? (d.source.source_conflicts || []).filter(c => c.type === 'answer_key_conflict').map(c => c.question_id || '') : []
}
const sourceConflictSectionLabels: Record<string,string> = { objective_questions: '客观题', subjective_subquestions: '主观题' }
const sourceConflictTypeLabels: Record<string,string> = { question_text_mismatch: '题干文本不一致', question_id_mismatch: '题号不一致', answer_key_conflict: '答案冲突' }
const conflictListText = (values: unknown) => (Array.isArray(values) ? values.map(value => String(value ?? '').trim()).filter(Boolean).join('、') : '')
function sourceConflictText(c: SourceConflict) {
  if (c.type === 'question_text_mismatch') {
    const { compared, matched } = c
    if (typeof compared === 'number' && Number.isFinite(compared) && typeof matched === 'number' && Number.isFinite(matched)) return `题干文本抽样：已比较 ${compared} 题，一致 ${matched} 题`
    return sourceConflictTypeLabels[c.type]
  }
  if (c.type === 'question_id_mismatch') {
    const section = sourceConflictSectionLabels[c.section || ''] || (c.section || '题目')
    const evidence: string[] = []
    const docxOnly = conflictListText(c.docx_only), pdfOnly = conflictListText(c.pdf_only)
    if (docxOnly) evidence.push(`Word 独有：${docxOnly}`)
    if (pdfOnly) evidence.push(`PDF 独有：${pdfOnly}`)
    return `${section}题号不一致${evidence.length ? `：${evidence.join('；')}` : ''}`
  }
  if (c.type === 'answer_key_conflict') {
    const questionId = String(c.question_id ?? '').trim()
    const docxAnswer = String(c.docx_answer ?? '').trim() || '无记录'
    const pdfAnswer = String(c.pdf_answer ?? '').trim() || '无记录'
    return `${questionId ? `第 ${questionId} 题` : ''}答案冲突：Word ${docxAnswer}；PDF ${pdfAnswer}`
  }
  return String(c.label || c.type || c.section || '来源冲突').trim() || '来源冲突'
}
export function conflictEvidence(d: Draft): string[] {
  const conflicts = d.source?.source_conflicts
  return Array.isArray(conflicts) ? conflicts.map(sourceConflictText) : []
}
export function references(d: Draft, id: string) {
  if (!/^[a-f0-9]{32}$/.test(id)) return []
  const declaredRoles = new Map((d.layout_reference?.page_roles || []).map(r => [r.page_number, r.page_role]))
  return (d.layout_reference?.pages || []).filter(p => {
    if (!Number.isInteger(p.page_number) || p.page_number <= 0) return false
    // Layout references include exam pages and the blank answer sheet (where
    // teachers draw answer regions), but never scoring-reference/answer pages.
    const role = p.page_role || declaredRoles.get(p.page_number) || 'unknown'
    return role === 'exam' || role === 'answer_sheet'
  }).map(p => ({ page: p.page_number, url: `/api/exam-import-reference?draft_id=${id}&page_number=${p.page_number}` }))
}
const positive = (v: unknown) => typeof v !== 'boolean' && v !== null && String(v).trim() !== '' && Number.isFinite(Number(v)) && Number(v) > 0
export function blockers(d: Draft, c: Confirmation): string[] {
  const errors: string[] = []
  if (d.source.source_pair_status === 'mismatch') errors.push('Word与PDF不一致，请移除其中一个文件重新解析。')
  if (d.source.source_pair_status === 'needs_teacher_confirmation' && !c.source_pair_confirmed) errors.push('请确认Word与PDF属于同一份试卷。')
  if (!c.exam_name.trim()) errors.push('请填写考试名称。')
  if (!(d.objective_questions || []).length && !c.material_questions.some(m => m.sub_questions.length)) errors.push('没有可创建的题目。')
  for (const q of d.objective_questions || []) {
    if (q.needs_teacher_review || !q.stem?.trim() || Object.keys(q.options || {}).sort().join('') !== 'ABCD') errors.push(`客观题${q.question_id}结构不完整，请整理原文件后重新解析。`)
    if (!positive(c.objective_points[q.question_id]) || Number(c.objective_points[q.question_id])>100) errors.push(`请确认客观题${q.question_id}分值（正数且不超过100）。`)
    if (!/^[ABCD]$/.test(c.objective_answer_key[q.question_id] || '')) errors.push(`请核对客观题${q.question_id}答案。`)
  }
  const ids=d.objective_questions.map(q=>q.question_id)
  if(ids.length>100||new Set(ids).size!==ids.length||ids.some(id=>! /^[1-9][0-9]*$/.test(id))||[...ids].sort((a,b)=>Number(a)-Number(b)).some((id,i)=>Number(id)!==i+1))errors.push('客观题须从1连续编号；不自动重排题号。')
  for (const id of conflictIds(d)) if (!c.answer_conflict_resolutions[id] || c.answer_conflict_resolutions[id] !== c.objective_answer_key[id]) errors.push(`请明确裁决客观题${id}的答案冲突。`)
  const subIds=c.material_questions.flatMap(m=>m.sub_questions.map(q=>q.question_id))
  if(new Set(c.material_questions.map(m=>m.label)).size!==c.material_questions.length)errors.push('大题编号重复，请修正。')
  if(new Set(subIds).size!==subIds.length)errors.push('小题编号重复，请修正。')
  for(const m of c.material_questions)if(!/^[1-9]\d{0,2}$/.test(m.label)||!m.sub_questions.length||m.sub_questions.some(q=>!new RegExp('^'+m.label+'\\.[1-9]\\d{0,2}$').test(q.question_id)))errors.push('大题、小题编号及归属需要修正。')
  for (const m of c.material_questions) for (const q of m.sub_questions) {
    if (!q.question.trim() || (q.scoring_mode!=='holistic'&&!q.reference_answer.trim())) errors.push(`${q.question_id}的设问和参考答案不能为空。`)
    if (!positive(q.max_score)) errors.push(`${q.question_id}需要有效满分。`)
    if (q.scoring_mode==='holistic') {
      if(q.holistic?.aggregation==='sum_projects'){const ps=q.holistic.projects||[];if(!ps.length||ps.some(p=>!p.label.trim()||!positive(p.max_score)||!positive(p.score_step)||!p.levels.length||p.levels.some(l=>!l.description.trim()))||Math.abs(ps.reduce((n,p)=>n+Number(p.max_score),0)-Number(q.max_score))>1e-6)errors.push(`${q.question_id}请核对各项目标准和满分合计。`);continue}
      if(!q.holistic?.dimensions.trim()&&!q.holistic?.dimension_definitions?.length)errors.push(`${q.question_id}请填写评价维度。`)
      if(!positive(q.holistic?.score_step))errors.push(`${q.question_id}请确认评分步长。`)
      if(!q.holistic?.levels.length||q.holistic.levels.some(l=>l.min_score===null||l.max_score===null||!l.description.trim()||!Number.isFinite(Number(l.min_score))||!Number.isFinite(Number(l.max_score))||String(l.min_score).trim()===''||String(l.max_score).trim()===''||Number(l.min_score)<0||Number(l.min_score)>Number(l.max_score)||Number(l.max_score)>Number(q.max_score)))errors.push(`${q.question_id}需要完整分档范围及评价标准。`)
      if(q.holistic?.levels.length){const levels=[...q.holistic.levels].sort((a,b)=>Number(a.min_score)-Number(b.min_score));if(Number(levels[0].min_score)!==0||Number(levels[levels.length-1].max_score)!==Number(q.max_score)||levels.some((l,i)=>i>0&&(Number(l.min_score)<=Number(levels[i-1].max_score)||Number(l.min_score)-Number(levels[i-1].max_score)>Number(q.holistic?.score_step||1))))errors.push(`${q.question_id}分档需覆盖0至满分，不能重叠或缺档。`)}
      continue
    }
    if (!q.rubric_draft.length || q.rubric_draft.some(p => !p.label.trim() || !positive(p.score))) errors.push(`${q.question_id}需要完整评分点及正分值。`)
    if (q.scoring_mode && !['weighted','point_bank'].includes(q.scoring_mode)) errors.push(`${q.question_id}评分方式无效。`)
    if (q.scoring_mode==='point_bank') {
      if (!positive(q.point_value) || q.rubric_draft.some(p=>Number(p.score)!==Number(q.point_value))) errors.push(`${q.question_id}封顶模式各点分值须与每点分值一致；请逐点核对，不自动改分。`)
      if (q.rubric_draft.reduce((n,p)=>n+Number(p.score),0)<Number(q.max_score)-1e-6) errors.push(`${q.question_id}候选评分点不足以达到满分。`)
    }
  }
  if (!c.teacher_confirmed) errors.push('请完成教师核对确认。')
  return errors
}
export function sumWarning(q: Sub) {
  if(q.scoring_mode==='holistic')return '按教师确认的档次和步长整体评分；评价维度不累计计分。'
  const sum = q.rubric_draft.reduce((a,p) => a + Number(p.score), 0)
  if(q.scoring_mode==='point_bank'&&sum>=Number(q.max_score)-1e-6)return ''
  return positive(q.max_score) && q.rubric_draft.every(p => positive(p.score)) && Math.abs(sum - Number(q.max_score)) > 1e-6 ? `评分点合计${sum}，本题满分${q.max_score}；创建不代表通过评分就绪检查，请核实。` : ''
}
export async function filePayload(file: File, kind: 'docx' | 'pdf'): Promise<Record<string,string>> {
  if (!file.name.toLowerCase().endsWith('.' + kind) || file.size <= 0 || file.size > (kind === 'docx' ? 10 : 26) * 1024 * 1024) throw new Error(kind === 'docx' ? '请选择非空且不超过10MB的.docx文件。' : '请选择非空且不超过26MB的.pdf文件。')
  const bytes = new Uint8Array(await file.arrayBuffer())
  let binary = ''
  for (let i=0; i<bytes.length; i+=8192) binary += String.fromCharCode(...bytes.subarray(i,i+8192))
  return { [kind + '_filename']: file.name, [kind + '_data_base64']: btoa(binary) }
}
export function parseExam(payload: Record<string,string>) { return request<{ draft_id: string; draft: Draft }>('/api/import-exam-draft','POST',payload) }
export function createExam(draft_id: string, confirmation: Confirmation) { return request<Created>('/api/create-imported-exam','POST',{draft_id,confirmation}) }
export type AiPreview = {draft_id:string;source_label:string;text_chars:number;provider:string;model:string;destination_host:string;preview_sha256:string;note?:string}
export type AiSummary = {objective:{ai_count:number;added:string[];answer_conflicts:unknown[]};subjective:{ai_count:number;added:string[];answer_conflicts:unknown[]};issues?:string[]}
export function previewAiExam(draft_id:string){return request<AiPreview>('/api/exam-import-ai-preview','POST',{draft_id})}
export function parseAiExam(draft_id:string,preview_sha256:string){return request<{draft:Draft;summary:AiSummary}>('/api/exam-import-ai-parse','POST',{draft_id,preview_sha256,confirm_send_text:true})}
export function preserveAiExamEdits(incoming:Draft,previous:Draft,current:Confirmation):Confirmation{
  const next=editDraft(incoming),baseline=editDraft(previous),value=(v:unknown)=>String(v??'')
  if(current.exam_name!==baseline.exam_name)next.exam_name=current.exam_name
  for(const {question_id:id} of incoming.objective_questions){
    if(Object.hasOwn(current.objective_answer_key,id)&&value(current.objective_answer_key[id])!==value(baseline.objective_answer_key[id]))next.objective_answer_key[id]=current.objective_answer_key[id]!
  }
  for(const major of next.material_questions){
    const old=baseline.material_questions.find(m=>m.label===major.label),edit=current.material_questions.find(m=>m.label===major.label)
    if(!old||!edit)continue
    if(edit.prompt!==old.prompt)major.prompt=edit.prompt
    for(const sub of major.sub_questions){
      const a=old.sub_questions.find(q=>q.question_id===sub.question_id),b=edit.sub_questions.find(q=>q.question_id===sub.question_id)
      if(!a||!b)continue
      for(const key of ['question','reference_answer','max_score','scoring_mode','point_value','question_type'] as const){
        if(value(a[key])!==value(b[key]))Object.assign(sub,{[key]:b[key]})
      }
      const points=(q:Sub)=>q.rubric_draft.map(p=>({label:p.label,score:value(p.score)}))
      if(JSON.stringify(points(a))!==JSON.stringify(points(b)) || JSON.stringify(a.holistic)!==JSON.stringify(b.holistic)
        || a.scoring_mode!==b.scoring_mode || value(a.point_value)!==value(b.point_value)){
        Object.assign(sub,{scoring_mode:b.scoring_mode,point_value:b.point_value,holistic:b.holistic,
          rubric_draft:JSON.parse(JSON.stringify(b.rubric_draft))})
      }
    }
  }
  for(const id of conflictIds(incoming))if(current.answer_conflict_resolutions[id]===next.objective_answer_key[id])next.answer_conflict_resolutions[id]=current.answer_conflict_resolutions[id]!
  next.objective_points=Object.fromEntries(incoming.objective_questions.map(q=>[q.question_id,current.objective_points[q.question_id]??next.objective_points[q.question_id]??null]))
  const structure=(ms:Major[])=>ms.map(m=>[m.label,m.sub_questions.map(q=>q.question_id)])
  if(JSON.stringify(structure(current.material_questions))!==JSON.stringify(structure(previous.material_questions)))next.material_questions=JSON.parse(JSON.stringify(current.material_questions))
  return next // Source and teacher confirmation must be explicitly renewed.
}

export function scoreTotals(c:Confirmation){
 const values=Object.values(c.objective_points),subs=c.material_questions.flatMap(m=>m.sub_questions).map(q=>q.max_score)
 const sum=(items:unknown[])=>items.every(positive)?items.reduce<number>((total,v)=>total+Number(v),0):null
 const objective=sum(values),subjective=sum(subs)
 return {objective,subjective,paper:objective===null||subjective===null?null:objective+subjective}
}

export function newSub(parent:string,subs:Sub[]):Sub {
 let n=1;while(subs.some(q=>q.question_id===`${parent}.${n}`))n++
 return {question_id:`${parent}.${n}`,question:'',reference_answer:'',max_score:null,rubric_draft:[],scoring_mode:'weighted',question_type:'subjective'}
}
export function moveSub(from:Major,to:Major,q:Sub){
 if(from===to)return
 const id=newSub(to.label,to.sub_questions).question_id
 if(from.prompt.trim())q.question=`原所属第${from.label}题材料：\n${from.prompt}\n\n${q.question}`
 from.sub_questions.splice(from.sub_questions.indexOf(q),1);q.question_id=id;to.sub_questions.push(q)
}
export function mergeNext(m:Major,index:number){
 const a=m.sub_questions[index],b=m.sub_questions[index+1];if(!b)return
 if(a.scoring_mode!==b.scoring_mode)throw new Error('请先手动统一两题的评分方式，再合并。')
 if(a.scoring_mode==='holistic')a.holistic={levels:[],dimensions:[a.holistic?.dimensions,b.holistic?.dimensions].filter(Boolean).join('；'),notes:'原两题分档标准（合并后请重新设置档次）：\n'+JSON.stringify([a.holistic,b.holistic])}
 a.question+='\n\n'+b.question;a.reference_answer+='\n\n'+b.reference_answer
 a.max_score=a.max_score!==null&&b.max_score!==null?Number(a.max_score)+Number(b.max_score):null
 a.rubric_draft.push(...b.rubric_draft);m.sub_questions.splice(index+1,1)
}
export function splitSub(m:Major,q:Sub){
 const parts=q.question.split('\n'),next=newSub(m.label,m.sub_questions)
 if(parts.length<2)throw new Error('请先在设问中换行，将要拆出的设问放在下一行。')
 next.scoring_mode=q.scoring_mode;next.point_value=q.point_value;next.question_type=q.question_type
 if(q.scoring_mode==='holistic')next.holistic={levels:[],dimensions:q.holistic?.dimensions||'',notes:'拆分后的档次标准需要重新配置。'}
 q.question=parts.shift()!;next.question=parts.join('\n');q.max_score=null
 m.sub_questions.splice(m.sub_questions.indexOf(q)+1,0,next)
 // All original reference text/points remain on the first part, never guessed.
}

export function reorder<T>(items:T[],index:number,delta:number){
 const target=index+delta;if(target<0||target>=items.length)return
 ;[items[index],items[target]]=[items[target],items[index]]
}
