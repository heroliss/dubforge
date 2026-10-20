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

let server: ViteDevServer, Panel: any
const styleFields = [
  ['start', '首次出现（秒）'], ['interval', '出现间隔（秒）'], ['duration', '停留时长（秒）'],
  ['width_percent', '最大宽度（画面 %）'], ['height_percent', '目标最大高度（画面 %）'],
  ['font_size', '译文最大字号（通用）'], ['min_font_size', '译文最小字号（通用）'],
  ['source_style_enabled', '原文独立样式'], ['source_font_size', '原文最大字号'], ['source_min_font_size', '原文最小字号'],
].map(([key, label]) => ({ path: 'comments.' + key, label,
  type: key === 'source_style_enabled' ? 'bool' : 'int', min: 0, max: 120,
  ...(['source_font_size', 'source_min_font_size'].includes(key) ? { depends_on: [['comments.source_style_enabled', true]] } : {}),
}))
const fixture = () => {
  const rows = [
    { id: 'a', author: '甲', source_hash: 'a', timestamp: 100, text: 'Original A', text_zh: '中文 A' },
    { id: 'b', author: '乙', source_hash: 'b', timestamp: 200, text: 'Original B', text_zh: '' },
    { id: 'c', author: '丙', source_hash: 'c', timestamp: 300, text: 'Original C', text_zh: '中文 C' },
  ]
  return {
    rows, available_assessments: rows.map(row => ({ id: row.id, eligible: true, score: 90, reason: '相关评论' })),
    filter: { items: [], assessments: [] }, rating_progress: { saved: 3, total: 3, remaining: 0 },
    overlay: { items: [
      { ...rows[0], source_text: '保留人工原文 A', display_text: '保留人工中文 A', selection_origin: 'automatic' },
      { ...rows[2], source_text: '保留人工原文 C', display_text: '保留人工中文 C', selection_origin: 'manual' },
    ], settings: { source_style_enabled: true, language_mode: 'bilingual' }, order_mode: 'chronological' },
    overlay_revision: 'layout-r1', defaults: {}, job: null,
    configuration: { values: { 'comments.count': 2, 'comments.prompt': '相关评论' },
      defaults: { 'comments.font_size': 36, 'comments.min_font_size': 18 },
      sources: { 'comments.font_size': { kind: 'project' }, 'comments.min_font_size': { kind: 'global' } },
      parent_sources: { 'comments.font_size': { kind: 'global' } }, overrides: { 'comments.font_size': 36 } },
    fields: styleFields,
  }
}
let activeFixture = fixture()
const calls: { url: string; options?: RequestInit }[] = []
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/CommentStudioPanel.vue')).default
  mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    return { ok: true, json: async () => String(url).includes('/api/configuration/')
      ? { name: '本项目', defaults: {}, overrides: {}, sources: {}, parent_sources: {}, sections: {} } : activeFixture } as any
  })
})
after(async () => { mock.restoreAll(); await server.close() })
async function render(options: { blocked?: boolean; seed?: (page: any) => void } = {}) {
  activeFixture = fixture()
  let page: any, preview: any
  const buttons: any[] = [], controls: any[] = [], resets: any[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(RouterView))) })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: {
    render: () => h(Panel, { uid: 'comment-layout', videoDuration: 1200, blocked: options.blocked }),
  } }] })
  app.use(router); await router.push('/projects/comment-layout/comments'); await router.isReady(); setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'CommentStudioPanel') {
      page = this.$.setupState; page.state = activeFixture; page.hydrate(activeFixture)
    }
    if (this.$options.__name === 'CommentPreview') preview = this
    if (this.$options.name === 'Button') buttons.push(this)
    if (this.$options.__name === 'ParamControl' && this.$props.field?.path?.startsWith('comments.')) controls.push(this)
    if (this.$options.__name === 'ParameterResetButton') resets.push(this)
  }, serverPrefetch() {
    // 首次真实读取先完成，再创建模板闭包，避免测试按钮引用被异步 hydrate 换掉的旧行。
    if (this.$options.__name === 'CommentStudioPanel') return this.$.setupState.load().then(() => options.seed?.(page))
  } })
  const html = await renderToString(app)
  return { html, page, preview, buttons, controls, resets }
}
const click = async (button: any) => {
  assert.ok(button, '必须找到真实按钮')
  button.$.setupState.handleClick({ preventDefault() {}, stopPropagation() {} })
  await nextTick()
}
const visualItems = (items: any[]) => items.map(({ selection_origin, ...item }) => item)
const withClass = (buttons: any[], name: string) => buttons.filter(button => String(button.$attrs.class || '').split(/\s+/).includes(name))
const nativeButtons = (html: string, name: string) => [...html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
  .map(match => match[0]).filter(button => new RegExp(`class="[^"]*\\b${name}\\b`).test(button))
const buttonText = (html: string) => html.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim()
const noWrites = (from: number) => assert.equal(calls.slice(from).filter(call => call.options?.method && call.options.method !== 'GET').length, 0)

test('真实评论工作区分别标记预览编辑与排版区域，普通参数和两组字号范围各出现一次', async () => {
  const { html, controls, resets, preview } = await render()
  assert.match(html, /class="[^"]*plan-preview-column[^"]*"[^>]*role="region"[^>]*aria-label="上屏预览与评论编辑"/)
  assert.match(html, /class="[^"]*layout-controls[^"]*"[^>]*role="region"[^>]*aria-label="评论节奏与排版参数"/)
  assert.match(html, /role="region" aria-label="已选评论编辑区"/)
  assert.equal([...html.matchAll(/class="style-control"/g)].length, 6)
  assert.equal([...html.matchAll(/class="[^"]*\bfont-range-field\b[^"]*"/g)].length, 2)
  for (const name of ['译文字号范围（通用）', '原文字号范围']) {
    for (const suffix of ['', '最小值', '最大值']) assert.ok(html.includes(`aria-label="${name}${suffix}"`), name + suffix)
  }
  assert.deepEqual(controls.map(control => control.$props.field.path).sort(), styleFields.map(field => field.path).sort())
  assert.equal(new Set(resets.map(reset => reset.$props.label)).size, styleFields.length)
  assert.match(html, /自动选择/); assert.match(html, /手动保留/)
  assert.deepEqual(preview.$props.items.map((item: any) => item.id), ['a', 'c'])
  assert.equal(preview.$props.videoDuration, 1200)
})

test('布局使用容器宽度切换主列和参数行，保留预览上限、两列字号与来源恢复区的空间约束', async () => {
  const { descriptor } = parse(await readFile(new URL('../src/components/CommentStudioPanel.vue', import.meta.url), 'utf8'))
  const css = postcss.parse(descriptor.styles.map(style => style.content).join('\n'))
  const values = (selector: string, property: string, container = false) => {
    const result: string[] = []
    css.walkRules(rule => {
      let parent: any = rule.parent, inContainer = false
      while (parent) { if (parent.type === 'atrule' && parent.name === 'container') inContainer = true; parent = parent.parent }
      if (rule.selector.includes(selector) && inContainer === container) rule.walkDecls(property, declaration => { result.push(declaration.value) })
    })
    return result
  }
  assert.ok(values('.comment-studio', 'container-type').includes('inline-size'))
  assert.ok(values('.layout-controls', 'container-type').includes('inline-size'))
  assert.ok(values('.plan-preview-column', 'max-width').some(value => !['none', '0', '0px'].includes(value)), '预览画面有宽度上限')
  assert.ok(values('.style-field', 'display').includes('grid'))
  assert.ok(values('.style-field', 'grid-template-columns').some(value =>
    value.replace(/minmax\([^)]*\)/g, 'track').trim().split(/\s+/).length === 2), '常规参数有标签和控件两列')
  assert.ok(values('.plan-grid', 'grid-template-columns').some(value =>
    /^minmax\(\s*0\s*,\s*1fr\s*\)\s+[\d.]+(?:px|rem)$/.test(value)), '右侧保持固定宽度，剩余空间留给左侧')
  const singleColumn = (value: string) => /^(?:1fr|minmax\(\s*0\s*,\s*1fr\s*\))$/.test(value)
  assert.ok(values('.plan-grid', 'grid-template-columns', true).some(singleColumn), '窄工作区切换单列')
  assert.ok(values('.style-field', 'grid-template-columns', true).some(singleColumn), '窄参数区切换竖排')
  assert.ok(values('.font-range-inputs', 'grid-template-columns').some(value => /repeat\(\s*2\s*,/.test(value)), '正常宽度保留两个字号输入')
  assert.ok(values('.font-range-bound>span', 'white-space').includes('nowrap'))
  assert.ok(values('.font-range-inputs small', 'flex-shrink').includes('0'), '来源和恢复入口不被压缩')
})

test('列表与已选区采用原生可键盘操作按钮，手动保留状态和重排名称可读取', async () => {
  const { html, buttons } = await render()
  const actions = nativeButtons(html, 'comment-action')
  assert.ok(actions.length >= 10)
  for (const action of actions) {
    assert.match(action, /^<button\b[^>]*type="button"/)
    assert.doesNotMatch(action.match(/^<button\b[^>]*>/)![0], /tabindex="-1"/)
    assert.ok(buttonText(action), '每个操作都有可读取的文字名称')
  }
  assert.equal(nativeButtons(html, 'preview-action').filter(button => buttonText(button) === '定位预览').length, 4)
  assert.equal(nativeButtons(html, 'preview-action').filter(button => buttonText(button) === '试放预览').length, 1)
  const retention = nativeButtons(html, 'retention-action')
  assert.equal(retention.filter(button => button.includes('aria-pressed="true"')).length, 2)
  assert.equal(retention.filter(button => button.includes('aria-pressed="false"')).length, 2)
  for (const label of ['甲的评论操作', '乙的评论操作', '第1条上屏评论操作', '第2条上屏评论操作']) assert.ok(html.includes(`aria-label="${label}"`))
  for (const label of ['上移第 1 条', '下移第 1 条', '上移第 2 条', '下移第 2 条']) assert.ok(html.includes(`aria-label="${label}"`))
  assert.equal(buttons.find(button => button.$attrs['aria-label'] === '上移第 1 条').$props.disabled, true)
  assert.equal(buttons.find(button => button.$attrs['aria-label'] === '下移第 2 条').$props.disabled, true)
})

test('两处定位按钮和未译评论试放接入真实事件，不改变上屏选择、人工文字或保存方案', async () => {
  const { page, buttons } = await render(), from = calls.length
  const previewActions = withClass(withClass(buttons, 'comment-action'), 'preview-action')
  assert.equal(previewActions.length, 5)
  const snapshot = page.snapshot
  await click(previewActions[0])
  assert.equal(page.previewFocusId, 'a'); assert.equal(page.temporaryPreviewId, '')
  const firstRequest = page.focusRequest
  await click(previewActions[4])
  assert.equal(page.previewFocusId, 'c'); assert.equal(page.temporaryPreviewId, '')
  assert.ok(page.focusRequest > firstRequest)
  await click(previewActions[1])
  assert.equal(page.previewFocusId, 'b'); assert.equal(page.temporaryPreviewId, 'b')
  assert.deepEqual(page.previewItems.map((item: any) => item.id), ['a', 'b', 'c'])
  assert.equal(page.previewSettings.language_mode, 'original', '未译评论只临时试放原文')
  assert.equal(page.settings.language_mode, 'bilingual', '不修改实际排版语言')
  assert.equal(page.snapshot, snapshot)
  assert.deepEqual(page.items.map((item: any) => item.id), ['a', 'c'])
  noWrites(from)
})

test('两处手动保留与取消按钮仅改对应来源，不改变文字、排列、临时试放和预览定位', async () => {
  for (const [index, targetId, expected] of [[0, 'a', 'manual'], [1, 'c', 'automatic'], [2, 'a', 'manual'], [3, 'c', 'automatic']] as const) {
    const { page, buttons } = await render({ seed: page => { page.previewFocusId = 'c'; page.focusRequest = 9; page.temporaryPreviewId = 'b' } })
    const from = calls.length, items = JSON.stringify(visualItems(page.items)), previewItems = JSON.stringify(visualItems(page.previewItems))
    const origins = new Map(page.items.map((item: any) => [item.id, item.selection_origin]))
    const settings = JSON.stringify(page.settings), exclusions = JSON.stringify(page.excluded)
    await click(withClass(buttons, 'retention-action')[index])
    assert.equal(page.items.find((item: any) => item.id === targetId).selection_origin, expected)
    for (const item of page.items) if (item.id !== targetId) assert.equal(item.selection_origin, origins.get(item.id))
    assert.equal(JSON.stringify(visualItems(page.items)), items)
    assert.equal(JSON.stringify(visualItems(page.previewItems)), previewItems)
    assert.equal(JSON.stringify(page.settings), settings); assert.equal(JSON.stringify(page.excluded), exclusions)
    assert.equal(page.previewFocusId, 'c'); assert.equal(page.focusRequest, 9); assert.equal(page.temporaryPreviewId, 'b')
    noWrites(from)
  }
})

test('普通参数与字号两端仍编辑同一份草稿，布局包装不改变文字或保留来源', async () => {
  const { page, controls } = await render(), from = calls.length, items = JSON.stringify(page.items)
  const update = (path: string, value: number) => controls.find(control => control.$props.field.path === path).$.vnode.props['onUpdate:modelValue'](value)
  update('comments.interval', 20); update('comments.min_font_size', 21); update('comments.font_size', 42)
  assert.equal(page.settings.interval, 20); assert.equal(page.settings.min_font_size, 21); assert.equal(page.settings.font_size, 42)
  assert.equal(page.dirty, true)
  assert.deepEqual([...page.styleEdited].sort(), ['font_size', 'interval', 'min_font_size'])
  assert.equal(JSON.stringify(page.items), items)
  noWrites(from)
})

test('锁定时手动保留与参数控件禁用且迟到点击无副作用，定位预览仍可使用', async () => {
  const { page, buttons, controls, resets, html } = await render({ blocked: true }), from = calls.length
  const snapshot = page.snapshot
  for (const button of withClass(buttons, 'retention-action')) {
    assert.equal(button.$props.disabled, true); await click(button)
  }
  for (const button of nativeButtons(html, 'retention-action')) assert.match(button.match(/^<button\b[^>]*>/)![0], /\bdisabled/)
  assert.ok(controls.every(control => control.$props.disabled))
  assert.ok(resets.every(reset => reset.$props.disabled))
  controls.find(control => control.$props.field.path === 'comments.interval').$.vnode.props['onUpdate:modelValue'](30)
  assert.equal(page.settings.interval, 15)
  const preview = withClass(buttons, 'preview-action')[0]
  assert.equal(preview.$props.disabled, false); await click(preview)
  assert.equal(page.previewFocusId, 'a'); assert.equal(page.snapshot, snapshot)
  noWrites(from)
})
