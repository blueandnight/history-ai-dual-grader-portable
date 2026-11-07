<script setup lang="ts">
import type {Sub} from './examImport'
defineProps<{config:NonNullable<Sub['holistic']>}>()
</script>
<template><section aria-label="按评价项目分级给分"><h4>按评价项目分级给分</h4><p>逐项给分后相加；参考范例不构成唯一答案。请核对原卷标准，不必填写整题档次。</p><article v-for="p in config.projects" :key="p.id"><label>评价项目<input v-model="p.label" required></label><label>项目满分<input type="number" v-model.number="p.max_score" min="0" step="any"></label><label>项目评分步长<input type="number" v-model.number="p.score_step" min="0.01" step="any"></label><div v-for="l in p.levels" :key="l.id"><label>最低分<input type="number" v-model.number="l.min_score" min="0" step="any"></label><label>最高分<input type="number" v-model.number="l.max_score" min="0" step="any"></label><label>该项目等级标准<textarea v-model="l.description" required rows="3"></textarea></label></div></article></section></template>
