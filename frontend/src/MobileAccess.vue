<script setup lang="ts">
import {computed,onMounted,onUnmounted,ref} from 'vue'
import {getSession,isOperator} from './api'
import {useTaskGuard} from './taskGuard'
import {readMobileStatus,createMobileAccess,revokeMobileAccess,mobileAddresses,type MobileStatus} from './mobileAccess'
withDefaults(defineProps<{closable?:boolean}>(),{closable:true})
const emit=defineEmits<{close:[]}>()
const status=ref<MobileStatus>(),token=ref(''),consent=ref(false),busy=ref(false),uncertain=ref(false),error=ref(''),message=ref(''),expiresAt=ref(0)
useTaskGuard(busy)
const allowed=computed(()=>isOperator(getSession()))
const links=computed(()=>status.value?mobileAddresses(status.value,window.location.origin,token.value):[])
let alive=true
async function read(){if(busy.value)return;busy.value=true;error.value='';try{const s=await readMobileStatus();if(alive){status.value=s;uncertain.value=false;if(!s.mobile_token_active)token.value=''}}catch(e){if(alive)error.value=(e as Error).message}finally{if(alive)busy.value=false}}
async function generate(){if(busy.value||uncertain.value||!consent.value)return;busy.value=true;token.value='';message.value='';error.value='';try{const r=await createMobileAccess(consent.value);if(!r.token||!Number.isFinite(r.expires_at))throw Error('访问码返回不完整');if(alive){token.value=r.token;expiresAt.value=r.expires_at;status.value={...status.value,mobile_token_active:true};message.value='访问码已生成，请复制下方完整地址到手机。'}}catch{if(alive){uncertain.value=true;error.value='生成结果未能确认，请先刷新连接状态；不会自动重复生成。'}}finally{if(alive){busy.value=false;consent.value=false}}}
async function revoke(){if(busy.value||uncertain.value)return;busy.value=true;error.value='';try{await revokeMobileAccess();if(alive){token.value='';status.value={...status.value,mobile_token_active:false};message.value='手机访问码已撤销。'}}catch{if(alive){uncertain.value=true;token.value='';error.value='撤销结果未能确认，请先刷新连接状态。'}}finally{if(alive)busy.value=false}}
async function copy(url:string){try{await navigator.clipboard.writeText(url);message.value='手机地址已复制。'}catch{message.value='浏览器未允许复制，请选中下方完整地址手动复制。'}}
onMounted(read);onUnmounted(()=>{alive=false;token.value='';status.value=undefined})
</script>
<template><section class="mobile-access-panel" aria-label="手机拍照与连接"><div class="page-heading"><h2>手机拍照与连接</h2><button v-if="closable" class="secondary" :disabled="busy" @click="emit('close')">收起手机连接</button></div><p v-if="!allowed" role="alert">仅管理员或本机工作台可管理手机连接。</p><template v-else><p>在这里准备手机连接，无需离开当前工作台。手机使用原有受限采集页面，不获得教师管理权限。</p><p v-if="!getSession()?.cloud_mode">手机与电脑连接同一 Wi-Fi，或开启 USB 网络共享，然后刷新地址。</p><button :disabled="busy" @click="read">刷新连接状态</button><p v-if="busy" role="status">正在处理…</p><p v-if="error" role="alert">{{error}}</p><p v-if="message" role="status">{{message}}</p><p v-if="status?.mobile_token_active&&!token">已有访问码正在生效；本页不会读取旧访问码。重新生成将使旧码失效。</p><fieldset :disabled="busy||uncertain"><legend>准备手机访问</legend><label class="check"><input type="checkbox" v-model="consent">确认生成临时手机访问码（如有旧码将失效）</label><button class="primary" :disabled="!consent" @click="generate">生成手机访问码</button><button class="secondary" :disabled="!status?.mobile_token_active" @click="revoke">撤销手机访问码</button></fieldset><p v-if="token">访问码有效至{{new Date(expiresAt*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai'})}}，重启或撤销后失效。请私下传递完整地址。</p><ul v-if="links.length" class="mobile-addresses"><li v-for="link in links" :key="link.url"><strong>{{link.label}}</strong><code>{{link.url}}</code><button v-if="token" class="secondary" @click="copy(link.url)">复制手机地址</button></li></ul><p v-else-if="status" role="status">未检测到可用的手机地址。请检查 Wi-Fi 或 USB 网络共享连接后刷新。</p></template></section></template>
