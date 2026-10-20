<script setup lang="ts">
import { computed } from 'vue'
import { publicationTargetDisplay } from '../utils/publishing'
const props = defineProps<{ plan: any }>()
const target = computed(() => publicationTargetDisplay(props.plan))
</script>
<template>
  <div class="publication-target-summary" aria-label="发布目标" :title="target.notice || undefined">
    <span v-for="field in target.fields" :key="field.key"><small>{{ field.label }}</small><span>{{ field.title }}</span><small v-if="field.id" class="target-id">（ID {{ field.id }}）</small></span>
    <small v-if="target.cached" class="target-source">名称为最近读取的快照</small>
  </div>
</template>
<style scoped>
.publication-target-summary{display:flex;flex-wrap:wrap;column-gap:18px;row-gap:4px;margin:10px 0;font-size:12px;line-height:1.8;color:var(--df-text)}.publication-target-summary>span{min-width:0;overflow-wrap:anywhere}.publication-target-summary small{margin-right:7px;font-size:11px;color:var(--df-muted)}.publication-target-summary .target-id{margin:0 0 0 3px}.publication-target-summary .target-source{flex-basis:100%;margin:0}
</style>
