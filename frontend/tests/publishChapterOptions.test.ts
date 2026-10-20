import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server:ViteDevServer,Options:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,
    plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Options=(await server.ssrLoadModule('/src/components/PublishChapterOptions.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('展示章节设置不得访问网络')})
})
after(async()=>{mock.restoreAll();await server.close()})
async function render(extra:any={}){
  let page:any
  const events:any[]=[],value={enabled:true,translate:true,permanent:true,items:[{title:'Opening',start_time:0,end_time:10}]}
  const app=createSSRApp({render:()=>h(Options,{value,projectUid:'fixture',...extra,onChange:(next:any)=>events.push(next)})})
  setup(app);app.mixin({created(){if(this.$options.__name==='PublishChapterOptions')page=this.$.setupState}})
  const html=await renderToString(app)
  return {page,events,value,html}
}

test('自动翻译是保存后任务选项，编辑开关和章节不会立即调用模型或B站',async()=>{
  const result=await render()
  assert.match(result.html,/整理时将标题翻译为中文/);assert.match(result.html,/AI翻译并整理供预览/)
  assert.match(result.html,/不会写入B站/)
  result.page.update({translate:false});result.page.edit(0,'title','人工标题')
  assert.equal(result.events.length,2);assert.equal(result.events[0].translate,false)
  assert.equal(result.events[1].items[0].title,'人工标题');assert.equal(result.value.items[0].title,'Opening')
})

test('只有显式现在翻译才请求模型，返回内容作为本地草稿且不提交章节',async t=>{
  const result=await render(),requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({path:String(url),body:JSON.parse(String(options?.body))})
    return {ok:true,json:async()=>({items:[{title:'开场'}]})}
  })
  await result.page.translate()
  assert.deepEqual(requests,[{path:'/api/publishing/projects/fixture/chapters/translate',body:{items:result.value.items,translate:true,instruction:''}}])
  assert.equal(result.events[0].items[0].title,'开场');assert.equal(result.events[0].translate,true)
  assert.equal(result.value.items[0].title,'Opening')
})

test('手动章节翻译只显示实际目标条数与未知等待，不推测逐条百分比',async t=>{
  const {page}=await render()
  let release:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',()=>new Promise(resolve=>{release=resolve}))
  const pending=page.translate();assert.equal(page.busy,true);assert.equal(page.translatingCount,1)
  await page.translate()
  release({ok:true,json:async()=>({items:[{title:'开场'}]})});await pending;assert.equal(page.busy,false)
})

test('旧稿件的关闭值保持关闭，禁用时也不能翻译或编辑',async()=>{
  const old=await render({value:{enabled:false,translate:false,permanent:false,items:[]}})
  assert.equal(old.page.options.enabled,false);assert.doesNotMatch(old.html,/AI翻译并整理供预览/)
  const locked=await render({disabled:true})
  await locked.page.translate();locked.page.update({enabled:false})
  assert.deepEqual(locked.events,[])
})

test('整理整体接受合并后的时间与来源，不按旧下标错配标题或丢掉合并',async t=>{
  const value={enabled:true,translate:false,permanent:true,items:[
    {title:'First',start_time:0,end_time:3},{title:'Second',start_time:3,end_time:10},
  ]}
  const result=await render({value,duration:10}),requests:any[]=[]
  const items=[{title:'Combined topic',start_time:0,end_time:10,source_indices:[0,1]}]
  const adaptation={source_count:2,output_count:1,merged_count:1,notes:['合并相邻主题']}
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    requests.push({path:String(url),body:JSON.parse(String(options?.body))})
    return {ok:true,json:async()=>({items,adaptation})}
  })
  await result.page.translate()
  assert.deepEqual(requests[0].body,{items:value.items,duration:10,translate:false,instruction:''})
  assert.deepEqual(result.events[0].items,items);assert.deepEqual(result.page.adaptation,adaptation)
  assert.equal(value.items.length,2);assert.match(result.html,/AI整理章节供预览/)
})

test('超量候选在本地说明合并规则，长标题不会被展示过程截断',async()=>{
  const title='尚未整理的超长标题用于验证完整原文保留'
  const result=await render({value:{enabled:true,items:Array.from({length:21},(_,index)=>({title,start_time:index*10,end_time:(index+1)*10}))}})
  assert.match(result.html,/超过 10 个上限/);assert.match(result.html,/9 个分界点/)
  assert.match(result.html,/首个分界点至少 1 秒/);assert.match(result.html,/其余每章至少 5 秒/)
  assert.equal(result.value.items[0].title,'Opening');assert.equal(result.page.options.items[0].title,title)
})

test('单一自定义要求随草稿与整理请求传递，编辑不请求模型、不暴露平台规则编辑',async t=>{
  const value={enabled:true,translate:true,instruction:'标题用名词短语；保留模型英文名',items:[{title:'开场',start_time:0,end_time:10}]}
  const result=await render({value}),calls:any[]=[]
  assert.match(result.html,/章节标题自定义要求/);assert.match(result.html,/保留模型英文名/)
  result.page.update({instruction:'补充术语要求'});assert.equal(result.events[0].instruction,'补充术语要求')
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push(JSON.parse(String(options.body)));return{ok:true,json:async()=>({items:value.items,adaptation:{review:{checked:true,summary:'已核对'}}})}})
  await result.page.translate()
  assert.equal(calls[0].instruction,value.instruction);assert.equal(calls[0].rules,undefined)
  assert.equal(result.page.adaptation.review.checked,true)
})
