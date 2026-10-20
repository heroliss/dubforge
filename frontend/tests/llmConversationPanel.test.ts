import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createRenderer,createSSRApp,h,reactive,ssrContextKey} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
let server:ViteDevServer,Panel:any,Drawer:any,Summary:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Panel=(await server.ssrLoadModule('/src/components/LlmConversationPanel.vue')).default
  Drawer=(await server.ssrLoadModule('/src/components/LlmConversationDrawer.vue')).default
  Summary=(await server.ssrLoadModule('/src/components/ChapterAdaptationSummary.vue')).default
})
after(async()=>{mock.restoreAll();await server.close()})
test('共享对话时间按本机时区格式化，与列表一致而非截取UTC',async t=>{
  const calls:any[]=[]
  t.mock.method(Date.prototype,'toLocaleTimeString',function(locale:any,options:any){calls.push({timestamp:this.getTime(),locale,options});return'21:23:24'})
  const html=await renderToString(createSSRApp({render:()=>h(Panel,{events:[{event:'request',role:'user',content:'输入',ts:'2026-10-15T13:23:24Z'}]})}))
  assert.match(html,/<time[^>]*>21:23:24<\/time>/);assert.doesNotMatch(html,/13:23:24/)
  assert.deepEqual(calls,[{timestamp:Date.parse('2026-10-15T13:23:24Z'),locale:'zh-CN',options:{hour12:false}}])
})
test('共享真实渲染只显示实际返回的思考、完整输出和校验，模型HTML按文本转义',async()=>{
  const html=await renderToString(createSSRApp({render:()=>h(Panel,{events:[
    {event:'request',role:'system',content:'系统要求'},
    {event:'request',role:'user',content:'字幕素材'},
    {event:'reasoning_delta',request_id:'r',content:'实际思考'},
    {event:'assistant_response',request_id:'r',content:'<script>模型正文</script>'},
    {event:'validation_error',content:'标题过长'},
  ]})}))
  assert.match(html,/展开模型返回的思考/);assert.match(html,/实际思考/);assert.match(html,/标题过长/)
  assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>模型正文/)
  const empty=await renderToString(createSSRApp({render:()=>h(Panel,{events:[],active:true})}))
  assert.match(empty,/等待模型返回内容/);assert.doesNotMatch(empty,/展开模型返回的思考/)
})

test('章节报告展示自定义要求和审查结果，需人工复核不冒充通过',async()=>{
  const html=await renderToString(createSSRApp({render:()=>h(Summary,{value:{source_count:21,output_count:10,
    review:{checked:true,summary:'标题按准确客观原则核对',findings:[
      {source_indices:[0,1],action:'rewritten',reason:'改为中性表述'},
      {source_indices:[2],action:'needs_review',reason:'缺少事实依据，需人工核对'},
    ]}}})}))
  assert.match(html,/标题审查/);assert.match(html,/已改写/);assert.match(html,/需人工复核/)
  assert.match(html,/源章节 1–2/);assert.match(html,/缺少事实依据/)
  assert.doesNotMatch(html,/<input|<textarea/)
})
const host=createRenderer<any,any>({createElement:()=>({}),createText:()=>({}),createComment:()=>({}),setText(){},setElementText(){},patchProp(){},parentNode(){return null},nextSibling(){return null},remove(){},insert(){}})
async function flush(){for(let i=0;i<20;i++)await Promise.resolve()}
async function client(t:any,request:(url:string)=>any){
  t.mock.method(globalThis,'fetch',async(url)=>({ok:true,json:async()=>request(String(url))}))
  const props=reactive({show:false,projectUid:'project-a',operation:'chapters',ownerId:'pub-a'}),Logic={...Drawer,render:()=>null,ssrRender:undefined};let page:any
  const app=host.createApp({render:()=>h(Logic,{...props})});app.provide(ssrContextKey,{})
  app.mixin({created(){if(this.$options.__name==='LlmConversationDrawer')page=this.$.setupState}})
  app.mount({});t.after(()=>app.unmount());return{props,page}
}
test('共享抽屉按项目和操作筛选，历史分页不重发模型，关闭后停止读取',async t=>{
  const calls:string[]=[]
  const {props,page}=await client(t,url=>{
    calls.push(url)
    if(url.includes('/latest?'))return{events:[{event:'assistant_response',content:'实际回复'}],cursor:100,trace_id:'t',status:'completed'}
    if(url.includes('before='))return{items:[{id:'old',label:'旧章节整理'}],more:false,next_cursor:''}
    return{items:[{id:'latest',label:'章节整理',project_title:'实际项目标题'}],more:true,next_cursor:'older'}
  })
  assert.equal(calls.length,0);props.show=true;await flush()
  assert.ok(calls.some(url=>url.includes('project_uid=project-a')&&url.includes('operation=chapters')&&url.includes('owner_id=pub-a')))
  assert.equal(page.selected,'latest');assert.equal(page.trace.events.value[0].content,'实际回复')
  await page.load(true);assert.equal(page.items.length,2);assert.ok(calls.some(url=>url.includes('before=older')))
  props.show=false;await flush();const before=calls.length;await page.load();await page.trace.load();assert.equal(calls.length,before)
})

test('切换项目后旧列表迟到响应不能恢复错误项目的选择',async t=>{
  let release:any
  const {props,page}=await client(t,url=>{
    if(url.includes('project_uid=project-a'))return new Promise(resolve=>{release=()=>resolve({items:[{id:'late',label:'旧项目'}]})})
    if(url.includes('/fresh?'))return{events:[],cursor:0}
    return{items:[{id:'fresh',label:'新项目'}]}
  })
  props.show=true;await flush();props.projectUid='project-b';await flush();release();await flush()
  assert.equal(page.selected,'fresh');assert.equal(page.items[0].label,'新项目')
})
