import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defaultDubbingRunPolicy } from '../src/utils/dubbingRunPolicy.ts'

let server:ViteDevServer, Panel:any,ProjectPlan:any,CurrentProgress:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/BatchReleasePanel.vue')).default
  ProjectPlan=(await server.ssrLoadModule('/src/components/ReleaseProjectPlan.vue')).default
  CurrentProgress=(await server.ssrLoadModule('/src/components/ReleaseCurrentProgress.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('测试不允许真实平台请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
const versions=[{id:'original',name:'原声版',dubbing:false,burn_subtitles:true,include_comments:false},{id:'dub',name:'中文配音版',dubbing:true,burn_subtitles:true,include_comments:true}]
const rows=[{project_uid:'two',title:'后期',source_date:'2026-02-01',versions,selected_version_ids:['dub'],publication_action:'prepare'},
  {project_uid:'one',title:'前期',source_date:'2026-01-01',versions,selected_version_ids:['dub'],publication_action:'prepare'}]
const targets=rows.map(x=>({uid:x.project_uid,title:x.title}))
const scope={account_id:'12',season_id:34,section_id:56}
const batch=()=>({id:'batch',status:'ready',items:rows,scope,total:2,cursor:0})
const plan=()=>({fingerprint:'frozen',scope,items:rows,blockers:[],warnings:[]})
const reply=(data:any,status=200)=>({ok:status===200,status,json:async()=>data})
async function render(seed:(page:any)=>void=()=>{},props:any={}){
  let page:any
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(Panel,props)});app.use(router);setup(app)
  // 既有用例覆盖历史批次的冻结范围与回执恢复，新目标模式在下方单独验收。
  app.mixin({created(){if(this.$options.__name==='BatchReleasePanel'){page=this.$.setupState;page.targetDriven=false;seed(page)}}})
  const context:any={}
  const html=await renderToString(app,context)+Object.values(context.teleports || {}).join('');return {page,html}
}

test('统一父批次的子任务按parent或excludeIds隐藏，真正历史批次保留且完成计数不重复',async()=>{
  const entries=[{...batch(),id:'owned-by-id',status:'running'},
    {...batch(),id:'owned-by-parent',status:'running',parent_batch_id:'parent'},
    {...batch(),id:'owned-by-plan',status:'done',plan:{parent_batch_id:'parent'}},
    {...batch(),id:'history-running',status:'running'},
    {...batch(),id:'history-done',status:'done'}]
  const {page,html}=await render(page=>{page.batches=entries},{excludeIds:['owned-by-id']})
  assert.deepEqual(page.standaloneBatches.map((row:any)=>row.id),['history-running','history-done'])
  assert.deepEqual(page.visibleBatches.map((row:any)=>row.id),['history-running'])
  assert.equal(page.completedBatchCount,1)
  assert.match(html,/id="release-batch-history-running"/)
  assert.doesNotMatch(html,/id="release-batch-owned-by-|id="release-batch-history-done"/)
  const expanded=await render(page=>{page.batches=entries;page.showCompletedBatches=true},{excludeIds:['owned-by-id']})
  assert.match(expanded.html,/id="release-batch-history-done"/);assert.match(expanded.html,/id="release-batch-history-running"/)
  assert.doesNotMatch(expanded.html,/id="release-batch-owned-by-/)
})

test('仅有统一批次子任务时旧顺序发布列表整体不呈现第二套操作',async()=>{
  const {page,html}=await render(page=>{page.batches=[{...batch(),id:'child',status:'running',parent_batch_id:'parent'}]})
  assert.equal(page.standaloneBatches.length,0);assert.equal(page.completedBatchCount,0)
  assert.doesNotMatch(html,/aria-label="顺序发布批次"|id="release-batch-child"|本项完成后暂停|停止当前项/)
})
function fixtures(t:any,custom:(path:string,body:any)=>any=()=>undefined){
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    const path=String(url),body=options?.body&&JSON.parse(options.body);calls.push({path,body})
    const result=custom(path,body);if(result!==undefined)return result
    if(path==='/api/publishing/catalog')return reply({accounts:[{id:'12',name:'测试账号'}]})
    if(path.includes('/candidates?'))return reply({items:rows})
    if(path.endsWith('/metadata'))return reply({human_categories:[],declarations:[],seasons:[{id:34,title:'旅行合集',sections:[{id:56,title:'正篇'}]}]})
    if(path==='/api/release-batches/plan'||path==='/api/release-batches/batch/plan')return reply(plan())
    if(path==='/api/release-batches'&&body)return reply(batch())
    if(path==='/api/release-batches')return reply({batches:[batch()]})
    if(/\/batch\/(start|resume|pause|stop)$/.test(path))return reply({...batch(),status:'running'})
    throw new Error('意外请求 '+path)
  });return calls
}
async function prepare(page:any){
  await page.open(targets,false);page.season=34;page.section=56;page.orderConfirmed=true;await nextTick();await page.inspect()
}
test('打开默认按原始日期排序，检查仅预检，未确认素材权利不能启动',async t=>{
  const {page}=await render(),calls=fixtures(t);await prepare(page)
  assert.deepEqual(page.items.map((x:any)=>x.project_uid),['one','two'])
  assert.ok(page.preview);assert.equal(page.canStart,false);await page.start()
  assert.ok(calls.every(x=>!x.path.endsWith('/start')&&!(x.path==='/api/release-batches'&&x.body)))
  assert.deepEqual(calls.find(x=>x.path==='/api/release-batches/plan').body,{project_uids:['one','two'],scope,order_confirmed:true,
    version_ids:{one:['dub'],two:['dub']},publication_actions:{one:'prepare',two:'prepare'},dubbing:defaultDubbingRunPolicy()})
})

