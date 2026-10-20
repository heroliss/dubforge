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

let server: ViteDevServer, Projects: any, useProjectListStore: any
const reply = (data: any, status = 200, workspace = 'root-a') => ({ ok: status < 400, status, json: async () => data,
  headers: { get: () => workspace } })
const entry = (uid: string, fields: any = {}) => ({ uid, title: `项目${uid}`, status: 'created', steps: [],
  source_kind: 'local', dir_exists: true, archived: false, ...fields })
function deferred() {
  let resolve: (value: any) => void = () => {}
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
const runningTts = (completed: number) => ({ key: 'tts', label: '字幕与配音', status: 'running', job_id: 7,
  progress: completed / 305 * 100, selected: true, fresh: false,
  execution: { schema: 1, job_id: 7, state: 'running', phase: 'tts.synthesizing', label: '处理第 70 句配音',
    completed, total: 305, unit: '句', updated_at: `2026-10-20T01:00:${completed - 60}Z` } })
const summaryEntry = (uid: string, revision = 'revision-a', fields: any = {}) => entry(uid, {
  projection: 'summary', summary_revision: revision, freshness_known: false, status: 'checking', dir_exists: null,
  steps: [{key:'tts',label:'字幕与配音',status:'done',recorded_status:'done',fresh:null,freshness_known:false,job_id:7}], ...fields })
const summaryReply = (rows: any[], workspace = 'root-a') => ({ ok: true, status: 200, json: async () => rows,
  headers: { get: (key: string) => key === 'X-DubForge-Workspace' ? workspace : key === 'X-DubForge-Projection' ? 'summary' : null } })
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Projects = (await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  ;({ useProjectListStore } = await server.ssrLoadModule('/src/stores/projectList.ts'))
  mock.method(globalThis, 'fetch', () => { throw new Error('禁止未声明的真实网络请求') })
})
after(async () => { mock.restoreAll(); await server.close() })

async function render(t: any, pinia = createPinia(), initialRows?: any[], initialSelection?: string[]) {
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: { render: () => null } }, { path: '/:pathMatch(.*)*', component: { render: () => null } },
  ] })
  await router.push('/')
  let page: any
  const bindings:any[]=[]
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Projects))) })
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ProjectsView') {
    page = this.$.setupState
    if (initialRows) { page.projects=initialRows;page.loading=false }
    if (initialSelection) page.sel=new Set(initialSelection)
  }
    if (this.$options.__name==='ProjectTemplateBinding') bindings.push({state:this.$.setupState,props:this.$props})
  } })
  const html=await renderToString(app)
  t.after(() => { page.disposeList(); disposePinia(pinia) })
  return { page, pinia, html, bindings, snapshots: useProjectListStore(pinia) }
}

async function runningListWithHeldFull(t:any) {
  const rendered=await render(t),reads:any[]=[],calls:any[]=[]
  const active=runningTts(69),terminal={...runningTts(305),status:'done',progress:100,
    execution:{schema:1,owner:'step',job_id:7,state:'completed',completed:305,total:305,unit:'句'}}
  const state={summaryStep:active,revision:'running',runtime:terminal as any}
  let first=true
  t.mock.method(globalThis,'fetch',(url:any,options:any)=>{
    const path=String(url);calls.push({path,options})
    assert.ok(!options?.method||options.method==='GET','列表回归不允许发出规划或执行写请求')
    if(path.endsWith('/context'))return Promise.resolve(reply({workspace_id:'root-a'}))
    if(path.includes('view=summary'))return Promise.resolve(summaryReply([summaryEntry('handoff',state.revision,
      {status:state.summaryStep.status==='queued'||state.summaryStep.status==='running'?'processing':'checking',
       steps:[{...state.summaryStep,fresh:null,freshness_known:false,execution:state.summaryStep.status==='done'?null:state.summaryStep.execution}]} )]))
    if(path.includes('/execution?'))return Promise.resolve(reply({workspace_id:'root-a',projects:[{uid:'handoff',steps:[state.runtime]}]}))
    assert.equal(path,'/api/projects')
    if(first){first=false;return Promise.resolve(reply([entry('handoff',{status:'processing',steps:[active]})]))}
    return new Promise((resolve,reject)=>reads.push({resolve,reject,options}))
  })
  await rendered.page.initializeList();await flush()
  return {...rendered,state,terminal,reads,calls}
}

