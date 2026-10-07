import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia } from 'pinia'
import { NAlert, NLayout, NLayoutSider, NMenu } from 'naive-ui'
import { createMemoryHistory, createRouter } from 'vue-router'

let server:ViteDevServer, MainLayout:any, useNavStore:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],
    appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  ;({default:MainLayout}=await server.ssrLoadModule('/src/layouts/MainLayout.vue'))
  ;({useNavStore}=await server.ssrLoadModule('/src/stores/nav.ts'))
})
after(async()=>{mock.restoreAll();await server?.close()})

async function renderLayout(path:string,pinia:ReturnType<typeof createPinia>) {
  const page={render:()=>null}
  const router=createRouter({history:createMemoryHistory(),routes:[
    {path:'/',name:'projects',component:page},
    {path:'/projects/:uid',name:'pipeline',component:page},
    {path:'/projects/:uid/steps/:key',name:'step',component:page},
    {path:'/publish',name:'publish',component:page},
    {path:'/settings',name:'settings',component:page},
    {path:'/help',name:'help',component:page},
    {path:'/about',name:'about',component:page},
    {path:'/projects/:uid/publish',name:'project-publish',component:page},
  ]})
  await router.push(path)
  const app=createSSRApp({render:()=>h(MainLayout)})
  for(const [name,component] of Object.entries({'n-layout':NLayout,'n-layout-sider':NLayoutSider,'n-menu':NMenu,'n-alert':NAlert}))
    app.component(name,component)
  app.use(pinia);app.use(router);setup(app)
  return renderToString(app)
}

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
    assert.equal(requests.length,0)
    const same=await renderLayout('/projects/a/steps/assemble',pinia)
    assert.equal(nav.suspended,false);assert.equal(nav.uid,'a')
    assert.equal(requests.length,4)
    assert.equal(requests[0].url,'/api/projects/a/navigation')
    assert.doesNotMatch(same,/显示最近状态 · 进入项目后刷新/)
    await renderLayout('/settings',pinia)
    assert.equal(nav.suspended,true);assert.equal(nav.title,'已打开的工程')
    assert.ok(requests.every(request=>request.options.signal.aborted))
    await renderLayout('/projects/b',pinia)
    assert.equal(nav.suspended,false);assert.equal(nav.uid,'b')
    assert.equal(nav.title,'');assert.equal(requests.length,8)
    assert.equal(requests[4].url,'/api/projects/b/navigation')
  } finally {nav.reset()}
})
