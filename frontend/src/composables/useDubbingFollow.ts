import { computed, ref, watch, type Ref } from 'vue'

export type DubbingFollowMode = 'off' | 'synthesis' | 'preview'

export function useDubbingFollow(options: {
  lines: () => readonly { id: number; start: number }[]
  synthesis: () => { job: string | undefined; line: number | null }
  previewLine: () => number | null
  selected: Ref<number | null>
  reveal: (id: number) => void
  seek: (time: number) => void
}) {
  const mode = ref<DubbingFollowMode>('off')
  function locate(id: number | null, syncVideo = true) {
    const line = options.lines().find(line => line.id === id)
    if (!line) return
    options.selected.value = line.id
    if (syncVideo) options.seek(line.start)
    options.reveal(line.id)
  }
  // 只在换句、切换模式或开始新任务时定位；进度和重试不会把视频反复拉回句首。
  const synthesisKey = computed(() => {
    const { job, line } = options.synthesis()
    return line == null ? null : `${job}:${line}`
  })
  watch([mode, synthesisKey], () => {
    if (mode.value === 'synthesis') locate(options.synthesis().line)
  }, { flush: 'post' })
  watch([mode, options.previewLine], () => {
    if (mode.value === 'preview') locate(options.previewLine(), false)
  }, { flush: 'post' })
  return { mode, locate }
}
