<script setup lang="ts">
import { computed } from 'vue'
import { useThemeVars } from 'naive-ui'
import { dubbingDurationReference } from '../utils/dubbingDurationReference'
import { dubbingTimingView, timeRange } from '../utils/dubbingTimingPresentation'

const props = defineProps<{ line: any }>()
const theme = useThemeVars()
const colors = computed(() => ({
  '--adjustment-text': theme.value.textColor2,
  '--adjustment-muted': theme.value.textColor3,
  '--adjustment-border': theme.value.borderColor,
  '--adjustment-surface': theme.value.actionColor,
  '--adjustment-position': theme.value.successColor,
  '--adjustment-warning': theme.value.warningColor,
}))
const timing = computed(() => dubbingTimingView(props.line))
const factor = computed(() => dubbingDurationReference(props.line))
const factorChanged = computed(() => factor.value.requested !== null && factor.value.used !== null
  && factor.value.requested !== factor.value.used)
const factorVisible = computed(() => factorChanged.value || factor.value.retries > 0)
const factorValue = computed(() => factorChanged.value ? `${factor.value.requested} → ${factor.value.used}`
  : factor.value.used === null ? '未记录' : String(factor.value.used))
const timingTitle = computed(() => `源稿 ${timeRange(timing.value.current)}；最终播放 ${timeRange(timing.value.effective)}。原文、译文和配音共用最终播放时间。`)
const factorTitle = computed(() => `当前配音槽的实际生成记录${factor.value.retries ? `；自动缩时重试 ${factor.value.retries} 次` : ''}${factor.value.retryFailed ? '；重试失败，已保留有效音频' : ''}。调整播放位置不会改变声音时长。`)
</script>

<template>
  <span v-if="timing.label || factorVisible" class="adjustments" :style="colors" aria-label="本句调整记录">
    <span v-if="timing.label" class="adjustment position" :class="{ warning: timing.stale }" :title="timingTitle">
      <span class="adjustment-label">位置</span>
      <span class="adjustment-value">{{ timing.stale ? '待核对' : timing.direction }}</span>
      <span class="adjustment-origin">{{ timing.stale || timing.manualActive ? '人工' : '自动' }}</span>
    </span>
    <span v-if="factorVisible" class="adjustment factor" :class="{ warning: factor.retryFailed }" :title="factorTitle">
      <span class="adjustment-label">时长因子</span>
      <span class="adjustment-value">{{ factorValue }}</span>
      <span v-if="factor.retries" class="adjustment-origin">重试 {{ factor.retries }} 次</span>
      <span v-if="factor.retryFailed" class="adjustment-origin warning-text">保留有效音频</span>
    </span>
  </span>
</template>

<style scoped>
.adjustments { display: inline-flex; flex-wrap: wrap; align-items: center; vertical-align: middle; gap: 4px 6px; min-width: 0; max-width: 100%; }
.adjustment { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 2px 5px; box-sizing: border-box; max-width: 100%; padding: 1px 6px; border: 1px solid color-mix(in srgb, var(--adjustment-accent) 30%, transparent); border-radius: 5px; background: color-mix(in srgb, var(--adjustment-accent) 9%, transparent); color: var(--adjustment-accent); font-size: 10px; font-weight: 400; line-height: 16px; }
.position { --adjustment-accent: var(--adjustment-position); }
.factor { --adjustment-accent: color-mix(in srgb, #9872dc 85%, var(--adjustment-text)); }
.adjustment-label { white-space: nowrap; }
.adjustment-value { font-weight: 500; font-variant-numeric: tabular-nums; white-space: nowrap; }
.adjustment-origin { color: color-mix(in srgb, var(--adjustment-accent) 65%, var(--adjustment-muted)); font-size: 10px; white-space: nowrap; }
.adjustment.warning { --adjustment-accent: var(--adjustment-warning); }
.warning .adjustment-value, .warning-text { color: var(--adjustment-warning); }
</style>
