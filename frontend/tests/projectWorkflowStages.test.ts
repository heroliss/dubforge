import assert from 'node:assert/strict'
import test, {before,after} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createPinia,disposePinia} from 'pinia'
import {createMemoryHistory,createRouter} from 'vue-router'
import {NDialogProvider,NMessageProvider} from 'naive-ui'
import {WORKFLOW_STAGES} from '../src/utils/workflowStages.ts'

// 使用真实页面、共享阶段投影和模板按钮事件；所有读取均隔离，不访问用户项目。
let server:ViteDevServer,Projects:any,projectWorkflowStages:(project:any)=>any[],projectProductionDisplayStatus:(project:any)=>string
before(async()=>{
 server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
 Projects=(await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
 ;({projectWorkflowStages,projectProductionDisplayStatus}=await server.ssrLoadModule('/src/utils/projectWorkflowStages.ts'))
})
after(()=>server.close())
const step=(key:string,patch:any={})=>({key,label:WORKFLOW_STAGES.find(stage=>stage.key===key)?.label||'字幕导出',selected:true,status:'done',fresh:true,...patch})
const main=()=>WORKFLOW_STAGES.filter(stage=>stage.kind==='step').map(stage=>step(stage.key))
const entry=(uid:string,patch:any={})=>({uid,title:`项目${uid}`,status:'created',dir_exists:true,params:{},source_kind:'local',steps:[],...patch})

test('卡片的终态待核验保持中性，不能借旧fresh把新完成Job说成最新',()=>{
  const stages=projectWorkflowStages(entry('handoff',{steps:[step('assemble',{status:'checking',fresh:null,
    execution:{schema:1,owner:'step',job_id:19,state:'completed'}})]}))
  const stage=stages.find(stage=>stage.key==='assemble')!
  assert.equal(stage.status,'checking');assert.equal(stage.statusText,'任务已结束，产物核验中')
  assert.equal(stage.complete,false)
})
function buttonNodes(nodes:any):any[]{
 return (Array.isArray(nodes)?nodes:[nodes]).flatMap(node=>!node?[]:[
  ...(node.type==='button'&&node.props?.['data-workflow-stage']?[node]:[]),
  ...(Array.isArray(node.children)?buttonNodes(node.children):[])])
}
async function render(t:any,entries:any[]){
 const requests:any[]=[],buttons:any[]=[]
 let page:any
 t.mock.method(globalThis,'fetch',(url:any,options:any)=>{requests.push({url:String(url),options});return new Promise(()=>{})})
 const pinia=createPinia(),router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
 await router.push('/')
 const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Projects)})})})
 app.use(pinia);app.use(router);setup(app)
 app.mixin({created(){
  if(this.$options.__name==='ProjectsView'){page=this.$.setupState;page.projects=entries;page.loading=false}
  // SSR 的真实 trigger slot 同时保留客户端 VNode 分支，可调用模板绑定的原生按钮事件。
  if(this.$options.name==='Tooltip'&&this.$slots.trigger)buttons.push(...buttonNodes(this.$slots.trigger()))
 }})
 const html=await renderToString(app)
 t.after(()=>disposePinia(pinia))
 return {html,buttons,router,requests,page}
}
const text=(html:string)=>html.replace(/<[^>]*>/g,'').replace(/\s+/g,' ')

test('空制作任务的项目卡片也显示九阶段及1至9中文名称，不为各阶段额外读取网络',async t=>{
 const {html,requests}=await render(t,[entry('empty')])
 const stages=[...html.matchAll(/<button\b[^>]*data-workflow-stage="([^"]+)"[^>]*>/g)]
 assert.deepEqual(stages.map(match=>match[1]),WORKFLOW_STAGES.map(stage=>stage.key))
 for(const [index,match]of stages.entries()){
  const label=match[0].match(/aria-label="([^"]+)"/)?.[1]||''
  assert.ok(label.includes(WORKFLOW_STAGES[index].label),label)
  assert.match(label,new RegExp(`\\b${index+1}\\b`),label)
 }
 assert.match(text(html),/9 个工作阶段\s*(?:·\s*)?已完成 0 个/)
 assert.equal(projectWorkflowStages(entry('empty')).some(stage=>stage.complete),false)
 assert.equal(requests.length,0)
})

test('卡片中的评论和发布按钮真实事件进入专用工作区，不构造制作任务路由',async t=>{
 const {buttons,router,requests}=await render(t,[entry('route-project')])
 for(const [key,path]of [['comments','/projects/route-project/comments'],['publish','/projects/route-project/publish']]as const){
  const button=buttons.find(node=>node.props['data-workflow-stage']===key)
  assert.ok(button,`必须捕获实际${key}按钮`)
  let stopped=false
  await button.props.onClick({stopPropagation(){stopped=true},preventDefault(){}})
  await new Promise(resolve=>setImmediate(resolve))
  assert.equal(router.currentRoute.value.path,path);assert.equal(stopped,true)
 }
 assert.equal(requests.length,0)
})

