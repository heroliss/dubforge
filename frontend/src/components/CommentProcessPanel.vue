<script setup lang="ts">
import { computed } from 'vue'
import { NButton, useThemeVars } from 'naive-ui'
import AppIcon from './AppIcon.vue'
import ExecutionProgress from './ExecutionProgress.vue'
import { executionForComment } from '../utils/executionProgress'
import { commentJobSummary, type CommentAction } from '../utils/commentWorkflow'
import { commentJobProgress, commentPreparationHint, commentProcessStages, commentProgressJob, type CommentAutomation, type CommentCollection } from '../utils/commentProcess'

const props = defineProps<{ progress?: any; state?: any; automation?: CommentAutomation | null; collection?: CommentCollection | null; pending?: string; compact?: boolean }>()
const emit = defineEmits<{ 'show-diagnostics': [action?: CommentAction]; stop: []; refresh: [] }>()
const job = computed<any>(() => commentProgressJob(props.progress, props.state, props.pending))
const preparation = computed(() => props.progress?.prepare_job || props.state?.prepare_job)
const activePreparation = computed(() => preparation.value?.status === 'running' ? preparation.value : null)
const preparing = computed(() => props.pending === 'prepare' || preparation.value?.status === 'running')
const detail = computed(() => commentJobProgress(job.value))
const execution = computed(() => executionForComment(job.value, detail.value))
const collection = computed(() => props.progress?.collection?.busy ? props.progress.collection
  : props.collection?.busy ? props.collection : props.progress?.collection || props.collection)
const stages = computed(() => commentProcessStages(props.progress, props.state, collection.value, props.pending))
const collecting = computed(() => collection.value?.busy || collection.value?.status === 'running')
const collectionExecution = computed(() => ({schema:1,state:'waiting',label:'正在获取热门评论',
  detail:[collection.value?.limit ? `本次最多 ${collection.value.limit} 条` : '',collection.value?.count ? `上次快照 ${collection.value.count} 条` : '',
    '平台未提供可用的实时百分比；获取完成后会更新原文数量。'].filter(Boolean).join(' · '),elapsed_seconds:collection.value?.elapsed_seconds}))
const running = computed(() => !!props.pending || preparing.value || collecting.value || job.value?.status === 'running' || props.automation?.state === 'running')
const summary = computed(() => commentJobSummary(job.value))
const attention = computed(() => ['failed', 'interrupted', 'cancelled', 'partial'].includes(job.value?.status) || ['failed', 'interrupted', 'cancelled', 'partial'].includes(preparation.value?.status) || !!collection.value?.error)
const saved = computed(() => stages.value.some(stage => ['done', 'available'].includes(stage.state)))
const theme = useThemeVars()
const panelStyle = computed(() => ({
  '--comment-text': theme.value.textColor1,
  '--comment-secondary': theme.value.textColor2,
  '--comment-muted': theme.value.textColor3,
  '--comment-border': theme.value.borderColor,
  '--comment-panel': theme.value.cardColor,
  '--comment-info': theme.value.infoColor,
  '--comment-success': theme.value.successColor,
  '--comment-warning': theme.value.warningColor,
}))
</script>

