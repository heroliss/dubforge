import * as Vue from 'vue'

type Scope = string | Vue.Ref<string> | (() => string)
type Entry = {
  known: Vue.Ref<boolean>; locked: Vue.Ref<boolean>; reason: Vue.Ref<string>; checking: Vue.Ref<boolean>
  users: number; epoch: number; timer?: ReturnType<typeof setInterval>; abort?: AbortController
  pending?: Promise<boolean>; updated: number
  lockedPaths: Vue.Ref<string[]>; runningSteps: Vue.Ref<any[]>; resourceLocked: Vue.Ref<boolean>; resourceReason: Vue.Ref<string>
}
const entries = new Map<string, Entry>()
const navigation = Vue.ref({ uid: '', busy: false, suspended: true, archived: false, verificationReason: '' })
const POLL_MS = 4000
const CHECKING = '正在核对运行状态，暂不能修改设置…'
const UNAVAILABLE = '未能核对运行状态，暂不能修改设置；请重新核对。'
const RUNNING = '项目有任务正在执行，结束后可修改设置。查看、导航和停止任务仍可用。'
const ARCHIVED = '项目已归档，请先恢复项目后再修改设置或继续处理。已有产物和记录仍可查看。'
// 这些字段在步骤进入 running 时已复制到执行快照，保存后只影响下一次
// 请求/步骤；允许它们在长任务期间修改，避免把“调整下一次兜底时限”误报为
// 当前步骤设置变更。后端仍会按同一启动锁再次校验。
const NEXT_BOUNDARY_PATHS = new Set([
  'llm.timeout', 'download.proxy', 'download.cookies_path',
  'tts.indextts25.url', 'tts.indextts25.project_dir', 'tts.indextts25.request_timeout_seconds',
  'tts.gptsovits.url', 'tts.gptsovits.project_dir', 'tts.gptsovits.request_timeout_seconds',
])
function nextBoundaryEditable(path: string) {
  return NEXT_BOUNDARY_PATHS.has(path) || path === 'runner.step_timeout_seconds'
    || path.startsWith('runner.step_timeout_seconds.')
}
function keyOf(scope: Scope) { return typeof scope === 'function' ? scope() : typeof scope === 'string' ? scope : scope.value }
function entryFor(scope: string) {
  let entry = entries.get(scope)
  if (!entry) {
    entry = { known: Vue.ref(false), locked: Vue.ref(true), reason: Vue.ref(CHECKING), checking: Vue.ref(false), users: 0, epoch: 0, updated: 0,
      lockedPaths: Vue.ref<string[]>([]), runningSteps: Vue.ref<any[]>([]), resourceLocked: Vue.ref(false), resourceReason: Vue.ref('') }
    entries.set(scope, entry)
  }
  return entry
}
function localBusy(scope: string) {
  const state = navigation.value
  return !state.suspended && state.busy && (scope === 'global' || scope === `project:${state.uid}`)
}
function localVerificationReason(scope: string) { return localBusy(scope) ? navigation.value.verificationReason : '' }
function localResourceReason(scope: string, entry: Entry, observing: boolean) {
  const verification = localVerificationReason(scope)
  if (verification && observing) {
    // 本地核验理由只替代本地 busy 自造的执行文案，不能盖住独立后台门禁。
    const knownActivity = entry.known.value && entry.runningSteps.value.some(row =>
      (!scope.startsWith('project:') || row.project_uid === scope.slice(8))
      && ['queued', 'running', 'stopping', 'cancelling'].includes(row.status))
    if (!entry.known.value || entry.resourceLocked.value || knownActivity) {
      return entry.resourceReason.value || entry.reason.value || (knownActivity ? RUNNING : verification)
    }
  }
  return verification || RUNNING
}
function localArchived(scope: string) {
  const state = navigation.value
  return !state.suspended && state.archived && scope === `project:${state.uid}`
}
function locallyLocked(scope: string) { return localBusy(scope) || localArchived(scope) }
// 导航已取得的本地执行状态立即收紧权限；离开项目后的旧状态不阻止服务器确认解锁。
export function setNavigationExecution(uid: string, busy: boolean, suspended = false, archived = false, verificationReason = '') {
  const previous = navigation.value, scope = `project:${uid}`
  const entry = uid && previous.uid === uid && previous.archived !== archived ? entries.get(scope) : undefined
  // 归档/恢复改变可编辑状态；旧请求和短期缓存不再代表当前项目。
  if (entry) {
    entry.epoch++; entry.abort?.abort(); entry.pending = undefined; entry.abort = undefined
    entry.known.value = false; entry.locked.value = true; entry.reason.value = CHECKING
    entry.resourceLocked.value = false; entry.resourceReason.value = ''
    entry.updated = 0; entry.checking.value = false
  }
  navigation.value = { uid, busy, suspended, archived, verificationReason: busy ? verificationReason : '' }
  if (entry?.users) void read(scope)
}
async function read(scope: string, force = true): Promise<boolean> {
  if (!scope) return false
  const entry = entryFor(scope)
  if (entry.pending) return entry.pending
  if (!force && entry.known.value && Date.now() - entry.updated < POLL_MS) return !entry.locked.value && !locallyLocked(scope)
  const epoch = ++entry.epoch, abort = new AbortController()
  entry.abort = abort; entry.checking.value = true
  const pending = (async () => {
    try {
      const response = await fetch(`/api/configuration/${encodeURIComponent(scope)}/editability`, {
        cache: 'no-store', signal: AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]),
      })
      const value = await response.json()
      if (!response.ok || typeof value.locked !== 'boolean') throw new Error('状态不可用')
      if (entry.epoch !== epoch || abort.signal.aborted) return false
      entry.known.value = true; entry.locked.value = value.locked
      entry.reason.value = value.locked ? (value.reason || RUNNING) : ''
      entry.lockedPaths.value = Array.isArray(value.locked_paths) ? value.locked_paths : []
      entry.runningSteps.value = Array.isArray(value.running_steps) ? value.running_steps : []
      entry.resourceLocked.value = typeof value.resource_locked === 'boolean' ? value.resource_locked : value.locked
      entry.resourceReason.value = entry.resourceLocked.value ? (value.resource_reason || RUNNING) : ''
      entry.updated = Date.now()
      return !value.locked && !locallyLocked(scope)
    } catch {
      if (entry.epoch === epoch && !abort.signal.aborted) {
        entry.known.value = false; entry.locked.value = true; entry.reason.value = UNAVAILABLE
        entry.resourceLocked.value = true; entry.resourceReason.value = UNAVAILABLE
      }
      return false
    } finally {
      if (entry.epoch === epoch) { entry.checking.value = false; entry.pending = undefined; entry.abort = undefined }
    }
  })()
  entry.pending = pending
  return pending
}
/** 少量一次性操作（如模板绑定）也必须在发送前核对被影响项目。 */
export function checkConfigurationEditability(scope: string) { return read(scope) }
function onVisibility() {
  if (document.hidden) return
  for (const [scope, entry] of entries) if (entry.users) void read(scope)
}
let subscriptions = 0
function acquire(scope: string) {
  if (!scope) return
  const entry = entryFor(scope)
  if (!entry.users++) {
    void read(scope, false)
    entry.timer = setInterval(() => { if (typeof document === 'undefined' || !document.hidden) void read(scope) }, POLL_MS)
  }
  if (!subscriptions++ && typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility)
}
function release(scope: string) {
  if (!scope) return
  const entry = entryFor(scope)
  if (entry.users && !--entry.users) {
    clearInterval(entry.timer); entry.timer = undefined; entry.epoch++; entry.abort?.abort()
    entry.pending = undefined; entry.abort = undefined; entry.checking.value = false
  }
  if (subscriptions && !--subscriptions && typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility)
}
/** 同一范围共享请求与低频轮询；首次读取和断线均禁止写入。SSR 不启动网络或轮询。 */
export function useConfigurationEditability(scope: Scope) {
  const active = Vue.ref(false), key = Vue.computed(() => keyOf(scope))
  const entry = Vue.computed(() => entryFor(key.value))
  // 项目忙只影响运行/停止等操作；设置表单按后端返回的活动步骤路径逐项锁定。
  // 这样其它步骤和排队中的步骤仍可编辑，保存时后端再以同一启动锁复核。
  const locked = Vue.computed(() => !!key.value && (localArchived(key.value) || (active.value && (!entry.value.known.value || entry.value.locked.value))))
  const reason = Vue.computed(() => localArchived(key.value) ? ARCHIVED : active.value && locked.value ? entry.value.reason.value : '')
  const checking = Vue.computed(() => active.value && entry.value.checking.value)
  const refresh = () => read(key.value)
  const canEdit = () => active.value ? read(key.value) : Promise.resolve(!localArchived(key.value))
  Vue.onMounted?.(() => { active.value = true; acquire(key.value) })
  const stop = Vue.watch(key, (next, previous) => { if (active.value) { release(previous); acquire(next) } }, { flush: 'sync' })
  Vue.onUnmounted?.(() => { stop(); if (active.value) release(key.value); active.value = false })
  const isPathLocked = (path: string) => {
    if (!active.value || !path) return false
    if (nextBoundaryEditable(path)) return false
    return entry.value.lockedPaths.value.some(candidate => path === candidate || path.startsWith(candidate + '.') || candidate === path.split('.', 1)[0])
  }
  // 普通设置保存只核对实际写入字段；其他步骤运行不等于这些字段已被占用。
  // 仍先读取新状态，拒绝未知、归档及当前步骤已冻结的参数。
  const canEditPaths = async (paths: readonly string[]) => {
    if (active.value) await read(key.value)
    return !localArchived(key.value) && !locked.value && !paths.some(isPathLocked)
  }
  return { locked, reason, checking, refresh, canEdit, canEditPaths, observing: active, isPathLocked,
    // 只接受明确执行事实；初读、断线和归档造成的资源锁不能冒充后台任务。
    executionActive: Vue.computed(() => !!key.value && !localArchived(key.value) && (localBusy(key.value) && !localVerificationReason(key.value)
      || active.value && entry.value.known.value && entry.value.runningSteps.value.some(row =>
        (!key.value.startsWith('project:') || row.project_uid === key.value.slice(8))
        && ['running', 'stopping', 'cancelling'].includes(row.status)))),
    lockedPaths: Vue.computed(() => entry.value.lockedPaths.value), runningSteps: Vue.computed(() => entry.value.runningSteps.value),
    resourceLocked: Vue.computed(() => localArchived(key.value) || localBusy(key.value)
      || active.value && (!entry.value.known.value || entry.value.resourceLocked.value)),
    resourceReason: Vue.computed(() => localArchived(key.value) ? ARCHIVED
      : localBusy(key.value) ? localResourceReason(key.value, entry.value, active.value)
      : active.value ? entry.value.resourceReason.value : '') }
}

