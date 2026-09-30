<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCard, NEmpty, NInputNumber, NSelect, NSpace, NTag, NText } from 'naive-ui'
import PreviewTimeline from './PreviewTimeline.vue'
import ProjectParameterForm from './ProjectParameterForm.vue'
import SubtitleExport from './SubtitleExport.vue'
import { useNavStore } from '../stores/nav'
import { SUBTITLE_PREVIEW_PATHS, SUBTITLE_WORKSPACE_PATHS, subtitleTimelineDuration, subtitleTimeLabel } from '../utils/subtitleWorkspace'

const props = defineProps<{ uid: string; position: number; duration?: number; lines: any[]; subtitleState: any;
  active: boolean; disabled?: boolean; noVideo?: boolean; returnToAssembly?: boolean }>()
const emit = defineEmits<{ 'dirty-change': [value: boolean]; refresh: []; preview: [name: string]; 'return-assembly': [] }>()
const nav = useNavStore(), dirty = ref(false), loading = ref(false)
const seconds = ref<number | null>(null), draft = ref<Record<string, any>>({})
const scrubbing = ref(false)
const timelineDuration = computed(() => subtitleTimelineDuration(props.duration, props.lines))
// 视频终点没有可解码的下一帧，预留 0.1 秒；元数据未就绪时暂按字幕时间轴定位。
const previewEnd = computed(() => Math.max(0, Math.floor((timelineDuration.value - .1) * 100) / 100))
const sliderValue = computed(() => Math.min(previewEnd.value, Math.max(0, seconds.value ?? 0)))
const cues = computed(() => props.lines.map(line => ({ start: line.start, end: line.end,
  label: `第 ${line.id} 句 · ${String(line.text || line.source_text || '').slice(0, 60)}` })))
const image = ref(''), error = ref(''), rendered = ref(''), subtitleActive = ref(true)
const selectedLine = computed(() => props.lines.find(line => seconds.value != null && line.start <= seconds.value && seconds.value < line.end)?.id ?? null)
const lineOptions = computed(() => props.lines.map(line => ({ value: line.id,
  label: `第 ${line.id} 句 · ${String(line.text || line.source_text || '').slice(0, 45)}` })))
const contentVersion = computed(() => JSON.stringify(props.lines.map(line => [line.id, line.start, line.end, line.source_text, line.text])))
const settings = computed(() => Object.fromEntries(SUBTITLE_PREVIEW_PATHS.filter(path => path in draft.value).map(path => [path, draft.value[path]])))
const signature = computed(() => JSON.stringify([seconds.value, settings.value, contentVersion.value]))
const outdated = computed(() => !!image.value && rendered.value !== signature.value)
const canPreview = computed(() => props.active && !props.noVideo && !scrubbing.value && props.lines.length > 0
  && seconds.value != null && Number.isFinite(seconds.value) && seconds.value >= 0 && Object.keys(settings.value).length > 0)
let request: AbortController | null = null, timer: ReturnType<typeof setTimeout> | undefined
let pending = false, disposed = false
watch(dirty, value => emit('dirty-change', value), { flush: 'sync' })

