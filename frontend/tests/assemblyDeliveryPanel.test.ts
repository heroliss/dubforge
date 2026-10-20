import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createRouter,createMemoryHistory} from 'vue-router'
import {createPinia} from 'pinia'
import {NMessageProvider,NDialogProvider} from 'naive-ui'
let server:ViteDevServer,Component:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Component=(await server.ssrLoadModule('/src/components/AssemblyDeliveryPanel.vue')).default
  mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
})
after(async()=>{mock.restoreAll();await server.close()})
async function render(open=false,extra:any={},seed:(page:any)=>void=()=>{}){
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
  const props={uid:'fixture',artifacts:[{name:'final_version_a.mp4',exists:true},{name:'preview_version_a_test.mp4',exists:true}],sourcePath:'D:/source.mp4',subtitlePath:'',subtitleName:'',options:[{label:'原片',value:'source'}],selected:'source',preview:{kind:'video',url:'/source.mp4'},...extra}
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Component,props)})})})
  app.use(createPinia());app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name===Component.__name){this.$.setupState.expanded=open;seed(this.$.setupState)}}})
  return renderToString(app)
}
test('正式成品与原片默认收起，不挂载额外播放器，短片不计为正式文件',async()=>{
  const html=await render()
  assert.match(html,/正式成品与原片/);assert.match(html,/1 份正式成品/)
  assert.doesNotMatch(html,/<video|<audio|选择要预览的成品/)
})
test('展开后完整媒体与短片试听分清，选择具有可访问名称，更新中不加载文件',async()=>{
  const html=await render(true)
  assert.match(html,/当前版本的短片试听与生成进度在上方/)
  assert.match(html,/aria-label="播放完整成品或原片"/)
  assert.match(html,/<video/)
  const updating=await render(true,{suspended:true})
  assert.doesNotMatch(updating,/<video/);assert.match(updating,/文件正在更新/)
})

test('部分正式版本已经提交但整轮合成仍运行时保留播放器保护，明确等待整轮结束',async()=>{
  const html=await render(true,{suspended:true},page=>{
    page.nav.uid='fixture';page.nav.steps=[{key:'assemble',status:'running',job_id:521}]
  })
  assert.match(html,/1 份正式成品/);assert.doesNotMatch(html,/<video|<audio/)
  assert.match(html,/完整成品播放将在本轮结束后恢复/)
  assert.match(html,/已完成版本的文件和报告可先查看/)
  assert.doesNotMatch(html,/文件正在更新，完成后可继续播放/)
})
