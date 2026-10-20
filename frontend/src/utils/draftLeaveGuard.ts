// 离开和同路由参数切换共用一次草稿确认；页内 query/hash 不会销毁草稿。
type DraftState = { busyReason: string; dirty: boolean; title: string; content: string }
type Confirmation = {
  title: string; content: string; positiveText: string; negativeText: string
  onPositiveClick: () => void; onNegativeClick: () => void; onClose: () => void; onMaskClick: () => void
}
export function createDraftLeaveGuard(state: () => DraftState, warn: (message: string) => void,
  confirm: (options: Confirmation) => void) {
  let pending: Promise<boolean> | null = null
  function leave(): boolean | Promise<boolean> {
    const draft = state()
    if (draft.busyReason) { warn(draft.busyReason); return false }
    if (!draft.dirty) return true
    if (pending) return pending
    let resolve!: (allowed: boolean) => void
    pending = new Promise<boolean>(done => { resolve = done })
    const result = pending
    let settled = false
    function settle(allowed: boolean) {
      if (settled) return
      settled = true; pending = null; resolve(allowed)
    }
    confirm({ title: draft.title, content: draft.content, positiveText: '放弃修改并离开', negativeText: '继续编辑',
      onPositiveClick: () => settle(true), onNegativeClick: () => settle(false),
      onClose: () => settle(false), onMaskClick: () => settle(false) })
    return result
  }
  return { leave, update: (to: { path: string }, from: { path: string }) => to.path === from.path || leave() }
}
