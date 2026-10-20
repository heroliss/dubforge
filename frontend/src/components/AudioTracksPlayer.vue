<script setup lang="ts">
// 音轨同步播放器 v4: 视频为主控, 右侧四个单选音轨 —— 原始 / 分离人声 / 背景音 /
// 合成(人声+背景)。单选 = 一次听一路; 选「合成」时人声+背景两路同时播放。
// 原始音轨优先用视频自带的声音 (原始音质, 16k 的 raw.wav 仅作无视频时兜底):
// 音轨选择与监听总音量独立；静音和恢复不会切换声源。
// 全部按头部对齐跟随视频 (拖动/倍速/暂停同步, 漂移>0.3s 校正); 波形画播放位置竖线
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NButton, NEmpty, NRadio, NRadioGroup, NText } from 'naive-ui'
import { fmtDur } from '../utils/format'
import { vReleaseMedia } from '../utils/preview'
import VideoControls from './VideoControls.vue'
import { listeningAudioState, type ListeningAudioState } from '../utils/listeningAudio'

const MIXED = '__mixed__'
const props = defineProps<{
  tracks: { name: string, label: string, url: string, peaks: string,
    size?: number, sha256?: string }[]
  videoUrl: string | null
  chapters?: { title?: string; start_time?: number; end_time?: number | null }[]
}>()

const videoEl = ref<HTMLVideoElement | null>(null)
const canvasEls = new Map<string, HTMLCanvasElement>()
const audioEls = new Map<string, HTMLAudioElement>()
const selected = ref<string>('')
const monitorAudio = ref<ListeningAudioState>({ volume: 1, muted: false })
function applyMonitorAudio() {
  const audio = monitorAudio.value, video = videoEl.value
  if (video) {
    video.volume = audio.volume
    video.muted = audio.muted || !(hasVideo.value && selected.value === rawName.value)
  }
  for (const media of audioEls.values()) { media.volume = audio.volume; media.muted = audio.muted }
}
function setMonitorAudio(value: ListeningAudioState) {
  monitorAudio.value = listeningAudioState(value)
  applyMonitorAudio()
}
const peaksMap = ref<Record<string, number[]>>({})
const durs = ref<Record<string, number>>({})
const peaksMax = ref<Record<string, number>>({})
const rightEl = ref<HTMLElement | null>(null)
let resizeObs: ResizeObserver | null = null
const videoErr = ref(false)
const trackErr = ref<Set<string>>(new Set())   // 加载失败的音轨 (选中也没声音, 明示)
const hasVideo = computed(() => !!props.videoUrl && !videoErr.value)
let raf = 0

const rawName = computed(() => props.tracks[0]?.name ?? '')
const hasVocalBg = computed(() =>
  ['vocal.wav', 'background.wav'].every(n => props.tracks.some(t => t.name === n)))
// 单选项: 原始 → 合成 (放原始下面, 方便对比) → 人声 → 背景
const options = computed(() => {
  const rest = props.tracks.slice(1).map(t => ({ name: t.name, label: t.label }))
  const mixed = hasVocalBg.value ? [{ name: MIXED, label: '合成 (人声+背景)' }] : []
  const head = props.tracks.slice(0, 1).map(t => ({ name: t.name, label: t.label }))
  return [...head, ...mixed, ...rest]
})

// 轮询会不断重建 tracks 数组。签名必须包含内容版本；同名 wav 被重跑替换后
// 也要重新加载波形和 audio 元素，不能只按文件名去重。
const trackSig = computed(() => props.tracks
  .map(t => `${t.name}:${t.sha256 || ''}:${t.size ?? ''}`).join('|'))
let lastSig = ''
watch(() => props.videoUrl, () => {
  videoErr.value = false
  for (const audio of audioEls.values()) audio.pause()
  stopLoop()
})
watch(trackSig, (sig) => {
  if (sig === lastSig) return
  lastSig = sig
  videoEl.value?.pause()
  for (const audio of audioEls.values()) audio.pause()
  trackErr.value = new Set()
  selected.value = rawName.value || props.tracks[0]?.name || ''
  void loadAllPeaks()
}, { immediate: true })

