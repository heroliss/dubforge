import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

// 使用现有 Vite 编译器加载真实 store，避免 Node 不支持无后缀 TS 导入。
const bundle = await build({ stdin: { contents: `export { useNavStore, STEP_LABELS } from './src/stores/nav';
  export { createPinia, setActivePinia } from 'pinia';`,
  resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', define: {'process.env.NODE_ENV': '"production"'} })
const { useNavStore, createPinia, setActivePinia, STEP_LABELS } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
// 评论读取由多个消费者共用；测试结束时结清网络 mock，避免同 UID 的后续用例复用悬空 promise。
function settleSharedComments(t:any,requests:any[]) {
  t.after(async()=>{
    for(const request of requests.filter(request=>request.url.endsWith('/comments/progress')))
      request.resolve(new Response(JSON.stringify({workspace:{state:'pending',label:'尚未准备'}})))
    await flush()
  })
}

test('各状态来源独立恢复，其他API成功不会隐藏步骤断线或丢掉最后可信状态', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia()); const nav = useNavStore()
  const reply = (request: any, data: any) => request.resolve(new Response(JSON.stringify(data)))
  try {
    nav.enter('p')
    reply(requests[0], {title: '已确认项目'})
    reply(requests[1], [{key: 'tts', status: 'done', fresh: true, selected: true}])
    reply(requests[2], {status: 'published'})
    reply(requests[3], {run: null})
    reply(requests.find(request=>request.url.endsWith('/comments/progress')), {workspace:{state:'pending',label:'尚未准备'}})
    await flush()
    const checked = nav.statusCheckedAt.steps
    assert.ok(checked)
    const start=requests.length,pending = nav.poll()
    const batch = requests.slice(start)
    batch.find(request=>request.url.endsWith('/steps')).reject(new TypeError('Failed to fetch')); await flush()
    assert.equal(nav.connectionLost, true)
    for (const [suffix,value] of [['/navigation',{title:'项目信息仍可用'}],['/status',{status:'published'}],['/workflow/status',{run:null}]] as const) {
      const request=batch.find(request=>request.url.endsWith(suffix)&&(!suffix.startsWith('/status')||request.url.startsWith('/api/publishing/')))
      reply(request,value)
    }
    const comments=batch.find(request=>request.url.endsWith('/comments/progress'))
    if(comments)reply(comments,{workspace:{state:'pending',label:'尚未准备'}})
    await pending
    assert.equal(nav.connectionLost, true)
    assert.equal(nav.stepMap.tts.status, 'done')
    assert.equal(nav.statusCheckedAt.steps, checked)
    assert.deepEqual(nav.connectionIssues.map((entry: any) => entry.source), ['steps'])
    assert.match(nav.stepsError, /保留上次核对结果/)
    const recovery = nav.refreshSteps()
    reply(requests.filter(request=>request.url.endsWith('/steps')).at(-1), [{key: 'tts', status: 'done', fresh: true, selected: true}]); await recovery
    assert.equal(nav.connectionLost, false); assert.equal(nav.stepsError, '')
    assert.deepEqual(nav.connectionIssues, [])
  } finally { nav.reset() }
})

test('步骤状态读取使用较宽的后端核对窗口，其他导航读取保持短超时', async t => {
  const timeoutValues: number[] = []
  const originalTimeout = AbortSignal.timeout
  t.mock.method(AbortSignal, 'timeout', (milliseconds: number) => {
    timeoutValues.push(milliseconds)
    return originalTimeout(milliseconds)
  })
  t.mock.method(globalThis, 'fetch', async url => new Response(JSON.stringify(
    String(url).endsWith('/steps') ? [{key: 'tts', status: 'done', fresh: true, selected: true}]
      : String(url).endsWith('/navigation') ? {title: '项目'} : {})))
  setActivePinia(createPinia())
  const nav = useNavStore()
  try {
    nav.uid = 'p'
    await nav.refreshSteps()
    await nav.refreshWorkflow()
    assert.ok(timeoutValues.includes(30000))
    assert.ok(timeoutValues.includes(15000))
  } finally { nav.reset() }
})

for (const [name, response, expected] of [
  ['HTTP失败', () => new Response('service temporarily unavailable', {status: 503}), /HTTP 503/],
  ['JSON不可解析', () => new Response('<html>gateway response</html>'), /无法解析/],
  ['状态结构错误', () => new Response(JSON.stringify({steps: []})), /格式不正确/],
] as const) test(`${name}只提示对应来源，不宣称全站断线`, async t => {
  t.mock.method(globalThis, 'fetch', async url => String(url).endsWith('/steps') ? response()
    : new Response(JSON.stringify(String(url).endsWith('/navigation') ? {title: '项目'} : {})))
  setActivePinia(createPinia()); const nav = useNavStore()
  try {
    nav.uid = 'p'; nav.steps = [{key: 'tts', status: 'done'}]; nav.stepsLoaded = true
    await nav.poll()
    assert.equal(nav.connectionLost, false)
    assert.match(nav.stepsError, expected)
    assert.equal(nav.projectError, ''); assert.equal(nav.workflowError, ''); assert.equal(nav.publicationError, '')
    assert.equal(nav.stepMap.tts.status, 'done')
  } finally { nav.reset() }
})

test('导航轻量读取批次占用，未来项目也锁编辑，暂停和切换释放旧项目状态',async t=>{
 const requests:any[]=[]
 t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
 settleSharedComments(t,requests)
 setActivePinia(createPinia());const nav=useNavStore()
 try {
  nav.enter('queued-project')
  requests.find(r=>r.url.endsWith('/navigation')).resolve(new Response(JSON.stringify({title:'排队项目',release_batch:{id:'batch',status:'running',position:2,total:3,phase:'等待前项完成'}})))
  await flush();assert.equal(nav.busy,true);assert.equal(nav.releaseBatch.position,2);assert.equal(nav.steps.length,0)
  for(const status of ['pausing','stopping']){nav.releaseBatch={...nav.releaseBatch,status};assert.equal(nav.busy,true)}
  nav.releaseBatch={...nav.releaseBatch,status:'paused'};assert.equal(nav.busy,false)
  nav.enter('other-project');assert.equal(nav.releaseBatch,null);assert.equal(nav.busy,false)
  nav.releaseBatch={id:'new',status:'running'};nav.reset();assert.equal(nav.releaseBatch,null)
 }finally{nav.reset()}
})

test('勾选保存期间及完成后的旧项目轮询不覆盖新勾选，下一轮仍能读取当前偏好',async t=>{
 const requests:any[]=[]
 t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
 settleSharedComments(t,requests)
 setActivePinia(createPinia());const nav=useNavStore()
 try {
  nav.enter('p');const old=requests.find(r=>r.url.endsWith('/navigation'))
  nav.autoStepsVersion++;nav.autoStepsSaving=true;nav.autoSteps=['translate']
  nav.autoStepsVersion++;nav.autoStepsSaving=false
  old.resolve(new Response(JSON.stringify({title:'项目',auto_steps:['download']})));await flush()
  assert.deepEqual(nav.autoSteps,['translate'])
  requests.find(r=>r.url.endsWith('/steps')).resolve(new Response(JSON.stringify([
   {key:'download',selected:true,status:'done',fresh:true},{key:'translate',selected:false,status:'done',fresh:true},
  ])));await flush()
  assert.equal(nav.requiredStepCount,1);assert.equal(nav.stepMap.translate.selected,true)
  void nav.poll(false);const current=requests.filter(r=>r.url.endsWith('/navigation')).at(-1)
  current.resolve(new Response(JSON.stringify({title:'项目',auto_steps:['split']})));await flush()
  assert.deepEqual(nav.autoSteps,['split'])
 }finally{nav.reset()}
})

test('慢状态请求跨过多个轮询周期也能显示，快速项目信息独立呈现', async t => {
  t.mock.timers.enable({apis: ['setInterval']})
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => new Promise(resolve => requests.push({url, options, resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia())
  const nav = useNavStore()
  try {
    nav.enter('a')
    assert.equal(requests.length, 5)
    assert.equal(requests[0].url, '/api/projects/a/navigation')
    requests[0].resolve(new Response(JSON.stringify({title: '中文工程', auto_steps: ['tts'], chain:null})))
    await flush()
    assert.equal(nav.title, '中文工程')
    assert.deepEqual(nav.autoSteps, ['tts'])
    assert.equal(nav.stepsLoaded, false)
    t.mock.timers.tick(6000)
    assert.equal(requests.filter(r => r.url.endsWith('/steps')).length, 1)
    requests[1].resolve(new Response(JSON.stringify([{key: 'tts', label: '配音'}])))
    await flush()
    assert.equal(nav.stepsLoaded, true)
    assert.equal(nav.stepMap.tts.label, '配音')
    assert.equal(STEP_LABELS.download, '获取视频')
    assert.equal(requests.filter(r => r.url.endsWith('/steps')).length, 2)
  } finally { nav.reset() }
})

test('全局页暂停轮询并取消独占读取与共享读取消费者，保留快照，返回同项目立即恢复', async t=>{
  t.mock.timers.enable({apis:['setInterval']})
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    const uid='pause-fixture'
    nav.$patch({uid,title:'已读取的项目',status:'done',steps:[{key:'tts',selected:true,status:'done',fresh:true}]})
    nav.enter(uid)
    const request=(suffix:string)=>requests.find(request=>request.url.endsWith(suffix))
    request('/steps').resolve(new Response(JSON.stringify([{key:'tts',selected:true,status:'running',fresh:false,job_id:8}])))
    request('/workflow/status').resolve(new Response(JSON.stringify({run:{id:'r',status:'running'}})))
    await flush()
    assert.equal(requests.length,7,'五类状态之外还读取步骤日志与冻结流程计划')
    const project=request('/navigation'),publication=requests.find(request=>request.url.startsWith('/api/publishing/'))
    const activity=requests.find(request=>request.url.includes('/log?')),plan=request('/workflow'),comments=request('/comments/progress')
    nav.suspend()
    assert.equal(nav.suspended,true);assert.equal(nav.timer,0)
    assert.equal(nav.uid,uid);assert.equal(nav.title,'已读取的项目')
    assert.equal(nav.stepMap.tts.status,'running');assert.equal(nav.workflow.run.id,'r')
    for(const request of [project,publication,activity,plan]) assert.equal(request.options.signal.aborted,true)
    t.mock.timers.tick(9000)
    await nav.poll();await nav.refreshSteps();await nav.refreshWorkflow()
    assert.equal(requests.length,7)
    project.resolve(new Response(JSON.stringify({title:'迟到项目',status:'done'})))
    publication.resolve(new Response(JSON.stringify({status:'published'})))
    activity.resolve(new Response(JSON.stringify({lines:['[12:00:00] [任务#8] 迟到动态']})))
    plan.resolve(new Response(JSON.stringify({runs:[{id:'r',plan:{steps:['tts']}}]})))
    // 网络请求可被其他评论消费者共用，暂停只须保证本消费者不接收迟到结果。
    comments.resolve(new Response(JSON.stringify({workspace:{state:'done',label:'迟到评论结果'}})))
    await flush()
    assert.equal(nav.title,'已读取的项目');assert.equal(nav.publication,null)
    assert.deepEqual(nav.activities,{});assert.equal(nav.workflowPlan,null);assert.equal(nav.commentState,null)
    const beforeReturn=requests.length
    nav.enter(uid)
    assert.equal(nav.suspended,false);assert.ok(nav.timer)
    const resumed=requests.slice(beforeReturn)
    assert.equal(resumed.filter(request=>!request.url.endsWith('/comments/progress')).length,4,'同项目返回立即重读四类独占状态，评论可复用短时共享快照')
    resumed.find(request=>request.url.endsWith('/navigation')).resolve(new Response(JSON.stringify({title:'最新项目',status:'done',auto_steps:['tts'],chain:null})))
    resumed.find(request=>request.url.endsWith('/steps')).resolve(new Response(JSON.stringify([{key:'tts',selected:true,status:'done',fresh:true}])))
    resumed.find(request=>request.url.startsWith('/api/publishing/')).resolve(new Response(JSON.stringify({status:'submitted'})))
    resumed.find(request=>request.url.endsWith('/workflow/status')).resolve(new Response(JSON.stringify({run:null})))
    await flush()
    assert.equal(nav.title,'最新项目');assert.equal(nav.status,'done')
    assert.equal(nav.publication.status,'submitted')
  } finally {nav.reset()}
})

test('项目完成状态复用步骤有效性，迟到持久状态不覆盖设置过期与无选中结果', async t=>{
  let steps:any[]=[{key:'translate',selected:true,status:'done',fresh:true}]
  let projectResponse:any
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(url.endsWith('/navigation'))return new Promise(resolve=>{projectResponse=resolve})
    return new Response(JSON.stringify(url.endsWith('/steps')?steps:{run:null}))
  })
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.enter('a');await flush();assert.equal(nav.status,'done')
    steps=[{key:'translate',selected:true,status:'done',fresh:false}]
    await nav.refreshSteps();assert.equal(nav.status,'created')
    projectResponse(new Response(JSON.stringify({title:'当前项目',status:'done',auto_steps:['translate'],chain:null})))
    await flush();assert.equal(nav.status,'created','完整生成后的设置变化不能被旧持久status抹掉')
    for(const [data,expected] of [
      [[], 'created'],
      [[{key:'split',selected:false,status:'done',fresh:true}], 'created'],
      [[{key:'split',selected:true,status:'failed',fresh:false}], 'failed'],
      [[{key:'split',selected:false,status:'failed',fresh:false},{key:'translate',selected:true,status:'done',fresh:true}], 'done'],
      [[{key:'split',selected:false,status:'queued',fresh:false},{key:'translate',selected:true,status:'failed',fresh:false}], 'processing'],
    ] as [any[],string][]) {
      steps=data;await nav.refreshSteps();assert.equal(nav.status,expected)
    }
  } finally {nav.reset()}
})

