import assert from 'node:assert/strict'
import test from 'node:test'
import { createListScrollAnchor, listScrollPage } from '../src/utils/listScrollAnchor.ts'

function harness(options: { count?: number; pageTop?: number; viewportTop?: number; pageTail?: number } = {}) {
  const all = Array.from({ length: options.count ?? 100 }, (_, index) => index + 1)
  let shown = [...all], scrollTop = 0, pageTop = options.pageTop ?? 0, boundsReads = 0
  const viewportTop = options.viewportTop ?? 0
  const pageTail = options.pageTail ?? 100
  const callbacks: (() => void)[] = []
  const height = () => Math.min(200, shown.length * 40)
  const listTop = () => viewportTop + 300 - pageTop
  const list = {
    get scrollTop() { return scrollTop },
    set scrollTop(value: number) { scrollTop = Math.max(0, Math.min(shown.length * 40 - height(), value)) },
    getBoundingClientRect: () => ({ top: listTop(), bottom: listTop() + height() }),
    querySelectorAll: () => shown.map((id, index) => ({
      getAttribute: () => String(id),
      getBoundingClientRect: () => {
        boundsReads++
        return { top: listTop() + index * 40 - scrollTop, bottom: listTop() + (index + 1) * 40 - scrollTop }
      },
    })),
  }
  function update(ids: number[]) {
    shown = ids
    list.scrollTop = scrollTop
    // 模拟列表高度缩小时浏览器对内外两层scrollTop的夹紧。
    pageTop = Math.min(pageTop, 300 + height() + pageTail - 400)
  }
  const controller = createListScrollAnchor({ list: () => list, allIds: () => all,
    page: () => ({ top: pageTop, height: 400, viewportTop }),
    scrollPage: value => { pageTop = Math.max(0, Math.min(value, 300 + height() + pageTail - 400)) },
    afterRender: () => new Promise<void>(resolve => callbacks.push(resolve)),
  })
  async function transition(from: string, to: string, ids: number[]) {
    const result = controller.transition(from, to)
    update(ids)
    callbacks.splice(0).forEach(resolve => resolve())
    return result
  }
  return { all, list, update, controller, transition,
    flush: () => callbacks.splice(0).forEach(resolve => resolve()),
    get pageTop() { return pageTop }, get boundsReads() { return boundsReads },
  }
}

test('按最上方可见句ID和负偏移保留真实容器阅读位置', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40 + 15
  assert.equal(await view.transition('all', 'odd', view.all.filter(id => id % 2)), true)
  assert.equal(view.list.scrollTop, 20 * 40 + 15)
  assert.equal(await view.transition('odd', 'all', view.all), true)
  assert.equal(view.list.scrollTop, 40 * 40 + 15)
})

test('当前句被过滤后选择全局顺序最近句，同距离优先后一句', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40 + 10
  const sparse = [1, 20, 39, 43, 60, 70, 80, 90, 100]
  await view.transition('all', 'sparse', sparse)
  assert.equal(view.list.scrollTop, 3 * 40 + 10)
})

test('每种筛选分别记住阅读锚点，来回切换不跳回开头', async () => {
  const view = harness()
  const odd = view.all.filter(id => id % 2)
  view.list.scrollTop = 40 * 40 + 9
  await view.transition('all', 'odd', odd)
  view.list.scrollTop = 30 * 40 + 7
  await view.transition('odd', 'all', view.all)
  assert.equal(view.list.scrollTop, 40 * 40 + 9)
  await view.transition('all', 'odd', odd)
  assert.equal(view.list.scrollTop, 30 * 40 + 7)
})

test('空结果不抹掉锚点，返回全部恢复内层与外层页面位置', async () => {
  const view = harness({ pageTop: 180 })
  view.list.scrollTop = 40 * 60 + 12
  assert.equal(await view.transition('all', 'empty', []), false)
  assert.equal(view.list.scrollTop, 0)
  assert.equal(view.pageTop, 0)
  await view.transition('empty', 'all', view.all)
  assert.equal(view.list.scrollTop, 40 * 60 + 12)
  assert.equal(view.pageTop, 180)
})

test('空结果再到另一筛选仍沿用最后的真实阅读锚点', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40 + 5
  await view.transition('all', 'empty', [])
  await view.transition('empty', 'odd', view.all.filter(id => id % 2))
  assert.equal(view.list.scrollTop, 20 * 40 + 5)
})

test('部分列表在外层viewport上方时按屏幕首句保留而非内部首句', async () => {
  const view = harness({ pageTop: 350, viewportTop: 60, pageTail: 500 })
  // list顶部在10px，viewport从60px开始，实际看到的首行比容器首行晚50px。
  view.list.scrollTop = 40 * 40 + 5
  await view.transition('all', 'odd', view.all.filter(id => id % 2))
  // 旧首行是42，最近可用为43，负偏移-15；页面本身先回到原scrollTop。
  assert.equal(view.list.scrollTop, 20 * 40 + 5)
})

