import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Help: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Help = (await server.ssrLoadModule('/src/components/ParameterHelp.vue')).default
})
after(() => server.close())

async function render(help = '用途：采样候选。推荐从 0.8 开始。', showIcon = true) {
  let page: any, popover: any, button: any, changes = 0
  const app = createSSRApp({ render: () => h(Help, { label: '累计概率', help, showIcon }, {
    default: () => h('input', { value: '.8', 'aria-label': '累计概率', onInput: () => changes++ }),
  }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ParameterHelp') page = this.$.setupState
    if (this.$options.name === 'Popover') popover = this
    if (this.$options.name === 'Button') button = this
  } })
  const html = await renderToString(app)
  return { page, popover, button, html, changes: () => changes }
}

test('参数区域延迟悬停显示说明，问号可聚焦和点击，打开说明不修改字段', async () => {
  const { page, popover, button, html, changes } = await render()
  assert.equal(popover.$props.trigger, 'hover'); assert.equal(popover.$props.delay, 350)
  assert.match(html, /aria-label="累计概率说明"/)
  assert.equal(page.shown, false)
  page.updateHover(true); assert.equal(page.shown, true)
  page.updateHover(false); assert.equal(page.shown, false)
  page.focus(); assert.equal(page.shown, true)
  button.$.setupState.handleClick({ preventDefault() {}, stopPropagation() {} })
  assert.equal(page.shown, true)
  button.$.setupState.handleClick({ preventDefault() {}, stopPropagation() {} })
  assert.equal(page.shown, false)
  assert.equal(changes(), 0)
})

test('Escape关闭说明，同一字段内移动焦点保持可读，离开后不保持点击弹层', async () => {
  const { page, changes } = await render()
  page.focus(); page.toggle()
  page.blur({ currentTarget: { contains: () => true }, relatedTarget: {} })
  assert.equal(page.shown, true)
  page.blur({ currentTarget: { contains: () => false }, relatedTarget: null })
  assert.equal(page.shown, false)
  page.updateHover(true); page.close(); assert.equal(page.shown, false)
  assert.equal(changes(), 0)
})

test('Escape或点击关闭只影响本次悬停，离开后重新进入仍可再次查看', async () => {
  const { page, changes } = await render()
  page.updateHover(true); page.close()
  assert.equal(page.shown, false)
  page.updateHover(false); assert.equal(page.shown, false)
  page.updateHover(true); assert.equal(page.shown, true)
  page.toggle(); page.toggle(); assert.equal(page.shown, false)
  page.updateHover(true); assert.equal(page.shown, false, '未离开前保持点击关闭')
  page.updateHover(false); page.updateHover(true)
  assert.equal(page.shown, true)
  assert.equal(changes(), 0)
})

test('没有说明不渲染空弹层；控件侧可复用悬停帮助且不重复问号', async () => {
  const empty = await render('  ')
  assert.equal(empty.popover, undefined); assert.equal(empty.button, undefined)
  assert.match(empty.html, /aria-label="累计概率"/)
  const control = await render('推荐从 0.8 开始。', false)
  assert.ok(control.popover); assert.equal(control.button, undefined)
  control.page.focus(); assert.equal(control.page.shown, true)
})
