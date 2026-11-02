import {ref, shallowRef} from 'vue'
import {request} from './api'
import {contextFromHash, examHref, type ExamContext, type View} from './navigation'
import type {FormalAssignment} from './formalUpload'

export function assertContextSyntax(hash:string){
  const p=new URLSearchParams(hash.split('?')[1]||'')
  for(const [key,pattern] of [['assignment',/^[a-f0-9]{12}$/],['exam',/^[a-f0-9]{32}$/],['assessment',/^[a-f0-9]{32}$/],['job',/^[a-f0-9]{32}$/],['batch',/^[a-f0-9]{12}$/]] as const){
    if(p.has(key)&&!pattern.test(p.get(key)||''))throw new Error('链接中的考试或答卷编号无效，请重新选择。')
  }
  if(p.has('mode')&&!['formal','quick'].includes(p.get('mode')||''))throw new Error('批改模式无效')
}

// One owner per authenticated workspace; no credentials or student data in storage.
export function createWorkspaceContext(){
  const current=ref<ExamContext>({assignmentId:'',examId:'',mode:'formal'})
  const assignments=shallowRef<FormalAssignment[]>([]),error=ref(''),busy=ref(false)
  let revision=0
  async function refresh(){assignments.value=(await request<{assignments:FormalAssignment[]}>('/api/assignments')).assignments}
  async function select(input:ExamContext){
    const n=++revision;busy.value=true;error.value=''
    try{
      const next={...input,mode:input.mode||current.value.mode||'formal'}
      if(next.assignmentId&&!/^[a-f0-9]{12}$/.test(next.assignmentId))throw new Error('考试编号无效')
      if(next.examId&&!/^[a-f0-9]{32}$/.test(next.examId))throw new Error('正式考试编号无效')
      if(next.mode==='quick'&&next.examId)throw new Error('快速匿名模式不能隐式绑定正式考试')
      if(next.mode!=='quick'&&!next.examId&&next.assignmentId){const a=assignments.value.find(a=>a.id===next.assignmentId);next.examId=a?.exam_id||a?.original_photo_exams?.[0]?.exam_id||a?.layout_exams?.[0]?.exam_id||''}
      if(next.examId){
        const exam=await request<{exam_id:string;assignment_id:string;class_name?:string}>('/api/exam-layout-profile?exam_id='+next.examId)
        if(exam.exam_id!==next.examId||(next.assignmentId&&exam.assignment_id!==next.assignmentId))throw new Error('考试与评分标准不匹配')
        next.assignmentId=exam.assignment_id
        if(exam.class_name!==undefined)next.className=exam.class_name
      }
      if(next.assignmentId&&!assignments.value.some(a=>a.id===next.assignmentId))throw new Error('考试或评分标准不存在，或无法访问')
      if(n!==revision)return false
      current.value=next;return true
    }catch(e){if(n===revision)error.value=(e as Error).message;return false}
    finally{if(n===revision)busy.value=false}
  }
  function href(view:View){return examHref(view,current.value)}
  function persist(view:View,preserveDetails=true){
    const old=new URLSearchParams(location.hash.split('?')[1]||'')
    let hash=href(view)
    // Detail selectors stay in the URL, never reinterpret a different assessment.
    for(const key of ['assessment','panel','submission',...(view==='objective'?['job']:[]),...(view==='batch'?['batch']:[])])if(preserveDetails&&(!old.has('exam')||old.get('exam')===current.value.examId)&&(!old.has('assignment')||old.get('assignment')===current.value.assignmentId)&&old.has(key))hash+=(hash.includes('?')?'&':'?')+new URLSearchParams({[key]:old.get(key)!})
    history.replaceState(null,'',hash)
  }
  async function restore(hash:string,view:View){
    const n=++revision;error.value=''
    try{
      assertContextSyntax(hash)
      const params=new URLSearchParams(hash.split('?')[1]||'')
      if(view==='batch'&&!params.has('batch'))throw new Error('缺少批次编号，请从最近批次重新打开')
      const explicit=['assignment','exam','mode'].some(k=>params.has(k))
      let next:ExamContext=explicit?{...contextFromHash(hash),mode:params.get('mode')==='quick'?'quick':'formal'}:{...current.value}
      if(view==='quick'&&params.has('exam'))throw new Error('快速匿名模式不能隐式绑定正式考试')
      if(view==='quick')next={assignmentId:params.get('mode')==='quick'?next.assignmentId:'',examId:'',mode:'quick'}
      if(params.has('assessment')){
        const a=await request<{source_exam_id?:string|null}>('/objective/api/assessments/'+params.get('assessment'))
        if(n!==revision)return false
        if(a.source_exam_id){
          if(view==='quick'||params.get('mode')==='quick')throw new Error('正式答案键不能隐式切换快速匿名模式')
          if(params.has('exam')&&next.examId!==a.source_exam_id)throw new Error('答案键快照与当前考试不一致')
          next={assignmentId:params.has('assignment')?next.assignmentId:'',examId:a.source_exam_id,mode:'formal'}
        }else{
          if(explicit&&next.examId)throw new Error('独立客观题答案键不属于当前正式考试')
          next={assignmentId:'',examId:'',mode:'quick'}
        }
      }
      return await select(next)
    }catch(e){if(n===revision){busy.value=false;error.value=(e as Error).message}return false}
  }
  function clear(){revision++;current.value={assignmentId:'',examId:'',mode:'formal'};error.value='';busy.value=false}
  return {current,assignments,error,busy,refresh,select,href,persist,restore,clear}
}
