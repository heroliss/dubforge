<script setup lang="ts">
// 音轨同步播放器: 视频为主控 (静音), 所选音轨按头部对齐跟播; 右侧为所选音轨波形
// 三轨等长时逐帧同步; 不等长时以视频时间为准 (音轨短则提前结束), 拖动/倍速跟随
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{
  tracks: { name: string, label: string, url: string, peaks: string, size?: number }[]
  videoUrl: string | null
}>()

const sel = ref(0)
const videoEl = ref<HTMLVideoElement | null>(null)
const audioEl = ref<HTMLAudioElement | null>(null)
const canvasEl = ref<HTMLCanvasElement | null>(null)
const peaks = ref<number[]>([])
const trackDur = ref(0)             // 所选音轨时长 (来自 peaks 端点)
const videoErr = ref(false)
const hasVideo = computed(() => !!props.videoUrl && !videoErr.value)
const cur = computed(() => props.tracks[sel.value])
let raf = 0

watch(() => props.tracks, () => {
  if (sel.value >= props.tracks.length) sel.value = 0
  loadPeaks()
}, { immediate: true })
watch(sel, loadPeaks)

async function loadPeaks() {
  peaks.value = []
  trackDur.value = 0
  const t = cur.value
  if (!t) return
  try {
    const r = await fetch(t.peaks)
    if (r.ok) {
      const j = await r.json()
      peaks.value = j.points ?? []
      trackDur.value = j.duration ?? 0
      draw()
    }
  } catch { /* 波形失败不影响播放 */ }
}

// —— 同步: 视频时间轴为基准, 音轨头部对齐 ——
function resync(hard = false) {
  const v = videoEl.value, a = audioEl.value
  if (!v || !a) return
  a.playbackRate = v.playbackRate
  if (hard || Math.abs(a.currentTime - v.currentTime) > 0.3) {
    a.currentTime = v.currentTime
  }
}
function onVideoPlay() {
  resync()
  startLoop()
  void audioEl.value?.play().catch(() => {})
}
function onVideoPause() {
  audioEl.value?.pause()
  stopLoop()
}
function onVideoEnded() {
  audioEl.value?.pause()
  stopLoop()
}
function onVideoSeeked() {
  resync(true)
  draw()
}
function onVideoRate() {
  if (audioEl.value && videoEl.value) {
    audioEl.value.playbackRate = videoEl.value.playbackRate
  }
}
function onAudioLoaded() {
  resync(true)
  if (videoEl.value && !videoEl.value.paused) {
    void audioEl.value?.play().catch(() => {})
  }
}

// —— 波形绘制: 播过的高亮蓝, 未播灰色; 点击跳转 ——
function draw() {
  const c = canvasEl.value
  if (!c) return
  const dpr = window.devicePixelRatio || 1
  const w = c.clientWidth, h = c.clientHeight
  if (!w || !h) return
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr)
    c.height = Math.round(h * dpr)
  }
  const ctx = c.getContext('2d')!
  ctx.clearRect(0, 0, c.width, c.height)
  const n = peaks.value.length
  if (!n) return
  const v = videoEl.value, a = audioEl.value
  const total = (hasVideo.value && v?.duration) ? v.duration : trackDur.value
  const now = hasVideo.value ? (v?.currentTime ?? 0) : (a?.currentTime ?? 0)
  const pos = total ? now / total : 0
  const bw = c.width / n
  for (let i = 0; i < n; i++) {
    const bar = Math.max(2 * dpr, peaks.value[i] * c.height * 0.92)
    ctx.fillStyle = i / n < pos ? '#4da3ff' : 'rgba(140,150,165,0.38)'
    ctx.fillRect(i * bw, (c.height - bar) / 2, Math.max(1, bw - 0.6), bar)
  }
}
function startLoop() {
  if (!raf) raf = requestAnimationFrame(loop)
}
function loop() {
  draw()
  raf = requestAnimationFrame(loop)
}
function stopLoop() {
  if (raf) { cancelAnimationFrame(raf); raf = 0 }
  draw()
}
function onCanvasClick(e: MouseEvent) {
  const c = canvasEl.value
  if (!c) return
  const rect = c.getBoundingClientRect()
  const total = (hasVideo.value && videoEl.value?.duration)
    ? videoEl.value!.duration : trackDur.value
  if (!total) return
  const t = (e.clientX - rect.left) / rect.width * total
  if (hasVideo.value && videoEl.value) {
    videoEl.value.currentTime = t
    resync(true)
  } else if (audioEl.value) {
    audioEl.value.currentTime = t
  }
  draw()
}

function fmtDur(sec?: number): string {
  if (!sec) return '—'
  const m = Math.floor(sec / 60)
  return `${m}分${Math.round(sec % 60).toString().padStart(2, '0')}秒`
}
function fmtSize(n?: number): string {
  if (!n) return '—'
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + ' GB'
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}
onBeforeUnmount(() => stopLoop())
</script>

<template>
  <n-grid :cols="5" :x-gap="16">
    <n-gi :span="3">
      <video v-if="hasVideo" ref="videoEl" :src="videoUrl || undefined" muted controls
             preload="metadata"
             style="width: 100%; max-height: 340px; border-radius: 8px; background: #000"
             @play="onVideoPlay" @pause="onVideoPause" @ended="onVideoEnded"
             @seeked="onVideoSeeked" @ratechange="onVideoRate" @error="videoErr = true" />
      <n-empty v-else-if="videoUrl && videoErr" size="small" description="视频无法加载 (源文件可能被移动)"
               style="padding: 40px 0" />
    </n-gi>
    <n-gi :span="2">
      <n-radio-group v-if="tracks.length > 1" v-model:value="sel" size="small"
                     style="margin-bottom: 8px">
        <n-radio-button v-for="(t, i) in tracks" :key="t.name" :value="i">
          {{ t.label }}
        </n-radio-button>
      </n-radio-group>
      <canvas ref="canvasEl" class="wave" @click="onCanvasClick"
              :title="hasVideo ? '点击波形跳转视频位置' : '点击波形跳转'" />
      <audio v-if="!hasVideo" ref="audioEl" :src="cur?.url" controls
             style="width: 100%; height: 36px; margin-top: 8px"
             @loadedmetadata="draw" @timeupdate="draw" />
      <audio v-else ref="audioEl" :src="cur?.url" preload="auto"
             style="display: none" @loadedmetadata="onAudioLoaded" />
      <n-text depth="3" style="font-size: 11.5px; display: block; margin-top: 6px">
        <template v-if="hasVideo">视频已静音, 播放的是所选音轨 (头部对齐同步; 点波形跳转)</template>
        <template v-else>点击波形跳转播放位置</template>
        <template v-if="trackDur"> · 音轨时长 {{ fmtDur(trackDur) }}</template>
        <template v-if="cur?.size"> · {{ fmtSize(cur.size) }}</template>
      </n-text>
    </n-gi>
  </n-grid>
</template>

<style scoped>
.wave {
  width: 100%;
  height: 84px;
  border: 1px solid #2a343f;
  border-radius: 6px;
  background: #0d1117;
  cursor: crosshair;
  display: block;
}
</style>
