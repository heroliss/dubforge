import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import * as Vue from 'vue'
import { compile } from '@vue/compiler-dom'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
import { assemblyEncodingValues, encodingFieldHelp, VIDEO_ENCODING_PATHS } from '../src/utils/assemblyEncoding.ts'
import { assemblySettingValue } from '../src/utils/assemblyInheritance.ts'
import { sourcePresentation } from '../src/utils/configurationSource.ts'

const encoder='assemble.video_encoder',crf='assemble.crf',cq='assemble.nvenc_cq'
const fields=[
  {path:encoder,label:'视频编码方式',type:'select',group:'输出',options:[{label:'软件编码（H.264 / CPU）',value:'software'},{label:'NVIDIA 硬件编码（H.264 / NVENC）',value:'nvenc'}]},
  {path:crf,label:'软件画质 CRF',type:'int',group:'输出',min:14,max:28,depends_on:[[encoder,'software']]},
  {path:cq,label:'硬件画质 CQ',type:'int',group:'输出',min:14,max:28,depends_on:[[encoder,'nvenc']]},
  {path:'assemble.subtitle_size',label:'字幕字号',type:'int',group:'字幕'},
]
const version=(patch:any={})=>({id:'version',name:'测试版本',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{},...patch})
const data=(patch:any={})=>({fields,defaults:{[encoder]:'software',[crf]:22,[cq]:18},versions:[version()],revision:7,fingerprint:'saved-config',sources:{[encoder]:{kind:'project',frozen:true}},...patch})
let server:ViteDevServer,Workspace:any,Configuration:any,ProjectForm:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Workspace=(await server.ssrLoadModule('/src/components/AssemblyVersionWorkspace.vue')).default
  Configuration=(await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
  ProjectForm=(await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
  mock.method(globalThis,'fetch',()=>new Promise(()=>{}))
})
after(async()=>{mock.restoreAll();await server.close()})

