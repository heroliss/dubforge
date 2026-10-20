// 实际组件逻辑 + 隔离请求：规划可读，制作启动只在确认后发送。
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
import { defaultDubbingRunPolicy } from '../src/utils/dubbingRunPolicy.ts'

let server: ViteDevServer, Panel: any, Projects: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/ProjectStepRun.vue')).default
  Projects = (await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  mock.method(globalThis, 'fetch', () => { throw new Error('禁止未声明的真实网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })
const targets = [{ uid: 'one', title: '列表旧标题一' }, { uid: 'two', title: '列表旧标题二' }]
const reply = (data: any, ok = true, status = 200) => ({ ok, status, json: async () => data })
const step = (key: string, changes: any = {}) => ({ key, selected: true, status: 'pending', fresh: false, ...changes })
const detail = (uid: string, changes: any = {}) => ({ uid, title: `最新标题${uid}`, dir_exists: true,
  running_step: null, chain: null, workflow_status: null, steps: [step('download'), step('translate')], ...changes })
const plan = (changes: any = {}) => ({ targets: ['download', 'translate'], steps: ['download', 'translate'],
  added: [], requirements: [], blockers: [], labels: { download: '获取视频', translate: '翻译' },
  effects: [], ...changes })
async function render(t: any, component = Panel, seed: (page: any) => void = () => {}, started?: () => any) {
  let page: any
  const emitted: any[] = [], bindings: any[] = [], pinia = createPinia()
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: { render: () => null } }] })
  await router.push('/')
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(NDialogProvider, null, { default: () => h(component, {
      onStarted: (value: any) => { emitted.push(value); return started?.() },
    }) }),
  }) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ProjectTemplateBinding') bindings.push({ uids: [...this.$props.uids], disabled: this.$props.disabled })
    if (this.$options.__name === (component === Panel ? 'ProjectStepRun' : 'ProjectsView')) {
      page = this.$.setupState; seed(page)
    }
  } })
  const context: any = {}
  const html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
  t.after(() => { page.close?.(); disposePinia(pinia) })
  return { page, html, emitted, bindings }
}
function fixtures(t: any, adjust: (uid: string, path: string, body: any) => any = () => undefined) {
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    const match = String(url).match(/^\/api\/projects(?:\/([^/?]+))?(.*)$/)
    assert.ok(match, `意外请求：${url}`)
    const uid = match[1] || '', path = match[2] || '', body = options?.body && JSON.parse(String(options.body))
    const request = { url: String(url), uid, path, body, method: options?.method || 'GET', signal: options?.signal }
    calls.push(request)
    const custom = adjust(uid, path, body)
    if (custom !== undefined) return custom
    if (uid === 'run_many') return reply({ ok: true, started: body.uids.length,
      results: body.uids.map((uid: string) => ({ uid, started: '获取视频' })) })
    if (!path) return reply(detail(uid))
    if (path === '/run_plan') return reply(plan({ targets: body.steps, steps: body.steps }))
    throw new Error(`未声明请求：${url}`)
  })
  return calls
}
const starts = (calls: any[]) => calls.filter(call => call.uid === 'run_many')

test('打开只读取最新项目与计划，保存勾选按制作顺序冻结，排除dubtask及未知步骤', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, (uid, path) => !path && uid !== 'run_many' ? reply(detail(uid, { steps: [
    step('assemble'), step('dubtask'), step('translate'), step('unrecognized'), step('download'), step('tts', { selected: false }),
  ] })) : undefined)
  await page.open([targets[0]], '单项目')
  assert.deepEqual(calls.map(call => [call.method, call.uid, call.path]), [['GET', 'one', ''], ['POST', 'one', '/run_plan']])
  assert.deepEqual(calls[1].body, { steps: ['download', 'translate', 'assemble'], include_dependencies: false, dubbing:defaultDubbingRunPolicy() })
  assert.deepEqual(page.items[0].steps, ['download', 'translate', 'assemble'])
  assert.equal(page.items[0].title, '最新标题one')
  assert.equal(page.ready.length, 1); assert.equal(page.canConfirm, true)
  assert.equal(starts(calls).length, 0, '打开和预检不启动制作')
})

