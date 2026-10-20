import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { createProjectRunSync, PROJECT_RUN_SYNC_DELAYS } from '../src/utils/projectRunFeedback.ts'

function deferred() {
  let resolve: (value: any) => void = () => {}
  const promise = new Promise<any>(done => { resolve = done })
  return { promise, resolve }
}
function timerQueue(t: any) {
  const queue: { id: number; delay: number; run: () => any }[] = []
  let nextId = 0
  t.mock.method(globalThis, 'setTimeout', (run: () => any, delay = 0) => {
    const id = ++nextId
    queue.push({ id, delay, run })
    return id
  })
  t.mock.method(globalThis, 'clearTimeout', (id: number) => {
    const index = queue.findIndex(row => row.id === id)
    if (index >= 0) queue.splice(index, 1)
  })
  return queue
}

test('受理后的轻量短轮询有固定上限，不在慢请求期间叠加读取', async t => {
  const queue = timerQueue(t), pending = deferred(), delays: number[] = []
  let calls = 0
  const sync = createProjectRunSync(async () => { calls++; return calls === 1 ? pending.promise : false })
  sync.start()
  assert.equal(queue.length, 1)
  const first = queue.shift()!; delays.push(first.delay)
  const waiting = first.run()
  assert.equal(calls, 1); assert.equal(queue.length, 0, '请求结束前不得安排并发读取')
  pending.resolve(false); await waiting
  while (queue.length) {
    const timer = queue.shift()!; delays.push(timer.delay); await timer.run()
  }
  assert.equal(calls, PROJECT_RUN_SYNC_DELAYS.length)
  assert.deepEqual(delays, [...PROJECT_RUN_SYNC_DELAYS])
  sync.stop(); assert.equal(queue.length, 0)
})

test('短轮询停止或被新范围替换后，迟到读取不能续接旧定时器', async t => {
  const queue = timerQueue(t), pending = deferred()
  let calls = 0
  const sync = createProjectRunSync(async () => { calls++; return pending.promise })
  sync.start(); const waiting = queue.shift()!.run()
  sync.start(); assert.equal(queue.length, 1)
  pending.resolve(false); await waiting
  assert.equal(queue.length, 1, '旧读取只能结束，不得给新范围额外排队')
  sync.stop(); assert.equal(queue.length, 0)
  assert.equal(calls, 1)
})

test('标签页隐藏或 20 秒窗口结束后不继续加快读取', async t => {
  const queue = timerQueue(t)
  let visible = false, calls = 0, now = 1000
  t.mock.method(Date, 'now', () => now)
  const sync = createProjectRunSync(async () => { calls++; return false }, () => visible)
  sync.start(); await queue.shift()!.run()
  assert.equal(calls, 0); assert.equal(queue.length, 0)
  visible = true; sync.start(); now += 20001
  await queue.shift()!.run()
  assert.equal(calls, 0); assert.equal(queue.length, 0)
  sync.stop()
})

test('轻量读取确认已交接真实进度后立即停止加快轮询', async t => {
  const queue = timerQueue(t)
  let calls = 0
  const sync = createProjectRunSync(async () => { calls++; return true })
  sync.start(); await queue.shift()!.run()
  assert.equal(calls, 1); assert.equal(queue.length, 0)
  sync.stop()
})

let server: ViteDevServer, Projects: any, CurrentProgress: any, useProjectListStore: any
const reply = (data: any, status = 200, workspace = 'review-root-a') => ({ ok: status < 400, status, json: async () => data,
  headers: { get: () => workspace } })
const entry = (uid: string, fields: any = {}) => ({ uid, title: `项目${uid}`, status: 'failed', steps: [],
  source_kind: 'local', dir_exists: true, archived: false, ...fields })
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Projects = (await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  CurrentProgress = (await server.ssrLoadModule('/src/components/ReleaseCurrentProgress.vue')).default
  ;({ useProjectListStore } = await server.ssrLoadModule('/src/stores/projectList.ts'))
  mock.method(globalThis, 'fetch', () => { throw new Error('禁止未声明的真实平台请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
async function render(t: any) {
  const pinia = createPinia()
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: { render: () => null } }, { path: '/:pathMatch(.*)*', component: { render: () => null } },
  ] })
  await router.push('/')
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Projects))) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ProjectsView') page = this.$.setupState } })
  await renderToString(app)
  t.after(() => { page.disposeList(); disposePinia(pinia) })
  return { page, snapshots: useProjectListStore(pinia) }
}

const feedback = (stage = 'checking', id = 'review-operation', extra: any = {}) => ({ id,
  batchId: 'review-batch', groupIds: ['review-group'], projectUids: ['one'], stage,
  message: stage === 'accepted' ? '请求已接收，正在准备执行…' : stage === 'blocked' ? '当前方案已变化，请重新检查。'
    : stage === 'unknown' ? '请求回执未确认，请核对原批次状态。' : '正在核对已有结果与执行条件…', ...extra })
