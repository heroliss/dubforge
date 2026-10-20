import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, createRenderer, getCurrentInstance, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { resolveProjectCopy, copyRestoreLabels } from '../src/utils/projectPublishCopy.ts'

let server: ViteDevServer, Form: any, Assistant: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
  Assistant = (await server.ssrLoadModule('/src/components/PublishCopyAssistant.vue')).default
})
after(() => server.close())

function preview(extra: any = {}) {
  return { project_uid: 'p', revision: 4, fingerprint: 'cfg4', content_fingerprint: 'video4',
    content: { title: '视频原始标题', description: '视频原始简介', source: 'https://example.com/video' },
    content_sources: { title: { kind: 'content' }, description: { kind: 'content' }, source: { kind: 'content' } },
    parent_configured: [], configured: [], source_published_date: '2026-10-01', ...extra }
}
function configuration(extra: any = {}) {
  const defaults = { 'publish.title': '', 'publish.description': '', 'publish.tags': [], 'publish.dynamic': '',
    'publish.source': '', 'publish.mode': 'draft', 'publish.ai_copy': true, 'publish.account_id': '',
    'publish.cover_source': 'video', 'publish.version_ids': null, 'publish.part_titles': {},
    'publish.human_type2': 0, 'publish.copyright': 2, 'publish.visibility': 'public', 'assembly.versions': [],
    'publish.watermark': false, 'publish.close_comments': false, 'publish.close_danmaku': false,
    'publish.selected_comments': false }
  return { key: 'project:p', name: '本项目', revision: 4, fingerprint: 'cfg4', overrides: {}, defaults,
    parent_sources: Object.fromEntries(Object.keys(defaults).map(k => [k, { kind: 'global' }])),
    sections: { publication: { label: '发布', fields: [
      { path: 'publish.mode', label: '发布方式', type: 'text' },
      { path: 'publish.ai_copy', label: '自动流程补全文案', type: 'bool' },
      { path: 'publish.account_id', label: '账号', type: 'text' },
      { path: 'publish.cover_source', label: '封面来源', type: 'text' },
    ] } }, version_fields: [], ...extra }
}
const response = (data: any, ok = true) => ({ ok, json: async () => data })
const settle = () => new Promise(resolve => setImmediate(resolve))

// 候选的生命周期/监听使用真实客户端 setup；空渲染宿主避免依赖浏览器或网络。
// 页面 HTML 仍由下方真正的 SSR 组件验证。
const host = createRenderer<any, any>({
  createElement: () => ({ children: [] }), createText: () => ({}), createComment: () => ({}),
  setText() {}, setElementText() {}, patchProp() {}, parentNode: node => node.parent,
  nextSibling(node) { return node.parent?.children[node.parent.children.indexOf(node) + 1] || null },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
  insert(node, parent, anchor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
  },
})
async function clientAssistant(t: any, page: any) {
  const props = reactive({ projectUid: 'p', revision: 4, fingerprint: 'cfg4', contentFingerprint: 'video4',
    values: page.publishValues, disabled: false })
  const events: boolean[] = []; let assistant: any
  const Logic = { ...Assistant, render: () => null, ssrRender: undefined }
  const app = host.createApp({ render: () => h(Logic, {
    ...props, onApply: (value: any) => page.applyCopy(value), onBusyChange: (busy: boolean) => {
      events.push(busy); page.copyBusyChanged(busy)
    },
  }) })
  app.provide(ssrContextKey, {})
  app.mixin({ created() { if (this.$options.__name === 'PublishCopyAssistant') assistant = this.$.setupState } })
  app.mount({ children: [] }); t.after(() => app.unmount()); await nextTick()
  return { assistant, assistantProps: props, busyEvents: events }
}

async function clientForm(t: any) {
  let page: any, assistant: any
  const timers: Function[] = []
  t.mock.method(globalThis, 'setInterval', ((fn: Function) => { timers.push(fn); return timers.length }) as any)
  t.mock.method(globalThis, 'clearInterval', () => {})
  // 真实挂载父表单与助手，包含运行权限共享器的定时读取和 busy 事件绑定。
  const AssistantLogic = { ...Assistant, render: () => null, ssrRender: undefined }
  const Logic = { ...Form, ssrRender: undefined, render() {
    const state = getCurrentInstance()!.setupState
    return state.data ? h(AssistantLogic, {
      projectUid: state.projectUid, revision: state.data.revision, fingerprint: state.data.fingerprint,
      contentFingerprint: state.copyPreview?.content_fingerprint, values: state.publishValues,
      disabled: !!(state.locked || state.copyLoading || !state.copyPreview || state.copyContextChanged),
      onApply: state.applyCopy, onBusyChange: state.copyBusyChanged,
    }) : null
  } }
  const app = host.createApp({ render: () => h(NMessageProvider, null, {
    default: () => h(Logic, { scope: 'project:p', sections: ['publication'] }),
  }) })
  app.provide(ssrContextKey, {})
  app.mixin({ created() {
    if (this.$options.__name === 'ConfigurationForm') page = this.$.setupState
    if (this.$options.__name === 'PublishCopyAssistant') assistant = this.$.setupState
  } })
  app.mount({ children: [] }); t.after(() => app.unmount())
  await settle(); await nextTick()
  return { page, assistant, timers }
}

