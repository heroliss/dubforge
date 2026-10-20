import assert from 'node:assert/strict'
import test, {before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createMemoryHistory,createRouter} from 'vue-router'

let server:ViteDevServer,Records:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],
    appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Records=(await server.ssrLoadModule('/src/views/PublicationRecordsView.vue')).default
})
after(async()=>{mock.restoreAll();await server.close()})
const entry=(id:string,project_uid:string,status:string,title:string)=>({id,project_uid,status,
  draft:{account_id:'123',parts:[{title:'配音版'}]},snapshot:{settings:{title}},receipt:{},updated_at:'2026-10-08T09:00:00'})
async function render(seed:(page:any)=>void=()=>{}){
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/publications')
  let page:any
  const app=createSSRApp({render:()=>h(Records)})
  app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='PublicationRecordsView'){page=this.$.setupState;seed(page)}}})
  const html=await renderToString(app)
  return {page,html}
}
const sources=[{uid:'active',title:'旅行第一集',archived:false},{uid:'archive',title:'旅行第二集',archived:true}]
const entries=[entry('pub1','active','draft','旅行草稿'),entry('pub2','archive','uploading','归档中的成片'),
  {...entry('pub3','removed','published','保留的回执'),receipt:{bvid:'BVretained'},snapshot:{settings:{title:'保留的回执'},account_name:'发布账号'}}]
const seed=(page:any)=>{page.records=entries;page.projects=sources;page.accounts=[{id:'123',name:'发布账号'}];page.ready=true;page.projectsReady=true}

test('跨项目记录保留归档与已删除项目回执，并按记录编号进入项目最后一步',async()=>{
  const {html}=await render(seed)
  assert.match(html,/全部发布记录/)
  assert.match(html,/href="\/projects\/active\/publish\?publication=pub1"/)
  assert.match(html,/href="\/projects\/archive\/publish\?publication=pub2"/)
  assert.match(html,/旅行第二集 · 已归档/)
  assert.match(html,/href="\/publications\/pub3"/)
  assert.match(html,/来源项目已删除 · 记录保留/)
  assert.match(html,/https:\/\/www.bilibili.com\/video\/BVretained/)
  assert.doesNotMatch(html,/新建草稿|确认开始发布|连接到B站账号/)
})

test('状态、项目与搜索共同筛选，包含已删除项目记录',async()=>{
  const {page,html}=await render(page=>{seed(page);page.projectFilter='missing';page.search='BVretained';page.statusFilter='published'})
  assert.equal(page.filteredRecords.length,1)
  assert.match(html,/显示 1 \/ 3 条记录/)
  assert.match(html,/保留的回执/)
  assert.doesNotMatch(html,/record-title[^>]*>.*旅行草稿/)
  page.clearFilters()
  assert.equal(page.filteredRecords.length,3)
})

test('刷新只读取本地状态，包含归档项目且不访问平台登录或投稿接口',async t=>{
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    reads.push({url,options})
    const data=String(url).includes('/publications')?entries:String(url).includes('/api/projects')?sources:{accounts:[{id:'123',name:'发布账号'}]}
    return {ok:true,json:async()=>data}
  })
  const {page}=await render()
  await page.refresh()
  assert.deepEqual(reads.map(read=>read.url),['/api/publishing/publications','/api/projects?archive=all','/api/publishing/catalog'])
  assert.ok(reads.every(read=>!read.options.method||read.options.method==='GET'))
  assert.equal(page.records.length,3);assert.equal(page.projectsReady,true)
  assert.equal(page.recordTarget(page.records[1]).path,'/projects/archive/publish')
})

test('项目读取失败不误报删除，记录读取失败保留旧结果',async t=>{
  t.mock.method(globalThis,'fetch',async(url:any)=>{
    if(String(url).includes('/catalog'))return {ok:true,json:async()=>({accounts:[]})}
    throw new Error('读取失败')
  })
  const {page}=await render(seed)
  await page.refresh()
  assert.equal(page.records.length,3);assert.equal(page.projectsReady,false)
  assert.equal(page.recordTarget(entries[2]),'/publications/pub3')
  assert.doesNotMatch(page.projectLabel(entries[2]),/已删除/)
  assert.match(page.contextError,/仍可打开保留的发布记录/)
  assert.match(page.error,/读取失败/)
})

test('迟到刷新不会覆盖新结果，离开时的请求可取消',async t=>{
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',(url:any,options:any)=>new Promise(resolve=>reads.push({url,options,resolve})))
  const {page}=await render()
  const first=page.refresh(),second=page.refresh()
  assert.ok(reads.slice(0,3).every(read=>read.options.signal.aborted))
  for(const read of reads.slice(3))read.resolve({ok:true,json:async()=>String(read.url).includes('/publications')?[entry('new','active','draft','新结果')]:String(read.url).includes('/api/projects')?sources:{accounts:[]}})
  await second
  for(const read of reads.slice(0,3))read.resolve({ok:true,json:async()=>String(read.url).includes('/publications')?[entry('old','active','draft','旧结果')]:String(read.url).includes('/api/projects')?sources:{accounts:[]}})
  await first
  assert.equal(page.records[0].id,'new');assert.equal(page.loading,false)
})
test('归档和回收独立浏览，恢复入口保留且不误导用户去系统回收站',async t=>{
  const reads:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:any)=>{reads.push(url);return {ok:true,json:async()=>String(url).includes('/publications')?[]:String(url).includes('/projects')?sources:{accounts:[]}}})
  const {page}=await render(seed)
  await page.setLifecycle('deleted');assert.equal(page.lifecycleFilter,'deleted');assert.equal(reads[0],'/api/publishing/publications?lifecycle=deleted')
  await page.setLifecycle('archived');assert.equal(reads[3],'/api/publishing/publications?lifecycle=archived')
  const {html}=await render(page=>{seed(page);page.lifecycleFilter='deleted';page.records=[{...entries[0],lifecycle:'deleted',lifecycle_actions:{restore:{allowed:true,reason:''}}}]})
  assert.match(html,/回收的草稿/);assert.match(html,/这里不是系统回收站/);assert.match(html,/恢复记录/)
  assert.match(html,/href="\/publications\/pub1"/)
})