test('真实列表收到终态后summary心跳不解除核验，旧full不放行，真实full三结果才接回',async t=>{
  for(const status of ['done','stale','failed']) {
    const {page,state,terminal,reads}=await runningListWithHeldFull(t),opened:any[]=[]
    page.stepRun={open:(...args:any[])=>opened.push(args)}
    await page.executionReader.refresh();assert.equal(page.projects[0].steps[0].status,'checking')
    state.summaryStep={...terminal,job_status:'done'} as any;state.revision='terminal'
    await page.readSummary();await page.readSummary()
    assert.equal(page.projects[0].steps[0].status,'checking');assert.equal(page.projects[0].steps[0].fresh,null)
    assert.equal(page.projectBusy(page.projects[0]),true);page.runProject(page.projects[0]);assert.equal(opened.length,0)
    assert.equal(page.unknownPlanProjects.length,0);assert.equal(page.verifyingProjects.length,1)
    const displayed=await render(t,createPinia(),page.projects)
    // Naive 按钮自身含 div 波纹节点，按真实 button 边界读取，不能截断 action 容器。
    const buttons=[...displayed.html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
      .map(match=>match[0].replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim())
    assert.ok(buttons.some(text=>text.includes('查看进度')));assert.ok(!buttons.some(text=>text==='运行项目…'))
    assert.match(displayed.html,/任务已结束，正在核验产物/);assert.doesNotMatch(displayed.html,/可以先打开项目、选择模板或勾选运行/)
    reads[0].resolve(reply([entry('handoff',{status:'processing',steps:[runningTts(69)]})]));await flush()
    assert.equal(page.projects[0].steps[0].status,'checking');assert.equal(reads.length,2)
    reads[1].resolve(reply([entry('handoff',{status:status==='done'?'done':status==='failed'?'failed':'created',steps:[
      {...terminal,job_id:null,status,fresh:status==='done',error:status==='failed'?'本次真实错误':null}]} )]));await flush()
    assert.equal(page.projects[0].steps[0].status,status);assert.equal(page.projects[0].steps[0].fresh,status==='done')
    assert.equal(page.projectBusy(page.projects[0]),false)
    if(status==='failed')assert.equal(page.projects[0].steps[0].error,'本次真实错误')
    page.disposeList()
  }
})

test('核验中的真实列表继续轻量采样，新queued摘要和执行代次接替且旧full不能盖回',async t=>{
  const {page,state,reads,calls}=await runningListWithHeldFull(t)
  await page.executionReader.refresh();assert.equal(page.projects[0].steps[0].status,'checking')
  const before=calls.filter(call=>call.path.includes('/execution?')).length
  await page.executionReader.refresh()
  assert.equal(calls.filter(call=>call.path.includes('/execution?')).length,before+1)
  state.summaryStep={...runningTts(0),status:'queued',job_id:8,execution:{schema:1,job_id:8,state:'queued'}} as any
  state.runtime=state.summaryStep;state.revision='new-job'
  await page.readSummary();await page.executionReader.refresh()
  assert.equal(page.projects[0].steps[0].job_id,8);assert.equal(page.projects[0].steps[0].status,'queued')
  state.summaryStep={...runningTts(305),status:'done',job_id:7,execution:null} as any;state.revision='older-job'
  await page.readSummary()
  assert.equal(page.projects[0].steps[0].job_id,8);assert.equal(page.projects[0].steps[0].status,'queued','旧摘要也不能把新队列改成可运行')
  reads[0].resolve(reply([entry('handoff',{status:'done',steps:[{...runningTts(305),status:'done',fresh:true}]})]));await flush()
  assert.equal(page.projects[0].steps[0].job_id,8);assert.equal(page.projects[0].steps[0].status,'queued')
  assert.notEqual(page.projects[0].steps[0].fresh,true);assert.equal(page.projectBusy(page.projects[0]),true)
})

test('summary后的完整核验失败仍只读可重试，冷未知项目继续允许打开计划',async t=>{
  const {page,state,terminal,reads}=await runningListWithHeldFull(t)
  await page.executionReader.refresh();state.summaryStep=terminal as any;state.revision='terminal'
  await page.readSummary()
  reads[0].reject(new DOMException('请求超时','TimeoutError'));await flush()
  reads[1].reject(new DOMException('请求超时','TimeoutError'));await flush()
  assert.match(page.loadError,/请求超时/);assert.equal(page.projectBusy(page.projects[0]),true)
  assert.equal(page.projects[0].steps[0].status,'checking')
  const retry=page.refreshList();await flush()
  assert.equal(reads[2].options.headers['X-DubForge-Refresh'],'fresh')
  reads[2].resolve(reply([entry('handoff',{status:'done',steps:[{...terminal,job_id:null,fresh:true}]})]));await retry
  assert.equal(page.loadError,'');assert.equal(page.projectBusy(page.projects[0]),false)
  const cold=summaryEntry('cold'),opened:any[]=[]
  const display=await render(t,createPinia(),[cold]);display.page.stepRun={open:(...args:any[])=>opened.push(args)}
  assert.equal(display.page.projectBusy(cold),false);display.page.runProject(cold);assert.equal(opened.length,1)
  assert.match(display.html,/运行项目/);assert.match(display.html,/可以先打开项目、选择模板或勾选运行/)
})

test('已知checking所选运行与多选模板都禁用，真实嵌套绑定不发计划；冷未知选择仍可规划',async t=>{
  const writes:any[]=[],calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    const path=String(url);calls.push(path)
    if(options?.method==='POST')writes.push({path,body:JSON.parse(options.body)})
    if(path.endsWith('/templates'))return reply([])
    if(path.endsWith('/binding-plan'))return reply({name:'模板',fingerprint:'fixture-plan',projects:[]})
    if(path.endsWith('/editability'))return reply({locked:false,resource_locked:false,running_steps:[]})
    return reply({})
  })
  const known=summaryEntry('known','a',{steps:[{key:'tts',status:'checking',job_id:7,fresh:null,execution:{schema:1,job_id:7,state:'completed'}}]})
  const cold=summaryEntry('cold'),both=await render(t,createPinia(),[known,cold],['known','cold']);await flush()
  const bulk=both.bindings.find(row=>!row.props.compact)
  assert.ok(bulk);assert.equal(bulk.props.disabled,true);await bulk.state.bind('template')
  assert.equal(writes.length,0,'本地核验即使后台idle也不能进入绑定计划')
  const selectedButton=[...both.html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)].find(match=>match[0].includes('批量运行'))?.[0]||''
  assert.match(selectedButton,/ disabled/)
  const planned:any[]=[];both.page.projectBatchPanel={open:(...args:any[])=>planned.push(args)}
  both.page.runSelected();assert.equal(planned.length,0)
  const single=await render(t,createPinia(),[known],['known']);single.page.stepRun={open:(...args:any[])=>planned.push(args)}
  single.page.runSelected();assert.equal(planned.length,0)
  const unknown=await render(t,createPinia(),[cold,summaryEntry('other')],['cold','other']);await flush()
  const enabled=unknown.bindings.find(row=>!row.props.compact)
  assert.equal(enabled.props.disabled,false)
  unknown.page.projectBatchPanel={open:(...args:any[])=>planned.push(args)};unknown.page.runSelected();assert.equal(planned.length,1)
  await enabled.state.bind('template')
  assert.equal(writes.filter(row=>row.path.endsWith('/binding-plan')).length,1,'规划请求与执行提交明确分开')
  assert.equal(writes.filter(row=>row.path.endsWith('/bind')||/\/run$|\/start$|\/resume$/.test(row.path)).length,0)
  for(const status of ['running','queued']) {
    const active=summaryEntry(status,'active',{status:'processing',steps:[{...runningTts(0),status}]})
    const display=await render(t,createPinia(),[active],[status]);const scope:any[]=[]
    display.page.stepRun={open:(...args:any[])=>scope.push(args)}
    display.page.runSelected();assert.equal(scope.length,1,`保留既有${status}的计划入口语义，只阻断已知核验`)
  }
})

