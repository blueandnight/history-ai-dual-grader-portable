<script setup lang="ts">
import {computed,onUnmounted,ref,watch} from 'vue'
import type {Approval} from './formalUpload'
import {imageBlob} from './privacyEditing'
const props=defineProps<{approvals:Approval[];statuses:string[];editable:boolean;busy:boolean}>()
const emit=defineEmits<{status:[index:number,value:string];edit:[index:number]}>()
const page=ref(1),active=ref(-1),zoom=ref(false),filter=ref('all')
const entries=computed(()=>props.approvals.flatMap((approval,paper)=>approval?approval.preview.images.map((image,index)=>({image,paper,index,key:paper+':'+index})):[]))
const filtered=computed(()=>entries.value.filter(e=>filter.value==='all'||props.statuses[e.paper]==='unchecked'))
const pages=computed(()=>Math.max(1,Math.ceil(filtered.value.length/20)))
const visible=computed(()=>filtered.value.slice((page.value-1)*20,page.value*20))
const current=computed(()=>entries.value[active.value])
const thumbnails=ref<Record<string,string>>({})
let thumbnailRevision=0
// Decode one page at a time; keep only this page's small thumbnails in the DOM.
watch(visible,async list=>{
  const token=++thumbnailRevision;thumbnails.value={}
  for(const e of list){
    if(token!==thumbnailRevision)return
    try{
      const bitmap=await createImageBitmap(imageBlob(e.image.data_url))
      const scale=Math.min(1,240/bitmap.width,240/bitmap.height)
      const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(bitmap.width*scale));canvas.height=Math.max(1,Math.round(bitmap.height*scale))
      canvas.getContext('2d')!.drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close()
      if(token===thumbnailRevision)thumbnails.value[e.key]=canvas.toDataURL('image/jpeg',.75)
    }catch{if(token===thumbnailRevision)thumbnails.value[e.key]=''}
  }
},{immediate:true})
onUnmounted(()=>thumbnailRevision++)
watch(filter,()=>page.value=1)
watch(pages,n=>page.value=Math.min(page.value,n))
function open(key:string){active.value=entries.value.findIndex(e=>e.key===key);zoom.value=false}
function move(delta:number){active.value=Math.max(0,Math.min(entries.value.length-1,active.value+delta));zoom.value=false}
</script>
<template><section aria-label="本批实际外发图片">
  <div class="context-actions"><strong>共 {{approvals.length}} 份 / {{entries.length}} 张</strong><span>待确认 {{statuses.filter(s=>s==='unchecked').length}} 份 · 已确认 {{statuses.filter(s=>s!=='unchecked').length}} 份</span><label>显示<select v-model="filter"><option value="all">全部图片</option><option value="pending">待确认图片</option></select></label><button :disabled="page<=1" @click="page--">上一页</button><span>{{page}} / {{pages}}</span><button :disabled="page>=pages" @click="page++">下一页</button></div>
  <p class="muted">缩略图用于定位。点击查看高清实际外发图，可连续切换；需要遮挡时单独修正，核对后可统一设置本批隐私状态。</p>
  <div class="privacy-grid"><article v-for="e in visible" :key="e.key"><button class="privacy-thumbnail" @click="open(e.key)" :aria-label="'查看第'+(e.paper+1)+'份第'+(e.index+1)+'张高清外发图'"><img v-if="thumbnails[e.key]" :src="thumbnails[e.key]" loading="lazy" decoding="async" alt="外发图缩略图"/><span v-else class="thumbnail-placeholder">点击查看高清图</span><span>第{{e.paper+1}}份 · 图{{e.index+1}}</span></button><small>{{statuses[e.paper]==='unchecked'?'待核对':'隐私状态已设置'}}</small><button v-if="editable" :disabled="busy" @click="emit('edit',e.paper)">单独遮挡修正</button></article></div>
  <div class="context-actions"><button :disabled="page<=1" @click="page--">上一页图片</button><span>第 {{page}} / {{pages}} 页</span><button :disabled="page>=pages" @click="page++">下一页图片</button></div>
  <div v-if="current" class="paper-modal" role="dialog" aria-modal="true" aria-label="高清实际外发图" @keydown.esc="active=-1" @keydown.left.prevent="move(-1)" @keydown.right.prevent="move(1)"><section><div class="context-actions"><strong>第{{current.paper+1}}份 · {{active+1}} / {{entries.length}}张</strong><button :disabled="active===0" @click="move(-1)">上一张</button><button :disabled="active===entries.length-1" @click="move(1)">下一张</button><button @click="zoom=!zoom">{{zoom?'适应窗口':'放大原图'}}</button><button autofocus @click="active=-1">关闭高清图</button></div><div class="privacy-full"><img :class="{zoom}" :src="current.image.data_url" alt="实际发送给模型的高清图片"/></div><label>本份隐私状态<select aria-label="本份隐私状态" :value="statuses[current.paper]" :disabled="busy" @change="emit('status',current.paper,($event.target as HTMLSelectElement).value)"><option value="unchecked">尚未确认</option><option value="pre_anonymized">导入前已避开身份信息</option><option value="redacted">身份信息已遮挡</option><option value="no_identity_visible">没有可辨认身份信息</option></select></label><details><summary>实际提示词与预览指纹</summary><pre>{{approvals[current.paper]?.preview.prompt}}</pre><p>{{approvals[current.paper]?.preview.preview_fingerprint}}</p></details></section></div>
</section></template>
<style>
.privacy-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}.privacy-grid article{border:1px solid #dce2eb;padding:8px;border-radius:6px;display:grid;gap:6px}.privacy-thumbnail{display:grid;width:100%;text-align:left}.privacy-thumbnail img,.thumbnail-placeholder{height:150px;width:100%;object-fit:contain;background:#f3f5f8}.paper-modal{position:fixed;inset:0;z-index:1100;background:#182332aa;padding:16px;display:flex;align-items:center;justify-content:center}.paper-modal>section{background:white;padding:16px;border-radius:8px;width:min(1100px,100%);max-height:95dvh;overflow:auto;box-sizing:border-box}.paper-modal .context-actions{position:sticky;top:0;background:white;z-index:1;padding:6px 0;flex-wrap:wrap}.paper-modal button{white-space:nowrap;min-height:42px}.privacy-full{overflow:auto;max-height:65dvh;text-align:center;background:#f3f5f8}.privacy-full img{max-width:100%;max-height:65dvh;object-fit:contain}.privacy-full img.zoom{max-width:none;max-height:none}.paper-modal .upload-preview{max-height:75dvh;object-fit:contain}@media(max-width:480px){.paper-modal{padding:4px}.paper-modal>section{padding:10px}.privacy-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style>
