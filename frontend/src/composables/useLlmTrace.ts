import { onBeforeUnmount, onMounted, ref, toValue, watch, type MaybeRefOrGetter } from 'vue'

/** 只读游标读取；停止观察不会取消业务任务，也不会重发模型请求。 */
export function useLlmTrace(id: MaybeRefOrGetter<string>, enabled: MaybeRefOrGetter<boolean> = true) {
  const events = ref<any[]>([]), error = ref(''), loading = ref(false), active = ref(false), status = ref(''), warning = ref('')
  let cursor = 0, traceId = '', generation = 0, disposed = false, mounted = false
  let controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined
  function clear() { clearTimeout(timer); controller?.abort(); controller = undefined }
  function reset() {
    generation++; clear(); events.value = []; cursor = 0; traceId = ''
    loading.value = false; error.value = ''; warning.value = ''; active.value = false; status.value = ''
    if (mounted && toValue(enabled) && toValue(id)) void load()
  }
  async function load() {
    const key = toValue(id)
    if (disposed || !toValue(enabled) || !key || loading.value) return
    if (typeof document !== 'undefined' && document.hidden) { if (mounted) timer = setTimeout(load, 2000); return }
    const ticket = generation, previousCursor = cursor
    clearTimeout(timer); loading.value = true; controller = new AbortController()
    let more = false
    try {
      const params = new URLSearchParams({ after: String(cursor), trace_id: traceId, limit: '200' })
      const response = await fetch(`/api/llm/conversations/${encodeURIComponent(key)}?${params}`, {
        cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
      })
      const value = await response.json()
      if (disposed || ticket !== generation || key !== toValue(id) || !toValue(enabled)) return
      if (!response.ok) throw new Error(value.message || 'LLM对话读取失败')
      if (value.retry) return
      if (value.reset || traceId && value.trace_id && value.trace_id !== traceId) events.value = []
      events.value.push(...(Array.isArray(value.events) ? value.events : []))
      cursor = Number(value.cursor) || 0; traceId = String(value.trace_id || '')
      active.value = value.active === true; status.value = String(value.status || '')
      warning.value = String(value.warning || (value.truncated ? '本页记录已截取；可继续读取其余记录。' : '')); error.value = ''
      more = !!value.more && cursor !== previousCursor
    } catch (reason: any) {
      if (!disposed && ticket === generation && reason?.name !== 'AbortError') error.value = reason?.name === 'TimeoutError' ? '读取超时，已显示的对话保留，可重试读取。' : reason.message || 'LLM对话读取失败'
    } finally {
      if (!disposed && ticket === generation) {
        loading.value = false
        if (mounted && toValue(enabled) && toValue(id)) timer = setTimeout(load, more ? 50 : active.value ? 1000 : 3000)
      }
    }
  }
  watch(() => [toValue(id), toValue(enabled)], reset, { flush: 'sync' })
  function visible() { if (typeof document === 'undefined' || !document.hidden) void load() }
  onMounted(() => { mounted = true; if (typeof document !== 'undefined') document.addEventListener('visibilitychange', visible); void load() })
  onBeforeUnmount(() => { disposed = true; generation++; clear(); if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', visible) })
  return { events, error, loading, active, status, warning, load, reset }
}
