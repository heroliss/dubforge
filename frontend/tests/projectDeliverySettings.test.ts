import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, nextTick, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia } from 'pinia'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
let server:ViteDevServer
const components:Record<string,any>={}
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  for(const name of ['ProjectDeliverySettings','AssemblyVersionWorkspace','ConfigurationForm','PublicationUpdatePanel'])components[name]=(await server.ssrLoadModule(`/src/components/${name}.vue`)).default
  mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
})
after(async()=>{mock.restoreAll();await server.close()})
const versions=()=>[{id:'v1',name:'原声版',dubbing:false,burn_subtitles:false,include_comments:false,overrides:{}},
  {id:'v2',name:'配音版',dubbing:true,burn_subtitles:true,include_comments:false,overrides:{}}]
const state=()=>({revision:4,fingerprint:'f'.repeat(64),versions:versions(),fields:[],defaults:{},target_version_ids:['v1'],target_selection:{version_ids:['v1'],source:'explicit',needs_confirmation:false}})
const values=()=>({'run.mode':'upload','assembly.versions':versions(),'publish.project_target':{kind:'existing',publication_id:'original',replace_videos:false,replacements:[],sync_fields:['chapters']}})
function partSettings(ids:string[]|null=null){return {name:'项目',revision:8,fingerprint:'d'.repeat(64),overrides:{},
 defaults:{...values(),'publish.version_ids':ids,'publish.part_titles':{v1:'原声正片',v2:'中文配音'}},
 sections:{publication:{label:'发布',fields:[{path:'run.mode',label:'交付方式',type:'select',options:[]}]}}}}
function response(body:any,status=200){return new Response(JSON.stringify(body),{status,headers:{'content-type':'application/json'}})}
async function render(name:string,props:any,seed:(page:any,props:any)=>void=()=>{}){
  let page:any,componentProps:any,instance:any;const changes:any[]=[]
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(components[name],{...props,onChange:(path:string,value:any)=>changes.push({path,value})})})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){if(this.$options.__name===name){instance=this.$;page=this.$.setupState;componentProps=this.$.props;seed(page,componentProps)}}})
  return {html:await renderToString(app),page,props:componentProps,changes,instance}
}
test('发布步骤只维护四种交付方式，已有稿件不自动代入目标',async()=>{
  const {page,html,changes}=await render('ProjectDeliverySettings',{uid:'p',values:{...values(),'publish.project_target':null}},page=>{
    page.records=[{id:'existing',receipt:{bvid:'BV123'},snapshot:{settings:{title:'已有稿件'}}}]
  })
  assert.deepEqual(page.modes.map((row:any)=>row.value),['produce','draft','upload','publish'])
  assert.equal(page.target,null);assert.equal(changes.length,0)
  assert.match(html,/不会自动选择最近的BV/);assert.match(html,/项目交付方式/)
})

test('首次发布默认新稿可直接读取，说明不增加设置改动，也不从已有记录选择BV',async()=>{
  const input={...values(),'publish.project_target':{kind:'new'}}
  const {page,html,changes}=await render('ProjectDeliverySettings',{uid:'p',values:input,
    targetSelection:{source:'first_publish',has_history:false,explicit:false}})
  assert.match(html,/首次发布 · 默认创建新稿/);assert.match(html,/可直接按模板运行/)
  assert.equal(page.choice,'new');assert.deepEqual(page.target,{kind:'new'});assert.equal(changes.length,0)
})

test('已有发布记录待选和主动清空目标有不同说明，不因读取记录自动发出修改',async()=>{
  for(const source of ['history_requires_choice','explicit_unset']){
    const {page,html,changes}=await render('ProjectDeliverySettings',{uid:'p',values:{...values(),'publish.project_target':null},
      targetSelection:{source,has_history:true,explicit:source==='explicit_unset'}},page=>{
        page.records=[{id:'recent',receipt:{bvid:'BVRecent'}}]
      })
    assert.match(html,source==='explicit_unset'?/稿件目标已清空/:/项目已有发布或上传记录/)
    assert.equal(page.choice,'');assert.equal(page.target,null);assert.equal(changes.length,0)
    assert.doesNotMatch(html,/首次发布 · 默认创建新稿/)
  }
})

