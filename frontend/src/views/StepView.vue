<script setup lang="ts">
import FilePathLink from '../components/FilePathLink.vue'
import ExecutionProgress from '../components/ExecutionProgress.vue'
import { executionForStep } from '../utils/executionProgress'
import { executionEtaSeconds } from '../utils/executionEta'
import { dubbingTaskState } from '../utils/dubbingTaskState'
import WorkflowStageNavigation from '../components/WorkflowStageNavigation.vue'
import LlmConversationPanel from '../components/LlmConversationPanel.vue'
import { stepConversationState, formatLlmEventTime } from '../utils/llmConversation'
import ContentReviewPanel from '../components/ContentReviewPanel.vue'
import FreshnessDetails from '../components/FreshnessDetails.vue'
import BatchWorkflowResume from '../components/BatchWorkflowResume.vue'
import { stoppingLabel } from '../utils/progressActivity'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { dependencyActionLabel, runPlannedSteps } from '../utils/runPlan'
import { createDraftLeaveGuard } from '../utils/draftLeaveGuard'
import { showStepError, stepDisplayStatus } from '../utils/stepFeedback'
import { WORKFLOW_BUSY } from '../utils/workflow'
import { commentFailure, commentWorkspaceLink } from '../utils/commentWorkflow'
// 步骤专属页: 说明 / 状态 / 依赖门禁 / 本步配置 / 完整日志 / 产物清单 / 运行操作
// 由左侧流程导航进入, 一页只专注一个步骤; 依赖未就绪时明确指出并禁用运行
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NDropdown, NDescriptions, NDescriptionsItem, NEmpty, NForm, NFormItem,
  NGrid, NGridItem as NGi, NH1, NLayoutContent, NPopover, NSpace, NSpin, NTag, NText,
  NTooltip, NSelect, useDialog, useMessage,
} from 'naive-ui'
import MediaInfo from '../components/MediaInfo.vue'
import VideoPreview from '../components/VideoPreview.vue'
import AudioTracksPlayer from '../components/AudioTracksPlayer.vue'
import AssemblyVersionWorkspace from '../components/AssemblyVersionWorkspace.vue'
import AssemblyDeliveryPanel from '../components/AssemblyDeliveryPanel.vue'
import VideoInfoPanel from '../components/VideoInfoPanel.vue'
import ProjectParameterForm from '../components/ProjectParameterForm.vue'
import TranslationContextPanel from '../components/TranslationContextPanel.vue'
import { SUBTITLE_STYLE_PATHS, subtitleSettingsLink, subtitleStyleSummary } from '../utils/subtitleWorkspace'
import { mapOutputCutChapters } from '../utils/outputCuts'
import StepHeader from '../components/StepHeader.vue'
import WorkflowPanel from '../components/WorkflowPanel.vue'
import AutoRunBadge from '../components/AutoRunBadge.vue'
import DubbingPanel from '../components/DubbingPanel.vue'
import DependencyNotice from '../components/DependencyNotice.vue'
import { fmtSize } from '../utils/format'
import { formatTimecode } from '../utils/timecode'
import { outputChoice, type AssemblyChoice } from '../utils/actionScope'
import { useNavStore, STEP_ORDER, STEP_LABELS, STATUS_TAG, STATUS_TEXT, STATUS_HINTS,
         FRESHNESS_HINTS, formatEta } from '../stores/nav'
import { createLatestRefresh, finalVideoKind, finalVideoLabel, latestFinalVideo, releaseMediaIn, vReleaseMedia } from '../utils/preview'
import { wrapCueText } from '../utils/subtitlePreview'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()

const uid = route.params.uid as string
const key = route.params.key as string
const detailSection = ref(['tts','assemble'].includes(key) ? 'logs' : 'parameters')
const assemblyReviewExpanded=ref(false)
const pageVisible = ref(typeof document === 'undefined' || document.visibilityState !== 'hidden')
const llmSection = ref<HTMLElement|null>(null)
watch(()=>route.hash,value=>{if(value==='#llm-conversation')detailSection.value='llm'},{immediate:true})

const projectExecution = ref<InstanceType<typeof WorkflowPanel>|null>(null)
const localRunOptions = computed(()=>[...(key==='assemble'?[]:[{label:'运行到此步…',key:'through'}]),{label:'从此继续项目…',key:'continue'}])
async function localRunAction(action:string) {
  if(nav.archived || nav.busy || nav.dirMissing || busy.value || dubbingBusy.value)return
  if(parametersDirty.value || dubbingDirty.value || reviewDirty.value || reviewBusy.value || assemblyPending.value){message.warning('请先保存当前步骤的修改，再检查运行方案');return}
  if(action==='through')await runWithDependencies()
  else if(action==='continue')await projectExecution.value?.open('',key)
}
const step = computed(() => nav.stepMap[key])
const execution = computed(() => {
  const value = executionForStep(step.value)
  if (!value) return null
  const extra = key === 'download' ? downloadProgressText.value : ''
  return extra ? {...value, detail:[value.detail,extra].filter(Boolean).join(' · ')} : value
})
const displayStatus = computed(() => step.value ? stepDisplayStatus(step.value) : 'pending')
const stepIdx = STEP_ORDER.indexOf(key as any)
const dubbingBusy = ref(false), dubbingDirty = ref(false)
const dubbingPanel = ref<InstanceType<typeof DubbingPanel> | null>(null)
const dubbingDocumentLines = ref<number | null>(null)
const dubbingTaskSummary = computed(() => dubbingTaskState(step.value?.synthesis_task, dubbingDocumentLines.value,
  step.value?.job_id ?? step.value?.execution?.job_id))
function dubbingContentLoaded(value: { uid: string; lines: number }) {
  if (value.uid === uid) dubbingDocumentLines.value = value.lines
}
const stepParametersDirty = ref(false), translationDirty = ref(false), translationBusy = ref(false)
const parametersDirty = computed(() => stepParametersDirty.value || translationDirty.value || translationBusy.value)
const reviewDirty = ref(false), reviewBusy = ref(false)
const assemblyDirty = ref(false), assemblyBusy = ref(false)
const assemblyPending = computed(() => assemblyDirty.value || assemblyBusy.value)
const assemblyWorkspace = ref<InstanceType<typeof AssemblyVersionWorkspace>|null>(null)
const projectParametersRefresh = ref(0), assemblySettingsRefresh = ref(0)
function assemblySettingsSaved() { projectParametersRefresh.value++; refreshInBackground(() => nav.poll()) }
function stepParametersSaved() { if (key === 'assemble') assemblySettingsRefresh.value++; refreshInBackground(() => nav.poll()) }
function reviewBeforeUnload(event: BeforeUnloadEvent) {
  if (reviewDirty.value || reviewBusy.value || translationDirty.value || translationBusy.value) { event.preventDefault(); event.returnValue = '' }
}
onMounted(() => window.addEventListener('beforeunload', reviewBeforeUnload))
onUnmounted(() => window.removeEventListener('beforeunload', reviewBeforeUnload))
const draftLeave = createDraftLeaveGuard(() => ({
  busyReason: translationBusy.value ? '正在读取或保存翻译背景，请稍候再离开'
    : reviewBusy.value ? '正在保存或刷新内容审查，请稍候再离开' : '',
  dirty: translationDirty.value || reviewDirty.value,
  title: translationDirty.value ? '翻译背景尚未保存' : '审查设置尚未保存',
  content: translationDirty.value ? '离开会放弃当前背景与术语修改。' : '离开会放弃当前审查设置修改。',
}), text => message.warning(text), options => { dialog.warning(options) })
onBeforeRouteLeave(draftLeave.leave)
onBeforeRouteUpdate(draftLeave.update)
const outputsArea = ref<HTMLElement | null>(null)
const assemblyChoice = ref<AssemblyChoice>(outputChoice('src'))
function chooseOutputs() {
  outputsArea.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  outputsArea.value?.focus({ preventScroll: true })
}
// 后端按当前设置解析所需文件，界面与执行器使用相同规则。
const blockers = computed(() => step.value?.unready_inputs ?? [])
// 字幕与配音是同一个编辑页；上游变化后把导出文件和逐句音频一起带到最新。
const editorBlockers = computed(() => {
  if (key !== 'tts') return blockers.value
  const seen = new Set<string>()
  return [...blockers.value, ...(nav.stepMap.subtitles?.unready_inputs ?? [])]
    .filter((item: any) => {
      const id = `${item.step}/${item.name}`
      if (seen.has(id)) return false
      seen.add(id)
      return true
    })
})
const actionBlockers = computed(() => key === 'tts' ? editorBlockers.value : blockers.value)
async function runWithDependencies() {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再运行。'); return }
  if (nav.busy) return
  if (parametersDirty.value || dubbingDirty.value || reviewDirty.value || reviewBusy.value || assemblyPending.value) { message.warning('请先保存参数、审查与成片剪辑设置，再确认运行范围'); return }
  const targets = key === 'tts' ? ['subtitles', 'tts'] : [key]
  await runPlannedSteps(uid, targets, true, message, dialog,
    async (steps) => {
      if (key !== 'tts' || steps.some(k => ['download', 'preprocess'].includes(k))) {
        releaseMediaIn(previewRoot.value); previewSuspended.value = true; await nextTick()
      }
    },
    async () => { previewSuspended.value = false; await nav.poll() }, () => nav.archived || assemblyPending.value)
}
const running = computed(() => step.value?.status === 'running' || step.value?.status === 'queued')
const canRun = computed(() => !!step.value && !running.value && !nav.archived
  && blockers.value.length === 0 && !dirMissing.value && !dubbingBusy.value && !dubbingDirty.value && !parametersDirty.value && !reviewDirty.value && !reviewBusy.value && !assemblyPending.value)

// 项目目录不在磁盘上 (被移走/删除) → 禁用运行, 从回收站还原后恢复
const dirMissing = computed(() => nav.dirMissing)

const busy = ref(false)
const previewRoot = ref<HTMLElement | null>(null)
const previewSuspended = ref(false)
const cancelPending = ref(false)
const pauseBusy = ref(false)
const forceBusy = ref(false)
const dialogAction = ref<'stop' | 'force' | 'rerun' | null>(null)

// 顶部操作区只允许存在一种状态分支。集中计算后用互斥模板渲染，
// 避免停止信号和后端轮询不同步时同时出现两个结束/运行按钮。
const actionMode = computed<
  'checking' | 'running' | 'cancelling' | 'deps' | 'chain-hold' | 'fill-voice' | 'fresh-done' | 'run'
>(() => {
  if (step.value?.status === 'checking') return 'checking'
  if (running.value && (step.value?.cancelling || cancelPending.value)) return 'cancelling'
  if (running.value) return 'running'
  // 链挂起在本步 (失败/停止/中断): 重试默认续跑剩余计划 (ADR-0072)。
  if (nav.chainHeldAt(key)) return 'chain-hold'
  if (key === 'tts' ? editorBlockers.value.length : blockers.value.length) return 'deps'
  if (key === 'tts' && step.value?.status === 'done' && !step.value.fresh) return 'fill-voice'
  if (step.value?.status === 'done' && step.value.fresh) return 'fresh-done'
  return 'run'
})

// 取消请求在后端完成前保持锁定；步骤离开 running/queued 后自动释放，
// 防止快速双击把同一个控制动作提交两次。
watch(() => [step.value?.status, step.value?.cancelling] as const, ([status, cancelling]) => {
  if (status !== 'running' && status !== 'queued') cancelPending.value = false
  // 后端已确认取消后释放本地请求锁; 此时才显示升级到强制结束的入口。
  else if (cancelling) cancelPending.value = false
})

async function run(force = false, resumeChain = false) {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再运行。'); return }
  if (nav.busy) return
  if (busy.value || cancelPending.value || forceBusy.value || dubbingBusy.value || dubbingDirty.value || parametersDirty.value || reviewDirty.value || reviewBusy.value || assemblyPending.value) return
  if (key === 'assemble') { chooseOutputs(); return }
  busy.value = true
  if (key !== 'tts') {
    releaseMediaIn(previewRoot.value)
    previewSuspended.value = true
    await nextTick()
  }
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force, resume_chain: resumeChain }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { message.error(j.message || `启动失败 (HTTP ${r.status})`); return }
    if (j.status === 'skipped') message.info(j.message)
    else message.success(resumeChain ? '已开始重试, 成功后继续剩余步骤' : force ? '已开始强制重跑' : '已启动')
    nav.stepStarted(uid, key, j)
    refreshInBackground(() => nav.poll())
    loadLog()
  } catch { message.error('无法连接后端服务（可能正在重启或更新），请稍候重试；恢复后可「重试并继续」') } finally {
    busy.value = false
    previewSuspended.value = false
  }
}

function rerunDialog() {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再运行。'); return }
  if (busy.value || dialogAction.value) return
  if (key === 'tts') { void dubbingPanel.value?.regenerateAll(); return }
  dialogAction.value = 'rerun'
  const downstream = nav.steps.filter(s => (step.value?.affected_steps ?? []).includes(s.key))
    .filter((s: any) => s.status === 'done' || s.status === 'stale')
  const resumeSplitCache = key === 'split' && ['failed', 'interrupted'].includes(String(step.value?.status || ''))
  const extra = resumeSplitCache
      ? '这是失败恢复：会重新校验并写入最终产物，已通过校验的语义分块按输入和参数指纹复用；未命中的分块重新请求模型。外部原文件不删除。'
    : '将清理本步骤可重建的中间产物并重新生成，使用已保存的参数；外部原文件不删除。'
  dialog.warning({
    title: `确认重做「${step.value.label}」`,
    content: extra + (downstream.length
      ? `下游 ${downstream.length} 个步骤将被标记为「已过期」（需要重跑）。`
      : ''),
    positiveText: '确认重做',
    negativeText: '取消',
    onPositiveClick: async () => {
      dialogAction.value = null
      await run(true)
    },
    onNegativeClick: () => { dialogAction.value = null },
    onClose: () => { dialogAction.value = null },
  })
}