async function loadAllPeaks() {
  const seq = lastSig                       // 轨道签名: await 之后若已变化则丢弃结果
  peaksMap.value = {}
  durs.value = {}
  peaksMax.value = {}
  const got: Record<string, number[]> = {}
  const gotDur: Record<string, number> = {}
  const gotMax: Record<string, number> = {}
  const jobs = props.tracks.map(async (t) => {
    try {
      const r = await fetch(t.peaks, { cache: 'no-store' })
      if (r.ok) {
        const j = await r.json()
        got[t.name] = j.points ?? []
        gotDur[t.name] = j.duration ?? 0
        gotMax[t.name] = Math.max(0.0001, ...(j.points ?? [1]))
      }
    } catch { /* 波形失败不影响播放 */ }
  })
  // 合成轨: 服务端逐样本真混音 (峰值包络直接相加是最坏情形上界, 会高估)
  const vT = props.tracks.find(t => t.name === 'vocal.wav')
  if (vT && hasVocalBg.value) {
    jobs.push((async () => {
      try {
        const url = `${vT.peaks}_mixed?other=${encodeURIComponent('background.wav')}`
        const r = await fetch(url, { cache: 'no-store' })
        if (r.ok) {
          const j = await r.json()
          got[MIXED] = j.points ?? []
          gotDur[MIXED] = j.duration ?? 0
          gotMax[MIXED] = Math.max(0.0001, ...(j.points ?? [1]))
        }
      } catch { /* 回退到客户端近似 */ }
    })())
  }
  await Promise.all(jobs)
  if (seq !== lastSig) return               // 期间轨道集合变了: 过期结果不写入
  peaksMap.value = got
  durs.value = gotDur
  peaksMax.value = gotMax
  draw()
}

function peaksFor(name: string): number[] {
  if (name !== MIXED) return peaksMap.value[name] ?? []
  const server = peaksMap.value[MIXED]
  if (server?.length) return server
  // 服务端真混音不可用时的客户端近似: 峰值包络相加 (最坏情形上界, 偏高)
  const v = peaksMap.value['vocal.wav'] ?? []
  const b = peaksMap.value['background.wav'] ?? []
  const n = Math.min(v.length, b.length)
  const out = new Array<number>(n)
  for (let i = 0; i < n; i++) out[i] = Math.min(1, v[i] + b[i])
  return out
}
function durFor(name: string): number {
  if (name !== MIXED) return durs.value[name] ?? 0
  return Math.max(durs.value['vocal.wav'] ?? 0, durs.value['background.wav'] ?? 0)
}
// 选中项对应的发声元素; 原始音轨在视频存在时用视频本身的声音 (不叠加 raw.wav)
function audiosFor(name: string): HTMLAudioElement[] {
  if (name === MIXED) {
    return ['vocal.wav', 'background.wav']
      .map(n => audioEls.get(n)).filter((a): a is HTMLAudioElement => !!a)
  }
  if (name === rawName.value && hasVideo.value) return []   // 声音走视频
  const a = audioEls.get(name)
  return a ? [a] : []
}

function setCanvas(name: string, el: HTMLCanvasElement | null) {
  if (el) {
    canvasEls.set(name, el)
    resizeObs?.observe(el)            // 行高变化 → 重绘 (位图尺寸跟随 clientHeight)
  } else {
    const old = canvasEls.get(name)
    if (old) resizeObs?.unobserve(old)
    canvasEls.delete(name)
  }
}
function setAudio(name: string, el: HTMLAudioElement | null) {
  if (el) { audioEls.set(name, el); el.volume = monitorAudio.value.volume; el.muted = monitorAudio.value.muted }
  else audioEls.delete(name)
}

// —— 选中变化 → 视频静音联动 + 立即换声源 ——
// (换台不能只等下次 play 事件: 视频正在播时切单选, 旧轨要停、新轨要从视频
//  当前时间对齐接着响, 否则叠音/无声)
watch(selected, (s) => {
  const v = videoEl.value
  applyMonitorAudio()
  for (const a of audioEls.values()) a.pause()
  const act = audiosFor(s)
  if (v) {
    for (const a of act) {
      a.playbackRate = v.playbackRate
      a.currentTime = Math.min(v.currentTime, a.duration || v.currentTime)
    }
    if (!v.paused) for (const a of act) void a.play().catch(() => {})
  }
  draw()
})
watch(videoEl, applyMonitorAudio)

