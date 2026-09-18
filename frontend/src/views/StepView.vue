<script setup lang="ts">
// 步骤专属页: 说明 / 状态 / 依赖门禁 / 本步配置 / 完整日志 / 产物清单 / 运行操作
// 由左侧流程导航进入, 一页只专注一个步骤; 依赖未就绪时明确指出并禁用运行
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import MediaInfo from '../components/MediaInfo.vue'
import AudioTracksPlayer from '../components/AudioTracksPlayer.vue'
import ParamControl from '../components/ParamControl.vue'
import { fmtSize } from '../utils/format'
import { useNavStore, STEP_ORDER, STATUS_TEXT } from '../stores/nav'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()

const uid = route.params.uid as string
const key = route.params.key as string

const step = computed(() => nav.stepMap[key])
const stepIdx = STEP_ORDER.indexOf(key as any)

// 依赖门禁: 上游步骤必须 done/stale (stale = 产物已更新, 重跑下游正是其语义)
const blockers = computed(() =>
  STEP_ORDER.slice(0, stepIdx < 0 ? 0 : stepIdx)
    .map((k) => nav.stepMap[k])
    .filter((s) => s && !['done', 'stale'].includes(s.status)))
const running = computed(() => step.value?.status === 'running' || step.value?.status === 'queued')
const canRun = computed(() => !!step.value && !running.value
  && blockers.value.length === 0 && !dirMissing.value)

// 项目目录不在磁盘上 (被移走/删除) → 禁用运行, 从回收站还原后恢复
const dirMissing = ref(false)
onMounted(async () => {
  try {
    const r = await fetch(`/api/projects/${uid}`)
    if (r.ok) dirMissing.value = (await r.json()).dir_exists === false
  } catch { /* 忽略 */ }
})

const STATUS_TAG: Record<string, 'default' | 'success' | 'error' | 'warning' | 'info'> = {
  pending: 'default', queued: 'warning', running: 'info', done: 'success',
  failed: 'error', stale: 'warning', interrupted: 'error',
}

const busy = ref(false)
async function run(force = false) {
  busy.value = true
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { message.error(j.message || `启动失败 (HTTP ${r.status})`); return }
    if (j.status === 'skipped') message.info(j.message)
    else message.success(force ? '已开始强制重跑' : '已启动')
    await nav.poll()
    loadLog()
  } catch { message.error('启动请求失败 (网络异常)') } finally { busy.value = false }
}

function rerunDialog() {
  const downstream = nav.steps.slice(nav.steps.findIndex(s => s.key === key) + 1)
    .filter((s: any) => s.status === 'done' || s.status === 'stale')
  dialog.warning({
    title: `强制重跑「${step.value.label}」`,
    content: downstream.length
      ? `将删除本步产物并重新执行；下游 ${downstream.length} 个步骤将被标记为「已过期」（需要重跑）。`
      : '将删除本步产物并重新执行。',
    positiveText: '强制重跑',
    negativeText: '取消',
    onPositiveClick: () => run(true),
  })
}

// ---- 停止 / 强制结束 / 暂停 (都有防误触确认) ----
const pausing = computed(() => !!step.value?.pausing)

function stopDialog() {
  dialog.warning({
    title: `停止「${step.value.label}」？`,
    content: '正在运行的子进程 (下载/ffmpeg 等) 将立即终止；进程内计算会在当前单元结束后退出。'
      + '过程临时文件自动清理，已完成的正式产物保留。',
    positiveText: '确认停止',
    negativeText: '继续运行',
    onPositiveClick: () => doCancel(),
  })
}

