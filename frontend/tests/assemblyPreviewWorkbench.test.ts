import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h,nextTick} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createPinia} from 'pinia'
import {NDialogProvider,NMessageProvider,useDialog} from 'naive-ui'
import {previewRange,previewSettingsMatch,previewTaskOwns,previewTime,stablePreviewValue} from '../src/utils/assemblyPreview.ts'
let server:ViteDevServer,Preview:any,useNav:any
const version={id:'dub',name:'配音版',dubbing:true,burn_subtitles:false,include_comments:false,overrides:{}}
const response=(value:any,ok=true)=>({ok,json:async()=>value})
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Preview=(await server.ssrLoadModule('/src/components/AssemblyVersionPreview.vue')).default
  useNav=(await server.ssrLoadModule('/src/stores/nav.ts')).useNavStore
  mock.method(globalThis,'fetch',async()=>response({items:[],report:null}))
})
after(async()=>{mock.restoreAll();await server.close()})
function item(name='preview_version_dub_1.mp4') {return {artifact_id:1,name,exists:true,size:1000,created_at:'2026-10-16T12:00:00Z',url:`/trial/${name}`,file_revision:'file-r1',metadata_status:'ready',range:{start:0,end:10},snapshot:{version_spec:version,effective_values:{},output_cuts:null,overlay_revision:'comments-r1'}}}
async function render(seed:(page:any,nav:any,props:any)=>void=()=>{}){
  let page:any,nav:any,props:any;const dialogs:any[]=[]
  const Probe={setup(){nav=useNav();nav.$patch({uid:'fixture',steps:[{key:'assemble',label:'合成成片',status:'done',job_id:1,artifacts:[]}]});nav.poll=async()=>{}
    useDialog().warning=((opts:any)=>{dialogs.push(opts);return {destroy(){}}}) as any
    return()=>h(Preview,{uid:'fixture',version:{...version,overrides:{}},overlayRevision:'comments-r1',effectiveValues:{}})}}
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Probe)})})})
  app.use(createPinia());setup(app)
  app.mixin({created(){if(this.$options.__name==='AssemblyVersionPreview'){page=this.$.setupState;props=this.$.props;page.context={duration:120,subtitles:[],comments:[]};seed(page,nav,props)}}})
  return {html:await renderToString(app),page,nav,props,dialogs}
}
test('范围与快照比较不受对象键序影响，也不把无完整信息的旧试片当作当前结果',()=>{
  assert.equal(stablePreviewValue({b:2,a:1}),stablePreviewValue({a:1,b:2}))
  assert.deepEqual(previewRange(115,10,120),{start:115,end:120})
  assert.deepEqual(previewRange(120,10,120),{start:119.8,end:120})
  assert.deepEqual(previewRange(119.9,.2,120),{start:119.8,end:120})
  assert.deepEqual(previewRange(10,10,.1),{start:0,end:.1})
  assert.equal(previewTime(331.25),'00:05:31.3')
  assert.equal(previewSettingsMatch(item(),version,{}, {start:0,end:10},null,'comments-r1'),true)
  assert.equal(previewSettingsMatch({...item(),metadata_status:'legacy'},version,{}, {start:0,end:10},null,'comments-r1'),false)
  assert.equal(previewSettingsMatch(item(),version,{}, {start:1,end:11},null,'comments-r1'),false)
  assert.equal(previewSettingsMatch(item(),version,{}, {start:0,end:10},null,'comments-r2'),true)
  const withComments={...version,include_comments:true},commentItem={...item(),snapshot:{...item().snapshot,version_spec:withComments}}
  assert.equal(previewSettingsMatch(commentItem,withComments,{}, {start:0,end:10},null,'comments-r1'),true)
  assert.equal(previewSettingsMatch(commentItem,withComments,{}, {start:0,end:10},null,'comments-r2'),false)
  assert.equal(previewSettingsMatch({...commentItem,snapshot:{...commentItem.snapshot,overlay_revision:null}},withComments,{}, {start:0,end:10},null,'comments-r1'),false)
  assert.equal(previewSettingsMatch(item(),version,{'assemble.voice_volume':2}, {start:0,end:10},null,'comments-r1'),false)
  assert.equal(previewSettingsMatch(item(),version,{}, {start:0,end:10},{ranges:[{start:1,end:3}]},'comments-r1'),false)
})
test('归属同时核对任务类型、版本和job，正式合成不能冒充本版本试片',()=>{
  const task={scope:'preview',job_id:7,version_ids:['dub']}
  assert.equal(previewTaskOwns(task,'dub',7),true)
  assert.equal(previewTaskOwns({...task,scope:'render'},'dub',7),false)
  assert.equal(previewTaskOwns(task,'source',7),false)
  assert.equal(previewTaskOwns(task,'dub',8),false)
})
test('试片按钮与可播结果就近，静帧为次级，未生成时无虚假完成状态',async()=>{
  const {html,page}=await render()
  for(const text of ['步骤 2','试片验证工作区','生成有声试片','还没有有声试片','本版本试片任务','aria-live="polite"'])assert.ok(html.includes(text),text)
  assert.doesNotMatch(html,/快速画面预览/,'纯声音版本不展示无用途的画面排版工具')
  assert.equal(page.frameOpen,false)
  assert.equal(page.actionDisabled,false)
  assert.equal(page.taskExecution,null)
})
test('开始或长度存在无效时码草稿时，禁止沿用旧范围生成试片',async()=>{
  const {page}=await render()
  assert.equal(page.actionDisabled,false)
  page.startInvalid=true;assert.equal(page.actionDisabled,true)
  page.startInvalid=false;page.lengthInvalid=true;assert.equal(page.actionDisabled,true)
  page.lengthInvalid=false;assert.equal(page.actionDisabled,false)
})
test('原片末尾保留至少0.2秒，真实不足最小时长时禁用生成并就近说明',async()=>{
  const {page}=await render(page=>{page.seconds=119.8})
  assert.deepEqual(page.range,{start:119.8,end:120})
  assert.equal(page.actionDisabled,false)
  const short=await render(page=>{page.context={duration:.1,subtitles:[],comments:[]}})
  assert.equal(short.page.actionDisabled,true)
  assert.match(short.html,/原片不足 00:00:00\.200，无法生成试片/)
})