test('显式全部重做使最新项目可执行，单独冻结每项目策略与计划指纹',async t=>{
  const {page}=await render(t)
  const calls=fixtures(t,(uid,path,body)=>!path && uid!=='run_many'
    ? reply(detail(uid,{steps:[step('tts',{status:'done',fresh:true}),step('assemble',{status:'done',fresh:true})]}))
    : path==='/run_plan' ? reply(plan({steps:['tts','assemble'],fingerprint:'fp-'+uid,
      dubbing:{policy:body.dubbing,generate:body.dubbing.mode==='all'?8:0,reuse:body.dubbing.mode==='all'?0:8}})) : undefined)
  await page.open(targets);assert.equal(page.canConfirm,false)
  const policy={...defaultDubbingRunPolicy(),mode:'all',target:'slot',slot:3}
  page.onDubbingChange(policy);assert.equal(page.canConfirm,false)
  for(let i=0;i<40&&page.busy;i++)await Promise.resolve()
  assert.equal(page.canConfirm,true);assert.ok(page.items.every((item:any)=>!item.allFresh))
  await page.confirmStart()
  const body=starts(calls)[0].body
  assert.deepEqual(body.dubbing_by_uid,{one:policy,two:policy})
  assert.deepEqual(body.plan_fingerprints_by_uid,{one:'fp-one',two:'fp-two'})
})

test('无勾选与全部最新明确不可确认，不产生制作启动请求', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, (uid, path) => !path && uid !== 'run_many' ? reply(detail(uid, { steps: uid === 'one'
    ? [step('download', { selected: false }), step('dubtask')]
    : [step('download', { status: 'done', fresh: true }), step('translate', { status: 'done', fresh: true })] })) : undefined)
  await page.open(targets)
  assert.equal(page.ready.length, 0); assert.equal(page.canConfirm, false)
  assert.match(page.items[0].blockers.join('；'), /勾选/)
  assert.equal(page.items[1].allFresh, true)
  await page.confirmStart(); assert.equal(starts(calls).length, 0)
  assert.ok(calls.every(call => call.uid !== 'one' || call.path !== '/run_plan'))
})

test('运行、排队、制作链和完整流程忙或目录缺失项目不进入运行计划', async t => {
  const { page } = await render(t)
  const values: any = {
    running: { steps: [step('download', { status: 'running' })] },
    queued: { steps: [step('download', { status: 'queued' })] },
    chain: { chain: { state: 'active', remaining: ['download'] } },
    workflowQueued: { workflow_status: 'queued' }, workflowRunning: { workflow_status: 'running' },
    workflowStopping: { workflow_status: 'stopping' }, missing: { dir_exists: false },
    releaseWaiting: { release_batch: { id:'batch',status:'running',position:2,total:3 } },
  }
  const calls = fixtures(t, (uid, path) => !path && values[uid] ? reply(detail(uid, values[uid])) : undefined)
  await page.open(Object.keys(values).map(uid => ({ uid, title: uid })))
  assert.equal(page.ready.length, 0); assert.equal(page.canConfirm, false)
  assert.ok(page.items.every((item: any) => item.blockers.length > 0))
  assert.ok(calls.every(call => call.path !== '/run_plan'))
  await page.confirmStart(); assert.equal(starts(calls).length, 0)
})

test('单项依赖受阻不妨碍其他项目，取消勾选只缩小确认清单', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, (uid, path) => uid === 'two' && path === '/run_plan' ? reply(plan({ blockers: [
    { label: '语义整理', name: 'split.txt', reason: '缺少整理结果', state: 'missing' },
  ] })) : undefined)
  await page.open(targets)
  assert.deepEqual(page.ready.map((item: any) => item.uid), ['one'])
  assert.match(page.items[1].blockers.join('；'), /缺少整理结果/)
  page.items[0].selected = false
  assert.equal(page.canConfirm, false)
  await page.confirmStart(); assert.equal(starts(calls).length, 0)
  page.items[0].selected = true; await page.confirmStart()
  assert.deepEqual(starts(calls)[0].body, { uids: ['one'], steps_by_uid: { one: ['download', 'translate'] },
    dubbing_by_uid:{one:defaultDubbingRunPolicy()},plan_fingerprints_by_uid:{} })
})