test('重新创建项目空间只保留列表快照，身份确认后立即显示卡片，不等待重列表', async t => {
  const first = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('old')]))
  await first.page.initializeList()
  assert.equal(first.snapshots.read('active').rows[0].uid, 'old')
  first.page.sel = new Set(['old']); first.page.disposeList()

  const slow = deferred(), calls: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url: String(url), options })
    return String(url).endsWith('/context') ? Promise.resolve(reply({ workspace_id: 'root-a' })) : slow.promise
  })
  const second = await render(t, first.pinia)
  assert.deepEqual(second.page.projects, [], '身份未核对时不直接展示跨后端快照')
  const loading = second.page.initializeList(); await flush()
  assert.deepEqual(calls.map(item => item.url).sort(), ['/api/projects/context', '/api/projects?view=summary'])
  assert.equal(second.page.projects[0].uid, 'old'); assert.equal(second.page.loading, false)
  assert.equal(second.page.refreshing, true); assert.equal(second.page.sel.size, 0)
  slow.resolve(reply([entry('new', { running_step: 'assemble' })])); await loading
  assert.equal(second.page.projects[0].uid, 'new'); assert.equal(second.page.refreshing, false)
})

test('历史交付轻量补读不阻塞首屏，普通刷新复用缓存且只读取workflow/status',async t=>{
  const {page}=await render(t),slow=deferred(),calls:string[]=[]
  const row=entry('delivery',{workflow_status:'attention',publication:{id:'same-publication',status:'published',
    latest_update:{id:'new-update',status:'completed',updated_at:'2026-01-02T00:00:00'}}})
  t.mock.method(globalThis,'fetch',(url:any)=>{
    const path=String(url);calls.push(path)
    if(path.endsWith('/context'))return Promise.resolve(reply({workspace_id:'root-a'}))
    if(path.endsWith('/workflow/status'))return slow.promise
    assert.ok(['/api/projects','/api/projects?view=summary'].includes(path),'不能为所有项目读取完整workflow或方案')
    return Promise.resolve(reply([row,entry('running',{workflow_status:'running'})]))
  })
  await page.initializeList();await flush()
  assert.equal(page.projects.length,2);assert.equal(page.loading,false);assert.equal(page.refreshing,false)
  assert.equal(page.currentProjectDelivery(page.projects[0]),null)
  assert.equal(calls.filter(path=>path.endsWith('/workflow/status')).length,1)
  await page.load();await flush()
  assert.equal(calls.filter(path=>path.endsWith('/workflow/status')).length,1,'在途补读不能随列表每轮刷新叠加')
  slow.resolve(reply({run:{id:'old-workflow',status:'attention',mode:'update',publication_id:'same-publication',
    publication:{id:'old-update',status:'paused',updated_at:'2026-01-01T00:00:00'}}}))
  await flush();assert.equal(page.currentProjectDelivery(page.projects[0]).text,'原稿更新已核验')
  await page.load();await flush()
  assert.equal(calls.filter(path=>path.endsWith('/workflow/status')).length,1,'同修订已经核验后复用缓存')
  page.projectStateChanged();await flush()
  assert.equal(calls.filter(path=>path.endsWith('/workflow/status')).length,2,'本页新操作后必须重新核对，不能沿用旧流程的修复证明')
})

test('实际卡片30秒内不重复核对，同修订到期待核对期间不闪回旧失败，未知结果回归原提示',async t=>{
  let clock=1000,reads=0
  t.mock.method(Date,'now',()=>clock)
  const {page}=await render(t),nextRead=deferred()
  const row=entry('ttl',{workflow_status:'attention',publication:{id:'same-publication',status:'published',
    latest_update:{id:'new-update',status:'completed',updated_at:'2026-01-02T00:00:00'}}})
  const old={id:'old-workflow',status:'attention',mode:'update',publication_id:'same-publication',
    publication:{id:'old-update',status:'paused',updated_at:'2026-01-01T00:00:00'}}
  t.mock.method(globalThis,'fetch',async(url:any)=>String(url).endsWith('/context')?reply({workspace_id:'root-a'})
    :String(url).endsWith('/workflow/status')?(++reads===1?reply({run:old}):nextRead.promise):reply([row]))
  await page.initializeList();await flush()
  assert.equal(page.currentProjectDelivery(page.projects[0]).text,'原稿更新已核验')
  clock=30999;await page.load();await flush();assert.equal(reads,1)
  clock=31000;await page.load();await flush();assert.equal(reads,2)
  assert.equal(page.currentProjectDelivery(page.projects[0]),null)
  assert.equal(page.recheckingProjectDelivery(page.projects[0]),true,'重新核对时使用中性标签，不能短暂复活旧失败')
  nextRead.resolve(reply({run:{...old,publication:{...old.publication,status:'unknown'}}}));await flush()
  assert.equal(page.recheckingProjectDelivery(page.projects[0]),false)
  assert.equal(page.currentProjectDelivery(page.projects[0]),null,'未知写入恢复原处理提示，不能继续显示缓存成功')
})

test('同源实际工作目录改变不显示旧卡片；列表成功后才登记新范围', async t => {
  const { page, snapshots } = await render(t)
  snapshots.activate('|root-a'); snapshots.save('active', [entry('old')])
  snapshots.save('archived', [entry('archived', { archived: true })])
  const slow = deferred()
  t.mock.method(globalThis, 'fetch', url => String(url).endsWith('/context')
    ? Promise.resolve(reply({ workspace_id: 'root-b' })) : slow.promise)
  const loading = page.initializeList(); await flush()
  assert.equal(snapshots.context, '|root-b'); assert.equal(page.projects.length, 0)
  assert.equal(snapshots.read('archived'), null)
  slow.resolve(reply([entry('new')], 200, 'root-b')); await loading
  assert.equal(page.projects[0].uid, 'new'); assert.equal(snapshots.read('active').rows[0].uid, 'new')
})

test('身份失败或旧后端404时不采用快照，也不阻断成功的实时列表', async t => {
  for (const failure of ['404', 'network', 'invalid']) {
    const { page, snapshots } = await render(t)
    snapshots.activate('|previous'); snapshots.save('active', [entry('old')])
    t.mock.method(globalThis, 'fetch', async url => {
      if (!String(url).endsWith('/context')) return reply([entry('live')])
      if (failure === 'network') throw new Error('Failed to fetch')
      return failure === '404' ? reply({}, 404) : reply({})
    })
    await page.initializeList()
    assert.equal(page.projects[0].uid, 'live'); assert.equal(page.loadError, '')
    assert.equal(page.loading, false); assert.equal(snapshots.context, '')
  }
})