function clearImage() { if (image.value) URL.revokeObjectURL(image.value); image.value = '' }
function setPosition(value: number | null) {
  seconds.value = value == null || !Number.isFinite(value) ? null
    : Math.min(previewEnd.value, Math.max(0, value))
}
function beginScrub() { scrubbing.value = true; clearTimeout(timer) }
function endScrub() { scrubbing.value = false; schedule() }
function chooseLine(id: number) {
  const line = props.lines.find(item => item.id === id)
  if (line) setPosition(Math.round((line.start + (line.end - line.start) / 2) * 100) / 100)
}
function usePlayerPosition() { setPosition(props.position) }
function schedule() {
  clearTimeout(timer)
  if (!canPreview.value || disposed) return
  timer = setTimeout(() => { void preview() }, 650)
}
async function preview() {
  clearTimeout(timer)
  if (!canPreview.value || disposed) return
  // FFmpeg 请求串行；拖动参数期间只补做最新画面，不并发编码每个中间值。
  if (request) { pending = true; return }
  const current = request = new AbortController(), snapshot = signature.value
  loading.value = true; error.value = ''; pending = false
  try {
    const response = await fetch(`/api/projects/${props.uid}/workbench/subtitle-preview`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: current.signal,
      body: JSON.stringify({ seconds: seconds.value, settings: settings.value }),
    })
    if (!response.ok) { const data = await response.json(); throw new Error(data.message || '字幕预览失败') }
    const blob = await response.blob()
    if (disposed || current.signal.aborted || snapshot !== signature.value || !canPreview.value) return
    clearImage(); image.value = URL.createObjectURL(blob); rendered.value = snapshot
    subtitleActive.value = response.headers.get('X-Subtitle-Active') !== '0'
  } catch (e: any) {
    if (!disposed && !current.signal.aborted && snapshot === signature.value && props.active) error.value = e.message || '字幕预览失败'
  } finally {
    if (request === current) {
      request = null; loading.value = false
      if (pending || snapshot !== signature.value) schedule()
    }
  }
}
watch(() => [props.active, props.lines.length], () => {
  if (props.active && seconds.value == null && props.lines.length) {
    const atPlayer = props.lines.find(line => line.start <= props.position && props.position < line.end)
    if (atPlayer) usePlayerPosition()
    else chooseLine(props.lines[0].id)
  }
}, { immediate: true })
watch([signature, canPreview], schedule, { immediate: true })
watch(previewEnd, end => { if (end > 0 && seconds.value != null && seconds.value > end) setPosition(seconds.value) })
watch(() => props.active, value => {
  if (!value) { clearTimeout(timer); pending = false; scrubbing.value = false }
})
async function saved() { emit('refresh'); await nav.poll(false) }
onUnmounted(() => { disposed = true; clearTimeout(timer); request?.abort(); clearImage() })
</script>

<template>
  <section class="subtitle-workspace" aria-label="字幕工作区">
    <div class="workspace-heading">
      <div><h2>字幕工作区</h2><p>在一处调整字幕、查看实际烧录效果并导出文件。仅影响当前项目。</p></div>
      <n-button v-if="returnToAssembly" secondary :disabled="dirty" @click="emit('return-assembly')">返回合成成片</n-button>
    </div>
    <n-alert v-if="dirty" type="warning" :show-icon="false" class="workspace-notice">当前为未保存草稿，预览将使用草稿设置。请保存后再导出或合成；切换到校对与配音不会丢失草稿。</n-alert>
    <div class="subtitle-layout">
      <div class="frame-column">
        <n-card size="small" title="烧录效果预览" class="frame-card">
          <template #header-extra><n-tag size="small" :bordered="false" :type="dirty ? 'warning' : 'default'">{{ dirty ? '草稿效果' : '已保存设置' }}</n-tag></template>
          <div class="preview-toolbar">
            <n-select :value="selectedLine" :options="lineOptions" filterable placeholder="选择预览句子"
              aria-label="选择预览句子" class="line-picker" @update:value="chooseLine" />
            <div class="preview-timeline">
              <div class="timeline-caption"><span>拖动定位画面</span><output>{{ subtitleTimeLabel(seconds) }} / {{ subtitleTimeLabel(timelineDuration) }}</output></div>
              <PreviewTimeline :value="sliderValue" :max="previewEnd" :cues="cues" kind="字幕" label="字幕预览进度"
                :disabled="noVideo || previewEnd <= 0"
                @update:value="setPosition" @dragstart="beginScrub" @dragend="endScrub" />
              <n-text depth="3" class="timeline-hint">{{ scrubbing ? '松开拖动条后更新画面' : '只改变预览位置，不修改字幕时间或播放器位置' }}</n-text>
            </div>
            <label>精确到秒<n-input-number :value="seconds" :min="0" :max="previewEnd || undefined" :step="0.1" :precision="2" :input-props="{ 'aria-label': '烧录预览位置' }" @update:value="setPosition" /></label>
            <n-button size="small" :disabled="noVideo" @click="usePlayerPosition">取播放器位置</n-button>
            <n-button size="small" :loading="loading" :disabled="!canPreview" @click="preview">更新预览</n-button>
          </div>
          <n-alert v-if="error" type="error" class="workspace-notice">{{ error }}</n-alert>
          <div class="frame-stage" :aria-busy="loading">
            <img v-if="image && !noVideo" :src="image" alt="字幕实际烧录效果预览" :class="{ outdated }" />
            <n-empty v-else :description="noVideo ? '源视频暂不可用，仍可编辑字幕设置' : !lines.length ? '语义整理完成后即可预览字幕' : loading ? '正在渲染字幕画面…' : '选择句子后自动显示实际效果'" />
            <span v-if="image && (loading || outdated)" class="frame-status">{{ loading ? '正在更新效果…' : '画面待更新' }}</span>
          </div>
          <n-text depth="3" class="note">停止调整后自动更新；与成片使用同一套字幕渲染。此处只生成一帧，不生成视频、不保存草稿。</n-text>
          <n-text v-if="image && !subtitleActive && !outdated" type="warning" class="note">此时刻没有字幕。请选择上方某一句，或调整预览秒数。</n-text>
        </n-card>
      </div>
      <n-card size="small" title="字幕设置" class="settings-card">
        <ProjectParameterForm :uid="uid" step-key="assemble" :additional-step-keys="['subtitles']"
          :section-titles="{ assemble: '烧录内容与样式', subtitles: '字幕文件与时间' }"
          :paths="SUBTITLE_WORKSPACE_PATHS" :disabled="disabled"
          @draft-change="draft = $event" @dirty-change="dirty = $event" @saved="saved">
          <template #actions="form">
            <n-space class="workspace-actions" align="center">
              <n-button type="primary" :loading="form.saving" :disabled="!form.dirty || disabled || form.loading" @click="form.save">保存字幕设置</n-button>
              <n-button v-if="form.dirty" quaternary :disabled="form.saving" @click="form.discard">放弃修改</n-button>
              <n-button v-if="form.hasOverrides" quaternary :disabled="disabled || form.saving" @click="form.resetAll">恢复继承（待保存）</n-button>
            </n-space>
          </template>
        </ProjectParameterForm>
        <n-text depth="3" class="note">字体、颜色等仅用于烧录到画面；SRT 只保存文字与时间。是否烧录由成片方案决定。</n-text>
      </n-card>
    </div>
    <SubtitleExport :uid="uid" :state="subtitleState" :disabled="!!disabled || dirty"
      @refresh="emit('refresh')" @preview="emit('preview', $event)" />
  </section>
