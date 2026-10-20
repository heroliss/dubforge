type Bounds = { top: number; bottom: number }
type Row = {
  getAttribute: (name: string) => string | null
  getBoundingClientRect: () => Bounds
}
type List = {
  scrollTop: number
  getBoundingClientRect: () => Bounds
  querySelectorAll: (selector: string) => ArrayLike<Row>
}
type ReadingAnchor = { id: string; index: number; offset: number; pageTop: number }

/** Naive UI等页面壳可能使用嵌套滚动，不能只保存window.scrollY。 */
export function listScrollPage(list: HTMLElement | undefined | null) {
  const doc = list?.ownerDocument ?? (typeof document === 'undefined' ? undefined : document)
  const view = doc?.defaultView ?? (typeof window === 'undefined' ? undefined : window)
  let parent = list?.parentElement
  while (view && parent && parent !== doc?.scrollingElement && parent !== doc?.body && parent !== doc?.documentElement) {
    if (/^(auto|scroll|overlay)$/.test(view.getComputedStyle(parent).overflowY)) {
      const element = parent, bounds = element.getBoundingClientRect()
      return { top: element.scrollTop, height: element.clientHeight,
        viewportTop: bounds.top + element.clientTop,
        scrollTo: (top: number) => element.scrollTo({ top, left: element.scrollLeft, behavior: 'instant' }) }
    }
    parent = parent.parentElement
  }
  return { top: view?.scrollY ?? 0, height: view?.innerHeight ?? Infinity, viewportTop: 0,
    scrollTo: (top: number) => view?.scrollTo({ top, left: view.scrollX, behavior: 'instant' }) }
}

/** 保留列表阅读锚点，不控制选中句子、播放进度或筛选内容。 */
export function createListScrollAnchor(options: {
  list: () => List | undefined | null
  allIds: () => readonly (number | string)[]
  page: () => { top: number; height: number; viewportTop?: number }
  scrollPage: (top: number) => void
  afterRender: () => Promise<unknown>
}) {
  let revision = 0
  let previous: ReadingAnchor | null = null
  const saved = new Map<string, ReadingAnchor>()

  function rows() {
    return Array.from(options.list()?.querySelectorAll('[data-id]') ?? [])
      .filter(row => row.getAttribute('data-id') !== null)
  }
  function visibleTop(bounds: Bounds) {
    const page = options.page(), viewportTop = page.viewportTop ?? 0
    const bottom = Math.min(viewportTop + page.height, bounds.bottom)
    const top = Math.max(viewportTop, bounds.top)
    // 列表在屏幕外时也保留内部位置，但不主动把页面滚到列表。
    return bottom > top ? top : bounds.top
  }
  function capture(): ReadingAnchor | null {
    const list = options.list(), items = rows()
    if (!list || !items.length) return null
    const top = visibleTop(list.getBoundingClientRect())
    // DOM顺序与竖向排布一致，用二分查找避免长片上逐一触发布局读取。
    let low = 0, high = items.length
    while (low < high) {
      const middle = (low + high) >>> 1
      if (items[middle].getBoundingClientRect().bottom <= top) low = middle + 1
      else high = middle
    }
    const row = items[Math.min(low, items.length - 1)]!, id = row.getAttribute('data-id')!
    return { id, index: options.allIds().map(String).indexOf(id),
      offset: row.getBoundingClientRect().top - top, pageTop: options.page().top }
  }
  function restore(anchor: ReadingAnchor): boolean {
    const list = options.list(), items = rows()
    if (!list || !items.length) return false
    const order = new Map(options.allIds().map((id, index) => [String(id), index]))
    const row = items.find(item => item.getAttribute('data-id') === anchor.id)
      ?? items.reduce((closest, item) => {
        const index = order.get(item.getAttribute('data-id')!) ?? Infinity
        const nearest = order.get(closest.getAttribute('data-id')!) ?? Infinity
        // 同距离优先后一句，让移除的当前句自然向后衔接。
        return Math.abs(index - anchor.index) < Math.abs(nearest - anchor.index)
          || Math.abs(index - anchor.index) === Math.abs(nearest - anchor.index) && index > nearest ? item : closest
      })
    if (Math.abs(options.page().top - anchor.pageTop) > .5) options.scrollPage(anchor.pageTop)
    const top = visibleTop(list.getBoundingClientRect())
    list.scrollTop += row.getBoundingClientRect().top - top - anchor.offset
    return true
  }
  async function transition(from: string, to: string): Promise<boolean> {
    if (from === to) return false
    const token = ++revision, anchor = capture()
    if (anchor) {
      saved.delete(from)
      saved.set(from, anchor)
      previous = anchor
      // 搜索词也能作为key；不为逐字搜索无限保存历史。
      while (saved.size > 32) saved.delete(saved.keys().next().value!)
    }
    const target = saved.get(to) ?? anchor ?? previous
    await options.afterRender()
    if (token !== revision || !target) return false
    return restore(target)
  }
  return {
    transition,
    async update(key: string, apply: () => void): Promise<boolean> {
      const token = ++revision, anchor = capture()
      if (anchor) { saved.set(key, anchor); previous = anchor }
      const target = anchor ?? saved.get(key) ?? previous
      apply()
      await options.afterRender()
      return token === revision && !!target && restore(target)
    },
    cancel() { revision++ },
    reset() { revision++; saved.clear(); previous = null },
  }
}
