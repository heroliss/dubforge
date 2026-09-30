<script setup lang="ts">
import FilePathLink from '../components/FilePathLink.vue'
import ProgressActivity from '../components/ProgressActivity.vue'
import CommentDiagnostics from '../components/CommentDiagnostics.vue'
import { stoppingLabel } from '../utils/progressActivity'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { dependencyActionLabel, runPlannedSteps } from '../utils/runPlan'
import { showStepError, stepDisplayStatus } from '../utils/stepFeedback'
import { WORKFLOW_BUSY } from '../utils/workflow'
// 步骤专属页: 说明 / 状态 / 依赖门禁 / 本步配置 / 完整日志 / 产物清单 / 运行操作
// 由左侧流程导航进入, 一页只专注一个步骤; 依赖未就绪时明确指出并禁用运行
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NDescriptions, NDescriptionsItem, NEmpty, NForm, NFormItem,
  NGrid, NGridItem as NGi, NH1, NLayoutContent, NPopover, NProgress, NSpace, NSpin, NTag, NText,
  NTooltip, NSelect, useDialog, useMessage,
} from 'naive-ui'
import MediaInfo from '../components/MediaInfo.vue'
import AudioTracksPlayer from '../components/AudioTracksPlayer.vue'
import AssemblyVersionWorkspace from '../components/AssemblyVersionWorkspace.vue'
import AssembleOutputsPanel from '../components/AssembleOutputsPanel.vue'
import VideoInfoPanel from '../components/VideoInfoPanel.vue'
import ProjectParameterForm from '../components/ProjectParameterForm.vue'
import { SUBTITLE_STYLE_PATHS, subtitleSettingsLink, subtitleStyleSummary } from '../utils/subtitleWorkspace'
import StepHeader from '../components/StepHeader.vue'
import AutoRunBadge from '../components/AutoRunBadge.vue'
import DubbingPanel from '../components/DubbingPanel.vue'
import DependencyNotice from '../components/DependencyNotice.vue'
import { fmtSize } from '../utils/format'
import { outputChoice, type AssemblyChoice } from '../utils/actionScope'
import { useNavStore, STEP_ORDER, STEP_LABELS, STATUS_TAG, STATUS_TEXT, STATUS_HINTS,
         FRESHNESS_HINTS, formatEta, formatProgressDetail } from '../stores/nav'
import { useSmoothProgress } from '../utils/smoothProgress'
import { createLatestRefresh, finalVideoKind, finalVideoLabel, latestFinalVideo, releaseMediaIn, vReleaseMedia } from '../utils/preview'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()

const uid = route.params.uid as string
const key = route.params.key as string
const detailSection = ref(key === 'tts' ? 'logs' : 'parameters')
const llmSection = ref<HTMLElement|null>(null)
async function showLlmDiagnostics(){detailSection.value='llm';await nextTick();llmSection.value?.scrollIntoView({behavior:'smooth',block:'start'})}
watch(()=>route.hash,value=>{if(value==='#llm-conversation')detailSection.value='llm'},{immediate:true})

const step = computed(() => nav.stepMap[key])
const displayStatus = computed(() => step.value ? stepDisplayStatus(step.value) : 'pending')
const stepIdx = STEP_ORDER.indexOf(key as any)
const dubbingBusy = ref(false), dubbingDirty = ref(false)
const parametersDirty = ref(false)
const outputsArea = ref<HTMLElement | null>(null)
const assemblyChoice = ref<AssemblyChoice>(outputChoice('src'))
function chooseOutputs() {
  outputsArea.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  outputsArea.value?.focus({ preventScroll: true })
}
const smoothProgress = useSmoothProgress(
  () => step.value?.progress,
  () => step.value?.status,
)
const formatProgress = (value: unknown) => {
  const n = Number(value)
  return `${(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0).toFixed(1)}%`
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
  if (nav.busy) return
  if (parametersDirty.value || dubbingDirty.value) { message.warning('请先保存参数，再确认运行范围'); return }
  const targets = key === 'tts' ? ['subtitles', 'tts'] : [key]
  await runPlannedSteps(uid, targets, true, message, dialog,
    async (steps) => {
      if (key !== 'tts' || steps.some(k => ['download', 'preprocess'].includes(k))) {
        releaseMediaIn(previewRoot.value); previewSuspended.value = true; await nextTick()
      }
    },
    async () => { previewSuspended.value = false; await nav.poll() })
}
const running = computed(() => step.value?.status === 'running' || step.value?.status === 'queued')
const canRun = computed(() => !!step.value && !running.value
  && blockers.value.length === 0 && !dirMissing.value && !dubbingBusy.value && !dubbingDirty.value && !parametersDirty.value)

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
  'running' | 'cancelling' | 'deps' | 'chain-hold' | 'fill-voice' | 'fresh-done' | 'run'