test('切换工程丢弃旧响应，失败提供提示且重试能够恢复', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => new Promise(resolve => requests.push({url, options, resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia())
  const nav = useNavStore()
  try {
    nav.enter('old'); nav.enter('new')
    assert.equal(requests[1].options.signal.aborted, true)
    requests[1].resolve(new Response(JSON.stringify([{key: 'tts', label: '旧数据'}])))
    requests.find(r => r.url === '/api/projects/new/steps').resolve(new Response(JSON.stringify({message: '模拟错误'}), {status: 503}))
    await flush()
    assert.equal(nav.stepsLoaded, false)
    assert.equal(nav.steps.length, 0)
    assert.match(nav.stepsError, /步骤状态未更新/)
    void nav.poll()
    requests.filter(r => r.url === '/api/projects/new/steps').at(-1).resolve(new Response(JSON.stringify([{key: 'tts', label: '新数据'}])))
    await flush()
    assert.equal(nav.stepMap.tts.label, '新数据')
    assert.equal(nav.stepsError, '')
  } finally { nav.reset() }
})

test('发布状态独立轮询且上传锁设置，切换项目丢弃旧稿件，不参与制作完成数', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => new Promise(resolve => requests.push({url, options, resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia())
  const nav = useNavStore()
  try {
    nav.enter('old'); nav.enter('new')
    requests.find(r => r.url === '/api/publishing/projects/old/status').resolve(new Response(JSON.stringify({id:'old',status:'published'})))
    requests.find(r => r.url === '/api/publishing/projects/new/status').resolve(new Response(JSON.stringify({id:'new',status:'uploading'})))
    await flush()
    assert.equal(nav.publication.id, 'new')
    assert.equal(nav.busy, true)
    nav.publication = { id:'new', status:'monitoring' }
    assert.equal(nav.busy, false)
    nav.publication = { id:'new', status:'checking' }
    assert.equal(nav.busy, false)
    assert.equal(nav.requiredStepCount, 0)
    assert.equal(nav.doneCount, 0)
    nav.reset()
    assert.equal(nav.publication, null)
  } finally { nav.reset() }
})

test('成品刷新只读取步骤，不等待发布状态；删除回执先改列表并丢弃旧轮询', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => new Promise(resolve => requests.push({url, options, resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia())
  const nav = useNavStore()
  const missing = {id:1,name:'missing.mp4',exists:false}, kept = {id:2,name:'kept.mp4',exists:true,file_revision:'v2'}
  try {
    nav.$patch({uid:'p',steps:[{key:'assemble',artifacts:[missing,kept]}]})
    const old = nav.refreshSteps()
    assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/projects/p/steps')
    nav.outputRemoved('p',missing,true)
    assert.deepEqual(nav.stepMap.assemble.artifacts.map((a:any)=>a.id),[2])
    assert.equal(requests[0].options.signal.aborted,true)
    requests[0].resolve(new Response(JSON.stringify([{key:'assemble',artifacts:[missing,kept]}])))
    await old
    assert.deepEqual(nav.stepMap.assemble.artifacts.map((a:any)=>a.id),[2])
    nav.outputRemoved('p',kept,false)
    assert.equal(nav.stepMap.assemble.artifacts[0].exists,false)
    assert.equal(nav.stepMap.assemble.artifacts[0].file_revision,'')
    const fresh = nav.refreshSteps()
    requests[1].resolve(new Response(JSON.stringify([{key:'assemble',artifacts:[kept]}])))
    await fresh
    assert.equal(nav.stepMap.assemble.artifacts[0].exists,true, '后续真实刷新允许磁盘原位恢复')
    nav.outputRemoved('other',kept,true)
    assert.equal(nav.stepMap.assemble.artifacts.length,1)
  } finally {nav.reset()}
})

test('启动回执即时锁定对应步骤，慢轮询不能覆盖；跨项目回执不污染新页', async t => {
  const requests: any[] = []
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia());const nav=useNavStore()
  try {
    nav.$patch({uid:'p',steps:[{key:'assemble',status:'done',progress:100}]})
    const read=nav.refreshSteps()
    nav.stepStarted('p','assemble',{status:'started',job_id:8})
    assert.equal(nav.busy,true);assert.equal(nav.stepMap.assemble.job_id,8)
    requests[0].resolve(new Response(JSON.stringify([{key:'assemble',status:'done'}])))
    await read
    assert.equal(nav.stepMap.assemble.status,'queued')
    nav.stepStarted('other','assemble',{status:'started',job_id:9})
    nav.stepStarted('p','assemble',{status:'skipped'})
    assert.equal(nav.stepMap.assemble.job_id,8)
  } finally {nav.reset()}
})

test('自动流程覆盖制作间隙的忙碌状态；慢响应不回退启动回执，换项目不串状态',async t=>{
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia());const nav=useNavStore()
  try{
    nav.$patch({uid:'p',workflow:{mode:'publish',run:null},steps:[]})
    const old=nav.refreshWorkflow()
    nav.workflowStarted('p',{id:'r',status:'running',phase:'准备发布资料',recipe:{mode:'publish'},plan:{steps:['download']},cursor:1})
    assert.equal(nav.busy,true);assert.equal(nav.publicationDisplay.text,'准备发布资料')
    assert.equal(requests[0].options.signal.aborted,true)
    requests[0].resolve(new Response(JSON.stringify({mode:'publish',run:null})));await old
    assert.equal(nav.workflow.run.id,'r')
    nav.workflowStarted('other',{id:'foreign',status:'queued',recipe:{mode:'produce'}})
    assert.equal(nav.workflow.run.id,'r')
    nav.workflow.run.status='monitoring';assert.equal(nav.busy,false)
    nav.reset();assert.equal(nav.workflow,null)
  }finally{nav.reset()}
})

test('运行日志共享只读轮询，慢请求合并，任务切换和离开时丢弃旧动态', async t => {
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  settleSharedComments(t,requests)
  setActivePinia(createPinia());const nav=useNavStore()
  nav.$patch({uid:'p',steps:[]})
  const refresh = async (job:number,status='running') => {
    const promise=nav.refreshSteps()
    requests.filter(r=>r.url.endsWith('/steps')).at(-1).resolve(new Response(JSON.stringify([{key:'tts',label:'配音',status,job_id:job}])))
    await promise;await flush()
  }
  try {
    await refresh(8)
    const old=requests.find(r=>r.url.includes('/log?'))
    assert.equal(old.url,'/api/projects/p/steps/tts/log?tail=24')
    assert.equal(old.options.method,undefined)
    await refresh(8);await refresh(8)
    assert.equal(requests.filter(r=>r.url.includes('/log?')).length,1)
    await refresh(9)
    assert.equal(old.options.signal.aborted,true)
    old.resolve(new Response(JSON.stringify({lines:['[12:00:00] [任务#8] 旧任务']})))
    const newer=requests.filter(r=>r.url.includes('/log?')).at(-1)
    newer.resolve(new Response(JSON.stringify({lines:['[12:00:01] [任务#9] 第 25 句正在处理']})))
    await flush()
    assert.equal(nav.activityFor('tts').message,'第 25 句正在处理')
    assert.equal(nav.currentActivity,null)
    nav.$patch({workflow:{run:{id:'current',status:'running',cursor:0}},workflowPlan:{id:'current',steps:['tts']}})
    assert.equal(nav.currentActivity.message,'第 25 句正在处理')
    await refresh(9,'done')
    assert.equal(nav.activityFor('tts'),null)
    assert.deepEqual(nav.activities,{})
    await refresh(10)
    const leaving=requests.filter(r=>r.url.includes('/log?')).at(-1)
    nav.reset()
    assert.equal(leaving.options.signal.aborted,true)
    leaving.resolve(new Response(JSON.stringify({lines:['[12:00:02] [任务#10] 迟到动态']})))
    await flush();assert.deepEqual(nav.activities,{})
  } finally {nav.reset()}
})

test('冻结运行计划只读一次供所有页面共用，评论状态按实际游标前进', async t => {
  const urls:string[]=[]
  let cursor=0
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    urls.push(url)
    return new Response(JSON.stringify(url.endsWith('/status')
      ? {mode:'publish',run:{id:'r',status:'running',cursor}}
      : {runs:[{id:'r',plan:{steps:['tts','comments','assemble'],labels:{tts:'配音'}},recipe:{comment_limit:200,comment_count:30}}]}))
  })
  setActivePinia(createPinia());const nav=useNavStore();nav.$patch({uid:'p'})
  try {
    await nav.refreshWorkflow();await flush()
    assert.equal(nav.commentsProgress.state,'upcoming')
    cursor=1;await nav.refreshWorkflow();await flush()
    assert.equal(nav.commentsProgress.state,'running')
    cursor=2;await nav.refreshWorkflow();await flush()
    assert.equal(nav.commentsProgress.state,'done')
    assert.equal(urls.filter(u=>u.endsWith('/workflow')).length,1)
  } finally {nav.reset()}
})
