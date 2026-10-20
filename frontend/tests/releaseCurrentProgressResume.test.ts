import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, CurrentProgress: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  CurrentProgress = (await server.ssrLoadModule('/src/components/ReleaseCurrentProgress.vue')).default
})
after(async () => { await server.close() })

// 与后端 _base 返回的已受理准备快照保持同形，不伪造 schema=1 诊断。
const preparing = { position: 1, project_uid: 'one', title: '准备中的项目', stage: 'preparing', stage_label: '准备执行',
  phase: '正在核对已有结果与执行条件', progress_label: '正在核对已有结果与执行条件',
  completed_steps: 0, total_steps: 0, percentage: null, indeterminate: true, estimated: false, execution: {} }
async function render(progress: any) {
  const app = createSSRApp({ render: () => h(CurrentProgress, { total: 2, progress }) })
  setup(app)
  return renderToString(app)
}

test('真实 preparing 空诊断快照仍显示中性排队状态，不谎报运行或百分比', async () => {
  const html = await render(preparing)
  assert.match(html, /class="execution-progress/)
  assert.match(html, /data-execution-state="queued"/)
  assert.match(html, /正在核对已有结果与执行条件/)
  assert.doesNotMatch(html, /data-execution-state="running"|class="execution-bar"|0%/)
})

test('未知或无效执行 schema 不吞掉准备阶段说明', async () => {
  for (const execution of [null, undefined, {}, { schema: 2, state: 'running' }, { schema: '1', state: 'running' }]) {
    const html = await render({ ...preparing, execution })
    assert.match(html, /data-execution-state="queued"/)
    assert.match(html, /正在核对已有结果与执行条件/)
  }
})

test('合法执行诊断与审核等待保留各自真实语义', async () => {
  const running = await render({ ...preparing, stage: 'production', percentage: 15, indeterminate: false,
    execution: { schema: 1, state: 'running', label: '正在处理真实语义整理结果' } })
  assert.match(running, /data-execution-state="running"/)
  assert.match(running, /正在处理真实语义整理结果/); assert.match(running, /15%/)
  const waiting = await render({ ...preparing, stage: 'review', phase: '等待平台审核', progress_label: '等待平台审核' })
  assert.match(waiting, /data-execution-state="waiting"/); assert.match(waiting, /等待平台审核/)
})
