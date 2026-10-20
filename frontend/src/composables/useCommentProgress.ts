import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { readCommentProgress } from '../utils/commentProgressSnapshot'

type Options = {
  uid: () => string
  active: () => boolean
  onRevision?: (revision: string) => void
  fallback?: () => Promise<unknown>
}

/** 过程快照与完整评论分开读取；旧服务只识别一次不支持的新接口。 */
export function useCommentProgress(options: Options) {
  const snapshot = ref<any>(null), loading = ref(false), error = ref('')
  const supported = ref<boolean | null>(null)
  let disposed = false, mounted = false, generation = 0, revision = ''
  let controller: AbortController | undefined, timer: ReturnType<typeof setTimeout> | undefined
  let inFlight: Promise<void> | undefined, again = false
  const busy = () => options.active() || snapshot.value?.collection?.busy
    || snapshot.value?.prepare_job?.status === 'running'
    || Object.values(snapshot.value?.jobs || {}).some((job: any) => job?.status === 'running')
    || snapshot.value?.job?.status === 'running'

  function schedule() {
    clearTimeout(timer)
    if (mounted && !disposed && busy() && !(typeof document !== 'undefined' && document.hidden))
      timer = setTimeout(() => void refresh(false), 2000)
  }
  function refresh(force=true): Promise<void> {
    if (disposed || !options.uid() || typeof document !== 'undefined' && document.hidden) return Promise.resolve()
    if (inFlight) { again = true; return inFlight }
    clearTimeout(timer)
    const ticket = generation, uid = options.uid()
    loading.value = true
    controller = new AbortController()
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(10000)])
    const current = (async () => {
      try {
        if (supported.value === false) {
          await options.fallback?.()
          if (!disposed && ticket === generation) error.value = ''
          return
        }
        const response = await readCommentProgress(uid,signal,force)
        if (disposed || ticket !== generation) return
        if (response.status === 404 || response.status === 405) {
          supported.value = false
          await options.fallback?.()
          return
        }
        const value = response.value
        if (!response.ok) throw new Error(value.message || '评论进度读取失败')
        if (disposed || ticket !== generation) return
        supported.value = true; snapshot.value = value; error.value = ''
        const next = typeof value.data_revision === 'string' ? value.data_revision : ''
        if (next && next !== revision) { revision = next; options.onRevision?.(next) }
      } catch (cause: any) {
        if (!disposed && ticket === generation && cause?.name !== 'AbortError')
          error.value = cause?.name === 'TimeoutError' ? '进度读取超时，已显示内容保留，稍后自动重试' : cause?.message || '评论进度读取失败'
      } finally {
        if (!disposed && ticket === generation) {
          loading.value = false; inFlight = undefined
          if (again) { again = false; void refresh() } else schedule()
        }
      }
    })()
    inFlight = current
    return current
  }
  function reset() {
    generation++; controller?.abort(); clearTimeout(timer); inFlight = undefined; again = false
    revision = ''; snapshot.value = null; supported.value = null; error.value = ''; loading.value = false
    if (mounted) void refresh()
  }
  function visibility() { if (!document.hidden) void refresh(); else clearTimeout(timer) }
  watch(options.uid, reset)
  watch(options.active, value => { if (mounted && value) void refresh(); else schedule() })
  onMounted(() => { mounted = true; document.addEventListener('visibilitychange', visibility); void refresh() })
  onBeforeUnmount(() => { disposed = true; generation++; controller?.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visibility) })
  return { snapshot, loading, error, supported, refresh }
}
