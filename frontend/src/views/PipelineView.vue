<script setup lang="ts">
// 总览: 项目信息 + 原视频 (预览+信息) + 项目文件 + 步骤清单
// 勾选步骤 = 允许自动续跑; 「运行该步骤」只跑一步, 「运行本步及后续」按勾选链自动推进
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'
import {
  useNavStore, STEP_ORDER, STATUS_TAG, STATUS_TEXT, STATUS_EMOJI,
} from '../stores/nav'
import MediaInfo from '../components/MediaInfo.vue'

const route = useRoute()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()
const uid = route.params.uid as string

const isAuto = (key: string) => (nav.autoSteps ? nav.autoSteps.includes(key) : true)
async function toggleAuto(key: string, val: boolean) {
  const set = new Set(nav.autoSteps ?? [...STEP_ORDER])
  if (val) set.add(key)
  else set.delete(key)
  nav.autoSteps = STEP_ORDER.filter((k) => set.has(k))
  await fetch(`/api/projects/${uid}/auto_steps`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ steps: nav.autoSteps }),
  })
}

// ---- 原视频 ----
const media = ref<any>({ found: false })
async function loadMedia() {
  try {
    const r = await fetch(`/api/projects/${uid}/media`)
    if (r.ok) media.value = await r.json()
  } catch { /* 忽略 */ }
}
onMounted(loadMedia)
watch(() => nav.stepMap['download']?.status, (s) => { if (s === 'done') loadMedia() })
const videoSrc = computed(() =>
  media.value.found ? `/api/projects/${uid}/workbench/video` : '')

function fmtSize(n?: number): string {
  if (n == null) return '—'
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + ' GB'
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}

// ---- 项目文件 (排序浏览 + 资源管理器) ----
const files = ref<any[]>([])
const fileDir = ref('')
const sortBy = ref<'name' | 'mtime' | 'ctime' | 'size'>('mtime')
const sortDesc = ref(true)
const SORT_OPTS = [
  { label: '按修改时间', value: 'mtime' },
  { label: '按创建时间', value: 'ctime' },
  { label: '按文件名', value: 'name' },
  { label: '按大小', value: 'size' },
]
async function loadFiles() {
  try {
    const r = await fetch(`/api/projects/${uid}/files`)
    if (r.ok) { const j = await r.json(); files.value = j.entries ?? []; fileDir.value = j.dir }
  } catch { /* 忽略 */ }
}
onMounted(loadFiles)
const sortedFiles = computed(() => {
  const arr = [...files.value]
  const dir = sortDesc.value ? -1 : 1
  arr.sort((a: any, b: any) => sortBy.value === 'name'
    ? dir * a.file.localeCompare(b.file)
    : dir * ((a[sortBy.value] || 0) - (b[sortBy.value] || 0)))
  return arr
})
function fmtT(ts?: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => n.toString().padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
async function reveal() {
  const r = await fetch(`/api/projects/${uid}/reveal`, { method: 'POST' })
  if (!r.ok) message.error('打开文件管理器失败')
}

// ---- 步骤运行 ----
const busyKey = ref('')
function blockersOf(key: string): any[] {
  const idx = nav.steps.findIndex((s: any) => s.key === key)
  return nav.steps.slice(0, idx < 0 ? 0 : idx)
    .filter((s: any) => !['done', 'stale'].includes(s.status))
}
const artsSize = (s: any) => (s.artifacts ?? []).reduce((a: number, x: any) => a + (x.size || 0), 0)

async function run(step: any, force = false) {
  busyKey.value = step.key
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${step.key}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    })
    const j = await r.json()
    if (!r.ok) { message.error(j.message || '启动失败'); return }
    if (j.status === 'skipped') message.info(j.message)
    else message.success(force ? '已开始强制重跑' : `已启动: ${step.label}`)
    await nav.poll()
  } finally { busyKey.value = '' }
}