test('列表先返回时等待并行身份核对，稍晚的身份结果不能用旧缓存覆盖新状态', async t => {
  const { page, snapshots } = await render(t), context = deferred()
  snapshots.activate('|root-a'); snapshots.save('active', [entry('old')])
  t.mock.method(globalThis, 'fetch', url => String(url).endsWith('/context')
    ? context.promise : Promise.resolve(reply([entry('live', { status: 'processing' })])))
  const loading = page.initializeList(); await flush()
  assert.equal(page.projects.length, 0)
  context.resolve(reply({ workspace_id: 'root-a' })); await loading
  assert.equal(page.projects[0].uid, 'live'); assert.equal(snapshots.read('active').rows[0].uid, 'live')
})

test('后台核对失败保留卡片和可重试错误；重试成功移除错误并更新快照', async t => {
  const { page, snapshots } = await render(t)
  snapshots.activate('|root-a'); snapshots.save('active', [entry('old')])
  t.mock.method(globalThis, 'fetch', async url => {
    if (String(url).endsWith('/context')) return reply({ workspace_id: 'root-a' })
    throw new Error('Failed to fetch')
  })
  await page.initializeList()
  assert.equal(page.projects[0].uid, 'old'); assert.match(page.loadError, /无法连接服务|保留上次显示/)
  assert.equal(page.loading, false); assert.equal(page.refreshing, false)
  t.mock.method(globalThis, 'fetch', async () => reply([entry('live')]))
  await page.refreshList()
  assert.equal(page.projects[0].uid, 'live'); assert.equal(page.loadError, '')
})

test('离开页面取消两路读取，迟到响应不污染新实例或已确认的工作区快照', async t => {
  const { page, snapshots } = await render(t), context = deferred(), list = deferred(), calls: any[] = []
  snapshots.activate('|old-root'); snapshots.save('active', [entry('old')])
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url: String(url), options })
    return String(url).endsWith('/context') ? context.promise : list.promise
  })
  const loading = page.initializeList(); page.disposeList()
  assert.ok(calls.every(item => item.options.signal.aborted))
  snapshots.activate('|new-root'); snapshots.save('active', [entry('new')])
  context.resolve(reply({ workspace_id: 'late-root' })); list.resolve(reply([entry('late')])); await loading
  assert.equal(snapshots.context, '|new-root'); assert.equal(snapshots.read('active').rows[0].uid, 'new')
  assert.equal(page.projects.length, 0)
})

test('活动与归档快照独立，切换范围先显示正确卡片，后台核对不盖住已有列表', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('active')]))
  await page.initializeList()
  snapshots.save('archived', [entry('saved', { archived: true })])
  const slow = deferred()
  t.mock.method(globalThis, 'fetch', () => slow.promise)
  page.switchArchiveScope('archived')
  assert.equal(page.projects[0].uid, 'saved'); assert.equal(page.loading, false)
  slow.resolve(reply([entry('saved-latest', { archived: true })])); await flush()
  assert.equal(page.projects[0].uid, 'saved-latest')
  page.switchArchiveScope('active')
  assert.equal(page.projects[0].uid, 'active'); assert.equal(page.loading, false)
})

test('归档成功更新当前快照并使另一范围失效，返回页面不会复活已移动项目', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('move'), entry('keep')]))
  await page.initializeList()
  snapshots.save('archived', [entry('older', { archived: true })])
  t.mock.method(globalThis, 'fetch', async () => reply({ ok: true, archived: true, archived_at: null }))
  await page.changeArchive([page.projects[0]], true)
  assert.deepEqual(snapshots.read('active').rows.map((item: any) => item.uid), ['keep'])
  assert.equal(snapshots.read('archived'), null)
})

test('改名后返回保留新标题，导入会失效快照；刷新失败不重新缓存旧范围', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('rename')]))
  await page.initializeList()
  page.renaming = page.projects[0]; page.renameTitle = '新标题'
  t.mock.method(globalThis, 'fetch', async () => reply({ uid: 'rename', title: '新标题', title_mode: 'manual' }))
  await page.applyRename()
  assert.equal(snapshots.read('active').rows[0].title, '新标题')
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('Failed to fetch') })
  page.projectsImported(['new-project']); await flush()
  assert.equal(snapshots.read('active'), null)
  assert.deepEqual([...page.sel], ['new-project'])
  assert.equal(page.projects[0].title, '新标题')
})

test('旧页面写回调晚到只使对应目录缓存失效，不重存被新页面删除的旧项目', async t => {
  const first = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('rename'), entry('removed-later')]))
  await first.page.initializeList()
  const pending = deferred(), writes: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => { writes.push({ url, options }); return pending.promise })
  first.page.renaming = first.page.projects[0]; first.page.renameTitle = '写入中的标题'
  const rename = first.page.applyRename(); await flush()
  first.page.disposeList()
  first.snapshots.save('active', [entry('rename', { title: '新页面最新状态' })])
  pending.resolve(reply({ uid: 'rename', title: '写入中的标题' })); await rename
  assert.equal(writes.length, 1)
  assert.equal(first.snapshots.read('active'), null, '旧数组不能覆盖新页面快照')
})

test('旧目录归档回执晚到不失效新目录缓存，不在已卸载页面启动刷新', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('move')]))
  await page.initializeList()
  const pending = deferred(), calls: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => { calls.push({ url, options }); return pending.promise })
  const archive = page.changeArchive([page.projects[0]], true)
  page.disposeList(); snapshots.activate('|root-b'); snapshots.save('active', [entry('new-root')])
  pending.resolve(reply({ ok: true, archived: true, archived_at: null })); await archive
  assert.equal(calls.length, 1); assert.equal(snapshots.read('active').rows[0].uid, 'new-root')
  await page.load(); assert.equal(calls.length, 1)
})

test('并行身份与列表来自不同工作目录时不显示也不保存错目录响应，重试重新核对身份', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-b' }) : reply([entry('wrong-root')], 200, 'root-a'))
  await page.initializeList()
  assert.equal(page.projects.length, 0); assert.equal(snapshots.read('active'), null)
  assert.match(page.loadError, /工作目录已变化/)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-b' }) : reply([entry('right-root')], 200, 'root-b'))
  await page.refreshList()
  assert.equal(page.projects[0].uid, 'right-root')
  assert.equal(snapshots.read('active').rows[0].uid, 'right-root')
})

test('缺少列表身份头的旧接口仍可显示实时结果，但不能污染已核对目录的快照', async t => {
  const { page, snapshots } = await render(t)
  snapshots.activate('|root-a'); snapshots.save('active', [entry('old')])
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('live')], 200, ''))
  await page.initializeList()
  assert.equal(page.projects[0].uid, 'live'); assert.equal(page.loadError, '')
  assert.equal(snapshots.read('active'), null)
})

