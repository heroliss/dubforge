import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Form: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/PublishSettingsForm.vue')).default
})
after(() => server.close())

const values = { title: '', copyright: 3, watermark: false, close_comments: false,
  close_danmaku: false, selected_comments: false }
const sources = Object.fromEntries(Object.keys(values).map(key => [key, { kind: 'global' }]))
async function render(t: any, extra: any = {}) {
  let page: any
  const buttons: any[] = [], events: string[] = []
  t.mock.method(globalThis, 'fetch', () => { throw new Error('发布参数展示不应读取网络或写入配置') })
  const input = { values: { ...values }, overrides: { title: '', close_comments: false }, sources,
    fieldKeys: ['title'], showPlatformAssist: false, ...extra, onInherit: (key: string) => events.push(key) }
  const beforeInput = JSON.stringify(input)
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Form, input) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'PublishSettingsForm') page = this.$.setupState
    if (this.$options.name === 'Button') buttons.push(this)
  } })
  const html = await renderToString(app)
  assert.equal(JSON.stringify(input), beforeInput)
  return { page, buttons, events, html, input }
}
function click(button: any) { button.$props.onClick({ preventDefault() {}, stopPropagation() {} }) }

test('普通字段与关闭状态开关保留恢复继承动作，明确空值与 false 不当作缺失', async t => {
  const result = await render(t)
  assert.match(result.html, /aria-label="标题恢复继承值"/)
  assert.match(result.html, /aria-label="关闭评论恢复继承值"/)
  click(result.buttons.find(button => button.$attrs['aria-label'] === '标题恢复继承值'))
  click(result.buttons.find(button => button.$attrs['aria-label'] === '关闭评论恢复继承值'))
  assert.deepEqual(result.events, ['title', 'close_comments'])
  assert.deepEqual(result.input.overrides, { title: '', close_comments: false })
})

test('全局字段与开关使用撤销动作名称和提示，事件仍交由父组件处理', async t => {
  const hint = '恢复为当前已保存的全局值，保留其他修改；不会自动保存。'
  const result = await render(t, { resetLabel: '撤销修改', resetHint: hint, inheritance: false })
  assert.match(result.html, /aria-label="标题撤销修改"/)
  assert.match(result.html, /aria-label="关闭评论撤销修改"/)
  assert.ok(result.html.includes(`title="${hint}"`))
  assert.doesNotMatch(result.html, /恢复继承|继承自全局默认/)
  click(result.buttons.find(button => button.$attrs['aria-label'] === '关闭评论撤销修改'))
  assert.deepEqual(result.events, ['close_comments'])
})

test('逐字段恢复名称优先于范围默认名称，开关也遵守同样规则', async t => {
  const result = await render(t, { resetLabel: '撤销修改',
    restoreLabels: { title: '使用视频资料', close_comments: '恢复平台设置' } })
  assert.match(result.html, /aria-label="标题使用视频资料"/)
  assert.match(result.html, /aria-label="关闭评论恢复平台设置"/)
  click(result.buttons.find(button => button.$attrs['aria-label'] === '标题使用视频资料'))
  assert.deepEqual(result.events, ['title'])
})

test('全局无自我继承提示，项目默认仍显示继承来源', async t => {
  const global = await render(t, { overrides: {}, inheritance: false })
  assert.match(global.html, /来源：全局默认/)
  assert.doesNotMatch(global.html, /继承自全局默认/)
  const project = await render(t, { overrides: {} })
  assert.match(project.html, /继承自全局默认/)
})

test('禁用表单保留禁用的恢复入口，迟到动作也不能清除覆盖', async t => {
  const result = await render(t, { disabled: true, resetLabel: '撤销修改' })
  for (const label of ['标题撤销修改', '关闭评论撤销修改']) {
    assert.ok(result.html.includes(`aria-label="${label}"`))
    const button = result.buttons.find(button => button.$attrs['aria-label'] === label)
    assert.equal(button.$props.disabled, true)
    click(button)
  }
  result.page.reset('title'); result.page.reset('close_comments')
  assert.deepEqual(result.events, [])
  assert.deepEqual(result.input.overrides, { title: '', close_comments: false })
})

test('没有本层明确设置时保持可发现的恢复入口并禁用，不触发恢复动作', async t => {
  const result = await render(t, { overrides: {} })
  for (const label of ['标题恢复继承值', '关闭评论恢复继承值']) {
    assert.ok(result.html.includes(`aria-label="${label}"`))
    const button = result.buttons.find(button => button.$attrs['aria-label'] === label)
    assert.equal(button.$props.disabled, true)
    click(button)
  }
  result.page.reset('title'); result.page.reset('close_comments')
  assert.deepEqual(result.events, [])
})
