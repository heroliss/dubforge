import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Preview: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Preview = (await server.ssrLoadModule('/src/components/ReferenceFilePreview.vue')).default
})
after(() => server.close())

async function render(path?: string, state: Record<string, unknown> = {}) {
  let page: any, button: any
  const app = createSSRApp({ render: () => h(Preview, { path, label: '音色参考文件' }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ReferenceFilePreview') { page = this.$.setupState; Object.assign(page, state) }
    if (this.$options.name === 'Button') button = this
  } })
  return { html: await renderToString(app), page, button }
}

test('空路径禁用试听，非空文件提供完整试听且不复制路径输入框', async () => {
  for (const path of [undefined, '', '  ', 'bad\0.wav']) {
    const f = await render(path)
    assert.equal(f.page.disabled, true)
    assert.equal(f.button.$props.disabled, true)
    assert.match(f.html, /选择参考音频文件后可试听/)
  }
  const f = await render('D:\\音色\\样本.wav')
  assert.equal(f.page.disabled, false)
  assert.equal(f.button.$props.disabled, false)
  assert.match(f.html, /aria-label="试听音色参考文件"/)
  assert.match(f.html, /试听音色参考文件（完整文件）/)
  assert.doesNotMatch(f.html, /<input/)
  assert.deepEqual(Object.keys(Preview.props).sort(), ['label', 'path'], '不接受配置输入框的 disabled，已填文件始终可只读试听')
})

test('加载与播放均能再次停止，状态、图标和错误有可访问表达', async () => {
  const loading = await render('voice.wav', { state: 'loading' })
  assert.equal(loading.button.$props.disabled, false)
  assert.match(loading.html, /aria-busy="true"/)
  assert.match(loading.html, /aria-pressed="true"/)
  assert.match(loading.html, /aria-label="停止音色参考文件"/)
  assert.match(loading.html, /preview-spinner/)
  const playing = await render('voice.wav', { state: 'playing' })
  assert.match(playing.html, /<rect/)
  assert.doesNotMatch(playing.html, /preview-spinner/)
  const failed = await render('voice.wav', { error: '参考文件不存在，请重新选择文件。' })
  assert.match(failed.html, /role="alert"/)
  assert.match(failed.html, /参考文件不存在，请重新选择文件。/)
  assert.equal(failed.button.$props.disabled, false, '失败后仍可重试')
})

const host = createRenderer<any, any>({ createElement: () => ({}), createText: () => ({}), createComment: () => ({}),
  setText() {}, setElementText() {}, patchProp() {}, parentNode: () => null, nextSibling: () => null, remove() {}, insert() {} })
async function client(t: any) {
  const props = reactive({ path: 'D:\\首选.wav', label: '情感参考文件' }), requests: any[] = []
  let page: any
  t.mock.method(globalThis, 'fetch', (url: string, options: RequestInit) => {
    let resolve!: (value: Response) => void
    const promise = new Promise<Response>(yes => { resolve = yes })
    requests.push({ url, options, resolve }); return promise
  })
  const Logic = { ...Preview, render: () => null, ssrRender: undefined }
  const app = host.createApp({ render: () => h(Logic, props) })
  app.provide(ssrContextKey, {})
  app.mixin({ created() { if (this.$options.__name === 'ReferenceFilePreview') page = this.$.setupState } })
  app.mount({}); t.after(() => app.unmount()); await nextTick()
  return { props, page, requests, unmount: () => app.unmount() }
}
async function settle() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }

test('点击文件试听时使用当前表单路径，加载中再次点击立即停止且忽略迟到错误', async t => {
  const f = await client(t)
  f.page.togglePreview()
  assert.equal(f.page.state, 'loading')
  assert.equal(f.requests[0].url, `/api/files/reference-audio?path=${encodeURIComponent(f.props.path)}`)
  f.page.togglePreview()
  assert.equal(f.page.state, 'idle')
  assert.equal(f.requests[0].options.signal.aborted, true)
  f.requests[0].resolve(new Response(JSON.stringify({ message: '旧错误' }), { status: 404 }))
  await settle()
  assert.equal(f.page.error, '')
  assert.equal(f.page.state, 'idle')
})

test('路径变化立即停止试听并清除旧错误，下一次读取新路径', async t => {
  const f = await client(t)
  f.page.togglePreview()
  f.page.error = '上次试听失败'
  f.props.path = 'D:\\新样本.wav'
  await nextTick()
  assert.equal(f.page.state, 'idle')
  assert.equal(f.page.error, '')
  assert.equal(f.requests[0].options.signal.aborted, true)
  f.page.togglePreview()
  assert.equal(f.requests[1].url, `/api/files/reference-audio?path=${encodeURIComponent(f.props.path)}`)
  f.requests[0].resolve(new Response(JSON.stringify({ message: '旧文件错误' }), { status: 404 }))
  await settle()
  assert.equal(f.page.state, 'loading')
  assert.equal(f.page.error, '')
})

test('组件卸载取消文件读取，迟到错误不再更新界面', async t => {
  const f = await client(t)
  f.page.togglePreview()
  f.unmount()
  assert.equal(f.requests[0].options.signal.aborted, true)
  assert.equal(f.page.state, 'idle')
  f.requests[0].resolve(new Response(JSON.stringify({ message: '旧文件错误' }), { status: 404 }))
  await settle()
  assert.equal(f.page.error, '')
})

test('具体文件错误直接显示，再次点击清错重试，空路径不发请求', async t => {
  const f = await client(t)
  f.page.togglePreview()
  f.requests[0].resolve(new Response(JSON.stringify({ message: '参考文件不存在，请重新选择文件。' }), { status: 404 }))
  await settle()
  assert.equal(f.page.error, '参考文件不存在，请重新选择文件。')
  assert.equal(f.page.state, 'idle')
  f.page.togglePreview()
  assert.equal(f.page.error, '')
  assert.equal(f.page.state, 'loading')
  f.props.path = ' '; await nextTick()
  f.page.togglePreview()
  assert.equal(f.page.disabled, true)
  assert.equal(f.requests.length, 2)
})
