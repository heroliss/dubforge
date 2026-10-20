import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { parse } from '@vue/compiler-sfc'
import { readFile } from 'node:fs/promises'
import * as batchDisplay from '../src/utils/projectBatch.ts'

let server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/ProjectBatchPanel.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止真实平台请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
const rows = [
  { project_uid: 'three', title: '合集第三集', source_date: '2026-03-01', delivery_mode: 'publish' },
  { project_uid: 'standalone', title: '独立制作视频', source_date: '2026-02-15', delivery_mode: 'produce' },
  { project_uid: 'one', title: '合集第一集', source_date: '2026-01-01', delivery_mode: 'publish' },
  { project_uid: 'two', title: '合集第二集', source_date: '2026-02-01', delivery_mode: 'publish' },
  { project_uid: 'upload', title: '上传等待确认的视频', source_date: '2026-04-01', delivery_mode: 'upload' },
]
const targets = rows.map(row => ({ uid: row.project_uid, title: row.title }))
const reply = (data: any, status = 200) => ({ ok: status < 400, status, json: async () => data })
function plan(ids = ['one', 'two', 'standalone', 'three', 'upload'], groupIds: string[] = []) {
  const ordered = ids.map(id => rows.find(row => row.project_uid === id)!)
  const collection = ordered.filter(row => ['one', 'two', 'three'].includes(row.project_uid))
  const groups = [{ id: 'collection', kind: 'collection', title: '测试账号 · 历史合集 · 中国史', items: collection, project_uids: collection.map(row => row.project_uid), status: 'ready' },
    ...ordered.filter(row => !collection.includes(row)).map(row => ({ id: row.project_uid, kind: 'independent', title: row.title, items: [row], project_uids: [row.project_uid], status: 'ready' }))]
  return { groups, group_ids: groupIds.length ? groupIds : groups.map(group => group.id), fingerprint: JSON.stringify([ids, groupIds]), blockers: [], warnings: [] }
}
async function render(seed: (page: any) => void = () => {}) {
  let page: any
  const feedback: any[] = [], progress: any[] = []
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }, { path: '/projects/:uid', component: { render: () => null } }, { path: '/settings', component: { render: () => null } }] }); await router.push('/')
  const app = createSSRApp({ render: () => h(Panel, { hiddenTrigger: true,
    onRunFeedback: (value: any) => feedback.push(value), onBatchProgress: (value: any) => progress.push(value) }) }); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ProjectBatchPanel') { page = this.$.setupState; seed(page) } } })
  const context: any = {}, html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
  return { page, html, feedback, progress }
}
function fixtures(t: any, custom: (path: string, body: any) => any = () => undefined) {
  let saved: any = null, current = plan()
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const path = String(url), body = options?.body && JSON.parse(options.body); calls.push({ path, body })
    const override = custom(path, body); if (override !== undefined) return override
    if (path === '/api/project-batches/candidates' || path.includes('/candidates?')) return reply({ items: rows })
    if (path === '/api/project-batches/plan') { current = plan(body.project_uids); return reply(current) }
    if (path === '/api/project-batches' && body) { saved = { id: 'batch', status: 'ready', groups: current.groups }; return reply(saved) }
    if (path === '/api/project-batches' || path.includes('?request_id=')) return reply({ batches: saved ? [saved] : [] })
    if (path === '/api/project-batches/batch') return reply(saved || { id: 'batch', status: 'paused', groups: current.groups })
    if (path === '/api/project-batches/batch/plan') return reply(body.group_ids?.length ? plan(current.groups.flatMap(group => group.items).map(item => item.project_uid), body.group_ids) : current)
    if (/\/batch\/(start|pause|stop)$/.test(path)) { saved = { id: 'batch', status: path.endsWith('/start') ? 'running' : path.endsWith('/pause') ? 'paused' : 'stopped', groups: current.groups }; return reply(saved) }
    throw new Error('意外请求 ' + path)
  })
  return calls
}
const consent = (page: any) => { page.orderConfirmed = true; page.rightsConfirmed = true }

test('混选5个项目自动按真实预检分组，不在弹窗改写终点，打开不启动', async t => {
  const { page } = await render(), calls = fixtures(t); await page.open(targets)
  assert.equal(page.groups.length, 3); assert.equal(page.publishCount, 3)
  assert.deepEqual(page.groups[0].items.map((item: any) => item.project_uid), ['one', 'two', 'three'])
  assert.deepEqual(calls.find(call => call.path === '/api/project-batches/candidates').body, { project_uids: targets.map(target => target.uid) })
  assert.equal(calls.filter(call => call.path.includes('/candidates?')).length, 0)
  assert.deepEqual(calls.find(call => call.path === '/api/project-batches/plan').body, { project_uids: ['one', 'two', 'standalone', 'three', 'upload'], order_confirmed: true })
  assert.ok(calls.every(call => !call.path.endsWith('/start') && !(call.path === '/api/project-batches' && call.body)))
  assert.equal(page.canStart, false); page.orderConfirmed = true; assert.equal(page.canStart, false)
  page.rightsConfirmed = true; assert.equal(page.canStart, true)
})

