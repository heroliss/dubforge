import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'
import { createPinia } from 'pinia'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'
import { versionSpec, effectiveVersion, workflowVersionSelection, assemblyFormalOutputSignature, mergeAssemblyOutputMetadata } from '../src/utils/assemblyVersions.ts'
let server: ViteDevServer, Workspace: any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  ;({default:Workspace}=await server.ssrLoadModule('/src/components/AssemblyVersionWorkspace.vue'))
  mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
})
after(async()=>{mock.restoreAll();await server.close()})
function data(){return {revision:1,template_revision:0,template_versions:[],defaults:{'assemble.subtitle_track':'trans','comments.background_opacity':40},sources:{'assemble.subtitle_track':{kind:'global'}},
  fingerprint:'fp-1',
  fields:[{path:'assemble.subtitle_track',label:'字幕语言',group:'字幕',type:'select',options:[{label:'译文',value:'trans'},{label:'双语',value:'bilingual'}]}],
  versions:[{id:'v1',name:'中文原声',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{},status:'已是最新',fresh:true},
    {id:'v2',name:'双语原声',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{'assemble.subtitle_track':'bilingual'},status:'需更新'}]}}
async function render(seed:(page:any,props:any)=>void=()=>{}, configureOnly=true){
  let page:any,props:any;const dialogs:any[]=[]
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const Probe={setup(){useDialog().warning=((opts:any)=>{dialogs.push(opts);return{destroy(){}}}) as any;return()=>h(Workspace,{uid:'fixture',configureOnly})}}
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Probe)})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){if(this.$options.__name==='AssemblyVersionWorkspace'){page=this.$.setupState;props=this.$.props;page.adopt(data());page.ready=true;seed(page,props)}}})
  return {html:await renderToString(app),page,dialogs,props}
}
test('版本定义不复制派生值，显式false/0覆盖保持，默认变更只影响继承项',()=>{
  const spec=versionSpec({...data().versions[0],overrides:{'comments.background_opacity':0,'assemble.loudnorm':false},values:{secret:'not-copy'}})
  assert.equal('values' in spec,false)
  const effective=effectiveVersion(spec,{'comments.background_opacity':80,'assemble.loudnorm':true})
  assert.equal(effective['comments.background_opacity'],0);assert.equal(effective['assemble.loudnorm'],false)
  assert.deepEqual(workflowVersionSelection({variants:['src']},[spec]),['v1'])
  assert.deepEqual(workflowVersionSelection({version_ids:[]},[spec]),[])
})

test('版本清单名称是唯一基准选择，生成勾选与检查基准分离', async () => {
  const { page, html } = await render(() => {}, false)
  assert.match(html, /点击版本名称选择当前基准成片/)
  assert.match(html, /当前基准/)
  const before = page.activeId
  page.active.name = `${page.active.name}（未保存）`
  page.baselineChanged('v2')
  assert.equal(page.activeId, before, '有未保存调整时不能让基准与编辑版本脱节')
})

test('片段重制确认后自动保存预检并生成独立版本，基准定义保持不变',async t=>{
  const {page,dialogs}=await render(), calls:any[]=[]
  const original=JSON.stringify(page.versions)
  t.mock.method(page.nav,'stepStarted',()=>{})
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const body=options?.body?JSON.parse(String(options.body)):null
    calls.push({path:String(url),method:options?.method,body})
    if(options?.method==='PUT')return response({...data(),revision:2,versions:body.versions})
    if(String(url).endsWith('/plan'))return response({fingerprint:'partial-confirmed',blockers:[]})
    if(String(url).endsWith('/run'))return response({job_id:43})
    throw new Error('片段重制不应读取其他资料')
  })
  await page.partialCreated({id:'partial-child',name:'两句修订',dubbing:false,burn_subtitles:true,include_comments:false,
    overrides:{'assemble.crf':18},partial:{plan_id:'a'.repeat(32)},
    partial_summary:{replace_audio:false,ranges:[{start:5,end:7}]}})
  assert.equal(page.activeId,'partial-child');assert.deepEqual(page.selected,['partial-child'])
  assert.equal(page.dirty,false);assert.equal(page.preservesAudio,true)
  assert.deepEqual(calls.map(row=>row.method==='PUT'?'save':row.path.split('/').at(-1)),['save','plan','run'])
  assert.deepEqual(calls[2].body,{version_ids:['partial-child'],fingerprint:'partial-confirmed'})
  assert.equal(JSON.stringify(page.versions.slice(0,2)),original);assert.equal(dialogs.length,0)
  assert.deepEqual(versionSpec(page.active).partial,{plan_id:'a'.repeat(32)})
  assert.equal('partial_summary' in versionSpec(page.active),false)
  assert.equal(page.fieldDisabled({path:'assemble.crf'}),true)
  page.override('assemble.subtitle_size',40);page.restoreVersionDefaults()
  assert.deepEqual(page.active.overrides,{'assemble.crf':18})
  page.inherit('assemble.crf');assert.equal(page.active.overrides['assemble.crf'],18)
})

function selectedUpdate(plan='a'.repeat(32)) {
  return {...versionSpec(data().versions[0]),update:{plan_id:plan},
    update_summary:{method:'partial',selection_count:1,remaining_count:1,summary:'应用所选1项；另有1项待更新'}}
}
test('所选更新替换同一版本并按保存预检运行顺序执行，无二次确认或额外评论读取',async t=>{
  const {page,dialogs}=await render(),calls:any[]=[]
  const other=JSON.stringify(page.versions[1])
  t.mock.method(page.nav,'stepStarted',()=>{})
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url),body=options?.body?JSON.parse(String(options.body)):null
    calls.push({path,method:options?.method,body})
    if(options?.method==='PUT')return response({...data(),revision:2,fingerprint:'saved-selection',versions:body.versions})
    if(path.endsWith('/plan'))return response({fingerprint:'selection-contract',blockers:[],versions:[body.version_ids[0]]})
    if(path.endsWith('/run'))return response({status:'started',job_id:42})
    throw new Error('所选更新不应读取其他资料')
  })
  await page.partialCreated(selectedUpdate())
  assert.deepEqual(calls.map(row=>row.method==='PUT'?'save':row.path.split('/').at(-1)),['save','plan','run'])
  assert.equal(calls[0].body.versions.length,2)
  assert.equal(calls[0].body.versions.filter((row:any)=>row.id==='v1').length,1)
  assert.deepEqual(calls[0].body.versions[0].update,{plan_id:'a'.repeat(32)})
  assert.equal(JSON.stringify(page.versions[1]),other)
  assert.deepEqual(calls[1].body,{version_ids:['v1']})
  assert.deepEqual(calls[2].body,{version_ids:['v1'],fingerprint:'selection-contract'})
  assert.equal(dialogs.length,0);assert.equal(page.dirty,false);assert.equal(page.busy,false)
  assert.deepEqual(page.selected,['v1']);assert.equal(page.activeId,'v1')
})

