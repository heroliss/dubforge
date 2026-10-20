import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { parse } from '@vue/compiler-sfc'
import postcss from 'postcss'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'

let server: ViteDevServer, Studio: any
const fixture = () => {
  const rows = [
    { id: 'automatic', source_hash: 'a', author: '自动项作者', timestamp: 100, like_count: 42, text: 'Original automatic', text_zh: '自动项原始译文' },
    { id: 'excluded', source_hash: 'b', author: '排除项作者', timestamp: 200, like_count: 0, text: 'Original excluded', text_zh: '已排除评论' },
    { id: 'manual', source_hash: 'c', author: '手动项作者', timestamp: 300, like_count: 9, text: 'Original manual', text_zh: '手动项原始译文', is_pinned: true },
    { id: 'untranslated', source_hash: 'd', author: '未译项作者', timestamp: 400, text: 'Untranslated source', text_zh: '' },
    { id: 'blocked', source_hash: 'e', author: '', timestamp: 500, text: 'Local review source', text_zh: '审查待处理', review_blocked: true, review_reasons: ['自定义规则命中'] },
  ]
  return {
    rows, available_assessments: rows.map((row, index) => ({ id: row.id, eligible: index !== 1, score: [88, 30, 95, 90, 80][index], reason: `第${index + 1}项评分理由` })),
    filter: { items: [], assessments: [] }, rating_progress: { saved: rows.length, total: rows.length, remaining: 0 },
    overlay: { items: [
      { ...rows[0], display_text: '已编辑的自动项中文', source_text: 'Edited original automatic', selection_origin: 'automatic' },
      { ...rows[2], display_text: '已编辑的手动项中文', source_text: 'Edited original manual', selection_origin: 'manual' },
    ], excluded_ids: [{ id: 'excluded', source_hash: 'b' }], order_mode: 'chronological', settings: { language_mode: 'bilingual' } },
    overlay_revision: 'presentation-r1', defaults: {}, job: null, fields: [],
    configuration: { values: { 'comments.count': 2, 'comments.prompt': '保留相关评论' }, defaults: {}, sources: {}, parent_sources: {}, overrides: {} },
  }
}
let activeFixture = fixture()
const requests: { url: string; options?: RequestInit }[] = []
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Studio = (await server.ssrLoadModule('/src/components/CommentStudioPanel.vue')).default
  mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push({ url: String(url), options })
    return { ok: true, json: async () => String(url).includes('/api/configuration/')
      ? { name: '本项目', defaults: {}, overrides: {}, sources: {}, parent_sources: {}, sections: {} } : activeFixture } as any
  })
})
after(async () => { mock.restoreAll(); await server.close() })
async function render(options: { blocked?: boolean; data?: any; seed?: (page: any) => void } = {}) {
  activeFixture = options.data || fixture()
  let page: any, preview: any
  const buttons: any[] = [], checkboxes: any[] = [], textInputs: any[] = []
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: {
    render: () => h(Studio, { uid: 'comment-presentation', videoDuration: 1200, blocked: options.blocked }),
  } }] })
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(RouterView))) })
  app.use(router); await router.push('/projects/comment-presentation/comments'); await router.isReady(); setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'CommentStudioPanel') { page = this.$.setupState; page.state = activeFixture; page.hydrate(activeFixture) }
    if (this.$options.__name === 'CommentPreview') preview = this
    if (this.$options.name === 'Button') buttons.push(this)
    if (this.$options.name === 'Checkbox' && this.$attrs['aria-label']?.startsWith('选择评论')) checkboxes.push(this)
    if (this.$options.name === 'Input' && /^第 \d+ 条(?:中文|原文)上屏文字$/.test(this.$props.inputProps?.['aria-label'] || '')) textInputs.push(this)
  }, serverPrefetch() {
    if (this.$options.__name === 'CommentStudioPanel') return this.$.setupState.load().then(() => options.seed?.(page))
  } })
  const html = await renderToString(app)
  return { html, page, preview, buttons, checkboxes, textInputs }
}
const withClass = (buttons: any[], name: string) => buttons.filter(button => String(button.$attrs.class || '').split(/\s+/).includes(name))
const click = async (button: any) => {
  assert.ok(button, '必须找到实际渲染的按钮')
  button.$.setupState.handleClick({ preventDefault() {}, stopPropagation() {} }); await nextTick()
}
const articleParts = (html: string, className: string) => [...html.matchAll(/<article\b[^>]*>[\s\S]*?<\/article>/g)]
  .map(match => match[0]).filter(article => new RegExp(`class="[^"]*\\b${className}\\b`).test(article))
