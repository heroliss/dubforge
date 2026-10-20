/** 只处理媒体输出增益；不接管播放、定位或倍速。大于 1 时才建立 Web Audio 节点。 */
export class MediaListeningGain {
  private context?: AudioContext
  private nodes = new Map<HTMLMediaElement, { source: MediaElementAudioSourceNode; gain: GainNode }>()
  private warned = false
  private unavailable = false
  private resuming?: Promise<boolean>
  private warning: (message: string) => void
  constructor(warning: (message: string) => void = () => {}) { this.warning = warning }
  /** 显式播放前先恢复输出链，避免媒体已计时而 AudioContext 仍静音。 */
  prepare(gain = 1) {
    if (!this.context && !this.unavailable && gain > 1) {
      try { this.context = new AudioContext() }
      catch { this.unavailable = true; this.warnUnavailable() }
    }
    return this.resume()
  }
  get ready() { return !this.context || this.context.state === 'running' }
  private warnUnavailable() {
    if (!this.warned) {
      this.warned = true
      this.warning('浏览器无法额外放大此音轨，当前最多按100%试听；请生成成片试片核对正增益效果。')
    }
  }
  set(media: HTMLMediaElement, value: number) {
    const gain = Number.isFinite(value) ? Math.max(0, value) : 1
    let node = this.nodes.get(media)
    if (!node && !this.unavailable && (gain > 1 || this.context)) {
      try {
        this.context ||= new AudioContext()
        const source = this.context.createMediaElementSource(media), level = this.context.createGain()
        source.connect(level); level.connect(this.context.destination)
        node = { source, gain: level }; this.nodes.set(media, node)
        void this.resume()
      } catch {
        this.unavailable = true
        this.warnUnavailable()
      }
    }
    if (node) { media.volume = 1; node.gain.gain.value = gain }
    else media.volume = Math.min(1, gain)
  }
  async resume() {
    const context = this.context
    if (!context) return true
    if (context.state === 'running') return true
    if (context.state === 'closed') return false
    if (this.resuming) return this.resuming
    const request = (async () => {
      try { await context.resume(); return this.context === context && context.state === 'running' }
      catch {
        if (this.context === context && !this.warned) { this.warned = true; this.warning('浏览器尚未允许声音输出，请再次点击播放后试听。') }
        return false
      }
    })()
    this.resuming = request
    try { return await request }
    finally { if (this.resuming === request) this.resuming = undefined }
  }
  release(media: HTMLMediaElement) {
    const node = this.nodes.get(media)
    if (!node) return
    node.source.disconnect(); node.gain.disconnect(); this.nodes.delete(media)
  }
  dispose() {
    for (const media of this.nodes.keys()) this.release(media)
    if (this.context && this.context.state !== 'closed') void this.context.close().catch(() => {})
    this.context = undefined
  }
}
