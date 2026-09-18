<script setup lang="ts">
// 项目列表: 真实 CRUD + 新建 (yt-dlp 支持的任意站点链接 / 本地导入) + 改名
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage, useDialog } from 'naive-ui'
import { useNavStore, STATUS_TEXT } from '../stores/nav'

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const nav = useNavStore()

const projects = ref<any[]>([])
const loading = ref(true)
const showNew = ref(false)
const creating = ref(false)
const form = ref({ source_kind: 'link', source_url: '', title: '' })
// 改名
const renaming = ref<any>(null)
const renameTitle = ref('')
const renameSaving = ref(false)

let pollTimer: number | undefined
async function load(showSpin = false) {
  if (showSpin) loading.value = true
  try {
    const r = await fetch('/api/projects')
    projects.value = await r.json()
  } finally {
    loading.value = false
  }
}
async function refreshList() {
  // 对账一次 (回收站还原/手动拷入的目录自动重新登记) 再重新拉列表
  try { await fetch('/api/settings/rebuild_db', { method: 'POST' }) } catch { /* 忽略 */ }
  await load(true)
}
onMounted(refreshList)
onUnmounted(() => pollTimer && clearInterval(pollTimer))

// —— 勾选批量运行 / 单项目运行 / 资源管理器 ——
const sel = ref<Set<string>>(new Set())
function toggleSel(uid: string, v: boolean) {
  const s = new Set(sel.value)
  if (v) s.add(uid)
  else s.delete(uid)
  sel.value = s
}
async function runMany(uids: string[]) {
  if (!uids.length) return
  const r = await fetch('/api/projects/run_many', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ uids }),
  })
  const j = await r.json()
  if (!r.ok) { message.error(j.message || '批量启动失败'); return }
  const skipped = j.results.filter((x: any) => x.skipped).map((x: any) => x.skipped)
  message.success(`已启动 ${j.started} 个项目` +
    (skipped.length ? `；跳过 ${skipped.length} 个 (${skipped[0]}${skipped.length > 1 ? ' 等' : ''})` : ''))
  sel.value = new Set()
  await load()
}
function runSelected() { runMany([...sel.value]) }
function runProject(p: any) { runMany([p.uid]) }
async function reveal(p: any) {
  const r = await fetch(`/api/projects/${p.uid}/reveal`, { method: 'POST' })
  if (!r.ok) message.error('打开文件管理器失败')
}
const STATUS_TAG: Record<string, string> = {
  done: 'success', failed: 'error', processing: 'info', created: 'default',
}
// —— 项目排序 ——
const sortBy = ref<'created' | 'title' | 'progress'>('created')
const sortDesc = ref(true)
const SORT_OPTS = [
  { label: '按创建时间', value: 'created' },
  { label: '按标题', value: 'title' },
  { label: '按完成进度', value: 'progress' },
]
const sortedProjects = computed(() => {
  const dir = sortDesc.value ? -1 : 1
  const arr = [...projects.value]
  arr.sort((a: any, b: any) => {
    if (sortBy.value === 'title') return dir * a.title.localeCompare(b.title, 'zh')
    if (sortBy.value === 'progress') return dir * (doneCount(a) - doneCount(b))
    return dir * ((a.created_at || '').localeCompare(b.created_at || ''))
  })
  return arr
})
const doneCount = (p: any) =>
  (p.steps ?? []).filter((x: any) => x.status === 'done').length
// 项目级状态文案 (步骤级状态用 nav store 的 STATUS_TEXT)
const PROJECT_STATUS_TEXT: Record<string, string> = {
  created: '待处理', processing: '进行中', done: '已完成', failed: '有失败步骤',
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
    const j = await r.json()
    if (j.uid) {
      renaming.value.title = j.title
      renaming.value = null
      message.success('已改名')
    } else {
      message.error(j.message || '改名失败')
    }
  } finally { renameSaving.value = false }
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

