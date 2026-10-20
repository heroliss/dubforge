import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter, RouterView, isNavigationFailure, NavigationFailureType } from 'vue-router'
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
  let page: any, manager: any, managerProps: any, workspaceProps:any
  app.mixin({ created() {
    if (this.$options.__name === 'SettingsView') {
      page = this.$.setupState
      if (['models', 'runtime'].includes(page.section)) { page.cfg = system; page.profiles = system.llm.profiles.map(p => ({ ...p, paramsJson: '' })); page.markApplied() }
    }
    if (this.$options.__name === 'ConfigurationForm') { scopes.push((this.$props as any).scope); this.$.setupState.adopt(configuration) }
    if (this.$options.__name === 'ConfigurationWorkspace') workspaceProps=this.$props
    if (this.$options.__name === 'BiliAccountManager') {
      manager = this.$.setupState; managerProps = this.$props
      manager.applyCatalog({ accounts: [{ id: '7', name: '测试账号', revision: 1 }], worker_ready: true })
    }
    if (this.$options.__name === 'ProjectTemplateBinding') this.$.setupState.current = boundTemplate
  } })
  const html = await renderToString(app)
  await new Promise(resolve => setImmediate(resolve))
  return { html, page, manager, managerProps, reads, scopes, router, workspaceProps }
}

test('制作阶段深链保留顶层分类和模板范围，旧栏目key归一且阶段切换不丢草稿',async t=>{
 const result=await render(t,'/settings?section=production&template=travel&stage=publication')
 assert.equal(result.page.section,'production');assert.equal(result.page.selectedStage,'publish')
 assert.equal(result.workspaceProps.stage,'publish');assert.equal(result.page.selectedScope,'series:travel')
 result.page.productionDirty=true
 result.page.selectStage('subtitles');await new Promise(resolve=>setImmediate(resolve))
 assert.equal(result.router.currentRoute.value.query.stage,'tts')
 assert.equal(result.router.currentRoute.value.query.section,'production')
 assert.equal(result.router.currentRoute.value.query.template,'travel')
 assert.equal(result.page.productionDirty,true)
})

test('切换配置范围保留精确制作阶段，模型等顶层分类仍保留阶段地址',async t=>{
 const result=await render(t,'/settings?section=production&template=travel&stage=assemble')
 result.page.selectScope('global');await new Promise(resolve=>setImmediate(resolve))
 assert.equal(result.router.currentRoute.value.query.stage,'assemble')
 assert.equal(result.router.currentRoute.value.query.template,undefined)
 result.page.selectSection('models');await new Promise(resolve=>setImmediate(resolve))
 assert.equal(result.router.currentRoute.value.query.section,'models')
 assert.equal(result.router.currentRoute.value.query.stage,'assemble')
})

test('真实路由更新守卫阻止制作操作忙时切换stage，操作结束后保留草稿正常切换',async t=>{
 const result=await render(t,'/settings?section=production&template=travel&stage=assemble')
 result.page.productionBusy=true;result.page.productionDirty=true
 const blocked=await result.router.push('/settings?section=production&template=travel&stage=tts')
 assert.equal(isNavigationFailure(blocked,NavigationFailureType.aborted),true)
 assert.equal(result.router.currentRoute.value.query.stage,'assemble')
 result.page.productionBusy=false
 const accepted=await result.router.push('/settings?section=production&template=travel&stage=tts')
 assert.equal(isNavigationFailure(accepted),false)
 assert.equal(result.router.currentRoute.value.query.stage,'tts')
 assert.equal(result.page.productionDirty,true)
 assert.equal(result.router.currentRoute.value.query.template,'travel')
})

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

test('模型字段和重复操作带配置名称，辅助技术可以区分每组设置', async t => {
  const { html } = await render(t, '/settings?section=models')
  for (const label of ['模型配置 1的名称', '测试连接的接口地址', '测试连接的模型名', '测试连接的 API Key',
    '测试连接是否启用', '测试测试连接连接', '提高测试连接的优先级', '降低测试连接的优先级', '删除测试连接']) {
    assert.ok(html.includes(`aria-label="${label}"`), `应提供关联名称：${label}`)
  }
})

test('项目快捷入口直接编辑绑定模板；无绑定时进入全局默认', async t => {
  const bound = await render(t, '/settings', Binding, 'travel')
  assert.match(bound.html, /href="\/settings\?section=production&amp;template=travel"/)
  assert.match(bound.html, /编辑当前模板/)
  const unbound = await render(t, '/settings', Binding)
  assert.match(unbound.html, /href="\/settings\?section=production"/)
  assert.match(unbound.html, /编辑全局默认/)
})

test('B站账号设置直达账号区域，不装载制作规则或读取模型密钥', async t => {
  const { html, page, reads, scopes } = await render(t, '/settings?section=bilibili')
  assert.equal(page.section, 'bilibili')
  assert.deepEqual(scopes, [])
  assert.equal(reads.includes('/api/settings'), false)
  assert.equal(reads.includes('/api/projects'), false)
  assert.match(html, /已保存的B站账号/)
  assert.match(html, /测试账号/)
  assert.match(html, /检查登录状态/)
  assert.match(html, /管理已有稿件/)
  assert.doesNotMatch(html, /大模型连接|运行并行度|配置范围/)
})

test('项目发布链接可直达指定账号原BV管理，并保留关联项目', async t => {
  const { html, manager, managerProps, reads } = await render(t, '/settings?section=bilibili&account=7&manage=studio&project=project-one')
  assert.equal(manager.tab, 'studio')
  assert.equal(manager.studioAccount, '7')
  assert.equal(managerProps.defaultProject, 'project-one')
  assert.match(html, /查找已有稿件/)
  assert.match(html, /无需关联本地项目/)
  assert.match(html, /返回账号列表/)
  assert.equal(reads.some(url => url.includes('/studio/read/')), false)
  assert.equal(reads.includes('/api/settings'), false)
})