function batch(extra: any = {}, item: any = {}, group: any = {}) {
  return { id: 'review-batch', status: 'running', groups: [{ id: 'review-group', title: '本次恢复组',
    status: 'running', phase: '已受理，正在准备继续任务', feedback: 'preparing', project_uids: ['one'],
    items: [{ project_uid: 'one', status: 'queued', feedback: 'preparing', phase: '正在核对已有结果与执行条件', ...item }],
    ...group }], ...extra }
}
async function initialize(t: any, rows: any[] = [entry('one'), entry('other')]) {
  const state = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'review-root-a' }) : reply(rows))
  await state.page.initializeList()
  return state
}

test('顺序批次的真实准备快照显示中性执行反馈，不因空诊断对象消失或谎报运行', async () => {
  const app = createSSRApp({ render: () => h(CurrentProgress, { total: 2, progress: {
    position: 1, project_uid: 'one', title: '准备中的项目', stage: 'preparing', stage_label: '准备执行',
    phase: '正在核对已有结果与执行条件', progress_label: '正在核对已有结果与执行条件',
    completed_steps: 0, total_steps: 0, percentage: null, indeterminate: true, execution: {},
  } }) })
  setup(app)
  const html = await renderToString(app)
  assert.match(html, /class="execution-progress/)
  assert.match(html, /data-execution-state="(?:queued|waiting)"/)
  assert.doesNotMatch(html, /data-execution-state="running"|class="execution-bar"|0%/)
  assert.match(html, /正在核对已有结果与执行条件/)
})

test('检查反馈立即只落在授权项目，不伪造步骤状态或百分比；明确拒绝后解锁编辑', async t => {
  const steps = [{ key: 'split', status: 'failed', progress: 35, error: '历史错误', job_id: 17 }]
  const { page } = await initialize(t, [entry('one', { steps }), entry('other')])
  const original = JSON.stringify(page.projects)
  page.receiveRunFeedback(feedback())
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).pending, true)
  assert.match(page.projectStartFeedbackOf(page.projects[0]).message, /正在核对/)
  assert.equal(page.projectStartFeedbackOf(page.projects[1]), null)
  assert.equal(JSON.stringify(page.projects), original, '本地提交反馈不能写进真实步骤和项目数据')
  assert.equal(page.projectBusy(page.projects[0]), true)
  page.receiveRunFeedback(feedback('blocked'))
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).warning, true)
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).pending, false)
  assert.equal(page.projectBusy(page.projects[0]), false, '确定拒绝后只展示说明，不能继续占用项目')
})

test('汇总和筛选统一将本轮核对准备计入进行中，拒绝后恢复失败且不影响其他项目', async t => {
  const { page } = await initialize(t)
  assert.equal(page.statusCounts.failed, 2); assert.equal(page.statusCounts.processing || 0, 0)
  for (const stage of ['checking', 'submitting', 'accepted']) {
    page.receiveRunFeedback(feedback(stage))
    assert.equal(page.statusCounts.failed, 1, `${stage} 不重复计入当前失败`)
    assert.equal(page.statusCounts.processing, 1)
    assert.equal(page.pendingRequestCount, 1)
    page.statusFilter = 'failed'
    assert.deepEqual(page.filteredProjects.map((project: any) => project.uid), ['other'])
    page.statusFilter = 'processing'
    assert.deepEqual(page.filteredProjects.map((project: any) => project.uid), ['one'])
    assert.equal(page.projects[0].status, 'failed', '统计展示不能改写真实步骤与项目状态')
  }
  page.receiveRunFeedback(feedback('blocked'))
  assert.equal(page.statusCounts.failed, 2); assert.equal(page.statusCounts.processing || 0, 0)
  assert.equal(page.pendingRequestCount, 0)
  page.statusFilter = 'failed'
  assert.deepEqual(page.filteredProjects.map((project: any) => project.uid), ['one', 'other'])
  page.receiveRunFeedback(feedback('unknown'))
  assert.equal(page.statusCounts.failed, 2, '未知回执保留待核对语义，不能按已受理任务计入进行中')
})

