<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { NAlert, NButton, NCheckbox, NDrawer, NDrawerContent, NInput, NProgress, NSelect, NTag } from 'naive-ui'
import ConfigurationSourceBadge from './ConfigurationSourceBadge.vue'

const emit = defineEmits<{ created: [uids: string[]]; selectCreated: [uids: string[]] }>()
const show = ref(false), text = ref(''), defaultTemplate = ref<string | null>(null)
const rowTemplates = ref<Record<string, string | null>>({})
const catalog = ref<any>(null), plan = ref<any>(null), result = ref<any>(null)
const recentImports = ref<any[]>([]), historyError = ref(''), historyLoading = ref(false)
const selected = ref<number[]>([]), busy = ref(false), loading = ref(false), inspecting = ref(false)
const error = ref(''), note = ref(''), filename = ref(''), request = ref<any>(null)
let generation = 0, disposed = false, pollTimer: ReturnType<typeof setInterval> | undefined
let statusEpoch = 0, statusSequence = 0
const RECOVERY_KEY = 'dubforge.project-import.pending'
const options = computed(() => [{label:'全局默认（不绑定模板）',value:''}, ...(catalog.value?.templates || [])
  .filter((t:any) => !t.archived).map((t:any) => ({label:t.name,value:t.key}))])
const ready = computed(() => (plan.value?.items || []).filter((r:any) => r.selectable))
const progress = computed(() => result.value?.items?.length ? Math.round(100 * result.value.items.filter((r:any) => r.status !== 'pending').length / result.value.items.length) : 0)
const createdUids = computed(() => (result.value?.items || []).filter((r:any) => r.status === 'created').map((r:any) => r.uid))
const canRestart = computed(() => !busy.value && !result.value?.running && !!result.value && result.value.items.every((r:any) => r.status !== 'pending'))
const canResume = computed(() => !busy.value && !result.value?.running && !result.value?.complete)
const sample = computed(() => catalog.value ? {...catalog.value.sample,
  defaults:{...catalog.value.sample.defaults,template:defaultTemplate.value || null}} : null)
const input = () => ({text:text.value, default_template:defaultTemplate.value, template_overrides:{...rowTemplates.value}})

