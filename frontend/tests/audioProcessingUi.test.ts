import assert from 'node:assert/strict'
import test,{before,after,mock} from 'node:test'
import {fileURLToPath} from 'node:url'
import {createServer,type ViteDevServer} from 'vite'
import vue from '@vitejs/plugin-vue'
import {createSSRApp,h,reactive} from 'vue'
import {renderToString} from '@vue/server-renderer'
import {setup} from '@css-render/vue3-ssr'
import {createRouter,createMemoryHistory} from 'vue-router'
import {createPinia} from 'pinia'
import {NDialogProvider,NMessageProvider} from 'naive-ui'
import {visualVersionSpec,visualValues} from '../src/utils/assemblyAudio.ts'
let server:ViteDevServer,Gain:any,Report:any,Preview:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Gain=(await server.ssrLoadModule('/src/components/SentenceAudioGain.vue')).default
  Report=(await server.ssrLoadModule('/src/components/AudioQualityReport.vue')).default
  Preview=(await server.ssrLoadModule('/src/components/AssemblyVersionPreview.vue')).default
  mock.method(globalThis,'fetch',()=>{throw new Error('禁止未声明网络请求')})
})
after(async()=>{mock.restoreAll();await server.close()})
const lineState=(extra:any={})=>({gain_db:0,effective_gain_db:0,saved_gain_db:null,stale:false,active:false,binding:'voice-a',revision:'r1',...extra})
const line=(extra:any={})=>({id:1,selection:{source:'dub',take:1},wav:{url:'voice-a'},audio_gain_state:lineState(),...extra})
async function render(Component:any,props:any,seed:(page:any)=>void=()=>{}){
  let page:any;const events:any[]=[]
  const input=reactive(props)
  const router=createRouter({history:createMemoryHistory(),routes:[{path:'/:pathMatch(.*)*',component:{render:()=>null}}]});await router.push('/')
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Component,{...input,onSaved:(value:any)=>events.push(['saved',value]),onDirtyChange:(value:boolean)=>events.push(['dirty',value])})})})})
  app.use(router);app.use(createPinia());setup(app)
  app.mixin({created(){if(this.$options.__name===Component.__name){page=this.$.setupState;seed(page)}}})
  const html=await renderToString(app)
  return {page,html,input,events}
}

test('本句后期音量明确应用且不改WAV，声源过期不会自动采用旧增益',async t=>{
  const {page,html,events}=await render(Gain,{uid:'fixture',line:line({audio_gain_state:lineState({stale:true,saved_gain_db:-3})})})
  assert.match(html,/之前的 −?|-3/);assert.match(html,/未自动应用/);assert.match(html,/不重新生成配音/)
  assert.equal(page.gain,0);page.reuse();assert.equal(page.gain,-3);assert.equal(page.dirty,true)
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{calls.push({url:String(url),options});return {ok:true,json:async()=>lineState({gain_db:-3,effective_gain_db:-3,active:true,revision:'r2'})}})
  await page.save(false)
  assert.equal(calls.length,1);assert.match(calls[0].url,/\/audio\/lines\/1$/)
  assert.deepEqual(JSON.parse(calls[0].options.body),{binding:'voice-a',revision:'r1',gain_db:-3})
  assert.equal(page.dirty,false);assert.ok(events.some(event=>event[0]==='saved'))
})

test('本句音量冲突保留调音草稿，不能继续用旧绑定提交，恢复用明确reset请求',async t=>{
  const {page}=await render(Gain,{uid:'fixture',line:line()});let attempts=0
  t.mock.method(globalThis,'fetch',async()=>{attempts++;return {ok:false,json:async()=>({code:'AUDIO_GAIN_CONFLICT',message:'声音已变化'})}})
  page.gain=4;await page.save(false)
  assert.equal(page.gain,4);assert.equal(page.conflict,true);assert.equal(page.dirty,true)
  await page.save(false);assert.equal(attempts,1)
  const reset=await render(Gain,{uid:'fixture',line:line({audio_gain_state:lineState({gain_db:2,effective_gain_db:2,active:true})})})
  let sent:any
  t.mock.method(globalThis,'fetch',async(_url,options)=>{sent=JSON.parse(String(options?.body));return {ok:true,json:async()=>lineState({revision:'r2'})}})
  await reset.page.save(true);assert.deepEqual(sent,{binding:'voice-a',revision:'r1',reset:true})
})

