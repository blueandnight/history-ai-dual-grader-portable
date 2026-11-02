<script setup lang="ts">
import {ref} from 'vue'
import QuickTotalReview from './QuickTotalReview.vue'
import PointTeacherReview from './PointTeacherReview.vue'
import {allowLeaving} from './taskGuard'
import type {Detail} from './records'
defineProps<{id:string;expectedExamId?:string}>()
const emit=defineEmits<{saved:[detail:Detail];confirmed:[detail:Detail]}>()
const points=ref(false)
</script>
<template><section><QuickTotalReview v-if="!points" :id="id" :expected-exam-id="expectedExamId" @saved="emit('saved',$event)" @confirmed="emit('confirmed',$event)"/><PointTeacherReview v-else :id="id" :expected-exam-id="expectedExamId" @saved="emit('saved',$event)" @confirmed="emit('confirmed',$event)"/><button class="secondary" @click="allowLeaving()&&(points=!points)">{{points?'返回快捷总分终评':'展开可选的逐点终评'}}</button></section></template>