test('依赖补齐是显式重检，新增步骤和影响来自实际计划而非静默扩大选择', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, (_uid, path, body) => path === '/run_plan' ? reply(body.include_dependencies
    ? plan({ steps: ['transcribe', 'split', 'translate'], added: ['transcribe', 'split'],
      effects: ['会生成已保存版本，评论按自动准备设置处理'] })
    : plan({ steps: ['translate'], targets: ['translate'], blockers: [{ name: 'split.txt', label: '语义整理', reason: '需更新' }] }))
    : path === '' ? reply(detail('one', { steps: [step('translate')] })) : undefined)
  await page.open([targets[0]])
  assert.equal(page.canConfirm, false)
  page.includeDependencies = true; await page.inspect()
  assert.equal(page.canConfirm, true)
  assert.deepEqual(page.items[0].plan.added, ['transcribe', 'split'])
  assert.match(page.items[0].plan.effects.join('；'), /自动准备/)
  assert.deepEqual(calls.filter(call => call.path === '/run_plan').map(call => call.body.include_dependencies), [false, true])
  assert.deepEqual(page.items[0].steps, ['translate'], '补齐计划不改项目保存的勾选步骤')
  await page.confirmStart()
  assert.deepEqual(starts(calls)[0].body.steps_by_uid.one, ['transcribe', 'split', 'translate'])
})

test('原勾选步骤最新但确有新增依赖时仍展示可执行计划', async t => {
  const { page } = await render(t)
  fixtures(t, (_uid, path) => !path ? reply(detail('one', { steps: [step('translate', { status: 'done', fresh: true })] }))
    : path === '/run_plan' ? reply(plan({ targets: ['translate'], steps: ['split', 'translate'], added: ['split'] })) : undefined)
  page.includeDependencies = true; await page.open([targets[0]])
  assert.equal(page.items[0].allFresh, false)
  assert.equal(page.ready.length, 1)
})

test('确认双击仅发送一次，发送快照不随抽屉草稿或目标数组变化', async t => {
  const { page } = await render(t), input = targets.map(item => ({ ...item }))
  let release: any
  const calls = fixtures(t, (uid) => uid === 'run_many' ? new Promise(done => { release = () => done(reply({
    ok: true, started: 2, results: [{ uid: 'one', started: '获取视频' }, { uid: 'two', started: '获取视频' }],
  })) }) : undefined)
  await page.open(input)
  const pending = page.confirmStart(); await page.confirmStart()
  for (let i = 0; i < 20 && !starts(calls).length; i++) await Promise.resolve()
  assert.equal(page.busy, true); assert.equal(starts(calls).length, 1)
  page.items[0].plan.steps.push('assemble'); page.items[1].selected = false; input.push({ uid: 'hidden', title: '后来加入' })
  assert.deepEqual(starts(calls)[0].body, { uids: ['one', 'two'], steps_by_uid: {
    one: ['download', 'translate'], two: ['download', 'translate'],
  },dubbing_by_uid:{one:defaultDubbingRunPolicy(),two:defaultDubbingRunPolicy()},plan_fingerprints_by_uid:{} })
  release(); await pending
  assert.equal(page.busy, false)
  await page.confirmStart(); assert.equal(starts(calls).length, 1)
})

test('归档项目保持可查看但不请求制作计划，活跃项目仍可确认', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, (uid, path) => uid === 'one' && !path ? reply(detail(uid, { archived: true })) : undefined)
  await page.open(targets)
  assert.deepEqual(page.ready.map((item: any) => item.uid), ['two'])
  assert.match(page.items[0].blockers.join('；'), /已归档.*恢复/)
  assert.ok(calls.every(call => call.uid !== 'one' || call.path !== '/run_plan'))
  await page.confirmStart()
  assert.deepEqual(starts(calls)[0].body.uids, ['two'])
})

test('旧制作抽屉提交前重读归档状态，只启动仍活跃的项目', async t => {
  const { page } = await render(t)
  let archived = false
  const calls = fixtures(t, (uid, path) => uid === 'one' && !path ? reply(detail(uid, { archived })) : undefined)
  await page.open(targets); assert.equal(page.ready.length, 2)
  archived = true; await page.confirmStart()
  assert.deepEqual(starts(calls)[0].body.uids, ['two'])
  assert.equal(page.items[0].resultKind, 'skipped'); assert.match(page.items[0].result, /已归档.*恢复/)
  assert.equal(page.items[1].resultKind, 'started')
})