test('已有32个版本仍可原位更新已有版本，不新增定义或撞上版本数量上限',async t=>{
  const {page}=await render(page=>{
    page.adopt({...data(),versions:Array.from({length:32},(_,index)=>({...data().versions[0],id:`v${index+1}`}))})
  })
  let writes=0,runs=0
  t.mock.method(page.nav,'stepStarted',()=>{})
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const body=options?.body?JSON.parse(String(options.body)):null
    if(options?.method==='PUT'){writes++;assert.equal(body.versions.length,32);return response({...data(),revision:2,versions:body.versions})}
    if(String(url).endsWith('/plan'))return response({fingerprint:'fixed',blockers:[],versions:[body.version_ids[0]]})
    if(String(url).endsWith('/run')){runs++;return response({job_id:42})}
    throw new Error('未预期请求')
  })
  await page.partialCreated(selectedUpdate())
  assert.equal(writes,1);assert.equal(runs,1);assert.equal(page.versions.length,32)
})

test('所选更新在保存或预检等待期间切项目，迟到结果不得启动旧项目任务',async t=>{
  for(const phase of ['save','plan'])await t.test(phase,async t=>{
    const {page,props,dialogs}=await render(),held=deferred<any>(),requested=deferred<void>(),calls:string[]=[]
    let saved:any
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      const path=String(url),body=options?.body?JSON.parse(String(options.body)):null
      calls.push(path)
      if(options?.method==='GET')return response({...data(),revision:9})
      if(options?.method==='PUT'){
        saved={...data(),revision:2,versions:body.versions}
        if(phase==='save'){requested.resolve();return held.promise}
        return response(saved)
      }
      if(path.endsWith('/plan')){requested.resolve();return held.promise}
      throw new Error('切换项目后禁止启动旧任务')
    })
    const operation=page.partialCreated(selectedUpdate())
    await requested.promise
    props.uid='next-project';page.resetContext();await settle()
    held.resolve(response(phase==='save'?saved:{fingerprint:'old-plan',blockers:[]}))
    await operation
    assert.equal(calls.some(path=>path.endsWith('/run')),false)
    assert.equal(dialogs.length,0);assert.equal(page.state.revision,9);assert.equal(page.error,'')
    assert.equal(page.versions.some((row:any)=>row.update),false)
  })
})

test('所选更新启动失败保留已保存方案，再次生成仍使用同一版本和引用',async t=>{
  const {page,dialogs}=await render(),calls:any[]=[]
  let attempts=0
  t.mock.method(page.nav,'stepStarted',()=>{})
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url),body=options?.body?JSON.parse(String(options.body)):null
    calls.push({path,body})
    if(options?.method==='PUT')return response({...data(),revision:2,versions:body.versions})
    if(path.endsWith('/comments'))return response({overlay_revision:'r1'})
    if(path.endsWith('/plan'))return response({fingerprint:'same-fixed-plan',blockers:[],versions:[selectedUpdate()]})
    if(path.endsWith('/run'))return ++attempts===1?{ok:false,json:async()=>({message:'服务暂不可用，请重试'})}:response({job_id:43})
    throw new Error('未预期请求')
  })
  await page.partialCreated(selectedUpdate())
  assert.match(page.error,/服务暂不可用/)
  assert.equal(page.dirty,false);assert.equal(page.busy,false);assert.equal(page.versions.length,2)
  assert.deepEqual(page.active.update,{plan_id:'a'.repeat(32)})
  await page.generate()
  assert.equal(dialogs.length,1)
  await dialogs[0].onPositiveClick()
  assert.equal(attempts,2);assert.equal(page.error,'')
  assert.equal(calls.filter(row=>row.path.endsWith('/run')).every(row=>JSON.stringify(row.body.version_ids)==='["v1"]'),true)
  assert.deepEqual(page.active.update,{plan_id:'a'.repeat(32)})
  assert.equal(calls.filter(row=>row.body?.versions).length,1,'重试不新增或重写版本定义')
})

test('所选成片文件缺失时重试固定方案，不转成采用全部工作稿的普通版本',async t=>{
  const {page,dialogs}=await render(page=>page.adopt({...data(),versions:[selectedUpdate(),data().versions[1]]}))
  const calls:any[]=[]
  t.mock.method(page.nav,'stepStarted',()=>{})
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url),body=options?.body?JSON.parse(String(options.body)):null
    calls.push({path,body})
    if(path.endsWith('/plan'))return response({fingerprint:'frozen-retry',blockers:[],versions:[selectedUpdate()]})
    if(path.endsWith('/run'))return response({job_id:45})
    throw new Error('固定方案不依赖当前评论，也不应另存普通版本')
  })
  await page.fullUpdate('v1')
  assert.equal(page.versions.length,2);assert.deepEqual(page.active.update,{plan_id:'a'.repeat(32)})
  assert.equal(dialogs.length,1)
  await dialogs[0].onPositiveClick()
  assert.deepEqual(calls.map(row=>row.path.split('/').at(-1)),['plan','run'])
  assert.deepEqual(calls[1].body,{version_ids:['v1'],fingerprint:'frozen-retry'})
})

test('复制所选更新用于新版本编辑时解除固定方案，原版本仍保留',async()=>{
  const {page}=await render(page=>page.adopt({...data(),versions:[selectedUpdate(),data().versions[1]]}))
  page.add(true)
  assert.equal(page.versions.length,3)
  assert.notEqual(page.active.id,'v1');assert.equal(page.active.update,undefined)
  assert.deepEqual(page.versions.find((row:any)=>row.id==='v1').update,{plan_id:'a'.repeat(32)})
})

test('可明确切回全部当前工作稿，先改草稿再保存，不生成或改动其他版本',async()=>{
  const {page}=await render(page=>page.adopt({...data(),versions:[selectedUpdate(),data().versions[1]]}))
  const other=JSON.stringify(page.versions[1])
  page.busy=true;page.followCurrentContent();assert.ok(page.active.update)
  page.busy=false;page.followCurrentContent()
  assert.equal(page.active.update,undefined);assert.equal(page.active.id,'v1')
  assert.equal(page.dirty,true);assert.equal(page.versions.length,2)
  assert.equal(JSON.stringify(page.versions[1]),other)
  assert.ok(page.state.versions[0].update,'明确保存之前保留已存固定方案')
})

test('从成片改动执行完整更新只重制所选基准，确认合同明确禁止复用',async t=>{
  const {page,dialogs}=await render()
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url),body=options?.body?JSON.parse(String(options.body)):null
    calls.push({path,body})
    return response(path.endsWith('/comments')?{overlay_revision:'comments-1',overlay:null}
      :path.endsWith('/plan')?{fingerprint:'force-contract',blockers:[],versions:[data().versions[1]]}
      :{job_id:42})
  })
  await page.fullUpdate('v2')
  assert.deepEqual(page.selected,['v2']);assert.equal(page.activeId,'v2')
  assert.equal(dialogs.length,1);assert.equal(dialogs[0].positiveText,'确认完整更新')
  assert.equal(calls.find(row=>row.path.endsWith('/plan')).body.force_rebuild,true)
  assert.equal(calls.some(row=>row.path.endsWith('/run')),false)
  await dialogs[0].onPositiveClick()
  const started=calls.find(row=>row.path.endsWith('/run'))
  assert.equal(started.body.force_rebuild,true);assert.deepEqual(started.body.version_ids,['v2'])
})

