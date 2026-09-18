<script setup lang="ts">
// 音轨同步播放器 v3: 视频为主控 (静音), 右侧三条波形各带启用勾选 ——
// 多轨同时启用即叠加混音 (每个音轨一个隐藏 audio 元素, 浏览器原生混音);
// 全部按头部对齐跟随视频 (拖动/倍速/暂停同步, 漂移>0.3s 校正);
// 每条波形画出播放位置竖线, 点击波形跳转
// v3 修复: 此前模板里漏渲染了 <audio> 元素 (勾选任何轨都不会发声),
// 且播放循环未按勾选过滤 (应只播启用的轨)
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{
  tracks: { name: string, label: string, url: string, peaks: string, size?: number }[]
  videoUrl: string | null
}>()

const videoEl = ref<HTMLVideoElement | null>(null)
const canvasEls = new Map<string, HTMLCanvasElement>()
const audioEls = new Map<string, HTMLAudioElement>()
const enabled = ref<Set<string>>(new Set())
const peaksMap = ref<Record<string, number[]>>({})
const durs = ref<Record<string, number>>({})
const videoErr = ref(false)
const hasVideo = computed(() => !!props.videoUrl && !videoErr.value)
let raf = 0

// 轮询会不断重建 tracks 数组 (内容不变) —— 按名字签名去重,
// 只有音轨集合真正变化时才重置勾选/重载波形
const trackSig = computed(() => props.tracks.map(t => t.name).join('|'))
let lastSig = ''
watch(trackSig, (sig) => {
  if (sig === lastSig) return
  lastSig = sig
  // 默认启用第一条 (原始音轨); 其余由用户勾选叠加
  enabled.value = new Set([props.tracks[0]?.name].filter(Boolean) as string[])
  void loadAllPeaks()
}, { immediate: true })

async function loadAllPeaks() {
  peaksMap.value = {}
  durs.value = {}
  await Promise.all(props.tracks.map(async (t) => {
    try {
      const r = await fetch(t.peaks)
      if (r.ok) {
        const j = await r.json()
        peaksMap.value[t.name] = j.points ?? []
        durs.value[t.name] = j.duration ?? 0
      }
    } catch { /* 波形失败不影响播放 */ }
  }))
  draw()
}

function enabledAudios(): HTMLAudioElement[] {
  return props.tracks
    .filter(t => enabled.value.has(t.name))
    .map(t => audioEls.get(t.name))
    .filter((a): a is HTMLAudioElement => !!a)
}

function toggle(name: string, v: boolean) {
  const s = new Set(enabled.value)
  if (v) s.add(name)
  else s.delete(name)
  enabled.value = s
  // 播放中切换: 新勾选的轨立即跟上, 取消勾选的轨立即停
  const a = audioEls.get(name)
  const vid = videoEl.value
  if (!a) return
  if (!v) {
    a.pause()
  } else if (vid && !vid.paused) {
    a.playbackRate = vid.playbackRate
    a.currentTime = Math.min(vid.currentTime, a.duration || vid.currentTime)
    void a.play().catch(() => {})
  }
}

function setCanvas(name: string, el: HTMLCanvasElement | null) {
  if (el) canvasEls.set(name, el)
  else canvasEls.delete(name)
}
function setAudio(name: string, el: HTMLAudioElement | null) {
  if (el) audioEls.set(name, el)
  else audioEls.delete(name)
}

// —— 同步: 视频时间轴为基准, 各启用音轨头部对齐 ——
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
  for (const a of enabledAudios()) void a.play().catch(() => {})
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
  if (!v.paused && enabled.value.has(name)) void a.play().catch(() => {})
}

// —— 波形: 启用轨亮色 / 未启用暗色; 竖线 = 播放位置 ——
function draw() {
  const v = videoEl.value
  const maxDur = Math.max(0.001, ...Object.values(durs.value))
  const total = (hasVideo.value && v?.duration) ? v.duration : maxDur
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
    const on = enabled.value.has(name)
    const pts = peaksMap.value[name] ?? []
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
    ? videoEl.value!.duration : Math.max(0.001, ...Object.values(durs.value))
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
  const any = [...audioEls.values()].some(a => !a.paused)
  if (any) {
    for (const a of audioEls.values()) a.pause()
    stopLoop()
  } else {
    for (const a of enabledAudios()) void a.play().catch(() => {})
    startLoop()
  }
}

function fmtDur(sec?: number): string {
  if (!sec) return ''
  const m = Math.floor(sec / 60)
  return `${m}:${Math.round(sec % 60).toString().padStart(2, '0')}`
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
      <template v-else>
        <n-empty v-if="videoUrl && videoErr" size="small"
                 description="视频无法加载 (源文件可能被移动)" style="padding: 30px 0" />
        <n-button v-if="enabled.size" size="small" @click="fallbackToggle">
          ▶/⏸ 播放启用的音轨
        </n-button>
      </template>
    </n-gi>
    <n-gi :span="2">
      <div v-for="t in tracks" :key="t.name" class="trk">
        <div class="trk-head">
          <n-checkbox size="small" :checked="enabled.has(t.name)"
                      @update:checked="(v: boolean) => toggle(t.name, v)">
            {{ t.label }}
          </n-checkbox>
          <n-text depth="3" style="font-size: 11px; margin-left: auto">{{ fmtDur(durs[t.name]) }}</n-text>
        </div>
        <canvas :ref="(el) => setCanvas(t.name, el as HTMLCanvasElement | null)"
                class="wave" @click="onCanvasClick" />
      </div>
      <n-text depth="3" style="font-size: 11.5px; display: block; margin-top: 6px">
        勾选启用; 多轨同时启用 = 叠加混音 · 竖线为播放位置 · 点波形跳转
      </n-text>
    </n-gi>
  </n-grid>
  <!-- 隐藏音轨元素: 实际发声者 (浏览器原生混音); 必须留在 DOM 里 -->
  <div style="display: none">
    <audio v-for="t in tracks" :key="t.name"
           :ref="(el) => setAudio(t.name, el as HTMLAudioElement | null)"
           :src="t.url" preload="auto" @loadedmetadata="onAudioLoaded(t.name)" />
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
