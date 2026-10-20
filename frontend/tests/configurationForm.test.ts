import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'
let server:ViteDevServer, Form:any, Batch:any, Workspace:any
before(async()=>{server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}});Form=(await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default;Batch=(await server.ssrLoadModule('/src/components/BatchWorkflow.vue')).default;Workspace=(await server.ssrLoadModule('/src/components/ConfigurationWorkspace.vue')).default})
after(()=>server.close())
const defaults={'assemble.voice_volume':1,'assemble.loudnorm':false,'publish.season_id':null,'comments.color':'#FFFFFF'}
function data(){return {key:'project:p',name:'本项目',revision:4,fingerprint:'f4',overrides:{},defaults,values:defaults,parent_sources:Object.fromEntries(Object.keys(defaults).map(k=>[k,{kind:'series',name:'旅行系列'}])),sections:{assemble:{label:'合成',fields:[{path:'assemble.voice_volume',label:'人声音量',type:'float',min:0,max:1},{path:'assemble.loudnorm',label:'归一化',type:'bool'}]}},version_fields:[]}}
async function render(t:any,component:any,props:any,seed?:(page:any)=>void){let page:any;const writes:any[]=[]
 const resets:any[]=[]
 t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{if(options?.body)writes.push([url,JSON.parse(options.body)]);return {ok:true,json:async()=>String(url).includes('/catalog')?{accounts:[]}:data()}})
 const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(component,props)})})});setup(app)
 app.mixin({created(){if(this.$options.__name===component.__name){page=this.$.setupState;if(component===Form)page.adopt({...data(),values:{...defaults,'assembly.versions':[]},defaults:{...defaults,'assembly.versions':[]}});seed?.(page)}if(this.$options.__name==='ParameterResetButton')resets.push(this)}})
 const html=await renderToString(app);await new Promise(r=>setImmediate(r));page.busy=false
 return {page,html,writes,resets}
}

test('普通参数始终提供统一恢复入口，继承项禁用且恢复只删除自己的覆盖',async t=>{
 const seed=(page:any)=>{page.adopt({...data(),defaults:{...defaults,'assembly.versions':[]},overrides:{'assemble.voice_volume':0},
  parent_sources:{'assemble.voice_volume':{kind:'global',frozen:true},'assemble.loudnorm':{kind:'global',frozen:true}}});page.loading=false}
 const {page,resets,writes}=await render(t,Form,{scope:'project:p',sections:['assemble']},seed)
 const volume=resets.find(button=>button.$props.label==='人声音量'),normalization=resets.find(button=>button.$props.label==='归一化')
 assert.ok(volume);assert.ok(normalization)
 assert.equal(volume.$props.disabled,false);assert.equal(normalization.$props.disabled,true)
 assert.equal(volume.$props.action,'恢复项目基础值')
 seed(page);page.change('assemble.loudnorm',true)
 volume.$emit('reset')
 assert.equal(Object.hasOwn(page.patch,'assemble.voice_volume'),false)
 assert.equal(page.values['assemble.voice_volume'],1)
 assert.equal(page.patch['assemble.loudnorm'],true)
 assert.equal(writes.length,0,'恢复只修改草稿，不自动保存')
})

