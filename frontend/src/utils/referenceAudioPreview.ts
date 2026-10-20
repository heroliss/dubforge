import { releaseMedia } from './preview.ts'

export type ReferencePreviewState = 'idle' | 'loading' | 'playing'
export interface ReferenceAudioClip {
  url: string; start: number; end: number; line_id: number
  reference_start?: number; reference_end?: number
}
interface ReferencePlayback { url: string; start?: number; end?: number }

// 参考句与参考文件共用播放权；后点的按钮停止前一个，包括尚未返回的读取。
let stopActive: (() => void) | undefined

export function referencePreviewMaxSeconds(value: unknown): number {
  const seconds = value == null ? NaN : Number(value)
  return Number.isFinite(seconds) ? Math.min(15, Math.max(.5, seconds)) : 5
}

export function createReferenceAudioPreview(options: {
  state: (state: ReferencePreviewState) => void
  error: (message: string) => void
  createAudio?: (url: string) => HTMLAudioElement
}) {
  let generation = 0, request: AbortController | undefined, audio: HTMLAudioElement | undefined
  let timer: ReturnType<typeof setTimeout> | undefined, startup: ReturnType<typeof setTimeout> | undefined
  const clearTimer = () => { if (timer !== undefined) clearTimeout(timer); timer = undefined }
  const clearStartup = () => { if (startup !== undefined) clearTimeout(startup); startup = undefined }
  function stop() {
    generation++; request?.abort(); request = undefined; clearTimer(); clearStartup()
    const previous = audio; audio = undefined
    if (previous) {
      previous.onloadedmetadata = previous.onseeked = previous.onplaying = previous.onwaiting = previous.ontimeupdate = null
      previous.onended = previous.onpause = previous.onerror = null
      releaseMedia(previous)
    }
    if (stopActive === stop) stopActive = undefined
    options.state('idle')
  }
  async function load(endpoint: string, parse: (body: any) => ReferencePlayback | undefined, file = false) {
    stop(); stopActive?.(); stopActive = stop
    const ticket = generation, controller = new AbortController()
    request = controller; options.error(''); options.state('loading')
    const current = () => generation === ticket && !controller.signal.aborted
    const fail = (message: string) => { if (current()) { stop(); options.error(message) } }
    try {
      const response = await fetch(endpoint, {
        cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]),
      })
      const body = await response.json()
      if (!current()) return
      if (response.status === 404 && (body?.code === 'API_NOT_FOUND' || body?.detail === 'Not Found')) {
        fail('当前后端尚未提供此试听接口，请重启后端并刷新页面后重试。'); return
      }
      if (!response.ok) { fail(body?.message || (file ? '参考文件不可试听，请检查文件路径。'
        : '参考原声未就绪，请先完成音轨分离后重试。')); return }
      const clip = parse(body)
      if (!clip) { fail(file ? '参考文件的试听地址无效，请重新选择文件后重试。'
        : '参考原声的时间信息无效，请重新选择句子后重试。'); return }
      // 只保留一个播放边界控制；媒体片段 #t 与显式 seek 会产生两次定位和提前停止。
      const media = (options.createAudio || (url => new Audio(url)))(clip.url)
      audio = media; media.preload = 'metadata'
      let positioned = false, started = false, end = clip.end
      startup = setTimeout(() => fail(file ? '参考文件加载超时，请重试。' : '参考原声定位或加载超时，请重试。'), 20000)
      const armEnd = () => {
        clearTimer()
        if (end === undefined) return // 文件试听由自然 ended 结束，不套用模型参考时长上限。
        timer = setTimeout(() => {
          if (!current()) return
          if (media.currentTime >= end!) stop()
          else if (!media.paused) armEnd()
        }, Math.max(30, (end - media.currentTime) * 1000))
      }
      media.onplaying = () => { if (current()) { clearStartup(); options.state('playing'); armEnd() } }
      media.onwaiting = () => { if (current()) { clearTimer(); options.state('loading') } }
      media.ontimeupdate = () => { if (current() && end !== undefined && media.currentTime >= end) stop() }
      media.onended = media.onpause = () => { if (current()) stop() }
      media.onerror = () => fail(file ? '参考文件加载或播放失败，请检查音频格式、文件是否仍存在和后台连接。'
        : '参考原声加载或播放失败，请重试；若仍失败，请检查音轨分离结果和后台连接。')
      const startPlayback = () => {
        if (!current() || started) return
        started = true; media.onseeked = null
        void media.play().catch(error => fail(error?.name === 'NotAllowedError'
          ? '浏览器暂未允许播放，请再点一次试听。' : file ? '参考文件播放失败，请重试。' : '参考原声播放失败，请重试。'))
      }
      media.onloadedmetadata = () => {
        if (!current() || positioned) return
        positioned = true
        if (!Number.isFinite(media.duration) || media.duration <= 0) {
          fail(file ? '参考文件时长无法读取，请检查音频格式后重试。' : '参考原声时长无法读取，请检查音轨分离结果后重试。'); return
        }
        if (file) { startPlayback(); return }
        end = Math.min(end!, media.duration)
        if (end <= clip.start!) { fail('该句超出参考原声音轨范围，请检查原文时间和音轨分离结果。'); return }
        try {
          // currentTime 赋值并不代表解码器已定位完成；等 seeked 后才让音频出声。
          if (clip.start === 0 && media.currentTime === 0 && !media.seeking) startPlayback()
          else { media.onseeked = startPlayback; media.currentTime = clip.start! }
        } catch { fail('参考原声无法定位到此句，请重试。') }
      }
      media.load()
    } catch (error: any) {
      fail(error?.name === 'TimeoutError' ? (file ? '读取参考文件超时，请重试。' : '读取参考原声超时，请重试。')
        : file ? '读取参考文件失败，请检查后台连接后重试。' : '读取参考原声失败，请检查后台连接后重试。')
    }
  }
  function play(uid: string, lineId: number, maxSeconds: number) {
    const base = `/api/projects/${encodeURIComponent(uid)}/workbench`
    return load(`${base}/reference-audio?line_id=${lineId}&max_seconds=${maxSeconds}`, (clip: ReferenceAudioClip) => {
      if (!clip) return
      const referenceStart = clip.reference_start ?? clip.start, referenceEnd = clip.reference_end ?? clip.end
      const hasReference = clip.reference_start !== undefined || clip.reference_end !== undefined
      if (clip.line_id !== lineId || typeof clip.url !== 'string' || clip.url.includes('#')
          || !(clip.url === base + '/audio/vocal' || clip.url.startsWith(base + '/audio/vocal?'))
          || !Number.isFinite(clip.start) || !Number.isFinite(clip.end) || clip.start < 0
          || clip.end <= clip.start || !Number.isFinite(referenceStart) || !Number.isFinite(referenceEnd)
          || (hasReference && (!Number.isFinite(clip.reference_start) || !Number.isFinite(clip.reference_end)))
          || referenceStart < clip.start || referenceEnd > clip.end || referenceEnd <= referenceStart
          || referenceEnd - referenceStart > maxSeconds + .001
          || referenceStart - clip.start > .151 || clip.end - referenceEnd > .251) return
      return clip
    })
  }
  async function playFile(path: string) {
    const trimmed = path.trim()
    if (!trimmed || trimmed.includes('\0')) { stop(); options.error('请选择有效的参考音频文件。'); return }
    return load(`/api/files/reference-audio?path=${encodeURIComponent(trimmed)}`, body => {
      const prefix = '/api/files/reference-audio/content?'
      if (!body || typeof body.filename !== 'string' || !body.filename || typeof body.url !== 'string'
          || !body.url.startsWith(prefix) || body.url.includes('#')) return
      const params = new URLSearchParams(body.url.slice(prefix.length))
      if (!params.get('path')) return
      return { url: body.url }
    }, true)
  }
  return { play, playFile, stop }
}