async function render(t:any,document=data(),seed:(page:any)=>void=()=>{}){
  let page:any;const controls:Record<string,any>={},helps:Record<string,any>={}
  const pinia=createPinia(),router=createRouter({history:createMemoryHistory(),routes:[{path:'/',component:Workspace,props:{uid:'isolated-encoding',configureOnly:true}},{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
  const app=Vue.createSSRApp({render:()=>Vue.h(NMessageProvider,null,{default:()=>Vue.h(NDialogProvider,null,{default:()=>Vue.h(RouterView)})})})
  app.use(pinia);app.use(router);setup(app)
  app.mixin({created(){
    if(this.$options.__name==='AssemblyVersionWorkspace'){page=this.$.setupState;page.adopt(document);page.ready=true;seed(page)}
    if(this.$options.__name==='ParamControl')controls[this.$props.field.path]=this
    if(this.$options.__name==='ParameterHelp')helps[this.$props.label]=this
  }})
  t.after(()=>{page.cancelReads();disposePinia(pinia)})
  const html=await renderToString(app)
  return {page,html,controls,helps}
}

test('编码方式枚举中文呈现，旧冻结快照只按软件解释，不借用后来全局硬件/CQ',()=>{
  assert.match(assemblySettingValue('software',encoder),/软件编码/)
  assert.match(assemblySettingValue('nvenc',encoder),/NVIDIA 硬件编码/)
  const defaults={[encoder]:'nvenc',[crf]:24,[cq]:25,'assemble.subtitle_size':40},saved={[crf]:18}
  const result=assemblyEncodingValues(defaults,saved)
  assert.equal(result[encoder],'software');assert.equal(result[crf],18);assert.equal(result[cq],undefined)
  assert.equal(result['assemble.subtitle_size'],40);assert.equal(defaults[encoder],'nvenc')
  assert.deepEqual(saved,{[crf]:18});assert.equal(assemblyEncodingValues({[encoder]:'unknown'})[encoder],'unknown','损坏枚举不静默回退软件')
  assert.match(encodingFieldHelp(fields[2],{[encoder]:'software'}),/暂不生效.*保留/)
  assert.match(encodingFieldHelp(fields[2],{[encoder]:'nvenc'}),/不是同一质量刻度/)
  assert.doesNotMatch(encodingFieldHelp(fields[2],{[encoder]:'nvenc'}),/暂不生效/)
})

test('真实版本表单依赖只禁用非当前质量项，保留各自参数、继承来源与透明说明',async t=>{
  for(const mode of ['software','nvenc']){
    const {page,html,controls,helps}=await render(t,data({defaults:{[encoder]:mode,[crf]:22,[cq]:18}}))
    assert.equal(controls[crf].$props.disabled,mode!=='software');assert.equal(controls[cq].$props.disabled,mode!=='nvenc')
    assert.equal(page.values[crf],22);assert.equal(page.values[cq],18)
    assert.match(html,/CQ 与软件 CRF 不是同一质量刻度/)
    assert.match(helps['硬件画质 CQ'].$props.help,/建议先生成试片/)
    assert.equal(page.fieldSource(encoder).kind,'project')
    assert.deepEqual(page.active.overrides,{})
  }
})

test('保存仅提交显式版本覆盖与已有revision/fingerprint，不从显示fallback生成覆盖或自动运行',async t=>{
  const {page}=await render(t);const requests:any[]=[]
  page.override(encoder,'nvenc');page.override(cq,20)
  t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{
    requests.push({url:String(url),method:options.method,body:JSON.parse(options.body)})
    assert.equal(options.method,'PUT','不得创建编码/试片/发布任务')
    return new Response(JSON.stringify(data({revision:8,versions:requests[0].body.versions})))
  })
  await page.save()
  assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/projects/isolated-encoding/assembly-versions')
  assert.equal(requests[0].body.revision,7);assert.equal(requests[0].body.fingerprint,'saved-config')
  assert.deepEqual(requests[0].body.versions[0].overrides,{[encoder]:'nvenc',[cq]:20})
  assert.equal(page.values[crf],22)
})

test('partial基准编码三项全锁且恢复只移除其他覆盖，update/timeline旧快照不继承新硬件默认',async t=>{
  const saved={[encoder]:'software',[crf]:18,[cq]:19}
  const overrides={...saved,'assemble.subtitle_size':42}
  const {page,html}=await render(t,data({defaults:{[encoder]:'nvenc',[crf]:24,[cq]:25},versions:[version({partial:{plan_id:'a'.repeat(32)},overrides,values:saved})]}))
  for(const path of VIDEO_ENCODING_PATHS){assert.equal(page.fieldLocked(path),true);page.override(path,path===encoder?'nvenc':27);page.inherit(path)}
  for(const path of VIDEO_ENCODING_PATHS){
    assert.equal(page.fieldSource(path).kind,'baseline');assert.equal(page.fieldSource(path).frozen,true)
    assert.match(sourcePresentation(page.fieldSource(path)).title,/基准成片.*不随当前项目或全局默认变化/)
  }
  assert.equal(page.values[encoder],'software');assert.equal(page.values[crf],18);assert.equal(page.values[cq],19)
  assert.match(html,/局部修订沿用基准成片的编码设置/)
  assert.match(html,/data-source-kind="baseline"/)
  page.restoreVersionDefaults();assert.deepEqual(page.active.overrides,saved)
  assert.equal(overrides['assemble.subtitle_size'],42,'不修改调用者的原始快照')
  for(const kind of ['update','timeline']){
    const {page}=await render(t,data({defaults:{[encoder]:'nvenc',[cq]:25},versions:[version({[kind]:{plan_id:'b'.repeat(32)},values:{[crf]:18}})]}))
    assert.equal(page.values[encoder],'software');assert.equal(page.values[cq],undefined)
    for(const path of VIDEO_ENCODING_PATHS)assert.equal(page.fieldLocked(path),true)
    assert.deepEqual(page.active.overrides,{})
  }
})

test('旧schema只有CRF时仍可编辑原软件质量，不凭空显示硬件选项',async t=>{
  const {page,html,controls}=await render(t,data({fields:[{...fields[1],label:'画面质量 CRF',depends_on:[]}],defaults:{[crf]:22}}))
  assert.equal(page.values[encoder],'software');assert.equal(controls[crf].$props.disabled,false)
  assert.equal(controls[encoder],undefined);assert.equal(controls[cq],undefined)
  assert.doesNotMatch(html,/NVIDIA 硬件编码|CQ 与软件 CRF/)
})

test('设备不支持或显存预检失败保留真实错误和硬件选择，不发送run也不自动改软件',async t=>{
  for(const message of ['当前设备不支持 NVIDIA 硬件编码，请检查驱动和 FFmpeg 支持。','可用显存不足，请释放闲置资源后重新预检。']){
    const document=data({versions:[version({overrides:{[encoder]:'nvenc',[cq]:18}})]})
    const {page}=await render(t,document);page.selected=['version'];const requests:string[]=[]
    t.mock.method(globalThis,'fetch',async(url:any)=>{
      if(String(url).endsWith('/comments')) return new Response(JSON.stringify({overlay:{items:[]},overlay_revision:'isolated'}))
      requests.push(String(url));assert.match(String(url),/assembly-versions\/plan$/)
      return new Response(JSON.stringify({code:'ASSEMBLY_ENCODER_UNAVAILABLE',message}),{status:400})
    })
    await page.generate()
    assert.equal(page.error,message);assert.equal(page.values[encoder],'nvenc');assert.equal(page.values[cq],18)
    assert.equal(requests.length,1);assert.equal(page.busy,false)
    const {html}=await render(t,document,page=>page.error=message)
    assert.ok(html.includes(message));assert.doesNotMatch(html,/硬件加速成功|已自动改用软件/)
  }
})

type Host={type:string;text:string;props:Record<string,any>;children:Host[];parent:Host|null}
function mountFields(page:any,render:any,badgeRender?:any){
  const host=(type:string,text=''):Host=>({type,text,props:{},children:[],parent:null})
  const remove=(node:Host)=>{if(node.parent)node.parent.children.splice(node.parent.children.indexOf(node),1);node.parent=null}
  const renderer=Vue.createRenderer<Host,Host>({createElement:host,createText:text=>host('#text',text),createComment:()=>host('#comment'),remove,
    insert(node,parent,anchor=null){remove(node);const index=anchor?parent.children.indexOf(anchor):-1;parent.children.splice(index<0?parent.children.length:index,0,node);node.parent=parent},
    setText(node,text){node.text=text},setElementText(node,text){node.children=[];node.text=text},patchProp(node,key,_old,value){node.props[key]=value},
    parentNode:node=>node.parent,nextSibling:node=>node.parent?.children[node.parent.children.indexOf(node)+1]||null})
  const Span=Vue.defineComponent({setup:(_props,{slots})=>()=>Vue.h('span',slots.default?.())})
  const Badge=badgeRender?Vue.defineComponent({props:['source','inherited'],setup:props=>({presentation:Vue.computed(()=>sourcePresentation(props.source,props.inherited))}),render:badgeRender}):Span
  const Control=Vue.defineComponent({props:['field','modelValue','disabled','values'],emits:['update:modelValue'],setup:(props,{emit})=>()=>Vue.h('input',{
    'data-path':props.field.path,value:props.modelValue,disabled:props.disabled,onChange:(value:any)=>emit('update:modelValue',value)})})
  const app=renderer.createApp({components:{ParameterHelp:Span,ConfigurationSourceBadge:Badge,ParameterResetButton:Span,ParamControl:Control},
    setup:()=>({group:'输出',styleGroups:page.styleGroups,fieldDisabled:page.fieldDisabled,fieldLocked:page.fieldLocked,
      versionFieldHelp:page.versionFieldHelp,override:page.override,inherit:page.inherit,fieldSource:page.fieldSource,
      get values(){return page.values},get active(){return page.active}}),render})
  const root=host('root');app.mount(root)
  function input(path:string){let found:any;const visit=(node:Host)=>{if(node.props['data-path']===path)found=node;node.children.forEach(visit)};visit(root);return found}
  function sources(){const values:Host[]=[];const visit=(node:Host)=>{if(node.props['data-source-kind'])values.push(node);node.children.forEach(visit)};visit(root);return values}
  return {input,sources,unmount:()=>app.unmount()}
}

test('真实输出字段模板挂载：CRF22切硬件仍CQ18，CQ调整后切回软件仍CRF22，不互拷贝或清空',async t=>{
  const source=await readFile(new URL('../src/components/AssemblyVersionWorkspace.vue',import.meta.url),'utf8')
  const template=source.match(/<section v-for="styleGroup in styleGroups\(group\)"[\s\S]*?<\/section>/)?.[0]
  assert.ok(template,'必须挂载实际版本字段模板')
  const renderFields=new Function('Vue',compile(template,{mode:'function',prefixIdentifiers:true}).code)(Vue)
  const {page}=await render(t),mounted=mountFields(page,renderFields)
  try{
    assert.equal(mounted.input(crf).props.value,22);assert.equal(mounted.input(cq).props.value,18)
    assert.equal(mounted.input(crf).props.disabled,false);assert.equal(mounted.input(cq).props.disabled,true)
    mounted.input(encoder).props.onChange('nvenc');await Vue.nextTick()
    assert.equal(mounted.input(crf).props.disabled,true);assert.equal(mounted.input(cq).props.disabled,false)
    assert.equal(mounted.input(crf).props.value,22);assert.equal(mounted.input(cq).props.value,18)
    mounted.input(cq).props.onChange(20);mounted.input(encoder).props.onChange('software');await Vue.nextTick()
    assert.equal(mounted.input(crf).props.value,22);assert.equal(mounted.input(cq).props.value,20)
    assert.equal(mounted.input(crf).props.disabled,false);assert.equal(mounted.input(cq).props.disabled,true)
    assert.deepEqual(page.active.overrides,{[encoder]:'software',[cq]:20})
  }finally{mounted.unmount()}
})

test('真实字段与来源badge模板挂载联动：partial编码三项显示已冻结基准，锁定与恢复均不借新默认',async t=>{
  const source=await readFile(new URL('../src/components/AssemblyVersionWorkspace.vue',import.meta.url),'utf8')
  const fieldsTemplate=source.match(/<section v-for="styleGroup in styleGroups\(group\)"[\s\S]*?<\/section>/)?.[0]
  const badge=await readFile(new URL('../src/components/ConfigurationSourceBadge.vue',import.meta.url),'utf8')
  const badgeTemplate=badge.match(/<template>([\s\S]*?)<\/template>/)?.[1]
  assert.ok(fieldsTemplate);assert.ok(badgeTemplate)
  const renderer=(template:string)=>new Function('Vue',compile(template,{mode:'function',prefixIdentifiers:true}).code)(Vue)
  const saved={[encoder]:'software',[crf]:18,[cq]:19}
  const {page}=await render(t,data({defaults:{[encoder]:'nvenc',[crf]:24,[cq]:25},versions:[version({partial:{plan_id:'a'.repeat(32)},
    partial_summary:{base_name:'此前原声成片'},overrides:{...saved,'assemble.subtitle_size':42},values:saved})]}))
  const mounted=mountFields(page,renderer(fieldsTemplate),renderer(badgeTemplate))
  try{
    for(const path of VIDEO_ENCODING_PATHS)assert.equal(mounted.input(path).props.disabled,true)
    assert.equal(mounted.sources().length,3)
    for(const badge of mounted.sources()){
      assert.equal(badge.props['data-source-kind'],'baseline')
      assert.match(badge.props.title,/基准成片「此前原声成片」.*不随当前项目或全局默认变化/)
      assert.doesNotMatch(badge.props.title,/项目基础值来自|稿件/)
    }
    mounted.input(encoder).props.onChange('nvenc');page.restoreVersionDefaults();await Vue.nextTick()
    assert.equal(mounted.input(encoder).props.value,'software');assert.equal(mounted.input(crf).props.value,18)
    assert.deepEqual(page.active.overrides,saved)
    assert.equal(mounted.sources().every(badge=>badge.props['data-source-kind']==='baseline'),true)
  }finally{mounted.unmount()}
})

test('全局/项目配置及项目步骤入口共用透明帮助与依赖，不把硬件CQ误称CRF同画质',async t=>{
  for(const component of [Configuration,ProjectForm]){
    const helps:Record<string,any>={},controls:Record<string,any>={};let page:any
    const isProject=component===ProjectForm
    const app=Vue.createSSRApp({render:()=>Vue.h(NMessageProvider,null,{default:()=>Vue.h(component,isProject?{uid:'isolated-encoding',stepKey:'assemble'}:{scope:'global',sections:['assemble']})})});setup(app)
    app.mixin({created(){
      if(this.$options.__name===component.__name){
        page=this.$.setupState
        if(isProject){page.fields=fields;page.defaults=data().defaults;page.draft={[encoder]:'nvenc',[crf]:22,[cq]:18};page.saved=JSON.stringify(page.draft);page.loading=false}
        else{page.adopt({name:'隔离编码',revision:1,fingerprint:'fixture',defaults:data().defaults,overrides:{[encoder]:'nvenc'},sections:{assemble:{label:'合成成片',fields}}});page.loading=false}
      }
      if(this.$options.__name==='ParameterHelp')helps[this.$props.label]=this
      if(this.$options.__name==='ParamControl')controls[this.$props.field.path]=this
    }})
    await renderToString(app)
    assert.equal(controls[crf].$props.disabled,true);assert.equal(controls[cq].$props.disabled,false)
    assert.match(helps['硬件画质 CQ'].$props.help,/不是同一质量刻度/)
    assert.match(helps['软件画质 CRF'].$props.help,/暂不生效.*保留/)
  }
})