// 勾选链: 从该步骤起连续勾选的步骤
function chainFrom(key: string): string[] {
  const set = new Set(nav.autoSteps ?? [...STEP_ORDER])
  const out: string[] = []
  for (const k of STEP_ORDER.slice(STEP_ORDER.indexOf(key as any))) {
    if (!set.has(k)) break
    out.push(k)
  }
  return out
}
async function runChain(step: any) {
  const chain = chainFrom(step.key)
  busyKey.value = step.key
  try {
    const r = await fetch(`/api/projects/${uid}/run_chain`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ steps: chain }),
    })
    const j = await r.json()
    if (!r.ok) { message.error(j.message || '启动失败'); return }
    message.success(`已启动「${step.label}」` +
      (chain.length > 1 ? `，完成后自动执行后续 ${chain.length - 1} 个勾选步骤` : ''))
    await nav.poll()
  } finally { busyKey.value = '' }
}
async function cancel(step: any) {
  await fetch(`/api/projects/${uid}/steps/${step.key}/cancel`, { method: 'POST' })
  message.info('取消信号已发送 (自动续跑将停止)')
}
function rerunDialog(step: any) {
  const downstream = nav.steps.slice(nav.steps.findIndex(s => s.key === step.key) + 1)
    .filter((s: any) => s.status === 'done' || s.status === 'stale')
  dialog.warning({
    title: `强制重跑「${step.label}」`,
    content: downstream.length
      ? `将删除本步产物并重新执行；下游 ${downstream.length} 个步骤将被标记为「已过期」（需要重跑）。`
      : '将删除本步产物并重新执行。',
    positiveText: '强制重跑',
    negativeText: '取消',
    onPositiveClick: () => run(step, true),
  })
}

