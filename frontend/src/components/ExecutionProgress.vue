<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { NProgress, NText, useThemeVars } from 'naive-ui'
import { normalizeExecution } from '../utils/executionProgress'
import type { ProgressActivity } from '../utils/progressActivity'
const props = defineProps<{ execution?: any; percentage?: number | null; estimated?: boolean; indeterminate?: boolean;
  eta?: string; compact?: boolean; activity?: ProgressActivity | null; progressLabel?: string; inactivityLabel?: string }>()
const observedSeconds = ref(0)
let observedAt = performance.now()
let clock: ReturnType<typeof setInterval> | undefined
const mounted = ref(false)
const current = computed(() => normalizeExecution(props.execution, observedSeconds.value))
watch([() => props.execution?.schema, () => props.execution?.state, () => props.execution?.job_id,
  () => props.execution?.run_id, () => props.execution?.phase, () => props.execution?.started_at,
  () => props.execution?.updated_at, () => props.execution?.elapsed_seconds, () => props.execution?.inactive_seconds,
  () => props.execution?.metrics?.sentence_started_at, () => props.execution?.metrics?.sentence_elapsed_seconds], () => {
  observedAt = performance.now()
  observedSeconds.value = 0
}, { flush: 'sync' })
watch(() => mounted.value && !!current.value?.clockActive, enabled => {
  if (clock !== undefined) clearInterval(clock)
  clock = enabled ? setInterval(() => { observedSeconds.value = Math.max(0, performance.now() - observedAt) / 1000 }, 1000) : undefined
}, { flush: 'sync' })
onMounted(() => { observedAt = performance.now(); mounted.value = true })
onUnmounted(() => { if (clock !== undefined) clearInterval(clock) })
const percentage = computed(() => current.value?.state !== 'queued' && !props.indeterminate && typeof props.percentage === 'number' && Number.isFinite(props.percentage)
  ? current.value?.workPercentage ?? Math.max(0, Math.min(100, props.percentage)) : null)
const progressLabel = computed(() => current.value?.workProgressLabel || props.progressLabel || '本阶段')
const displayEstimated = computed(() => props.estimated && current.value?.workPercentage == null)
const theme = useThemeVars()
const statusType = computed(() => current.value?.statusType || 'info')
const style = computed(() => ({ '--execution-accent': statusType.value !== 'default'
  ? theme.value[`${statusType.value}Color`] : theme.value.textColor3, '--execution-border': theme.value.borderColor,
  '--execution-text': theme.value.textColor1, '--execution-muted': theme.value.textColor3 }))
