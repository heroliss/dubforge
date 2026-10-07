import { releaseMedia } from './preview.ts'

export type VideoPreviewState = { kind: 'idle' | 'loading' | 'stalled' | 'error'; message: string }

// 缓冲只提示、不自动重载；反复拖动不能生成一串后台重试请求。
export function createVideoRecovery(
  media: () => HTMLVideoElement | null,
  source: () => string,
  show: (state: VideoPreviewState) => void,
  timers = { set: (fn: () => void) => setTimeout(fn, 8000), clear: (id: ReturnType<typeof setTimeout>) => clearTimeout(id) },
) {
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false, restoring = false, position = 0
  let state: VideoPreviewState = { kind: 'idle', message: '' }
  function publish(next: VideoPreviewState) { state = next; if (!disposed) show(next) }
  function clear() { if (timer !== undefined) timers.clear(timer); timer = undefined }
  function waiting() {
    if (disposed || state.kind === 'error' || state.kind === 'stalled') return
    publish({ kind: 'loading', message: '视频正在缓冲…' })
    // 重复 waiting/seeking 不延后超时，卡住时始终能出现恢复入口。
    if (timer === undefined) timer = timers.set(() => {
      timer = undefined
      publish({ kind: 'stalled', message: '视频缓冲时间较长，可重新加载当前位置。' })
    })
  }
  function ready() { if (!disposed) { clear(); publish({ kind: 'idle', message: '' }) } }
  function remember() {
    const current = media()?.currentTime
    if (!restoring && Number.isFinite(current)) position = Math.max(0, current!)
  }
  function failed() {
    if (disposed) return
    const code = media()?.error?.code
    if (code === 1) return // 主动释放旧媒体时的中止事件不作为错误。
    clear()
    publish({ kind: 'error', message: code === 3 || code === 4
      ? '视频无法解码或格式不受浏览器支持。可重新加载；反复失败时请检查成片文件。'
      : '视频加载失败，请检查后台连接后重新加载。' })
  }
  function retry() {
    const video = media(), url = source()
    if (disposed || !video || !url || (restoring && state.kind === 'loading')) return
    remember(); clear(); restoring = true
    // 先撤销旧请求，再用同一内容版本重载；保留音量和倍速，不自动播放。
    releaseMedia(video)
    video.setAttribute('src', url)
    video.load()
    publish({ kind: 'idle', message: '' }); waiting()
  }
  function metadata() {
    const video = media()
    if (!video || disposed) return
    // preload=metadata 的暂停视频不一定触发 loadeddata/canplay。
    // 元数据已就绪便结束首次加载；恢复位置后的 seeking 会重新计时。
    ready()
    if (video.videoWidth && video.videoHeight) video.style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`
    if (restoring) {
      restoring = false
      if (Number.isFinite(video.duration)) video.currentTime = Math.min(position, Math.max(0, video.duration - 0.01))
    }
  }
  function dispose() { disposed = true; clear() }
  return { waiting, ready, remember, failed, retry, metadata, dispose }
}
