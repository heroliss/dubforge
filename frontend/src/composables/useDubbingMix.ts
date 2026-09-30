import { ref, type Ref } from 'vue'

type Mix = { voice: number; background: number }

// 即时试听与保存分离；同一时间只提交一次，后续拖动合并为最新音量。
export function useDubbingMix(options: {
  voice: Ref<number>; background: Ref<number>
  save: (mix: Mix) => Promise<unknown>; saved: () => void
}) {
  const pending = ref(false), saving = ref(false), error = ref('')
  let version = 0, revision = 0, disposed = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let flight: Promise<void> | undefined

  function change(kind: keyof Mix, value: number) {
    if (disposed || options[kind].value === value) return
    options[kind].value = value
    version++; revision++
    pending.value = true
    error.value = ''
    clearTimeout(timer)
    timer = setTimeout(() => void save(), 300)
  }

  async function flush() {
    saving.value = true
    try {
      while (pending.value) {
        const sentVersion = version
        const mix = { voice: options.voice.value, background: options.background.value }
        try {
          await options.save(mix)
          revision++ // 保存前发出的读取，即使晚到也不能覆盖新音量。
          error.value = ''
          if (version === sentVersion) pending.value = false
        } catch (err: any) {
          error.value = err.message || '音量保存失败'
          // 新拖动可以继续提交；相同值失败后保留试听值，等待用户重试。
          if (version === sentVersion) break
        }
      }
    } finally { saving.value = false }
    if (!disposed && !pending.value) options.saved()
  }

  function save(): Promise<void> {
    clearTimeout(timer)
    if (disposed || !pending.value) return Promise.resolve()
    if (!flight) flight = flush().finally(() => { flight = undefined })
    return flight
  }

  function reconcile(mix: Mix, readRevision: number) {
    if (disposed || pending.value || saving.value || readRevision < revision) return
    options.voice.value = mix.voice
    options.background.value = mix.background
  }

  function dispose() {
    // 离开页面时仍把最后一次操作提交完，但不再刷新页面或显示提示。
    const completion = save()
    disposed = true
    return completion
  }

  return { pending, saving, error, change, save, reconcile, revision: () => revision, dispose }
}
