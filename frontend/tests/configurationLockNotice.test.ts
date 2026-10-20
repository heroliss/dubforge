import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Notice: any
const reason = '项目有任务正在执行，结束后可修改设置。查看、导航和停止任务仍可用。'
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Notice = (await server.ssrLoadModule('/src/components/ConfigurationLockNotice.vue')).default
})
after(async () => { await server.close() })

async function render(checking: boolean, refresh: () => Promise<unknown>, seed?: (page: any) => void) {
  let page: any
  // 后台核对是调用方的状态，提示组件只接收稳定文案和手动核对操作。
  const app = createSSRApp({ render: () => h('div', { 'aria-busy': checking }, [h(Notice, { reason, refresh })]) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ConfigurationLockNotice') { page = this.$.setupState; seed?.(page) }
  } })
  const html = await renderToString(app)
  return { page, html }
}

test('后台核对不改变按钮外观或禁止手动核对；提示内容保持可见', async () => {
  let calls = 0
  const { page, html } = await render(true, async () => { calls++ })
  assert.match(html, /项目有任务正在执行/)
  assert.match(html, /重新核对/)
  assert.doesNotMatch(html, /<button\b[^>]*\sdisabled(?:\s|=|>)/)
  assert.doesNotMatch(html, /class="[^"]*\bn-button--loading\b/)
  const idle = await render(false, async () => {})
  assert.equal(html.match(/<button\b[\s\S]*?<\/button>/)?.[0], idle.html.match(/<button\b[\s\S]*?<\/button>/)?.[0])
  assert.equal(page.refreshing, false)
  await page.recheck()
  assert.equal(calls, 1)
  assert.equal(page.refreshing, false)
})

test('只有手动核对显示加载状态，在途重复点击合并且完成后复位', async () => {
  let calls = 0, finish!: (value: unknown) => void, pending!: Promise<unknown>
  const gate = new Promise(resolve => { finish = resolve })
  const { page, html } = await render(false, () => { calls++; return gate }, page => { pending = page.recheck() })
  assert.equal(calls, 1)
  assert.equal(page.refreshing, true)
  assert.match(html, /class="[^"]*\bn-button--loading\b/)
  await page.recheck()
  assert.equal(calls, 1)
  assert.equal(page.refreshing, true)
  finish(false)
  await pending
  assert.equal(page.refreshing, false)
  await page.recheck()
  assert.equal(calls, 2)
  assert.equal(page.refreshing, false)
})

test('手动核对异常结束仍清除加载状态并允许再次核对', async () => {
  let calls = 0, fail!: (reason: unknown) => void, pending!: Promise<unknown>
  const gate = new Promise((_, reject) => { fail = reject })
  const { page } = await render(false, () => { calls++; return calls === 1 ? gate : Promise.resolve(false) }, page => {
    pending = page.recheck()
    // 组件可以处理或继续抛出刷新异常；这里验证两种情况下都不能遗留加载锁。
    void pending.catch(() => {})
  })
  assert.equal(page.refreshing, true)
  fail(new Error('测试核对失败'))
  await pending.catch(() => {})
  assert.equal(page.refreshing, false)
  await page.recheck()
  assert.equal(calls, 2)
  assert.equal(page.refreshing, false)
})