test('内嵌版本各参数保留独立恢复入口，只恢复指定参数并同时遵守字段与清单锁',async t=>{
 const versions=[{id:'v1',name:'配音版',dubbing:true,burn_subtitles:false,include_comments:false,overrides:{'assemble.voice_volume':0}}]
 const seed=(page:any)=>{page.adopt({...data(),defaults:{...defaults,'assembly.versions':versions},version_fields:[
  {path:'assemble.voice_volume',label:'人声音量',group:'声音',type:'float'},
  {path:'assemble.loudnorm',label:'归一化',group:'声音',type:'bool'}]});page.loading=false}
 const {page,resets,writes}=await render(t,Form,{scope:'project:p',sections:['assemble']},seed)
 const versionReset=resets.find(button=>button.$props.label==='人声音量'&&button.$props.action==='恢复项目默认')
 const inheritedReset=resets.find(button=>button.$props.label==='归一化'&&button.$props.action==='恢复项目默认')
 assert.ok(versionReset);assert.ok(inheritedReset)
 assert.equal(versionReset.$props.disabled,false);assert.equal(inheritedReset.$props.disabled,true)
 seed(page)
 t.mock.method(page.editability,'isPathLocked',(path:string)=>path==='assemble.voice_volume')
 versionReset.$emit('reset')
 assert.equal(page.values['assembly.versions'][0].overrides['assemble.voice_volume'],0)
 t.mock.method(page.editability,'isPathLocked',(path:string)=>path==='assembly.versions')
 versionReset.$emit('reset')
 assert.equal(page.values['assembly.versions'][0].overrides['assemble.voice_volume'],0)
 t.mock.method(page.editability,'isPathLocked',()=>false)
 page.editVersionOverride(0,'assemble.loudnorm',true)
 versionReset.$emit('reset')
 assert.deepEqual({...page.values['assembly.versions'][0].overrides},{'assemble.loudnorm':true})
 assert.equal(page.values['assembly.versions'][0].id,'v1');assert.equal(page.values['assembly.versions'][0].name,'配音版')
 assert.equal(versions[0].overrides['assemble.voice_volume'],0,'原始清单不被修改')
 assert.equal(writes.length,0)
})

test('内嵌版本定义逐项撤销到已保存清单，保留其他编辑与版本专用参数，新建版本无撤销基准',async t=>{
 const versions=[{id:'v1',name:'已保存配音版',dubbing:true,burn_subtitles:true,include_comments:false,overrides:{'assemble.voice_volume':.8}}]
 const seed=(page:any)=>{page.adopt({...data(),defaults:{...defaults,'assembly.versions':versions}});page.loading=false}
 const {page,resets,writes}=await render(t,Form,{scope:'global',sections:['assemble']},page=>{
  seed(page);page.editVersion(0,'name','新名字');page.editVersion(0,'dubbing',false);page.editVersion(0,'include_comments',true)
 })
 const nameReset=resets.find(button=>button.$props.label==='版本名称'),voiceReset=resets.find(button=>button.$props.label==='配音')
 const subtitlesReset=resets.find(button=>button.$props.label==='烧录字幕')
 assert.ok(nameReset);assert.ok(voiceReset);assert.ok(subtitlesReset)
 assert.equal(nameReset.$props.disabled,false);assert.equal(voiceReset.$props.disabled,false);assert.equal(subtitlesReset.$props.disabled,true)
 seed(page);page.editVersion(0,'name','新名字');page.editVersion(0,'dubbing',false);page.editVersion(0,'include_comments',true)
 nameReset.$emit('reset')
 assert.equal(page.values['assembly.versions'][0].name,'已保存配音版')
 assert.equal(page.values['assembly.versions'][0].dubbing,false)
 assert.equal(page.values['assembly.versions'][0].include_comments,true)
 assert.equal(page.values['assembly.versions'][0].overrides['assemble.voice_volume'],.8)
 page.busy=true;voiceReset.$emit('reset');assert.equal(page.values['assembly.versions'][0].dubbing,false)
 page.busy=false;voiceReset.$emit('reset');assert.equal(page.values['assembly.versions'][0].dubbing,true)
 page.addVersion();const index=page.values['assembly.versions'].length-1
 assert.equal(page.versionDefinitionChanged(index,'name'),false)
 page.restoreVersionDefinition(index,'name');assert.equal(page.values['assembly.versions'][index].name,'新版本')
 assert.equal(writes.length,0)
})
test('字段显示继承来源；同值、false、0和空值仍可明确覆盖，读取不写入',async t=>{const {page,html,writes}=await render(t,Form,{scope:'project:p',sections:['assemble']});assert.match(html,/旅行系列/);assert.equal(writes.length,0)
 for(const [k,v] of Object.entries({'assemble.voice_volume':0,'assemble.loudnorm':false,'publish.season_id':null}))page.change(k,v)
 assert.equal(page.dirty,true);assert.equal(page.fieldSource('assemble.loudnorm').kind,'project');await page.save()
 assert.deepEqual(writes[0][1].updates,{'assemble.voice_volume':0,'assemble.loudnorm':false,'publish.season_id':null});assert.equal(writes[0][1].fingerprint,'f4')
})
test('全局与模板明确仅供新项目，已有项目保存基础值；项目恢复操作不读取最新来源', async t => {
 const global = await render(t, Form, {scope:'global', sections:['assemble']})
 assert.match(global.html, /已有项目的基础值与单独设置保持不变/)
 assert.match(global.html, /主动应用最新设置/)
 assert.doesNotMatch(global.html, /已有项目也会跟随|未覆盖的系列与项目跟随/)
 const project = await render(t, Form, {scope:'project:p', sections:['assemble']}, page => {
  page.adopt({...data(),overrides:{'assemble.voice_volume':0},parent_sources:{'assemble.voice_volume':{kind:'series',name:'旅行系列',frozen:true}}})
 })
 assert.equal(project.page.resetLabel, '恢复项目基础值')
 assert.match(project.html, /创建或上次应用时的基础值/)
 assert.match(project.html, /aria-label="人声音量恢复项目基础值"/)
 project.page.inherit('assemble.voice_volume')
 assert.equal(project.page.values['assemble.voice_volume'], 1)
 assert.deepEqual({...project.page.patch}, {})
})