// ---- 停止 / 强制结束 / 暂停 (都有防误触确认) ----
const pausing = computed(() => !!step.value?.pausing)
const paused = computed(() => displayStatus.value === 'paused')

function stopDialog() {
  if (cancelPending.value || dialogAction.value) return
  const targetJobId = step.value?.job_id ?? null
  dialogAction.value = 'stop'
  dialog.warning({
    title: `停止「${step.value.label}」？`,
    content: '正在等待的 LLM 请求会主动中断并关闭本地连接；服务端是否停止计算取决于服务商。'
      + '下载/ffmpeg 等子进程将终止；进程内计算会在当前单元结束后退出。'
      + '过程临时文件自动清理，已完成的正式产物保留。',
    positiveText: '确认停止',
    negativeText: '继续运行',
    onPositiveClick: async () => {
      dialogAction.value = null
      await doCancel(targetJobId)
    },
    onNegativeClick: () => { dialogAction.value = null },
    onClose: () => { dialogAction.value = null },
  })
}

async function doCancel(expectedJobId: number | null = null) {
  if (cancelPending.value) return
  cancelPending.value = true
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/cancel`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: expectedJobId }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { cancelPending.value = false; message.error(j.message || '停止信号发送失败'); return }
    if (j.status === 'stale_action' || j.status === 'not_running') {
      cancelPending.value = false
      message.info(j.message || '步骤已经结束，无需停止')
      refreshInBackground(() => nav.poll())
      return
    }
    if (j.status === 'already_cancelling') {
      message.info('该步骤已经在停止中，请等待它退出')
      return
    }
  } catch { cancelPending.value = false; message.error('停止信号发送失败（后端可能正在重启），请稍候重试'); return }
  message.info('已请求停止，将取消当前网络请求并安全收尾；长时间无响应可用「强制结束」')
}

function forceDialog() {
  if (forceBusy.value || dialogAction.value) return
  const targetJobId = step.value?.job_id ?? null
  dialogAction.value = 'force'
  dialog.error({
    title: `强制结束「${step.value.label}」？`,
    content: '仅建议在「取消中」长时间无响应时使用：子进程立即终止，状态立刻按已取消呈现；'
      + 'LLM 网络请求也会取消，但这不代表服务端已确认停止；进程内计算若仍在后台收尾，其结果将被自动丢弃。',
    positiveText: '强制结束',
    negativeText: '返回',
    onPositiveClick: async () => {
      dialogAction.value = null
      await doForce(targetJobId)
    },
    onNegativeClick: () => { dialogAction.value = null },
    onClose: () => { dialogAction.value = null },
  })
}

async function doForce(expectedJobId: number | null = null) {
  if (forceBusy.value) return
  forceBusy.value = true
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/force_stop`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: expectedJobId }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { message.error(j.message || '强制结束失败'); return }
    if (j.status === 'stale_action' || j.status === 'not_running'
        || j.status === 'already_force_stopped') {
      message.info(j.message || '步骤已经结束，无需强制结束')
      refreshInBackground(() => nav.poll())
      return
    }
    message.success('已强制结束')
    refreshInBackground(() => nav.poll())
  } catch { message.error('强制结束请求失败（后端可能正在重启），请稍候重试'); return }
  finally { forceBusy.value = false }
}

async function setPause(p: boolean) {
  if (pauseBusy.value || cancelPending.value) return
  pauseBusy.value = true
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/pause`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pause: p, job_id: step.value?.job_id ?? null }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { message.error(j.message || (p ? '暂停失败' : '继续失败')); return }
    if (j.status === 'stale_action' || j.status === 'not_running') {
      message.info(j.message || '步骤已经结束，无需操作')
      refreshInBackground(() => nav.poll())
      return
    }
  } catch { message.error(p ? '暂停请求失败（后端可能正在重启），请稍候重试' : '继续请求失败（后端可能正在重启），请稍候重试'); return }
  finally { pauseBusy.value = false }
  refreshInBackground(() => nav.poll())
}

async function revealStep() {
  const r = await fetch(`/api/projects/${uid}/steps/${key}/reveal`, { method: 'POST' })
  if (!r.ok) message.error('打开目录失败')
}

async function revealArt(stepKey: string, name: string) {
  const r = await fetch(
    `/api/projects/${uid}/steps/${stepKey}/artifacts/${encodeURIComponent(name)}/reveal`,
    { method: 'POST' })
  if (!r.ok) message.error('定位失败 (文件可能已不在磁盘)')
}

// ---- 文件清单: 上游文件 / 计划产物 / 实际产物 ----
function upInfo(inp: { step: string, name: string, ready: boolean }) {
  const up = nav.stepMap[inp.step]
  const art = up?.artifacts?.find((a: any) => a.name === inp.name)
  return { ok: inp.ready, size: art?.size, label: up?.label ?? inp.step }
}
function producedCount(pattern: string): number | null {
  pattern = pattern.replace('<句id>', '*')
  if (!pattern.includes('*')) return null
  const re = new RegExp('^'
    + pattern.split('*').map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$')
  return (step.value?.artifacts ?? []).filter((a: any) => re.test(a.name)).length
}
function isProduced(name: string): boolean {
  const n = producedCount(name)
  return n === null
    ? (step.value?.artifacts ?? []).some((a: any) => a.name === name)
    : n > 0
}

// ---- 日志: 运行态 1.5s 轮询；新增内容平滑逐字显示，贴底才自动跟随 ----
const logLines = ref<string[]>([])
const logTruncated = ref(false)
const logDisplayText = ref('')
const logBox = ref<HTMLElement | null>(null)
let logTimer: number | undefined
let logTargetText = ''
let logHistoryLoaded = false
let logAnimationTimer: number | undefined
let logAnimationLastAt = 0
let logAnimationPosition = 0
let logAnimationVelocity = 0
const logFollow = ref(true)

function onLogScroll() {
  const el = logBox.value
  if (!el) return
  logFollow.value = el.scrollHeight - el.scrollTop - el.clientHeight <= 24
}

function followLogOutput() {
  if (!logFollow.value) return
  nextTick(() => {
    const el = logBox.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function stopLogAnimation() {
  if (logAnimationTimer !== undefined) {
    window.cancelAnimationFrame(logAnimationTimer)
    logAnimationTimer = undefined
  }
}

function animateLogText() {
  const target = logTargetText
  const current = logDisplayText.value
  if (current.length >= target.length) {
    if (current !== target) logDisplayText.value = target
    stopLogAnimation()
    return
  }
  const now = performance.now()
  const elapsed = Math.min(80, Math.max(0, now - (logAnimationLastAt || now))) / 1000
  logAnimationLastAt = now
  let position = Math.max(current.length, logAnimationPosition)
  if (position > target.length) position = target.length
  const distance = target.length - position
  const referenceDistance = 500
  const referenceSpeed = 360
  const targetSpeed = referenceSpeed * Math.min(1, Math.sqrt(distance / referenceDistance))
  const acceleration = (targetSpeed - logAnimationVelocity) * 8
  logAnimationVelocity = Math.max(0, Math.min(referenceSpeed,
    logAnimationVelocity + acceleration * elapsed))
  position = Math.min(target.length, position + logAnimationVelocity * elapsed)
  if (distance > 0 && logAnimationVelocity < 0.2) {
    position = Math.min(target.length, position + Math.min(0.5, distance))
  }
  logAnimationPosition = position
  const visibleLength = Math.floor(position)
  if (visibleLength > current.length) {
    logDisplayText.value = target.slice(0, visibleLength)
    followLogOutput()
  }
  if (position >= target.length && logAnimationVelocity < 0.5) {
    logDisplayText.value = target
    stopLogAnimation()
    return
  }
  logAnimationTimer = window.requestAnimationFrame(animateLogText)
}

function syncLogAnimation(lines: string[]) {
  const target = lines.join('\n')
  const previousTarget = logTargetText
  logTargetText = target
  stopLogAnimation()
  if (!logHistoryLoaded || !running.value || !pageVisible.value || detailSection.value !== 'logs') {
    logHistoryLoaded = true
    logDisplayText.value = target
    logAnimationPosition = target.length
    logAnimationVelocity = 0
    followLogOutput()
    return
  }
  const displayed = logDisplayText.value
  if (previousTarget && target.startsWith(previousTarget)) {
    logAnimationPosition = previousTarget.length
  } else if (displayed && target.startsWith(displayed)) {
    logAnimationPosition = displayed.length
  } else {
    logDisplayText.value = target
    logAnimationPosition = target.length
    logAnimationVelocity = 0
    followLogOutput()
    return
  }
  logAnimationVelocity = 0
  if (logAnimationPosition >= target.length) {
    logDisplayText.value = target
    followLogOutput()
    return
  }
  animateLogText()
}

async function loadLog() {
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/log?tail=500`,
      { cache: 'no-store' })
    if (!r.ok) return
    const j = await r.json()
    logLines.value = j.lines ?? []
    logTruncated.value = !!j.truncated
    syncLogAnimation(logLines.value)
  } catch { /* 后端瞬断: 下轮轮询再取 */ }
}
function syncLogPolling() {
  const need = running.value
  if (need && !logTimer) {
    loadLog()
    logTimer = window.setInterval(loadLog, 1500)
  } else if (!need) {
    if (logTimer) {
      clearInterval(logTimer)
      logTimer = undefined
    }
    stopLogAnimation()
    loadLog()                    // 初始挂载 / 运行收尾: 拉当前日志
  }
}
watch(running, syncLogPolling, { immediate: true })

// ---- LLM 实时会话: 仅翻译/语义整理步骤显示，按请求聚合发送内容、思考和模型输出 ----
const LLM_TRACE_POLL_MS = 400
const llmStep = computed(() => key === 'split' || key === 'translate')
const splitSemanticDescription = computed(() => {
  const cfg = step.value?.cfg?.split ?? {}
  const targetValue = Number(cfg.llm_chunk_target_words ?? 500)
  const target = Math.max(100, Math.round(Number.isFinite(targetValue) ? targetValue : 500))
  const overlap = Math.max(0, Math.round(Number(cfg.llm_chunk_overlap_words ?? 80)))
  const retryValue = Number(cfg.llm_chunk_max_retries ?? 3)
  const retries = Math.min(5, Math.max(1, Math.round(Number.isFinite(retryValue) ? retryValue : 3)))
  return `短稿一次整理；长稿自动按约${target}词分块，块间保留约${overlap}词上下文；每块独立校验，最多尝试${retries}次。`
})
const llmReferenceDistance = ref(500)
const llmReferenceSpeed = ref(360)
let llmDisplayRevision = 0
function applyLlmDisplayChange(value: unknown) {
  if (!llmStep.value || llmDisposed || typeof value !== 'number' || !Number.isFinite(value)) return
  llmDisplayRevision++
  llmReferenceDistance.value = Math.max(50, Math.min(5000, Math.round(value)))
}
function onLlmDisplayChanged(event: Event) {
  applyLlmDisplayChange((event as CustomEvent)?.detail?.reference_distance_chars)
}
function onLlmDisplayStorage(event: StorageEvent) {
  if (event.key !== 'dubforge:llm-display-changed' || !event.newValue) return
  try { applyLlmDisplayChange(JSON.parse(event.newValue)?.reference_distance_chars) }
  catch { /* 其它窗口的无效存储数据不改变显示参数 */ }
}
async function loadLlmDisplayConfig() {
  if (!llmStep.value) return
  const revision = llmDisplayRevision
  try {
    const r = await fetch('/api/settings/llm_display', { cache: 'no-store' })
    if (!r.ok || llmDisposed) return
    const data = await r.json()
    if (llmDisposed || revision !== llmDisplayRevision) return
    llmReferenceDistance.value = Math.max(50, Math.min(5000,
      Number(data.reference_distance_chars) || 500))
    llmReferenceSpeed.value = Math.max(60, Math.min(1000,
      Number(data.reference_speed_chars_per_second) || 360))
  } catch { /* 显示参数读取失败时使用内置默认值 */ }
}
onMounted(() => {
  window.addEventListener('dubforge:llm-display-changed', onLlmDisplayChanged)
  window.addEventListener('storage', onLlmDisplayStorage)
  void loadLlmDisplayConfig()
})
onUnmounted(() => {
  window.removeEventListener('dubforge:llm-display-changed', onLlmDisplayChanged)
  window.removeEventListener('storage', onLlmDisplayStorage)
})
const llmEvents = ref<any[]>([])
let llmCursor = 0
let llmTraceId = ''
let llmLoading = false
let llmTimer: number | undefined
const llmRestoringHistory = ref(true)
const llmTraceActive = ref<boolean | null>(null)
let llmDisposed = false
let llmCatchupTimer: number | undefined
let llmAbortController: AbortController | undefined
let llmRefreshPending = false
const llmCanAnimate = computed(() => running.value && llmTraceActive.value !== false
  && pageVisible.value && detailSection.value === 'llm' && !llmRestoringHistory.value)