test('组内上下移后重新检查指纹，其他独立项目位置和保存终点不变', async t => {
  const { page } = await render(), calls = fixtures(t); await page.open(targets); consent(page)
  await page.move(page.groups[0], 1, -1)
  assert.equal(page.orderConfirmed, false); assert.equal(page.rightsConfirmed, false); assert.equal(page.canStart, false)
  assert.deepEqual(page.groups[0].items.map((item: any) => item.project_uid), ['two', 'one', 'three'])
  assert.deepEqual(calls.filter(call => call.path === '/api/project-batches/plan').at(-1).body.project_uids, ['two', 'one', 'standalone', 'three', 'upload'])
  assert.equal(page.groups.find((group: any) => group.id === 'standalone').items[0].delivery_mode, 'produce')
  assert.equal(calls.filter(call => call.path === '/api/project-batches/candidates').length, 1)
})

test('最终一次确认先保存父批次再核对启动，双击不重复创建', async t => {
  const { page } = await render(); let release: () => void = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  const calls = fixtures(t, (path, body) => path === '/api/project-batches' && body ? gate.then(() => reply({ id: 'batch', status: 'ready', groups: plan().groups })) : undefined)
  await page.open(targets); consent(page)
  const first = page.start(); await page.start(); release(); await first
  assert.equal(calls.filter(call => call.path === '/api/project-batches' && call.body).length, 1)
  assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 1)
  const start = calls.find(call => call.path.endsWith('/start')); assert.equal(start.body.rights_confirmed, true)
  assert.ok(calls.findIndex(call => call.path.endsWith('/batch/plan')) < calls.findIndex(call => call.path.endsWith('/start')))
  assert.equal(page.show, false)
})

test('启动前目标变化时不启动，展示新方案要求重新确认', async t => {
  const { page } = await render(), calls = fixtures(t, path => path.endsWith('/batch/plan') ? reply({ ...plan(), fingerprint: 'changed' }) : undefined)
  await page.open(targets); consent(page); await page.start()
  assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 0)
  assert.equal(page.existing.id, 'batch'); assert.equal(page.preview.fingerprint, 'changed')
  assert.equal(page.orderConfirmed, false); assert.equal(page.rightsConfirmed, false); assert.match(page.error, /方案已变化/)
})

test('创建回执丢失后锁定原方案，重新检查按request_id找回且不另建', async t => {
  const { page } = await render(); let lost = false
  const calls = fixtures(t, (path, body) => {
    if (path === '/api/project-batches' && body && !lost) { lost = true; return Promise.reject(new Error('Failed to fetch')) }
    if (path.includes('?request_id=')) return reply({ batches: [{ id: 'batch', status: 'ready', groups: plan().groups }] })
  })
  await page.open(targets); consent(page); await page.start()
  assert.ok(page.pendingCreation); assert.equal(page.locked, true); assert.match(page.error, /后台任务可能仍在继续/)
  page.close(); await page.open([{ uid: 'other', title: '不能另建' }]); assert.equal(page.existing.id, 'batch')
  consent(page); await page.start()
  assert.equal(calls.filter(call => call.path === '/api/project-batches' && call.body).length, 1)
  assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 1)
})

test('启动回执丢失后重试沿用同一启动标识与已持久化批次', async t => {
  const { page } = await render(); let starts = 0
  const calls = fixtures(t, path => path.endsWith('/start') && ++starts === 1 ? Promise.reject(new Error('Failed to fetch')) : undefined)
  await page.open(targets); consent(page); await page.start(); await page.start()
  const requests = calls.filter(call => call.path.endsWith('/start'))
  assert.equal(requests.length, 2); assert.equal(requests[0].body.request_id, requests[1].body.request_id)
  assert.equal(calls.filter(call => call.path === '/api/project-batches' && call.body).length, 1)
})

test('创建仍在途时保持原UUID与方案锁定，核对ready后才能继续启动', async t => {
  const { page } = await render(); let ready = false
  const calls = fixtures(t, (path, body) => {
    if (path === '/api/project-batches' && body) return reply({ id: 'batch', status: 'creating' })
    if (path.includes('?request_id=')) return reply({ batches: [{ id: 'batch', status: ready ? 'ready' : 'creating', groups: plan().groups }] })
  })
  await page.open(targets); consent(page); await page.start()
  const request = page.pendingCreation.requestId
  assert.equal(page.pendingCreation.state, 'creating'); assert.equal(page.canStart, false); assert.equal(page.existing, null)
  assert.equal(calls.filter(call => call.path.endsWith('/batch/plan')).length, 0)
  await page.inspect(); assert.equal(page.pendingCreation.requestId, request); assert.equal(page.locked, true)
  ready = true; await page.inspect(); assert.equal(page.pendingCreation, null); assert.equal(page.existing.id, 'batch')
  consent(page); await page.start()
  assert.equal(calls.filter(call => call.path === '/api/project-batches' && call.body).length, 1)
  assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 1)
})

test('只有持久化creation_failed才能解锁旧创建请求并使用新UUID', async t => {
  const { page } = await render(); let creates = 0
  const calls = fixtures(t, (path, body) => {
    if (path === '/api/project-batches' && body && ++creates === 1) return Promise.reject(new Error('Failed to fetch'))
    if (path.includes('?request_id=')) return reply({ batches: [{ id: 'batch', status: 'creation_failed', creation_error: { message: '项目目标已变化' } }] })
  })
  await page.open(targets); consent(page); await page.start(); const oldRequest = page.pendingCreation.requestId
  await page.inspect(); assert.equal(page.pendingCreation, null); assert.equal(page.existing, null); assert.equal(page.locked, false)
  assert.ok(page.preview); assert.match(page.error, /项目目标已变化/); consent(page); await page.start()
  const requests = calls.filter(call => call.path === '/api/project-batches' && call.body)
  assert.equal(requests.length, 2); assert.equal(requests[0].body.request_id, oldRequest); assert.notEqual(requests[1].body.request_id, oldRequest)
})

