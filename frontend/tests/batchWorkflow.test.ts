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
import { defaultDubbingRunPolicy } from '../src/utils/dubbingRunPolicy.ts'

let server:ViteDevServer, Panel:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
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
  const context:any={}
  const html=await renderToString(app,context)+Object.values(context.teleports || {}).join('');return {page,html}
}
test('批量打开仅预检，不要求权利勾选且不启动任何项目',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([url,JSON.parse(String(options?.body))]);return {ok:true,json:async()=>({items:[item('one','publish'),item('two','draft')]})}
  })
  await page.open()
  assert.deepEqual(calls,[['/api/configuration/batch-plan',{uids:['one','two'],target_driven:true}]])
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

test('批量保留每个项目的上传和原稿终点，不把上传等待算成投稿',async()=>{
  const upload=item('one','upload')
  const update=item('two','update');update.plan.recipe={...update.plan.recipe,delivery_mode:'upload'} as any
  const {page}=await render(page=>page.items=[upload,update])
  assert.equal(page.ready.length,2);assert.equal(page.publishCount,0)
  assert.equal(page.confirmLabel,'确认运行 2 个项目（不投稿）')
  page.items=[{...update,plan:{...update.plan,recipe:{...update.plan.recipe,delivery_mode:'publish'}}}]
  assert.equal(page.publishCount,1)
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
  assert.equal(page.ready.length,1);assert.equal(page.results[0].run_id,'run-one')
})
test('部分项目启动失败保留失败项，重试不再提交成功项且保留请求标识',async t=>{
  const {page}=await render(p=>p.items=[item('one','produce'),item('two','produce')])
  const writes:any[]=[];let first=true
  t.mock.method(globalThis,'fetch',async(_url,options)=>{
    writes.push(JSON.parse(String(options?.body)))
    const items=first?[{uid:'one',run_id:'run-one'},{uid:'two',error:'临时失败'}]:[{uid:'two',run_id:'run-two'}]
    first=false;return {ok:true,json:async()=>({items})}
  })
  await page.start();assert.deepEqual(page.ready.map((r:any)=>r.uid),['two'])
  await page.start()
  assert.deepEqual(writes[1].items.map((r:any)=>r.uid),['two'])
  assert.equal(writes[1].request_id,writes[0].request_id)
  assert.equal(page.ready.length,0);assert.equal(page.results.filter((r:any)=>r.run_id).length,2)
})
test('网络失败重试仍使用最初冻结的运行参数',async t=>{
  const {page}=await render(p=>p.items=[item('one','produce')])
  const writes:any[]=[];let first=true
  t.mock.method(globalThis,'fetch',async(_url,options)=>{
    writes.push(JSON.parse(String(options?.body)))
    if(first){first=false;throw new Error('断线')}
    return {ok:true,json:async()=>({items:[{uid:'one',run_id:'run-one'}]})}
  })
  await page.start();page.items[0].plan.recipe.part_titles.a='请求后改变'
  await page.start();assert.deepEqual(writes[1],writes[0])
})
test('预检失败清空旧结果，不允许用过期清单启动',async t=>{
  const {page}=await render(p=>p.items=[item('one','publish')])
  const calls:string[]=[]
  t.mock.method(globalThis,'fetch',async(url)=>{calls.push(String(url));return {ok:false,json:async()=>({message:'预检失败'})}})
  await page.inspect();await page.start()
  assert.equal(page.ready.length,0);assert.equal(page.publishCount,0)
  assert.equal(page.error,'预检失败');assert.deepEqual(calls,['/api/configuration/batch-plan'])
})

test('批量检查使用统一可读发布目标，运行回执保留项目标题',async t=>{
  const {page,html}=await render(page=>{page.show=true;page.items=[{...item('one','publish'),title:'标题项目',
    plan:{...item('one','publish').plan,recipe:{mode:'publish',part_titles:{a:'原声版'},account_id:'12'},settings:{season_id:34,section_id:56},
      settings_display:{account_id:'12',season_id:34,section_id:56,season_title:'旅行合集',section_title:'正篇'}}}]})
  assert.match(html,/旅行合集/);assert.match(html,/正篇/);assert.match(html,/class="target-id"/)
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>({items:[{uid:'one',run_id:'run-one'}]})}))
  await page.start();assert.equal(page.results[0].title,'标题项目')
})