async function api(path:string, body?:any) {
  let response:Response
  try { response = await fetch('/api/project-import' + path, {method:body ? 'POST' : 'GET',
    headers:body ? {'Content-Type':'application/json'} : undefined, body:body ? JSON.stringify(body) : undefined, cache:'no-store'}) }
  catch { throw new Error('无法连接后端服务，请确认服务已启动后重试') }
  let data:any
  try { data = await response.json() } catch { throw new Error(`导入服务响应无法解析（HTTP ${response.status}），请查询结果后重试`) }
  if (!response.ok) throw Object.assign(new Error(data.message || `导入请求失败（HTTP ${response.status}）`), {code:data.code})
  return data
}
function forgetRecovery() { try { sessionStorage.removeItem(RECOVERY_KEY) } catch {} }
function remember() { try { sessionStorage.setItem(RECOVERY_KEY, JSON.stringify(request.value)) } catch {} }
async function refreshHistory() {
  historyLoading.value=true; historyError.value=''
  try { const data=await api('/history'); if (!disposed) recentImports.value=data.items }
  catch { if (!disposed) historyError.value='最近导入读取失败，可点击刷新重试' }
  finally { historyLoading.value=false }
}
function validRecovery(value:any) {
  return value && typeof value.request_id==='string' && /^[0-9a-f-]{36}$/i.test(value.request_id)
    && Array.isArray(value.selected) && value.selected.length > 0 && value.selected.every((i:any)=>Number.isInteger(i) && i>=0)
    && (value.resume===true || typeof value.input?.text==='string')
}
async function open() {
  if (loading.value) return
  show.value = true; error.value = ''; loading.value = true
  try {
    catalog.value = await api('/catalog')
    if (!request.value) {
      try {
        const raw = sessionStorage.getItem(RECOVERY_KEY)
        if (raw) { const data=JSON.parse(raw); if (validRecovery(data)) request.value=data; else forgetRecovery() }
      } catch { forgetRecovery() }
      if (request.value?.input) {
        text.value = request.value.input.text; defaultTemplate.value = request.value.input.default_template
        rowTemplates.value = request.value.input.template_overrides || {}
      }
    }
    await Promise.all([refreshHistory(), request.value?.request_id ? checkStatus() : Promise.resolve()])
  } catch (e:any) { error.value = e.message || '导入选项读取失败' }
  finally { loading.value = false }
}
async function restoreBatch(id:string) {
  if (busy.value || loading.value) return
  if (request.value && !canRestart.value) { await checkStatus(); return }
  loading.value=true;error.value='';statusEpoch++
  try {
    const data=await api('/'+id)
    if (disposed) return
    request.value={request_id:id,selected:data.items.map((r:any)=>r.index),resume:true}
    applyStatus(data);if (!data.complete) remember();plan.value=null;selected.value=[]
  } catch (e:any) { error.value=e.message }
  finally { loading.value=false }
}
function batchDate(value:string) { return value ? new Date(value).toLocaleString('zh-CN') : '较早的导入' }
function invalidate() { generation++; inspecting.value = false; plan.value = null; selected.value = []; error.value = ''; note.value = '' }
function changeText(value:string) {
  if (busy.value || request.value) return
  text.value = value; rowTemplates.value = {}; filename.value = ''; invalidate()
}
function changeDefault(value:string | null) {
  if (busy.value || request.value) return
  defaultTemplate.value = value || null; invalidate()
}
async function inspect() {
  if (busy.value || request.value || !text.value.trim()) return
  const ticket = ++generation, payload = input()
  inspecting.value = true; plan.value = null; selected.value = []; error.value = ''
  try {
    const data = await api('/preview', payload)
    if (disposed || ticket !== generation) return
    plan.value = data; selected.value = [...data.selected]
  } catch (e:any) { if (ticket === generation && !disposed) error.value = e.message }
  finally { if (ticket === generation) inspecting.value = false }
}
async function changeRow(index:number, value:string) {
  if (busy.value || request.value) return
  const preserve = [...selected.value]
  if (value === '__input__') delete rowTemplates.value[String(index)]
  else rowTemplates.value[String(index)] = value || null
  const pending = inspect(), ticket = generation
  await pending
  if (ticket === generation && plan.value) selected.value = preserve.filter(i => plan.value.items[i]?.selectable)
}
function toggle(index:number, value:boolean) {
  if (busy.value || request.value) return
  selected.value = value ? [...new Set([...selected.value,index])] : selected.value.filter(i => i !== index)
}
async function readFile(file:File) {
  if (busy.value || request.value) return
  if (!/\.(json|txt)$/i.test(file.name)) { error.value = '请选择 UTF-8 编码的 .json 或 .txt 清单文件'; return }
  if (file.size > (catalog.value?.max_bytes || 1048576)) { error.value = '清单文件不能超过1MB'; return }
  invalidate()
  const ticket = generation
  try {
    const content = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer())
    if (ticket !== generation || disposed) return
    changeText(content); filename.value = file.name
    await inspect()
  } catch { if (ticket === generation) error.value = '文件读取失败；请将清单保存为 UTF-8 编码后重试' }
}
function chooseFile(event:Event) {
  const element = event.target as HTMLInputElement, file = element.files?.[0]
  if (file) void readFile(file)
  element.value = ''
}
function drop(event:DragEvent) {
  const files = event.dataTransfer?.files
  if (!files?.length) return
  if (files.length !== 1) { error.value = '一次请选择一个清单文件；多个视频地址写在同一个清单里'; return }
  void readFile(files[0])
}
async function copySample() {
  try { await navigator.clipboard.writeText(JSON.stringify(sample.value,null,2)); note.value = 'JSON 样板已复制' }
  catch { error.value = '复制失败，请使用“下载样板”' }
}
function download(name:string, value:any) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'}))
  const anchor = document.createElement('a'); anchor.href=url; anchor.download=name; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
