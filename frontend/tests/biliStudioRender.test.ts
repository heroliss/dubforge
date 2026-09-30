import assert from 'node:assert/strict'
import test, {before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,defineComponent,h,reactive} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {NDialogProvider,NMessageProvider,useDialog} from 'naive-ui'

let server:ViteDevServer,Studio:any,Assist:any,Import:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null}})
  Studio=(await server.ssrLoadModule('/src/components/BiliStudio.vue')).default
  Assist=(await server.ssrLoadModule('/src/components/PublishPlatformAssist.vue')).default
  Import=(await server.ssrLoadModule('/src/components/BiliContentImport.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('测试禁止未声明网络请求')})
})
after(async()=>{mock.restoreAll();await server?.close()})
const BV='BV1234567890'
const detail=()=>({bvid:BV,aid:10,mid:'123',version:'v1',collection:null,
  settings:{title:'原标题',description:'原简介',tags:['旅行'],dynamic:'',copyright:3,declaration_id:1},
  videos:[{cid:1,title:'原声',reject_reason:'<script>alert(1)</script>'},{cid:2,title:'配音'}]})
async function render(component:any,props:any={},seed:(page:any)=>void=()=>{}){
  let page:any;const dialogs:any[]=[],events:any[]=[]
  const Probe=defineComponent({setup(){const dialog=useDialog();dialog.info=dialog.warning=((o:any)=>{dialogs.push(o);return {destroy(){}}}) as any
    return()=>h(component,{...props,onChange:(...a:any[])=>events.push(a),onRefresh:()=>events.push(['refresh'])})}})
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Probe)})})})
  setup(app)
  app.mixin({created(){if(this.$options.__name===component.__name){page=this.$.setupState;seed(page)}}})
  return{html:await renderToString(app),page,dialogs,events}
}
const studioProps={accounts:[{id:'123',name:'测试账号'}],projects:[{uid:'project',title:'项目'}]}
function respond(data:any){return Promise.resolve({ok:true,json:async()=>data})}

test('创作管理真实组件打开不自动联网，操作文字区分查询、预览和投稿',async()=>{
  const {html}=await render(Studio,studioProps)
  assert.match(html,/读取我的投稿/);assert.match(html,/评论与字幕导入/);assert.match(html,/官网投稿模板/)
  assert.doesNotMatch(html,/确认修改此BV/)
})
test('读取稿件保留来源，审核错误安全显示，编辑不会隐式发请求',async()=>{
  const {html,page}=await render(Studio,studioProps,p=>p.setDetail(detail()))
  assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>alert/)
  page.edit.title='新标题';page.move(1,-1)
  assert.equal(page.dirty,true);assert.deepEqual(page.order,[2,1])
  page.resetEdit();assert.equal(page.dirty,false)
})
test('预览与确认分开，提交只引用冻结计划ID，双击不重复发送',async()=>{
  const {page}=await render(Studio,studioProps,p=>p.setDetail(detail()))
  const calls:any[]=[];let release:any
  mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    const body=options?.body?JSON.parse(options.body):undefined;calls.push({url,body})
    if(String(url).endsWith('/commit'))await new Promise(r=>{release=r})
    return respond(String(url).endsWith('/read/detail')?detail():String(url).endsWith('/changes')&&body
      ?{id:'frozen-plan',bvid:BV,kind:'edit',preview:{before:detail(),after:{settings:{...detail().settings,title:'新标题'},videos:detail().videos}}}
      :String(url).endsWith('/changes')?[]:{status:'done'})
  })
  page.edit.title='新标题'
  await page.prepare('edit')
  assert.equal(calls.length,1);assert.deepEqual(calls[0].body.values,{title:'新标题'})
  assert.equal(page.modal,true)
  const pending=page.commit();await page.commit()
  assert.equal(calls.filter(c=>c.url.endsWith('/commit')).length,1)
  assert.deepEqual(calls[1].body,{confirmed:true})
  release();await pending
  assert.equal(page.modal,false)
})
test('结果不明关闭确认按钮，读取恢复记录但不自动重发',async()=>{
  const {page}=await render(Studio,studioProps,p=>{p.setDetail(detail());p.preview={id:'plan',bvid:BV,preview:{before:detail(),after:{settings:detail().settings,videos:detail().videos}}};p.modal=true})
  let writes=0
  mock.method(globalThis,'fetch',(url:any)=>{
    if(String(url).endsWith('/commit')){writes++;return Promise.resolve({ok:false,json:async()=>({code:'PUBLISH_WRITE_UNKNOWN',message:'请核对'})})}
    return respond([{id:'plan',status:'unknown'}])
  })
  await page.commit();assert.equal(writes,1);assert.equal(page.modal,false);assert.equal(page.changes[0].status,'unknown')
})