test('部分项目启动而另一项跳过，逐项回执保留，不自动再次发送成功或跳过项', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, uid => uid === 'run_many' ? reply({ ok: true, started: 1,
    results: [{ uid: 'one', started: '获取视频' }, { uid: 'two', skipped: '另一个操作已经启动该项目' }] }) : undefined)
  await page.open(targets); await page.confirmStart()
  assert.equal(page.items.length, 2)
  assert.equal(page.items[0].resultKind, 'started'); assert.equal(page.items[0].submitted, true)
  assert.equal(page.items[1].resultKind, 'skipped'); assert.match(page.items[1].result, /已经启动/)
  await page.confirmStart(); assert.equal(starts(calls).length, 1)
})

test('网络回执未知不能直接重发相同启动命令，须重新检查当前状态', async t => {
  const { page } = await render(t)
  const calls = fixtures(t, uid => uid === 'run_many' ? Promise.reject(new Error('回执丢失')) : undefined)
  await page.open(targets); await page.confirmStart()
  assert.ok(page.items.every((item: any) => item.resultKind === 'unknown'))
  assert.equal(page.canConfirm, false)
  await page.confirmStart(); assert.equal(starts(calls).length, 1)
  assert.ok(page.items.every((item: any) => /重新|状态|不确定|未确认/.test(item.result)))
})

test('读取单项失败保留原因与其余计划，慢刷新不拖住已返回的启动回执', async t => {
  const { page, emitted } = await render(t, Panel, () => {}, () => new Promise(() => {}))
  const calls = fixtures(t, (uid, path) => uid === 'two' && !path ? reply({ message: '项目目录读取失败' }, false, 400) : undefined)
  await page.open(targets)
  assert.equal(page.ready.length, 1); assert.match(page.items[1].blockers.join('；'), /读取失败/)
  await page.confirmStart()
  assert.equal(page.busy, false); assert.equal(page.items[0].resultKind, 'started')
  assert.equal(emitted.length, 1); assert.equal(starts(calls).length, 1)
})

test('关闭预检立即Abort，迟到旧响应不能覆盖重新打开的新目标', async t => {
  const { page } = await render(t); let release: any, signal: any
  const calls = fixtures(t, (uid, path) => uid === 'one' && !path ? new Promise(done => {
    signal = calls.at(-1).signal; release = () => done(reply(detail('one')))
  }) : undefined)
  const old = page.open([targets[0]], '旧范围')
  assert.equal(page.checking, true)
  page.close(); assert.equal(signal.aborted, true); assert.equal(page.show, false)
  await page.open([targets[1]], '新范围')
  assert.deepEqual(page.ready.map((item: any) => item.uid), ['two'])
  release(); await old
  assert.equal(page.show, true); assert.equal(page.checking, false)
  assert.deepEqual(page.items.map((item: any) => item.uid), ['two'])
  assert.equal(starts(calls).length, 0)
})

test('重新检查依赖模式取消旧计划，迟到旧计划不能撤销新增依赖', async t => {
  const { page } = await render(t); let release: any, signal: any
  const calls = fixtures(t, (_uid, path, body) => path === '/run_plan' && !body.include_dependencies
    ? new Promise(done => { signal = calls.at(-1).signal; release = () => done(reply(plan())) })
    : path === '/run_plan' ? reply(plan({ steps: ['split', 'translate'], added: ['split'] })) : undefined)
  const old = page.open([targets[0]])
  for (let i = 0; i < 12 && !release; i++) await Promise.resolve()
  assert.ok(release)
  page.includeDependencies = true; await page.inspect()
  assert.equal(signal.aborted, true)
  assert.deepEqual(page.items[0].plan.steps, ['split', 'translate'])
  release(); await old
  assert.equal(page.checking, false); assert.equal(page.canConfirm, true)
  assert.deepEqual(page.items[0].plan.added, ['split'])
  assert.equal(starts(calls).length, 0)
})