function downloadGuide() {
  const guide={...catalog.value,sample:sample.value,examples:{two_parts:{...catalog.value.examples.two_parts,
    defaults:{...catalog.value.examples.two_parts.defaults,template:defaultTemplate.value || null}}}}
  download('DubForge-批量导入-LLM说明.json', guide)
}
function syncPolling() {
  const active=show.value && !disposed && (busy.value || result.value?.running)
  if (active && !pollTimer) {
    let checking=false
    pollTimer=setInterval(()=>{if (!checking) {checking=true;void checkStatus().finally(()=>{checking=false})}},1000)
  } else if (!active) {clearInterval(pollTimer);pollTimer=undefined}
}
function applyStatus(data:any) {
  const previous = createdUids.value.join(',')
  result.value = data
  if (data.complete) forgetRecovery()
  if (createdUids.value.join(',') !== previous) emit('created', createdUids.value)
  syncPolling()
}
async function checkStatus() {
  if (!request.value?.request_id) return
  const id=request.value.request_id, epoch=statusEpoch, sequence=++statusSequence
  const current=()=>!disposed && request.value?.request_id===id && epoch===statusEpoch && sequence===statusSequence
  try { const data=await api('/'+id); if (current()) applyStatus(data) }
  catch (e:any) {
    if (!busy.value && current()) {
      if (e.code==='PROJECT_IMPORT_NOT_FOUND' && request.value.resume) {
        request.value=null;result.value=null;forgetRecovery();invalidate()
        error.value='此导入回执已不存在，请先核对项目列表，再导入新清单。';void refreshHistory()
      } else error.value = e.message + '；可用原批次安全重试，不会重复创建。'
    }
  }
}
async function create() {
  if (busy.value || loading.value || inspecting.value || result.value?.running || (!request.value && (!plan.value || !selected.value.length))) return
  if (!request.value) {
    request.value = {input:input(), selected:[...selected.value], fingerprint:plan.value.fingerprint,
      request_id:crypto.randomUUID(), confirmed:true}
    remember()
  }
  busy.value = true; error.value = ''; statusEpoch++
  syncPolling()
  try {
    const data = request.value.resume ? await api('/'+request.value.request_id+'/resume',{confirmed:true}) : await api('/create', request.value)
    statusEpoch++ // 已完成的响应优先于尚未返回的轮询。
    if (!disposed) applyStatus(data)
  } catch (e:any) {
    statusEpoch++
    if (!disposed) {
      if (['PROJECT_IMPORT_INVALID','PROJECT_IMPORT_CONFLICT'].includes(e.code)) {
        request.value=null; result.value=null; forgetRecovery(); invalidate(); error.value=e.message
      } else {
        error.value = e.message + '；保留了批次标识，请查询结果或安全重试。'; await checkStatus()
      }
    }
  } finally { busy.value=false; syncPolling(); void refreshHistory() }
}
function restart() {
  if (!canRestart.value) return
  statusEpoch++
  request.value=null; result.value=null; forgetRecovery(); invalidate()
}
function selectCreated() {
  if (busy.value || !createdUids.value.length) return
  emit('selectCreated',[...createdUids.value]);show.value=false
}
function retryFailedAsNew() {
  const rows=(result.value?.items || []).filter((r:any) => r.status==='error')
  if (!rows.length || !canRestart.value) return
  // 回执保留稀疏参数，重新打开页面后也能只调整失败项。
  const projects=rows.map((r:any) => ({source:r.source,title:r.title,template:r.template || null,settings:r.settings}))
  restart(); changeText(JSON.stringify({schema:'dubforge.batch.v1',projects},null,2)); void inspect()
}
watch(show, value => { if (!value) { generation++; inspecting.value=false } syncPolling() })
onUnmounted(() => {disposed=true;generation++;statusEpoch++;clearInterval(pollTimer)})
</script>