test('取消找回创建回执后，迟到ready不能改写已关闭页面状态', async t => {
  const { page } = await render(); let release: (data: any) => void = () => {}
  const gate = new Promise(resolve => { release = resolve })
  fixtures(t, (path, body) => path === '/api/project-batches' && body ? Promise.reject(new Error('Failed to fetch')) : path.includes('?request_id=') ? gate : undefined)
  await page.open(targets); consent(page); await page.start()
  const pending = page.inspect(); page.close(); release(reply({ batches: [{ id: 'batch', status: 'ready', groups: plan().groups }] })); await pending
  assert.equal(page.existing, null); assert.ok(page.pendingCreation); assert.equal(page.show, false)
})

test('只恢复独立上传组时不要求无关合集顺序或投稿授权，原计划保持只读', async t => {
  const { page } = await render(), calls = fixtures(t)
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload'])
  assert.deepEqual(page.groups.map((group: any) => group.id), ['upload']); assert.equal(page.publishCount, 0)
  assert.equal(page.orderedGroups.length, 0); assert.equal(page.canStart, true); assert.equal(page.locked, true)
  await page.move(page.groups[0], 0, 1)
  assert.ok(calls.filter(call => call.path.endsWith('/batch/plan')).every(call => JSON.stringify(call.body) === JSON.stringify({ group_ids: ['upload'] })))
})

test('没有投稿的独立组直接确认运行，上传终点不伪造权利或投稿授权', async t => {
  const { page } = await render(), calls = fixtures(t)
  await page.openBatch({ id: 'batch', status: 'paused' }, ['upload'])
  assert.equal(page.canStart, true); assert.match(page.confirmLabel, /不投稿/)
  await page.start()
  const start = calls.find(call => call.path.endsWith('/start'))
  assert.equal(start.body.rights_confirmed, false); assert.deepEqual(start.body.group_ids, ['upload'])
  assert.equal(calls.filter(call => call.path === '/api/project-batches' && call.body).length, 0)
})

test('父批次管理的合集子任务去重暴露给历史列表，独立workflow不当成旧顺序批次', async () => {
  const { page } = await render(value => { value.batches = [{ groups: [
    { kind: 'collection', child_id: 'old-child' }, { kind: 'collection', child_id: 'old-child' }, { kind: 'independent', workflow_id: 'workflow' },
  ] }] })
  assert.deepEqual(page.managedReleaseIds, ['old-child'])
})

test('某组暂停只发送该组操作，不启动其他项目或修改发布目标', async t => {
  const { page } = await render(), calls = fixtures(t)
  await page.control({ id: 'batch', status: 'running' }, 'pause', 'collection')
  assert.deepEqual(calls.filter(call => call.body).map(call => ({ path: call.path, body: call.body })), [{ path: '/api/project-batches/batch/pause', body: { group_id: 'collection' } }])
})

test('关闭取消读取，迟到候选不能重新填充方案或继续检查', async t => {
  const { page } = await render(); let release: (value: any) => void = () => {}
  const gate = new Promise(resolve => { release = resolve })
  const calls = fixtures(t, path => path === '/api/project-batches/candidates' ? gate : undefined)
  const pending = page.open(targets); page.close(); release(reply({ items: rows })); await pending
  assert.equal(page.show, false); assert.equal(page.preview, null); assert.equal(page.items.length, 0)
  assert.equal(calls.filter(call => call.path === '/api/project-batches/plan').length, 0)
  assert.equal(calls.filter(call => call.path.includes('/candidates?')).length, 0)
})

test('补齐日期只占用候选阶段，首次预检采用补齐后的原视频日期', async t => {
  const { page } = await render(); let release: (value: any) => void = () => {}
  const gate = new Promise(resolve => { release = resolve })
  const calls = fixtures(t, path => path === '/api/project-batches/candidates' ? gate : undefined)
  const pending = page.open(targets)
  assert.equal(page.loading, true); assert.equal(page.readingDates, true)
  release(reply({ items: rows.map(row => row.project_uid === 'three' ? { ...row, source_date: '2025-01-01' } : row) })); await pending
  assert.deepEqual(calls.find(call => call.path === '/api/project-batches/plan').body.project_uids, ['three', 'one', 'two', 'standalone', 'upload'])
  assert.equal(page.readingDates, false); assert.equal(page.loading, false)
})

