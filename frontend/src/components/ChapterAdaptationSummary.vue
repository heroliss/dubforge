<script setup lang="ts">
import { chapterRuleText } from '../utils/chapterRules'
import { chapterSourceLabel } from '../utils/chapterRules'
withDefaults(defineProps<{ value?: any; local?: boolean }>(), { local: true })
</script>

<template>
  <div class="chapter-adaptation" aria-label="B站章节整理说明">
    <p class="chapter-rules">{{ chapterRuleText(value?.rules) }}</p>
    <template v-if="value">
      <p class="chapter-adaptation-result" role="status">{{ local ? '本次 ' : '' }}{{ value.source_count }} 条{{ local ? '输入' : '源章节' }} → {{ value.output_count }} 个B站章节<span v-if="value.merged_count"> · 合并精简 {{ value.merged_count }} 条</span>。{{ local ? '仅更新本地候选，尚未写入B站。' : '本次任务整理结果；交付状态以上方回执为准。' }}</p>
      <ul v-if="value.notes?.length"><li v-for="(note,index) in value.notes" :key="index">{{ note }}</li></ul>
      <p v-if="value.instruction" class="chapter-custom-instruction">本次自定义要求：{{ value.instruction }}</p>
      <details v-if="value.review?.checked"><summary>标题审查{{ value.review.findings?.length ? ` · ${value.review.findings.length} 项调整或待复核` : '已完成' }}</summary><p v-if="value.review.summary">{{ value.review.summary }}</p><ul v-if="value.review.findings?.length"><li v-for="(finding,index) in value.review.findings" :key="index" :class="{ 'needs-review': finding.action === 'needs_review' }"><strong>{{ finding.action === 'needs_review' ? '需人工复核' : '已改写' }}</strong> · {{ chapterSourceLabel(finding.source_indices) }}：{{ finding.reason }}</li></ul></details>
    </template>
  </div>
</template>

<style scoped>
.chapter-adaptation{margin:10px 0;padding:10px 12px;border-radius:8px;background:var(--df-surface-hover,rgba(128,155,180,.06));border:1px solid var(--df-border);font-size:11px;line-height:1.8;color:var(--df-muted)}.chapter-adaptation p{margin:0}.chapter-adaptation-result{color:var(--df-primary,#93b7ed);margin-top:5px!important}.chapter-adaptation ul{margin:5px 0 0;padding-left:18px}
.chapter-adaptation details{margin-top:7px}.chapter-adaptation summary{cursor:pointer}.needs-review{color:var(--df-warning,#efbd70)}
.chapter-custom-instruction{margin-top:6px!important;white-space:pre-wrap;overflow-wrap:anywhere}
</style>