test('首次及普通轮询允许合并，手动刷新显式旁路服务端旧读取', async t => {
  const { page } = await render(t), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url: String(url), options })
    return String(url).endsWith('/context') ? reply({ workspace_id: 'root-a' }) : reply([entry('one')])
  })
  await page.initializeList(); await page.load(); await page.refreshList()
  const reads = calls.filter(item => item.url.startsWith('/api/projects') && !item.url.endsWith('/context'))
  assert.equal(reads[0].options.headers, undefined)
  assert.equal(reads[1].options.headers, undefined)
  assert.equal(reads[2].options.headers['X-DubForge-Refresh'], 'fresh')
})

test('后台更新不触发手动按钮忙态；点击时反馈加载、防重入并在失败后恢复', async t => {
  const { page } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one')]))
  await page.initializeList()
  const background = deferred()
  t.mock.method(globalThis, 'fetch', () => background.promise)
  const polling = page.load()
  assert.equal(page.refreshing, true); assert.equal(page.manualRefreshing, false)
  background.resolve(reply([entry('live')])); await polling
  assert.equal(page.manualRefreshing, false)

  const clicked = deferred(), reads: any[] = []
  t.mock.method(globalThis, 'fetch', (_url, options) => { reads.push(options); return clicked.promise })
  const manual = page.refreshList(); await page.refreshList()
  assert.equal(page.manualRefreshing, true); assert.equal(reads.length, 1)
  assert.equal(reads[0].headers['X-DubForge-Refresh'], 'fresh')
  clicked.resolve(reply({}, 500)); await manual
  assert.equal(page.manualRefreshing, false); assert.equal(page.refreshing, false)
  assert.match(page.loadError, /500/); assert.equal(page.projects[0].uid, 'live')
})

test('改名使写前列表失效且两范围各补一次fresh，旧轮询不能覆盖新标题', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('rename')]))
  await page.initializeList()
  const stale = deferred(), calls: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url: String(url), options })
    if (options?.method === 'PATCH') return Promise.resolve(reply({ uid: 'rename', title: '新标题' }))
    if ((options?.headers as any)?.['X-DubForge-Refresh'] === 'fresh') {
      return Promise.resolve(reply(String(url).includes('archived') ? [entry('saved', { archived: true })] : [entry('rename', { title: '新标题' })]))
    }
    return stale.promise
  })
  const polling = page.load()
  page.renaming = page.projects[0]; page.renameTitle = '新标题'
  await page.applyRename()
  assert.equal(snapshots.freshNeeded.active, true); assert.equal(snapshots.freshNeeded.archived, true)
  stale.resolve(reply([entry('rename', { title: '写前旧标题' })])); await polling; await flush()
  assert.equal(page.projects[0].title, '新标题')
  assert.equal(snapshots.freshNeeded.active, false); assert.equal(snapshots.freshNeeded.archived, true)
  assert.equal(calls.filter(item => item.url === '/api/projects' && item.options.headers?.['X-DubForge-Refresh'] === 'fresh').length, 1)
  page.switchArchiveScope('archived'); await flush()
  assert.equal(calls.at(-1).options.headers['X-DubForge-Refresh'], 'fresh')
  assert.equal(snapshots.freshNeeded.archived, false)
})

test('旧页面迟到写入后离开再返回也发送fresh，不加入服务端仍在运行的写前读取', async t => {
  const first = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('move')]))
  await first.page.initializeList()
  const pending = deferred()
  t.mock.method(globalThis, 'fetch', () => pending.promise)
  const archive = first.page.changeArchive([first.page.projects[0]], true)
  first.page.disposeList()
  pending.resolve(reply({ ok: true, archived: true, archived_at: null })); await archive
  assert.equal(first.snapshots.freshNeeded.active, true)
  const second = await render(t, first.pinia), reads: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (String(url).endsWith('/context')) return reply({ workspace_id: 'root-a' })
    reads.push(options)
    return reply([])
  })
  await second.page.initializeList()
  assert.equal(reads[0].headers['X-DubForge-Refresh'], 'fresh')
  assert.equal(second.snapshots.freshNeeded.active, false); assert.equal(second.snapshots.freshNeeded.archived, true)
})

test('写后fresh读取失败仍需fresh，直到同范围成功核对后才恢复普通合并读取', async t => {
  const { page, snapshots } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one')]))
  await page.initializeList()
  snapshots.changed()
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url, options) => { calls.push(options); throw new Error('Failed to fetch') })
  await page.load()
  assert.equal(calls[0].headers['X-DubForge-Refresh'], 'fresh'); assert.equal(snapshots.freshNeeded.active, true)
  t.mock.method(globalThis, 'fetch', async (_url, options) => { calls.push(options); return reply([entry('live')]) })
  await page.load(); await page.load()
  assert.equal(calls[1].headers['X-DubForge-Refresh'], 'fresh')
  assert.equal(calls[2].headers, undefined); assert.equal(snapshots.freshNeeded.active, false)
})

test('完整列表还在核对时轻量进度先显示，迟到列表不倒退当前句', async t => {
  const { page } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one', { steps: [runningTts(69)] })]))
  await page.initializeList()
  const slow = deferred()
  t.mock.method(globalThis, 'fetch', url => String(url).includes('/execution?')
    ? Promise.resolve(reply({ workspace_id: 'root-a', projects: [{ uid: 'one', steps: [runningTts(70)] }] })) : slow.promise)
  const fullRead = page.load()
  await page.executionReader.refresh()
  assert.equal(page.projects[0].steps[0].execution.completed, 70)
  assert.equal(page.projects[0].steps[0].selected, true)
  slow.resolve(reply([entry('one', { steps: [runningTts(69)] })])); await fullRead
  assert.equal(page.projects[0].steps[0].execution.completed, 70)
})