>(() => {
  if (running.value && (step.value?.cancelling || cancelPending.value)) return 'cancelling'
  if (running.value) return 'running'
  if (key === 'tts' ? editorBlockers.value.length : blockers.value.length) return 'deps'
  // 链挂起在本步 (失败/停止/中断): 重试默认续跑剩余计划 (ADR-0072)。
  if (nav.chainHeldAt(key)) return 'chain-hold'
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
  if (nav.busy) return
  if (busy.value || cancelPending.value || forceBusy.value || dubbingBusy.value || dubbingDirty.value || parametersDirty.value) return
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
  if (busy.value || dialogAction.value) return
  dialogAction.value = 'rerun'
  const downstream = nav.steps.filter(s => (step.value?.affected_steps ?? []).includes(s.key))
    .filter((s: any) => s.status === 'done' || s.status === 'stale')
  const extra = key === 'tts'
    ? '范围是全项目中选用配音的句子，与下方勾选句子无关。将重新生成当前配音版本，已满意版本也会重做；其他版本保留。'
    : '将清理本步骤可重建的中间产物并重新生成，使用已保存的参数；外部原文件不删除。'
  dialog.warning({
    title: key === 'tts' ? '确认重做全项目配音' : `确认重做「${step.value.label}」`,
    content: extra + (downstream.length
      ? `下游 ${downstream.length} 个步骤将被标记为「已过期」（需要重跑）。`
      : ''),
    positiveText: key === 'tts' ? '重做全项目配音' : '确认重做',
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
    content: '正在运行的子进程 (下载/ffmpeg 等) 将立即终止；进程内计算会在当前单元结束后退出。'
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
  message.info('停止信号已发送, 等待步骤退出；长时间无响应可用「强制结束」')
}

function forceDialog() {
  if (forceBusy.value || dialogAction.value) return
  const targetJobId = step.value?.job_id ?? null
  dialogAction.value = 'force'
  dialog.error({
    title: `强制结束「${step.value.label}」？`,
    content: '仅建议在「取消中」长时间无响应时使用：子进程立即终止，状态立刻按已取消呈现；'
      + '进程内计算若仍在后台收尾，其结果将被自动丢弃。',
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

// 元数据到达: 用真实宽高比替换 16/9 兜底 —— 视频盒与内容比例一致, 不出黑边
function onMeta(e: Event) {
  const v = e.target as HTMLVideoElement
  if (v.videoWidth && v.videoHeight) {
    v.style.aspectRatio = `${v.videoWidth} / ${v.videoHeight}`
  }
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
  if (!logHistoryLoaded) {
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
const llmReferenceDistance = ref(500)
const llmReferenceSpeed = ref(360)
async function loadLlmDisplayConfig() {
  if (!llmStep.value) return
  try {
    const r = await fetch('/api/settings/llm_display', { cache: 'no-store' })
    if (!r.ok || llmDisposed) return
    const data = await r.json()
    llmReferenceDistance.value = Math.max(50, Math.min(5000,
      Number(data.reference_distance_chars) || 500))
    llmReferenceSpeed.value = Math.max(60, Math.min(1000,
      Number(data.reference_speed_chars_per_second) || 360))
  } catch { /* 显示参数读取失败时使用内置默认值 */ }
}
onMounted(() => { void loadLlmDisplayConfig() })
const llmEvents = ref<any[]>([])
let llmCursor = 0
let llmTraceId = ''
let llmLoading = false
let llmTimer: number | undefined
let llmRestoringHistory = true
let llmDisposed = false
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
  // 动画缓存是渐进显示层；若 watcher/历史恢复没有及时填充，则直接回退真实
  // 内容，绝不能让已经完成的思考或输出一直显示“等待内容”。
  return llmAnimatedContent.value[row.id] ?? row.content
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
  // 接近末尾时不会像线性弹簧那样拖得太久。所有思考/输出、进行中/已完成
  // 内容都用同一套参数，避免切换阶段时变速。
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
    if (!active.has(id)) delete llmAnimatedContent.value[id]
  }
}
const LLM_VALIDATION_METHODS: Record<string, string> = {
  translate: '校验方法：逐行匹配“行号. 译文” → 行号必须是已知且顺序一致的子序列 → 缺行/空译文可保留有效行并局部重译 → 结构性格式错误不猜续行 → 校验并截断交接摘要',
  split: '校验方法：逐行匹配“序号. 开始秒-结束秒: 片段” → 序号必须从 1 连续 → 禁止空行、注释和无前缀行 → 开始时间有序且在窗口内（允许少量重叠） → 允许修正明显 ASR 错词',
}
const llmValidationMethod = () => LLM_VALIDATION_METHODS[key] || '校验方法：提取 JSON → 检查结构、字段与步骤业务规则'
const formatLlmTime = (ts: unknown) => {
  const text = String(ts || '')
  const match = text.match(/T(\d{2}:\d{2}:\d{2})/)
  return match ? match[1] : text
}
const llmMessageState = computed(() => {
  const rows: any[] = []
  const assistantByRequest = new Map<string, any>()
  const reasoningByRequest = new Map<string, any>()
  const retryInfo = new Map<string, { maxAttempt: number, total: number, failures: string[] }>()
  // Chat Completions 的重试是一次新的 HTTP 请求。为了让模型知道此前任务，请求体
  // 会重复携带 system/历史消息；界面按内容去重，只保留这些消息第一次出现的顺序。
  const seenContext = new Set<string>()
  const retryMeta = (event: any) => {
    const group = String(event.retry_group || '')
    if (!group) return null
    const attempt = Number(event.retry_attempt) || 1
    const total = Number(event.retry_total) || 1
    const info = retryInfo.get(group) || { maxAttempt: 1, total, failures: [] }
    info.maxAttempt = Math.max(info.maxAttempt, attempt)
    info.total = Math.max(info.total, total)
    if (event.event === 'validation_error' && event.content) info.failures.push(String(event.content))
    retryInfo.set(group, info)
    return { group, attempt, total }
  }
  for (const event of llmEvents.value) {
    const retry = retryMeta(event)
    const requestId = String(event.request_id || '')
    if (event.event === 'json_repair') {
      rows.push({
        id: `json-repair-${rows.length}`, requestId, role: 'json_repair',
        kind: 'repaired', content: String(event.content || ''),
        status: '已本地修复', ts: event.ts,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'retry_strategy') {
      rows.push({
        id: `strategy-${rows.length}`, requestId, role: 'strategy',
        kind: event.mode === 'fresh' ? 'fresh' : 'conversation',
        content: String(event.content || ''),
        status: '已选择', ts: event.ts,
        retryAttempt: Number(event.retry_attempt) || 1,
        retryTotal: Number(event.retry_total) || 1,
      })
    } else if (event.event === 'merge_summary') {
      rows.push({
        id: `merge-${rows.length}`, requestId, role: 'merge',
        kind: 'success', content: String(event.content || ''),
        status: '合并完成', ts: event.ts,
      })
    } else if (event.event === 'request') {
      const role = event.role === 'system' ? 'system' : 'user'
      const content = String(event.content || '')
      // 只去重同一个逻辑请求内重复携带的上下文；不同窗口允许出现相同 role。
      const contextKey = retry ? `${retry.group}:${role}:${content}` : ''
      if (contextKey && seenContext.has(contextKey)) continue
      if (contextKey) seenContext.add(contextKey)
      rows.push({
        id: `${requestId}-${rows.length}`,
        requestId, role, content,
        profile: event.profile || '', model: event.model || '',
        status: '等待首字节', ts: event.ts,
        inputChars: Number(event.input_chars) || 0,
        requestKind: content.includes('局部修复请求')
          ? 'line-repair'
          : (retry?.attempt || 1) > 1 ? 'format-repair' : 'initial',
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'assistant_delta') {
      let row = assistantByRequest.get(requestId)
      if (!row) {
        row = { id: `${requestId}-assistant`, requestId, role: 'assistant',
          content: '', profile: event.profile || '', model: '',
          status: '生成中', ts: event.ts,
          retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
          retryTotal: retry?.total || 1 }
        rows.push(row)
        assistantByRequest.set(requestId, row)
      }
      row.content += String(event.content || '')
      row.status = '生成中'
    } else if (event.event === 'reasoning_delta') {
      let row = reasoningByRequest.get(requestId)
      if (!row) {
        row = { id: `${requestId}-reasoning`, requestId, role: 'reasoning',
          content: '', profile: event.profile || '', model: '',
          status: '思考中', ts: event.ts,
          retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
          retryTotal: retry?.total || 1 }
        rows.push(row)
        reasoningByRequest.set(requestId, row)
      }
      row.content += String(event.content || '')
      row.status = '思考中'
    } else if (event.event === 'assistant_response') {
      let row = assistantByRequest.get(requestId)
      if (!row) {
        row = { id: `${requestId}-assistant`, requestId, role: 'assistant',
          content: '', profile: event.profile || '', model: '',
          status: '已完成', ts: event.ts,
          retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
          retryTotal: retry?.total || 1 }
        rows.push(row)
        assistantByRequest.set(requestId, row)
      }
      row.content = String(event.content || '')
      row.status = '已完成'
      row.model = event.model || row.model
      row.outputChars = Number(event.output_chars) || row.content.length
      const startedAt = Date.parse(String(row.ts || ''))
      const finishedAt = Date.parse(String(event.ts || ''))
      if (Number.isFinite(startedAt) && Number.isFinite(finishedAt)) {
        row.elapsedMs = Math.max(0, finishedAt - startedAt)
      }
      const reasoning = reasoningByRequest.get(requestId)
      if (reasoning) {
        reasoning.status = '思考完成'
        const reasoningStart = Date.parse(String(reasoning.ts || ''))
        if (Number.isFinite(reasoningStart) && Number.isFinite(finishedAt)) {
          reasoning.elapsedMs = Math.max(0, finishedAt - reasoningStart)
        }
      }
    } else if (event.event === 'validation_error' || event.event === 'validation_success') {
      const eventText = String(event.content || '')
      const parseFailure = event.event === 'validation_error'
        && /未找到.*JSON|JSONDecodeError/i.test(eventText)
      rows.push({
        id: `validation-${rows.length}`, requestId, role: 'validation',
        kind: event.event === 'validation_error' ? 'error' : 'success',
        content: parseFailure
          ? `JSON 解析失败：${eventText || '未知原因'}`
          : event.event === 'validation_error'
            ? `校验失败：${eventText || '未知原因'}\n${llmValidationMethod()}`
            : `${eventText || '校验通过'}\n${llmValidationMethod()}`,
        status: parseFailure ? 'JSON 需修复' : event.event === 'validation_error' ? '需自动修复' : '已通过',
        ts: event.ts,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'fallback') {
      rows.push({
        id: `fallback-${rows.length}`, requestId, role: 'fallback',
        kind: 'warning',
        content: String(event.content || '已回退本地规则切分'),
        status: '已回退',
        ts: event.ts,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'status') {
      for (const row of rows) {
        if (row.requestId === requestId
            && ['system', 'user', 'reasoning'].includes(row.role)) {
          row.status = String(event.content || '')
        }
      }
    }
  }
  for (const row of rows) {
    const info = row.retryGroup ? retryInfo.get(row.retryGroup) : null
    if (info) {
      row.retryFailures = info.failures.length
      row.retryMaxAttempt = info.maxAttempt
    }
  }
  // 不再“只显示最后一次尝试”：完整对话按时间线展示，用户能看到首轮大输出
  // 和后续短修复输出之间的关系；重复携带的历史上下文已在上面被去重。
  return { allRows: rows, visibleRows: rows, retryInfo }
})
const llmMessages = computed(() => llmMessageState.value.visibleRows)
watch(llmMessages, syncLlmAnimations, { deep: true })
const llmRetryCount = computed(() =>
  Array.from(llmMessageState.value.retryInfo.values())
    .filter((info: any) => info.maxAttempt > 1).length)
const llmRoleLabel = (row: any) => ({
  system: '系统提示',
  user: row.requestKind === 'line-repair' ? '小片段重译请求'
    : row.requestKind === 'format-repair' ? '格式修复请求' : '发送内容',
  reasoning: '模型思考',
  assistant: '模型输出',
  strategy: '请求策略',
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
  json_repair: 'info',
  validation: row.kind === 'error' ? 'error' : 'success',
  merge: 'success',
  fallback: 'warning',
}[row.role] || 'default');

// 行头状态只用固定短标签，不直接显示后端 status 事件原文，避免闪烁和结束后残留。
const llmRowState = (row: any) => {
  if (row.role === 'assistant') return row.elapsedMs != null ? '已完成' : '生成中'
  if (row.role === 'reasoning') return row.elapsedMs != null ? '思考完成' : '思考中'
  return row.status || ''
}
const llmStatus = computed(() => {
  const assistant = [...llmMessages.value].reverse().find(
    (x: any) => x.role === 'assistant' && x.status === '生成中')
  const reasoning = [...llmMessages.value].reverse().find(
    (x: any) => x.role === 'reasoning' && x.status === '思考中')
  if (assistant) return `生成中 · ${(assistant.content || '').length.toLocaleString()} 字符`
  if (reasoning) return `思考中 · ${(reasoning.content || '').length.toLocaleString()} 字符`
  if (running.value) return '正在准备…'
  return ''
})
async function loadLlmTrace() {
  if (!llmStep.value || llmLoading || llmDisposed) return
  llmLoading = true
  try {
    const query = new URLSearchParams({ tail: '500', after: String(llmCursor), trace_id: llmTraceId })
    const r = await fetch(`/api/projects/${uid}/steps/${key}/llm_trace?${query}`,
      { cache: 'no-store' })
    if (!r.ok) return
    const j = await r.json()
    if (llmDisposed) return
    if (j.retry) return
    if (j.reset || !j.exists) {
      llmRestoringHistory = true
      llmEvents.value = []
      llmOutputFollow.clear()
      stopLlmAnimation()
      llmAnimatedContent.value = {}
    }
    llmCursor = j.cursor || 0
    llmTraceId = j.trace_id || ''
    if (Array.isArray(j.events) && j.events.length) llmEvents.value.push(...j.events)
    // 首次进入/刷新后的全部历史分页直接显示，不重新播放打字动画。
    // 在 Vue 刷新视图及动画监听执行前填好已显示内容；追完历史后，
    // 后续轮询才从这个位置平滑追加新字符。
    if (llmRestoringHistory) {
      for (const row of llmMessages.value) {
        if (row.role === 'assistant' || row.role === 'reasoning') {
          stopLlmAnimation(row.id)
          llmAnimatedContent.value[row.id] = row.content
        }
      }
      if (j.events?.length !== 500) llmRestoringHistory = false
    }
    // 页面初次打开或任务刚结束时也要读完积压的所有页。
    if (j.events?.length === 500) window.setTimeout(loadLlmTrace, 0)
  } catch { /* 后端瞬断: 下轮轮询再取 */ }
  finally { llmLoading = false }
}
function syncLlmPolling() {
  if (!llmStep.value) return
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
onUnmounted(() => {
  llmDisposed = true
  if (logTimer) clearInterval(logTimer)
  stopLogAnimation()
  if (llmTimer) clearInterval(llmTimer)
  if (llmFollowTimer !== undefined) window.clearTimeout(llmFollowTimer)
  stopLlmAnimation()
})

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
let mediaFailed = false
let mediaSignature = ''
const mediaLoader = createLatestRefresh(async (signal) => {
  const r = await fetch(`/api/projects/${uid}/media`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取媒体信息失败')
  return r.json()
}, (value) => { media.value = value; mediaFailed = !value.found }, () => { mediaFailed = true })
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
onUnmounted(() => mediaLoader.cancel())

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
const textLoader = createLatestRefresh(async (signal) => {
  const name = subtitleArtifactName.value!
  const r = await fetch(`/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(name)}/raw`,
    { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取失败')
  return { name, raw: await r.text() }
}, ({ name, raw }) => {
  previewTextFailed.value = false
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
    if (text) cues.push(`${cues.length + 1}\n${timing}\n${text}`)
  }
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

function jsonToVtt(raw: string, minDuration = 0): string {
  const data = JSON.parse(raw)
  const fmt = (seconds: number) => {
    const total = Math.max(0, Number(seconds) || 0)
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    const s = (total % 60).toFixed(3).padStart(6, '0')
    return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s}`
  }
  // words.json 是 [{start,end,word}]; split/translate 均使用行协议预览。
  // 词级预览每个词一个 cue；只用于视频预览，不改写正式字幕产物。
  if (Array.isArray(data) && data.some((item: any) => item && 'word' in item)) {
    const cues = data
      .filter((word: any) => word && String(word.word ?? '').trim()
        && Number.isFinite(Number(word.start)) && Number.isFinite(Number(word.end)))
      .map((word: any, i: number) => {
        const start = Number(word.start)
        const end = Math.max(Number(word.end), start + Math.max(0, minDuration))
        return `${i + 1}\n${fmt(start)} --> ${fmt(end)}\n${String(word.word).trim()}`
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
      return `${i + 1}\n${fmt(start)} --> ${fmt(end)}\n${String(line.dst ?? line.text).trim()}`
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
      return text ? `${match[1]}\n${fmtVttSeconds(start)} --> ${fmtVttSeconds(end)}\n${text}` : ''
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
        ? `${match[1]}\n${fmtVttSeconds(timing.start)} --> ${fmtVttSeconds(end)}\n${text}`
        : ''
    })
    .filter(Boolean)
  return `WEBVTT\n\n${cues.join('\n\n')}\n`
}

function fmtVttSeconds(seconds: number): string {
  const total = Math.max(0, Number(seconds) || 0)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = (total % 60).toFixed(3).padStart(6, '0')
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s}`
}

const subtitleLoader = createLatestRefresh(async (signal) => {
  const name = subtitleArtifactName.value!
  const r = await fetch(`/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(name)}/raw`,
    { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取字幕产物失败')
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
      <template #action><n-button size="small" @click="nav.poll()">重试</n-button></template>
    </n-alert>
    <div v-if="step">
      <StepHeader :title="step.label" :description="step.description">
        <template #status>
          <span class="header-badges">
            <!-- 按状态切换整个提示组件，trigger 必须始终有一个实际元素。 -->
            <n-tooltip v-if="step.status !== 'done'" trigger="hover" :delay="200" placement="top">
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
            <n-tooltip v-else trigger="hover" :delay="200" placement="top">
              <template #trigger>
                <n-tag size="small" type="warning" :bordered="false">{{ step.freshness_label || (key === 'tts' ? '仍有待配音句' : '需重跑') }}</n-tag>
              </template>
              {{ step.freshness_reason || (key === 'tts' ? '本次任务已完成；还有选用配音的句子缺少有效音频，可继续合成所选或补齐配音。' : FRESHNESS_HINTS.stale) }}
            </n-tooltip>
            <AutoRunBadge :workflow-status="nav.workflow?.run?.status || null"
                          :chain="nav.chain" :running-label="null" />
          </span>
        </template>
        <template #actions>
        <n-space class="step-action-toolbar" :data-action-mode="actionMode">
          <n-button size="small" quaternary title="在文件浏览器中打开本步骤目录"
                    @click="revealStep">📁</n-button>
          <template v-if="actionMode === 'running'">
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
          <n-button v-else-if="actionMode === 'deps'" type="primary"
                    :loading="busy" :disabled="nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty"
                    @click="runWithDependencies">
            {{ key === 'tts' ? '同步全项目字幕与配音…' : dependencyActionLabel(actionBlockers) + '…' }}
          </n-button>
          <n-button v-else-if="key === 'assemble'" type="primary" @click="chooseOutputs">选择成品方案</n-button>
          <template v-else-if="actionMode === 'chain-hold'">
            <n-tag size="small" type="warning" :bordered="false"
                   :title="'自动运行计划已挂起, 剩余 ' + (nav.chain?.remaining.length || 0) + ' 步'">⛓ 计划挂起</n-tag>
            <n-button type="primary" :loading="busy" :disabled="nav.busy || !canRun || dirMissing"
                      title="重试本步; 成功后自动继续剩余计划, 再失败则再次挂起"
                      @click="run(false, true)">重试并继续剩余步骤…</n-button>
            <n-button secondary :loading="busy" :disabled="nav.busy || !canRun || dirMissing"
                      title="只重跑本步, 不继续剩余计划 (计划保留挂起)"
                      @click="run(false)">仅重试本步</n-button>
          </template>
          <n-button v-else-if="actionMode === 'fill-voice'" type="primary" :loading="busy"
                    :disabled="nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty"
                    @click="run(false)">补齐全项目配音</n-button>
          <n-button v-else-if="actionMode === 'fresh-done'" secondary
                    :loading="busy" :disabled="nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty"
                    :title="key === 'tts'
                      ? '重做全项目中选用配音的句子，与下方勾选无关；执行前再次确认'
                      : '忽略缓存并重新生成本步产物'"
                    @click="rerunDialog()">{{ key === 'tts' ? '重做全项目配音…' : '重做本步产物…' }}</n-button>
          <n-button v-else type="primary" :loading="busy"
                    :disabled="nav.busy || !canRun || dirMissing"
                    @click="run(false)">
            {{ key === 'tts' ? '生成全项目配音' : ['done', 'stale', 'failed', 'interrupted'].includes(step.status) ? '重新运行本步' : '运行本步' }}
          </n-button>
        </n-space>
        </template>
      </StepHeader>
      <n-alert v-if="WORKFLOW_BUSY.includes(nav.workflow?.run?.status)" type="info" :show-icon="false" style="margin-bottom:14px">
        自动流程正在执行：{{ nav.workflow.run.phase }}。单步重跑暂不可用；停止完整流程请回总览。
        <template #action><n-button size="small" @click="router.push(`/projects/${uid}#automatic-workflow`)">查看完整流程</n-button></template>
      </n-alert>
      <n-alert v-if="parametersDirty" type="warning" :show-icon="false" style="margin-bottom: 14px">
        参数尚未保存，运行入口已暂停。请在「步骤参数」保存后再生成；不会自动用旧参数运行。
        <template #action><n-button size="small" @click="detailSection = 'parameters'">查看未保存参数</n-button></template>
      </n-alert>
      <n-text v-if="key === 'tts' && route.query.panel !== 'subtitles'" depth="3" style="display:block;margin-bottom:14px;font-size:12px">
        顶部操作作用于全项目；只处理部分句子，请使用下方「生成勾选句配音」。字幕显示模式只影响试听，不决定是否烧录成片。
      </n-text>
      <n-tag v-if="running && step.pausing && !step.cancelling && !cancelPending" size="small" type="warning"
             :bordered="false" style="margin-left: 10px">
        {{ paused ? '已暂停 · 等待继续' : '正在暂停 · 当前处理完成后暂停' }}
      </n-tag>
      <div v-if="running && !step.progress_indeterminate" class="step-progress" style="margin-top: 12px">
        <n-progress type="line" :percentage="smoothProgress" :show-indicator="false"
                    :height="14" :processing="!paused" />
        <span class="step-progress-value">{{ step.progress_estimated ? '预计 ' : '' }}{{ formatProgress(smoothProgress) }}</span>
        <span v-if="formatEta(step?.eta_seconds, step?.eta_scope)" class="step-progress-eta">{{
          formatEta(step?.eta_seconds, step?.eta_scope) }}</span>
      </div>

      <n-text v-if="key === 'tts' && step.synthesis_task && (running || (route.query.panel !== 'subtitles' && step.synthesis_task.scope === 'selected'))" depth="3" style="display:block;margin-top:8px;font-size:12px">
        本次{{ step.synthesis_task.scope === 'selected' ? '所选' : '配音' }} {{ step.synthesis_task.total }} 句 · 已完成 {{ step.synthesis_task.completed }} 句<span v-if="step.synthesis_task.failed"> · 失败 {{ step.synthesis_task.failed }} 句</span><span v-if="running"> · 后台执行，刷新或切换页面可继续查看</span>
      </n-text>

      <div v-if="running && step.progress_phase" style="display:flex;align-items:center;gap:8px;margin-top:8px">
        <n-spin v-if="step.progress_indeterminate && !paused" :size="14" />
        <n-text depth="3" style="font-size:12px">{{ formatProgressDetail(step) }}</n-text>
      </div>

      <ProgressActivity v-if="running" :activity="nav.activityFor(key)" />

      <!-- 幽灵项目门禁: 目录缺失时明确禁用并说明 -->
      <n-alert v-if="dirMissing" type="error" style="margin-top: 14px"
               title="项目目录不在磁盘上 —— 运行已禁用">
        目录可能被移动或删除。从回收站还原目录后刷新本页即可恢复运行;
        确认不要了可回项目列表删除该项目清理登记。
      </n-alert>

      <!-- 依赖门禁: 明确指出缺哪些上游；字幕与配音合并展示一次 -->
      <DependencyNotice v-if="key !== 'assemble' && !(key === 'tts' && route.query.panel === 'subtitles')" :uid="uid" :items="actionBlockers">
        <n-button size="small" type="primary"
                  :disabled="nav.busy || dirMissing || dubbingBusy || dubbingDirty || parametersDirty"
                  @click="runWithDependencies">
          {{ key === 'tts' ? '同步全项目字幕与配音…' : dependencyActionLabel(actionBlockers) + '…' }}
        </n-button>
      </DependencyNotice>

      <!-- 错误三件套 -->
      <n-alert v-if="showStepError(step)" type="error" :show-icon="false" style="margin-top: 14px">
        <n-text strong>{{ step.error.message }}</n-text><br />
        <n-text depth="3" style="font-size: 12px">[{{ step.error.code }}]
          {{ step.error.suggestion }}</n-text><br />
        <n-text depth="3" style="font-size: 12px">
          请查看步骤日志中的具体原因与文件保留、清理记录。
        </n-text>
      </n-alert>

      <!-- 产出预览: 有对应产物时直接展示 -->
      <div ref="previewRoot">
      <DubbingPanel v-if="key === 'tts'" :key="uid" :uid="uid" :suspended="previewSuspended || mediaRebuilding"
                    @busy-change="dubbingBusy = $event" @dirty-change="dubbingDirty = $event" />
      <section v-if="key === 'assemble'" ref="outputsArea" tabindex="-1" aria-label="正式成品方案">
      <AssemblyVersionWorkspace :key="uid" :uid="uid" :disabled="nav.busy || dirMissing || parametersDirty" />
      <AssembleOutputsPanel :uid="uid" history-only
                            :artifacts="step.artifacts ?? []"
                            :source-path="assembleSourcePath"
                            :subtitle-path="assembleSubtitlePath"
                            :subtitle-name="assembleSubtitleName"
                            :disabled="nav.busy || dirMissing || parametersDirty" @choice-change="assemblyChoice = $event"
                            @recycle-active="value => { if (value) releaseMediaIn(previewRoot); previewSuspended = value }" />
      </section>
      <n-card v-if="key !== 'tts' && preview && !previewSuspended && !mediaRebuilding" size="small" title="产出预览" style="margin-top: 14px">
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
          <video v-release-media :key="subtitleVideo" class="vid" :src="subtitleVideo" controls preload="metadata"
                 @loadedmetadata="onMeta">
            <track v-if="subtitlePreviewUrl" :key="subtitlePreviewUrl"
                   kind="subtitles" label="当前步骤字幕"
                   srclang="und" :src="subtitlePreviewUrl" @load="onSubtitleLoaded" default />
          </video>
          <n-text depth="3" style="display:block; margin-top:8px; font-size:12px">
            语音转文字、语义整理、翻译和字幕导出页面会叠加当前步骤生成的字幕；可拖动进度检查文字与时间轴是否同步。
            <template v-if="key === 'transcribe' && transcribePreviewMode === 'word'">当前为词级预览：视频字幕和下方时间轴均来自 words.json，按词级时间戳逐个显示；正式的 src.srt 短语产物仍保留在产物清单中。</template>
            <template v-else-if="key === 'transcribe'">当前为短语级预览：视频字幕和下方文本使用识别后的短语锚点；词级时间轴仍保存在 words.json 中。</template>
            <template v-else-if="key === 'translate'">当前为译文预览：视频字幕和下方文本均来自 translate.txt 的编号译文；翻译重跑后会自动刷新。</template>
            <template v-if="running">步骤运行中会自动检查新产物并刷新字幕（约每 1.5 秒一次）。</template>
          </n-text>
          <n-alert v-if="subtitlePreviewError" type="warning" :show-icon="false"
                   style="margin-top:8px">{{ subtitlePreviewError }}</n-alert>
        </template>
        <template v-if="key === 'download' && media.found">
          <div class="prev-row">
            <div class="vid-col">
              <video v-if="preview.url" v-release-media :key="preview.version || preview.url" class="vid" :src="preview.url" controls
                     preload="metadata" @loadedmetadata="onMeta" />
            </div>
            <div class="info">
              <MediaInfo :media="media" />
            </div>
          </div>
        </template>
        <AudioTracksPlayer v-else-if="preview.kind === 'tracks'"
                           :tracks="preview.audios" :video-url="preview.videoUrl" />
        <video v-else-if="preview.kind === 'video' && preview.url" v-release-media :key="preview.version || preview.url" class="vid"
               :src="preview.url" controls preload="metadata"
               @loadedmetadata="onMeta" />
        <template v-if="preview.kind === 'audio'">
          <div v-for="a in preview.audios ?? []" :key="`${a.name}:${a.version || ''}`" class="audio-row">
            <n-text depth="3" style="font-size: 12px; width: 76px">{{ a.label }}</n-text>
            <audio v-release-media :src="a.url" controls style="width: 100%; height: 36px" />
          </div>
        </template>
        <pre v-if="preview.kind === 'text'" class="textprev">{{ previewText || '(空)' }}</pre>
        <n-text v-if="preview.kind === 'text' && previewText.length >= 6000"
                depth="3" style="font-size: 11px">
          (内容较长, 仅显示前 6000 字符; 完整文件见项目目录)
        </n-text>

      </n-card>
      <VideoInfoPanel v-if="key === 'download'" :key="uid" :uid="uid" :available="!!videoInfoArtifact"
                      :version="videoInfoVersion" :automation="nav.commentsProgress" @show-diagnostics="showLlmDiagnostics" />

      </div>
      <nav v-show="key !== 'tts' || route.query.panel !== 'subtitles'" class="section-nav step-detail-nav" aria-label="步骤详情分类">
        <button v-if="cfgRows.length && key !== 'tts'" :aria-pressed="detailSection === 'parameters'" @click="detailSection = 'parameters'">{{ key === 'assemble' ? '项目默认参数' : '步骤参数' }}</button>
        <button :aria-pressed="detailSection === 'logs'" @click="detailSection = 'logs'">运行日志<span v-if="running" class="live-dot" /></button>
        <button v-if="llmStep || key === 'download'" :aria-pressed="detailSection === 'llm'" @click="detailSection = 'llm'">LLM 对话<span v-if="llmStep && running || key === 'download' && nav.commentsProgress?.state === 'running'" class="live-dot" /></button>
        <button :aria-pressed="detailSection === 'files'" @click="detailSection = 'files'">输入与产物 · {{ step.artifacts?.length || 0 }}</button>
      </nav>
      <div v-show="key !== 'tts' || route.query.panel !== 'subtitles'" class="step-detail-content">
        <!-- 日志 -->
        <div v-show="detailSection === 'llm'" id="llm-conversation" ref="llmSection">
          <CommentDiagnostics v-if="key === 'download' && detailSection === 'llm'" :uid="uid"/>
          <n-empty v-if="llmStep && !running && !llmMessages.length" description="暂无对话记录；运行后可查看模型输入、输出和纠错过程。"/>
          <!-- LLM 实时对话: 产出预览之后展示当前请求详情 -->
      <n-card v-if="llmStep && (running || llmMessages.length)" size="small"
              title="LLM 实时对话" style="margin-top: 14px">
        <template #header-extra>
          <n-tag v-if="running" size="small" type="info" :bordered="false">{{ llmStatus || '处理中…' }}</n-tag>
          <n-tag v-else size="small" :bordered="false">本轮已结束</n-tag>
        </template>
        <n-alert v-if="key === 'split' && step?.cfg?.split?.use_llm !== false"
                 type="info" :show-icon="false" style="margin-bottom: 10px">
          已启用<strong>整体语义重组</strong>：每个 token 窗口会按“时间轴粒度”设置发送短语级或词级时间、
          长停顿提示和相邻上下文，由模型完成纠错、合并、拆分和时间轴整理；结果采用“序号. 开始秒-结束秒: 原文片段”，便于字幕对齐。
        </n-alert>
        <n-alert v-if="key === 'translate'" type="info" :show-icon="false" style="margin-bottom: 10px">
          当前采用<strong>串行滚动窗口翻译</strong>：在上下文预算内尽量整篇交给所选模型；
          超过预算时按顺序分窗口，每个窗口返回译文和交接摘要，下一窗口自动接收上一窗口摘要后继续翻译。
          正常情况下每个窗口只请求一次；只有格式或缺行校验失败时，才在同一窗口内针对性补齐，
          不再额外执行“两步直译”或“全局审校”；下方视频会叠加当前翻译产物。
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
  <div class="llm-history llm-conversation-history">
    <div v-for="row in llmMessages" :key="row.id"
         :class="['llm-message', `llm-message-${row.role}`, row.kind ? `llm-message-validation-${row.kind}` : '']">
      <div class="llm-message-head">
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
      </div>
      <details v-if="row.role === 'reasoning'" class="llm-reasoning">
        <summary>展开模型思考（{{ row.content.length.toLocaleString() }} 字符）</summary>
        <pre class="llm-message-content" :ref="(el) => setLlmOutputEl(row.id, el)"
             @scroll="onLlmOutputScroll(row.id, $event)"
             v-text="displayLlmContent(row) || '（等待思考内容）'"></pre>
      </details>
      <pre v-else class="llm-message-content"
           :ref="row.role === 'assistant' ? (el) => setLlmOutputEl(row.id, el) : undefined"
           @scroll="row.role === 'assistant' ? onLlmOutputScroll(row.id, $event) : undefined"
           v-text="row.role === 'assistant' ? (displayLlmContent(row) || '（等待内容）') : (row.content || '（等待内容）')"></pre>
    </div>
  </div>
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
            <n-text v-if="key === 'assemble'" depth="3" style="display:block;margin-bottom:14px;font-size:12px">仅影响选择“跟随项目默认”的版本；本版本覆盖值保持不变。</n-text>
            <ProjectParameterForm :uid="uid" :step-key="key" :disabled="nav.busy"
                                  :exclude-paths="key === 'assemble' ? [...SUBTITLE_STYLE_PATHS, 'assemble.burn_subtitles'] : undefined"
                                  @draft-change="draft = $event" @fields-change="editablePaths = $event"
                                  @dirty-change="parametersDirty = $event"
                                  @saved="nav.poll()" />
            <n-descriptions v-if="extraCfgRows.length" :column="1" label-placement="left"
                            size="small" bordered>
              <n-descriptions-item v-for="row in extraCfgRows" :key="row.k" :label="row.k">
                {{ row.v }}
              </n-descriptions-item>
            </n-descriptions>

          </n-card>
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
  width: auto;
  height: auto;
  max-width: 100%;
  max-height: 340px;
  aspect-ratio: 16 / 9;
  border-radius: 8px;
  background: #000;
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
.step-progress { position: relative; }
.step-progress-eta { margin-left: 8px; font-size: 12px; opacity: 0.65; }
.step-progress :deep(.n-progress) { display: block; width: 100%; }
.step-progress-value {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  z-index: 1; pointer-events: none; color: #fff; font-size: 11px;
  line-height: 14px; font-variant-numeric: tabular-nums; text-shadow: 0 1px 2px #000;
}
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
</style>