async function render(t: any, { scope = 'project:p', cfg = configuration(), base = preview() } = {}) {
  let page: any, assistant: any, assistantProps: any
  const calls: any[] = [], busyEvents: boolean[] = [], settingsForms: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    calls.push({ url: String(url), options })
    if (String(url).endsWith('/copy-preview')) return response(base)
    if (String(url).endsWith('/catalog')) return response({ accounts: [] })
    return response(cfg)
  })
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(Form, {
    scope, sections: ['publication'], onBusyChange: (busy: boolean) => busyEvents.push(busy),
  }) }) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ConfigurationForm') {
      page = this.$.setupState; page.adopt(cfg); if (scope.startsWith('project:')) page.copyPreview = base
    }
    if (this.$options.__name === 'PublishCopyAssistant') { assistant = this.$.setupState; assistantProps = this.$.props }
    if (this.$options.__name === 'PublishSettingsForm') settingsForms.push(this.$.props)
  } })
  const html = await renderToString(app); await settle()
  // SSR 没有父组件更新循环，模拟异步基础资料读取结束后的实际传入属性。
  if (assistantProps) {
    assistantProps.disabled = !!(page.locked || page.copyLoading || !page.copyPreview || page.copyContextChanged)
    assistantProps.values = page.publishValues
    assistantProps.contentFingerprint = page.copyPreview?.content_fingerprint
  }
  return { page, assistant, assistantProps, html, calls, busyEvents, settingsForms }
}
function candidate(extra: any = {}) {
  return { project_uid: 'p', revision: 4, fingerprint: 'cfg4', content_fingerprint: 'video4',
    values: { title: '候选标题', description: '候选简介', tags: ['旅行'], dynamic: '候选动态' },
    basis: ['视频资料'], warnings: ['请核对事实'], ...extra }
}

test('自动基础只补未配置字段，项目及父层显式空值优先，恢复按钮区分自动与继承', () => {
  const base = { title: '', description: '', tags: ['模板标签'], dynamic: '', source: '' }
  const result = resolveProjectCopy(base, { title: '', dynamic: '' }, {}, preview({ parent_configured: ['publish.description', 'publish.tags'] }))
  assert.equal(result.values.title, ''); assert.equal(result.values.description, '')
  assert.deepEqual(result.values.tags, ['模板标签']); assert.equal(result.values.source, 'https://example.com/video')
  assert.equal(base.source, '')
  assert.equal(result.sources.source.kind, 'content')
  assert.equal(copyRestoreLabels(preview({ parent_configured: ['publish.title'] })).title, '恢复项目基础值')
  assert.equal(copyRestoreLabels(preview()).title, '恢复自动填充')
})

test('项目设置实际渲染基础文案、来源与分组，打开只读，不自动生成或创建稿件', async t => {
  const { page, html, calls, settingsForms } = await render(t)
  assert.match(html, /视频原始标题/); assert.match(html, /视频原始简介/); assert.match(html, /视频资料/)
  assert.match(html, /刷新基础资料/); assert.match(html, /刷新账号选项/); assert.match(html, /保留当前文案/)
  assert.doesNotMatch(html, /aria-label="设置类别"/)
  assert.ok(html.indexOf('本项目发布文案') < html.indexOf('发布方式、账号与封面'))
  assert.ok(html.indexOf('分P清单') < html.indexOf('更多平台选项'))
  assert.equal(page.dirty, false); assert.ok(calls.every(c => !c.options?.body))
  assert.deepEqual(settingsForms[0].fieldKeys, ['title', 'description', 'tags', 'dynamic', 'source'])
  assert.equal(settingsForms[0].showMore, false)
  assert.ok(!settingsForms[1].fieldKeys.includes('title'))
})

