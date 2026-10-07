import assert from 'node:assert/strict'
import test, { before, after, beforeEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter } from 'vue-router'

let server:ViteDevServer, Panel:any
const saved = new Map<string,string>(), storageDescriptor = Object.getOwnPropertyDescriptor(globalThis,'sessionStorage')
const recoveryKey='dubforge.project-import.pending'
const catalog={max_bytes:1048576,templates:[{key:'series:a',name:'旅行'},{key:'series:b',name:'美食'},{key:'series:x',name:'已归档',archived:true}],
  sample:{schema:'dubforge.batch.v1',defaults:{template:'series:a',settings:{}},projects:[]}}
const row=(index:number)=>({index,title:'项目'+index,source:'https://example.com/'+index,template:'series:a',settings:{'comments.count':5},selectable:true,errors:[],warnings:[],duplicates:[]})
const plan=()=>({items:[row(0),row(1)],selected:[0,1],fingerprint:'preview-fingerprint',total:2})
const receipt=(id:string, statuses=['created','created'])=>({request_id:id,complete:statuses.every(s=>s==='created'),created:statuses.filter(s=>s==='created').length,
  items:statuses.map((status,index)=>({...row(index),status,uid:status==='created'?'p'+index:'',error:status==='error'?'测试失败':''}))})
const ok=(value:any)=>({ok:true,json:async()=>value})
function deferred(){let done:any;const promise=new Promise(resolve=>{done=resolve});return {promise,done}}
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/BatchProjectImport.vue')).default
  Object.defineProperty(globalThis,'sessionStorage',{configurable:true,value:{getItem:(k:string)=>saved.get(k)??null,setItem:(k:string,v:string)=>saved.set(k,v),removeItem:(k:string)=>saved.delete(k)}})
  mock.method(globalThis,'fetch',()=>{throw new Error('不允许未声明的网络请求')})
})
beforeEach(()=>saved.clear())
after(async()=>{
  mock.restoreAll();await server.close()
  if(storageDescriptor)Object.defineProperty(globalThis,'sessionStorage',storageDescriptor)
  else delete (globalThis as any).sessionStorage
})
async function render(seed:(page:any)=>void=()=>{}){
  let page:any;const emitted:string[][]=[],selections:string[][]=[]
  const app=createSSRApp({render:()=>h(Panel,{onCreated:(uids:string[])=>emitted.push(uids),onSelectCreated:(uids:string[])=>selections.push(uids)})})
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:{render:()=>null}},{path:'/projects/:uid',component:{render:()=>null}}]})
  await router.push('/');app.use(router)
  setup(app)
  app.mixin({created(){if(this.$options.__name==='BatchProjectImport'){page=this.$.setupState;seed(page)}}})
  const context:any={};const html=await renderToString(app,context)+Object.values(context.teleports||{}).join('')
  return {page,emitted,selections,html}
}