test('单项目走恢复检查、多项目走统一批次，待制作范围含失败项并保留隐藏选择', async t => {
  const opened: any[] = [], batches: any[] = []
  const { page, html } = await render(t, Projects, page => {
    page.projects = [
      { uid: 'one', title: '当前创建项目', status: 'created', dir_exists: true, steps: [step('download')] },
      { uid: 'two', title: '当前失败项目', status: 'failed', dir_exists: true, steps: [step('translate')] },
      { uid: 'hidden', title: '筛选隐藏的选择', status: 'created', dir_exists: true, steps: [step('download')] },
    ]
    page.searchText = '当前'; page.sel = new Set(['hidden'])
  })
  page.stepRun = { open: (items: any[], scope: string) => { opened.push({ items, scope }) } }
  page.projectBatchPanel = { open: (items: any[]) => { batches.push(items) } }
  page.runSelected()
  assert.deepEqual(opened[0].items, [{ uid: 'hidden', title: '筛选隐藏的选择' }])
  assert.match(opened[0].scope, /所选.*1/)
  page.runAllPending()
  assert.deepEqual(new Set(batches[0].map((item: any) => item.uid)), new Set(['one', 'two']))
  assert.equal(opened.length, 1, '多项目不再逐项使用旧运行抽屉')
  page.runProject(page.projects[0])
  assert.deepEqual(opened[1].items, [{ uid: 'one', title: '当前创建项目' }])
  page.projects[0].title = '稍后改变'
  assert.equal(opened[1].items[0].title, '当前创建项目', '确认抽屉持有点击时范围快照')
  assert.equal(batches[0].find((item: any) => item.uid === 'one').title, '当前创建项目', '批次入口也持有明确选择快照')
  assert.match(html, /运行项目…/)
  assert.match(html, /运行所选项目（1）…/)
  assert.doesNotMatch(html, /运行待制作项目（2）…/, '已选择项目时只呈现所选范围入口')
  assert.doesNotMatch(html, /核对运行步骤/)
})

test('制作状态仍是created的暂停完整流程只保留统一运行入口，不重复展示恢复按钮', async t => {
  const { html } = await render(t, Projects, page => {
    page.projects = [{ uid: 'paused', title: '暂停自动流程', status: 'created', workflow_status: 'paused',
      dir_exists: true, chain: null, steps: [step('download')] }]
  })
  assert.doesNotMatch(html, /检查恢复方案/)
  assert.match(html, /运行项目…/)
  assert.match(html, /运行待制作项目（1）…/)
  assert.doesNotMatch(html, /核对运行步骤/)
})

test('统一批量入口包含筛选隐藏的有效选择，单选保留恢复入口，归档或删除中不打开',async t=>{
  const {page,html}=await render(t,Projects,page=>{
    page.projects=[{uid:'one',title:'可见项目',status:'created',dir_exists:true,steps:[]},
      {uid:'hidden',title:'筛选隐藏项目',status:'created',dir_exists:true,steps:[]},
      {uid:'saved',title:'已归档快照',archived:true,status:'created',dir_exists:true,steps:[]}]
    page.sel=new Set(['one','hidden','saved']);page.searchText='可见'
  })
  const opened:any[]=[],singles:any[]=[]
  page.projectBatchPanel={open:(items:any[])=>opened.push(items)};page.stepRun={open:(items:any[])=>singles.push(items)}
  page.runSelected();assert.deepEqual(opened[0],[{uid:'one',title:'可见项目'},{uid:'hidden',title:'筛选隐藏项目'}])
  assert.match(html,/批量运行（3）/);assert.doesNotMatch(html,/顺序发布（|按顺序发布所选|按项目设置批量运行/)
  page.sel=new Set(['one']);page.runSelected();assert.equal(opened.length,1);assert.deepEqual(singles[0],[{uid:'one',title:'可见项目'}])
  page.sel=new Set(['one','hidden']);page.archiveScope='archived';page.runSelected();assert.equal(opened.length,1)
  page.archiveScope='active';page.deletingUids=new Set(['one']);page.openProjectBatch(page.projects);assert.equal(opened.length,1)
})