test('可选字幕未导出不阻断已完成配音，已保存空评论方案算完成但缺少摘要不猜测完成或跳过',async t=>{
 const project=entry('optional',{status:'done',steps:[...main(),step('subtitles',{status:'pending',fresh:false})],
  comments_workspace:{state:'done',label:'已保存空方案',detail:'明确保存为空'}})
 const stages=projectWorkflowStages(project)
 assert.equal(stages.length,9);assert.equal(stages.find(stage=>stage.key==='tts').complete,true)
 assert.equal(stages.find(stage=>stage.key==='comments').complete,true)
 const unknown=projectWorkflowStages(entry('legacy')).find(stage=>stage.key==='comments')
 assert.equal(unknown.status,'unknown');assert.equal(unknown.complete,false)
 const {html,requests}=await render(t,[project,entry('legacy')])
 assert.equal((html.match(/data-workflow-stage="/g)||[]).length,18)
 assert.match(text(html),/9 个工作阶段\s*(?:·\s*)?已完成 8 个/)
 assert.match(text(html),/项目运行按目标规划/)
 assert.match(html,/字幕文件未单独导出（可选）/)
 assert.equal(requests.length,0)
})

test('字幕子任务运行时配音阶段保留活动状态与真实进度，不沿用先前配音完成色',async t=>{
 const project=entry('exporting',{steps:[...main(),step('subtitles',{status:'running',fresh:false,progress:37,progress_estimated:true})]})
 const merged=projectWorkflowStages(project).find(stage=>stage.key==='tts')
 assert.equal(merged.status,'running');assert.equal(merged.complete,false);assert.equal(merged.progress,37)
 assert.equal(merged.progress_estimated,true)
 const {html}=await render(t,[project])
 const button=[...html.matchAll(/<button\b[^>]*data-workflow-stage="tts"[^>]*>[\s\S]*?<\/button>/g)][0]?.[0]||''
 assert.match(button,/seg-running/);assert.doesNotMatch(button,/seg-done/)
 assert.match(button,/37%/)
})

test('已发布与本地需更新分别保留，卡片不因平台成功覆盖制作过期',async t=>{
 const project=entry('published-stale',{status:'done',steps:main().map(row=>row.key==='split'?{...row,fresh:false,freshness_reason:'原文已修改'}:row),
  publication:{id:'record',status:'published'},comments_workspace:{state:'done',label:'已保存',detail:'30条已确认'}})
 const stages=projectWorkflowStages(project)
 assert.equal(stages.find(stage=>stage.key==='split').complete,false)
 assert.equal(stages.find(stage=>stage.key==='split').status,'stale')
 assert.equal(stages.find(stage=>stage.key==='publish').complete,true)
 const {html}=await render(t,[project])
 assert.match(html,/已公开发布/);assert.match(html,/需更新/)
 assert.match(text(html),/9 个工作阶段\s*(?:·\s*)?已完成 8 个/)
})

test('只有发布成功终态计完成，上传100%或已投稿待审核不冒充平台交付完成',()=>{
 for(const status of ['published','private','reviewed','uploaded','submitted','uploading','unknown','collection_pending','rejected']){
  const stage=projectWorkflowStages(entry(status,{publication:{status,progress:100}})).find(stage=>stage.key==='publish')
  assert.equal(stage.complete,['published','private','reviewed'].includes(status),status)
 }
 const jobsOnly=projectWorkflowStages(entry('ratings',{comments_workspace:{state:'attention',label:'待确认方案',detail:'评分与翻译已结束'}})).find(stage=>stage.key==='comments')
 assert.equal(jobsOnly.complete,false)
})

test('轻量首屏历史done中性待核对、failed保留明确上次错误，不冒绿条/零完成；确切活动仍保留',async t=>{
 const project=entry('summary',{projection:'summary',freshness_known:false,status:'checking',
  steps:[step('tts',{status:'done',fresh:null}),step('assemble',{status:'failed',fresh:null,error:'历史错误'})]})
 const stages=projectWorkflowStages(project)
 for(const key of ['tts','assemble']){
  assert.equal(stages.find(stage=>stage.key===key).status,key==='assemble'?'failed':'checking')
  assert.equal(stages.find(stage=>stage.key===key).complete,false)
 }
 const {html}=await render(t,[project])
 assert.match(text(html),/制作状态待核对/);assert.match(text(html),/产物待核对/)
 assert.doesNotMatch(text(html),/已完成 0 个/)
 assert.doesNotMatch(html,/seg-done/)
 assert.match(html,/上次执行失败 · 产物待核对/);assert.equal(stages.find(stage=>stage.key==='assemble').detail,'历史错误')
 const active=projectWorkflowStages({...project,status:'processing',steps:[step('tts',{status:'queued',fresh:null})],
  comments_workspace:{state:'running',label:'评论处理中',detail:'详细数量在评论工作区查看'}})
 assert.equal(active.find(stage=>stage.key==='tts').status,'queued')
 assert.equal(active.find(stage=>stage.key==='comments').status,'running')
})

test('更新原稿件进行中时发布阶段显示独立进度，不把旧BV状态当作空闲', async t => {
 const publication={id:'record',status:'rejected',progress:100,latest_update:{id:'update-1',status:'uploading',phase:'正在上传替换视频',progress:42,stages:{upload:{status:'running',progress:42}}}}
 const project=entry('updating-original',{status:'done',publication})
 const stage=projectWorkflowStages(project).find(stage=>stage.key==='publish')
 assert.equal(stage?.status,'running');assert.equal(stage?.progress,42);assert.equal(stage?.complete,false)
 const {html}=await render(t,[project])
 assert.match(text(html),/正在上传替换视频/);assert.match(html,/42%/)
})

test('实际项目卡片沿用当前运行目标，并将已核验的新交付与旧中断历史分开',async t=>{
 const publication={id:'same-publication',status:'published',execution:{schema:1,state:'completed',label:'旧投稿核对过程',elapsed_seconds:93*3600},
  latest_update:{id:'new-update',status:'completed',phase:'当前章节与原稿更新已核验',updated_at:'2026-01-02T00:00:00',progress:100}}
 const repaired=entry('repaired',{workflow_status:'attention',publication,workflow_summary:{run:{id:'old-workflow',status:'attention',mode:'update',
  publication_id:'same-publication',publication:{id:'old-update',status:'paused',updated_at:'2026-01-01T00:00:00'}}}})
 const running=entry('running',{workflow_status:'running',project_settings:{known:true,delivery_mode:'publish',target_kind:'',
  target_selection:{source:'history_requires_choice',has_history:true}}})
 const {html,requests}=await render(t,[repaired,running])
 assert.match(text(html),/沿用本次已确认方案/);assert.match(text(html),/无需重新选择稿件/)
 assert.doesNotMatch(text(html),/已有发布记录.*请选择新稿|上次完整流程需处理|旧投稿核对过程|93 小时/)
 assert.match(html,/data-current-delivery/);assert.match(text(html),/原稿更新已核验/)
 assert.equal((html.match(/class="execution-progress\b/g)||[]).length,1,'当前交付只显示一份执行面板')
 assert.equal(requests.length,0,'新后端已含充分摘要时不能追加卡片请求')
})

test('主动停止的完整卡片与冷摘要中性呈现，真失败共存仍需关注，新任务不借旧停止',async t=>{
 const stopped=step('assemble',{status:'failed',fresh:false,error:'步骤已停止，已完成产物保留',
  execution:{schema:1,owner:'step',job_id:18,state:'cancelled'}})
 const project=entry('stopped',{status:'failed',steps:[stopped]})
 assert.equal(projectWorkflowStages(project).find(stage=>stage.key==='assemble').status,'stopped')
 assert.equal(projectProductionDisplayStatus(project),'stopped')
 const mixed={...project,steps:[stopped,step('translate',{status:'failed',fresh:false,error:'模型服务失败'})]}
 assert.equal(projectProductionDisplayStatus(mixed),'failed')
 const summary=entry('summary-stopped',{projection:'summary',status:'checking',freshness_known:false,steps:[
  {...stopped,job_id:18,job_status:'cancelled',fresh:null,execution:null}]})
 assert.equal(projectProductionDisplayStatus(summary),'checking')
 assert.equal(projectWorkflowStages(summary).find(stage=>stage.key==='assemble').statusText,'上次已停止 · 产物待核对')
 const active={...project,steps:[{...stopped,job_id:19,execution:{schema:1,owner:'step',job_id:19,state:'running'}}]}
 assert.equal(projectWorkflowStages(active).find(stage=>stage.key==='assemble').status,'running')
 assert.equal(projectProductionDisplayStatus(active),'processing')
 const {html,page}=await render(t,[project,summary,mixed,active])
 assert.match(html,/上次已停止 · 产物待核对/);assert.match(html,/seg-stopped/)
 assert.deepEqual(page.statusCounts,{stopped:1,checking:1,failed:1,processing:1},'汇总统计采用同一展示合同，只取消不计真实失败')
 const onlyStopped=(await render(t,[project])).html
 assert.doesNotMatch(onlyStopped,/seg-failed|上次执行失败/)
 assert.match(onlyStopped,/已停止/)
 const mixedHtml=(await render(t,[mixed])).html
 assert.match(mixedHtml,/seg-failed/);assert.match(mixedHtml,/有失败步骤/)
})