test('刷新基础资料保留全部未保存参数，只改变未覆盖基础，不暗中adopt或保存', async t => {
  const { page } = await render(t)
  page.change('publish.title', '手工标题'); page.change('publish.visibility', 'self')
  const saved = page.saved
  t.mock.method(globalThis, 'fetch', async () => response(preview({ content_fingerprint: 'video5',
    content: { title: '新的原始标题', description: '新的基础简介', source: 'https://example.com/new' } })))
  await page.loadCopyPreview()
  assert.equal(page.publishValues.title, '手工标题'); assert.equal(page.publishValues.description, '新的基础简介')
  assert.equal(page.patch['publish.visibility'], 'self'); assert.equal(page.saved, saved); assert.equal(page.dirty, true)
  page.inherit('publish.title'); assert.equal(page.publishValues.title, '新的原始标题')
})

test('资料错误不锁其它设置；配置版本变化不混合新旧继承值，也不能保留为项目值', async t => {
  const { page } = await render(t)
  t.mock.method(globalThis, 'fetch', async () => response({ message: '资料暂不可读' }, false))
  await page.loadCopyPreview(); page.change('publish.title', '仍可编辑')
  assert.equal(page.copyError, '资料暂不可读'); assert.equal(page.publishValues.description, '视频原始简介')
  assert.equal(!!page.locked, false)
  page.inherit('publish.title')
  t.mock.method(globalThis, 'fetch', async () => response(preview({ revision: 5, fingerprint: 'cfg5',
    parent_configured: ['publish.title'], values: { title: '新父层标题' } })))
  await page.loadCopyPreview()
  assert.equal(page.copyContextChanged, true); assert.equal(page.usableCopyPreview, null)
  assert.equal(page.publishValues.title, ''); page.retainCopy(); assert.deepEqual({ ...page.patch }, {})
})

test('保存取消旧资料读取，迟到的旧版本不覆盖新预览；保存仅提交明确修改', async t => {
  const { page } = await render(t)
  let finish: any, signal: AbortSignal | undefined, readCount = 0
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    if (options?.method === 'PUT') {
      const body = JSON.parse(options.body)
      assert.deepEqual(body.updates, { 'publish.title': '保存标题' })
      return response(configuration({ revision: 5, fingerprint: 'cfg5', overrides: body.updates }))
    }
    if (String(url).endsWith('/copy-preview')) {
      if (++readCount === 1) { signal = options.signal; return new Promise(resolve => finish = resolve) }
      return response(preview({ revision: 5, fingerprint: 'cfg5', content_fingerprint: 'video5' }))
    }
    return response({ accounts: [] })
  })
  const pending = page.loadCopyPreview(); page.change('publish.title', '保存标题'); await page.save(); await settle()
  assert.equal(signal?.aborted, true); assert.equal(readCount, 2)
  finish(response(preview({ content: { title: '迟到旧标题' } }))); await pending
  assert.equal(page.copyPreview.fingerprint, 'cfg5'); assert.equal(page.publishValues.title, '保存标题')
  assert.equal(page.dirty, false)
})

test('保留当前文案仅填四项到编辑区，含空动态；原始来源不被复制为覆盖', async t => {
  const { page, calls } = await render(t)
  const count = calls.length; page.retainCopy()
  assert.deepEqual({ ...page.patch }, { 'publish.title': '视频原始标题', 'publish.description': '视频原始简介',
    'publish.tags': [], 'publish.dynamic': '' })
  assert.equal(page.dirty, true); assert.equal(calls.length, count)
  assert.equal('publish.source' in page.patch, false)
})

test('项目AI明确生成携带资料版本，当前/候选对照后只应用所选字段到patch', async t => {
  const { page, assistant, assistantProps } = await render(t)
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    calls.push([url, JSON.parse(options.body)]); return response(candidate())
  })
  await assistant.generate()
  assert.equal(calls.length, 1); assert.equal(calls[0][0], '/api/publishing/projects/p/suggest')
  assert.equal(calls[0][1].fingerprint, 'cfg4'); assert.equal(calls[0][1].content_fingerprint, 'video4')
  assert.deepEqual(Object.keys(calls[0][1].current).sort(), ['description', 'dynamic', 'tags', 'title'])
  assert.equal(page.dirty, false)
  assistant.selected = ['title', 'tags']; assistant.apply()
  assert.deepEqual({ ...page.patch }, { 'publish.title': '候选标题', 'publish.tags': ['旅行'] })
  assert.equal(page.publishValues.description, '视频原始简介'); assert.equal(calls.length, 1)
  assistantProps.values = page.publishValues; await nextTick()
  assert.equal(assistant.applied, true)
})