test('已有历史 running 步骤不吞新准备反馈，服务器交接后不改写物理步骤', async t => {
  const steps = [{ key: 'download', status: 'running', progress: 50, job_id: 10 }]
  const { page } = await initialize(t, [entry('one', { steps, running_step: 'download' })])
  page.receiveRunFeedback(feedback()); page.receiveRunFeedback(feedback('accepted'))
  page.applyBatchProgress(batch())
  assert.match(page.projectStartFeedbackOf(page.projects[0]).message, /核对已有结果/)
  assert.equal(page.projects[0].steps[0].job_id, 10)
  assert.equal(page.projects[0].steps[0].progress, 50)
  page.applyBatchProgress(batch({}, { status: 'running', feedback: '', phase: '正在执行语义整理' },
    { feedback: '', phase: '正在执行语义整理' }))
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).pending, true, '调度器交接不等于新物理步骤已启动，旧 running 不能解除等待')
  assert.match(page.projectStartFeedbackOf(page.projects[0]).message, /语义整理/)
  assert.equal(page.projects[0].project_batch.item_phase, '正在执行语义整理')
  assert.equal(page.projects[0].steps[0].key, 'download', '轻量进度不能假造下一物理步骤')
})

test('直接恢复立即轻读执行，旧失败 Job 不解除等待，新排队 Job 才交回真实阶段', async t => {
  const steps = [{ key: 'tts', label: '字幕与配音', status: 'failed', progress: 35, error: '上次失败', job_id: 17 }]
  const { page } = await initialize(t, [entry('one', { steps, workflow_status: 'failed' })])
  let runtime = { ...steps[0] }, calls: string[] = []
  t.mock.method(globalThis, 'fetch', async url => {
    const path = String(url); calls.push(path)
    if (path.startsWith('/api/projects/execution?')) return reply({ workspace_id: 'review-root-a', projects: [{ uid: 'one', steps: [runtime] }] })
    if (path === '/api/projects') return reply([entry('one', { steps: [runtime], workflow_status: 'queued' })])
    throw new Error('不允许额外请求 ' + path)
  })
  const direct = feedback('checking', 'direct', { batchId: undefined, groupIds: [], workflowId: 'old-run' })
  page.receiveRunFeedback(direct); page.receiveRunFeedback({ ...direct, stage: 'accepted', message: '恢复请求已接收，等待执行…' })
  await flush()
  assert.ok(calls.some(path => path.startsWith('/api/projects/execution?')))
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).pending, true, '旧失败记录不能冒充本轮启动或失败')
  assert.equal(page.orderedProjectSteps(page.projects[0]).find((row: any) => row.key === 'tts').status, 'pending')
  assert.equal(page.projects[0].steps[0].status, 'failed', '中性准备展示不修改实际步骤')
  runtime = { ...runtime, status: 'queued', job_id: 18 }
  await page.executionReader.refresh(); await flush()
  assert.equal(page.projectStartFeedbackOf(page.projects[0]), null)
  assert.equal(page.projects[0].steps[0].job_id, 18); assert.equal(page.projects[0].steps[0].status, 'queued')
  assert.ok(calls.every(path => !path.includes('/resume') && !path.includes('/start')))
})

test('直接恢复受理后全部复用而无新 Job，后发完整终态也能解除准备反馈', async t => {
  const { page } = await initialize(t, [entry('one', { workflow_status: 'failed' })])
  t.mock.method(globalThis, 'fetch', async url => String(url).includes('/execution?')
    ? reply({ workspace_id: 'review-root-a', projects: [{ uid: 'one', steps: [] }] })
    : reply([entry('one', { status: 'done', workflow_status: 'completed' })]))
  const direct = feedback('checking', 'cached', { batchId: undefined, groupIds: [], workflowId: 'old-run' })
  page.receiveRunFeedback(direct); page.receiveRunFeedback({ ...direct, stage: 'accepted' })
  await page.load(); await flush()
  assert.equal(page.projectStartFeedbackOf(page.projects[0]), null)
})

test('准备投影严格限定 batch、组与项目；未知回执不被当成成功受理', async t => {
  const { page } = await initialize(t)
  page.receiveRunFeedback(feedback()); page.receiveRunFeedback(feedback('unknown'))
  page.applyBatchProgress(batch())
  assert.equal(page.projects[0].project_batch, undefined)
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).warning, true)
  assert.equal(page.projectStartFeedbackOf(page.projects[0]).pending, false)
  page.receiveRunFeedback(feedback('accepted'))
  page.applyBatchProgress(batch({ id: 'other-batch' }))
  page.applyBatchProgress(batch({}, {}, { id: 'other-group' }))
  page.applyBatchProgress(batch({}, { project_uid: 'other' }))
  assert.equal(page.projects[0].project_batch, undefined)
  assert.equal(page.projects[1].project_batch, undefined)
  page.applyBatchProgress(batch())
  assert.equal(page.projects[0].project_batch.group_id, 'review-group')
  assert.equal(page.projects[1].project_batch, undefined)
})

