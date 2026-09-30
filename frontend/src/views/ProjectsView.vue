<script setup lang="ts">
import ProjectTemplateBinding from '../components/ProjectTemplateBinding.vue'
import BatchWorkflow from '../components/BatchWorkflow.vue'
import BatchWorkflowResume from '../components/BatchWorkflowResume.vue'
import BatchProjectImport from '../components/BatchProjectImport.vue'
import FilePathLink from '../components/FilePathLink.vue'
import AutoRunBadge from '../components/AutoRunBadge.vue'
// 项目列表: 真实 CRUD + 新建 (yt-dlp 支持的任意站点链接 / 本地导入) + 改名
import { computed, h, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NCheckbox, NEmpty, NForm, NFormItem, NH1, NInput,
  NInputGroup, NLayoutContent, NModal, NProgress, NRadio, NRadioGroup, NSelect, NDropdown,
  NSpace, NSpin, NTag, NText, NTooltip, useMessage, useDialog,
} from 'naive-ui'
import { useNavStore, STEP_ORDER, STEP_LABELS, STATUS_TEXT, STATUS_HINTS } from '../stores/nav'
import { fmtSize } from '../utils/format'
import { chainKind, chainSequence, holdDescription, parseChain, type ChainInfo } from '../utils/chainStatus'
import AppIcon from '../components/AppIcon.vue'
import { hiddenSelectionCount, runPlanDescription } from '../utils/actionScope'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()

const projects = ref<any[]>([])
const loading = ref(true)
const loadError = ref('')
let loadSeq = 0
const showNew = ref(false)
const creating = ref(false)
const form = ref({ source_kind: 'link', source_url: '', title: '' })
// 改名
const renaming = ref<any>(null)
const renameTitle = ref('')
const renameSaving = ref(false)

async function load(showSpin = false) {
  const seq = ++loadSeq
  if (showSpin) loading.value = true
  try {
    const r = await fetch('/api/projects', { cache: 'no-store' })
    if (!r.ok) throw new Error(`读取项目失败（HTTP ${r.status}）`)
    const next = await r.json()
    if (seq === loadSeq) {
      projects.value = next
      loadError.value = ''
      sel.value = new Set([...sel.value].filter(uid => next.some((p: any) => p.uid === uid)))
    }
  } catch (err: any) {
    if (seq === loadSeq) loadError.value = err.message || '无法连接服务，请检查连接后重试'
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}
async function refreshList() {
  // 刷新只读取列表；找回目录使用独立的确认入口。
  await load(true)
}
const recovering = ref(false)
function recoverProjects() {
  if (recovering.value) return
  dialog.info({ title: '扫描工作目录并找回项目？',
    content: '读取当前工作目录内的项目记录，找回从回收站还原或手动复制进来的项目。只补缺失登记，不覆盖已有登记，不移动文件，也不运行任何步骤。',
    positiveText: '扫描并找回', negativeText: '取消',
    onPositiveClick: async () => {
      recovering.value = true
      try {
        const response = await fetch('/api/settings/rebuild_db', { method: 'POST' })
        const data = await response.json()
        if (!response.ok) throw new Error(data.message || '找回项目失败')
        message.success(`已找回 ${data.projects ?? 0} 个项目，补充 ${data.steps ?? 0} 条步骤登记`)
        await load(true)
      } catch (error: any) { message.error(error.message || '找回项目失败') }
      finally { recovering.value = false }
    },
  })
}
let listTimer: ReturnType<typeof setInterval> | 0 = 0
let listAlive = false
onMounted(async () => {
  listAlive = true
  await refreshList()
  // 项目列表也要像管线页一样持续取最新步骤状态；否则后台任务运行时，
  // 卡片进度条只会停在进入页面时的快照。
  if (listAlive) listTimer = setInterval(() => load(), 2500)
})
onUnmounted(() => {
  listAlive = false
  if (listTimer) clearInterval(listTimer)
  listTimer = 0
})

// —— 勾选批量运行 / 单项目运行 / 资源管理器 ——
const sel = ref<Set<string>>(new Set())
function toggleSel(uid: string, v: boolean) {
  const s = new Set(sel.value)
  if (v) s.add(uid)
  else s.delete(uid)
  sel.value = s
}
const runningMany = ref(false)
const planningMany = ref(false)
async function confirmRuns(uids: string[]) {
  if (!uids.length || planningMany.value || runningMany.value) return
  planningMany.value = true
  try {
    const plans: Record<string, string[]> = {}
    const summaries = await Promise.all(uids.map(async uid => {
      const project = projects.value.find(p => p.uid === uid)
      const steps = (project?.steps ?? []).filter((s: any) => s.selected).map((s: any) => s.key)
      if (!steps.length) throw new Error(`「${project?.title || uid}」未勾选任何步骤`)
      const response = await fetch(`/api/projects/${uid}/run_plan`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ steps, include_dependencies: false }),
      })
      const plan = await response.json()
      if (!response.ok) throw new Error(plan.message || '读取运行计划失败')
      if (plan.blockers?.length) throw new Error(`「${project?.title || uid}」依赖未就绪；请进入总览核对步骤和依赖`)
      plans[uid] = [...plan.steps]
      return `「${project?.title || uid}」：${runPlanDescription(plan)}`
    }))
    dialog.info({ title: `确认运行 ${uids.length} 个项目`,
      content: () => h('div', { style: 'max-height:55vh;overflow:auto;white-space:pre-wrap;line-height:1.8' },
        summaries.map(summary => h('p', { style: 'margin:0 0 12px' }, summary))),
      positiveText: '按上述计划运行', negativeText: '取消',
      onPositiveClick: () => runMany([...uids], plans),
    })
  } catch (error: any) { message.warning(error.message || '无法核对项目运行范围') }
  finally { planningMany.value = false }
}
async function runMany(uids: string[], plans: Record<string, string[]>) {
  if (!uids.length || runningMany.value) return
  runningMany.value = true
  try {
    const r = await fetch('/api/projects/run_many', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uids, steps_by_uid: plans }),
    })
    const j = await r.json().catch(() => ({}))
    if (!r.ok) { message.error(j.message || '批量启动失败'); return }
    const skipped = (j.results ?? []).filter((x: any) => x.skipped).map((x: any) => x.skipped)
    message.success(`已启动 ${j.started ?? 0} 个项目` +
      (skipped.length ? `；跳过 ${skipped.length} 个 (${skipped[0]}${skipped.length > 1 ? ' 等' : ''})` : ''))
    sel.value = new Set()
    await load()
  } catch { message.error('批量启动失败 (网络异常)') } finally { runningMany.value = false }
}
function runSelected() { void confirmRuns([...sel.value]) }
function runAllPending() {
  // 有失败项目时按钮语义变为「重试失败项目」: 只针对失败的项目重新核对运行;
  // 没有失败项目时保持原行为, 覆盖全部待处理项目。
  const targets = failedInView.value.length ? failedInView.value : pendingInView.value
  void confirmRuns(targets.map((p: any) => p.uid))
}
const pendingInView = computed(() => sortedProjects.value
  .filter((p: any) => p.status !== 'done' && !p.running_step && p.dir_exists !== false))