test('打开只读取模板；归档模板不可选，不会启动项目',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,options?.method]);return ok(String(url).endsWith('/history')?{items:[]}:catalog)})
  await page.open()
  assert.deepEqual(calls,[['/api/project-import/catalog','GET'],['/api/project-import/history','GET']])
  assert.deepEqual(page.options.map((o:any)=>o.value),['','series:a','series:b'])
})
test('输入变化清空预览，迟到的响应不覆盖新输入，也不卡住解析按钮',async t=>{
  const {page}=await render();const pending=deferred()
  t.mock.method(globalThis,'fetch',()=>pending.promise as any)
  page.changeText('https://example.com/old');const parsing=page.inspect()
  assert.equal(page.inspecting,true)
  page.changeText('https://example.com/new')
  assert.equal(page.inspecting,false)
  pending.done(ok(plan()));await parsing
  assert.equal(page.plan,null);assert.deepEqual(page.selected,[])
})
test('逐项切换模板保留勾选；默认模板仅作为清单未指定项的后备',async t=>{
  const {page}=await render(p=>{p.text='https://example.com/0\nhttps://example.com/1';p.plan=plan();p.selected=[1];p.defaultTemplate='series:a'})
  const inputs:any[]=[]
  t.mock.method(globalThis,'fetch',async(_url,options)=>{inputs.push(JSON.parse(String(options?.body)));return ok(plan())})
  await page.changeRow(1,'series:b')
  assert.equal(inputs[0].default_template,'series:a')
  assert.deepEqual(inputs[0].template_overrides,{'1':'series:b'})
  assert.deepEqual(page.selected,[1])
  page.changeDefault(null);assert.equal(page.plan,null)
})
test('文件只在浏览器读成UTF-8文本，自动预览；拒绝过大和错误编码',async t=>{
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([url,JSON.parse(String(options?.body))]);return ok(plan())})
  await page.readFile(new File(['\ufeffhttps://example.com/0'],'清单.txt'))
  assert.equal(page.filename,'清单.txt')
  assert.equal(calls[0][0],'/api/project-import/preview')
  assert.equal(calls[0][1].text,'https://example.com/0')
  await page.readFile(new File(['ignored'],'video.mp4'))
  assert.match(page.error,/json.*txt/)
  await page.readFile(new File([new Uint8Array([0xff])],'bad.txt'))
  assert.match(page.error,/UTF-8/)
  await page.readFile({name:'huge.txt',size:1048577} as File)
  assert.match(page.error,/1MB/);assert.equal(calls.length,1)
})
test('确认才创建，双击只提交一次，并持久化请求标识',async t=>{
  const {page,emitted}=await render(p=>{p.text='https://example.com/0';p.plan=plan();p.selected=[0,1]})
  const pending=deferred(), writes:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{writes.push([url,JSON.parse(String(options?.body))]);return pending.promise as any})
  const creating=page.create();await page.create()
  assert.equal(writes.length,1)
  assert.equal(writes[0][0],'/api/project-import/create')
  assert.equal(writes[0][1].confirmed,true)
  assert.equal(saved.has(recoveryKey),true)
  const id=page.request.request_id
  page.changeText('不允许修改');assert.equal(page.text,'https://example.com/0')
  pending.done(ok(receipt(id)));await creating
  assert.deepEqual(emitted,[['p0','p1']])
  assert.equal(page.result.complete,true);assert.equal(saved.has(recoveryKey),false)
})
test('迟到轮询不会把创建成功改回待创建',async t=>{
  const {page}=await render(p=>{p.text='https://example.com/0';p.plan=plan();p.selected=[0,1]})
  const posting=deferred(), polling=deferred()
  t.mock.method(globalThis,'fetch',(url)=>String(url).endsWith('/create')?posting.promise as any:polling.promise as any)
  const creating=page.create(), checking=page.checkStatus(), id=page.request.request_id
  posting.done(ok(receipt(id)));await creating
  polling.done(ok(receipt(id,['pending','pending'])));await checking
  assert.equal(page.result.complete,true);assert.equal(page.progress,100)
})
test('未知网络结果禁止另开一批，安全重试保留相同标识',async t=>{
  const {page}=await render(p=>{p.text='https://example.com/0';p.plan=plan();p.selected=[0,1]})
  const writes:any[]=[];let broken=true
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/create')){const body=JSON.parse(String(options?.body));writes.push(body);if(broken)throw new Error('连接中断');return ok(receipt(body.request_id))}
    return ok(receipt(page.request.request_id,['created','pending']))
  })
  await page.create();const id=page.request.request_id
  assert.equal(page.canRestart,false);page.restart();assert.equal(page.request.request_id,id)
  broken=false;await page.create()
  assert.equal(writes[1].request_id,writes[0].request_id);assert.equal(page.result.complete,true)
})
test('模板冲突是确定的拒绝，恢复编辑且要求重新预览',async t=>{
  const {page}=await render(p=>{p.text='https://example.com/0';p.plan=plan();p.selected=[0]})
  t.mock.method(globalThis,'fetch',async()=>({ok:false,json:async()=>({code:'PROJECT_IMPORT_CONFLICT',message:'模板已变化，请重新预览'})}))
  await page.create()
  assert.equal(page.request,null);assert.equal(page.plan,null)
  assert.match(page.error,/重新预览/);assert.equal(saved.has(recoveryKey),false)
})
test('重新打开恢复同批回执；只将失败项目转为新清单且保留稀疏覆盖',async t=>{
  const id='d4679a7d-6ad9-4c69-a2cc-e813b05af916'
  saved.set(recoveryKey,JSON.stringify({request_id:id,input:{text:'https://example.com/0\nhttps://example.com/1',default_template:null},selected:[0,1]}))
  const {page}=await render();const previews:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/catalog'))return ok(catalog)
    if(String(url).endsWith('/history'))return ok({items:[]})
    if(String(url).endsWith('/preview')){previews.push(JSON.parse(String(options?.body)));return ok(plan())}
    return ok(receipt(id,['created','error']))
  })
  await page.open();assert.equal(page.result.created,1)
  assert.equal(page.canRestart,true)
  page.retryFailedAsNew()
  const projects=JSON.parse(previews[0].text).projects
  assert.equal(projects.length,1);assert.equal(projects[0].source,'https://example.com/1')
  assert.deepEqual(projects[0].settings,{'comments.count':5})
  assert.equal(projects[0].template,'series:a');assert.equal(page.request,null)
})