// —— 同步: 视频时间轴为基准, 各发声元素头部对齐 ——
function resync(hard = false) {
  const v = videoEl.value
  if (!v) return
  for (const a of audioEls.values()) {
    a.playbackRate = v.playbackRate
    if (hard || Math.abs(a.currentTime - v.currentTime) > 0.3) {
      a.currentTime = Math.min(v.currentTime, a.duration || v.currentTime)
    }
  }
}
function onVideoPlay() {
  resync()
  startLoop()
  for (const a of audiosFor(selected.value)) void a.play().catch(() => {})
}
function onVideoPause() {
  for (const a of audioEls.values()) a.pause()
  stopLoop()
}
function onVideoEnded() {
  for (const a of audioEls.values()) a.pause()
  stopLoop()
}
function onVideoSeeked() {
  resync(true)
  draw()
}
function onVideoRate() {
  const v = videoEl.value
  if (v) for (const a of audioEls.values()) a.playbackRate = v.playbackRate
}
function onAudioLoaded(name: string) {
  const a = audioEls.get(name), v = videoEl.value
  if (!a || !v) return
  a.playbackRate = v.playbackRate
  a.currentTime = Math.min(v.currentTime, a.duration || v.currentTime)
  if (!v.paused && audiosFor(selected.value).includes(a)) void a.play().catch(() => {})
}
function onAudioError(name: string) {
  trackErr.value = new Set([...trackErr.value, name])
}

// 元数据到达: 用真实宽高比替换 16/9 兜底, 元素盒不再信箱式留黑边
function onMeta(e: Event) {
  const v = e.target as HTMLVideoElement
  if (v.videoWidth && v.videoHeight) {
    v.style.aspectRatio = `${v.videoWidth} / ${v.videoHeight}`
  }
  applyMonitorAudio()
  draw()
}

// 布局/尺寸变化 (视频元数据加载后高度变化、窗口缩放) → 波形重绘,
// 否则初次绘制可能停留在布局未定的小尺寸上
function onWinResize() { draw() }
onMounted(() => {
  resizeObs = new ResizeObserver(() => draw())
  if (rightEl.value) resizeObs.observe(rightEl.value)
  window.addEventListener('resize', onWinResize)
})

// —— 波形: 选中轨亮色 / 其余暗色; 竖线 = 播放位置 ——
function draw() {
  const v = videoEl.value
  const total = (hasVideo.value && v?.duration) ? v.duration
    : Math.max(0.001, ...props.tracks.map(t => durFor(t.name)),
               props.tracks.length ? 0.001 : 0.001)
  const now = v?.currentTime ?? 0
  const pos = total ? Math.min(1, now / total) : 0
  for (const [name, c] of canvasEls.entries()) {
    const dpr = window.devicePixelRatio || 1
    const w = c.clientWidth, h = c.clientHeight
    if (!w || !h) continue
    if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
      c.width = Math.round(w * dpr)
      c.height = Math.round(h * dpr)
    }
    const ctx = c.getContext('2d')!
    ctx.clearRect(0, 0, c.width, c.height)
    const on = selected.value === name
    const pts = peaksFor(name)
    const n = pts.length
    const bw = n ? c.width / n : 0
    // 显示归一: 安静的轨也放大到可视高度 (增益限 1~4 倍, 不放大噪声底到满幅)
    const gain = Math.min(4, Math.max(1, 0.92 / (peaksMax.value[name] ?? 1)))
    for (let i = 0; i < n; i++) {
      const bar = Math.max(2 * dpr, pts[i] * gain * c.height * 0.92)
      ctx.fillStyle = on
        ? (i / n < pos ? '#4da3ff' : 'rgba(90,110,140,0.75)')
        : 'rgba(120,130,145,0.22)'
      ctx.fillRect(i * bw, (c.height - bar) / 2, Math.max(1, bw - 0.6), bar)
    }
    // 播放位置竖线
    const x = pos * c.width
    ctx.fillStyle = on ? '#7ec2ff' : 'rgba(255,255,255,0.35)'
    ctx.fillRect(x - dpr, 0, 2 * dpr, c.height)
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
  const total = (hasVideo.value && videoEl.value?.duration)
    ? videoEl.value!.duration
    : Math.max(0.001, ...props.tracks.map(t => durFor(t.name)), 0.001)
  const target = e.currentTarget as HTMLCanvasElement
  const rect = target.getBoundingClientRect()
  const t = (e.clientX - rect.left) / rect.width * total
  if (hasVideo.value && videoEl.value) {
    videoEl.value.currentTime = t
    resync(true)
  } else {
    for (const a of audioEls.values()) a.currentTime = t
  }
  draw()
}
// 无视频时的简易走带 (波形单击跳转已够用, 这里补播放/暂停)
function fallbackToggle() {
  const act = audiosFor(selected.value)
  const any = act.some(a => !a.paused)
  if (any) {
    for (const a of audioEls.values()) a.pause()
    stopLoop()
  } else {
    for (const a of act) void a.play().catch(() => {})
    startLoop()
  }
}

onBeforeUnmount(() => {
  stopLoop()
  resizeObs?.disconnect()
  window.removeEventListener('resize', onWinResize)
})
</script>

