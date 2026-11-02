export type Question={id:string;max_score:number|null;a:number|null;b:number|null;point_conflict:boolean;blank_candidate?:boolean;blank_confirmed?:boolean;blank_auto?:boolean;a_status?:string;b_status?:string}
export type Paper={id:string;exam_id:string;sequence?:number|null;label:string;state:string;progress_state?:string;reason:string;a:number|null;b:number|null;names:{A:string;B:string};max_score:number|null;questions:Question[];evidence_only?:boolean;model_issues?:{A:string;B:string};totals_complete?:{A:boolean;B:boolean};confirmation_method?:string;final_total?:number|null}
const finite=(v:unknown):v is number=>typeof v==='number'&&Number.isFinite(v)
export function visibleTotal(p:Paper,side:'A'|'B'){
 const key=side==='A'?'a':'b',value=p[key]
 if(p.model_issues?.[side]||(p.totals_complete?.[side]===false&&!p.questions.some(q=>finite(q[key]))))return null
 return finite(value)?value:null
}
export function answerNote(status?:string,confirmed=false){
 if(confirmed&&['unanswered_candidate','question_unmatched','image_unreadable','missing'].includes(status||''))return '原AI结果：'+({unanswered_candidate:'疑似空白',question_unmatched:'题号未匹配',image_unreadable:'图像读取异常',missing:'无小题评分'} as Record<string,string>)[status!]
 return ({unanswered_candidate:'疑似空白，待教师确认',question_unmatched:'题号未匹配，待核对',image_unreadable:'图像无法可靠读取',missing:'未获得小题评分',blank_confirmed:'已确认空白'} as Record<string,string>)[status||'']||''
}
export function difference(a:number|null,b:number|null,conflict=false){return !finite(a)||!finite(b)?'结果不完整':a===b?(conflict?'同分，评分点判断不同':'一致'):`相差 ${Math.abs(a-b)} 分`}
export function questionDifference(q:Question,confirmed=false){
 if(confirmed&&(!finite(q.a)||!finite(q.b)||[q.a_status,q.b_status].some(s=>['unanswered_candidate','question_unmatched','image_unreadable','missing'].includes(s||''))))return '原AI结果不完整'
 if(q.blank_auto)return '双模型明确空白，自动0分'
 if(q.blank_confirmed)return '空白已核对；保留原AI判断'
 if([q.a_status,q.b_status].includes('unanswered_candidate'))return '空白判断待核对'
 if([q.a_status,q.b_status].includes('question_unmatched'))return '题号待核对'
 if([q.a_status,q.b_status].includes('image_unreadable'))return '图像待核对'
 return difference(q.a,q.b,q.point_conflict)
}
export function statusLabel(p:Paper){return ({queued:'排队中',grading:'正在评分',paused:'批次暂停等待',recovery:'待人工恢复',failed:'处理失败',adoptable:'可批量采纳',review:'待复核',processing:'处理中',stopped:'已停止（未开始）',confirmed:'已确认'} as Record<string,string>)[p.progress_state||p.state]||p.state}
export function scoreSummary(p:Paper){
 if(['queued','grading','paused','recovery','failed','stopped'].includes(p.progress_state||p.state))return p.reason
 if(p.confirmation_method==='automatic_blank_zero')return '双模型明确整卷空白，自动确认0分'
 if(p.state==='confirmed')return '成绩已确认；原AI结果与确认成绩分别保留'
 const issues=(['A','B'] as const).filter(s=>p.model_issues?.[s]).map(s=>`${p.names[s]}：${p.model_issues![s]}`)
 if(issues.length)return issues.join('；')
 const incomplete=(['A','B'] as const).filter(s=>p.totals_complete?.[s]===false||!finite(p[s==='A'?'a':'b'])||p.questions.some(q=>!finite(q[s==='A'?'a':'b'])||['unanswered_candidate','question_unmatched','image_unreadable','missing'].includes(q[s==='A'?'a_status':'b_status']||'')))
 if(incomplete.length)return `${incomplete.map(s=>p.names[s]).join('、')}存在待确认或缺失的小题结果，不能按完整双评比较总分`
 const summary=difference(p.a,p.b,p.questions.some(q=>q.point_conflict))
 return summary==='一致'&&p.state==='review'?'分数一致，仍有待核对项':summary
}
export function needsRecovery(p:Paper){return ['paused','recovery','failed','stopped'].includes(p.progress_state||p.state)}
export function shortQuestion(id:string){return id.replace(/^Q(?=\d)/,'')}
export function compactReason(p:Paper){
 if(p.state==='confirmed'||p.state==='adoptable')return ''
 if(needsRecovery(p))return p.progress_state==='paused'?'前序任务待处理':'请到本批恢复处理'
 if(p.state==='processing')return ''
 const issues=(['A','B'] as const).filter(s=>p.model_issues?.[s]).map(s=>p.names[s]+(p.model_issues![s].includes('内容过滤')?'内容过滤':'调用异常'))
 if(issues.length)return issues.join('、')
 const problems=p.questions.flatMap(q=>{
  const id=shortQuestion(q.id),states=[q.a_status,q.b_status]
  if(q.blank_auto)return []
  if(q.blank_confirmed)return [id+'空白已核对，待终评']
  if(states.includes('unanswered_candidate'))return [id+'疑似空白']
  if(states.includes('question_unmatched'))return [id+'题号待核对']
  if(states.includes('image_unreadable'))return [id+'图像待核对']
  if(!finite(q.a)||!finite(q.b))return [id+'结果缺失']
  if(q.a!==q.b)return [id+`相差${Math.abs(q.a-q.b)}分`]
  if(q.point_conflict)return [id+'评分点分歧']
  return []
 })
 return problems.length>1?`${problems.length}处待核对`:problems[0]||'证据或评分状态待核对'
}
export function compactQuestionScore(q:Question,side:'a'|'b'){
 if(finite(q[side]))return String(q[side])
 if(q.blank_confirmed&&q[side==='a'?'a_status':'b_status']==='unanswered_candidate')return '原疑似空白'
 return ({unanswered_candidate:'待确认',question_unmatched:'未匹配',image_unreadable:'看不清'} as Record<string,string>)[q[side==='a'?'a_status':'b_status']||'']||'—'
}

export function reviewGroup(p:Paper){
 if(needsRecovery(p))return 'recovery'
 if(p.state!=='review')return p.state
 if(p.questions.some(q=>!q.blank_confirmed&&(q.blank_candidate||[q.a_status,q.b_status].includes('unanswered_candidate'))))return 'blank'
 if(p.questions.some(q=>!q.blank_confirmed&&finite(q.a)&&finite(q.b)&&(q.a!==q.b||q.point_conflict)))return 'disagreement'
 if(p.evidence_only)return 'evidence'
 return 'other'
}