test('父级清空或移除原稿设置清理选项与只读上下文，选择更新仍可先留空再选择具体稿件',async()=>{
  const input=reactive<Record<string,any>>(values()),{page,changes}=await render('ProjectDeliverySettings',{uid:'p',values:input})
  page.context={remote:{videos:[{cid:120}]}};page.loadedPublication='original';page.contextLoading=true
  input['publish.project_target']=null;page.syncTargetChoice()
  assert.equal(page.choice,'');assert.equal(page.context,null);assert.equal(page.loadedPublication,'');assert.equal(page.contextLoading,false)
  assert.equal(changes.length,0,'同步父级目标不能反向写入覆盖')
  input['publish.project_target']={kind:'new'};page.syncTargetChoice();assert.equal(page.choice,'new')
  page.setChoice('existing');assert.equal(changes.at(-1).value,null)
  input['publish.project_target']=null;page.syncTargetChoice();assert.equal(page.choice,'existing')
  page.selectPublication('chosen');assert.equal(changes.at(-1).value?.publication_id,'chosen',JSON.stringify(changes))
  input['publish.project_target']=changes.at(-1).value;page.syncTargetChoice()
  assert.equal(page.choice,'existing');assert.equal(page.target.publication_id,'chosen')
  input['publish.project_target']=null;page.syncTargetChoice();assert.equal(page.choice,'')
})
test('原稿只同步章节明确无需制作，替换映射以CID和版本ID保存',async()=>{
  const input=values(),{page,changes,html}=await render('ProjectDeliverySettings',{uid:'p',values:input},page=>{
    page.context={remote:{videos:[{cid:120,title:'原视频'}]}};page.loadedPublication='original'
  })
  assert.match(html,/无需重新制作视频/)
  page.replacePart(120,'v2')
  assert.deepEqual(changes.at(-1),{path:'publish.project_target',value:{...input['publish.project_target'],replacements:[{old_cid:120,version_id:'v2'}]}})
  assert.deepEqual(input['publish.project_target'].replacements,[],'组件不能直接修改父级已保存状态')
  page.selectPublication('another')
  assert.deepEqual(changes.at(-1).value,{kind:'existing',publication_id:'another',replace_videos:false,replacements:[],sync_fields:[]})
})
test('原稿本地草稿的空同步范围有清晰提示，不默认增加替换或文案',async()=>{
  const input={...values(),'run.mode':'draft','publish.project_target':{...values()['publish.project_target'],sync_fields:[]}}
  const {page,html,changes}=await render('ProjectDeliverySettings',{uid:'p',values:input})
  assert.match(html,/尚未选择原稿更新范围/);assert.match(html,/运行预检会阻止空范围任务/)
  assert.equal(page.emptyUpdateScope,true);assert.equal(changes.length,0)
})
test('启用替换但没有明确映射时提示预检阻断，不从既有稿件猜测替换分P',async()=>{
  const input={...values(),'publish.project_target':{...values()['publish.project_target'],replace_videos:true,replacements:[]}}
  const {html,changes}=await render('ProjectDeliverySettings',{uid:'p',values:input})
  assert.match(html,/尚未选择要替换的分P与版本/);assert.match(html,/不会自动替换原视频/)
  assert.equal(changes.length,0)
})
test('读取原稿迟到结果不能覆盖已切换项目或目标的编辑',async t=>{
  const {page,props}=await render('ProjectDeliverySettings',{uid:'p',values:values()})
  let complete:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',()=>new Promise(resolve=>{complete=resolve}))
  const loading=page.loadContext()
  props.values={...values(),'publish.project_target':{...values()['publish.project_target'],publication_id:'another'}}
  complete(response({remote:{videos:[{cid:1}]}}));await loading
  assert.equal(page.context,null);assert.equal(page.loadedPublication,'')
})
test('切换项目清理只读忙态，离开后的迟到原稿读取也不复活状态',async t=>{
  const {page,props,instance}=await render('ProjectDeliverySettings',{uid:'p',values:values()})
  let complete:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',url=>String(url).includes('/updates/context')?new Promise(resolve=>{complete=resolve}):Promise.resolve(response([])))
  const loading=page.loadContext();assert.equal(page.contextLoading,true)
  // SSR 不保留组件 watch 调度器，直接调用切项目时绑定的同一清理处理器。
  props.uid='another';page.resetProjectContext();await nextTick();assert.equal(page.contextLoading,false);assert.equal(page.context,null)
  for(const hook of instance.bum || [])hook()
  complete(response({remote:{videos:[{cid:1}]}}));await loading
  assert.equal(page.context,null);assert.equal(page.loadedPublication,'')
})
test('项目制作目标与临时生成勾选独立，保存版本和目标共用CAS',async t=>{
  const data=state(),{page}=await render('AssemblyVersionWorkspace',{uid:'p',configureOnly:true},page=>{page.adopt(data)})
  page.toggle('v2',true)
  assert.deepEqual(page.selected,['v2']);assert.deepEqual(page.targetVersions,['v1']);assert.equal(page.dirty,false)
  page.setTargets(['v2']);assert.equal(page.dirty,true)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const body=JSON.parse(String(options?.body));calls.push(body)
    return response({...data,revision:5,target_version_ids:body.target_version_ids,versions:body.versions})
  })
  await page.save()
  assert.equal(calls.length,1);assert.deepEqual(calls[0].target_version_ids,['v2'])
  assert.equal(calls[0].revision,4);assert.equal(calls[0].fingerprint,data.fingerprint)
  assert.deepEqual(calls[0].versions,versions());assert.equal(page.dirty,false)
})
test('清空目标不能静默保存，CAS失败也保留目标草稿',async t=>{
  const {page}=await render('AssemblyVersionWorkspace',{uid:'p',configureOnly:true},page=>{page.adopt(state())})
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push(options);return response({message:'设置已变化，请重新读取'},409)})
  page.setTargets([]);await page.save();assert.equal(calls.length,0);assert.match(page.error,/至少选择一个/)
  page.setTargets(['v2']);await page.save();assert.deepEqual(page.targetVersions,['v2']);assert.equal(page.dirty,true);assert.match(page.error,/已变化/)
})
test('交付方式、明确稿件目标与其它发布设置通过同一次项目保存',async t=>{
  const data={name:'项目',revision:7,fingerprint:'c'.repeat(64),defaults:{...values(),'publish.version_ids':['v1']},overrides:{},sections:{publication:{label:'发布',fields:[{path:'run.mode',label:'交付方式',type:'select',options:[]}]}}}
  const {page,html}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
  assert.match(html,/项目交付目标/)
  assert.equal(page.styleGroups(data.sections.publication,'publication').flatMap((group:any)=>group.fields).some((field:any)=>field.path==='run.mode'),false,'通用字段不能重复呈现交付方式')
  page.change('run.mode','publish');page.change('publish.project_target',{kind:'new'});page.change('publish.version_ids',['v2'])
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT'){const body=JSON.parse(String(options.body));calls.push(body);return response({...data,revision:8,overrides:body.updates})}
    return response({})
  })
  await page.save()
  assert.equal(calls.length,1);assert.equal(calls[0].revision,7);assert.equal(calls[0].fingerprint,data.fingerprint)
  assert.deepEqual(calls[0].updates,{'run.mode':'publish','publish.project_target':{kind:'new'},'publish.version_ids':['v2']})
})