test('手动重新检查会重试补齐日期，但保留用户已调整的顺序', async t => {
  const { page } = await render(); let checks = 0
  const refreshed = rows.map(row => ({ ...row, source_date: row.project_uid === 'three' ? '2020-01-01' : row.source_date }))
  const calls = fixtures(t, (path, body) => {
    if (path === '/api/project-batches/candidates' && ++checks === 2) return reply({ items: refreshed })
    if (path === '/api/project-batches/plan' && checks === 2) {
      const next = plan(body.project_uids)
      next.groups = next.groups.map(group => ({ ...group, items: group.items.map(item => refreshed.find(row => row.project_uid === item.project_uid)!) }))
      return reply(next)
    }
  })
  await page.open(targets); await page.move(page.groups[0], 1, -1); await page.inspect()
  assert.equal(checks, 2)
  assert.deepEqual(calls.filter(call => call.path === '/api/project-batches/plan').at(-1).body.project_uids, ['two', 'one', 'standalone', 'three', 'upload'])
  assert.equal(page.items.find((item: any) => item.project_uid === 'three').source_date, '2020-01-01')
})

test('日期补齐请求失败时回退缓存，缓存请求也失败仍然继续方案检查', async t => {
  const { page } = await render()
  const calls = fixtures(t, path => path.includes('/candidates') ? Promise.reject(new Error('日期资料暂时不可用')) : undefined)
  await page.open(targets)
  assert.ok(page.preview); assert.equal(page.error, '')
  assert.equal(calls.filter(call => call.path === '/api/project-batches/candidates').length, 1)
  assert.equal(calls.filter(call => call.path.includes('/candidates?')).length, 1)
  assert.deepEqual(calls.find(call => call.path === '/api/project-batches/plan').body.project_uids, targets.map(target => target.uid))
  assert.equal(page.readingDates, false)
})

test('补齐失败后采用缓存日期完成首次排序，仍可继续确认', async t => {
  const { page } = await render(), calls = fixtures(t, path => path === '/api/project-batches/candidates'
    ? reply({ message: '平台获取暂时失败' }, 503) : undefined)
  await page.open(targets); consent(page)
  assert.equal(page.canStart, true); assert.equal(page.error, '')
  assert.deepEqual(calls.find(call => call.path === '/api/project-batches/plan').body.project_uids, ['one', 'two', 'standalone', 'three', 'upload'])
})

test('日期仍然未知时核对顺序后可运行，缺失日期本身不成为阻断', async t => {
  const { page } = await render()
  const calls = fixtures(t, (path, body) => {
    if (path === '/api/project-batches/candidates') return reply({ items: rows.map(row => ({ ...row, source_date: null })) })
    if (path === '/api/project-batches/plan') {
      const next = plan(body.project_uids)
      return reply({ ...next, groups: next.groups.map(group => ({ ...group, items: group.items.map(item => ({ ...item, source_date: null })) })) })
    }
  })
  await page.open(targets)
  assert.equal(page.canStart, false); assert.deepEqual(page.blockers, [])
  consent(page); assert.equal(page.canStart, true)
  assert.deepEqual(calls.find(call => call.path === '/api/project-batches/plan').body.project_uids, targets.map(target => target.uid))
})

test('已保存批次打开与重新检查都不补齐日期或改变冻结顺序', async t => {
  const { page } = await render(), calls = fixtures(t)
  await page.openBatch({ id: 'batch', status: 'paused' }); await page.inspect()
  assert.equal(calls.filter(call => call.path.includes('/candidates')).length, 0)
  assert.ok(calls.filter(call => call.path.endsWith('/batch/plan')).every(call => Object.keys(call.body).length === 0))
  assert.deepEqual(page.groups[0].items.map((item: any) => item.project_uid), ['one', 'two', 'three'])
})

test('日期提醒仅出现在缺日期的多项目合集，单项合集与独立项目保持中性行内说明', async () => {
  const missing = { project_uid: 'missing', title: '日期缺失的视频', delivery_mode: 'produce' }
  const group = (id: string, kind: string, items: any[]) => ({ id, kind, items, title: id })
  const { html } = await render(page => { page.show = true; page.preview = { fingerprint: 'date-warning', groups: [
    group('multi-missing', 'collection', [missing, rows[0]]),
    group('single-missing', 'collection', [{ ...missing, project_uid: 'single' }]),
    group('standalone-missing', 'independent', [{ ...missing, project_uid: 'alone' }]),
    group('multi-known', 'collection', [rows[2], rows[3]]),
  ] } })
  assert.equal(html.match(/部分原视频发布日期仍无法获取，请核对下方顺序。/g)?.length, 1)
  assert.equal(html.match(/发布日期未获取/g)?.length, 3)
})

test('组内阻塞即使根blockers为空也禁止启动', async t => {
  const { page } = await render(), calls = fixtures(t, path => path === '/api/project-batches/plan' ? reply({ ...plan(), groups: [{ ...plan().groups[0], blockers: ['账号未连接'] }] }) : undefined)
  await page.open(targets); consent(page); await page.start()
  assert.equal(page.canStart, false); assert.deepEqual(page.blockers, ['账号未连接'])
  assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 0)
})

test('恢复检查没有可继续组时不把已完成项目重新纳入确认范围', async t => {
  const { page } = await render(); fixtures(t, path => path.endsWith('/batch/plan') ? reply({ ...plan(), group_ids: [] }) : undefined)
  await page.openBatch({ id: 'batch', status: 'attention' })
  assert.equal(page.groups.length, 0); assert.equal(page.canStart, false); assert.equal(page.publishCount, 0)
})

