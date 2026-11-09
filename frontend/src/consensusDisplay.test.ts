import {describe,it,expect} from 'vitest'
import {reviewGroup,answerNote,questionDifference,scoreSummary,statusLabel,needsRecovery,visibleTotal,compactReason,compactQuestionScore,type Paper,type Question} from './consensusDisplay'
const q:Question={id:'Q17.1',max_score:4,a:0,b:0,point_conflict:false,a_status:'answered',b_status:'answered'}
const paper:Paper={id:'synthetic',exam_id:'',label:'合成答卷',state:'review',reason:'需复核',a:0,b:0,names:{A:'DeepSeek',B:'MiMo'},max_score:4,questions:[q],totals_complete:{A:true,B:true}}
describe('真实状态与完整性展示',()=>{
 it('完整合法零分保持一致，待复核仍标注异常',()=>{expect(scoreSummary({...paper,state:'adoptable'})).toBe('一致');expect(scoreSummary(paper)).toBe('分数一致，仍有待核对项');expect(questionDifference(q)).toBe('一致')})
 it('疑似空白不是零分或同分分歧',()=>{
  const blank={...q,a:null,a_status:'unanswered_candidate',point_conflict:true}
  expect(questionDifference(blank)).toBe('空白判断待核对')
  const summary=scoreSummary({...paper,questions:[blank],totals_complete:{A:false,B:true}})
  expect(summary).toContain('DeepSeek');expect(summary).not.toContain('同分');expect(summary).not.toContain('一致')
  expect(answerNote(blank.a_status)).toContain('待教师确认')
 })
 it('区分未匹配与图像问题',()=>{
  expect(questionDifference({...q,b:null,b_status:'question_unmatched'})).toBe('题号待核对')
  expect(questionDifference({...q,b:null,b_status:'image_unreadable'})).toBe('图像待核对')
  expect(answerNote('missing')).toBe('未获得小题评分')
  expect(compactQuestionScore({...q,b:null,b_status:'scoring_invalid'},'b')).toBe('评分校验未通过')
  expect(answerNote('scoring_invalid')).toBe('评分校验未通过，待核对')
 })
 it('内容过滤优先显示，不从HTTP200推断评分成功',()=>{
  expect(scoreSummary({...paper,model_issues:{A:'',B:'服务商内容过滤，未获得完整评分；请核对，勿反复重试'}})).toContain('MiMo：服务商内容过滤')
 })
 it('缺失不与零分混淆',()=>{expect(scoreSummary({...paper,a:null})).toContain('缺失');expect(questionDifference({...q,a:null})).toBe('结果不完整')})
 it('全部疑似空白不显示累计占位零分，合法零分和部分分保留',()=>{
  expect(visibleTotal(paper,'A')).toBe(0)
  expect(visibleTotal({...paper,questions:[{...q,a:null,a_status:'unanswered_candidate'}],totals_complete:{A:false,B:true}},'A')).toBeNull()
  expect(visibleTotal({...paper,questions:[q,{...q,id:'Q17.2',a:null}],totals_complete:{A:false,B:true}},'A')).toBe(0)
 })
 it('正式确认后不把历史AI空白判断重新标为待确认',()=>{
  const blank={...q,a:null,a_status:'unanswered_candidate'}
  expect(answerNote(blank.a_status,true)).toBe('原AI结果：疑似空白')
  expect(questionDifference(blank,true)).toBe('原AI结果不完整')
  expect(scoreSummary({...paper,state:'confirmed',questions:[blank],final_total:0})).toContain('成绩已确认')
 })
 it('紧凑布局保留零分、疑似空白及多处异常',()=>{
  expect(compactQuestionScore(q,'a')).toBe('0')
  const blank={...q,b:null,b_status:'unanswered_candidate'}
  expect(compactQuestionScore(blank,'b')).toBe('待确认')
  expect(compactReason({...paper,questions:[blank]})).toBe('17.1疑似空白')
  expect(compactReason({...paper,questions:[blank,{...q,id:'Q17.2',a:2}]})).toBe('2处待核对')
 })
 it('短摘要保留具体模型问题，已确认记录不重复提示',()=>{
  expect(compactReason({...paper,model_issues:{A:'',B:'服务商内容过滤'}})).toBe('MiMo内容过滤')
  expect(compactReason({...paper,state:'confirmed'})).toBe('')
  expect(compactReason({...paper,questions:[{...q,point_conflict:true}]})).toBe('17.1评分点分歧')
 })
 it('真实完整分歧仍展示',()=>{
  expect(scoreSummary({...paper,a:2,questions:[{...q,a:2}]})).toBe('相差 2 分')
  expect(scoreSummary({...paper,questions:[{...q,point_conflict:true}]})).toBe('同分，评分点判断不同')
 })
 it.each([['queued','排队中'],['grading','正在评分'],['paused','批次暂停等待'],['recovery','待人工恢复'],['failed','处理失败'],['stopped','已停止（未开始）']])('区分%s', (state,label)=>{
  const p={...paper,progress_state:state,reason:'实际原因'}
  expect(statusLabel(p)).toBe(label);expect(scoreSummary(p)).toBe('实际原因')
  expect(needsRecovery(p)).toBe(['paused','recovery','failed','stopped'].includes(state))
 })
})

it('复核分组不把空白、缺失、已确认或历史差异当成共识',()=>{
 expect(reviewGroup({...paper,questions:[{...q,blank_candidate:true}]})).toBe('blank')
 expect(reviewGroup({...paper,questions:[{...q,a:2}]})).toBe('disagreement')
 expect(reviewGroup({...paper,evidence_only:true})).toBe('evidence')
 expect(reviewGroup(paper)).toBe('other')
 expect(reviewGroup({...paper,state:'confirmed',evidence_only:true})).toBe('confirmed')
 expect(reviewGroup({...paper,progress_state:'recovery',evidence_only:true})).toBe('recovery')
 const checked={...q,a:null,a_status:'unanswered_candidate',blank_confirmed:true}
 expect(reviewGroup({...paper,questions:[checked]})).toBe('other')
 expect(compactReason({...paper,questions:[checked]})).toContain('空白已核对')
})

it('明确空白自动计零不显示待终评',()=>{
 const blank={...q,blank_confirmed:true,blank_auto:true,a_status:'blank_confirmed',b_status:'blank_confirmed'}
 expect(questionDifference(blank)).toBe('双模型明确空白，自动0分')
 expect(compactReason({...paper,questions:[blank],state:'confirmed',confirmation_method:'automatic_blank_zero'})).toBe('')
 expect(scoreSummary({...paper,state:'confirmed',confirmation_method:'automatic_blank_zero',questions:[blank]})).toContain('自动确认0分')
})