test('保留基准音轨的局部版本隐藏不生效声音设置，显示范围和固定编码约束',async()=>{
  const {page,html}=await render(page=>{
    const current=data()
    Object.assign(current.versions[0],{partial:{plan_id:'a'.repeat(32)},
      partial_summary:{replace_audio:false,ranges:[{start:5.125,end:7.25}]}})
    page.adopt(current)
  })
  assert.equal(page.preservesAudio,true);assert.equal(page.groups.includes('声音'),false)
  assert.match(html,/保留基准音轨/);assert.match(html,/00:05.125/);assert.match(html,/编码画质与基准一致/)
})

test('原声版本显示最终响度参数，配音人声背景与句间均衡仅配音版显示',async()=>{
  const {page,html}=await render(page=>{
    const current=data()
    current.fields=[{path:'assemble.voice_volume',label:'配音人声音量',group:'声音',type:'float'},
      {path:'assemble.sentence_leveling',label:'保守均衡句间音量',group:'声音',type:'bool'},
      {path:'assemble.final_loudness_mode',label:'最终成片响度',group:'声音',type:'select',options:[{label:'保持混音响度',value:'off'}]}] as any
    page.adopt(current)
  })
  assert.match(html,/最终成片响度/);assert.doesNotMatch(html,/配音人声音量|保守均衡句间音量/)
  assert.ok(page.groups.includes('声音'));assert.equal(page.groupFields('声音').length,1)
  page.active.dubbing=true;assert.equal(page.groupFields('声音').length,3)
})
test('真实版本编辑器标明继承与版本边界，空选不产生生成请求',async()=>{
  const {html,page}=await render()
  assert.match(html,/成片版本清单|中文原声/);assert.match(html,/data-source-kind="global"/);assert.match(html,/字幕语言/)
  assert.deepEqual(page.selected,[])
  await page.generate();assert.equal(page.busy,false)
  page.override('assemble.subtitle_track','trans')
  assert.equal(page.dirty,true);assert.equal(page.fieldSource('assemble.subtitle_track').kind,'version');assert.equal(page.active.overrides['assemble.subtitle_track'],'trans')
  page.inherit('assemble.subtitle_track');assert.equal(page.dirty,false);assert.equal(page.fieldSource('assemble.subtitle_track').kind,'global')
  page.override('comments.background_opacity',0);assert.equal(page.values['comments.background_opacity'],0)
  page.activeId='v2';assert.equal(page.values['assemble.subtitle_track'],'bilingual')
})

test('版本参数恢复项目默认只修改当前版本草稿，保留名称开关和其他版本',async t=>{
  const {page,html}=await render(page=>{
    page.override('assemble.subtitle_track','source')
    page.override('comments.background_opacity',0)
    page.override('assemble.loudnorm',false)
  })
  assert.match(html,/全部参数恢复项目默认/)
  assert.match(html,/恢复项目默认/)
  assert.match(html,/修改参数会设为版本专用/)
  assert.equal(page.parameterOverrides.length,3)
  const other=JSON.stringify(page.versions[1])
  const identity={...page.active};delete identity.overrides
  let requests=0
  t.mock.method(globalThis,'fetch',async()=>{requests++;throw new Error('恢复草稿不应请求后端')})
  page.restoreVersionDefaults()
  assert.deepEqual(page.active.overrides,{})
  const restored={...page.active};delete restored.overrides
  assert.deepEqual(restored,identity)
  assert.equal(JSON.stringify(page.versions[1]),other)
  assert.equal(page.values['assemble.subtitle_track'],'trans')
  assert.equal(page.values['comments.background_opacity'],40)
  assert.equal(page.fieldSource('assemble.subtitle_track').kind,'global')
  assert.equal(page.parameterOverrides.length,0)
  assert.equal(requests,0)
  assert.equal(page.dirty,false)
})

test('已保存的版本专用参数恢复后需明确保存，提交仍使用原版本身份',async t=>{
  const {page}=await render(page=>{page.activeId='v2'})
  page.restoreVersionDefaults()
  assert.equal(page.dirty,true)
  assert.equal(page.active.id,'v2')
  assert.equal(page.values['assemble.subtitle_track'],'trans')
  let request:any
  t.mock.method(globalThis,'fetch',async(_url,options)=>{
    request=JSON.parse(String(options?.body))
    return response({...data(),revision:2,versions:request.versions})
  })
  await page.save()
  assert.equal(request.revision,1)
  assert.equal(request.fingerprint,'fp-1')
  assert.deepEqual(request.versions[1].overrides,{})
  assert.equal(request.versions[1].name,'双语原声')
  assert.equal(page.dirty,false)
})

test('版本参数批量恢复遵守父级禁用、归档、忙态和逐字段锁定',async t=>{
  for(const lock of ['parent','archived','busy','field']) {
    const {page}=await render((page,props)=>{
      page.activeId='v2'
      props.disabled=lock==='parent'
      page.nav.archived=lock==='archived'
      page.busy=lock==='busy'
      t.mock.method(page.editability,'isPathLocked',(path:string)=>lock==='field' && path==='assemble.subtitle_track')
    })
    const original=JSON.stringify(page.active.overrides)
    assert.equal(page.restoreParametersLocked,true,lock)
    page.restoreVersionDefaults()
    assert.equal(JSON.stringify(page.active.overrides),original,lock)
  }
  const {page}=await render(page=>{page.activeId='v2'})
  assert.equal(page.restoreParametersLocked,false)
  page.restoreVersionDefaults()
  assert.deepEqual(page.active.overrides,{})
})
test('复制创建独立身份，重命名不改变身份，保存只发送稀疏定义与版本号',async t=>{
  const {page}=await render()
  page.add(true);assert.equal(page.versions.length,3);assert.notEqual(page.active.id,'v1')
  const id=page.active.id;page.active.name='新的P2效果';assert.equal(page.active.id,id)
  let request:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{request={url,method:options?.method,body:JSON.parse(String(options?.body))};return {ok:true,json:async()=>({...data(),revision:2,versions:request.body.versions})}})
  await page.save();assert.equal(request.method,'PUT');assert.equal(request.body.revision,1)
  assert.equal(request.body.versions.length,3);assert.equal('fresh' in request.body.versions[0],false);assert.equal(page.dirty,false)
})
test('预检确认冻结所选版本，不随确认框后的选择改变',async t=>{
  const {page,dialogs}=await render();page.selected=['v2']
  const writes:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/comments'))return {ok:true,json:async()=>({overlay_revision:'r1'})}
    const body=JSON.parse(String(options?.body));writes.push({url:String(url),body})
    if(String(url).endsWith('/plan'))return {ok:true,json:async()=>({versions:[data().versions[1]],blockers:[],fingerprint:'fp'})}
    return {ok:false,json:async()=>({message:'测试停止执行'})}
  })
  await page.generate();assert.equal(dialogs.length,1);page.selected=['v1']
  await dialogs[0].onPositiveClick()
  assert.deepEqual(writes[1].body.version_ids,['v2']);assert.equal(writes[1].body.fingerprint,'fp');assert.match(page.error,/测试停止执行/)
})