const opening = (html: string) => html.match(/^<[^>]+>/)![0]
const text = (html: string) => html.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim()
const noWrites = (from: number) => assert.equal(requests.slice(from).filter(request => request.options?.method && request.options.method !== 'GET').length, 0)

test('评论文章把序号、作者与时间点赞、评分选择状态、正文和操作分成可识别区域', async () => {
  const { html } = await render(), rows = articleParts(html, 'comment-row')
  assert.equal(rows.length, 5)
  const authors = ['自动项作者', '排除项作者', '手动项作者', '未译项作者', '匿名观众']
  for (const [index, row] of rows.entries()) {
    assert.ok(opening(row).includes(`aria-label="评论：${authors[index]}"`))
    assert.match(row, /<header\b[^>]*class="comment-heading"/)
    assert.match(row, /class="comment-identity"/); assert.match(row, /class="comment-facts"/)
    assert.ok(row.includes(`title="当前列表第 ${index + 1} 条"`))
    assert.match(row, new RegExp(`class="comment-number"[^>]*>0${index + 1}<`))
    assert.match(row, /class="comment-state-group"[^>]*role="group"[^>]*aria-label="评分与选择状态"/)
    assert.match(row, /class="[^"]*\bcomment-copy\b/)
    assert.ok(row.includes(`aria-label="${authors[index]}的评论操作"`))
  }
  assert.match(rows[0], /aria-label="42 个赞"/)
  assert.match(rows[1], /aria-label="0 个赞"/)
  assert.ok(text(rows[0]).includes('88 分')); assert.ok(text(rows[0]).includes('自动选择'))
  assert.ok(text(rows[1]).includes('30 分')); assert.ok(text(rows[1]).includes('手动排除'))
  assert.ok(text(rows[2]).includes('95 分')); assert.ok(text(rows[2]).includes('手动保留')); assert.ok(text(rows[2]).includes('置顶'))
  assert.ok(text(rows[3]).includes('90 分')); assert.ok(text(rows[3]).includes('仅原文 · 未译'))
  assert.ok(text(rows[4]).includes('本地审查命中 · 不上屏'))
  assert.match(opening(rows[0]), /\bselected\b/); assert.match(opening(rows[2]), /\bselected\b/)
  assert.doesNotMatch(opening(rows[1]), /\bselected\b/)
})

test('已选文章明确上屏位置与作者，保留来源、人工双语文字和预览定位状态', async () => {
  const { html, page, textInputs } = await render({ seed: page => { page.previewFocusId = 'manual'; page.highlightedId = 'manual' } })
  const selected = articleParts(html, 'edit-row'), rows = articleParts(html, 'comment-row')
  assert.equal(selected.length, 2)
  for (const [index, author] of ['自动项作者', '手动项作者'].entries()) {
    assert.ok(opening(selected[index]).includes(`aria-label="第${index + 1}条上屏评论：${author}"`))
    assert.match(selected[index], /<header\b[^>]*class="edit-row-heading"/)
    assert.match(selected[index], /class="edit-number"/); assert.match(selected[index], /class="edit-position"/)
    assert.ok(text(selected[index]).includes(`第 ${index + 1} 条 · ${5 + index * 15} 秒`))
    assert.ok(selected[index].includes(`aria-label="第${index + 1}条上屏评论操作"`))
    assert.ok(selected[index].indexOf('edit-row-heading') < selected[index].indexOf('bilingual-editor'))
    assert.ok(selected[index].indexOf('bilingual-editor') < selected[index].indexOf('edit-actions'))
    assert.ok(selected[index].includes('中文上屏文字')); assert.ok(selected[index].includes('原文上屏文字'))
  }
  assert.match(opening(selected[1]), /\bis-focused\b/); assert.doesNotMatch(opening(selected[0]), /\bis-focused\b/)
  assert.match(opening(rows[2]), /\blocated-comment\b/)
  assert.ok(text(selected[0]).includes('自动选择')); assert.ok(text(selected[1]).includes('手动保留'))
  assert.deepEqual(textInputs.map(input => [input.$props.inputProps['aria-label'], input.$props.value]), [
    ['第 1 条中文上屏文字', '已编辑的自动项中文'], ['第 1 条原文上屏文字', 'Edited original automatic'],
    ['第 2 条中文上屏文字', '已编辑的手动项中文'], ['第 2 条原文上屏文字', 'Edited original manual'],
  ])
  assert.deepEqual(page.items.map((item: any) => item.selection_origin), ['automatic', 'manual'])
})

