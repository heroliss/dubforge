import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { parse } from '@vue/compiler-sfc'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NDialogProvider, NMessageProvider } from 'naive-ui'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { createPinia } from 'pinia'

// 不监听端口、不读写用户配置；真实组件事件链 + HTML 结构共同覆盖 label 默认激活回归。
let server: ViteDevServer
before(async () => { server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
  configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } }) })
after(() => server.close())

const prefix = 'comments.', enabled = prefix+'source_style_enabled'
const values = { [enabled]: false, 'comments.color': '#FFFFFF', 'comments.outline_color': '#000000',
  'comments.source_color': '#FFD65A', 'comments.source_outline_color': '#30213A', 'comments.width_percent': 60, 'assembly.versions': [] }
const fields = [
  { path: 'comments.width_percent', label: '最大宽度（画面 %）', type: 'float', min: 0, max: 100, group: '评论' },
  { path: enabled, label: '原文独立样式', type: 'bool', group: '评论' },
  { path: 'comments.color', label: '译文颜色', type: 'color', group: '评论' },
  { path: 'comments.outline_color', label: '译文描边', type: 'color', group: '评论' },
  { path: 'comments.source_color', label: '原文颜色', type: 'color', group: '评论', depends_on: [[enabled,true]] },
]
function config() { return { key:'global', name:'全局默认', overrides:{}, defaults:values, parent_sources:{},
  sections:{ comments:{ label:'评论',fields } }, version_fields:fields } }

async function render(t:any, name:string, initial=false, locked=false) {
  t.mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
  const Component = (await server.ssrLoadModule(`/src/components/${name}.vue`)).default
  const props=name==='ConfigurationForm'?{scope:'global',sections:['comments']}:{uid:'fixture',configureOnly:true,disabled:locked}
  const router = createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Component,props},
    {path:'/:pathMatch(.*)*',component:{render:()=>null}}]})
  await router.push('/')
  let page:any, switchControl:any, palette:any, widthControl:any
  const buttons:any[]=[]
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(RouterView)})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){
    if(this.$options.__name===name){
      page=this.$.setupState
      if(name==='ConfigurationForm') { page.adopt({...config(),overrides:initial?{[enabled]:true}:{}}); page.loading=false; page.busy=locked }
      if(name==='CommentStudioPanel') {
        const state={ rows:[{id:'a',source_hash:'a',text:'Original',text_zh:'译文'}], fields, defaults:{source_style_enabled:initial,source_color:values['comments.source_color'],width_percent:60},
          configuration:{...config(),sources:{},overrides:initial?{[enabled]:true}:{}}, overlay_revision:'r1', overlay:null }
        page.state=state;page.hydrate(state);page.saving=locked
      }
      if(name==='AssemblyVersionWorkspace') { page.adopt({defaults:values,sources:{},fields,versions:[{id:'v1',name:'双语',dubbing:false,burn_subtitles:false,include_comments:true,overrides:initial?{[enabled]:true}:{}}]});page.ready=true }
    }
    if(this.$options.__name==='TextColorPresets')palette=this.$.setupState
    if(this.$options.__name==='ParamControl' && this.$props.field.path==='comments.width_percent')widthControl=this.$.setupState
    if(this.$options.name==='Switch' && this.$attrs['aria-label']==='原文独立样式')switchControl=this.$.setupState
    if(this.$options.name==='Button' && this.$attrs['aria-label']==='原文独立样式恢复继承')buttons.push(this)
  }})
  const html=await renderToString(app)
  return {page,switchControl,palette,widthControl,buttons,html}
}

test('设置行不得用 label 包住恢复按钮与复合控件，避免浏览器额外触发恢复继承', async()=>{
  for(const name of ['ConfigurationForm','CommentStudioPanel','AssemblyVersionWorkspace','ProjectParameterForm']) {
    const {descriptor}=parse(await readFile(new URL(`../src/components/${name}.vue`,import.meta.url),'utf8'))
    const labels:any[]=[]
    function visit(node:any,insideLabel=false){
      if(node.tag==='label')insideLabel=true
      if(insideLabel && ['n-button','NButton','button'].includes(node.tag))labels.push(node.loc.source)
      for(const child of node.children||[])visit(child,insideLabel)
    }
    visit(descriptor.template!.ast)
    assert.deepEqual(labels,[],name)
  }
})

for(const name of ['ConfigurationForm','CommentStudioPanel','AssemblyVersionWorkspace']) {
  test(`${name}：宽度控件修改同一草稿，自动值 0 不等于恢复继承`,async t=>{
    const {page,widthControl,html}=await render(t,name)
    const current=()=>name==='CommentStudioPanel'?page.settings.width_percent:page.values['comments.width_percent']
    assert.match(html,/最大宽度（画面 %）/); assert.ok(widthControl)
    assert.equal(current(),60)
    widthControl.val=45; assert.equal(current(),45); assert.equal(page.dirty,true)
    widthControl.val=0; assert.equal(current(),0)
    assert.equal(values['comments.width_percent'],60)
  })
  const current=(page:any)=>name==='CommentStudioPanel'?page.settings.source_style_enabled:page.values[enabled]
  test(`${name}：真实开关事件只开启草稿，原文配色与继承默认不被覆盖`,async t=>{
    const {page,switchControl,html}=await render(t,name)
    assert.ok(switchControl,'必须使用真实 NSwitch')
    assert.match(html,/跟随译文/)
    assert.match(html,/原文 · 单独设置/)
    switchControl.handleClick()
    assert.equal(current(page),true)
    assert.equal(page.dirty,true)
    assert.equal(values[enabled],false)
    assert.equal(name==='CommentStudioPanel'?page.settings.source_color:page.values['comments.source_color'],'#FFD65A')
  })
  test(`${name}：上方原文入口接入同一草稿；锁定时开关和入口都不改值`,async t=>{
    const {page,palette}=await render(t,name)
    assert.ok(palette)
    palette.selectTarget(true)
    assert.equal(current(page),true)
    const locked=await render(t,name,false,true)
    locked.switchControl.handleClick();locked.palette.selectTarget(true)
    assert.equal(current(locked.page),false)
    assert.equal(locked.page.dirty,false)
  })
  test(`${name}：关闭独立样式保留备用配色，恢复继承是单独操作`,async t=>{
    const {page,switchControl,buttons,html}=await render(t,name,true)
    assert.match(html,/独立设置/)
    assert.ok(buttons.length)
    switchControl.handleClick()
    assert.equal(current(page),false)
    assert.equal(name==='CommentStudioPanel'?page.settings.source_color:page.values['comments.source_color'],'#FFD65A')
    const saved=await render(t,name,true)
    // 使用模板绑定的按钮事件；不是模拟调用父级方法。
    saved.buttons[0].$.setupState.handleClick({preventDefault(){},stopPropagation(){}})
    assert.equal(current(saved.page),false)
  })
}
