import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createPinia,disposePinia} from 'pinia'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import {NDialogProvider,NMessageProvider} from 'naive-ui'
import {readFile} from 'node:fs/promises'
import {parse} from '@vue/compiler-sfc'
import {compile} from '@vue/compiler-dom'
import * as Vue from 'vue'

let server:ViteDevServer,Pipeline:any,useNavStore:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],
    appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Pipeline=(await server.ssrLoadModule('/src/views/PipelineView.vue')).default
  useNavStore=(await server.ssrLoadModule('/src/stores/nav.ts')).useNavStore
  mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
})
after(async()=>{mock.restoreAll();await server.close()})

async function render(navigation:any) {
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/projects/:uid',component:Pipeline},
    {path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/projects/fixture')
  let page:any
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(RouterView)})})})
  app.use(navigation._p);app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='PipelineView')page=this.$.setupState}})
  return {html:await renderToString(app),page}
}
async function settle(){for(let index=0;index<10;index++)await Promise.resolve()}

test('导航先返回上次failed、步骤和流程尚未读取时，总览显示中性核对状态',async t=>{
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{
    assert.ok(!options?.method || options.method==='GET','本测试不得提交运行请求')
    return new Promise(resolve=>requests.push({url:String(url),resolve}))
  })
  const pinia=createPinia(),nav=useNavStore(pinia)
  try {
    nav.enter('fixture')
    requests.find(row=>row.url.endsWith('/navigation')).resolve(new Response(JSON.stringify({title:'正在恢复的项目',status:'failed'})))
    await settle()
    assert.equal(nav.status,'failed');assert.equal(nav.stepsLoaded,false)
    let displayed=await render(nav)
    assert.equal(displayed.page.headerStatus.text,'正在读取运行状态')
    assert.equal(displayed.page.headerStatus.type,'default')
    assert.match(displayed.html,/正在读取步骤状态/);assert.doesNotMatch(displayed.html,/有失败步骤/)
    requests.find(row=>row.url.endsWith('/steps')).resolve(new Response(JSON.stringify([
      {key:'tts',label:'字幕与配音',status:'running',job_id:520,selected:true,progress:40,artifacts:[],inputs:[],unready_inputs:[]},
    ])))
    requests.find(row=>row.url.endsWith('/workflow/status')).resolve(new Response(JSON.stringify({run:{id:'original',status:'running',phase:'字幕与配音',cursor:5}})))
    await settle()
    assert.equal(nav.status,'processing');assert.equal(nav.busy,true)
    displayed=await render(nav)
    assert.match(displayed.page.headerStatus.text,/执行中.*字幕与配音/)
    assert.equal(displayed.page.headerStatus.type,'info')
    assert.doesNotMatch(displayed.html,/有失败步骤/)
  } finally {nav.reset();disposePinia(pinia)}
})

test('初读失败只提示状态待核对，明确的步骤或流程失败继续正常显示',async()=>{
  for(const state of [
    {patch:{stepsLoaded:false,stepsError:'读取步骤失败'},text:'运行状态待核对',type:'warning'},
    {patch:{stepsLoaded:false,workflowError:'读取流程失败'},text:'运行状态待核对',type:'warning'},
    {patch:{stepsLoaded:true,steps:[{key:'tts',label:'字幕与配音',status:'failed',artifacts:[],inputs:[],unready_inputs:[]}]},text:'有失败步骤',type:'error'},
    {patch:{stepsLoaded:false,workflow:{run:{id:'original',status:'failed',phase:'语义整理'}}},text:'需处理 · 语义整理',type:'error'},
  ]) {
    const pinia=createPinia(),nav=useNavStore(pinia)
    try {
      nav.$patch({uid:'fixture',title:'项目',status:'failed',steps:[],...state.patch})
      const {page}=await render(nav)
      assert.equal(page.headerStatus.text,state.text);assert.equal(page.headerStatus.type,state.type)
      assert.equal(nav.status,'failed','展示层不能修改持久化来源的状态')
    } finally {nav.reset();disposePinia(pinia)}
  }
})