test('顺序发布新批次保存配音意图，原批次恢复只读不重分配策略',async t=>{
  const {page}=await render(),calls=fixtures(t),policy={...defaultDubbingRunPolicy(),mode:'all',target:'slot',slot:3}
  await prepare(page);page.rightsConfirmed=true;page.onDubbingChange(policy)
  assert.equal(page.preview,null);assert.equal(page.canStart,false)
  await nextTick();await page.inspect();page.rightsConfirmed=true;await page.start()
  assert.deepEqual(calls.find(call=>call.path==='/api/release-batches'&&call.body).body.dubbing,policy)
  await page.openBatch({...batch(),status:'paused',dubbing:policy})
  assert.deepEqual(page.dubbing,policy)
  page.onDubbingChange(defaultDubbingRunPolicy());assert.deepEqual(page.dubbing,policy)
  assert.ok(calls.filter(call=>call.path==='/api/release-batches/batch/plan').every(call=>call.body.dubbing===undefined),'原批次不重新传新槽意图')
})
test('确认后先持久化批次再启动，双击不重复创建或启动',async t=>{
  const {page}=await render();let release:()=>void=()=>{}
  const gate=new Promise<void>(resolve=>{release=resolve})
  const calls=fixtures(t,(path,body)=>path==='/api/release-batches'&&body?gate.then(()=>reply(batch())):undefined)
  await prepare(page);page.rightsConfirmed=true
  const first=page.start();await page.start();release();await first
  assert.equal(calls.filter(x=>x.path==='/api/release-batches'&&x.body).length,1)
  const starts=calls.filter(x=>x.path.endsWith('/start'));assert.equal(starts.length,1)
  assert.equal(starts[0].body.rights_confirmed,true);assert.equal(starts[0].body.fingerprint,'frozen')
})
test('开始响应丢失后保留批次和同一请求标识，重试不创建新批次',async t=>{
  const {page}=await render();let attempts=0
  const calls=fixtures(t,(path)=>path.endsWith('/start')&&++attempts===1?Promise.reject(new Error('网络回执丢失')):undefined)
  await prepare(page);page.rightsConfirmed=true;await page.start();assert.equal(page.existing.id,'batch')
  await page.start()
  assert.equal(calls.filter(x=>x.path==='/api/release-batches'&&x.body).length,1)
  const starts=calls.filter(x=>x.path.endsWith('/start'));assert.equal(starts.length,2)
  assert.equal(starts[0].body.request_id,starts[1].body.request_id)
})