test('外部成片锁禁止保存和沿用，不因为控件事件绕过权限',async t=>{
  const {page,html}=await render(Gain,{uid:'fixture',line:line({audio_gain_state:lineState({stale:true,saved_gain_db:3})}),disabled:true})
  let calls=0;t.mock.method(globalThis,'fetch',()=>{calls++;throw new Error('不应保存')})
  page.reuse();page.gain=3;await page.save(false)
  assert.equal(calls,0);assert.equal(page.canSave,false);assert.match(html,/disabled/)
})

test('尚无声音时禁止应用和沿用旧增益，但仍可明确恢复0',async t=>{
  const {page,html}=await render(Gain,{uid:'fixture',line:line({audio_gain_state:lineState({available:false,stale:true,saved_gain_db:2})})})
  assert.match(html,/尚无可用声音/);page.reuse();assert.equal(page.gain,0)
  const calls:any[]=[];t.mock.method(globalThis,'fetch',async(_url,options)=>{calls.push(JSON.parse(String(options?.body)));return {ok:true,json:async()=>lineState({available:false,revision:'r2'})}})
  page.gain=2;await page.save(false);assert.equal(calls.length,0)
  await page.save(true);assert.equal(calls.length,1);assert.equal(calls[0].reset,true)
})

test('声音报告显示实际数值、历史来源和逐句定位，不将缺失值写成0',async()=>{
  const {html}=await render(Report,{uid:'fixture',outdated:true,report:{integrated_lufs:-20.2,true_peak_db:-1.7,loudness_range_lu:null,
    settings:{mode:'normalize',target_lufs:-16,true_peak_db:-1.5},processing:{gain_db:2,peak_limited:true},target_reached:false,
    issues:[{severity:'warning',message:'本次保留峰值余量'}],line_report:[{id:36,start:325,end:332,gain_db:-2,gain_stale:true,active_rms_db:-18,leveling_gain_db:0}]}} ,page=>{page.expanded=true})
  assert.match(html,/-20.2 LUFS/);assert.match(html,/-1.7 dBTP/);assert.match(html,/未测得/)
  assert.match(html,/历史成片报告/);assert.match(html,/本次增益受到限制/)
  assert.match(html,/\/projects\/fixture\/steps\/tts\?line=36/);assert.match(html,/旧增益未应用/)
})

test('纯声音调整不改变画面签名，声音试片仍按完整参数区分',async()=>{
  const version={id:'dub',name:'配音',dubbing:true,burn_subtitles:true,include_comments:false,overrides:{'assemble.voice_volume':.3,'assemble.subtitle_size':48}}
  const changed={...version,overrides:{...version.overrides,'assemble.voice_volume':.6,'assemble.final_loudness_mode':'normalize'}}
  assert.deepEqual(visualVersionSpec(version),visualVersionSpec(changed))
  assert.deepEqual(visualValues({'assemble.voice_volume':.3,'assemble.subtitle_size':48}),{'assemble.subtitle_size':48})
  assert.notDeepEqual(visualVersionSpec(version),visualVersionSpec({...version,overrides:{'assemble.subtitle_size':60}}))
  const {page,html,input}=await render(Preview,{uid:'fixture',version,effectiveValues:{'assemble.voice_volume':.3},overlayRevision:'r'})
  const before=page.signature,trial=page.trialSignature
  input.version.overrides['assemble.voice_volume']=.8
  assert.equal(page.signature,before);assert.notEqual(page.trialSignature,trial)
  assert.match(html,/快速画面预览/);assert.match(html,/不含声音/);assert.match(html,/生成有声试片/)
  assert.ok(before && trial)
})

test('纯原声版开启最终响度处理后提供试片入口，不再误示原片直用',async()=>{
  const {page,html}=await render(Preview,{uid:'fixture',version:{id:'raw',name:'原声',dubbing:false,burn_subtitles:false,include_comments:false,overrides:{}},
    effectiveValues:{'assemble.final_loudness_mode':'normalize'},overlayRevision:'r'})
  assert.equal(page.trialApplicable,true);assert.match(html,/生成有声试片/)
  assert.doesNotMatch(html,/此版本直接使用原片/)
})

