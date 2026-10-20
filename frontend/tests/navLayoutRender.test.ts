import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h, markRaw } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia } from 'pinia'
import { NAlert, NLayout, NLayoutSider, NMenu } from 'naive-ui'
import { createMemoryHistory, createRouter } from 'vue-router'

let server:ViteDevServer, MainLayout:any, StageNavigation:any, useNavStore:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],
    appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  ;({default:MainLayout}=await server.ssrLoadModule('/src/layouts/MainLayout.vue'))
  ;({default:StageNavigation}=await server.ssrLoadModule('/src/components/WorkflowStageNavigation.vue'))
  ;({useNavStore}=await server.ssrLoadModule('/src/stores/nav.ts'))
})
after(async()=>{mock.restoreAll();await server?.close()})

async function renderLayout(path:string,pinia:ReturnType<typeof createPinia>,capture?:(page:any)=>void) {
  const page={render:()=>null}
  const router=createRouter({history:createMemoryHistory(),routes:[
    {path:'/',name:'projects',component:page},
    {path:'/projects/:uid',name:'pipeline',component:page},
    {path:'/projects/:uid/steps/:key',name:'step',component:page},
    {path:'/publications',name:'publications',component:page},
    {path:'/publications/:id',name:'publication-record',component:page},
    {path:'/settings',name:'settings',component:page},
    {path:'/help',name:'help',component:page},
    {path:'/about',name:'about',component:page},
    {path:'/projects/:uid/publish',name:'project-publish',component:page},
    {path:'/projects/:uid/comments',name:'project-comments',component:page},
  ]})
  await router.push(path)
  const app=createSSRApp({render:()=>h(MainLayout)})
  for(const [name,component] of Object.entries({'n-layout':NLayout,'n-layout-sider':NLayoutSider,'n-menu':NMenu,'n-alert':NAlert}))
    app.component(name,component)
  app.use(pinia);app.use(router);setup(app)
  if(capture)app.mixin({created(){if(this.$options.__name==='MainLayout')capture(this.$.setupState)}})
  return renderToString(app)
}

test('窄屏导航打开移入焦点，Tab首尾循环，Escape返回触发器，切页移交当前位置',async t=>{
  const pinia=createPinia(),nav=useNavStore(pinia);let page:any
  await renderLayout('/',pinia,value=>page=value)
  const original=Object.getOwnPropertyDescriptor(globalThis,'document')
  const documentState:any={activeElement:null}
  Object.defineProperty(globalThis,'document',{configurable:true,value:documentState})
  t.after(()=>{if(original)Object.defineProperty(globalThis,'document',original);else Reflect.deleteProperty(globalThis,'document');nav.reset()})
  const element=(name:string)=>markRaw({name,focus(){documentState.activeElement=this},closest:()=>null,getClientRects:()=>[{}]})
  const close=element('close'),link=element('link'),trigger=element('trigger'),location=element('location')
  page.narrowScreen=true;page.navigationClose=close;page.navigationTrigger=trigger;page.currentPageLabel=location
  page.navigationContent=markRaw({querySelectorAll:()=>[close,link]})
  await page.openMobileNavigation();assert.equal(documentState.activeElement,close)
  let prevented=0
  page.onNavigationKeydown({key:'Tab',shiftKey:true,preventDefault(){prevented++}})
  assert.equal(documentState.activeElement,link)
  page.onNavigationKeydown({key:'Tab',shiftKey:false,preventDefault(){prevented++}})
  assert.equal(documentState.activeElement,close);assert.equal(prevented,2)
  page.onNavigationKeydown({key:'Escape',preventDefault(){},stopPropagation(){}})
  await Promise.resolve();assert.equal(documentState.activeElement,trigger);assert.equal(page.mobileOpen,false)
  await page.openMobileNavigation();await page.closeMobileNavigation(true)
  assert.equal(documentState.activeElement,location)
  const opening=page.openMobileNavigation(),closing=page.closeMobileNavigation()
  await Promise.all([opening,closing]);assert.equal(documentState.activeElement,trigger)
})

