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
  useNavStore, STEP_ORDER, NAV_STEP_ORDER, STATUS_TAG, STATUS_TEXT,
  formatEta, formatProgressDetail,
} from '../stores/nav'
import MediaInfo from '../components/MediaInfo.vue'
import FilePathLink from '../components/FilePathLink.vue'
import ProgressActivity from '../components/ProgressActivity.vue'
import { stoppingLabel } from '../utils/progressActivity'
import WorkflowPanel from '../components/WorkflowPanel.vue'
import ChainStatus from '../components/ChainStatus.vue'
import ProjectTemplateBinding from '../components/ProjectTemplateBinding.vue'
import { fmtSize } from '../utils/format'
import { WORKFLOW_OPEN, WORKFLOW_BUSY, workflowRunStage } from '../utils/workflow'
import { useSmoothProgressMap } from '../utils/smoothProgress'
import { createLatestRefresh, releaseMedia, vReleaseMedia } from '../utils/preview'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()
const uid = route.params.uid as string
// 后端与前端共用完整管线定义，工作台位于配音计划与批量配音之间。
const orderedSteps = computed(() => STEP_ORDER.map(k => nav.stepMap[k]).filter(Boolean))
const displayedSteps = computed(() => NAV_STEP_ORDER.map(k => nav.stepMap[k]).filter(Boolean))
const smoothProgress = useSmoothProgressMap(
  () => orderedSteps.value.map((s: any) => ({
    key: s.key, progress: s.progress, status: s.status,
  })),
)
const formatProgress = (value: unknown) => {
  const n = Number(value)
  return `${(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0).toFixed(1)}%`
}

const isAuto = (key: string) => (nav.autoSteps ? nav.autoSteps.includes(key) : true)
async function toggleAuto(key: string, val: boolean) {
  const prev = nav.autoSteps
  const set = new Set(nav.autoSteps ?? [...STEP_ORDER])
  if (val) set.add(key)
  else set.delete(key)
  nav.autoSteps = STEP_ORDER.filter(k => set.has(k))
  try {
    const r = await fetch(`/api/projects/${uid}/auto_steps`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps: nav.autoSteps }),
    })
    if (!r.ok) throw new Error(String(r.status))
  } catch {
    // 保存失败: 回滚本地勾选并提示, 否则 2.5s 后被轮询静默弹回, 像闹鬼
    nav.autoSteps = prev ?? null
    message.error('勾选保存失败 (网络异常)')
  }
}

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
onUnmounted(() => mediaLoader.cancel())
// 项目目录是否还在磁盘上 (被移走/删除 → 头部标注)
const dirMissing = computed(() => nav.dirMissing)
const mediaVersion = computed(() => {
  const artifact = nav.stepMap.download?.artifacts?.find((a: any) => a.name === 'video.mp4')
  return String(artifact?.sha256 || artifact?.size || 0)
})
let lastMediaSignature = ''
watch(() => nav.stepMap.download, download => {
  if (downloadRunning.value) { mediaLoader.cancel(); return }
  const signature = `${download?.status}:${mediaVersion.value}`
  const changed = signature !== lastMediaSignature
  lastMediaSignature = signature
  if (changed || mediaFailed || (!media.value.found && download?.status === 'done')) {
    void mediaLoader.refresh(changed)
  }
}, { immediate: true })
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
  if (step?.status === 'done') return step.freshness_label || (step.fresh ? '已是最新' : '需更新')
  return STATUS_TEXT[stepDisplayStatus(step)] || step?.status || '待运行'
}
function statusTagType(step: any): 'default' | 'success' | 'error' | 'warning' | 'info' {
  if (step?.status === 'done') return step.fresh ? 'success' : 'warning'
  return STATUS_TAG[stepDisplayStatus(step)] || 'default'
}
function runTitle(step: any): string {
  if (['failed', 'interrupted', 'stopped'].includes(step?.status)) return '重新运行'
  if (step?.status === 'done') return '更新结果'
  return '运行'
}
function editorTaskText(step: any): string {
  return step?.status === 'done' && !step.fresh ? '需更新' : statusText(step)
}
function editorTaskType(step: any): 'default' | 'success' | 'error' | 'warning' | 'info' {
  return statusTagType(step)
}

// 依赖项会重复出现在多个下游步骤里；总览顶部只提示一次。
const selectedBlockers = computed(() => {
  const seen = new Set<string>()
  return STEP_ORDER.filter(k => k !== 'dubtask' && isAuto(k))
    .flatMap(k => blockersOf(k))
    .filter((item: any) => {
      const id = `${item.step}/${item.name}`
      if (seen.has(id)) return false
      seen.add(id)
      return true
    })
})

