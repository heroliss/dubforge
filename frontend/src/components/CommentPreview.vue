<script setup lang="ts">
// 总时间范围与实际烧录帧一起预览；连续修改合并为串行请求，不保存草稿。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NInputNumber } from 'naive-ui'
import AppIcon from './AppIcon.vue'
import PreviewTimeline from './PreviewTimeline.vue'
import { subtitleTimeLabel as timeLabel } from '../utils/subtitleWorkspace'
const props = defineProps<{ uid: string; items: any[]; settings: any; invalid: boolean; videoDuration?: number; focusId?: string; focusRequest?: number; temporary?: boolean }>()
const emit = defineEmits<{ 'locate-comment': [id: string] }>()
const seconds = ref(0), image = ref(''), error = ref(''), loading = ref(false), rendered = ref(''), scrubbing = ref(false), pending = ref(false)
type PreviewLayout = { min_reached: boolean; height_exceeded: boolean; video_height_exceeded: boolean }
const layoutDetails = ref<PreviewLayout | null>(null)
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
  // 直接比较与时间轴相同的边界，避免小数节奏在起点相除后落到上一条。
  for (let i = cues.value.length - 1; i >= 0; i--) if (seconds.value >= cues.value[i].start && seconds.value < cues.value[i].end) return i
  return -1
})
const payload = computed(() => JSON.stringify({ items: props.items, settings: props.settings, seconds: seconds.value }))
const signature = computed(() => props.uid + ':' + payload.value)
const outdated = computed(() => !!image.value && rendered.value !== signature.value)
// 防抖和排队也属于等待画面，不能等网络请求发出后才给用户反馈。
const previewPending = computed(() => pending.value && !props.invalid && !scrubbing.value && props.items.length > 0)
const pendingMessage = computed(() => !loading.value ? '正在准备预览…' : image.value ? '正在更新预览…' : '正在生成预览…')
const layoutNotice = computed(() => {
  if (outdated.value || previewPending.value || !image.value || !layoutDetails.value) return ''
  if (layoutDetails.value.video_height_exceeded) return '已完整展开，部分文字可能超出画面；可增大宽度或调整字号范围'
  return layoutDetails.value.height_exceeded ? '已用最小字号，超出目标高度并完整展开' : ''
})
const activeItem = computed(() => activeIndex.value >= 0 ? props.items[activeIndex.value] : null)
const canLocateComment = computed(() => !!activeItem.value && !!image.value && !outdated.value && !previewPending.value)
let mounted = false, disposed = false, request: AbortController | null = null
// 已就绪的定位优先接续在途请求；参数连续编辑则等防抖结束后才进入队列。
let requestKey = '', readyKey = ''
let timer: ReturnType<typeof setTimeout> | undefined
const canPreview = computed(() => props.items.length > 0 && !props.invalid && !scrubbing.value)
function clearImage() { if (image.value) URL.revokeObjectURL(image.value); image.value = ''; rendered.value = ''; layoutDetails.value = null }
function responseLayout(response: Response): PreviewLayout | null {
  try {
    const raw = response.headers?.get?.('X-DubForge-Comment-Layout')
    if (!raw) return null
    const details = JSON.parse(raw)
    if (!details || typeof details !== 'object') return null
    return { min_reached: details.min_reached === true, height_exceeded: details.height_exceeded === true, video_height_exceeded: details.video_height_exceeded === true }
  } catch { return null }
}
function setPosition(value: number | null, immediate = true) {
  if (value == null || !Number.isFinite(value)) return
  seconds.value = Math.max(0, Math.min(previewEnd.value, value))
  if (immediate && mounted && !disposed && canPreview.value) { clearTimeout(timer); readyKey = signature.value; void preview() }
}
function locateStart(immediate = true) { setPosition(start.value, immediate) }
function locateFocused(immediate = true) {
  if (!props.focusId) return false
  const index = props.items.findIndex(item => item.id === props.focusId)
  if (index < 0) return false
  setPosition(cues.value[index].start, immediate)
  return true
}
function locateComment() {
  if (canLocateComment.value) emit('locate-comment', activeItem.value.id)
}
function schedule() {
  clearTimeout(timer)
  timer = undefined
  if (!mounted || disposed || !canPreview.value || rendered.value === signature.value) { pending.value = false; return }
  pending.value = true; error.value = ''
  if (requestKey === signature.value) return
  if (readyKey === signature.value) { void preview(); return }
  timer = setTimeout(() => { timer = undefined; readyKey = signature.value; void preview() }, 500)
}
function beginScrub() { scrubbing.value = true; pending.value = false; clearTimeout(timer); timer = undefined }
function endScrub() { scrubbing.value = false; setPosition(seconds.value) }
async function preview() {
  clearTimeout(timer)
  timer = undefined
  if (!mounted || disposed || !canPreview.value) { pending.value = false; return }
  if (rendered.value === signature.value) { readyKey = ''; pending.value = false; return }
  pending.value = true
  if (request) return
  const current = request = new AbortController(), key = signature.value, body = payload.value
  requestKey = key; readyKey = ''
  loading.value = true; error.value = ''
  try {
    const response = await fetch(`/api/projects/${props.uid}/comments/preview`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, signal: current.signal })
    if (!response.ok) { const data = await response.json(); throw new Error(data.message || '评论预览失败') }
    const blob = await response.blob()
    if (disposed || current.signal.aborted || key !== signature.value || !canPreview.value) return
    clearImage(); image.value = URL.createObjectURL(blob); rendered.value = key; layoutDetails.value = responseLayout(response)
  } catch (e: any) {
    if (!disposed && !current.signal.aborted && key === signature.value) { error.value = e.message || '评论预览失败，请重试'; layoutDetails.value = null }
  } finally {
    if (request === current) {
      request = null; requestKey = ''; loading.value = false
      if (key === signature.value) pending.value = false
      if (readyKey === signature.value && key !== signature.value) void preview()
      else if (key !== signature.value && !timer) schedule()
    }
  }
}
watch([signature, canPreview], schedule)
watch(previewEnd, () => setPosition(seconds.value, false))
// 同一个 ID 可反复定位；只随定位请求和评论顺序变化跳转，不干扰用户自由拖动。
watch([() => props.focusRequest, () => props.focusId, () => JSON.stringify(props.items.map(item => item.id))], () => locateFocused())
watch(() => props.uid, () => { request?.abort(); request = null; requestKey = ''; readyKey = ''; loading.value = false; pending.value = false; error.value = ''; clearImage(); if (!locateFocused()) locateStart(false); schedule() })
onMounted(() => { mounted = true; if (!locateFocused()) locateStart(false); schedule() })
onBeforeUnmount(() => { disposed = true; pending.value = false; clearTimeout(timer); request?.abort(); clearImage() })
</script>