test('已知步骤或流程失败不依赖导航的旧done/created状态，同稿件已核验修复保留优先',async()=>{
  const failedStep={key:'tts',label:'字幕与配音',status:'failed',artifacts:[],inputs:[],unready_inputs:[]}
  for(const status of ['done','created']) for(const fact of [
    {steps:[failedStep],workflow:null,text:'有失败步骤',type:'error'},
    ...['failed','attention','paused'].map(state=>({steps:[],workflow:{run:{id:'original',status:state,phase:'发布到B站'}},
      text:state==='paused'?'已暂停 · 发布到B站':'需处理 · 发布到B站',type:state==='failed'?'error':'warning'})),
  ]) {
    const pinia=createPinia(),nav=useNavStore(pinia)
    try {
      nav.$patch({uid:'fixture',title:'项目',status,stepsLoaded:false,steps:fact.steps,workflow:fact.workflow})
      const {page}=await render(nav)
      assert.equal(page.headerStatus.text,fact.text);assert.equal(page.headerStatus.type,fact.type)
      assert.equal(nav.status,status)
    } finally {nav.reset();disposePinia(pinia)}
  }
  const workflow={run:{id:'original',mode:'update',status:'attention',phase:'旧原稿更新需处理',publication_id:'same',
    publication_update:{id:'old',status:'failed',source_publication_id:'same',updated_at:'2026-10-17T12:00:00'}}}
  const publication={id:'same',latest_update:{id:'new',status:'completed',source_publication_id:'same',updated_at:'2026-10-20T00:00:00Z'}}
  for(const value of [
    {workflow,publication,text:'原稿更新已核验',type:'success'},
    {workflow,publication:{...publication,id:'unrelated'},text:'需处理 · 旧原稿更新需处理',type:'warning'},
    {workflow:{run:{...workflow.run,publication_update:{...workflow.run.publication_update,status:'unknown'}}},publication,
      text:'需处理 · 旧原稿更新需处理',type:'warning'},
  ]) {
    const pinia=createPinia(),nav=useNavStore(pinia)
    try {
      nav.$patch({uid:'fixture',title:'项目',status:'done',stepsLoaded:false,steps:[],workflow:value.workflow,publication:value.publication})
      const {page}=await render(nav)
      assert.equal(page.headerStatus.text,value.text);assert.equal(page.headerStatus.type,value.type)
    } finally {nav.reset();disposePinia(pinia)}
  }
})

test('已有活动步骤和完整流程快照优先于初读提示，保留既有完成快照',async()=>{
  for(const state of [
    {patch:{workflow:{run:{id:'original',status:'running',phase:'合成成片'}}},text:'执行中 · 合成成片',type:'info'},
    {patch:{steps:[{key:'tts',label:'字幕与配音',status:'running',job_id:520,artifacts:[],inputs:[],unready_inputs:[]}]},text:'正在「字幕与配音」…',type:'info'},
    {patch:{workflow:{run:{id:'original',status:'completed',phase:'已完成'}}},text:'上次项目运行已完成',type:'success'},
  ]) {
    const pinia=createPinia(),nav=useNavStore(pinia)
    try {
      nav.$patch({uid:'fixture',title:'项目',status:'failed',stepsLoaded:false,steps:[],...state.patch})
      const {page}=await render(nav)
      assert.equal(page.headerStatus.text,state.text);assert.equal(page.headerStatus.type,state.type)
    } finally {nav.reset();disposePinia(pinia)}
  }
})