test('预检等待期间修改审查设置，迟到方案不打开确认也不启动成片',async t=>{
  const {page,dialogs,props}=await render();page.selected=['v2']
  const writes:string[]=[];let finish:any,requested:any
  const planRequested=new Promise(resolve=>{requested=resolve})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/comments'))return {ok:true,json:async()=>({overlay_revision:'r1'})}
    writes.push(String(url))
    if(String(url).endsWith('/plan')) {
      requested()
      return new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({versions:[data().versions[1]],blockers:[],fingerprint:'fp'})})})
    }
    throw new Error('不允许启动成片')
  })
  const pending=page.generate();await planRequested
  // StepView 在审查设置未保存时通过 disabled 禁用整个版本工作区。
  props.disabled=true;finish();await pending
  assert.equal(dialogs.length,0);assert.ok(writes.every(url=>!url.endsWith('/run')))
  assert.equal(page.busy,false);assert.match(page.error,/重新确认生成/)
})

test('确认窗口打开后出现未保存审查设置，确认不会绕过父级禁用发出run',async t=>{
  const {page,dialogs,props}=await render();page.selected=['v2']
  const writes:string[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/comments'))return {ok:true,json:async()=>({overlay_revision:'r1'})}
    writes.push(String(url))
    if(String(url).endsWith('/plan'))return {ok:true,json:async()=>({versions:[data().versions[1]],blockers:[],fingerprint:'fp'})}
    throw new Error('不允许启动成片')
  })
  await page.generate();assert.equal(dialogs.length,1)
  props.disabled=true
  assert.equal(await dialogs[0].onPositiveClick(),false)
  assert.ok(writes.every(url=>!url.endsWith('/run')));assert.match(page.error,/重新确认生成/)
  dialogs[0].onAfterLeave();assert.equal(page.busy,false)
})

function deferred<T>() {
  let resolve!:(value:T)=>void
  const promise=new Promise<T>(done=>{resolve=done})
  return {promise,resolve}
}
function response(value:any) { return {ok:true,json:async()=>value} }
async function settle() { for(let i=0;i<6;i++) await Promise.resolve() }

test('版本工作区多次刷新合并为一个请求并补读最终状态',async t=>{
  const {page}=await render(),first=deferred<any>(),last=deferred<any>()
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{
    reads.push({url:String(url),options})
    return reads.length===1?first.promise:last.promise
  })
  const initial=page.refresh();void page.refresh();void page.refresh()
  assert.equal(reads.length,1);assert.equal(reads[0].options.cache,'no-store')
  assert.ok(reads[0].options.signal instanceof AbortSignal)
  first.resolve(response(data()));await initial;await settle()
  assert.equal(reads.length,2)
  last.resolve(response({...data(),revision:2,fingerprint:'fp-2'}));await settle()
  assert.equal(page.state.revision,2);assert.equal(page.state.fingerprint,'fp-2')
  assert.equal(reads.length,2)
})

test('自动刷新只更新草稿继承默认，不偷换提交revision与fingerprint',async t=>{
  const {page}=await render();page.override('comments.background_opacity',0)
  t.mock.method(globalThis,'fetch',async()=>response({...data(),revision:9,fingerprint:'fp-9',
    defaults:{...data().defaults,'assemble.subtitle_track':'bilingual'},
    versions:data().versions.map(v=>({...v,name:'另一页面的新名称'}))}))
  await page.refresh()
  assert.equal(page.dirty,true);assert.equal(page.active.name,'中文原声')
  assert.equal(page.values['comments.background_opacity'],0)
  assert.equal(page.values['assemble.subtitle_track'],'bilingual')
  assert.equal(page.state.revision,1);assert.equal(page.state.fingerprint,'fp-1')
})

test('保存取消旧刷新，迟到的旧版本不能覆盖保存结果',async t=>{
  const {page}=await render(),old=deferred<any>()
  let oldSignal:AbortSignal|undefined,write:any
  t.mock.method(globalThis,'fetch',(url,options)=>{
    if(options?.method==='PUT') {
      write=JSON.parse(String(options.body))
      return Promise.resolve(response({...data(),revision:2,fingerprint:'fp-2',versions:write.versions}))
    }
    oldSignal=options?.signal
    return old.promise
  })
  const pending=page.refresh();void page.refresh()
  page.active.name='刚保存的名称';await page.save()
  assert.equal(oldSignal?.aborted,true)
  assert.equal(write.revision,1);assert.equal(write.fingerprint,'fp-1')
  old.resolve(response(data()));await pending;await settle()
  assert.equal(page.state.revision,2);assert.equal(page.active.name,'刚保存的名称')
  assert.equal(page.dirty,false);assert.equal(page.error,'')
})

test('读取已保存版本取消旧轮询并阻止迟到响应回滚',async t=>{
  const {page,dialogs}=await render(),old=deferred<any>()
  let count=0,oldSignal:AbortSignal|undefined
  t.mock.method(globalThis,'fetch',(_url,options)=>{
    if(++count===1){oldSignal=options?.signal;return old.promise}
    return Promise.resolve(response({...data(),revision:3,fingerprint:'fp-3',
      versions:data().versions.map(v=>({...v,name:v.id==='v1'?'最新已保存版本':v.name}))}))
  })
  const pending=page.refresh();page.active.name='未保存草稿'
  await page.reloadSaved();assert.equal(dialogs.length,1)
  await dialogs[0].onPositiveClick()
  assert.equal(oldSignal?.aborted,true);assert.equal(page.dirty,false)
  old.resolve(response(data()));await pending;await settle()
  assert.equal(count,2);assert.equal(page.state.revision,3)
  assert.equal(page.active.name,'最新已保存版本')
})

test('切换项目中止旧读，旧结果和失败都不能写入新项目',async t=>{
  const {page,props}=await render(),old=deferred<any>(),next=deferred<any>()
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{
    reads.push({url:String(url),signal:options?.signal})
    return reads.length===1?old.promise:next.promise
  })
  const pending=page.refresh();void page.refresh()
  props.uid='next-project';page.resetContext()
  assert.equal(reads[0].signal.aborted,true);assert.equal(page.state,null)
  assert.equal(reads.length,2);assert.match(reads[1].url,/next-project\/assembly-versions$/)
  next.resolve(response({...data(),revision:8,versions:data().versions.map(v=>({...v,name:'新项目版本'}))}));await settle()
  old.resolve({ok:false,json:async()=>({message:'旧项目读取失败'})});await pending;await settle()
  assert.equal(page.state.revision,8);assert.equal(page.active.name,'新项目版本')
  assert.equal(page.error,'');assert.equal(reads.length,2)
})

