<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ApiError, checkSession, isOperator, login, logout, setSession, type Session } from './api'
import { routeFromHash, type ExamContext, type View } from './navigation'
import HistoryView from './HistoryView.vue'
import BatchesView from './BatchesView.vue'
import BatchView from './BatchView.vue'
import RostersView from './RostersView.vue'
import SystemView from './SystemView.vue'
import ExamCreateView from './ExamCreateView.vue'
import ClearGradingData from './ClearGradingData.vue'
import CopyExam from './CopyExam.vue'
import AnswerManagement from './AnswerManagement.vue'
import FormalUploadView from './FormalUploadView.vue'
import UsersView from './UsersView.vue'
import LayoutEditorView from './LayoutEditorView.vue'
import PrivacyWorkspaceView from './PrivacyWorkspaceView.vue'
import QuickUploadView from './QuickUploadView.vue'
import {allowLeaving,hasUnsavedTask,useTaskGuard} from './taskGuard'
import ObjectiveView from './ObjectiveView.vue'
import MobileAccess from './MobileAccess.vue'
import {createWorkspaceContext} from './workspaceContext'
import {previewDeletion,deleteExam,type DeletePreview} from './examMaintenance'

const session = ref<Session | null>(null), checking = ref(true), busy = ref(false)
const error = ref(''), username = ref(''), password = ref('')
const answerManageOpen=ref(false),lastUploadView=ref<View>('upload')
const uploadTarget=computed(()=>lastUploadView.value==='objective'?'objective':context.value.mode==='quick'?'quick':'upload')
const view = ref<View>('home'), routeKey=ref(''),routeBatchId=ref('')
const workspace=createWorkspaceContext()
const context=workspace.current
const href=workspace.href
const routeBusy=ref(false),picker=ref(''),changingContext=ref(false),taskBusy=ref(false)
useTaskGuard(taskBusy)
const catalogue=workspace.assignments,contextError=workspace.error
const search=ref(''),mobileOpen=ref(false)
const copyId=ref(''),copyRoot=ref<HTMLElement>()
const copyTarget=computed(()=>catalogue.value.find(a=>a.id===copyId.value))
async function openCopy(id:string){if(!allowLeaving())return;copyId.value=id;clearDataId.value='';deleteId.value='';await nextTick();copyRoot.value?.scrollIntoView({block:'center'})}
async function copiedExam(value:{assignmentId:string;examId:string;message:string}){copyId.value='';search.value='';deleteMessage.value=value.message;await createdExam(value)}
const clearDataId=ref(''),dataRefreshPending=ref(false),clearRoot=ref<HTMLElement>()
async function openClear(id:string){if(!allowLeaving())return;copyId.value='';clearDataId.value=id;deleteId.value='';await nextTick();clearRoot.value?.scrollIntoView({block:'center'})}
watch(hasUnsavedTask,(busy)=>{if(!busy&&dataRefreshPending.value){dataRefreshPending.value=false;routeKey.value=location.hash+':data:'+Date.now()}})
async function gradingDataChanged(){try{await workspace.refresh();if(hasUnsavedTask.value)dataRefreshPending.value=true;else routeKey.value=location.hash+':data:'+Date.now()}catch{deleteError.value='数据已变化，列表暂未刷新；已保存数据不受影响。'}}
function gradingDataStorage(e:StorageEvent){if(e.key==='grading-data-revision')void gradingDataChanged()}
const deleteId=ref(''),deleteName=ref(''),deleteBusy=ref(false),deleteError=ref(''),deleteMessage=ref(''),deletePreview=ref<DeletePreview>()
useTaskGuard(deleteBusy)
const deleteTarget=computed(()=>catalogue.value.find(a=>a.id===deleteId.value))
const deleteRoot=ref<HTMLElement>()
const visibleExams=computed(()=>catalogue.value.filter(a=>a.name.toLowerCase().includes(search.value.trim().toLowerCase())))
async function openDelete(id:string){if(!allowLeaving())return;copyId.value='';clearDataId.value='';deleteId.value=id;deleteName.value='';deleteError.value='';deletePreview.value=undefined;deleteBusy.value=true;try{const p=await previewDeletion(id);if(deleteId.value===id)deletePreview.value=p}catch{deleteError.value='无法读取删除范围，请刷新后重试'}finally{deleteBusy.value=false;await nextTick();deleteRoot.value?.scrollIntoView({block:'center'});deleteRoot.value?.querySelector('input')?.focus({preventScroll:true})}}
async function removeExam(){const target=deleteTarget.value,p=deletePreview.value;if(!target||!p||deleteBusy.value||p.blocked)return;deleteBusy.value=true;deleteError.value='';deleteMessage.value='';try{const r=await deleteExam(target.id,deleteName.value,p.fingerprint);deleteMessage.value=r.warning||'考试及其主客观题答卷、成绩和关联记录已删除';deleteId.value='';deletePreview.value=undefined;if(context.value.assignmentId===target.id)clearContext();await workspace.refresh()}catch{deletePreview.value=undefined;deleteError.value='删除结果未能确认或考试内容已变化。请刷新页面核对，不会自动重试。'}finally{deleteBusy.value=false}}
async function createdExam(value:ExamContext){await workspace.refresh();await adopt({...value,mode:'formal'});picker.value=value.assignmentId}
async function adopt(value:ExamContext){if(routeBusy.value)return;if(await workspace.select(value))workspace.persist(view.value)}
async function chooseCurrent(){if(!allowLeaving()){picker.value=context.value.assignmentId;return}changingContext.value=true;try{if(await workspace.select({assignmentId:picker.value,examId:'',mode:context.value.mode})){workspace.persist(view.value,false);routeKey.value=location.hash+':'+Date.now()}}finally{changingContext.value=false}}
async function chooseAssignment(id:string){if(!allowLeaving())return;if(await workspace.select({assignmentId:id,examId:'',mode:'formal'})){picker.value=id;location.hash=href('upload')}}
function clearContext(){workspace.clear();picker.value='';history.replaceState(null,'',href(view.value));routeKey.value=location.hash+':'+Date.now()}
const operator = computed(() => isOperator(session.value))
let navigationRevision=0
async function navigate() {
  if(!allowLeaving()){history.replaceState(null,'',href(view.value));return}
  mobileOpen.value=false;answerManageOpen.value=false
  const n=++navigationRevision;routeBusy.value=true
  view.value=routeFromHash(location.hash,session.value);if(['objective','upload','quick'].includes(view.value))lastUploadView.value=view.value;routeBatchId.value=new URLSearchParams(location.hash.split('?')[1]||'').get('batch')||''
  try{if(await workspace.restore(location.hash,view.value)&&n===navigationRevision){workspace.persist(view.value);picker.value=context.value.assignmentId}}finally{if(n===navigationRevision){routeKey.value=location.hash;routeBusy.value=false}}
}
function expired() { workspace.clear(); session.value = null; setSession(null); password.value = ''; error.value = '登录已失效，请重新登录' }
async function signIn() {
  busy.value = true; error.value = ''
  try { session.value = await login(username.value.trim(), password.value);await workspace.refresh();await navigate() }
  catch (e) { error.value = e instanceof Error ? e.message : '登录失败' }
  finally { password.value = ''; busy.value = false }
}
async function signOut() {
  if(!allowLeaving())return
  busy.value = true; error.value = ''
  try { await logout(); workspace.clear();session.value = null; location.hash = '#/home' }
  catch (e) { error.value = e instanceof Error ? e.message : '退出失败' }
  finally { busy.value = false }
}
function beforeUnload(e:BeforeUnloadEvent){if(hasUnsavedTask.value){e.preventDefault();e.returnValue=''}}
onMounted(async () => {
  window.addEventListener('beforeunload',beforeUnload)
  window.addEventListener('grading-data-changed',gradingDataChanged);window.addEventListener('storage',gradingDataStorage);
  window.addEventListener('hashchange', navigate); window.addEventListener('session-expired', expired)
  try { session.value = await checkSession();await workspace.refresh();await navigate() }
  catch (e) { if (!(e instanceof ApiError && e.status === 401)) error.value = '无法检查登录状态，请刷新重试' }
  finally { checking.value = false }
})
onUnmounted(() => {window.removeEventListener('grading-data-changed',gradingDataChanged);window.removeEventListener('storage',gradingDataStorage);window.removeEventListener('beforeunload',beforeUnload); window.removeEventListener('hashchange', navigate); window.removeEventListener('session-expired', expired) })
</script>

