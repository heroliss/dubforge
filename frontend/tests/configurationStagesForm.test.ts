import assert from 'node:assert/strict'
import test, {before,after} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {NMessageProvider,NDialogProvider} from 'naive-ui'

let server:ViteDevServer,Form:any
before(async()=>{
 server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
 Form=(await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
})
after(()=>server.close())
const field=(path:string,label:string)=>({path,label,type:'str'})
const state={name:'测试配置',revision:1,fingerprint:'f',overrides:{},parent_sources:{},version_fields:[],defaults:{
 'assembly.versions':[],'tts.backend':'indextts25','subtitles.mode':'all','assemble.voice_volume':1},sections:{
 content_review:{label:'内容审查',fields:[field('content_review.enabled','启用输出筛查')]},
 assemble:{label:'合成成片',fields:[field('assemble.voice_volume','人声音量')]},
 comments:{label:'上屏评论',fields:[field('comments.prompt','评分要求')]},
 subtitles:{label:'字幕导出',fields:[field('subtitles.mode','导出内容')]},
 tts:{label:'旧配音标题',fields:[field('tts.backend','配音服务')]}}}
async function render(t:any,props:any){
 const writes:any[]=[],stageEvents:string[]=[]
 t.mock.method(globalThis,'fetch',(_url:any,options:any)=>{if(options?.body)writes.push(options.body);return new Promise(()=>{})})
 let page:any,liveProps:any
 const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Form,{scope:'global',...props,onStageChange:(key:string)=>stageEvents.push(key)})})})})
 setup(app)
 app.mixin({created(){if(this.$options.__name==='ConfigurationForm'){
  page=this.$.setupState;liveProps=this.$.props;page.adopt(state);page.loading=false
 }}})
 const html=await renderToString(app)
 return {page,liveProps,html,writes,stageEvents}
}

test('配置栏目归并字幕并按工作流排序，通用规则独立；切换栏目保留同一草稿',async t=>{
 const {page,html,writes,stageEvents}=await render(t,{stage:'subtitles'})
 assert.deepEqual(page.sections.map(([key]:any)=>key),['tts','comments','assemble','content_review'])
 assert.equal(page.tab,'tts');assert.match(html,/字幕与配音/);assert.match(html,/字幕导出与时间/)
 assert.match(html,/aria-label="跨流程通用规则"/);assert.doesNotMatch(html,/>字幕导出<|>旧配音标题</)
 page.change('tts.backend','gptsovits');page.selectTab('comments')
 assert.equal(page.tab,'comments');assert.equal(page.patch['tts.backend'],'gptsovits');assert.equal(page.dirty,true)
 assert.deepEqual(stageEvents,['comments']);assert.equal(writes.length,0)
})

test('局部旧字幕设置入口继续仅编辑原字幕路径，配音子组不混入字幕导出参数',async t=>{
 const {page,html}=await render(t,{sections:['subtitles']})
 assert.equal(page.tab,'tts');assert.match(html,/导出内容/);assert.doesNotMatch(html,/配音服务/)
 assert.deepEqual(page.styleGroups(page.sections[0][1],'tts').map((group:any)=>[group.name,group.fields.map((field:any)=>field.path)]),
  [['字幕导出与时间',['subtitles.mode']]])
})

test('应用地址阶段只变可见栏目，不读取新配置或丢弃编辑；未知阶段安全回退',async t=>{
 const {page,liveProps,writes}=await render(t,{stage:'missing-stage'})
 assert.equal(page.tab,'tts')
 // SSR 渲染后不继续运行 watcher，直接调用其绑定的阶段应用函数。
 page.change('tts.backend','gptsovits');liveProps.stage='content_review';page.adoptStage()
 assert.equal(page.tab,'content_review');assert.equal(page.patch['tts.backend'],'gptsovits');assert.equal(page.dirty,true)
 liveProps.stage='assemble';page.adoptStage();assert.equal(page.tab,'assemble')
 assert.equal(writes.length,0)
})

test('保存、载入预设及文案结果接收期间不卸载当前栏目，普通草稿仍可切换',async t=>{
 const {page,stageEvents,writes}=await render(t,{stage:'tts'})
 page.change('tts.backend','gptsovits')
 for(const busy of ['busy','presetBusy','copyBusy']){
  page[busy]=true;page.selectTab('comments')
  assert.equal(page.tab,'tts',busy);assert.deepEqual(stageEvents,[])
  page[busy]=false
 }
 page.selectTab('comments');assert.equal(page.tab,'comments')
 assert.equal(page.patch['tts.backend'],'gptsovits');assert.equal(page.dirty,true)
 assert.deepEqual(stageEvents,['comments']);assert.equal(writes.length,0)
})