test('版本结构注明整单来源，基础版本专用参数沿来源展示且编辑后标为版本值',async t=>{
 const seed=(page:any)=>page.adopt({...data(),defaults:{...defaults,'assembly.versions':[{id:'v1',name:'原声版',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{'assemble.voice_volume':.8}}]},
  parent_sources:{'assembly.versions':{kind:'global',frozen:true},'assemble.voice_volume':{kind:'global',frozen:true}},
  version_fields:[{path:'assemble.voice_volume',label:'人声音量',group:'声音',type:'float'}]})
 const {page,html}=await render(t,Form,{scope:'project:p',sections:['assemble']},seed);seed(page)
 assert.match(html,/声音、字幕与评论开关来自整份版本清单/);assert.match(html,/已存全局值/)
 assert.equal(page.versionFieldSource(page.values['assembly.versions'][0],'assemble.voice_volume').kind,'global')
 page.editVersionOverride(0,'assemble.voice_volume',.6)
 assert.equal(page.versionFieldSource(page.values['assembly.versions'][0],'assemble.voice_volume').kind,'version')
 assert.equal(page.values['assembly.versions'][0].overrides['assemble.voice_volume'],.6)
})
test('项目制作参数恢复固定基础，服务字段恢复共享当前值，未冻结来源不误标为快照', async t => {
 const seed = (page:any) => {
  const values = {'tts.backend':'indextts25','tts.indextts25.url':'http://127.0.0.1:7860','tts.indextts25.num_beams':3}
  page.adopt({...data(),defaults:values,overrides:{'tts.indextts25.url':'http://127.0.0.1:7861','tts.indextts25.num_beams':2},
   parent_sources:{'tts.indextts25.url':{kind:'global'},'tts.indextts25.num_beams':{kind:'global',frozen:true}},
   sections:{tts:{label:'配音',fields:[{path:'tts.indextts25.url',label:'服务地址',type:'str'},{path:'tts.indextts25.num_beams',label:'束搜索数量',type:'int'}]}}})
 }
 const project = await render(t, Form, {scope:'project:p',sections:['tts']}, seed)
 seed(project.page)
 assert.equal(project.page.fieldResetLabel('tts.indextts25.url'),'恢复服务设置')
 assert.equal(project.page.fieldResetLabel('tts.indextts25.num_beams'),'恢复项目基础值')
 assert.match(project.html,/aria-label="服务地址恢复服务设置"/)
 assert.match(project.html,/aria-label="束搜索数量恢复项目基础值"/)
 assert.match(project.html,/连接、代理、等待超时等共享服务设置用于之后的调用/)
 assert.match(project.page.fieldResetHint('tts.indextts25.url'),/共享服务的当前设置/)
})
test('恢复继承删除覆盖键，不用上级当前值覆盖；保存失败保留草稿',async t=>{const {page,writes}=await render(t,Form,{scope:'project:p',sections:['assemble']});page.adopt({...data(),overrides:{'assemble.voice_volume':0}});page.inherit('assemble.voice_volume');assert.equal(page.values['assemble.voice_volume'],1);await page.save();assert.deepEqual(writes[0][1].reset,['assemble.voice_volume']);assert.deepEqual(writes[0][1].updates,{})
 page.change('assemble.voice_volume',.3);t.mock.method(globalThis,'fetch',async()=>({ok:false,json:async()=>({message:'继承设置已变化'})}));await page.save();assert.equal(page.patch['assemble.voice_volume'],.3);assert.equal(page.dirty,true);assert.equal(page.error,'继承设置已变化')
})
test('读取批量计划不运行；明确确认只提交检查通过项目，重复点击只发一次',async t=>{const {page}=await render(t,Batch,{uids:['a','b']});let resolve:any;const calls:any[]=[]
 t.mock.method(globalThis,'fetch',async(url:any,options:any)=>{calls.push([url,JSON.parse(options.body)]);if(String(url).endsWith('batch-plan'))return {ok:true,json:async()=>({items:[{uid:'a',blockers:[],plan:{recipe:{mode:'produce'},fingerprint:'a'}},{uid:'b',blockers:['缺少音色'],plan:null}]})};return new Promise(done=>resolve=()=>done({ok:true,json:async()=>({items:[]})}))})
 await page.open();assert.equal(calls.length,1);const running=page.start();await page.start();assert.equal(calls.length,2);assert.deepEqual(calls[1][1].items.map((i:any)=>i.uid),['a']);assert.equal(calls[1][1].confirmed,true);resolve();await running
})