test('按评分浏览只改变阅读顺序及列表序号，原平台顺序和已选上屏排列保持不变', async () => {
  const plain = await render(), sorted = await render({ seed: page => { page.sortByScore = true } })
  const rows = articleParts(sorted.html, 'comment-row')
  assert.deepEqual(rows.map(row => opening(row).match(/aria-label="评论：([^"]*)"/)![1]),
    ['手动项作者', '未译项作者', '自动项作者', '匿名观众', '排除项作者'])
  for (const [index, row] of rows.entries()) assert.ok(row.includes(`title="当前列表第 ${index + 1} 条"`))
  assert.deepEqual(sorted.page.rows.map((row: any) => row.id), fixture().rows.map(row => row.id))
  assert.deepEqual(sorted.page.items.map((item: any) => item.id), ['automatic', 'manual'])
  assert.equal(sorted.page.snapshot, plain.page.snapshot)
  const cards = articleParts(sorted.html, 'edit-row')
  assert.ok(opening(cards[0]).includes('第1条上屏评论：自动项作者'))
  assert.ok(opening(cards[1]).includes('第2条上屏评论：手动项作者'))
})

test('第二页的列表序号接续上一页，不与独立上屏序号混用', async () => {
  const data: any = fixture()
  for (let index = 6; index <= 25; index++) data.rows.push({ id: 'extra-' + index, source_hash: 'extra-' + index,
    author: '追加作者' + index, timestamp: index * 100, text: 'Original ' + index, text_zh: '追加中文' + index })
  const { html, page } = await render({ data, seed: page => { page.page = 2 } }), rows = articleParts(html, 'comment-row')
  assert.equal(rows.length, 5)
  for (const [index, row] of rows.entries()) {
    assert.ok(row.includes(`title="当前列表第 ${21 + index} 条"`))
    assert.ok(row.includes(`>${21 + index}</span>`))
  }
  assert.deepEqual(page.items.map((item: any) => item.id), ['automatic', 'manual'])
  assert.ok(html.includes('aria-label="第1条上屏评论：自动项作者"'))
})

test('卡片定位与临时试放按钮仍指向对应评论，不改变选择、文字、排除或上屏排序', async () => {
  const { page, buttons } = await render(), from = requests.length, snapshot = page.snapshot
  const actions = withClass(withClass(buttons, 'comment-action'), 'preview-action')
  assert.equal(actions.length, 7)
  await click(actions[2]); assert.equal(page.previewFocusId, 'manual'); assert.equal(page.highlightedId, 'manual')
  await click(actions[5]); assert.equal(page.previewFocusId, 'automatic'); assert.equal(page.temporaryPreviewId, '')
  await click(actions[3]); assert.equal(page.previewFocusId, 'untranslated'); assert.equal(page.temporaryPreviewId, 'untranslated')
  assert.deepEqual(page.previewItems.map((item: any) => item.id), ['automatic', 'manual', 'untranslated'])
  assert.equal(page.previewSettings.language_mode, 'original')
  assert.equal(page.settings.language_mode, 'bilingual')
  assert.equal(page.snapshot, snapshot)
  noWrites(from)
})

test('序号旁勾选事件只作用于对应评论，恢复手选和排除不会覆盖其他已编辑文字', async () => {
  const { page, checkboxes } = await render(), from = requests.length, originalRows = JSON.stringify(page.rows)
  const update = (author: string, selected: boolean) => {
    const checkbox = checkboxes.find(checkbox => checkbox.$attrs['aria-label'] === `选择评论：${author}`)
    assert.ok(checkbox); checkbox.$.vnode.props['onUpdate:checked'](selected)
  }
  update('排除项作者', true)
  assert.deepEqual(page.items.map((item: any) => item.id), ['automatic', 'excluded', 'manual'])
  assert.equal(page.items[1].selection_origin, 'manual'); assert.deepEqual(page.excluded, [])
  assert.equal(page.items[0].display_text, '已编辑的自动项中文'); assert.equal(page.items[2].source_text, 'Edited original manual')
  update('自动项作者', false)
  assert.deepEqual(page.items.map((item: any) => item.id), ['excluded', 'manual'])
  assert.deepEqual(page.excluded, [{ id: 'automatic', source_hash: 'a' }])
  assert.equal(page.items[1].display_text, '已编辑的手动项中文'); assert.equal(page.orderMode, 'chronological')
  assert.equal(JSON.stringify(page.rows), originalRows)
  noWrites(from)
})

