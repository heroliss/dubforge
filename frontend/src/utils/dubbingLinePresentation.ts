import type { PlaybackPosition } from './dubbingTimeline'

function changeReasons(line: any): string[] {
  const reasons = line?.wav?.change_reasons
  return Array.isArray(reasons)
    ? [...new Set(reasons.filter((value): value is string => typeof value === 'string')
      .map(value => value.trim()).filter(Boolean))]
    : []
}

// exists表示可播放，file_exists表示旧文件仍在；文本变更后两者可能不同。
export function hasPendingDubbingChanges(line: any): boolean {
  const wav = line?.wav
  if (line?.selection?.source !== 'dub'
    || !Number.isInteger(line?.selection?.take) || line.selection.take <= 0
    || !wav || !(typeof wav.file_exists === 'boolean' ? wav.file_exists : wav.exists === true)) return false

  // 统一后端结论优先，特别是固定自定义朗读时，字幕变化未必改变实际朗读。
  if (typeof wav.changes_pending === 'boolean') {
    return wav.changes_pending && (!wav.stale || changeReasons(line).length > 0)
  }
  // 老后端没有细分原因，沿用已确认的参数差异；不能猜测所有过期都是文字变化。
  return !!wav.exists && !wav.stale && !!wav.settings_changed
}

export function pendingDubbingChangeReason(line: any): string {
  if (!hasPendingDubbingChanges(line)) return ''
  return changeReasons(line).join('；') || '配音参数已修改'
}

// 当前查看句与正在试听的句子分别表达；试听结束后仍保留当前句标记。
export function sentenceFocus(id: number, selected: number | null, cursor: PlaybackPosition) {
  if (cursor?.lineId === id) {
    if (cursor.phase === 'playing') return { label: '试听中', tone: 'playing' }
    return { label: cursor.phase === 'gap' ? '句间停顿' : '当前无声', tone: 'waiting' }
  }
  return { label: id === selected ? '当前句' : '', tone: 'current' }
}

/**
 * 配音列表状态文案。
 *
 * 失效的选中版本在试听层必须降为静音，避免播放旧文本/旧时间轴的音频；
 * 文案仍优先保留“过期”原因，避免把过期误认为用户主动选择了静音。
 */
export function dubbingStatusLabel(line: any, options: {
  error?: string | null; silent: boolean; emptySlot: boolean
}) {
  if (options.error) return '合成失败'
  if (line?.wav?.stale) return '配音过期 · 预览无声'
  if (options.silent) return options.emptySlot ? '人声静音' : '预览无声'
  if (line?.confirmed) return '已满意'
  if (line?.wav?.exists) return '已合成'
  return '尚无配音'
}
