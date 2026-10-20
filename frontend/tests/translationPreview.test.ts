import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createPinia, disposePinia } from 'pinia'
import { createRouter, createMemoryHistory, RouterView } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, StepView: any, useNavStore: any
const savedWindow = Object.getOwnPropertyDescriptor(globalThis, 'window')
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  StepView = (await server.ssrLoadModule('/src/views/StepView.vue')).default
  useNavStore = (await server.ssrLoadModule('/src/stores/nav.ts')).useNavStore
  Object.defineProperty(globalThis, 'window', { configurable: true, value: {
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
  } })
})
after(async () => {
  await server.close()
  if (savedWindow) Object.defineProperty(globalThis, 'window', savedWindow)
  else Reflect.deleteProperty(globalThis, 'window')
})

for (const state of ['partial', 'waiting', 'complete', 'failed-partial']) {
  test(`翻译${state}预览与正式产物分开，首次运行无需先登记半成品`, async t => {
    const reads: string[] = []
    const phase = state === 'failed-partial' ? 'partial' : state
    const text = phase === 'partial' ? '1. 本次窗口\n' : phase === 'waiting' ? '' : '1. 上次完整一\n2. 上次完整二\n'
    t.mock.method(globalThis, 'fetch', (url: any) => {
      if (!String(url).includes('translate.txt/raw')) return new Promise(() => {})
      reads.push(String(url))
      return Promise.resolve(new Response(text, { headers: phase === 'complete' ? {} : {
        'X-DubForge-Preview': phase, 'X-DubForge-Completed': '1', 'X-DubForge-Total': '2',
      } }))
    })
    const pinia = createPinia(), nav = useNavStore(pinia)
    nav.$patch({ uid: 'preview-fixture', title: '翻译预览', stepsLoaded: true, steps: [{
      key: 'translate', label: '翻译', status: state === 'failed-partial' ? 'failed' : 'running', job_id: 9,
      partial_preview_job_id: state === 'failed-partial' ? 9 : null, cfg: {}, artifacts: [], inputs: [], outputs: [],
    }] })
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/projects/:uid/steps/:key', component: StepView }] })
    await router.push('/projects/preview-fixture/steps/translate')
    let page: any
    const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(RouterView) }) }) })
    app.use(pinia); app.use(router); setup(app)
    app.mixin({ created() { if (this.$options.__name === 'StepView') page = this.$.setupState } })
    try {
      const html = await renderToString(app)
      await page.loadText('translate.txt', true)
      assert.equal(page.preview.kind, 'text')
      assert.equal(page.preview.name, 'translate.txt')
      assert.equal(page.previewTextRaw, text)
      assert.equal(page.translationPreviewState, phase === 'complete' ? '' : phase)
      assert.equal(page.previewTextFailed, false)
      assert.ok(reads.length && reads.every(url => url.endsWith('/raw?preview=true')))
      assert.deepEqual(nav.stepMap.translate.artifacts, [])
      assert.match(html, /产出预览/)
    } finally { page.textLoader.cancel(); nav.reset(); disposePinia(pinia) }
  })
}
