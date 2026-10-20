import assert from 'node:assert/strict'
import test,{before,after} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {NMessageProvider} from 'naive-ui'

let server:ViteDevServer,ConfigurationForm:any,ProjectForm:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  ConfigurationForm=(await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
  ProjectForm=(await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
})
after(()=>server.close())
const modePath='assemble.final_loudness_mode',targetPath='assemble.final_loudness_lufs',peakPath='assemble.final_true_peak_db'
const fields=[
  {path:'assemble.voice_volume',label:'人声音量',group:'声音',type:'float',help:'调整混入背景前的人声。'},
  {path:'assemble.bgm_volume',label:'背景音音量',group:'声音',type:'float',help:'调整背景比例。'},
  {path:modePath,label:'最终成片响度',group:'声音',type:'select',help:'统一输出总体响度。',options:[{label:'统一响度',value:'uniform'},{label:'保留动态',value:'normalize'},{label:'保持混音',value:'off'}]},
  {path:targetPath,label:'成片目标响度（LUFS）',group:'声音',type:'float',help:'建议 −16 LUFS。',depends_on:[[modePath,['normalize','uniform']]]},
  {path:peakPath,label:'成片真峰值上限（dBTP）',group:'声音',type:'float',min:-6,max:0,help:'建议 −1.5 dBTP。',depends_on:[[modePath,['normalize','uniform']]]},
  {path:'assemble.subtitle_font',label:'译文字体',group:'外观与输出',type:'str',help:'保持原来的字幕分组。'},
  {path:'assemble.crf',label:'画面质量 CRF',group:'外观与输出',type:'int',help:'保持原来的其他设置。'},
]
const defaults={ [modePath]:'off',[targetPath]:-16,[peakPath]:-1.5,'assemble.voice_volume':1,'assemble.bgm_volume':.6,'assemble.subtitle_font':'Arial','assemble.crf':18,'assembly.versions':[] }
type Scope='global'|'project-configuration'|'project-step'
async function render(t:any,scope:Scope,mode:string,sectionTitles?:Record<string,string>,extraPatch:Record<string,any>={},versionFields:any[]=[]){
  const writes:any[]=[],controls:Record<string,any>={},controlRows:any[]=[],widgets:Record<string,any>={},helps:Record<string,any>={}
  t.mock.method(globalThis,'fetch',(_url:any,options:any)=>{if(options?.body)writes.push(options.body);return new Promise(()=>{})})
  const component=scope==='project-step'?ProjectForm:ConfigurationForm
  const props=scope==='project-step'?{uid:'fixture-audio',stepKey:'assemble',sectionTitles}:{scope:scope==='global'?'global':'project:fixture-audio',sections:['assemble']}
  const patch={[modePath]:mode,[targetPath]:-18,[peakPath]:-2,...extraPatch}
  let page:any
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(component,props)})});setup(app)
  app.mixin({created(){
    if(this.$options.__name===component.__name){
      page=this.$.setupState
      const parent_sources=Object.fromEntries(fields.map(field=>[field.path,{kind:'global',frozen:true}]))
      if(scope==='project-step'){
        page.fields=fields.map(field=>({...field,group:sectionTitles?.assemble || field.group}));page.defaults={...defaults};page.draft={...defaults,...patch}
        page.saved=JSON.stringify(page.draft);page.overrides=Object.fromEntries(Object.keys(patch).map(path=>[path,true]));page.parentSources=parent_sources;page.sources=parent_sources;page.loading=false
      }else{
        page.adopt({name:'声音配置',revision:1,fingerprint:'fixture',defaults:{...defaults},overrides:{...patch},parent_sources,
          sections:{assemble:{label:'合成成片',fields}},version_fields:versionFields});page.loading=false
      }
    }
    if(this.$options.__name==='ParamControl'){controls[this.$props.field.path]=this;controlRows.push(this)}
    if(this.$options.__name==='ParameterHelp')helps[this.$props.label]=this
    if(this.$options.name==='InputNumber'){
      let owner=this.$parent
      while(owner && owner.$options.__name!=='ParamControl')owner=owner.$parent
      if(owner)widgets[owner.$props.field.path]=this
    }
  }})
  const html=await renderToString(app)
  const values=()=>scope==='project-step'?page.draft:page.values
  const groups=()=>scope==='project-step'?page.groups:page.styleGroups({fields},'assemble')
  const disabled=(field:any)=>scope==='project-step'?page.disabledField(field):page.fieldDisabled(field)
  return {page,html,controls,controlRows,widgets,helps,writes,values,groups,disabled}
}

