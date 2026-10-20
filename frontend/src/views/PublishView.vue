<script setup lang="ts">
import WorkflowStageNavigation from '../components/WorkflowStageNavigation.vue'
import PublicationRightsHint from '../components/PublicationRightsHint.vue'
import ContentReviewPanel from '../components/ContentReviewPanel.vue'
import PublishPartsEditor from '../components/PublishPartsEditor.vue'
import PublishResultPanel from '../components/PublishResultPanel.vue'
import PublicationUpdatePanel from '../components/PublicationUpdatePanel.vue'
import PublicationRecordLifecycle from '../components/PublicationRecordLifecycle.vue'
import ConfigurationForm from '../components/ConfigurationForm.vue'
import { computed, h, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter, onBeforeRouteLeave, onBeforeRouteUpdate, RouterLink } from 'vue-router'
import { NAlert, NButton, NCard, NInput, NSelect, NTag, NTabs, NTabPane, useMessage, useDialog } from 'naive-ui'
import PublishSettingsForm from '../components/PublishSettingsForm.vue'
import PublishCopyAssistant from '../components/PublishCopyAssistant.vue'
import PublishRemoteSyncPanel from '../components/PublishRemoteSyncPanel.vue'
import BiliChaptersPanel from '../components/BiliChaptersPanel.vue'
import PublishChapterOptions from '../components/PublishChapterOptions.vue'
import { PUBLICATION_STATUS, PUBLISH_ACTIVE, publicationReadbackPending, movePart, publicationScope, publicationAiNotice, collectionDisplay } from '../utils/publishing'
import { publicationChapterState, preferredPublicationChapterParts } from '../utils/publicationChapters'
import { finalVideoLabel } from '../utils/preview'
import { usePublishMetadata } from '../composables/usePublishMetadata'
import { refreshInBackground } from '../utils/backgroundRefresh'
import type { ConfigurationSource } from '../utils/configurationSource'
import { useConfigurationEditability } from '../utils/configurationEditability'
import { publishWorkspace, resolvePublishTask, type PublishTask } from '../utils/publishWorkspace'
import { publicationRecordDate } from '../utils/publishVideo'
import { createPublicationDiscovery, publicationObservationChanged } from '../utils/publicationDiscovery'

const props = defineProps<{ recordOnly?: boolean }>()
const route = useRoute(), router = useRouter(), message = useMessage(), dialog = useDialog()
const catalog = ref<any>({ accounts: [], templates: [], defaults: {} }), records = ref<any[]>([])
const catalogLoading = ref(false), catalogReady = ref(false), catalogError = ref('')
const recordsLoading = ref(false), recordsReady = ref(false), recordsError = ref('')
// 后台轮询保留已读取的列表，首次读取才展示占位，避免加载提示反复挤动卡片。
const recordsInitialLoading = computed(() => !recordsReady.value && (recordsLoading.value || !recordsError.value))
const current = ref<any>(null), draft = ref<any>(null), source = ref<any>(null)
const tab = ref(route.params.uid && route.hash === '#project-rules' ? 'rules' : 'drafts'), busy = ref(false), busyKind = ref<'read' | 'write' | ''>(''), error = ref(''), saved = ref(''), projectUid = ref(String(route.params.uid || ''))
// 阶段导航跟随当前页面项目，不跟随新建草稿的临时来源选择。
const stageUid = computed(() => String(route.params.uid || ''))
const { locked: projectLocked, reason: projectLockReason, resourceLocked: projectResourceLocked, resourceReason: projectResourceReason, canEdit: canEditProject, refresh: refreshProjectEditability, observing: editorObserving } = useConfigurationEditability(() => !props.recordOnly && current.value?.project_uid ? 'project:' + current.value.project_uid : '')
const { locked: creationLocked, reason: creationLockReason, resourceLocked: creationResourceLocked, resourceReason: creationResourceReason, canEdit: canCreateDraft, observing: creationObserving, executionActive: projectExecutionActive } = useConfigurationEditability(() => projectUid.value ? 'project:' + projectUid.value : '')
const receiptBV = ref('')
const confirming = ref(false)
const copying = ref(false)
const refreshingFiles = ref(false)
const sourceError = ref('')
const reviewDraftChecking = ref(false)
const settingsDirty = ref(false), settingsBusy = ref(false)
const copyBusy = ref(false)
const chapterBusy = ref(false)
const remoteCopyBusy = ref(false)
const remoteCopyValues = ref<Record<string, any>>({})
const remoteCopyFields = ref<string[]>([])
const remoteCopySubmitted = ref(false)
const remoteProjectCopyLoading = ref(false)
const remoteProjectCopyError = ref('')
const remoteSyncBusy = ref(false)
const originalUpdateBusy = ref(false), originalUpdateDirty = ref(false), originalUpdateActive = ref(false)
const taskChoice = ref<PublishTask | null>(null)
const reviewDirty = ref(false), reviewBusy = ref(false)
const coverLoading = ref(false), coverError = ref(''), coverBroken = ref(false)
const coverOperation = ref(''), preflighting = ref(false), savingDraft = ref(false)
const recordManaging=ref(false)
let coverGeneration = 0
let timer: ReturnType<typeof setTimeout> | undefined, disposed = false
let listRevision = 0
let catalogRevision = 0
let editorRevision = 0
let copyRequest: {publicationId:string, revision:number, requestId:string} | null = null
const discoveryContext = computed(() => !props.recordOnly && stageUid.value ? `project:${stageUid.value}` : '')
const knownProjectExecution = computed(() => !!discoveryContext.value && projectUid.value === stageUid.value && projectExecutionActive.value)
const publicationDiscovery = createPublicationDiscovery()
watch(() => [discoveryContext.value, knownProjectExecution.value] as const,
  ([context, active]) => publicationDiscovery.observe(context, active), { immediate: true, flush: 'sync' })
const dirty = computed(() => !!draft.value && JSON.stringify(draft.value) !== saved.value)
const recordInactive=computed(()=>!!current.value && (current.value.lifecycle ? current.value.lifecycle!=='active' : !!current.value.archived_at || !!current.value.deleted_at))
const locked = computed(() => props.recordOnly || recordInactive.value || recordManaging.value || projectResourceLocked.value || busy.value || current.value?.status !== 'draft')
const metadataLocked = computed(() => props.recordOnly || recordInactive.value || recordManaging.value || projectResourceLocked.value || busy.value || !['draft', 'uploaded'].includes(current.value?.status))
const lockReason = computed(() => projectResourceLocked.value ? (projectResourceReason.value || projectLockReason.value) : current.value?.status === 'unknown'
  ? '请先核对B站回执，暂不允许修改或复制，以免重复投稿。'
  : ['queued', 'uploading', 'paused', 'failed'].includes(current.value?.status)
    ? '上传清单已冻结；完成上传后可修改文案。若要更换文件或账号，请复制新草稿。'
  : '当前资料已锁定；已有BVID的文案可在下方“同步B站稿件资料”中生成候选并确认同步，替换原视频请使用“更新原稿件”；另发新BV可准备本地新草稿。')
// 发布草稿的账号、成片、封面和提交属于项目级资源操作；字段锁只用于
// 其它设置表单，不能让这些入口在资源占用期间继续发请求。
const projectActionLocked = computed(() => projectResourceLocked.value || projectLocked.value)
const originalUpdateBlockReason = computed(() => {
  const chapterStatus = current.value?.chapter_sync?.status
  if (chapterStatus === 'running') return '原视频章节正在处理，请等待完成后更新原稿件。'
  if (chapterStatus === 'unknown') return '上次章节写入结果待核对，请先在视频章节中核对平台结果。'
  if (projectActionLocked.value) return projectResourceReason.value || projectLockReason.value || '项目正在执行任务，请等待当前操作完成。'
  if (busy.value || remoteSyncBusy.value || remoteCopyBusy.value || reviewBusy.value || recordManaging.value) return '正在处理发布资料或审查，请等待当前操作完成。'
  if (dirty.value || remoteCopyDirty.value || reviewDirty.value) return '请先保存、同步或放弃当前修改，再准备原稿更新。'
  return ''
})
const creationActionLocked = computed(() => creationResourceLocked.value || creationLocked.value)
const resolved = computed(() => ({values:draft.value?{...current.value?.base_settings,...draft.value.overrides}:{}}))
const active = computed(() => PUBLISH_ACTIVE.includes(current.value?.status))
const isDraft = computed(() => current.value?.status === 'draft')
const canManageRemoteCopy = computed(() => !props.recordOnly && !!current.value?.receipt?.bvid
  && ['submitted','published','private','reviewed','rejected','collection_pending'].includes(current.value?.status)
  && !recordInactive.value)
const canReadRemoteChapters = computed(() => !!current.value?.receipt?.bvid && !!(current.value?.snapshot?.account_id || draft.value?.account_id)
  && ['submitted','published','private','reviewed','rejected','collection_pending'].includes(current.value?.status)
  && !recordInactive.value)
const remoteSyncValues = computed(() => ({ ...displayValues.value, ...remoteCopyValues.value }))
const remoteCopyDirty = computed(() => remoteCopyFields.value.length > 0 && !remoteCopySubmitted.value)
const editableMetadata = computed(() => !props.recordOnly && !recordInactive.value && ['draft','uploaded'].includes(current.value?.status))
const actionable = computed(() => !props.recordOnly && !recordInactive.value && ['draft','uploaded','paused','failed'].includes(current.value?.status))
const selectedNeedsReview = computed(() => isDraft.value && (draft.value?.parts || []).some((part:any) => source.value?.files?.some((file:any) => file.id === part.artifact_id && file.review_ready === false)))
const selectedFilesMissing = computed(() => isDraft.value && !!source.value && (draft.value?.parts || []).some((part:any) => !source.value.files?.some((file:any) => file.id === part.artifact_id && file.exists)))
const publishingStep = computed(() => current.value?.status === 'draft'
  ? !draft.value?.parts.length || !source.value || selectedFilesMissing.value || reviewDirty.value || selectedNeedsReview.value ? 1
    : dirty.value || !draft.value?.account_id || !displayValues.value.title?.trim() || !draft.value?.cover_id ? 2 : 3
  : ['uploaded','paused','failed'].includes(current.value?.status) ? 3 : 4)
const canCreateReviewDraft = computed(() => !props.recordOnly && !recordInactive.value && !!current.value?.receipt?.bvid && !!current.value?.snapshot?.parts?.length
  && !current.value?.snapshot?.update_target && current.value?.receipt?.update_kind !== 'append'
  && !active.value && !['draft', 'unknown'].includes(current.value?.status))