<template>
  <n-button @click="open">批量导入</n-button>
  <n-drawer v-model:show="show" :width="1040" :auto-focus="false" :mask-closable="!busy" :close-on-esc="!busy" style="max-width:100vw">
    <n-drawer-content title="批量创建项目" :closable="!busy" body-content-style="padding:20px 24px">
      <div class="import-intro"><span>粘贴多行视频链接 / 本地路径，或导入 JSON 清单。</span><n-tag size="small" :bordered="false">只创建 · 不运行</n-tag></div>
      <n-alert v-if="error" type="error" class="notice">{{ error }}</n-alert>
      <n-alert v-if="note" type="success" class="notice" closable @close="note=''">{{ note }}</n-alert>
      <details v-if="!request && (recentImports.length || historyError)" class="import-history" :open="recentImports.some(r=>!r.complete)">
        <summary>最近导入 <small>重启后也可在这里继续</small></summary>
        <div class="history-toolbar"><span>{{ historyError }}</span><n-button size="tiny" :loading="historyLoading" @click="refreshHistory">刷新</n-button></div>
        <div class="history-list"><div v-for="batch in recentImports" :key="batch.request_id" class="history-row">
          <div><strong>{{ batch.title }}</strong><small>{{ batchDate(batch.created_at) }} · {{ batch.created }}/{{ batch.total }} 个已创建<span v-if="batch.failed"> · {{ batch.failed }} 个失败</span></small></div>
          <n-button size="small" :disabled="loading" @click="restoreBatch(batch.request_id)">{{ batch.running?'查看进度':batch.complete?'查看结果':'恢复本批' }}</n-button>
        </div></div>
      </details>
      <details v-if="!request" class="import-input" :open="!plan" @dragover.prevent @drop.prevent="drop">
        <summary :hidden="!plan" class="input-summary">输入清单 <small>{{ filename || '已解析，可展开修改' }}</small></summary>
        <div class="input-toolbar">
          <label class="template-default"><span>未指定时使用</span><n-select :value="defaultTemplate || ''" :options="options" :loading="loading" :disabled="busy" filterable aria-label="批量导入默认模板" @update:value="changeDefault" /></label>
          <label class="file-button">读取清单文件<input type="file" accept=".json,.txt,application/json,text/plain" :disabled="busy" @change="chooseFile" /></label>
        </div>
        <n-input :value="text" type="textarea" :autosize="{minRows:7,maxRows:14}" :disabled="busy" aria-label="批量视频地址或JSON清单" placeholder="每行一个视频链接或本地绝对路径；也可粘贴 JSON。支持拖入 .txt / .json 清单文件。" @update:value="changeText" />
        <div class="input-footer"><small>{{ filename || '最多100项 · 本地视频只引用，不复制原文件' }}</small><n-button type="primary" :loading="inspecting" :disabled="loading || !text.trim()" @click="inspect">解析并预览</n-button></div>
        <details v-if="catalog" class="format-help"><summary>JSON 样板与 LLM 生成说明</summary><p>逐项模板优先于批次模板；明确填写的参数作为项目覆盖，其余保持继承。样板使用上方选定的模板。</p><div class="help-actions"><n-button size="small" @click="copySample">复制样板</n-button><n-button size="small" @click="download('DubForge-批量导入样板.json',sample)">下载样板</n-button><n-button size="small" @click="downloadGuide">下载 LLM 说明与可用参数</n-button></div><pre>{{ JSON.stringify(sample,null,2) }}</pre></details>
      </details>
      <div v-if="plan && !request" class="import-preview">
        <div class="preview-heading"><strong>创建预览 <small>{{ plan.total }} 项</small></strong><span><n-checkbox :checked="selected.length===ready.length && !!ready.length" :indeterminate="!!selected.length && selected.length<ready.length" @update:checked="selected=$event?ready.map((r:any)=>r.index):[]">选择全部有效项</n-checkbox></span></div>
        <article v-for="row in plan.items" :key="row.index" class="import-row" :class="{invalid:row.errors.length}">
          <n-checkbox :checked="selected.includes(row.index)" :disabled="!row.selectable" :aria-label="`选择第${row.index+1}个项目`" @update:checked="toggle(row.index,$event)" />
          <div class="row-content"><div class="row-heading"><span class="row-number">{{ row.index+1 }}</span><strong>{{ row.title || '无法解析的条目' }}</strong><ConfigurationSourceBadge v-if="row.template_name" :source="row.template?{kind:'series',name:row.template_name}:{kind:'global'}" /><small v-if="row.template">{{ row.template_name }}</small></div><div class="source-text">{{ row.source }}</div><div v-for="problem in row.errors" :key="problem" class="row-error">{{ problem }}</div><div v-for="warning in row.warnings" :key="warning" class="row-warning">{{ warning }}</div><RouterLink v-for="previous in row.duplicates" :key="previous.uid" :to="`/projects/${previous.uid}`" @click="show=false">查看已有：{{ previous.title }}</RouterLink><details v-if="Object.keys(row.settings).length" class="row-settings"><summary><ConfigurationSourceBadge :source="{kind:'project'}"/> {{ Object.keys(row.settings).length }} 项覆盖</summary><pre>{{ JSON.stringify(row.settings,null,2) }}</pre></details></div>
          <n-select class="row-template" :value="rowTemplates[String(row.index)] === undefined ? '__input__' : (rowTemplates[String(row.index)] || '')" :options="[{label:'按清单 / 默认模板',value:'__input__'},...options]" :disabled="inspecting" filterable :aria-label="`第${row.index+1}项模板`" @update:value="changeRow(row.index,$event)" />
        </article>
      </div>
      <div v-if="request" class="import-results">
        <div class="preview-heading"><strong>{{ busy || result?.running?'正在创建项目':result?.complete?'创建完成':'核对本批结果' }}</strong><small>{{ result?.created || 0 }} / {{ request.selected.length }} 个已创建</small></div>
        <n-progress type="line" :percentage="progress" :processing="busy || !!result?.running" :show-indicator="false" />
        <p class="result-note">{{ busy || result?.running?'正在登记项目、绑定模板和保存项目参数…':'已创建的项目不会因重试重复创建；需要运行时，在项目列表中勾选后批量运行。' }}</p>
        <article v-for="row in result?.items || []" :key="row.index" class="result-row"><span>{{ row.index+1 }}</span><RouterLink v-if="row.status==='created'" :to="`/projects/${row.uid}`" @click="show=false">{{ row.title }}</RouterLink><strong v-else>{{ row.title }}</strong><n-tag size="small" :type="row.status==='created'?'success':row.status==='error'?'error':'default'">{{ {created:'已创建',pending:'待创建',error:'未创建',removed:'后来已删除'}[row.status as string] }}</n-tag><small v-if="row.error" class="row-warning">{{ row.error }}</small></article>
      </div>
      <template #footer>
        <div class="import-actions">
          <span>{{ request?'不会启动下载、LLM 或投稿':`已选 ${selected.length} 项，确认后创建项目` }}</span>
          <template v-if="!request">
            <n-button @click="show=false">取消</n-button>
            <n-button type="primary" :disabled="!plan || !selected.length || inspecting" @click="create">确认创建 {{ selected.length }} 个项目</n-button>
          </template>
          <template v-else>
            <n-button v-if="!result?.complete" :disabled="busy" @click="checkStatus">查询结果</n-button>
            <n-button v-if="!result?.complete" :disabled="!canResume" :loading="busy" type="primary" @click="create">{{ busy || result?.running?'创建中…':request.resume?'继续未完成项目':'安全重试本批' }}</n-button>
            <n-button v-if="canRestart && !result?.complete" @click="retryFailedAsNew">调整未完成条目</n-button>
            <n-button :disabled="!canRestart" @click="restart">导入另一批</n-button>
            <n-button v-if="createdUids.length" :disabled="busy" @click="selectCreated">选中已创建项目</n-button>
            <n-button :disabled="busy" @click="show=false">{{ result?.complete?'完成':'关闭（保留批次）' }}</n-button>
          </template>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.history-list{max-height:260px;overflow-y:auto}.input-summary{font-size:12px;padding:10px 0;border-bottom:1px solid var(--df-border)}.input-summary small{color:var(--df-muted);margin-left:10px}.import-input[open]>.input-summary{margin-bottom:12px}