async function doCancel() {
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/cancel`, { method: 'POST' })
    if (!r.ok) { message.error('停止信号发送失败'); return }
  } catch { message.error('停止信号发送失败'); return }
  message.info('停止信号已发送, 等待步骤退出；长时间无响应可用「强制结束」')
}

function forceDialog() {
  dialog.error({
    title: `强制结束「${step.value.label}」？`,
    content: '仅建议在「取消中」长时间无响应时使用：子进程立即终止，状态立刻按已取消呈现；'
      + '进程内计算若仍在后台收尾，其结果将被自动丢弃。',
    positiveText: '强制结束',
    negativeText: '返回',
    onPositiveClick: () => doForce(),
  })
}

async function doForce() {
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/force_stop`, { method: 'POST' })
    if (!r.ok) { message.error('强制结束失败'); return }
  } catch { message.error('强制结束失败'); return }
  message.success('已强制结束')
  await nav.poll()
}

async function setPause(p: boolean) {
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/pause`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pause: p }),
    })
    if (!r.ok) { message.error(p ? '暂停失败' : '继续失败'); return }
  } catch { message.error(p ? '暂停失败' : '继续失败'); return }
  await nav.poll()
}

async function revealStep() {
  const r = await fetch(`/api/projects/${uid}/steps/${key}/reveal`, { method: 'POST' })
  if (!r.ok) message.error('打开目录失败')
}

async function revealArt(stepKey: string, name: string) {
  const r = await fetch(
    `/api/projects/${uid}/steps/${stepKey}/artifacts/${encodeURIComponent(name)}/reveal`,
    { method: 'POST' })
  if (!r.ok) message.error('定位失败 (文件可能已不在磁盘)')
}

// ---- 文件清单: 上游文件 / 计划产物 / 实际产物 ----
function upInfo(inp: { step: string, name: string }) {
  const up = nav.stepMap[inp.step]
  const art = up?.artifacts?.find((a: any) => a.name === inp.name)
  return { ok: !!art, size: art?.size, label: up?.label ?? inp.step }
}
function producedCount(pattern: string): number | null {
  if (!pattern.includes('*')) return null
  const re = new RegExp('^'
    + pattern.replace(/[.*+?^${}()|[\]\\]/g, (m) => (m === '*' ? '.*' : `\\${m}`)) + '$')
  return (step.value?.artifacts ?? []).filter((a: any) => re.test(a.name)).length
}
function isProduced(name: string): boolean {
  const n = producedCount(name)
  return n === null
    ? (step.value?.artifacts ?? []).some((a: any) => a.name === name)
    : n > 0
}

// ---- 日志: 运行态 1.5s 轮询并自动滚底 ----
const logLines = ref<string[]>([])
const logBox = ref<HTMLElement | null>(null)
let logTimer: number | undefined

async function loadLog() {
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/log?tail=500`)
    if (!r.ok) return
    const j = await r.json()
    logLines.value = j.lines ?? []
  } catch { /* 后端瞬断: 下轮轮询再取 */ }
  nextTick(() => {
    if (logBox.value && running.value) logBox.value.scrollTop = logBox.value.scrollHeight
  })
}
function syncLogPolling() {
  const need = running.value
  if (need && !logTimer) {
    loadLog()
    logTimer = window.setInterval(loadLog, 1500)
  } else if (!need) {
    if (logTimer) {
      clearInterval(logTimer)
      logTimer = undefined
    }
    loadLog()                    // 初始挂载 / 运行收尾: 拉当前日志
  }
}
watch(running, syncLogPolling, { immediate: true })
onUnmounted(() => logTimer && clearInterval(logTimer))

// ---- 本步配置快照 (展平一层) ----
const cfgRows = computed(() => {
  const cfg = step.value?.cfg ?? {}
  const rows: { k: string, v: string }[] = []
  for (const [k, v] of Object.entries(cfg)) {
    if (v && typeof v === 'object') {
      for (const [k2, v2] of Object.entries(v as any)) {
        rows.push({ k: `${k}.${k2}`, v: Array.isArray(v2) ? `${v2.length} 项` : String(v2) })
      }
    } else {
      rows.push({ k, v: v === '' || v === null ? '(空)' : String(v) })
    }
  }
  return rows
})