test('旧候选读取未结束时切换已有批次，新的预检能完成且迟到响应无效',async t=>{
  const {page}=await render();let release:(data:any)=>void=()=>{}
  const waiting=new Promise(resolve=>{release=resolve})
  let reads=0
  const calls=fixtures(t,path=>path.includes('/candidates?') && ++reads===1 ? waiting:undefined)
  const old=page.open(targets,false);assert.equal(page.loading,true)
  await page.openBatch({...batch(),status:'paused'})
  assert.equal(page.loading,false);assert.ok(page.preview)
  release(reply({items:rows}));await old
  assert.equal(page.existing.id,'batch');assert.equal(page.loading,false)
  assert.equal(calls.filter(x=>x.path==='/api/release-batches/batch/plan').length,1)
})
test('打开已有批次后，恢复预检回填完整条目但保持原批次顺序',async t=>{
  const {page}=await render()
  const summary={...batch(),status:'attention',cursor:0,items:[
    {project_uid:'two',title:'后期',status:'attention',error:'字幕与配音失败'},
    {project_uid:'one',title:'前期',status:'done'},
  ]}
  const complete=[
    {...rows[0],status:'queued',publication_id:'pub-two',publication_action:'reuse_existing',selected_version_ids:['dub'],
      publication:{bvid:'BV1234567890',reuse_existing:true},production:{start_label:'字幕与配音',execute_steps:['tts'],reusable_steps:['download','asr']},recipe:{version_ids:['dub']}},
    {...rows[1],status:'done',publication_id:'pub-one',publication_action:'prepare',selected_version_ids:['original','dub'],
      publication:{id:'pub-one',reuse_existing:false},production:{start_label:'制作已就绪',execute_steps:[],reusable_steps:['download','asr','split','translate']},recipe:{version_ids:['original','dub']}},
  ]
  fixtures(t,path=>path==='/api/release-batches/batch/plan'?reply({...plan(),items:complete}):undefined)
  await page.openBatch(summary)
  assert.deepEqual(page.items.map((item:any)=>item.project_uid),['two','one'])
  assert.equal(page.items[0].publication_action,'reuse_existing')
  assert.equal(page.items[0].publication.bvid,'BV1234567890')
  assert.deepEqual(page.items[0].selected_version_ids,['dub'])
  assert.deepEqual(page.items[0].production.execute_steps,['tts'])
})

test('创建回执丢失后重新检查先找回原批次，不生成第二个创建请求',async t=>{
  const {page}=await render();let created=false
  const calls=fixtures(t,(path,body)=>{
    if(path==='/api/release-batches'&&body&&!created){created=true;return Promise.reject(new Error('Failed to fetch'))}
    if(path.startsWith('/api/release-batches?request_id='))return reply({batches:[batch()]})
  })
  await prepare(page);page.rightsConfirmed=true;await page.start()
  assert.equal(page.locked,true);assert.match(page.error,/后台任务可能仍在执行/)
  assert.doesNotMatch(page.error,/已被安全中断/)
  await page.inspect();assert.equal(page.existing.id,'batch');assert.ok(page.preview)
  page.rightsConfirmed=true;await page.start()
  assert.equal(calls.filter(x=>x.path==='/api/release-batches'&&x.body).length,1)
  assert.equal(calls.filter(x=>x.path.endsWith('/start')).length,1)
})

test('尚未找到创建回执时锁定方案，关闭再打开也沿用原创建标识重试',async t=>{
  const {page}=await render();let attempts=0
  const calls=fixtures(t,(path,body)=>{
    if(path==='/api/release-batches'&&body&&++attempts===1)return Promise.reject(new Error('Failed to fetch'))
    if(path.startsWith('/api/release-batches?request_id='))return reply({batches:[]})
  })
  await prepare(page);page.rightsConfirmed=true;await page.start()
  page.close();await page.open([{uid:'other',title:'其他'},{uid:'four',title:'第四'}])
  assert.equal(page.locked,true);assert.deepEqual(page.items.map((x:any)=>x.project_uid),['one','two'])
  assert.ok(page.preview);assert.equal(page.rightsConfirmed,false)
  page.rightsConfirmed=true;await page.start()
  const creates=calls.filter(x=>x.path==='/api/release-batches'&&x.body)
  assert.equal(creates.length,2);assert.deepEqual(creates[0].body,creates[1].body)
})
test('手动调整顺序后原预检与权利确认失效，不能越范围启动',async t=>{
  const {page}=await render(),calls=fixtures(t);await prepare(page);page.rightsConfirmed=true
  page.move(1,-1);await nextTick();assert.equal(page.preview,null);assert.equal(page.orderConfirmed,false)
  assert.equal(page.rightsConfirmed,false);await page.start();assert.equal(calls.filter(x=>x.path.endsWith('/start')).length,0)
})
test('暂停批次只发暂停请求，恢复先重检且沿用原批次',async t=>{
  const {page}=await render(),calls=fixtures(t)
  await page.pause({...batch(),status:'running'})
  assert.equal(calls.filter(x=>x.path.endsWith('/pause')).length,1)
  await page.openBatch({...batch(),status:'paused'});assert.equal(page.locked,true);assert.ok(page.preview)
  page.rightsConfirmed=true;await page.start()
  assert.equal(calls.filter(x=>x.path.endsWith('/resume')).length,1)
  assert.equal(calls.filter(x=>x.path==='/api/release-batches'&&x.body).length,0)
})
test('有旧退回稿与修订草稿时由用户明确选择，冻结稿件选择且修改后重检',async t=>{
  const {page}=await render()
  const choices=[{id:'rejected',title:'旧稿',status:'rejected',bvid:'BV1234567890'},
    {id:'review-draft',title:'审核修订草稿',status:'draft',is_review_revision:true,created_at:'2026-10-08T01:00:00+00:00'}]
  const calls=fixtures(t,path=>path.includes('/candidates?')?reply({items:[{...rows[0],publication_choices:choices},rows[1]]}):undefined)
  await page.open(targets,false);assert.equal(page.publications.two,undefined)
  assert.match(page.publicationOptions(page.items.find((p:any)=>p.project_uid==='two'))[1].label,/审核修订稿 · 审核修订草稿/)
  page.selectPublication('two','review-draft');page.season=34;page.section=56;page.orderConfirmed=true
  await nextTick();await page.inspect();page.rightsConfirmed=true
  page.selectPublication('two','rejected');assert.equal(page.preview,null);assert.equal(page.rightsConfirmed,false)
  page.selectPublication('two','review-draft');await nextTick();await page.inspect();page.rightsConfirmed=true;await page.start()
  const inspected=calls.find(x=>x.path==='/api/release-batches/plan').body
  assert.deepEqual(inspected.publication_ids,{two:'review-draft'})
  assert.deepEqual(calls.find(x=>x.path==='/api/release-batches'&&x.body).body.publication_ids,inspected.publication_ids)
  page.selectPublication('two','rejected');assert.equal(page.publications.two,'review-draft')
})

