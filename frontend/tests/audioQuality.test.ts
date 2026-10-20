import assert from 'node:assert/strict'
import test from 'node:test'
import { audioAssessment, audioFieldHelp, audioFieldNotice, audioMode, audioProcessing, audioAacPeakCorrection, audioStaticGainPlan, audioPeakConstraint, audioScope, audioSettingGroups, audioTime, audioValue, sourceComparison } from '../src/utils/audioQuality.ts'

test('新旧报告分清原片对照、整片混音和未知值，缺失不伪造零',()=>{
  assert.equal(audioValue(null,'LUFS'),'未测得')
  assert.equal(audioValue(0,'LU'),'0.0 LU')
  const legacy=sourceComparison({reference:{scope:'full_output_mix',integrated_lufs:-20}})
  assert.equal(legacy.ready,false);assert.deepEqual(legacy.measurement,{})
  assert.match(legacy.note,/历史报告未记录原片对照/)
  const ready=sourceComparison({source_comparison:{status:'ready',measurement:{integrated_lufs:-23}}})
  assert.equal(ready.ready,true);assert.equal(ready.measurement.integrated_lufs,-23)
  assert.match(ready.note,/相同源时段与剪辑/)
  assert.equal(sourceComparison({source_comparison:{status:'unavailable',reason:'原片没有音轨'}}).note,'原片没有音轨')
})

test('局部试片不按整片目标误报失败，完整成片保留达标及峰值限制状态',()=>{
  assert.equal(audioAssessment({preview:true,settings:{mode:'normalize'},target_reached:null,processing:{peak_limited:true}}).status,'preview')
  assert.equal(audioAssessment({settings:{mode:'normalize'},target_reached:false,processing:{peak_limited:true}}).status,'peak_limited')
  assert.equal(audioAssessment({assessment:{target_status:'below'}}).label,'低于统一目标')
  assert.equal(audioAssessment({assessment:{target_status:'reached'}}).type,'success')
  assert.equal(audioAssessment({settings:{mode:'off'}}).status,'off')
  assert.equal(audioAssessment({}).status,'unmeasurable')
})

test('动态峰值控制不显示伪造固定增益，静态模式明确实际整轨增益',()=>{
  assert.match(audioProcessing({processing:{actual_method:'dynamic',gain_db:10,nominal_gain_db:12}}),/瞬时增益会变化/)
  assert.doesNotMatch(audioProcessing({processing:{actual_method:'dynamic',gain_db:10}}),/10.0 dB|实际增益/)
  assert.equal(audioProcessing({processing:{actual_method:'linear',gain_db:2}}),'整轨实际增益 +2.0 dB')
  assert.match(audioMode('normalize'),/保留动态/);assert.match(audioMode('uniform'),/控制峰值/)
})

test('范围明确剪辑与试片，声音设置保持平衡与交付两个层级',()=>{
  assert.match(audioScope({preview:true,scope:{source_start:325,source_end:360,source_segments:[{start:325,end:330},{start:340,end:360}],output_duration:25}}),/当前试片 · 源时间 00:05:25.0–00:06:00.0 · 已应用同一剪辑保留范围 · 输出时长 00:00:25.0/)
  assert.match(audioScope({preview:true}),/历史报告未记录源时间/)
  const groups=audioSettingGroups([{path:'assemble.voice_volume'},{path:'assemble.final_loudness_mode'},{path:'assemble.final_true_peak_db'}])
  assert.deepEqual(groups.map(row=>[row.name,row.fields.length]),[['人声与背景平衡',1],['统一输出响度',2]])
  assert.match(audioFieldHelp({path:'assemble.final_true_peak_db',help:'峰值上限'},{'assemble.final_loudness_mode':'off'}),/暂不生效/)
  assert.match(audioFieldHelp({path:'assemble.voice_volume',help:'比例'},{}),/不修改已合成配音或旧成品/)
  assert.equal(audioFieldHelp({path:'assemble.subtitle_size',help:'字号'},{}),'字号')
})

test('声音报告时长统一时分秒，进位正确且未知值不伪装成零',()=>{
  assert.equal(audioTime(3661.2),'01:01:01.2')
  assert.equal(audioTime(59.96),'00:01:00.0')
  assert.equal(audioTime(0),'00:00:00.0')
  for(const value of [undefined,null,NaN,Infinity,-1,'10']) assert.equal(audioTime(value),'—')
})

