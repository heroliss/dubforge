import { OUTPUT_LABELS } from './actionScope.ts'
// 同版本轮询合并请求；版本变化取消旧请求，避免旧响应覆盖新预览或丢掉最终刷新。
export function createLatestRefresh<T>(
  read: (signal: AbortSignal) => Promise<T>,
  apply: (value: T) => void,
  onError: (error: unknown) => void,
) {
  let active: { controller: AbortController, promise?: Promise<void> } | null = null
  let pending = false

  function cancel() {
    const previous = active
    active = null
    pending = false
    previous?.controller.abort()
  }

  function refresh(replace = false): Promise<void> {
    if (replace) cancel()
    if (active) {
      pending = true
      return active.promise!
    }
    const request = { controller: new AbortController(), promise: undefined as Promise<void> | undefined }
    active = request
    request.promise = (async () => {
      try {
        const value = await read(request.controller.signal)
        if (active === request) apply(value)
      } catch (error) {
        if (active === request && !request.controller.signal.aborted) onError(error)
      } finally {
        if (active === request) {
          active = null
          if (pending) {
            pending = false
            void refresh()
          }
        }
      }
    })()
    return request.promise
  }
  return { refresh, cancel }
}

// 暂停后清除源并 load()，中止浏览器持有的媒体请求；仅移除 DOM 不能保证停止播放。
export function releaseMedia(media: HTMLMediaElement) {
  media.pause()
  media.removeAttribute('src')
  media.load()
}

export function releaseMediaIn(root: ParentNode | null) {
  root?.querySelectorAll<HTMLMediaElement>('video, audio').forEach(releaseMedia)
}

export const vReleaseMedia = { beforeUnmount: releaseMedia }

// 替换后的成片保留文件名；优先看产物登记时间，旧数据回退到文件名时间戳。
export function latestFinalVideo<T extends { name: string; exists?: boolean }>(artifacts: T[]): T | undefined {
  return artifacts.filter(a => a.exists !== false && a.name.startsWith('final_') && a.name.endsWith('.mp4'))
    .sort(newestVideoFirst)[0]
}

export function newestVideoFirst(a: { name: string; created_at?: string }, b: { name: string; created_at?: string }) {
  const aTime = Date.parse(a.created_at || ''), bTime = Date.parse(b.created_at || '')
  if (Number.isFinite(aTime) && Number.isFinite(bTime) && aTime !== bTime) return bTime - aTime
  const stamp = (name: string) => name.match(/\d{8}_\d{6}_\d+(?=\.mp4$)/)?.[0] || name
  return stamp(b.name).localeCompare(stamp(a.name), 'en', { numeric: true })
}

// 成片结算面板: 按文件名组合后缀把 final_*.mp4 归类; 无法识别的旧命名进历史列表。
// 成片文件名 → 界面组合名；旧版 final_时间戳 归为历史成片。
export function finalVideoKind(name: string): string | null {
  if (/^final_version_[a-z0-9][a-z0-9_-]{0,63}\.mp4$/.test(name)) return 'version'
  for (const key of Object.keys(OUTPUT_LABELS)) if (new RegExp(`^final_${key}_\\d{8}_\\d{6}_\\d+\\.mp4$`).test(name)) return key
  if (/^final_\d{8}_\d{6}_\d+\.mp4$/.test(name)) return 'legacy'
  return null
}

// 选择器使用实际更新时间，不把复用文件名中的首次生成时间误当成本次时间。
export function finalVideoLabel(name: string, createdAt?: string): string {
  const kind = finalVideoKind(name)
  if (!kind) return name
  const match = name.match(/_(\d{8})_(\d{6})_\d+\.mp4$/)
  const time = createdAt && Number.isFinite(Date.parse(createdAt))
    ? new Date(createdAt).toLocaleTimeString('zh-CN', { hourCycle: 'h23' })
    : match ? `${match[2].slice(0, 2)}:${match[2].slice(2, 4)}:${match[2].slice(4, 6)}` : ''
  const label = OUTPUT_LABELS[kind] || (kind === 'version' ? '独立成片版本' : '历史成片')
  return time ? `${label} · ${time}` : label
}

export function groupFinalVideos<T extends { name: string; exists?: boolean }>(artifacts: T[]) {
  const pick = (re: RegExp): T[] => artifacts.filter(a => a.exists !== false && re.test(a.name))
    .sort(newestVideoFirst)
  const dub = pick(/^final_dub_(?:comments_)?\d{8}_\d{6}_\d+\.mp4$/)
  const dubHardsub = pick(/^final_dub_hardsub_(?:comments_)?\d{8}_\d{6}_\d+\.mp4$/)
  const srcHardsub = pick(/^final_src_hardsub_(?:comments_)?\d{8}_\d{6}_\d+\.mp4$/)
  const srcComments = pick(/^final_src_comments_\d{8}_\d{6}_\d+\.mp4$/)
  const matched = new Set([...dub, ...dubHardsub, ...srcHardsub, ...srcComments].map(a => a.name))
  const legacy = artifacts
    .filter(a => a.name.startsWith('final_') && a.name.endsWith('.mp4') && !matched.has(a.name))
    .sort((a, b) => b.name.localeCompare(a.name, 'en', { numeric: true }))
  return { dub, dubHardsub, srcHardsub, srcComments, legacy }
}