test('生成中仍可手工编辑但不可保存；重复点击只发一次，迟到候选不能覆盖编辑', { timeout: 5000 }, async t => {
  const { page } = await render(t)
  const { assistant, assistantProps, busyEvents } = await clientAssistant(t, page)
  let finish: any, count = 0
  t.mock.method(globalThis, 'fetch', () => { count++; return new Promise(resolve => finish = resolve) })
  const pending = assistant.generate(); const duplicate = assistant.generate()
  assert.equal(page.pending, true); assert.equal(!!page.locked, false)
  page.change('publish.title', '人工编辑'); assistantProps.values = page.publishValues; await nextTick()
  const save = page.save(); assert.equal(count, 1)
  finish(response(candidate())); await pending; await duplicate; await save
  assert.equal(assistant.suggestion, null); assert.match(assistant.error, /项目资料已变化/)
  assert.equal(page.publishValues.title, '人工编辑'); assert.equal(page.pending, false)
  assert.deepEqual(busyEvents.slice(-2), [true, false])
})

test('真实父表单轮询锁不使成功候选失效，结束重新核对权限后才允许应用', { timeout: 5000 }, async t => {
  let reads = 0, releaseCopy: any, releasePermission: any
  t.mock.method(globalThis, 'fetch', (url: any) => {
    const path = String(url)
    if (path.endsWith('/editability')) {
      reads++
      if (reads >= 3) return new Promise(resolve => releasePermission = resolve)
      return Promise.resolve(response({ locked: reads === 2, reason: reads === 2 ? '项目发布文案正在生成' : '' }))
    }
    if (path.endsWith('/suggest')) return new Promise(resolve => releaseCopy = resolve)
    if (path.endsWith('/copy-preview')) return Promise.resolve(response(preview()))
    if (path.endsWith('/catalog')) return Promise.resolve(response({ accounts: [] }))
    return Promise.resolve(response(configuration()))
  })
  const { page, assistant, timers } = await clientForm(t)
  assert.equal(page.editability.observing.value, true); assert.equal(reads, 1)
  page.copyBusyChanged(false); assert.equal(reads, 1, '初始化false不发多余权限读取')
  const pending = assistant.generate(); await nextTick()
  assert.equal(page.pending, true)
  timers[0](); await settle(); await nextTick()
  assert.equal(page.locked, true)
  releaseCopy(response(candidate())); await pending; await nextTick()
  assert.equal(reads, 3, '生成完成立即只读刷新权限')
  assert.ok(assistant.suggestion); assert.equal(assistant.error, ''); assert.equal(assistant.stale, false)
  assert.equal(page.pending, false); assistant.apply(); assert.equal(page.dirty, false)
  const visual = createSSRApp({ render: () => h(Assistant, {
    projectUid: 'p', revision: 4, fingerprint: 'cfg4', contentFingerprint: 'video4',
    values: page.publishValues, disabled: true,
  }) })
  visual.mixin({ created() {
    if (this.$options.__name === 'PublishCopyAssistant') {
      const state = this.$.setupState as any; state.suggestion = candidate(); state.base = state.signature
    }
  } })
  const html = await renderToString(visual)
  assert.match(html, /候选已生成；正在核对编辑权限或项目仍有任务/)
  assert.match(html, /候选标题/); assert.match(html, /disabled[^>]*>[\s\S]*?应用所选文案/)
  releasePermission(response({ locked: false })); await settle(); await nextTick()
  assert.equal(page.locked, false); assistant.apply(); assert.equal(page.patch['publish.title'], '候选标题')
  page.copyBusyChanged(false); assert.equal(reads, 3, '重复false不再次刷新')
})

test('轮询锁定时真实文案变化仍拒绝迟到候选，取消后也不强制解锁', { timeout: 5000 }, async t => {
  let locked = false, reads = 0, release: any
  t.mock.method(globalThis, 'fetch', (url: any) => {
    const path = String(url)
    if (path.endsWith('/editability')) { reads++; return Promise.resolve(response({ locked, reason: '项目发布文案正在生成' })) }
    if (path.endsWith('/suggest')) return new Promise(resolve => release = resolve)
    if (path.endsWith('/copy-preview')) return Promise.resolve(response(preview()))
    if (path.endsWith('/catalog')) return Promise.resolve(response({ accounts: [] }))
    return Promise.resolve(response(configuration()))
  })
  const { page, assistant, timers } = await clientForm(t)
  // 共用状态可能保留上个测试的短期可编辑缓存，明确取得本次状态。
  await page.editability.refresh(); await nextTick()
  const pending = assistant.generate(); page.change('publish.title', '真实修改'); await nextTick()
  locked = true; timers[0](); await settle(); await nextTick()
  release(response(candidate())); await pending; await settle(); await nextTick()
  assert.equal(assistant.suggestion, null); assert.match(assistant.error, /项目资料已变化/)
  assert.equal(page.patch['publish.title'], '真实修改'); assert.equal(page.locked, true)
  locked = false; await page.editability.refresh(); await nextTick()
  const cancelled = assistant.generate(); locked = true
  const previousReads = reads; assistant.cancel(); await settle(); await nextTick()
  assert.equal(reads, previousReads + 1); assert.equal(page.locked, true)
  release(response(candidate())); await cancelled; assert.equal(assistant.suggestion, null)
})