// ---- 本步骤参数 (本项目覆盖 ?? 全局默认兜底; 逐键标识是否偏离全局) ----
const _paramSchema = ref<Record<string, any> | null>(null)
const overriddenMap = ref<Record<string, boolean>>({})
const defaultsMap = ref<Record<string, any>>({})
const paramFields = computed<any[]>(() => _paramSchema.value?.[key]?.fields ?? [])
const draft = ref<Record<string, any>>({})
const savedDraft = ref('')
const savingParams = ref(false)
const paramsDirty = computed(() => savedDraft.value !== '' &&
  JSON.stringify(draft.value) !== savedDraft.value)
const anyOverridden = computed(() => Object.values(overriddenMap.value).some(Boolean))
const extraCfgRows = computed(() =>
  cfgRows.value.filter((r) => !paramFields.value.some((f) => f.path === r.k)))

async function loadParamSchema() {
  try {
    const r = await fetch(`/api/projects/${uid}/step_params`)
    if (!r.ok) return
    const j = await r.json()
    _paramSchema.value = j.steps
    overriddenMap.value = j.overridden || {}
    defaultsMap.value = j.defaults || {}
    initDraft()
  } catch { /* 拿不到 schema 时退回只读展示 */ }
}
function initDraft() {
  const d: Record<string, any> = {}
  for (const f of paramFields.value) {
    let v = _paramSchema.value?.[key]?.values?.[f.path]
    if (v === null || v === undefined) {
      v = f.type === 'bool' ? false : (f.type === 'int' || f.type === 'float' ? 0 : '')
    }
    d[f.path] = v
  }
  draft.value = d
  savedDraft.value = JSON.stringify(d)
}
onMounted(loadParamSchema)

async function putParams(updates: Record<string, any>, reset: string[]) {
  try {
    const r = await fetch(`/api/projects/${uid}/step_params`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates, reset }),
    })
    if (!r.ok) { message.error('参数保存失败'); return false }
    await loadParamSchema()
    await nav.poll()
    return true
  } catch { message.error('参数保存失败 (网络异常)'); return false }
}

async function saveParams() {
  savingParams.value = true
  try {
    // 与全局默认相同的键不落覆盖 (改回原值 = 自动回到跟随全局, 过期标识随之撤销)
    const updates: Record<string, any> = {}
    const reset: string[] = []
    for (const f of paramFields.value) {
      if (JSON.stringify(draft.value[f.path]) === JSON.stringify(defaultsMap.value[f.path])) {
        if (overriddenMap.value[f.path]) reset.push(f.path)
      } else {
        updates[f.path] = draft.value[f.path]
      }
    }
    if (await putParams(updates, reset)) {
      message.success('已保存到本项目 (全局默认不变) —— 本步骤下次运行按新参数执行')
    }
  } finally { savingParams.value = false }
}

async function resetOne(path: string) {
  if (await putParams({}, [path])) message.info(`已恢复跟随全局默认: ${path}`)
}
async function resetAll() {
  const paths = Object.keys(overriddenMap.value).filter((k) => overriddenMap.value[k])
  if (!paths.length) return
  if (await putParams({}, paths)) message.success('已全部恢复跟随全局默认')
}

// ---- download 页媒体信息 (视频右侧信息栏) ----
const media = ref<any>({ found: false })
if (key === 'download') {
  onMounted(async () => {
    try {
      const r = await fetch(`/api/projects/${uid}/media`)
      if (r.ok) media.value = await r.json()
    } catch { /* 忽略 */ }
  })
}

// ---- 产出预览: 按步骤类型直接预览对应产物 (视频/音频/文本) ----
const AUDIO_LABEL: Record<string, string> = {
  'raw.wav': '原始音轨', 'vocal.wav': '分离人声', 'background.wav': '背景音',
  'dub_full.wav': '完整配音轨',
}
const TEXT_TRUNCATE = 6000