test('渲染检查页呈现可读分组与顺序按钮，目标不出现临时覆盖控件', async () => {
  const { html } = await render(page => { page.show = true; page.preview = plan(); page.items = rows })
  assert.match(html, /测试账号 · 历史合集 · 中国史/); assert.match(html, /独立执行，不等待其他组/)
  assert.match(html, /上移 合集第二集/); assert.match(html, /上传后等待确认投稿/); assert.match(html, /仅制作成片/)
  assert.doesNotMatch(html, /选择发布账号|选择目标合集|本次执行终点/)
})

test('重复读取合并请求，写入前迟到列表不覆盖操作结果', async t => {
  const { page } = await render(); let release: (value: any) => void = () => {}, reads = 0
  const gate = new Promise(resolve => { release = resolve })
  fixtures(t, (path, body) => path === '/api/project-batches' && !body ? (++reads === 1 ? gate : reply({ batches: [{ id: 'batch', status: 'paused', groups: plan().groups }] })) : undefined)
  const old = page.load(true), duplicate = page.load(); assert.equal(reads, 1)
  await page.control({ id: 'batch', status: 'running' }, 'pause', 'collection')
  release(reply({ batches: [{ id: 'batch', status: 'running', groups: plan().groups }] })); await Promise.all([old, duplicate])
  for (let i = 0; i < 8; i++) await nextTick()
  assert.equal(reads, 2); assert.equal(page.batches[0].status, 'paused'); assert.equal(page.listLoading, false)
})

const failedModelError = (account = '2126017182') => `split 未完成：全部启用的 API 配置均失败 —— 【智谱GLM】：LLM 请求失败 HTTP 429 —— 请求过于频繁 (限流)（可能是并发太高或配额用完）；【火山方舟-agent-plan】：服务商返回错误：InvalidSubscription Your account (${account}) does not have a valid AgentPlan subscription. Please visit https://example.com/${account}`
const failedGroup = (id: string, error = failedModelError()) => ({ id, kind: 'collection', title: `${id}合集`, status: 'attention', phase: '批次已暂停，等待处理', error,
  items: [{ project_uid: id, title: `${id}视频`, status: 'failed', phase: '需要处理后重新确认' }] })
const withoutDetails = (html: string) => html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '')

test('全部中断时共同故障只呈现一次，原始响应默认折叠，恢复入口保留两种作用域', async () => {
  const groups = [failedGroup('one'), failedGroup('two', failedModelError('9999999999'))]
  const { html } = await render(page => { page.batches = [{ id: 'batch', status: 'attention', error: groups.map(group => group.error).join('；'), groups }] })
  const visible = withoutDetails(html)
  assert.match(visible, /2 组待处理/)
  assert.equal(visible.match(/智谱GLM：上次请求受限，需核对限流或配额/g)?.length, 1)
  assert.equal(visible.match(/火山方舟-agent-plan：上次订阅无效、未生效或已过期/g)?.length, 1)
  assert.equal(visible.match(/上次在语义整理中断/g)?.length, 2)
  assert.equal(visible.match(/检查并继续本组…/g)?.length, 2)
  assert.equal(visible.match(/检查待继续的组…/g)?.length, 1)
  assert.match(visible, /settings\?section=models/)
  assert.doesNotMatch(visible, /2126017182|9999999999|https:\/\/example|InvalidSubscription|批次已暂停，等待处理|本组需处理时，其他组继续执行/)
  assert.equal(html.match(/<details\b/g)?.length, 2)
  assert.doesNotMatch(html, /<details\b[^>]*\bopen/)
  assert.match(html, /2126017182/); assert.doesNotMatch(html, /查看批次错误详情/)
})

test('部分失败时运行组没有故障提示，批次继续执行的范围清楚', async () => {
  const groups = [failedGroup('failed'), { ...failedGroup('working'), status: 'running', phase: '语音转文字' }]
  const { html } = await render(page => { page.batches = [{ id: 'batch', status: 'running', error: groups[0].error, groups }] })
  const visible = withoutDetails(html)
  assert.match(visible, /1 组执行中/); assert.match(visible, /1 组待处理/)
  assert.match(visible, /待处理的组已中断，其余 1 组继续执行/)
  assert.equal(visible.match(/上次在语义整理中断/g)?.length, 1)
  assert.equal(html.match(/查看上次中断详情/g)?.length, 1)
  const working = visible.split('data-group="working"')[1]!
  assert.match(working, /语音转文字/); assert.match(working, /暂停本组/)
  assert.doesNotMatch(working.split('</div><footer')[0]!, /模型服务需要处理|上次在语义整理中断/)
})

test('未知故障与父级独有回执问题仍可追查，不能被组故障去重吞掉', async () => {
  const groups = [failedGroup('one', 'compose 未完成：encoder failed at https://example.com/private')]
  const { html } = await render(page => { page.batches = [{ id: 'batch', status: 'attention',
    error: `${groups[0].error}；投稿回执未确认，请核对平台稿件`, groups }] })
  const visible = withoutDetails(html)
  assert.match(visible, /发布结果待核验，请先到项目发布页核对平台稿件，避免重复投稿/); assert.match(visible, /上次在合成成片中断/)
  assert.doesNotMatch(visible, /encoder failed|example.com|模型服务需要处理/)
  assert.match(html, /查看批次错误详情/); assert.match(html, /投稿回执未确认，请核对平台稿件/)
  assert.match(html, /encoder failed/)
})

