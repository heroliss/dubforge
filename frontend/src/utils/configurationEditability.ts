import * as Vue from 'vue'

type Scope = string | Vue.Ref<string> | (() => string)
type Entry = {
  known: Vue.Ref<boolean>; locked: Vue.Ref<boolean>; reason: Vue.Ref<string>; checking: Vue.Ref<boolean>
  users: number; epoch: number; timer?: ReturnType<typeof setInterval>; abort?: AbortController
  pending?: Promise<boolean>; updated: number
}
const entries = new Map<string, Entry>()
const navigation = Vue.ref({ uid: '', busy: false, suspended: true })
const POLL_MS = 4000
const CHECKING = '正在核对运行状态，暂不能修改设置…'
const UNAVAILABLE = '未能核对运行状态，暂不能修改设置；请重新核对。'
const RUNNING = '项目有任务正在执行，结束后可修改设置。查看、导航和停止任务仍可用。'
function keyOf(scope: Scope) { return typeof scope === 'function' ? scope() : typeof scope === 'string' ? scope : scope.value }
function entryFor(scope: string) {
  let entry = entries.get(scope)
  if (!entry) {
    entry = { known: Vue.ref(false), locked: Vue.ref(true), reason: Vue.ref(CHECKING), checking: Vue.ref(false), users: 0, epoch: 0, updated: 0 }
    entries.set(scope, entry)
  }
  return entry
}
function localBusy(scope: string) {
  const state = navigation.value
  return !state.suspended && state.busy && (scope === 'global' || scope === `project:${state.uid}`)
}
// 导航已取得的本地执行状态立即收紧权限；离开项目后的旧状态不阻止服务器确认解锁。
export function setNavigationExecution(uid: string, busy: boolean, suspended = false) {
  navigation.value = { uid, busy, suspended }
}
async function read(scope: string, force = true): Promise<boolean> {
  if (!scope) return false
  const entry = entryFor(scope)
  if (entry.pending) return entry.pending
  if (!force && entry.known.value && Date.now() - entry.updated < POLL_MS) return !entry.locked.value && !localBusy(scope)
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
      entry.updated = Date.now()
      return !value.locked && !localBusy(scope)
    } catch {
      if (entry.epoch === epoch && !abort.signal.aborted) {
        entry.known.value = false; entry.locked.value = true; entry.reason.value = UNAVAILABLE
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
  const locked = Vue.computed(() => !!key.value && (localBusy(key.value) || (active.value && (!entry.value.known.value || entry.value.locked.value))))
  const reason = Vue.computed(() => localBusy(key.value) ? RUNNING : active.value && locked.value ? entry.value.reason.value : '')
  const checking = Vue.computed(() => active.value && entry.value.checking.value)
  const refresh = () => read(key.value)
  const canEdit = () => active.value ? read(key.value) : Promise.resolve(!localBusy(key.value))
  Vue.onMounted?.(() => { active.value = true; acquire(key.value) })
  const stop = Vue.watch(key, (next, previous) => { if (active.value) { release(previous); acquire(next) } }, { flush: 'sync' })
  Vue.onUnmounted?.(() => { stop(); if (active.value) release(key.value); active.value = false })
  return { locked, reason, checking, refresh, canEdit, observing: active }
}

/** 批量模板绑定只订阅当前所选项目，与项目页共享同一份状态。 */
export function useConfigurationBatchEditability(scopes: () => string[]) {
  const active = Vue.ref(false), keys = Vue.computed(() => [...new Set(scopes().filter(Boolean))])
  const locked = Vue.computed(() => keys.value.some(key => localBusy(key) || active.value && (!entryFor(key).known.value || entryFor(key).locked.value)))
  const reason = Vue.computed(() => locked.value ? '所选项目正在执行任务，或正在核对运行状态；暂不能改变模板绑定。' : '')
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
