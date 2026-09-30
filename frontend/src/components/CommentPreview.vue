<script setup lang="ts">
// 总时间范围与实际烧录帧一起预览；连续修改合并为串行请求，不保存草稿。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NInputNumber } from 'naive-ui'
import PreviewTimeline from './PreviewTimeline.vue'
import { subtitleTimeLabel as timeLabel } from '../utils/subtitleWorkspace'
const props = defineProps<{ uid: string; items: any[]; settings: any; invalid: boolean; videoDuration?: number }>()
const seconds = ref(0), image = ref(''), error = ref(''), loading = ref(false), rendered = ref(''), scrubbing = ref(false)
const start = computed(() => Number(props.settings.start) || 0)
const end = computed(() => props.items.length ? start.value + (props.items.length - 1) * Number(props.settings.interval) + Number(props.settings.duration) : start.value)
const knownDuration = computed(() => Number.isFinite(props.videoDuration) && Number(props.videoDuration) > 0 ? Number(props.videoDuration) : 0)
const axisEnd = computed(() => knownDuration.value || Math.max(1, Number.isFinite(end.value) ? end.value : 1))
const previewEnd = computed(() => Math.max(0, axisEnd.value - .1))
const cues = computed(() => props.items.map((item, index) => {
  const begin = start.value + index * Number(props.settings.interval)
  const text = props.settings.language_mode === 'original' ? item.source_text || item.text : item.display_text
  return { start: begin, end: begin + Number(props.settings.duration), label: `第 ${index + 1} 条 · ${String(text || '').slice(0, 60)}` }
}))
const spanStyle = computed(() => ({ left: `${Math.min(100, start.value / axisEnd.value * 100)}%`, width: `${Math.max(0, Math.min(axisEnd.value, end.value) - Math.min(axisEnd.value, start.value)) / axisEnd.value * 100}%` }))
const visibleCount = computed(() => knownDuration.value ? props.items.filter((_, i) => start.value + i * props.settings.interval < knownDuration.value).length : props.items.length)
const outsideCount = computed(() => props.items.length - visibleCount.value)
const clipped = computed(() => !!knownDuration.value && visibleCount.value > 0 && start.value + (visibleCount.value - 1) * props.settings.interval + props.settings.duration > knownDuration.value)
const activeIndex = computed(() => {
  const i = Math.floor((seconds.value - start.value) / Number(props.settings.interval))
  return i >= 0 && i < props.items.length && seconds.value < start.value + i * props.settings.interval + Number(props.settings.duration) ? i : -1
})
const payload = computed(() => JSON.stringify({ items: props.items, settings: props.settings, seconds: seconds.value }))
const signature = computed(() => props.uid + ':' + payload.value)
const outdated = computed(() => !!image.value && rendered.value !== signature.value)
let mounted = false, disposed = false, pending = false, request: AbortController | null = null
let timer: ReturnType<typeof setTimeout> | undefined
const canPreview = computed(() => props.items.length > 0 && !props.invalid && !scrubbing.value)
function clearImage() { if (image.value) URL.revokeObjectURL(image.value); image.value = ''; rendered.value = '' }
function setPosition(value: number | null) { if (value != null && Number.isFinite(value)) seconds.value = Math.max(0, Math.min(previewEnd.value, value)) }
function locateStart() { setPosition(start.value + Math.min(.5, Number(props.settings.duration) / 2)) }
function schedule() {
  clearTimeout(timer)
  if (mounted && !disposed && canPreview.value) timer = setTimeout(() => void preview(), 500)
}
function beginScrub() { scrubbing.value = true; clearTimeout(timer) }
function endScrub() { scrubbing.value = false; schedule() }
async function preview() {
  clearTimeout(timer)
  if (!mounted || disposed || !canPreview.value) return
  if (request) { pending = true; return }
  const current = request = new AbortController(), key = signature.value, body = payload.value
  loading.value = true; error.value = ''; pending = false
  try {
    const response = await fetch(`/api/projects/${props.uid}/comments/preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, signal: current.signal })
    if (!response.ok) { const data = await response.json(); throw new Error(data.message || '评论预览失败') }
    const blob = await response.blob()
    if (disposed || current.signal.aborted || key !== signature.value || !canPreview.value) return
    clearImage(); image.value = URL.createObjectURL(blob); rendered.value = key
  } catch (e: any) {
    if (!disposed && !current.signal.aborted && key === signature.value) error.value = e.message || '评论预览失败，请重试'
  } finally {
    if (request === current) { request = null; loading.value = false; if (pending || key !== signature.value) schedule() }
  }
}
watch([signature, canPreview], schedule)
watch(previewEnd, () => setPosition(seconds.value))
watch(() => props.uid, () => { request?.abort(); request = null; loading.value = false; pending = false; error.value = ''; clearImage(); locateStart(); schedule() })
onMounted(() => { mounted = true; locateStart(); schedule() })
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); request?.abort(); clearImage() })
</script>

<template>
  <section class="comment-preview" aria-label="评论整体时间轴与实时预览">
    <div class="range-heading"><strong>整体上屏范围</strong><span>{{ timeLabel(start) }} → {{ timeLabel(end) }} <small>· {{ items.length }} 条</small></span></div>
    <div class="range-track" role="img" :aria-label="`评论总范围：${timeLabel(start)} 至 ${timeLabel(end)}`"><span :style="spanStyle" /></div>
    <div class="axis-label"><span>0:00</span><span>{{ knownDuration ? '视频结束' : '预计评论结束 · 未取得视频时长' }} {{ timeLabel(axisEnd) }}</span></div>
    <n-alert v-if="outsideCount || clipped" type="warning" :show-icon="false" class="warning"><template v-if="outsideCount">{{ outsideCount }} 条评论在视频结束后才出现，不会显示。</template><template v-if="clipped">最后一条可见评论会在视频结尾被截短。</template></n-alert>
    <div class="frame-stage" :class="{ 'has-frame': !!image }" :aria-busy="loading">
      <img v-if="image" :src="image" alt="评论实际烧录画面预览" :class="{ outdated }" />
      <span v-else>{{ invalid ? '请先补齐文字并检查时间设置' : loading ? '正在生成画面…' : '调整后自动预览' }}</span>
      <span v-if="image && (loading || outdated)" class="frame-status">{{ loading ? '正在更新…' : '画面待更新' }}</span>
    </div>
    <div class="cursor-heading"><span>{{ activeIndex < 0 ? '此刻无上屏评论' : `此刻显示第 ${activeIndex + 1} 条` }}</span><output>{{ timeLabel(seconds) }} / {{ timeLabel(axisEnd) }}</output></div>
    <PreviewTimeline :value="seconds" :max="previewEnd" :cues="cues" kind="评论" label="评论预览进度" @update:value="setPosition" @dragstart="beginScrub" @dragend="endScrub" />
    <div class="preview-tools"><n-button size="tiny" quaternary @click="locateStart">定位到开始</n-button><label>预览秒数<n-input-number :value="seconds" :min="0" :max="previewEnd" :step=".1" :precision="1" :input-props="{ 'aria-label': '评论预览秒数' }" @update:value="setPosition" /></label><span>{{ scrubbing ? '松开后更新画面' : '调整后自动更新 · 不自动保存' }}</span></div>
    <n-alert v-if="error" type="error" :show-icon="false" class="warning">{{ error }} <n-button size="tiny" :disabled="invalid || loading" @click="preview">重试预览</n-button></n-alert>
  </section>
</template>

<style scoped>
.comment-preview { min-width:0; margin-bottom:18px; }
.range-heading,.cursor-heading,.axis-label { display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:12px; }
.range-heading { margin-bottom:10px; flex-wrap:wrap; } .range-heading small,.axis-label,.preview-tools { color:var(--df-muted); }
.range-track { position:relative; height:9px; border-radius:5px; overflow:hidden; background:var(--df-border); }
.range-track span { position:absolute; height:100%; background:var(--df-accent); border-radius:5px; }
.axis-label { margin:5px 0 12px; font-size:11px; } .warning { margin:10px 0; }
.frame-stage { position:relative; display:flex; align-items:center; justify-content:center; aspect-ratio:16/9; background:#080b10; border-radius:8px; overflow:hidden; color:var(--df-muted); }
.frame-stage.has-frame { display:block; aspect-ratio:auto; background:transparent; }
.frame-stage img { display:block; width:100%; height:auto; } .outdated { opacity:.65; }
.frame-status { position:absolute; bottom:10px; left:10px; background:#101318dd; padding:4px 8px; border-radius:4px; font-size:11px; }
.cursor-heading { margin:12px 0 7px; } output { font-variant-numeric:tabular-nums; color:var(--df-accent); }
.preview-tools { display:flex; align-items:center; gap:10px; flex-wrap:wrap; font-size:11px; margin-top:8px; }
.preview-tools label { display:flex; align-items:center; gap:6px; } .preview-tools :deep(.n-input-number) { width:100px; }
</style>
