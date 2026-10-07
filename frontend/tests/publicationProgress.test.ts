import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { createPinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { publicationUploadProgress } from '../src/utils/publishing.ts'

let server: ViteDevServer, Progress: any
before(async () => {
  server = await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Progress = (await server.ssrLoadModule('/src/components/PublicationProgress.vue')).default
})
after(async () => await server.close())

async function render(publication: any) {
  const app = createSSRApp({render:()=>h(Progress,{publication,compact:true})})
  setup(app)
  return renderToString(app)
}

test('真实字节优先于旧百分比，显示具体分P且不推测投稿进度', async () => {
  const row = {status:'uploading',progress:90,progress_detail:{phase:'uploading',loaded:125,total:500,current_part:2,current_title:'配音版',completed_parts:1,total_parts:3}}
  assert.equal(publicationUploadProgress(row)!.percentage,25)
  const html = await render(row)
  assert.match(html,/视频文件上传/); assert.match(html,/25.0%.*125 B \/ 500 B/)
  assert.match(html,/P2 \/ 3.*配音版/); assert.match(html,/已完成 1 \/ 3 个分P/)
})

test('视频上传100%时封面和投稿仍显示实际阶段，暂停显示恢复边界', async () => {
  const row = {status:'uploading',progress:100,progress_detail:{phase:'cover',loaded:500,total:500,completed_parts:3,total_parts:3}}
  const cover = await render(row)
  assert.match(cover,/视频文件已上传/); assert.match(cover,/正在处理并上传封面/)
  assert.doesNotMatch(cover,/已公开发布|已投稿/)
  assert.match(await render({...row,status:'submitting',progress_detail:{...row.progress_detail,phase:'submitting'}}),/正在提交稿件资料/)
  assert.match(await render({...row,status:'paused'}),/继续时复用已完成分P/)
  const waiting = await render({...row,status:'uploaded',progress_detail:{...row.progress_detail,phase:'validating'}})
  assert.match(waiting,/等待确认投稿/); assert.doesNotMatch(waiting,/正在核对/)
})

test('字节100%仍等待合并时不会宣称文件已完成上传', async () => {
  const row = {status:'uploading',progress:100,progress_detail:{phase:'merging',loaded:500,total:500,completed_parts:0,total_parts:1}}
  const html = await render(row)
  assert.match(html,/视频字节已传完/);assert.match(html,/B站正在合并上传分片/)
  assert.doesNotMatch(html,/视频文件已上传/)
})

test('兼容历史记录，并对非有限与超范围值做边界处理', async () => {
  assert.equal(publicationUploadProgress({status:'uploading',progress:120})!.percentage,100)
  assert.equal(publicationUploadProgress({status:'uploading',progress:NaN})!.percentage,0)
  assert.equal(publicationUploadProgress({status:'uploading',progress:42})!.hasBytes,false)
  assert.doesNotMatch(await render({status:'published',progress:100}),/data-publication-progress/)
  assert.doesNotMatch(await render({status:'draft'}),/data-publication-progress/)
})

test('项目列表显示独立发布段、真实上传进度和审核状态，制作完成不等于已发布', async () => {
  const Projects = (await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  const router = createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/projects')
  const projects = [
    {uid:'uploading',title:'上传中的成片',publication:{id:'1',status:'uploading',progress:25,progress_detail:{phase:'uploading',loaded:250,total:1000,current_part:1,completed_parts:0,total_parts:1}}},
    {uid:'submitted',title:'审核中的成片',publication:{id:'2',status:'submitted',progress:100}},
    {uid:'published',title:'公开成片',publication:{id:'3',status:'published',progress:100}},
    {uid:'private',title:'私密成片',publication:{id:'4',status:'private',progress:100}},
  ].map(p=>({...p,status:'done',dir_exists:true,params:{},steps:[{key:'assemble',label:'合成',status:'done',fresh:true,selected:true}]}))
  const app = createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Projects)})})})
  app.use(createPinia());app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='ProjectsView'){this.$.setupState.projects=projects;this.$.setupState.loading=false}}})
  const html = await renderToString(app)
  assert.match(html,/制作完成/);assert.match(html,/B站 · 上传文件中/)
  assert.match(html,/发布到B站：上传文件中，进入发布页/)
  assert.match(html,/25.0%.*250 B \/ 1000 B/)
  assert.match(html,/B站 · 已投稿 · 查看平台状态/)
  assert.match(html,/B站 · 已公开发布/);assert.match(html,/B站 · 已通过 · 仅自己可见/)
  assert.equal((html.match(/进入发布页/g)||[]).length,4)
})

