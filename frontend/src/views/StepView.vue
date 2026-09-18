<script setup lang="ts">
// 步骤专属页: 说明 / 状态 / 依赖门禁 / 本步配置 / 完整日志 / 产物清单 / 运行操作
// 由左侧流程导航进入, 一页只专注一个步骤; 依赖未就绪时明确指出并禁用运行
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useDialog, useMessage } from 'naive-ui'
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
const canRun = computed(() => !!step.value && !running.value && blockers.value.length === 0)

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
    const j = await r.json()
    if (!r.ok) { message.error(j.message || '启动失败'); return }
    if (j.status === 'skipped') message.info(j.message)
    else message.success(force ? '已开始强制重跑' : '已启动')
    await nav.poll()
    loadLog()
  } finally { busy.value = false }
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

async function cancel() {
  await fetch(`/api/projects/${uid}/steps/${key}/cancel`, { method: 'POST' })
  message.info('取消信号已发送, 等待步骤在检查点退出')
}

// ---- 日志: 运行态 1.5s 轮询并自动滚底 ----
const logLines = ref<string[]>([])
const logBox = ref<HTMLElement | null>(null)
let logTimer: number | undefined

async function loadLog() {
  const r = await fetch(`/api/projects/${uid}/steps/${key}/log?tail=500`)
  const j = await r.json()
  logLines.value = j.lines ?? []
  nextTick(() => {
    if (logBox.value && running.value) logBox.value.scrollTop = logBox.value.scrollHeight
  })
}
function syncLogPolling() {
  const need = running.value
  if (need && !logTimer) {
    loadLog()
    logTimer = window.setInterval(loadLog, 1500)
  } else if (!need && logTimer) {
    clearInterval(logTimer)
    logTimer = undefined
    loadLog()                    // 收尾: 拉最后一次完整日志
  }
}
watch(running, syncLogPolling, { immediate: true })
watch(() => route.params.key, () => {
  logLines.value = []
  if (running.value) syncLogPolling()
})
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

function fmtSize(n: number): string {
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(2) + ' GB'
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + ' MB'
  if (n > 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
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
        </n-space>
        <n-space>
          <n-button v-if="running" type="warning" @click="cancel">⏹ 取消</n-button>
          <n-button v-else type="primary" :loading="busy" :disabled="!canRun"
                    @click="run(false)">▶ 运行</n-button>
          <n-button v-if="['done', 'stale', 'failed'].includes(step.status)"
                    :disabled="running" @click="rerunDialog">🔁 强制重跑</n-button>
        </n-space>
      </n-space>
      <n-text depth="3" style="font-size: 12px">{{ step.description }}</n-text>

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

      <n-grid :cols="2" :x-gap="14" style="margin-top: 14px">
        <!-- 日志 -->
        <n-gi span="2">
          <n-card size="small" title="运行日志">
            <pre ref="logBox" class="logbox">{{ logLines.join('\n') || '(暂无日志)' }}</pre>
          </n-card>
        </n-gi>
        <!-- 本步配置 -->
        <n-gi v-if="cfgRows.length">
          <n-card size="small" title="本步骤当前配置 (只读, 全局修改在设置页)">
            <n-descriptions :column="1" label-placement="left" size="small" bordered>
              <n-descriptions-item v-for="row in cfgRows" :key="row.k" :label="row.k">
                {{ row.v }}
              </n-descriptions-item>
            </n-descriptions>
          </n-card>
        </n-gi>
        <!-- 产物清单 -->
        <n-gi>
          <n-card size="small" title="产物">
            <n-text v-if="!step.artifacts?.length" depth="3" style="font-size: 12px">
              (暂无产物)
            </n-text>
            <ul v-else class="arts">
              <li v-for="a in step.artifacts" :key="a.name">
                <n-text style="font-size: 12.5px">{{ a.name }}</n-text>
                <n-text depth="3" style="font-size: 11.5px; margin-left: 8px">
                  {{ a.file }} · {{ fmtSize(a.size) }}
                </n-text>
              </li>
            </ul>
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
.arts { list-style: none; margin: 0; padding: 0; }
.arts li { padding: 3px 0; }
</style>