test('全局、项目配置与步骤表单一致分组，仅声音字段进入平衡和统一响度',async t=>{
  for(const scope of ['global','project-configuration','project-step'] as Scope[]){
    const result=await render(t,scope,'uniform')
    const groups=result.groups()
    assert.equal(groups[0].name,'人声与背景平衡');assert.equal(groups[1].name,'统一输出响度')
    assert.deepEqual(groups[0].fields.map((field:any)=>field.path),['assemble.voice_volume','assemble.bgm_volume'])
    assert.deepEqual(groups[1].fields.map((field:any)=>field.path),[modePath,targetPath,peakPath])
    const paths=groups.flatMap((group:any)=>group.fields.map((field:any)=>field.path))
    assert.equal(paths.length,fields.length);assert.equal(new Set(paths).size,fields.length)
    assert.ok(groups.some((group:any)=>group.name==='译文样式' && group.fields.some((field:any)=>field.path==='assemble.subtitle_font')))
    assert.match(result.html,/人声与背景平衡/);assert.match(result.html,/统一输出响度/)
    assert.ok(result.helps['成片目标响度（LUFS）'])
    assert.equal(result.writes.length,0)
  }
})

test('两种响度处理方式实际启用目标与峰值控件，关闭仅禁用且不清除现有值或覆盖',async t=>{
  for(const scope of ['global','project-configuration','project-step'] as Scope[]){
    for(const mode of ['uniform','normalize','off']){
      const result=await render(t,scope,mode)
      for(const path of [targetPath,peakPath]){
        assert.ok(result.controls[path],`${scope}/${mode} 保持参数可见`)
        assert.equal(result.controls[path].$props.disabled,mode==='off')
        assert.equal(result.widgets[path].$props.disabled,mode==='off')
        assert.equal(result.disabled(fields.find(field=>field.path===path)),mode==='off')
      }
      assert.equal(result.values()[targetPath],-18);assert.equal(result.values()[peakPath],-2)
      assert.equal(scope==='project-step'?result.page.overrides[targetPath]:result.page.patch[targetPath],scope==='project-step'?true:-18)
      assert.equal(result.writes.length,0,'渲染不自动保存或运行')
    }
  }
})

test('项目步骤保留调用方显式栏目名称，不把自定义展示范围再次拆分',async t=>{
  const result=await render(t,'project-step','uniform',{assemble:'本次版本参数'})
  assert.ok(result.groups().some((group:any)=>group.name==='本次版本参数'))
  assert.ok(!result.groups().some((group:any)=>group.name==='统一输出响度'))
  assert.equal(result.controls[targetPath].$props.disabled,false)
})

test('全局、项目配置和步骤中的真峰值0可编辑，轻提示随生效模式及阈值统一显示',async t=>{
  for(const scope of ['global','project-configuration','project-step'] as Scope[]){
    for(const [mode,peak,notice] of [['uniform',0,true],['normalize',-.9,true],['uniform',-1,false],['off',0,false]] as const){
      const result=await render(t,scope,mode,undefined,{[peakPath]:peak})
      assert.equal(result.values()[peakPath],peak)
      assert.equal(result.widgets[peakPath].$props.max,0)
      assert.equal(Boolean(result.controls[peakPath].$.setupState.audioNotice),notice,`${scope}/${mode}/${peak}`)
      assert.equal(result.html.includes('role="note" aria-live="polite"'),notice)
      const help=result.helps['成片真峰值上限（dBTP）'].$props.help
      assert.ok(help.startsWith(fields.find(field=>field.path===peakPath)!.help))
      assert.match(help,/作用范围/)
      assert.doesNotMatch(help,/可调至 0 dBTP/,'schema已有说明时不再追加前端核心兜底')
      assert.equal(result.writes.length,0)
    }
  }
})

test('配置页内嵌版本按自己的有效模式显示提示和禁用，不误用共同设置',async t=>{
  for(const [commonMode,versionMode,notice] of [['off','uniform',true],['uniform','off',false]] as const){
    const version={id:'dub',name:'测试版本',dubbing:true,burn_subtitles:false,include_comments:false,overrides:{[modePath]:versionMode,[peakPath]:0}}
    const result=await render(t,'global',commonMode,undefined,{'assembly.versions':[version]},fields.filter(field=>field.group==='声音'))
    const control=result.controlRows.find(row=>row.$props.field.path===peakPath && row.$props.values[modePath]===versionMode)
    assert.ok(control)
    assert.equal(control.$props.disabled,versionMode==='off')
    assert.equal(Boolean(control.$.setupState.audioNotice),notice)
    assert.equal(result.values()['assembly.versions'][0].overrides[peakPath],0)
    assert.equal(result.writes.length,0)
  }
})
