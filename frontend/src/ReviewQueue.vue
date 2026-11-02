<script setup lang="ts">
import {computed,nextTick,ref} from 'vue'
import TeacherReview from './TeacherReview.vue'
import {allowLeaving} from './taskGuard'
import {nextReviewId,pendingReview,reviewScope,type ReviewItem} from './reviewQueue'
import type {Detail} from './records'
const props=defineProps<{id:string;items:ReviewItem[];expectedExamId?:string;backLabel?:string}>()
const emit=defineEmits<{'update:id':[id:string];saved:[detail:Detail]}>()
const continuous=ref(true),message=ref('')
const scoped=computed(()=>reviewScope(props.items,props.id,props.expectedExamId))
const nextId=computed(()=>nextReviewId(props.items,props.id,props.expectedExamId))
function move(){if(nextId.value&&allowLeaving()){message.value='';emit('update:id',nextId.value)}}
async function confirmed(d:Detail){
 if(!d.confirmed_at||d.teacher_reconfirmation_required||d.id!==props.id)return
 await nextTick()
 if(continuous.value&&nextId.value)move()
 else message.value=nextId.value?'本份终评已保存，可以继续下一份。':'本份终评已保存，本范围内暂无下一份待评审答卷。'
}
</script>
<template><section class="review-queue" aria-label="连续评阅导航">
 <div class="review-queue-bar"><span>当前第{{scoped.findIndex(s=>s.id===id)+1}} / {{scoped.length}}份 · {{scoped.filter(pendingReview).length}}份待评审</span><div class="context-actions"><button class="secondary" @click="allowLeaving()&&emit('update:id','')">{{backLabel||'返回答卷列表'}}</button><button class="secondary" :disabled="!nextId" @click="move">下一份待评审</button></div></div>
 <label class="check"><input type="checkbox" v-model="continuous">保存教师终评后自动进入下一份</label><p v-if="message" role="status">{{message}}</p>
<p v-if="!scoped.some(s=>s.id===id)" role="alert">当前答卷不在本次评阅范围，不能继续终评，请返回列表重新选择。</p>
 <TeacherReview v-else :id="id" :expected-exam-id="expectedExamId" @saved="emit('saved',$event)" @confirmed="confirmed" />
 <div class="review-queue-footer"><button class="secondary" :disabled="!nextId" @click="move">继续下一份待评审</button><small>不会自动给分、确认成绩或关联考生。</small></div>
</section></template>