/** 批量模板绑定只订阅当前所选项目，与项目页共享同一份状态。 */
export function useConfigurationBatchEditability(scopes: () => string[]) {
  const active = Vue.ref(false), keys = Vue.computed(() => [...new Set(scopes().filter(Boolean))])
  const locked = Vue.computed(() => keys.value.some(key => {
    const entry = entryFor(key)
    return locallyLocked(key) || active.value && (!entry.known.value || entry.resourceLocked.value || entry.runningSteps.value.length > 0)
  }))
  const reason = Vue.computed(() => keys.value.some(localArchived) ? '所选项目包含已归档项目，请先恢复项目后再改变模板绑定。'
    : locked.value ? '所选项目正在执行任务，或正在核对运行状态；暂不能改变模板绑定。' : '')
  const refresh = async () => (await Promise.all(keys.value.map(key => read(key)))).every(Boolean)
  Vue.onMounted?.(() => { active.value = true; keys.value.forEach(acquire) })
  const stop = Vue.watch(keys, (next, previous) => {
    if (!active.value) return
    previous.filter(key => !next.includes(key)).forEach(release)
    next.filter(key => !previous.includes(key)).forEach(acquire)
  }, { flush: 'sync' })
  Vue.onUnmounted?.(() => { stop(); if (active.value) keys.value.forEach(release); active.value = false })
  return { locked, reason, refresh, observing: active }
}