const llmOutputEls = new Map<string, HTMLElement>()
const llmOutputFollow = new Map<string, boolean>()
const llmAnimatedContent = ref<Record<string, string>>({})
const llmAnimationTimers = new Map<string, number>()
const llmAnimationLastAt = new Map<string, number>()
const llmAnimationPosition = new Map<string, number>()
const llmAnimationVelocity = new Map<string, number>()
let llmFollowTimer: number | undefined
function setLlmOutputEl(id: string, el: Element | null) {
  if (el instanceof HTMLElement) llmOutputEls.set(id, el)
  else llmOutputEls.delete(id)
}
function onLlmOutputScroll(id: string, event: Event) {
  const el = event.currentTarget as HTMLElement
  // 留出 24px 容差，用户只要已经贴近底部就继续跟随；主动向上查看时停止抢滚动。
  llmOutputFollow.set(id, el.scrollHeight - el.scrollTop - el.clientHeight <= 24)
}
function followLlmOutput() {
  // 文字动画每帧都会更新 DOM；滚动也每帧读写布局会造成轻微卡顿。
  // 合并到约 50ms 一次，视觉上仍然跟手，但不和文本渲染互相抢主线程。
  if (llmFollowTimer !== undefined) return
  llmFollowTimer = window.setTimeout(() => {
    llmFollowTimer = undefined
    nextTick(() => {
      for (const row of llmMessages.value) {
        if (!['assistant', 'reasoning'].includes(row.role) || llmOutputFollow.get(row.id) === false) continue
        const el = llmOutputEls.get(row.id)
        if (el) el.scrollTop = el.scrollHeight
      }
    })
  }, 50)
}
function displayLlmContent(row: any): string {
  if (!['assistant', 'reasoning'].includes(row.role)) return row.content
  if (!llmCanAnimate.value || !llmRowStreaming(row)) return row.content
  // 动画缓存是渐进显示层；若 watcher/历史恢复没有及时填充，则直接回退真实
  // 内容，绝不能让已经完成的思考或输出一直显示“等待内容”。
  return llmAnimatedContent.value[row.id] ?? row.content
}
function llmRowStreaming(row: any): boolean {
  return !row.aborted && row.elapsedMs == null && (row.role === 'assistant'
    ? row.status === '生成中' : row.role === 'reasoning' && row.status === '思考中')
}
function showLlmRowImmediately(row: any) {
  stopLlmAnimation(row.id)
  llmAnimatedContent.value[row.id] = String(row.content || '')
}
function stopLlmAnimation(id?: string) {
  if (id) {
    const timer = llmAnimationTimers.get(id)
    if (timer !== undefined) window.cancelAnimationFrame(timer)
    llmAnimationTimers.delete(id)
    llmAnimationLastAt.delete(id)
    llmAnimationPosition.delete(id)
    llmAnimationVelocity.delete(id)
    return
  }
  for (const timer of llmAnimationTimers.values()) window.cancelAnimationFrame(timer)
  llmAnimationTimers.clear()
  llmAnimationLastAt.clear()
  llmAnimationPosition.clear()
  llmAnimationVelocity.clear()
}
function animateLlmRow(id: string) {
  const row = llmMessages.value.find((item: any) => item.id === id)
  if (!row || !['assistant', 'reasoning'].includes(row.role)) {
    stopLlmAnimation(id)
    return
  }
  if (!llmCanAnimate.value || !llmRowStreaming(row)) {
    showLlmRowImmediately(row)
    return
  }
  const target = String(row.content || '')
  const current = String(llmAnimatedContent.value[id] || '')
  if (current.length >= target.length) {
    if (current !== target) llmAnimatedContent.value[id] = target
    stopLlmAnimation(id)
    return
  }
  // 把显示位置当成追赶者：距离最新内容越远，追赶速度目标越高；位置保留
  // 小数，只有整数部分变化时才更新 DOM，这样既能平滑追赶突发的一批文本，
  // 也不会一帧吐出整句。
  const now = performance.now()
  const previousAt = llmAnimationLastAt.get(id) ?? now
  const elapsed = Math.min(80, Math.max(0, now - previousAt)) / 1000
  llmAnimationLastAt.set(id, now)
  let position = llmAnimationPosition.get(id)
  if (position === undefined) position = current.length
  let velocity = llmAnimationVelocity.get(id) || 0
  if (position > target.length) {
    position = target.length
    velocity = 0
  }
  const distance = target.length - position
  // 统一的“速度弹簧”公式：
  //   v_target = referenceSpeed × sqrt(distance / referenceDistance)
  //   a = response × (v_target - v)
  // 距离达到设置值时，追赶速度目标正好等于参考生成速度；平方根曲线让
  // 接近末尾时不会像线性弹簧那样拖得太久；只平滑显示正在生成的新内容。
  // 历史、完成和页面重新可见时直接同步，不能假装模型还在输出。
  const referenceDistance = llmReferenceDistance.value
  const referenceSpeed = llmReferenceSpeed.value
  const response = 8
  const distanceRatio = Math.max(0, distance / referenceDistance)
  const targetSpeed = referenceSpeed * Math.min(1, Math.sqrt(distanceRatio))
  const maxSpeed = referenceSpeed
  const acceleration = (targetSpeed - velocity) * response
  velocity += acceleration * elapsed
  velocity = Math.max(0, Math.min(maxSpeed, velocity))
  position = Math.min(target.length, position + velocity * elapsed)
  if (distance > 0 && velocity < 0.2 && position < target.length) {
    // 极短文本或非常接近末尾时给弹簧一个最小推动，避免无限停在小数位。
    position = Math.min(target.length, position + Math.min(0.5, distance))
  }
  llmAnimationPosition.set(id, position)
  llmAnimationVelocity.set(id, velocity)
  const visibleLength = Math.floor(position)
  if (visibleLength > current.length) {
    llmAnimatedContent.value[id] = target.slice(0, visibleLength)
    followLlmOutput()
  }
  if (position >= target.length && velocity < 0.5) {
    llmAnimatedContent.value[id] = target
    stopLlmAnimation(id)
    return
  }
  llmAnimationTimers.set(id, window.requestAnimationFrame(() => animateLlmRow(id)))
}
function syncLlmAnimations() {
  const active = new Set<string>()
  for (const row of llmMessages.value) {
    if (!['assistant', 'reasoning'].includes(row.role)) continue
    active.add(row.id)
    if (!llmCanAnimate.value || !llmRowStreaming(row)) {
      showLlmRowImmediately(row)
      continue
    }
    const shown = llmAnimatedContent.value[row.id] || ''
    if (shown.length > row.content.length) {
      llmAnimatedContent.value[row.id] = ''
      llmAnimationPosition.delete(row.id)
      llmAnimationVelocity.delete(row.id)
    }
    if (shown.length !== row.content.length || llmAnimationTimers.has(row.id)) {
      if (!llmAnimationTimers.has(row.id)) animateLlmRow(row.id)
    }
  }
  for (const id of Object.keys(llmAnimatedContent.value)) {
    if (!active.has(id)) {
      stopLlmAnimation(id)
      delete llmAnimatedContent.value[id]
    }
  }
}
const LLM_VALIDATION_METHODS: Record<string, string> = {
  translate: '校验方法：逐行匹配“行号. 译文” → 行号必须是已知且顺序一致的子序列 → 缺行/空译文可保留有效行并局部重译 → 结构性格式错误不猜续行 → 校验并截断交接摘要',
  split: '校验方法：逐行匹配“序号. 开始秒-结束秒: 片段” → 连续编号、非空片段，兼容无害空白与注释 → 检查最终有效时间与真实长静音 → 检查明显大段遗漏、台词搬移或重复抄写 → 内容纠错变化提示复核',
}
const llmValidationMethod = () => LLM_VALIDATION_METHODS[key] || '校验方法：提取 JSON → 检查结构、字段与步骤业务规则'
const formatLlmTime = formatLlmEventTime
const llmMessageState = computed(() => stepConversationState(llmEvents.value, llmValidationMethod()))
const llmMessages = computed(() => llmMessageState.value.visibleRows)
watch([llmMessages, llmCanAnimate], syncLlmAnimations)
const llmRetryCount = computed(() =>
  Array.from(llmMessageState.value.retryInfo.values())
    .filter((info: any) => info.maxAttempt > 1).length)
const llmRoleLabel = (row: any) => ({
  system: '系统提示', history: '历史回复',
  user: row.requestKind === 'line-repair' ? '小片段重译请求'
    : row.requestKind === 'format-repair' ? '格式修复请求' : '发送内容',
  reasoning: '模型思考',
  assistant: '模型输出',
  strategy: '请求策略',
  aborted: '本地已中止响应',
  cancellation: '取消请求',
  json_repair: 'JSON 自修复',
  validation: row.kind === 'error'
    ? (String(row.content || '').startsWith('JSON 解析失败') ? 'JSON 解析失败' : '校验失败')
    : '校验通过',
  merge: '合并结果',
  fallback: '回退本地切分',
}[row.role] || row.role)
const llmRoleTagType = (row: any) => ({
  system: 'default', user: 'info', reasoning: 'warning', assistant: 'success',
  strategy: row.kind === 'fresh' ? 'warning' : 'info',
  aborted: 'error',
  json_repair: 'info',
  validation: row.kind === 'error' ? 'error' : 'success',
  merge: 'success',
  fallback: 'warning',
}[row.role] || 'default');

// 行头状态只用固定短标签，不直接显示后端 status 事件原文，避免闪烁和结束后残留。
const llmRowState = (row: any) => {
  if (row.aborted) return '本次候选未采用'
  if (row.status === '本地已取消') return row.status
  if (row.role === 'assistant') return row.status === '已完成' || row.elapsedMs != null
    ? '已完成' : running.value && llmTraceActive.value !== false ? '生成中' : '本轮已结束'
  if (row.role === 'reasoning') return row.status === '思考完成' || row.elapsedMs != null
    ? '思考完成' : running.value && llmTraceActive.value !== false ? '思考中' : '本轮已结束'
  return row.status || ''
}
const llmStatus = computed(() => {
  const assistant = [...llmMessages.value].reverse().find(
    (x: any) => !x.aborted && x.role === 'assistant' && x.status === '生成中')
  const reasoning = [...llmMessages.value].reverse().find(
    (x: any) => !x.aborted && x.role === 'reasoning' && x.status === '思考中')
  if (assistant) return `生成中 · ${(assistant.content || '').length.toLocaleString()} 字符`
  if (reasoning) return `思考中 · ${(reasoning.content || '').length.toLocaleString()} 字符`
  const latestRequest = [...llmMessages.value].reverse().find((x: any) =>
    x.requestId && ['system', 'user', 'assistant', 'reasoning', 'aborted', 'cancellation'].includes(x.role))
  if (latestRequest?.aborted) return '本次候选未采用'
  if (running.value) return '正在准备…'
  return ''
})
async function loadLlmTrace() {
  if (!llmStep.value || llmDisposed || !pageVisible.value || detailSection.value !== 'llm') return
  if (llmLoading) { llmRefreshPending = true; return }
  llmLoading = true
  llmRefreshPending = false
  const controller = new AbortController()
  llmAbortController = controller
  let continueHistory = false
  try {
    const previousCursor = llmCursor
    const query = new URLSearchParams({ tail: '500', after: String(llmCursor), trace_id: llmTraceId })
    const r = await fetch(`/api/projects/${uid}/steps/${key}/llm_trace?${query}`,
      { cache: 'no-store', signal: controller.signal })
    if (!r.ok) return
    const j = await r.json()
    if (llmDisposed || controller.signal.aborted) return
    if (j.retry) return
    if (j.reset || !j.exists) {
      llmRestoringHistory.value = true
      llmEvents.value = []
      llmOutputFollow.clear()
      stopLlmAnimation()
      llmAnimatedContent.value = {}
    }
    if (typeof j.active === 'boolean') llmTraceActive.value = j.active
    else if (!j.exists) llmTraceActive.value = false
    llmCursor = j.cursor || 0
    llmTraceId = j.trace_id || ''
    if (Array.isArray(j.events) && j.events.length) llmEvents.value.push(...j.events)
    // 首次进入/刷新后的全部历史分页直接显示，不重新播放打字动画。
    // 在 Vue 刷新视图及动画监听执行前填好已显示内容；追完历史后，
    // 后续轮询才从这个位置平滑追加新字符。
    const more = typeof j.more === 'boolean' ? j.more : j.events?.length === 500
    if (llmRestoringHistory.value || !llmCanAnimate.value) {
      for (const row of llmMessages.value) {
        if (row.role === 'assistant' || row.role === 'reasoning') {
          showLlmRowImmediately(row)
        }
      }
    }
    if (!more) llmRestoringHistory.value = false
    // 页面初次打开或任务刚结束时也要读完积压的所有页。
    // more 由字节游标决定，不能按有效事件数量猜测。半行尚未写完时不空转。
    continueHistory = more && (j.reset || llmCursor > previousCursor)
  } catch { /* 后端瞬断: 下轮轮询再取 */ }
  finally {
    llmLoading = false
    if (llmAbortController === controller) llmAbortController = undefined
    if (!llmDisposed && pageVisible.value && detailSection.value === 'llm'
        && (continueHistory || llmRefreshPending) && llmCatchupTimer === undefined) {
      llmRefreshPending = false
      llmCatchupTimer = window.setTimeout(() => {
        llmCatchupTimer = undefined
        void loadLlmTrace()
      }, 0)
    }
  }
}
function syncLlmPolling() {
  if (!llmStep.value) return
  if (!pageVisible.value || detailSection.value !== 'llm' || llmDisposed) {
    if (llmTimer !== undefined) window.clearInterval(llmTimer)
    llmTimer = undefined
    if (llmCatchupTimer !== undefined) window.clearTimeout(llmCatchupTimer)
    llmCatchupTimer = undefined
    llmRefreshPending = false
    llmAbortController?.abort()
    stopLlmAnimation()
    return
  }
  if (running.value && !llmTimer) {
    loadLlmTrace()
    // 轮询间隔适中，避免过密读取让每一批模型内容的到达节奏更明显；
    // 请求仍由 llmLoading 串行保护，不会因刷新堆积多个读取请求。
    llmTimer = window.setInterval(loadLlmTrace, LLM_TRACE_POLL_MS)
  } else if (!running.value) {
    if (llmTimer) {
      clearInterval(llmTimer)
      llmTimer = undefined
    }
    loadLlmTrace()
  }
}
watch(running, syncLlmPolling, { immediate: true })
function onTraceVisibilityChanged() {
  if (llmDisposed) return
  pageVisible.value = typeof document === 'undefined' || document.visibilityState !== 'hidden'
  llmRestoringHistory.value = true
  syncLlmAnimations()
  syncLogAnimation(logLines.value)
  syncLlmPolling()
}
watch(detailSection, (section, previous) => {
  if (section === 'llm' && previous !== 'llm') {
    llmRestoringHistory.value = true
    syncLlmAnimations()
  }
  if (section === 'logs') {
    logHistoryLoaded = false
    syncLogAnimation(logLines.value)
  }
  syncLlmPolling()
})
onMounted(() => {
  document.addEventListener('visibilitychange', onTraceVisibilityChanged)
})
function disposeLlmTrace() {
  llmDisposed = true
  if (logTimer) clearInterval(logTimer)
  stopLogAnimation()
  if (llmTimer) clearInterval(llmTimer)
  if (llmCatchupTimer !== undefined) window.clearTimeout(llmCatchupTimer)
  llmCatchupTimer = undefined
  llmRefreshPending = false
  llmAbortController?.abort()
  if (llmFollowTimer !== undefined) window.clearTimeout(llmFollowTimer)
  stopLlmAnimation()
  if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onTraceVisibilityChanged)
}
onUnmounted(disposeLlmTrace)