test('工作区退出中止当前与排队刷新，不恢复已关闭上下文',async t=>{
  const {page}=await render(),old=deferred<any>()
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',(_url,options)=>{reads.push(options);return old.promise})
  const pending=page.refresh();void page.refresh()
  page.disposeWorkspace();assert.equal(reads[0].signal.aborted,true)
  old.resolve(response({...data(),revision:99}));await pending;await settle();await page.refresh()
  assert.equal(reads.length,1);assert.equal(page.state.revision,1);assert.equal(page.error,'')
})

test('旧项目保存回执不能覆盖切换后的项目版本',async t=>{
  const {page,props}=await render(),save=deferred<any>()
  const writes:string[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{
    if(options?.method==='PUT'){writes.push(String(url));return save.promise}
    return Promise.resolve(response({...data(),revision:7,versions:data().versions.map(v=>({...v,name:'新项目版本'}))}))
  })
  page.active.name='旧项目的修改';const saving=page.save()
  props.uid='next-project';page.resetContext();await settle()
  save.resolve(response({...data(),revision:2,versions:data().versions.map(v=>({...v,name:'旧项目回执'}))}));await saving
  assert.equal(writes.length,1);assert.match(writes[0],/fixture\/assembly-versions$/)
  assert.equal(page.state.revision,7);assert.equal(page.active.name,'新项目版本')
  assert.equal(page.busy,false);assert.equal(page.error,'')
})

test('切换项目后的旧生成确认不能向新项目发送运行请求',async t=>{
  const {page,props,dialogs}=await render();page.selected=['v2']
  const writes:string[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/comments'))return response({overlay_revision:'r1'})
    if(options?.method==='GET')return response(data())
    writes.push(String(url))
    if(String(url).endsWith('/plan'))return response({versions:[data().versions[1]],blockers:[],fingerprint:'fp'})
    throw new Error('禁止旧确认启动任务')
  })
  await page.generate();assert.equal(dialogs.length,1)
  props.uid='next-project';page.resetContext();await settle()
  await dialogs[0].onPositiveClick()
  assert.equal(writes.length,1);assert.ok(writes[0].endsWith('/plan'))
  assert.equal(page.busy,false);assert.equal(page.error,'')
})

test('恢复继承成功读取新清单，忙态不会吞掉必要的刷新',async t=>{
  const {page,dialogs}=await render(),requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({url:String(url),options})
    if(String(url).includes('/api/configuration/'))return response({ok:true})
    return response({...data(),revision:4,fingerprint:'fp-4',inherited:true,
      versions:data().versions.map(v=>({...v,name:'继承的新版本'}))})
  })
  page.restoreInheritance();assert.equal(dialogs.length,1)
  await dialogs[0].onPositiveClick()
  assert.equal(requests.length,2);assert.equal(requests[0].options.method,'PUT')
  assert.deepEqual(JSON.parse(requests[0].options.body),{reset:['assembly.versions'],revision:1,fingerprint:'fp-1'})
  assert.match(requests[1].url,/fixture\/assembly-versions$/)
  assert.ok(requests[1].options.signal instanceof AbortSignal)
  assert.equal(page.state.revision,4);assert.equal(page.state.inherited,true)
  assert.equal(page.active.name,'继承的新版本');assert.equal(page.busy,false)
})

test('成片确认打开后项目归档，保留方案但拦截生成，恢复后需重新确认',async t=>{
  const {page,dialogs}=await render();page.selected=['v2']
  const writes:string[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/comments'))return response({overlay_revision:'r1'})
    if(options?.method==='GET')return response(data())
    writes.push(String(url));return response({versions:[data().versions[1]],blockers:[],fingerprint:'fp'})
  })
  await page.generate();assert.equal(dialogs.length,1)
  page.nav.archived=true
  assert.equal(await dialogs[0].onPositiveClick(),false)
  assert.equal(writes.length,1);assert.ok(writes[0].endsWith('/plan'))
  assert.match(page.error,/已归档.*恢复/)
})

test('来源版本专用值使用后端真实来源，同值固定保留显式版本设置，恢复只跟随项目默认',async()=>{
  const {page}=await render(),path='assemble.subtitle_track'
  const source={kind:'global',frozen:true,version_name:'双语原声'}
  page.adopt({...data(),source:{kind:'global',frozen:true},sources:{[path]:{kind:'global',frozen:true}},versions:data().versions.map(v=>({...v,sources:{[path]:v.id==='v2'?source:{kind:'global',frozen:true}}}))})
  page.activeId='v2';assert.equal(page.fieldSource(path).kind,'global');assert.equal(page.fieldSource(path).frozen,true)
  page.activeId='v1';page.override(path,'trans')
  assert.equal(page.fieldSource(path).kind,'version');assert.equal(page.dirty,true)
  page.inherit(path);assert.equal(page.fieldSource(path).kind,'global');assert.equal(page.dirty,false)
})

test('应用基础设置采用服务器最终视图并取消旧读，不用第二次GET回滚来源与版本',async t=>{
  const {page}=await render(),old=deferred<any>();let reads=0,signal:any
  t.mock.method(globalThis,'fetch',(_url,options)=>{reads++;signal=options?.signal;return old.promise})
  const pending=page.refresh()
  page.defaultsApplied({...data(),revision:2,fingerprint:'fp-applied',inheritance:{changed_count:0},versions:data().versions.map(v=>({...v,name:'应用后版本'}))})
  assert.equal(signal.aborted,true);assert.equal(page.state.fingerprint,'fp-applied')
  old.resolve(response(data()));await pending;await settle()
  assert.equal(reads,1);assert.equal(page.active.name,'应用后版本');assert.equal(page.dirty,false)
})

test('聚焦刷新只更新草稿基础与来源摘要，应用入口被版本或剪辑草稿锁住',async t=>{
  const {page}=await render();page.override('comments.background_opacity',0)
  t.mock.method(globalThis,'fetch',async()=>response({...data(),revision:8,fingerprint:'new',inheritance:{changed_count:2},defaults:{...data().defaults,'assemble.subtitle_track':'bilingual'}}))
  await page.refresh();assert.equal(page.state.inheritance.changed_count,2);assert.equal(page.defaultsLocked,true)
  assert.equal(page.state.revision,1);assert.equal(page.state.fingerprint,'fp-1');assert.equal(page.active.overrides['comments.background_opacity'],0)
  page.inherit('comments.background_opacity');page.cutsDirty=true;assert.equal(page.defaultsLocked,true)
})