test('轻量结束回执只触发一次完整核对，不直接把未校验产物算成完成', async t => {
  const { page } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one', { steps: [runningTts(69)] })]))
  await page.initializeList()
  let fullReads = 0
  const slow = deferred(), completed = { ...runningTts(305), status: 'done', progress: 100,
    execution: { ...runningTts(305).execution, state: 'completed' } }
  t.mock.method(globalThis, 'fetch', url => {
    if (String(url).includes('/execution?')) return Promise.resolve(reply({ workspace_id: 'root-a', projects: [{ uid: 'one', steps: [completed] }] }))
    fullReads++; return slow.promise
  })
  await page.executionReader.refresh(); await page.executionReader.refresh()
  assert.equal(fullReads, 1)
  assert.equal(page.projects[0].steps[0].fresh, null)
  assert.equal(page.projects[0].steps[0].status, 'checking')
  slow.resolve(reply([entry('one', { steps: [{ ...completed, status: 'stale', fresh: false }] })]))
  await flush()
  assert.equal(page.projects[0].steps[0].status, 'stale')
})

test('独立任务终态核验期间卡片不开放重跑，完整核对后才恢复原运行入口',async t=>{
  const {page}=await render(t),slow=deferred(),opened:any[]=[]
  t.mock.method(globalThis,'fetch',async url=>String(url).endsWith('/context')
    ?reply({workspace_id:'root-a'}):reply([entry('manual',{status:'processing',steps:[runningTts(69)]})]))
  await page.initializeList()
  page.stepRun={open:(...args:any[])=>opened.push(args)}
  const completed={...runningTts(305),status:'done',progress:100,execution:{...runningTts(305).execution,state:'completed'}}
  t.mock.method(globalThis,'fetch',url=>String(url).includes('/execution?')
    ?Promise.resolve(reply({workspace_id:'root-a',projects:[{uid:'manual',steps:[completed]}]})):slow.promise)
  await page.executionReader.refresh()
  const checking=page.projects[0]
  assert.equal(checking.steps[0].status,'checking');assert.equal(checking.steps[0].fresh,null)
  assert.equal(checking.running_step,null);assert.equal(checking.workflow_status,undefined)
  assert.equal(page.projectBusy(checking),true);page.runProject(checking);assert.equal(opened.length,0)
  assert.equal(page.pendingInView.length,0,'待制作快捷入口同样不能加入核验中的独立任务')
  slow.resolve(reply([entry('manual',{status:'created',steps:[{...completed,job_id:null,status:'stale',fresh:false}]})]));await flush()
  assert.equal(page.projectBusy(page.projects[0]),false)
  page.runProject(page.projects[0]);assert.equal(opened.length,1)
})

test('换归档范围和退出页面后迟到的进度不会覆盖新列表', async t => {
  const { page } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one', { steps: [runningTts(69)] })]))
  await page.initializeList()
  const late = deferred()
  t.mock.method(globalThis, 'fetch', url => String(url).includes('/execution?') ? late.promise
    : Promise.resolve(reply([entry('archived', { archived: true })])))
  const read = page.executionReader.refresh(); await flush()
  page.switchArchiveScope('archived'); await flush()
  late.resolve(reply({ workspace_id: 'root-a', projects: [{ uid: 'one', steps: [runningTts(70)] }] })); await read
  assert.equal(page.projects[0].uid, 'archived')
  page.disposeList()
  await page.executionReader.refresh()
  assert.equal(page.projects[0].uid, 'archived')
})

test('批次准备阶段也读取轻量状态，实际新任务开始时立即更新卡片制作状态', async t => {
  const { page } = await render(t)
  const old = { ...runningTts(69), status: 'failed', execution: { ...runningTts(69).execution, state: 'failed' } }
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one', { status: 'failed', steps: [old], project_batch: { group_status: 'running' } })]))
  await page.initializeList()
  const slow = deferred()
  t.mock.method(globalThis, 'fetch', url => String(url).includes('/execution?')
    ? Promise.resolve(reply({ workspace_id: 'root-a', projects: [{ uid: 'one', steps: [{ ...runningTts(70), job_id: 8,
      execution: { ...runningTts(70).execution, job_id: 8 } }] }] })) : slow.promise)
  await page.executionReader.refresh()
  assert.equal(page.projects[0].status, 'processing')
  assert.equal(page.projects[0].steps[0].job_id, 8)
  assert.equal(page.projects[0].steps[0].selected, true)
  slow.resolve(reply([entry('one', { status: 'processing', steps: [runningTts(70)] })])); await flush()
})

test('旧后端缺少轻量接口时只探测一次，继续用完整列表且不要求中断任务', async t => {
  const { page } = await render(t)
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/context')
    ? reply({ workspace_id: 'root-a' }) : reply([entry('one', { steps: [runningTts(69)] })]))
  await page.initializeList()
  let probes = 0
  t.mock.method(globalThis, 'fetch', async url => {
    if (String(url).includes('/execution?')) { probes++; return reply({ message: '接口不存在' }, 404) }
    return reply([entry('one', { steps: [runningTts(70)] })])
  })
  await page.executionReader.refresh(); await page.executionReader.refresh()
  assert.equal(probes, 1)
  await page.load()
  assert.equal(page.projects[0].steps[0].execution.completed, 70)
  assert.equal(page.loadError, '')
})


test('冷首屏先展示轻量摘要，未知不进入完成失败或待制作范围；慢full不阻塞选择与模板',async t=>{
  const {page}=await render(t),full=deferred(),calls:string[]=[]
  t.mock.method(globalThis,'fetch',(url:any)=>{
    const path=String(url);calls.push(path)
    if(path.endsWith('/context'))return Promise.resolve(reply({workspace_id:'root-a'}))
    if(path.includes('view=summary'))return Promise.resolve(summaryReply([summaryEntry('first','a',{project_settings:{known:true,template_name:'模板'}})]))
    assert.equal(path,'/api/projects');return full.promise
  })
  await page.initializeList()
  assert.equal(page.projects[0].uid,'first');assert.equal(page.projects[0].project_settings.template_name,'模板')
  assert.equal(page.loading,false);assert.equal(page.manualRefreshing,false)
  assert.equal(page.projects[0].status,'checking');assert.equal(page.statusCounts.checking,1)
  assert.equal(page.statusCounts.failed,undefined);assert.equal(page.statusCounts.done,undefined)
  assert.equal(page.pendingInView.length,0)
  assert.equal(page.orderedProjectSteps(page.projects[0]).find((step:any)=>step.key==='tts').status,'checking')
  page.toggleSel('first',true);assert.equal(page.sel.has('first'),true)
  full.resolve(reply([entry('first',{status:'done',steps:[{key:'tts',label:'字幕与配音',status:'done',fresh:true,job_id:7}]})]));await flush()
  assert.equal(page.projects[0].status,'done');assert.equal(page.projects[0].freshness_known,true)
  assert.equal(page.sel.has('first'),true);assert.equal(calls.filter(path=>path==='/api/projects').length,1)
})

