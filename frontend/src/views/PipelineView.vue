<script setup lang="ts">
// 管线页: 九步骤常驻卡 (状态/错误/stale/fresh) + 单步运行/重跑/取消 + 日志尾读
// 核心原则的可视化: 步骤永远可见、可单独重跑; 下游 stale 明确标出
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const uid = route.params.uid as string

const project = ref<any>(null)
const steps = ref<any[]>([])
const selected = ref<string | null>(null)
const logLines = ref<string[]>([])
let timer: number | undefined
const busy = ref(false)

const STATUS_META: Record<string, { tag: 'default' | 'success' | 'error' | 'warning' | 'info', text: string }> = {
  pending: { tag: 'default', text: '待运行' },
  running: { tag: 'info', text: '运行中' },
  done: { tag: 'success', text: '✓ 完成' },
  failed: { tag: 'error', text: '✗ 失败' },
  stale: { tag: 'warning', text: '⟳ 已过期' },
}

const selectedStep = computed(() => steps.value.find(s => s.key === selected.value))

async function load() {
  const [p, s] = await Promise.all([
    fetch(`/api/projects/${uid}`).then(r => r.json()),
    fetch(`/api/projects/${uid}/steps`).then(r => r.json()),
  ])
  project.value = p
  steps.value = s
}
onMounted(async () => {
  await load()
  timer = window.setInterval(load, 2000)   // 运行态 2s 轮询 (WebSocket M6 评估)
})
onUnmounted(() => clearInterval(timer))

function select(key: string) {
  selected.value = selected.value === key ? null : key
  if (selected.value) loadLog()
}

async function loadLog() {
  if (!selected.value) return
  const r = await fetch(`/api/projects/${uid}/steps/${selected.value}/log?tail=300`)
  const j = await r.json()
  logLines.value = j.lines ?? []
}

async function run(step: any, force = false) {
  busy.value = true
  try {
    const r = await fetch(`/api/projects/${uid}/steps/${step.key}/run`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ force }),
    })
    const j = await r.json()
    if (!r.ok) { message.error(j.message || '启动失败'); return }
    if (j.status === 'skipped') message.info(j.message)
    else { message.success(`已启动: ${step.label}`); selected.value = step.key }
    await load()
  } finally { busy.value = false }
}

async function cancel(step: any) {
  await fetch(`/api/projects/${uid}/steps/${step.key}/cancel`, { method: 'POST' })
  message.info('取消信号已发送')
}

function rerunDialog(step: any) {
  const downstream = steps.value.slice(steps.value.indexOf(step) + 1)
    .filter(s => s.status === 'done' || s.status === 'stale')
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

function openWorkbench() {
  router.push('/workbench')
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;" v-if="project">
    <n-space justify="space-between" align="center" style="margin-bottom: 4px">
      <n-h1 style="margin: 0">{{ project.title }}</n-h1>
      <n-button quaternary @click="router.push('/')">← 返回列表</n-button>
    </n-space>
    <n-text depth="3" style="font-size: 12px">
      {{ project.uid }} · 状态:
      <n-tag size="small" style="margin-left: 4px">{{ project.status }}</n-tag>
    </n-text>

    <n-space vertical size="medium" style="margin-top: 16px">
      <n-card v-for="s in steps" :key="s.key" size="small"
              :style="selected === s.key ? 'border-color: var(--accent, #4da3ff)' : ''"
              @click="select(s.key)">
        <n-space justify="space-between" align="center">
          <n-space align="center">
            <n-tag size="small" :type="STATUS_META[s.status]?.tag || 'default'"
                   :bordered="s.status !== 'pending'">
              {{ STATUS_META[s.status]?.text || s.status }}
            </n-tag>
            <n-text strong>{{ s.label }}</n-text>
            <n-text depth="3" style="font-size: 12px">{{ s.description }}</n-text>
            <n-tag v-if="s.status === 'done' && s.fresh" size="small" type="success" bordered={false}>已是最新</n-tag>
          </n-space>
          <n-space @click.stop>
            <n-button v-if="s.status === 'running'" size="tiny" type="warning"
                      @click="cancel(s)">取消</n-button>
            <n-button v-else size="tiny" :loading="busy" @click="run(s)">运行</n-button>
            <n-button v-if="s.status === 'done'" size="tiny" quaternary
                      @click="rerunDialog(s)">强制重跑</n-button>
          </n-space>
        </n-space>

        <!-- 错误详情 -->
        <n-alert v-if="s.error" type="error" :show-icon="false" size="small"
                 style="margin-top: 8px" @click.stop>
          <n-text strong>{{ s.error.message }}</n-text><br />
          <n-text depth="3" style="font-size: 12px">[{{ s.error.code }}] {{ s.error.suggestion }}</n-text>
        </n-alert>

        <!-- 日志 -->
        <div v-if="selected === s.key" style="margin-top: 10px" @click.stop>
          <n-space justify="space-between" align="center" style="margin-bottom: 4px">
            <n-text depth="3" style="font-size: 12px">日志 (最近 300 行)</n-text>
            <n-button size="tiny" quaternary @click="loadLog">刷新日志</n-button>
          </n-space>
          <pre class="logbox">{{ logLines.join('\n') || '(暂无日志)' }}</pre>
        </div>
      </n-card>

      <!-- 工作台入口 -->
      <n-card size="small" style="cursor: pointer" @click="openWorkbench"
              v-if="steps.some(s => s.key === 'dubtask' && s.status === 'done')">
        <n-space justify="space-between" align="center">
          <n-text>🎧 逐句精修工作台 —— 逐句试听/调参/采纳后再跑成片</n-text>
          <n-button size="small">进入</n-button>
        </n-space>
      </n-card>
    </n-space>
  </n-layout-content>
</template>

<style scoped>
.logbox {
  background: #0d1117;
  border: 1px solid #2a343f;
  border-radius: 6px;
  padding: 8px 12px;
  max-height: 320px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
</style>