function ttsData() {
 const defaults = { 'tts.backend': 'indextts25', 'tts.indextts25.timbre_audio': 'voice.wav',
  'tts.indextts25.emo_mode': 'text', 'tts.indextts25.emo_weight': 1, 'tts.indextts25.num_beams': 3 }
 return { ...data(), defaults, values: defaults, sections: { tts: { label: '配音', fields: Object.keys(defaults).map(path => ({ path, label: path, type: 'text' })) } } }
}

test('音色预设受当前字段范围限制；保存中及归档模板不应用，错误后保留草稿', async t => {
 const { page } = await render(t, Form, { scope: 'series:travel', sections: ['tts'], paths: ['tts.indextts25.num_beams'] })
 page.adopt(ttsData())
 page.applyPreset({ 'tts.indextts25.num_beams': 2, 'tts.indextts25.timbre_audio': 'new.wav', 'server.data_root': 'unexpected' })
 assert.deepEqual({ ...page.patch }, { 'tts.indextts25.num_beams': 2 })
 page.busy = true
 page.applyPreset({ 'tts.indextts25.num_beams': 5 })
 assert.equal(page.patch['tts.indextts25.num_beams'], 2)
 page.busy = false
 t.mock.method(globalThis, 'fetch', async () => ({ ok: false, json: async () => ({ message: '修订冲突' }) }))
 await page.save()
 assert.equal(page.dirty, true)
 assert.equal(page.patch['tts.indextts25.num_beams'], 2)
 page.adopt({ ...ttsData(), archived: true })
 page.applyPreset({ 'tts.indextts25.num_beams': 5 })
 assert.deepEqual({ ...page.patch }, {})
})

test('执行权限即时锁住字段、继承、预设和保存，已编辑草稿保留且刷新不写入',async t=>{
 const {page}=await render(t,Form,{scope:'project:p',sections:['assemble']})
 page.change('assemble.voice_volume',.6)
 const writes:any[]=[]
 t.mock.method(globalThis,'fetch',async(url:any,init:any)=>{
  if(init?.body)writes.push(init.body)
  return new Response(JSON.stringify({locked:true,reason:'正在合成成片'}))
 })
 page.editability.observing.value=true
 await page.editability.refresh()
 assert.equal(page.locked,true)
 page.change('assemble.voice_volume',.2);page.inherit('assemble.voice_volume');page.applyPreset({'assemble.voice_volume':.9})
 await page.save()
 assert.equal(page.patch['assemble.voice_volume'],.6);assert.equal(page.dirty,true);assert.equal(writes.length,0)
})

