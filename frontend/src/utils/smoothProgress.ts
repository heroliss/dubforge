import { onUnmounted, ref, watch, type WatchSource } from 'vue'

type ProgressStatus = string | undefined
type ProgressItem = { key: string; progress?: number; status?: string }

const TRANSITION_MS = 1500

function clamp(value: unknown): number {
  const n = Number(value)
  return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0
}

function now(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

function easing(t: number): number {
  // 匀速插值。之前的 smoothstep 会在前半段加速，用户会感觉进度“冲出去”。
  return t
}

/**
 * 把后端轮询得到的离散进度平滑追赶到目标值。
 * 目标变化后按时间在约 1.5 秒内完成过渡，不依赖浏览器刷新率。
 */
export function useSmoothProgress(
  progress: WatchSource<number | undefined>,
  status?: WatchSource<ProgressStatus>,
) {
  const displayed = ref(0)
  let target = 0
  let from = 0
  let transitionStart = 0
  let raf = 0
  let disposed = false

  const frame = (timestamp: number) => {
    if (disposed) return
    const ratio = Math.min(1, Math.max(0, (timestamp - transitionStart) / TRANSITION_MS))
    displayed.value = from + (target - from) * easing(ratio)
    if (ratio >= 1 || Math.abs(target - displayed.value) < 0.01) {
      displayed.value = target
      raf = 0
      return
    }
    raf = window.requestAnimationFrame(frame)
  }

  const update = (value: unknown, currentStatus?: ProgressStatus) => {
    const next = clamp(value)
    // 新一轮运行从较小进度开始时立即重置；正常前进永远按过渡动画显示。
    if (next + 1 < displayed.value
        && (currentStatus === 'queued' || currentStatus === 'running')) {
      displayed.value = next
      from = next
    }
    if (Math.abs(next - target) < 0.001) return
    target = next
    from = displayed.value
    transitionStart = now()
    if (raf) window.cancelAnimationFrame(raf)
    raf = window.requestAnimationFrame(frame)
  }

  const sources = status ? [progress, status] as const : [progress] as const
  watch(sources as any, (values: unknown[]) => {
    update(values[0], values[1] as ProgressStatus)
  }, { immediate: true })

  onUnmounted(() => {
    disposed = true
    if (raf) window.cancelAnimationFrame(raf)
  })
  return displayed
}

/** 多步骤总览页使用的同一套时间驱动平滑进度。 */
export function useSmoothProgressMap(source: WatchSource<ProgressItem[]>) {
  const displayed = ref<Record<string, number>>({})
  const targets = new Map<string, number>()
  const froms = new Map<string, number>()
  const starts = new Map<string, number>()
  const rafs = new Map<string, number>()
  let disposed = false

  const animate = (key: string, timestamp: number) => {
    if (disposed) return
    const target = targets.get(key) ?? 0
    const from = froms.get(key) ?? (displayed.value[key] ?? 0)
    const started = starts.get(key) ?? timestamp
    const ratio = Math.min(1, Math.max(0, (timestamp - started) / TRANSITION_MS))
    const next = from + (target - from) * easing(ratio)
    displayed.value = { ...displayed.value, [key]: next }
    if (ratio >= 1 || Math.abs(target - next) < 0.01) {
      displayed.value = { ...displayed.value, [key]: target }
      rafs.delete(key)
      froms.delete(key)
      starts.delete(key)
      return
    }
    rafs.set(key, window.requestAnimationFrame((frameNow) => animate(key, frameNow)))
  }

  const sync = (items: ProgressItem[]) => {
    const active = new Set(items.map(item => item.key))
    for (const key of targets.keys()) {
      if (!active.has(key)) {
        targets.delete(key)
        froms.delete(key)
        starts.delete(key)
        const id = rafs.get(key)
        if (id) window.cancelAnimationFrame(id)
        rafs.delete(key)
        const values = { ...displayed.value }
        delete values[key]
        displayed.value = values
      }
    }
    for (const item of items) {
      const next = clamp(item.progress)
      const current = displayed.value[item.key] ?? 0
      const previousTarget = targets.get(item.key)
      if (next + 1 < current && (item.status === 'queued' || item.status === 'running')) {
        displayed.value = { ...displayed.value, [item.key]: next }
      }
      if (previousTarget !== undefined && Math.abs(next - previousTarget) < 0.001) continue
      targets.set(item.key, next)
      froms.set(item.key, displayed.value[item.key] ?? 0)
      starts.set(item.key, now())
      const oldRaf = rafs.get(item.key)
      if (oldRaf) window.cancelAnimationFrame(oldRaf)
      rafs.set(item.key, window.requestAnimationFrame((frameNow) => animate(item.key, frameNow)))
    }
  }

  watch(source, (items) => sync(items), { immediate: true })
  onUnmounted(() => {
    disposed = true
    for (const id of rafs.values()) window.cancelAnimationFrame(id)
    rafs.clear()
    froms.clear()
    starts.clear()
  })
  return displayed
}