test('大报告明细逐页渲染，静音和缺失指标不伪造响度',async()=>{
  const report={processing:{silent:true},settings:{mode:'off'},line_report:Array.from({length:1500},(_,i)=>({id:i+1,start:i*3,end:i*3+2,gain_db:-1,active_rms_db:null,leveling_gain_db:0}))}
  const {page,html}=await render(Report,{uid:'fixture',report},page=>{page.expanded=true})
  assert.equal(page.visible.length,40);assert.match(html,/40\/1500/);assert.match(html,/此输出为静音/)
  assert.doesNotMatch(html,/0\.0 LUFS/)
  page.limit+=40;assert.equal(page.visible.length,80)
})

test('纯诊断异常句在默认筛选中可见，短片参考与成品静音时间边界明确',async()=>{
  const {html,page}=await render(Report,{uid:'fixture',report:{integrated_lufs:-24,true_peak_db:-4,
    settings:{mode:'normalize',target_lufs:-16,true_peak_db:-1.5},processing:{peak_limited:true},target_reached:null,
    preview:true,reference:{scope:'full_output_mix'},silence_ranges:[{start:2,end:6}],trailing_silence_start:10,
    line_report:[{id:42,start:325,end:330,gain_db:0,leveling_gain_db:0,issues:[{kind:'silent_line',message:'该句声音接近静音，请试听核对。'}]}]}},page=>{page.expanded=true})
  assert.equal(page.filtered.length,1);assert.match(html,/该句声音接近静音/)
  assert.match(html,/沿用完整成片混音增益/);assert.match(html,/不要求等于整片目标/)
  assert.match(html,/成品静音区间（不一定异常）/);assert.match(html,/使用生成文件的成品时间/)
  assert.match(html,/00:00:10.0 起持续静音/);assert.match(html,/句子 \/ 源时间/)
  assert.doesNotMatch(html,/为保留真峰值余量，未继续放大至目标响度/)
})

test('本句音量读取失败保留调音草稿，明确重读成功后才替换状态',async t=>{
  const {page}=await render(Gain,{uid:'fixture',line:line()});page.gain=-4
  t.mock.method(globalThis,'fetch',async()=>({ok:false,json:async()=>({message:'连接中断'})}))
  await page.load();assert.equal(page.gain,-4);assert.equal(page.dirty,true);assert.match(page.error,/连接中断/)
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>lineState({gain_db:-2,effective_gain_db:-2,revision:'r2'})}))
  await page.load();assert.equal(page.gain,-2);assert.equal(page.dirty,false);assert.equal(page.error,'')
})


test('完整成片和旧报告不会误标为短片，能量优先展示调整后值并标明阶段',async()=>{
  const report={preview:false,reference:{scope:'full_output_mix'},settings:{mode:'normalize'},line_report:[
    {id:1,gain_db:2,active_rms_db:-20,effective_rms_db:-17,leveling_gain_db:1},
    {id:2,gain_db:1,active_rms_db:-22,leveling_gain_db:0},
  ]}
  let result=await render(Report,{uid:'fixture',report},page=>{page.expanded=true})
  assert.match(result.html,/正式成片的实际测量结果/);assert.doesNotMatch(result.html,/这里测量的是短片/)
  assert.match(result.html,/-17.0 dBFS/);assert.match(result.html,/调整后 · 整轨校准前/)
  assert.doesNotMatch(result.html,/-20.0 dBFS/);assert.match(result.html,/-22.0 dBFS/);assert.match(result.html,/>调整前</)
  const legacy={...report};delete (legacy as any).preview
  result=await render(Report,{uid:'fixture',report:legacy})
  assert.match(result.html,/当前输出文件的实际测量结果/);assert.doesNotMatch(result.html,/这里测量的是短片|正式成片的实际测量结果/)
})