test('追加分P的声明建议放在原确认预览，不弹第二次窗口、不阻止继续',async()=>{
  const warning='追加分P含配音版本，建议核对AI声明。此次保留目标稿件原声明，不自动修改；平台可能要求补标。可返回调整，也可确认继续追加。'
  const {page,dialogs,html}=await render(Studio,studioProps,p=>{
    const original=detail();original.settings.declaration_id=-1
    p.setDetail(original)
    p.preview={id:'append-plan',bvid:BV,kind:'append',preview:{before:original,
      after:{settings:original.settings,videos:[...original.videos,{cid:3,title:'新配音'}]},warnings:[warning]}}
    p.modal=true
  })
  assert.equal(page.preview.preview.warnings[0],warning)
  assert.equal(page.edit.declaration_id,-1)
  assert.equal(page.diffs.length,0)
  assert.match(html,/保留原视频、封面、文案及创作声明/)
  const calls:any[]=[]
  mock.method(globalThis,'fetch',(url:any,options:any)=>{
    calls.push([url,options?.body?JSON.parse(options.body):null])
    return respond(String(url).endsWith('/read/detail')?detail():String(url).endsWith('/changes')?[]:{status:'done'})
  })
  assert.equal(calls.length,0)
  assert.equal(dialogs.length,0)
  await page.commit()
  assert.equal(calls.filter(c=>c[0].endsWith('/commit')).length,1)
  assert.deepEqual(calls[0][1],{confirmed:true})
  assert.equal(dialogs.length,0)
})
test('标签建议需点击添加，保留现有标签，旧标题返回的推荐被忽略',async()=>{
  const values=reactive({title:'标题',description:'简介',tags:['旅行'],tid:21})
  const {page,events}=await render(Assist,{accountId:'123',values})
  let resolve:any
  mock.method(globalThis,'fetch',()=>new Promise(r=>{resolve=r}))
  const loading=page.load('tags');values.title='已修改'
  resolve({ok:true,json:async()=>({suggestions:['美食'],checks:[]})});await loading
  assert.equal(page.tags,null);assert.equal(events.length,0)
  page.add('美食');assert.deepEqual(events,[['tags',['旅行','美食']]])
  page.add('旅行');assert.equal(events.length,1)
})
test('话题只有显式确认后更新草稿，取消不报名',async()=>{
  const {page,events,dialogs}=await render(Assist,{accountId:'123',values:{tags:[]}})
  page.topic({id:10,name:'旅行',mission_id:20,conditions:'需原创'})
  assert.equal(events.length,0);assert.match(dialogs[0].content,/需原创/)
  dialogs[0].onPositiveClick();assert.deepEqual(events,[['topic_id',10],['topic_name','旅行'],['mission_id',20]])
})
test('官网模板导入只提交人工选择字段，不修改远端模板',async()=>{
  const {page,dialogs}=await render(Studio,studioProps);page.templateFields=['tags']
  const calls:any[]=[];mock.method(globalThis,'fetch',(url:any,options:any)=>{calls.push([url,JSON.parse(options.body)]);return respond({})})
  page.importTemplate({id:9,name:'官网模板'});assert.equal(calls.length,0)
  await dialogs[0].onPositiveClick()
  assert.match(calls[0][0],/import-template$/);assert.deepEqual(calls[0][1],{id:9,fields:['tags'],confirmed:true})
})
test('评论和字幕导入显示明确目标；仅确认后写入本地，不触发制作',async()=>{
  const {page,dialogs}=await render(Import,{accountId:'123',projects:studioProps.projects,defaultProject:'project'})
  const calls:any[]=[];mock.method(globalThis,'fetch',(url:any,options:any)=>{calls.push([url,JSON.parse(options.body)]);return respond({count:1})})
  page.importData({items:[{}],import_key:'candidate'},'comments');assert.equal(calls.length,0)
  assert.match(dialogs[0].content,/项目.*来源ID去重/)
  await dialogs[0].onPositiveClick()
  assert.equal(calls.length,1);assert.match(calls[0][0],/import-content$/)
  assert.deepEqual(calls[0][1],{import_key:'candidate',project_uid:'project',confirmed:true})
})