async function browseTo(path: string) {
  const r = await fetch(`/api/files/list?path=${encodeURIComponent(path)}`)
  const j = await r.json()
  if (!r.ok) { message.error(j.message || '打开文件夹失败'); return }
  browsePath.value = j.path
  browseParent.value = j.parent
  browseEntries.value = j.entries
  browseSel.value = null
  localStorage.setItem('dubforge.browse', j.path)     // 记住浏览位置
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
function fmtSize(n: number) {
  if (!n) return ''
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`
}
function onDrop(e: DragEvent) {
  const files = e.dataTransfer?.files
  if (!files?.length) { message.warning('未识别到文件'); return }
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
  } finally {
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
  } finally { creating.value = false }
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
  <n-layout-content content-style="padding: 24px;">
    <n-space justify="space-between" align="center" style="margin-bottom: 16px">
      <n-h1 style="margin: 0">项目</n-h1>
      <n-space>
        <n-select v-model:value="sortBy" :options="SORT_OPTS" size="small"
                  style="width: 130px" />
        <n-button size="small" @click="sortDesc = !sortDesc">
          {{ sortDesc ? '↓ 降序' : '↑ 升序' }}
        </n-button>
        <n-button v-if="sel.size" type="primary" @click="runSelected">
          ▶▶ 运行选中 ({{ sel.size }})
        </n-button>
        <n-button size="small" quaternary title="重新对账并刷新列表 (回收站还原的项目会自动找回)"
                  @click="refreshList">🔄 刷新</n-button>
        <n-button type="primary" @click="showNew = true">＋ 新建项目</n-button>
      </n-space>
    </n-space>

    <n-spin :show="loading">
      <n-empty v-if="!loading && !projects.length"
               description="还没有项目 —— 点击右上角新建，粘贴视频链接 (YouTube / B站 等网站) 或导入本地视频" />
      <div v-else style="display:flex;flex-direction:column;gap:16px">
        <n-card v-for="p in sortedProjects" :key="p.uid" size="small">
          <n-space justify="space-between" align="center">
            <div style="display:flex;align-items:center;gap:12px">
              <n-checkbox size="small" :checked="sel.has(p.uid)"
                          @update:checked="(v: boolean) => toggleSel(p.uid, v)" />
              <div>
                <n-space align="center">
                  <n-text strong style="font-size: 15px">{{ p.title }}</n-text>
                  <n-tag size="small" :type="(STATUS_TAG as any)[p.status] || 'default'">
                    {{ PROJECT_STATUS_TEXT[p.status] || p.status }}
                  </n-tag>
                  <n-tag v-if="p.running_step" size="small" type="info" :bordered="false">
                    🔄 {{ p.running_label }}
                  </n-tag>
                  <n-tooltip v-if="p.dir_exists === false" trigger="hover" :delay="0"
                             placement="top">
                    <template #trigger>
                      <n-tag size="small" type="error" :bordered="false">⚠ 目录缺失</n-tag>
                    </template>
                    项目目录不在磁盘上 —— 可能被移动或删除。从回收站还原后此标记会自动消失;
                    确认不再需要可点「删除」清理登记。
                  </n-tooltip>
                </n-space>
                <n-text depth="3" style="font-size: 12px; display: block; margin-top: 4px">
                  {{ p.source_kind === 'local' ? '本地导入' : p.source_url }} · {{ p.uid }}
                </n-text>
              </div>
            </div>
            <n-space>
              <n-button size="small" type="primary" @click.stop="router.push(`/projects/${p.uid}`)">打开项目</n-button>
              <n-button size="small" @click.stop="runProject(p)">▶ 运行项目</n-button>
              <n-button size="small" quaternary @click.stop="reveal(p)">📁</n-button>
              <n-button size="small" quaternary @click.stop="openRename(p)">✏️ 改名</n-button>
              <n-tooltip trigger="hover" :delay="200" placement="top">
                <template #trigger>
                  <n-button size="small" quaternary type="error" @click.stop="remove(p)">删除</n-button>
                </template>
                删除项目并移动文件到回收站；如果系统没开启回收站将永久删除
              </n-tooltip>
            </n-space>
          </n-space>
          <!-- 分段进度条: 每段 = 一个步骤, 悬停看详情, 点击进步骤页 -->
          <div class="segwrap" v-if="(p.steps || []).length">
            <div class="segbar">
              <n-tooltip v-for="st in p.steps" :key="st.key" trigger="hover"
                         :delay="0" :duration="0" placement="top">
                <template #trigger>
                  <div class="seg" :class="'seg-' + st.status"
                       @click.stop="router.push(`/projects/${p.uid}/steps/${st.key}`)">
                    <div class="fill" v-if="st.status === 'running'"
                         :style="{ width: (st.progress || 0) + '%' }" />
                  </div>
                </template>
                {{ st.label }} · {{ STATUS_TEXT[st.status] || st.status
                }}{{ st.status === 'running' ? ` ${Math.round(st.progress || 0)}%` : ''
                }}{{ st.error ? ` — ${st.error}` : '' }}
                <n-text depth="3" style="font-size: 11px">(点击进入步骤页)</n-text>
              </n-tooltip>
            </div>
            <n-text depth="3" style="font-size: 11px">
              已完成 {{ doneCount(p) }}/{{ (p.steps || []).length }}
              <span v-if="p.running_label"> · 正在「{{ p.running_label }}」…</span>
            </n-text>
          </div>
        </n-card>
      </div>
    </n-spin>

    <n-modal v-model:show="showNew" preset="card" title="新建项目" style="width: 560px"
             @after-leave="resetNewForm">
      <div @dragover.prevent @drop.prevent="onDrop">
        <n-form label-placement="left" label-width="90">
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
            ⬇️ 正在把视频副本写入项目目录… (服务端即本机, 本地磁盘速度)
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
          <n-button @click="showNew = false">取消</n-button>
          <n-button type="primary" :loading="creating || ingesting" @click="create">创建</n-button>
        </n-space>
      </template>
    </n-modal>

    <n-modal :show="showBrowse" preset="card" title="选择视频文件" style="width: 680px">
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <n-input v-model:value="browsePath" size="small" placeholder="输入文件夹路径后回车"
                 @keyup.enter="browseTo(browsePath)" />
        <n-button size="small" @click="browseTo(browsePath)">跳转</n-button>
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

    <n-modal :show="!!renaming" preset="card" title="重命名项目" style="width: 460px">
      <n-input v-model:value="renameTitle" maxlength="200" show-count
               placeholder="输入新的标题" @keyup.enter="applyRename" />
      <template #footer>
        <n-space justify="end">
          <n-button @click="renaming = null">取消</n-button>
          <n-button type="primary" :loading="renameSaving" @click="applyRename">应用</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-layout-content>
</template>

<style scoped>
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
.segwrap { margin-top: 10px; }
.segbar { display: flex; gap: 3px; }
.seg {
  flex: 1; height: 8px; border-radius: 3px;
  background: rgba(128, 128, 128, 0.25);
  overflow: hidden; cursor: pointer; position: relative;
}
.seg:hover { outline: 1px solid rgba(255, 255, 255, 0.4); }
.seg-done { background: #2ea44f; }
.seg-failed, .seg-interrupted { background: #e5534b; }
.seg-stale { background: #d29922; }
.seg-running { background: rgba(77, 163, 255, 0.25); }
.seg-running .fill { height: 100%; background: #4da3ff; }
</style>