test('纯原声版本启用成片剪辑后开放输出画质设置，无剪辑时保持直接复用语义',async()=>{
  const {page}=await render()
  const plain={id:'plain',name:'纯原声',dubbing:false,burn_subtitles:false,include_comments:false,overrides:{},status:'需更新'}
  page.adopt({...data(),versions:[plain]});assert.equal(page.groups.includes('输出'),false)
  page.adopt({...data(),versions:[{...plain,output_cuts:{ranges:[{start:1,end:2}],source_duration:60,output_duration:59}}]})
  assert.equal(page.groups.includes('输出'),true)
  page.add(true);assert.equal(page.active.dubbing,false);assert.equal(page.groups.includes('输出'),true)
  page.add();page.active.burn_subtitles=false;assert.equal(page.groups.includes('输出'),true)
})


function deletePlan(id='v1', extra:any={}) {
  return {version_id:id,name:data().versions.find(row=>row.id===id)?.name || id,revision:1,fingerprint:'fp-1',can_delete:true,
    blockers:[],dependent_versions:['局部修订版'],formal_count:2,preview_count:1,notice:'后续修订基准文件保留。',...extra}
}
function deletedData(id='v1') {return {...data(),revision:2,fingerprint:'after-delete',versions:data().versions.filter(row=>row.id!==id),deleted_version:{id,name:id}}}

test('每张版本卡有独立删除入口，确认前只预检，确认后立即保存并同步基准和勾选',async t=>{
  const {page,dialogs,html}=await render();page.selected=['v1','v2']
  assert.match(html,/aria-label="删除版本：中文原声"/);assert.match(html,/aria-label="删除版本：双语原声"/)
  assert.match(html,/删除版本…/);assert.doesNotMatch(html,/移除定义/)
  const calls:any[]=[]
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url),body=JSON.parse(String(options?.body));calls.push({path,body})
    return response(path.endsWith('/delete-plan')?deletePlan():deletedData())
  })
  await page.remove();assert.equal(dialogs.length,1);assert.equal(page.dirty,false);assert.equal(page.busy,true)
  assert.deepEqual(calls.map(row=>row.path.split('/').at(-1)),['delete-plan'])
  assert.match(dialogs[0].content,/2 份正式成片、1 份试片/);assert.match(dialogs[0].content,/发布记录/)
  assert.match(dialogs[0].content,/后续版本/);assert.match(dialogs[0].content,/正式成品与原片/)
  await dialogs[0].onPositiveClick()
  assert.deepEqual(page.versions.map((row:any)=>row.id),['v2']);assert.deepEqual(page.selected,['v2'])
  assert.equal(page.activeId,'v2');assert.equal(page.dirty,false);assert.equal(page.busy,false)
  assert.deepEqual(calls[1].body,{version_id:'v1',revision:1,fingerprint:'fp-1',confirmed:true})
  await dialogs[0].onPositiveClick();assert.equal(calls.length,2)
})

test('取消删除保留版本和选择，不提交删除，释放操作忙态',async t=>{
  const {page,dialogs}=await render();page.selected=['v1','v2'];let calls=0
  t.mock.method(globalThis,'fetch',async()=>{calls++;return response(deletePlan())})
  await page.remove('v2');dialogs[0].onAfterLeave()
  assert.equal(calls,1);assert.equal(page.versions.length,2);assert.equal(page.activeId,'v1')
  assert.deepEqual(page.selected,['v1','v2']);assert.equal(page.busy,false)
})

test('删除预检或提交失败均保留版本，提交失败可在确认框内安全重试',async t=>{
  for(const phase of ['plan','delete'])await t.test(phase,async t=>{
    const {page,dialogs}=await render();const original=JSON.stringify(page.versions);let attempts=0
    t.mock.method(page.nav,'poll',async()=>{})
    t.mock.method(globalThis,'fetch',async url=>{
      if(String(url).endsWith('/delete-plan'))return phase==='plan'?{ok:false,json:async()=>({message:'预检失败，请重试'})}:response(deletePlan())
      if(++attempts===1)return {ok:false,json:async()=>({message:'清单已变化，请重新检查'})}
      return response(deletedData())
    })
    await page.remove()
    if(phase==='plan') {assert.equal(dialogs.length,0);assert.equal(page.busy,false);assert.match(page.error,/预检失败/)}
    else {
      assert.equal(await dialogs[0].onPositiveClick(),false);assert.equal(page.deleteLoading,false)
      assert.equal(JSON.stringify(page.versions),original);assert.match(page.error,/清单已变化/)
      await dialogs[0].onPositiveClick();assert.equal(page.versions.length,1);assert.equal(page.busy,false);assert.equal(page.error,'')
    }
    if(phase==='plan')assert.equal(JSON.stringify(page.versions),original)
  })
})

test('未保存的新版本只移除本地草稿，其他版本调整及生成勾选保留',async t=>{
  const {page,dialogs}=await render();page.active.name='保留的调整';page.add();const added=page.activeId
  page.selected=['v1',added]
  t.mock.method(globalThis,'fetch',async()=>{throw new Error('本地草稿不应请求服务器')})
  await page.remove();assert.equal(dialogs.length,1);assert.match(dialogs[0].content,/其他版本草稿保留/)
  await dialogs[0].onPositiveClick()
  assert.equal(page.versions.length,2);assert.equal(page.active.name,'保留的调整');assert.equal(page.dirty,true)
  assert.deepEqual(page.selected,['v1']);assert.equal(page.busy,false)
})

test('已有版本存在版本或剪辑草稿时先提示处理，不能吞掉其他未保存改动',async t=>{
  const {page,dialogs}=await render();let calls=0,warnings=0
  t.mock.method(globalThis,'fetch',async()=>{calls++;return response(deletePlan())})
  t.mock.method(page.message,'warning',()=>{warnings++})
  page.active.name='未保存';await page.remove('v2')
  assert.equal(page.versions.length,2);assert.equal(page.active.name,'未保存')
  page.active.name=data().versions[0].name;page.cutsDirty=true;await page.remove('v2')
  assert.equal(dialogs.length,0);assert.equal(calls,0);assert.equal(warnings,2);assert.equal(page.cutsDirty,true)
})

test('最后一个版本、运行和排队期间不允许发起删除',async t=>{
  const {page,dialogs}=await render();let calls=0
  t.mock.method(globalThis,'fetch',async()=>{calls++;return response(deletePlan())})
  page.nav.steps=[{key:'assemble',status:'running'}];await page.remove()
  page.nav.stepMap.assemble.status='queued';await page.remove()
  page.nav.stepMap.assemble.status='done';page.adopt({...data(),versions:[data().versions[0]]});await page.remove()
  assert.equal(dialogs.length,0);assert.equal(calls,0);assert.equal(page.versions.length,1)
})

test('后端预检阻挡删除时显示具体原因，保留当前清单',async t=>{
  const {page,dialogs}=await render()
  t.mock.method(globalThis,'fetch',async()=>response(deletePlan('v1',{can_delete:false,blockers:['分P发布清单只有这个版本，请先调整分P清单。']})))
  await page.remove();assert.equal(dialogs.length,0);assert.equal(page.versions.length,2)
  assert.match(page.error,/分P发布清单/);assert.equal(page.busy,false)
})