const failedInView = computed(() => pendingInView.value.filter((p: any) => p.status === 'failed'))
function runProject(p: any) { void confirmRuns([p.uid]) }

// —— 自动续跑链 (ADR-0072): 批量卡片显示计划状态, 挂起可一键继续 ——
const chainMap = computed(() => new Map(sortedProjects.value
  .map((p: any) => [p.uid, parseChain(p.chain ?? p.params?._chain)])))
function chainOf(p: any): ChainInfo | null { return chainMap.value.get(p.uid) ?? null }
function chainSummary(p: any): string {
  const chain = chainOf(p)
  if (!chain) return ''
  if (chain.state === 'held') {
    const head = STEP_LABELS[chain.hold?.step || chain.remaining[0]] || '链头'
    return `⏸ 挂起于「${head}」 · 剩余 ${chain.remaining.length} 步: ${chainSequence(chain, STEP_LABELS)}`
  }
  return `⛓ 续跑中 · 剩余 ${chain.remaining.length} 步: ${chainSequence(chain, STEP_LABELS)}`
}
const heldProjects = computed(() => sortedProjects.value.filter((p: any) => chainOf(p)?.state === 'held' && !p.running_step))
const resumingChain = ref(false)
async function resumeChainQuiet(uid: string): Promise<boolean> {
  const title = projects.value.find((p: any) => p.uid === uid)?.title || uid
  try {
    const r = await fetch(`/api/projects/${uid}/chain/resume`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    })
    const d = await r.json()
    if (!r.ok) throw new Error(d.message || '继续失败')
    return true
  } catch (e: any) {
    message.error(`「${title}」${e.message}`)
    return false
  }
}
async function resumeOneChain(uid: string) {
  if (resumingChain.value) return
  resumingChain.value = true
  try {
    if (await resumeChainQuiet(uid)) { message.success('已继续自动运行'); await load() }
  } finally { resumingChain.value = false }
}
function resumeAllHeld() {
  if (resumingChain.value || !heldProjects.value.length) return
  const targets = [...heldProjects.value]
  dialog.info({
    title: `继续 ${targets.length} 个项目的自动运行？`,
    content: '逐个项目从挂起的链头继续执行；依赖未就绪的项目会再次挂起并提示原因，不会偷跑下游。',
    positiveText: '全部继续', negativeText: '取消',
    onPositiveClick: async () => {
      resumingChain.value = true
      let ok = 0
      try {
        for (const p of targets) { if (await resumeChainQuiet(p.uid)) ok++ }
        message.success(`${ok}/${targets.length} 个项目已继续自动运行`)
        await load()
      } finally { resumingChain.value = false }
    },
  })
}
async function reveal(p: any) {
  try {
    const r = await fetch(`/api/projects/${p.uid}/reveal`, { method: 'POST' })
    if (!r.ok) message.error('打开文件管理器失败')
  } catch { message.error('打开文件管理器失败 (网络异常)') }
}
const STATUS_TAG: Record<string, string> = {
  done: 'success', failed: 'error', processing: 'info', created: 'default',
}
const PROJECT_STATUS_OPTS = [
  { label: '全部状态', value: 'all' },
  { label: '待处理', value: 'created' },
  { label: '进行中', value: 'processing' },
  { label: '已完成', value: 'done' },
  { label: '有失败步骤', value: 'failed' },
]
const searchText = ref('')
const statusFilter = ref('all')
// —— 项目排序 ——
const sortBy = ref<'created' | 'title' | 'progress'>('created')
const sortDesc = ref(true)
const SORT_OPTS = [
  { label: '按创建时间', value: 'created' },
  { label: '按标题', value: 'title' },
  { label: '按完成进度', value: 'progress' },
]
// 项目级状态文案 (步骤级状态用 nav store 的 STATUS_TEXT)
const PROJECT_STATUS_TEXT: Record<string, string> = {
  created: '待处理', processing: '进行中', done: '已完成', failed: '有失败步骤',
}
const requiredSteps = (p: any) => (p.steps ?? []).filter((x: any) => x.selected)
const doneCount = (p: any) =>
  requiredSteps(p).filter((x: any) => x.status === 'done' && x.fresh).length