async function run(step: any, force = false) {
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
  if (nav.busy || dirMissing.value) return
  const steps = onlyKey ? [onlyKey] : STEP_ORDER.filter(k => k !== 'dubtask' && isAuto(k))
  if (!steps.length) { message.info('请先勾选步骤'); return }
  await runPlannedSteps(uid, [...steps], includeDependencies, message, dialog,
    async (plan) => { await stopPreviewForRun(plan.includes('download') ? 'download' : plan[0]) },
    async () => { previewSuspended.value = false; await nav.poll() })
}

// 字幕导出和逐句配音属于同一个编辑工作流；同步时一并带到最新。
async function runEditorWorkflow() {
  await runPlannedSteps(uid, ['subtitles', 'tts'], true, message, dialog,
    async () => { await stopPreviewForRun('tts') },
    async () => { previewSuspended.value = false; await nav.poll() })
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
  const flow = nav.workflow?.run
  if (flow && WORKFLOW_OPEN.includes(flow.status)) return {
    text: `${workflowRunStage(flow).text} · ${flow.phase}`, type: workflowRunStage(flow).tone, key: null,
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
      <template #action><n-button size="small" @click="nav.poll()">重试</n-button></template>
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
    <ProjectTemplateBinding :uids="[uid]" />
    <WorkflowPanel :uid="uid" />
    <ChainStatus v-if="nav.chain" :uid="uid" :chain="nav.chain" style="margin-top: 12px" />
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
          <video v-if="videoSrc && !downloadRunning && !previewSuspended" ref="videoEl" v-release-media
                 :key="videoSrc" class="vid" :src="videoSrc" controls preload="metadata"
                 @loadedmetadata="onMeta" />
          <n-empty v-else-if="downloadRunning || previewSuspended" description="正在更新视频，完成后自动刷新预览" />
        </div>
        <div class="info">
          <MediaInfo :media="media" />
        </div>
      </div>
      <n-empty v-else description="还没有视频 —— 运行「获取视频」后这里会显示信息与预览"
               size="small" style="margin: 8px 0" />
    </n-card>

    <!-- ============ 步骤清单 (勾选 + 一个“下一步”动作) ============ -->
    <n-card size="small" style="margin-top: 14px"
            title="步骤进度与手动制作">
      <template #header-extra>
        <n-button secondary :disabled="nav.busy || dirMissing || !nav.requiredStepCount" @click="runSelection(!!selectedBlockers.length)">仅运行勾选制作步骤（{{ nav.requiredStepCount }}）…</n-button>
      </template>
      <n-text depth="3" style="display:block; margin-bottom:12px; font-size:12px; line-height:1.8">
        勾选立即保存但不启动；手动制作不上传、不投稿。完整流程使用上方「自动运行」，不受此处勾选影响。
      </n-text>
      <div class="project-progress-summary"><span>手动勾选</span><n-progress type="line" :percentage="nav.requiredStepCount ? Math.round(nav.requiredDoneCount / nav.requiredStepCount * 100) : 0" :show-indicator="false"/><strong>{{ nav.requiredDoneCount }} / {{ nav.requiredStepCount }}</strong><span>项制作任务已就绪</span></div>
      <ProgressActivity :activity="nav.currentActivity" />
      <div class="rows">
        <div v-for="(s, index) in displayedSteps" :key="s.key" class="row">
          <span class="step-index">{{ String(index + 1).padStart(2, '0') }}</span>
          <template v-if="s.key === 'tts'">
            <router-link class="lbl steplink" title="进入字幕与配音页" :to="`/projects/${uid}/steps/tts`">字幕与配音</router-link>
            <span class="editor-tasks">
              <span v-for="task in ['subtitles', 'tts']" :key="task" class="editor-task">
                <n-checkbox size="small" :checked="isAuto(task)"
                            :disabled="WORKFLOW_BUSY.includes(nav.workflow?.run?.status)"
                            @update:checked="(v: boolean) => toggleAuto(task, v)">
                  {{ task === 'tts' ? '合成配音' : '导出字幕' }}
                </n-checkbox>
                <n-tag v-if="nav.stepMap[task]" size="small"
                       :type="editorTaskType(nav.stepMap[task])">
                  {{ editorTaskText(nav.stepMap[task]) }}
                </n-tag>
              </span>
            </span>
            <n-button size="small" type="primary" secondary
                      :disabled="nav.busy || dirMissing" @click="runEditorWorkflow">同步字幕与配音…</n-button>
            <n-button size="small" @click="router.push(`/projects/${uid}/steps/tts`)">编辑与导出</n-button>
          </template>
          <template v-else>
            <n-checkbox size="small" class="select-box" :checked="isAuto(s.key)"
                        :disabled="WORKFLOW_BUSY.includes(nav.workflow?.run?.status)"
                        :aria-label="`选择${s.label}`"
                        @update:checked="(v: boolean) => toggleAuto(s.key, v)" />
            <router-link class="lbl steplink" title="进入该步骤页" :to="`/projects/${uid}/steps/${s.key}`">{{ s.label }}</router-link>
            <n-tag size="small" class="status-tag"
                   :type="statusTagType(s)" :title="statusText(s)">
              {{ statusText(s) }}
            </n-tag>
            <span class="pbar" v-if="s.status === 'running'">
              <span v-if="s.progress_indeterminate" class="pbar-eta" :title="formatProgressDetail(s)">{{ s.progress_phase }}</span>
              <template v-else>
                <span class="pbar-inner">
                  <n-progress type="line" :percentage="smoothProgress[s.key] || 0" :show-indicator="false" :aria-label="`${s.label}进度`" />
                  <span class="pbar-value" :title="formatProgressDetail(s)">
                    {{ s.progress_estimated ? '预计 ' : '' }}{{ formatProgress(smoothProgress[s.key]) }}
                  </span>
                </span>
                <span v-if="formatEta(s.eta_seconds, s.eta_scope)" class="pbar-eta">
                  {{ formatEta(s.eta_seconds, s.eta_scope) }}
                </span>
              </template>
            </span>
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
                        :disabled="nav.busy || dirMissing"
                        @click="runSelection(true, s.key)">
                {{ dependencyActionLabel(blockersOf(s.key)) }}
              </n-button>
              <n-button v-else-if="s.status !== 'done' || !s.fresh" size="small" type="primary" secondary
                        :disabled="nav.busy || dirMissing"
                        :loading="busyKey === s.key"
                        :title="runTitle(s)"
                        @click="run(s)">
                {{ runTitle(s) }}
              </n-button>
            </span>
          </template>
          <ProgressActivity v-if="['running','queued'].includes(s.status)" class="row-activity" :activity="nav.activityFor(s.key)" />
        </div>
        <div class="row publication-step">
          <span class="step-index">{{ String(NAV_STEP_ORDER.length + 1).padStart(2, '0') }}</span>
          <router-link class="lbl steplink" :to="`/projects/${uid}/publish`">发布到B站</router-link>
          <n-tag size="small" :type="nav.publicationDisplay.tone" :title="nav.publicationDisplay.note">{{ nav.publicationDisplay.text }}</n-tag>
          <span class="publish-note">{{ nav.publicationDisplay.note }}</span>
          <span class="ops"><n-button size="small" secondary @click="router.push(`/projects/${uid}/publish`)">{{ nav.publication?.id ? '查看稿件 →' : '发布设置 →' }}</n-button></span>
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
.project-progress-summary { display: flex; align-items: center; gap: 14px; padding: 16px 20px; background: var(--df-panel); border: 1px solid var(--df-border); border-radius: 12px; color: var(--df-muted); font-size: 12px; }
.project-progress-summary .n-progress { flex: 1; }
.project-progress-summary strong { color: var(--df-text); white-space: nowrap; }
.step-index { color: #788aa0; font-size: 12px; font-variant-numeric: tabular-nums; min-width: 26px; }
.editor-tasks { display: flex; flex-wrap: wrap; gap: 10px 20px; flex: 1; }
.editor-task { display: flex; gap: 8px; align-items: center; }
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
  max-height: 440px;
  aspect-ratio: 16 / 9;
  border-radius: 8px;
  background: #000;
}
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
.row-activity { flex-basis:100%; padding-left:34px; box-sizing:border-box; margin-top:0; }
.pbar { display: grid; gap: 4px; flex: 0 1 240px; min-width: 0; max-width: 100%; line-height: 1.5; }
/* 百分比与进度条并排，预计时间独占下一行；文字不覆盖轨道，容器变窄可自然换行。 */
.pbar-inner { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 8px; min-width: 0; }
.pbar-inner :deep(.n-progress) { min-width: 0; }
.pbar-eta { font-size: 11px; color: var(--df-muted); overflow-wrap: anywhere; }
.pbar-value {
  color: var(--df-text); font-size: 11px; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
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
  .project-progress-summary > span:last-child { display: none; }
  .page-head { flex-direction: column; }
  .header-status { justify-content: flex-start; }
  .ops { justify-content: flex-start; padding-left: 28px; }
}
</style>
