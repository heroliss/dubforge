// ChainStatus 组件: 链三态 (active/held-failed/held-interrupted) 的渲染与操作入口。
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
import { createPinia } from 'pinia'
import { NDialogProvider, NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Chain: any, useNavStore: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null } })
  Chain = (await server.ssrLoadModule('/src/components/ChainStatus.vue')).default
  ;({ useNavStore } = await server.ssrLoadModule('/src/stores/nav.ts'))
  mock.method(globalThis, 'fetch', () => { throw new Error('不允许未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })

const activeChain = () => ({ remaining: ['translate', 'tts'], state: 'active' })
const heldChain = (code = 'STEP_FAILED', message = '模拟失败') => ({
  remaining: ['translate', 'tts'], state: 'held',
  hold: { step: 'translate', code, message },
})

async function render(chain: any, navSeed: any = {}) {
  const pinia = createPinia(), nav = useNavStore(pinia)
  nav.$patch({ uid: 'fixture', chain, ...navSeed })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
  await router.push('/')
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(Chain, { uid: 'fixture', chain }) }) }) })
  app.use(pinia); app.use(router); setup(app)
  const html = await renderToString(app)
  return { nav, html }
}

test('无链不渲染; active 显示剩余计划与停止入口', async () => {
  const none = await render(null)
  assert.doesNotMatch(none.html, /chain-status/)
  const html = (await render(activeChain())).html
  assert.match(html, /自动运行中/)
  assert.match(html, /剩余：翻译 → 字幕与配音/)
  assert.match(html, /停止后续自动运行/)
  assert.doesNotMatch(html, /放弃计划/)
})

test('挂起显示原因分类与 恢复/放弃 双操作', async () => {
  const html = (await render(heldChain())).html
  assert.match(html, /自动运行已挂起/)
  assert.match(html, /「翻译」失败 \[STEP_FAILED\]：模拟失败/)
  assert.match(html, /重试并继续/)
  assert.match(html, /放弃计划/)
  assert.match(html, /查看该步骤/)
})

test('中断与停止的分类文案', async () => {
  assert.match((await render(heldChain('STEP_INTERRUPTED', '上次执行被中断 (进程退出)'))).html, /被中断/)
  assert.match((await render(heldChain('STEP_CANCELLED', '用户取消'))).html, /已停止/)
})

test('组件文案与帮助保持一致: 恢复动作明确指向剩余计划', async () => {
  const source = readFileSync(new URL('../src/components/ChainStatus.vue', import.meta.url), 'utf8')
  // 调用 chain/{resume,stop} 两个端点; 恢复按钮在项目运行中被禁用, 避免争抢。
  assert.match(source, /chain\/\$\{path\}/)
  assert.match(source, /'resume'/)
  assert.match(source, /'stop'/)
  assert.match(source, /nav\.busy/)
})
