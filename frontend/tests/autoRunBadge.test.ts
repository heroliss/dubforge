// 自动运行统一标识: 三类运行状态的图标/配色与优先级 (ADR-0076)。
import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Badge: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Badge = (await server.ssrLoadModule('/src/components/AutoRunBadge.vue')).default
})
after(async () => await server.close())

async function render(props: any) {
  const app = createSSRApp({ render: () => h(Badge, props) })
  setup(app)
  return renderToString(app)
}

test('全流程忙碌/监测/待继续 三态', async () => {
  const busy = await render({ workflowStatus: 'running', runningLabel: '配音' })
  assert.match(busy, /data-run-badge="⚙"/); assert.match(busy, /自动流程 · 配音/)
  const monitor = await render({ workflowStatus: 'monitoring' })
  assert.match(monitor, /已投稿 · 监测审核中/)
  const attention = await render({ workflowStatus: 'attention' })
  assert.match(attention, /上次完整流程需处理/)
})

test('自动链 active/held, 优先级低于全流程', async () => {
  const chain = { remaining: ['translate'], state: 'active' as const }
  assert.match(await render({ chain, runningLabel: '翻译' }), /制作续跑 · 翻译/)
  const held = { remaining: ['translate'], state: 'held' as const, hold: { step: 'translate', code: 'X', message: 'y' } }
  assert.match(await render({ chain: held }), /制作计划已挂起/)
  // 全流程存在时盖过链标识 (引擎互斥下的兜底优先级)。
  const both = await render({ workflowStatus: 'queued', chain, runningLabel: null })
  assert.match(both, /自动流程运行中/); assert.doesNotMatch(both, /制作续跑/)
})

test('无运行状态不渲染; 手动单步回退 🔄 标签', async () => {
  assert.doesNotMatch(await render({}), /data-run-badge/)
  const manual = await render({ runningLabel: '获取视频' })
  assert.match(manual, /data-run-badge="🔄"/); assert.match(manual, /获取视频/)
})

test('旧完整流程中断不会覆盖当前制作，两个状态分别显示', async () => {
  const chain = { remaining: ['assemble'], state: 'active' as const }
  const active = await render({ workflowStatus: 'failed', chain, runningLabel: '合成成片' })
  assert.match(active, /data-run-badge="⛓"/)
  assert.match(active, /制作续跑 · 合成成片/)
  assert.match(active, /data-workflow-history="true"/)
  assert.match(active, /上次完整流程需处理/)
  const manual = await render({ workflowStatus: 'paused', runningLabel: '配音' })
  assert.match(manual, /data-run-badge="🔄"/)
  assert.match(manual, /data-workflow-history="true"/)
  const historical = await render({ workflowStatus: 'failed' })
  assert.equal((historical.match(/data-run-badge=/g) || []).length, 1)
  assert.doesNotMatch(historical, /data-workflow-history/)
})