<template>
  <div class="prev-row">
    <div class="vid-col">
      <div v-if="hasVideo" class="video-shell">
      <video v-release-media :key="videoUrl || ''" ref="videoEl" class="vid" :src="videoUrl || undefined"
             muted preload="metadata"
             @play="onVideoPlay" @pause="onVideoPause" @ended="onVideoEnded"
             @seeked="onVideoSeeked" @ratechange="onVideoRate"
             @loadedmetadata="onMeta"
             @error="videoErr = true" />
      <VideoControls :media="videoEl" :chapters="chapters" :audio-control="monitorAudio" @audio-change="setMonitorAudio" />
      </div>
      <template v-else>
        <n-empty v-if="videoUrl && videoErr" size="small"
                 description="视频无法加载 (源文件可能被移动)" style="padding: 30px 0" />
        <n-button v-if="selected" size="small" @click="fallbackToggle">
          ▶/⏸ 播放选中的音轨
        </n-button>
      </template>
    </div>
    <div ref="rightEl" class="right">
      <n-radio-group :value="selected" size="small"
                     @update:value="(v: string) => (selected = v)">
        <div v-for="o in options" :key="o.name" class="trk">
          <div class="trk-head">
            <n-radio :value="o.name">{{ o.label }}</n-radio>
            <n-text depth="3" style="font-size: 11px; margin-left: auto">
              {{ fmtDur(durFor(o.name)) }}
            </n-text>
          </div>
          <canvas :ref="(el) => setCanvas(o.name, el as HTMLCanvasElement | null)"
                  class="wave" @click="onCanvasClick" />
        </div>
      </n-radio-group>
      <n-text depth="3" style="font-size: 11.5px; display: block; margin-top: 6px">
        单选试听一路; 「合成」= 人声+背景逐样本混音 · 选「原始」时用视频自带声音
        · 小喇叭与音量只影响本机试听 · 竖线为播放位置 · 点波形跳转
      </n-text>
      <n-text v-if="trackErr.size" type="warning" style="font-size: 11.5px; display: block">
        ⚠ 音轨加载失败: {{ [...trackErr].map(n => options.find(o => o.name === n)?.label ?? n).join('、') }}
      </n-text>
    </div>
  </div>
  <!-- 隐藏音轨元素: 实际发声者 (原始轨在无视频时兜底); 必须留在 DOM 里 -->
  <div style="display: none">
    <audio v-for="t in (hasVideo ? tracks.filter(t => t.name !== rawName) : tracks)" v-release-media
           :key="`${t.name}:${t.sha256 || ''}:${t.size ?? ''}`"
           :ref="(el) => setAudio(t.name, el as HTMLAudioElement | null)"
           :src="t.url" preload="auto"
           @loadedmetadata="onAudioLoaded(t.name)" @error="onAudioError(t.name)" />
  </div>
</template>

<style scoped>
/* 预览行: 视频列贴内容宽 (由高度封顶决定), 音轨列吃掉全部剩余宽度;
   窄屏 (<900px) 自动改上下堆叠 —— 不留大块空白 */
.prev-row { display: flex; gap: 16px; align-items: stretch; flex-wrap: wrap; }
.vid-col { flex: 0 1 auto; min-width: 0; }
.right { flex: 1 1 340px; min-width: 260px; display: flex; flex-direction: column; min-height: 0; }
@media (max-width: 900px) {
  .prev-row { flex-direction: column; flex-wrap: nowrap; }
}
/* 视频始终按自身宽高比呈现, 由最大高度/最大宽度封顶 (绝不信箱式拉伸出黑边);
   贴左排布 (列已贴合内容宽, 不再居中留白) */
.vid {
  display: block;
  width: auto;
  height: auto;
  max-width: 100%;
  max-height: 340px;
  aspect-ratio: 16 / 9;              /* 元数据到达后替换为真实比例 */
  border-radius: 8px;
  background: #000;
}
.video-shell { position: relative; min-width: 0; }
/* 音轨行均分右列高度。canvas 固有尺寸 300x150 会污染 flex 基准 ——
   用 basis 0 + height 0 消掉 */
.right :deep(.n-radio-group) { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.trk {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  margin-bottom: 8px;
}
.trk-head { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; flex: none; }
.wave {
  width: 100%;
  flex: 1 1 0;
  height: 0;
  min-height: 40px;
  border: 1px solid #2a343f;
  border-radius: 5px;
  background: #0d1117;
  cursor: crosshair;
  display: block;
}
</style>
