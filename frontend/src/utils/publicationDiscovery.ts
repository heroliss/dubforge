import { videoFileTime } from './publishVideo.ts'

type ReadTicket = { context: string; epoch: number; active: boolean }

/** 目录只能触发同记录核对；旧列表不能把新回执退回创建或上传阶段。 */
export function publicationObservationChanged(current: any, listed: any): boolean {
  if (!current?.id || listed?.id !== current.id) return false
  const revision = (value: unknown) => typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
  const before = revision(current.revision), after = revision(listed.revision)
  if (before !== null && after !== null && after < before) return false
  const beforeTime = videoFileTime(current.updated_at), afterTime = videoFileTime(listed.updated_at)
  if (beforeTime && afterTime && afterTime.iso < beforeTime.iso) return false
  if (before !== null && after !== null && after > before) return true
  if (typeof listed.status !== 'string' || listed.status === current.status) return false
  if (beforeTime && afterTime && afterTime.iso > beforeTime.iso) return true
  // 旧接口缺少修订/时间时只接受已离开draft的正向事实，不猜其它终态的先后。
  return current.status === 'draft' && listed.status !== 'draft'
}

/** 后台创建记录仍使用页面原有低频轮询；执行结束后保留一次成功尾读。 */
export function createPublicationDiscovery() {
  let context = '', active = false, tail = false, epoch = 0
  function observe(nextContext: string, nextActive: boolean) {
    if (context !== nextContext) {
      context = nextContext; active = false; tail = false; epoch++
    }
    nextActive = !!context && nextActive
    if (active !== nextActive) epoch++
    active = nextActive
    if (active) tail = true
  }
  function read(): ReadTicket | null { return context && (active || tail) ? { context, epoch, active } : null }
  function complete(ticket: ReadTicket) {
    // 执行中发出的读取不能抵消结束后的尾读；失败、切换或新一轮执行也不能抵消。
    if (ticket.context === context && ticket.epoch === epoch && !ticket.active && !active) tail = false
  }
  return { observe, read, complete, cancel: () => observe('', false) }
}