test('五秒摘要心跳不排队慢full，完整核验按完成时间间隔一分钟并静默保留上次结果',async t=>{
  let clock=1000,fullReads=0,summaryReads=0
  t.mock.method(Date,'now',()=>clock)
  const {page}=await render(t),first=deferred(),second=deferred()
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary')){summaryReads++;return summaryReply([summaryEntry('slow')])}
    fullReads++;return fullReads===1?first.promise:second.promise
  })
  await page.initializeList()
  for(let i=0;i<6;i++){clock+=5000;await page.pollList()}
  assert.equal(summaryReads,7);assert.equal(fullReads,1,'不能在慢读取期间不断设置pending full')
  first.resolve(reply([entry('slow',{status:'done',steps:[{key:'tts',status:'done',fresh:true,job_id:7}]})]));await flush()
  await page.pollList();assert.equal(fullReads,1)
  assert.equal(page.projects[0].status,'done');assert.equal(page.projects[0].steps[0].fresh,true)
  assert.equal(page.projects[0].projection,'summary');assert.equal(page.manualRefreshing,false)
  clock+=59999;await page.pollList();assert.equal(fullReads,1)
  clock++;await page.pollList();assert.equal(fullReads,2)
  second.resolve(reply([entry('slow',{status:'done',steps:[{key:'tts',status:'done',fresh:true,job_id:7}]})]));await flush()
})

test('旧后端忽略view时首屏完整结果只读一次，后续五秒不持续重核验',async t=>{
  let clock=1000,reads=0;t.mock.method(Date,'now',()=>clock)
  const {page}=await render(t)
  t.mock.method(globalThis,'fetch',async(url:any)=>String(url).endsWith('/context')?reply({workspace_id:'root-a'}):(++reads,reply([entry('legacy',{status:'done'})])))
  await page.initializeList();assert.equal(reads,1)
  for(let i=0;i<11;i++){clock+=5000;await page.pollList()}
  assert.equal(reads,1);assert.equal(page.projects[0].status,'done')
  clock+=5000;await page.pollList();assert.equal(reads,2)
})

test('摘要404只回退一次完整读取，服务错误不盲目追加heavy请求；空列表可由响应头识别summary',async t=>{
  for(const status of [404,422,500]){
    const {page}=await render(t);const calls:string[]=[]
    t.mock.method(globalThis,'fetch',async(url:any)=>{
      const path=String(url);calls.push(path)
      if(path.endsWith('/context'))return reply({workspace_id:'root-a'})
      return path.includes('view=summary')?reply({},status):reply([entry('legacy')])
    })
    await page.initializeList()
    assert.equal(calls.filter(path=>path==='/api/projects').length,status===500?0:1)
    if(status!==500){await page.pollList();assert.equal(calls.filter(path=>path.includes('view=summary')).length,1)}
  }
  const {page}=await render(t);let full=0
  t.mock.method(globalThis,'fetch',async(url:any)=>String(url).endsWith('/context')?reply({workspace_id:'root-a'}):String(url).includes('view=summary')?summaryReply([]):(++full,reply([])))
  await page.initializeList();await flush();await page.pollList()
  assert.equal(full,1);assert.deepEqual(page.projects,[])
})

test('新Job摘要撤销旧完成与范围，迟到full不覆盖新修订且只合并一次补核',async t=>{
  const {page}=await render(t),old=deferred(),next=deferred();let token='a',reads=0
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return summaryReply([summaryEntry('switch',token,token==='a'?{}:{status:'processing',steps:[{key:'tts',label:'字幕与配音',job_id:8,status:'queued',fresh:null,execution:{schema:1,job_id:8,state:'queued',label:'排队',phase:'queued'}}]})])
    return ++reads===1?old.promise:next.promise
  })
  await page.initializeList();token='b';await page.pollList();await page.pollList()
  assert.equal(reads,1,'旧核验尚未结束时不另启并行完整扫描')
  old.resolve(reply([entry('switch',{status:'done',steps:[{key:'tts',status:'done',fresh:true,job_id:7}]})]));await flush()
  assert.equal(reads,2);assert.equal(page.projects[0].status,'processing')
  assert.equal(page.projects[0].steps[0].job_id,8);assert.notEqual(page.projects[0].steps[0].fresh,true)
  assert.equal(page.projects[0].freshness_known,false)
  next.resolve(reply([entry('switch',{status:'processing',steps:[{key:'tts',status:'queued',fresh:false,job_id:8}]})]));await flush()
  assert.equal(page.projects[0].steps[0].job_id,8)
})

test('手动刷新不与在途核验并行，按钮等待实际fresh补读完成且连点不重复',async t=>{
  const {page}=await render(t),before=deferred(),after=deferred();const reads:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return summaryReply([summaryEntry('manual')])
    reads.push(options);return reads.length===1?before.promise:after.promise
  })
  await page.initializeList();const clicked=page.refreshList();await page.refreshList()
  assert.equal(reads.length,1);assert.equal(page.manualRefreshing,true)
  before.resolve(reply([entry('manual')]));await flush()
  assert.equal(reads.length,2);assert.equal(reads[1].headers['X-DubForge-Refresh'],'fresh')
  assert.equal(page.manualRefreshing,true,'不能把写前核验完成冒充手动fresh完成')
  after.resolve(reply([entry('manual',{status:'done'})]));await clicked
  assert.equal(page.manualRefreshing,false);assert.equal(page.projects[0].status,'done')
})

test('完整核验后summary终态清除活动且马上补核，评论摘要不借旧计数或伪造停滞',async t=>{
  const {page}=await render(t);let terminal=false,reads=0
  const pending=deferred()
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return summaryReply([summaryEntry('comment',terminal?'b':'a',terminal?
      {workflow_status:'completed',steps:[{key:'tts',status:'done',fresh:null,job_id:7}]}:
      {status:'processing',workflow_status:'running',workflow_summary:{id:'run',phase:'评论评分：测试'},comments_workspace:{state:'running',label:'评论处理中',detail:'详细数量在评论工作区查看'}})])
    reads++;return reads===1?reply([entry('comment',{status:'processing',workflow_status:'running',steps:[{key:'tts',status:'done',fresh:true,job_id:7}],comments_workspace:{state:'running',label:'正在评分',detail:'已评分185/500'}})]):pending.promise
  })
  await page.initializeList();await flush();terminal=true;await page.pollList()
  assert.equal(reads,2);assert.equal(page.projects[0].workflow_status,'completed')
  assert.equal(page.projects[0].status,'checking');assert.equal(page.currentProjectProcess(page.projects[0]).length,0)
  assert.equal(page.projects[0].comments_workspace,null,'终态新修订不能继续借用旧的185条活动信息')
  pending.resolve(reply([entry('comment',{status:'done',workflow_status:'completed'})]));await flush()
})