const nextCheck = computed(() => {
  const raw = current.value?.nextCheck
  if (!raw) return ''
  const date = new Date(raw)
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour12: false }) : ''
})
</script>
<template>
  <section v-if="current || activity" class="execution-progress" :class="{ compact }" :style="style" aria-label="执行过程" :data-execution-state="current?.state || 'running'">
    <div class="execution-current">
      <div class="execution-heading" role="status" aria-live="polite" aria-atomic="true"><strong>{{ current?.label || activity?.message }}</strong><n-text v-if="current" class="execution-state" :type="current.statusType" :depth="current.statusType === 'default' ? 3 : undefined" :data-status-type="current.statusType">{{ current.stateLabel }}</n-text></div>
      <p v-if="current?.detail && current.detail !== current.label" class="execution-detail">{{ current.detail }}</p>
      <div v-if="current && current.state !== 'skipped'" class="execution-facts">
        <span v-if="current.versionLabel">{{ current.versionLabel }}</span>
        <span v-if="current.total !== null && current.total > 0 && current.completed !== null">已完成 {{ current.completedText }} / {{ current.totalText }}{{ current.displayUnit ? ' ' + current.displayUnit : '' }}</span>
        <span v-else-if="current.completed !== null">已完成 {{ current.completedText }}{{ current.displayUnit ? ' ' + current.displayUnit : '' }}</span>
        <span v-if="current.attempt && (current.attempt > 1 || current.state === 'retrying')">第 {{ current.attempt }}<template v-if="current.max_attempts"> / {{ current.max_attempts }}</template> 次尝试</span>
        <span v-if="current.phaseElapsed" title="按当前阶段起点计时，包含该阶段内的等待，不代表整项任务用时">本阶段已用 {{ current.phaseElapsed }}</span>
        <span v-else-if="current.elapsed">{{ current.state==='queued' ? '已等待' : '累计已用' }} {{ current.elapsed }}<template v-if="current.state!=='queued'">（含等待）</template></span>
        <span v-if="current.priorQueueElapsed">此前排队 {{ current.priorQueueElapsed }}</span>
        <span v-if="current.sentenceElapsed">本句已用 {{ current.sentenceElapsed }}</span>
        <span v-if="current.sentenceProgress !== null">本句模型进度 {{ current.sentenceProgress }}%</span>
        <span v-if="current.sentenceAttempt && current.sentenceAttempts && current.sentenceAttempts > 1">本句第 {{ current.sentenceAttempt }} / {{ current.sentenceAttempts }} 次生成</span>
        <span v-if="current.inactive && !current.sentenceActive">{{ inactivityLabel || '距进度更新' }} {{ current.inactive }}</span>
        <span v-if="nextCheck">下次核对 {{ nextCheck }}</span>
      </div>
      <p v-if="current?.sentenceActive" class="execution-sentence-hint">句数在音频生成并保存后更新<template v-if="current.sentenceProgress === null">；当前模型暂未回报句内进度</template>。</p>
    </div>
    <div v-if="percentage !== null" class="execution-bar">
      <n-progress type="line" :height="compact ? 4 : 6" :percentage="percentage" :show-indicator="false" :processing="!!current?.active && current.state === 'running'" :status="current?.state === 'failed' ? 'error' : 'default'" />
      <span>{{ progressLabel }}{{ displayEstimated ? '估算 ' : ' ' }}{{ Number(percentage.toFixed(1)) }}%</span>
    </div>
    <p v-if="eta || displayEstimated" class="execution-estimate">{{ eta }}<template v-if="displayEstimated">{{ eta ? ' · ' : '' }}估算仅供参考，不代表整项或整批剩余时间</template></p>
    <dl v-if="current?.metrics.length" class="execution-metrics"><div v-for="metric in current.metrics" :key="metric.key"><dt>{{ metric.label }}</dt><dd>{{ metric.value }}</dd></div></dl>
    <details v-if="current?.history.length" class="execution-history"><summary>最近过程 · {{ current.history.length }} 项</summary><ol><li v-for="(item,index) in current.history" :key="index"><strong>{{ item.label }}</strong><n-text class="execution-history-state" :type="item.statusType" :depth="item.statusType === 'default' ? 3 : undefined" :data-status-type="item.statusType">{{ item.stateLabel }}</n-text><p v-if="item.detail">{{ item.detail }}</p></li></ol></details>
    <details v-if="current && activity && activity.message !== current.label && activity.message !== current.detail" class="execution-log"><summary>{{ activity.label === '最新动态' ? '最新日志动态' : '补充说明' }}<span v-if="activity.time"> · {{ activity.time }}</span></summary><p>{{ activity.message }}</p></details>
  </section>
</template>
<style scoped>
.execution-progress{min-width:0;max-width:100%;margin:10px 0;padding:12px 14px;border:1px solid var(--execution-border);border-left:1px solid var(--execution-accent);border-radius:8px;color:var(--execution-text);background:color-mix(in srgb,var(--execution-accent) 4%,transparent);font-size:12px;line-height:1.75;box-sizing:border-box;overflow-wrap:anywhere}.execution-heading{display:flex;align-items:baseline;gap:8px 14px;flex-wrap:wrap}.execution-heading strong{font-weight:550;font-size:13px}.execution-state{margin-left:auto;font-size:11px;flex-shrink:0}.execution-detail{margin:6px 0}.execution-facts{display:flex;gap:4px 16px;flex-wrap:wrap;color:var(--execution-muted);font-variant-numeric:tabular-nums}.execution-facts:empty{display:none}.execution-sentence-hint{margin:3px 0 0;color:var(--execution-muted);font-size:11px}.execution-bar{display:flex;gap:12px;align-items:center;margin-top:9px}.execution-bar>.n-progress{flex:1;min-width:0}.execution-bar>span{flex-shrink:0;color:var(--execution-muted);font-size:11px;font-variant-numeric:tabular-nums}.execution-estimate{font-size:11px;color:var(--execution-muted);margin:5px 0 0}.execution-metrics{display:flex;gap:6px 20px;flex-wrap:wrap;margin:9px 0 0;padding-top:8px;border-top:1px solid var(--execution-border)}.execution-metrics>div{display:flex;gap:7px}.execution-metrics dt{color:var(--execution-muted)}.execution-metrics dd{margin:0;font-variant-numeric:tabular-nums}.execution-history,.execution-log{margin-top:9px;color:var(--execution-muted)}summary{cursor:pointer;font-size:11px}.execution-history ol{margin:8px 0;padding-left:20px}.execution-history li{padding:4px 0}.execution-history strong{font-weight:500;color:var(--execution-text);margin-right:12px}.execution-history p,.execution-log p{margin:3px 0 6px;white-space:pre-wrap}.compact{padding:8px 10px;margin:6px 0;font-size:11px}.compact .execution-heading strong{font-size:12px}.compact .execution-metrics{gap:4px 14px}@media(max-width:520px){.execution-progress{padding:10px}.execution-heading{gap:5px 10px}.execution-bar{flex-wrap:wrap}.execution-bar>.n-progress{flex-basis:100%}.execution-metrics{gap:5px 12px}}
</style>