const orderedProjectSteps = (p: any) => {
  const map = Object.fromEntries((p.steps ?? []).map((x: any) => [x.key, x]))
  return STEP_ORDER.map(k => map[k]).filter(Boolean)
}
const stepPath = (p: any, key: string) =>
  `/projects/${p.uid}/steps/${key}`
const filteredProjects = computed(() => {
  const q = searchText.value.trim().toLocaleLowerCase()
  return projects.value.filter((p: any) => {
    if (statusFilter.value !== 'all' && p.status !== statusFilter.value) return false
    if (!q) return true
    return [p.title, p.uid, p.source_url]
      .filter(Boolean)
      .some((v: any) => String(v).toLocaleLowerCase().includes(q))
  })
})
const sortedProjects = computed(() => {
  const dir = sortDesc.value ? -1 : 1
  const arr = [...filteredProjects.value]
  arr.sort((a: any, b: any) => {
    if (sortBy.value === 'title') return dir * a.title.localeCompare(b.title, 'zh')
    if (sortBy.value === 'progress') return dir * (doneCount(a) - doneCount(b))
    return dir * ((a.created_at || '').localeCompare(b.created_at || ''))
  })
  return arr
})
const statusCounts = computed(() => projects.value.reduce((acc: Record<string, number>, p: any) => {
  acc[p.status] = (acc[p.status] || 0) + 1
  return acc
}, {}))
const hasProjectFilter = computed(() => !!searchText.value.trim() || statusFilter.value !== 'all')
function clearProjectFilter() {
  searchText.value = ''
  statusFilter.value = 'all'
}
const visibleSelected = computed(() => sortedProjects.value.filter(p => sel.value.has(p.uid)).length)
function selectVisible(checked: boolean) {
  const next = new Set(sel.value)
  for (const project of sortedProjects.value) checked ? next.add(project.uid) : next.delete(project.uid)
  sel.value = next
}
const projectMenu = [
  { label: '打开项目文件夹', key: 'reveal' },
  { label: '重命名', key: 'rename' },
  { type: 'divider', key: 'divider' },
  { label: '删除项目…', key: 'delete' },
]
function projectAction(action: string, project: any) {
  if (action === 'reveal') void reveal(project)
  else if (action === 'rename') openRename(project)
  else if (action === 'delete') remove(project)
}