test('声音矩阵按相同范围对照原片、成片和目标，名称与数值可聚焦查看帮助',async()=>{
  const {html}=await render(Report,{uid:'fixture',report:{preview:false,integrated_lufs:-16.1,true_peak_db:-1.7,loudness_range_lu:3.5,
    scope:{source_start:0,source_end:180,source_segments:[{start:0,end:60},{start:90,end:180}],output_duration:150},
    source_comparison:{status:'ready',measurement:{integrated_lufs:-28.4,true_peak_db:-11.6,loudness_range_lu:3.5}},
    comparison:{available:true,integrated_delta_lu:12.3},assessment:{target_status:'reached',lra_reliable:true},
    settings:{mode:'uniform',target_lufs:-16,true_peak_db:-1.5},processing:{actual_method:'linear',gain_db:2}}})
  assert.match(html,/原片同范围/);assert.match(html,/当前成片/);assert.match(html,/统一目标/)
  assert.match(html,/-28.4 LUFS/);assert.match(html,/-16.1 LUFS/);assert.match(html,/≤ -1.5 dBTP/)
  assert.match(html,/接近统一目标/);assert.match(html,/综合响度差 \+12.3 LU/)
  assert.match(html,/aria-label="综合响度说明"/);assert.match(html,/class="metric-value[^>]*tabindex="0"/)
  assert.match(html,/保持自然起伏/);assert.match(html,/LUFS 相近不代表人声一定清楚/)
})

test('试片动态处理和短样本范围明确，整片基准单独展示，不伪称固定放大或短片未达标',async()=>{
  const {html}=await render(Report,{uid:'fixture',report:{preview:true,integrated_lufs:-24,true_peak_db:-4,loudness_range_lu:1,
    scope:{source_start:325,source_end:335,output_duration:10},
    source_comparison:{status:'unavailable',reason_code:'no_audio',reason:'原片没有音轨'},assessment:{target_status:'preview',lra_reliable:false},
    settings:{mode:'uniform',target_lufs:-16,true_peak_db:-1.5},processing:{actual_method:'dynamic',gain_db:12},
    reference:{scope:'full_output_mix',integrated_lufs:-30},full_output_measurement:{integrated_lufs:-16,true_peak_db:-1.5,loudness_range_lu:4}}})
  assert.match(html,/试片局部测量/);assert.match(html,/00:05:25.0–00:05:35.0/);assert.match(html,/短于 60 秒/)
  assert.match(html,/完整成片声音处理再截取试片/);assert.match(html,/整片处理基准（编码前）/)
  assert.match(html,/原片没有音轨/);assert.match(html,/瞬时增益会变化/)
  assert.doesNotMatch(html,/实际增益 12.0|未继续放大至目标响度|-30.0 LUFS/)
  const historical=await render(Report,{uid:'fixture',outdated:true,report:{preview:true,settings:{mode:'uniform'}}})
  assert.match(historical.html,/历史试片报告/);assert.doesNotMatch(historical.html,/历史成片报告/)
})

test('静态峰值受限显示冻结目标、真实增益方案和整片实测缺口，不重复同一提示',async()=>{
  const {html}=await render(Report,{uid:'fixture',outdated:true,report:{preview:false,integrated_lufs:-29.2,true_peak_db:-1.3,
    settings:{mode:'normalize',target_lufs:-10,true_peak_db:-1},target_reached:false,
    processing:{gain_db:-.2,peak_limited:true,requested_gain_db:19,available_gain_db:-.1,headroom_db:.1},
    issues:[{kind:'peak_limited',severity:'info',message:'重复的峰值受限提示'},{kind:'other',severity:'info',message:'其他诊断保留'}]}})
  assert.match(html,/保留动态（仅固定增益）/)
  assert.match(html,/整轨实际增益 -0.2 dB/)
  assert.match(html,/目标所需增益 \+19.0 dB；峰值允许增益上限 -0.1 dB/)
  assert.match(html,/已计入 0.1 dB 编码余量/)
  assert.match(html,/完整输出实测仍低于本次目标 19.2 LU/)
  assert.match(html,/-10.0 LUFS/);assert.doesNotMatch(html,/>-16.0 LUFS</)
  assert.match(html,/不保证达到目标响度/);assert.match(html,/其他诊断保留/)
  assert.doesNotMatch(html,/重复的峰值受限提示/)
})

test('保留动态试片解释整轨受限，但不从局部或旧未知范围推算未达标缺口',async()=>{
  const report={preview:true,integrated_lufs:-28.7,true_peak_db:-1.3,settings:{mode:'normalize',target_lufs:-10,true_peak_db:-1},
    target_reached:null,reference:{scope:'full_output_mix',integrated_lufs:-29,true_peak_db:-1.1},
    processing:{gain_db:-.2,peak_limited:true,requested_gain_db:19,available_gain_db:-.1,headroom_db:.1}}
  const {html}=await render(Report,{uid:'fixture',report})
  assert.match(html,/试片局部测量/);assert.match(html,/整轨实际增益 -0.2 dB/)
  assert.match(html,/本次增益受到限制/);assert.match(html,/目标所需增益 \+19.0 dB/)
  assert.doesNotMatch(html,/低于本次目标|18.7 LU|低于统一目标/)
  const historical=await render(Report,{uid:'fixture',report:{...report,preview:undefined,processing:{gain_db:-.2,peak_limited:true}}})
  assert.match(historical.html,/整轨实际增益 -0.2 dB/)
  assert.doesNotMatch(historical.html,/低于本次目标|编码前整轨方案/)
})

test('AAC纠偏原因复用现有处理说明行，保留完整成片的实际指标和低于目标状态',async()=>{
  const report={preview:false,has_audio:true,integrated_lufs:-15.1,true_peak_db:-2.1,target_reached:false,
    assessment:{target_status:'below'},settings:{mode:'uniform',target_lufs:-14,true_peak_db:-1.5},
    processing:{actual_method:'dynamic',gain_db:null,aac_peak_correction_db:-.8,aac_peak_verification:{attempts:[
      {attempt:1,gain_db:-.4,true_peak_db:-1.4},{attempt:2,gain_db:-.8,true_peak_db:-2.1},
    ]}}}
  const original=structuredClone(report)
  const {html}=await render(Report,{uid:'fixture',report})
  assert.match(html,/低于统一目标/);assert.match(html,/-15.1 LUFS/);assert.match(html,/-14.0 LUFS/)
  assert.match(html,/-2.1 dBTP/);assert.match(html,/瞬时增益会变化/)
  assert.match(html,/<p class="report-note processing-note"[^>]*>[^<]*AAC 编码真峰值复核后衰减 0.8 dB（复测 2 次）；实际响度以上表测量为准。<\/p>/)
  assert.doesNotMatch(html,/接近统一目标|统一完成|整轨实际增益/)
  assert.deepEqual(report,original)
})

test('旧报告缺复测过程不猜次数，试片纠偏说明不把局部响度误判为目标失败',async()=>{
  const legacy={preview:undefined,integrated_lufs:-15.1,true_peak_db:-2.1,
    settings:{mode:'normalize',target_lufs:-14,true_peak_db:-1.5},
    processing:{gain_db:1.2,aac_peak_correction_db:-.8}}
  const historical=await render(Report,{uid:'fixture',report:legacy,outdated:true})
  assert.match(historical.html,/历史成片报告/);assert.match(historical.html,/整轨实际增益 \+1.2 dB/)
  assert.match(historical.html,/AAC 编码真峰值复核后衰减 0.8 dB/);assert.doesNotMatch(historical.html,/复测.*次/)
  const preview={...legacy,preview:true,integrated_lufs:-24,target_reached:null,settings:{...legacy.settings,mode:'uniform'},
    processing:{...legacy.processing,actual_method:'dynamic'},reference:{scope:'full_output_mix'}}
  const trial=await render(Report,{uid:'fixture',report:preview})
  assert.match(trial.html,/试片局部测量/);assert.match(trial.html,/不要求等于整片目标/)
  assert.match(trial.html,/AAC 编码真峰值复核后衰减 0.8 dB/)
  assert.doesNotMatch(trial.html,/低于统一目标|失败|未达标|复测.*次/)
  const unrecorded=await render(Report,{uid:'fixture',report:{...legacy,processing:{gain_db:1.2}}})
  assert.doesNotMatch(unrecorded.html,/AAC 编码真峰值复核后衰减|复测.*次/)
})
