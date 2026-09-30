// 批量继续失败的自动流程: 只挑可恢复(paused/failed/attention且未投稿)的运行,
// 预检通过才确认; 恢复必须携带预检返回的精确配方与指纹。
import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null } })
  Panel = (await server.ssrLoadModule('/src/components/BatchWorkflowResume.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('不允许未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })

const projects = [{ uid: 'one', title: '项目一' }, { uid: 'two', title: '项目二' }]
const status = (run: any) => ({ run })
const planOf = (uid: string) => ({ recipe: { mode: 'draft', part_titles: {} }, fingerprint: 'fp-' + uid,
  steps: ['tts', 'assemble'], labels: { tts: '字幕与配音', assemble: '合成成片' }, blockers: [] })

async function render() {
  let page: any
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
  await router.push('/')
  const app = createSSRApp({ render: () => h(Panel, { projects }) })
  app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'BatchWorkflowResume') page = this.$.setupState } })
  const html = await renderToString(app)
  return { page, html }
}

test('打开后逐项目读取状态并预检, 只保留可恢复运行; 已投稿不出现', async t => {
  const { page } = await render(); const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const method = options?.method || 'GET'
    calls.push([method, String(url)])
    if (String(url).endsWith('/status')) {
      const uid = String(url).includes('/projects/one/') ? 'one' : 'two'
      return { ok: true, json: async () => status(uid === 'one'
        ? { id: 'run-one', status: 'failed', mode: 'draft', publication_id: '' }
        : { id: 'run-two', status: 'completed', mode: 'draft', publication_id: 'pub-1' }) }
    }
    if (String(url).endsWith('/plan')) return { ok: true, json: async () => planOf('one') }
    return { ok: false, json: async () => ({ message: '意外请求' }) }
  })
  await page.open()
  assert.deepEqual(calls[0], ['GET', '/api/projects/one/workflow/status'])
  assert.deepEqual(calls[1], ['POST', '/api/projects/one/workflow/plan'])
  assert.deepEqual(calls[2], ['GET', '/api/projects/two/workflow/status'])
  assert.equal(page.ready.length, 1)
  assert.match(page.ready[0].plan.steps.map((k: string) => page.ready[0].plan.labels[k]).join(' → '), /合成成片/)
})

test('确认恢复携带预检配方与指纹, 预检失败项目不恢复且不中断其余', async t => {
  const { page } = await render(); const bodies: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const u = String(url)
    if (u.endsWith('/status')) {
      const failed = u.includes('/projects/one/')
      return { ok: true, json: async () => status({ id: failed ? 'run-one' : 'run-two', status: failed ? 'failed' : 'paused', mode: 'draft', publication_id: '' }) }
    }
    if (u.endsWith('/plan')) {
      const uid = u.includes('/projects/one/') ? 'one' : 'two'
      return { ok: true, json: async () => uid === 'one' ? planOf('one')
        : { ...planOf('two'), blockers: ['配音：服务未就绪'] } }
    }
    bodies.push(JSON.parse(String(options?.body)))
    return { ok: true, json: async () => ({}) }
  })
  await page.open()
  assert.equal(page.ready.length, 1)
  await page.resumeAll()
  assert.equal(bodies.length, 1)
  assert.equal(bodies[0].confirmed, true)
  assert.equal(bodies[0].fingerprint, 'fp-one')
  assert.equal(bodies[0].request_id.length, 36)
})

test('预检失败清空旧清单, 不能用过期计划恢复', async t => {
  const { page } = await render(p => p)
  t.mock.method(globalThis, 'fetch', async () => ({ ok: false, json: async () => ({ message: '连接失败' }) }))
  await page.inspect()
  assert.equal(page.ready.length, 0)
  await page.resumeAll()
  assert.equal(page.items.length, 2)   // 每个项目带原因, 不静默跳过
})

test('恢复入口始终显式: 按钮文案与确认数量一致, 不自动恢复', async () => {
  const source = readFileSync(new URL('../src/components/BatchWorkflowResume.vue', import.meta.url), 'utf8')
  assert.match(source, /继续失败的自动流程/)
  assert.match(source, /确认继续 \{\{ ready\.length \}\} 个项目/)
  assert.match(source, /publication_id/)
  assert.match(source, /confirmed: true/)
})