test('实际主布局在全局页保留导航快照，返回同项目恢复读最新状态，换项目取消旧请求',async t=>{
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise(resolve=>requests.push({url,options,resolve})))
  const pinia=createPinia(), nav=useNavStore(pinia)
  nav.$patch({uid:'a',title:'已打开的工程',status:'done',steps:[{key:'assemble',selected:true,status:'done',fresh:true}]})
  try {
    const global=await renderLayout('/',pinia)
    assert.equal(nav.suspended,true);assert.equal(nav.uid,'a')
    assert.match(global,/已打开的工程/)
    assert.match(global,/显示最近状态 · 进入项目后刷新/)
    assert.match(productionLink(global,'a','translate'),/aria-label="状态待核对"/)
    assert.doesNotMatch(productionLink(global,'a','translate'),/正在读取|待运行|aria-busy/)
    assert.equal(requests.length,0)
    const same=await renderLayout('/projects/a/steps/assemble',pinia)
    assert.equal(nav.suspended,false);assert.equal(nav.uid,'a')
    assert.equal(requests.length,5)
    assert.equal(requests[0].url,'/api/projects/a/navigation')
    assert.doesNotMatch(same,/显示最近状态 · 进入项目后刷新/)
    await renderLayout('/settings',pinia)
    assert.equal(nav.suspended,true);assert.equal(nav.title,'已打开的工程')
    // 评论进度与工作区共用一次网络读取；离开项目只取消当前消费者。
    assert.ok(requests.filter(request=>!request.url.endsWith('/comments/progress')).every(request=>request.options.signal.aborted))
    await renderLayout('/projects/b',pinia)
    assert.equal(nav.suspended,false);assert.equal(nav.uid,'b')
    assert.equal(nav.title,'');assert.equal(requests.length,10)
    assert.equal(requests[5].url,'/api/projects/b/navigation')
    requests.find(request=>request.url==='/api/projects/a/comments/progress').resolve(new Response(JSON.stringify({workspace:{state:'done',label:'旧项目结果'}})))
    await new Promise(resolve=>setImmediate(resolve))
    assert.equal(nav.uid,'b');assert.equal(nav.commentState,null,'旧评论响应不得写入新项目')
  } finally {nav.reset()}
})

test('全局导航收拢发布入口，项目仍保留最后一步，记录页归属项目空间',async t=>{
  t.mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
  const pinia=createPinia(),nav=useNavStore(pinia)
  nav.$patch({uid:'a',title:'旅行视频',steps:[],publication:{status:'published',progress:100}})
  try {
    const records=await renderLayout('/publications',pinia)
    assert.doesNotMatch(records,/发布中心|href="\/publish"/)
    assert.match(records,/全部发布记录/)
    assert.match(records,/href="\/projects\/a\/publish"/)
    assert.match(records,/发布到B站/)
    assert.equal((records.match(/class="step-menu-number"/g)||[]).length,9)
    const retained=await renderLayout('/publications/retained',pinia)
    assert.match(retained,/aria-current="page"[^>]*>发布记录/)
    assert.match(retained,/项目空间/)
    assert.doesNotMatch(retained,/href="\/publish"/)
  } finally {nav.reset()}
})

