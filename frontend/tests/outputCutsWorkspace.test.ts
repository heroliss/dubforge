import assert from 'node:assert/strict'
import test, { after, before } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia } from 'pinia'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, Workspace: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  ;({ default: Workspace } = await server.ssrLoadModule('/src/components/AssemblyVersionWorkspace.vue'))
})
after(async () => { await server.close() })
async function fixture() {
  let page: any
  const dialogs: any[] = [], events: any[] = []
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] }); await router.push('/')
  const Probe = { setup() {
    useDialog().warning = ((options: any) => { dialogs.push(options); return { destroy() {} } }) as any
    return () => h(Workspace, { uid: 'fixture', configureOnly: true, 'onDirty-change': (value: boolean) => events.push({ name: 'dirty', value }), 'onBusy-change': (value: boolean) => events.push({ name: 'busy', value }) })
  } }
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(Probe) }) }) })
  app.use(router); app.use(createPinia()); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'AssemblyVersionWorkspace') {
    page = this.$.setupState
    page.adopt({ revision: 1, fingerprint: 'fp', defaults: {}, fields: [], sources: {}, versions: [{ id: 'v1', name: '成片', dubbing: false, burn_subtitles: false, include_comments: false, overrides: {}, status: '需更新' }] })
    page.ready = true
  } } })
  const html = await renderToString(app)
  return { page, dialogs, events, html }
}

test('未保存剪辑阻止版本生成，向上报告同一草稿状态，不把版本表单误判为改动', async t => {
  const { page, events, html } = await fixture(); let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; throw new Error('不应发请求') })
  assert.match(html, /成片剪辑/)
  page.selected = ['v1']; page.cutsDirty = true
  assert.equal(page.dirty, false); assert.equal(page.allDirty, true)
  assert.ok(events.some(event => event.name === 'dirty' && event.value))
  await page.generate(); assert.equal(requests, 0)
  assert.equal(page.generationReady(), false); assert.match(page.error, /剪辑设置/)
})

test('保存剪辑期间阻止生成，版本设置编辑仍遵循自己的状态', async () => {
  const { page } = await fixture()
  page.cutsBusy = true
  assert.equal(page.actionLocked, true); assert.equal(page.settingsLocked, false)
  page.cutsBusy = false
  assert.equal(page.actionLocked, false)
})

test('展开剪辑占用独立工作区，不改变当前成片和版本表单', async () => {
  const { page } = await fixture()
  const activeId = page.activeId
  page.cutsExpanded = true
  assert.equal(page.activeId, activeId)
  assert.equal(page.dirty, false)
  page.cutsExpanded = false
  assert.equal(page.activeId, activeId)
  assert.equal(page.allDirty, false)
})
