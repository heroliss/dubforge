<script setup lang="ts">
// 短片使用上方当前组合，与完整成品清单分开；原声试片不要求配音就绪。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  NAlert, NButton, NCard, NText, useMessage,
} from 'naive-ui'
import { useNavStore } from '../stores/nav'
import { createLatestRefresh, newestVideoFirst, vReleaseMedia, finalVideoKind } from '../utils/preview'
import { choiceLabel, outputKey, outputChoice, OUTPUT_LABELS, type AssemblyChoice } from '../utils/actionScope'
import DependencyNotice from './DependencyNotice.vue'
import VideoControls from './VideoControls.vue'
import { useCommentOverlay } from '../composables/useCommentOverlay'
import { refreshInBackground } from '../utils/backgroundRefresh'
import { formatTimecode } from '../utils/timecode'
import TimecodeInput from './TimecodeInput.vue'

const props = defineProps<{
  uid: string
  sourceUrl: string
  artifacts: any[]
  disabled?: boolean
  choice?: AssemblyChoice
}>()

const nav = useNavStore()
let disposed = false
onBeforeUnmount(() => { disposed = true; chapterReader.cancel() })
const message = useMessage()
const videoEl = ref<HTMLVideoElement | null>(null)
const resultVideoEl = ref<HTMLVideoElement | null>(null)
const sourceChapters = ref<any[]>([])
const chapterReader = createLatestRefresh(async signal => {
  const r = await fetch(`/api/projects/${props.uid}/steps/download/video-info`, { cache: 'no-store', signal })
  if (!r.ok) throw new Error('读取视频章节失败')
  const value = await r.json()
  const rows = value?.info?.content?.chapters
  return Array.isArray(rows) ? rows : []
}, value => { sourceChapters.value = value }, () => { sourceChapters.value = [] })
const duration = ref(0)
const start = ref(0)
const end = ref(0)
const startInvalid = ref(false), endInvalid = ref(false)
const submitting = ref(false)
const { count: commentCount, revision: commentRevision, failed: commentsFailed } = useCommentOverlay(() => props.uid)
const currentChoice = computed(() => props.choice || outputChoice('src'))
const planBlockers = ref<any[]>([])
watch(currentChoice, () => { planBlockers.value = [] }, { deep: true })
watch(() => props.sourceUrl, (url, previous) => {
  if (!url) { chapterReader.cancel(); sourceChapters.value = []; return }
  if (url !== previous || !sourceChapters.value.length) void chapterReader.refresh(url !== previous)
}, { immediate: true })
const running = computed(() => ['running', 'queued'].includes(nav.stepMap.assemble?.status))
const previewRunning = computed(() => running.value && nav.stepMap.assemble?.assembly_task?.scope === 'preview')
const ready = computed(() => !!props.sourceUrl && duration.value >= 0.2)
const validRange = computed(() => ready.value && end.value - start.value >= 0.2 - 1e-9)
const canSubmit = computed(() => validRange.value && !startInvalid.value && !endInvalid.value && outputKey(currentChoice.value) !== 'src' && !props.disabled && !running.value
  && (!currentChoice.value.include_comments || (commentCount.value > 0 && !commentsFailed.value)))

function fmt(seconds: unknown): string {
  return formatTimecode(typeof seconds === 'number' ? seconds : null, 3)
}
function clamp(value: number): number {
  if (!Number.isFinite(value)) return start.value
  return Math.max(0, Math.min(duration.value, value))
}
function onMeta(event: Event) {
  const video = event.target as HTMLVideoElement
  duration.value = Number.isFinite(video.duration) ? video.duration : 0
  if (duration.value >= 0.2 && end.value <= 0.2) {
    start.value = 0
    end.value = Math.min(10, duration.value)
  }
}
watch(() => props.sourceUrl, () => {
  duration.value = 0
  start.value = 0
  end.value = 0
})
function setStart(value: number | null) {
  if (!ready.value || value === null || !Number.isFinite(value)) return
  start.value = Math.min(clamp(value), Math.max(0, duration.value - 0.2))
  end.value = Math.max(end.value, Math.min(duration.value, start.value + 0.2))
}
function setEnd(value: number | null) {
  if (!ready.value || value === null || !Number.isFinite(value)) return
  end.value = Math.max(0.2, clamp(value))
  start.value = Math.min(start.value, Math.max(0, end.value - 0.2))
}
function markStart() {
  if (!videoEl.value) return
  setStart(videoEl.value.currentTime)
  videoEl.value.pause()
}
function markEnd() {
  if (!videoEl.value) return
  setEnd(videoEl.value.currentTime)
  videoEl.value.pause()
}
const previewArtifacts = computed(() => (props.artifacts ?? [])
  .filter(item => item.exists !== false && /^preview_(?:[a-z_]+_)?\d{8}_\d{6}_\d+\.mp4$/.test(item.name))
  .sort(newestVideoFirst))