function firstPublicationSettings(hasHistory=false,overrides:Record<string,any>={}){
  return {...partSettings(['v1']),defaults:{...partSettings(['v1']).defaults,'run.mode':'publish','publish.project_target':null},
    values:{...values(),'publish.project_target':hasHistory?null:{kind:'new'}},overrides,
    target_selection:{source:Object.hasOwn(overrides,'publish.project_target')
      ?overrides['publish.project_target']?'project':'explicit_unset':hasHistory?'history_requires_choice':'first_publish',
      has_history:hasHistory,explicit:Object.hasOwn(overrides,'publish.project_target')}}
}

test('项目首次发布的有效默认不污染dirty或稀疏保存，保存其它字段后仍按历史解析',async t=>{
  const data=firstPublicationSettings(),{page,html}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
  assert.deepEqual(page.values['publish.project_target'],{kind:'new'});assert.equal(page.dirty,false)
  assert.equal(Object.hasOwn(page.patch,'publish.project_target'),false);assert.match(html,/首次发布 · 默认创建新稿/)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='PUT'){const body=JSON.parse(String(options.body));calls.push(body);return response({...data,revision:9,overrides:body.updates})}
    return response({})
  })
  page.change('publish.part_titles',{v1:'正片'});await page.save()
  assert.deepEqual(calls[0].updates,{'publish.part_titles':{v1:'正片'}});assert.deepEqual(calls[0].reset,[])
  assert.equal(page.dirty,false);assert.equal(Object.hasOwn(page.patch,'publish.project_target'),false)
  page.adopt(firstPublicationSettings(true));assert.equal(page.values['publish.project_target'],null)
  assert.equal(page.targetSelection.source,'history_requires_choice');assert.equal(page.dirty,false)
})