test('共享配色只填入文字与描边两色，保留粗细且不自动写入', async t => {
 const { page, writes } = await render(t, Form, { scope: 'global', sections: ['comments'] })
 const colors = { 'comments.color': '#FFFFFF', 'comments.outline_color': '#000000', 'comments.outline': 3 }
 page.adopt({ ...data(), defaults: colors, sections: { comments: { label: '评论', fields: Object.keys(colors).map(path => ({ path })) } } })
 page.preset('mint', 'comments.')
 assert.equal(page.values['comments.color'], '#8FF0C5')
 assert.equal(page.values['comments.outline'], 3)
 assert.equal(Object.keys(page.patch).length, 2)
 assert.equal(writes.length, 0)
 page.presetBusy = true
 page.preset('sunshine', 'comments.')
 assert.equal(page.values['comments.color'], '#8FF0C5')
})

test('统一工作区导航由页面保护；保存中不切换范围，新建不会绕过未保存草稿', async t => {
 const selections:string[] = []
 const { page, html, writes } = await render(t, Workspace, { scope: 'global', onSelect: (key:string) => selections.push(key) })
 assert.match(html, /配置范围/)
 assert.match(html, /全局默认/)
 assert.match(html, /系列模板/)
 assert.doesNotMatch(html, /步骤默认参数/)
 page.choose('series:travel')
 assert.deepEqual(selections, ['series:travel'])
 page.editorBusy = true
 page.choose('series:other')
 assert.equal(selections.length, 1)
 page.editorBusy = false
 page.dirty = true
 page.name = '草稿未保存'
 await page.create()
 assert.equal(writes.length, 0)
})

test('当前运行模板归档按钮与handler同时锁定，其他模板隔离且导航和新建空模板仍可用',async t=>{
 const shared=await server.ssrLoadModule('/src/utils/configurationEditability.ts')
 t.mock.method(globalThis,'fetch',async()=>new Response(JSON.stringify({locked:true,reason:'绑定项目正在运行，结束后可修改模板'})))
 await shared.checkConfigurationEditability('series:running')
 const current={key:'series:running',name:'正在使用的模板',revision:1,project_count:1,archived:false}
 const other={key:'series:other',name:'独立模板',revision:1,project_count:0,archived:false}
 const selections:string[]=[]
 const {page,html}=await render(t,Workspace,{scope:current.key,onSelect:(key:string)=>selections.push(key)},page=>{
  page.rows=[current,other];page.editability.observing.value=true
 })
 const archiveButtons=[...html.matchAll(/<button\b([^>]*)>([\s\S]*?)<\/button>/g)].filter(match=>match[2].includes('归档'))
 assert.equal(archiveButtons.length,2)
 assert.match(archiveButtons[0][1],/\sdisabled(?:=|\s|$)/);assert.match(archiveButtons[0][1],/title="绑定项目正在运行/)
 assert.doesNotMatch(archiveButtons[1][1],/\sdisabled(?:=|\s|$)/)
 let prompts=0;t.mock.method(page.dialog,'warning',()=>{prompts++;return {} as any})
 page.archive(current);assert.equal(prompts,0)
 page.archive(other);assert.equal(prompts,1)
 page.choose('global');assert.deepEqual(selections,['global'])
 const writes:any[]=[]
 t.mock.method(globalThis,'fetch',async(url:any,init:any)=>{
  if(init?.body){writes.push([url,JSON.parse(init.body)]);return new Response(JSON.stringify({key:'series:new'}))}
  return new Response('[]')
 })
 page.name='新空模板';await page.create()
 assert.equal(writes.length,1);assert.equal(writes[0][0],'/api/configuration/templates')
 assert.deepEqual(writes[0][1],{name:'新空模板'})
})

test('新建模板防重复，回执先解除导航锁再切换，列表刷新失败不重复创建', async t => {
 let pageRef:any
 const selections:any[] = []
 const { page } = await render(t, Workspace, { scope: 'global', onSelect: (key:string) => selections.push([key, pageRef.locked]) })
 pageRef = page
 page.name = '旅行系列'
 let finish:any, count = 0
 t.mock.method(globalThis, 'fetch', async (_url:any, options:any) => {
  if (options.body) { count++; return new Promise(resolve => { finish = () => resolve({ ok: true, json: async () => ({ key: 'series:created' }) }) }) }
  return { ok: false, json: async () => ({ message: '列表暂不可用' }) }
 })
 const pending = page.create()
 await page.create()
 assert.equal(count, 1)
 assert.equal(page.locked, true)
 finish(); await pending
 assert.deepEqual(selections, [['series:created', false]])
 assert.equal(page.error, '列表暂不可用')
 assert.equal(page.name, '')
})

test('慢读取只锁编辑字段，不发送保存中状态，也不产生未保存提示', async t => {
 const busyEvents:boolean[] = [], dirtyEvents:boolean[] = []
 t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
 const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Form, {
  scope: 'global', onBusyChange: (value:boolean) => busyEvents.push(value), onDirtyChange: (value:boolean) => dirtyEvents.push(value),
 }) }) })
 setup(app)
 const html = await renderToString(app)
 assert.match(html, /读取设置/)
 assert.doesNotMatch(html, /未保存/)
 assert.equal(busyEvents.includes(true), false)
 assert.equal(dirtyEvents.includes(true), false)
})