const latestPreview = computed(() => previewArtifacts.value[0] ?? null)
const resultLabel = computed(() => latestPreview.value ? OUTPUT_LABELS[finalVideoKind(latestPreview.value.name.replace(/^preview_/, 'final_')) || ''] || (latestPreview.value.name.includes('_comments_') ? '含评论' : '') : '')
function previewUrl(item: any): string {
  const version = encodeURIComponent(String(item.sha256 || item.size || item.name))
  return `/api/projects/${props.uid}/steps/assemble/artifacts/${encodeURIComponent(item.name)}/raw?v=${version}`
}
async function submit() {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  videoEl.value?.pause()
  const uid = props.uid, payload = { start: start.value, end: end.value, output: { ...currentChoice.value },
    ...(currentChoice.value.include_comments ? { overlay_revision: commentRevision.value } : {}) }
  try {
    const check = await fetch(`/api/projects/${uid}/steps/assemble/output_plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outputs: [payload.output], ...(payload.overlay_revision ? { overlay_revision: payload.overlay_revision } : {}) }) })
    const plan = await check.json()
    if (!check.ok) throw new Error(plan.message || '无法检查试片方案')
    if (disposed || uid !== props.uid) return
    if (plan.blockers?.length) { planBlockers.value = plan.blockers; return }
    const response = await fetch(`/api/projects/${uid}/steps/assemble/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data.message || `试合成启动失败 (HTTP ${response.status})`)
    message.success(`已开始试合成：${fmt(payload.start)} → ${fmt(payload.end)}`)
    nav.stepStarted(uid, 'assemble', data)
    refreshInBackground(() => nav.poll())
  } catch (error: any) {
    message.error(error.message || '试合成启动失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <n-card size="small" title="短片试合成 · 与正式成品分开" class="assemble-preview-panel">
    <template #header-extra>
      <n-text depth="3" style="font-size:11.5px">只生成短片段，不替代正式成片</n-text>
    </template>
    <n-text depth="3" class="hint">本次试片：{{ choiceLabel(currentChoice) }}。跟随上方“当前组合”，不运行整份成品清单。</n-text>
    <n-alert v-if="outputKey(currentChoice) === 'src'" type="info" :show-icon="false" class="hint">当前组合就是原片，可直接播放左侧视频，无需试合成。</n-alert>
    <n-alert v-if="disabled && !running" type="warning" :show-icon="false" class="hint">项目忙碌、目录缺失或参数尚未保存，暂不能生成试片。</n-alert>
    <DependencyNotice :uid="uid" :items="planBlockers" />

    <n-alert v-if="!sourceUrl" type="warning" :show-icon="false">
      还没有可用的源视频，无法选择试合成区域。
    </n-alert>

    <template v-else>
      <div class="video-grid">
        <section class="video-column">
          <div class="video-title">源视频</div>
          <div class="video-shell">
            <video v-release-media ref="videoEl" class="preview-video" :key="sourceUrl"
                   :src="sourceUrl" preload="metadata" @loadedmetadata="onMeta" />
            <VideoControls :media="videoEl" :chapters="sourceChapters" :duration="duration" />
          </div>
        </section>
        <section class="video-column">
          <div class="video-title">上次试合成结果{{ resultLabel ? ' · ' + resultLabel : '' }}</div>
          <div v-if="latestPreview" class="video-shell">
            <video v-release-media ref="resultVideoEl" class="preview-video"
                   :key="latestPreview.name" :src="previewUrl(latestPreview)" preload="metadata" />
            <VideoControls :media="resultVideoEl" />
          </div>
          <div v-else class="result-empty">
            {{ previewRunning ? '试片正在生成，完成后显示在这里' : '选择区域并生成试片后显示在这里' }}
          </div>
        </section>
      </div>

      <div class="range-bar">
        <div class="endpoint">
          <span>起点</span>
          <TimecodeInput size="small" :value="start" :min="0" :max="Math.max(0,duration - 0.2)"
                         :disabled="!ready || submitting" aria-label="试合成起点（时分秒）" @update:value="setStart" @invalid-change="startInvalid=$event" />
          <n-button size="small" circle quaternary :disabled="!ready || submitting" title="使用当前播放时间作为起点"
                    aria-label="使用当前播放时间作为起点" @click="markStart">⏱</n-button>
        </div>
        <div class="endpoint">
          <span>终点</span>
          <TimecodeInput size="small" :value="end" :min="0.2" :max="duration"
                         :disabled="!ready || submitting" aria-label="试合成终点（时分秒）" @update:value="setEnd" @invalid-change="endInvalid=$event" />
          <n-button size="small" circle quaternary :disabled="!ready || submitting" title="使用当前播放时间作为终点"
                    aria-label="使用当前播放时间作为终点" @click="markEnd">⏱</n-button>
        </div>
        <div class="actions">
          <n-text depth="3">{{ validRange ? `片段 ${fmt(end - start)}` : '至少 0.2 秒' }}</n-text>
          <n-button size="small" type="primary" secondary :loading="submitting"
                    :disabled="!canSubmit" @click="submit">
            {{ running ? (previewRunning ? '正在生成试片…' : '正式成品任务进行中') : `生成试片（${fmt(end - start)}）` }}
          </n-button>
        </div>
      </div>

      <n-text depth="3" class="hint">
        时间使用时:分:秒，可输入秒数或分:秒；播放源视频到目标位置后点 ⏱ 取当前时间。评论按原视频时间轴出现，样式与混音使用已保存设置。
      </n-text>
      <n-text v-if="previewArtifacts.length > 1" depth="3" class="history">
        共保留 {{ previewArtifacts.length }} 个试合成片段；生成正式成品不会替换这些片段。
      </n-text>
    </template>
  </n-card>
</template>

<style scoped>
.assemble-preview-panel { margin-top: 14px; }
.video-grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
.video-shell { position: relative; min-width: 0; }
.video-column { min-width: 0; }
.video-title { font-size: 12px; font-weight: 600; margin-bottom: 6px; }
.preview-video {
  display: block; width: 100%; max-height: 360px; aspect-ratio: 16 / 9;
  border-radius: 8px; background: #000;
}
.result-empty {
  display: flex; align-items: center; justify-content: center;
  width: 100%; aspect-ratio: 16 / 9; max-height: 360px;
  border: 1px dashed rgba(128,128,128,.35); border-radius: 8px;
  color: rgba(255,255,255,.5); font-size: 12px; text-align: center; padding: 12px;
}
.range-bar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-top: 14px;
}
.endpoint { display: flex; align-items: center; gap: 6px; }
.endpoint > span { font-size: 12px; }
.endpoint :deep(.timecode-input) { width: 154px; }
.actions { display: flex; align-items: center; gap: 10px; margin-left: auto; }
.hint, .history { display: block; margin-top: 10px; font-size: 12px; line-height: 1.6; }
.history { font-size: 11.5px; opacity: .75; }
@media (min-width: 900px) {
  .video-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
}
@media (max-width: 650px) {
  .range-bar { align-items: stretch; flex-direction: column; }
  .endpoint { justify-content: space-between; }
  .actions { justify-content: space-between; margin-left: 0; width: 100%; }
}
</style>