.import-history{margin-bottom:18px;border:1px solid var(--df-border);border-radius:8px;padding:12px;font-size:12px}.import-history summary small,.history-row small{color:var(--df-muted);font-size:11px}.import-history summary small{margin-left:8px}.history-toolbar{display:flex;justify-content:space-between;align-items:center;margin-top:8px;color:var(--df-warning,#e8b86d)}.history-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--df-border)}.history-row:last-child{border-bottom:0}.history-row>div{min-width:0}.history-row strong,.history-row small{display:block;overflow-wrap:anywhere}.row-heading small{color:var(--df-muted);font-size:11px}
.import-intro,.input-toolbar,.input-footer,.preview-heading,.import-actions{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}.import-intro{color:var(--df-muted);font-size:12px;margin-bottom:18px}.notice{margin:12px 0}.input-toolbar{margin-bottom:12px}.template-default{display:flex;align-items:center;gap:10px;font-size:12px}.template-default :deep(.n-select){width:260px}.file-button{position:relative;overflow:hidden;border:1px solid var(--df-border);padding:7px 12px;border-radius:6px;font-size:12px;cursor:pointer}.file-button input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%}.input-footer{margin-top:10px}.input-footer small,.preview-heading small,.result-note,.import-actions>span{color:var(--df-muted);font-size:12px}.format-help{margin-top:18px;padding:12px 0;border-top:1px solid var(--df-border);font-size:12px;color:var(--df-muted)}summary{cursor:pointer}.format-help p{line-height:1.7}.help-actions{display:flex;gap:8px;flex-wrap:wrap}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;background:var(--df-accent-soft);padding:12px;border-radius:6px;color:var(--df-text)}.import-preview,.import-results{margin-top:20px}.preview-heading{margin-bottom:12px}.import-row{display:flex;gap:12px;align-items:flex-start;padding:15px 0;border-top:1px solid var(--df-border)}.row-content{min-width:0;flex:1}.row-heading{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:13px}.row-number{color:var(--df-muted);font-size:11px}.source-text{font-size:11px;color:var(--df-muted);overflow-wrap:anywhere;margin-top:5px}.row-template{width:210px;flex:none}.row-warning,.row-error{font-size:11px;margin-top:6px;line-height:1.6}.row-warning{color:var(--df-warning,#e8b86d)}.row-error{color:var(--df-danger,#f08d8d)}.row-content a{font-size:11px;display:block;margin-top:5px}.row-settings{font-size:11px;margin-top:8px}.row-settings summary{display:flex;align-items:center;gap:6px}.result-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 0;border-bottom:1px solid var(--df-border);font-size:12px}.result-row a,.result-row strong{flex:1}.result-row small{flex-basis:100%}.import-actions{width:100%;justify-content:flex-end}.import-actions>span{margin-right:auto}@media(max-width:700px){.import-row{flex-wrap:wrap}.row-template{width:100%;margin-left:26px}.template-default{width:100%}.template-default :deep(.n-select){flex:1;width:auto}.import-actions>span{width:100%}}
</style>