<template>
  <header class="app-header"><div><h1>历史学科 AI 双评阅卷系统</h1><span class="header-subtitle">教师阅卷工作台</span></div>    <div v-if="session" class="account"><span>{{ session.username || '本机工作台' }} · {{ session.required === false ? '本机' : session.role === 'admin' ? '管理员' : '教师' }}</span>
      <button v-if="session.required" @click="signOut" :disabled="busy">退出登录</button></div>
</header>
  <main v-if="checking" aria-live="polite">正在检查登录状态…</main>
  <main v-else-if="!session" class="login">
    <h2>登录工作台</h2>
    <p>使用现有账户。教师无需邮箱或登录验证码。</p>
    <form @submit.prevent="signIn">
      <label>用户名<input v-model="username" autocomplete="username" required maxlength="254"></label>
      <label>密码<input v-model="password" autocomplete="current-password" type="password" required></label>
      <button :disabled="busy">{{ busy ? '正在登录…' : '登录' }}</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <a href="/login">账户帮助与密码找回</a>
  </main>
  <main v-else>
    <nav class="workflow-nav" aria-label="工作台导航">
      <a :href="href('home')" :aria-current="['home','create'].includes(view)?'page':undefined">考试管理</a>
      <a :href="href(context.mode==='quick'?'quick':'upload')" :aria-current="['upload','quick','objective','batches','batch','layout','privacy'].includes(view)?'page':undefined">答卷批改</a>
      <a :href="href('history')" :aria-current="view==='history'?'page':undefined">终评与成绩</a>
      <a href="#/system" :aria-current="['system','rosters','users'].includes(view)?'page':undefined">系统管理</a>
    </nav>
    <section class="workspace-context" aria-label="考试上下文"><div class="context-picker">
      <label for="workspace-assignment">当前{{context.mode==='quick'?'评分标准':'考试'}}</label><select id="workspace-assignment" v-model="picker" @change="chooseCurrent" :disabled="workspace.busy.value||routeBusy||taskBusy"><option value="">{{context.mode==='quick'?'选择已有评分标准（不必创建正式考试）':'请选择考试'}}</option><option v-for="a in catalogue" :key="a.id" :value="a.id">{{a.name}}</option></select>
      </div><div class="context-meta"><span class="status-tag">{{context.mode==='quick'?'快速匿名':'正式考试'}}</span><span>{{context.className||'不关联花名册'}}</span><small v-if="context.examId">同一考试来源的全部答卷统一查看</small><small v-else>{{context.mode==='quick'?'无需预建正式考试':'上传时核对本次阅卷记录'}}</small></div>
    </section>
    <p v-if="routeBusy" role="status">正在核验考试上下文…</p>
    <p v-if="contextError" role="alert">{{contextError}} <button @click="clearContext">清除无效选择</button></p>
    <p v-if="error" role="alert">{{ error }}</p>
    <section v-if="view === 'home' && !contextError" class="exam-home"><div class="page-heading"><div><h2>考试管理</h2><p class="muted">准备题目与评分标准，继续上传、终评或查看成绩。</p></div><div class="context-actions"><a class="button" :href="href(context.mode==='quick'?'quick':'upload')">开始批改</a><a class="button secondary" href="#/quick">快速匿名批改（无需预先创建正式考试）</a><a class="button secondary" :href="href('create')">新建考试</a></div></div>
      <p v-if="deleteMessage" role="status">{{deleteMessage}}</p><p v-if="deleteError" role="alert">{{deleteError}}</p><label class="search-field">查找考试<input v-model="search" type="search" placeholder="输入考试名称" autocomplete="off"></label>
      <ul class="exam-list"><li v-for="a in visibleExams" :key="a.id" :data-assignment-id="a.id"><div><strong>{{a.name}}</strong><div class="exam-row-meta"><span class="status-tag" :class="a.submission_count?'success':'neutral'">{{typeof a.submission_count==='number'?('主观题 '+a.submission_count+' 份 · 客观题 '+(a.objective_submission_count??'待查询')+' 份'):'答卷数量待查询'}}</span><span>{{a.sub_questions?.length?'已保存'+a.sub_questions?.length+'个主观小题':'题型与评分标准请进入考试核对'}}</span><time v-if="a.created_at">{{a.created_at.slice(0,10)}}</time></div></div><div class="exam-row-actions"><button class="primary" @click="chooseAssignment(a.id)" :disabled="deleteBusy">使用本考试</button><button class="secondary" :disabled="deleteBusy||hasUnsavedTask" @click="openCopy(a.id)">复制为新考试</button><button class="secondary" :disabled="deleteBusy||hasUnsavedTask" @click="openClear(a.id)">清空阅卷数据</button><button class="danger" :disabled="deleteBusy||hasUnsavedTask" @click="openDelete(a.id)">删除考试</button></div></li></ul>
      <div v-if="copyTarget" ref="copyRoot"><CopyExam :key="copyTarget.id" :assignment="copyTarget" @close="copyId=''" @created="copiedExam"/></div>
      <div v-if="clearDataId" ref="clearRoot"><ClearGradingData :assignment-id="clearDataId" @close="clearDataId=''" @cleared="deleteMessage=$event;clearDataId=''"/></div>
      <section v-if="deleteTarget" ref="deleteRoot" class="delete-confirm" aria-label="删除考试确认"><h3>永久删除：{{deleteTarget.name}}</h3><p>将删除该考试题目来源及其全部正式阅卷记录、主客观题答卷、成绩和关联。共享花名册、公共 OMR 模板、系统配置、既有备份和审计日志保留。此操作无法撤销。</p><p v-if="deleteBusy">正在处理…</p><template v-if="deletePreview"><p>正式阅卷记录{{deletePreview.exams}}场；主观答卷{{deletePreview.subjective}}份；客观答卷{{deletePreview.objective}}份；原始照片{{deletePreview.photos}}张；关联记录{{deletePreview.links}}条。</p><p v-if="deletePreview.pending_subjective||deletePreview.unfinished_batches" class="muted">将一并取消{{deletePreview.pending_subjective||0}}份尚未执行的主观题任务，并移除{{deletePreview.unfinished_batches||0}}个未完成批次。</p><p v-if="deletePreview.blocked" role="alert">仍有正在运行的主观题任务，或客观题任务尚未结束，暂不能删除。请待任务停止后重新核对。</p><label>输入完整考试名称确认<input v-model="deleteName" :disabled="deleteBusy" autocomplete="off"></label><button class="danger" @click="removeExam" :disabled="deleteBusy||deletePreview.blocked||deleteName!==deleteTarget.name">永久删除考试</button></template> <button @click="deleteId='';deletePreview=undefined" :disabled="deleteBusy">取消</button></section><div v-if="!catalogue.length" class="empty-state"><h3>从第一场考试开始</h3><p>上传 Word 或 PDF，核对答案和分值后即可批改。临时阅卷也可使用快速匿名模式。</p><a :href="href('create')">导入第一场考试</a></div><p v-else-if="!visibleExams.length" class="empty-state">没有匹配的考试。<button class="secondary" @click="search=''">清除搜索</button></p>
      <p class="workflow-hint">创建与核对 → 上传答卷 → 复核与教师终评 → 关联并查看成绩。评分就绪与完整性以原有服务端核验为准。</p><a href="/help">使用帮助</a>
    </section>
    <section v-else-if="routeBusy||contextError" aria-label="等待核验"></section>
    <div v-else :inert="changingContext||undefined">
    <div v-if="['upload','quick','objective','batches','batch','history'].includes(view)" class="panel-tabs" aria-label="阅卷操作">
      <a :href="href(uploadTarget)" :aria-current="!answerManageOpen&&['upload','quick','objective'].includes(view)?'page':undefined" @click="answerManageOpen=false">上传答卷</a>
      <a :href="href('batches')" :aria-current="!answerManageOpen&&['batches','batch'].includes(view)?'page':undefined" @click="answerManageOpen=false">任务进度</a>
      <button :aria-pressed="answerManageOpen" :disabled="hasUnsavedTask" @click="answerManageOpen=!answerManageOpen">答卷管理与删除</button>
      <button v-if="operator" class="secondary" :aria-expanded="mobileOpen" :disabled="answerManageOpen" @click="mobileOpen=!mobileOpen">手机拍照与连接</button>
    </div>
    <AnswerManagement v-if="answerManageOpen&&context.assignmentId" :key="context.assignmentId+':'+context.examId" :assignment-id="context.assignmentId" :exam-id="context.examId"/>
    <p v-if="answerManageOpen&&!context.assignmentId">请先选择考试或已有评分标准，再管理已上传的答卷。</p>
    <div v-show="!answerManageOpen">
    <div v-if="['upload','quick','objective'].includes(view)" class="panel-tabs" aria-label="答卷类型">
      <a :href="href(context.mode==='quick'?'quick':'upload')" :aria-current="['upload','quick'].includes(view)?'page':undefined">主观题</a><a :href="href('objective')" :aria-current="view==='objective'?'page':undefined">客观题</a>
      <a v-if="context.mode!=='quick'" class="mode-switch" href="#/quick">切换快速匿名</a><a v-else class="mode-switch" href="#/upload?mode=formal">切换班级正式批改</a>
    </div>
    <p v-if="['upload','objective'].includes(view)&&context.examId" class="muted"><a :href="href('history')+'&panel=links'">答卷关联（可选）</a></p>
    <MobileAccess v-if="mobileOpen&&operator" @close="mobileOpen=false"/>
    <ObjectiveView v-if="view==='objective'&&!routeBusy&&!contextError" :key="routeKey" :context="context" @context="adopt" @busy="taskBusy=$event"/>
    <ExamCreateView v-else-if="view === 'create'" @created="createdExam"/>
    <FormalUploadView v-else-if="view === 'upload'" :key="routeKey" :initial-context="context" @context="adopt" />
    <LayoutEditorView v-else-if="view === 'layout'" :key="routeKey" :initial-context="context"/>
    <PrivacyWorkspaceView v-else-if="view === 'privacy'" :key="routeKey" :initial-context="context"/>
    <QuickUploadView v-else-if="view === 'quick'" :key="routeKey" :initial-context="context" @context="adopt"/>
    <HistoryView v-else-if="view === 'history'" :key="routeKey" :initial-context="context" @context="adopt" />
    <BatchView v-else-if="view==='batch'" :key="routeKey" :context="context" :batch-id="routeBatchId"/>
    <BatchesView v-else-if="view === 'batches'" :key="routeKey" :context="context"/>
    <RostersView v-else-if="view === 'rosters'" />
    <UsersView v-else-if="view === 'users' && session.role==='admin'" />
    <section v-else-if="view==='system'"><h2>系统管理</h2><div class="panel-tabs"><a href="#/rosters">班级花名册</a><a v-if="session.role==='admin'" href="#/users">用户管理</a></div><button v-if="operator" class="secondary" :aria-expanded="mobileOpen" @click="mobileOpen=!mobileOpen">手机拍照与连接</button><SystemView v-if="operator"/><p v-else>模型与数据维护由管理员处理。</p><details><summary>兼容工具与辅助功能</summary><div class="tool-links-inline"><a :href="href('layout')">答题区设置</a><a :href="href('privacy')">独立隐私编辑</a><a href="/legacy#view-rubric">手动评分标准编辑</a><a href="/help">使用帮助</a></div><p>手机临时访问入口及历史兼容功能继续保留，不扩展手机权限。</p></details></section>
    </div>
    </div>
  </main>
</template>
