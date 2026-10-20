import { onScopeDispose, ref, watch } from 'vue'
import type { TimelineLine } from '../utils/dubbingTimeline'

export type SynthesisPreviewStatus = 'off' | 'waiting' | 'playing' | 'paused' | 'complete' | 'error'
export interface SynthesisPreviewLine extends TimelineLine {
  versions?: readonly { id: number; exists: boolean; url: string }[]
}
export interface SynthesisPreviewTask {
  job_id: number | string
  active: boolean
  lines: readonly { id: number; status: string; take?: number | null }[]
}

type Candidate = {
  key: string; id: number; take: number; baseline?: string | null
  incompatible?: { snapshot: SynthesisPreviewLine; choice: string }
}

export function useSynthesisPreview(options: {
  enabled: () => boolean
  task: () => SynthesisPreviewTask | null | undefined
  lines: () => readonly SynthesisPreviewLine[]
  finished: () => { serial: number; lineId: number | null }
  interrupted: () => number
  error: () => string
  previewLine?: () => number | null
  playing?: () => boolean
  preservePlaybackOnDisable?: () => boolean
  select: (id: number) => void
  play: (line: TimelineLine) => Promise<void>
  resume?: (lineId: number) => boolean | void
  pause: () => void
}) {
  const status = ref<SynthesisPreviewStatus>('off')
  const currentLine = ref<number | null>(null), pendingLine = ref<number | null>(null)
  const observed = new Map<string, Candidate>(), baselines = new Map<string, string | null>()
  const heard = new Set<string>()
  let enabled = false, disposed = false, admitted = false, paused = false, failed = false
  let job: string | null = null, initialized = false, admitExisting = false, epoch = 0, finishSerial = 0
  let interruption = options.interrupted()
  let current: Candidate | null = null, pending: Candidate | null = null, retry: Candidate | null = null

  function key(id: number, take: number) { return `${id}:${take}` }
  function version(candidate: Pick<Candidate, 'id' | 'take'>) {
    return options.lines().find(line => line.id === candidate.id)?.versions?.find(take => take.id === candidate.take)
  }
  function setPending(candidate: Candidate | null) {
    // 合成快于试听时只留最新一句，不堆积队列，也不打断正在播放的句子。
    pending = candidate; pendingLine.value = candidate?.id ?? null
  }
  function stopCurrent(stopPlayback: boolean, keepRetry: boolean) {
    epoch++
    if (current) {
      if (keepRetry) retry = current
      current = null; currentLine.value = null
      if (stopPlayback) options.pause()
    }
  }
  function refreshStatus() {
    status.value = !enabled ? 'off' : failed ? 'error' : paused ? 'paused' : current ? 'playing'
      : !pending && admitted && options.task()?.active === false ? 'complete' : 'waiting'
  }
  function readiness(candidate: Candidate): 'ready' | 'wait' | 'skip' {
    const line = options.lines().find(item => item.id === candidate.id)
    if (!line) return 'wait'
    const take = version(candidate)
    if (!take?.exists || !take.url) return 'wait'
    // 工作台先读媒体后读任务，done 可能短暂配上旧 URL；等完整新版本再接入。
    if (candidate.baseline != null && take.url === candidate.baseline) return 'wait'
    // 首次生成时，媒体采样可能仍读到旧空位/原声。至少等一份新句子快照确认，
    // 同一次轮询内的播放事件不算确认；持续的用户选择才略过，不能强切声源。
    if (line.selection?.source === 'original' || line.selection?.take === null) {
      const choice = `${line.selection.source}:${line.selection.take}`
      const previous = candidate.incompatible
      if (!previous || previous.snapshot === line || previous.choice !== choice) {
        candidate.incompatible = { snapshot: line, choice }
        return 'wait'
      }
      return 'skip'
    }
    candidate.incompatible = undefined
    if (line.selection?.source !== 'dub' || line.selection.take !== candidate.take
      || !line.wav.exists || line.wav.url !== take.url) return 'wait'
    return heard.has(`${job}:${candidate.key}:${take.url}`) ? 'skip' : 'ready'
  }
  function playbackFailed(token: number) {
    if (disposed || !enabled || token !== epoch || !current) return
    failed = true
    stopCurrent(true, true)
    refreshStatus()
  }
  function pump() {
    if (disposed || !enabled || paused || failed || current || !pending) { refreshStatus(); return }
    const candidate = pending, ready = readiness(candidate)
    if (ready === 'wait') { refreshStatus(); return }
    setPending(null)
    if (ready === 'skip') { refreshStatus(); return }
    const line = options.lines().find(item => item.id === candidate.id)!
    heard.add(`${job}:${candidate.key}:${line.wav.url}`)
    retry = null; current = candidate; currentLine.value = candidate.id
    finishSerial = options.finished().serial
    const token = ++epoch
    refreshStatus()
    options.select(candidate.id)
    // play() 只表示启动成功。必须等自然结束事件，缓冲、暂停不能当成播完。
    try {
      void options.play(line).then(() => {
        if (!disposed && token === epoch && options.error()) playbackFailed(token)
      }).catch(() => playbackFailed(token))
    } catch { playbackFailed(token) }
  }
  function observeTask() {
    const task = options.task()
    if (!task) return
    const nextJob = String(task.job_id)
    if (nextJob !== job) {
      stopCurrent(true, false)
      setPending(null); retry = null; observed.clear(); baselines.clear(); heard.clear(); failed = false
      // 自动初载不重放历史；用户明确开启跟随时，立即接入当前任务最新完成结果。
      admitted = initialized || task.active || admitExisting
      admitExisting = false
      initialized = true; job = nextJob
    }
    if (!admitted) {
      if (task.active) admitted = true
      else return
    }
    for (const row of task.lines) {
      if (row.take == null) continue
      const rowKey = key(row.id, row.take)
      if (row.status !== 'done') {
        if (!baselines.has(rowKey)) baselines.set(rowKey, version({ id: row.id, take: row.take })?.url ?? null)
        continue
      }
      if (observed.has(rowKey)) continue
      const candidate: Candidate = { key: rowKey, id: row.id, take: row.take,
        ...(baselines.has(rowKey) ? { baseline: baselines.get(rowKey) } : {}) }
      observed.set(rowKey, candidate)
      // 后端按 task.lines 顺序执行；一轮见到多个完成结果时最后一个就是最新结果。
      setPending(candidate)
    }
  }
  function adoptPreview() {
    if (!options.playing?.()) return false
    const id = options.previewLine?.()
    const candidate = [...observed.values()].find(row => row.id === id && readiness(row) === 'ready')
    // 只有同一条单句时间轴才能接管；连续预览仍由正常 play() 冻结单句范围。
    if (!candidate || !options.resume?.(candidate.id)) return false
    const line = options.lines().find(row => row.id === candidate.id)!
    heard.add(`${job}:${candidate.key}:${line.wav.url}`)
    current = candidate; currentLine.value = candidate.id
    if (pending?.key === candidate.key) setPending(null)
    finishSerial = options.finished().serial
    epoch++
    options.select(candidate.id)
    return true
  }
  function suspend(stopPlayback = true) {
    if (disposed || !enabled) return
    paused = true
    stopCurrent(stopPlayback, true)
    refreshStatus()
  }
  function resume() {
    if (disposed || !enabled) return
    paused = false; failed = false
    observeTask()
    if (!pending && retry) {
      const candidate = retry
      retry = null
      // 当前单句仍占用同一条播放器时间轴时，恢复媒体即可继续原位置。
      // 只有时间轴已被手动播放/定位接管时才退回从句首重新试听。
      if (options.resume?.(candidate.id)) {
        current = candidate; currentLine.value = candidate.id
        finishSerial = options.finished().serial
        epoch++
        refreshStatus()
        return
      }
      const take = version(candidate)
      if (take?.url) heard.delete(`${job}:${candidate.key}:${take.url}`)
      setPending(candidate)
    }
    pump()
  }
  watch(() => ({
    enabled: options.enabled(),
    task: (() => { const task = options.task(); return task && { job: task.job_id, active: task.active,
      rows: task.lines.map(row => [row.id, row.status, row.take]) } })(),
    // 只观察与接入有关的媒体字段，不因进度、译文或播放器时钟刷新而重复调度。
    media: options.lines().map(line => [line.id, line.selection?.source, line.selection?.take,
      line.wav.exists, line.wav.url, line.versions?.map(take => [take.id, take.exists, take.url])]),
    finished: (() => { const value = options.finished(); return { serial: value.serial, lineId: value.lineId } })(),
    interrupted: options.interrupted(), error: options.error(),
  }), (snapshot, previous) => {
    if (disposed) return
    let activating = false
    if (snapshot.enabled !== enabled) {
      activating = snapshot.enabled && previous !== undefined
      enabled = snapshot.enabled
      stopCurrent(enabled || !options.preservePlaybackOnDisable?.(), false)
      setPending(null); retry = null; observed.clear(); baselines.clear(); heard.clear()
      paused = false; failed = false; job = null; initialized = false; admitExisting = activating
      interruption = snapshot.interrupted
    }
    if (!enabled) { refreshStatus(); return }
    if (snapshot.interrupted !== interruption) {
      interruption = snapshot.interrupted
      suspend(false)
    }
    observeTask()
    // 已在试听当前任务的完成句时延续原位置；否则停止旧连续预览再接入最新结果。
    if (activating && !adoptPreview() && (options.playing?.() || options.previewLine?.() != null)) options.pause()
    if (current && snapshot.error) playbackFailed(epoch)
    if (current && snapshot.finished.serial > finishSerial && snapshot.finished.lineId === current.id) {
      // 播放器已暂停整组媒体后才发布完成事件，下一句不会和旧尾音双播。
      current = null; currentLine.value = null; retry = null; epoch++
    }
    pump()
  }, { immediate: true, flush: 'post' })
  onScopeDispose(() => {
    disposed = true
    stopCurrent(true, false)
    setPending(null)
  })
  return { status, currentLine, pendingLine, resume, suspend }
}
