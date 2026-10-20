import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Preview: any, Outputs: any, useNav: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Preview = (await server.ssrLoadModule('/src/components/AssemblePreviewPanel.vue')).default
  Outputs = (await server.ssrLoadModule('/src/components/AssembleOutputsPanel.vue')).default
  useNav = (await server.ssrLoadModule('/src/stores/nav.ts')).useNavStore
  mock.method(globalThis, 'fetch', async () => new Response('{"overlay":{"items":[]},"info":{"content":{"chapters":[]}}}'))
})
after(async () => { mock.restoreAll(); await server.close() })

async function render(component: any, seed: (page: any, nav: any) => void = () => {}) {
  const pinia = createPinia(), nav = useNav(pinia)
  nav.$patch({ uid: 'fixture', steps: [{ key: 'assemble', label: '合成成片', status: 'done', artifacts: [] }] })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }] })
  await router.push('/')
  let page: any
  const props = component === Preview
    ? { uid: 'fixture', sourceUrl: '/source.mp4', artifacts: [], choice: { dubbing: true, burn_subtitles: false, include_comments: false } }
    : { uid: 'fixture', artifacts: [], sourcePath: '', subtitlePath: '', subtitleName: '', historyOnly: true }
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(component, props) }) }) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === component.__name) { page = this.$.setupState; seed(page, nav) } } })
  const html = await renderToString(app)
  return { page, html, close: () => { nav.reset(); disposePinia(pinia) } }
}

test('试合成时间用完整时分秒，跨小时范围与小数精度不会丢失', async () => {
  const { page, html, close } = await render(Preview, page => { page.duration = 3700; page.start = 3601.25; page.end = 3611.375 })
  try {
    assert.match(html, /01:00:01\.250/); assert.match(html, /01:00:11\.375/)
    assert.match(html, /试合成起点（时分秒）/)
    assert.equal(page.fmt(page.end - page.start), '00:00:10.125')
    assert.equal(page.canSubmit, true)
    page.startInvalid = true; assert.equal(page.canSubmit, false)
    page.startInvalid = false; page.endInvalid = true; assert.equal(page.canSubmit, false)
    page.endInvalid = false; assert.equal(page.canSubmit, true)
  } finally { close() }
})

test('末尾取起点和片头取终点保留最小片段，空值与非有限值不改选区', async () => {
  const { page, close } = await render(Preview)
  try {
    page.onMeta({ target: { duration: 3601.456 } })
    page.setStart(3601.456)
    assert.equal(page.start, 3601.456 - 0.2); assert.equal(page.end, 3601.456)
    assert.equal(page.validRange, true)
    const previous = { start: page.start, end: page.end }
    page.setStart(null); page.setEnd(null); page.setStart(NaN); page.setEnd(Infinity)
    assert.deepEqual({ start: page.start, end: page.end }, previous)
    page.setEnd(0)
    assert.equal(page.start, 0); assert.equal(page.end, 0.2); assert.equal(page.validRange, true)
    page.onMeta({ target: { duration: 0.2 } })
    assert.equal(page.ready, true); assert.equal(page.validRange, true)
  } finally { close() }
})

test('进行中的长视频试合成摘要按时分秒显示，缺失时间不冒充零秒', async () => {
  const { page, html, close } = await render(Outputs, (_page, nav) => {
    nav.steps[0] = { ...nav.steps[0], status: 'running', assembly_task: { scope: 'preview', start: 3661.25, end: 3671.5 } }
  })
  try {
    assert.match(html, /01:01:01\.250–01:01:11\.500/)
    assert.doesNotMatch(page.taskSummary, /3661|3671/)
  } finally { close() }
  const missing = await render(Outputs, (_page, nav) => {
    nav.steps[0] = { ...nav.steps[0], status: 'running', assembly_task: { scope: 'preview' } }
  })
  try { assert.match(missing.page.taskSummary, /—–—/) } finally { missing.close() }
})