test('summary与完整读取均受删除/归档/工作目录身份约束，迟到摘要不复活成员',async t=>{
  const {page,snapshots}=await render(t);let archived=false
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return summaryReply([summaryEntry(archived?'archived':'active','a',{archived})])
    return reply([entry(archived?'archived':'active',{archived})])
  })
  await page.initializeList();await flush()
  const slow=deferred();const signals:AbortSignal[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    signals.push(options.signal)
    if(signals.length===1)return slow.promise
    return String(url).includes('view=summary')?summaryReply([summaryEntry('archived','b',{archived:true})]):reply([entry('archived',{archived:true})])
  })
  const reading=page.readSummary();archived=true;page.switchArchiveScope('archived');await flush()
  assert.equal(signals[0].aborted,true)
  slow.resolve(summaryReply([summaryEntry('late-active')]));await reading;await flush()
  assert.deepEqual(page.projects.map((row:any)=>row.uid),['archived'])
  page.disposeList();snapshots.activate('|new-root');snapshots.save('active',[entry('new-root')])
  assert.equal(snapshots.read('active').rows[0].uid,'new-root')
})

test('summary修订变化保留最新成员，迟到full不能把已移除项目放回或丢掉新增项目',async t=>{
  const {page}=await render(t),old=deferred(),next=deferred();let rows=[summaryEntry('removed')],reads=0
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return summaryReply(rows)
    return ++reads===1?old.promise:next.promise
  })
  await page.initializeList();rows=[summaryEntry('added')];await page.pollList()
  old.resolve(reply([entry('removed',{status:'done'})]));await flush()
  assert.deepEqual(page.projects.map((row:any)=>row.uid),['added']);assert.equal(reads,2)
  next.resolve(reply([entry('added')]));await flush()
})

test('summary请求开始后完成的full优先，迟到旧摘要不会复活旧running',async t=>{
  const {page}=await render(t);let slow:any=null
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return slow?slow.promise:summaryReply([summaryEntry('late')])
    return reply([entry('late',{status:'done',steps:[{key:'tts',status:'done',fresh:true,job_id:7}]})])
  })
  await page.initializeList();await flush();slow=deferred();const polling=page.readSummary()
  await page.load()
  slow.resolve(summaryReply([summaryEntry('late','old',{status:'processing',steps:[runningTts(70)]})]));await polling
  assert.equal(page.projects[0].status,'done');assert.equal(page.projects[0].steps[0].status,'done')
})


test('轻量执行先出现新Job时旧full不得借旧fresh或制作完成，尚未到下一轮summary也安全',async t=>{
  const {page}=await render(t),slow=deferred();let fullReads=0
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    const path=String(url)
    if(path.endsWith('/context'))return reply({workspace_id:'root-a'})
    if(path.includes('view=summary'))return summaryReply([summaryEntry('generation')])
    if(path.includes('/execution?'))return reply({workspace_id:'root-a',projects:[{uid:'generation',steps:[{...runningTts(71),job_id:8,execution:{...runningTts(71).execution,job_id:8}}]}]})
    return ++fullReads===1?reply([entry('generation',{status:'processing',workflow_status:'running',steps:[{...runningTts(70),fresh:true}]})]):slow.promise
  })
  await page.initializeList();await flush()
  const pending=page.load();await page.executionReader.refresh()
  assert.equal(page.projects[0].steps[0].job_id,8);assert.equal(page.projects[0].steps[0].fresh,null)
  slow.resolve(reply([entry('generation',{status:'done',steps:[{key:'tts',status:'done',fresh:true,job_id:7}]})]));await pending;await flush()
  assert.equal(page.projects[0].status,'processing');assert.equal(page.projects[0].freshness_known,false)
  assert.equal(page.projects[0].steps[0].job_id,8);assert.equal(page.projects[0].steps[0].fresh,null)
})


test('full核验失败提示不会被后续正常摘要抹掉，重试成功才清除且不持续重试full',async t=>{
  const {page}=await render(t);let fail=true,reads=0
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).endsWith('/context'))return reply({workspace_id:'root-a'})
    if(String(url).includes('view=summary'))return summaryReply([summaryEntry('error')])
    reads++;return fail?reply({},500):reply([entry('error',{status:'done'})])
  })
  await page.initializeList();await flush();assert.match(page.loadError,/500/)
  for(let i=0;i<4;i++)await page.pollList()
  assert.match(page.loadError,/500/);assert.equal(reads,1)
  fail=false;await page.refreshList();assert.equal(page.loadError,'');assert.equal(reads,2)
})


test('summary确切内存活动没有running_step也禁用再次运行，legacy raw processing不额外推断忙碌',async t=>{
  const {page}=await render(t)
  const processing=summaryEntry('managed','a',{status:'processing',running_step:null,steps:[],workflow_status:null})
  assert.equal(page.projectBusy(processing),true)
  assert.equal(page.projectBusy(entry('legacy',{status:'processing',steps:[],workflow_status:null})),false)
  page.projects=[processing];assert.equal(page.pendingInView.length,0)
})


test('活动范围full失败提示不串到归档摘要，归档full慢读时仍保持该范围无错',async t=>{
  const {page}=await render(t),archived=deferred()
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    const path=String(url)
    if(path.endsWith('/context'))return reply({workspace_id:'root-a'})
    if(path.includes('view=summary'))return summaryReply([summaryEntry(path.includes('archive=archived')?'archived':'active')])
    return path.includes('archive=archived')?archived.promise:reply({},500)
  })
  await page.initializeList();await flush();assert.match(page.loadError,/500/)
  page.switchArchiveScope('archived');await flush()
  assert.equal(page.projects[0].uid,'archived');assert.equal(page.loadError,'')
  archived.resolve(reply([entry('archived',{archived:true})]));await flush()
  assert.equal(page.loadError,'')
})