test('画面预览与定位范围同区且默认收起，无密集字幕刻度',async()=>{
  const {html,page}=await render((page,_nav,props)=>{
    props.version.burn_subtitles=true
    page.context={duration:120,subtitles:Array.from({length:100},(_,i)=>({start:i,end:i+.8})),comments:[]}
    page.seconds=12
  })
  assert.match(html,/快速画面预览/)
  assert.match(html,/原片 00:00:12\.000/)
  assert.match(html,/共用上方开始位置，不随下方试片播放/)
  assert.match(html,/字幕 \/ 评论共 100处/)
  assert.ok(html.indexOf('range-panel')<html.indexOf('快速画面预览'))
  assert.ok(html.indexOf('快速画面预览')<html.indexOf('trial-action'))
  assert.doesNotMatch(html,/class="cue-marker/)
  assert.equal(page.frameOpen,false)
})
test('本版本进行中显示真实阶段并保留上次可播结果，外部任务只显示占用提示',async()=>{
  const {page,nav,html}=await render((page,nav)=>{
    nav.steps[0]={...nav.steps[0],status:'running',job_id:9,progress:34,execution:{schema:1,state:'running',job_id:9,label:'编码视频'}}
    page.history={items:[item()],active_task:{scope:'preview',job_id:9,version_ids:['dub']},latest_task:{scope:'preview',job_id:9,version_ids:['dub'],status:'running'}}
  })
  assert.equal(page.ownActive,true);assert.equal(page.actionDisabled,true)
  assert.equal(page.taskExecution.label,'编码视频');assert.equal(page.previewUrl,'/trial/preview_version_dub_1.mp4')
  assert.match(html,/停止试片|编码视频/)
  page.history={...page.history,active_task:{scope:'render',job_id:9,version_ids:['dub']},latest_task:null}
  await nextTick()
  assert.equal(page.ownActive,false);assert.equal(page.foreignActive,true)
  assert.equal(page.previewUrl,'/trial/preview_version_dub_1.mp4')
})
test('生成请求去重，按当前草稿提交，不覆盖保存值，运行期间改表单标明用于下次',async t=>{
  const {page,nav,props}=await render(page=>{page.history={items:[item()]}})
  let finish:any;const writes:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/preview'))return new Promise(resolve=>{writes.push({url,body:JSON.parse(String(options?.body))});finish=()=>resolve(response({status:'started',job_id:10}))})
    return response({items:[item()],latest_task:{scope:'preview',job_id:10,version_ids:['dub'],status:'running'}})
  })
  props.version.overrides['assemble.voice_volume']=1.2
  const pending=page.trial();await page.trial()
  assert.equal(writes.length,1);assert.equal(page.submitting,true)
  assert.equal(writes[0].body.version.overrides['assemble.voice_volume'],1.2)
  assert.deepEqual({start:writes[0].body.start,end:writes[0].body.end},{start:0,end:10})
  finish();await pending;await nextTick()
  assert.equal(nav.stepMap.assemble.job_id,10);assert.equal(page.ownActive,true)
  assert.equal(page.previewUrl,'/trial/preview_version_dub_1.mp4')
  props.version.overrides['assemble.voice_volume']=1.5;await nextTick()
  assert.equal(page.changedDuringRun,true)
})
test('失败保留旧播放器，停止仅发送本次job，清理不会选择最新或当前播放项',async t=>{
  const {page,nav,dialogs}=await render((page,nav)=>{
    page.history={items:[item('new.mp4'),{...item('old.mp4'),artifact_id:2},{...item('older.mp4'),artifact_id:3}],latest_task:{scope:'preview',job_id:9,version_ids:['dub'],status:'failed',error_message:'视频编码内存不足'}}
    nav.steps[0].status='failed';nav.steps[0].job_id=9
  })
  assert.equal(page.taskFailed,true);assert.equal(page.previewUrl,'/trial/new.mp4')
  page.selectHistory(page.existing[1]);page.historySelection=['new.mp4','old.mp4','older.mp4']
  assert.deepEqual(page.selectedForRecycle.map((row:any)=>row.name),['older.mp4'])
  page.recycle();assert.equal(dialogs.length,1);assert.match(dialogs[0].content,/清空回收站/)
  let stopped:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{if(String(url).endsWith('/cancel'))stopped=JSON.parse(String(options?.body));return response({status:'cancelling'})})
  nav.steps[0].status='running';page.history.active_task={scope:'preview',job_id:9,version_ids:['dub']};await nextTick()
  await page.stop();assert.deepEqual(stopped,{job_id:9})
  page.history.active_task={scope:'render',job_id:9,version_ids:['dub']};page.history.latest_task=null;page.cancelPending=false;stopped=null;await nextTick();await page.stop()
  assert.equal(stopped,null)
})
test('后台刷新不跳走手动选中的历史试片；新成功结果到达才自动切换',async t=>{
  const {page}=await render(page=>{page.history={items:[item('new.mp4'),item('old.mp4')]};page.selectedName='old.mp4'})
  t.mock.method(globalThis,'fetch',async()=>response({items:[item('new.mp4'),item('old.mp4')]}))
  await page.refreshHistory();assert.equal(page.selectedName,'old.mp4')
  t.mock.method(globalThis,'fetch',async()=>response({items:[item('newest.mp4'),item('new.mp4'),item('old.mp4')]}))
  await page.refreshHistory();assert.equal(page.selectedName,'newest.mp4')
})
test('启动响应丢失时先核对任务，核对失败仍阻止重复提交，确认结束后才可重试',async t=>{
  const {page,nav}=await render()
  let finishHistory:any,finishSteps:any,posts=0
  nav.refreshSteps=()=>new Promise(resolve=>{finishSteps=resolve})
  t.mock.method(globalThis,'fetch',async(url)=>{
    if(String(url).endsWith('/preview')){posts++;throw new DOMException('测试响应丢失','TimeoutError')}
    if(String(url).includes('preview-history'))return new Promise(resolve=>{finishHistory=()=>resolve(response({message:'暂时无法核对'},false))})
    return response({})
  })
  const pending=page.trial();for(let i=0;i<10;i++)await Promise.resolve()
  assert.equal(page.submissionUnknown,true);assert.equal(page.actionDisabled,true)
  await page.trial();assert.equal(posts,1)
  finishHistory();finishSteps();await pending
  assert.equal(page.submissionUnknown,true);assert.equal(page.actionDisabled,true)
  nav.refreshSteps=async()=>{nav.stepsError=''}
  t.mock.method(globalThis,'fetch',async()=>response({items:[],active_task:null,latest_task:null}))
  await page.verifySubmission()
  assert.equal(page.submissionUnknown,false);assert.equal(page.actionDisabled,false)
})
test('切到另一个版本后，旧启动失败不会写入新版本错误',async t=>{
  const {page,props}=await render()
  let finish:any
  t.mock.method(globalThis,'fetch',async(url)=>String(url).endsWith('/preview')?new Promise(resolve=>{finish=()=>resolve(response({message:'旧版本预检失败'},false))}):response({items:[]}))
  const pending=page.trial();props.version={...version,id:'other'};await nextTick();finish();await pending
  assert.equal(page.trialError,'')
})
test('系统盘和提交内存紧张时就近解释资源，不把D盘可用试片错误禁用',async()=>{
  const {page,html}=await render(page=>{page.history={items:[],resources:{disk_free_bytes:100*1024**3,system_disk_free_bytes:130*1024**2,commit_available_bytes:600*1024**2}}})
  assert.match(html,/系统盘仅余 130.0 MB/);assert.match(html,/系统可提交内存仅余 600.0 MB/)
  assert.match(html,/查看资源与存储/);assert.equal(page.actionDisabled,false)
})
test('回收提交显式文件身份与保护项，部分失败保留真实提示而不声称全部成功',async t=>{
  const {page,dialogs}=await render(page=>{page.history={items:[item('new.mp4'),{...item('old.mp4'),artifact_id:2}]};page.historySelection=['old.mp4']})
  let body:any
  t.mock.method(globalThis,'fetch',async(url,options)=>{
    if(String(url).endsWith('/recycle')){body=JSON.parse(String(options?.body));return response({status:'partial',recycled_count:0,failed_count:1,failures:[{name:'old.mp4',message:'文件正在使用'}],history:{items:[item('new.mp4'),item('old.mp4')]}})}
    return response({items:[item('new.mp4'),item('old.mp4')]})
  })
  page.recycle();await dialogs[0].onPositiveClick()
  assert.deepEqual(body,{confirmed:true,version_id:'dub',items:[{artifact_id:2,name:'old.mp4',file_revision:'file-r1'}],keep_name:'new.mp4'})
  assert.match(page.historyError,/另有 1 份未能回收.*文件正在使用/)
  assert.equal(page.recycling,false)
})
test('纯原声存在内容审查静音时仍需有声试片，无效规则就近说明并阻止无效启动',async()=>{
  const {page,html}=await render((page,_nav,props)=>{props.version={...version,dubbing:false};page.context={duration:120,subtitles:[],comments:[],audio_redaction_required:true}})
  assert.equal(!!page.trialApplicable,true);assert.equal(page.actionDisabled,false)
  assert.match(html,/生成有声试片/);assert.doesNotMatch(html,/不需要生成试片/)
  page.context={...page.context,audio_redaction_error:'静音范围超出视频长度'}
  assert.equal(page.actionDisabled,true)
})
test('修复建议在试片按钮附近显示一次，只取同job的实际建议并保留旧播放器',async()=>{
  const {page,nav,html}=await render((page,nav)=>{
    page.history={items:[item()],latest_task:{scope:'preview',job_id:9,version_ids:['dub'],status:'failed',error_message:'视频编码内存不足',execution:{schema:1,state:'failed',phase:'cleanup',label:'登记结果与清理临时文件',detail:'视频编码内存不足',history:[{phase:'encoding_preview',label:'编码试片视频',state:'failed'},{phase:'cleanup',state:'failed'}]}}}
    nav.steps[0]={...nav.steps[0],status:'failed',job_id:9,error:{message:'视频编码内存不足',suggestion:'请关闭暂时不用的配音服务，然后重试试片。'}}
  })
  assert.equal(page.taskSuggestion,'请关闭暂时不用的配音服务，然后重试试片。')
  assert.equal((html.match(/请关闭暂时不用的配音服务，然后重试试片。/g) || []).length,1)
  assert.match(html,/上一次成功试片仍可播放/)
  assert.equal(page.previewUrl,'/trial/preview_version_dub_1.mp4')
  nav.steps[0].job_id=10;await nextTick()
  assert.equal(page.taskSuggestion,'')
  page.history.latest_task.error_suggestion='视频编码内存不足'
  assert.equal(page.taskSuggestion,'')
})
test('停止与取消单独展示，Step.failed的取消投影不误报失败建议',async()=>{
  for(const status of ['cancelled','stopped']){
    const {page,html}=await render((page,nav)=>{
      page.history={items:[item()],latest_task:{scope:'preview',job_id:9,version_ids:['dub'],status,error_message:'步骤已停止'}}
      nav.steps[0]={...nav.steps[0],status:'failed',job_id:9,error:{code:'STEP_CANCELLED',message:'步骤已停止',suggestion:'可重新运行'}}
      page.requests={dub:{jobId:9,signature:'旧参数',range:{start:0,end:10}}}
    })
    assert.equal(page.taskStopped,true);assert.equal(page.taskFailed,false)
    assert.equal(page.taskExecution.state,'cancelled');assert.equal(page.taskExecution.label,'试片已停止')
    assert.match(html,/可重新生成试片/);assert.match(html,/上一次成功试片仍可播放/)
    assert.doesNotMatch(html,/处理建议|根据上方失败原因|这次失败替换/)
  }
})
test('资源预警与后端使用相同commit比例和物理内存边界，未知值不警报',async()=>{
  const gib=1024**3,mib=1024**2
  const {page,html}=await render(page=>{page.history={items:[],resources:{commit_available_bytes:2.89*gib,commit_limit_bytes:60*gib,physical_available_bytes:gib}}})
  assert.match(html,/系统可提交内存仅余 2.89 GB/);assert.equal(page.actionDisabled,false)
  page.history={items:[],resources:{commit_available_bytes:2*gib,commit_limit_bytes:20*gib,physical_available_bytes:512*mib}}
  assert.equal(page.resourceWarning,'')
  page.history={items:[],resources:{physical_available_bytes:511*mib}}
  assert.match(page.resourceWarning,/可用物理内存仅余 511.0 MB/)
  page.history={items:[],resources:{temp_disk_free_bytes:100*mib,disk_free_bytes:7*mib}}
  assert.match(page.resourceWarning,/临时目录所在磁盘仅余 100.0 MB/)
  assert.match(page.resourceWarning,/试片所在磁盘仅余 7.0 MB/)
  page.history={items:[],resources:{commit_available_bytes:null,commit_limit_bytes:null,physical_available_bytes:null}}
  assert.equal(page.resourceWarning,'')
})
test('真实终态job_id为空时通过同job执行投影关联建议，历史与运行任务仍严格隔离',async()=>{
  for(const localRequest of [false,true]){
    const {page,nav,html}=await render((page,nav)=>{
      page.history={items:[item()],latest_task:{scope:'preview',job_id:77,version_ids:['dub'],status:'failed',error_message:'视频编码内存不足'}}
      nav.steps[0]={...nav.steps[0],status:'failed',job_id:null,execution:{schema:1,job_id:77,state:'failed'},error:{code:'ASSEMBLE_MEMORY_PRESSURE',message:'视频编码内存不足',suggestion:'关闭暂时不用的配音服务，释放内存后重试即可。'}}
      if(localRequest)page.requests={dub:{jobId:77,signature:'旧参数',range:{start:0,end:10}}}
    })
    assert.equal(page.stepJobId,77)
    assert.equal(page.taskSuggestion,'关闭暂时不用的配音服务，释放内存后重试即可。')
    assert.match(html,/关闭暂时不用的配音服务，释放内存后重试即可。/)
    nav.steps[0].execution.job_id=78;await nextTick()
    assert.equal(page.taskSuggestion,'')
    nav.steps[0].execution={schema:2,job_id:77,state:'failed'};await nextTick()
    assert.equal(page.taskSuggestion,'')
    nav.steps[0].execution={schema:1,job_id:77,state:'running'};nav.steps[0].status='running';await nextTick()
    assert.equal(page.stepJobId,null);assert.equal(page.taskSuggestion,'')
  }
})