test('项目制作状态与需更新独立显示，保留悬浮摘要和只读原因详情，最新及仅失败项目没有更新入口',async t=>{
  const Projects=(await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/projects')
  const entries=[{uid:'needs',title:'有变化项目',steps:[{key:'split',label:'语义整理',status:'done',fresh:false,freshness_reason:'上游文字已变化',selected:true},{key:'tts',label:'字幕与配音',status:'stale',fresh:false,freshness_label:'仍有2句待配音',selected:true}]},
    {uid:'current',title:'已就绪项目',steps:[{key:'tts',label:'字幕与配音',status:'stale',fresh:true,freshness_label:'配音已补齐',selected:true}]},
    {uid:'failed',title:'失败且有更新项目',status:'failed',steps:[{key:'split',label:'语义整理',status:'done',fresh:false,freshness_reason:'整理参数已变化',selected:true},{key:'tts',label:'字幕与配音',status:'failed',fresh:false,selected:true}]},
    {uid:'running',title:'运行且有更新项目',status:'processing',running_step:'tts',steps:[{key:'split',label:'语义整理',status:'done',fresh:false,selected:true},{key:'tts',label:'字幕与配音',status:'running',fresh:false,selected:true}]},
    {uid:'failure-only',title:'只有失败项目',status:'failed',steps:[{key:'tts',label:'字幕与配音',status:'failed',fresh:false,selected:true}]},
    {uid:'running-only',title:'只有运行项目',status:'processing',running_step:'tts',steps:[{key:'tts',label:'字幕与配音',status:'running',fresh:false,selected:true}]}]
    .map(row=>({status:'done',...row,dir_exists:true,params:{}}))
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Projects)})})})
  app.use(createPinia());app.use(router);setup(app)
  const triggers:any[]=[],details:any[]=[];let page:any
  app.mixin({created(){if(this.$options.__name==='ProjectsView'){page=this.$.setupState;page.projects=entries;page.loading=false};if(this.$options.__name==='FreshnessDetails'){triggers.push(this.$.props);details.push({props:this.$.props,state:this.$.setupState})}}})
  const html=await renderToString(app)
  assert.equal((html.match(/class="freshness-badge"/g)||[]).length,3)
  assert.match(html,/需更新 · 2步/)
  assert.match(html,/aria-label="有变化项目 · 查看更新原因"/)
  assert.doesNotMatch(html,/为什么需更新/)
  assert.match(html,/字幕与配音：仍有2句待配音，进入步骤/)
  assert.doesNotMatch(html,/字幕与配音：需更新，进入步骤/)
  assert.match(html,/字幕与配音：配音已补齐，进入步骤/)
  assert.equal(triggers.length,3)
  const needs=triggers.find(trigger=>trigger.uid==='needs'),failed=triggers.find(trigger=>trigger.uid==='failed')
  assert.equal(needs.statusText,'需更新 · 2步');assert.match(needs.summary,/语义整理：上游文字已变化/)
  assert.equal(failed.statusText,'需更新 · 1步');assert.match(failed.summary,/语义整理：整理参数已变化/)
  assert.match(html,/有失败步骤/);assert.match(html,/制作中/)
  assert.match(html,/aria-label="失败且有更新项目 · 查看更新原因"/)
  assert.match(html,/aria-label="运行且有更新项目 · 查看更新原因"/)
  assert.doesNotMatch(html,/aria-label="(?:已就绪项目|只有失败项目|只有运行项目) · 查看更新原因"/)
  const failedLink=html.indexOf('href="/projects/failed"')
  const failedTitleLine=html.slice(html.lastIndexOf('class="title-line"',failedLink),html.indexOf('class="project-source"',failedLink))
  assert.match(failedTitleLine,/有失败步骤/);assert.match(failedTitleLine,/需更新 · 1步/)
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{reads.push({url,method:options?.method});return {ok:true,json:async()=>({project_uid:'failed',steps:[{key:'split',label:'语义整理',status:'done',fresh:false,changes:[],warnings:[],summary:'整理参数已变化'}]})}})
  await details.find(detail=>detail.props.uid==='failed').state.open()
  assert.deepEqual(reads,[{url:'/api/projects/failed/freshness',method:'GET'}])
  assert.equal(details.find(detail=>detail.props.uid==='failed').state.show,true)
  assert.equal(page.projects.find((project:any)=>project.uid==='failed').status,'failed')
  page.statusFilter='failed';assert.deepEqual(page.sortedProjects.map((project:any)=>project.uid).sort(),['failed','failure-only'])
  page.statusFilter='needs_update';assert.deepEqual(page.sortedProjects.map((project:any)=>project.uid).sort(),['failed','needs','running'])
  page.searchText='失败';assert.deepEqual(page.sortedProjects.map((project:any)=>project.uid),['failed'])
  assert.equal(page.PROJECT_STATUS_OPTS.find((option:any)=>option.value==='needs_update').label,'需更新')
  for(const button of html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g))assert.equal((button[0].match(/<button\b/g)||[]).length,1,'不得嵌套状态按钮与分段导航按钮')
})