test('写入前的迟到列表不覆盖启动结果，写入后再读一次真实状态',async t=>{
  const {page}=await render();let release:(data:any)=>void=()=>{},reads=0
  const waiting=new Promise(resolve=>{release=resolve})
  fixtures(t,(path,body)=>path==='/api/release-batches'&&!body
    ? (++reads===1 ? waiting : reply({batches:[{...batch(),status:'running'}]})) : undefined)
  const old=page.load();await prepare(page);page.rightsConfirmed=true;await page.start()
  release(reply({batches:[batch()]}));await old
  for(let i=0;i<5;i++)await nextTick()
  assert.equal(reads,2);assert.equal(page.batches[0].status,'running')
})
test('后台轮询静默读取，手动刷新显示忙态，重复点击合并同一读取',async t=>{
  const {page}=await render();let release:any,reads=0
  fixtures(t,(path,body)=>path==='/api/release-batches'&&!body ? (reads++,new Promise(resolve=>{release=resolve})) : undefined)
  const quiet=page.load(true);assert.equal(page.listLoading,false)
  const manual=page.load();assert.equal(page.listLoading,true);const duplicate=page.load();assert.equal(reads,1)
  release(reply({batches:[{...batch(),status:'running'}]}));await Promise.all([quiet,manual,duplicate]);assert.equal(page.listLoading,false)
  const poll=page.load(true);assert.equal(page.listLoading,false);release(reply({batches:[{...batch(),status:'running'}]}));await poll
})

test('账号读取失败不被显示为未连接账号',async t=>{
  const {page}=await render()
  fixtures(t,path=>path==='/api/publishing/catalog'?Promise.reject(new Error('Failed to fetch')):undefined)
  await page.open(targets,false);assert.equal(page.catalogLoaded,false)
  assert.match(page.error,/无法读取顺序发布的最新状态/)
  assert.doesNotMatch(page.error,/后台任务可能仍在执行/)
})