test('编辑区正文与重排事件保持原逻辑，单项编辑不修改来源正文或另一条文字', async () => {
  const { page, textInputs, buttons } = await render(), from = requests.length, originalRows = JSON.stringify(page.rows)
  const chinese = textInputs.find(input => input.$props.inputProps['aria-label'] === '第 1 条中文上屏文字')
  chinese.$.vnode.props['onUpdate:value']('这一条人工精简后的中文')
  assert.equal(page.items[0].display_text, '这一条人工精简后的中文'); assert.equal(page.items[0].selection_origin, 'manual')
  assert.equal(page.items[0].source_text, 'Edited original automatic'); assert.equal(page.items[1].display_text, '已编辑的手动项中文')
  await click(buttons.find(button => button.$attrs['aria-label'] === '上移第 2 条'))
  assert.deepEqual(page.items.map((item: any) => item.id), ['manual', 'automatic']); assert.equal(page.orderMode, 'manual')
  assert.equal(page.items[1].display_text, '这一条人工精简后的中文'); assert.equal(JSON.stringify(page.rows), originalRows)
  assert.deepEqual(page.excluded, [{ id: 'excluded', source_hash: 'b' }])
  noWrites(from)
})

test('只读期间评分状态与预览仍可用，所有编辑和迟到事件都保持当前草稿', async () => {
  const { html, page, buttons, checkboxes, textInputs } = await render({ blocked: true }), from = requests.length, snapshot = page.snapshot
  assert.ok(text(html).includes('88 分')); assert.ok(text(html).includes('手动排除'))
  assert.ok(checkboxes.length === 5 && checkboxes.every(checkbox => checkbox.$props.disabled))
  assert.ok(textInputs.length === 4 && textInputs.every(input => input.$props.disabled))
  for (const button of [...withClass(buttons, 'retention-action'), ...withClass(buttons, 'remove-action'),
    ...buttons.filter(button => /^(上移|下移)第 /.test(button.$attrs['aria-label'] || ''))]) {
    assert.equal(button.$props.disabled, true); await click(button)
  }
  checkboxes[0].$.vnode.props['onUpdate:checked'](false)
  textInputs[0].$.vnode.props['onUpdate:value']('迟到的修改')
  assert.equal(page.snapshot, snapshot)
  const preview = withClass(withClass(buttons, 'comment-action'), 'preview-action')[2]
  assert.equal(preview.$props.disabled, false); await click(preview)
  assert.equal(page.previewFocusId, 'manual'); assert.equal(page.snapshot, snapshot)
  noWrites(from)
})

test('卡片状态与作者可换行，窄工作区调整身份和操作布局且不依赖固定屏幕像素', async () => {
  const { descriptor } = parse(await readFile(new URL('../src/components/CommentStudioPanel.vue', import.meta.url), 'utf8'))
  const css = postcss.parse(descriptor.styles.map(style => style.content).join('\n'))
  const values = (selector: string, property: string, container = false) => {
    const result: string[] = []
    css.walkRules(rule => {
      let parent: any = rule.parent, inside = false
      while (parent) { if (parent.type === 'atrule' && parent.name === 'container') inside = true; parent = parent.parent }
      if (inside === container && rule.selector.includes(selector)) rule.walkDecls(property, declaration => { result.push(declaration.value) })
    })
    return result
  }
  assert.ok(values('.comment-state-group', 'flex-wrap').includes('wrap'))
  assert.ok(values('.comment-facts', 'flex-wrap').includes('wrap'))
  assert.ok(values('.author', 'overflow-wrap').includes('anywhere'))
  assert.ok(values('.comment-body', 'min-width').includes('0')); assert.ok(values('.edit-row', 'min-width').includes('0'))
  assert.ok(values('.row-actions', 'flex-wrap').includes('wrap')); assert.ok(values('.edit-actions', 'flex-wrap').includes('wrap'))
  assert.ok(values('.comment-heading', 'flex-direction', true).includes('column'))
  assert.ok(values('.comment-state-group', 'max-width', true).includes('100%'))
  assert.ok(values('.edit-row-heading', 'flex-wrap', true).includes('wrap'))
  for (const selector of ['.comment-number', '.edit-number']) assert.ok(values(selector, 'font-variant-numeric').includes('tabular-nums'))
})
