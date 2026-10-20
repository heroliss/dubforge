<script setup lang="ts">
import { showStepError, stepDisplayStatus } from '../utils/stepFeedback'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { dependencyActionLabel, runPlannedSteps } from '../utils/runPlan'
// 总览: 项目信息 + 原视频 (预览+信息) + 项目文件 + 步骤清单
// 各步骤按需选用；运行门禁由后端解析实际输入产物。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NEmpty, NH1, NLayoutContent, NProgress, NSelect, NSpin,
  NSpace, NTable, NTag, NText, NTooltip, useMessage, useDialog,
} from 'naive-ui'
import {
  useNavStore, STEP_ORDER, STATUS_TAG, STATUS_TEXT,
  formatEta,
} from '../stores/nav'
import MediaInfo from '../components/MediaInfo.vue'
import VideoControls from '../components/VideoControls.vue'
import FilePathLink from '../components/FilePathLink.vue'
import ExecutionProgress from '../components/ExecutionProgress.vue'
import { executionForStep, workflowExecutionStep } from '../utils/executionProgress'
import { executionEtaSeconds } from '../utils/executionEta'
import PublicationProgress from '../components/PublicationProgress.vue'
import FreshnessDetails from '../components/FreshnessDetails.vue'
import { freshnessStatus, needsFreshnessDetails } from '../utils/freshness'
import { stoppingLabel } from '../utils/progressActivity'
import WorkflowPanel from '../components/WorkflowPanel.vue'
import ChainStatus from '../components/ChainStatus.vue'
import ProjectTemplateBinding from '../components/ProjectTemplateBinding.vue'
import { fmtSize } from '../utils/format'
import { WORKFLOW_OPEN, WORKFLOW_BUSY, supersedingPublicationUpdate, workflowPublication, workflowRunStage } from '../utils/workflow'
import { createLatestRefresh, releaseMedia, vReleaseMedia } from '../utils/preview'
import { releaseBatchLockMessage } from '../utils/releaseBatch'
import { WORKFLOW_STAGES, stageNumber, stagePath } from '../utils/workflowStages'
import { publicationUpdateProgress } from '../utils/publicationUpdate'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()
const uid = route.params.uid as string
const automaticCurrentStep = computed(() => WORKFLOW_BUSY.includes(nav.workflow?.run?.status)
  ? workflowExecutionStep(nav.workflow?.run, nav.workflowPlan, nav.steps)?.key : null)
// 后端与前端共用完整管线定义，工作台位于配音计划与批量配音之间。
const orderedSteps = computed(() => STEP_ORDER.map(k => nav.stepMap[k]).filter(Boolean))
const displayedSteps = computed(() => WORKFLOW_STAGES.filter(stage=>stage.kind!=='publication').map(stage=>
  stage.kind==='comments'?{key:stage.key,label:stage.label}:nav.stepMap[stage.key]?{...nav.stepMap[stage.key],label:stage.label}:null).filter(Boolean))



