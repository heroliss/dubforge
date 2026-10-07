import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'

let server: ViteDevServer, Settings: any, Binding: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Settings = (await server.ssrLoadModule('/src/views/SettingsView.vue')).default
  Binding = (await server.ssrLoadModule('/src/components/ProjectTemplateBinding.vue')).default
})
after(() => server.close())

const system = { llm: { profiles: [{ name: '测试连接', enabled: true, base_url: '', model: '', api_key: '' }] }, runner: {}, server: {} }
const configuration = { key: 'global', name: '测试系列', revision: 1, fingerprint: 'f1', overrides: {},
  defaults: { 'assembly.versions': [], 'download.quality': '1080p' }, parent_origins: {},
  sections: { download: { label: '获取视频', fields: [{ path: 'download.quality', label: '画质', type: 'text' }] } }, version_fields: [] }

async function render(t: any, url: string, component = Settings, boundTemplate: string | null = null) {
  const reads: string[] = [], scopes: string[] = []
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    reads.push(url)
    const data = url === '/api/settings' ? system : url.endsWith('/catalog') ? { accounts: [] }
      : url.endsWith('/templates') ? [] : url.startsWith('/api/configuration/project:') ? { overrides: {}, parent: boundTemplate && 'series:' + boundTemplate }
      : url.startsWith('/api/configuration/') ? configuration : {}
    return new Response(JSON.stringify(data))
  })
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/settings', component }, { path: '/', component: { render: () => null } },
  ] })
  await router.push(url)
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null,
    { default: () => component === Settings ? h(RouterView) : h(Binding, { uids: ['project-fixture'] }) }) }) })
  app.use(router); setup(app)
  let page: any
  app.mixin({ created() {
    if (this.$options.__name === 'SettingsView') {
      page = this.$.setupState
      if (page.section !== 'production') { page.cfg = system; page.profiles = system.llm.profiles.map(p => ({ ...p, paramsJson: '' })); page.markApplied() }
    }
    if (this.$options.__name === 'ConfigurationForm') { scopes.push((this.$props as any).scope); this.$.setupState.adopt(configuration) }
    if (this.$options.__name === 'ProjectTemplateBinding') this.$.setupState.current = boundTemplate
  } })
  const html = await renderToString(app)
  await new Promise(resolve => setImmediate(resolve))
  return { html, page, reads, scopes }
}

test('真实设置页默认只渲染统一制作工作区，不读取模型密钥或复制旧表单', async t => {
  const { html, reads, scopes } = await render(t, '/settings')
  assert.deepEqual(scopes, ['global'])
  assert.match(html, /制作与发布/)
  assert.match(html, /配置范围/)
  assert.match(html, /画质/)
  assert.match(html, /1080p/)
  assert.doesNotMatch(html, /aria-label="设置类别"/)
  assert.doesNotMatch(html, /步骤默认参数|应用全局步骤参数|大模型连接/)
  assert.equal(reads.includes('/api/settings'), false)
})

test('带模板的地址直接打开对应范围；模型与运行入口不额外装载制作表单', async t => {
  const template = await render(t, '/settings?section=production&template=travel')
  assert.deepEqual(template.scopes, ['series:travel'])
  assert.equal(template.page.selectedScope, 'series:travel')
  const models = await render(t, '/settings?section=models')
  assert.equal(models.page.section, 'models')
  assert.deepEqual(models.scopes, [])
  assert.match(models.html, /大模型连接/)
  assert.match(models.html, /环境与服务/)
  const runtime = await render(t, '/settings?section=runtime')
  assert.equal(runtime.page.section, 'runtime')
  assert.deepEqual(runtime.scopes, [])
  assert.match(runtime.html, /保存显示偏好/)
  assert.match(runtime.html, /运行并行度/)
  assert.match(runtime.html, /存储位置/)
  assert.match(runtime.html, /耗时记录与进度估算/)
})

test('项目快捷入口直接编辑绑定模板；无绑定时进入全局默认', async t => {
  const bound = await render(t, '/settings', Binding, 'travel')
  assert.match(bound.html, /href="\/settings\?section=production&amp;template=travel"/)
  assert.match(bound.html, /编辑当前模板/)
  const unbound = await render(t, '/settings', Binding)
  assert.match(unbound.html, /href="\/settings\?section=production"/)
  assert.match(unbound.html, /编辑全局默认/)
})