test('重复点击和重复确认只预检、提交一次，迟到预检不污染新项目',async t=>{
  const {page,dialogs,props}=await render(),held=deferred<any>();let plans=0,deletes=0
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(options?.method==='GET')return response({...data(),revision:9})
    if(String(url).endsWith('/delete-plan')){plans++;return held.promise}
    deletes++;return response(deletedData())
  })
  const first=page.remove();await page.remove();assert.equal(plans,1)
  props.uid='another-project';page.resetContext();await settle();held.resolve(response(deletePlan()));await first
  assert.equal(dialogs.length,0);assert.equal(deletes,0);assert.equal(page.state.revision,9);assert.equal(page.busy,false)
})

test('删除提交双击只发一次请求，失败或成功回执均不会覆盖后来项目',async t=>{
  for(const outcome of ['success','failure'])await t.test(outcome,async t=>{
    const {page,dialogs,props}=await render(),held=deferred<any>(),requested=deferred<void>();let writes=0
    t.mock.method(globalThis,'fetch',async(url,options)=>{
      if(options?.method==='GET')return response({...data(),revision:9})
      if(String(url).endsWith('/delete-plan'))return response(deletePlan())
      writes++;requested.resolve();return held.promise
    })
    await page.remove()
    const submitted=dialogs[0].onPositiveClick();await requested.promise
    await dialogs[0].onPositiveClick();assert.equal(writes,1)
    props.uid='new-project';page.resetContext();await settle()
    held.resolve(outcome==='success'?response(deletedData()):{ok:false,json:async()=>({message:'旧项目失败'})})
    await submitted
    assert.equal(page.versions.length,2);assert.equal(page.state.revision,9);assert.equal(page.error,'');assert.equal(page.busy,false)
  })
})

test('删除其他卡片保留当前基准，预检发现远端版本变化时要求先刷新',async t=>{
  const {page,dialogs}=await render();page.selected=['v1','v2'];let stale=true,writes=0
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async url=>{
    if(String(url).endsWith('/delete-plan'))return response(deletePlan('v2',stale?{revision:2,fingerprint:'remote-changed'}:{}))
    writes++;return response(deletedData('v2'))
  })
  await page.remove('v2');assert.equal(dialogs.length,0);assert.equal(writes,0)
  assert.match(page.error,/请刷新版本/);assert.equal(page.activeId,'v1');assert.equal(page.versions.length,2)
  stale=false;await page.remove('v2');await dialogs[0].onPositiveClick()
  assert.equal(writes,1);assert.equal(page.activeId,'v1');assert.deepEqual(page.selected,['v1']);assert.equal(page.dirty,false)
})

test('切换项目关闭删除确认，迟到的旧确认不能删除新项目同编号版本',async t=>{
  const {page,dialogs,props}=await render()
  t.mock.method(globalThis,'fetch',async(_url,options)=>response(options?.method==='GET'?{...data(),revision:9}:deletePlan()))
  await page.remove();let closed=0
  t.mock.method(page.confirmation,'destroy',()=>{closed++})
  const old=dialogs[0]
  props.uid='another-project';page.resetContext();await settle()
  assert.equal(closed,1);const before=JSON.stringify(page.versions)
  await old.onPositiveClick()
  assert.equal(JSON.stringify(page.versions),before);assert.equal(page.dirty,false);assert.equal(page.state.revision,9)
})

test('确认期间出现禁用、归档、字段锁、排队或草稿变更时不提交删除',async t=>{
  for(const lock of ['archived','parent','field','queued','draft','revision'])await t.test(lock,async t=>{
    const {page,props,dialogs}=await render();let calls=0
    t.mock.method(globalThis,'fetch',async()=>{calls++;return response(deletePlan())})
    await page.remove()
    props.disabled=lock==='parent';page.nav.archived=lock==='archived'
    t.mock.method(page.editability,'isPathLocked',(path:string)=>lock==='field' && path==='assembly.versions')
    if(lock==='queued')page.nav.steps=[{key:'assemble',status:'queued'}]
    if(lock==='draft')page.active.name='新增调整'
    if(lock==='revision')page.state.revision++
    assert.equal(await dialogs[0].onPositiveClick(),false)
    assert.equal(calls,1);assert.equal(page.versions.length,2)
    dialogs[0].onAfterLeave();assert.equal(page.busy,false)
  })
})


test('成片剪辑版保存固定来源并直接预检生成，不读取或混入当前评论工作稿', async t => {
  const {page,dialogs}=await render(),calls:any[]=[]
  const version={...versionSpec(data().versions[0]),id:'timeline-child',name:'原声修订3 · 剪辑',timeline:{plan_id:'b'.repeat(32)},values:{'assemble.subtitle_size':22}}
  assert.deepEqual(versionSpec(version).timeline,{plan_id:'b'.repeat(32)})
  t.mock.method(page.nav,'stepStarted',()=>{})
  t.mock.method(page.nav,'poll',async()=>{})
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    const path=String(url),body=options?.body?JSON.parse(String(options.body)):null;calls.push({path,body})
    if(options?.method==='PUT') {
      // 新版本的剪辑编辑器正在读取冻结清单；不能把只读初始化误当作
      // 用户又开始修改，从而悄悄中断已经确认的保存并生成操作。
      page.cutsBusy=true
      return response({...data(),versions:body.versions.map((row:any)=>({...row,values:row.id===version.id?version.values:{}}))})
    }
    if(path.endsWith('/plan'))return response({fingerprint:'timeline-fixed',blockers:[]})
    if(path.endsWith('/run'))return response({job_id:44})
    throw new Error('成片剪辑版不应读取其他工作稿')
  })
  page.cutsDirty=true
  await page.timelineCreated(version)
  assert.equal(page.activeId,'timeline-child');assert.deepEqual(page.selected,['timeline-child'])
  assert.equal(page.dirty,false);assert.equal(page.cutsDirty,false);assert.equal(page.preservesAudio,true)
  assert.equal(page.values['assemble.subtitle_size'],22)
  assert.equal(page.fieldLocked('assemble.subtitle_size'),true)
  page.override('assemble.subtitle_size',99);assert.equal(page.active.overrides['assemble.subtitle_size'],undefined)
  assert.deepEqual(calls.map(row=>row.path.split('/').at(-1)),['assembly-versions','plan','run'])
  assert.deepEqual(calls[2].body,{version_ids:['timeline-child'],fingerprint:'timeline-fixed'})
  assert.equal(dialogs.length,0)
})


test('剪辑版本展示实际编码回退结果，不能把预估主体复用标成执行事实',async()=>{
  const encoding={method:'smart_cut',encoded_seconds:2,copied_seconds:58,audio_mode:'encode_once'}
  const actual={method:'full_encode',encoded_seconds:60,copied_seconds:0,audio_mode:'encode_once',fallback_reason:'接缝媒体校验未通过，已自动完整编码'}
  const {page,html}=await render(page=>page.adopt({...data(),versions:[{...data().versions[0],timeline:{plan_id:'a'.repeat(32)},timeline_summary:{encoding,actual_encoding:actual}}]}),false)
  assert.equal(page.activeTimelineEncoding.method,'full_encode');assert.equal(page.activeTimelineEncodingIsActual,true)
  assert.match(html,/实际生成记录/);assert.match(html,/完整重新编码/);assert.match(html,/00:01:00\.000/)
  assert.match(html,/接缝媒体校验未通过/)
  page.state.versions[0].timeline_summary.actual_encoding=undefined
  assert.equal(page.activeTimelineEncoding.method,'smart_cut');assert.equal(page.activeTimelineEncodingIsActual,false)
})