test('显式null保持清空，移除目标覆盖按客观历史恢复默认，不复用旧响应中的effective目标',async()=>{
  for(const hasHistory of [false,true]){
    const data=firstPublicationSettings(hasHistory,{'publish.project_target':null})
    const {page}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
    assert.equal(page.values['publish.project_target'],null);assert.equal(page.targetSelection.source,'explicit_unset');assert.equal(page.dirty,false)
    page.inherit('publish.project_target')
    assert.deepEqual(page.values['publish.project_target'],hasHistory?null:{kind:'new'})
    assert.equal(page.targetSelection.source,hasHistory?'history_requires_choice':'first_publish')
    assert.equal(Object.hasOwn(page.patch,'publish.project_target'),false);assert.equal(page.dirty,true)
    page.change('publish.project_target',{kind:'new'});assert.equal(page.targetSelection.source,'project')
    page.inherit('publish.project_target');assert.deepEqual(page.values['publish.project_target'],hasHistory?null:{kind:'new'})
  }
})

test('模板与全局不应用或展示项目首次发布目标，默认解析不会进入可复用设置',async()=>{
  for(const scope of ['global','series:template']){
    const {page,html}=await render('ConfigurationForm',{scope,sections:['publication']},page=>{page.adopt(firstPublicationSettings());page.loading=false})
    assert.equal(page.values['publish.project_target'],null);assert.equal(page.targetSelection,undefined)
    assert.equal(page.dirty,false);assert.doesNotMatch(html,/首次发布 · 默认创建新稿|项目稿件目标/)
  }
})

test('损坏恢复目标沿用后端无效判定，不能因无历史变成默认新稿，重新选择后才能修正',async()=>{
  const data={...firstPublicationSettings(false,{'publish.project_target':{kind:'existing'}}),
    target_selection:{source:'invalid',has_history:false,explicit:true,reason:'本项目稿件目标格式无效，请重新选择'}}
  const {page,html}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
  assert.equal(page.targetSelection.source,'invalid');assert.equal(page.values['publish.project_target'],null)
  assert.equal(page.dirty,false);assert.match(html,/保存的稿件目标格式无效/);assert.doesNotMatch(html,/首次发布 · 默认创建新稿/)
  page.change('publish.project_target',{kind:'new'});assert.equal(page.targetSelection.source,'project')
  assert.deepEqual(page.values['publish.project_target'],{kind:'new'});assert.equal(page.dirty,true)
})

test('分P设置按已保存顺序显示序号，首尾移动有边界并说明保存后发布生效',async()=>{
 const {page,html}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(partSettings(['v2','v1']));page.loading=false})
 assert.deepEqual(page.publishedVersions.map((row:any)=>row.id),['v2','v1'])
 assert.match(html,/aria-label="发布分P顺序"/);assert.match(html,/P1 · 配音版/);assert.match(html,/P2 · 原声版/)
 assert.match(html,/自动流程按下方 P1、P2/);assert.match(html,/保存设置后生效/);assert.match(html,/已有草稿请在发布记录中调整顺序/)
 assert.match(html,/<button\b[^>]*disabled[^>]*aria-label="上移 P1"/)
 assert.match(html,/<button\b[^>]*disabled[^>]*aria-label="下移 P2"/)
})

test('默认全部版本调整顺序会明确保存发布顺序，名称跟随版本且不重排成片定义',async()=>{
 const data=partSettings(),original=JSON.stringify(data)
 const {page}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
 page.movePublishedVersion('v2',-1)
 assert.deepEqual(page.values['publish.version_ids'],['v2','v1']);assert.equal(page.dirty,true)
 assert.deepEqual(page.publishedVersions.map((row:any)=>[row.id,page.values['publish.part_titles'][row.id]]),[['v2','中文配音'],['v1','原声正片']])
 assert.deepEqual(page.values['assembly.versions'],versions());assert.equal(JSON.stringify(data),original)
 assert.match(page.partOrderNotice,/配音版移至 P1/)
 page.movePublishedVersion('v2',1);assert.deepEqual(page.values['publish.version_ids'],['v1','v2'])
 page.adopt(data);assert.equal(page.dirty,false);assert.equal(page.partOrderNotice,'')
 assert.equal(page.values['publish.version_ids'],null)
})