// 项目动态状态
const headerStatus = computed(() => {
  const run = nav.steps.find((s: any) => s.status === 'running')
  const que = nav.steps.find((s: any) => s.status === 'queued')
  if (run) return { text: run.cancelling ? `正在取消「${run.label}」…`
                                         : `正在「${run.label}」…`, type: 'info' }
  if (que) return { text: `「${que.label}」排队中…`, type: 'warning' }
  const map: Record<string, [string, string]> = {
    done: ['已完成', 'success'], failed: ['有失败步骤', 'error'],
    processing: ['部分完成', 'default'], created: ['待开始', 'default'],
  }
  const [text, type] = map[nav.status] ?? [nav.status, 'default']
  return { text, type }
})
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-space justify="space-between" align="center">
      <n-h1 style="margin: 0; font-size: 20px">{{ nav.title || '…' }}</n-h1>
      <n-tag size="small" :type="headerStatus.type as any">{{ headerStatus.text }}</n-tag>
    </n-space>

    <!-- ============ 原视频 ============ -->
    <n-card size="small" title="原视频" style="margin-top: 16px">
      <template #header-extra>
        <n-space align="center">
          <n-text v-if="media.found && media.external" depth="3" style="font-size: 11.5px">
            外部引用 (零拷贝, 未复制进项目)
          </n-text>
          <n-button size="tiny" @click="reveal">📁 在资源管理器中显示</n-button>
        </n-space>
      </template>
      <n-grid :cols="5" :x-gap="16" v-if="media.found">
        <n-gi :span="3">
          <video v-if="videoSrc" :src="videoSrc" controls preload="metadata"
                 style="width: 100%; max-height: 440px; border-radius: 8px; background: #000" />
        </n-gi>
        <n-gi :span="2">
          <MediaInfo :media="media" />
        </n-gi>
      </n-grid>
      <n-empty v-else description="还没有视频 —— 运行「获取视频」后这里会显示信息与预览"
               size="small" style="margin: 8px 0" />
    </n-card>

    <!-- ============ 步骤清单 (勾选 + 快捷运行) ============ -->
    <n-card size="small" style="margin-top: 14px"
            :title="`步骤进度 (${nav.doneCount}/${nav.steps.length})`">
      <template #header-extra>
        <n-text depth="3" style="font-size: 11.5px">
          ☑ = 完成后允许自动续跑该步；「运行本步及后续」沿勾选链自动推进，遇未勾选即停
        </n-text>
      </template>
      <div class="rows">
        <div v-for="s in nav.steps" :key="s.key" class="row">
          <n-checkbox size="small" :checked="isAuto(s.key)"
                      @update:checked="(v: boolean) => toggleAuto(s.key, v)" />
          <span class="emoji">{{ STATUS_EMOJI[s.status] ?? '⚪' }}</span>
          <span class="lbl">{{ s.label }}</span>
          <n-tag size="small" :type="STATUS_TAG[s.status] || 'default'"
                 :bordered="s.status !== 'pending'">
            {{ STATUS_TEXT[s.status] || s.status }}
          </n-tag>
          <n-tag v-if="nav.chain.includes(s.key)" size="small" type="info"
                 :bordered="false">⛓ 将自动执行</n-tag>
          <n-tag v-if="s.status === 'done' && s.fresh" size="small" type="success"
                 :bordered="false">已是最新</n-tag>
          <span class="info">
            {{ (s.artifacts?.length ?? 0)
              ? `产物 ${s.artifacts.length} 个 · ${fmtSize(artsSize(s))}` : '—' }}
          </span>
          <span class="pbar" v-if="s.status === 'running'">
            <n-progress type="line" :percentage="s.progress || 0" :show-indicator="false"
                        style="width: 100px" />
            <n-text depth="3" style="font-size: 11px">{{ Math.round(s.progress || 0) }}%</n-text>
          </span>
          <span class="err" v-if="s.error" :title="s.error.message">⚠ {{ s.error.message }}</span>
          <span class="ops">
            <n-button v-if="s.status === 'running' || s.status === 'queued'"
                      size="tiny" type="warning" :disabled="s.cancelling"
                      @click="cancel(s)">{{ s.cancelling ? '取消中…' : '取消' }}</n-button>
            <template v-else>
              <n-button size="tiny" type="primary"
                        :disabled="blockersOf(s.key).length > 0"
                        :loading="busyKey === s.key"
                        :title="blockersOf(s.key).length
                          ? `上游未就绪: ${blockersOf(s.key).map(b => b.label).join('、')}` : ''"
                        @click="run(s)">运行该步骤</n-button>
              <n-button size="tiny" :disabled="blockersOf(s.key).length > 0"
                        :title="chainFrom(s.key).length > 1
                          ? `将自动执行: ${chainFrom(s.key).map(k => nav.stepMap[k]?.label ?? k).join(' → ')}` : ''"
                        @click="runChain(s)">▶▶ 本步及后续</n-button>
              <n-button v-if="['done', 'stale', 'failed'].includes(s.status)"
                        size="tiny" quaternary @click="rerunDialog(s)">强制重跑</n-button>
            </template>
          </span>
        </div>
      </div>
      <n-text depth="3" style="font-size: 11.5px; display: block; margin-top: 8px">
        详细日志 / 配置 / 产物预览请从左侧导航进入各步骤页面。
      </n-text>
    </n-card>

    <!-- ============ 项目文件 ============ -->
    <n-card size="small" style="margin-top: 14px" title="项目文件">
      <template #header-extra>
        <n-space align="center">
          <n-select v-model:value="sortBy" :options="SORT_OPTS" size="tiny"
                    style="width: 130px" />
          <n-button size="tiny" @click="sortDesc = !sortDesc">
            {{ sortDesc ? '↓ 降序' : '↑ 升序' }}
          </n-button>
          <n-button size="tiny" quaternary @click="loadFiles">刷新</n-button>
        </n-space>
      </template>
      <n-text depth="3" style="font-size: 11px; display: block; margin-bottom: 6px">
        {{ fileDir }}
      </n-text>
      <n-table size="small" :single-line="false" v-if="sortedFiles.length">
        <thead>
          <tr><th>文件</th><th style="width:90px">大小</th>
              <th style="width:150px">修改时间</th><th style="width:150px">创建时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="f in sortedFiles" :key="f.file">
            <td style="font-size: 12px">{{ f.file }}</td>
            <td style="font-size: 12px">{{ fmtSize(f.size) }}</td>
            <td style="font-size: 12px">{{ fmtT(f.mtime) }}</td>
            <td style="font-size: 12px">{{ fmtT(f.ctime) }}</td>
          </tr>
        </tbody>
      </n-table>
      <n-empty v-else description="(目录为空)" size="small" style="margin: 6px 0" />
    </n-card>
  </n-layout-content>
</template>

<style scoped>
.rows { display: flex; flex-direction: column; }
.row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 4px; border-bottom: 1px dashed rgba(128, 128, 128, 0.18);
}
.row:last-child { border-bottom: none; }
.emoji { width: 20px; text-align: center; }
.lbl { font-size: 13px; font-weight: 600; width: 76px; }
.info { font-size: 11.5px; opacity: 0.65; flex: 1; }
.err {
  font-size: 11.5px; color: #e88080; max-width: 220px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ops { display: flex; gap: 6px; }
.pbar { display: flex; align-items: center; gap: 6px; }
</style>