test('真峰值提示只在实际启用且高于−1时出现，0不被前端改写或拒绝',()=>{
  const field={path:'assemble.final_true_peak_db'}
  for(const mode of ['normalize','uniform']){
    for(const value of [-.9,0]) assert.match(audioFieldNotice(field,value,{'assemble.final_loudness_mode':mode}),/峰值余量较小/)
    for(const value of [-1.5,-1,null,undefined,NaN,Infinity,'0']) assert.equal(audioFieldNotice(field,value,{'assemble.final_loudness_mode':mode}),'')
  }
  for(const mode of ['off',undefined,'unknown']) assert.equal(audioFieldNotice(field,0,{'assemble.final_loudness_mode':mode}),'')
  assert.equal(audioFieldNotice({path:'assemble.final_loudness_lufs'},0,{'assemble.final_loudness_mode':'uniform'}),'')
  const help=audioFieldHelp({...field,help:''},{'assemble.final_loudness_mode':'uniform'})
  assert.match(help,/默认 −1.5 dBTP，可调至 0 dBTP/)
  assert.match(audioFieldHelp({path:'assemble.final_loudness_mode',help:''},{}),/仅用整轨固定增益.*甚至需要减小音量/)
})

test('核心声音帮助以schema为准，缺失才兜底，不重复核心建议且保留上下文',()=>{
  for(const path of ['assemble.final_loudness_mode','assemble.final_true_peak_db','assemble.final_loudness_lufs']){
    const supplied='后端提供的完整参数说明'
    const help=audioFieldHelp({path,help:supplied},{'assemble.final_loudness_mode':'off'},'configuration')
    assert.ok(help.startsWith(supplied+'\n'))
    assert.equal(help.split(supplied).length-1,1)
    assert.doesNotMatch(help,/默认 −1.5|可调至 0|试听 −14|仅用整轨固定增益/)
    assert.match(help,/作用范围：保存后/)
    if(path!=='assemble.final_loudness_mode')assert.match(help,/暂不生效/)
    for(const empty of [undefined,null,'','  ']){
      const fallback=audioFieldHelp({path,help:empty},{'assemble.final_loudness_mode':'uniform'})
      assert.match(fallback,/保留动态|默认 −1.5|产品默认/)
      assert.match(fallback,/作用范围：此版本/)
    }
  }
})

test('只展示已保存的静态增益方案，编码修正、动态名义增益和缺失数据不互相替代',()=>{
  const report={settings:{mode:'normalize'},processing:{requested_gain_db:19,available_gain_db:-.1,headroom_db:.2,gain_db:-.3}}
  assert.match(audioStaticGainPlan(report),/目标所需增益 \+19.0 dB；峰值允许增益上限 -0.1 dB/)
  assert.match(audioStaticGainPlan(report),/已计入 0.2 dB 编码余量/)
  assert.equal(audioProcessing(report),'整轨实际增益 -0.3 dB')
  assert.doesNotMatch(audioStaticGainPlan(report),/实际.*-0.1 dB/)
  assert.equal(audioStaticGainPlan({settings:{mode:'normalize'},processing:{gain_db:1,nominal_gain_db:12}}),'')
  assert.equal(audioStaticGainPlan({...report,processing:{...report.processing,actual_method:'dynamic'}}),'')
  assert.equal(audioStaticGainPlan({...report,processing:{...report.processing,silent:true}}),'')
})

test('峰值受限解释适用于静态整轨方案，目标缺口仅从明确完整输出的实测值计算',()=>{
  const report={preview:false,integrated_lufs:-29.2,settings:{mode:'normalize',target_lufs:-10},processing:{peak_limited:true,gain_db:-.2}}
  assert.match(audioPeakConstraint(report)!.message,/不保证达到目标响度/)
  assert.match(audioPeakConstraint(report)!.shortfall,/19.2 LU/)
  assert.equal(audioPeakConstraint({...report,preview:true})!.shortfall,'')
  assert.equal(audioPeakConstraint({...report,preview:undefined})!.shortfall,'')
  assert.equal(audioPeakConstraint({...report,integrated_lufs:null})!.shortfall,'')
  assert.equal(audioPeakConstraint({...report,integrated_lufs:NaN})!.shortfall,'')
  for(const processing of [{peak_limited:false},{peak_limited:true,actual_method:'dynamic'},{peak_limited:true,silent:true}]) assert.equal(audioPeakConstraint({...report,processing}),null)
  assert.equal(audioPeakConstraint({...report,settings:{...report.settings,mode:'uniform'}}),null)
  assert.equal(audioPeakConstraint({...report,has_audio:false}),null)
})

