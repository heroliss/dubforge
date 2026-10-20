import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'

let server: ViteDevServer, PartsEditor: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  PartsEditor = (await server.ssrLoadModule('/src/components/PublishPartsEditor.vue')).default
})
after(() => server.close())

const chinese = () => ({ id: 1, name: 'final_version_chinese.mp4', version_name: '中文配音版',
  version_summary: '中文配音 · 双语字幕', path: 'D:\\视频\\final_version_chinese.mp4', size: 2 * 1024 * 1024,
  exists: true, has_dubbing: true, review_ready: true })
const source = () => ({ id: 2, name: 'source.mp4', path: 'D:\\视频\\source.mp4', size: 1024, exists: true,
  is_source: true, has_dubbing: false, review_ready: null })
const parts = () => [{ artifact_id: 1, title: '旅行纪录片（中文）' }, { artifact_id: 2, title: '旅行纪录片（原声）' }]

async function render(props: any = {}) {
  let page: any
  const events: any[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(PartsEditor, { parts: [], files: [], ...props,
      onAdd: (file: any) => events.push(['add', file]),
      onRemove: (index: number) => events.push(['remove', index]),
      onMove: (index: number, delta: number) => events.push(['move', index, delta]),
      onRename: (index: number, value: string) => events.push(['rename', index, value]),
      onRefresh: () => events.push(['refresh']),
    }),
  }) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'PublishPartsEditor') page = this.$.setupState } })
  return { html: await renderToString(app), page, events }
}
function partCards(html: string) { return [...html.matchAll(/<li\b[^>]*>[\s\S]*?<\/li>/g)].map(row => row[0]).filter(row => /class="[^"]*\bpart-card\b/.test(row)) }
function text(html: string) { return html.replace(/<[^>]*>/g, '') }

test('一个分P清楚映射一个视频，显示名称与实际成片信息同时可见', async () => {
  const { html } = await render({ parts: parts(), files: [chinese(), source()] })
  const cards = partCards(html)
  assert.equal(cards.length, 2)
  assert.match(cards[0], /P1/); assert.match(cards[0], /旅行纪录片（中文）/)
  assert.match(cards[0], /中文配音版/); assert.match(cards[0], /中文配音 · 双语字幕/)
  assert.match(cards[0], /final_version_chinese\.mp4/); assert.match(cards[0], /2\.0 MB/)
  assert.match(cards[0], /定位文件/); assert.match(cards[0], /已按当前规则处理/)
  assert.match(cards[0], /<input\b[^>]*aria-label="P1 B站分P名称"/)
  assert.match(cards[1], /P2/); assert.match(cards[1], /旅行纪录片（原声）/)
  assert.match(cards[1], /原声原片/); assert.match(cards[1], /source\.mp4/)
  assert.match(html, /一个视频文件对应一个分P/); assert.match(html, /不会自动切片/)
  assert.match(html, /分P名称只修改B站显示名称/)
})

test('数组顺序决定Pn和候选的已加入位置，不靠成片列表位置匹配', async () => {
  const { html } = await render({ parts: parts().reverse(), files: [chinese(), source()] })
  const cards = partCards(html)
  assert.match(cards[0], /P1/); assert.match(cards[0], /source\.mp4/)
  assert.match(cards[1], /P2/); assert.match(cards[1], /final_version_chinese\.mp4/)
  assert.match(html, /已加入 P2/); assert.match(html, /已加入 P1/)
})

test('加入、改名、调整顺序和移除发出明确事件，不直接修改父组件草稿', async () => {
  const draft = [parts()[0]], files = [chinese(), source()]
  const { page, events } = await render({ parts: draft, files })
  page.add(files[1]); page.rename(0, '新的分P名称'); page.remove(0); page.refresh()
  assert.deepEqual(events, [['add', files[1]], ['rename', 0, '新的分P名称'], ['remove', 0], ['refresh']])
  assert.deepEqual(draft, [parts()[0]])
  const ordered = await render({ parts: parts(), files })
  ordered.page.move(0, 1); ordered.page.move(1, -1)
  assert.deepEqual(ordered.events, [['move', 0, 1], ['move', 1, -1]])
  assert.deepEqual(ordered.page.props.parts, parts())
})

test('重复选用与越界操作由事件处理器拦截，按钮边界可访问', async () => {
  const { html, page, events } = await render({ parts: parts(), files: [chinese(), source()] })
  page.add(chinese()); page.add(source()); page.move(0, -1); page.move(1, 1); page.move(0, 2)
  page.move(-1, 1); page.remove(2); page.remove(0.5); page.rename(-1, '错误'); page.rename(0, parts()[0].title)
  assert.deepEqual(events, [])
  assert.match(html, /aria-label="上移 P1"[^>]*disabled|disabled[^>]*aria-label="上移 P1"/)
  assert.match(html, /aria-label="下移 P2"[^>]*disabled|disabled[^>]*aria-label="下移 P2"/)
  assert.equal((text(html).match(/已加入 P/g) || []).length, 2)
  assert.doesNotMatch(text(html), /加入 P3/)
})