test('组内未知发布回执的关键核对要求直接可见，完整错误仍在详情中', async () => {
  const { html } = await render(page => { page.batches = [{ id: 'batch', status: 'attention', groups: [
    failedGroup('publication', '投稿回执待核对，已暂停；不会重复投稿。平台地址 https://example.com/1234567890'),
  ] }] })
  const visible = withoutDetails(html)
  assert.match(visible, /发布结果待核验/)
  assert.match(visible, /请先到项目发布页核对平台稿件，避免重复投稿/)
  assert.doesNotMatch(visible, /https:\/\/example|1234567890|模型服务需要处理/)
  assert.match(html, /投稿回执待核对，已暂停/)
})

test('真实批次展示分支挂载：旧模型错误明确为上次记录，单项目恢复不冒称修复或改变批次门禁', async () => {
  const source = await readFile(new URL('../src/components/ProjectBatchPanel.vue', import.meta.url), 'utf8')
  const template = parse(source).descriptor.template!.content
  const aside = template.match(/<aside v-if="issues.length"[\s\S]*?<\/aside>/)?.[0]
  const start = template.indexOf('<div v-for="{group,failure,phase} in displayedGroups"')
  const end = template.indexOf('<footer class="batch-record-actions"', start)
  assert.ok(aside && start >= 0 && end > start, '挂载真实提示、分组、详情和操作模板，不复制文字')
  const renderBranch = new Function('Vue', compile(`<section>${aside}${template.slice(start,end)}</section>`, { mode:'function',prefixIdentifiers:true }).code)(Vue)
  type Host = { type:string; text:string; props:Record<string,any>; children:Host[]; parent:Host|null }
  const node = (type:string,text=''):Host => ({type,text,props:{},children:[],parent:null})
  const remove = (value:Host) => { if(value.parent)value.parent.children.splice(value.parent.children.indexOf(value),1);value.parent=null }
  const renderer = Vue.createRenderer<Host,Host>({createElement:node,createText:text=>node('#text',text),createComment:()=>node('#comment'),remove,
    insert(value,parent,anchor=null){remove(value);const index=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(index<0?parent.children.length:index,0,value);value.parent=parent},
    setText(value,text){value.text=text},setElementText(value,text){value.text=text;value.children=[]},patchProp(value,key,_old,next){value.props[key]=next},
    parentNode:value=>value.parent,nextSibling:value=>value.parent?.children[value.parent.children.indexOf(value)+1]||null})
  const batch = Vue.reactive({id:'fixture-batch',status:'attention',groups:[failedGroup('fixture', 'comments 未完成：LLM 请求失败 —— 【DeepSeek】：旧模型响应失败')],
    current_workflow:{id:'same-workflow',status:'paused',phase:'合成成片'}})
  const calls:any[]=[]
  const Span=Vue.defineComponent({setup:(_props,{slots})=>()=>Vue.h('span',slots.default?.())})
  const Button=Vue.defineComponent({props:['disabled','loading'],setup:(props,{attrs,slots})=>()=>Vue.h('button',{...attrs,disabled:props.disabled},slots.default?.())})
  const app=renderer.createApp({components:{NButton:Button,NTag:Span,RouterLink:Span,ReleaseCurrentProgress:Span},setup:()=>({
    ...batchDisplay,batch,writing:false,runFeedback:null,
    issues:Vue.computed(()=>batchDisplay.projectBatchIssues(batch)),
    displayedGroups:Vue.computed(()=>batch.groups.map(group=>({group,failure:batchDisplay.projectBatchGroupFailure(group),phase:batchDisplay.projectBatchGroupPhase(group)}))),
    pendingGroupFeedback:()=>null,groupRunFeedback:()=>'',controlBusy:()=>false,
    openBatch:(_batch:any,ids:string[])=>calls.push(ids),control:()=>{throw new Error('测试禁止生产操作')},
  }),render:renderBranch})
  const root=node('root');app.mount(root)
  const text=(value:Host,details=true):string=>value.type==='#comment'||!details&&value.type==='details'?'':value.text+value.children.map(child=>text(child,details)).join('')
  const all=()=>{const nodes:Host[]=[];const visit=(value:Host)=>{nodes.push(value);value.children.forEach(visit)};visit(root);return nodes}
  try {
    assert.match(text(root,false),/上次模型请求中断/);assert.match(text(root,false),/DeepSeek：上次请求失败/)
    assert.match(text(root,false),/上次在上屏评论中断/);assert.doesNotMatch(text(root,false),/模型服务需要处理|已修复|已恢复正常|旧模型响应失败/)
    assert.match(text(root),/旧模型响应失败/)
    assert.ok(all().filter(value=>value.type==='details').every(value=>!value.props.open))
    const resume=all().find(value=>value.type==='button'&&text(value)==='检查并继续本组…')!
    assert.ok(resume);assert.equal(resume.props.disabled,false)
    resume.props.onClick();assert.deepEqual(calls,[['fixture']])
    batch.current_workflow.status='running';await Vue.nextTick()
    assert.equal(batch.status,'attention');assert.equal(batch.groups[0].status,'attention')
    assert.match(text(root,false),/上次模型请求中断/);assert.doesNotMatch(text(root,false),/模型服务需要处理|已修复/)
    batch.groups[0].status='running';batch.groups[0].phase='合成成片';await Vue.nextTick()
    assert.doesNotMatch(text(root,false),/上次模型请求中断|上次在上屏评论中断/)
    assert.match(text(root,false),/合成成片/);assert.match(text(root,false),/暂停本组/)
    assert.match(batch.groups[0].error,/旧模型响应失败/,'呈现变化不能删除持久中断记录')
  } finally {app.unmount()}
})

