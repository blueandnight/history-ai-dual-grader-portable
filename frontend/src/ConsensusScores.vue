<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import BlankReviewPanel from './BlankReviewPanel.vue'
import {request} from './api'
import {reviewGroup,answerNote,questionDifference,statusLabel,scoreSummary,needsRecovery,visibleTotal,compactReason,compactQuestionScore,shortQuestion,type Paper} from './consensusDisplay'
import {examHref} from './navigation'
const props=defineProps<{assignmentId:string;examId?:string;batchId?:string;autoRefresh?:boolean;refreshKey?:number;recoveryInline?:boolean}>()
const emit=defineEmits<{changed:[];recover:[];review:[id:string,ids:string[]];detail:[id:string]}>()
const papers=ref<Paper[]>([]),selected=ref<string[]>([]),token=ref(''),busy=ref(false),error=ref(''),confirmed=ref(false),message=ref('')
const filter=ref('all'),blankOpen=ref(false),blankDirty=ref(false)
const groups=[['all','全部'],['blank','空白待核对'],['disagreement','分数 / 评分点分歧'],['evidence','仅证据差异'],['other','其他待复核'],['recovery','任务恢复']]
const visible=computed(()=>papers.value.filter(p=>filter.value==='all'||reviewGroup(p)===filter.value))
const blankIds=computed(()=>papers.value.filter(p=>p.state==='review'&&!needsRecovery(p)&&p.questions.some(q=>q.blank_candidate)).map(p=>p.id))
function openBlanks(){pauseLive();blankDirty.value=false;blankOpen.value=true}
function blankChanged(){blankDirty.value=true}
async function closeBlanks(){blankOpen.value=false;if(blankDirty.value){emit('changed');return}await load()}
const interacting=ref(false),refreshing=ref(false)
const adoptable=computed(()=>papers.value.filter(p=>p.state==='adoptable')),pending=computed(()=>visible.value.filter(p=>p.state==='review'))
let revision=0,pendingRefresh=false,timer:ReturnType<typeof setTimeout>|undefined
function stopTimer(){if(timer)clearTimeout(timer);timer=undefined}
function pauseLive(reset=true){interacting.value=true;stopTimer();revision++;refreshing.value=false;if(reset)confirmed.value=false}
function schedule(delay=3000){stopTimer();if(props.autoRefresh&&!interacting.value)timer=setTimeout(()=>void load(true),delay)}
function points(value:number|null|undefined,max:number|null){return typeof value!=='number'||!Number.isFinite(value)?'—':max?`${value}/${max}`:String(value)}
function recover(event:MouseEvent){if(props.recoveryInline){event.preventDefault();emit('recover')}}
function review(p:Paper){emit('review',p.id,p.state==='review'?pending.value.map(r=>r.id):[p.id])}
async function load(passive=false){
 if(passive&&(interacting.value||busy.value||refreshing.value)){if(!interacting.value)pendingRefresh=true;return}
 stopTimer();const n=++revision;pendingRefresh=false;error.value=''
 if(passive)refreshing.value=true;else{interacting.value=false;busy.value=true;selected.value=[];confirmed.value=false;token.value=''}
 try{const d=await request<{papers:Paper[];preview_token:string}>('/api/batch-adoption?assignment_id='+props.assignmentId+'&exam_id='+(props.examId||'')+'&batch_id='+(props.batchId||''));if(n!==revision)return;papers.value=d.papers;token.value=d.preview_token;selected.value=d.papers.filter(p=>p.state==='adoptable').map(p=>p.id)}catch{if(n===revision){token.value='';confirmed.value=false;error.value='双评成绩读取失败，请刷新；已显示的成绩保留，未确认的结果不会自动重试。'}}finally{if(n===revision){busy.value=false;refreshing.value=false;if(!error.value&&(pendingRefresh||papers.value.some(p=>p.state==='processing')))schedule(pendingRefresh?0:3000)}}
}
async function adopt(){if(busy.value||!confirmed.value||!selected.value.length)return;stopTimer();const n=++revision;refreshing.value=false;busy.value=true;error.value='';try{const d=await request<{adopted:number;reused:number}>('/api/batch-adoption','POST',{assignment_id:props.assignmentId,exam_id:props.examId||'',batch_id:props.batchId||'',preview_token:token.value,items:papers.value.filter(p=>selected.value.includes(p.id)).map(p=>({id:p.id,exam_id:p.exam_id})),confirmed:true});if(n!==revision)return;await load();message.value=`已批量采纳 ${d.adopted+d.reused} 份 AI 共识成绩，未记录为教师逐点评阅。`;emit('changed')}catch{if(n===revision){confirmed.value=false;token.value='';interacting.value=true;error.value='采纳未能确认或数据已变化。请先刷新核实，不会自动重试。'}}finally{if(n===revision)busy.value=false}}
watch(()=>[props.assignmentId,props.examId,props.batchId],()=>{message.value='';papers.value=[];void load()},{immediate:true})
watch(()=>props.refreshKey,()=>{if(!interacting.value)void load(true)})
onUnmounted(()=>{revision++;stopTimer()})
</script>
<template><section aria-label="双评成绩与批量采纳">
<h3>双评成绩与复核</h3><p>双模型完整成功、逐点一致且无未解决异常时可批量采纳。两模型可能共同判错；批量采纳不代表教师逐点评阅。分歧、缺失和版本异常仍需复核。</p>
<div class="context-actions"><button :disabled="busy||blankOpen" @click="load()">刷新双评成绩</button><button class="primary" :disabled="busy||blankOpen||!pending.length" @click="review(pending[0]!)">处理待复核答卷（{{pending.length}}份）</button></div>
<div class="context-actions"><label>显示 <select v-model="filter"><option v-for="[value,label] in groups" :key="value" :value="value">{{label}}（{{value==='all'?papers.length:papers.filter(p=>reviewGroup(p)===value).length}}）</option></select></label><button :disabled="busy||!blankIds.length||blankOpen" @click="openBlanks">集中核对空白（{{blankIds.length}}份）</button></div>
<BlankReviewPanel v-if="blankOpen" :ids="blankIds" :assignment-id="assignmentId" :exam-id="examId" :batch-id="batchId" @changed="blankChanged" @close="closeBlanks"/>
<p v-if="filter==='evidence'" class="muted">本组双评分数及评分点判断一致，但引用证据有差异；仍需核对，批量采纳条件保持不变。</p>
<p v-if="autoRefresh&&interacting" class="muted">正在核对本次采纳选择，已暂停表格自动刷新；保存后恢复，或点击刷新重新选择。</p>
<p v-if="busy" role="status">正在核实双评成绩…</p><p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p>
<fieldset v-if="adoptable.length" :disabled="busy||blankOpen"><legend>批量采纳（{{adoptable.length}}份符合条件）</legend>
<button @click="selected=adoptable.map(p=>p.id);pauseLive()">全选可采纳答卷</button> <button @click="selected=[];pauseLive()">清空选择</button>
<label class="check"><input type="checkbox" v-model="confirmed" @change="pauseLive(false)">我已核对所选答卷及共识状态，确认将 AI 共识采纳为本次成绩；知悉未逐点评阅及共同误判风险</label>
<button class="primary" :disabled="!confirmed||!selected.length||!token" @click="adopt">批量采纳所选 {{selected.length}} 份</button></fieldset>
<p v-if="papers.length" class="muted score-legend">小题按 A / B 顺序显示；* 为部分小题合计，不是完整成绩。“待确认”表示 AI 判断疑似空白。手机可横向滑动。</p>
<div v-if="papers.length" class="table-wrap history-table" tabindex="0" aria-label="双评成绩表格"><table><thead><tr><th>答卷</th><th>双评总分（A / B）</th><th>小题分数（A / B）</th><th>状态</th><th>操作</th></tr></thead><tbody>
<tr v-for="p in visible" :key="p.id"><td class="paper-label"><span v-if="p.sequence">第{{p.sequence}}份 · </span><label v-if="p.state==='adoptable'"><input type="checkbox" v-model="selected" :value="p.id" :disabled="busy" :aria-label="'采纳 '+p.label" @change="pauseLive()"><span :title="p.label">{{p.label.length>12?p.label.slice(0,8)+'…':p.label}}</span></label><span v-else :title="p.label">{{p.label.length>12?p.label.slice(0,8)+'…':p.label}}</span></td>
<td class="totals-cell"><strong class="dual-totals"><span>{{p.names.A}} {{visibleTotal(p,'A')??'—'}}{{p.totals_complete?.A===false&&visibleTotal(p,'A')!=null?'*':''}}</span><span> · {{p.names.B}} {{visibleTotal(p,'B')??'—'}}{{p.totals_complete?.B===false&&visibleTotal(p,'B')!=null?'*':''}}</span><span v-if="p.max_score!=null"> / {{p.max_score}}</span></strong><small v-if="p.final_total!=null" class="saved-score">{{p.state==='confirmed'?'确认成绩':'历史确认'}} {{points(p.final_total,p.max_score)}}{{p.confirmation_method==='automatic_blank_zero'?' · 明确空白自动0分':p.confirmation_method==='teacher_batch_adoption'?' · 共识采纳':''}}</small></td>
<td><div v-if="p.questions.length" class="question-scores" role="group" aria-label="小题双评对照"><div v-for="q in p.questions" :key="q.id" class="question-score" :class="{'score-conflict':q.point_conflict||q.a!==q.b||q.a==null||q.b==null}" :title="answerNote(q.a_status,p.state==='confirmed')+'；'+answerNote(q.b_status,p.state==='confirmed')+'；'+questionDifference(q,p.state==='confirmed')"><span>{{shortQuestion(q.id)}}</span><span>{{compactQuestionScore(q,'a')}} · {{compactQuestionScore(q,'b')}} <small v-if="q.max_score!=null">/ {{q.max_score}}</small></span></div></div><span v-else class="muted">尚无小题结果</span></td>
<td><span class="status-tag" :class="p.state==='adoptable'?'success':p.state==='review'||needsRecovery(p)?'warning':'neutral'">{{statusLabel(p)}}</span><p v-if="compactReason(p)" class="brief-reason">{{compactReason(p)}}</p><details v-if="p.state==='review'||needsRecovery(p)" class="score-reasons"><summary>原因</summary><p>{{scoreSummary(p)}}</p><p v-if="scoreSummary(p)!==p.reason">{{p.reason}}</p><p v-for="q in p.questions.filter(q=>q.a_status!=='answered'||q.b_status!=='answered')" :key="q.id">{{shortQuestion(q.id)}}：A {{answerNote(q.a_status,p.state==='confirmed')||'已有结果'}}；B {{answerNote(q.b_status,p.state==='confirmed')||'已有结果'}}</p></details></td>
<td class="score-actions"><a v-if="needsRecovery(p)" @click="recover" class="button secondary" :href="batchId?examHref('batch',{assignmentId,examId:p.exam_id})+'&batch='+batchId:examHref('batches',{assignmentId,examId:p.exam_id})">任务恢复</a><button :disabled="blankOpen||p.state==='processing'||p.state==='stopped'" :data-review-id="p.id" @click="review(p)">{{p.state==='review'?'处理分歧 / 教师终评':'确认最终总分'}}</button><button :disabled="blankOpen" @click="emit('detail',p.id)">查看详情</button></td></tr>
</tbody></table></div><p v-if="papers.length&&!visible.length">该分组没有答卷，可切换查看全部。</p><p v-else-if="!papers.length&&!busy&&!error">尚无答卷，请先上传照片。</p>
</section></template>
<style scoped>
.history-table>table{min-width:930px;width:100%;table-layout:auto}.history-table td{vertical-align:top;padding:12px 14px}.paper-label{min-width:130px;white-space:nowrap}.totals-cell{width:250px;min-width:210px}.dual-totals{font-size:15px;color:#24446b;line-height:1.65}.dual-totals span{white-space:nowrap}.saved-score{display:block;color:#64748b;margin-top:5px;font-size:12px}.question-scores{min-width:200px}.question-score{display:flex;gap:18px;line-height:1.8;white-space:nowrap;font-size:14px}.question-score>span:first-child{min-width:42px;color:#475569}.question-score small{color:#64748b}.score-conflict{color:#9a3412;font-weight:600}.brief-reason{font-size:13px;margin:5px 0}.score-reasons{font-size:12px;max-width:260px}.score-reasons summary{color:#64748b;cursor:pointer}.score-reasons p{white-space:normal;margin:5px 0}.score-actions{min-width:145px}.score-actions button,.score-actions a{white-space:nowrap;margin:0 4px 5px 0;padding:6px 10px;min-height:34px;font-size:13px}.score-legend{font-size:12px;margin:10px 0}
</style>
