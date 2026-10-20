import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer, transformWithEsbuild, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { parse, compileScript } from '@vue/compiler-sfc'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { NMessageProvider } from 'naive-ui'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer
let Result: any, Chapters: any, Options: any, ClientConversationButton: any
before(async () => {
  // SSR 编译会移除原生事件修饰符；点击验证使用同一 SFC 的客户端渲染函数。
  const filename = fileURLToPath(new URL('../src/components/LlmConversationButton.vue', import.meta.url))
  const source = await readFile(filename, 'utf8')
  const { descriptor } = parse(source, { filename })
  const compiled = compileScript(descriptor, { id: 'chapter-entry-client', inlineTemplate: true }).content
    .replace("'../utils/llmDialog'", "'/src/utils/llmDialog.ts'")
  const clientModule = (await transformWithEsbuild(compiled, 'chapter-entry-client.ts', { loader: 'ts' })).code
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    cacheDir: 'node_modules/.vite-chapter-conversation-entry-test',
    plugins: [{ name: 'chapter-entry-client-test', enforce: 'pre',
      resolveId: id => id === 'virtual:chapter-entry-client' ? '\0chapter-entry-client.js' : undefined,
      load: id => id === '\0chapter-entry-client.js' ? clientModule : undefined }, vue()],
    appType: 'custom', optimizeDeps: { noDiscovery: true, include: [], entries: [] },
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Result = (await server.ssrLoadModule('/src/components/PublishResultPanel.vue')).default
  Chapters = (await server.ssrLoadModule('/src/components/BiliChaptersPanel.vue')).default
  Options = (await server.ssrLoadModule('/src/components/PublishChapterOptions.vue')).default
  ClientConversationButton = (await server.ssrLoadModule('virtual:chapter-entry-client')).default
})
after(async () => await server.close())

async function render(component: any, props: any, state: any = {}) {
  const entries: any[] = [], buttons: any[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(component, props) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === component.__name) Object.assign(this.$.setupState, state)
    if (this.$options.__name === 'LlmConversationButton') entries.push(this.$.props)
    if (this.$options.name === 'Button' && this.$.parent?.type.__name === 'LlmConversationButton') {
      buttons.push(this.$.vnode.props)
    }
  } })
  return { html: await renderToString(app), entries, buttons }
}

function entryButton(result: Awaited<ReturnType<typeof render>>) {
  assert.equal(result.entries.length, 1, '每个章节区域只提供一个对话入口')
  assert.equal(result.buttons.length, 1, '必须使用真实按钮展示入口')
  assert.notEqual(result.buttons[0].disabled, true, '查看历史不应随写操作锁定')
  assert.equal(result.entries[0].prominent, true, '章节主入口应使用清晰的次级按钮外观')
  return result.entries[0]
}

test('自动章节的进行中、失败、待核对与完成状态都在标题行提供当前稿件对话入口', async () => {
  for (const status of ['running', 'failed', 'unknown', 'completed']) {
    const publication = { id: 'publication-甲', project_uid: 'project-乙', status: 'published',
      receipt: { bvid: 'BV1234567890' }, snapshot: { chapter_options: { enabled: true } },
      chapter_sync: { status, enabled: true, phase: status === 'running' ? 'adapting' : '', parts: [] } }
    const result = await render(Result, { publication, recordOnly: true, busy: true, projectLocked: true, dirty: true })
    const entry = entryButton(result)
    assert.equal(entry.projectUid, publication.project_uid)
    assert.equal(entry.ownerId, publication.id)
    assert.equal(entry.operation, 'chapters')
    const heading = result.html.indexOf('class="chapter-sync-heading"')
    const entryAt = result.html.indexOf('查看LLM对话', heading)
    const progressAt = result.html.indexOf('aria-label="执行过程"', heading)
    assert.ok(heading >= 0 && entryAt > heading && progressAt > entryAt,
      `状态 ${status} 的入口应直接出现在自动章节标题行、执行进度之前`)
    assert.doesNotMatch(result.html, /重试设置章节/, '只读稿件不能因此得到章节写操作')
  }
})

test('远端章节读取前、读取中、读取失败与空分P时仍在章节标题栏保留稿件对话', async () => {
  const states = [
    {}, { loading: true }, { loading: false, error: '网络读取失败，请稍后重试' },
    { loaded: true, parts: [] }, { syncLoading: true, adapting: true },
  ]
  for (const state of states) {
    const result = await render(Chapters, { accountId: 'account-1', aid: 1, bvid: 'BV1234567890',
      projectUid: 'project-1', publicationId: 'publication-1', disabled: true }, state)
    const entry = entryButton(result)
    assert.equal(entry.projectUid, 'project-1'); assert.equal(entry.ownerId, 'publication-1')
    assert.equal(entry.operation, 'chapters')
    const header = result.html.match(/<header\b[^>]*class="chapters-heading"[^>]*>([\s\S]*?)<\/header>/)?.[1] || ''
    assert.match(header, /查看LLM对话/, '对话入口不依赖远端章节是否读取成功')
  }
})

test('草稿关闭自动章节或正在整理、被锁定时仍在标题栏提供项目章节历史', async () => {
  for (const variant of [{ enabled: false }, { enabled: true, disabled: true }, { enabled: true, busy: true }]) {
    const result = await render(Options, { projectUid: 'project-only', disabled: variant.disabled,
      value: { enabled: variant.enabled, items: [] } }, { busy: !!variant.busy })
    const entry = entryButton(result)
    assert.equal(entry.projectUid, 'project-only'); assert.equal(entry.operation, 'chapters')
    assert.equal(entry.ownerId, undefined, '项目草稿预览对话不应错误筛选成稿件所属')
    const heading = result.html.match(/<div class="chapter-heading"[^>]*>([\s\S]*?)<p\b/)?.[1] || ''
    assert.match(heading, /查看LLM对话/, '对话入口应位于章节标题行')
    assert.ok(result.html.indexOf('查看LLM对话') < result.html.indexOf('章节标题自定义要求') || !variant.enabled,
      '入口应位于标题操作区，不能埋在自定义要求之后')
  }
})

test('清晰的共享入口保留原文，点击只打开筛选对话且阻止事件冒泡', async t => {
  const events: any[] = []
  const descriptors = Object.fromEntries(['window', 'CustomEvent'].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { dispatchEvent: (event: any) => { events.push(event); return true } } })
  Object.defineProperty(globalThis, 'CustomEvent', { configurable: true, value: class {
    type: string; detail: any
    constructor(type: string, options: any) { this.type = type; this.detail = options.detail }
  } })
  t.mock.method(globalThis, 'fetch', () => { throw new Error('查看对话不得触发模型或发布请求') })
  t.after(() => {
    for (const key of ['window', 'CustomEvent']) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]!)
      else delete (globalThis as any)[key]
    }
  })
  const filter = { projectUid: '中文/项目', ownerId: 'pub 1', operation: 'chapters' }
  const result = await render(ClientConversationButton, { ...filter, prominent: true })
  entryButton(result)
  assert.match(result.html, /查看LLM对话/)
  assert.equal(result.buttons[0].secondary, true)
  assert.notEqual(result.buttons[0].quaternary, true)
  let stopped = 0
  result.buttons[0].onClick({ stopPropagation() { stopped++ } })
  assert.equal(stopped, 1)
  assert.equal(events.length, 1); assert.equal(events[0].type, 'dubforge:llm-conversations')
  assert.deepEqual(events[0].detail, filter)
})