function openRename(p: any) {
  renaming.value = p
  renameTitle.value = p.title
}
async function applyRename() {
  if (!renameTitle.value.trim()) { message.warning('标题不能为空'); return }
  renameSaving.value = true
  try {
    const r = await fetch(`/api/projects/${renaming.value.uid}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: renameTitle.value }),
    })
    const j = await r.json().catch(() => ({}))
    if (r.ok && j.uid) {
      renaming.value.title = j.title
      renaming.value = null
      message.success('已改名')
    } else {
      message.error(j.message || `改名失败 (HTTP ${r.status})`)
    }
  } catch { message.error('改名请求失败 (网络异常)') } finally { renameSaving.value = false }
}

function remove(p: any) {
  const hasRun = !!p.running_step
  dialog.warning({
    title: '删除项目',
    content: hasRun
      ? `「${p.title}」正在运行「${p.running_label}」。删除将先取消该任务并等待退出，然后删除项目并移动文件到回收站 —— 如果系统没开启回收站将永久删除。`
      : `确定删除「${p.title}」？删除项目并移动文件到回收站 —— 如果系统没开启回收站将永久删除。`,
    positiveText: hasRun ? '取消任务并删除' : '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      const url = hasRun ? `/api/projects/${p.uid}?force=true` : `/api/projects/${p.uid}`
      const r = await fetch(url, { method: 'DELETE' })
      if (!r.ok) {
        const j = await r.json().catch(() => ({}))
        message.error(j.message || '删除失败')
        return
      }
      message.success('已删除')
      if (nav.uid === p.uid) nav.reset()
      await load()
    },
  })
}

// —— 本地导入: 拖入 (副本进项目目录) / 浏览 + 粘贴路径 (零拷贝引用) ——
const showBrowse = ref(false)
const browsePath = ref('')
const browseParent = ref<string | null>(null)
const browseEntries = ref<any[]>([])
const browseSel = ref<any>(null)
// 拖入: 浏览器拿不到真实路径, 先持有 File, 创建项目后再把内容流式写进项目目录 media/
const pendingFile = ref<File | null>(null)
const ingesting = ref(false)
const ingestPct = ref(0)
const ingestErr = ref('')
const newUid = ref<string | null>(null)

let browseSeq = 0
async function browseTo(path: string) {
  const seq = ++browseSeq                    // 序号守卫: 快速连点时慢响应不覆盖新路径
  try {
    const r = await fetch(`/api/files/list?path=${encodeURIComponent(path)}`,
      { cache: 'no-store' })
    if (!r.ok) {
      const j = await r.json().catch(() => ({}))
      message.error(j.message || '打开文件夹失败')
      return
    }
    const j = await r.json()
    if (seq !== browseSeq) return            // 已有更新的浏览请求, 丢弃过期结果
    browsePath.value = j.path
    browseParent.value = j.parent
    browseEntries.value = j.entries
    browseSel.value = null
    localStorage.setItem('dubforge.browse', j.path)     // 记住浏览位置
  } catch { message.error('浏览请求失败 (网络异常)') }
}
function openBrowse() {
  showBrowse.value = true
  void browseTo(localStorage.getItem('dubforge.browse') || '')
}
function browseConfirm() {
  if (!browseSel.value) return
  form.value.source_kind = 'local'
  form.value.source_url = browseSel.value.path
  pendingFile.value = null                    // 后选的路径优先于先前拖入的文件
  if (!form.value.title.trim()) {
    form.value.title = browseSel.value.name.replace(/\.[^.]+$/, '')
  }
  showBrowse.value = false
}
function onDrop(e: DragEvent) {
  if (creating.value || ingesting.value || ingestErr.value) return
  const files = e.dataTransfer?.files
  if (!files?.length) { message.warning('未识别到文件'); return }
  if (files.length > 1) message.info('一次只支持一个视频, 已取第一个')
  const f = files[0]
  if (f.size === 0 && f.type === '') {
    message.warning('似乎拖入的是文件夹 —— 请拖入视频文件')
    return
  }
  form.value.source_kind = 'local'
  form.value.source_url = ''                  // 后拖入的文件优先于先前选的路径
  pendingFile.value = f
  if (!form.value.title.trim()) form.value.title = f.name.replace(/\.[^.]+$/, '')
}

async function create() {
  if (creating.value || ingesting.value) return
  if (form.value.source_kind === 'local' && pendingFile.value) {
    await createThenIngest()
    return
  }
  if (!form.value.source_url.trim()) { message.warning('请填写视频链接或本地路径'); return }
  creating.value = true
  try {
    const r = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form.value),
    })
    const j = await r.json()
    if (j.uid) {
      showNew.value = false
      message.success('项目已创建')
      router.push(`/projects/${j.uid}`)
    } else {
      message.error(j.message || '创建失败')
    }
  } catch { message.error('创建失败，请检查服务连接后重试') } finally {
    creating.value = false
  }
}

// 拖入流程: 先建项目 (项目目录即容器), 再把文件内容流式写进 media/ —— 不经收件箱
async function createThenIngest() {
  creating.value = true
  try {
    const r = await fetch('/api/projects', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form.value, source_url: '' }),
    })
    const j = await r.json()
    if (!j.uid) { message.error(j.message || '创建失败'); return }
    newUid.value = j.uid
    await runIngest()
  } catch { message.error('创建失败，请检查服务连接后重试') } finally { creating.value = false }
}

async function runIngest() {
  const f = pendingFile.value
  if (!newUid.value || !f) return
  ingestErr.value = ''
  ingesting.value = true
  ingestPct.value = 0
  try {
    await ingest(newUid.value, f)
    showNew.value = false
    message.success('项目已创建, 视频副本已存入项目目录')
    router.push(`/projects/${newUid.value}`)
  } catch (e: any) {
    ingestErr.value = e.message || '导入失败'
  } finally { ingesting.value = false }
}

// 用 XHR 而非 fetch: 需要 upload.onprogress 显示复制进度
function ingest(uid: string, f: File): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `/api/projects/${uid}/ingest?filename=${encodeURIComponent(f.name)}`)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) ingestPct.value = Math.round((e.loaded / e.total) * 100)
    }
    xhr.onload = () => {
      if (xhr.status < 300) { resolve(); return }
      let msg = `HTTP ${xhr.status}`
      try { msg = JSON.parse(xhr.responseText).message || msg } catch { /* 原样 */ }
      reject(new Error(msg))
    }
    xhr.onerror = () => reject(new Error('网络错误 —— 请确认后端正在运行'))
    xhr.send(f)
  })
}

// 导入失败时项目刚建、什么都没有: 重试, 或删掉空项目重来
async function abortIngest() {
  const uid = newUid.value
  resetNewForm()
  showNew.value = false
  if (uid) {
    await fetch(`/api/projects/${uid}`, { method: 'DELETE' })
    message.info('已放弃导入, 空项目已删除')
  }
}
function resetNewForm() {
  pendingFile.value = null
  newUid.value = null
  ingestErr.value = ''
  ingestPct.value = 0
  form.value = { source_kind: 'link', source_url: '', title: '' }
}
</script>

<template>
  <div class="workspace-page projects-page">
    <div class="page-head">
      <div>
        <span class="page-kicker">创作 / 管理 / 交付</span>
        <n-h1 style="margin: 0">项目空间</n-h1>
        <n-text depth="3" class="page-subtitle">
          从一段视频开始，让每一种声音被听见。管理译制进度，继续你的创作。
        </n-text>
      </div>
      <n-space><BatchProjectImport @created="load()"/><n-button type="primary" @click="showNew = true"><template #icon><AppIcon name="plus" /></template>新建项目</n-button></n-space>
    </div>
    <div v-if="projects.length" class="summary-strip">
      <button v-for="item in [{label:'全部项目', value:'all', count:projects.length}, {label:'进行中', value:'processing', count:statusCounts.processing || 0}, {label:'已完成', value:'done', count:statusCounts.done || 0}, {label:'需关注', value:'failed', count:statusCounts.failed || 0}]"
              :key="item.value" class="summary-item" :class="`summary-${item.value}`" :aria-pressed="statusFilter === item.value" @click="statusFilter = item.value">
        <span>{{ item.label }}</span><strong>{{ item.count.toString().padStart(2, '0') }}</strong>
        <span class="summary-caption">{{ item.value === 'all' ? '你的全部译制项目' : item.value === 'processing' ? '处理进度持续更新' : item.value === 'done' ? '所选步骤已完成' : '查看失败步骤与日志' }}</span>
      </button>
    </div>
    <div class="toolbar filter-toolbar">
        <n-input v-model:value="searchText" size="small" clearable
                 placeholder="搜索项目名称或视频来源" aria-label="搜索项目" class="project-search"><template #prefix><AppIcon name="search" :size="16" /></template></n-input>
        <n-select v-model:value="statusFilter" :options="PROJECT_STATUS_OPTS" size="small"
                  aria-label="筛选项目状态" style="width: 124px" />
        <n-select v-model:value="sortBy" :options="SORT_OPTS" size="small"
                  aria-label="项目排序方式" style="width: 130px" />
        <n-button size="small" @click="sortDesc = !sortDesc">
          {{ sortDesc ? '↓ 降序' : '↑ 升序' }}
        </n-button>
        <n-button size="small" quaternary title="只重新读取项目状态，不扫描目录或启动任务"
                  :loading="loading" @click="refreshList">刷新列表</n-button>
        <n-button size="small" quaternary :loading="recovering" @click="recoverProjects">找回项目…</n-button>
        <n-button v-if="hasProjectFilter" size="small" quaternary @click="clearProjectFilter">清除筛选</n-button>
      </div>
    <div v-if="sortedProjects.length || sel.size" class="selection-bar">
      <n-checkbox :disabled="!sortedProjects.length" :checked="sortedProjects.length > 0 && visibleSelected === sortedProjects.length" :indeterminate="visibleSelected > 0 && visibleSelected < sortedProjects.length" @update:checked="selectVisible">选择当前列表</n-checkbox>
      <span class="list-count">{{ sel.size ? `已选 ${sel.size} 个项目` : `共 ${sortedProjects.length} 个项目` }}</span>
      <n-text v-if="hiddenSelectionCount([...sel], sortedProjects.map(p => p.uid))" type="warning" style="font-size:12px">含 {{ hiddenSelectionCount([...sel], sortedProjects.map(p => p.uid)) }} 个被筛选隐藏的项目</n-text>
      <n-button v-if="sel.size" size="small" quaternary @click="sel = new Set()">取消选择</n-button>
      <n-button v-if="heldProjects.length" size="small" secondary type="primary" :loading="resumingChain"
                title="把列表中所有挂起的自动运行计划从链头继续执行"
                @click="resumeAllHeld">继续全部已挂起（{{ heldProjects.length }}）…</n-button>
      <BatchWorkflowResume v-if="projects.length" :projects="sortedProjects.map(p => ({ uid: p.uid, title: p.title }))" @resumed="load()" />
      <BatchWorkflow v-if="sel.size" :uids="[...sel]" @started="refreshList" />
      <n-button v-if="sel.size" size="small" :loading="runningMany || planningMany" @click="runSelected">仅运行制作步骤（{{ sel.size }}）…</n-button>
      <n-button v-else-if="pendingInView.length" size="small" secondary :loading="runningMany || planningMany" @click="runAllPending">{{ failedInView.length ? `重试失败项目（${failedInView.length}）…` : '核对并运行当前列表待处理项目…' }}</n-button>
    </div>
    <n-alert v-if="loadError" type="error" class="load-error">{{ loadError }}<template #action><n-button size="small" @click="load(true)">重试</n-button></template></n-alert>
    <ProjectTemplateBinding v-if="sel.size" :uids="[...sel]" @changed="refreshList" />
    <n-spin :show="loading">
      <n-empty v-if="!loading && !loadError && !projects.length"
               description="还没有项目 —— 新建后可粘贴视频链接或导入本地视频">
        <template #extra>
          <n-button type="primary" size="small" @click="showNew = true">＋ 新建项目</n-button>
        </template>
      </n-empty>
      <n-empty v-else-if="!loading && !loadError && !sortedProjects.length"
               description="没有符合当前筛选条件的项目">
        <template #extra>
          <n-button size="small" @click="clearProjectFilter">清除筛选</n-button>
        </template>
      </n-empty>
      <div v-else class="project-list">
        <n-card v-for="p in sortedProjects" :key="p.uid" size="small"
                class="project-card" :class="{ selected: sel.has(p.uid) }">
          <div class="project-head">
            <div class="project-main">
              <n-checkbox size="small" :checked="sel.has(p.uid)"
                          :aria-label="`选择项目 ${p.title}`"
                          @update:checked="(v: boolean) => toggleSel(p.uid, v)" />
              <div class="project-title-block">
                <div class="title-line">
                  <router-link :to="`/projects/${p.uid}`" class="project-title">{{ p.title }}</router-link>
                  <n-tag size="small" :type="(STATUS_TAG as any)[p.status] || 'default'">
                    {{ PROJECT_STATUS_TEXT[p.status] || p.status }}
                  </n-tag>
                  <AutoRunBadge :workflow-status="p.workflow_status" :chain="chainOf(p)" :running-label="p.running_label" />
                  <n-tooltip v-if="p.dir_exists === false" trigger="hover" :delay="0"
                             placement="top">
                    <template #trigger>
                      <n-tag size="small" type="error" :bordered="false">⚠ 目录缺失</n-tag>
                    </template>
                    项目目录不在磁盘上 —— 可能被移动或删除。从回收站还原后此标记会自动消失;
                    确认不再需要可点「删除」清理登记。
                  </n-tooltip>
                </div>
                <n-text depth="3" class="project-source" :title="p.source_kind === 'local' ? '本地导入' : p.source_url">
                  {{ p.source_kind === 'local' ? '本地导入' : p.source_url }} · {{ p.uid }}
                </n-text>
              </div>
            </div>
            <div class="project-actions">
              <n-button size="small" secondary type="primary" @click.stop="router.push(`/projects/${p.uid}`)">打开项目 →</n-button>
              <n-button size="small" :disabled="!!p.running_step || p.dir_exists === false" :loading="runningMany || planningMany" @click.stop="runProject(p)">{{ p.running_step ? '运行中' : '核对运行步骤…' }}</n-button>
              <n-dropdown :options="projectMenu" trigger="click" @select="(action: string) => projectAction(action, p)"><n-button size="small" quaternary :aria-label="`${p.title}的更多操作`">更多 ···</n-button></n-dropdown>
            </div>
          </div>
          <!-- 分段进度条: 每段 = 一个步骤, 悬停看详情, 点击进步骤页 -->
          <div class="segwrap" v-if="(p.steps || []).length">
            <div class="segbar">
              <n-tooltip v-for="st in orderedProjectSteps(p)" :key="st.key" trigger="hover"
                         :delay="0" :duration="0" placement="top">
                <template #trigger>
                  <button type="button" class="seg" :class="'seg-' + (st.status === 'done' && !st.fresh ? 'stale' : st.status)" :aria-label="`${st.label}：${st.status === 'done' && !st.fresh ? '需更新' : STATUS_TEXT[st.status] || st.status}，进入步骤`"
                       @click.stop="router.push(stepPath(p, st.key))">
                    <div class="fill" v-if="st.status === 'running'"
                         :style="{ width: (st.progress || 0) + '%' }" />
                  </button>
                </template>
                {{ st.label }} · {{ st.status === 'done' && !st.fresh ? '需更新' : STATUS_TEXT[st.status] || st.status
                }}{{ st.status === 'running' ? (st.progress_indeterminate ? ` · ${st.progress_phase}` : ` ${st.progress_estimated ? '预计 ' : ''}${Math.round(st.progress || 0)}%`) : ''
                }}{{ st.error ? ` — ${st.error}` : STATUS_HINTS[st.status]
                ? ` — ${STATUS_HINTS[st.status]}` : '' }}
                <n-text depth="3" style="font-size: 11px">(点击进入步骤页)</n-text>
              </n-tooltip>
            </div>
            <n-text depth="3" style="font-size: 11px">
              已完成 {{ doneCount(p) }}/{{ requiredSteps(p).length }} 个所选步骤
              <span v-if="p.running_label"> · 正在「{{ p.running_label }}」…</span>
            </n-text>
            <div v-if="chainOf(p)" class="chain-line">
              <span :class="chainOf(p)!.state === 'held' ? 'chain-held' : 'chain-active'"
                    :title="chainOf(p)!.state === 'held' ? holdDescription(chainOf(p)!, STEP_LABELS) : '本步完成后自动继续剩余计划'">{{ chainSummary(p) }}</span>
              <n-button v-if="chainOf(p)!.state === 'held'" size="tiny" type="primary" secondary
                        :loading="resumingChain" :disabled="!!p.running_step || p.dir_exists === false"
                        @click.stop="resumeOneChain(p.uid)">重试并继续</n-button>
            </div>
          </div>
        </n-card>
      </div>
    </n-spin>

    <n-modal v-model:show="showNew" preset="card" title="新建项目"
             :closable="!creating && !ingesting && !ingestErr" :mask-closable="!creating && !ingesting && !ingestErr" :close-on-esc="!creating && !ingesting && !ingestErr"
             style="width: min(560px, calc(100vw - 32px)); max-width: calc(100vw - 32px)"
             @after-leave="resetNewForm">
      <div @dragover.prevent @drop.prevent="onDrop">
        <n-form class="new-project-form" label-placement="top" :disabled="creating || ingesting || !!ingestErr">
          <n-form-item label="来源类型">
            <n-radio-group v-model:value="form.source_kind">
              <n-radio value="link">视频链接 (YouTube / B站 等网站)</n-radio>
              <n-radio value="local">本地视频导入</n-radio>
            </n-radio-group>
          </n-form-item>
          <n-form-item :label="form.source_kind === 'local' ? '本地路径' : '视频链接'">
            <n-input-group>
              <n-input v-model:value="form.source_url"
                       :placeholder="form.source_kind === 'local'
                         ? 'D:\\videos\\my.mp4 · 或点右侧「📂 选择文件」'
                         : '支持 YouTube / Bilibili / Twitter 等数千个站点 (由 yt-dlp 支持)'" />
              <n-button v-if="form.source_kind === 'local'" @click="openBrowse">
                📂 选择文件
              </n-button>
            </n-input-group>
          </n-form-item>
          <n-form-item label="标题 (可选)">
            <n-input v-model:value="form.title"
                     placeholder="留空自动获取: 优先用视频标题, 其次 文件名/站点+ID" />
          </n-form-item>
        </n-form>
        <div v-if="ingesting" style="margin-top: 10px">
          <n-progress type="line" :percentage="ingestPct" indicator-placement="inside" processing />
          <n-text depth="3" style="font-size:12px; display:block; margin-top:4px">
            {{ ingestPct >= 100 ? '视频已传送，正在等待项目文件校验与登记' : '正在把视频副本传送到项目目录' }}<template v-if="pendingFile"> · {{ pendingFile.name }} · {{ fmtSize(pendingFile.size) }}</template>
          </n-text>
        </div>
        <n-text v-else depth="3" style="font-size:12px; display:block; line-height:1.8">
          <template v-if="pendingFile">
            已拖入: {{ pendingFile.name }} ({{ fmtSize(pendingFile.size) }}) —— 创建项目时复制一份进项目目录 media\, 随项目存亡
          </template>
          <template v-else>
            提示: 把视频文件<b>拖进本窗口</b> —— 浏览器不提供原文件路径, 内容会作为副本存进项目目录 (随项目存亡)。<br />
            点「📂 选择文件」或<b>直接粘贴路径</b> —— 零拷贝引用: 只登记原位置, 不复制、不改不动原文件, 大文件推荐这种方式。
          </template>
        </n-text>
        <n-alert v-if="ingestErr" type="error" closable style="margin-top: 8px"
                 @close="ingestErr = ''">
          视频导入失败: {{ ingestErr }}
        </n-alert>
      </div>
      <template #footer>
        <n-space v-if="ingestErr" justify="end">
          <n-button @click="abortIngest">放弃并删除空项目</n-button>
          <n-button type="primary" :loading="ingesting" @click="runIngest">重试导入</n-button>
        </n-space>
        <n-space v-else justify="end">
          <n-button :disabled="creating || ingesting" @click="showNew = false">取消</n-button>
          <n-button type="primary" :loading="creating || ingesting" :disabled="!pendingFile && !form.source_url.trim()" @click="create">创建项目</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal v-model:show="showBrowse" preset="card" title="选择视频文件"
             style="width: min(680px, calc(100vw - 32px)); max-width: calc(100vw - 32px)">
      <div class="browse-toolbar" style="display:flex;gap:8px;margin-bottom:8px">
        <n-input v-model:value="browsePath" size="small" placeholder="输入文件夹路径后回车"
                 @keyup.enter="browseTo(browsePath)" />
        <n-button size="small" @click="browseTo(browsePath)">跳转</n-button>
        <FilePathLink v-if="browsePath" :path="browsePath" label="资源管理器" />
        <n-button size="small" :disabled="!browseParent" @click="browseTo(browseParent || '')">
          上级
        </n-button>
      </div>
      <div class="browse-list">
        <div v-for="e in browseEntries" :key="e.path" class="browse-item"
             :class="{ sel: browseSel?.path === e.path }"
             @click="e.is_dir ? browseTo(e.path) : (browseSel = e)"
             @dblclick="!e.is_dir && browseConfirm()">
          <span>{{ e.is_dir ? '📁' : (e.video ? '🎬' : '📄') }}</span>
          <span style="flex:1;margin-left:8px;overflow:hidden;text-overflow:ellipsis">
            {{ e.name }}
          </span>
          <n-text v-if="!e.is_dir" depth="3" style="font-size:11px">
            {{ fmtSize(e.size) }}
          </n-text>
          <FilePathLink :path="e.path" label="定位" />
        </div>
        <n-empty v-if="!browseEntries.length" size="small" description="(空文件夹)" />
      </div>
      <template #footer>
        <n-space justify="space-between" align="center">
          <n-text depth="3" style="font-size:12px">
            {{ browseSel ? `已选: ${browseSel.name}` : '单击进入文件夹 / 选中文件, 双击文件直接确定' }}
          </n-text>
          <n-space>
            <n-button @click="showBrowse = false">取消</n-button>
            <n-button type="primary" :disabled="!browseSel" @click="browseConfirm">确定</n-button>
          </n-space>
        </n-space>
      </template>
    </n-modal>

    <n-modal :show="!!renaming" preset="card" title="重命名项目"
             style="width: min(460px, calc(100vw - 32px)); max-width: calc(100vw - 32px)"
             @update:show="v => !v && (renaming = null)">
      <n-input v-model:value="renameTitle" maxlength="200" show-count
               placeholder="输入新的标题" @keyup.enter="applyRename" />
      <template #footer>
        <n-space justify="end">
          <n-button @click="renaming = null">取消</n-button>
          <n-button type="primary" :loading="renameSaving" @click="applyRename">应用</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.load-error { margin-bottom: 16px; }
.selection-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 18px 0 14px; }
.selection-bar > .n-button:last-child { margin-left: auto; }
.list-count { font-size: 12px; color: var(--df-muted); }
.filter-toolbar { padding: 14px; background: var(--df-panel); border: 1px solid var(--df-border); border-radius: 12px; justify-content: flex-start; }
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 14px;
}
.page-subtitle { display: block; margin-top: 8px; font-size: 13px; line-height: 1.8; }
.toolbar {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 8px;
}
.project-search { width: 280px; flex: 1; min-width: 180px; }
.summary-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin: 26px 0 24px;
}
.summary-item { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; border: 1px solid var(--df-border); border-radius: 12px; background: var(--df-panel); padding: 18px 22px; text-align: left; color: var(--df-muted); cursor: pointer; }
.summary-item:hover, .summary-item[aria-pressed=true] { border-color: #597ca8; background: #1d2837; }
.summary-item strong { font-size: 30px; font-weight: 600; line-height: 1.2; color: var(--df-text); font-variant-numeric: tabular-nums; }
.summary-done strong { color: #78c9a5; }
.summary-processing strong { color: var(--df-accent); }
.summary-failed strong { color: #efbd70; }
.summary-caption { font-size: 11px; color: #8392a6; }
.info-number { color: #63b3ed; }
.success-number { color: #63d391; }
.error-number { color: #f08080; }
.result-count { margin-left: auto; font-size: 12px; }
.project-list { display: flex; flex-direction: column; gap: 12px; }
.project-card { transition: border-color .18s ease, background .18s ease; }
.project-card:hover { border-color: #4c5c71; }
.project-card.selected { border-color: #83b5ff; background: #1b2736; }
.project-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.project-main { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.project-title-block { min-width: 0; }
.title-line { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.project-title { max-width: min(50vw, 640px); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 16px; font-weight: 600; text-decoration: none; color: var(--df-text); }
.project-title:hover { color: var(--df-accent); }
.project-source {
  display: block;
  max-width: min(58vw, 760px);
  margin-top: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.project-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.browse-list {
  height: 340px;
  overflow-y: auto;
  border: 1px solid #2a343f;
  border-radius: 6px;
}
.browse-item {
  display: flex;
  align-items: center;
  padding: 6px 10px;
  cursor: pointer;
  gap: 4px;
}
.browse-item:hover {
  background: rgba(255, 255, 255, 0.06);
}
.browse-item.sel {
  background: rgba(24, 160, 88, 0.25);
}
.segwrap { margin-top: 18px; }
.chain-line { display: flex; align-items: center; gap: 10px; margin-top: 6px; font-size: 11px; flex-wrap: wrap; }
.chain-line .chain-held { color: #efbd70; }
.chain-line .chain-active { color: var(--df-accent, #6aa2ff); }
.segbar { display: flex; gap: 3px; }
.seg {
  flex: 1; height: 12px; border-radius: 4px; border: 0; padding: 0;
  background: rgba(128, 128, 128, 0.25);
  overflow: hidden; cursor: pointer; position: relative;
}
.seg:hover { outline: 1px solid rgba(255, 255, 255, 0.4); }
.seg-done { background: #2ea44f; }
.seg-failed, .seg-interrupted { background: #e5534b; }
.seg-stale { background: #d29922; }
.seg-skipped { background: #667085; }
.seg-running { background: rgba(77, 163, 255, 0.25); }
.seg-running .fill { height: 100%; background: #4da3ff; }
@media (max-width: 900px) {
  .page-head { flex-direction: row; flex-wrap: wrap; }
  .toolbar { justify-content: flex-start; width: 100%; }
  .project-search { width: min(100%, 280px); }
  .summary-strip { flex-wrap: wrap; gap: 12px 20px; }
  .result-count { width: 100%; margin-left: 0; }
  .project-head { flex-direction: column; }
  .project-actions { justify-content: flex-start; padding-left: 28px; }
  .project-title, .project-source { max-width: 100%; }
  .project-main { width: 100%; }
  .summary-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 600px) {
  .summary-strip { align-items: flex-start; }
  .summary-item { padding: 14px; }
  .summary-caption { display: none; }
  .selection-bar > .n-button:last-child { margin-left: 0; }
  .browse-toolbar { flex-wrap: wrap; }
  .browse-toolbar :deep(.n-input) { flex: 1 1 100%; }
  :deep(.new-project-form .n-form-item) { display: block; }
  :deep(.new-project-form .n-form-item-label) { width: 100% !important; padding-bottom: 4px; }
  :deep(.new-project-form .n-form-item-blank) { width: 100%; }
}
</style>