test('实际主菜单显示九个阶段，评论合成发布编号7至9并使用独立路由',async t=>{
  t.mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
  const pinia=createPinia(),nav=useNavStore(pinia)
  nav.$patch({uid:'a',suspended:false,title:'测试视频',steps:[
    {key:'tts',status:'done',fresh:true},{key:'subtitles',status:'running',fresh:false},{key:'assemble',status:'pending',fresh:false}],
    commentState:{workspace:{state:'attention',label:'待确认上屏方案',detail:'候选已准备'}},
    publication:{status:'published',progress:100}})
  try {
    let layout:any
    const html=await renderLayout('/projects/a/comments',pinia,page=>layout=page)
    assert.equal((html.match(/class="step-menu-number"/g)||[]).length,9)
    assert.match(html,/<span class="step-menu-number"[^>]*title="上屏评论"[^>]*>7<\/span>/)
    assert.match(html,/<span class="step-menu-number"[^>]*title="合成成片"[^>]*>8<\/span>/)
    assert.match(html,/<span class="step-menu-number"[^>]*title="发布到B站"[^>]*>9<\/span>/)
    assert.match(html,/href="\/projects\/a\/comments"/);assert.match(html,/href="\/projects\/a\/publish"/)
    assert.doesNotMatch(html,/href="\/projects\/a\/steps\/(comments|publish|subtitles|dubtask)"/)
    assert.match(html,/aria-label="待确认上屏方案"/)
    assert.equal(layout.projActive,'comments');assert.equal(layout.pageName,'上屏评论')
    await renderLayout('/projects/a/publish',pinia,page=>layout=page)
    assert.equal(layout.projActive,'publish');assert.equal(layout.pageName,'发布到B站')
  } finally {nav.reset()}
})

function productionLink(html:string,uid:string,key:string) {
  const link=html.match(new RegExp(`<a\\b[^>]*href="/projects/${uid}/steps/${key}"[^>]*>[\\s\\S]*?</a>`))?.[0]
  assert.ok(link,`实际侧栏必须包含 ${key} 步骤链接`)
  return link
}

test('发布路由的侧栏在终态待核验期间保持中性，不声称合成运行或产物最新',async t=>{
  t.mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
  const pinia=createPinia(),nav=useNavStore(pinia),uid='terminal-sidebar'
  nav.$patch({uid,title:'交接核验',stepsLoaded:true,steps:[{key:'assemble',status:'checking',job_id:19,fresh:null,
    execution:{schema:1,owner:'step',job_id:19,state:'completed'}}]})
  try {
    const html=await renderLayout(`/projects/${uid}/publish`,pinia)
    const link=productionLink(html,uid,'assemble')
    assert.match(link,/任务已结束，产物核验中/);assert.match(link,/nav-status-checking/)
    assert.doesNotMatch(link,/nav-status-running|运行中|✓ 完成|已是最新|已过期/)
    assert.equal(nav.busy,true)
  } finally {nav.reset()}
})
const settle=()=>new Promise(resolve=>setImmediate(resolve))

test('实际首屏步骤慢读期间显示正在读取，成功后才显示确认的待运行和完成状态',async t=>{
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise((resolve,reject)=>requests.push({url:String(url),options,resolve,reject})))
  const pinia=createPinia(),nav=useNavStore(pinia),uid='initial-load'
  try {
    const initial=await renderLayout(`/projects/${uid}`,pinia)
    assert.equal(nav.stepsLoaded,false)
    for(const key of ['download','preprocess','transcribe','split','translate','tts','assemble']){
      const link=productionLink(initial,uid,key)
      assert.match(link,/aria-label="正在读取"/);assert.match(link,/aria-busy="true"/)
      assert.doesNotMatch(link,/待运行|nav-status-pending|完成/)
    }
    requests.find(row=>row.url.endsWith('/steps')).resolve(new Response(JSON.stringify([
      {key:'download',status:'done',fresh:true},{key:'preprocess',status:'pending',fresh:false},
    ])))
    await settle()
    const loaded=await renderLayout(`/projects/${uid}`,pinia)
    assert.equal(nav.stepsLoaded,true)
    assert.match(productionLink(loaded,uid,'download'),/aria-label="✓ 完成"/)
    assert.match(productionLink(loaded,uid,'preprocess'),/aria-label="待运行"/)
    const missing=productionLink(loaded,uid,'translate')
    assert.match(missing,/aria-label="状态待核对"/);assert.doesNotMatch(missing,/待运行|aria-busy/)
    assert.match(missing,/当前响应尚未提供该步骤状态/)
  } finally {nav.reset()}
})

