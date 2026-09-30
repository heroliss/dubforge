<script setup lang="ts">
// 跨项目批量恢复失败的自动流程：逐项目读取状态 → 预检（resume_id）→ 一次确认后逐个恢复。
// 复用现有单项目工作流端点，不新增后端面；预检未通过的项目显示原因，不会带病恢复。
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { NAlert, NButton, NDrawer, NDrawerContent } from 'naive-ui'
import { workflowRunStage } from '../utils/workflow'

const props = defineProps<{ projects: { uid: string; title: string }[] }>()
const emit = defineEmits<{ 'resumed': [] }>()
const show = ref(false), busy = ref(false), error = ref('')
const items = ref<any[]>([]), results = ref<any[]>([])
const ready = computed(() => items.value.filter(item => item.run && item.plan && !item.blockers.length))
const title = '批量继续自动流程'
const requestKey = () => (typeof crypto !== 'undefined' && crypto.randomUUID
  ? crypto.randomUUID()
  : `rk-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`)

async function api(uid: string, path: string, body?: any) {
  const r = await fetch(`/api/projects/${encodeURIComponent(uid)}/workflow${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const d = await r.json()
  if (!r.ok) throw new Error(d.message || '读取失败')
  return d
}

async function inspect() {
  if (busy.value) return
  busy.value = true; error.value = ''; items.value = []; results.value = []
  try {
    for (const project of props.projects) {
      try {
        const status = await api(project.uid, '/status')
        const run = status.run
        if (!run || !['paused', 'failed', 'attention'].includes(run.status) || run.publication_id) {
          items.value.push({ uid: project.uid, title: project.title, run: null, plan: null, blockers: [] })
          continue
        }
        const plan = await api(project.uid, '/plan', { mode: run.mode, resume_id: run.id })
        items.value.push({ uid: project.uid, title: project.title, run, plan, blockers: plan.blockers })
      } catch (e: any) {
        items.value.push({ uid: project.uid, title: project.title, run: null, plan: null, blockers: [e.message] })
      }
    }
  } finally { busy.value = false }
}

async function open() {
  if (busy.value || !props.projects.length) return
  show.value = true
  await inspect()
}

async function resumeAll() {
  if (busy.value || !ready.value.length) return
  busy.value = true; error.value = ''; results.value = []
  try {
    for (const item of ready.value) {
      try {
        await api(item.uid, `/${item.run.id}/resume`, {
          recipe: item.plan.recipe, fingerprint: item.plan.fingerprint, confirmed: true,
          request_id: requestKey(),
        })
        results.value.push({ uid: item.uid, title: item.title, error: '' })
      } catch (e: any) {
        results.value.push({ uid: item.uid, title: item.title, error: e.message })
      }
    }
    emit('resumed')
    items.value = []
  } finally { busy.value = false }
}
</script>
<template>
  <n-button size="small" secondary :disabled="!projects.length || busy" @click="open">继续失败的自动流程…</n-button>
  <n-drawer :show="show" width="min(780px,100vw)" :mask-closable="!busy" :close-on-esc="!busy" @update:show="show = $event">
    <n-drawer-content :title="title" :closable="!busy">
      <div class="batch-form">
        <p class="muted">逐个项目检查最近一次自动流程：失败或暂停且尚未投稿的可以继续；从上次中断的步骤接着执行，已完成且未变化的步骤自动跳过。设置已变化的项目会预检失败，需要到项目里重新检查。</p>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <div v-if="!items.length && !busy" class="muted">正在检查…</div>
        <div v-for="item in items" :key="item.uid" class="batch-row">
          <strong>{{ item.title }}</strong>
          <small v-if="item.run">{{ workflowRunStage(item.run).text }}</small>
          <small v-else class="muted">没有可继续的流程</small>
          <p v-if="item.blockers.length" class="blocked">{{ item.blockers.join('；') }}</p>
          <p v-else-if="item.plan">{{ item.plan.steps.map((key: string) => item.plan.labels[key] || key).join(' → ') }}</p>
        </div>
        <div v-for="item in results" :key="item.uid" class="batch-row">
          <RouterLink :to="`/projects/${item.uid}`">{{ item.title }}</RouterLink>
          <p>{{ item.error || '已交给后台继续执行' }}</p>
        </div>
      </div>
      <template #footer>
        <div class="batch-actions">
          <n-button :disabled="busy" :loading="busy" @click="inspect">重新检查</n-button>
          <n-button type="primary" :disabled="busy || !ready.length" @click="resumeAll">确认继续 {{ ready.length }} 个项目</n-button>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>
<style scoped>
.batch-form { display: grid; gap: 20px; padding: 20px 0; }
.batch-form p { font-size: 12px; margin-bottom: 0; }
.muted { color: var(--df-muted); font-size: 12px; }
.batch-row { border-bottom: 1px solid var(--df-border); padding: 12px 0; }
.batch-row small { float: right; color: var(--df-muted); }
.blocked { color: var(--df-warning, #dfb66f); }
.batch-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 12px; margin-top: 12px; }
</style>