test('快速切换只允许最后一轮异步恢复生效', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40 + 5
  const first = view.controller.transition('all', 'odd')
  view.update(view.all.filter(id => id % 2))
  view.list.scrollTop = 20 * 40 + 5
  const second = view.controller.transition('odd', 'all')
  view.update(view.all)
  view.flush()
  assert.equal(await first, false)
  assert.equal(await second, true)
  assert.equal(view.list.scrollTop, 40 * 40 + 5)
})

test('手动滚动取消待恢复，不被nextTick后的旧任务抢回', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40
  const pending = view.controller.transition('all', 'odd')
  view.update(view.all.filter(id => id % 2))
  view.controller.cancel()
  view.list.scrollTop = 120
  view.flush()
  assert.equal(await pending, false)
  assert.equal(view.list.scrollTop, 120)
})

test('定位与跟随主动取消恢复后，目标滚动保持优先', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40
  const pending = view.controller.transition('all', 'odd')
  view.update(view.all.filter(id => id % 2))
  view.controller.cancel()
  view.update(view.all)
  view.list.scrollTop = 40 * 70
  view.flush()
  assert.equal(await pending, false)
  assert.equal(view.list.scrollTop, 40 * 70)
})

test('重置清除项目历史和已排队恢复', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40
  const pending = view.controller.transition('all', 'odd')
  view.controller.reset()
  view.update([])
  view.flush()
  assert.equal(await pending, false)
  await view.transition('empty', 'new-project', view.all)
  assert.equal(view.list.scrollTop, 0)
})

test('页面停在列表上方时不会自动滚到屏幕外的列表', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40 + 4
  await view.transition('all', 'odd', view.all.filter(id => id % 2))
  assert.equal(view.pageTop, 0)
  assert.equal(view.list.scrollTop, 20 * 40 + 4)
})

test('长列表只读取少量行布局，不逐句测量10000行', async () => {
  const view = harness({ count: 10000 })
  view.list.scrollTop = 40 * 5000 + 4
  await view.transition('all', 'odd', view.all.filter(id => id % 2))
  assert.ok(view.boundsReads < 30, `读取布局${view.boundsReads}次`)
})

test('缺失DOM或相同筛选时安全跳过', async () => {
  const controller = createListScrollAnchor({ list: () => undefined, allIds: () => [],
    page: () => ({ top: 0, height: 500 }), scrollPage() { throw new Error('不应滚动') },
    afterRender: async () => {} })
  assert.equal(await controller.transition('all', 'empty'), false)
  assert.equal(await controller.transition('all', 'all'), false)
})

test('找到Naive UI外层scrollport，保留真实viewport和scrollTop', () => {
  let called: unknown
  const document: any = { scrollingElement: {}, body: {}, documentElement: {} }
  const host: any = { parentElement: document.body, scrollTop: 900, scrollLeft: 2, clientTop: 1,
    clientHeight: 660, getBoundingClientRect: () => ({ top: 58, bottom: 720 }),
    scrollTo(value: unknown) { called = value } }
  document.defaultView = { scrollY: 0, innerHeight: 720,
    getComputedStyle: (element: unknown) => ({ overflowY: element === host ? 'auto' : 'visible' }) }
  const middle: any = { parentElement: host }
  const port = listScrollPage({ parentElement: middle, ownerDocument: document } as any)
  assert.equal(port.top, 900)
  assert.equal(port.viewportTop, 59)
  assert.equal(port.height, 660)
  port.scrollTo(1000)
  assert.deepEqual(called, { top: 1000, left: 2, behavior: 'instant' })
})

test('无嵌套scrollport时回退window页面滚动', () => {
  let called: unknown
  const document: any = { body: {}, documentElement: {} }
  document.scrollingElement = document.documentElement
  document.defaultView = { scrollY: 180, scrollX: 4, innerHeight: 720,
    getComputedStyle: () => ({ overflowY: 'visible' }), scrollTo(value: unknown) { called = value } }
  const port = listScrollPage({ parentElement: document.body, ownerDocument: document } as any)
  assert.equal(port.top, 180)
  assert.equal(port.height, 720)
  assert.equal(port.viewportTop, 0)
  port.scrollTo(210)
  assert.deepEqual(called, { top: 210, left: 4, behavior: 'instant' })
})

test('异步对照更新同一筛选成员时仍按旧DOM锚点恢复位置', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40 + 12
  const pending = view.controller.update('assembly', () => view.update(view.all.filter(id => id !== 1)))
  view.flush()
  assert.equal(await pending, true)
  assert.equal(view.list.scrollTop, 39 * 40 + 12)
})

test('同一筛选数据更新后的恢复仍让手动定位优先', async () => {
  const view = harness()
  view.list.scrollTop = 40 * 40
  const pending = view.controller.update('assembly', () => view.update(view.all.filter(id => id !== 1)))
  view.controller.cancel()
  view.list.scrollTop = 400
  view.flush()
  assert.equal(await pending, false)
  assert.equal(view.list.scrollTop, 400)
})