// ---- 本步配置快照 (展平一层) ----
const cfgRows = computed(() => {
  const cfg = step.value?.cfg ?? {}
  const rows: { k: string, v: string }[] = []
  for (const [k, v] of Object.entries(cfg)) {
    if (v && typeof v === 'object') {
      for (const [k2, v2] of Object.entries(v as any)) {
        rows.push({ k: `${k}.${k2}`, v: Array.isArray(v2) ? `${v2.length} 项` : String(v2) })
      }
    } else {
      rows.push({ k, v: v === '' || v === null ? '(空)' : String(v) })
    }
  }
  return rows
})

// 参数表单与试配页共用；草稿仅用于本页 ASR 分段预览。
const draft = ref<Record<string, any>>({})
const editablePaths = ref<string[]>([])
const extraCfgRows = computed(() => cfgRows.value.filter(row => !editablePaths.value.includes(row.k)
  && !(key === 'assemble' && [...SUBTITLE_STYLE_PATHS, 'assemble.burn_subtitles'].includes(row.k))))

// ---- download 页媒体信息 (视频右侧信息栏) ----
const media = ref<any>({ found: false })
// 视频资料面板与原视频预览共享同一份只读章节快照；章节仅用于本地跳转标记，
// 发布到 B 站仍由发布设置中的显式章节选项控制。
const previewChapters = ref<any[]>([])
const downloadInfo = ref<any | null>(null)
const downloadChapters = computed(() => key === 'download'
  ? (downloadInfo.value?.content?.chapters ?? [])
  : previewChapters.value)
const downloadDuration = computed(() => Number(downloadInfo.value?.source?.duration)
  || Number(media.value?.duration) || 0)
const downloadProgressDetail = computed(() => key === 'download'
  ? (step.value?.progress_detail || {}) : {})