const correctedAudio={preview:false,has_audio:true,integrated_lufs:-15.1,true_peak_db:-2.1,target_reached:false,
  settings:{mode:'uniform',target_lufs:-14,true_peak_db:-1.5},processing:{actual_method:'dynamic',gain_db:null,
    aac_peak_correction_db:-.8,aac_peak_verification:{initial_true_peak_db:-1.4,attempts:[
      {attempt:1,gain_db:-.4,true_peak_db:-1.4,integrated_lufs:-14.7},
      {attempt:2,gain_db:-.8,true_peak_db:-2.1,integrated_lufs:-15.1},
    ]}}}

test('AAC编码复核后衰减由保存证据解释，动态控制和未达目标不被伪装成统一完成',()=>{
  const original=structuredClone(correctedAudio)
  assert.equal(audioAacPeakCorrection(correctedAudio),'AAC 编码真峰值复核后衰减 0.8 dB（复测 2 次）；实际响度以上表测量为准。')
  assert.match(audioProcessing(correctedAudio),/瞬时增益会变化.*AAC 编码真峰值复核后衰减 0.8 dB/)
  assert.doesNotMatch(audioProcessing(correctedAudio),/达标|统一完成|整轨实际增益/)
  assert.notEqual(audioAssessment(correctedAudio).status,'reached')
  const preview={...correctedAudio,preview:true,target_reached:null}
  assert.equal(audioAssessment(preview).status,'preview')
  assert.doesNotMatch(audioAacPeakCorrection(preview),/失败|低于目标|未达标/)
  assert.deepEqual(correctedAudio,original)
})

test('旧报告只陈述已保存的AAC衰减，不根据衰减量或上限猜复测次数与峰值',()=>{
  for(const changed of [{aac_peak_verification:undefined},{aac_peak_verification:{max_attempts:3}},
    {aac_peak_verification:{attempts:[]}},{aac_peak_verification:{attempts:'2'}}]) {
    const report={...correctedAudio,processing:{...correctedAudio.processing,...changed}}
    const description=audioAacPeakCorrection(report)
    assert.match(description,/衰减 0.8 dB/)
    assert.doesNotMatch(description,/复测.*次|dBTP|-1.4|-2.1/)
  }
  const legacy={processing:{aac_peak_correction_db:-.4,gain_db:1.2}}
  assert.match(audioProcessing(legacy),/^整轨实际增益 \+1.2 dB；AAC 编码真峰值复核后衰减 0.4 dB/)
  assert.doesNotMatch(audioProcessing(legacy),/复测.*次|达标/)
  assert.match(audioAacPeakCorrection({processing:{aac_peak_correction_db:-.01}}),/衰减不足 0.1 dB/)
})

test('不完整或错代的复测记录不补出次数，没有有效负增益时不添加纠偏说明',()=>{
  const attempts=correctedAudio.processing.aac_peak_verification.attempts
  for(const changed of [[null],attempts.slice(1),[attempts[1],attempts[0]],
    [{...attempts[0],gain_db:'-.4'},attempts[1]],[{...attempts[0],true_peak_db:null},attempts[1]],
    [attempts[0],{...attempts[1],gain_db:-.7}],[attempts[0],{...attempts[1],true_peak_db:-1.8}]]) {
    const report={...correctedAudio,processing:{...correctedAudio.processing,aac_peak_verification:{attempts:changed}}}
    assert.match(audioAacPeakCorrection(report),/衰减 0.8 dB/)
    assert.doesNotMatch(audioAacPeakCorrection(report),/复测.*次/)
  }
  assert.doesNotMatch(audioAacPeakCorrection({...correctedAudio,true_peak_db:undefined}),/复测.*次/)
  for(const gain of [null,undefined,NaN,Infinity,-Infinity,0,.4,'-.4',false]) {
    assert.equal(audioAacPeakCorrection({processing:{aac_peak_correction_db:gain}}),'')
  }
  assert.equal(audioAacPeakCorrection({...correctedAudio,has_audio:false}),'')
  assert.equal(audioAacPeakCorrection({...correctedAudio,processing:{...correctedAudio.processing,silent:true}}),'')
})