const formalArtifact={id:101,name:'final_version_v1.mp4',version_id:'v1',sha256:'a'.repeat(64),exists:true,created_at:'2026-10-20T01:05:55Z'}
function completedCatalog() {
  return {...data(),versions:data().versions.map(row=>row.id==='v1'?{...row,artifact_id:101,fresh:true,status:'已是最新',
    audio_report:{preview:false,has_audio:true,integrated_lufs:-15.1,true_peak_db:-2.1,target_reached:false,
      assessment:{target_status:'below'},settings:{mode:'uniform',target_lufs:-14,true_peak_db:-1.5},
      processing:{actual_method:'dynamic',aac_peak_correction_db:-.8}}}:row)}
}
function runningCatalog(page:any,artifacts:any[]=[]) {
  page.nav.uid='fixture';page.nav.steps=[{key:'assemble',status:'running',job_id:521,artifacts}]
  page.adopt({...data(),versions:data().versions.map(row=>({...row,artifact_id:null,fresh:false,status:'尚未生成'}))})
}

test('正式提交签名忽略试片、临时文件、进度和排列，只跟随正式身份与登记',()=>{
  const original=structuredClone(formalArtifact)
  const before=assemblyFormalOutputSignature([formalArtifact])
  assert.equal(assemblyFormalOutputSignature([{...formalArtifact,disk_size:999},
    {id:2,name:'.tmp_final_version_v1.mp4',sha256:'b'},
    {id:3,name:'preview_version_v1_test.mp4',sha256:'c'}]),before)
  assert.equal(assemblyFormalOutputSignature([formalArtifact,{id:2,name:'final_version_v2.mp4'}]),
    assemblyFormalOutputSignature([{id:2,name:'final_version_v2.mp4'},formalArtifact]))
  for(const patch of [{id:102},{sha256:'b'.repeat(64)},{exists:false},{version_id:'v2'},{generated_at:'2026-10-20T01:06:55Z'}])
    assert.notEqual(assemblyFormalOutputSignature([{...formalArtifact,...patch}]),before)
  assert.equal(assemblyFormalOutputSignature([null,{name:'final_version_v1.mp4'}]),'[]')
  assert.deepEqual(formalArtifact,original)
})

test('草稿只合并同一定义的正式提交元数据，不采用另一页面改过的版本配置',()=>{
  const saved=data().versions,next=completedCatalog().versions
  next[1]={...next[1],name:'另一页面已改名',artifact_id:202} as any
  const merged=mergeAssemblyOutputMetadata(saved,next)
  assert.equal(merged[0].artifact_id,101);assert.equal(merged[0].audio_report.integrated_lufs,-15.1)
  assert.equal(merged[1],saved[1]);assert.equal(merged[1].name,'双语原声')
  assert.equal('artifact_id' in saved[0],false)
})

test('同一合成任务仍运行时首个正式版本登记立即刷新目录和声音报告，不等整体done',async t=>{
  let reads=0
  t.mock.method(globalThis,'fetch',(url)=>{
    if(String(url).endsWith('/assembly-versions')) {reads++;return Promise.resolve(response(completedCatalog()))}
    return new Promise(()=>{})
  })
  const {page}=await render(page=>{
    runningCatalog(page)
    page.nav.stepMap.assemble.artifacts=[formalArtifact]
    for(let index=0;index<5;index++) {
      page.nav.stepMap.assemble.progress=index
      page.nav.stepMap.assemble.artifacts=[{...formalArtifact},{id:301,name:'preview_version_v1_test.mp4',sha256:String(index)}]
    }
  })
  await settle()
  assert.equal(reads,1);assert.equal(page.nav.stepMap.assemble.status,'running')
  assert.equal(page.state.versions[0].artifact_id,101);assert.equal(page.savedActive.artifact_id,101)
  assert.equal(page.versionStatus(page.versions[0]),'已是最新')
  assert.equal(page.savedActive.audio_report.integrated_lufs,-15.1)
  const displayed=await render(next=>next.adopt(page.state),false)
  assert.match(displayed.html,/已生成正式成片的声音报告/);assert.match(displayed.html,/-15.1 LUFS/)
  assert.match(displayed.html,/低于统一目标/);assert.match(displayed.html,/AAC 编码真峰值复核后衰减 0.8 dB/)
})

test('正式提交变化合并补读且忽略早于提交身份的旧响应，不让旧目录覆盖最新事实',async t=>{
  const first=deferred<any>(),last=deferred<any>();let reads=0
  t.mock.method(globalThis,'fetch',(url)=>{
    if(!String(url).endsWith('/assembly-versions')) return new Promise(()=>{})
    return ++reads===1?first.promise:last.promise
  })
  const {page}=await render(page=>{
    runningCatalog(page);void page.refresh()
    page.nav.stepMap.assemble.artifacts=[formalArtifact]
    page.nav.stepMap.assemble.artifacts=[{...formalArtifact,generated_at:'2026-10-20T01:06:55Z'}]
  })
  assert.equal(reads,1)
  first.resolve(response({...data(),revision:0,fingerprint:'stale-before-save'}));await settle()
  assert.equal(reads,2);assert.equal(page.state.revision,1)
  last.resolve(response(completedCatalog()));await settle()
  assert.equal(page.savedActive.artifact_id,101);assert.equal(reads,2)
})

test('忙时正式目录刷新延后，草稿与提交基线保留但成功版本的事实仍更新',async t=>{
  let reads=0
  t.mock.method(globalThis,'fetch',(url)=>{
    if(String(url).endsWith('/assembly-versions')) {reads++;return Promise.resolve(response({...completedCatalog(),revision:9,fingerprint:'new-metadata'}))}
    return new Promise(()=>{})
  })
  const {page}=await render(page=>{
    runningCatalog(page);page.busy=true;page.activeId='v2';page.active.name='保留未保存的版本名'
    page.selected=['v1'];page.nav.stepMap.assemble.artifacts=[formalArtifact]
    assert.equal(reads,0);page.busy=false
  })
  await settle()
  assert.equal(reads,1);assert.equal(page.dirty,true);assert.equal(page.activeId,'v2')
  assert.equal(page.active.name,'保留未保存的版本名');assert.deepEqual(page.selected,['v1'])
  assert.equal(page.state.revision,1);assert.equal(page.state.fingerprint,'fp-1')
  assert.equal(page.state.versions[0].artifact_id,101)
  assert.equal(page.state.versions[0].audio_report.true_peak_db,-2.1)
})