test('首次步骤读取失败不会显示待运行，重试成功后恢复真实完成状态',async t=>{
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>new Promise((resolve,reject)=>requests.push({url:String(url),options,resolve,reject})))
  const pinia=createPinia(),nav=useNavStore(pinia),uid='initial-failure'
  try {
    await renderLayout(`/projects/${uid}`,pinia)
    requests.find(row=>row.url.endsWith('/steps')).resolve(new Response(JSON.stringify({message:'状态服务暂不可用'}),{status:503}))
    await settle()
    assert.equal(nav.stepsLoaded,false);assert.match(nav.stepsError,/状态服务暂不可用/)
    const failed=productionLink(await renderLayout(`/projects/${uid}`,pinia),uid,'download')
    assert.match(failed,/aria-label="状态读取失败"/);assert.match(failed,/nav-status-unavailable/)
    assert.match(failed,/状态服务暂不可用/);assert.doesNotMatch(failed,/待运行|nav-status-failed|aria-busy/)
    const retry=nav.refreshSteps()
    requests.filter(row=>row.url.endsWith('/steps')).at(-1).resolve(new Response(JSON.stringify([{key:'download',status:'done',fresh:true}])))
    await retry
    const recovered=productionLink(await renderLayout(`/projects/${uid}`,pinia),uid,'download')
    assert.match(recovered,/aria-label="✓ 完成"/);assert.doesNotMatch(recovered,/状态读取失败|nav-status-unavailable/)
  } finally {nav.reset()}
})

test('完整状态尚未返回或后来读取失败时，已有完成和活动执行快照仍优先且不改写领域状态',async t=>{
  t.mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
  const pinia=createPinia(),nav=useNavStore(pinia),uid='retained-snapshot'
  const steps=[{key:'download',status:'done',fresh:true},{key:'tts',status:'running',fresh:false,job_id:42}]
  nav.$patch({uid,suspended:false,stepsLoaded:false,steps:structuredClone(steps)})
  try {
    const initial=await renderLayout(`/projects/${uid}`,pinia)
    assert.match(productionLink(initial,uid,'download'),/aria-label="✓ 完成"/)
    assert.match(productionLink(initial,uid,'tts'),/aria-label="运行中"/)
    nav.stepsError='步骤状态未更新：请求超时。保留上次核对结果。';nav.stepsLoaded=true
    const failed=await renderLayout(`/projects/${uid}`,pinia)
    for(const [key,label]of [['download','✓ 完成'],['tts','运行中']]){
      const link=productionLink(failed,uid,key)
      assert.ok(link.includes(`aria-label="${label}"`));assert.match(link,/保留上次核对结果/)
      assert.doesNotMatch(link,/aria-label="状态读取失败"|nav-status-unavailable|待运行/)
    }
    assert.deepEqual(nav.steps,steps)
  } finally {nav.reset()}
})

test('实际阶段导航显示7至9的连续编号，前后链接和设置深链落到对应工作区',async()=>{
  for(const [stage,number,previous,next] of [
    ['tts',6,'/projects/a%20b/steps/translate','/projects/a%20b/comments'],
    ['comments',7,'/projects/a%20b/steps/tts','/projects/a%20b/steps/assemble'],
    ['assemble',8,'/projects/a%20b/comments','/projects/a%20b/publish'],
    ['publish',9,'/projects/a%20b/steps/assemble',null],
  ] as const){
    const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
    await router.push('/')
    const app=createSSRApp({render:()=>h(StageNavigation,{uid:'a b',stage})});app.use(router)
    const html=await renderToString(app)
    assert.ok(html.includes(`第 ${number} / 9 阶段`),stage)
    assert.ok(html.includes(`href="${previous}"`),stage)
    if(next)assert.ok(html.includes(`href="${next}"`),stage)
    else assert.doesNotMatch(html,/下一步/)
    assert.ok(html.includes(`href="/settings?section=production&amp;stage=${stage}"`),stage)
    assert.match(html,/aria-label="工作流阶段导航"/)
  }
})
