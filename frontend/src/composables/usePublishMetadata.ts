import { onScopeDispose, ref, watch, type Ref } from 'vue'

// 缓存仅属于当前发布页，按账号隔离；上传/投稿仍由后端重新校验实时能力。
export function usePublishMetadata(account: Readonly<Ref<string>>, fetchMetadata: (id: string) => Promise<any>) {
  const metadata = ref<any>({}), loading = ref(false), error = ref(''), ready = ref(false)
  const cache = new Map<string, { data: any; time: number }>()
  const pending = new Map<string, Promise<any>>()
  let generation = 0, disposed = false
  async function refresh(force = true) {
    const id = account.value, ticket = ++generation
    const previous = cache.get(id)
    metadata.value = previous?.data || {}; ready.value = !!previous
    error.value = ''; loading.value = false
    if (!id || disposed) return
    if (!force && previous && Date.now() - previous.time < 5 * 60 * 1000) return
    loading.value = true
    try {
      let request = pending.get(id)
      if (!request) {
        request = fetchMetadata(id).then(data => {
          if (!Array.isArray(data?.human_categories) || !Array.isArray(data?.declarations)
              || !Array.isArray(data?.seasons)) throw new Error('账号选项响应不完整，请重试')
          cache.set(id, { data, time: Date.now() }); return data
        }).finally(() => pending.delete(id))
        pending.set(id, request)
      }
      const data = await request
      // 切换账号、默认设置或离开页面后，迟到响应不能覆盖当前账号选项。
      if (!disposed && ticket === generation && account.value === id) {
        metadata.value = data; ready.value = true
      }
    } catch (e: any) {
      if (!disposed && ticket === generation && account.value === id) error.value = e.message || '账号选项读取失败，请重试'
    } finally {
      if (!disposed && ticket === generation) loading.value = false
    }
  }
  watch(account, () => void refresh(false), { immediate: true })
  onScopeDispose(() => { disposed = true; generation++ })
  async function refreshAfterMutation() {
    const id = account.value
    // 创建前发出的慢读取不能当成创建后的列表；等待旧读取完成后再发一次。
    await pending.get(id)?.catch(() => {})
    cache.delete(id)
    if (!disposed && id === account.value) await refresh(true)
  }
  return { metadata, loading, error, ready, refresh, refreshAfterMutation }
}
