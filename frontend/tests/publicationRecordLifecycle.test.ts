import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h,reactive} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
let server:ViteDevServer,Manager:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Manager=(await server.ssrLoadModule('/src/components/PublicationRecordLifecycle.vue')).default
})
after(async()=>{mock.restoreAll();await server.close()})
function row(lifecycle='active') {return {id:'draft',revision:4,status:'draft',lifecycle,draft:{content:{title:'本地草稿'}},snapshot:{},lifecycle_actions:{archive:{allowed:lifecycle==='active',reason:''},delete:{allowed:lifecycle==='active',reason:''},restore:{allowed:lifecycle!=='active',reason:''}}}}
async function render(record:any,disabled=false){
  let page:any;const events:any[]=[]
  const props=reactive({record,disabled})
  const app=createSSRApp({render:()=>h(Manager,{...props,onChanged:(record:any)=>events.push(['changed',record]),onBusyChange:(busy:boolean)=>events.push(['busy',busy])})});setup(app)
  app.mixin({created(){if(this.$options.__name==='PublicationRecordLifecycle')page=this.$.setupState}})
  const html=await renderToString(app);return {page,props,events,html}
}
test('回收只在明确确认后发送带版本号的本地请求，双击只发送一次',async t=>{
  const {page,events}=await render(row());let release:any;const calls:any[]=[]
  t.mock.method(globalThis,'fetch',(url:any,options:any)=>{calls.push({url,options});return new Promise(resolve=>release=()=>resolve({ok:true,json:async()=>({...row('deleted'),revision:5})}))})
  page.prepare('delete');assert.equal(calls.length,0);assert.match(page.notice,/不是系统回收站/)
  const first=page.confirm();await page.confirm();assert.equal(calls.length,1)
  assert.equal(calls[0].url,'/api/publishing/publications/draft');assert.equal(calls[0].options.method,'DELETE')
  assert.deepEqual(JSON.parse(calls[0].options.body),{revision:4,confirmed:true})
  release();await first;assert.equal(page.pending,null);assert.equal(events.filter(event=>event[0]==='changed').length,1)
})
test('有上传或BV的记录不能回收，可归档且保留平台身份说明',async t=>{
  const record={...row(),status:'published',receipt:{bvid:'BVkeep'}};record.lifecycle_actions.delete={allowed:false,reason:'已有平台回执，请归档'}
  const {page,html}=await render(record)
  assert.match(html,/已有平台回执，请归档/)
  page.prepare('delete');assert.equal(page.pending,null)
  page.prepare('archive');assert.match(page.notice,/BV号.*防重复投稿回执均保留/)
  const calls:any[]=[];t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{calls.push({url,options});return {ok:true,json:async()=>({...record,lifecycle:'archived'})}})
  await page.confirm();assert.equal(calls[0].url,'/api/publishing/publications/draft/archive')
})
test('归档和回收恢复为常用记录，按钮不会重新上传投稿',async t=>{
  const {page,html}=await render(row('deleted'));assert.match(html,/恢复记录/);assert.doesNotMatch(html,/回收草稿|归档本地记录/)
  page.prepare('restore');assert.match(page.notice,/不会上传、重新投稿/)
  const calls:any[]=[];t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{calls.push({url,options});return {ok:true,json:async()=>row()}})
  await page.confirm();assert.equal(calls[0].url,'/api/publishing/publications/draft/restore')
  assert.ok(calls.every(call=>!call.url.includes('/actions')))
})
test('状态或锁在确认前变化时不发送写请求，错误保留在确认面板',async()=>{
  const {page,props}=await render(row());page.prepare('delete');props.record.revision=5
  await page.confirm();assert.match(page.error,/状态已变化/)
  const locked=await render(row(),true);locked.page.prepare('delete');await locked.page.confirm();assert.equal(locked.page.pending,null);assert.equal(locked.page.busy,false)
})
test('运行保护原因可查看，所有管理入口在运行时关闭',async()=>{
  const record=row();for(const action of Object.values(record.lifecycle_actions)){action.allowed=false;action.reason='正在顺序发布，结束后可管理'}
  const {page,html}=await render(record);assert.match(html,/正在顺序发布/)
  page.prepare('archive');page.prepare('delete');assert.equal(page.pending,null)
})
