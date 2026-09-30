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
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null}})
  ;({default:Workspace}=await server.ssrLoadModule('/src/components/AssemblyVersionWorkspace.vue'))
  mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
})
after(async()=>{mock.restoreAll();await server.close()})
function data(){return {revision:1,template_revision:0,template_versions:[],defaults:{'assemble.subtitle_track':'trans','comments.background_opacity':40},sources:{'assemble.subtitle_track':{kind:'global'}},
  fields:[{path:'assemble.subtitle_track',label:'字幕语言',group:'字幕',type:'select',options:[{label:'译文',value:'trans'},{label:'双语',value:'bilingual'}]}],
  versions:[{id:'v1',name:'中文原声',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{},status:'已是最新',fresh:true},
    {id:'v2',name:'双语原声',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{'assemble.subtitle_track':'bilingual'},status:'需更新'}]}}
async function render(){
  let page:any;const dialogs:any[]=[]
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]});await router.push('/')
  const Probe={setup(){useDialog().warning=((opts:any)=>{dialogs.push(opts);return{destroy(){}}}) as any;return()=>h(Workspace,{uid:'fixture',configureOnly:true})}}
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Probe)})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){if(this.$options.__name==='AssemblyVersionWorkspace'){page=this.$.setupState;page.adopt(data());page.ready=true}}})
  return {html:await renderToString(app),page,dialogs}
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