test('单独字幕导出只按明确保存清单或同流程冻结范围标可选，未知与显式选择保留实际状态',async()=>{
  for(const item of [
    {saved:null,plan:null,expected:'待运行'},
    {saved:['tts'],plan:null,expected:'按需导出'},
    {saved:['subtitles','tts'],plan:null,expected:'待运行'},
    {saved:null,plan:{id:'current',steps:['tts','comments','assemble']},expected:'按需导出'},
    {saved:null,plan:{id:'current',steps:['subtitles','tts']},expected:'待运行'},
    {saved:['tts'],plan:{id:'current',steps:['subtitles','tts']},expected:'待运行'},
    {saved:['subtitles'],plan:{id:'current',steps:['tts']},expected:'待运行'},
    {saved:null,plan:{id:'other',steps:['tts']},expected:'待运行'},
    {saved:null,plan:{id:'current'},expected:'待运行'},
    {saved:null,plan:{id:'current',steps:'tts'},expected:'待运行'},
    {saved:null,plan:{id:'current',steps:{tts:true}},expected:'待运行'},
    {saved:null,plan:{id:'current',steps:null},expected:'待运行'},
    {saved:null,plan:{id:'current',steps:['tts',null]},expected:'待运行'},
    {saved:null,plan:{id:'current',steps:['tts',' ']},expected:'待运行'},
  ]) {
    const pinia=createPinia(),nav=useNavStore(pinia)
    try {
      const subtitle={key:'subtitles',label:'字幕导出',status:'pending',artifacts:[],unready_inputs:[]}
      nav.$patch({uid:'fixture',steps:[{key:'tts',label:'字幕与配音',status:'done',fresh:true,artifacts:[]},subtitle],
        stepsLoaded:true,autoSteps:item.saved,workflow:{run:{id:'current',status:'running',phase:'上屏评论'}},workflowPlan:item.plan})
      const {page,html}=await render(nav)
      assert.equal(page.editorTaskText(subtitle),item.expected)
      assert.match(html,/单独导出字幕/);assert.match(html,new RegExp(item.expected))
      assert.doesNotMatch(html,/同步字幕与配音…/)
      if(!Array.isArray(item.plan?.steps) || item.plan.steps.some((key:any)=>typeof key!=='string' || !key.trim())) {
        assert.doesNotMatch(html,/class="run-progress"|class="run-sequence"/,'未知范围不显示虚假的阶段分母')
      }
      for(const status of ['queued','running','failed','stale']) {
        assert.notEqual(page.editorTaskText({...subtitle,status}),'按需导出','可选不能掩盖实际执行或问题状态')
      }
    } finally {nav.reset();disposePinia(pinia)}
  }
})

type HostNode={type:string;text:string;props:Record<string,any>;children:HostNode[];parent:HostNode|null}
function mountEditor(page:any,nav:any,renderEditor:any) {
  const host=(type:string,text=''):HostNode=>({type,text,props:{},children:[],parent:null})
  const remove=(node:HostNode)=>{if(node.parent)node.parent.children.splice(node.parent.children.indexOf(node),1);node.parent=null}
  const renderer=Vue.createRenderer<HostNode,HostNode>({
    createElement:tag=>host(tag),createText:text=>host('#text',text),createComment:text=>host('#comment',text),remove,
    insert(node,parent,anchor=null){remove(node);const index=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(index<0?parent.children.length:index,0,node);node.parent=parent},
    setText(node,text){node.text=text},setElementText(node,text){node.children.forEach(child=>child.parent=null);node.children=[];node.text=text},
    parentNode:node=>node.parent,nextSibling:node=>node.parent?.children[node.parent.children.indexOf(node)+1]||null,
    patchProp(node,key,_previous,value){node.props[key]=value},
  })
  const Button=Vue.defineComponent({inheritAttrs:false,props:['disabled','loading'],setup:(props,{attrs,slots})=>()=>Vue.h('button',{
    ...attrs,disabled:props.disabled,'data-loading':!!props.loading},slots.default?.())})
  const Span=Vue.defineComponent({setup:(_props,{slots})=>()=>Vue.h('span',slots.default?.())})
  const app=renderer.createApp({components:{NButton:Button,NTag:Span,RouterLink:Span,FreshnessDetails:Span},
    setup:()=>({uid:'fixture',nav,dirMissing:false,router:{push:()=>{}},needsFreshnessDetails:()=>false,
      editorTaskText:page.editorTaskText,editorTaskType:page.editorTaskType,runEditorWorkflow:page.runEditorWorkflow,
      get editorSyncChecking(){return page.editorSyncChecking}}),render:renderEditor})
  const root=host('root');app.mount(root)
  const text=(node:HostNode):string=>node.type==='#comment'?'':node.text+node.children.map(text).join('')
  function buttons(){const result:HostNode[]=[];const visit=(node:HostNode)=>{if(node.type==='button')result.push(node);node.children.forEach(visit)};visit(root);return result}
  return {button:()=>buttons()[0],text:()=>text(buttons()[0]),unmount:()=>app.unmount()}
}