const preview = computed(() => {
  const arts = step.value?.artifacts ?? []
  const has = (n: string) => arts.some((a: any) => a.name === n)
  const artUrl = (n: string) => `/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(n)}/raw`
  if (key === 'download') {
    // 本地引用/下载统一走 workbench 视频端点 (支持 Range 拖动)
    return step.value?.status === 'done'
      ? { kind: 'video', url: `/api/projects/${uid}/workbench/video`, audios: [] } : null
  }
  if (key === 'assemble') {
    const fin = arts.find((a: any) => a.name.startsWith('final_') && a.name.endsWith('.mp4'))
    const audios = ['dub_full.wav'].filter(has)
      .map((n) => ({ name: n, label: AUDIO_LABEL[n] ?? n, url: artUrl(n) }))
    if (!fin && !audios.length) return null
    // 成片未生成但配音轨已有时退回纯音频预览 (此前 kind 不匹配导致永远不显示)
    return fin ? { kind: 'video', url: artUrl(fin.name), audios }
               : { kind: 'audio', url: null, audios }
  }
  if (key === 'preprocess') {
    // 音轨分离: 视频同步预览 + 三轨切换 (AudioTracksPlayer)
    const audios = ['raw.wav', 'vocal.wav', 'background.wav'].filter(has)
      .map((n) => {
        const art = (step.value?.artifacts ?? []).find((a: any) => a.name === n)
        const base = `/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(n)}`
        return { name: n, label: AUDIO_LABEL[n] ?? n, url: `${base}/raw`,
                 peaks: `${base}/peaks`, size: art?.size }
      })
    return audios.length
      ? { kind: 'tracks', audios, videoUrl: `/api/projects/${uid}/workbench/video` }
      : null
  }
  if (key === 'tts') {
    return { kind: 'workbench' }
  }
  // 文本类: 转录/分句/翻译/字幕/配音任务 → 取第一个存在的文本产物
  const textArts: Record<string, string[]> = {
    transcribe: ['src.srt', 'transcribe.json'],
    split: ['split.json'],
    translate: ['translate.json'],
    subtitles: ['bilingual.srt', 'trans.srt'],
    dubtask: ['dubtask.json'],
  }
  const name = (textArts[key] ?? []).find(has)
  return name ? { kind: 'text', name } : null
})

const previewText = ref('')
watch(() => (preview.value?.kind === 'text' ? (preview.value as any).name : null),
      loadText, { immediate: true })