test('语义模式只显示目标词数，本地模式只显示字符长度；切换保留草稿且不隐藏其它依赖字段', async t => {
 const writes:any[]=[]
 t.mock.method(globalThis, 'fetch', (_url:any,options:any) => {if(options?.body)writes.push(options.body);return new Promise(() => {})})
 const fields = [
  { path:'split.use_llm', label:'启用 LLM 语义整理', type:'bool' },
  { path:'split.target_word_count', label:'语义目标词数（软参考）', type:'int', depends_on:[['split.use_llm',true]] },
  { path:'split.max_split_length', label:'本地单句长度（字符）', type:'int', depends_on:[['split.use_llm',false]] },
  { path:'split.llm_profile', label:'语义整理模型', type:'str', depends_on:[['split.use_llm',true]] },
 ]
 for (const mode of [true, false, null, undefined]) {
  let page:any
  const values:any = {'split.target_word_count':10,'split.max_split_length':20,'split.llm_profile':'fixture','assembly.versions':[]}
  if (mode !== undefined) values['split.use_llm'] = mode
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(Form,{scope:'global',sections:['split']})})})
  setup(app)
  app.mixin({created(){if(this.$options.__name==='ConfigurationForm'){
   page=this.$.setupState
   page.adopt({...data(),defaults:values,values,overrides:{'split.max_split_length':20},sections:{split:{label:'语义整理',fields}}})
   page.loading=false
  }}})
  const html=await renderToString(app)
  const useLlm=mode!==false, active=fields[useLlm?1:2], inactive=fields[useLlm?2:1]
  assert.ok(html.includes(active.label), `当前模式应显示${active.label}`)
  assert.ok(!html.includes(inactive.label), `当前模式不能显示${inactive.label}`)
  assert.match(html,/语义整理模型/, '其它depends_on字段保留原有可见性')
  assert.equal(page.fieldDisabled(active),false, '空/缺省开关也按LLM默认模式启用目标控件')
  assert.equal(page.fieldDisabled(inactive),true)
  assert.equal(page.values['split.target_word_count'],10)
  page.change('split.use_llm',true)
  page.change('split.target_word_count',14)
  page.change('split.use_llm',false)
  assert.deepEqual(page.visibleFields({fields}).map((field:any)=>field.path),['split.use_llm','split.max_split_length','split.llm_profile'])
  assert.equal(page.values['split.max_split_length'],20)
  page.change('split.use_llm',true)
  assert.deepEqual(page.visibleFields({fields}).map((field:any)=>field.path),['split.use_llm','split.target_word_count','split.llm_profile'])
  assert.equal(page.values['split.target_word_count'],14)
  assert.equal(page.patch['split.max_split_length'],20, '隐藏已有字符覆盖时不丢弃设置')
 }
 assert.equal(writes.length,0)
})