test('调整分P顺序沿用项目CAS保存，重新读取仍保持相同顺序及名称',async t=>{
 const data=partSettings(['v1','v2']),{page}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
 page.movePublishedVersion('v1',1)
 const calls:any[]=[]
 t.mock.method(globalThis,'fetch',async(url,options)=>{
  if(options?.method==='PUT'){const body=JSON.parse(String(options.body));calls.push(body);return response({...data,revision:9,overrides:body.updates})}
  return response({})
 })
 await page.save()
 assert.equal(calls.length,1);assert.equal(calls[0].revision,8);assert.equal(calls[0].fingerprint,data.fingerprint)
 assert.deepEqual(calls[0].updates,{'publish.version_ids':['v2','v1']})
 assert.deepEqual(page.publishedVersions.map((row:any)=>row.id),['v2','v1']);assert.equal(page.values['publish.part_titles'].v2,'中文配音');assert.equal(page.dirty,false)
})

test('分P调整保存冲突保留顺序草稿，撤销清单恢复已保存配置',async t=>{
 const data=partSettings(['v1','v2']),{page}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(data);page.loading=false})
 page.movePublishedVersion('v2',-1)
 t.mock.method(globalThis,'fetch',async()=>response({message:'设置已变化，请重新核对'},409))
 await page.save();assert.deepEqual(page.values['publish.version_ids'],['v2','v1']);assert.equal(page.dirty,true);assert.match(page.error,/设置已变化/)
 page.inherit('publish.version_ids');assert.deepEqual(page.publishedVersions.map((row:any)=>row.id),['v1','v2']);assert.equal(page.dirty,false)
})

test('空清单、无效移动、首尾边界及保存忙态不能产生错误顺序',async()=>{
 const {page}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{page.adopt(partSettings(['v1','v2']));page.loading=false})
 for(const [id,delta] of [['v1',-1],['v2',1],['missing',1],['v1',2]])page.movePublishedVersion(id,delta)
 assert.equal(page.dirty,false)
 page.busy=true;page.movePublishedVersion('v2',-1);assert.equal(page.dirty,false)
 page.busy=false;page.adopt(partSettings([]));page.movePublishedVersion('v1',1);assert.deepEqual(page.values['publish.version_ids'],[]);assert.equal(page.dirty,false)
})

test('发布顺序或成片结构被运行锁占用时，上下移动不改变设置',async()=>{
 for(const locked of ['publish.version_ids','assembly.versions']){
  const {page}=await render('ConfigurationForm',{scope:'project:p',sections:['publication']},page=>{
   page.editability.isPathLocked=(path:string)=>path===locked;page.adopt(partSettings(['v1','v2']));page.loading=false
  })
  assert.equal(page.partOrderDisabled,true);page.movePublishedVersion('v2',-1)
  assert.deepEqual(page.values['publish.version_ids'],['v1','v2']);assert.equal(page.dirty,false)
 }
})
test('上传后等待确认的原稿更新只能显式submit，start与retry不能越过授权',async t=>{
  const publication={id:'p1',project_uid:'p',receipt:{bvid:'BV123'},snapshot:{account_name:'账号'}}
  const {page,html}=await render('PublicationUpdatePanel',{publication},page=>{page.task={id:'u1',status:'uploaded',delivery_mode:'upload',fingerprint:'confirmed',preview:{parts:[]},execution:{}}})
  assert.match(html,/确认提交原稿更新/);assert.doesNotMatch(html,/确认上传替换文件/)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url,body:JSON.parse(String(options?.body))});return response({...page.task,status:'queued'})})
  await page.perform('start');await page.perform('retry');assert.equal(calls.length,0)
  await page.perform('submit');assert.equal(calls.length,1)
  assert.deepEqual(calls[0].body,{action:'submit',confirmed:true,fingerprint:'confirmed'})
})
test('原稿本地准备方案不展示提交按钮，不能用start升级发布授权',async t=>{
  const {page,html}=await render('PublicationUpdatePanel',{publication:{id:'p1',project_uid:'p',receipt:{bvid:'BV123'},snapshot:{}}},page=>{
    page.task={id:'u1',status:'prepared',delivery_mode:'draft',fingerprint:'draft',preview:{parts:[]}}
  })
  assert.match(html,/去发布步骤调整交付方式/);assert.doesNotMatch(html,/确认更新原稿件<\/span>/)
  const calls:any[]=[];t.mock.method(globalThis,'fetch',async(...args)=>{calls.push(args);return response({})})
  await page.perform('start');assert.equal(calls.length,0);assert.equal(page.localDraft,true)
})
