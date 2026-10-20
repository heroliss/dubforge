import { versionSpec } from './assemblyVersions.ts'
const AUDIO_PATHS=new Set(['assemble.voice_volume','assemble.bgm_volume','assemble.ducking','assemble.loudnorm',
  'assemble.sentence_leveling','assemble.sentence_leveling_max_db','assemble.final_loudness_mode',
  'assemble.final_loudness_lufs','assemble.final_true_peak_db','assemble.audio_line_gains'])
export const isAudioSetting=(path:string)=>AUDIO_PATHS.has(path)
/** 声音调整不触发纯画面渲染；试片仍使用完整版本快照。 */
export function visualVersionSpec(version:any){
  const spec=versionSpec(version)
  return {...spec,overrides:Object.fromEntries(Object.entries(spec.overrides).filter(([key])=>!isAudioSetting(key)))}
}
export const visualValues=(values:Record<string,any>)=>Object.fromEntries(Object.entries(values).filter(([key])=>!isAudioSetting(key)))