<template>
  <section class="comment-preview" aria-label="评论整体时间轴与实时预览">
    <div class="range-heading"><strong>整体上屏范围</strong><span>{{ timeLabel(start) }} → {{ timeLabel(end) }} <small>· {{ items.length }} 条</small></span></div>
    <p v-if="temporary" class="temporary-note">含待选评论试放，不改变上屏方案</p>
    <div class="range-track" role="img" :aria-label="`评论总范围：${timeLabel(start)} 至 ${timeLabel(end)}`"><span :style="spanStyle" /></div>
    <div class="axis-label"><span>0:00</span><span>{{ knownDuration ? '视频结束' : '预计评论结束 · 未取得视频时长' }} {{ timeLabel(axisEnd) }}</span></div>
    <n-alert v-if="outsideCount || clipped" type="warning" :show-icon="false" class="warning"><template v-if="outsideCount">{{ outsideCount }} 条评论在视频结束后才出现，不会显示。</template><template v-if="clipped">最后一条可见评论会在视频结尾被截短。</template></n-alert>
    <div class="frame-stage" :class="{ 'has-frame': !!image }" :aria-busy="previewPending">
      <img v-if="image" :src="image" alt="评论实际烧录画面预览" :class="{ outdated }" />
      <span v-else-if="!previewPending">{{ invalid ? '请先补齐文字并检查时间设置' : error ? '预览暂不可用' : '调整后自动预览' }}</span>
      <div v-if="previewPending" class="frame-transition" role="status" aria-live="polite" aria-atomic="true"><span class="frame-transition-label"><span class="preview-spinner" aria-hidden="true" />{{ pendingMessage }}</span></div>
      <span v-else-if="image && outdated" class="frame-status">{{ error ? '预览未更新' : scrubbing ? '松开后更新画面' : '画面待更新' }}</span>
    </div>
    <n-alert v-if="layoutNotice" :type="layoutDetails?.video_height_exceeded ? 'warning' : 'info'" :show-icon="false" class="layout-note">{{ layoutNotice }}</n-alert>
    <div class="cursor-heading"><div class="cursor-comment"><span>{{ activeIndex < 0 ? '此刻无上屏评论' : `此刻显示第 ${activeIndex + 1} 条` }}</span><n-button v-if="activeItem" class="preview-action" size="small" secondary :disabled="!canLocateComment" aria-label="定位评论列表" :title="outdated || loading ? '画面更新后可定位当前显示的评论' : '返回上方评论列表并高亮此条'" @click="locateComment"><template #icon><AppIcon name="locate" :size="14" /></template>返回列表</n-button></div><output>{{ timeLabel(seconds) }} / {{ timeLabel(axisEnd) }}</output></div>
    <PreviewTimeline :value="seconds" :max="previewEnd" :cues="cues" kind="评论" label="评论预览进度" @update:value="setPosition" @dragstart="beginScrub" @dragend="endScrub" />
    <div class="preview-tools" role="group" aria-label="评论预览定位操作"><div class="preview-position-tools"><n-button class="preview-action" size="small" quaternary aria-label="定位到开始" title="回到第一条评论的上屏开始时间" @click="locateStart()"><template #icon><AppIcon name="skip-back" :size="14" /></template>上屏起点</n-button><label>跳转到<n-input-number size="small" :value="seconds" :min="0" :max="previewEnd" :step=".1" :update-value-on-input="true" :input-props="{ 'aria-label': '评论预览秒数' }" @update:value="setPosition"><template #suffix>秒</template></n-input-number></label></div><span class="preview-helper">{{ scrubbing ? '松开后更新画面' : '定位后更新画面 · 不自动保存' }}</span></div>
    <n-alert v-if="error" type="error" :show-icon="false" class="warning"><div class="preview-error"><span>{{ error }}</span><n-button class="preview-action" size="small" secondary aria-label="重试预览" title="重新生成当前时刻的评论预览" :disabled="invalid || loading" :loading="loading" @click="preview"><template #icon><AppIcon name="refresh" :size="14" /></template>重试</n-button></div></n-alert>
  </section>
