import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { createRouter, createMemoryHistory } from 'vue-router'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { SOURCE_STYLES, scopeSource, sourcePresentation } from '../src/utils/configurationSource.ts'

let server: ViteDevServer, Badge: any, Form: any, Project: any, Publish: any, Binding: any, Workspace: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null } })
  ;[Badge, Form, Project, Publish, Binding, Workspace] = await Promise.all(['ConfigurationSourceBadge', 'ConfigurationForm', 'ProjectParameterForm', 'PublishSettingsForm', 'ProjectTemplateBinding', 'ConfigurationWorkspace'].map(async name => (await server.ssrLoadModule(`/src/components/${name}.vue`)).default))
})
after(() => server.close())

test('来源类别由身份决定，模板即使叫全局默认或项目设置也保持模板色；文字不随继承状态改变', () => {
  for (const name of ['全局默认', '本项目', '项目设置', '稿件设置']) {
    const source = scopeSource('series:fixture', name)
    assert.equal(source.kind, 'series')
    const local = sourcePresentation(source), inherited = sourcePresentation(source, true)
    assert.equal(local.label, '系列模板')
    assert.equal(local.color, SOURCE_STYLES.series.color)
    assert.equal(local.label, inherited.label)
    assert.equal(local.color, inherited.color)
    assert.match(inherited.title, /继承自系列模板/)
    assert.ok(inherited.title.includes(name))
  }
  assert.equal(sourcePresentation().kind, 'unknown')
  assert.equal(sourcePresentation({ kind: '__proto__' } as any).kind, 'unknown')
})

test('核心配置来源色互不重复，小字号文字在自身底色及界面深色底上达到4.5:1', () => {
  const core = ['global','series','project','version','line','publication'] as const
  assert.equal(new Set(core.map(kind => SOURCE_STYLES[kind].color)).size, core.length)
  function luminance(hex:string) {
    const rgb = hex.slice(1).match(/../g)!.map(x => parseInt(x,16)/255).map(x => x<=.04045?x/12.92:((x+.055)/1.055)**2.4)
    return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722
  }
  for (const style of Object.values(SOURCE_STYLES)) for (const background of [style.background,'#101318','#191e26','#202731']) {
    const values = [luminance(style.color),luminance(background)].sort((a,b)=>b-a)
    assert.ok((values[0]+.05)/(values[1]+.05)>=4.5, style.label)
  }
})

test('标识保留中文文字和可访问说明，模板名安全转义，未知来源不误标为全局', async () => {
  const app = createSSRApp({ render: () => h('div', [
    h(Badge, { source: { kind: 'series', name: '<script>长模板</script>' }, inherited: true }),
    h(Badge),
  ]) })
  const html = await renderToString(app)
  assert.match(html, /系列模板/)
  assert.match(html, /aria-label="继承自系列模板/)
  assert.match(html, /&lt;script&gt;/)
  assert.doesNotMatch(html, /<script>/)
  assert.match(html, /data-source-kind="unknown"/)
  assert.match(html, /来源待确认/)
})

test('设置、步骤与发布表单渲染相同来源时，名称、色值、样式组件完全一致', async t => {
  t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  const path = 'assemble.voice_volume', source = { kind: 'series', name: '旅行系列' }
  const defaults = { [path]: 1, 'assembly.versions': [] }
  const field = { path, label: '人声音量', type: 'float' }
  async function render(component:any, props:any) {
    const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(component, props) }) }) })
    setup(app)
    app.mixin({ created() {
      const page = this.$.setupState
      if (this.$options.__name === 'ConfigurationForm') page.adopt({ key:'global',name:'全局默认',overrides:{},defaults,parent_sources:{[path]:source},sections:{assemble:{label:'合成',fields:[field]}},version_fields:[] })
      if (this.$options.__name === 'ProjectParameterForm') {
        page.fields=[field]; page.draft=defaults; page.defaults=defaults
        page.sources={[path]:source}; page.parentSources={[path]:source}; page.loading=false
      }
    } })
    return renderToString(app)
  }
  const pages = await Promise.all([
    render(Form,{scope:'series:fixture',sections:['assemble']}),
    render(Project,{uid:'fixture',stepKey:'assemble'}),
    render(Publish,{values:{copyright:3},overrides:{},sources:{watermark:source},template:true}),
  ])
  const tags = pages.map(html => html.match(/<span class="configuration-source"[^>]*data-source-kind="series"[^>]*>/)?.[0])
  assert.ok(tags.every(Boolean))
  for (const tag of tags) {
    assert.ok(tag!.includes(`color:${SOURCE_STYLES.series.color}`))
    assert.ok(tag!.includes(`background-color:${SOURCE_STYLES.series.background}`))
    assert.match(tag!, /data-v-/)
  }
  assert.ok(pages.every(html => html.includes('系列模板')))
})

test('模板导航沿用来源色，项目绑定摘要沿用项目设置标识，覆盖数量单独显示', async t => {
  t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, {
    default: () => [h(Binding, { uids: ['fixture'] }), h(Workspace, { scope: 'global' })],
  }) }) })
  app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { template: '<div/>' } }] }))
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ProjectTemplateBinding') this.$.setupState.overrides = 3
    if (this.$options.__name === 'ConfigurationWorkspace') this.$.setupState.rows = [{ key: 'series:fixture', name: '旅行系列', project_count: 2 }]
  } })
  const html = await renderToString(app)
  assert.match(html, /data-source-kind="project"[^>]*style="color:#82D9BE/)
  assert.match(html, /3 项覆盖/)
  assert.ok(html.includes(`color:${SOURCE_STYLES.global.color};`))
  assert.ok(html.includes(`color:${SOURCE_STYLES.series.color};`))
  assert.match(html, /旅行系列/)
})