test('取消等待及切项目拒绝迟到候选，新请求不受旧请求finally干扰', { timeout: 5000 }, async t => {
  const { page } = await render(t)
  const { assistant, assistantProps } = await clientAssistant(t, page)
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => new Promise(resolve => requests.push({ resolve, signal: options.signal })))
  const old = assistant.generate(); assistant.cancel()
  assert.equal(requests[0].signal.aborted, true)
  const fresh = assistant.generate(); requests[0].resolve(response(candidate())); await old
  assert.equal(assistant.busy, true); assert.equal(assistant.suggestion, null)
  requests[1].resolve(response(candidate())); await fresh; assert.ok(assistant.suggestion)
  const third = assistant.generate(); assistantProps.projectUid = 'other'; await nextTick()
  assert.equal(requests[2].signal.aborted, true)
  requests[2].resolve(response(candidate())); await third
  assert.equal(assistant.suggestion, null); assert.equal(assistant.busy, false)
})

test('发布文案由后端统一控制生成时限，前端不追加五分钟截止但仍可取消等待', async t => {
  const { page } = await render(t)
  const { assistant } = await clientAssistant(t, page)
  const timeoutValues: number[] = [], timeLimit = AbortSignal.timeout.bind(AbortSignal)
  t.mock.method(AbortSignal, 'timeout', (milliseconds: number) => { timeoutValues.push(milliseconds); return timeLimit(milliseconds) })
  let signal: AbortSignal | undefined, finish: any
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => {
    signal = options.signal
    return new Promise(resolve => { finish = resolve })
  })
  const pending = assistant.generate()
  assert.ok(signal instanceof AbortSignal);assert.equal(signal.aborted,false)
  assert.equal(timeoutValues.includes(300000),false)
  assistant.cancel();assert.equal(signal.aborted,true)
  finish(response(candidate()));await pending
  assert.equal(assistant.suggestion,null);assert.equal(assistant.busy,false)
})

test('基础资料刷新使旧候选过期，配置或内容版本不一致的回执不可应用', async t => {
  const { page, assistant, assistantProps } = await render(t)
  t.mock.method(globalThis, 'fetch', async () => response(candidate()))
  await assistant.generate()
  page.copyPreview = preview({ content_fingerprint: 'video5' }); assistantProps.contentFingerprint = 'video5'; await nextTick()
  assert.equal(assistant.stale, true); assistant.apply(); assert.equal(page.dirty, false)
  await assistant.generate(); assert.equal(assistant.suggestion, null); assert.match(assistant.error, /版本不一致/)
})

test('应用候选后保存版本变化清除待保存提示，未应用候选继续提示过期', { timeout: 5000 }, async t => {
  const { page } = await render(t)
  const { assistant, assistantProps } = await clientAssistant(t, page)
  t.mock.method(globalThis, 'fetch', async () => response(candidate()))
  await assistant.generate(); assistant.apply(); assert.equal(assistant.applied, true)
  assistantProps.revision = 5; assistantProps.fingerprint = 'cfg5'; await nextTick()
  assert.equal(assistant.suggestion, null); assert.equal(assistant.applied, false)
  t.mock.method(globalThis, 'fetch', async () => response(candidate({ revision: 5, fingerprint: 'cfg5' })))
  await assistant.generate(); assert.ok(assistant.suggestion)
  assistantProps.revision = 6; await nextTick()
  assert.ok(assistant.suggestion); assert.equal(assistant.stale, true)
})

test('模板与全局不加载项目基础资料，不显示项目文案；旧发布平台字段仍保留', async t => {
  for (const scope of ['global', 'series:travel']) {
    const { html, calls, settingsForms, assistant } = await render(t, { scope })
    assert.doesNotMatch(html, /本项目发布文案|视频原始标题|保留当前文案/)
    assert.equal(calls.some(c => c.url.endsWith('/copy-preview')), false); assert.equal(assistant, undefined)
    assert.equal(settingsForms.length, 1); assert.equal(settingsForms[0].template, true)
    assert.equal(settingsForms[0].fieldKeys, undefined)
  }
})
