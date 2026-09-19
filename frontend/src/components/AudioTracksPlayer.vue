<script setup lang="ts">
// 音轨同步播放器 v4: 视频为主控, 右侧四个单选音轨 —— 原始 / 分离人声 / 背景音 /
// 合成(人声+背景)。单选 = 一次听一路; 选「合成」时人声+背景两路同时播放。
// 原始音轨优先用视频自带的声音 (原始音质, 16k 的 raw.wav 仅作无视频时兜底):
// 勾选「原始」= 解除视频静音; 在原生控件里点小喇叭 = 同步选中「原始」, 双向一致。
// 全部按头部对齐跟随视频 (拖动/倍速/暂停同步, 漂移>0.3s 校正); 波形画播放位置竖线
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { fmtDur } from '../utils/format'

const MIXED = '__mixed__'
const props = defineProps<{
  tracks: { name: string, label: string, url: string, peaks: string, size?: number }[]
  videoUrl: string | null
}>()

const videoEl = ref<HTMLVideoElement | null>(null)
const canvasEls = new Map<string, HTMLCanvasElement>()
const audioEls = new Map<string, HTMLAudioElement>()
const selected = ref<string>('')
const peaksMap = ref<Record<string, number[]>>({})
const durs = ref<Record<string, number>>({})
const videoErr = ref(false)
const trackErr = ref<Set<string>>(new Set())   // 加载失败的音轨 (选中也没声音, 明示)
const hasVideo = computed(() => !!props.videoUrl && !videoErr.value)
let raf = 0

const rawName = computed(() => props.tracks[0]?.name ?? '')
const hasVocalBg = computed(() =>
  ['vocal.wav', 'background.wav'].every(n => props.tracks.some(t => t.name === n)))
// 单选项: 已生成的三轨 + 合成 (人声/背景齐备时)
const options = computed(() => {
  const opts = props.tracks.map(t => ({ name: t.name, label: t.label }))
  if (hasVocalBg.value) opts.push({ name: MIXED, label: '合成 (人声+背景)' })
  return opts
})

// 轮询会不断重建 tracks 数组 (内容不变) —— 按名字签名去重,
// 只有音轨集合真正变化时才重置选择/重载波形
const trackSig = computed(() => props.tracks.map(t => t.name).join('|'))
let lastSig = ''
watch(trackSig, (sig) => {
  if (sig === lastSig) return
  lastSig = sig
  trackErr.value = new Set()
  selected.value = rawName.value || props.tracks[0]?.name || ''
  void loadAllPeaks()
}, { immediate: true })

async function loadAllPeaks() {
  const seq = lastSig                       // 轨道签名: await 之后若已变化则丢弃结果
  peaksMap.value = {}
  durs.value = {}
  const got: Record<string, number[]> = {}
  const gotDur: Record<string, number> = {}
  await Promise.all(props.tracks.map(async (t) => {
    try {
      const r = await fetch(t.peaks)
      if (r.ok) {
        const j = await r.json()
        got[t.name] = j.points ?? []
        gotDur[t.name] = j.duration ?? 0
      }
    } catch { /* 波形失败不影响播放 */ }
  }))
  if (seq !== lastSig) return               // 期间轨道集合变了: 过期结果不写入
  peaksMap.value = got
  durs.value = gotDur
  draw()
}

// 合成波形 = 人声 + 背景逐桶相加 (削顶到 1) —— 预览用的近似, 试听才是真叠加
const mixedPeaks = computed<number[]>(() => {
  const v = peaksMap.value['vocal.wav'] ?? []
  const b = peaksMap.value['background.wav'] ?? []
  if (!v.length || !b.length) return []
  const n = Math.min(v.length, b.length)
  const out = new Array<number>(n)
  for (let i = 0; i < n; i++) out[i] = Math.min(1, v[i] + b[i])
  return out
})

function peaksFor(name: string): number[] {
  return name === MIXED ? mixedPeaks.value : (peaksMap.value[name] ?? [])
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
  if (el) canvasEls.set(name, el)
  else canvasEls.delete(name)
}
function setAudio(name: string, el: HTMLAudioElement | null) {
  if (el) audioEls.set(name, el)
  else audioEls.delete(name)
}

// —— 选中变化 → 视频静音联动 (选原始且视频在 = 解除静音, 其余静音) ——
watch(selected, (s) => {
  const v = videoEl.value
  if (v) v.muted = !(hasVideo.value && s === rawName.value)
})
// 原生控件的小喇叭 → 同步回单选 (双向一致, 防止叠加两层声音)
function onVideoVolumeChange() {
  const v = videoEl.value
  if (!v) return
  const wantRaw = hasVideo.value && selected.value === rawName.value
  if (!v.muted && !wantRaw) selected.value = rawName.value
  else if (v.muted && wantRaw) selected.value = ''
}

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
    for (let i = 0; i < n; i++) {
      const bar = Math.max(2 * dpr, pts[i] * c.height * 0.92)
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

onBeforeUnmount(() => stopLoop())
</script>

<template>
  <n-grid :cols="5" :x-gap="16">
    <n-gi :span="3">
      <video v-if="hasVideo" ref="videoEl" :src="videoUrl || undefined" muted controls
             preload="metadata"
             style="width: 100%; max-height: 340px; border-radius: 8px; background: #000"
             @play="onVideoPlay" @pause="onVideoPause" @ended="onVideoEnded"
             @seeked="onVideoSeeked" @ratechange="onVideoRate"
             @volumechange="onVideoVolumeChange" @error="videoErr = true" />
      <template v-else>
        <n-empty v-if="videoUrl && videoErr" size="small"
                 description="视频无法加载 (源文件可能被移动)" style="padding: 30px 0" />
        <n-button v-if="selected" size="small" @click="fallbackToggle">
          ▶/⏸ 播放选中的音轨
        </n-button>
      </template>
    </n-gi>
    <n-gi :span="2">
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
        单选试听一路; 「合成」= 人声+背景同时播放 · 选「原始」时用视频自带声音
        (原生小喇叭与本单选联动) · 竖线为播放位置 · 点波形跳转
      </n-text>
      <n-text v-if="trackErr.size" type="warning" style="font-size: 11.5px; display: block">
        ⚠ 音轨加载失败: {{ [...trackErr].map(n => options.find(o => o.name === n)?.label ?? n).join('、') }}
      </n-text>
    </n-gi>
  </n-grid>
  <!-- 隐藏音轨元素: 实际发声者 (原始轨在无视频时兜底); 必须留在 DOM 里 -->
  <div style="display: none">
    <audio v-for="t in (hasVideo ? tracks.filter(t => t.name !== rawName) : tracks)"
           :key="t.name"
           :ref="(el) => setAudio(t.name, el as HTMLAudioElement | null)"
           :src="t.url" preload="auto"
           @loadedmetadata="onAudioLoaded(t.name)" @error="onAudioError(t.name)" />
  </div>
</template>

<style scoped>
.trk { margin-bottom: 8px; }
.trk-head { display: flex; align-items: center; gap: 6px; margin-bottom: 3px; }
.wave {
  width: 100%;
  height: 52px;
  border: 1px solid #2a343f;
  border-radius: 5px;
  background: #0d1117;
  cursor: crosshair;
  display: block;
}
</style>