async function loadText(name: string | null) {
  if (!name) { previewText.value = ''; return }
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${key}/artifacts/${encodeURIComponent(name)}/raw`)
    if (!r.ok) { previewText.value = '(读取失败)'; return }
    previewText.value = (await r.text()).slice(0, TEXT_TRUNCATE)
  } catch { previewText.value = '(读取失败)' }
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <div v-if="step">
      <n-space justify="space-between" align="center">
        <n-space align="center">
          <n-h1 style="margin: 0; font-size: 20px">{{ step.label }}</n-h1>
          <n-tag size="small" :type="STATUS_TAG[step.status] || 'default'"
                 :bordered="step.status !== 'pending'">
            {{ STATUS_TEXT[step.status] || step.status }}
          </n-tag>
          <n-tag v-if="step.status === 'done' && step.fresh" size="small" type="success"
                 :bordered="false">已是最新</n-tag>
          <n-tag v-else-if="step.status === 'done'" size="small" type="warning"
                 :bordered="false">需重跑</n-tag>
        </n-space>
        <n-space>
          <n-button size="small" quaternary title="在文件浏览器中打开本步骤目录"
                    @click="revealStep">📁</n-button>
          <n-button v-if="running" size="small" secondary :disabled="step.cancelling"
                    :title="pausing ? '继续执行' : '在当前单元结束后暂停 (如 demucs 单曲计算中会等它算完)'"
                    @click="setPause(!pausing)">{{ pausing ? '▶ 继续' : '⏸ 暂停' }}</n-button>
          <n-button v-if="running" type="warning" :disabled="step.cancelling"
                    @click="stopDialog">⏹ {{ step.cancelling ? '取消中…' : '停止' }}</n-button>
          <n-button v-if="running && step.cancelling" type="error" secondary
                    @click="forceDialog">⛔ 强制结束</n-button>
          <n-button v-else-if="!running" type="primary" :loading="busy"
                    :disabled="!canRun || dirMissing"
                    @click="run(false)">▶ 运行</n-button>
          <n-button v-if="['done', 'stale', 'failed', 'interrupted'].includes(step.status)"
                    :loading="busy" :disabled="dirMissing"
                    @click="rerunDialog">🔁 强制重跑</n-button>
        </n-space>
      </n-space>
      <n-text depth="3" style="font-size: 12px">{{ step.description }}</n-text>
      <n-tag v-if="running && step.cancelling" size="small" type="warning"
             :bordered="false" style="margin-left: 10px">正在取消…</n-tag>
      <n-progress v-if="running" type="line" :percentage="step.progress || 0"
                  indicator-placement="inside" :height="14" processing
                  style="margin-top: 12px" />

      <!-- 幽灵项目门禁: 目录缺失时明确禁用并说明 -->
      <n-alert v-if="dirMissing" type="error" style="margin-top: 14px"
               title="项目目录不在磁盘上 —— 运行已禁用">
        目录可能被移动或删除。从回收站还原目录后刷新本页即可恢复运行;
        确认不要了可回项目列表删除该项目清理登记。
      </n-alert>

      <!-- 依赖门禁: 明确指出缺哪些上游 -->
      <n-alert v-if="blockers.length" type="warning" :show-icon="true"
               style="margin-top: 14px" title="无法运行 —— 上游依赖未就绪">
        <div v-for="b in blockers" :key="b.key" style="margin: 2px 0">
          <n-text strong>「{{ b.label }}」</n-text>
          <n-text depth="3">当前状态: {{ STATUS_TEXT[b.status] || b.status }}</n-text>
          <n-button size="tiny" quaternary type="primary"
                    @click="router.push(`/projects/${uid}/steps/${b.key}`)">前往 →</n-button>
        </div>
      </n-alert>

      <!-- 错误三件套 -->
      <n-alert v-if="step.error" type="error" :show-icon="false" style="margin-top: 14px">
        <n-text strong>{{ step.error.message }}</n-text><br />
        <n-text depth="3" style="font-size: 12px">[{{ step.error.code }}]
          {{ step.error.suggestion }}</n-text>
      </n-alert>

      <!-- 产出预览: 有对应产物时直接展示 -->
      <n-card v-if="preview" size="small" title="产出预览" style="margin-top: 14px">
        <template v-if="key === 'download' && media.found">
          <n-grid :cols="2" :x-gap="16">
            <n-gi>
              <video v-if="preview.url" :src="preview.url" controls preload="metadata"
                     style="width: 100%; max-height: 340px; border-radius: 8px; background: #000" />
            </n-gi>
            <n-gi>
              <MediaInfo :media="media" />
            </n-gi>
          </n-grid>
        </template>
        <AudioTracksPlayer v-else-if="preview.kind === 'tracks'"
                           :tracks="preview.audios" :video-url="preview.videoUrl" />
        <video v-else-if="preview.kind === 'video' && preview.url" :src="preview.url"
               controls preload="metadata"
               style="width: 100%; max-height: 340px; border-radius: 8px; background: #000" />
        <template v-if="preview.kind === 'audio'">
          <div v-for="a in preview.audios ?? []" :key="a.name" class="audio-row">
            <n-text depth="3" style="font-size: 12px; width: 76px">{{ a.label }}</n-text>
            <audio :src="a.url" controls style="width: 100%; height: 36px" />
          </div>
        </template>
        <pre v-if="preview.kind === 'text'" class="textprev">{{ previewText || '(空)' }}</pre>
        <n-text v-if="preview.kind === 'text' && previewText.length >= 6000"
                depth="3" style="font-size: 11px">
          (内容较长, 仅显示前 6000 字符; 完整文件见项目目录)
        </n-text>
        <n-text v-if="preview.kind === 'workbench'" depth="3" style="font-size: 12.5px">
          逐句音频的试听与参数精修请进入「🎧 精修工作台」(左侧导航) —— 每句可单独合成、
          试听、调参考音色与情感、采纳后进入成片合成。
        </n-text>
      </n-card>

      <n-grid :cols="2" :x-gap="14" style="margin-top: 14px">
        <!-- 日志 -->
        <n-gi span="2">
          <n-card size="small" title="运行日志">
            <pre ref="logBox" class="logbox">{{ logLines.join('\n') || '(暂无日志)' }}</pre>
          </n-card>
        </n-gi>
        <!-- 本步骤参数: 项目级覆盖 ?? 全局默认; 标识偏离项, 可单键/整体恢复 -->
        <n-gi v-if="cfgRows.length">
          <n-card size="small" title="本步骤参数 (本项目 · 全局默认兜底)">
            <n-form v-if="paramFields.length" label-placement="left" label-width="170"
                    size="small" style="margin-bottom: 10px">
              <n-form-item v-for="f in paramFields" :key="f.path">
                <template #label>
                  <span class="plabel">
                    {{ f.label }}
                    <n-tag v-if="overriddenMap[f.path]" size="tiny" type="warning"
                           :bordered="false">项目已改</n-tag>
                    <n-tag v-else size="tiny" :bordered="false" class="gtag">跟随全局</n-tag>
                  </span>
                </template>
                <div class="frow">
                  <n-tooltip trigger="hover" placement="top" :delay="200">
                    <template #trigger>
                      <span class="ctl">
                        <ParamControl :field="f" v-model="draft[f.path]" />
                      </span>
                    </template>
                    {{ f.help || f.path }}<template v-if="overriddenMap[f.path]">
                      <br />全局默认: {{ defaultsMap[f.path] ?? '(空)' }}
                    </template>
                  </n-tooltip>
                  <n-button v-if="overriddenMap[f.path]" size="tiny" quaternary
                            title="恢复跟随全局默认" @click="resetOne(f.path)">↺</n-button>
                </div>
              </n-form-item>
              <div class="frow">
                <n-button size="small" type="primary" :loading="savingParams"
                          :disabled="!paramsDirty" @click="saveParams">💾 应用本步参数</n-button>
                <n-button v-if="anyOverridden" size="small" quaternary
                          :disabled="savingParams" @click="resetAll">↺ 全部恢复全局默认</n-button>
                <n-text v-if="paramsDirty" type="warning" style="font-size: 12px">
                  ● 有未应用的修改
                </n-text>
              </div>
            </n-form>
            <n-descriptions v-if="extraCfgRows.length" :column="1" label-placement="left"
                            size="small" bordered>
              <n-descriptions-item v-for="row in extraCfgRows" :key="row.k" :label="row.k">
                {{ row.v }}
              </n-descriptions-item>
            </n-descriptions>
            <n-text depth="3" style="font-size: 11.5px; display: block; margin-top: 8px">
              新项目跟随全局默认; 这里改动只影响本项目 (与全局相同的值不落覆盖, 改回原值即自动回到跟随全局)。
              参数一改输入指纹就变, 已完成的本步/下游会失去「已是最新」, 重跑即按新参数执行。
              其余关联配置 (如 LLM 密钥) 在「⚙️ 设置」页修改。
            </n-text>
          </n-card>
        </n-gi>
        <!-- 文件清单: 上游文件 / 计划产物 / 实际产物, 每行可点击定位 -->
        <n-gi>
          <n-card size="small" title="文件 (上游 / 计划 / 实际)">
            <template v-if="(step.inputs?.length ?? 0) || step.artifacts?.length">
              <div class="fsec">上游文件</div>
              <div v-for="inp in step.inputs" :key="inp.step + '/' + inp.name" class="fline">
                <span class="dot" :class="upInfo(inp).ok ? 'ok' : 'miss'" />
                <n-text style="font-size: 12.5px">{{ inp.name }}</n-text>
                <n-text depth="3" style="font-size: 11.5px; margin-left: 6px; flex: 1">
                  「{{ upInfo(inp).label }}」{{ inp.desc }}<template v-if="upInfo(inp).ok">
                    · {{ fmtSize(upInfo(inp).size) }}</template><template v-else> · 未生成</template>
                </n-text>
                <n-button v-if="upInfo(inp).ok" size="tiny" quaternary title="在文件浏览器中定位"
                          @click="revealArt(inp.step, inp.name)">📍</n-button>
              </div>
              <div class="fsec">计划产物</div>
              <div v-for="o in step.outputs" :key="o.name" class="fline">
                <span class="dot" :class="isProduced(o.name) ? 'ok' : 'plan'" />
                <n-text style="font-size: 12.5px">{{ o.name }}</n-text>
                <n-text depth="3" style="font-size: 11.5px; margin-left: 6px; flex: 1">
                  {{ o.desc }}<template v-if="producedCount(o.name) !== null">
                    · 已有 {{ producedCount(o.name) }} 个</template>
                </n-text>
              </div>
            </template>
            <div class="fsec">
              实际产物<template v-if="step.artifacts?.length"> ({{ step.artifacts.length }})</template>
            </div>
            <n-text v-if="!step.artifacts?.length" depth="3" style="font-size: 12px">
              (暂无产物)
            </n-text>
            <div v-for="a in step.artifacts" :key="a.name" class="fline">
              <n-text style="font-size: 12.5px">{{ a.name }}</n-text>
              <n-text depth="3" style="font-size: 11.5px; margin-left: 8px; flex: 1">
                {{ a.file }} · {{ fmtSize(a.size) }}
              </n-text>
              <n-button size="tiny" quaternary title="在文件浏览器中定位"
                        @click="revealArt(key, a.name)">📍</n-button>
            </div>
            <n-text depth="3" style="font-size: 11px; display: block; margin-top: 8px">
              📍 在 Windows 资源管理器中显示该文件 (由系统决定窗口是否前置)。
            </n-text>
          </n-card>
        </n-gi>
      </n-grid>
    </div>
    <n-empty v-else description="步骤不存在" style="margin-top: 80px">
      <template #extra>
        <n-button @click="router.push(`/projects/${uid}`)">返回流程总览</n-button>
      </template>
    </n-empty>
  </n-layout-content>
</template>

<style scoped>
.logbox {
  background: #0d1117;
  border: 1px solid #2a343f;
  border-radius: 6px;
  padding: 10px 14px;
  height: 38vh;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.fsec { font-size: 11.5px; color: rgba(255,255,255,0.45); margin: 8px 0 4px; }
.fsec:first-child { margin-top: 0; }
.fline { display: flex; align-items: center; gap: 4px; padding: 2px 0; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: none; margin-right: 4px; }
.dot.ok { background: #18a058; }
.dot.miss { background: #d03050; }
.dot.plan { background: rgba(255,255,255,0.25); }
.ctl { display: block; width: 100%; }
.frow { display: flex; align-items: center; gap: 4px; width: 100%; }
.plabel { display: inline-flex; align-items: center; gap: 4px; }
.gtag { opacity: 0.55; }
.audio-row { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
.textprev {
  background: #0d1117;
  border: 1px solid #2a343f;
  border-radius: 6px;
  padding: 10px 14px;
  max-height: 300px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