test('缺失文件保留对应分P并说明还原或移除；缺失候选不能加入', async () => {
  const missing = { ...chinese(), exists: false }
  const { html, page, events } = await render({ parts: parts(), files: [missing] })
  assert.equal(partCards(html).length, 2)
  assert.match(partCards(html)[0], /final_version_chinese\.mp4/)
  assert.match(html, /本地文件缺失，已保留此分P/)
  assert.match(html, /文件已不在当前视频列表中/)
  page.add({ ...source(), id: 3, exists: false }); page.add({ ...source(), id: 4, exists: undefined })
  assert.deepEqual(events, [])
})

test('首次读取前和刷新期间不把未读取状态误报为文件丢失', async () => {
  for (const state of [{ ready: false }, { ready: true, loading: true }]) {
    const { html, page, events } = await render({ parts: parts(), files: [], ...state })
    assert.equal(partCards(html).length, 2)
    assert.match(html, /已加入的分P保留/)
    assert.doesNotMatch(html, /本地文件缺失|文件已不在当前视频列表中/)
    page.add(source()); assert.deepEqual(events, [])
  }
  const refreshing = await render({ parts: [parts()[0]], files: [{ ...chinese(), exists: false }], loading: true })
  assert.match(refreshing.html, /中文配音版/)
  assert.doesNotMatch(refreshing.html, /本地文件缺失|文件不存在/)
  refreshing.page.refresh(); assert.deepEqual(refreshing.events, [])
})

test('需要审查修订的候选说明风险但允许加入，最终由上传预检拦截', async () => {
  const file = { ...chinese(), review_ready: false, review_reason: '字幕规则已修改，请重新生成' }
  const { html, page, events } = await render({ files: [file] })
  assert.match(html, /需审查修订/); assert.match(html, /字幕规则已修改/); assert.match(html, /加入 P1/)
  assert.equal(page.canAdd(file), true)
  page.add(file); assert.deepEqual(events, [['add', file]])
})

test('只读草稿保留分P引用，不能表述成文件已经确认或上传', async () => {
  const { html, page, events } = await render({ frozen: true, unconfirmed: true, parts: parts(), files: [chinese(), source()] })
  assert.match(html, /草稿分P清单/); assert.match(html, /已保存的视频引用/)
  assert.match(html, /尚未确认上传文件信息/); assert.match(html, /尚未核对文件和开始上传/)
  assert.doesNotMatch(html, /已确认分P|已确认文件|以上为确认时|本地文件缺失|可添加的视频/)
  page.remove(0); page.add(chinese()); page.rename(0, '改名'); assert.deepEqual(events, [])
})

test('已确认稿件只用文件快照，当前版本名和审查状态不能替代原记录', async () => {
  const snapshot = [{ artifact_id: 1, path: 'E:\\发布备份\\confirmed_old.mp4', size: 5 * 1024 * 1024, duration: 62, has_dubbing: true }]
  const { html, page, events } = await render({ frozen: true, parts: [parts()[0]], files: [{ ...chinese(), exists: false, review_ready: false }], snapshotParts: snapshot })
  assert.match(html, /已确认分P/); assert.match(html, /已确认文件/); assert.match(html, /confirmed_old\.mp4/)
  assert.match(html, /5\.0 MB/); assert.match(html, /1分02秒/); assert.match(html, /含AI配音/)
  assert.doesNotMatch(html, /中文配音版|final_version_chinese\.mp4|需审查修订|本地文件缺失|可添加的视频/)
  assert.match(html, /不代表B站上传或审核状态/)
  page.add(source()); page.rename(0, '改名'); page.move(0, 1); page.remove(0); page.refresh()
  assert.deepEqual(events, [])
})

test('旧稿件缺失快照如实提示，不能用今天候选或分P标题推断原文件', async () => {
  const { html } = await render({ frozen: true, parts: parts(), files: [chinese(), source()] })
  assert.equal(partCards(html).length, 2)
  assert.equal((text(html).match(/原文件信息未记录/g) || []).length, 2)
  assert.match(html, /旅行纪录片（中文）/)
  assert.doesNotMatch(html, /中文配音版|source\.mp4|已按当前规则处理|本地文件缺失/)
})

