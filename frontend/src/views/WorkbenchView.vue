<script setup lang="ts">
// 逐句精修工作台 (M5): 列表点选 → 视频定位 → 单句合成 → WebAudio 实时混音试听 → 采纳
// 后端: /api/projects/{uid}/workbench/* (M3a 已交付, 与正式步骤共享句级缓存)
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'

const route = useRoute()
const router = useRouter()
const message = useMessage()

const projects = ref<any[]>([])
const uid = ref((route.params.uid as string) || (route.query.uid as string) || '')
const state = ref<any>(null)
const sel = ref<number | null>(null)
const filter = ref('')
const volDub = ref(1.0), volBg = ref(0.3), syncVideo = ref(true)
const busyLine = ref<number | null>(null)
const playing = ref(false)

onUnmounted(() => { if (_keyHandler) window.removeEventListener('keydown', _keyHandler) })
const videoEl = ref<HTMLVideoElement>()
const lineListEl = ref<HTMLElement>()

const selLine = computed(() => state.value?.lines.find((l: any) => l.id === sel.value) ?? null)
const shownLines = computed(() => {
  const q = filter.value.trim().toLowerCase()
  return (state.value?.lines ?? []).filter((l: any) =>
    !q || l.text.toLowerCase().includes(q) || String(l.id) === q)
})

const presets = ref<string[]>([])
const presetSel = ref('')
const presetSource = ref('')
const diffCount = ref(0)
const applying = ref(false)
const presetOpts = computed(() => presets.value.map(p => ({ label: p, value: p })))

async function loadPresets() {
  const r = await fetch('/api/workbench-presets')
  const j = await r.json()
  presets.value = j.presets ?? []
  presetSource.value = j.source ?? ''
  if (presets.value.length && !presetSel.value)
    presetSel.value = j.source || presets.value[0]
  const d = await fetch('/api/workbench-presets/diff').then(x => x.json())
  diffCount.value = (d.diff ?? []).length
}
async function applyPreset() {
  if (!presetSel.value) return
  applying.value = true
  try {
    await fetch('/api/workbench-presets/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: presetSel.value }),
    })
    message.success(`已应用预设「${presetSel.value}」(参数即时生效)`)
    await loadPresets()
  } finally { applying.value = false }
}

async function loadProjects() {
  const r = await fetch('/api/projects')
  projects.value = (await r.json()).filter((p: any) => p.status !== 'created' || true)
}
async function load() {
  if (!uid.value) return
  const r = await fetch(`/api/projects/${uid.value}/workbench`)
  state.value = await r.json()
}
onMounted(async () => {
  await loadProjects()
  await loadPresets()
  await load()
  if (!uid.value && projects.value.length) pick(projects.value[0].uid)
  window.addEventListener('keydown', onKey)
})
let _keyHandler: ((e: KeyboardEvent) => void) | null = onKey
function onKey(e: KeyboardEvent) {
  const tag = (e.target as HTMLElement)?.tagName
  if (['INPUT', 'SELECT', 'TEXTAREA'].includes(tag)) return
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); move(1) }
  if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); move(-1) }
}
function pick(u: string) {
  uid.value = u
  sel.value = null
  router.replace({ query: { uid: u } })
  load()
}

function fmt(t: number) {
  const m = Math.floor(t / 60), s = (t % 60).toFixed(1)
  return `${String(m).padStart(2, '0')}:${s.padStart(4, '0')}`
}

async function select(id: number) {
  sel.value = id
  const l = state.value.lines.find((x: any) => x.id === id)
  if (l && videoEl.value && state.value.video.found) {
    videoEl.value.currentTime = l.start
  }
  await nextTick()
  lineListEl.value?.querySelector(`[data-id="${id}"]`)?.scrollIntoView({ block: 'nearest' })
}
function move(d: number) {
  const arr = shownLines.value
  if (!arr.length) return
  const idx = arr.findIndex((l: any) => l.id === sel.value)
  const next = arr[Math.min(arr.length - 1, Math.max(0, idx + d))]
  if (next) select(next.id)
}

async function synth(force = false) {
  if (!sel.value) return
  busyLine.value = sel.value
  try {
    const r = await fetch(`/api/projects/${uid.value}/workbench/lines/${sel.value}/synthesize`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    })
    const j = await r.json()
    if (!j.ok) { message.error(j.message || j.detail || '合成失败'); return }
    message.success(j.status === 'skipped' ? '已有缓存, 直接试听' : '合成完成')
    await load()
    preview(false)
  } finally { busyLine.value = null }
}

