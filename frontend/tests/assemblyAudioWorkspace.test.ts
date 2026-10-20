import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createRouter,createMemoryHistory,RouterView} from 'vue-router'
import {createPinia} from 'pinia'
import {NDialogProvider,NMessageProvider} from 'naive-ui'

let server:ViteDevServer,Workspace:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Workspace=(await server.ssrLoadModule('/src/components/AssemblyVersionWorkspace.vue')).default
  mock.method(globalThis,'fetch',async()=>new Response(JSON.stringify({overlay:{items:[]},overlay_revision:'r'})))
})
after(async()=>{mock.restoreAll();await server.close()})
const fields=[{path:'assemble.voice_volume',label:'人声音量',group:'声音',type:'float',help:'1 为原音量，0 为静音。'},
  {path:'assemble.final_loudness_mode',label:'最终成片响度',group:'声音',type:'select',help:'统一成片总体音量。',options:[{label:'统一响度',value:'uniform'},{label:'保留动态',value:'normalize'},{label:'保持混音',value:'off'}]},
  {path:'assemble.final_loudness_lufs',label:'成片目标响度（LUFS）',group:'声音',type:'float',help:'建议统一使用 −16 LUFS。',depends_on:[['assemble.final_loudness_mode',['normalize','uniform']]]},
  {path:'assemble.final_true_peak_db',label:'成片真峰值上限（dBTP）',group:'声音',type:'float',min:-6,max:0,help:'建议 −1.5 dBTP。',depends_on:[['assemble.final_loudness_mode',['normalize','uniform']]]}]
async function render(mode='uniform',overrides:Record<string,unknown>={},defaultPatch:Record<string,unknown>={}){
  let page:any
  const controls:Record<string,any>={},helps:Record<string,any>={},resets:Record<string,any>={}
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Workspace,props:{uid:'fixture',configureOnly:true}},{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(RouterView)})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){
   if(this.$options.__name==='ParamControl')controls[this.$props.field.path]=this
   if(this.$options.__name==='ParameterHelp')helps[this.$props.label]=this
   if(this.$options.__name==='ParameterResetButton')resets[this.$props.label]=this
   if(this.$options.__name==='AssemblyVersionWorkspace'){
    page=this.$.setupState;page.adopt({fields,versions:[{id:'dub',name:'配音版',dubbing:true,burn_subtitles:false,include_comments:false,overrides}],
      defaults:{'assemble.voice_volume':1,'assemble.final_loudness_mode':mode,'assemble.final_loudness_lufs':-16,'assemble.final_true_peak_db':-1.5,...defaultPatch},
      sources:{'assemble.final_loudness_lufs':{kind:'global',frozen:true}},source:{kind:'project'},revision:1,fingerprint:'f'})
  }}})
  const html=await renderToString(app)
  return {page,html,controls,helps,resets}
}

test('版本声音设置分清监听、平衡和输出，控件整行支持问号说明并保留继承操作',async()=>{
  const {page,html}=await render()
  for(const label of ['本机监听','人声与背景平衡','统一输出响度','播放器音量，只影响本机','aria-label="人声音量说明"','aria-label="成片目标响度（LUFS）说明"','aria-label="人声音量恢复项目默认"']) assert.ok(html.includes(label),label)
  assert.deepEqual(page.styleGroups('声音').map((row:any)=>row.name),['人声与背景平衡','统一输出响度'])
  assert.equal(page.fieldDisabled(fields[2]),false)
  assert.deepEqual(page.active.overrides,{})
  page.override('assemble.final_loudness_lufs',-18)
  assert.equal(page.fieldSource('assemble.final_loudness_lufs').kind,'version')
  page.inherit('assemble.final_loudness_lufs')
  assert.equal(page.values['assemble.final_loudness_lufs'],-16)
  assert.equal(page.fieldSource('assemble.final_loudness_lufs').kind,'global')
})

test('每个版本参数都有独立恢复小按钮，已继承禁用，单项恢复保留其他精调',async()=>{
  const {page,resets}=await render('uniform',{'assemble.voice_volume':0,'assemble.final_true_peak_db':0})
  for(const field of fields) assert.ok(resets[field.label],field.path)
  assert.equal(resets['人声音量'].$props.disabled,false)
  assert.equal(resets['成片目标响度（LUFS）'].$props.disabled,true)
  resets['人声音量'].$emit('reset')
  assert.equal(page.values['assemble.voice_volume'],1)
  assert.equal(Object.hasOwn(page.active.overrides,'assemble.voice_volume'),false)
  assert.equal(page.active.overrides['assemble.final_true_peak_db'],0)
  assert.equal(page.dirty,true)
})

test('版本名称和三项结构开关可逐项撤销，恢复不重置其他草稿',async()=>{
  const {page,resets}=await render()
  for(const key of ['版本名称','声音','烧录字幕','叠加评论']) assert.ok(resets[key],key)
  page.active.name='新的名称';page.active.dubbing=false;page.active.burn_subtitles=true;page.active.include_comments=true
  page.override('assemble.final_true_peak_db',0)
  assert.equal(page.definitionChanged('name'),true)
  page.restoreDefinition('name')
  assert.equal(page.active.name,'配音版')
  assert.equal(page.active.dubbing,false)
  page.restoreDefinition('dubbing')
  assert.equal(page.active.dubbing,true)
  page.restoreDefinition('burn_subtitles');page.restoreDefinition('include_comments')
  assert.equal(page.active.burn_subtitles,false);assert.equal(page.active.include_comments,false)
  assert.deepEqual(page.active.overrides,{'assemble.final_true_peak_db':0})
})

test('统一和保留动态都允许设置目标，关闭时保留参数但禁用，无自动更改配置',async()=>{
  for(const mode of ['uniform','normalize','off']){
    const {page,html}=await render(mode,{'assemble.final_loudness_lufs':-18})
    assert.equal(page.fieldDisabled(fields[2]),mode==='off')
    assert.equal(page.fieldDisabled(fields[3]),mode==='off')
    assert.equal(page.values['assemble.final_loudness_lufs'],-18)
    assert.deepEqual(page.active.overrides,{'assemble.final_loudness_lufs':-18})
    assert.ok(html.includes('恢复项目默认'))
  }
})

test('版本继承或覆盖的真峰值0仅在有效模式启用时轻提示，不保存或更改配置',async()=>{
  const modePath='assemble.final_loudness_mode',peakPath='assemble.final_true_peak_db'
  for(const [mode,overrides,defaultPatch,notice] of [
    ['uniform',{}, {[peakPath]:0},true],
    ['off',{[modePath]:'normalize',[peakPath]:0},{},true],
    ['uniform',{[modePath]:'off',[peakPath]:0},{},false],
    ['normalize',{[peakPath]:-1},{},false],
  ] as const){
    const result=await render(mode,overrides,defaultPatch)
    assert.equal(Boolean(result.controls[peakPath].$.setupState.audioNotice),notice)
    assert.equal(result.controls[peakPath].$props.values[modePath],result.page.values[modePath])
    assert.equal(result.page.fieldDisabled(fields[3]),result.page.values[modePath]==='off')
    const help=result.helps['成片真峰值上限（dBTP）'].$props.help
    assert.ok(help.startsWith(fields[3].help))
    assert.match(help,/作用范围/)
    assert.doesNotMatch(help,/可调至 0 dBTP/)
    assert.deepEqual(result.page.active.overrides,overrides)
    assert.equal(result.page.dirty,false)
  }
})