test('从故障组检查再切到全批次检查仍使用原有作用域，不自动启动或发起模型请求', async t => {
  const { page } = await render(), calls = fixtures(t)
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload'])
  assert.deepEqual(calls.find(call => call.path.endsWith('/batch/plan')).body, { group_ids: ['upload'] })
  page.close(); await page.openBatch({ id: 'batch', status: 'attention' })
  assert.deepEqual(calls.filter(call => call.path.endsWith('/batch/plan')).at(-1).body, {})
  assert.ok(calls.every(call => !call.path.endsWith('/start') && !call.path.includes('test_llm')))
})

test('点击继续立即显示核对阶段并防重入，提交和受理按真实回执切换', async t => {
  const { page, feedback } = await render()
  let checks = 0, releaseCheck: (value: any) => void = () => {}, releaseStart: (value: any) => void = () => {}
  const checkGate = new Promise(resolve => { releaseCheck = resolve }), startGate = new Promise(resolve => { releaseStart = resolve })
  const calls = fixtures(t, path => {
    if (path.endsWith('/batch/plan') && ++checks === 2) return checkGate
    if (path.endsWith('/start')) return startGate
  })
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload'])
  const checked = page.preview
  const pending = page.start()
  assert.equal(page.writing, true); assert.equal(page.runFeedback.stage, 'checking')
  assert.match(page.runFeedback.message, /核对已有结果与执行条件/)
  assert.match(page.confirmLabel, /正在核对/); assert.equal(page.canStart, false)
  await page.start(); assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 0)
  releaseCheck(reply(checked))
  for (let i = 0; i < 8; i++) await nextTick()
  assert.equal(page.runFeedback.stage, 'submitting'); assert.match(page.confirmLabel, /正在提交/)
  assert.deepEqual(page.runFeedback.projectUids, ['upload']); assert.deepEqual(page.runFeedback.groupIds, ['upload'])
  releaseStart(reply({ id: 'batch', status: 'running', groups: [{ ...plan().groups[2], id: 'upload', status: 'running', feedback: 'preparing', phase: '正在核对已有结果与执行条件' }] }))
  await pending
  assert.ok(feedback.some(row => row.stage === 'accepted')); assert.equal(page.show, false)
  assert.equal(calls.filter(call => call.path.endsWith('/start')).length, 1)
  assert.deepEqual(calls.find(call => call.path.endsWith('/start')).body.group_ids, ['upload'])
})

