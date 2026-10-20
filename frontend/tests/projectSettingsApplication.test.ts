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

let server: ViteDevServer, Binding: any, Preview: any, execution: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Binding = (await server.ssrLoadModule('/src/components/ProjectTemplateBinding.vue')).default
  Preview = (await server.ssrLoadModule('/src/components/ConfigurationApplyPreview.vue')).default
  execution = await server.ssrLoadModule('/src/utils/configurationEditability.ts')
})
after(() => server.close())

const plan = () => ({ name: '旅行系列', fingerprint: 'confirmed-plan', template_id: 'travel', projects: [{
  uid: 'fixture', title: '测试视频', preserved: 2, changes: ['assemble.voice_volume'], affected_steps: ['assemble'],
  change_details: [{ path: 'assemble.voice_volume', label: '人声音量', before: 1, after: 0.8 }],
}] })
async function render(t: any, uids = ['fixture'], parent = 'series:travel') {
  let page: any
  const calls: any[] = [], dialogs: any[] = [], events: string[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    calls.push({ url: String(url), options, body: options?.body && JSON.parse(options.body) })
    const data = String(url).endsWith('/templates') ? [{ id: 'travel', name: '旅行系列' }]
      : String(url).includes('/project:') ? { parent, overrides: { 'split.target_word_count': 10, 'tts.tolerance': 0.4 } }
      : String(url).endsWith('/binding-plan') ? plan()
      : String(url).endsWith('/editability') ? { locked: false } : {}
    return { ok: true, json: async () => data }
  })
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, {
    default: () => h(Binding, { uids, onChanged: () => events.push('changed') }),
  }) }) })
  app.use(createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: { render: () => null } }] }))
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ProjectTemplateBinding') {
    page = this.$.setupState
    page.dialog.warning = (options: any) => { dialogs.push(options); return { destroy() {} } }
  } } })
  const html = await renderToString(app)
  await new Promise(resolve => setImmediate(resolve))
  return { page, calls, dialogs, events, html }
}
async function renderPreview(data: any) { return renderToString(createSSRApp({ render: () => h(Preview, { plan: data }) })) }

test('相同模板也可主动应用最新值；先比较影响，确认前不写入', async t => {
  const { page, calls, dialogs, events } = await render(t)
  assert.equal(page.current, 'travel')
  const writes = () => calls.filter(row => row.url.endsWith('/bind'))
  await page.applyLatest(); await page.applyLatest()
  assert.equal(dialogs.length, 1)
  assert.equal(calls.filter(row => row.url.endsWith('/binding-plan')).length, 1)
  assert.deepEqual(calls.find(row => row.url.endsWith('/binding-plan')).body, { uids: ['fixture'], template_id: 'travel' })
  assert.equal(writes().length, 0)
  const content = await renderToString(createSSRApp({ render: () => dialogs[0].content() }))
  for (const text of ['保留项目单独设置', '不会自动制作或投稿', '保留 2 项项目设置', '人声音量', '当前项目值', '应用后的值', '可能需更新：合成成片']) assert.ok(content.includes(text), text)
  await dialogs[0].onPositiveClick()
  assert.equal(writes().length, 1)
  assert.deepEqual(writes()[0].body, { uids: ['fixture'], template_id: 'travel', fingerprint: 'confirmed-plan', confirmed: true })
  assert.deepEqual(events, ['changed']); assert.equal(page.busy, false)
})

test('全局来源主动应用也有预览，取消保留当前项目且不保存', async t => {
  const { page, calls, dialogs } = await render(t, ['fixture'], '')
  assert.equal(page.current, '')
  await page.applyLatest()
  assert.equal(calls.find(row => row.url.endsWith('/binding-plan')).body.template_id, '')
  dialogs[0].onNegativeClick()
  assert.equal(page.busy, false)
  assert.ok(calls.every(row => !row.url.endsWith('/bind')))
})

test('慢应用防重复；请求期间关闭或取消不会解除写入锁', async t => {
  const { page, calls, dialogs } = await render(t)
  await page.applyLatest()
  let finish: any, count = 0
  t.mock.method(globalThis, 'fetch', (url: any) => {
    if (String(url).endsWith('/bind')) { count++; return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({}) }) }) }
    return Promise.resolve({ ok: true, json: async () => String(url).endsWith('/templates') ? [] : { parent: '', overrides: {} } })
  })
  const saving = dialogs[0].onPositiveClick()
  await dialogs[0].onPositiveClick()
  assert.equal(count, 1); assert.equal(page.busy, true)
  assert.equal(dialogs[0].onNegativeClick(), false); assert.equal(dialogs[0].onClose(), false)
  dialogs[0].onMaskClick(); assert.equal(page.busy, true)
  await page.applyLatest(); assert.equal(calls.filter(row => row.url.endsWith('/binding-plan')).length, 1)
  finish(); await saving; assert.equal(page.busy, false)
})

test('确认前项目开始运行时不应用；现有基础与覆盖保持', async t => {
  const { page, calls, dialogs, events } = await render(t)
  await page.applyLatest()
  execution.setNavigationExecution('fixture', true)
  try {
    page.editability.observing.value = true
    t.mock.method(globalThis, 'fetch', async () => ({ ok: true, json: async () => ({ locked: true, reason: '项目正在运行' }) }))
    await dialogs[0].onPositiveClick()
    assert.match(page.error, /暂不能应用基础设置/)
    assert.equal(page.current, 'travel'); assert.equal(page.overrides, 2); assert.equal(page.busy, false)
    assert.deepEqual(events, []); assert.ok(calls.every(row => !row.url.endsWith('/bind')))
  } finally { execution.setNavigationExecution('', false, true) }
})

test('批量选择来源只预览，保留目标顺序并等待明确确认', async t => {
  const { page, calls, dialogs } = await render(t, ['b', 'a'])
  await page.bind('travel')
  const request = calls.find(row => row.url.endsWith('/binding-plan'))
  assert.deepEqual(request.body.uids, ['b', 'a']); assert.equal(dialogs.length, 1)
  assert.ok(calls.every(row => !row.url.endsWith('/bind')))
  dialogs[0].onNegativeClick()
})

test('变化预览转义文字并保留显式空值、false、0，不误提示重做无关步骤', async () => {
  const data = plan(); data.projects[0].affected_steps = []
  data.projects[0].change_details = [
    { path: 'title', label: '<script>字段</script>', before: '<script>旧值</script>', after: '' },
    { path: 'voice', label: '数值', before: false as any, after: 0 as any },
  ] as any
  const html = await renderPreview(data)
  assert.match(html, /&lt;script&gt;旧值&lt;\/script&gt;/); assert.doesNotMatch(html, /<script>/)
  assert.match(html, /（空文本）/); assert.match(html, />false</); assert.match(html, />0</)
  assert.match(html, /没有制作步骤需要因本次应用更新/)
})

test('发布平台字段使用中文名称，不把内部配置路径直接展示给用户', async () => {
  const data = plan()
  data.projects[0].change_details = [
    { path: 'publish.human_type2', label: 'publish.human_type2', before: 1029, after: 1011 },
    { path: 'publish.season_id', label: 'publish.season_id', before: 9278299, after: 9318140 },
    { path: 'publish.section_id', label: 'publish.section_id', before: 10376340, after: 10424451 },
  ]
  const html = await renderPreview(data)
  for (const label of ['分区', '合集', '合集小节']) assert.match(html, new RegExp(label))
  assert.doesNotMatch(html, /publish\.(human_type2|season_id|section_id)/)
})