test('丢失浏览器会话仍可从最近导入恢复，只提交已冻结批次的继续请求',async t=>{
  const id='d4679a7d-6ad9-4c69-a2cc-e813b05af916'
  const {page}=await render();const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    calls.push([String(url),options?.body?JSON.parse(String(options.body)):null])
    if(String(url).endsWith('/catalog'))return ok(catalog)
    if(String(url).endsWith('/history'))return ok({items:[{request_id:id,created:1,total:2,complete:false}]})
    if(String(url).endsWith('/resume'))return ok(receipt(id))
    return ok(receipt(id,['created','pending']))
  })
  await page.open();assert.equal(page.recentImports.length,1)
  await page.restoreBatch(id)
  assert.equal(page.request.resume,true);assert.equal(page.request.input,undefined)
  await page.create()
  assert.deepEqual(calls.find(c=>c[0].endsWith('/resume')),['/api/project-import/'+id+'/resume',{confirmed:true}])
  assert.equal(calls.some(c=>c[0].endsWith('/create')),false)
  assert.equal(page.result.complete,true)
})
test('损坏的会话恢复数据不会锁死导入，样板沿用当前选择而非第一份模板',async t=>{
  saved.set(recoveryKey,'{}')
  const {page}=await render()
  t.mock.method(globalThis,'fetch',async(url)=>ok(String(url).endsWith('/history')?{items:[]}:catalog))
  await page.open();assert.equal(page.request,null)
  page.changeDefault('series:b');assert.equal(page.sample.defaults.template,'series:b')
  page.changeDefault(null);assert.equal(page.sample.defaults.template,null)
})
test('后台仍在创建的恢复批次只能查看进度，不另建或并发继续',async t=>{
  const {page}=await render(p=>{p.request={request_id:'batch',selected:[0],resume:true};p.result={...receipt('batch',['pending']),running:true}})
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url)=>{calls.push(url);return ok({})})
  await page.create();page.restart()
  assert.equal(page.canRestart,false);assert.equal(page.canResume,false);assert.deepEqual(calls,[])
})

test('预览后收起输入清单，创建后仅由用户操作选中项目，不自动运行',async()=>{
  const preview=await render(p=>{p.show=true;p.plan=plan()})
  assert.match(preview.html,/<details class="import-input"[^>]*>/)
  assert.doesNotMatch(preview.html,/<details class="import-input"[^>]* open/)
  const {page,selections}=await render(p=>{p.show=true;p.request={request_id:'batch',selected:[0,1]};p.result=receipt('batch')})
  assert.deepEqual(selections,[])
  page.selectCreated();assert.deepEqual(selections,[['p0','p1']]);assert.equal(page.show,false)
})

test('已恢复回执后来不存在时退出恢复状态，不锁死新导入',async t=>{
  const {page}=await render(p=>{p.request={request_id:'batch',selected:[0],resume:true};p.result=receipt('batch',['pending'])})
  t.mock.method(globalThis,'fetch',async(url)=>String(url).endsWith('/history')?ok({items:[]}):
    {ok:false,json:async()=>({code:'PROJECT_IMPORT_NOT_FOUND',message:'没有此批导入记录'})})
  await page.checkStatus();assert.equal(page.request,null);assert.equal(page.result,null);assert.match(page.error,/先核对项目列表/)
})

test('网络中断显示中文提示，不把连接失败断言为步骤已停止',async t=>{
  const {page}=await render()
  t.mock.method(globalThis,'fetch',async()=>{throw new TypeError('Failed to fetch')})
  await page.open();assert.match(page.error,/无法连接后端服务/);assert.doesNotMatch(page.error,/Failed to fetch|已.*中断/)
})