const workspace = computed(() => {
  const state = publishWorkspace(current.value, { recordOnly: props.recordOnly,
    originalUpdateActive: originalUpdateActive.value, originalUpdateDirty: originalUpdateDirty.value,
    remoteCopyDirty: remoteCopyDirty.value, preparationStep: publishingStep.value })
  // 导航只安排界面，不替代各操作原有的权限与预检。
  const tasks = state.tasks.filter(task => task.key !== 'sync' || canManageRemoteCopy.value)
  if (current.value?.lifecycle_actions || canCreateReviewDraft.value || !state.readOnly && !isDraft.value && current.value?.status !== 'unknown') {
    tasks.push({ key: 'more', label: '更多操作', description: '另发新稿、官网操作与本地记录管理' })
  }
  return { ...state, tasks }
})
const selectedTask = computed(() => resolvePublishTask(taskChoice.value, workspace.value))
const selectedTaskInfo = computed(() => workspace.value.tasks.find(task => task.key === selectedTask.value))
function selectTask(task: PublishTask) {
  // 同一稿件只切换可见区域，子组件保持挂载，候选与未保存表单继续保留。
  taskChoice.value = resolvePublishTask(task, workspace.value)
  void nextTick(() => {
    if (typeof document === 'undefined') return
    const navigation = document.querySelector<HTMLElement>('.publication-tasks')
    if (navigation && navigation.getBoundingClientRect().top < 0) navigation.scrollIntoView({ block: 'start' })
  })
}
function focusPublicationRecords() {
  const picker = document.querySelector<HTMLInputElement>('.mobile-record-controls input')
  if (picker && picker.getClientRects().length) { picker.focus(); picker.click(); return }
  const list = document.getElementById('publication-records')
  list?.scrollIntoView({ block: 'start' })
  list?.querySelector<HTMLButtonElement>('.publication-item')?.focus()
}
const anchorTasks: Record<string, PublishTask> = {
  'publish-video': 'video', 'publish-metadata': 'metadata', 'publish-confirm': 'confirm',
  'publish-receipt': 'status', 'publication-original-update': 'update',
  'publish-remote-sync': 'sync', 'confirmed-content': 'snapshot',
}
function taskForAnchor(hash: string): PublishTask | undefined {
  const requested = anchorTasks[hash.replace(/^#/, '')]
  if (!requested) return
  return workspace.value.tasks.some(task => task.key === requested) ? requested
    : ['video', 'metadata'].includes(requested) ? 'snapshot' : requested
}
function revealPublicationAnchor(event: MouseEvent) {
  const anchor = (event.target as Element | null)?.closest?.('a[href]') as HTMLAnchorElement | null
  if (!anchor) return
  const href = anchor.getAttribute('href') || ''
  const url = new URL(href, typeof window === 'undefined' ? 'http://localhost' + route.fullPath : window.location.href)
  // 只接管当前页面的任务锚点，跨项目和外部链接仍服从路由离开保护。
  if (url.pathname !== route.path || (url.search && url.search !== new URL('http://localhost' + route.fullPath).search)) return
  const task = taskForAnchor(url.hash)
  if (!task) return
  event.preventDefault()
  selectTask(task)
  void nextTick(() => {
    if (typeof document !== 'undefined') document.getElementById(url.hash.slice(1))?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  })
}
const reviewSource = computed(() => draft.value?.review_source)
const confirmationScope = computed(() => publicationScope({ account_name: current.value?.status === 'draft'
  ? catalog.value.accounts.find((account:any) => account.id === draft.value?.account_id)?.name || '尚未选择'
  : current.value?.snapshot?.account_name || '尚未选择', parts: draft.value?.parts || [], settings: displayValues.value }))
const preparationHint = computed(() => preflighting.value ? '正在执行发布预检：核对账号、视频文件与投稿资料…'
  : copyBusy.value ? '正在生成候选文案，请等待完成并核对结果'
  : reviewDirty.value || reviewBusy.value ? '请先保存审查设置，并等待审查完成'
  : coverLoading.value ? '封面仍在准备中，请稍候'
  : dirty.value ? '投稿资料有未保存修改，请先保存资料'
  : selectedNeedsReview.value ? '所选视频需按当前规则重新生成成片，完成后刷新视频列表并核对分P'
  : selectedFilesMissing.value ? '所选视频的本地文件缺失，请还原后刷新，或从分P清单移除'
  : !draft.value?.parts.length ? '先选择需要发布的视频版本'
  : !draft.value?.account_id ? '先选择发布账号'
  : isDraft.value && !source.value ? '视频文件信息尚未读取，请刷新视频列表后核对'
  : isDraft.value && !displayValues.value.title?.trim() ? '先填写稿件标题，再保存资料'
  : isDraft.value && !draft.value?.cover_id ? '先选择封面，再保存资料'
  : ['paused','failed'].includes(current.value?.status) ? '继续时沿用已确认的清单，复用已完成分P；更换视频请复制为新草稿'
  : '确认后按下方范围继续；上传和投稿由后台自动完成')
const submissionBlocked = computed(() => isDraft.value && (!source.value || selectedFilesMissing.value || selectedNeedsReview.value || !draft.value?.account_id || !displayValues.value.title?.trim() || !draft.value?.cover_id))
const hasAiDubbing = computed(() => current.value?.status === 'draft'
  ? !!draft.value?.parts.some((p:any) => source.value?.files.some((f:any) => f.id === p.artifact_id && f.has_dubbing))
  : !!current.value?.snapshot.parts?.some((p:any) => p.has_dubbing))
const accountOptions = computed(() => catalog.value.accounts.map((a: any) => ({ label: `${a.name} · ${a.id}`, value: a.id })))
const savedAccountSummary = computed(() => catalog.value.accounts.map((a: any) => a.name || `UID ${a.id}`).join('、'))
const recordOptions = computed(() => records.value.map((item:any) => ({value:item.id,label:`${item.snapshot?.settings?.title || item.draft?.overrides?.title || item.draft?.content?.title || '未命名稿件'} · ${PUBLICATION_STATUS[item.status] || item.status}`})))
const accountPlaceholder = computed(() => catalogLoading.value || !catalogReady.value && !catalogError.value ? '正在读取账号…'
  : catalogError.value ? '账号读取失败，请重新读取' : catalog.value.accounts.length ? '选择发布账号' : '先连接B站账号')
const emptyWorkspace = computed(() => {
  if (props.recordOnly) return { title:'发布记录', hint:'保留稿件快照、发布进度与平台回执；来源项目删除后仍可核对记录。' }
  if (catalogLoading.value || !catalogReady.value && !catalogError.value) return { title:'正在读取发布资料', hint:'正在读取已保存账号，请稍候。' }
  if (catalogError.value) return { title:'账号资料读取失败', hint:'暂不能确认最新账号列表，请重新读取；读取失败不代表账号已退出登录。' }
  if (!catalog.value.accounts.length) return { title:'先连接B站账号', hint:'在设置中连接账号，返回本项目新建发布草稿；新建草稿不会上传或投稿。' }
  if (recordsError.value) return { title:'发布记录读取失败', hint:'请重新读取发布记录，已有账号和草稿不会因此被删除。' }
  if (recordsInitialLoading.value) return { title:'正在读取发布记录', hint:'已保存账号资料，正在读取草稿与回执。' }
  if (knownProjectExecution.value) return { title:'项目正在后台执行', hint:'发布记录会自动刷新；可前往项目总览查看当前进度。' }
  if (records.value.length) return { title:'选择发布记录', hint:'从发布记录列表打开草稿、发布进度或回执；准备新稿时点击新建发布草稿。' }
  if (projectUid.value) return { title:'为所选项目新建草稿', hint:'新建本地草稿后，选择账号与视频版本，再确认发布。' }
  return { title:'准备下一条发布', hint:'先在左侧选择来源项目，再新建本地草稿。原声和配音版本可加入同一个稿件。' }
})
const displayValues = computed(() => current.value?.status === 'draft' ? resolved.value.values : current.value?.status === 'uploaded' ? { ...(current.value.snapshot.base_settings || current.value.snapshot.settings), ...draft.value.overrides } : current.value?.snapshot.settings || {})
const frozenCoverId = computed(() => String(current.value?.snapshot?.cover_path || '').split(/[\\/]/).pop() || current.value?.draft?.cover_id || '')
const recordedValues = computed(() => isDraft.value ? { ...draft.value?.content, ...draft.value?.overrides } : current.value?.snapshot?.settings || {})
const frozenFacts = computed(() => {
  const row=current.value, s=recordedValues.value
  const season = metadata.value?.seasons?.find((item:any) => item.id === s.season_id)
  const section = season?.sections?.find((item:any) => item.id === s.section_id)
  return [
    ['发布账号',row?.snapshot?.account_name || (isDraft.value && draft.value?.account_id ? `账号 UID ${draft.value.account_id}` : '原账号名称未记录')],
    ['稿件类型',({1:'原创',2:'转载',3:'其他创作类型'} as Record<number,string>)[s.copyright] || '未记录'],
    ['可见范围',s.visibility==='self'?'仅自己可见':s.visibility==='public'?'公开可见':'未记录'],
    ['发布时间',s.publish_at?new Date(s.publish_at*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):Object.hasOwn(s,'publish_at')?'立即提交审核':'未记录'],
    ['分区',s.human_type2?`分区 ID ${s.human_type2}`:'未记录'],
    ['创作声明',s.declaration_id===null?'未选择':s.declaration_id!==undefined?`声明 ID ${s.declaration_id}`:'未记录'],
    ['合集',s.season_id?collectionDisplay(s.season_id, season?.title, section?.title):Object.hasOwn(s,'season_id')?'不加入合集':'未记录'],
    ['B站水印',s.copyright===2?'转载不提交该选项':typeof s.watermark==='boolean'?s.watermark?'开启':'关闭':'未记录'],
    ['评论',typeof s.close_comments==='boolean'?s.close_comments?'关闭':'开放':'未记录'],
    ['弹幕',typeof s.close_danmaku==='boolean'?s.close_danmaku?'关闭':'开放':'未记录'],
    ['精选评论',typeof s.selected_comments==='boolean'?s.selected_comments?'开启':'关闭':'未记录'],
    ['话题',s.topic_name || (s.topic_id?`话题 ID ${s.topic_id}`:Object.hasOwn(s,'topic_id')?'未选用':'未记录')],
  ]
})
const displaySources = computed<Record<string, ConfigurationSource>>(() => {
  const row=current.value, snapshot=row?.snapshot
  const editable=row?.status==='draft'||row?.status==='uploaded'
  // 冻结稿件的来源不能用今天的项目设置倒推；未记录来源时如实显示为稿件快照。
  const recorded=row?.status==='draft'?row.base_sources:editable?snapshot?.base_sources:snapshot?.sources
  const result:Record<string,ConfigurationSource>=Object.fromEntries(Object.keys(displayValues.value).map(key=>[key,recorded?.[key]||{kind:row?.status==='draft'?'unknown':'snapshot'}]))
  if(editable)for(const key of Object.keys(draft.value?.overrides||{}))result[key]={kind:'publication'}
  return result
})
const clone = (value: any) => JSON.parse(JSON.stringify(value))
function copyBusyChanged(value:boolean) {
  const finished = copyBusy.value && !value; copyBusy.value = value
  // 成功、失败或取消等待都只重新读取权限；仍在后台生成时继续服从服务器锁。
  if (finished && !disposed && editorObserving.value) void refreshProjectEditability()
}
const metadataAccount = computed(() => editableMetadata.value && tab.value === 'drafts' ? String(draft.value?.account_id || '') : '')
const collectionContext = computed(() => `publication:${current.value?.id || ''}`)
const metadataAccountName = computed(() => catalog.value.accounts.find((a: any) => a.id === metadataAccount.value)?.name || '')
const { metadata, loading: metadataLoading, error: metadataError, ready: metadataReady, refresh: refreshMetadata, refreshAfterMutation } =
  usePublishMetadata(metadataAccount, account => api(`/accounts/${encodeURIComponent(account)}/metadata`, 'POST', {}))

async function collectionCreated(result: any) {
  message.success(`已在B站创建合集「${result.title}」，尚未加入视频`)
  if (result.account_id !== metadataAccount.value || result.context_key !== collectionContext.value) return
  const target = draft.value
  const selected = () => displayValues.value.season_id
  await refreshAfterMutation()
  if (disposed || result.account_id !== metadataAccount.value || result.context_key !== collectionContext.value
      || target !== draft.value
      || (selected() ?? null) !== result.previous_season) return
  const season = metadata.value.seasons?.find((s: any) => s.id === result.season_id)
  if (!season) { message.warning(`合集已创建（ID ${result.season_id}），列表暂未返回；请稍后刷新，不要重复创建`); return }
  if (tab.value === 'drafts' && metadataLocked.value) return
  const overrides = target.overrides
  overrides.season_id = season.id
  overrides.section_id = season.sections?.length === 1 ? season.sections[0].id : null
}

async function api(path: string, method = 'GET', body?: any) {
  let response: Response
  try {
    response = await fetch(`/api/publishing${path}`, { method,
      headers: body === undefined || body instanceof FormData ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
      signal: AbortSignal.timeout(30000) })
  } catch (error: any) {
    if (error?.name === 'TimeoutError') throw Object.assign(new Error('发布服务响应超时，请检查后端状态后重试'), { code: 'PUBLISH_REQUEST_TIMEOUT' })
    throw error
  }
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw Object.assign(new Error(data.message || '发布操作失败'), { code: data.code, detail: data.detail })
  return data
}
async function reviewDraftRequest(path:string,body:any) {
  try { return await api(path,'POST',body) }
  catch(e:any) {
    if(e.code==='PUBLISH_REVIEW_MAPPING' && Array.isArray(e.detail?.parts)) {
      const reasons=e.detail.parts.filter((part:any)=>Number.isInteger(part?.index) && part.index>0 && typeof part.reason==='string' && part.reason.trim())
        .map((part:any)=>`P${part.index}${typeof part.title==='string' && part.title.trim() ? ` · ${part.title}` : ''}：${part.reason}`)
      if(reasons.length)e.message+=`。${reasons.join('；')}`
    }
    throw e
  }
}
async function run(action: () => Promise<any>, kind: 'read' | 'write' = 'write') {
  if (busy.value) return
  const previousRecord = current.value?.id
  busy.value = true; busyKind.value = kind; error.value = ''
  try { return await action() } catch (e: any) { error.value = e.message; message.error(e.message) }
  finally {
    busy.value = false; busyKind.value = ''
    // 新建、复制和审查修订得到新记录后，同步地址而不重新装载已打开的表单。
    if (kind === 'write' && !props.recordOnly && !disposed && current.value?.id && current.value.id !== previousRecord) {
      await router.replace({ query: { ...route.query, publication: current.value.id }, hash: '' })
    }
  }
}
/**
 * 确认对话框的关闭回调并不保证每条路径都会触发 onAfterLeave（例如遮罩、Esc、
 * 外部 destroy 或测试/恢复流程直接调用 onClose）。统一包一层，避免 confirming
 * 留在 true 导致整个发布页一直被判定为“确认发布中”。正向确认不在点击时清锁，
 * 让上传/投稿请求先接管 busy；请求失败或对话框关闭时再由关闭回调清理。
 */
function openConfirmation(factory: (options: any) => any, options: any) {
  const { onAfterLeave, onClose, onNegativeClick, onMaskClick, ...rest } = options
  const clear = () => { confirming.value = false }
  try {
    const handle = factory({
      ...rest,
      onAfterLeave: (...args: any[]) => { clear(); return onAfterLeave?.(...args) },
      onClose: (...args: any[]) => { clear(); return onClose?.(...args) },
      onNegativeClick: (...args: any[]) => {
        try {
          const result = onNegativeClick?.(...args)
          if (result !== false) clear()
          return result
        } catch (error) {
          clear()
          throw error
        }
      },
      onMaskClick: (...args: any[]) => {
        try {
          const result = onMaskClick?.(...args)
          if (result !== false) clear()
          return result
        } catch (error) {
          clear()
          throw error
        }
      },
    })
    // Provider 未挂载或被外部销毁时没有可等待的对话框，不能留下全页锁。
    if (!handle) clear()
    return handle
  } catch (error) {
    clear()
    throw error
  }
}
async function refreshList() {
  const revision = ++listRevision, context = discoveryContext.value, recordId = String(route.params.id || '')
  recordsLoading.value = true
  try {
    const data = props.recordOnly ? [await api(`/publications/${encodeURIComponent(String(route.params.id))}`)]
      : await api(`/publications${route.params.uid ? '?project_uid=' + encodeURIComponent(String(route.params.uid)) : ''}`)
    if (!Array.isArray(data)) throw new Error('发布记录格式无效，请重新读取')
    if (!disposed && revision === listRevision && context === discoveryContext.value && recordId === String(route.params.id || '')) {
      records.value = data; recordsReady.value = true; recordsError.value = ''; return data
    }
    return null
  } catch (e: any) {
    if (!disposed && revision === listRevision && context === discoveryContext.value && recordId === String(route.params.id || '')) recordsError.value = e.message || '发布记录读取失败'
    throw e
  } finally { if (!disposed && revision === listRevision) recordsLoading.value = false }
}
function recordManagementBusy(value:boolean) {recordManaging.value=value;busy.value=value;busyKind.value=value?'write':''}
async function lifecycleChanged(row:any) { recordManaging.value=false;busy.value=false;busyKind.value='';apply(row);syncList();try {await refreshList()} catch {} }
function applyCatalog(data: any) {
  if (!Array.isArray(data?.accounts)) throw new Error('账号列表格式无效，请重新读取')
  // 登录成功后的目录更新也使先前的慢读取失效。
  catalogRevision++; catalogLoading.value = false
  catalog.value = data; catalogReady.value = true; catalogError.value = ''
}
async function refreshCatalog() {
  if (catalogLoading.value || disposed) return
  const revision = ++catalogRevision
  catalogLoading.value = true
  try {
    const data = await api('/catalog')
    if (!disposed && revision === catalogRevision) applyCatalog(data)
  } catch (e: any) {
    if (!disposed && revision === catalogRevision) catalogError.value = e.message || '账号资料读取失败'
  } finally { if (!disposed && revision === catalogRevision) catalogLoading.value = false }
}
async function rereadRecords() { await run(refreshList, 'read') }
function syncList() {
  refreshInBackground(refreshList, () => { if (!disposed) message.warning('操作已完成，但队列列表刷新失败，请稍后刷新') })
}
function apply(record: any) {
  const changedRecord = current.value?.id !== record.id
  // 写回执立即呈现；先前发出的列表请求不能用旧状态覆盖它。
  listRevision++
  recordsLoading.value = false
  records.value = records.value.some(r => r.id === record.id)
    ? records.value.map(r => r.id === record.id ? record : r) : [record, ...records.value]
  coverGeneration++; coverLoading.value = false; coverError.value = ''; coverBroken.value = false
  current.value = record; draft.value = clone(record.draft); saved.value = JSON.stringify(draft.value)
  remoteCopyValues.value = {}
  remoteCopyFields.value = []
  remoteCopySubmitted.value = false
  remoteProjectCopyError.value = ''
  remoteSyncBusy.value = false
  if (changedRecord) taskChoice.value = resolvePublishTask(taskForAnchor(route.hash), workspace.value)
  else taskChoice.value = resolvePublishTask(taskChoice.value, workspace.value)
}
function applyRemoteCopy(values: Record<string, any>) {
  if (originalUpdateBusy.value || originalUpdateActive.value || originalUpdateDirty.value) return
  remoteCopyValues.value = { ...remoteCopyValues.value, ...values }
  remoteCopyFields.value = [...new Set([...remoteCopyFields.value, ...Object.keys(values)])]
  remoteCopySubmitted.value = false
  remoteProjectCopyError.value = ''
}
const remoteCopyLabels: Record<string, string> = { title: '标题', description: '简介', tags: '标签', dynamic: '粉丝动态' }
async function loadSavedProjectCopy() {
  if (originalUpdateBusy.value || originalUpdateActive.value || originalUpdateDirty.value || remoteProjectCopyLoading.value || !canManageRemoteCopy.value || !current.value?.project_uid) return
  const id = current.value.id, uid = current.value.project_uid
  remoteProjectCopyLoading.value = true; remoteProjectCopyError.value = ''
  try {
    const data = await api(`/projects/${encodeURIComponent(uid)}/copy-preview`)
    if (disposed || current.value?.id !== id) return
    const keys = ['title', 'description', 'tags', 'dynamic'].filter(key =>
      Array.isArray(data?.configured) && data.configured.includes(`publish.${key}`))
    if (!keys.length) {
      remoteProjectCopyError.value = '项目发布设置中没有已保存的文案覆盖；请先在“项目发布设置”应用候选并保存。'
      return
    }
    remoteCopyValues.value = Object.fromEntries(keys.map(key => [key,
      key === 'tags' ? (Array.isArray(data.values?.[key]) ? data.values[key] : []) : (data.values?.[key] ?? '')]))
    remoteCopyFields.value = keys
    remoteCopySubmitted.value = false
    message.success(`已载入项目文案：${keys.map(key => remoteCopyLabels[key]).join('、')}；请读取B站资料并预览同步`)
  } catch (e: any) {
    if (!disposed && current.value?.id === id) remoteProjectCopyError.value = e.message || '项目文案读取失败，请重试'
  } finally { if (!disposed && current.value?.id === id) remoteProjectCopyLoading.value = false }
}
function clearRemoteCopy() {
  if (originalUpdateBusy.value || originalUpdateActive.value || originalUpdateDirty.value || remoteCopyBusy.value || remoteSyncBusy.value) return
  remoteCopyValues.value = {}
  remoteCopyFields.value = []
  remoteCopySubmitted.value = false
  remoteProjectCopyError.value = ''
}
function remoteCopyBusyChanged(value: boolean) {
  remoteCopyBusy.value = value
  if (value) remoteSyncBusy.value = true
  else if (!value) remoteSyncBusy.value = false
}
function remoteSyncBusyChanged(value: boolean) { remoteSyncBusy.value = value }
function remoteCopySynced() { remoteCopySubmitted.value = true }
watch(() => route.hash, hash => {
  const task = taskForAnchor(hash)
  if (task && current.value) selectTask(task)
}, { flush: 'post' })
async function originalUpdateChanged() {
  const id = current.value?.id
  try { await refreshList() } catch { return }
  const row = records.value.find(item => item.id === id)
  if (!disposed && row && current.value?.id === id && !dirty.value && !remoteCopyDirty.value && !reviewDirty.value) {
    // 只刷新原稿平台观察状态，冻结的历史文案和当前表单继续保留。
    current.value = { ...current.value, status: row.status, receipt: row.receipt, error: row.error,
      revision: row.revision, latest_update: row.latest_update }
  }
}
async function openAutomaticOriginalUpdate(){
  if(originalUpdateBlockReason.value || originalUpdateActive.value || originalUpdateDirty.value || !current.value?.project_uid || !canSwitch())return
  tab.value='rules'
  await router.replace({path:`/projects/${current.value.project_uid}/publish`,query:{publication:current.value.id},hash:'#project-rules'})
  await nextTick()
  if(typeof document!=='undefined')document.getElementById('project-delivery-settings')?.scrollIntoView({block:'start'})
}
const writeBusy = computed(() => busy.value && busyKind.value !== 'read')
function canSwitch() { if (originalUpdateBusy.value || writeBusy.value || copyBusy.value || chapterBusy.value || remoteCopyBusy.value || remoteSyncBusy.value || confirming.value || coverLoading.value || reviewBusy.value || settingsBusy.value) return false; if (originalUpdateDirty.value || dirty.value || reviewDirty.value || settingsDirty.value || remoteCopyDirty.value) { message.warning(remoteCopyDirty.value ? '已有待同步的项目文案，请先同步、清除或完成当前操作' : '请先保存/放弃当前修改，并等待当前操作完成，再切换页面'); return false }; return true }
function switchWorkspace(value:string) { if(['rules','drafts'].includes(value) && canSwitch()) tab.value=value }
async function manageAccount(studio = false) {
  if (!canSwitch()) return
  const account = draft.value?.account_id || current.value?.snapshot?.account_id
  const bvid = studio ? String(current.value?.receipt?.bvid || '').trim() : ''
  await router.push({ path:'/settings', query:{ section:'bilibili', ...(account ? { account } : {}), ...(studio ? { manage:'studio', ...(projectUid.value ? { project:projectUid.value } : {}), ...(bvid ? { bvid } : {}) } : {}) } })
}
async function studioRefresh() { await refreshCatalog();await refreshList();if(current.value && !dirty.value)apply(await api(`/publications/${current.value.id}`)) }
function discoveryCanOpen(context: string) {
  return !disposed && context === discoveryContext.value && !current.value && !draft.value && tab.value === 'drafts'
    && !Object.hasOwn(route.query, 'publication') && !originalUpdateDirty.value && !dirty.value && !reviewDirty.value && !settingsDirty.value && !remoteCopyDirty.value
    && !writeBusy.value && !originalUpdateBusy.value && !copyBusy.value && !chapterBusy.value && !remoteCopyBusy.value && !remoteSyncBusy.value
    && !confirming.value && !coverLoading.value && !reviewBusy.value && !settingsBusy.value && !recordManaging.value
}
async function open(id: string, options?: { discoveryContext?: string }) {
  if (!canSwitch()) return
  const automaticContext = options?.discoveryContext
  if (automaticContext && !discoveryCanOpen(automaticContext)) return
  const revision = ++editorRevision, uid = String(route.params.uid || '')
  const directLink = route.query.publication === id || !current.value
  const read = async () => {
    const record = await api(`/publications/${id}`)
    if (disposed || revision !== editorRevision || uid !== String(route.params.uid || '')
      || automaticContext && !discoveryCanOpen(automaticContext)) return
    if (uid && record.project_uid !== uid) throw new Error('这条发布记录属于其他项目，请从全部发布记录打开')
    apply(record); source.value = null; sourceError.value=''; tab.value = 'drafts'
    if (!directLink) taskChoice.value = workspace.value.defaultTask
    // 已确认记录直接读取冻结清单，避免反复扫描当前成片，也不以当前文件状态倒推历史。
    if (!editableMetadata.value) return
    if (props.recordOnly) return
    try {
      const candidates = await api(`/projects/${record.project_uid}/candidates`)
      if (!disposed && revision === editorRevision && current.value?.id === id) source.value = candidates
    } catch { if (!disposed && revision === editorRevision) {sourceError.value='视频列表暂未读取成功，已选分P仍保留，请刷新后核对。';error.value = '来源项目不可用；已保存的发布回执仍可查看。'} }
  }
  // 自动发现只读详情不占整页操作锁；用户选择可立即抢占，旧详情由代次检查丢弃。
  if (automaticContext) {
    try { await read() }
    catch (failure) {
      if (disposed || revision !== editorRevision || uid !== String(route.params.uid || '') || !discoveryCanOpen(automaticContext)) return
      throw failure
    }
  }
  else await run(read, 'read')
  // 记录选择与地址同步，刷新或复制链接时不会回到之前的稿件。
  if (!props.recordOnly && !disposed && revision === editorRevision && current.value?.id === id && route.query.publication !== id) {
    await router.replace({ query: { ...route.query, publication: id }, hash: directLink ? route.hash : '' })
  }
}
async function create() {
  if (props.recordOnly || !projectUid.value || creationActionLocked.value || !canSwitch()) return
  const uid = projectUid.value
  if (creationObserving.value && !(await canCreateDraft()) || projectUid.value !== uid || !canSwitch()) return
  await run(async () => {
    const record = await api(`/projects/${uid}/drafts`, 'POST', {})
    // 本地创建成功立即保留入口，后续资料读取失败时可直接打开该草稿。
    listRevision++; recordsLoading.value = false
    records.value = [record, ...records.value.filter(row => row.id !== record.id)]; recordsReady.value = true
    syncList()
    try { apply(await api(`/publications/${record.id}`)) }
    catch (e:any) { throw new Error(`草稿已创建，请从左侧记录重新打开；详情读取失败：${e.message}`) }
    source.value = null;sourceError.value=''
    try { source.value = await api(`/projects/${uid}/candidates`) }
    catch { sourceError.value='成片列表暂未读取成功，请刷新视频列表。';error.value = '草稿已创建；成片列表暂未读取成功，请刷新文件列表。' }
  })
}
async function save() {
  if (copyBusy.value || chapterBusy.value || coverLoading.value || metadataLocked.value || !current.value) return
  const key = current.value.id
  if (editorObserving.value && !(await canEditProject()) || copyBusy.value || current.value?.id !== key || metadataLocked.value) return
  savingDraft.value = true
  try { await run(async () => { apply(await api(`/publications/${key}`, 'PUT', { ...draft.value, revision: current.value.revision })); syncList(); message.success('资料已保存，未触发投稿') }) }
  finally {savingDraft.value = false}
}
function change(key: string, value: any) { if (!metadataLocked.value) draft.value.overrides[key] = value }
function changeChapters(value: any) { if (!metadataLocked.value) draft.value.chapter_options = value }
function inherit(key: string) { if (!metadataLocked.value) delete draft.value.overrides[key] }
function selectAccount(value: string) { if (locked.value) return; draft.value.account_id = value || ''; draft.value.template_id = '' }
function addPart(file: any, selected: boolean) {
  if (locked.value || !draft.value) return
  if (selected) {
    if (!file?.exists || draft.value.parts.some((part:any) => part.artifact_id === file.id)) return
    draft.value.parts.push({ artifact_id: file.id, title: file.is_source ? '原声原片' : file.version_name || finalVideoLabel(file.name).split(' · ').slice(0, 2).join(' · ') })
  }
  else draft.value.parts = draft.value.parts.filter((p: any) => p.artifact_id !== file.id)
}
function renamePart(index:number,value:string) { if(!locked.value && draft.value?.parts[index])draft.value.parts[index].title=value }
function reorderPart(index:number,delta:number) { if(!locked.value && draft.value)draft.value.parts=movePart(draft.value.parts,index,delta) }
function removePart(index:number) { if(!locked.value && draft.value?.parts[index])draft.value.parts.splice(index,1) }
function discardDraft() { if(!current.value || busy.value || copyBusy.value || confirming.value || coverLoading.value)return;apply(current.value) }
async function refreshFiles() {
  if (!current.value || refreshingFiles.value) return
  const id = current.value.id, uid = current.value.project_uid
  refreshingFiles.value = true
  try {
    const data = await api(`/projects/${uid}/candidates`)
    if (!disposed && current.value?.id === id) { source.value = data; sourceError.value=''; message.success('已刷新文件状态，分P选择与未保存资料保持不变') }
  } catch (e: any) { if (!disposed && current.value?.id === id) {sourceError.value=e.message || '视频列表读取失败，请重试';message.error(sourceError.value)} }
  finally { refreshingFiles.value = false }
}
async function cover(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  const data = new FormData(); data.append('file', file)
  await setCover(() => api('/covers', 'POST', data), '正在上传并检查封面…')
  input.value = ''
}
async function setCover(request: () => Promise<any>, label = '正在准备封面…') {
  if (locked.value || coverLoading.value) return
  const target = draft.value, id = current.value.id
  if (editorObserving.value && !(await canEditProject()) || locked.value || coverLoading.value || current.value?.id !== id || draft.value !== target || disposed) return
  const ticket = ++coverGeneration
  coverLoading.value = true; coverOperation.value = label; coverError.value = ''
  try {
    const result = await request()
    if (disposed || ticket !== coverGeneration || current.value?.id !== id || draft.value !== target) return
    if (editorObserving.value && !(await canEditProject())) { coverError.value = projectLockReason.value || '项目正在运行，封面候选尚未应用'; return }
    if (!disposed && ticket === coverGeneration && current.value?.id === id && draft.value === target && !projectActionLocked.value) {
      target.cover_id = result.cover_id; coverBroken.value = false
    }
  } catch (e: any) {
    if (!disposed && ticket === coverGeneration) coverError.value = e.message || '封面设置失败，请重试'
  } finally { if (!disposed && ticket === coverGeneration) coverLoading.value = false }
}
async function importCover() {
  const uid = current.value?.project_uid
  if (uid) await setCover(() => api(`/projects/${uid}/platform-cover`, 'POST', {}), '正在读取并检查平台封面…')
}
async function publicationPlan(key:string) {
  preflighting.value = true
  try {return await api(`/publications/${key}/plan`, 'POST', {})}
  finally {preflighting.value = false}
}
async function upload(andSubmit = false) {
  if (!['draft','paused','failed'].includes(current.value?.status)) return
  if (props.recordOnly || copyBusy.value || chapterBusy.value || projectActionLocked.value || confirming.value || busy.value || coverLoading.value || disposed) return
  if (editorObserving.value && !(await canEditProject()) || copyBusy.value || projectActionLocked.value || busy.value || confirming.value || disposed) return
  if (reviewDirty.value || reviewBusy.value) { message.warning('请先保存或放弃审查设置，并等待审查完成'); return }
  if (dirty.value) { message.warning('请先保存草稿'); return }
  await run(async () => {
    const key = current.value.id
    const preview = await publicationPlan(key)
    if (disposed || current.value?.id !== key) return
    confirming.value = true
    const aiNotice = publicationAiNotice(preview, metadata.value)
    let accepted = false
    const confirm = andSubmit || aiNotice ? dialog.warning : dialog.info
    openConfirmation(confirm, { title: andSubmit ? '确认上传并投稿到B站' : '确认仅上传所选文件',
      content: (andSubmit ? `${publicationScope(preview.snapshot)}。标题「${preview.snapshot.settings.title}」。全部文件上传成功后将自动正式投稿，不再二次询问；定时发布与可见范围保持上述设置。关闭页面不停止后台任务。`
        : `只上传 ${preview.snapshot.parts.length} 个分P及封面至「${preview.snapshot.account_name}」，不会正式投稿。适合稍后投稿或追加到已有BV；上传后文字资料仍可修改。`) + (aiNotice ? `

${aiNotice}` : ''),
      positiveText: andSubmit ? '确认上传并投稿' : '仅上传，不投稿', negativeText: '取消',
      onPositiveClick: () => {
        if (accepted || copyBusy.value || projectActionLocked.value || busy.value || disposed || current.value?.id !== key) return false
        if (dirty.value || reviewDirty.value || reviewBusy.value) { message.warning('资料或审查设置已修改，请先保存并重新确认'); return false }
        accepted = true
        return run(async () => { if (editorObserving.value && !(await canEditProject())) throw new Error(projectLockReason.value || '项目正在运行，请完成后重新核对发布范围'); apply(await api(`/publications/${key}/actions`, 'POST', {
          action: andSubmit ? 'publish' : 'upload', revision: preview.revision, fingerprint: preview.fingerprint,
          ...(andSubmit ? {confirmed:true} : {}),
          ...(aiNotice ? {acknowledge_ai_notice:true} : {}),
        })); syncList() })
      } })
  })
}
async function submit() {
  if (current.value?.status !== 'uploaded') return
  if (props.recordOnly || copyBusy.value || chapterBusy.value || projectActionLocked.value || confirming.value || busy.value || dirty.value || reviewDirty.value || reviewBusy.value || disposed) return
  if (editorObserving.value && !(await canEditProject()) || copyBusy.value || projectActionLocked.value || busy.value || confirming.value || disposed) return
  await run(async () => {
    const key = current.value.id
    const preview = await publicationPlan(key)
    if (disposed || current.value?.id !== key) return
    const aiNotice = publicationAiNotice(preview, metadata.value)
    confirming.value = true
    let accepted = false
    openConfirmation(dialog.warning, { title: '确认正式投稿到B站', content: publicationScope(preview.snapshot) + (aiNotice ? `

${aiNotice}` : ''), positiveText: '确认投稿', negativeText: '取消',
      onPositiveClick: () => {
        if (accepted || copyBusy.value || projectActionLocked.value || busy.value || disposed || current.value?.id !== key) return false
        if (dirty.value || reviewDirty.value || reviewBusy.value) { message.warning('资料或审查设置已修改，请先保存并重新确认'); return false }
        accepted = true
        return run(async () => { if (editorObserving.value && !(await canEditProject())) throw new Error(projectLockReason.value || '项目正在运行，请完成后重新核对发布范围'); apply(await api(`/publications/${key}/actions`, 'POST', {
          action:'submit', revision:preview.revision, fingerprint:preview.fingerprint, confirmed:true,
          ...(aiNotice ? {acknowledge_ai_notice:true} : {}),
        })); syncList() })
      } })
  })
}
async function action(name: string) {
  if (props.recordOnly && name !== 'check') return
  await run(async () => { apply(await api(`/publications/${current.value.id}/actions`, 'POST', { action: name, revision: current.value.revision })); syncList() }, name === 'check' ? 'read' : 'write')
}
async function chapterAction(kind: 'retry' | 'reconcile' | 'refresh', enable = false) {
  if (!current.value || disposed || busy.value || confirming.value || active.value || copyBusy.value || chapterBusy.value
    || remoteCopyBusy.value || remoteSyncBusy.value || dirty.value || remoteCopyDirty.value || reviewDirty.value || reviewBusy.value) return
  const key = current.value.id, state = publicationChapterState(current.value)
  if (!current.value.receipt?.bvid) return
  const perform = async () => {
    const body = kind === 'reconcile' ? { confirmed: true, reconcile_only: true }
      : { confirmed: true, ...(enable ? { enable: true } : {}) }
    const row = kind === 'refresh' ? await api(`/publications/${key}`)
      : await api(`/publications/${key}/chapters/retry`, 'POST', body)
    if (!disposed && current.value?.id === key) { apply(row); syncList() }
  }
  if (kind === 'refresh') { await run(perform, 'read'); return }
  if (kind === 'reconcile') { if (state.reconcile) await run(perform, 'read'); return }
  if (props.recordOnly || recordInactive.value || projectActionLocked.value || state.reconcile
    || !(state.retry || enable && state.supplement)
    || !['published','private','reviewed'].includes(current.value.status)) return
  const revision = current.value.revision
  const chapterOptions = current.value.snapshot?.chapter_options || {}
  const chapterCount = Array.isArray(chapterOptions.items) ? chapterOptions.items.length : 0
  const partCount = current.value.snapshot?.parts?.length || 0
  const chapterScope = `${chapterCount} 条源章节 · ${partCount} 个分P · ${chapterOptions.translate === false ? '保留原语言并整理标题' : '自动翻译并精简中文标题'} · 按平台规则合并至每P最多10章 · ${chapterOptions.permanent === false ? '不设置App默认展示' : 'App默认展示章节'}`
  confirming.value = true
  let accepted = false
  openConfirmation(dialog.warning, {
    title: enable ? '补充已投稿视频的章节' : '重试设置视频章节',
    content: `当前BV ${current.value.receipt.bvid}：${chapterScope}。按各分P成片剪辑调整时间；源视频没有章节时跳过。不会重新上传视频或重新投稿。`,
    positiveText: '确认设置章节', negativeText: '取消',
    onPositiveClick: async () => {
      const latestState = publicationChapterState(current.value)
      if (accepted || disposed || busy.value || projectActionLocked.value || current.value?.id !== key
        || current.value.revision !== revision || dirty.value || remoteCopyDirty.value || reviewDirty.value || reviewBusy.value
        || remoteCopyBusy.value || remoteSyncBusy.value
        || !['published','private','reviewed'].includes(current.value.status)
        || latestState.reconcile || !(latestState.retry || enable && latestState.supplement)) return false
      accepted = true
      return run(perform)
    },
  })
}
async function pause() {
  if (props.recordOnly || busy.value || disposed || !['queued','uploading'].includes(current.value?.status)) return
  await run(async () => { await api(`/publications/${current.value.id}/pause`, 'POST', {}); message.info('正在停止上传，不会删除B站稿件') })
}
async function copy() {
  if (props.recordOnly || !current.value || projectActionLocked.value || disposed || copying.value || active.value || ['draft','unknown'].includes(current.value.status) || !canSwitch()) return
  const key = current.value.id, revision = current.value.revision
  if (editorObserving.value && !(await canEditProject()) || current.value?.id !== key || copying.value || !canSwitch()) return
  if (!copyRequest || copyRequest.publicationId !== key || copyRequest.revision !== revision)
    copyRequest = {publicationId:key,revision,requestId:crypto.randomUUID()}
  const request = copyRequest
  copying.value = true
  try {
    await run(async () => {
      const row = await api(`/publications/${key}/copy`, 'POST', {revision:request.revision,request_id:request.requestId})
      if (disposed || current.value?.id !== key) return
      // 写操作一次返回可编辑详情；列表慢或刷新失败都不耽误打开新草稿。
      apply(row);copyRequest = null;syncList()
      message.success(row.reused ? '已打开本次复制的草稿，未上传或投稿' : '已复制为本地新草稿，未上传或投稿')
    })
  } finally { copying.value = false }
}
async function createReviewDraft() {
  if(!canCreateReviewDraft.value || projectActionLocked.value || busy.value || confirming.value || coverLoading.value || disposed) return
  if(dirty.value || reviewDirty.value || reviewBusy.value) { message.warning('请先保存或放弃当前资料与审查设置'); return }
  await run(async()=>{
    const key=current.value.id
    let plan:any
    reviewDraftChecking.value=true
    try { plan=await reviewDraftRequest(`/publications/${key}/review-draft/plan`,{}) }
    finally { reviewDraftChecking.value=false }
    if(disposed || current.value?.id!==key) return
    if(dirty.value || reviewDirty.value || reviewBusy.value) { message.warning('当前资料或审查设置已变化，请保存后重新预览修订草稿'); return }
    if(plan.mode!=='new_submission' || plan.source?.publication_id!==key || !Number.isInteger(plan.source?.revision)
      || typeof plan.fingerprint!=='string' || !plan.fingerprint || !Array.isArray(plan.parts) || !plan.parts.length) throw new Error('修订草稿预览不完整，请刷新后重试')
    const requestId=crypto.randomUUID()
    // 只去除已在摘要中完整说明的固定提示；新增警告仍逐条展示。
    const coveredNotices=new Set([
      '这会创建新投稿并获得新BV；原稿件、审核记录和已发布快照保持保留。',
      '创建草稿只保存到本地；上传与正式投稿仍需执行发布动作。',
      '本版创作管理可修改文案、排序或追加分P；替换原BV的视频请前往B站创作中心。',
      '保留原BV替换视频，请使用本页「更新原稿件」。',
    ])
    confirming.value=true
    let accepted=false
    openConfirmation(dialog.warning,{title:'使用审查修订版新建草稿',style:{width:'min(680px, calc(100vw - 40px))'},positiveText:'创建本地修订草稿',negativeText:'取消',
      content:()=>h('div',{style:{maxHeight:'60vh',overflow:'auto',fontSize:'12px',lineHeight:'1.8'}},[
        h('p',`账号「${plan.account_name}」 · ${plan.parts.length} 个修订分P · 原BV：${plan.source.bvid}。只创建本地草稿，不上传、不投稿；保留原稿账号、封面、冻结文案及分P顺序和名称。随后可点击「上传并投稿」，确认后后台自动完成；发布时将创建新BV，原BV及其视频保持原状。保留原BV替换视频，请取消本次新稿准备，使用本页「更新原稿件」。`),
        h('ol',plan.parts.map((part:any)=>h('li',{style:{marginBottom:'12px',overflowWrap:'anywhere'}},[
          h('strong',`P${part.index} · ${part.title}`),h('div',`原文件：${part.source_name}`),h('div',`修订版本：${part.version_name || part.name}`),
        ]))),
        ...(Array.isArray(plan.notices)?plan.notices:[]).filter((notice:any)=>typeof notice==='string' && !coveredNotices.has(notice)).map((notice:string)=>h('p',notice)),
      ]),
      onPositiveClick:()=>{
        if(accepted || projectActionLocked.value || busy.value || disposed || current.value?.id!==key) return false
        if(dirty.value || reviewDirty.value || reviewBusy.value || current.value.revision!==plan.source.revision) { message.warning('当前资料或审查设置已变化，请重新预览修订草稿');return false }
        accepted=true
        return run(async()=>{
          if (editorObserving.value && !(await canEditProject())) throw new Error(projectLockReason.value || '项目正在运行，请完成后重新核对修订草稿')
          const created=await reviewDraftRequest(`/publications/${key}/review-draft`,{revision:plan.source.revision,fingerprint:plan.fingerprint,request_id:requestId})
          if(disposed) return
          apply(created);source.value=null;sourceError.value='';tab.value='drafts';syncList();refreshInBackground(refreshFiles)
          message.success(created.reused?'已打开已有的本地修订草稿，未上传或投稿':'已创建本地修订草稿，未上传或投稿')
        })
      }})
  })
}
async function attachReceipt() {
  if (busy.value || confirming.value || disposed || current.value?.status !== 'unknown' || !receiptBV.value.trim()) return
  const key=current.value.id, bvid=receiptBV.value.trim()
  await run(async () => { const record=await api(`/publications/${key}/receipt`, 'POST', { bvid }); if (!disposed && current.value?.id === key) { apply(record); syncList() } })
}
function receiptReadbackAllowed() {
  return !busy.value && !dirty.value && !remoteCopyDirty.value && !originalUpdateBusy.value && !originalUpdateDirty.value
    && !reviewDirty.value && !reviewBusy.value && !settingsDirty.value && !settingsBusy.value && !remoteCopyBusy.value && !remoteSyncBusy.value
}
async function refreshCurrentReceipt(before: any) {
  let row: any
  try { row = await api(`/publications/${before.id}`) }
  catch (failure) {
    if (disposed || !receiptReadbackAllowed() || current.value !== before) return
    throw failure
  }
  // 本地写回或切换记录后，迟到的旧读取与旧错误都不能覆盖新回执。
  if (!disposed && receiptReadbackAllowed() && current.value === before) apply(row)
}
async function poll() {
  const context = discoveryContext.value
  publicationDiscovery.observe(context, knownProjectExecution.value)
  try {
    if (!busy.value) {
      const discovery = publicationDiscovery.read()
      if (current.value && (publicationReadbackPending(current.value.status) || publicationChapterState(current.value).active) && receiptReadbackAllowed()) {
        await refreshCurrentReceipt(current.value)
      }
      if (!recordsLoading.value && (discovery || records.value.some(r => publicationReadbackPending(r.status) || publicationChapterState(r).active))) {
        const previousIds = new Set(records.value.map(row => row.id)), editor = editorRevision
        // 发现读取失败由记录列表原位说明并重试，不留下恢复后仍显示的重复顶部错误。
        const rows = await refreshList().catch(failure => { if (!discovery) throw failure; return null })
        if (rows && current.value && receiptReadbackAllowed()
          && publicationObservationChanged(current.value, rows.find(row => row.id === current.value.id))) {
          await refreshCurrentReceipt(current.value)
        }
        if (rows && discovery) {
          publicationDiscovery.complete(discovery)
          const discovered = rows.find(row => typeof row.id === 'string' && row.id && !previousIds.has(row.id)
            && row.project_uid === stageUid.value && (!row.lifecycle || row.lifecycle === 'active') && !row.archived_at && !row.deleted_at)
          if (discovered && editor === editorRevision && discoveryCanOpen(discovery.context)) await open(discovered.id, { discoveryContext: discovery.context })
        }
      }
    }
  } catch (e: any) { if (!disposed && context === discoveryContext.value) error.value = e.message }
  finally { if (!disposed) timer = setTimeout(poll, 4000) }
}
function beforeUnload(event: BeforeUnloadEvent) {
  // 浏览器刷新/关闭也要保护正在写入或确认的投稿；只读请求可安全丢弃。
  if (writeBusy.value || confirming.value || coverLoading.value || copyBusy.value || dirty.value
      || reviewDirty.value || reviewBusy.value || settingsDirty.value || settingsBusy.value || remoteCopyDirty.value || originalUpdateBusy.value || originalUpdateDirty.value) {
    event.preventDefault(); event.returnValue = ''
  }
}
onMounted(async () => {
  window.addEventListener('beforeunload', beforeUnload)
  await run(async () => {
    // 各列表独立读取，账号失败不能挡住本地稿件与项目查看。
    const results = await Promise.allSettled([refreshCatalog(), refreshList()])
    const failure = results.find(result => result.status === 'rejected')
    if (failure?.status === 'rejected') throw failure.reason
  }, 'read')
  if (!disposed && tab.value === 'drafts') await openRequestedRecord()
  if (!disposed) timer = setTimeout(poll, 4000)
})
function disposeWorkspace() { disposed = true; publicationDiscovery.cancel(); editorRevision++; confirming.value = false; clearTimeout(timer); window.removeEventListener('beforeunload', beforeUnload) }
onBeforeUnmount(disposeWorkspace)
async function openRequestedRecord() {
  const requested = props.recordOnly ? route.params.id : route.query.publication
  const id = typeof requested === 'string' && requested ? requested : records.value[0]?.id
  if (id) await open(id)
}
function confirmLeave() {
  // 读取发布记录、账号目录或成片候选时允许离开；请求会在组件卸载后丢弃迟到结果。
  // 只有写入/确认操作需要阻止离开，避免网络暂时不可用时页面被永久卡住。
  if (originalUpdateBusy.value || writeBusy.value || confirming.value || coverLoading.value) { message.warning('正在处理发布资料或确认发布，请完成当前操作后再离开'); return false }
  if (copyBusy.value) { message.warning('正在生成候选文案，请稍候或先取消等待再离开'); return false }
  if (remoteSyncBusy.value) { message.warning('正在读取或同步B站稿件，请稍候再离开'); return false }
  if (remoteCopyDirty.value) { message.warning('已有待同步的项目文案，请先同步、清除或完成当前操作'); return false }
  if (settingsBusy.value) { message.warning('正在保存项目设置或生成候选文案，请稍候再离开'); return false }
  if (reviewBusy.value) { message.warning('正在保存或刷新内容审查，请稍候再离开'); return false }
  if (!originalUpdateDirty.value && !dirty.value && !reviewDirty.value && !settingsDirty.value && !remoteCopyDirty.value) return true
  return new Promise<boolean>(resolve => dialog.warning({ title: '尚有未保存的发布资料', content: '离开会丢弃当前修改，不会取消已经开始的上传。', positiveText: '放弃修改并离开', negativeText: '继续编辑', onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false), onClose: () => resolve(false), onMaskClick: () => resolve(false) }))
}
onBeforeRouteLeave(confirmLeave)
// 切换项目会复用同一路由记录，仍需保护旧项目的编辑；页内锚点可以正常跳转。
onBeforeRouteUpdate((to, from) => to.path === from.path
  && (to.query.publication === from.query.publication || !!current.value?.id && to.query.publication === current.value.id)
  || confirmLeave())
watch(() => route.query.publication, async (value, previous) => {
  if (disposed || value === previous || value === current.value?.id || props.recordOnly) return
  current.value = null; draft.value = null; saved.value = ''; source.value = null; sourceError.value = ''
  reviewDirty.value = false; settingsDirty.value = false; tab.value = 'drafts'
  await openRequestedRecord()
})
</script>

<template>
  <main class="publish-page workspace-page" @click.capture="revealPublicationAnchor">
    <header class="page-heading"><div><h1>{{ props.recordOnly ? '发布记录' : '发布到B站' }}</h1><p>{{ props.recordOnly ? '查看保留的稿件快照、发布进度与平台回执。' : '准备新稿，或保留原BV更新已有稿件。制作内容、发布确认与平台回执各有清晰入口。' }}</p></div><a href="https://member.bilibili.com/platform/upload/video/frame" target="_blank" rel="noopener noreferrer">打开B站创作中心 ↗</a></header>
    <WorkflowStageNavigation v-if="!props.recordOnly && stageUid" :uid="stageUid" stage="publish" />
    <n-alert v-if="error" type="error" class="notice">{{ error }}</n-alert>
    <n-alert v-if="catalogError" type="warning" class="notice">账号资料读取失败：{{ catalogError }}<span v-if="catalogReady">；暂显示上次读取的账号。</span><n-button size="small" quaternary :loading="catalogLoading" :disabled="catalogLoading" @click="refreshCatalog">重新读取账号</n-button></n-alert>
    <n-alert v-if="catalogReady && catalog.worker_ready === false" type="warning" class="notice">发布组件尚未就绪。请在 workers/bilibili 目录运行 npm ci --ignore-scripts；本地草稿仍可编辑。</n-alert>
    <n-tabs :value="tab" type="line" @update:value="switchWorkspace">
      <n-tab-pane v-if="route.params.uid" name="rules" tab="项目发布设置"><ConfigurationForm :scope="`project:${route.params.uid}`" :sections="['publication']" @dirty-change="settingsDirty=$event" @busy-change="settingsBusy=$event" @saved="studioRefresh" /></n-tab-pane>
      <n-tab-pane name="drafts" :tab="props.recordOnly ? '进度与回执' : '发布与记录'">
        <div v-if="!props.recordOnly" class="mobile-record-controls">
          <label><span>发布记录</span><n-select aria-label="本项目发布记录" :value="current?.id || null" :options="recordOptions" filterable :loading="recordsInitialLoading" :disabled="busy || confirming || copyBusy || coverLoading || reviewBusy || settingsBusy || dirty || reviewDirty || settingsDirty" placeholder="选择草稿、进度或回执" @update:value="open" /></label>
          <n-button size="small" secondary :disabled="!projectUid || creationActionLocked || busy || dirty || reviewDirty || reviewBusy" @click="create">新建草稿</n-button>
        </div>
        <div class="publish-workspace" :class="{ 'record-only': props.recordOnly }">
          <aside v-if="!props.recordOnly" id="publication-records" class="publication-list"><h2>本项目发布记录</h2><p class="list-guide">一条记录对应一个稿件；平台稿件与回执保留。</p><div class="new-draft"><n-button :disabled="!projectUid || creationActionLocked || busy || dirty || reviewDirty || reviewBusy" secondary @click="create">新建发布草稿</n-button><RouterLink :to="{path:'/publications',query:{project:projectUid}}">管理记录与回收草稿 →</RouterLink></div>
            <p v-if="recordsInitialLoading" class="muted" role="status">正在读取发布记录…</p>
            <div v-if="recordsError" class="empty" role="status">发布记录读取失败：{{ recordsError }}<n-button size="small" quaternary :loading="recordsLoading" :disabled="busy || recordsLoading" @click="rereadRecords">重新读取发布记录</n-button></div>
            <div v-else-if="recordsReady && !records.length" class="empty">{{ knownProjectExecution ? '项目正在后台执行，发布记录会自动刷新。' : '还没有发布记录。新建草稿不会上传视频。' }}</div>
            <button v-for="item in records" :key="item.id" class="publication-item" :class="{ selected: current?.id === item.id, rejected: item.status === 'rejected' }" :aria-pressed="current?.id === item.id" :disabled="busy" @click="open(item.id)"><strong>{{ item.snapshot.settings?.title || item.draft.overrides?.title || item.draft.content?.title || '未命名稿件' }}</strong><span class="record-state" :data-tone="publishWorkspace(item).tone">{{ PUBLICATION_STATUS[item.status] || item.status }}</span><small>{{ item.draft.parts.length }} 个分P · {{ publicationRecordDate(item.updated_at) }}</small><small v-if="item.status === 'rejected'" class="revision-entry">打开原记录 → 更新原稿件（保留原BV）</small></button>
          </aside>
          <section v-if="current && draft" class="publication-editor">
            <n-alert v-if="recordInactive" type="info" :show-icon="false" class="notice">{{ current.lifecycle==='deleted' || current.deleted_at ? '草稿已放入本地回收区' : '这条本地记录已归档' }}。资料与回执保留，恢复记录后才能编辑或继续发布；B站稿件没有删除。</n-alert>
            <n-alert v-if="projectActionLocked" type="info" :show-icon="false" class="notice">{{ projectResourceReason || projectLockReason }}；稿件、成片和回执仍可查看。</n-alert>
            <div class="editor-heading">
              <div><div class="editor-status"><n-tag size="small" :type="workspace.tone" :bordered="false">{{ PUBLICATION_STATUS[current.status] }}</n-tag><span v-if="dirty" class="dirty">未保存</span><span class="record-detail">{{ isDraft ? '本地草稿 · 尚未上传' : '已确认的发布记录' }} · {{ draft.parts.length }} 个分P</span></div><h2>{{ (props.recordOnly && isDraft ? recordedValues.title : displayValues.title) || '尚未填写稿件标题' }}</h2></div>
              <RouterLink v-if="!props.recordOnly" :to="'/projects/' + current.project_uid + '/steps/assemble'">返回成片 →</RouterLink><RouterLink v-else to="/publications">全部发布记录 →</RouterLink>
            </div>
            <div class="workspace-context" :data-tone="workspace.tone"><span class="context-dot" aria-hidden="true"></span><div><strong>{{ workspace.title }}</strong><p>{{ workspace.description }}</p></div></div>
            <nav class="publication-tasks" aria-label="当前稿件操作">
              <button v-for="(task, index) in workspace.tasks" :key="task.key" type="button" :aria-current="selectedTask === task.key ? 'page' : undefined" :data-task="task.key" @click="selectTask(task.key)">
                <span v-if="['draft','resume'].includes(workspace.mode) && ['video','metadata','confirm','status'].includes(task.key)" class="task-number" aria-hidden="true">{{ index + 1 }}</span>
                <span>{{ task.label }}</span><small v-if="task.badge">{{ task.badge }}</small>
                <span v-if="task.key === 'metadata' && dirty || task.key === 'update' && originalUpdateDirty || task.key === 'sync' && remoteCopyDirty" class="task-dirty" aria-label="有未保存的修改"></span>
              </button>
            </nav>
            <p class="task-description">{{ selectedTaskInfo?.description }}</p>
            <n-alert v-if="reviewSource?.mode === 'new_submission'" v-show="selectedTask === 'video' || selectedTask === 'confirm'" type="info" class="notice">这是来自 {{ reviewSource.source_bvid }} 的审查修订草稿。上传并投稿会创建新BV，原BV不会自动替换。<span v-if="reviewSource.selection_changed">分P选择已手动调整，以当前清单为准。</span></n-alert>
            <p v-else-if="!props.recordOnly && isDraft" v-show="selectedTask === 'confirm'" class="publication-intent">当前准备发布新稿，投稿后生成新BV。保留原BV替换视频，请打开原发布记录使用「更新原稿件」。</p>
            <n-alert v-if="current.status === 'unknown'" v-show="selectedTask === 'status'" type="warning" class="notice">投稿可能已成功。请先在B站核对，填写BV号关联回执，不要重复发布。<div class="receipt-form"><n-input v-model:value="receiptBV" placeholder="BV号" /><n-button :disabled="busy || confirming || !receiptBV.trim()" @click="attachReceipt">核对并关联</n-button></div></n-alert>
            <PublishResultPanel v-if="!isDraft || props.recordOnly || recordInactive" v-show="selectedTask === 'status'" surface="status" :publication="current" :record-only="props.recordOnly || recordInactive" :remote-sync-available="canManageRemoteCopy" :busy="busy || coverLoading || remoteSyncBusy || originalUpdateBusy || originalUpdateActive" :project-locked="projectActionLocked" :confirming="confirming" :copying="copying" :review-draft-checking="reviewDraftChecking" :dirty="dirty || remoteCopyDirty" :review-dirty="reviewDirty" :review-busy="reviewBusy" :can-create-review-draft="canCreateReviewDraft" @pause="pause" @check="action('check')" @collection="action('collection')" @copy="copy" @prepare-review="createReviewDraft" @chapters-retry="enable => chapterAction('retry',enable)" @chapters-reconcile="chapterAction('reconcile')" @chapters-refresh="chapterAction('refresh')" />
            <PublicationUpdatePanel v-if="canManageRemoteCopy" v-show="selectedTask === 'update'" :key="current.id" :publication="current" :disabled="!!originalUpdateBlockReason" :disabled-reason="originalUpdateBlockReason" @busy-change="originalUpdateBusy=$event" @dirty-change="originalUpdateDirty=$event" @active-change="originalUpdateActive=$event" @automatic-update="openAutomaticOriginalUpdate" @changed="originalUpdateChanged" />
            <div v-if="canManageRemoteCopy" v-show="selectedTask === 'sync'" class="single-sync-tools">
            <n-alert v-if="originalUpdateDirty || originalUpdateActive || originalUpdateBusy" type="info" :show-icon="false">{{ originalUpdateDirty ? '原稿更新中有未保存的方案，请先完成或取消该方案，再单独同步。' : '原稿更新正在处理，单独同步暂不可用。' }} <a href="#publication-original-update">返回更新原稿件 →</a></n-alert>
            <section v-if="canManageRemoteCopy" id="publish-remote-sync" class="remote-copy-workspace publication-panel">
              <div class="remote-copy-heading"><div><h3>同步文案</h3><p>载入项目文案或生成 AI 候选，核对差异后同步至当前BV。</p></div><div class="remote-copy-tools"><n-tag size="small" :bordered="false">{{ current.receipt.bvid }}</n-tag><n-button size="small" secondary :loading="remoteProjectCopyLoading" :disabled="remoteProjectCopyLoading || remoteCopyBusy || remoteSyncBusy || originalUpdateBusy || originalUpdateActive || originalUpdateDirty" @click="loadSavedProjectCopy">载入已保存项目文案</n-button><n-button v-if="remoteCopyFields.length" size="small" quaternary :disabled="remoteProjectCopyLoading || remoteCopyBusy || remoteSyncBusy || originalUpdateBusy || originalUpdateActive || originalUpdateDirty" @click="clearRemoteCopy">清除待同步文案</n-button></div></div>
              <n-alert v-if="remoteProjectCopyError" type="warning" :show-icon="false" class="notice">{{ remoteProjectCopyError }} <n-button size="small" quaternary @click="loadSavedProjectCopy">重新读取项目文案</n-button>
              </n-alert>
              <p v-if="remoteCopyFields.length" class="remote-copy-target">本次明确准备同步：{{ remoteCopyFields.map(key => remoteCopyLabels[key] || key).join('、') }}<span v-if="remoteCopySubmitted"> · 已提交，正在以B站回读结果为准</span></p>
              <PublishCopyAssistant :publication-id="current.id" :revision="current.revision" :values="remoteSyncValues" :disabled="remoteCopyBusy || remoteSyncBusy || originalUpdateBusy || originalUpdateActive || originalUpdateDirty" @apply="applyRemoteCopy" @busy-change="remoteCopyBusyChanged" />
              <PublishRemoteSyncPanel :account-id="current.snapshot?.account_id || draft.account_id" :bvid="current.receipt.bvid" :local-values="remoteSyncValues" :selected-fields="remoteCopyFields" :disabled="remoteCopyBusy || originalUpdateBusy || originalUpdateActive || originalUpdateDirty" @busy-change="remoteSyncBusyChanged" @synced="remoteCopySynced" />
            </section>
            <BiliChaptersPanel v-if="canReadRemoteChapters" :account-id="current.snapshot?.account_id || draft.account_id" :aid="current.receipt.aid" :bvid="current.receipt.bvid" :preferred-parts="current.snapshot?.chapter_options?.enabled ? preferredPublicationChapterParts(current) : []" :preferred-chapters="current.snapshot?.chapter_options?.enabled ? current.snapshot.chapter_options.items : []" :preferred-permanent="current.snapshot?.chapter_options?.permanent" :preferred-instruction="current.snapshot?.chapter_options?.instruction" :project-uid="current.project_uid" :publication-id="current.id" :disabled="props.recordOnly || projectActionLocked || busy || remoteSyncBusy || originalUpdateBusy || originalUpdateActive || originalUpdateDirty || recordManaging || publicationChapterState(current).reconcile || publicationChapterState(current).active" />

            </div>

            <n-card v-if="isDraft && !props.recordOnly && !recordInactive" v-show="selectedTask === 'video'" id="publish-video" title="准备本次发布的视频" size="small" class="publication-stage publication-panel">
              <p class="stage-description">先把视频加入本次投稿，再调整分P名称与顺序；所有分P属于同一个稿件。</p>
              <n-alert v-if="sourceError" type="warning" :show-icon="false" class="notice">{{ sourceError }}</n-alert>
              <n-alert v-if="selectedNeedsReview" type="warning" :show-icon="false" class="notice review-recovery"><p>所选文件未满足当前审查规则，具体变化见对应视频的说明。「重新检查文字」只更新报告；请先保存并复核审查设置，再到合成页勾选需要发布的版本并生成成片。</p><RouterLink :to="'/projects/' + encodeURIComponent(current.project_uid) + '/steps/assemble'">去合成重新生成 →</RouterLink><p>完成后返回「刷新视频列表」，核对并选用有效文件。自动制作按当前规则生成；自动发布遇到选中的失效文件会停止并提示，需更新选片后再继续。</p></n-alert>
              <PublishPartsEditor :parts="draft.parts" :files="source?.files || []" :disabled="locked" :loading="refreshingFiles || busy && !source" :ready="!!source" @add="file => addPart(file,true)" @remove="removePart" @move="reorderPart" @rename="renamePart" @refresh="refreshFiles"><template #footer><RouterLink v-if="!selectedNeedsReview" :to="'/projects/' + current.project_uid + '/steps/assemble'">修改成片版本效果 →</RouterLink></template></PublishPartsEditor>
              <details class="review-workspace" :open="reviewDirty || selectedNeedsReview"><summary>字幕、配音与评论审查<span>核对规则、命中明细及人工屏蔽时段</span></summary>
                <ContentReviewPanel v-if="current.project_uid" :key="current.project_uid" :uid="current.project_uid" :disabled="projectActionLocked || busy || confirming || active" @dirty-change="reviewDirty = $event" @busy-change="reviewBusy = $event" @saved="refreshFiles" />
              </details>
              <div class="stage-next"><span>{{ draft.parts.length ? '已选择 ' + draft.parts.length + ' 个分P' : '选择视频后继续准备资料' }}</span><n-button type="primary" secondary :disabled="!draft.parts.length" @click="selectTask('metadata')">下一步：投稿资料 →</n-button></div>
            </n-card>
            <details v-if="current.status==='uploaded' && !props.recordOnly && !recordInactive" v-show="selectedTask === 'video'" open id="publish-video" class="frozen-content"><summary><strong>已上传的视频清单 · {{ draft.parts.length }} 个分P</strong><span>账号、封面、视频和顺序已确认</span></summary><PublishPartsEditor :parts="draft.parts" :snapshot-parts="current.snapshot?.parts || []" frozen disabled /></details>
            <n-card v-if="editableMetadata" v-show="selectedTask === 'metadata'" id="publish-metadata" title="投稿资料" size="small" class="publication-stage publication-panel">
              <p class="stage-description">这里的账号、封面和稿件资料由所有分P共用；分P名称在视频清单中单独编辑。</p>
              <div class="account-section"><label>发布账号<n-select :value="draft.account_id || null" :options="accountOptions" :disabled="locked || catalogLoading || !catalogReady" :placeholder="accountPlaceholder" @update:value="selectAccount" /></label><div class="account-guide"><span class="muted">继承项目发布设置；此处修改仅用于当前稿件。</span><n-button text size="small" :disabled="busy || confirming || reviewBusy || settingsBusy" @click="manageAccount()">{{ catalogReady && !catalogError && !catalog.accounts.length ? '连接B站账号' : '管理账号' }}</n-button></div></div>
              <section class="cover-section"><h3>稿件封面</h3><div class="cover-row"><div class="cover-preview"><img v-if="draft.cover_id" :src="'/api/publishing/covers/' + draft.cover_id" alt="本稿件封面" /><img v-else-if="isDraft && source?.thumbnail_preview && !coverBroken" :src="source.thumbnail_preview" alt="可选的平台封面" @error="coverBroken = true" /><div v-else class="cover-placeholder">选择封面</div><small v-if="!draft.cover_id && source?.thumbnail_preview && !coverBroken" class="muted">平台封面 · 尚未使用</small></div><div class="cover-tools"><label class="file-picker">上传封面<input type="file" accept="image/png,image/jpeg,image/webp" :disabled="locked || coverLoading" @change="cover" /></label><n-button size="small" :loading="coverLoading" :disabled="locked || coverLoading || !source?.platform_cover_available" @click="importCover">使用平台封面</n-button><small v-if="coverLoading" role="status" aria-live="polite" class="muted">{{ coverOperation }}</small><small v-if="coverError" role="alert" class="cover-error">{{ coverError }}</small><small class="muted">{{ isDraft ? '预览平台封面不等于已选用；点击使用后再保存。' : '封面已随视频确认，当前仅可修改下方文字资料。' }}</small></div></div></section>
              <div class="metadata-bar"><span>{{ metadataLoading ? '正在读取账号选项，其他资料可继续编辑' : '账号选项用于核对分区、声明和合集' }}</span><n-button size="small" :loading="metadataLoading" :disabled="busy || metadataLoading || !metadataAccount" @click="refreshMetadata()">刷新账号选项</n-button></div>
              <n-alert v-if="metadataError" type="warning" class="notice">{{ metadataError }}<span v-if="metadataReady">；暂显示上次读取的选项。</span></n-alert>
              <n-alert v-if="metadata.collection_error" type="warning" class="notice">合集读取失败，请重试；不会擅自改为不加入合集。</n-alert>
              <n-alert v-if="metadataLocked && !busy" type="info" class="notice">{{ lockReason }}</n-alert>
              <details class="copy-tools"><summary><strong>AI 文案助手</strong><span>生成标题、简介、标签和动态 · 查看生成对话</span></summary>
              <PublishCopyAssistant :publication-id="current.id" :revision="current.revision" :values="displayValues" :disabled="metadataLocked" @apply="values => Object.entries(values).forEach(([key, value]) => change(key, value))" @busy-change="copyBusyChanged" />
              </details>
              <PublishSettingsForm grouped :field-keys="Object.keys(displayValues).filter(key => !key.startsWith('chapters_'))" :values="displayValues" :source-published-date="source?.source_published_date" :sources="displaySources" :overrides="draft.overrides" :metadata="metadata" :metadata-loading="metadataLoading" :metadata-error="metadataError" :metadata-ready="metadataReady" :metadata-account="metadataAccount" :metadata-account-name="metadataAccountName" :context-key="collectionContext" :cover-id="draft.cover_id" :has-ai-dubbing="hasAiDubbing" :disabled="metadataLocked" @change="change" @inherit="inherit" @collection-created="collectionCreated" @refresh-metadata="refreshMetadata()" />
              <PublishChapterOptions :value="draft.chapter_options" :source="source?.chapters || []" :duration="source?.duration" :project-uid="current.project_uid" :disabled="metadataLocked" @change="changeChapters" @busy-change="value => chapterBusy=value" />
              <details class="api-scope"><summary>与B站网页设置的对应范围</summary><p>已接入基本文案、封面、分P、新版分区、稿件类型与声明、可见范围、定时发布、水印、互动开关和话题选择。合集与视频章节在取得投稿回执后独立处理。</p><p>启用视频章节后会自动设置本稿件的源视频章节；源视频没有章节时跳过，失败可只重试章节。商业推广、付费推广、卡片、平台外挂字幕、杜比／Hi-Res、空间隐藏仍可在B站补充。账号权限及最终校验以B站为准。</p></details>
              <div class="draft-save-bar"><span>{{ dirty ? '资料有未保存修改' : '资料已保存' }}<small>保存到本地草稿，不会开始上传。</small></span><div><n-button :disabled="copyBusy || chapterBusy || busy || confirming || coverLoading || !dirty" @click="discardDraft">放弃修改</n-button><n-button type="primary" secondary :loading="savingDraft" :disabled="copyBusy || chapterBusy || metadataLocked || coverLoading || !dirty" @click="save">{{ savingDraft ? '正在保存资料…' : '保存资料' }}</n-button><n-button :disabled="dirty || copyBusy || chapterBusy || busy || coverLoading" @click="selectTask('confirm')">下一步：确认投稿 →</n-button></div></div>
            </n-card>
            <n-card v-if="actionable" v-show="selectedTask === 'confirm'" id="publish-confirm" :title="isDraft ? '确认发布' : current.status==='uploaded' ? '确认正式投稿' : '继续未完成的发布'" size="small" class="publication-stage publication-panel confirmation-stage">
              <p class="confirmation-scope">{{ confirmationScope }}</p>
              <div class="preflight-summary"><a href="#publish-video" :class="{attention:selectedFilesMissing || isDraft && !source}">{{ selectedFilesMissing ? '本地文件缺失' : isDraft && !source ? '文件状态待读取' : draft.parts.length + ' 个分P' }}</a><a href="#publish-metadata" :class="{attention:dirty}">{{ dirty ? '资料未保存' : '资料已保存' }}</a><a v-if="isDraft" href="#publish-video" :class="{attention:reviewDirty || reviewBusy || selectedNeedsReview}">{{ reviewDirty ? '审查设置未保存' : reviewBusy ? '审查进行中' : selectedNeedsReview ? '视频需审查修订' : '确认时执行发布预检' }}</a></div>
              <p class="preparation-hint" role="status">{{ preparationHint }}</p>
              <PublicationRightsHint />
              <div class="primary-publish-action">
                <n-button v-if="['draft','paused','failed'].includes(current.status)" type="primary" size="large" :loading="preflighting" :disabled="submissionBlocked || copyBusy || projectActionLocked || busy || confirming || coverLoading || dirty || reviewDirty || reviewBusy || !draft.parts.length || !catalog.worker_ready" @click="upload(true)">{{ isDraft ? '上传并投稿' : '继续上传并投稿' }}</n-button>
                <n-button v-if="current.status === 'uploaded'" type="primary" size="large" :loading="preflighting" :disabled="copyBusy || projectActionLocked || busy || dirty || reviewDirty || reviewBusy || confirming" @click="submit">确认投稿</n-button>
                <span v-if="['draft','paused','failed'].includes(current.status)">确认一次后自动上传视频、封面并投稿；关闭页面不会停止后台任务。</span><span v-else>文件已上传，确认后提交稿件资料。</span>
              </div>
              <details class="alternative-actions"><summary>其他发布方式</summary><div class="publish-actions secondary-publish-actions">
                <n-button v-if="['draft','paused','failed'].includes(current.status)" quaternary :disabled="submissionBlocked || copyBusy || projectActionLocked || busy || confirming || coverLoading || dirty || reviewDirty || reviewBusy || !draft.parts.length || !catalog.worker_ready" title="只上传文件，随后可单独投稿或追加至已有BV" @click="upload(false)">{{ isDraft ? '仅上传文件' : '仅继续上传' }}</n-button>
                <n-button v-if="current.status === 'uploaded'" quaternary :disabled="busy || dirty || reviewDirty || reviewBusy || confirming" @click="manageAccount(true)">改为追加到已有BV</n-button><span>{{ current.status==='uploaded'?'追加会保留已有BV的旧视频，不会替换原分P。':'仅上传完成后停在待投稿，稍后仍可修改文字资料。' }}</span>
              </div></details>
            </n-card>
            <details v-if="(!isDraft && current.status!=='uploaded') || props.recordOnly || recordInactive" v-show="selectedTask === 'snapshot'" open id="confirmed-content" class="frozen-content">
              <summary><strong>{{ isDraft ? '已保存的草稿内容' : '已确认的投稿内容' }}</strong><span>{{ (current.snapshot?.parts || draft.parts).length }} 个分P · {{ isDraft ? '尚未确认上传' : '查看视频与稿件资料' }}</span></summary>
              <section id="publish-video"><h3>{{ isDraft ? '已选视频清单 · 尚未确认文件' : '已确认视频清单' }}</h3><PublishPartsEditor :parts="current.snapshot?.parts || draft.parts" :snapshot-parts="current.snapshot?.parts || []" :unconfirmed="isDraft" frozen disabled /></section>
              <section id="publish-metadata" class="frozen-metadata">
                <h3>{{ isDraft ? '已保存的稿件资料' : '稿件资料快照' }}</h3>
                <p class="muted">{{ isDraft ? '仅显示草稿当时保存的文案与覆盖项；未记录的继承值不以当前项目设置推算。' : '展示确认发布时记录的内容；当前项目设置不会改写此快照。' }}</p>
                <div v-if="frozenCoverId" class="cover-preview snapshot-cover"><img v-if="!coverBroken" :src="'/api/publishing/covers/' + frozenCoverId" alt="此记录保存的封面" @error="coverBroken=true" /><p v-else class="muted">封面文件暂不可用</p><small class="muted">{{ frozenCoverId }}</small></div>
                <h4>{{ recordedValues.title || '未记录标题' }}</h4><p class="snapshot-description">{{ recordedValues.description || '未记录简介' }}</p>
                <div v-if="recordedValues.tags?.length" class="snapshot-tags"><span v-for="tagValue in recordedValues.tags" :key="tagValue">{{ tagValue }}</span></div>
                <dl><div v-for="[label,value] in frozenFacts" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl>
                <p v-if="recordedValues.source" class="muted">原视频来源：{{ recordedValues.source }}</p><p v-if="recordedValues.dynamic" class="snapshot-description">粉丝动态：{{ recordedValues.dynamic }}</p>
                <p v-if="props.recordOnly" class="muted">来源项目暂不可用。保留的记录用于核对已保存资料、进度与回执；重新发布请先恢复项目并准备草稿。</p>
              <RouterLink v-if="props.recordOnly && current.receipt?.bvid" :to="{ path:'/settings', query:{ section:'bilibili', account:current.snapshot?.account_id || draft.account_id, manage:'studio', bvid:current.receipt.bvid } }">在 DubForge 管理此稿件 →</RouterLink>
              </section>
            </details>
            <PublishResultPanel v-if="isDraft && !props.recordOnly && !recordInactive" v-show="selectedTask === 'status'" surface="status" :publication="current" />
            <PublishResultPanel v-if="!isDraft || props.recordOnly || recordInactive" v-show="selectedTask === 'more'" surface="more" :publication="current" :record-only="props.recordOnly || recordInactive" :remote-sync-available="canManageRemoteCopy" :busy="busy || coverLoading || remoteSyncBusy || originalUpdateBusy || originalUpdateActive" :project-locked="projectActionLocked" :confirming="confirming" :copying="copying" :review-draft-checking="reviewDraftChecking" :dirty="dirty || remoteCopyDirty" :review-dirty="reviewDirty" :review-busy="reviewBusy" :can-create-review-draft="canCreateReviewDraft" @pause="pause" @check="action('check')" @collection="action('collection')" @copy="copy" @prepare-review="createReviewDraft" @chapters-retry="enable => chapterAction('retry',enable)" @chapters-reconcile="chapterAction('reconcile')" @chapters-refresh="chapterAction('refresh')" />
            <details v-if="current.lifecycle_actions" v-show="selectedTask === 'more'" open class="local-record-management"><summary>本地记录管理<span>归档、回收与恢复；平台稿件保留</span></summary><p class="muted">未上传的草稿可回收，已有上传或BV的记录可归档。全部资料与防重复回执保留，可在全部发布记录中恢复。</p><PublicationRecordLifecycle :record="current" :disabled="busy || confirming || copying || coverLoading || copyBusy || reviewBusy || settingsBusy || dirty || reviewDirty || settingsDirty" @busy-change="recordManagementBusy" @changed="lifecycleChanged" /></details>
          </section>
          <div v-else class="editor-empty">
            <h2>{{ emptyWorkspace.title }}</h2><p>{{ emptyWorkspace.hint }}</p>
            <p v-if="catalog.accounts.length" class="saved-accounts">已保存账号：{{ savedAccountSummary }}</p>
            <div v-if="!props.recordOnly" class="empty-actions">
              <n-button v-if="catalogLoading || !catalogReady || catalogError" type="primary" secondary :loading="catalogLoading" :disabled="catalogLoading" @click="refreshCatalog">重新读取账号</n-button>
              <n-button v-else-if="!catalog.accounts.length" type="primary" secondary :disabled="busy || confirming || reviewBusy || settingsBusy" @click="manageAccount()">连接B站账号</n-button>
              <n-button v-else-if="recordsError || !recordsReady" type="primary" secondary :loading="recordsLoading" :disabled="busy || recordsLoading" @click="rereadRecords">重新读取发布记录</n-button>
              <RouterLink v-else-if="knownProjectExecution" :to="`/projects/${stageUid}`">查看项目总览 →</RouterLink>
              <n-button v-else-if="records.length" type="primary" secondary @click="focusPublicationRecords">选择发布记录</n-button>
              <n-button v-else-if="projectUid" type="primary" :disabled="creationActionLocked || busy || dirty || reviewDirty || reviewBusy" @click="create">新建发布草稿</n-button>

              <n-button v-if="catalog.accounts.length" quaternary :disabled="busy || confirming || reviewBusy || settingsBusy" @click="manageAccount()">管理账号</n-button>
            </div>
            <p v-if="projectUid && creationActionLocked" role="status">{{ creationResourceReason || creationLockReason }}</p>
          </div>
        </div>
      </n-tab-pane>
    </n-tabs>
  </main>
</template>

<style scoped>
.publish-page { max-width:1480px; margin:0 auto; padding:20px 32px 60px; }
/* 发布工作区沿用全站色板，以任务和状态建立层次。 */
.single-sync-tools { display:grid; gap:20px; }
.single-sync-tools .remote-copy-workspace,.single-sync-tools :deep(.chapters-panel) { margin:0; }
.remote-copy-workspace { padding:22px 24px; border:1px solid var(--df-border); border-radius:12px; background:var(--df-panel); }
.remote-copy-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
.remote-copy-heading h3 { margin:0; font-size:16px; }
.remote-copy-heading p { margin:8px 0 0; color:var(--df-muted); font-size:12px; line-height:1.8; }
.remote-copy-tools { display:flex; align-items:center; justify-content:flex-end; gap:8px; flex-wrap:wrap; }
.remote-copy-target { margin:14px 0 0; color:var(--df-muted); font-size:12px; line-height:1.8; }
.remote-copy-target span { color:var(--df-accent); }
.remote-copy-workspace :deep(.copy-assistant) { border-top:0; margin-top:20px; padding-top:0; }
.workspace-context { display:flex; align-items:flex-start; gap:11px; padding:10px 14px; margin:0 0 14px; border:1px solid color-mix(in srgb,var(--context-color,var(--df-accent)) 25%,var(--df-border)); background:color-mix(in srgb,var(--context-color,var(--df-accent)) 5%,var(--df-panel)); border-radius:8px; }
.workspace-context[data-tone=error] { --context-color:#e897a0; }
.workspace-context[data-tone=warning] { --context-color:#edc27d; }
.workspace-context[data-tone=success] { --context-color:#8fcbb6; }
.context-dot { width:7px; height:7px; flex:0 0 7px; border-radius:50%; background:var(--context-color,var(--df-accent)); margin-top:6px; }
.workspace-context>div { display:flex; align-items:baseline; flex-wrap:wrap; gap:4px 12px; }
.workspace-context strong { font-size:13px; font-weight:600; }
.workspace-context p { margin:0; font-size:12px; line-height:1.8; color:var(--df-muted); }
.publication-tasks { display:flex; gap:2px; overflow-x:auto; margin:0; padding:0 0 1px; border-bottom:1px solid var(--df-border); scrollbar-width:thin; }
.publication-tasks button { display:flex; align-items:center; gap:7px; flex:0 0 auto; padding:12px 13px; border:0; border-bottom:2px solid transparent; background:transparent; color:var(--df-muted); font:inherit; font-size:13px; white-space:nowrap; cursor:pointer; }
.publication-tasks button:hover { color:var(--df-text); background:var(--df-accent-soft); }
.publication-tasks button[aria-current=page] { color:var(--df-accent); border-bottom-color:var(--df-accent); background:var(--df-accent-soft); }
.publication-tasks button:focus-visible,.publication-item:focus-visible { outline:2px solid var(--df-accent); outline-offset:-2px; }
.task-number { display:grid; place-items:center; width:20px; height:20px; border-radius:5px; background:var(--df-panel-raised); font-size:11px; }
.publication-tasks button[aria-current=page] .task-number { background:var(--df-accent); color:var(--df-bg); }
.publication-tasks small { font-size:10px; color:#edc27d; }
.task-dirty { width:6px; height:6px; border-radius:50%; background:#edc27d; }
.task-description { color:var(--df-muted); font-size:12px; margin:13px 0 22px; line-height:1.7; }
.stage-next { display:flex; justify-content:space-between; align-items:center; gap:14px; padding-top:20px; margin-top:20px; border-top:1px solid var(--df-border); }
.stage-next>span { font-size:12px; color:var(--df-muted); }
.copy-tools { border:1px solid var(--df-border); border-radius:8px; padding:12px 14px; margin:16px 0; background:color-mix(in srgb,var(--df-accent-soft) 35%,transparent); }
.copy-tools>summary { display:flex; gap:12px; align-items:center; list-style:none; color:var(--df-text); }
.copy-tools>summary::before { content:'＋'; font-size:16px; color:var(--df-accent); }
.copy-tools[open]>summary::before { content:'−'; }
.copy-tools>summary>span { font-size:11px; color:var(--df-muted); }
.copy-tools :deep(.copy-assistant) { border:0; padding:0; margin:18px 0 0; }
.record-state { display:flex!important; align-items:center; gap:6px; }
.record-state::before { content:''; width:6px; height:6px; border-radius:50%; background:var(--df-muted); }
.record-state[data-tone=success] { color:#8fcbb6; }
.record-state[data-tone=warning] { color:#edc27d; }
.record-state[data-tone=error] { color:#e897a0; }
.record-state[data-tone=info] { color:var(--df-accent); }
.record-state[data-tone]::before { background:currentColor; }
.local-record-management { padding:20px 24px; border:1px solid var(--df-border); border-radius:12px; background:var(--df-panel); }
.local-record-management>summary { color:var(--df-text); font-weight:600; }
.local-record-management>summary>span { display:block; font-size:11px; color:var(--df-muted); font-weight:400; }

.page-heading { display:flex; align-items:center; justify-content:space-between; gap:24px; margin-bottom:14px; }
h1 { margin:0; font-size:24px; line-height:1.5; }
.page-heading p { margin:5px 0 0; }
.publish-page :deep(.workflow-stage-navigation) { margin:8px 0 12px; }
.page-heading p,.muted { color:var(--df-muted); font-size:12px; line-height:1.8; }
.page-heading a,.editor-heading a { font-size:12px; flex-shrink:0; }
.notice { margin:12px 0; }
.publish-workspace { display:grid; grid-template-columns:196px minmax(0,1fr); gap:24px; align-items:start; margin-top:16px; }
.publish-workspace.record-only { grid-template-columns:minmax(0,1fr); }
.mobile-record-controls { display:none; }
.publication-list { position:sticky; top:20px; max-height:calc(100vh - 40px); overflow:auto; padding-right:16px; border-right:1px solid var(--df-border); scroll-margin-top:24px; }
.publication-list h2 { font-size:13px; margin:0 0 8px; font-weight:600; }
.list-guide,.publication-intent,.stage-description { font-size:12px; line-height:1.8; color:var(--df-muted); }
.list-guide { margin:0 0 16px; }
.new-draft { display:grid; gap:10px; margin-bottom:20px; }
.new-draft>a { font-size:12px; }
.publication-item { width:100%; padding:12px; text-align:left; border:1px solid transparent; border-radius:10px; margin-bottom:8px; background:transparent; color:var(--df-text); cursor:pointer; font:inherit; }
.publication-item strong { display:block; line-height:1.7; font-size:12px; overflow-wrap:anywhere; }
.publication-item span,.publication-item small { display:block; color:var(--df-muted); font-size:11px; margin-top:6px; }
.publication-item.selected { border-color:var(--df-accent); background:var(--df-accent-soft); }
.publication-item:hover { background:var(--df-accent-soft); }
.publication-item:disabled { cursor:wait; opacity:.7; }
.publication-item.rejected .revision-entry { color:var(--df-accent); line-height:1.7; }
.publication-editor { min-width:0; }
.publication-editor :deep(.publication-panel) { margin-bottom:20px; }
.publication-editor :deep(.publication-panel>.n-card-header) { padding:20px 24px 0; font-size:16px; font-weight:600; }
.publication-editor :deep(.publication-panel>.n-card__content) { padding:18px 24px 24px; }
.editor-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin-bottom:14px; }
.editor-heading>div { min-width:0; }
.editor-heading h2 { font-size:19px; font-weight:600; line-height:1.5; margin:7px 0 0; overflow-wrap:anywhere; }
.editor-heading p { margin:0; font-size:12px; color:var(--df-muted); }
.editor-heading>a { margin-top:6px; }
.editor-status { display:flex; align-items:center; flex-wrap:wrap; gap:10px; }
.record-detail { font-size:11px; color:var(--df-muted); }
.dirty { font-size:11px; color:#efbd70; }
.publication-intent { margin:0 0 18px; }
.stage-description { margin:0 0 20px; }
.publication-stage,#publish-video,#publish-metadata,#publish-confirm,#publish-receipt { scroll-margin-top:24px; }

summary { cursor:pointer; color:var(--df-muted); font-size:12px; line-height:1.8; }
.review-workspace { border-top:1px solid var(--df-border); padding-top:20px; margin-top:24px; }
.review-workspace>summary { color:var(--df-text); font-weight:500; }
.review-workspace>summary span { display:block; margin-left:16px; margin-top:4px; color:var(--df-muted); font-size:11px; font-weight:400; }
.review-workspace :deep(.content-review-panel) { margin:14px 0 0; }
.account-section { display:grid; grid-template-columns:minmax(240px,1fr) minmax(180px,1fr); align-items:end; gap:20px; margin-bottom:24px; }
.account-section>label { display:grid; gap:8px; font-size:12px; }
.account-guide { display:grid; justify-items:start; gap:6px; }
.account-guide .muted { font-size:11px; }
.cover-section { border-top:1px solid var(--df-border); padding-top:20px; }
.cover-section h3 { font-size:13px; margin:0 0 14px; font-weight:500; }
.cover-row { display:flex; gap:20px; align-items:center; margin-bottom:20px; }
.cover-preview { display:grid; gap:6px; flex:0 0 192px; }
.cover-row img,.cover-placeholder { width:192px; aspect-ratio:16/10; object-fit:cover; border-radius:8px; border:1px solid var(--df-border); background:var(--df-panel-raised); }
.cover-placeholder { display:grid; place-items:center; color:var(--df-muted); font-size:12px; }
.cover-tools { display:grid; justify-items:start; gap:12px; min-width:0; }
.cover-tools small { line-height:1.8; }
.file-picker { font-size:12px; display:grid; gap:8px; }
.file-picker input { max-width:250px; font-size:11px; }
.cover-error { color:#ef9299; max-width:360px; overflow-wrap:anywhere; }
.metadata-bar { display:flex; flex-wrap:wrap; justify-content:space-between; gap:10px; align-items:center; margin:16px 0 20px; }
.metadata-bar span { color:var(--df-muted); font-size:11px; }
.api-scope { margin-top:20px; color:var(--df-muted); font-size:11px; line-height:1.8; }
.api-scope p { max-width:760px; }
.draft-save-bar { position:sticky; bottom:16px; z-index:2; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px; margin-top:24px; padding:14px 16px; border:1px solid var(--df-border); border-radius:10px; background:var(--df-panel-raised); box-shadow:0 6px 20px #0003; }
.draft-save-bar>span { font-size:12px; }
.draft-save-bar small { display:block; margin-top:4px; font-size:11px; color:var(--df-muted); }
.draft-save-bar>div { display:flex; gap:8px; }
.confirmation-stage { border-color:var(--df-accent); }
.confirmation-scope { font-size:13px; line-height:1.85; margin:0 0 14px; overflow-wrap:anywhere; }
.preflight-summary { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; font-size:11px; }
.preflight-summary a { padding:6px 10px; border:1px solid var(--df-border); border-radius:6px; color:var(--df-muted); text-decoration:none; }
.preflight-summary a:hover { background:var(--df-accent-soft); color:var(--df-text); }
.preflight-summary .attention,.preparation-hint { color:var(--df-accent); }
.preparation-hint { font-size:12px; line-height:1.8; margin:0 0 12px; }
.primary-publish-action { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin:20px 0 6px; }
.primary-publish-action>span { font-size:12px; line-height:1.8; color:var(--df-muted); flex:1 1 240px; }
.alternative-actions { border-top:1px solid var(--df-border); margin-top:20px; padding-top:14px; }
.publish-actions { display:flex; flex-wrap:wrap; align-items:center; gap:10px; padding:12px 0 0; }
.publish-actions>span { color:var(--df-muted); font-size:11px; line-height:1.8; }
.receipt-form { display:flex; gap:10px; margin-top:12px; }
.frozen-content { border:1px solid var(--df-border); border-radius:12px; padding:18px 24px; margin-bottom:20px; background:var(--df-panel); }
.frozen-content>summary { color:var(--df-text); }
.frozen-content>summary strong { font-size:13px; font-weight:500; }
.frozen-content>summary span { display:block; margin-top:4px; margin-left:16px; color:var(--df-muted); font-size:11px; }
.frozen-content[open]>summary { padding-bottom:20px; margin-bottom:20px; border-bottom:1px solid var(--df-border); }
.frozen-content section>h3 { font-size:14px; font-weight:500; margin:0 0 16px; }
.frozen-metadata { border-top:1px solid var(--df-border); margin-top:24px; padding-top:24px; }
.frozen-metadata h4 { font-size:15px; margin:14px 0 8px; overflow-wrap:anywhere; }
.frozen-metadata dl { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px 24px; margin:20px 0; }
.frozen-metadata dl>div { min-width:0; }
.frozen-metadata dt { color:var(--df-muted); font-size:11px; }
.frozen-metadata dd { margin:6px 0 0; font-size:12px; overflow-wrap:anywhere; }
.frozen-metadata p { overflow-wrap:anywhere; }
.snapshot-cover { margin:16px 0; }
.snapshot-cover img { width:192px; aspect-ratio:16/10; object-fit:cover; border:1px solid var(--df-border); border-radius:8px; }
.snapshot-description { white-space:pre-wrap; color:var(--df-muted); font-size:12px; line-height:1.8; margin:0 0 14px; }
.snapshot-tags { display:flex; flex-wrap:wrap; gap:6px; }
.snapshot-tags span { padding:4px 8px; border:1px solid var(--df-border); border-radius:5px; color:var(--df-muted); font-size:11px; }
.empty,.editor-empty { padding:24px; color:var(--df-muted); font-size:12px; line-height:1.8; }
.editor-empty { padding:48px 24px; border:1px dashed var(--df-border); border-radius:12px; }
.editor-empty h2 { margin-top:0; color:var(--df-text); font-size:21px; font-weight:600; }
.editor-empty p { max-width:620px; }
.empty-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:20px; }
.saved-accounts { color:var(--df-text); overflow-wrap:anywhere; }
@media(max-width:1350px) {
  .publish-workspace { display:block; }
  .publication-list { display:none; }
  .mobile-record-controls { display:flex; align-items:end; gap:10px; margin:8px 0 16px; }
  .mobile-record-controls>label { display:grid; grid-template-columns:auto minmax(0,1fr); align-items:center; gap:12px; font-size:11px; color:var(--df-muted); flex:1; min-width:0; }
  .mobile-record-controls>.n-button { flex-shrink:0; }
}
@media(max-width:1100px) {
  .publish-page { padding:24px; }
  .account-section { grid-template-columns:1fr; gap:10px; }
  .cover-row { flex-wrap:wrap; }
  .remote-copy-heading { flex-direction:column; }
  .remote-copy-tools { justify-content:flex-start; }
}
@media(max-width:800px) {
  .publish-page { padding:20px 16px 48px; }
  .publish-workspace { display:flex; flex-direction:column; }
  .mobile-record-controls { display:flex; align-items:end; gap:10px; margin-top:12px; }
  .mobile-record-controls>label { display:grid; gap:7px; font-size:11px; flex:1; min-width:0; color:var(--df-muted); }
  .mobile-record-controls>.n-button { flex-shrink:0; }
  .publication-editor,.editor-empty { width:100%; }
  .publication-list { position:static; order:2; width:100%; max-height:360px; padding:20px 0 0; border-right:0; border-top:1px solid var(--df-border); }
  .copy-tools>summary { align-items:flex-start; flex-direction:column; gap:4px; }
  .publication-tasks button { padding:12px 10px; font-size:12px; }
  .stage-next { flex-wrap:wrap; }
  .page-heading { align-items:flex-start; flex-direction:column; gap:10px; }
  .editor-heading { flex-wrap:wrap; }
  .editor-heading h2 { font-size:18px; }
  .publication-editor :deep(.publication-panel>.n-card-header) { padding:18px 16px 0; }
  .publication-editor :deep(.publication-panel>.n-card__content) { padding:16px; }
  .frozen-content { padding:16px; }
  .draft-save-bar { bottom:8px; padding:12px; }
  .draft-save-bar>div { flex-wrap:wrap; }
  .primary-publish-action>.n-button { width:100%; }
}
@media(max-width:480px) {
  .draft-save-bar { gap:10px; }
  .draft-save-bar>div { width:100%; }
  .draft-save-bar>div :deep(.n-button) { flex:1; }
  .frozen-metadata dl { grid-template-columns:1fr; }
  .receipt-form { flex-wrap:wrap; }
}
</style>
