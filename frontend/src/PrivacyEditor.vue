<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref,shallowRef,watch} from 'vue'
import {getSession,isOperator} from './api'
import {normalizedPoint,dragBox} from './layoutEditor'
import {detectPerspectiveAnomaly} from './imagePreparation'
import {emptyPrivacy,initialPrivacy,coverage,readPrivacyFiles,preparePrivacyPages,requestPrivacyPreview,getPrivacyTemplate,savePrivacyTemplate,templateLocated,type PrivacySettings,type PrivacyApproval,type PrivacyPreview,type PrivacyTemplate,type Rectangle} from './privacyEditing'
const props=defineProps<{assignment:{id:string;sub_questions:{id:string;label:string}[];answer_region_profile?:{page_count:number;regions:import('./privacyEditing').AnswerRegion[]}|null};files:File[];studentLabel:string;dual:boolean;initialSettings?:PrivacySettings;gradingInputMode?:'auto_crop'|'original_photo';examId?:string;className?:string}>()
const emit=defineEmits<{ready:[PrivacyApproval];invalidated:[]}>()
const settings=ref<PrivacySettings>(emptyPrivacy(props.files.length)),urls=shallowRef<string[]>([]),prepared=shallowRef<string[]>([])
const preview=shallowRef<PrivacyPreview>(),template=shallowRef<PrivacyTemplate|null>(null)
const page=ref(0),tool=ref<'redact'|'crop'|'corners'>('redact'),questionIds=ref<string[]>([]),cornerDraft=ref<[number,number][]>([])
const busy=ref(true),error=ref(''),message=ref(''),loaded=ref(false),templateConsent=ref(false),previewConsent=ref(false),anomalies=ref<number[]>([]),templateApplied=ref(false)
const drag=ref<Rectangle>(),start=ref<{x:number;y:number}>(),editor=ref<HTMLElement>()
let alive=true,revision=0
const displayed=computed(()=>tool.value==='corners'?urls.value[page.value]:prepared.value[page.value])
const pageRegions=computed(()=>settings.value.answer_regions.filter(r=>r.photo_index===page.value+1))
const operator=computed(()=>isOperator(getSession()))
function initial(){const value=initialPrivacy(props.files.length,props.initialSettings,props.assignment.answer_region_profile);if(props.gradingInputMode==='original_photo')value.answer_regions=[];return value}
function shownRegion(r:import('./privacyEditing').AnswerRegion){if(r.rotation_ccw===90)return {x:1-r.y-r.height,y:r.x,width:r.height,height:r.width};return {x:r.x,y:r.y,width:r.width,height:r.height}}
function invalidate(){revision++;preview.value=undefined;previewConsent.value=false;templateConsent.value=false;message.value='';emit('invalidated')}
function check(token:number){if(!alive||token!==revision)throw new Error('图片设置已变化，请重新预览')}
async function initialize(){invalidate();const token=revision;busy.value=true;error.value='';try{const input=await readPrivacyFiles(props.files);check(token);urls.value=input;prepared.value=await preparePrivacyPages(props.assignment.id,input,settings.value.corners);check(token);const flags=[];for(let i=0;i<input.length;i++){if(await detectPerspectiveAnomaly(input[i]!))flags.push(i+1);check(token)}anomalies.value=flags;template.value=await getPrivacyTemplate(props.assignment.id,input.length);check(token)}catch(e){if(alive&&token===revision)error.value=(e as Error).message}finally{if(alive&&token===revision)busy.value=false}}
function point(event:PointerEvent){return normalizedPoint(event.clientX,event.clientY,editor.value!.getBoundingClientRect())}
function down(event:PointerEvent){if(busy.value||!loaded.value||!prepared.value.length)return;event.preventDefault();invalidate();const p=point(event);if(tool.value==='corners'){if(cornerDraft.value.length===4)cornerDraft.value=[];cornerDraft.value.push([p.x,p.y]);return}start.value=p;drag.value=undefined;editor.value?.setPointerCapture(event.pointerId)}
function move(event:PointerEvent){if(!start.value)return;const box=dragBox(start.value,point(event));drag.value={x:box.x1,y:box.y1,width:box.x2-box.x1,height:box.y2-box.y1}}
function up(event:PointerEvent){if(!start.value)return;move(event);const r=drag.value;if(r&&r.width>=0.006&&r.height>=0.006){if(tool.value==='redact')settings.value.redactions[page.value]!.push({...r});else if(questionIds.value.length)settings.value.answer_regions.push({...r,photo_index:page.value+1,sub_question_ids:[...questionIds.value]});else error.value='先选择答题框所属的小题'}start.value=undefined;drag.value=undefined}
function cancel(){start.value=undefined;drag.value=undefined}
function clear(kind:'redact'|'crop',all=false){invalidate();if(kind==='redact'){if(all)settings.value.redactions[page.value]=[];else settings.value.redactions[page.value]!.pop()}else{const list=settings.value.answer_regions;if(all)settings.value.answer_regions=list.filter(r=>r.photo_index!==page.value+1);else{const index=list.map(r=>r.photo_index).lastIndexOf(page.value+1);if(index>=0)list.splice(index,1)}}templateApplied.value=false}
async function applyCorners(reset=false){if(busy.value||(!reset&&cornerDraft.value.length!==4))return;invalidate();const token=revision;busy.value=true;error.value='';try{const corners=JSON.parse(JSON.stringify(settings.value.corners)) as PrivacySettings['corners'];corners[page.value]=reset?null:cornerDraft.value.map(p=>[...p]);const pages=await preparePrivacyPages(props.assignment.id,urls.value,corners);check(token);settings.value.corners=corners;settings.value.redactions[page.value]=[];settings.value.answer_regions=settings.value.answer_regions.filter(r=>r.photo_index!==page.value+1);templateApplied.value=false;prepared.value=pages;tool.value='redact';cornerDraft.value=[];loaded.value=false;message.value='已由服务端校正；本页遮盖和答题框已清空，请重新核对'}catch(e){if(alive&&token===revision)error.value=(e as Error).message}finally{if(alive&&token===revision)busy.value=false}}
function applyTemplate(){if(!template.value||!templateLocated(settings.value)||!templateConsent.value||busy.value)return;const value=structuredClone(template.value.redactions);invalidate();settings.value.redactions=value;settings.value.answer_regions=[];templateApplied.value=true;message.value='已应用身份遮盖模板；请逐页核对实际位置，再生成外发预览'}
async function saveTemplate(){if(busy.value)return;busy.value=true;error.value='';try{await savePrivacyTemplate(props.assignment.id,settings.value,templateConsent.value);template.value=await getPrivacyTemplate(props.assignment.id,urls.value.length);message.value='身份模板已保存，仅包含遮盖位置'}catch(e){error.value=(e as Error).message}finally{busy.value=false;templateConsent.value=false}}
async function generate(){if(busy.value||!prepared.value.length)return;invalidate();const token=revision;busy.value=true;error.value='';try{if(props.gradingInputMode==='original_photo'){if(settings.value.answer_regions.length)throw new Error('原照片模式不允许设置小题答题框')}else if(!coverage(settings.value,props.assignment.sub_questions.map(q=>q.id)))throw new Error('答题框必须覆盖本次全部小题；或清空全部答题框使用原快速整页路径');const result=await requestPrivacyPreview(props.assignment.id,props.studentLabel,urls.value,settings.value,props.dual,{gradingInputMode:props.gradingInputMode,examId:props.examId,className:props.className});check(token);if(result.blocked||!result.preview_token||!result.preview_fingerprint||!result.images?.length)throw new Error('没有可核对的实际外发图，已阻止继续');preview.value=result}catch(e){if(alive&&token===revision)error.value=(e as Error).message}finally{if(alive&&token===revision)busy.value=false}}
function approve(){if(!preview.value||!previewConsent.value||busy.value)return;emit('ready',{dataUrls:[...urls.value],settings:JSON.parse(JSON.stringify(settings.value)),preview:preview.value,anomalyPages:[...anomalies.value],templateApplied:templateApplied.value});message.value='外发图已核对；仍需在上传区选择隐私状态并确认发送'}
watch(page,()=>{cancel();cornerDraft.value=[];loaded.value=false;error.value=''})
watch(tool,()=>{cancel();cornerDraft.value=[];loaded.value=false})
watch(()=>[props.assignment.id,props.files,props.studentLabel,props.dual],()=>{settings.value=initial();urls.value=[];prepared.value=[];page.value=0;void initialize()})
onMounted(()=>{settings.value=initial();void initialize()});onUnmounted(()=>{alive=false;revision++;preview.value=undefined;urls.value=[];prepared.value=[]})
</script>
<template>
 <section aria-label="隐私图片编辑"><h3>隐私与实际外发图片</h3><p>这里只准备图片，不调用模型。遮盖姓名、学号等身份信息后，核对服务端生成的实际外发图。</p>
  <p v-if="busy" role="status">正在准备或验证图片…</p><p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p>
  <p v-if="anomalies.length">第{{anomalies.join('、')}}页可能有透视偏斜，建议四角定位后再核对。此提示不代表校正已通过。</p>