</template>

<style scoped>
.comment-preview { min-width:0; margin-bottom:18px; }
.range-heading,.cursor-heading,.axis-label { display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:12px; }
.range-heading { margin-bottom:10px; flex-wrap:wrap; } .range-heading small,.axis-label,.preview-tools { color:var(--df-muted); }
.temporary-note { margin:0 0 10px; color:var(--df-muted); font-size:11px; }
.range-track { position:relative; height:9px; border-radius:5px; overflow:hidden; background:var(--df-border); }
.range-track span { position:absolute; height:100%; background:var(--df-accent); border-radius:5px; }
.axis-label { margin:5px 0 12px; font-size:11px; } .warning { margin:10px 0; }
.frame-stage { position:relative; display:flex; align-items:center; justify-content:center; aspect-ratio:16/9; background:#080b10; border-radius:8px; overflow:hidden; color:var(--df-muted); }
.frame-stage.has-frame { display:block; aspect-ratio:auto; background:transparent; }
.frame-stage img { display:block; width:100%; height:auto; } .outdated { opacity:.55; }
.frame-transition { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; background:#080b1026; pointer-events:none; }
.frame-transition-label { display:flex; align-items:center; gap:9px; padding:10px 15px; border:1px solid #ffffff20; border-radius:8px; background:#101722ed; color:#e8edf5; font-size:12px; box-shadow:0 3px 16px #0003; }
.preview-spinner { width:16px; height:16px; box-sizing:border-box; flex:none; border:2px solid #ffffff38; border-top-color:var(--df-accent, #e7bd75); border-radius:50%; animation:preview-spin .8s linear infinite; }
@keyframes preview-spin { to { transform:rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .preview-spinner { animation:none; } }
.frame-status { position:absolute; bottom:10px; left:10px; background:#101318dd; padding:4px 8px; border-radius:4px; font-size:11px; }
.layout-note { margin-top:8px; font-size:12px; }
.cursor-heading { margin:12px 0 7px; flex-wrap:wrap; } output { margin-left:auto; white-space:nowrap; font-variant-numeric:tabular-nums; color:var(--df-accent); }
.cursor-comment { display:flex; align-items:center; flex-wrap:wrap; gap:6px 10px; min-width:0; }
.preview-action{flex-shrink:0;font-size:12px}
.preview-tools { display:flex; align-items:center; justify-content:space-between; gap:8px 16px; flex-wrap:wrap; font-size:11px; margin-top:12px; }
.preview-position-tools{display:flex;align-items:center;flex-wrap:wrap;gap:8px 14px}
.preview-tools label { display:flex; align-items:center; gap:8px; font-size:12px; } .preview-tools :deep(.n-input-number) { width:112px; }
.preview-helper{line-height:1.65}
.preview-error{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px 14px}.preview-error>span{flex:1;min-width:160px;overflow-wrap:anywhere}
@media(max-width:600px){.preview-tools{gap:8px}.preview-helper{flex-basis:100%}.cursor-comment{gap:6px 8px}.preview-error>span{flex-basis:100%;min-width:0}}
</style>