test('等待前项的批次项目保留批次进度入口，不能单项运行或归档',async t=>{
  const target={uid:'future',title:'后续项目',status:'created',dir_exists:true,steps:[],release_batch:{id:'batch',status:'running',position:2,total:3,phase:'等待前项完成'}}
  const {page,html}=await render(t,Projects,page=>{page.projects=[target]})
  assert.equal(page.projectBusy(target),true);assert.equal(page.pendingInView.length,0)
  assert.match(html,/查看进度/);assert.doesNotMatch(html,/运行项目…/)
  assert.match(html.replace(/<[^>]*>/g,''),/顺序发布 · 第 2\/3 项/);assert.match(html,/等待前项完成/);assert.match(html,/\/#release-batch-batch/)
  let opened=0;page.stepRun={open:()=>opened++};page.runProject(target);assert.equal(opened,0)
  const visited:any[]=[];t.mock.method(page.router,'push',(path:any)=>{visited.push(path);return Promise.resolve()})
  page.showProjectProgress(target);assert.deepEqual(visited,[{path:'/',hash:'#release-batch-batch'}])
})

for (const group_status of ['queued','running','pausing','stopping','attention']) {
  test(`统一批次${group_status}项目卡片保留父批次进度，禁止单独运行与绑定模板`,async t=>{
    const target={uid:'owned',title:'统一批次中的项目',status:'created',dir_exists:true,steps:[],
      project_batch:{batch_id:'parent',group_id:'collection',status:'running',group_status,position:2,total:3,phase:'等待本组合集顺序核验'},
      release_batch:{id:'child',status:'running',position:2,total:3,phase:'子任务进度'}}
    const {page,html,bindings}=await render(t,Projects,page=>{page.projects=[target]})
    assert.equal(page.projectBusy(target),true);assert.equal(page.pendingInView.length,0)
    assert.equal(page.projectMenu(target).find((item:any)=>item.key==='archive').disabled,true)
    assert.equal(page.projectMenu(target).find((item:any)=>item.key==='rename').disabled,true)
    assert.deepEqual(bindings,[{uids:['owned'],disabled:true}])
    assert.match(html,/查看进度/);assert.match(html,/批量运行 · 等待本组合集顺序核验/);assert.match(html,/\/#project-batch-parent/)
    assert.doesNotMatch(html,/运行项目…|\/#release-batch-child|顺序发布 · 第/)
    let opened=0;page.stepRun={open:()=>opened++};page.runProject(target);assert.equal(opened,0)
    const visited:any[]=[];t.mock.method(page.router,'push',(path:any)=>{visited.push(path);return Promise.resolve()})
    page.showProjectProgress(target);assert.deepEqual(visited,[{path:'/',hash:'#project-batch-parent'}])
  })
}

test('统一批次暂停后项目可编辑和进入单项目检查，同时保留父批次定位入口',async t=>{
  const target={uid:'paused',title:'已暂停的批次项目',status:'created',dir_exists:true,steps:[step('download')],
    project_batch:{batch_id:'parent',group_id:'collection',status:'paused',group_status:'paused',position:1,total:2,phase:'本组已暂停'}}
  const {page,html,bindings}=await render(t,Projects,page=>{page.projects=[target]})
  assert.equal(page.projectBusy(target),false);assert.equal(page.pendingInView.length,1)
  assert.equal(page.projectMenu(target).find((item:any)=>item.key==='archive').disabled,false)
  assert.equal(page.projectMenu(target).find((item:any)=>item.key==='rename').disabled,false)
  assert.deepEqual(bindings,[{uids:['paused'],disabled:false}])
  assert.match(html,/运行项目…/);assert.match(html,/\/#project-batch-parent/)
  const opened:any[]=[];page.stepRun={open:(items:any[])=>opened.push(items)};page.runProject(target)
  assert.deepEqual(opened,[[{uid:'paused',title:'已暂停的批次项目'}]])
})

test('239个逐句配音依赖汇总为两类，实际抽屉仅显示两行问题及完整步骤顺序', async t => {
  const requirements = Array.from({ length: 239 }, (_, index) => ({ step: 'tts', label: '字幕与配音',
    name: `配音文件-${index + 1}.wav`, state: index < 118 ? 'stale' : 'missing',
    reason: index < 118 ? '所选配音需要更新' : '尚无可选配音文件', config_blocked: false }))
  const { page } = await render(t)
  const calls = fixtures(t, (uid, path) => uid === 'one' && !path
    ? reply(detail(uid, { steps: [step('tts'), step('assemble')] }))
    : path === '/run_plan' ? reply(plan({ steps: ['tts', 'assemble'], requirements,
      labels: { tts: '字幕与配音', assemble: '合成成片' } })) : undefined)
  await page.open([targets[0]])
  const groups = page.issueGroups(page.items[0].plan.requirements)
  assert.equal(groups.length, 2)
  assert.deepEqual(groups.map((group: any) => group.count), [118, 121])
  assert.equal(groups.reduce((sum: number, group: any) => sum + group.count, 0), 239)
  assert.ok(groups.every((group: any) => group.names.length === 3), '仅保留少量样例，不能把全量文件名搬入汇总')
  assert.equal(page.canConfirm, true, '问题汇总不改变原来的可执行计划门禁')
  const snapshot = JSON.parse(JSON.stringify(page.items[0]))
  const { html } = await render(t, Panel, page => { page.show = true; page.items = [snapshot] })
  const list = html.match(/<ul\b[^>]*class="requirements"[^>]*>[\s\S]*?<\/ul>/)?.[0]
  assert.ok(list, '检验真实抽屉内容而非仅检查分组函数')
  assert.equal((list.match(/<li\b/g) || []).length, 2)
  assert.match(list, /118项需更新/); assert.match(list, /121项缺失/)
  assert.match(list, /所选配音需要更新/); assert.match(list, /尚无可选配音文件/)
  assert.doesNotMatch(list, /配音文件-239\.wav/)
  assert.deepEqual([...html.matchAll(/<li\b[^>]*data-step="([^"]+)"/g)].map(match => match[1]), ['tts', 'assemble'])
  assert.equal(starts(calls).length, 0)
})

test('不同原因、状态、配置门禁和步骤不被误合并，同类阻塞汇总仍禁止启动并显示于抽屉', async t => {
  const base = { step: 'tts', label: '字幕与配音', state: 'stale', reason: '音色设置已变化', config_blocked: false }
  const blockers = [
    ...Array.from({ length: 20 }, (_, index) => ({ ...base, name: `句${index + 1}.wav` })),
    ...Array.from({ length: 3 }, (_, index) => ({ ...base, reason: '参考音频已变化', name: `参考${index + 1}.wav` })),
    ...Array.from({ length: 2 }, (_, index) => ({ ...base, config_blocked: true, name: `配置${index + 1}.wav` })),
    ...Array.from({ length: 4 }, (_, index) => ({ ...base, state: 'deleted', name: `删除${index + 1}.wav` })),
    { ...base, step: 'translate', label: '翻译', name: 'translate.txt' },
    { ...base, label: '配音轨', name: 'alternate.wav' },
  ]
  const { page } = await render(t)
  const calls = fixtures(t, (_uid, path) => path === '/run_plan' ? reply(plan({ blockers })) : undefined)
  await page.open([targets[0]])
  const groups = page.issueGroups(blockers)
  assert.deepEqual(groups.map((group: any) => group.count), [20, 3, 2, 4, 1, 1])
  assert.equal(new Set(groups.map((group: any) => group.id)).size, 6)
  assert.equal(groups[2].configBlocked, true); assert.equal(groups[0].configBlocked, false)
  assert.equal(groups[3].state, 'deleted'); assert.equal(groups[4].step, 'translate')
  assert.equal(groups[5].label, '配音轨')
  assert.equal(page.items[0].blockers.length, 6)
  assert.equal(page.ready.length, 0); assert.equal(page.canConfirm, false)
  await page.confirmStart(); assert.equal(starts(calls).length, 0)
  const snapshot = JSON.parse(JSON.stringify(page.items[0]))
  const { html } = await render(t, Panel, page => { page.show = true; page.items = [snapshot] })
  const list = html.match(/<ul\b[^>]*class="blockers"[^>]*>[\s\S]*?<\/ul>/)?.[0]
  assert.ok(list)
  assert.equal((list.match(/<li\b/g) || []).length, 6)
  assert.match(list, /20项需更新/); assert.match(list, /3项需更新/); assert.match(list, /4项已删除/)
  assert.match(list, /音色设置已变化/); assert.match(list, /参考音频已变化/)
  assert.match(list, /翻译：translate\.txt/); assert.match(list, /配音轨：alternate\.wav/)
})