test('真实挂载编辑动作：comments只禁用、TTS/字幕保留实际标签，仅本地检查显示loading且请求单飞',async t=>{
  const source=await readFile(new URL('../src/views/PipelineView.vue',import.meta.url),'utf8')
  const template=parse(source).descriptor.template!.content
  const branch=template.match(/<template v-else-if="s.key === 'tts'">([\s\S]*?)<\/template>/)
  assert.ok(branch,'必须挂载实际总览编辑分支，不复制按钮模板')
  const renderEditor=new Function('Vue',compile(branch[1],{mode:'function',prefixIdentifiers:true}).code)(Vue)
  const pinia=createPinia(),nav=useNavStore(pinia)
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',(url,options)=>{
    if(options?.method==='POST') {
      assert.equal(url,'/api/projects/fixture/run_plan','不得启动任务，仅隔离预检替身')
      return new Promise(resolve=>requests.push({url,resolve}))
    }
    return new Promise(()=>{})
  })
  let mounted:any
  try {
    nav.$patch({uid:'fixture',stepsLoaded:true,autoSteps:['tts'],steps:[
      {key:'tts',label:'字幕与配音',status:'done',fresh:true,artifacts:[]},
      {key:'subtitles',label:'字幕导出',status:'pending',artifacts:[]},
    ],workflow:{run:{id:'current',status:'running',phase:'上屏评论'}}})
    const {page}=await render(nav)
    mounted=mountEditor(page,nav,renderEditor)
    assert.equal(mounted.text(),'同步字幕与配音')
    assert.equal(mounted.button().props.disabled,true);assert.equal(mounted.button().props['data-loading'],false)
    await page.runEditorWorkflow();assert.equal(requests.length,0)
    for(const key of ['tts','subtitles']) for(const status of ['queued','running']) {
      nav.workflow=null;nav.steps=nav.steps.map((step:any)=>({...step,status:step.key===key?status:'done'}));await Vue.nextTick()
      assert.equal(mounted.button().props.disabled,true)
      assert.equal(mounted.button().props['data-loading'],false,'执行标签承载进度，按钮不伪装成本地检查')
      assert.equal(mounted.text(),'同步字幕与配音')
    }
    nav.steps=nav.steps.map((step:any)=>({...step,status:'done',fresh:true}));await Vue.nextTick()
    assert.equal(mounted.button().props.disabled,false)
    const first=page.runEditorWorkflow(),second=page.runEditorWorkflow();await Vue.nextTick()
    assert.equal(requests.length,1,'预检返回前重复点击只提交一次')
    assert.equal(mounted.text(),'正在检查同步范围');assert.equal(mounted.button().props['data-loading'],true)
    requests[0].resolve(new Response(JSON.stringify({blockers:[{name:'translate.txt',reason:'隔离阻断',label:'翻译'}]})))
    await Promise.all([first,second]);await Vue.nextTick()
    assert.equal(mounted.text(),'同步字幕与配音');assert.equal(mounted.button().props['data-loading'],false)
    assert.equal(mounted.button().props.disabled,false)
  } finally {mounted?.unmount();nav.reset();disposePinia(pinia)}
})