// ---- 原视频 ----
const media = ref<any>({ found: false })
const videoEl = ref<HTMLVideoElement | null>(null)
const previewSuspended = ref(false)
const downloadRunning = computed(() => ['running', 'queued'].includes(nav.stepMap.download?.status))
let mediaFailed = false
const mediaLoader = createLatestRefresh(async signal => {
  const r = await fetch(`/api/projects/${uid}/media`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取媒体信息失败')
  return r.json()
}, value => { media.value = value; mediaFailed = false }, () => { mediaFailed = true })
const sourceChapters = ref<any[]>([])
// 原视频章节与资料面板共用 download/video-info 的只读快照；不重复触发抓取。
const videoInfoLoader = createLatestRefresh(async signal => {
  const r = await fetch(`/api/projects/${uid}/steps/download/video-info`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取视频章节失败')
  const payload = await r.json()
  const rows = payload?.info?.content?.chapters
  return Array.isArray(rows) ? rows : []
}, value => { sourceChapters.value = value }, () => { sourceChapters.value = [] })
onUnmounted(() => { mediaLoader.cancel(); videoInfoLoader.cancel() })
// 项目目录是否还在磁盘上 (被移走/删除 → 头部标注)
const dirMissing = computed(() => nav.dirMissing)
const mediaVersion = computed(() => {
  const artifact = nav.stepMap.download?.artifacts?.find((a: any) => a.name === 'video.mp4')
  return String(artifact?.sha256 || artifact?.size || 0)
})
let lastMediaSignature = ''
watch(() => nav.stepMap.download, download => {
  if (downloadRunning.value) { mediaLoader.cancel(); videoInfoLoader.cancel(); sourceChapters.value = []; return }
  const signature = `${download?.status}:${mediaVersion.value}`
  const changed = signature !== lastMediaSignature
  lastMediaSignature = signature
  if (changed || mediaFailed || (!media.value.found && download?.status === 'done')) {
    void mediaLoader.refresh(changed)
  }
  if (!downloadRunning.value && media.value.found && changed) void videoInfoLoader.refresh(true)
}, { immediate: true })
watch(() => `${media.value.found}:${mediaVersion.value}`, (signature, previous) => {
  if (!downloadRunning.value && media.value.found && signature !== previous) void videoInfoLoader.refresh(true)
})
const videoSrc = computed(() =>
  media.value.found ? `/api/projects/${uid}/workbench/video?v=${mediaVersion.value}` : '')

async function stopPreviewForRun(key: string) {
  videoEl.value?.pause()
  if (key !== 'download') return
  if (videoEl.value) releaseMedia(videoEl.value)
  previewSuspended.value = true
  await nextTick()
}

// ---- 项目文件 (排序浏览 + 资源管理器) ----
const files = ref<any[]>([])
const fileDir = ref('')
const sortBy = ref<'name' | 'mtime' | 'ctime' | 'size'>('mtime')
const sortDesc = ref(true)
const SORT_OPTS = [
  { label: '按修改时间', value: 'mtime' },
  { label: '按创建时间', value: 'ctime' },
  { label: '按文件名', value: 'name' },
  { label: '按大小', value: 'size' },
]
async function loadFiles() {
  try {
    const r = await fetch(`/api/projects/${uid}/files`, { cache: 'no-store' })
    if (r.ok) { const j = await r.json(); files.value = j.entries ?? []; fileDir.value = j.dir }
  } catch { /* 忽略 */ }
}
onMounted(loadFiles)
watch(() => nav.steps.map((s: any) =>
  `${s.key}:${(s.artifacts ?? []).map((a: any) => `${a.name}:${a.sha256 || a.size || ''}`).join(',')}`
).join('|'), () => loadFiles())
const sortedFiles = computed(() => {
  const arr = [...files.value]
  const dir = sortDesc.value ? -1 : 1
  arr.sort((a: any, b: any) => sortBy.value === 'name'
    ? dir * a.file.localeCompare(b.file)
    : dir * ((a[sortBy.value] || 0) - (b[sortBy.value] || 0)))
  return arr
})
function fmtT(ts?: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
// 元数据到达: 用真实宽高比替换 16/9 兜底 —— 视频盒与内容比例一致, 不出黑边
function onMeta(e: Event) {
  const v = e.target as HTMLVideoElement
  if (v.videoWidth && v.videoHeight) {
    v.style.aspectRatio = `${v.videoWidth} / ${v.videoHeight}`
  }
}
async function reveal() {
  const r = await fetch(`/api/projects/${uid}/reveal`, { method: 'POST' })
  if (!r.ok) message.error('打开文件管理器失败')
}

// ---- 步骤运行 ----
// 总览只保留“怎么继续”的动作；暂停/强制结束等精细控制放在步骤详情页。
const busyKey = ref('')
const cancelPending = ref<Record<string, boolean>>({})
const editorSyncChecking = ref(false)
const subtitleExportOptional = computed(() => {
  const saved = nav.autoSteps, plan = nav.workflowPlan, run = nav.workflow?.run
  const knownPlan = !!run?.id && plan?.id === run.id && Array.isArray(plan.steps)
    && plan.steps.every((key: unknown) => typeof key === 'string' && key.trim())
  // 当前冻结范围或保存清单显式包含时保留待运行；身份不明不能推断为可选。
  if ((knownPlan && plan.steps.includes('subtitles')) || (Array.isArray(saved) && saved.includes('subtitles'))) return false
  return Array.isArray(saved) || knownPlan
})

// 后端完成取消后释放本地锁；否则快速重跑时会把上一次的取消状态带过去。
watch(() => orderedSteps.value.map((s: any) => `${s.key}:${s.status}:${!!s.cancelling}`).join('|'), () => {
  const next = { ...cancelPending.value }
  for (const key of Object.keys(next)) {
    const status = orderedSteps.value.find((s: any) => s.key === key)?.status
    if (status !== 'running' && status !== 'queued') delete next[key]
  }
  cancelPending.value = next
})
function blockersOf(key: string): any[] {
  return nav.stepMap[key]?.unready_inputs ?? []
}

// 状态文案合并“完成”与“是否最新”，总览不再并排显示两个标签。
function statusText(step: any): string {
  const freshness=freshnessStatus(step)
  if(freshness)return freshness
  return STATUS_TEXT[stepDisplayStatus(step)] || step?.status || '待运行'
}
function statusTagType(step: any): 'default' | 'success' | 'error' | 'warning' | 'info' {
  if (step?.status === 'done') return step.fresh ? 'success' : 'warning'
  return STATUS_TAG[stepDisplayStatus(step)] || 'default'
}
function runTitle(step: any): string {
  if (nav.archived) return '恢复后运行'
  if (['failed', 'interrupted', 'stopped'].includes(step?.status)) return '重新运行'
  if (step?.status === 'done') return '更新结果'
  return '运行'
}
function editorTaskText(step: any): string {
  if (step?.key === 'subtitles' && step.status === 'pending' && subtitleExportOptional.value) return '按需导出'
  return statusText(step)
}
function editorTaskType(step: any): 'default' | 'success' | 'error' | 'warning' | 'info' {
  return statusTagType(step)
}

async function run(step: any, force = false) {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再运行。'); return }
  if (nav.busy) return
  if (busyKey.value === step.key || mapCancel(step.key)) return
  busyKey.value = step.key
  await stopPreviewForRun(step.key)
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${step.key}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { message.error(j.message || `启动失败 (HTTP ${r.status})`); return }
    if (j.status === 'skipped') message.info(j.message)
    else message.success(force ? '已开始强制重跑' : `已启动: ${step.label}`)
    nav.stepStarted(uid, step.key, j)
    refreshInBackground(() => nav.poll())
  } catch { message.error('启动请求失败 (网络异常)') } finally {
    busyKey.value = ''
    previewSuspended.value = false
  }
}

async function runSelection(includeDependencies = false, onlyKey?: string) {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再运行。'); return }
  if (nav.busy || nav.autoStepsSaving || dirMissing.value) return
  const steps = onlyKey ? [onlyKey] : []
  if (!steps.length) return
  await runPlannedSteps(uid, [...steps], includeDependencies, message, dialog,
    async (plan) => { await stopPreviewForRun(plan.includes('download') ? 'download' : plan[0]) },
    async () => { previewSuspended.value = false; await nav.poll() }, () => nav.archived)
}

// 字幕导出和逐句配音属于同一个编辑工作流；同步时一并带到最新。
async function runEditorWorkflow() {
  if (nav.archived) { message.info('项目已归档，请先恢复项目后再运行。'); return }
  if (nav.busy || dirMissing.value || editorSyncChecking.value) return
  editorSyncChecking.value = true
  try {
    await runPlannedSteps(uid, ['subtitles', 'tts'], true, message, dialog,
      async () => { await stopPreviewForRun('tts') },
      async () => { previewSuspended.value = false; await nav.poll() }, () => nav.archived)
  } finally { editorSyncChecking.value = false }
}

function mapCancel(key: string): boolean {
  return !!cancelPending.value[key]
}

async function stopDialog(step: any) {
  if (mapCancel(step.key)) return
  const targetJobId = step.job_id ?? null
  dialog.warning({
    title: `停止「${step.label}」？`,
    content: '正在运行的子进程将立即终止；进程内计算会在当前单元结束后退出。已完成的正式产物保留。',
    positiveText: '确认停止',
    negativeText: '继续运行',
    onPositiveClick: async () => {
      await doCancel(step, targetJobId)
    },
  })
}

async function doCancel(step: any, expectedJobId: number | null = null) {
  if (mapCancel(step.key)) return
  cancelPending.value = { ...cancelPending.value, [step.key]: true }
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${step.key}/cancel`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_id: expectedJobId }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) {
      clearCancel(step.key); message.error(j.message || '停止信号发送失败'); return
    }
    if (j.status === 'stale_action' || j.status === 'not_running') {
      clearCancel(step.key)
      message.info(j.message || '步骤已经结束，无需停止')
      refreshInBackground(() => nav.poll())
      return
    }
    if (j.status === 'already_cancelling') {
      message.info('该步骤已经在停止中，请等待它退出')
      return
    }
  } catch {
    clearCancel(step.key); message.error('停止信号发送失败'); return
  }
  message.info('停止信号已发送；长时间无响应可在步骤页处理')
}

function clearCancel(key: string) {
  const next = { ...cancelPending.value }
  delete next[key]
  cancelPending.value = next
}

// 项目动态状态 (running/queued 时附带步骤 key, 头部胶囊可点击直达该步骤页)
const headerStatus = computed(() => {
  const batch = nav.releaseBatch
  if (batch && ['running','pausing','stopping'].includes(batch.status)) return {
    text: `顺序发布 · 第 ${batch.position}/${batch.total} 项${batch.phase ? ' · '+batch.phase : ''}`,
    type: batch.status === 'running' ? 'info' : 'warning', key: null,
  }
  const flow = nav.workflow?.run
  if (flow && WORKFLOW_OPEN.includes(flow.status)) return {
    text: `${workflowRunStage(flow).text} · ${flow.phase}`, type: workflowRunStage(flow).tone, key: null,
  }
  const update = publicationUpdateProgress(nav.publication)
  if (update && (update.active || ['failed', 'unknown', 'paused'].includes(update.status))) return {
    text: `${update.text}${update.progress !== null ? ` · ${Math.round(update.progress)}%` : ''}`,
    type: update.active ? 'info' : update.status === 'failed' ? 'error' : 'warning', key: null,
  }
  const run = orderedSteps.value.find((s: any) => s.status === 'running')
  const que = orderedSteps.value.find((s: any) => s.status === 'queued')
  const active = run || que
  if (active) {
    return { text: run ? (run.cancelling ? `${stoppingLabel(run)} · ${run.label}…`
                                               : `正在「${run.label}」…`)
                        : `「${que.label}」排队中…`,
             type: run ? 'info' : 'warning', key: active.key as string | null }
  }
  if (flow && ['completed','ready'].includes(flow.status)) return {
    text: flow.status==='completed'?'上次项目运行已完成':workflowRunStage(flow).text,
    type: 'success', key: null,
  }
  // 导航的持久化项目状态可能来自上次失败；未取得步骤事实时不能据此判断当前失败。
  if (!nav.stepsLoaded) {
    if (orderedSteps.value.some((step:any)=>step.status==='failed')) return {
      text: '有失败步骤', type: 'error', key: null,
    }
    if (flow && ['failed','attention','paused'].includes(flow.status)) {
      if (supersedingPublicationUpdate(nav.workflow, nav.publication)) {
        const delivery = workflowPublication(nav.workflow, nav.publication)
        return { text: delivery.text, type: delivery.tone, key: null }
      }
      const stage = workflowRunStage(flow)
      return { text: `${stage.text} · ${flow.phase || '项目运行'}`, type: stage.tone, key: null }
    }
    return {
      text: nav.stepsError || nav.workflowError || nav.projectError ? '运行状态待核对' : '正在读取运行状态',
      type: nav.stepsError || nav.workflowError || nav.projectError ? 'warning' : 'default', key: null,
    }
  }
  const map: Record<string, [string, string]> = {
    done: ['制作已完成', 'success'], failed: ['有失败步骤', 'error'],
    processing: ['部分完成', 'default'], created: ['待开始', 'default'],
  }
  const [text, type] = map[nav.status] ?? [nav.status, 'default']
  return { text, type, key: null }
})
</script>

<template>
  <div class="workspace-page pipeline-page">
    <n-alert v-if="nav.stepsError || nav.projectError" type="error" style="margin-bottom: 16px">
      {{ nav.stepsError || nav.projectError }}
      <template #action><n-button size="small" @click="nav.poll(true)">重试</n-button></template>
    </n-alert>
    <n-spin v-if="!nav.stepsLoaded && !nav.stepsError" size="small" description="正在读取步骤状态…" style="margin-bottom: 16px" />
    <div class="page-head">
      <div class="project-heading">
        <span class="page-kicker">制作流程 / 总览</span>
        <n-h1>{{ nav.title || '正在读取项目…' }}</n-h1>
        <n-text depth="3" class="subline">完整流程按项目设置执行；各步骤也可单独检查与运行。</n-text>
      </div>
      <div class="header-status">
        <!-- 运行/排队中: 变成可点按钮, 直达该步骤页; 其余状态仍是纯标签 -->
        <n-button v-if="headerStatus.key" size="small"
                  :type="headerStatus.type as any" secondary
                  @click="router.push(`/projects/${uid}/steps/${headerStatus.key}`)">
          {{ headerStatus.text }} →
        </n-button>
        <n-tag v-else size="small" :type="headerStatus.type as any">{{ headerStatus.text }}</n-tag>
        <n-tooltip v-if="dirMissing" trigger="hover" :delay="0" placement="top">
          <template #trigger>
            <n-tag size="small" type="error" :bordered="false">⚠ 项目目录缺失</n-tag>
          </template>
          项目目录不在磁盘上 (可能被移动或删除)。从回收站还原后刷新页面标记即消失。
        </n-tooltip>
      </div>
    </div>
    <ProjectTemplateBinding :uids="[uid]" @changed="refreshInBackground(() => nav.poll())" />
    <WorkflowPanel :uid="uid" @started="nav.poll()" />
    <n-alert v-if="nav.releaseBatch && ['running','pausing','stopping'].includes(nav.releaseBatch.status)" type="info" style="margin-top:12px">
      {{ releaseBatchLockMessage(nav.releaseBatch) }}
      <router-link :to="`/#release-batch-${nav.releaseBatch.id}`">查看批次进度 →</router-link>
    </n-alert>
    <ChainStatus v-if="nav.chain" :uid="uid" :chain="nav.chain" hide-recovery style="margin-top: 12px" />
    <!-- ============ 原视频 ============ -->
    <n-card size="small" title="原视频" style="margin-top: 16px">
      <template #header-extra>
        <n-space align="center">
          <n-text v-if="media.found && media.external" depth="3" style="font-size: 11.5px">
            外部引用 (零拷贝, 未复制进项目)
          </n-text>
          <n-button size="tiny" @click="reveal">📁 在资源管理器中显示</n-button>
        </n-space>
      </template>
      <div class="prev-row" v-if="media.found">
        <div class="vid-col">
          <div v-if="videoSrc && !downloadRunning && !previewSuspended" class="video-shell">
            <video ref="videoEl" v-release-media :key="videoSrc" class="vid" :src="videoSrc" preload="metadata"
                   @loadedmetadata="onMeta" />
            <VideoControls :media="videoEl" :chapters="sourceChapters" :duration="media.duration" :frame-rate="media.fps" />
          </div>
          <n-empty v-else-if="downloadRunning || previewSuspended" description="正在更新视频，完成后自动刷新预览" />
        </div>
        <div class="info">
          <MediaInfo :media="media" />
        </div>
      </div>
      <n-empty v-else description="还没有视频 —— 运行「获取视频」后这里会显示信息与预览"
               size="small" style="margin: 8px 0" />
    </n-card>

    <!-- 各步骤的状态、配置入口与局部制作操作。 -->
    <n-card size="small" style="margin-top: 14px"
            title="步骤进度与手动制作">
      <n-text depth="3" style="display:block; margin-bottom:12px; font-size:12px; line-height:1.8">各步骤保存处理参数与可选功能；项目运行按目标成片自动补齐依赖。下方的单步操作只更新对应制作结果。{{ nav.archived ? '已归档项目需先恢复后才能运行。' : '' }}</n-text>
      <div class="rows">
        <div v-for="s in displayedSteps" :key="s.key" class="row">
          <span class="step-index">{{ String(stageNumber(s.key)).padStart(2, '0') }}</span>
          <template v-if="s.key==='comments'">
            <router-link class="lbl steplink" :to="stagePath(uid,'comments')">上屏评论</router-link>
            <n-tag size="small" :type="nav.commentsDisplay.type" :title="nav.commentsDisplay.detail">{{ nav.commentsDisplay.label }}</n-tag>
            <span class="comment-stage-note">供启用评论叠加的成片版本共用；自动准备与手动准备复用同一流程。</span>
            <span class="ops"><n-button size="small" secondary @click="router.push(stagePath(uid,'comments'))">选择与确认评论 →</n-button></span>
          </template>
          <template v-else-if="s.key === 'tts'">
            <router-link class="lbl steplink" title="进入字幕与配音页" :to="`/projects/${uid}/steps/tts`">字幕与配音</router-link>
            <span class="editor-tasks">
              <span v-for="task in ['subtitles', 'tts']" :key="task" class="editor-task">
                <span :title="task === 'subtitles' ? '单独生成可下载的字幕文件；成片烧录按目标版本设置执行。' : ''">{{ task === 'tts' ? '合成配音' : '单独导出字幕' }}</span>
                <FreshnessDetails v-if="needsFreshnessDetails(nav.stepMap[task])" :uid="uid" :step-key="task" :label="nav.stepMap[task].label"
                                  :status-text="editorTaskText(nav.stepMap[task])" :summary="nav.stepMap[task].freshness_reason" />
                <n-tag v-else-if="nav.stepMap[task]" size="small"
                       :type="editorTaskType(nav.stepMap[task])">
                  {{ editorTaskText(nav.stepMap[task]) }}
                </n-tag>
              </span>
            </span>
            <n-button size="small" type="primary" secondary
                      :loading="editorSyncChecking" :disabled="nav.archived || nav.busy || dirMissing || editorSyncChecking"
                      @click="runEditorWorkflow">{{ editorSyncChecking ? '正在检查同步范围' : '同步字幕与配音' }}</n-button>
            <n-button size="small" @click="router.push(`/projects/${uid}/steps/tts`)">编辑与导出</n-button>
          </template>
          <template v-else>
            <router-link class="lbl steplink" title="进入该步骤页" :to="`/projects/${uid}/steps/${s.key}`">{{ s.label }}</router-link>
            <FreshnessDetails v-if="needsFreshnessDetails(s)" :uid="uid" :step-key="s.key" :label="s.label"
                              :status-text="statusText(s)" :summary="s.freshness_reason" />
            <n-tag v-else size="small" class="status-tag"
                   :type="statusTagType(s)" :title="statusText(s)">
              {{ statusText(s) }}
            </n-tag>
            <span class="err" v-if="showStepError(s)" :title="s.error.message">⚠ {{ s.error.message }}</span>
            <span class="ops">
              <n-button v-if="s.status === 'running' || s.status === 'queued'"
                        size="small" type="warning" secondary
                        :disabled="s.cancelling || cancelPending[s.key]"
                        @click="stopDialog(s)">
                {{ s.cancelling || cancelPending[s.key] ? `⏳ ${stoppingLabel(s)}…` : '⏹ 停止' }}
              </n-button>
              <n-button v-else-if="s.key === 'assemble'" size="small" type="primary" secondary
                        @click="router.push(`/projects/${uid}/steps/assemble`)">选择成品方案 →</n-button>
              <n-button v-else-if="blockersOf(s.key).length" size="small" type="info" secondary
                        :disabled="nav.archived || nav.busy || dirMissing"
                        @click="runSelection(true, s.key)">
                {{ dependencyActionLabel(blockersOf(s.key)) }}
              </n-button>
              <n-button v-else-if="s.status !== 'done' || !s.fresh" size="small" type="primary" secondary
                        :disabled="nav.archived || nav.busy || dirMissing"
                        :loading="busyKey === s.key"
                        :title="runTitle(s)"
                        @click="run(s)">
                {{ runTitle(s) }}
              </n-button>
            </span>
          </template>
          <ExecutionProgress v-if="s.key!=='comments' && automaticCurrentStep !== s.key" class="row-activity" :execution="executionForStep(s)" :percentage="['running','queued'].includes(s.status) ? s.progress : null" :estimated="s.progress_estimated" :indeterminate="s.progress_indeterminate" :eta="['running','queued'].includes(s.status) ? formatEta(executionEtaSeconds(s),s.eta_scope) : ''" :activity="nav.activityFor(s.key)" compact />
        </div>
        <div class="row publication-step">
          <span class="step-index">{{ String(stageNumber('publish')).padStart(2, '0') }}</span>
          <router-link class="lbl steplink" :to="`/projects/${uid}/publish`">发布到B站</router-link>
          <n-tag size="small" :type="nav.publicationDisplay.tone" :title="nav.publicationDisplay.note">{{ nav.publicationDisplay.text }}</n-tag>
          <span class="publish-note">{{ nav.publicationDisplay.note }}</span>
          <span class="ops"><n-button size="small" secondary @click="router.push(`/projects/${uid}/publish`)">{{ nav.publication?.id ? '查看稿件 →' : '发布设置 →' }}</n-button></span>
          <PublicationProgress class="row-activity" :publication="nav.publication" compact />
        </div>
      </div>
      <n-text depth="3" style="font-size: 11.5px; display: block; margin-top: 8px">
        暂停、强制结束和详细日志在步骤页处理；详细产物预览也从对应步骤进入。
      </n-text>
    </n-card>

    <!-- ============ 项目文件 ============ -->
    <n-card size="small" style="margin-top: 14px" title="项目文件">
      <template #header-extra>
        <n-space align="center">
          <n-select v-model:value="sortBy" :options="SORT_OPTS" size="tiny"
                    style="width: 130px" />
          <n-button size="tiny" @click="sortDesc = !sortDesc">
            {{ sortDesc ? '↓ 降序' : '↑ 升序' }}
          </n-button>
          <n-button size="tiny" quaternary @click="loadFiles">刷新</n-button>
        </n-space>
      </template>
      <div style="font-size: 11px; margin-bottom: 10px"><FilePathLink :path="fileDir" /></div>
      <div class="project-files-table">
      <n-table size="small" :single-line="false" v-if="sortedFiles.length">
        <thead>
          <tr><th>文件</th><th style="min-width:180px">说明</th><th style="width:90px">大小</th>
              <th style="width:150px">修改时间</th><th style="width:150px">创建时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="f in sortedFiles" :key="f.file">
            <td style="font-size: 12px"><FilePathLink :path="f.path" :label="f.file" /></td>
            <td class="file-description">{{ f.description }}</td>
            <td style="font-size: 12px">{{ fmtSize(f.size) }}</td>
            <td style="font-size: 12px">{{ fmtT(f.mtime) }}</td>
            <td style="font-size: 12px">{{ fmtT(f.ctime) }}</td>
          </tr>
        </tbody>
      </n-table>
      <n-empty v-else description="(目录为空)" size="small" style="margin: 6px 0" />
      </div>
    </n-card>
  </div>
</template>

<style scoped>
.project-files-table { overflow-x:auto; }.file-description { color:var(--df-muted); font-size:12px; }.project-files-table td { vertical-align:top; }
.project-progress-summary { display: flex; align-items: center; gap: 12px; min-width: 0; margin-bottom: 6px; padding: 14px 16px; background: var(--df-bg); border: 1px solid var(--df-border); border-radius: 10px; color: var(--df-muted); font-size: 12px; }
.project-progress-summary .n-progress { flex: 1; min-width: 50px; }
.project-progress-summary > span:first-child { white-space: nowrap; }
.project-progress-summary strong { color: var(--df-text); white-space: nowrap; font-variant-numeric: tabular-nums; }
.step-index { color: #788aa0; font-size: 12px; font-variant-numeric: tabular-nums; min-width: 26px; }
.editor-tasks { display: flex; flex-wrap: wrap; gap: 10px 20px; flex: 1; }
.editor-task { display: flex; gap: 8px; align-items: center; }
/* 预览行: 视频列贴内容宽, 信息列吃掉剩余; 窄屏上下堆叠 */
.prev-row { display: flex; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
.vid-col { flex: 0 1 auto; min-width: 0; }
.video-shell { position: relative; min-width: 0; }
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
  max-height: 440px;
  aspect-ratio: 16 / 9;
  border-radius: 8px;
  background: #000;
}
.comment-stage-note{font-size:12px;color:var(--df-muted);flex:1;min-width:180px;line-height:1.7}
.rows { display: flex; flex-direction: column; }
.publication-step { border-top: 1px solid var(--df-border); }
.publish-note { font-size: 11px; color: var(--df-muted); }
.row {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  padding: 16px 8px; border-bottom: 1px solid #2a323e;
}
.row:hover { background: #83b5ff05; }
.row:last-child { border-bottom: none; }
.select-box { flex: none; }
.lbl { font-size: 14px; font-weight: 600; width: 100px; color: var(--df-text); text-decoration: none; }
.status-tag { flex: none; min-width: 66px; justify-content: center; }
.steplink { cursor: pointer; }
.steplink:hover { color: #4da3ff; text-decoration: underline; }
.info { font-size: 13px; flex: 1; }
.err {
  font-size: 11.5px; color: #e88080; flex: 1 1 160px; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ops { display: flex; align-items: center; gap: 8px; margin-left: auto; }
.row > .row-activity { flex: 1 1 calc(100% - 34px); min-width: 0; margin: 2px 0 0 34px; box-sizing: border-box; }
.page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.project-heading { min-width: 0; }
.eyebrow { display: block; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; }
.subline { display: block; margin-top: 8px; font-size: 13px; line-height: 1.8; }
.header-status { display: flex; align-items: center; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
@media (max-width: 1100px) {
  .info { flex: 1 1 150px; }
  .ops { flex: 1 1 100%; justify-content: flex-end; flex-wrap: wrap; }
}
@media (max-width: 650px) {
  .project-progress-summary { padding: 12px; }
  .project-progress-summary > span:last-child { display: none; }
  .row > .row-activity { flex-basis: 100%; margin-left: 0; }
  .page-head { flex-direction: column; }
  .header-status { justify-content: flex-start; }
  .ops { justify-content: flex-start; padding-left: 28px; }
}
</style>
