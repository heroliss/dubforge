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
import { versionSpec, effectiveVersion, workflowVersionSelection } from '../src/utils/assemblyVersions.ts'
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
async function render(){
  let page:any,props:any;const dialogs:any[]=[]
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const Probe={setup(){useDialog().warning=((opts:any)=>{dialogs.push(opts);return{destroy(){}}}) as any;return()=>h(Workspace,{uid:'fixture',configureOnly:true})}}
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Probe)})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){if(this.$options.__name==='AssemblyVersionWorkspace'){page=this.$.setupState;props=this.$.props;page.adopt(data());page.ready=true}}})
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