function formatDownloadRate(value: unknown): string {
  const rate = Number(value)
  if (!Number.isFinite(rate) || rate <= 0) return ''
  if (rate >= 1024 * 1024) return `${(rate / (1024 * 1024)).toFixed(1)} MB/s`
  if (rate >= 1024) return `${(rate / 1024).toFixed(1)} KB/s`
  return `${Math.round(rate)} B/s`
}
const downloadProgressText = computed(() => {
  const detail = downloadProgressDetail.value
  const loaded = Number(detail.loaded), total = Number(detail.total)
  const parts: string[] = []
  const rate = formatDownloadRate(detail.speed_bps)
  if (rate) parts.push(`下载速度 ${rate}`)
  if (Number.isFinite(loaded) && loaded >= 0 && Number.isFinite(total) && total > 0) {
    parts.push(`${fmtSize(loaded)} / ${fmtSize(total)}`)
  }
  return parts.join(' · ')
})
let mediaFailed = false
let mediaSignature = ''
const mediaLoader = createLatestRefresh(async (signal) => {
  const r = await fetch(`/api/projects/${uid}/media`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取媒体信息失败')
  return r.json()
}, (value) => { media.value = value; mediaFailed = !value.found }, () => { mediaFailed = true })
const chapterLoader = createLatestRefresh(async (signal) => {
  const r = await fetch(`/api/projects/${uid}/steps/download/video-info`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取视频章节失败')
  const value = await r.json()
  const rows = value?.info?.content?.chapters
  return Array.isArray(rows) ? rows : []
}, value => { previewChapters.value = value }, () => { previewChapters.value = [] })
if (key === 'download') {
  watch(step, (value) => {
    if (value?.status !== 'done') {
      mediaLoader.cancel()
      media.value = { found: false }
      mediaSignature = ''
      return
    }
    const signature = (value.artifacts ?? []).map((a: any) => `${a.name}:${a.sha256 || a.size || ''}`).join('|')
    if (signature !== mediaSignature || mediaFailed || !media.value.found) {
      const changed = signature !== mediaSignature
      mediaSignature = signature
      void mediaLoader.refresh(changed)
    }
  }, { immediate: true })
}
if (key !== 'download') {
  const readChapterSignature = () => (nav.stepMap.download?.artifacts ?? [])
    .map((a: any) => `${a.name}:${a.sha256 || a.size || ''}`).join('|')
  watch(readChapterSignature, (signature, previous) => {
    if (!signature) { chapterLoader.cancel(); previewChapters.value = []; return }
    if (signature !== previous || !previewChapters.value.length) void chapterLoader.refresh(signature !== previous)
  }, { immediate: true })
}
onUnmounted(() => { mediaLoader.cancel(); chapterLoader.cancel() })

// ---- 产出预览: 按步骤类型直接预览对应产物 (视频/音频/文本) ----
const AUDIO_LABEL: Record<string, string> = {
  'raw.wav': '原始音轨', 'vocal.wav': '分离人声', 'background.wav': '背景音',
  'dub_full.wav': '完整配音轨',
}
const TEXT_TRUNCATE = 6000

// 产物清单由导航轮询更新。用登记时计算的哈希作为 URL 版本，确保同名文件
// 被重跑替换后，video/audio 元素和文本观察器都能立刻切到新内容。
function artifactVersion(art: any): string {
  return String(art?.file_revision || art?.sha256 || `${art?.size ?? 0}`)
}
function versionedUrl(url: string, version: string): string {
  return version ? `${url}?v=${encodeURIComponent(version)}` : url
}
function sourceVideoVersion(): string {
  const arts = nav.stepMap.download?.artifacts ?? []
  return artifactVersion(arts.find((a: any) => a.name === 'video.mp4'))
}

const assembleSourceUrl = computed(() => {
  if (key !== 'assemble') return ''
  const artifact = (nav.stepMap.download?.artifacts ?? [])
    .find((item: any) => item.name === 'video.mp4')
  if (!artifact) return ''
  return versionedUrl(`/api/projects/${uid}/workbench/video`, sourceVideoVersion())
})
// 结算面板: 原视频与字幕文件的实际位置 (字幕轨与烧录配置一致)。
const videoInfoArtifact = computed(() => key === 'download'
  ? (nav.stepMap.download?.artifacts ?? []).find((a: any) => a.name === 'video_info.json')
  : undefined)
const videoInfoVersion = computed(() => artifactVersion(videoInfoArtifact.value))
const assembleSourceArtifact = computed(() => key === 'assemble'
  ? (nav.stepMap.download?.artifacts ?? []).find((item: any) => item.name === 'video.mp4')
  : undefined)
const assembleSourcePath = computed(() => assembleSourceArtifact.value?.path ?? '')
const assembleSubtitleName = computed(() => {
  if (key !== 'assemble') return ''
  const track = String(step.value?.cfg?.assemble?.subtitle_track ?? 'trans')
  return `${track}.srt`
})
const assembleSubtitlePath = computed(() => key === 'assemble'
  ? (nav.stepMap.subtitles?.artifacts ?? [])
      .find((a: any) => a.name === assembleSubtitleName.value)?.path ?? ''
  : '')

// 成片页可预览所有已生成组合; source 是原片, dub_full 是尚无成片时的音频兜底。
const selectedAssembleOutput = ref('')
const outputPreviewChapters = computed(() => {
  if (key !== 'assemble' || selectedAssembleOutput.value === 'source') return downloadChapters.value
  const art = (step.value?.artifacts ?? []).find((row: any) => row.name === selectedAssembleOutput.value)
  // 试片缺少完整源轴范围，不能把原片章节时间直接叠在短片上。
  if (!art || art.name.startsWith('preview_')) return []
  return mapOutputCutChapters(downloadChapters.value, art.output_cuts)
})
const assembleOutputOptions = computed(() => {
  if (key !== 'assemble') return []
  const arts = (step.value?.artifacts ?? []).filter((a: any) => a.exists !== false)
  const finals = arts.filter((a: any) => finalVideoKind(a.name))
    .sort((a: any, b: any) => b.name.localeCompare(a.name, 'en', { numeric: true }))
  const options = finals.map((a: any) => ({ label: a.version_name ? `${a.version_name} · ${a.version_summary}` : finalVideoLabel(a.name, a.created_at), value: a.name }))
  if (assembleSourceArtifact.value) {
    options.push({ label: '原片 · 原声 · 不烧录字幕', value: 'source' })
  }
  if (arts.some((a: any) => a.name === 'dub_full.wav')) {
    options.push({ label: '完整配音轨', value: 'dub_full.wav' })
  }
  return options
})
watch(assembleOutputOptions, (options) => {
  if (!options.some(option => option.value === selectedAssembleOutput.value)) {
    const latest = latestFinalVideo(step.value?.artifacts ?? [])?.name
    const hasDubAudio = (step.value?.artifacts ?? []).some((a: any) => a.exists !== false && a.name === 'dub_full.wav')
    const fallback = latest ?? (assembleSourceArtifact.value
      ? 'source'
      : hasDubAudio ? 'dub_full.wav' : '')
    selectedAssembleOutput.value = options.some(option => option.value === fallback) ? fallback : ''
  }
}, { immediate: true })

// 词级模式下，转录页的预览选择 words.json；未生成词产物时退回 src.srt。
const transcribePreviewMode = computed(() => String(
  draft.value['asr.segmentation_mode']
    ?? step.value?.cfg?.asr?.segmentation_mode
    ?? 'phrase'))

const subtitleTrack = ref('bilingual.srt')
const subtitleOptions = computed(() => [
  { label: '原文字幕', value: 'source.srt' }, { label: '目标语言字幕', value: 'trans.srt' },
  { label: '双语字幕', value: 'bilingual.srt' },
].filter(o => step.value?.artifacts?.some((a: any) => a.name === o.value)))
watch(subtitleOptions, (options) => {
  if (!options.some(o => o.value === subtitleTrack.value) && options.length) subtitleTrack.value = options[0].value
}, { immediate: true })
const preview = computed(() => {
  const arts = (step.value?.artifacts ?? []).filter((a: any) => a.exists !== false)
  const has = (n: string) => arts.some((a: any) => a.name === n)
  const artInfo = (n: string) => arts.find((a: any) => a.name === n)
  const artUrl = (n: string) => versionedUrl(
    `/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(n)}/raw`,
    artifactVersion(artInfo(n)))
  if (key === 'download') {
    // 本地引用/下载统一走 workbench 视频端点 (支持 Range 拖动)
    return step.value?.status === 'done'
      ? { kind: 'video', url: versionedUrl(`/api/projects/${uid}/workbench/video`,
          sourceVideoVersion()), version: sourceVideoVersion(), audios: [] } : null
  }
  if (key === 'assemble') {
    const selected = selectedAssembleOutput.value
    if (selected === 'source' && assembleSourceArtifact.value) {
      return { kind: 'video', url: assembleSourceUrl.value,
               version: sourceVideoVersion(), audios: [] }
    }
    if (selected === 'dub_full.wav' && has(selected)) {
      return { kind: 'audio', url: null, audios: [{ name: selected,
        label: AUDIO_LABEL[selected], url: artUrl(selected), version: artifactVersion(artInfo(selected)) }],
        version: artifactVersion(artInfo(selected)) }
    }
    const selectedArt = selected && selected !== 'source' ? artInfo(selected) : undefined
    const fin = selectedArt ?? latestFinalVideo(arts)
    const audios = ['dub_full.wav'].filter(has)
      .map((n) => ({ name: n, label: AUDIO_LABEL[n] ?? n, url: artUrl(n),
                     version: artifactVersion(artInfo(n)) }))
    if (!fin && !audios.length) return null
    // 成片未生成但配音轨已有时退回纯音频预览 (此前 kind 不匹配导致永远不显示)
    return fin ? { kind: 'video', url: artUrl(fin.name),
                  version: artifactVersion(fin), audios }
               : { kind: 'audio', url: null, audios,
                   version: audios.map((a) => a.version).join('|') }
  }
  if (key === 'preprocess') {
    // 音轨分离: 视频同步预览 + 三轨切换 (AudioTracksPlayer)
    const audios = ['raw.wav', 'vocal.wav', 'background.wav'].filter(has)
      .map((n) => {
        const art = (step.value?.artifacts ?? []).find((a: any) => a.name === n)
        const base = `/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(n)}`
        const version = artifactVersion(art)
        return { name: n, label: AUDIO_LABEL[n] ?? n,
                 url: versionedUrl(`${base}/raw`, version),
                 peaks: `${base}/peaks`, size: art?.size, sha256: art?.sha256 }
      })
    return audios.length
      ? { kind: 'tracks', audios,
          videoUrl: versionedUrl(`/api/projects/${uid}/workbench/video`, sourceVideoVersion()) }
      : null
  }
  // 文本类: 转录/分句/翻译/字幕 → 取第一个存在的文本产物
  const textArts: Record<string, string[]> = {
    transcribe: transcribePreviewMode.value === 'word'
      ? ['words.json', 'src.srt', 'transcribe.json']
      : ['src.srt', 'transcribe.json'],
    split: ['split.txt'],
    translate: ['translate.txt'],
    subtitles: [subtitleTrack.value, 'source.srt', 'trans.srt', 'bilingual.srt'],
  }
  const name = (textArts[key] ?? []).find(has)
  if (key === 'translate' && !name && (running.value || step.value?.partial_preview_job_id)) {
    return { kind: 'text', name: 'translate.txt', version: `job-${step.value?.partial_preview_job_id || step.value?.job_id}` }
  }
  return name ? { kind: 'text', name, version: artifactVersion(artInfo(name)) } : null
})
const subtitleArtifactName = computed(() => {
  return preview.value?.kind === 'text' ? (preview.value as any).name : null
})

const previewText = ref('')
const previewTextRaw = ref('')
let previewTextName = ''
let previewTextTimer: number | undefined
const previewTextFailed = ref(false)
const translationPreviewState = ref('')
const translationPreviewCount = ref('')
function textPreviewUrl(name: string) {
  const url = `/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(name)}/raw`
  return key === 'translate' && name === 'translate.txt' ? `${url}?preview=true` : url
}
const textLoader = createLatestRefresh(async (signal) => {
  const name = subtitleArtifactName.value!
  const r = await fetch(textPreviewUrl(name),
    { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取失败')
  return { name, raw: await r.text(), state: r.headers.get('X-DubForge-Preview') || '',
    count: `${r.headers.get('X-DubForge-Completed') || ''}/${r.headers.get('X-DubForge-Total') || ''}` }
}, ({ name, raw, state, count }) => {
  previewTextFailed.value = false
  translationPreviewState.value = state
  translationPreviewCount.value = count
  if (name !== previewTextName || raw !== previewTextRaw.value) {
    let display = raw
    if (name === 'words.json') {
      try {
        const words = JSON.parse(raw)
        display = (Array.isArray(words) ? words : [])
          .map((word: any, i: number) =>
            `${String(i + 1).padStart(4, ' ')}  ${Number(word.start).toFixed(2)}s → ${Number(word.end).toFixed(2)}s  ${String(word.word ?? '').trim()}`)
          .join('\n')
      } catch { /* 解析失败时保留原始文本，便于诊断 */ }
    } else if (name === 'translate.txt') {
      try {
        const data = JSON.parse(raw)
        const lines = Array.isArray(data) ? data : (data.lines ?? [])
        display = lines.map((line: any) =>
          `${String(line.id ?? '').padStart(4, ' ')}  ${String(line.src ?? '').trim()}\n       → ${String(line.dst ?? '').trim()}`)
          .join('\n')
      } catch { /* 编号纯文本直接显示 */ }
    }
    previewText.value = display.slice(0, TEXT_TRUNCATE)
    previewTextRaw.value = raw
    previewTextName = name
  }
}, () => {
  previewTextFailed.value = true
  if (!previewText.value) previewText.value = '(读取失败，正在重试)'
})
function loadText(name: string | null, refresh = false) {
  if (!name) {
    textLoader.cancel()
    previewText.value = ''
    previewTextRaw.value = ''
    previewTextName = ''
    previewTextFailed.value = false
    translationPreviewState.value = ''
    translationPreviewCount.value = ''
    return
  }
  return textLoader.refresh(!refresh)
}
watch(() => preview.value?.kind === 'text'
  ? `${(preview.value as any).name}:${(preview.value as any).version || ''}` : null,
  (sig) => loadText(sig ? sig.split(':')[0] : null), { immediate: true })
function syncTextPolling() {
  const name = preview.value?.kind === 'text' ? (preview.value as any).name : null
  const shouldPoll = !!name && (running.value || previewTextFailed.value)
  if (shouldPoll && !previewTextTimer) {
    previewTextTimer = window.setInterval(() => {
      const current = preview.value?.kind === 'text' ? (preview.value as any).name : null
      loadText(current, true)
    }, 1500)
  } else if (!shouldPoll && previewTextTimer) {
    clearInterval(previewTextTimer)
    previewTextTimer = undefined
    // 步骤刚结束时再拉一次，确保同名产物的最终内容已经显示。
    if (!running.value && name) loadText(name, true)
  }
}
watch([running, previewTextFailed, () => (preview.value?.kind === 'text' ? (preview.value as any).name : null)],
      syncTextPolling, { immediate: true })
onUnmounted(() => {
  textLoader.cancel()
  if (previewTextTimer) clearInterval(previewTextTimer)
})

// 转录、语义整理、翻译和字幕阶段同时提供“原视频 + 当前步骤字幕”预览。
const subtitlePreviewUrl = ref('')
const subtitlePreviewError = ref('')
let subtitleVtt = ''
let subtitleTimer: number | undefined
const subtitleVideo = computed(() =>
  (key === 'transcribe' || key === 'split' || key === 'translate' || key === 'subtitles')
    && preview.value?.kind === 'text'
    && nav.stepMap.download?.artifacts?.some((a: any) => a.name === 'video.mp4')
    ? versionedUrl(`/api/projects/${uid}/workbench/video`, sourceVideoVersion()) : '')

function srtToVtt(raw: string): string {
  const blocks = raw.replace(/^\uFEFF/, '').trim().split(/\r?\n\r?\n+/)
  const cues: string[] = []
  for (const block of blocks) {
    const rows = block.split(/\r?\n/)
    const timeIndex = rows.findIndex((row) => row.includes('-->'))
    if (timeIndex < 0) continue
    const timing = rows[timeIndex].replace(/,/g, '.')
    const text = rows.slice(timeIndex + 1).join('\n').trim()
    if (text) cues.push(`${cues.length + 1}\n${timing}\n${wrapCueText(text)}`)
  }
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

function jsonToVtt(raw: string, minDuration = 0): string {
  const data = JSON.parse(raw)
  const fmt = fmtVttSeconds
  // words.json 是 [{start,end,word}]; split/translate 均使用行协议预览。
  // 词级预览每个词一个 cue；只用于视频预览，不改写正式字幕产物。
  if (Array.isArray(data) && data.some((item: any) => item && 'word' in item)) {
    const cues = data
      .filter((word: any) => word && String(word.word ?? '').trim()
        && Number.isFinite(Number(word.start)) && Number.isFinite(Number(word.end)))
      .map((word: any, i: number) => {
        const start = Number(word.start)
        const end = Math.max(Number(word.end), start + Math.max(0, minDuration))
        return `${i + 1}\n${fmt(start)} --> ${fmt(end)}\n${wrapCueText(String(word.word).trim())}`
      })
    return `WEBVTT\n\n${cues.join('\n\n')}\n`
  }
  const lines = Array.isArray(data) ? data : (data.lines ?? [])
  const cues = lines
    .filter((line: any) => line && (line.text || line.dst) && Number.isFinite(Number(line.start))
      && Number.isFinite(Number(line.end)))
    .map((line: any, i: number) => {
      const start = Number(line.start)
      const end = Math.max(Number(line.end), start + Math.max(0, minDuration))
      return `${i + 1}\n${fmt(start)} --> ${fmt(end)}\n${wrapCueText(String(line.dst ?? line.text).trim())}`
    })
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

function timedTextToVtt(raw: string, minDuration = 0): string {
  const cues = raw.split(/\r?\n/)
    .map((row) => row.trim())
    .filter(Boolean)
    .map((row) => {
      const match = row.match(/^(\d+)\. (\d+(?:\.\d+)?)-(\d+(?:\.\d+)?): (.*)$/)
      if (!match) throw new Error('split 行协议格式错误')
      const start = Number(match[2])
      const end = Math.max(Number(match[3]), start + Math.max(0, minDuration))
      const text = match[4].trim()
      return text ? `${match[1]}\n${fmtVttSeconds(start)} --> ${fmtVttSeconds(end)}\n${wrapCueText(text)}` : ''
    })
    .filter(Boolean)
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

function translationTextToVtt(translationRaw: string, timingRaw: string,
                              minDuration = 0): string {
  const timedLines = new Map<number, { start: number; end: number }>()
  for (const row of timingRaw.split(/\r?\n/)) {
    const match = row.trim().match(/^(\d+)\. (\d+(?:\.\d+)?)-(\d+(?:\.\d+)?): (.*)$/)
    if (match) timedLines.set(Number(match[1]), { start: Number(match[2]), end: Number(match[3]) })
  }
  if (timingRaw.includes('-->')) {
    const seconds = (ts: string) => ts.replace(',', '.').split(':').reduce((n, part) => n * 60 + Number(part), 0)
    for (const block of timingRaw.trim().split(/\r?\n\s*\r?\n/)) {
      const rows = block.split(/\r?\n/)
      const parts = rows[1]?.split(' --> ')
      if (parts?.length === 2) timedLines.set(Number(rows[0]), { start: seconds(parts[0]), end: seconds(parts[1]) })
    }
  }
  const cues = translationRaw.split(/\r?\n/)
    .map((row) => row.trim())
    .filter(Boolean)
    .map((row) => {
      const match = row.match(/^(\d+)\. ?(.*)$/)
      if (!match) throw new Error('译文行协议格式错误')
      const timing = timedLines.get(Number(match[1]))
      if (!timing || !Number.isFinite(timing.start) || !Number.isFinite(timing.end)) return ''
      const text = match[2].trim()
      const end = Math.max(timing.end, timing.start + Math.max(0, minDuration))
      return text
        ? `${match[1]}\n${fmtVttSeconds(timing.start)} --> ${fmtVttSeconds(end)}\n${wrapCueText(text)}`
        : ''
    })
    .filter(Boolean)
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

function fmtVttSeconds(seconds: number): string {
  return formatTimecode(Number.isFinite(seconds) ? Math.max(0, seconds) : 0, 3)
}

const subtitleLoader = createLatestRefresh(async (signal) => {
  const name = subtitleArtifactName.value!
  const r = await fetch(textPreviewUrl(name),
    { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取字幕产物失败')
  if (r.headers.get('X-DubForge-Preview') === 'waiting') return null
  const raw = await r.text()
  let vtt: string
  if (key === 'translate' && name === 'translate.txt') {
    const source = step.value?.inputs?.[0]
    if (!source) throw new Error('未选择原文时间轴')
    const timingResponse = await fetch(
      `/api/projects/${uid}/steps/${source.step}/artifacts/${source.name}/raw`, { cache: 'no-store', signal })
    if (!timingResponse.ok) throw new Error('读取原文时间轴失败')
    vtt = translationTextToVtt(raw, await timingResponse.text())
  } else if (key === 'split' && name === 'split.txt') {
    vtt = timedTextToVtt(raw)
  } else {
    vtt = name.endsWith('.srt') ? srtToVtt(raw) : jsonToVtt(raw)
  }
  if (!vtt.includes('-->')) throw new Error('字幕产物尚未生成完整')
  return vtt
}, (vtt) => {
  if (vtt === null) { subtitlePreviewError.value = ''; return }
  if (vtt !== subtitleVtt) {
    const previousUrl = subtitlePreviewUrl.value
    subtitlePreviewUrl.value = URL.createObjectURL(new Blob([vtt], { type: 'text/vtt' }))
    subtitleVtt = vtt
    // 等新 track 挂载再释放旧 URL，播放中的 video 本身不重建。
    if (previousUrl) void nextTick(() => URL.revokeObjectURL(previousUrl))
  }
  subtitlePreviewError.value = ''
}, () => {
  subtitlePreviewError.value = '字幕预览暂时不可用，正在自动重试；已有预览会保留。'
})
function loadSubtitlePreview(name: string | null, refresh = false) {
  if (!name || !subtitleVideo.value) {
    subtitleLoader.cancel()
    if (subtitlePreviewUrl.value) URL.revokeObjectURL(subtitlePreviewUrl.value)
    subtitlePreviewUrl.value = ''
    subtitleVtt = ''
    subtitlePreviewError.value = ''
    return
  }
  return subtitleLoader.refresh(!refresh)
}
function onSubtitleLoaded(event: Event) {
  (event.target as HTMLTrackElement).track.mode = 'showing'
}
watch(() => (
  (key === 'transcribe' || key === 'split' || key === 'translate' || key === 'subtitles')
    && preview.value?.kind === 'text'
    ? `${subtitleArtifactName.value}:${(preview.value as any).version || ''}:${transcribePreviewMode.value}:${subtitleVideo.value}:${key === 'translate' ? artifactVersion(nav.stepMap[step.value?.inputs?.[0]?.step]?.artifacts?.find((a: any) => a.name === step.value?.inputs?.[0]?.name)) : ''}` : null
), (sig) => loadSubtitlePreview(sig ? sig.split(':')[0] : null), { immediate: true })
function syncSubtitlePolling() {
  const shouldPoll = !!subtitleVideo.value && (running.value || !!subtitlePreviewError.value)
  if (shouldPoll && !subtitleTimer) {
    subtitleTimer = window.setInterval(() => {
      loadSubtitlePreview(subtitleArtifactName.value, true)
    }, 1500)
  } else if (!shouldPoll && subtitleTimer) {
    clearInterval(subtitleTimer)
    subtitleTimer = undefined
    // 步骤结束后再拉一次最终产物，避免最后一个窗口刚写入时仍显示旧字幕。
    if (!running.value && subtitleVideo.value) {
      void loadSubtitlePreview(subtitleArtifactName.value, true)
    }
  }
}
watch([running, subtitleVideo, subtitlePreviewError], syncSubtitlePolling, { immediate: true })
watch(running, (value) => {
  if (value && key !== 'tts') previewRoot.value?.querySelectorAll<HTMLMediaElement>('video, audio').forEach(el => el.pause())
})
const mediaRebuilding = computed(() =>
  ['running', 'queued'].includes(nav.stepMap.download?.status)
  || (key === 'tts' && ['running', 'queued'].includes(nav.stepMap.preprocess?.status))
  || (running.value && ['preprocess', 'assemble'].includes(key)))
onUnmounted(() => {
  subtitleLoader.cancel()
  if (subtitleTimer) clearInterval(subtitleTimer)
  if (subtitlePreviewUrl.value) URL.revokeObjectURL(subtitlePreviewUrl.value)
})
</script>

<template>
  <div class="workspace-page step-page">
    <n-alert v-if="nav.stepsError || nav.projectError" type="error" style="margin-bottom: 16px">
      {{ nav.stepsError || nav.projectError }}
      <n-button size="small" @click="nav.poll(true)">重新核对</n-button>
    </n-alert>
    <div v-if="step">
      <StepHeader :title="step.label" :description="step.description">
        <template #status>
          <span class="header-badges">
            <!-- 按状态切换整个提示组件，trigger 必须始终有一个实际元素。 -->
            <n-tag v-if="step.settings_changed" size="small" type="warning" :bordered="false"
                   title="本步骤的设置相对上次产物发生了变化">本步设置已改</n-tag>
            <n-tag v-if="step.upstream_changed" size="small" type="warning" :bordered="false"
                   title="上游产物相对本步骤上次运行发生了变化">上游产物已变</n-tag>
            <n-tooltip v-if="!['done','stale'].includes(step.status)" trigger="hover" :delay="200" placement="top">
              <template #trigger>
                <n-tag size="small" :type="STATUS_TAG[displayStatus] || 'default'"
                       :bordered="step.status !== 'pending'">
                  {{ key === 'tts' ? '配音 · ' : '' }}{{ STATUS_TEXT[displayStatus] || displayStatus }}
                </n-tag>
              </template>
              {{ STATUS_HINTS[displayStatus] || '当前步骤状态。' }}
            </n-tooltip>
            <n-tooltip v-else-if="step.fresh" trigger="hover" :delay="200" placement="top">
              <template #trigger>
                <n-tag size="small" type="success" :bordered="false">{{ step.freshness_label || '已是最新' }}</n-tag>
              </template>
              {{ step.freshness_reason || FRESHNESS_HINTS.fresh }}
            </n-tooltip>
            <FreshnessDetails v-else :uid="uid" :step-key="key" :label="step.label"
                              :status-text="step.freshness_label || (key === 'tts' ? '仍有待配音句' : '需更新')"
                              :summary="step.freshness_reason || (key === 'tts' ? '还有选用配音的句子缺少有效音频，可继续合成所选或补齐配音。' : FRESHNESS_HINTS.stale)" />
            <AutoRunBadge :workflow-status="nav.workflow?.run?.status || null"
                          :chain="nav.chain" :running-label="null" />
          </span>
        </template>
        <template #actions>
        <div class="step-action-toolbar" :data-action-mode="actionMode" role="group" aria-label="本步骤操作">
          <n-button size="small" quaternary title="在文件浏览器中打开本步骤目录"
                    @click="revealStep">📁</n-button>
          <n-button v-if="actionMode === 'checking'" disabled>产物核验中</n-button>
          <template v-else-if="actionMode === 'running'">
            <n-tag v-if="nav.chainRunning" size="small" type="info" :bordered="false"
                   title="本步完成后自动继续剩余计划">⛓ 续跑中</n-tag>
            <n-button size="small" secondary :loading="pauseBusy"
                      :title="pausing ? '继续执行' : '在下一个检查点暂停 (循环步骤在句/块间, 分离在分段间)'"
                      @click="setPause(!pausing)">{{ pausing ? '▶ 继续' : '⏸ 暂停' }}</n-button>
            <n-button type="warning" @click="stopDialog">⏹ 停止</n-button>
          </template>
          <template v-else-if="actionMode === 'cancelling'">
            <n-tag size="small" type="warning" :bordered="false"
                   title="正在等待当前处理安全收尾；超时停止不代表你点击了取消">⏳ {{ stoppingLabel(step) }}…</n-tag>
            <n-button v-if="step.cancelling && !cancelPending" type="error" secondary :loading="forceBusy"
                      title="仅在停止长时间无响应时使用；不能保证外部模型立即停止计算"
                      @click="forceDialog">⛔ 强制结束</n-button>
          </template>
          <n-button v-else-if="key === 'tts' && route.query.panel === 'subtitles'" secondary
                    @click="router.replace({ query: { ...route.query, panel: undefined } })">返回校对与配音</n-button>
          <n-button v-else-if="key === 'assemble' && actionMode!=='chain-hold'" type="primary" :disabled="nav.archived || nav.busy || dirMissing" @click="chooseOutputs">选择成品方案</n-button>
          <n-button v-else-if="actionMode === 'deps'" type="primary"
                    :loading="busy" :disabled="nav.archived || nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty || reviewDirty || reviewBusy || assemblyPending"
                    @click="runWithDependencies">
            {{ key === 'tts' ? '同步全项目字幕与配音…' : dependencyActionLabel(actionBlockers) + '…' }}
          </n-button>
          <template v-else-if="actionMode === 'chain-hold'">
            <n-tag size="small" type="warning" :bordered="false"
                   :title="'自动运行计划已挂起, 剩余 ' + (nav.chain?.remaining.length || 0) + ' 步'">⛓ 计划挂起</n-tag>
            <BatchWorkflowResume :projects="[{ uid, title: nav.title || uid }]" kind="chain" label="检查恢复方案…"
              :disabled="busy || nav.archived || nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty || reviewDirty || reviewBusy || assemblyPending"
              @resumed="nav.poll()" />
            <n-button secondary :loading="busy" :disabled="nav.archived || nav.busy || !canRun || dirMissing"
                      :title="key === 'tts' ? '补齐待配音句子，保留有效音频与手动声源选择；计划保持挂起' : '只重跑本步，不继续剩余计划（计划保留挂起）'"
                      @click="run(false)">{{ key === 'tts' ? '仅补齐配音' : '仅重试本步' }}</n-button>
          </template>
          <template v-else-if="key === 'tts'">
            <n-button v-if="actionMode !== 'fresh-done'" type="primary" :loading="busy"
                      :disabled="nav.archived || nav.busy || !canRun || dirMissing"
                      title="补齐待配音的句子，保留已生成的有效音频；手动选用原声或空位的句子保持当前声源。"
                      @click="run(false)">补齐未完成配音</n-button>
            <n-button secondary :disabled="nav.archived || nav.busy || dirMissing || parametersDirty || reviewDirty || reviewBusy || !dubbingPanel?.canRegenerateAll"
                      title="使用已保存参数生成全部句子；默认覆盖当前槽，需要对比不同声音时可选择新槽。"
                      @click="rerunDialog()">生成全部配音…</n-button>
          </template>
          <n-button v-else-if="actionMode === 'fresh-done'" secondary
                    :loading="busy" :disabled="nav.archived || nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty || reviewDirty || reviewBusy"
                    :title="key === 'split' && ['failed', 'interrupted'].includes(String(step.status || ''))
                        ? '失败恢复会复用已通过校验的分块；未命中的分块重新请求模型'
                      : '忽略缓存并重新生成本步产物'"
                    @click="rerunDialog()">重做本步产物…</n-button>
          <n-button v-else type="primary" :loading="busy"
                    :disabled="nav.archived || nav.busy || !canRun || dirMissing"
                    @click="run(false)">
            {{ ['done', 'stale', 'failed', 'interrupted'].includes(step.status) ? '重新运行本步' : '运行本步' }}
          </n-button>
          <n-dropdown v-if="!running && actionMode !== 'checking'" :options="localRunOptions" trigger="click" @select="localRunAction"><n-button :disabled="nav.archived || nav.busy || dirMissing || busy || dubbingBusy || dubbingDirty || parametersDirty || reviewDirty || reviewBusy || assemblyPending">更多运行操作</n-button></n-dropdown>
        </div>
        </template>
      </StepHeader>
      <WorkflowStageNavigation :uid="uid" :stage="key" />
      <WorkflowPanel ref="projectExecution" :uid="uid" headless @started="nav.poll()" />
      <n-alert v-if="WORKFLOW_BUSY.includes(nav.workflow?.run?.status)" type="info" :show-icon="false" style="margin-bottom:14px">
        自动流程正在执行：{{ nav.workflow.run.phase }}。单步重跑暂不可用；停止完整流程请回总览。
        <template #action><n-button size="small" @click="router.push(`/projects/${uid}#automatic-workflow`)">查看完整流程</n-button></template>
      </n-alert>
      <n-alert v-if="parametersDirty" type="warning" :show-icon="false" style="margin-bottom: 14px">
        <template v-if="translationBusy">翻译背景资料正在读取或保存，运行入口暂时暂停，请稍候。</template>
        <template v-else>步骤参数{{ key === 'translate' ? '或背景资料' : '' }}尚未保存，运行入口已暂停。请在「步骤参数」保存修改后再生成；不会自动用旧设置运行。</template>
        <template #action><n-button size="small" @click="detailSection = 'parameters'">{{ translationDirty || translationBusy ? '查看背景资料' : '查看未保存参数' }}</n-button></template>
      </n-alert>
      <n-text v-if="key === 'tts' && route.query.panel !== 'subtitles'" depth="3" style="display:block;margin-bottom:14px;font-size:12px">
        <template v-if="actionMode === 'deps'">请先同步上游内容，再保存配音参数并重新生成。</template>
        <template v-else-if="actionMode === 'chain-hold'">自动计划已挂起，可先检查恢复方案。</template>
        <template v-else>保存配音参数后，点击「生成全部配音」选择目标槽；默认覆盖当前槽，需要对比不同声音时可主动选择新槽。</template>
        仅更新部分句子可在下方勾选生成。字幕显示模式只影响试听。
      </n-text>
      <div v-if="key !== 'tts' || route.query.panel !== 'subtitles' || running" class="step-runtime">
        <ExecutionProgress :execution="execution" :percentage="running ? step.progress : null" :progress-label="key==='assemble'?'合成步骤':undefined" :estimated="step.progress_estimated" :indeterminate="step.progress_indeterminate" :eta="running ? formatEta(executionEtaSeconds(step),step.eta_scope) : ''" :activity="running ? nav.activityFor(key) : null" :compact="key==='assemble'" />
        <n-text v-if="key === 'tts' && dubbingTaskSummary && (running || (route.query.panel !== 'subtitles' && dubbingTaskSummary.selected))" depth="3" class="runtime-meta">
          <template v-if="dubbingTaskSummary?.rangePending">配音范围待准备<span v-if="dubbingTaskSummary.documentLines"> · 已有文稿 {{ dubbingTaskSummary.documentLines }} 句</span><span v-else> · 正在核对文稿内容</span>；取得执行资源后核定实际生成与复用范围。</template><template v-else>本次{{ dubbingTaskSummary?.selected ? '所选' : '配音' }} {{ dubbingTaskSummary?.total }} 句<span v-if="!running && dubbingTaskSummary?.completed !== null"> · 已完成 {{ dubbingTaskSummary?.completed }} 句</span><span v-if="dubbingTaskSummary?.failed"> · 失败 {{ dubbingTaskSummary.failed }} 句</span></template><span v-if="running"> · {{ step.status==='queued' ? '排队后自动继续，可切换页面查看' : '后台执行，刷新或切换页面可继续查看' }}</span>
        </n-text>
      </div>

      <!-- 幽灵项目门禁: 目录缺失时明确禁用并说明 -->
      <n-alert v-if="dirMissing" type="error" style="margin-top: 14px"
               title="项目目录不在磁盘上 —— 运行已禁用">
        目录可能被移动或删除。从回收站还原目录后刷新本页即可恢复运行;
        确认不要了可回项目列表删除该项目清理登记。
      </n-alert>

      <!-- 依赖门禁: 明确指出缺哪些上游；字幕与配音合并展示一次 -->
      <details v-if="actionBlockers.length && key !== 'assemble' && !(key === 'tts' && route.query.panel === 'subtitles')"
               class="dependency-context" :open="!['done','stale'].includes(step.status)">
        <summary><span>{{ actionBlockers.length }} 项输入待准备</span><small>展开查看来源与原因；运行操作在顶部</small></summary>
        <DependencyNotice :uid="uid" :items="actionBlockers" />
      </details>

      <!-- 错误三件套 -->
      <n-alert v-if="showStepError(step)" type="error" :show-icon="false" style="margin-top: 14px">
        <n-text strong>{{ step.error.message }}</n-text><br />
        <n-text depth="3" style="font-size: 12px">[{{ step.error.code }}]
          {{ step.error.suggestion }}</n-text><br />
        <n-text depth="3" style="font-size: 12px">
          请查看步骤日志中的具体原因与文件保留、清理记录。
        </n-text>
        <n-text v-if="step.error.code === 'LLM_OUTPUT_BUDGET'"
                depth="3" style="display:block;font-size:12px;margin-top:6px">
          这是响应正文超过本步骤安全上限触发的停止；本次候选不会写入，系统不会重复发送相同配置的请求。
          模型思考内容不按字符数截断，仍由请求总时限兜底。
        </n-text>
        <n-text v-if="step.error.code === 'TTS_REQUEST_TIMEOUT' && step.error.detail"
                depth="3" style="display:block;font-size:12px;margin-top:6px">
          本地等待阶段：{{ step.error.detail.phase || '服务端请求' }}；
          已等待 {{ Number(step.error.detail.elapsed_seconds || 0).toFixed(1) }} 秒
          <span v-if="typeof step.error.detail.last_progress === 'number'">
            · 最后已知进度 {{ Math.round(step.error.detail.last_progress * 100) }}%
          </span>。服务端可能仍在收尾，重试前请等待配音服务恢复空闲。
        </n-text>
        <div v-if="key==='assemble' && commentFailure(step.error)" class="comment-recovery-links"><RouterLink :to="commentWorkspaceLink(uid)">核对评论与上屏方案 →</RouterLink><RouterLink :to="commentWorkspaceLink(uid,true)">查看评论 AI 日志 →</RouterLink></div>
      </n-alert>

      <details v-if="key==='assemble'" class="assembly-review-context" :open="assemblyReviewExpanded || reviewDirty || reviewBusy" @toggle="assemblyReviewExpanded=($event.target as HTMLDetailsElement).open">
        <summary><strong>内容审查与屏蔽</strong><span>{{ reviewDirty?'有未保存调整':reviewBusy?'正在处理…':'规则与人工屏蔽，作用于所有成片版本' }}</span></summary>
        <ContentReviewPanel :key="uid" :uid="uid" :disabled="nav.archived || dirMissing" @dirty-change="reviewDirty=$event" @busy-change="reviewBusy=$event" @saved="refreshInBackground(()=>nav.poll())" />
      </details>
      <ContentReviewPanel v-else-if="key === 'tts'" :key="uid" :uid="uid"
                          :disabled="nav.archived || dirMissing"
                          @dirty-change="reviewDirty = $event" @busy-change="reviewBusy = $event"
                          @saved="refreshInBackground(() => nav.poll())" />

      <!-- 产出预览: 有对应产物时直接展示 -->
      <div ref="previewRoot">
      <DubbingPanel v-if="key === 'tts'" ref="dubbingPanel" :key="uid" :uid="uid" :suspended="previewSuspended || mediaRebuilding"
                    @busy-change="dubbingBusy = $event" @dirty-change="dubbingDirty = $event" @content-loaded="dubbingContentLoaded" />
      <section v-if="key === 'assemble'" ref="outputsArea" tabindex="-1" aria-label="正式成品方案">
      <AssemblyVersionWorkspace ref="assemblyWorkspace" :key="uid" :uid="uid" :partial-request="route.query.partial_request" :refresh-token="assemblySettingsRefresh" :disabled="nav.archived || step.status === 'checking' || dirMissing || parametersDirty || reviewDirty || reviewBusy"
                                @dirty-change="assemblyDirty = $event" @busy-change="assemblyBusy = $event"
                                @saved="assemblySettingsSaved" />
      <AssemblyDeliveryPanel :uid="uid"
                            :artifacts="step.artifacts ?? []"
                            :source-path="assembleSourcePath"
                            :subtitle-path="assembleSubtitlePath"
                            :subtitle-name="assembleSubtitleName"
                            :disabled="nav.archived || nav.busy || dirMissing || parametersDirty || reviewDirty || reviewBusy || assemblyPending"
                            :preview="preview" :options="assembleOutputOptions" v-model:selected="selectedAssembleOutput" :chapters="outputPreviewChapters" :suspended="previewSuspended || mediaRebuilding"
                            @recycle-active="value => { if (value) releaseMediaIn(previewRoot); previewSuspended = value }" />
      </section>
      <n-card v-if="!['tts','assemble'].includes(key) && preview && !previewSuspended && !mediaRebuilding" size="small" title="产出预览" style="margin-top: 14px">
        <n-select v-if="key === 'subtitles'" v-model:value="subtitleTrack" :options="subtitleOptions"
                  style="max-width:300px; margin-bottom:12px" />
        <n-select v-if="key === 'assemble'" v-model:value="selectedAssembleOutput"
                  :options="assembleOutputOptions" size="small"
                  aria-label="选择要预览的成品"
                  style="max-width:300px; margin-bottom:12px" />
        <button v-if="key === 'subtitles' && (step.artifacts ?? []).find((a: any) => a.name === subtitleArtifactName)?.path"
                type="button" class="path-link"
                :title="'在文件浏览器中定位 ' + subtitleArtifactName"
                @click="revealArt(key, subtitleArtifactName!)">
          {{ (step.artifacts ?? []).find((a: any) => a.name === subtitleArtifactName)?.path }}
        </button>
        <template v-if="subtitleVideo">
          <VideoPreview :key="subtitleVideo" class="vid" :src="subtitleVideo" :chapters="downloadChapters">
            <track v-if="subtitlePreviewUrl" :key="subtitlePreviewUrl"
                   kind="subtitles" label="当前步骤字幕"
                   srclang="und" :src="subtitlePreviewUrl" @load="onSubtitleLoaded" default />
          </VideoPreview>
          <n-text depth="3" style="display:block; margin-top:8px; font-size:12px">
            语音转文字、语义整理、翻译和字幕导出页面会叠加当前步骤生成的字幕；可拖动进度检查文字与时间轴是否同步。
            <template v-if="key === 'transcribe' && transcribePreviewMode === 'word'">当前为词级预览：视频字幕和下方时间轴均来自 words.json，按词级时间戳逐个显示；正式的 src.srt 短语产物仍保留在产物清单中。</template>
            <template v-else-if="key === 'transcribe'">当前为短语级预览：视频字幕和下方文本使用识别后的短语锚点；词级时间轴仍保存在 words.json 中。</template>
            <template v-else-if="key === 'translate'">视频字幕与下方文本使用同一份译文预览；整轮成功后才更新完整译文。</template>
            <template v-if="running">步骤运行中会自动检查新产物并刷新字幕（约每 1.5 秒一次）。</template>
          </n-text>
          <n-alert v-if="subtitlePreviewError" type="warning" :show-icon="false"
                   style="margin-top:8px">{{ subtitlePreviewError }}</n-alert>
        </template>
        <template v-if="key === 'download' && media.found">
          <div class="prev-row">
            <div class="vid-col">
              <VideoPreview v-if="preview.url" :key="preview.version || preview.url" class="vid" :src="preview.url"
                            :chapters="downloadChapters" :duration="downloadDuration" :frame-rate="media.fps" />
            </div>
            <div class="info">
              <MediaInfo :media="media" />
            </div>
          </div>
        </template>
        <AudioTracksPlayer v-else-if="preview.kind === 'tracks'"
                           :tracks="preview.audios" :video-url="preview.videoUrl" :chapters="downloadChapters" />
        <VideoPreview v-else-if="preview.kind === 'video' && preview.url" :key="preview.version || preview.url" class="vid"
                      :src="preview.url" :chapters="outputPreviewChapters" :frame-rate="media.fps" />
        <template v-if="preview.kind === 'audio'">
          <div v-for="a in preview.audios ?? []" :key="`${a.name}:${a.version || ''}`" class="audio-row">
            <n-text depth="3" style="font-size: 12px; width: 76px">{{ a.label }}</n-text>
            <audio v-release-media :src="a.url" controls style="width: 100%; height: 36px" />
          </div>
        </template>
        <n-alert v-if="key === 'translate' && (running || translationPreviewState === 'partial')" type="info" :show-icon="false" style="margin-top:12px" role="status">
          <template v-if="translationPreviewState === 'partial'">本次部分译文 · 已完成 {{ translationPreviewCount }} 句。尚未替换完整产物。</template>
          <template v-else-if="translationPreviewState === 'waiting'">正在生成本次译文；完成首个窗口后在这里显示。</template>
          <template v-else>当前显示上次完整译文，本次结果将在完成窗口后刷新。</template>
        </n-alert>
        <pre v-if="preview.kind === 'text'" class="textprev">{{ previewText || (translationPreviewState === 'waiting' ? '等待首个翻译窗口…' : '(空)') }}</pre>
        <n-text v-if="preview.kind === 'text' && previewText.length >= 6000"
                depth="3" style="font-size: 11px">
          (内容较长, 仅显示前 6000 字符; 完整文件见项目目录)
        </n-text>

      </n-card>
      <VideoInfoPanel v-if="key === 'download'" :key="uid" :uid="uid" :available="!!videoInfoArtifact"
                      mode="video"
                      :version="videoInfoVersion" :automation="nav.commentsProgress"
                      @info-change="downloadInfo = $event" />

      </div>
      <nav v-show="key !== 'tts' || route.query.panel !== 'subtitles'" class="section-nav step-detail-nav" aria-label="步骤详情分类">
        <button v-if="cfgRows.length && key !== 'tts'" :aria-pressed="detailSection === 'parameters'" @click="detailSection = 'parameters'">{{ key === 'assemble' ? '项目默认参数' : '步骤参数' }}</button>
        <button :aria-pressed="detailSection === 'logs'" @click="detailSection = 'logs'">运行日志<span v-if="running" class="live-dot" /></button>
        <button v-if="llmStep" :aria-pressed="detailSection === 'llm'" @click="detailSection = 'llm'">LLM 对话<span v-if="running" class="live-dot" /></button>
        <button :aria-pressed="detailSection === 'files'" @click="detailSection = 'files'">输入与产物 · {{ step.artifacts?.length || 0 }}</button>
      </nav>
      <div v-show="key !== 'tts' || route.query.panel !== 'subtitles'" class="step-detail-content">
        <!-- 日志 -->
        <div v-show="detailSection === 'llm'" id="llm-conversation" ref="llmSection">
          <n-empty v-if="llmStep && !running && !llmMessages.length" description="暂无对话记录；运行后可查看模型输入、输出和纠错过程。"/>
          <!-- LLM 实时对话: 产出预览之后展示当前请求详情 -->
      <n-card v-if="llmStep && (running || llmMessages.length)" size="small"
              title="LLM 实时对话" style="margin-top: 14px">
        <template #header-extra>
          <n-tag v-if="running" size="small" type="info" :bordered="false">{{ llmStatus || '处理中…' }}</n-tag>
          <n-tag v-else size="small" :bordered="false">{{ llmStatus === '本次候选未采用' ? llmStatus : '本轮已结束' }}</n-tag>
        </template>
        <n-alert v-if="key === 'split' && step?.cfg?.split?.use_llm !== false"
                 type="info" :show-icon="false" style="margin-bottom: 10px">
          已启用<strong>全文语义整理</strong>：{{ splitSemanticDescription }} 保留真实长静音和相邻上下文。
          思考参数的实际生效值见下方设置预览；程序校正词级时间并保留静音空档。
        </n-alert>
        <n-alert v-if="key === 'translate'" type="info" :show-icon="false" style="margin-bottom: 10px">
          当前优先<strong>完整窗口翻译</strong>：每个窗口保留连续上下文，逐句沿用整理稿的编号、数量和时间。
          超过模型上下文预算或单窗口字幕行数安全上限时自动分窗（默认最多 100 行，可在步骤参数中设为 0 关闭行数上限），保留边界参考与术语交接；格式或缺行问题按范围恢复。
          思考参数的实际生效值见下方设置预览，下方视频会叠加当前翻译产物。
        </n-alert>
        <n-alert v-if="running && !llmMessages.length" type="info" :show-icon="false" style="margin-bottom: 10px">
          正在建立 LLM 请求；如果服务商尚未返回首字节，日志会显示等待状态。这里会实时显示返回内容。
        </n-alert>
        <n-text depth="3" style="display:block; margin-bottom: 10px; font-size: 11.5px">
  “系统提示”说明任务背景、目标和风格；“发送内容”提供本次窗口的素材。下方按发送/接收顺序展示；
  重试会保留必要的任务说明，并按问题范围选择完整上下文或局部片段，页面不会重复显示相同历史消息。
</n-text>
<div v-if="llmMessages.length" class="llm-panel-section">
  <div class="llm-panel-title">
    LLM 对话（按发送 / 接收顺序）
    <n-text depth="3">
      （线性时间线<template v-if="llmRetryCount"> · {{ llmRetryCount }} 个逻辑请求发生自动修复</template>）
    </n-text>
  </div>
  <LlmConversationPanel :rows="llmMessages" :heading="false">
    <template #header="{row}">
        <n-tag size="tiny" :type="llmRoleTagType(row)" :bordered="false">{{ llmRoleLabel(row) }}</n-tag>
        <n-text depth="3" style="font-size: 11px">
          <template v-if="row.ts"> {{ formatLlmTime(row.ts) }}</template>
          · {{ llmRowState(row) }}
          <template v-if="row.model"> · {{ row.profile }} / {{ row.model }}</template>
          <template v-if="row.retryAttempt > 1"> · 第 {{ row.retryAttempt }}/{{ row.retryTotal }} 次尝试</template>
          <template v-if="row.content"> · {{ row.content.length.toLocaleString() }} 字符</template>
          <template v-if="row.inputChars"> · 输入 {{ row.inputChars.toLocaleString() }} 字符</template>
          <template v-if="row.outputChars"> · 输出 {{ row.outputChars.toLocaleString() }} 字符</template>
          <template v-if="row.elapsedMs != null"> · 耗时 {{ (row.elapsedMs / 1000).toFixed(1) }}s</template>
        </n-text>
    </template>
    <template #content="{row}">      <details v-if="row.role === 'reasoning'" class="llm-reasoning">
        <summary>展开模型思考（{{ row.content.length.toLocaleString() }} 字符）</summary>
        <pre class="llm-message-content" :ref="(el) => setLlmOutputEl(row.id, el)"
             @scroll="onLlmOutputScroll(row.id, $event)"
             v-text="displayLlmContent(row) || '（等待思考内容）'"></pre>
      </details>
      <pre v-else class="llm-message-content"
           :ref="row.role === 'assistant' ? (el) => setLlmOutputEl(row.id, el) : undefined"
           @scroll="row.role === 'assistant' ? onLlmOutputScroll(row.id, $event) : undefined"
           v-text="row.role === 'assistant' ? (displayLlmContent(row) || '（等待内容）') : (row.content || '（等待内容）')"></pre>
    </template>
  </LlmConversationPanel>
  <n-text depth="3" style="display:block; margin-top:6px; font-size:11px">
    重试请求中重复携带的 system / 历史 user / assistant 消息只显示第一次出现；完整原始请求仍保存于项目 logs/{{ key }}.llm.jsonl。
  </n-text>
</div>
      </n-card>

        </div>
        <div v-show="detailSection === 'logs'">
      <n-card size="small" title="运行日志">
            <n-alert v-if="logTruncated" type="info" style="margin-bottom: 12px">
              日志过长，当前仅显示末尾 1 MiB 内的内容；完整日志仍保存在项目 logs 目录。
            </n-alert>
            <pre ref="logBox" class="logbox" @scroll="onLogScroll">{{ logDisplayText || '(暂无日志)' }}</pre>
          </n-card>
        </div>
        <!-- 本步骤参数: 项目级覆盖 ?? 全局默认; 标识偏离项, 可单键/整体恢复 -->
        <div v-if="cfgRows.length && key !== 'tts'" v-show="detailSection === 'parameters'">
          <n-card size="small" :title="key === 'assemble' ? '项目默认 · 混音与输出' : '本步骤参数'">
            <div v-if="key === 'assemble'" class="project-defaults-guide"><n-text depth="3">这里的基础值保留创建或上次应用时的设置。单项恢复不会读取最新全局或模板；修改只影响跟随项目默认的版本。</n-text><n-button size="small" :disabled="nav.archived || dirMissing || parametersDirty || assemblyPending || reviewDirty || reviewBusy" @click="assemblyWorkspace?.openDefaults()">查看并应用最新合成设置</n-button></div>
            <ProjectParameterForm :uid="uid" :step-key="key" :refresh-token="projectParametersRefresh" :disabled="nav.archived"
                                  :exclude-paths="key === 'assemble' ? [...SUBTITLE_STYLE_PATHS, 'assemble.burn_subtitles'] : undefined"
                                  @draft-change="draft = $event" @fields-change="editablePaths = $event"
                                  @dirty-change="stepParametersDirty = $event"
                                  @saved="stepParametersSaved" />
            <n-descriptions v-if="extraCfgRows.length" :column="1" label-placement="left"
                            size="small" bordered>
              <n-descriptions-item v-for="row in extraCfgRows" :key="row.k" :label="row.k">
                {{ row.v }}
              </n-descriptions-item>
            </n-descriptions>

          </n-card>
          <TranslationContextPanel v-if="key === 'translate'" :uid="uid" :disabled="nav.archived || dirMissing"
            @dirty-change="translationDirty = $event" @busy-change="translationBusy = $event" @saved="nav.poll()" />
        </div>
        <!-- 文件清单: 上游文件 / 计划产物 / 实际产物, 每行可点击定位 -->
        <div v-show="detailSection === 'files'">
          <n-card size="small" title="文件 (上游 / 计划 / 实际)">
            <template v-if="(step.inputs?.length ?? 0) || step.artifacts?.length">
              <div class="fsec">上游文件</div>
              <div v-for="inp in step.inputs" :key="inp.step + '/' + inp.name" class="fline">
                <span class="dot" :class="upInfo(inp).ok ? 'ok' : 'miss'" />
                <FilePathLink v-if="inp.file" :path="inp.file" :label="inp.name" /><n-text v-else style="font-size: 12.5px">{{ inp.name }}</n-text>
                <n-text depth="3" style="font-size: 11.5px; margin-left: 6px; flex: 1">
                  「{{ upInfo(inp).label }}」{{ inp.desc }}<template v-if="upInfo(inp).ok">
                    · {{ fmtSize(upInfo(inp).size) }}</template><template v-else> · {{ inp.reason }}</template>
                </n-text>
                <n-button v-if="upInfo(inp).ok" size="tiny" quaternary title="在文件浏览器中定位"
                          @click="revealArt(inp.step, inp.name)">📍</n-button>
              </div>
              <div class="fsec">计划产物</div>
              <div v-for="o in step.outputs" :key="o.name" class="fline">
                <span class="dot" :class="isProduced(o.name) ? 'ok' : 'plan'" />
                <n-text style="font-size: 12.5px">{{ o.name }}</n-text>
                <n-text depth="3" style="font-size: 11.5px; margin-left: 6px; flex: 1">
                  {{ o.desc }}<template v-if="producedCount(o.name) !== null">
                    · 已有 {{ producedCount(o.name) }} 个</template>
                </n-text>
              </div>
            </template>
            <div class="fsec">
              实际产物<template v-if="step.artifacts?.length"> ({{ step.artifacts.length }})</template>
            </div>
            <n-text v-if="!step.artifacts?.length" depth="3" style="font-size: 12px">
              (暂无产物)
            </n-text>
            <div v-for="a in step.artifacts" :key="a.name" class="fline">
              <FilePathLink :path="a.path" :label="a.name" />
              <FilePathLink class="flex-path" :path="a.path" />
              <n-tag v-if="a.exists === false" size="small" :bordered="false" type="warning">磁盘上缺失</n-tag>
              <n-text depth="3" style="font-size: 11.5px">{{ fmtSize(a.size) }}</n-text>
              <n-text v-if="a.job_id || a.settings_snapshot_hash" depth="3" style="font-size: 10.5px"
                      :title="a.settings_snapshot_hash ? `生成任务 #${a.job_id || '—'} · 配置快照 ${a.settings_snapshot_hash}` : `生成任务 #${a.job_id}`">
                · 任务 #{{ a.job_id || '—' }} · 设置快照 {{ (a.settings_snapshot_hash || '').slice(0, 10) }}
              </n-text>
              <a v-if="key === 'subtitles' && a.name.endsWith('.srt')"
                 :href="`/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(a.name)}/raw`"
                 :download="a.name" style="font-size:12px">下载</a>
            </div>
            <n-text depth="3" style="font-size: 11px; display: block; margin-top: 8px">
              上游文件可用 📍 定位；实际产物的完整路径可直接点击定位。
            </n-text>
          </n-card>
        </div>
      </div>
    </div>
    <div v-else-if="STEP_LABELS[key] && !nav.stepsLoaded">
      <StepHeader :title="STEP_LABELS[key]" description="正在读取步骤状态与文件信息…" />
      <n-card size="small" style="margin-top: 20px; padding: 24px 0; text-align: center">
        <n-spin v-if="!nav.stepsError" size="small" description="正在加载，请稍候" />
        <n-text v-else depth="3">暂时无法读取步骤，恢复连接后会自动更新。</n-text>
      </n-card>
    </div>
    <n-empty v-else description="步骤不存在" style="margin-top: 80px">
      <template #extra>
        <n-button @click="router.push(`/projects/${uid}`)">返回流程总览</n-button>
      </template>
    </n-empty>
  </div>
</template>

<style scoped>
.step-action-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; }
.project-defaults-guide{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px 18px;margin-bottom:18px;padding-bottom:16px;border-bottom:1px solid var(--df-border)}
.project-defaults-guide .n-text{flex:1;min-width:min(100%,300px);font-size:12px;line-height:1.7}
.comment-recovery-links { display:flex; gap:16px; flex-wrap:wrap; margin-top:10px; font-size:12px; }
.header-badges{display:inline-flex;align-items:center;flex-wrap:wrap;gap:8px}.dependency-context{margin:14px 0;border:1px solid var(--df-border);border-radius:8px;padding:10px 14px;background:var(--df-panel)}.dependency-context>summary{cursor:pointer;color:var(--df-text);font-size:12px;line-height:1.8}.dependency-context>summary::marker{color:var(--df-muted)}.dependency-context>summary small{font-size:11px;color:var(--df-muted);margin-left:14px}.dependency-context :deep(.dependency-notice){margin-top:10px}
.step-detail-nav { margin-top: 24px; margin-bottom: 16px; }
.step-detail-content { min-width: 0; }
.live-dot { width: 6px; height: 6px; background: var(--df-accent); border-radius: 50%; }
.step-detail-content :deep(.n-card + .n-card) { margin-top: 16px; }
.fline { flex-wrap: wrap; }
/* 预览行: 视频列贴内容宽, 信息列吃掉剩余; 窄屏上下堆叠 */
.prev-row { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.vid-col { flex: 0 1 auto; min-width: 0; }
.info { flex: 1 1 320px; min-width: 0; }
@media (max-width: 900px) {
  .prev-row { flex-direction: column; flex-wrap: nowrap; }
}
/* 视频按自身宽高比呈现, 最大高度封顶 (绝不信箱式拉伸出黑边) */
.vid {
  display: block;
  width: 604px;
  max-width: 100%;
  --df-preview-height: 340px;
}
.logbox {
  background: #0d1117;
  border: 1px solid #2a343f;
  border-radius: 6px;
  padding: 10px 14px;
  height: 38vh;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.fsec { font-size: 11.5px; color: rgba(255,255,255,0.45); margin: 8px 0 4px; }
.fsec:first-child { margin-top: 0; }
.fline { display: flex; align-items: center; gap: 4px; padding: 2px 0; }
.path-link {
  display: inline; max-width: 100%; border: 0; padding: 0; background: transparent;
  color: inherit; font: inherit; line-height: inherit; text-align: left;
  overflow-wrap: anywhere; cursor: pointer;
}
.path-link:hover { color: #63e2b7; text-decoration: underline; }
.path-link.muted { color: rgba(255,255,255,.52); font-size: 11.5px; }
.flex-path { flex: 1; min-width: 0; margin-left: 8px; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; margin-right: 4px; }
.dot.ok { background: #18a058; }
.dot.miss { background: #d03050; }
.dot.plan { background: rgba(255,255,255,0.25); }
.ctl { display: block; width: 100%; }
.frow { display: flex; align-items: center; gap: 4px; width: 100%; }
.plabel { display: inline-flex; align-items: center; gap: 4px; }
.gtag { opacity: 0.55; }
.audio-row { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
.step-runtime { display: grid; gap: 10px; min-width: 0; }
.step-runtime > :deep(.progress-activity) { margin-top: 0; }
.runtime-pause { justify-self: start; }
.runtime-meta { display: block; font-size: 12px; line-height: 1.7; overflow-wrap: anywhere; }
.param-label { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.help-dot { min-width: 18px; width: 18px; height: 18px; padding: 0; font-size: 11px; }
.textprev {
  background: #0d1117;
  border: 1px solid #2a343f;
  border-radius: 6px;
  padding: 10px 14px;
  max-height: 300px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.llm-panel-section { margin: 10px 0 12px; }
.llm-panel-title { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: #d7e2ef; }
.llm-panel-title :deep(.n-text) { font-weight: 400; font-size: 11px; }
.llm-history { max-height: 420px; overflow: auto; padding-right: 3px; }
.llm-message { margin: 8px 0; border: 1px solid #2a343f; border-radius: 6px; overflow: hidden; }
.llm-message-head { display: flex; align-items: center; gap: 8px; padding: 5px 8px; background: rgba(77,163,255,.07); }
.llm-message-content {
  margin: 0; padding: 8px 10px;
  white-space: pre-wrap; word-break: break-word; font-size: 12px; line-height: 1.5;
  background: #0d1117;
}
/* 对话时间线不再自己套一层外层滚动，长页面交给浏览器；
   只有单条长内容（思考/输出/长输入）保留内部滚动。 */
.llm-conversation-history {
  max-height: none;
  overflow: visible;
  padding-right: 3px;
}
.llm-message-system .llm-message-head { background: rgba(148,163,184,.08); }
.llm-message-user .llm-message-head { background: rgba(77,163,255,.09); }
.llm-message-assistant .llm-message-head { background: rgba(40,167,69,.08); }
.llm-message-json-repair { border-color: #3d5a73; }
.llm-message-json-repair .llm-message-head { background: rgba(77,163,255,.08); }
.llm-message-strategy { border-color: #43404f; }
.llm-message-strategy .llm-message-head { background: rgba(210,153,34,.06); }
.llm-message-merge .llm-message-head { background: rgba(24,160,88,.08); }
.llm-message-validation-error { border-color: #6b3138; }
.llm-message-validation-error .llm-message-head { background: rgba(208,48,80,.08); }
.llm-message-validation-success { border-color: #2f573d; }
.llm-message-validation-success .llm-message-head { background: rgba(24,160,88,.08); }
.llm-conversation-history .llm-message-content {
  max-height: min(72vh, 640px);
  overflow-y: auto;
  overscroll-behavior: contain;
}
.llm-reasoning { background: rgba(210,153,34,.06); }
.llm-reasoning summary { cursor: pointer; padding: 7px 10px; color: #d29922; font-size: 12px; }
.llm-reasoning[open] summary { border-bottom: 1px solid #3a3428; }@media (max-width: 720px) {
  .param-label { max-width: 100%; }
  .frow { flex-wrap: wrap; }
  .audio-row { flex-wrap: wrap; }
  .audio-row > audio { flex: 1 1 100%; width: 100% !important; }
  :deep(.n-form-item) { display: block; }
  :deep(.n-form-item-label) { width: 100% !important; padding-bottom: 4px; }
  :deep(.n-form-item-blank) { width: 100%; }
}
.assembly-review-context{margin:14px 0;border-bottom:1px solid var(--df-border);padding:0 0 14px}.assembly-review-context>summary{display:flex;align-items:center;gap:14px;cursor:pointer;list-style:none;font-size:12px;min-height:28px}.assembly-review-context>summary::before{content:'›';color:var(--df-muted);font-size:18px}.assembly-review-context[open]>summary::before{transform:rotate(90deg)}.assembly-review-context>summary>span{color:var(--df-muted);font-size:11px}.assembly-review-context>summary:focus-visible{outline:2px solid var(--df-accent);outline-offset:4px}.assembly-review-context :deep(.n-card){margin-top:12px}@media(max-width:600px){.assembly-review-context>summary{align-items:flex-start;flex-wrap:wrap;gap:6px}.assembly-review-context>summary>span{flex-basis:100%;padding-left:14px}}
</style>