<template>
  <section class="comment-process" :class="{ compact }" :style="panelStyle" aria-label="评论处理流程">
    <header class="process-heading">
      <div class="process-title"><h4>评论处理流程</h4><span class="process-state" :class="{ running, attention }">{{ running ? '进行中' : attention ? '有待处理项' : saved ? '已有保存结果' : '等待操作' }}</span></div>
    </header>
    <ol class="process-stages" aria-label="评论处理阶段">
      <li v-for="(stage, index) in stages" :key="stage.id" class="process-stage" :class="stage.state" :data-stage-id="stage.id" :aria-current="stage.state === 'running' ? 'step' : undefined"><span class="stage-dot"><AppIcon v-if="['done','available'].includes(stage.state)" name="check" :size="15" /><template v-else>{{ index + 1 }}</template></span><span class="stage-copy"><strong>{{ stage.label }}</strong><small>{{ stage.detail }}</small></span><AppIcon v-if="index < stages.length - 1" name="chevron" :size="13" class="stage-direction" /></li>
    </ol>
    <div v-if="preparing" class="preparation-progress" role="status"><strong>正在准备目标数量评论<template v-if="activePreparation?.target"> · {{ activePreparation.target }} 条</template></strong><p>{{ activePreparation?.message || '正在检查缓存与人工选择…' }}</p><small>{{ commentPreparationHint(automation, pending) }}</small></div>
    <div v-if="running || job || preparation?.error || collection?.error || automation" class="process-current" :class="{'has-execution':job || collecting}">
      <template v-if="collecting">
        <ExecutionProgress :execution="collectionExecution" indeterminate :compact="compact" />
      </template>
      <template v-else-if="job">
        <ExecutionProgress :execution="execution" :percentage="detail.percent" :indeterminate="detail.total === null || !detail.total" :compact="compact" />
        <div v-if="summary" class="result-summary"><span>{{ preparation ? '最近一批结果' : '本次结果' }}</span><p>{{ summary }}</p></div>
        <p v-if="job.error" class="process-error">{{ job.error }}<span v-if="job.suggestion" class="error-suggestion">{{ job.suggestion }}</span></p>
        <p v-else-if="job.warning && !compact && !summary" class="process-error">{{ job.warning }}</p>
      </template>
      <p v-if="preparation?.error && !preparing && preparation.error !== job?.error" class="process-error">{{ preparation.error }}</p>
      <p v-if="collection?.error" class="process-error">{{ collection.error }}</p>
      <p v-if="automation && !compact" class="automation-note">{{ automation.message }}</p>
    </div>
    <footer v-if="!compact" class="process-actions"><span class="muted">复用已有结果，按需补齐；上屏方案统一确认。</span><div class="process-action-buttons"><n-button v-if="job || pending || preparation" text size="small" @click="emit('show-diagnostics', job ? detail.action : undefined)">查看 LLM 对话与日志</n-button><n-button text size="small" @click="emit('refresh')"><template #icon><AppIcon name="refresh" :size="15" /></template>刷新进度</n-button><n-button v-if="activePreparation?.id || job?.status === 'running' && job.id" secondary type="warning" size="small" :disabled="activePreparation ? activePreparation.cancelling : job?.cancelling" @click="emit('stop')">{{ (activePreparation ? activePreparation.cancelling : job?.cancelling) ? '正在停止…' : activePreparation ? '停止准备' : '停止当前处理' }}</n-button></div></footer>
  </section>
</template>

