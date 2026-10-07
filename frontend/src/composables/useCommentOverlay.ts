// 只读取已确认方案；页面初始不勾选，提交时携带版本防止确认范围悄悄变化。
import { computed, onBeforeUnmount, ref, watch } from 'vue'
export function useCommentOverlay(uid: () => string, readEnabled: () => boolean = () => true) {
  const enabled = ref(false), plan = ref<any>(null), revision = ref(''), failed = ref(false)
  let generation = 0
  let controller: AbortController | undefined
  async function refresh() {
    if (!readEnabled()) return
    const ticket = ++generation
    controller?.abort(); controller = new AbortController()
    try {
      const response = await fetch(`/api/projects/${uid()}/comments`, {
        cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
      })
      const value = await response.json()
      if (!response.ok) throw new Error('读取失败')
      if (ticket !== generation) return
      plan.value = value.overlay; revision.value = value.overlay_revision; failed.value = false
      if (!value.overlay?.items?.length) enabled.value = false
    } catch { if (ticket === generation) failed.value = true }
  }
  watch(() => [uid(), readEnabled()], () => {
    generation++; controller?.abort()
    enabled.value = false; plan.value = null; revision.value = ''; failed.value = false
    void refresh()
  }, { immediate: true })
  onBeforeUnmount(() => { generation++; controller?.abort() })
  const count = computed(() => plan.value?.items?.length || 0)
  const payload = computed(() => enabled.value ? { include_comments: true, overlay_revision: revision.value } : {})
  return { enabled, count, payload, revision, failed, refresh }
}
