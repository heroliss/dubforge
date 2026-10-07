import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { conversationRows } from '../src/utils/llmConversation.ts'

let server:ViteDevServer, Panel:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/CommentDiagnostics.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('不允许未声明请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
async function render(seed:(p:any)=>void=()=>{}){
  let page:any
  const app=createSSRApp({render:()=>h(Panel,{uid:'fixture'})});setup(app)
  app.mixin({created(){if(this.$options.__name==='CommentDiagnostics'){page=this.$.setupState;seed(page)}}})
  return {html:await renderToString(app),page}
}
test('流式增量与完整回复合并，重复系统提示去重，不把历史回复当成当前输出',()=>{
  const rows=conversationRows([
    {event:'request',role:'system',content:'任务说明',retry_group:'g'},
    {event:'request',role:'system',content:'任务说明',retry_group:'g'},
    {event:'request',role:'assistant',content:'历史内容'},
    {event:'assistant_delta',request_id:'r',content:'中文'},
    {event:'assistant_delta',request_id:'r',content:'回复'},
    {event:'reasoning_delta',request_id:'r',content:'服务商返回的思考'},
    {event:'assistant_response',request_id:'r',content:'完整中文回复'},
    {event:'validation_error',content:'缺少 ID 3'},
    {event:'retry_strategy',content:'仅补缺失项'},
  ])
  assert.equal(rows.filter(r=>r.role==='system').length,1)
  assert.equal(rows.find(r=>r.role==='assistant')?.content,'完整中文回复')
  assert.equal(rows.find(r=>r.role==='history')?.content,'历史内容')
  assert.equal(rows.at(-1)?.role,'retry_strategy')
})
test('服务商结束原因保留在对话中，不会显示成模型正文或格式纠错',()=>{
  const rows=conversationRows([
    {event:'response_metadata',content:'响应结束：length',finish_reason:'length'},
    {event:'provider_error',content:'模型输出被长度限制截断',code:'LLM_OUTPUT_TRUNCATED'},
  ])
  assert.deepEqual(rows.map(r=>r.role),['response_metadata','provider_error'])
  assert.equal(rows[1].code,'LLM_OUTPUT_TRUNCATED')
})
test('服务错误即使没有 model 字段也标出所属配置与错误码',async()=>{
  const {html}=await render(p=>{
    p.events=[{event:'provider_error',profile:'主配置',content:'内容审核拒绝',code:'LLM_CONTENT_REJECTED'},
      {event:'response_metadata',profile:'备用配置',content:'响应结束：stop'}]
  })
  assert.match(html,/主配置/);assert.match(html,/备用配置/);assert.match(html,/LLM_CONTENT_REJECTED/)
})
test('真实组件并列呈现日志与对话，错误可见，模型内容按文本转义',async()=>{
  const {html}=await render(p=>{
    p.job={status:'failed',processed:8,total:10,cached:60,code:'LLM_OUTPUT_INVALID',error:'缺少两条评论',suggestion:'可以继续补齐'}
    p.lines=['[12:00:00] 已保存 8 条']
    p.events=[{event:'request',role:'system',content:'系统任务'},{event:'assistant_response',request_id:'r',content:'<script>危险文本</script>'}]
  })
  assert.match(html,/评论运行日志/);assert.match(html,/LLM 对话/);assert.match(html,/LLM_OUTPUT_INVALID/)
  assert.match(html,/复用 60 条/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>危险文本/)
  assert.match(html,/<details/);assert.match(html,/评论评分/);assert.match(html,/评论翻译/)
  const source=readFileSync(new URL('../src/views/StepView.vue',import.meta.url),'utf8')
  assert.match(source,/detailSection === 'logs'.*运行日志/)
  assert.match(source,/detailSection === 'llm'.*LLM 对话/)
})
test('诊断请求只读，增量游标持续前进，新一轮清除旧对话',async t=>{
  const {page}=await render();const calls:any[]=[]
  const values=[{events:[{event:'request',content:'旧轮',role:'user'}],cursor:80,trace_id:'a',reset:true,exists:true,lines:['旧日志']},
    {events:[{event:'assistant_response',content:'旧答',request_id:'r'}],cursor:150,trace_id:'a',exists:true},
    {events:[{event:'request',content:'新轮',role:'user'}],cursor:90,trace_id:'b',reset:true,exists:true}]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push([String(url),options]);return {ok:true,json:async()=>values.shift()}})
  await page.load();await page.load();assert.equal(page.events.length,2)
  assert.match(calls[1][0],/after=80&trace_id=a/)
  await page.load();assert.equal(page.events.length,1);assert.equal(page.rows[0].content,'新轮')
  assert.ok(calls.every(c=>!c[1].method&&!c[1].body))
})
test('切换评分和翻译立即中止旧请求，迟到响应不会混入新面板',async t=>{
  const {page}=await render();let resolve:any,signal:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).includes('action=filter')){signal=options?.signal;return new Promise(done=>{resolve=()=>done({ok:true,json:async()=>({events:[{event:'request',content:'过期评分'}],exists:true})})})}
    return {ok:true,json:async()=>({events:[{event:'request',content:'中文翻译'}],exists:true,trace_id:'t',cursor:80,reset:true})}
  })
  const first=page.load();page.selectAction('translate');assert.equal(signal.aborted,true)
  await page.load();resolve();await first
  assert.equal(page.events.length,1);assert.equal(page.rows[0].content,'中文翻译')
})
test('读取失败保留已展示内容，不发送任何重试任务',async t=>{
  const {page}=await render(p=>p.events=[{event:'request',content:'已读内容'}])
  t.mock.method(globalThis,'fetch',async()=>({ok:false,json:async()=>({message:'暂时无法读取'})}))
  await page.load();assert.equal(page.error,'暂时无法读取');assert.equal(page.rows[0].content,'已读内容')
})