test('旧操作的 clear 迟到不能删除另一轮新检查反馈', async t => {
  const { page } = await initialize(t)
  page.receiveRunFeedback(feedback('checking', 'old'))
  page.receiveRunFeedback(feedback('checking', 'new', { message: '正在核对新的确认范围…' }))
  page.receiveRunFeedback(feedback('clear', 'old', { message: '' }))
  assert.equal(page.runFeedbacks.one.id, 'new')
  assert.match(page.projectStartFeedbackOf(page.projects[0]).message, /新的确认范围/)
})

test('整表旧响应不能覆盖较晚受理的轻量准备进度，也不能把旧失败重存快照', async t => {
  const { page, snapshots } = await initialize(t)
  const slow = deferred(), calls: string[] = []
  t.mock.method(globalThis, 'fetch', url => { calls.push(String(url)); return slow.promise })
  const loading = page.load()
  page.receiveRunFeedback(feedback()); page.receiveRunFeedback(feedback('accepted'))
  page.applyBatchProgress(batch())
  slow.resolve(reply([entry('one', { project_batch: { batch_id: 'review-batch', group_status: 'attention', phase: '历史错误' } }), entry('other')]))
  await loading
  assert.equal(calls.filter(path => path === '/api/projects').length, 1, '轻量反馈不得额外扫描完整项目列表')
  assert.equal(calls.filter(path => path.startsWith('/api/projects/execution?')).length, 1, '受理后立即补读轻量执行状态')
  assert.equal(page.projects[0].project_batch.feedback, 'preparing')
  assert.match(page.projects[0].project_batch.item_phase, /核对已有结果/)
  assert.equal(snapshots.read('active').rows[0].project_batch.feedback, 'preparing', '返回项目空间不能复活旧失败快照')
})

test('工作目录切换与卸载清空反馈；迟到受理不能污染相同 uid 的新目录项目', async t => {
  const { page, snapshots } = await initialize(t)
  page.receiveRunFeedback(feedback()); page.receiveRunFeedback(feedback('accepted'))
  page.applyBatchProgress(batch())
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'review-root-b' }, 200, 'review-root-b')
    : reply([entry('one', { title: '新工作目录中的同名 uid' })], 200, 'review-root-b'))
  await page.initializeList()
  assert.equal(snapshots.context, '|review-root-b')
  assert.equal(page.projectStartFeedbackOf(page.projects[0]), null)
  page.receiveRunFeedback(feedback('accepted')); page.applyBatchProgress(batch())
  assert.equal(page.projects[0].project_batch, undefined)
  page.receiveRunFeedback(feedback('checking', 'new-context'))
  assert.ok(page.projectStartFeedbackOf(page.projects[0]))
  page.disposeList()
  assert.deepEqual(page.runFeedbacks, {})
  page.receiveRunFeedback(feedback('checking', 'after-disposal')); page.applyBatchProgress(batch())
  assert.deepEqual(page.runFeedbacks, {})
  assert.equal(page.projects[0].project_batch, undefined)
})

test('旧后端刚完成完整读取也必须重读新工作目录，身份先到即清旧卡片和准备态，旧在途结果不倒灌', async t => {
  const { page, snapshots } = await initialize(t)
  page.receiveRunFeedback(feedback()); page.receiveRunFeedback(feedback('accepted')); page.applyBatchProgress(batch())
  const oldRead=deferred(),nextRead=deferred(),calls:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{calls.push({url:String(url),options});return oldRead.promise})
  const oldLoading=page.load()
  assert.equal(calls.length,1)
  t.mock.method(globalThis,'fetch',(url,options)=>{
    calls.push({url:String(url),options})
    return String(url).endsWith('/context')?Promise.resolve(reply({workspace_id:'review-root-b'},200,'review-root-b')):nextRead.promise
  })
  const changing=page.initializeList();await flush()
  assert.equal(calls[0].options.signal.aborted,true,'新身份核对不能复用旧空间的在途读取')
  assert.equal(snapshots.context,'|review-root-b')
  assert.deepEqual(page.projects,[],'身份已改变时慢列表未返回也不能显示旧同名卡片')
  assert.deepEqual(page.runFeedbacks,{})
  assert.equal(calls.filter(call=>call.url==='/api/projects').length,2,'新初读不受旧后端的一分钟节流拦截')
  oldRead.resolve(reply([entry('one',{title:'旧空间迟到',project_batch:{group_status:'running',feedback:'preparing'}})]))
  await oldLoading;assert.deepEqual(page.projects,[])
  nextRead.resolve(reply([entry('one',{title:'新空间同名项目'})],200,'review-root-b'))
  await changing
  assert.equal(page.projects[0].title,'新空间同名项目')
  assert.equal(page.projectStartFeedbackOf(page.projects[0]),null)
  page.receiveRunFeedback(feedback('accepted'));page.applyBatchProgress(batch())
  assert.equal(page.projects[0].project_batch,undefined)
})
