import { ref, watch, type Ref } from 'vue'

export type DubbingFollowMode = 'off' | 'synthesis' | 'preview'

export function useDubbingFollow(options: {
  lines: () => readonly { id: number; start: number; playback_start?: number | null }[]
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
    if (syncVideo) options.seek(typeof line.playback_start === 'number' && Number.isFinite(line.playback_start)
      && line.playback_start >= 0 ? line.playback_start : line.start)
    options.reveal(line.id)
  }
  // 合成跟随由完成结果驱动，不能在新句正在生成时打断上一句试听。
  // 模式切换先捕获当前句；退出合成控制器可能随后清空 cursor，不能等到下次换句。
  watch([mode, options.previewLine], () => {
    if (mode.value === 'preview') locate(options.previewLine(), false)
  }, { flush: 'sync' })
  return { mode, locate }
}