<fieldset :disabled="busy||!prepared.length"><legend>编辑图片</legend>
   <label>当前页<select v-model.number="page"><option v-for="(_,i) in urls" :key="i" :value="i">第{{i+1}}页</option></select></label>
   <label>编辑工具<select v-model="tool"><option value="redact">身份遮盖</option><option v-if="gradingInputMode!=='original_photo'" value="crop">小题答题框（可选）</option><option value="corners">纸张四角定位</option></select></label>
   <p v-if="tool==='corners'">依次点左上、右上、右下、左下；服务端校正成功后，本页旧遮盖及答题框会清空。</p>
   <fieldset v-if="tool==='crop'&&gradingInputMode!=='original_photo'"><legend>此答题框包含的小题</legend><label class="check" v-for="q in assignment.sub_questions" :key="q.id"><input type="checkbox" :value="q.id" v-model="questionIds">{{q.label||q.id}}</label><p>正式考试必须走合法服务端裁切流程；手工框不能代替正式裁切凭据。</p></fieldset>
   <div class="coordinate-editor" ref="editor" data-testid="privacy-canvas" @pointerdown="down" @pointermove="move" @pointerup="up" @pointercancel="cancel">
    <img v-if="displayed" :key="displayed" :src="displayed" alt="本机待编辑学生页" @load="loaded=true" @error="loaded=false;error='图片无法显示，不能继续'">
    <svg viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
     <template v-if="tool!=='corners'"><rect v-for="(r,i) in settings.redactions[page]" :key="'r'+i" v-bind="r" fill="black" opacity="0.85"/><rect v-for="(r,i) in pageRegions" :key="'q'+i" v-bind="shownRegion(r)" fill="#0066cc22" stroke="#0066cc" stroke-width="0.004"/></template>
     <rect v-if="drag" v-bind="drag" fill="#00884433" stroke="#008844" stroke-width="0.004"/><circle v-for="(p,i) in cornerDraft" :key="i" :cx="p[0]" :cy="p[1]" r="0.012" fill="#df6200"/>
    </svg>
   </div>
   <template v-if="tool==='corners'"><p>已定位{{cornerDraft.length}}/4角</p><button :disabled="cornerDraft.length!==4" @click="applyCorners()">应用四角校正</button> <button @click="applyCorners(true)">恢复本页原图</button></template>
   <template v-else><button @click="clear('redact')">撤销本页最后遮盖</button> <button @click="clear('redact',true)">清空本页遮盖</button> <button @click="clear('crop')">撤销本页最后答题框</button> <button @click="clear('crop',true)">清空本页答题框</button></template>
  </fieldset>
  <details v-if="prepared.length"><summary>身份遮盖模板</summary><p>模板仅保存校正坐标中的遮盖位置，不保存答题框、照片或学生内容。应用或保存前，必须逐页完成四角定位。</p><label class="check"><input type="checkbox" v-model="templateConsent" :disabled="busy||!templateLocated(settings)">已逐页定位四角，确认模板遮盖位置</label><button v-if="template" :disabled="busy||!templateConsent||!templateLocated(settings)" @click="applyTemplate">应用已有身份模板</button> <button v-if="operator" :disabled="busy||!templateConsent||!templateLocated(settings)" @click="saveTemplate">保存身份遮盖模板</button></details>
  <button :disabled="busy||!prepared.length" @click="generate">生成实际外发预览</button>
  <section v-if="preview" aria-label="实际外发预览"><h4>实际将发给模型的图片</h4><p>{{preview.provider}} / {{preview.model}} · 图片{{preview.images.length}}张</p><figure v-for="(image,i) in preview.images" :key="i"><figcaption>外发图{{i+1}} · {{image.image_kind||'whole_paper'}} · 第{{image.source_page}}页 · {{image.sub_question_ids?.join('、')}}</figcaption><img :src="image.data_url" class="upload-preview" :alt="'实际外发图'+(i+1)"></figure><p>本份预计模型请求{{preview.estimated_model_requests??2}}次；混合模式可能增加费用，重试另计。</p><details><summary>实际提示词与指纹</summary><pre>{{preview.prompt}}</pre><pre v-for="(p,i) in preview.region_prompts" :key="i">{{p.label}}：{{p.prompt}}</pre><p>{{preview.preview_fingerprint}}</p></details><label class="check"><input type="checkbox" v-model="previewConsent">已逐张核对外发图，身份信息已处理，答案未被误遮盖</label><button :disabled="!previewConsent||busy" @click="approve">采用这组外发预览</button></section>
 </section>
</template>
