<script setup lang="ts">
// 批次协调在后端；页面只读预检并明确确认，不在轮询里启动下一项。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { NAlert, NButton, NCheckbox, NDrawer, NDrawerContent, NSelect, NSpin, NTag } from 'naive-ui'
import { usePublishMetadata } from '../composables/usePublishMetadata'
import { batchStage, chronologicalItems, moveReleaseItem, releaseScopeKey, releasePublicationAction, releaseActionLabel, releaseVersionSelection, releaseBatchControls, releaseItemStage, type ReleaseItem, type ReleasePublicationAction } from '../utils/releaseBatch'
import { versionSummary } from '../utils/assemblyVersions'
import { PUBLICATION_STATUS, publicationTargetDisplay } from '../utils/publishing'
import { STEP_LABELS } from '../stores/nav'
import PublicationTargetSummary from './PublicationTargetSummary.vue'
import PublicationRightsHint from './PublicationRightsHint.vue'
import ReleaseProjectPlan from './ReleaseProjectPlan.vue'
import ReleaseCurrentProgress from './ReleaseCurrentProgress.vue'
import DubbingRunOptions from './DubbingRunOptions.vue'
import { defaultDubbingRunPolicy, dubbingRunPolicy, dubbingPolicyKey, dubbingPolicyError, type DubbingRunPolicy } from '../utils/dubbingRunPolicy'

const props = withDefaults(defineProps<{ excludeIds?: string[] }>(), { excludeIds: () => [] })
const emit = defineEmits<{ changed: []; started: [] }>()
const route = useRoute()
const batches = ref<any[]>([]), listError = ref(''), listLoading = ref(false)
// 顺序发布批次是编排记录，不是一个项目。已完成批次保留供审计，默认收起，
// 让项目列表先突出仍需要用户处理的批次，避免完成批次看起来像一个“大项目”。
const showCompletedBatches = ref(false)
const standaloneBatches = computed(() => batches.value.filter(batch => !props.excludeIds.includes(batch.id) && !batch.plan?.parent_batch_id && !batch.parent_batch_id))
const visibleBatches = computed(() => showCompletedBatches.value
  ? standaloneBatches.value
  : standaloneBatches.value.filter(batch => batch.status !== 'done'))
const completedBatchCount = computed(() => standaloneBatches.value.filter(batch => batch.status === 'done').length)
const show = ref(false), loading = ref(false), writing = ref(false), error = ref('')
const targetDriven = ref(true)
const items = ref<ReleaseItem[]>([]), catalog = ref<any>({ accounts: [] })
const catalogLoaded = ref(false)
const account = ref(''), season = ref<number|null>(null), section = ref<number|null>(null)
const orderConfirmed = ref(false), rightsConfirmed = ref(false), preview = ref<any>(null)
const selectionExpanded=ref(true)
const existing = ref<any>(null), inspectedScope = ref('')
const dubbing = ref(defaultDubbingRunPolicy())
const scopeDisplay = ref<{season_title?: string; section_title?: string}>({})
const publications = ref<Record<string,string>>({})
const versions = ref<Record<string,string[]>>({})
const publicationActions = computed(() => Object.fromEntries(items.value.map(item => [item.project_uid, releasePublicationAction(item, publications.value[item.project_uid])])) as Record<string,ReleasePublicationAction>)
// 创建回执不明时保留原请求；只允许核对或重试，不改变目标后另建一批。
const pendingCreation = ref<{ requestId:string; body:any }|null>(null)
let alive = true, generation = 0, readAbort: AbortController|undefined
let createRequest = '', startRequest = '', timer: ReturnType<typeof setInterval>|undefined
let listing: Promise<void>|undefined
let listRevision = 0
let positionedHash = ''
const scope = computed(() => ({ account_id: account.value, season_id: season.value, section_id: section.value }))
const displayLabels = computed(() => {
  if (existing.value || targetDriven.value) return scopeDisplay.value
  const selectedSeason = seasons.value.find((item:any) => item.value === season.value)
  const selectedSection = sections.value.find((item:any) => item.value === section.value)
  const labels: Record<string,string> = {}
  if (selectedSeason?.label) labels.season_title = selectedSeason.label
  if (selectedSection?.label) labels.section_title = selectedSection.label
  return labels
})
const scopeKey = computed(() => targetDriven.value ? JSON.stringify([items.value.map(item=>[item.project_uid,item.target_recipe,item.saved_scope]),orderConfirmed.value,existing.value?.id])
  : JSON.stringify([releaseScopeKey(items.value, scope.value, orderConfirmed.value, publications.value, versions.value, publicationActions.value),dubbingPolicyKey(dubbing.value)]))
const metadataAccount = computed(() => show.value && !existing.value && !targetDriven.value ? account.value : '')
const { metadata, loading: metadataLoading, error: metadataError, refresh: refreshMetadata } = usePublishMetadata(
  metadataAccount, id => api(`/api/publishing/accounts/${encodeURIComponent(id)}/metadata`, {}))
const accountOptions = computed(() => (catalog.value.accounts || []).map((a:any) => ({ value:a.id, label:`${a.name} · ${a.id}` })))
const seasons = computed(() => (metadata.value.seasons || []).map((s:any) => ({ value:s.id, label:s.title })))
const sections = computed(() => (metadata.value.seasons || []).find((s:any) => s.id === season.value)?.sections
  ?.map((s:any) => ({ value:s.id, label:s.title })) || [])
// 继续原批次时目标、顺序和版本仍冻结，但允许用户在检查前明确选择新的未投稿草稿。
const locked = computed(() => writing.value || loading.value || !!pendingCreation.value || !!existing.value)
const draftSelectionLocked = computed(() => targetDriven.value || writing.value || loading.value || !!pendingCreation.value
  || !!existing.value && !['ready','paused','failed','attention'].includes(existing.value.status))
const canInspect = computed(() => !loading.value && !writing.value && (existing.value ||
  (items.value.length >= 2 && orderConfirmed.value && (targetDriven.value || account.value && season.value && section.value && !dubbingPolicyError(dubbing.value)))))
const canStart = computed(() => !writing.value && !loading.value && (targetDriven.value || rightsConfirmed.value) && !!preview.value?.fingerprint
  && !preview.value.blockers?.length && inspectedScope.value === scopeKey.value)
