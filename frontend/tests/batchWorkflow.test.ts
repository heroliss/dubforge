import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'

let server:ViteDevServer, Panel:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null}})
  Panel=(await server.ssrLoadModule('/src/components/BatchWorkflow.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('不允许未声明网络请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
const item=(uid:string,mode:string,blockers:string[]=[])=>({uid,title:uid,blockers,
  plan:{recipe:{mode,part_titles:{a:'原声版'}},fingerprint:'fingerprint-'+uid,
    version_specs:[{id:'a',name:'原声版'}],account_name:'隔离测试账号',settings:{visibility:'public',season_id:null}}})
async function render(seed:(p:any)=>void=()=>{}){
  let page:any
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}}]})
  await router.push('/')
  const app=createSSRApp({render:()=>h(Panel,{uids:['one','two']})})
  app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='BatchWorkflow'){page=this.$.setupState;seed(page)}}})
  const html=await renderToString(app);return {page,html}
}
test('批量打开仅预检，不要求权利勾选且不启动任何项目',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([url,JSON.parse(String(options?.body))]);return {ok:true,json:async()=>({items:[item('one','publish'),item('two','draft')]})}
  })
  await page.open()
  assert.deepEqual(calls,[['/api/configuration/batch-plan',{uids:['one','two'],mode:null}]])
  assert.equal(page.ready.length,2);assert.equal(page.publishCount,1)
  assert.equal(page.confirmLabel,'确认运行 2 个项目并投稿 1 个')
  const source=readFileSync(new URL('../src/components/BatchWorkflow.vue',import.meta.url),'utf8')
  assert.doesNotMatch(source,/rights_confirmed|n-checkbox|已核对/)
  assert.match(source,/PublicationRightsHint v-if="publishCount"/)
  assert.match(source,/\{\{ confirmLabel \}\}/)
})
test('投稿数量只统计检查通过的项目，纯制作或草稿明确显示不投稿',async()=>{
  const {page}=await render(p=>p.items=[item('one','produce'),item('two','draft'),item('blocked','publish',['缺少封面'])])
  assert.equal(page.ready.length,2);assert.equal(page.publishCount,0)
  assert.equal(page.confirmLabel,'确认运行 2 个项目（不投稿）')
  page.items=[item('one','publish'),item('two','publish')]
  assert.equal(page.confirmLabel,'确认运行 2 个项目并投稿 2 个')
})
test('点击后才确认冻结清单，跳过受阻项目，双击不会重复启动',async t=>{
  const rows=[item('one','publish'),item('two','produce'),item('blocked','publish',['缺少封面'])]
  const {page}=await render();const writes:any[]=[];let resolve:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/batch-plan'))return {ok:true,json:async()=>({items:rows})}
    writes.push(JSON.parse(String(options?.body)))
    return new Promise(done=>{resolve=()=>done({ok:true,json:async()=>({items:[{uid:'one',run_id:'run-one'}]})})})
  })
  await page.open();assert.equal(writes.length,0)
  const starting=page.start();await page.start()
  page.items[0].plan.recipe.part_titles.a='后来修改'
  assert.equal(writes.length,1);assert.equal(writes[0].confirmed,true)
  assert.equal(writes[0].request_id.length,36)
  assert.deepEqual(writes[0].items.map((i:any)=>i.uid),['one','two'])
  assert.equal(writes[0].items[0].recipe.part_titles.a,'原声版')
  assert.equal(writes[0].items[0].fingerprint,'fingerprint-one')
  resolve();await starting
  assert.equal(page.ready.length,0);assert.equal(page.results[0].run_id,'run-one')
})
test('预检失败清空旧结果，不允许用过期清单启动',async t=>{
  const {page}=await render(p=>p.items=[item('one','publish')])
  const calls:string[]=[]
  t.mock.method(globalThis,'fetch',async(url)=>{calls.push(String(url));return {ok:false,json:async()=>({message:'预检失败'})}})
  await page.inspect();await page.start()
  assert.equal(page.ready.length,0);assert.equal(page.publishCount,0)
  assert.equal(page.error,'预检失败');assert.deepEqual(calls,['/api/configuration/batch-plan'])
})