test('已确认快照按artifact_id映射，不因快照数组排序错配；缺项保持未知', async () => {
  const { html } = await render({ frozen: true, parts: parts().reverse(), snapshotParts: [
    { artifact_id: 1, path: '/original/chinese.mp4', size: 3, has_dubbing: true },
    { artifact_id: 2, path: '/original/source.mp4', size: 4, has_dubbing: false },
  ] })
  const cards = partCards(html)
  assert.match(cards[0], /P1/); assert.match(cards[0], /source\.mp4/); assert.match(cards[0], /原声视频/)
  assert.match(cards[1], /P2/); assert.match(cards[1], /chinese\.mp4/); assert.match(cards[1], /含AI配音/)
})

test('运行中禁用编辑保留所有信息，直接调用处理器也不发出写事件', async () => {
  const { html, page, events } = await render({ parts: parts(), files: [chinese(), source()], disabled: true })
  page.add({ ...source(), id: 3 }); page.rename(0, '改名'); page.move(0, 1); page.remove(0); page.refresh()
  assert.deepEqual(events, [])
  assert.match(html, /中文配音版/); assert.match(html, /source\.mp4/)
  assert.equal(page.mutable, false)
})

test('空清单引导添加P1，未知旧成片仍显示实际文件且没有NaN日期', async () => {
  const { html } = await render({ files: [{ id: 9, path: '/videos/historical.mp4', size: 0, exists: true }] })
  assert.match(html, /从下方选择一个文件，即成为 P1/)
  assert.match(html, /historical\.mp4/); assert.match(html, /0 B/)
  assert.doesNotMatch(html, /Invalid Date|NaN/)
})

test('同名成片通过生成时间区分，候选和已选分P均显示名称、时间、摘要与文件名', async () => {
  const files = [
    { ...chinese(), generated_at: '2026-10-17T10:20:30+08:00', created_at: '2026-10-15T10:00:00+08:00' },
    { ...chinese(), id: 3, name: 'final_second.mp4', generated_at: '2026-10-17T11:40:50+08:00' },
  ]
  const { html, page } = await render({ parts: [parts()[0]], files })
  const first = page.candidates[0].info, second = page.candidates[1].info
  assert.notEqual(first.timeText, second.timeText)
  assert.match(first.timeText, /^生成时间 /)
  assert.ok(partCards(html)[0].includes(first.timeText))
  assert.ok(html.includes(second.timeText))
  assert.ok(html.includes(`将 ${second.title}（${second.timeText}） 加入 P2`))
  assert.match(html, /final_second\.mp4/)
  assert.match(partCards(html)[0], /中文配音版/)
  assert.match(partCards(html)[0], /中文配音 · 双语字幕/)
  assert.doesNotMatch(html, /登记时间/)
})

test('生成时名称是主标题，后续重命名的设置名称单独说明', async () => {
  const file = { ...chinese(), version_generated_name: '中文配音 · 审查修订', version_name: '明天准备再调的配音', generated_at: '2026-10-17T11:00:00+08:00' }
  const { html } = await render({ files: [file], parts: [parts()[0]] })
  const card = partCards(html)[0]
  assert.match(card, /class="version-name"[^>]*>中文配音 · 审查修订<\/strong>/)
  assert.match(card, /当前版本名：明天准备再调的配音/)
  assert.match(card, /生成时间/)
})

test('冻结记录可显示当时的名称与生成时间，不能借用当前文件的资料补齐旧快照', async () => {
  const current = { ...chinese(), version_name: '现在的全新名称', version_generated_name: '今天重新生成的版本', generated_at: '2026-10-17T12:00:00+08:00' }
  const snapshot = { artifact_id: 1, path: 'E:\\发布备份\\confirmed_old.mp4', size: 500,
    version_generated_name: '当时生成的原声版', version_name: '当时的版本名', version_summary: '当时的字幕布局', generated_at: '2026-10-10T09:08:07+08:00' }
  const saved = await render({ frozen: true, parts: [parts()[0]], files: [current], snapshotParts: [snapshot] })
  assert.match(saved.html, /当时生成的原声版/)
  assert.match(saved.html, /当时的字幕布局/)
  assert.match(saved.html, /生成时间 2026-10-10/)
  assert.doesNotMatch(saved.html, /今天重新生成的版本|现在的全新名称|2026-10-17/)
  const unknown = await render({ frozen: true, parts: [parts()[0]], files: [current],
    snapshotParts: [{ artifact_id: 1, path: snapshot.path, size: 500 }] })
  assert.match(unknown.html, /confirmed_old\.mp4/)
  assert.doesNotMatch(unknown.html, /class="version-name"|生成时间|登记时间|当前版本名|今天重新生成的版本|现在的全新名称/)
})