const startingLabel = computed(() => existing.value && existing.value.status !== 'ready' ? '确认从断点继续' : '确认制作并按顺序发布')
const targetLabel=computed(()=>[account.value ? publicationTargetDisplay(targetPlan.value).account : targetDriven.value?'项目账号待设置':'选择发布账号',
  season.value ? publicationTargetDisplay(targetPlan.value).collection : targetDriven.value?'项目合集待设置':'选择目标合集'].join(' · '))
const targetPlan=computed(()=>({scope:scope.value,
  account_name:(catalog.value.accounts || []).find((item:any)=>String(item.id)===String(account.value))?.name,
  settings_display:preview.value?.settings_display || existing.value?.settings_display,
  items:preview.value?.items || existing.value?.plan?.items || existing.value?.items,
  scope_display:displayLabels.value}))
const batchCollection=(batch:any)=>publicationTargetDisplay(batch).collection
const stepLabel=(value:string)=>STEP_LABELS[value] || ({comments:'准备评论',publish:'发布到B站',prepare:'准备发布资料',upload:'上传视频',review:'核对平台审核',ordering:'核对合集顺序'} as Record<string,string>)[value] || (/[^\x00-\x7f]/.test(value || '') ? value : '处理步骤')

async function api(path:string, body?:any, signal?:AbortSignal) {
  const response = await fetch(path, { method:body === undefined ? 'GET':'POST',
    headers:body === undefined ? undefined : { 'Content-Type':'application/json' },
    body:body === undefined ? undefined : JSON.stringify(body),
    signal:signal ? AbortSignal.any([signal,AbortSignal.timeout(120000)]) : AbortSignal.timeout(120000) })
  let data:any
  try { data = await response.json() }
  catch { throw new Error('顺序发布服务返回了无效数据，请刷新页面或重启 DubForge 服务后重新读取。') }
  if (response.status === 404) throw new Error('当前服务尚未提供顺序发布接口，请更新或重启 DubForge 服务。')
  if (!response.ok) throw Object.assign(new Error(data.message || '顺序发布操作失败'), { code:data.code, status:response.status })
  return data
}
type ErrorContext = 'read' | 'write'
function friendlyError(e:unknown, context:ErrorContext = 'write') {
  const message = e instanceof Error ? e.message : String(e || '顺序发布操作失败')
  if (!/failed to fetch|networkerror|load failed|connection refused|err_connection|err_network|socket hang up|timeout|aborted/i.test(message)) return message
  return context === 'read'
    ? '暂时无法读取顺序发布的最新状态；这次读取没有启动或停止后台任务，请恢复连接后重新读取。'
    : '暂时无法确认操作结果。后台任务可能仍在执行，请恢复连接后核对批次状态；重试会沿用原请求。'
}
function invalidate() {
  if (pendingCreation.value) return
  selectionExpanded.value=true
  preview.value = null; inspectedScope.value = ''; rightsConfirmed.value = false; createRequest = ''; startRequest = ''
}
function publicationOptions(item:ReleaseItem) {
  return (item.publication_choices || []).map(p => {
    const time = new Date(p.created_at || '')
    const created = Number.isFinite(time.getTime()) ? ' · '+time.toLocaleString('zh-CN',{dateStyle:'short',timeStyle:'short'}) : ''
    return { value:p.id, label:`${p.is_review_revision ? '审核修订稿 · ' : ''}${p.title || '未命名草稿'} · ${PUBLICATION_STATUS[p.status] || '状态待核对'}${p.bvid ? ' · '+p.bvid : ''}${created}` }
  })
}
function publicationAction(item:any) {
  if (item.publication?.reuse_existing) return '复用原BV · 仅核对审核、合集与排列顺序'
  if (item.publication?.bvid) return '继续核对原BV · 不重新投稿'
  if (item.publication_action) return releaseActionLabel(item.publication_action)
  return item.publication_id ? '继续已有草稿 · 投稿后生成新BV' : '补齐制作并发布新稿'
}
function selectPublication(uid:string, key:string) { if (!draftSelectionLocked.value && items.value.find(item=>item.project_uid===uid)?.status!=='done') { publications.value = {...publications.value,[uid]:key}; invalidate() } }
function selectVersion(uid:string, id:string, checked:boolean) {
  if (targetDriven.value || locked.value || publicationActions.value[uid] !== 'prepare') return
  const selected = new Set(versions.value[uid] || [])
  checked ? selected.add(id) : selected.delete(id)
  const item = items.value.find(item=>item.project_uid===uid)
  versions.value = {...versions.value,[uid]:(item?.versions || []).filter(version=>selected.has(version.id)).map(version=>version.id)}
  invalidate()
}
function selectedPublication(item:ReleaseItem) { return item.publication_choices?.find(row=>row.id===publications.value[item.project_uid]) }
function selectionToggled(event:Event) {selectionExpanded.value=(event.target as HTMLDetailsElement).open}
function planningBody() {
  if(targetDriven.value)return {target_driven:true,project_uids:items.value.map(item=>item.project_uid),order_confirmed:orderConfirmed.value}
  const chosen = Object.fromEntries(items.value.filter(p=>publications.value[p.project_uid]).map(p=>[p.project_uid,publications.value[p.project_uid]]))
  const labels = displayLabels.value
  return { project_uids:items.value.map(p=>p.project_uid),scope:scope.value,
    ...(Object.keys(labels).length ? {scope_display:labels} : {}), order_confirmed:orderConfirmed.value,
    ...(Object.keys(chosen).length ? {publication_ids:chosen} : {}), version_ids:versions.value, publication_actions:publicationActions.value, dubbing:dubbing.value }
}
function onDubbingChange(value:DubbingRunPolicy){if(targetDriven.value||locked.value||existing.value)return;dubbing.value=value;invalidate()}
watch(scopeKey, invalidate)
watch(account, (_value, previous) => { if (previous && !existing.value && !targetDriven.value) { season.value = null; section.value = null } })
watch(season, (_value, previous) => { if (previous && !existing.value && !targetDriven.value) section.value = null })
async function focusBatch() {
  await nextTick()
  if (!alive || typeof document === 'undefined' || positionedHash === route.hash
      || !/^#release-batch-[a-zA-Z0-9_-]+$/.test(route.hash)) return
  let element = document.getElementById(route.hash.slice(1))
  // 直接从历史链接打开已完成批次时，先展开历史区，再定位到记录。
  if (!element && batches.value.some(batch => `release-batch-${batch.id}` === route.hash.slice(1))) {
    showCompletedBatches.value = true
    await nextTick()
    element = document.getElementById(route.hash.slice(1))
  }
  if (element) { positionedHash = route.hash; element.scrollIntoView({ behavior:'smooth', block:'center' }) }
}
watch(() => route.hash, () => { positionedHash = ''; void focusBatch() })
async function load(quiet=false) {
  if (listing) {if(!quiet)listLoading.value=true;return listing}
  // 后台读状态不插入按钮的加载图标，避免标题每次轮询都换行或变高。
  listLoading.value = !quiet
  const ticket = listRevision
  listing = (async () => {
    try {
      const result = await api('/api/release-batches')
      if (alive && ticket === listRevision) { batches.value = result.batches || []; listError.value = ''; void focusBatch() }
    } catch (e) { if (alive && ticket === listRevision) listError.value = friendlyError(e, 'read') }
    finally { listing = undefined; if (alive) listLoading.value = false }
  })()
  return listing
}
async function refreshAfterWrite() {
  listRevision++
  await listing
  if (alive) await load(true)
}
function candidatesPath(uids:string[]){return '/api/release-batches/candidates?uids='+encodeURIComponent(uids.join(','))+(targetDriven.value?'&target_driven=true':'')}
function adoptProjectScope(result:any){
  const saved=result.derived_scope || items.value[0]?.saved_scope || {}
  account.value=saved.account_id || '';season.value=saved.season_id ?? null;section.value=saved.section_id ?? null
  scopeDisplay.value=result.scope_display || saved
}
function goalRecipe(item:ReleaseItem){return existing.value?item.recipe || item.target_recipe || {}:item.target_recipe || item.recipe || {}}
function itemMode(item:ReleaseItem){const recipe=goalRecipe(item);return recipe.delivery_mode || recipe.mode || ''}
function goalNames(item:ReleaseItem){const recipe=goalRecipe(item),ids=recipe.production_version_ids || item.project_goal?.production_version_ids || []
  if(ids.length)return ids.map((id:string)=>item.versions?.find(version=>version.id===id)?.name || id).join('、')
  return recipe.mode==='update' && recipe.update?.replace_videos===false && !recipe.update?.sync_fields?.includes('add_parts')?'无需制作（只同步原稿资料）':'制作目标待确认'
}
function goalTarget(item:ReleaseItem){const recipe=goalRecipe(item)
  if(itemMode(item)==='produce')return ''
  if(recipe.mode==='update')return '更新已选原稿 · 保留原BV'
  return existing.value || item.project_goal?.target_kind==='new'?'创建新稿 · 新BV':'未设置稿件目标'
}
function publicationNames(item:ReleaseItem){const recipe=goalRecipe(item),ids=recipe.version_ids || [];return ids.map((id:string,index:number)=>`P${index+1} · ${recipe.part_titles?.[id] || item.versions?.find(version=>version.id===id)?.name || id}`).join('；')}
async function open(targets:{uid:string;title:string}[], useSavedSettings=true) {
  if (writing.value || targets.length < 2) return
  if (pendingCreation.value) { show.value = true; await inspect(); return }
  readAbort?.abort(); readAbort = new AbortController(); const ticket = ++generation
  show.value = true; existing.value = null; targetDriven.value=useSavedSettings; error.value = ''; loading.value = true; catalogLoaded.value = false
  dubbing.value=defaultDubbingRunPolicy()
  scopeDisplay.value = {}
  items.value = targets.map(p => ({ project_uid:p.uid, title:p.title })); invalidate(); orderConfirmed.value = false
  publications.value = {}
  versions.value = {}
  try {
    const [accounts, candidates] = await Promise.all([
      api('/api/publishing/catalog', undefined, readAbort.signal),
      api(candidatesPath(targets.map(p=>p.uid)), undefined, readAbort.signal),
    ])
    if (!alive || ticket !== generation) return
    catalog.value = accounts; catalogLoaded.value = true; items.value = chronologicalItems(candidates.items || [])
    if(targetDriven.value)adoptProjectScope(candidates)
    else {
      publications.value = Object.fromEntries(items.value.filter(p=>p.publication_choices?.length === 1).map(p=>[p.project_uid,p.publication_choices![0].id]))
      versions.value = Object.fromEntries(items.value.map(item=>[item.project_uid,releaseVersionSelection(item)]))
      if (!account.value) account.value = accounts.accounts?.[0]?.id || ''
    }
    await nextTick()
  } catch (e) { if (alive && ticket === generation) error.value = friendlyError(e, 'read') }
  finally { if (alive && ticket === generation) loading.value = false }
}
async function openBatch(batch:any) {
  if (writing.value) return
  if (pendingCreation.value) { show.value = true; await inspect(); return }
  readAbort?.abort(); readAbort = new AbortController(); const ticket = ++generation
  show.value = true; loading.value = true; existing.value = batch; targetDriven.value=batch.plan?.target_driven===true; items.value = batch.items || []; error.value = ''; invalidate()
  dubbing.value=dubbingRunPolicy(batch.dubbing || batch.plan?.dubbing?.policy || batch.plan?.dubbing || batch.items?.find((item:any)=>item.recipe?.dubbing)?.recipe.dubbing)
  scopeDisplay.value = batch.scope_display || batch.plan?.scope_display || {}
  publications.value = Object.fromEntries(items.value.filter(p=>p.publication_id).map(p=>[p.project_uid,p.publication_id!]))
  versions.value = Object.fromEntries(items.value.map(item=>[item.project_uid,releaseVersionSelection(item)]))
  account.value = batch.scope.account_id; season.value = batch.scope.season_id; section.value = batch.scope.section_id
  orderConfirmed.value = true
  try {
    const result = await api(candidatesPath(items.value.map(item=>item.project_uid)), undefined, readAbort.signal)
    if (!alive || ticket !== generation) return
    const byUid = new Map<string, any>((result.items || []).map((item:any) => [item.project_uid, item]))
    items.value = items.value.map(item => {
      const fresh = byUid.get(item.project_uid)
      return fresh ? { ...fresh, ...item, publication_choices: fresh.publication_choices || [] } : item
    })
  } catch (e) { if (alive && ticket === generation) error.value = friendlyError(e, 'read') }
  finally { if (alive && ticket === generation) loading.value = false }
  await nextTick()
  if (ticket === generation) await inspect()
}
function applyInspectedItems(resultItems:any[]) {
  if (!Array.isArray(resultItems) || !resultItems.length) return
  const byUid = new Map(resultItems.map(item => [item.project_uid, item]))
  // 批次顺序已经冻结；只用预检返回的完整记录补齐摘要字段，不接受服务端回传顺序改变。
  items.value = items.value.map(item => {
    const complete = byUid.get(item.project_uid)
    return complete ? { ...item, ...complete } : item
  })
  publications.value = Object.fromEntries(items.value
    .filter(item => item.publication_id)
    .map(item => [item.project_uid, item.publication_id!]))
  versions.value = Object.fromEntries(items.value.map(item => [item.project_uid, releaseVersionSelection(item)]))
}
function batchFailureItem(batch:any) {
  if (!['attention', 'failed'].includes(batch?.status) || !Array.isArray(batch.items)) return null
  const index = Number.isInteger(batch.cursor) ? batch.cursor : -1
  const item = index >= 0 ? batch.items[index] : null
  return item && ['attention', 'failed'].includes(item.status) ? item : null
}
function batchFailureDetails(batch:any) {
  const item = batchFailureItem(batch)
  if (!item && !['attention', 'failed'].includes(batch?.status)) return null
  return {
    item,
    step: item?.failed_step || item?.failure?.step || batch?.failed_step || batch?.failure?.step || '',
    code: item?.error_code || item?.failure?.code || batch?.error_code || batch?.failure?.code || '',
    suggestion: item?.suggestion || item?.failure?.suggestion || batch?.suggestion || batch?.failure?.suggestion || '',
  }
}
function batchBlockedMessage(batch:any) {
  const item = batchFailureItem(batch)
  if (!item) return ''
  const position = Number.isInteger(batch.cursor) ? batch.cursor + 1 : 0
  const total = Number.isInteger(batch.total) ? batch.total : batch.items.length
  const remaining = Math.max(0, total - position)
  return `第 ${position}/${total} 项${item.title ? `「${item.title}」` : ''}阻塞，后续 ${remaining} 项未开始。`
}
async function refreshCandidates() {
  if (locked.value || !items.value.length) return
  readAbort?.abort();readAbort=new AbortController();const ticket=++generation
  loading.value=true;error.value='';invalidate()
  try {
    const result=await api(candidatesPath(items.value.map(item=>item.project_uid)),undefined,readAbort.signal)
    if (!alive || ticket!==generation) return
    const byId=new Map<string,ReleaseItem>((result.items || []).map((item:ReleaseItem)=>[item.project_uid,item]))
    const next=items.value.map(item=>byId.get(item.project_uid) || item)
    if(targetDriven.value){items.value=next;adoptProjectScope(result);orderConfirmed.value=false;return}
    publications.value=Object.fromEntries(next.flatMap(item=>{
      const choices=item.publication_choices || [],selected=publications.value[item.project_uid]
      const id=choices.some(choice=>choice.id===selected) ? selected : choices.length===1 ? choices[0].id : ''
      return id ? [[item.project_uid,id]] : []
    }))
    versions.value=Object.fromEntries(next.map(item=>[item.project_uid,Object.hasOwn(versions.value,item.project_uid)
      ? (versions.value[item.project_uid] || []).filter(id=>item.versions?.some(version=>version.id===id)) : releaseVersionSelection(item)]))
    items.value=next;orderConfirmed.value=false
  } catch(e) {if(alive && ticket===generation)error.value=friendlyError(e, 'read')}
  finally {if(alive && ticket===generation)loading.value=false}
}
async function inspect() {
  if (!canInspect.value) return
  readAbort?.abort(); readAbort = new AbortController(); const ticket = ++generation, key = scopeKey.value
  loading.value = true; error.value = ''; rightsConfirmed.value = false
  if (pendingCreation.value) {
    try {
      const receipt = await api('/api/release-batches?request_id='+encodeURIComponent(pendingCreation.value.requestId), undefined, readAbort.signal)
      if (!alive || ticket !== generation) return
      const batch = receipt.batches?.[0]
      if (batch) { pendingCreation.value = null; loading.value = false; await openBatch(batch) }
      else error.value = '尚未找到创建回执，原方案与请求已保留。可重新确认后重试同一请求，或稍后再次检查；不会另建一个批次。'
    } catch (e) { if (alive && ticket === generation) error.value = friendlyError(e, 'read') }
    finally { if (alive && ticket === generation) loading.value = false }
    return
  }
  preview.value = null
  try {
    const chosen = Object.fromEntries(items.value.filter(p=>publications.value[p.project_uid]).map(p=>[p.project_uid, publications.value[p.project_uid]]))
    const result = existing.value
      ? await api(`/api/release-batches/${encodeURIComponent(existing.value.id)}/plan`, !targetDriven.value && Object.keys(chosen).length ? { publication_ids: chosen } : {}, readAbort.signal)
      : await api('/api/release-batches/plan', planningBody(), readAbort.signal)
    if (!alive || ticket !== generation || key !== scopeKey.value) return
    if (existing.value || targetDriven.value) applyInspectedItems(result.items)
    if(targetDriven.value && result.scope){account.value=result.scope.account_id;season.value=result.scope.season_id;section.value=result.scope.section_id;scopeDisplay.value=result.scope_display || scopeDisplay.value}
    preview.value = result; inspectedScope.value = scopeKey.value; createRequest = crypto.randomUUID(); startRequest = crypto.randomUUID()
    selectionExpanded.value=!!result.blockers?.length
  } catch (e) { if (alive && ticket === generation) error.value = friendlyError(e, 'read') }
  finally { if (alive && ticket === generation) loading.value = false }
}
async function start() {
  if (!canStart.value) return
  writing.value = true; error.value = ''
  const snapshot = JSON.parse(JSON.stringify(preview.value))
  const intent = JSON.parse(JSON.stringify(planningBody()))
  try {
    let batch = existing.value
    if (!batch) {
      pendingCreation.value ||= { requestId:createRequest, body:{ ...intent, order_confirmed:true, confirmed:true,
        fingerprint:snapshot.fingerprint, request_id:createRequest } }
      batch = await api('/api/release-batches', pendingCreation.value.body)
      pendingCreation.value = null
      // 创建回执保留；开始响应丢失时继续同一批次，绝不新建第二批。
      existing.value = batch
    }
    const chosen = Object.fromEntries((snapshot.items || []).filter((p:any)=>p.publication_id).map((p:any)=>[p.project_uid, p.publication_id]))
    const result = await api(`/api/release-batches/${encodeURIComponent(batch.id)}/${batch.status === 'ready' ? 'start':'resume'}`,
      { confirmed:true, rights_confirmed:true, fingerprint:snapshot.fingerprint, ...(!targetDriven.value?{publication_ids:chosen}:{}), request_id:startRequest })
    if (!alive) return
    existing.value = result; show.value = false; emit('started'); emit('changed'); void refreshAfterWrite()
  } catch (e:any) {
    if (alive) {
      error.value = friendlyError(e, 'write')
      if (e.status >= 400 && e.status < 500) pendingCreation.value = null
      if (e.status === 409 || /CHANGED|CONFLICT/.test(e.code || '')) invalidate()
      void refreshAfterWrite()
    }
  } finally { if (alive) writing.value = false }
}
async function pause(batch:any) {
  if (writing.value || !releaseBatchControls(batch.status).pause) return
  writing.value = true; listError.value = ''
  try { await api(`/api/release-batches/${encodeURIComponent(batch.id)}/pause`, {}); if (alive) { emit('changed'); void refreshAfterWrite() } }
  catch (e) { if (alive) listError.value = friendlyError(e, 'write') }
  finally { if (alive) writing.value = false }
}
async function stop(batch:any) {
  if (writing.value || !releaseBatchControls(batch.status).stop) return
  writing.value = true; listError.value = ''
  try { await api(`/api/release-batches/${encodeURIComponent(batch.id)}/stop`, {}); if (alive) { emit('changed'); void refreshAfterWrite() } }
  catch (e) { if (alive) listError.value = friendlyError(e, 'write') }
  finally { if (alive) writing.value = false }
}
function sort(desc = false) { if (!locked.value) { items.value = chronologicalItems(items.value,desc); orderConfirmed.value = false; invalidate() } }
function move(index:number, offset:number) { if (!locked.value) { items.value = moveReleaseItem(items.value,index,offset); orderConfirmed.value = false; invalidate() } }
function close() { if (writing.value) return; generation++; readAbort?.abort(); show.value = false; loading.value = false; rightsConfirmed.value = false; invalidate() }
onMounted(() => {
  void load(); timer = setInterval(() => {
    if (!document.hidden && batches.value.some(b => ['running','pausing','stopping'].includes(b.status))) void load(true)
  }, 5000)
})
onUnmounted(() => { alive = false; generation++; readAbort?.abort(); if (timer) clearInterval(timer) })
// 对项目列表只暴露基于已保存设置的新建入口；历史兼容分支仅用于恢复。
defineExpose({ open:(targets:{uid:string;title:string}[])=>open(targets) })
</script>

<template>
  <section v-if="standaloneBatches.length || listError" class="release-batches" aria-label="顺序发布批次">
    <div class="batch-heading"><div><strong>顺序发布批次</strong><p>批量编排任务；项目仍独立管理。有效产物复用，缺失或过期步骤自动补齐。</p></div><n-button size="small" secondary :loading="listLoading" :disabled="listLoading" class="batch-refresh" @click="load()">刷新批次</n-button></div>
    <n-alert v-if="listError" type="warning">{{ listError }} <n-button text @click="load()">重新读取</n-button></n-alert>
    <div v-if="completedBatchCount" class="batch-history-toggle">
      <n-button size="small" quaternary @click="showCompletedBatches = !showCompletedBatches">
        {{ showCompletedBatches ? '收起已完成批次' : '查看已完成批次' }}（{{ completedBatchCount }}）
      </n-button>
      <span>已完成批次仅保留发布顺序与回执，项目状态不会因此合并。</span>
    </div>
    <article v-for="batch in visibleBatches" :key="batch.id" :id="'release-batch-'+batch.id" class="batch-record">
      <div class="batch-record-head"><div><n-tag size="small" :type="batchStage(batch.status).tone">{{ batchStage(batch.status).label }}</n-tag><strong :title="batchCollection(batch)">{{ batchCollection(batch) }}</strong></div><span>项目完成 {{ batch.cursor }}/{{ batch.total }}</span></div>
      <ReleaseCurrentProgress v-if="batch.current_item" :progress="batch.current_item" :total="batch.total" />
      <p v-if="!batch.current_item || batch.error || ['pausing','stopping'].includes(batch.status)" class="batch-phase">{{ batch.phase }}<template v-if="batch.error"> · {{ batch.error }}</template></p>
      <p v-if="batchBlockedMessage(batch)" class="batch-blocked">{{ batchBlockedMessage(batch) }}</p>
      <p v-if="batchFailureDetails(batch)?.step || batchFailureDetails(batch)?.code || batchFailureDetails(batch)?.suggestion" class="batch-failure-details">
        <span v-if="batchFailureDetails(batch)?.step">失败步骤：{{ stepLabel(batchFailureDetails(batch)?.step) }}</span>
        <span v-if="batchFailureDetails(batch)?.code">错误码：{{ batchFailureDetails(batch)?.code }}</span>
        <span v-if="batchFailureDetails(batch)?.suggestion">建议：{{ batchFailureDetails(batch)?.suggestion }}</span>
      </p>
      <div class="batch-toolbar" aria-label="批次操作"><div class="batch-toolbar-actions">
        <n-button v-if="releaseBatchControls(batch.status).pause" size="small" :disabled="writing" title="当前项目完成制作、发布和审核核对后暂停，不再启动下一项目。" @click="pause(batch)">本项完成后暂停</n-button>
        <n-button v-if="releaseBatchControls(batch.status).stop" size="small" secondary :disabled="writing" title="请求终止当前制作、上传或审核等待；保留已有产物。已发送的投稿可能已生效，需要核对回执。" @click="stop(batch)">停止当前项</n-button>
        <n-button v-if="releaseBatchControls(batch.status).resume" size="small" secondary :disabled="writing" @click="openBatch(batch)">{{ batch.status === 'ready' ? '检查并开始…':'检查并继续…' }}</n-button>
      </div><p v-if="releaseBatchControls(batch.status).hint" class="batch-control-hint">{{ releaseBatchControls(batch.status).hint }}</p></div>
      <details class="batch-progress"><summary>查看 {{ batch.total }} 个项目的顺序与进度</summary><ol class="batch-items">
        <li v-for="(item,index) in batch.items" :key="item.project_uid" :class="{'current-item':index===batch.cursor && ['running','pausing','stopping'].includes(batch.status)}">
          <span class="batch-item-number">{{ index+1 }}</span><div class="batch-item-copy"><div class="batch-item-heading"><RouterLink :to="'/projects/'+item.project_uid">{{ item.title }}</RouterLink><n-tag size="small" :bordered="false" :type="releaseItemStage(item.status).tone">{{ releaseItemStage(item.status).label }}</n-tag></div><p v-if="item.phase" class="batch-item-phase">{{ item.phase }}</p><p v-if="item.error" class="item-error">{{ item.error }}</p><p v-if="item.failed_step || item.error_code || item.suggestion" class="batch-item-diagnostic"><span v-if="item.failed_step">失败步骤：{{ stepLabel(item.failed_step) }}</span><span v-if="item.error_code">错误码：{{ item.error_code }}</span><span v-if="item.suggestion">建议：{{ item.suggestion }}</span></p><RouterLink v-if="item.publication_id" class="batch-item-record" :to="{path:'/projects/'+item.project_uid+'/publish',query:{publication:item.publication_id}}">核对现有稿件 →</RouterLink></div>
        </li>
      </ol></details>
    </article>
  </section>
  <n-drawer :show="show" width="min(880px,100vw)" :mask-closable="!writing" :close-on-esc="!writing" @update:show="value => { if (!value) close() }">
    <n-drawer-content :title="existing ? '检查并继续原批次':'制作并按顺序发布'" :closable="!writing">
      <div class="release-form">
        <p class="release-intro">{{ targetDriven?'核对项目顺序，使用各项目步骤中已保存的目标成片与交付设置。只有明确设为“上传并提交”、且账号和合集小节一致的项目才能组成顺序发布批次。':'沿用原批次确认的合集、顺序与版本，检查方案后从断点继续。' }}后台逐项目执行，关闭页面仍继续处理。</p>
        <div class="release-flow" aria-label="自动执行流程"><span>补齐 / 更新制作</span><span>核对成片与审查</span><span>顺序上传与审核</span><span>核对合集顺序</span></div>
        <n-alert type="info" :show-icon="false">{{ targetDriven?'批次只安排跨项目顺序，不覆盖各项目的发布授权。新稿或原BV更新范围均从发布步骤读取；需要修改时先回对应步骤保存，再刷新项目资料并检查。':'此历史批次沿用原来的已确认范围；不会因当前项目默认设置变化而扩大发布授权。' }}</n-alert>
        <n-alert v-if="error" type="error">{{ error }}</n-alert>
        <details class="selection-settings" :open="selectionExpanded" @toggle="selectionToggled">
          <summary><strong>发布范围 · {{ items.length }} 个项目</strong><span>{{ targetLabel }}</span><small>{{ targetDriven?'查看已保存的交付目标与项目顺序':preview && !selectionExpanded ? '展开查看已确认的顺序与版本' : '按原批次核对稿件与分P' }}</small></summary>
          <div class="selection-body">
        <div v-if="!existing && !targetDriven" class="release-target"><label>发布账号<n-select v-model:value="account" :options="accountOptions" :disabled="locked" placeholder="选择已保存的账号" /></label><label>目标合集<n-select v-model:value="season" :options="seasons" :loading="metadataLoading" :disabled="locked || !account" placeholder="选择合集" /></label><label>合集小节<n-select v-model:value="section" :options="sections" :disabled="locked || !season" placeholder="选择小节" /></label></div>
        <div v-else class="target-summary"><PublicationTargetSummary :plan="targetPlan" /><span>已确认的顺序与目标保持固定</span></div>
        <n-alert v-if="!existing && catalogLoaded && catalog.accounts.length === 0 && !loading" type="info"><RouterLink to="/settings?section=bilibili">到设置连接B站账号 →</RouterLink></n-alert>
        <p v-if="!existing && !targetDriven && metadataError" class="item-error">{{ metadataError }} <n-button text size="small" @click="refreshMetadata(true)">重新读取合集</n-button></p>
        <div class="order-heading"><strong>发布顺序 · {{ items.length }} 个项目</strong><div v-if="!existing"><n-button size="small" quaternary :disabled="locked" @click="refreshCandidates">刷新项目资料</n-button><n-button size="small" :disabled="locked" @click="sort(false)">时间从早到晚</n-button><n-button size="small" :disabled="locked" @click="sort(true)">时间从晚到早</n-button></div></div>
        <ol class="release-order">
          <li v-for="(item,index) in items" :key="item.project_uid">
            <span class="order-number">{{ index+1 }}</span>
            <div class="order-copy">
              <RouterLink :to="'/projects/'+item.project_uid">{{ item.title }}</RouterLink>
              <small>{{ item.source_date || '原视频日期未知，请手动核对顺序' }}</small>
              <section v-if="targetDriven" class="saved-project-goal" :aria-label="item.title+'的已保存交付目标'">
                <strong>交付方式：{{ itemMode(item)==='publish'?'上传并提交':itemMode(item)==='upload'?'上传后等待确认':itemMode(item)==='draft'?'准备发布草稿':itemMode(item)==='produce'?'仅保留成片':'尚未读取' }}</strong>
                <span>制作目标：{{ goalNames(item) }}</span><span v-if="publicationNames(item)">发布分P：{{ publicationNames(item) }}</span>
                <span v-if="goalTarget(item)">{{ goalTarget(item) }}</span>
                <p v-if="itemMode(item)!=='publish' && !item.blockers?.length" class="item-error">顺序发布仅接受“上传并提交”。请到该项目发布步骤修改并保存；批次不会自动升级交付方式。</p>
                <p v-for="issue in item.blockers || []" :key="issue" class="item-error">{{ issue }}</p>
                <RouterLink :to="`/projects/${item.project_uid}/publish#project-rules`">检查此项目发布设置 →</RouterLink>
                <RouterLink :to="`/projects/${item.project_uid}/steps/assemble`">检查此项目成片目标 →</RouterLink>
              </section>
              <template v-else>
              <label v-if="item.publication_choices?.length && (!existing || item.status !== 'done')" class="publication-choice">本次处理的稿件
                <n-select :value="publications[item.project_uid] || null" :options="publicationOptions(item)" :disabled="draftSelectionLocked" size="small" placeholder="请选择一条记录，避免重复投稿" :aria-label="item.title+'的发布记录'" @update:value="key => selectPublication(item.project_uid,key)" />
              </label>
              <small v-if="!item.publication_choices?.length && !item.publication_id">制作完成后自动创建一份发布草稿</small>
              <small v-if="!item.publication_choices?.length || publications[item.project_uid] || existing">{{ releaseActionLabel(publicationActions[item.project_uid]) }}<template v-if="selectedPublication(item)?.bvid"> · {{ selectedPublication(item)?.bvid }}</template></small>
              <div v-if="publicationActions[item.project_uid]==='prepare' && item.versions?.length" class="version-choices" :aria-label="item.title+'的发布版本'">
                <span class="choice-heading">本次发布版本 · 每个勾选版本对应一个分P</span>
                <label v-for="version in item.versions" :key="version.id" class="version-choice">
                  <n-checkbox :checked="versions[item.project_uid]?.includes(version.id)" :disabled="locked" @update:checked="checked => selectVersion(item.project_uid,version.id,checked)">{{ version.name }}</n-checkbox>
                  <small><template v-if="versions[item.project_uid]?.includes(version.id)">P{{ versions[item.project_uid].indexOf(version.id)+1 }} · </template>{{ versionSummary(version) }}</small>
                </label>
                <p v-if="!versions[item.project_uid]?.length" class="item-error">至少选择一个发布版本，再检查方案。</p>
              </div>
              <small v-else-if="publicationActions[item.project_uid]==='reuse_existing'">沿用原稿件分P；本地重新制作不会替换原BV视频。</small>
              <small v-else-if="publicationActions[item.project_uid]==='continue'">沿用已经确认的上传清单，不更换视频文件。</small>
              <RouterLink v-if="item.publication_choices && item.publication_choices.length>1" class="record-management" :to="{path:'/publications',query:{project:item.project_uid}}" target="_blank" rel="noopener noreferrer">管理多余草稿与历史记录 ↗</RouterLink>
              <small v-if="item.publication_choices && item.publication_choices.length>1">整理记录后点「刷新项目资料」，保留当前顺序与版本选择。</small>
              </template>
            </div>
            <div v-if="!existing" class="order-controls"><n-button size="tiny" :disabled="locked || index === 0" :aria-label="'上移 '+item.title" @click="move(index,-1)">上移</n-button><n-button size="tiny" :disabled="locked || index === items.length-1" :aria-label="'下移 '+item.title" @click="move(index,1)">下移</n-button></div>
          </li>
        </ol>
        <n-checkbox v-if="!existing" v-model:checked="orderConfirmed" :disabled="locked">已核对项目顺序，按此顺序排列在目标合集小节中</n-checkbox>
          </div>
        </details>
        <DubbingRunOptions v-if="!targetDriven" :model-value="dubbing" :readonly="!!existing" :disabled="locked" @update:model-value="onDubbingChange" />
        <p v-if="existing" class="release-intro">继续原批次时沿用已确认的范围和授权；每个项目的实际制作与发布范围见执行方案。</p>
        <n-spin v-if="loading" size="small" description="正在核对项目、已有稿件与目标合集…" />
        <div v-if="preview" class="release-preview" data-testid="release-preview">
          <n-alert v-if="preview.blockers?.length" type="warning">请先处理以下问题并重新检查方案，之后才可确认开始：<ul><li v-for="issue in preview.blockers" :key="issue">{{ issue }}</li></ul><div v-if="targetDriven" class="settings-recovery-links"><RouterLink v-for="item in items" :key="item.project_uid" :to="`/projects/${item.project_uid}/publish#project-rules`">{{ item.title }} · 发布设置 →</RouterLink></div></n-alert>
          <ul v-if="preview.warnings?.length" class="release-warnings"><li v-for="warning in preview.warnings" :key="warning">{{ warning }}</li></ul>
          <div class="preview-heading"><strong>执行方案 · 按以下顺序</strong><span>每项可展开查看完整步骤与分P</span></div>
          <ReleaseProjectPlan v-for="(item,index) in preview.items" :key="item.project_uid" :item="item" :index="index" />
        </div>
      </div>
      <template #footer>
        <div class="release-confirmation" data-testid="release-confirmation">
          <template v-if="preview"><PublicationRightsHint /><n-checkbox v-if="!targetDriven" v-model:checked="rightsConfirmed" :disabled="writing || !!preview.blockers?.length">确认素材可用于本批发布，并按以上方案制作、投稿或复用原BV</n-checkbox></template>
          <div class="confirmation-actions"><span>{{ preview ? preview.blockers?.length ? '先处理阻断问题，再重新检查' : preview.items.length+' 个项目已检查；失败时保留进度并暂停' : '只检查，不运行；确认后才自动制作和发布' }}</span><n-button :disabled="!canInspect" :loading="loading" @click="inspect">{{ preview ? '重新检查方案':'预览制作与发布方案' }}</n-button><n-button v-if="preview" type="primary" :disabled="!canStart" :loading="writing" @click="start">{{ startingLabel }}</n-button></div>
        </div>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.saved-project-goal{display:grid;gap:7px;margin-top:12px;padding:12px;border:1px solid var(--df-border);border-radius:8px;background:var(--df-bg);font-size:12px;line-height:1.7}.saved-project-goal span{color:var(--df-muted)}.saved-project-goal a,.settings-recovery-links a{font-size:12px;color:var(--df-accent)}.settings-recovery-links{display:flex;gap:8px 16px;flex-wrap:wrap;margin-top:12px}
.release-batches{margin:18px 0;padding:18px;border:1px solid var(--df-border);border-radius:14px;background:var(--df-panel)}
.batch-history-toggle{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:12px;padding-top:10px;border-top:1px solid var(--df-border);font-size:11px;color:var(--df-muted);line-height:1.8}
.order-heading,.order-heading>div,.order-controls,.check-action{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.batch-record-head>span,.batch-phase,.check-action>span,.release-intro,.target-summary>span{color:var(--df-muted);font-size:12px;line-height:1.8}
.batch-blocked{margin:8px 0 0;color:var(--df-danger,#d97777);font-size:12px;font-weight:600;line-height:1.8}.batch-failure-details,.batch-item-diagnostic{display:flex;flex-wrap:wrap;gap:5px 14px;margin:4px 0 0;color:var(--df-danger,#d97777);font-size:11px;line-height:1.7}.batch-item-diagnostic{font-size:11px}
.batch-heading{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:14px;min-height:44px}.batch-heading strong{font-size:14px}.batch-heading p{font-size:11px;color:var(--df-muted);line-height:1.8;margin:4px 0 0}.batch-refresh{width:112px;flex-shrink:0}.batch-record{margin-top:16px;padding-top:16px;border-top:1px solid var(--df-border)}
.batch-record-head{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;margin-bottom:10px;min-height:28px}.batch-record-head>div{display:flex;align-items:center;gap:10px;min-width:0}.batch-record-head .n-tag{flex-shrink:0}.batch-record-head strong{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.batch-record-head>span{font-size:11px;font-variant-numeric:tabular-nums;white-space:nowrap}.batch-phase{margin:10px 0;min-height:22px;overflow-wrap:anywhere}
.batch-toolbar{display:grid;gap:7px;margin:12px 0}.batch-toolbar-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.batch-toolbar-actions:empty{display:none}.batch-control-hint{font-size:11px;color:var(--df-muted);line-height:1.8;margin:0}.batch-progress{border-top:1px solid var(--df-border);padding-top:12px}.batch-progress>summary{cursor:pointer;color:var(--df-muted);font-size:12px;line-height:1.8}.batch-items{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:6px}.batch-items li{display:flex;align-items:flex-start;gap:10px;padding:10px 12px;border-radius:8px;background:var(--df-bg)}.batch-items .current-item{background:var(--df-accent-soft);box-shadow:inset 3px 0 var(--df-accent)}.batch-item-number{display:grid;place-items:center;width:23px;height:23px;border-radius:6px;font-size:11px;background:var(--df-panel);color:var(--df-muted);flex-shrink:0}.current-item .batch-item-number{color:var(--df-accent)}.batch-item-copy{flex:1;min-width:0}.batch-item-heading{display:flex;align-items:flex-start;gap:12px;justify-content:space-between}.batch-item-heading>a{font-size:12px;line-height:1.8;color:var(--df-text);text-decoration:none;overflow-wrap:anywhere}.batch-item-heading>.n-tag{flex-shrink:0}.batch-item-phase{font-size:11px;color:var(--df-muted);line-height:1.7;margin:3px 0 0;overflow-wrap:anywhere}.batch-item-record{display:inline-block;font-size:11px;color:var(--df-accent);text-decoration:none;margin-top:5px}
.release-form{display:flex;flex-direction:column;gap:20px}.release-intro{font-size:13px;margin:0}.release-target{display:grid;grid-template-columns:1fr 1fr;gap:16px}.release-target label{display:grid;gap:8px;font-size:12px;color:var(--df-muted)}.release-target label:first-child{grid-column:1/-1}
.selection-settings{border:1px solid var(--df-border);border-radius:10px;padding:14px 16px}.selection-settings>summary{cursor:pointer;color:var(--df-muted);font-size:12px;line-height:1.8}.selection-settings>summary strong{color:var(--df-text);font-size:13px;margin-right:12px}.selection-settings>summary>span{display:inline}.selection-settings>summary small{display:block;font-size:11px;margin-left:16px;color:var(--df-accent)}.selection-body{display:grid;gap:20px;padding-top:18px}.preview-heading{display:flex;gap:10px;align-items:center;justify-content:space-between;font-size:13px}.preview-heading>span{font-size:11px;color:var(--df-muted)}.release-confirmation{display:grid;gap:10px;width:100%}.release-confirmation>.n-checkbox{font-size:12px}.confirmation-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.confirmation-actions>span{font-size:11px;line-height:1.8;color:var(--df-muted);margin-right:auto}.confirmation-actions>.n-button{flex-shrink:0}
.target-summary{margin:0}.target-summary>span{display:block}.order-heading{justify-content:space-between}.release-order{list-style:none;margin:0;padding:0;border:1px solid var(--df-border);border-radius:12px;overflow:hidden}
.release-order li{display:flex;align-items:center;gap:14px;padding:14px 16px;border-bottom:1px solid var(--df-border)}.release-order li:last-child{border:0}.order-number{width:28px;height:28px;display:grid;place-items:center;background:var(--df-bg);border-radius:8px;color:var(--df-muted);font-variant-numeric:tabular-nums}
.order-copy{flex:1;min-width:0}.order-copy a{color:var(--df-text);text-decoration:none;font-weight:600}.order-copy small{display:block;color:var(--df-muted);font-size:12px;margin-top:4px}.item-error{color:var(--df-danger,#d97777);font-size:12px;margin:6px 0}
.publication-choice{display:grid;gap:7px;margin-top:12px;color:var(--df-muted);font-size:12px}
.release-flow{display:flex;flex-wrap:wrap;gap:8px;color:var(--df-muted);font-size:12px}.release-flow span{padding:7px 10px;border-radius:8px;background:var(--df-bg)}.release-flow span+span:before{content:'→';margin-right:10px;color:var(--df-accent)}
.release-order li{align-items:flex-start}.order-controls{padding-top:3px}.version-choices{display:grid;gap:9px;margin-top:14px;padding:12px;background:var(--df-bg);border-radius:8px}.choice-heading{font-size:11px;color:var(--df-muted)}.version-choice{display:grid;gap:2px}.version-choice small{margin:0 0 0 25px;font-size:11px}.record-management{display:inline-block!important;font-weight:400!important;font-size:11px;margin-top:10px;color:var(--df-accent)!important}.production-plan{margin-top:14px}.preparation-note{font-size:12px;line-height:1.8;color:var(--df-muted);margin:10px 0 0}
.preview-publication{display:inline-block;margin-top:10px;font-size:12px}
.release-preview{display:flex;flex-direction:column;gap:16px;padding-top:18px;border-top:1px solid var(--df-border)}.preview-project{padding:14px 16px;border:1px solid var(--df-border);border-radius:10px}.preview-project>span{display:block;color:var(--df-muted);font-size:12px;margin-top:6px}.preview-version{font-size:12px;margin-top:8px;color:var(--df-muted)}.release-warnings{margin:0;padding-left:20px;color:var(--df-muted);font-size:12px;line-height:1.8}
@media(max-width:600px){.release-target{grid-template-columns:1fr}.release-order li{padding:12px;gap:10px}.order-controls{gap:5px}.order-heading>div{gap:6px}.order-number{width:24px;height:24px;flex-shrink:0}.preview-heading{align-items:flex-start;flex-direction:column;gap:4px}.confirmation-actions>span{flex-basis:100%}.confirmation-actions>.n-button{flex:1}.selection-settings>summary>span{display:block;margin-left:16px}}
@media(max-width:480px){.release-batches{padding:14px}.batch-heading p{max-width:190px}.batch-record-head{grid-template-columns:1fr;gap:4px}.batch-item-heading{flex-wrap:wrap;gap:3px}.batch-item-heading>.n-tag{margin-left:auto}}
</style>
