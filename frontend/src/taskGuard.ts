import {computed,onUnmounted,reactive,watchEffect,type Ref} from 'vue'
const tasks=reactive(new Map<symbol,boolean>())
export const hasUnsavedTask=computed(()=>[...tasks.values()].some(Boolean))
export function useTaskGuard(active:Ref<boolean>){const key=Symbol();watchEffect(()=>tasks.set(key,active.value));onUnmounted(()=>tasks.delete(key))}
export function allowLeaving(){return !hasUnsavedTask.value||window.confirm('当前有未完成的上传、隐私核对或未保存终评。离开会丢失本页尚未提交的操作；已提交任务仍保留。确认离开？')}