test('运行核对中的抽屉显示可读状态，准备态分组保留真实步骤而不伪造执行百分比', async () => {
  const { html } = await render(page => {
    page.show = true; page.writing = true; page.preview = plan(undefined, ['upload'])
    page.runFeedback = { id: 'feedback', batchId: 'batch', groupIds: ['upload'], projectUids: ['upload'], stage: 'checking', message: '正在核对已有结果与执行条件…' }
    page.batches = [{ id: 'batch', status: 'running', groups: [{ ...plan().groups[2], id: 'upload', status: 'running', feedback: 'preparing', phase: '正在核对已有结果与执行条件…' }] }]
  })
  assert.match(html, /role="status" aria-live="polite"/)
  assert.match(html, /正在核对已有结果与执行条件/); assert.match(html, /核对中/)
  assert.doesNotMatch(html, /aria-valuenow="[1-9]/)
})

test('本轮检查只替换选中组旧失败展示，其他组与父级独有诊断仍可见', async () => {
  const { html, page } = await render(page => {
    page.writing = true
    page.runFeedback = { id: 'pending', batchId: 'batch', groupIds: ['upload'], projectUids: ['upload'], stage: 'submitting', message: '正在提交恢复请求…' }
    page.batches = [{ id: 'batch', status: 'attention', error: '父级独有平台回执待核对', groups: [
      { id: 'upload', kind: 'independent', status: 'attention', error: '本轮以前的错误甲', phase: '旧失败阶段甲', items: [{ project_uid: 'upload', title: '正在恢复项目', status: 'attention', error: '本轮以前的错误甲' }] },
      { id: 'other', kind: 'independent', status: 'attention', error: '另一个组的错误乙', phase: '旧失败阶段乙', items: [{ project_uid: 'other', title: '未选择项目', status: 'attention', error: '另一个组的错误乙' }] },
    ] }]
  })
  const selected = html.split('data-group="upload"')[1]!.split('data-group="other"')[0]!
  assert.match(selected, /正在提交恢复请求/)
  assert.doesNotMatch(selected, /本轮以前的错误甲|旧失败阶段甲|检查并继续本组|暂停本组|停止本组/)
  assert.match(html, /另一个组的错误乙|旧失败阶段乙/)
  assert.match(html, /父级独有平台回执待核对/)
  assert.equal(page.batches[0].groups[0].status, 'attention', '展示准备态不能篡改后台状态')
  assert.equal(page.batches[0].groups[0].error, '本轮以前的错误甲')
})

test('同次继续中的一组已交接时立即展示真实步骤，慢核对组仍保留准备反馈', async () => {
  const { html } = await render(page => {
    page.runFeedback = { id: 'feedback', batchId: 'batch', groupIds: ['working', 'preparing'], projectUids: ['working', 'preparing'], stage: 'accepted', message: '请求已接收，正在准备执行…' }
    page.batches = [{ id: 'batch', status: 'running', groups: [
      { id: 'working', kind: 'independent', status: 'running', phase: '语义整理', items: [{ project_uid: 'working', title: '已启动项目', status: 'running', phase: '语义整理' }] },
      { id: 'preparing', kind: 'independent', status: 'running', feedback: 'preparing', phase: '正在核对已有结果与执行条件', items: [{ project_uid: 'preparing', title: '仍在核对项目', status: 'queued', feedback: 'preparing', phase: '正在核对已有结果与执行条件' }] },
    ] }]
  })
  const working = html.split('data-group="working"')[1]!.split('data-group="preparing"')[0]!
  assert.match(working, /语义整理/); assert.doesNotMatch(working, /请求已接收，正在准备执行/)
  assert.match(html.split('data-group="preparing"')[1]!, /请求已接收，正在准备执行/)
})

test('启动回执未知不能显示已接收，重新检查仍保留原启动request_id与分组范围', async t => {
  const { page, feedback } = await render(); let attempts = 0
  const calls = fixtures(t, path => path.endsWith('/start') && ++attempts === 1 ? Promise.reject(new Error('Failed to fetch')) : undefined)
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload']); await page.start()
  assert.equal(page.runFeedback.stage, 'unknown'); assert.equal(page.show, true)
  assert.ok(!feedback.some(row => row.stage === 'accepted'))
  const first = calls.find(call => call.path.endsWith('/start')).body
  await page.inspect(); await page.start()
  const retry = calls.filter(call => call.path.endsWith('/start')).at(-1).body
  assert.equal(first.request_id, retry.request_id); assert.deepEqual(retry.group_ids, ['upload'])
  assert.equal(retry.rights_confirmed, false)
})

test('已受理后再次中断并恢复同方案使用新的启动标识，不能复用已消费回执', async t => {
  const { page, feedback } = await render(), calls = fixtures(t)
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload']); await page.start()
  assert.ok(feedback.some(row => row.stage === 'accepted'))
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload']); await page.start()
  const starts = calls.filter(call => call.path.endsWith('/start'))
  assert.equal(starts.length, 2)
  assert.equal(starts[0].body.fingerprint, starts[1].body.fingerprint)
  assert.deepEqual(starts[0].body.group_ids, starts[1].body.group_ids)
  assert.notEqual(starts[0].body.request_id, starts[1].body.request_id)
})

test('方案变更与确定拒绝清楚停止等待，服务端异常仍标记回执未知', async t => {
  for (const status of [409, 500]) {
    const { page, feedback } = await render()
    fixtures(t, path => path.endsWith('/start') ? reply({ message: status === 409 ? '方案需要重新检查' : '服务返回异常' }, status) : undefined)
    await page.openBatch({ id: 'batch', status: 'attention' }, ['upload']); await page.start()
    assert.equal(page.runFeedback.stage, status === 409 ? 'blocked' : 'unknown')
    assert.ok(!feedback.some(row => row.stage === 'accepted'))
  }
})

test('反馈只覆盖实际确认组的未完成项目，已完成项目不进入卡片等待范围', async t => {
  const { page, feedback } = await render()
  fixtures(t, path => path.endsWith('/batch/plan') ? reply({ ...plan(undefined, ['collection']), groups: plan().groups.map(group => ({ ...group,
    items: group.items.map(item => item.project_uid === 'one' ? { ...item, status: 'done' } : item) })) }) : undefined)
  await page.openBatch({ id: 'batch', status: 'attention' }, ['collection']); consent(page); await page.start()
  const checking = feedback.find(row => row.stage === 'checking')
  assert.deepEqual(checking.groupIds, ['collection']); assert.deepEqual(checking.projectUids, ['two', 'three'])
  assert.ok(!checking.projectUids.includes('upload')); assert.ok(!checking.projectUids.includes('one'))
})

test('受理前迟到列表与旧更新时间快照不能覆盖新回执', async t => {
  const { page } = await render(); let release: (value: any) => void = () => {}, reads = 0
  const gate = new Promise(resolve => { release = resolve })
  const accepted = { id: 'batch', status: 'running', updated_at: '2026-10-20T12:00:02', groups: [
    { ...plan().groups[2], id: 'upload', status: 'running', feedback: 'preparing', phase: '正在核对已有结果与执行条件' }] }
  fixtures(t, (path, body) => {
    if (path === '/api/project-batches' && !body) return ++reads === 1 ? gate : reply({ batches: [{ ...accepted, status: 'attention', updated_at: '2026-10-20T12:00:01' }] })
    if (path.endsWith('/start')) return reply(accepted)
  })
  const old = page.load(true)
  await page.openBatch({ id: 'batch', status: 'attention' }, ['upload']); await page.start()
  release(reply({ batches: [{ id: 'batch', status: 'attention', groups: plan().groups }] })); await old
  for (let i = 0; i < 10; i++) await nextTick()
  assert.equal(page.batches[0].status, 'running'); assert.equal(page.runFeedback.stage, 'accepted')
  assert.equal(page.batches[0].groups[0].feedback, 'preparing')
})
