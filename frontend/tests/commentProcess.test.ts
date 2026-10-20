import test, { before, after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { commentJobProgress, commentPreparationHint, commentProcessStages, commentProgressJob } from '../src/utils/commentProcess.ts'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/CommentProcessPanel.vue')).default
})
after(() => server.close())

async function renderPanel(props: any) {
  const app = createSSRApp({ render: () => h(Panel, props) })
  setup(app)
  return renderToString(app)
}

test('评论过程：新提交动作优先于上次完成记录，不伪装后续排队', () => {
  const progress = { job: { action: 'translate', status: 'done', processed: 10, total: 10 } }
  const job: any = commentProgressJob(progress, null, 'filter')
  assert.equal(job.action, 'filter')
  assert.equal(job.phase_label, '准备提交')
  const stages = commentProcessStages(progress, { rows: [] }, null, 'filter')
  assert.equal(stages.find(row => row.id === 'filter')?.state, 'running')
  assert.notEqual(stages.find(row => row.id === 'overlay')?.state, 'running')
})

test('评论过程：旧任务只显示已有事实，不猜测思考或请求进度', () => {
  const job = { action: 'filter', status: 'running', processed: 4, total: 10, skipped: 2, cached: 8, batch: 1 }
  const view = commentJobProgress(job)
  assert.equal(view.completed, 6)
  assert.equal(view.percent, 60)
  assert.equal(view.phase, '等待本批处理结果')
  assert.equal(view.total, 10)
})

test('整体准备复用真实子任务，结束后的停止标记不再伪装进行中', () => {
  const child = { action: 'translate', status: 'running', phase: 'thinking' }
  assert.equal(commentProgressJob({ jobs: { translate: child } }, null, 'prepare'), child)
  assert.equal(commentProgressJob({ job: { ...child, status: 'done' } }, null, 'prepare'), null)
  assert.equal(commentJobProgress({ ...child, status: 'cancelled', cancelling: true, phase_label: '正在停止' }).phase, '处理已停止')
})

test('评论过程：已归类失败计入已处理，缓存不重复加入本次请求总量', () => {
  const view = commentJobProgress({ action: 'translate', status: 'partial', processed: 7, skipped: 3, total: 10, cached: 20 })
  assert.equal(view.completed, 10)
  assert.equal(view.percent, 100)
  assert.equal(view.processed, 7)
})

test('评论过程：采集中旧快照不成为实时百分比，也不标记采集完成', () => {
  const stages = commentProcessStages(null, { rows: Array.from({ length: 100 }) }, { status: 'running', busy: true, count: 100, limit: 20 })
  assert.equal(stages[0].state, 'running')
  assert.equal(stages[0].detail, '正在向平台获取')
})

test('评论过程：空项目所有阶段等待操作，已有合法评分和译文单独计数', () => {
  const empty = commentProcessStages(null, { rows: [] }, { status: 'idle', count: 0 })
  assert.ok(empty.every(row => row.state === 'idle'))
  assert.equal(empty[0].detail, '尚未获取')
  const rows = [{ text_zh: '译文' }, { text_zh: '' }]
  const populated = commentProcessStages(null, { rows, rating_progress: { saved: 1 }, overlay: { items: [rows[0]] } })
  assert.equal(populated[1].detail, '有效评分 1 条')
  assert.equal(populated[2].detail, '有效译文 1 条')
  assert.equal(populated[3].detail, '已确认 1 条')
})

test('准备已成功复用缓存时旧停止不标待处理，后来新失败仍明确显示', () => {
  const progress = { prepare_job: { status: 'done', finished_at: '2026-10-16T10:00:00Z' },
    jobs: { translate: { action: 'translate', status: 'cancelled', finished_at: '2026-10-16T09:00:00Z' } } }
  const state = { rows: [{ text_zh: '缓存译文' }] }
  assert.equal(commentProcessStages(progress, state)[2].state, 'available')
  progress.jobs.translate.finished_at = '2026-10-16T11:00:00Z'
  assert.equal(commentProcessStages(progress, state)[2].state, 'attention')
})

