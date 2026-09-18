<script setup lang="ts">
// 总览: 项目信息 + 原视频 (信息+播放预览) + 各步骤关键信息与快捷运行。
// 只负责"看"与快捷操作, 不放步骤页跳转入口 (左侧导航已承担); 运行/日志/配置在步骤专属页。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
import { useNavStore, STATUS_TAG, STATUS_TEXT, STATUS_EMOJI } from '../stores/nav'

const route = useRoute()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()
const uid = route.params.uid as string

// ---- 原视频信息 (ffprobe, 随 download 完成后刷新) ----
const media = ref<any>({ found: false })
async function loadMedia() {
  try {
    const r = await fetch(`/api/projects/${uid}/media`)
    if (r.ok) media.value = await r.json()
  } catch { /* 忽略, 显示未就绪 */ }
}
onMounted(loadMedia)
watch(() => nav.stepMap['download']?.status, (s) => { if (s === 'done') loadMedia() })

function fmtSize(n?: number): string {
  if (n == null) return '—'
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + ' GB'
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}
function fmtDur(s?: number): string {
  if (!s) return '—'
  const m = Math.floor(s / 60), sec = Math.round(s % 60)
  return `${m}分${sec.toString().padStart(2, '0')}秒`
}

// ---- 步骤关键信息 + 快捷运行 ----
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
async function cancel(step: any) {
  await fetch(`/api/projects/${uid}/steps/${step.key}/cancel`, { method: 'POST' })
  message.info('取消信号已发送')
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

const videoSrc = computed(() =>
  media.value.found ? `/api/projects/${uid}/workbench/video` : '')

// 项目动态状态: 「翻译」运行中… / 正在取消「翻译」… / 「X」排队中…
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
        <n-text v-if="media.found && media.external" depth="3" style="font-size: 11.5px">
          外部引用 (零拷贝, 未复制进项目)
        </n-text>
      </template>
      <n-grid :cols="5" :x-gap="16" v-if="media.found">
        <n-gi :span="3">
          <video v-if="videoSrc" :src="videoSrc" controls preload="metadata"
                 style="width: 100%; max-height: 440px; border-radius: 8px; background: #000" />
        </n-gi>
        <n-gi :span="2">
          <n-descriptions :column="1" label-placement="left" size="small" bordered>
            <n-descriptions-item label="文件名">{{ media.name }}</n-descriptions-item>
            <n-descriptions-item label="大小">{{ fmtSize(media.size) }}</n-descriptions-item>
            <n-descriptions-item label="时长">{{ fmtDur(media.duration) }}</n-descriptions-item>
            <n-descriptions-item label="分辨率">
              <template v-if="media.width">{{ media.width }} × {{ media.height }}</template>
              <template v-else>—</template>
            </n-descriptions-item>
            <n-descriptions-item label="封装格式">{{ media.container || '—' }}</n-descriptions-item>
            <n-descriptions-item label="视频编码">
              {{ media.vcodec || '—' }}
              <n-text v-if="media.fps" depth="3" style="font-size: 11px"> @{{ media.fps }}</n-text>
            </n-descriptions-item>
            <n-descriptions-item label="音频编码">
              {{ media.acodec || '—' }}
              <n-text v-if="media.sample_rate" depth="3" style="font-size: 11px">
                {{ (media.sample_rate / 1000).toFixed(1) }}kHz
              </n-text>
            </n-descriptions-item>
          </n-descriptions>
        </n-gi>
      </n-grid>
      <n-empty v-else description="还没有视频 —— 运行「获取视频」后这里会显示信息与预览"
               size="small" style="margin: 8px 0" />
    </n-card>

    <!-- ============ 各步骤关键信息 + 快捷运行 ============ -->
    <n-card size="small" style="margin-top: 14px"
            :title="`步骤进度 (${nav.doneCount}/${nav.steps.length})`">
      <div class="rows">
        <div v-for="s in nav.steps" :key="s.key" class="row">
          <span class="emoji">{{ STATUS_EMOJI[s.status] ?? '⚪' }}</span>
          <span class="lbl">{{ s.label }}</span>
          <n-tag size="small" :type="STATUS_TAG[s.status] || 'default'"
                 :bordered="s.status !== 'pending'">
            {{ STATUS_TEXT[s.status] || s.status }}
          </n-tag>
          <n-tag v-if="s.status === 'done' && s.fresh" size="small" type="success"
                 :bordered="false">已是最新</n-tag>
          <span class="info">
            {{ (s.artifacts?.length ?? 0)
              ? `产物 ${s.artifacts.length} 个 · ${fmtSize(artsSize(s))}` : '—' }}
          </span>
          <span class="pbar" v-if="s.status === 'running'">
            <n-progress type="line" :percentage="s.progress || 0" :show-indicator="false"
                        style="width: 110px" />
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
                        @click="run(s)">运行</n-button>
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
  </n-layout-content>
</template>

<style scoped>
.rows { display: flex; flex-direction: column; }
.row {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 4px; border-bottom: 1px dashed rgba(128, 128, 128, 0.18);
}
.row:last-child { border-bottom: none; }
.emoji { width: 20px; text-align: center; }
.lbl { font-size: 13px; font-weight: 600; width: 84px; }
.info { font-size: 11.5px; opacity: 0.65; flex: 1; }
.err {
  font-size: 11.5px; color: #e88080; max-width: 260px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.ops { display: flex; gap: 6px; }
.pbar { display: flex; align-items: center; gap: 6px; }
</style>