</template>

<style scoped>
.subtitle-workspace { margin-top: 18px; min-width: 0; }
.workspace-heading { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:18px; }
.workspace-heading h2 { margin:0 0 6px; font-size:18px; }
.workspace-heading p { margin:0; color:var(--df-muted); font-size:12px; line-height:1.7; }
.workspace-notice { margin-bottom:14px; }
.subtitle-layout { display:grid; grid-template-columns:minmax(0, 1.3fr) minmax(360px, 1fr); gap:20px; align-items:start; }
.frame-column { position:sticky; top:18px; min-width:0; }
.settings-card { min-width:0; }
.preview-toolbar { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:16px; }
.line-picker { flex:1 0 100%; min-width:0; }
.preview-timeline { flex:1 0 100%; min-width:0; padding:2px 0 4px; }
.timeline-caption { display:flex; align-items:center; justify-content:space-between; gap:12px; font-size:12px; margin-bottom:8px; }
.timeline-caption output { color:var(--df-accent); font-variant-numeric:tabular-nums; }
.timeline-hint { display:block; margin-top:6px; font-size:11px; }
.preview-toolbar label { display:flex; align-items:center; gap:8px; font-size:12px; }
.preview-toolbar :deep(.n-input-number) { width:125px; }
.frame-stage { position:relative; display:flex; align-items:center; justify-content:center; min-height:240px; aspect-ratio:16/9; background:#090c10; border:1px solid var(--df-border); border-radius:9px; overflow:hidden; }
.frame-stage img { display:block; width:100%; height:100%; object-fit:contain; max-height:62vh; }
.frame-stage .outdated { opacity:.65; }
.frame-status { position:absolute; bottom:12px; left:12px; padding:5px 10px; border-radius:6px; background:#101318dd; font-size:12px; }
.note { display:block; margin-top:12px; font-size:12px; line-height:1.8; }
.workspace-actions { position:sticky; bottom:0; z-index:1; padding:16px 0 10px; margin-top:18px; background:var(--df-panel); border-top:1px solid var(--df-border); }
.settings-card :deep(.parameter-grid) { grid-template-columns:repeat(2, minmax(0, 1fr)); gap:14px; }
.settings-card :deep(.n-input-number) { max-width:100%; }
@media (max-width:1150px) { .subtitle-layout { grid-template-columns:minmax(0, 1fr); } .frame-column { position:static; } }
@media (max-width:650px) { .settings-card :deep(.parameter-grid) { grid-template-columns:minmax(0, 1fr); } .workspace-heading { align-items:flex-start; flex-direction:column; } .frame-stage { min-height:180px; } }
</style>