async function toggleConfirm() {
  if (!sel.value) return
  const l = state.value.lines.find((x: any) => x.id === sel.value)
  await fetch(`/api/projects/${uid.value}/workbench/lines/${sel.value}/confirm`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirmed: !l.confirmed }),
  })
  await load()
}

// ===== Web Audio 实时混音 (不落盘) =====
let ctx: AudioContext | null = null
let bgBuf: AudioBuffer | null = null
const bufCache = new Map<string, AudioBuffer>()
let live: AudioBufferSourceNode[] = []
let stopTimer: number | undefined

function actx() {
  if (!ctx) ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
  if (ctx.state === 'suspended') ctx.resume()
  return ctx
}
function stopAll() {
  live.forEach(s => { try { s.stop() } catch {} }); live = []
  clearTimeout(stopTimer); playing.value = false
  if (videoEl.value) videoEl.value.pause()
}
async function getBuf(url: string): Promise<AudioBuffer> {
  if (bufCache.has(url)) return bufCache.get(url)!
  const r = await fetch(url)
  if (!r.ok) throw new Error('音频不存在')
  const buf = await actx().decodeAudioData(await r.arrayBuffer())
  bufCache.set(url, buf)
  return buf
}
async function preview(mix: boolean) {
  const l = selLine.value
  if (!l) return
  stopAll()
  const c = actx()
  const t0 = c.currentTime + 0.08
  let end = t0
  if (l.wav.exists) {
    const buf = await getBuf(l.wav.url)
    const src = c.createBufferSource(); const g = c.createGain()
    g.gain.value = volDub.value
    src.buffer = buf; src.connect(g).connect(c.destination); src.start(t0)
    live.push(src); end = Math.max(end, t0 + buf.duration)
  } else {
    message.warning('本句还没有配音, 请先「合成本句」')
    return
  }
  if (mix && state.value.bg.found && volBg.value > 0) {
    if (!bgBuf) bgBuf = await getBuf(state.value.bg.url)
    const src = c.createBufferSource(); const g = c.createGain()
    g.gain.value = volBg.value
    src.buffer = bgBuf; src.connect(g).connect(c.destination)
    src.start(t0, l.start)
    live.push(src)
  }
  if (syncVideo.value && state.value.video.found && videoEl.value) {
    videoEl.value.muted = true; videoEl.value.currentTime = l.start; videoEl.value.play()
  }
  playing.value = true
  const span = Math.max(end - t0, l.end - l.start) + 0.6
  stopTimer = window.setTimeout(stopAll, span * 1000)
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <!-- 无 uid: 项目选择器 -->
    <template v-if="!uid">
      <n-h1>精修工作台</n-h1>
      <n-card title="选择项目">
        <n-space vertical>
          <n-text depth="3">选择一个已完成「配音任务」分析的项目开始逐句精修。</n-text>
          <n-empty v-if="!projects.length" description="还没有项目" />
          <n-button v-for="p in projects" :key="p.uid" block style="justify-content: flex-start"
                    @click="pick(p.uid)">
            {{ p.title }} <n-text depth="3" style="margin-left: 8px; font-size: 12px">{{ p.uid }}</n-text>
          </n-button>
        </n-space>
      </n-card>
    </template>

    <template v-else-if="state">
      <n-card size="small" style="margin-bottom: 12px" v-if="presets.length">
        <n-space align="center">
          <n-text style="font-size:13px">WebUI 预设</n-text>
          <n-select v-model:value="presetSel" :options="presetOpts" size="small" style="width:180px" />
          <n-button size="small" :loading="applying" @click="applyPreset">应用预设</n-button>
          <n-tag v-if="diffCount > 0" size="small" type="warning">{{ diffCount }} 项与预设不同</n-tag>
          <n-text v-else-if="presetSource" depth="3" style="font-size:12px">与预设「{{ presetSource }}」一致</n-text>
        </n-space>
      </n-card>
      <n-space justify="space-between" align="center" style="margin-bottom: 8px">
        <n-space align="center">
          <n-h1 style="margin: 0; font-size: 22px">🎧 精修工作台</n-h1>
          <n-tag size="small" :type="state.service.online ? 'success' : 'error'">
            {{ state.service.backend }} {{ state.service.online ? '在线' : '离线' }}
          </n-tag>
          <n-tag size="small">合成 {{ state.counts.synthesized }}/{{ state.counts.total }}</n-tag>
          <n-tag size="small" type="success">采纳 {{ state.counts.confirmed }}</n-tag>
        </n-space>
        <n-button quaternary size="small" @click="router.push(`/projects/${uid}`)">← 管线页</n-button>
      </n-space>

      <n-space size="large" align="start">
        <!-- 左: 逐句列表 -->
        <n-card size="small" style="width: 340px">
          <n-input v-model:value="filter" size="small" placeholder="过滤文字/句号…" clearable />
          <div ref="lineListEl" style="max-height: 62vh; overflow-y: auto; margin-top: 8px">
            <div v-for="l in shownLines" :key="l.id" :data-id="l.id"
                 @click="select(l.id)"
                 :style="`padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:4px;` +
                   (l.id === sel ? 'background:#24466b;' : 'background:transparent')"
                 class="lineitem">
              <div style="display:flex;justify-content:space-between;gap:8px">
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px">{{ l.text }}</span>
                <span style="color:#4da3ff;font-size:11px;white-space:nowrap">#{{ l.id }} {{ fmt(l.start) }}</span>
              </div>
              <div style="display:flex;gap:4px;margin-top:2px">
                <n-tag size="tiny" :type="l.wav.exists ? 'success' : 'default'">
                  {{ l.wav.exists ? `✓ ${l.wav.seconds ?? '?'}s` : '未合成' }}
                </n-tag>
                <n-tag v-if="l.confirmed" size="tiny" type="success">已采纳</n-tag>
                <n-tag v-if="l.speed_flag >= 1" size="tiny" type="warning">语速{{ l.speed_flag === 2 ? '!' : '' }}</n-tag>
              </div>
            </div>
          </div>
        </n-card>

        <!-- 右: 视频 + 操作 -->
        <div style="flex:1; min-width: 0">
          <n-card size="small">
            <video v-if="state.video.found" ref="videoEl" controls preload="metadata"
                   :src="state.video.url" style="width:100%;max-height:40vh;background:#000;border-radius:8px" />
            <n-alert v-else type="warning" :show-icon="false">无视频</n-alert>
          </n-card>

          <n-card size="small" style="margin-top: 12px" v-if="selLine">
            <n-text depth="3" style="font-size:12px">#{{ selLine.id }} · {{ fmt(selLine.start) }} → {{ fmt(selLine.end) }} · 句长 {{ selLine.duration }}s · 预估 {{ selLine.est_dur }}s</n-text>
            <div style="font-size:15px;margin:6px 0">{{ selLine.text }}</div>
            <n-space style="margin-top:8px">
              <n-button type="primary" :loading="busyLine === selLine.id" @click="synth(false)">🎤 合成本句</n-button>
              <n-button quaternary :disabled="busyLine === selLine.id" @click="synth(true)">强制重合成</n-button>
              <n-button type="info" @click="preview(true)">▶ 试听混音</n-button>
              <n-button quaternary @click="preview(false)">只听配音</n-button>
              <n-button quaternary @click="stopAll">■ 停止</n-button>
              <n-button :type="selLine.confirmed ? 'default' : 'success'" @click="toggleConfirm">
                {{ selLine.confirmed ? '取消采纳' : '✓ 采纳' }}
              </n-button>
            </n-space>
            <n-space align="center" style="margin-top:10px">
              <n-text style="font-size:13px">人声</n-text>
              <n-slider v-model:value="volDub" :min="0" :max="1.5" :step="0.05" style="width:110px" />
              <n-text style="font-size:13px">背景</n-text>
              <n-slider v-model:value="volBg" :min="0" :max="1.5" :step="0.05" style="width:110px"
                        :disabled="!state.bg.found" />
              <n-checkbox v-model:checked="syncVideo" size="small">画面同步</n-checkbox>
              <n-text v-if="!state.bg.found" depth="3" style="font-size:12px">(背景音未分离)</n-text>
            </n-space>
          </n-card>
          <n-card size="small" style="margin-top:12px" v-else>
            <n-text depth="3">← 选择左侧任意一句开始 (j/k 键或点击切换)</n-text>
          </n-card>
        </div>
      </n-space>
    </template>
  </n-layout-content>
</template>

<style scoped>
.lineitem:hover { background: #1c242d; }
</style>