test('评论上屏阶段区分未确认、已保存空方案与非空方案，不把空方案当成未保存', async () => {
  const stage = (state: any) => commentProcessStages(null, state).find(row => row.id === 'overlay')!
  for (const state of [{ rows: [] }, { rows: [], overlay: null }]) {
    assert.equal(stage(state).state, 'idle')
    assert.equal(stage(state).detail, '尚未确认')
  }
  const empty = { rows: [], overlay: { items: [], settings: {}, confirmed_at: '2026-10-17T01:00:00Z' } }
  assert.equal(stage(empty).state, 'available')
  assert.equal(stage(empty).detail, '已保存空方案')
  const html = await renderPanel({ state: empty })
  assert.match(html, /已保存空方案/)
  assert.doesNotMatch(html, /尚未确认/)
  assert.match(html, /已有保存结果/)
  assert.equal(stage({ rows: [], overlay: { items: [{ id: 'a' }] } }).detail, '已确认 1 条')
})

test('只有当前自动评论阶段提示自动保存，手动prepare与无归属后台任务各自如实说明', async () => {
  const progress = { prepare_job: { id: 'real-prepare', status: 'running', target: 10 } }
  const automatic = { state: 'running', message: '评论筛选：已准备 4/10 条' }
  const autoHtml = await renderPanel({ progress, automation: automatic })
  assert.match(autoHtml, /准备完成后由当前自动流程确认保存/)
  assert.doesNotMatch(autoHtml, /上屏草稿完成后仍需确认/)
  const manualHtml = await renderPanel({ progress, pending: 'prepare' })
  assert.match(manualHtml, /上屏草稿完成后仍需确认/)
  assert.doesNotMatch(manualHtml, /由当前自动流程确认保存/)
  for (const state of ['done', 'upcoming', 'none', 'stopped']) {
    const history = { state, message: '已有历史自动流程' }
    assert.match(commentPreparationHint(history, 'prepare'), /仍需确认/)
    assert.doesNotMatch(commentPreparationHint(history), /自动流程确认保存|仍需确认/)
  }
  const unknownHtml = await renderPanel({ progress, automation: { state: 'done', message: '历史流程已完成' } })
  assert.match(unknownHtml, /上屏方案以最终保存结果为准/)
  assert.doesNotMatch(unknownHtml, /由当前自动流程确认保存|上屏草稿完成后仍需确认/)
})

test('流程轨迹保留四阶段真值与当前阶段标识，轻量和完整模式使用同一进度事实', async () => {
  const progress = { collection: { status: 'completed', count: 4 }, jobs: {
    filter: { action: 'filter', status: 'done', processed: 4, total: 4 },
    translate: { id: 'current-translation', action: 'translate', status: 'running',
      processed: 2, skipped: 1, total: 4, phase_label: '正在接收结果' },
  } }
  const state = { rows: [{ text_zh: '已保存译文' }], rating_progress: { saved: 4 },
    overlay: { items: [{ id: 'saved' }] } }
  for (const compact of [false, true]) {
    const html = await renderPanel({ progress, state, compact })
    assert.match(html, /aria-label="评论处理阶段"/)
    assert.equal((html.match(/data-stage-id=/g) || []).length, 4)
    assert.match(html, /data-stage-id="translate" aria-current="step"/)
    assert.equal((html.match(/aria-current="step"/g) || []).length, 1)
    assert.match(html, /已获取 4 条/)
    assert.match(html, /有效评分 4 条/)
    assert.match(html, /已保存 2 \/ 4 条/)
    assert.match(html, /已确认 1 条/)
    assert.match(html, /正在接收结果/)
    assert.match(html, /本阶段 75%/)
    assert.equal(html.includes('刷新进度'), !compact)
    assert.equal(html.includes('停止当前处理'), !compact)
  }
})