test('预检明确区分复用原BV、继续草稿和发布新稿，不把已有稿件说成自动覆盖',async t=>{
  const {page}=await render()
  const original={...rows[0],publication_id:'original',publication:{bvid:'BV1mxpw6CEFi',reuse_existing:true}}
  const calls=fixtures(t,path=>path==='/api/release-batches/plan'?reply({...plan(),items:[original,rows[1]]}):undefined)
  await prepare(page)
  assert.equal(page.publicationAction(page.preview.items[0]),'复用原BV · 仅核对审核、合集与排列顺序')
  assert.equal(page.publicationAction({publication_id:'draft'}),'继续已有草稿 · 投稿后生成新BV')
  assert.equal(page.publicationAction({}),'补齐制作并发布新稿')
  assert.ok(calls.every(x=>!x.path.endsWith('/start')&&!(x.path==='/api/release-batches'&&x.body)))
})
test('关闭时取消预检，迟到响应不能重新填充旧方案',async t=>{
  const {page}=await render();let release:(data:any)=>void=()=>{}
  const waiting=new Promise(resolve=>{release=resolve})
  fixtures(t,path=>path==='/api/release-batches/plan'?waiting:undefined)
  await page.open(targets,false);page.season=34;page.section=56;page.orderConfirmed=true;await nextTick()
  const pending=page.inspect();page.close();release(reply(plan()));await pending
  assert.equal(page.show,false);assert.equal(page.preview,null)
})
test('停止当前项只停止同一批次，不发启动或新的投稿请求',async t=>{
  const {page}=await render(),calls=fixtures(t)
  await page.stop({...batch(),status:'pausing'})
  assert.deepEqual(calls.filter(x=>x.body).map(x=>x.path),['/api/release-batches/batch/stop'])
})
test('空草稿自动准备项目默认版本，改分P后原方案失效且确认完整冻结选择',async t=>{
  const {page}=await render()
  const calls=fixtures(t,path=>path.includes('/candidates?')?reply({items:rows.map(row=>({...row,publication_choices:[{id:row.project_uid+'-draft',title:'空草稿',status:'draft'}]}))}):undefined)
  await prepare(page)
  assert.deepEqual(page.versions,{one:['dub'],two:['dub']})
  assert.deepEqual(page.publications,{one:'one-draft',two:'two-draft'})
  assert.equal(page.publicationActions.one,'prepare');page.rightsConfirmed=true
  page.selectVersion('one','original',true)
  assert.equal(page.preview,null);assert.equal(page.rightsConfirmed,false)
  assert.deepEqual(page.versions.one,['original','dub'])
  await nextTick();await page.inspect();page.rightsConfirmed=true;await page.start()
  const creation=calls.find(call=>call.path==='/api/release-batches'&&call.body).body
  assert.deepEqual(creation.version_ids,{one:['original','dub'],two:['dub']})
  assert.deepEqual(creation.publication_actions,{one:'prepare',two:'prepare'})
  page.selectVersion('one','dub',false);assert.deepEqual(page.versions.one,['original','dub'])
})
test('选BV与已上传记录时不允许更换版本，切回修订草稿才能重新制作',async t=>{
  const {page}=await render()
  const choices=[{id:'bv',title:'原稿',status:'rejected',bvid:'BV1234567890'},{id:'draft',title:'修订',status:'draft'},{id:'upload',title:'上传清单',status:'uploaded'}]
  fixtures(t,path=>path.includes('/candidates?')?reply({items:[{...rows[0],publication_choices:choices},rows[1]]}):undefined)
  await page.open(targets,false);page.selectPublication('two','bv')
  assert.equal(page.publicationActions.two,'reuse_existing');page.selectVersion('two','original',true)
  assert.deepEqual(page.versions.two,['dub'])
  page.selectPublication('two','upload');assert.equal(page.publicationActions.two,'continue')
  page.selectVersion('two','dub',false);assert.deepEqual(page.versions.two,['dub'])
  page.selectPublication('two','draft');assert.equal(page.publicationActions.two,'prepare')
  page.selectVersion('two','original',true);assert.deepEqual(page.versions.two,['original','dub'])
})
test('整理多余草稿后刷新候选保留手动顺序和版本，消失的记录重新选择且需再确认',async t=>{
  const {page}=await render();let reads=0
  const calls=fixtures(t,path=>path.includes('/candidates?')?reply({items:rows.map(row=>({...row,publication_choices:++reads<=2
    ? [{id:'old-'+row.project_uid,title:'多余草稿',status:'draft'},{id:'new-'+row.project_uid,title:'保留草稿',status:'draft'}]
    : [{id:'new-'+row.project_uid,title:'保留草稿',status:'draft'}]}))}):undefined)
  await page.open(targets,false);page.move(1,-1);page.selectVersion('one','original',true)
  page.selectPublication('one','old-one');page.selectPublication('two','old-two')
  page.season=34;page.section=56;page.orderConfirmed=true;await nextTick();await page.inspect();page.rightsConfirmed=true
  await page.refreshCandidates()
  assert.deepEqual(page.items.map((item:any)=>item.project_uid),['two','one']);assert.deepEqual(page.versions.one,['original','dub'])
  assert.deepEqual(page.publications,{two:'new-two',one:'new-one'});assert.equal(page.preview,null);assert.equal(page.orderConfirmed,false);assert.equal(page.rightsConfirmed,false)
  assert.ok(calls.every(call=>!call.path.endsWith('/start')&&!(call.path==='/api/release-batches'&&call.body)))
})
test('批次列表显示真实发布入口、失败原因和已有稿件核对入口',async()=>{
  const {html}=await render(page=>{page.batches=[{...batch(),status:'attention',cursor:0,phase:'等待核对',error:'投稿结果不明',items:[{...rows[0],status:'attention',publication_id:'pub',error:'请核对回执',failed_step:'字幕与配音',error_code:'TEXT_ALIGNMENT',suggestion:'请按当前语义整理结果重新翻译'}]}]})
  assert.match(html,/顺序发布/);assert.match(html,/检查并继续/);assert.match(html,/投稿结果不明/);assert.match(html,/核对现有稿件/)
  assert.match(html,/第 1\/2 项/);assert.match(html,/后续 1 项未开始/);assert.match(html,/失败步骤：字幕与配音/);assert.match(html,/错误码：TEXT_ALIGNMENT/);assert.match(html,/建议：请按当前语义整理结果重新翻译/)
})
test('批次操作固定在独立工具栏，展开进度不会把按钮放入长列表中间',async()=>{
  const {html}=await render(page=>{page.batches=[{...batch(),status:'running',phase:'第1/7项：旅行',items:rows,total:7}]})
  assert.match(html,/项目完成 0\/7/);assert.match(html,/暂停：完成本项目后等待/);assert.match(html,/停止：请求终止当前任务/)
  const toolbar=html.match(/<div class="batch-toolbar"[^>]*>[\s\S]*?<\/div><p[\s\S]*?<\/p><\/div>/)?.[0]
  assert.ok(toolbar);assert.match(toolbar!,/本项完成后暂停|停止当前项/);assert.doesNotMatch(toolbar!,/<details|batch-items/)
  assert.ok(html.indexOf('class="batch-toolbar"')<html.indexOf('class="batch-progress"'))
  assert.match(html,/current-item/)
  const stopping=await render(page=>{page.batches=[{...batch(),status:'stopping',phase:'等待收尾'}]})
  assert.match(stopping.html,/正在停止当前任务并等待已发送操作的回执/)
  assert.doesNotMatch(stopping.html,/>本项完成后暂停<|>停止当前项<|>检查并继续…</)
})
test('阶段进度真实显示步骤、上传和估算，审核等待不造百分比，最终排序区分批次收尾',async()=>{
  async function progress(data:any) {const app=createSSRApp({render:()=>h(CurrentProgress,{progress:data,total:7})});setup(app);return renderToString(app)}
  const production=await progress({position:1,title:'旅行',stage_label:'语义整理',completed_steps:2,total_steps:7,percentage:42.7,estimated:true,eta_seconds:60,progress_label:'正在接收LLM输出'})
  assert.match(production,/当前项目 1\/7/);assert.match(production,/2\/7 步已完成/);assert.match(production,/本阶段估算 42.7%/)
  assert.match(production,/当前阶段预计剩余约 1 分钟/);assert.match(production,/不代表整项或整批剩余时间/)
  const upload=await progress({position:1,title:'旅行',stage_label:'上传视频',percentage:25,estimated:false,progress_label:'P1 / 2 · 已上传250 / 1000字节'})
  assert.match(upload,/本阶段 25%/);assert.doesNotMatch(upload,/本阶段估算/)
  const review=await progress({position:1,title:'旅行',stage_label:'等待B站审核',percentage:null,indeterminate:true,progress_label:'平台审核中'})
  assert.match(review,/data-execution-state="running"/);assert.match(review,/进度暂不可计算/);assert.doesNotMatch(review,/本阶段 0%|aria-valuenow="/)
  const sorting=await progress({position:null,title:'合集排列',stage_label:'核对合集顺序',percentage:null,indeterminate:true})
  assert.match(sorting,/批次收尾/);assert.doesNotMatch(sorting,/当前项目 7\/7/)
})
test('预检通过后收起编辑区，展开查看不丢确认，实际修改才要求重新检查',async t=>{
  const {page}=await render();fixtures(t)
  await page.open(targets,false);assert.equal(page.selectionExpanded,true)
  page.season=34;page.section=56;page.orderConfirmed=true;await nextTick();await page.inspect()
  assert.equal(page.selectionExpanded,false);page.rightsConfirmed=true;assert.equal(page.canStart,true)
  page.selectionToggled({target:{open:true}});assert.equal(page.selectionExpanded,true);assert.ok(page.preview);assert.equal(page.canStart,true)
  page.selectVersion('one','original',true);assert.equal(page.preview,null);assert.equal(page.rightsConfirmed,false);assert.equal(page.selectionExpanded,true)
  await nextTick();await page.inspect();assert.equal(page.selectionExpanded,false)
})
test('预检有阻断保持编辑区展开，不隐藏需要修正的稿件和版本',async t=>{
  const {page}=await render();fixtures(t,path=>path==='/api/release-batches/plan'?reply({...plan(),blockers:['请选择稿件']}):undefined)
  await prepare(page);assert.equal(page.selectionExpanded,true);page.rightsConfirmed=true;assert.equal(page.canStart,false)
})
test('项目计划默认只显示动作起点与计数，完整步骤在折叠详情中且原BV不误示制作',async()=>{
  async function compact(item:any) {
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
    const app=createSSRApp({render:()=>h(ProjectPlan,{item,index:0})});app.use(router);setup(app);return renderToString(app)
  }
  const production={start_label:'语义整理',execute_steps:['split','translate','tts'],reusable_steps:['download','asr'],rows:[{key:'download',label:'下载视频',action:'reuse',reason:'指纹匹配'},{key:'split',label:'语义整理',action:'retry',reason:'重试失败步骤'}]}
  const html=await compact({...rows[0],publication_action:'prepare',production,plan:{version_specs:versions},recipe:{part_titles:{dub:'中文<script>配音</script>'}}})
  assert.match(html,/从「语义整理」开始/);assert.match(html,/执行 3 步/);assert.match(html,/复用 2 步/);assert.match(html,/2 个分P/)
  assert.match(html,/中文&lt;script&gt;配音&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>配音/)
  const details=html.match(/<details[^>]+class="plan-details"[^>]*>/)?.[0];assert.ok(details);assert.doesNotMatch(details!,/\bopen/)
  assert.match(html,/查看步骤与分P详情/);assert.match(html,/旧成片引用不会直接上传/)
  const original=await compact({...rows[0],publication_action:'reuse_existing',publication:{bvid:'BV1234567890',reuse_existing:true},production})
  assert.match(original,/保留原BV，仅核对并排序/);assert.match(original,/沿用原稿视频/);assert.doesNotMatch(original,/执行 3 步|data-testid="recovery-plan"/)
})


test('继续批次从身份匹配预检展示账号合集小节，失败步骤使用中文',async()=>{
  const frozen={...batch(),status:'paused',items:[{...rows[0],failed_step:'split',status:'failed'}],
    plan:{items:[{plan:{settings_display:{account_id:'12',season_id:34,section_id:56,account_name:'旅行账号',season_title:'旅行合集',section_title:'正篇',source:'cache'}}}]}}
  const {page,html}=await render(page=>{page.show=true;page.existing=frozen;page.batches=[frozen];
    page.account='12';page.season=34;page.section=56;page.items=frozen.items})
  assert.match(html,/旅行账号/);assert.match(html,/旅行合集/);assert.match(html,/正篇/)
  assert.match(html,/失败步骤：语义整理/);assert.doesNotMatch(html,/失败步骤：split/)
  assert.equal(page.publicationOptions({publication_choices:[{id:'p',title:'测试稿',status:'future_status'}]})[0].label,'测试稿 · 状态待核对')
})

const targetRecipe=()=>({target_driven:true,mode:'publish',delivery_mode:'publish',production_version_ids:['original','dub'],version_ids:['dub'],part_titles:{dub:'最终发布版'},account_id:'12'})
const savedRows=()=>rows.map(row=>({...row,target_recipe:targetRecipe(),saved_scope:scope,blockers:[],project_goal:{production_version_ids:['original','dub'],target_kind:'new'}}))
const goalPlan=()=>({...plan(),target_driven:true,items:savedRows().map(row=>({...row,recipe:row.target_recipe,plan:{version_specs:versions}}))})
test('新建顺序发布只读项目设置，仅确认项目顺序，不提交第二份账号版本或配音配置',async t=>{
  const {page}=await render(),calls=fixtures(t,(path,body)=>{
    if(path.includes('/candidates?'))return reply({items:savedRows(),derived_scope:scope})
    if(path==='/api/release-batches/plan')return reply(goalPlan())
    if(path==='/api/release-batches'&&body)return reply({...batch(),plan:goalPlan()})
  })
  await page.open(targets);assert.equal(page.targetDriven,true)
  assert.ok(calls.some(call=>call.path.includes('target_driven=true')))
  assert.deepEqual(page.scope,scope)
  const before=JSON.stringify(page.versions);page.selectVersion('one','original',true);assert.equal(JSON.stringify(page.versions),before)
  page.orderConfirmed=true;await nextTick();await page.inspect()
  assert.deepEqual(calls.find(call=>call.path==='/api/release-batches/plan').body,{target_driven:true,project_uids:['one','two'],order_confirmed:true})
  page.rightsConfirmed=true;await page.start()
  const creation=calls.find(call=>call.path==='/api/release-batches'&&call.body).body
  assert.equal(creation.target_driven,true)
  for(const field of ['scope','version_ids','publication_ids','publication_actions','dubbing'])assert.equal(Object.hasOwn(creation,field),false)
  assert.ok(!calls.some(call=>call.path.endsWith('/metadata')),'批次不维护第二份账号合集编辑器')
})
test('新顺序发布不升级produce、draft或upload终点，阻断项引导回对应发布步骤',async t=>{
  const {page}=await render(),candidates=savedRows();candidates[0].target_recipe={...targetRecipe(),mode:'upload',delivery_mode:'upload'}
  const calls=fixtures(t,path=>path.includes('/candidates?')?reply({items:candidates,derived_scope:scope}):path==='/api/release-batches/plan'?reply({...goalPlan(),items:candidates.map(row=>({...row,recipe:row.target_recipe})),blockers:['后期：请在发布步骤设为上传并提交']}):undefined)
  await page.open(targets);page.orderConfirmed=true;await nextTick();await page.inspect();page.rightsConfirmed=true;await page.start()
  assert.equal(page.canStart,false);assert.equal(page.itemMode(page.items.find((item:any)=>item.project_uid==='two')),'upload')
  assert.ok(calls.every(call=>!call.path.endsWith('/start')&&!(call.path==='/api/release-batches'&&call.body)))
  const {html}=await render(page=>{page.targetDriven=true;page.show=true;page.items=candidates;page.catalog={accounts:[{id:'12',name:'项目账号'}]};page.account='12';page.season=34;page.section=56})
  assert.match(html,/批次不会自动升级交付方式/);assert.match(html,/\/projects\/two\/publish#project-rules/)
  assert.doesNotMatch(html,/class="release-target"|本次发布版本 · 每个勾选版本对应一个分P/)
})
test('新目标批次恢复沿用冻结配方，当前步骤设置变化不扩大授权',async t=>{
  const {page}=await render(),frozen={...batch(),status:'paused',plan:goalPlan(),items:goalPlan().items}
  const changed=savedRows().map(row=>({...row,target_recipe:{...targetRecipe(),mode:'produce',delivery_mode:'produce'}}))
  const calls=fixtures(t,path=>path.includes('/candidates?')?reply({items:changed,derived_scope:scope}):path==='/api/release-batches/batch/plan'?reply(goalPlan()):undefined)
  await page.openBatch(frozen)
  assert.equal(page.targetDriven,true);assert.equal(page.itemMode(page.items[0]),'publish')
  assert.deepEqual(calls.find(call=>call.path==='/api/release-batches/batch/plan').body,{})
  assert.equal(page.draftSelectionLocked,true)
})
test('未明确制作目标或稿件目标时如实提示，只同步已选原稿才标为无需制作',async()=>{
  const {page,html}=await render(page=>{page.targetDriven=true;page.show=true;page.items=[{...rows[0],blockers:['请到发布步骤设为上传并提交'],target_recipe:{mode:'produce',delivery_mode:'produce',production_version_ids:[]},project_goal:{target_kind:''}}]})
  assert.match(html,/制作目标待确认/);assert.doesNotMatch(html,/创建新稿 · 新BV|无制作目标（仅交付）/)
  assert.doesNotMatch(html,/批次不会自动升级交付方式/,'候选阻断已有同义说明时不重复提示')
  assert.equal(page.goalTarget({target_recipe:{mode:'publish'},project_goal:{target_kind:''}}),'未设置稿件目标')
  assert.equal(page.goalNames({target_recipe:{mode:'update',update:{replace_videos:false,sync_fields:['chapters']},production_version_ids:[]}}),'无需制作（只同步原稿资料）')
})
test('顺序发布预检分别列制作目标与真正发布分P，不把额外制作版本显示为额外P',async()=>{
  const item=goalPlan().items[0],router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(ProjectPlan,{item,index:0})});app.use(router);setup(app)
  const html=await renderToString(app)
  assert.match(html,/制作与复用目标：原声版、中文配音版/);assert.match(html,/1 个分P · 最终发布版/)
  assert.doesNotMatch(html,/P2 · 原声版|2 个分P/)
})