<style scoped>
.comment-process{min-width:0;margin:18px 0 24px;color:var(--comment-text)}
.process-heading,.process-title,.process-actions,.process-action-buttons{display:flex;align-items:center;flex-wrap:wrap}
.process-heading{justify-content:space-between;gap:10px 16px}.process-title{gap:10px}.process-heading h4{margin:0;font-size:14px;font-weight:600;line-height:1.6}
.process-state{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;border-radius:5px;background:color-mix(in srgb,var(--comment-muted) 8%,transparent);color:var(--comment-secondary);font-size:11px;line-height:1.5}.process-state::before{content:'';width:5px;height:5px;border-radius:50%;background:currentColor}.process-state.attention{color:var(--comment-warning);background:color-mix(in srgb,var(--comment-warning) 9%,transparent)}.process-state.running{color:var(--comment-info);background:color-mix(in srgb,var(--comment-info) 10%,transparent)}
.process-stages{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px;list-style:none;padding:12px 0;margin:12px 0 0;border-top:1px solid var(--comment-border);border-bottom:1px solid var(--comment-border)}
.process-stage{display:grid;grid-template-columns:28px minmax(0,1fr) 13px;align-items:start;gap:10px;min-width:0;padding:10px 12px;border-radius:8px;box-sizing:border-box}.process-stage:last-child{grid-template-columns:28px minmax(0,1fr)}
.stage-dot{display:grid;place-items:center;flex:none;width:26px;height:26px;border:1px solid var(--comment-border);border-radius:50%;color:var(--comment-secondary);font-size:11px;line-height:1;font-variant-numeric:tabular-nums}.stage-copy{display:grid;gap:5px;min-width:0;padding-top:2px}.stage-copy strong{font-size:13px;font-weight:550;line-height:1.6;color:var(--comment-secondary)}.stage-copy small{color:var(--comment-muted);font-size:12px;line-height:1.65;overflow-wrap:anywhere}.stage-direction{margin-top:8px;color:var(--comment-border);justify-self:end}
.process-stage.running{background:color-mix(in srgb,var(--comment-info) 7%,transparent)}.process-stage.running .stage-dot{color:var(--comment-info);border-color:color-mix(in srgb,var(--comment-info) 55%,var(--comment-border));background:color-mix(in srgb,var(--comment-info) 10%,transparent)}.process-stage.running .stage-copy strong{color:var(--comment-info)}.process-stage.running .stage-copy small{color:color-mix(in srgb,var(--comment-info) 24%,var(--comment-secondary))}
.process-stage.done .stage-dot,.process-stage.available .stage-dot{color:var(--comment-success);background:color-mix(in srgb,var(--comment-success) 9%,transparent);border-color:color-mix(in srgb,var(--comment-success) 35%,var(--comment-border))}.process-stage.done .stage-copy strong,.process-stage.available .stage-copy strong{color:var(--comment-text)}.process-stage.attention .stage-dot{color:var(--comment-warning);border-color:color-mix(in srgb,var(--comment-warning) 50%,var(--comment-border));background:color-mix(in srgb,var(--comment-warning) 7%,transparent)}.process-stage.attention .stage-copy strong{color:var(--comment-warning)}
.preparation-progress{display:grid;gap:4px;padding:12px 14px;margin:14px 0;border-radius:8px;background:color-mix(in srgb,var(--comment-info) 6%,transparent);font-size:12px;line-height:1.75}.preparation-progress strong{font-weight:550;font-size:13px;color:var(--comment-info)}.preparation-progress p{margin:0;color:var(--comment-secondary)}.preparation-progress small{color:color-mix(in srgb,var(--comment-info) 20%,var(--comment-secondary));font-size:11px}
.process-current{min-width:0;padding-top:16px;font-size:12px;line-height:1.75;overflow-wrap:anywhere}.process-current:empty{display:none}.process-current :deep(.execution-progress){margin:0;padding:0;border:0;border-radius:0;background:transparent}.process-current :deep(.execution-heading strong){font-size:13px}.process-current :deep(.execution-heading){gap:8px 14px}
.result-summary{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 12px;margin:12px 0 0;padding-top:12px;border-top:1px solid var(--comment-border)}.result-summary>span{font-size:11px;color:var(--comment-muted);line-height:1.9}.result-summary p{margin:0;color:var(--comment-secondary);font-size:12px;line-height:1.8}
.process-error{margin:12px 0 0;padding:10px 12px;border:1px solid color-mix(in srgb,var(--comment-warning) 30%,var(--comment-border));border-radius:8px;background:color-mix(in srgb,var(--comment-warning) 5%,transparent);color:var(--comment-warning);font-size:12px;line-height:1.8}.error-suggestion{display:block;margin-top:4px;color:color-mix(in srgb,var(--comment-warning) 20%,var(--comment-secondary))}.automation-note{margin:12px 0 0;color:var(--comment-secondary);font-size:12px;line-height:1.8}.muted{font-size:12px;color:var(--comment-muted);line-height:1.8}
.process-actions{justify-content:space-between;align-items:baseline;gap:10px 18px;margin-top:12px}.process-action-buttons{gap:10px 16px;margin-left:auto}.process-action-buttons :deep(.n-button){font-size:12px}.process-actions>.muted{font-size:11px}
.compact{margin:12px 0 0}.compact .process-stages{padding:10px 0;margin-top:10px}.compact .process-stage{grid-template-columns:24px minmax(0,1fr) 13px;padding:8px;gap:7px}.compact .process-stage:last-child{grid-template-columns:24px minmax(0,1fr)}.compact .stage-dot{width:22px;height:22px}.compact .stage-copy{padding-top:0;gap:3px}.compact .stage-copy strong{font-size:12px}.compact .stage-copy small{font-size:11px}.compact .process-current{padding-top:12px}.compact .result-summary{margin-top:10px;padding-top:10px}
@media(max-width:850px){.process-stages{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 6px}.process-stage:nth-child(2n){grid-template-columns:28px minmax(0,1fr)}.process-stage:nth-child(2n) .stage-direction{display:none}.compact .process-stage:nth-child(2n){grid-template-columns:24px minmax(0,1fr)}}
@media(max-width:520px){.comment-process{margin:16px 0 20px}.process-stage,.compact .process-stage{grid-template-columns:24px minmax(0,1fr) 13px;padding:8px 4px;gap:7px}.process-stage:nth-child(2n),.compact .process-stage:nth-child(2n){grid-template-columns:24px minmax(0,1fr)}.stage-dot,.compact .stage-dot{width:22px;height:22px}.stage-copy strong{font-size:12px}.stage-copy small{font-size:11px}.preparation-progress{padding:10px 12px}.process-actions{align-items:flex-start;gap:10px}.process-action-buttons{margin-left:0;gap:10px 14px}.result-summary{grid-template-columns:1fr;gap:3px}}
</style>
