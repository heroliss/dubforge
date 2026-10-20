// 配音预设只管理声音和生成参数，连接地址及运行保护继续由当前配置范围管理。
const PREFIX = 'tts.indextts25.'
export const INDEX_TTS_PRONUNCIATION_PATH = PREFIX + 'pronunciation_text'
const NAMES = ['timbre_mode', 'timbre_audio', 'timbre_reference_line_id', 'emo_mode', 'emo_audio', 'emo_reference_line_id', 'emo_weight', 'emo_vector',
  'emo_text', 'emo_random', 'do_sample', 'top_p', 'top_k', 'temperature', 'length_penalty',
  'num_beams', 'repetition_penalty', 'max_mel_tokens', 'max_text_tokens_per_segment',
  'lang', 'duration_factor', 'duration_retry_factors', 'dynamic_reference_max_seconds']
export const INDEX_TTS_PRESET_PATHS = NAMES.map(name => PREFIX + name)
const PATHS = new Set(INDEX_TTS_PRESET_PATHS)

export function indexTtsParameterGroups(fields: any[]) {
  // 统一旧服务返回的显示名称；不改变已保存枚举和参考句配置。
  fields = fields.map(field => field.path === PREFIX + 'dynamic_reference_max_seconds'
    ? { ...field, label: '本句原声截取上限（秒）' }
    : [PREFIX + 'timbre_mode', PREFIX + 'emo_mode'].includes(field.path) && Array.isArray(field.options)
      ? { ...field, options: field.options.map((option: any) => option.value === 'dynamic' ? { ...option, label: '本句原声' } : option) } : field)
  return [
    { name: '朗读内容', fields: fields.filter(field => field.path === INDEX_TTS_PRONUNCIATION_PATH) },
    { name: '预设参数', fields: fields.filter(field => PATHS.has(field.path)) },
    { name: '配音时间排布', fields: fields.filter(field => field.path.startsWith('tts.placement.')) },
    { name: '服务与运行', fields: fields.filter(field => field.path !== INDEX_TTS_PRONUNCIATION_PATH && !PATHS.has(field.path) && !field.path.startsWith('tts.placement.')) },
  ].filter(group => group.fields.length)
}

const LABELS: Record<string, string> = {
  timbre_mode: '音色参考来源', timbre_audio: '音色参考文件', emo_mode: '情感来源',
  timbre_reference_line_id: '音色参考句', emo_reference_line_id: '情感参考句',
  emo_audio: '情感参考文件', emo_weight: '情感强度', emo_vector: '情感向量',
  emo_text: '情感描述', emo_random: '情感随机采样', do_sample: '语音随机采样（do_sample）',
  top_p: '采样概率范围（top_p）', top_k: '采样候选数量（top_k）', temperature: '采样温度（temperature）',
  length_penalty: '序列长度偏好（length_penalty）', num_beams: '搜索分支数（num_beams）', repetition_penalty: '语音重复惩罚（repetition_penalty）',
  max_mel_tokens: '每段语音 token 上限（max_mel_tokens）', max_text_tokens_per_segment: '内部分段 token 上限（max_text_tokens_per_segment）',
  lang: '合成语言', duration_factor: '合成时长因子', duration_retry_factors: '超长配音重试因子',
  dynamic_reference_max_seconds: '本句原声截取上限（秒）', url: '服务地址', project_dir: '安装目录',
  request_timeout_seconds: '单句请求超时',
}
function fieldLabel(path: string, fields: any[]) {
  const label = fields.find(field => field.path === path)?.label
  return label && label !== path ? label : LABELS[path.slice(PREFIX.length)]
    || (path === 'tts.backend' ? '配音服务' : path.startsWith('runner.') ? '步骤超时兜底' : path)
}
export function presetValueLabel(path: string, value: any, fields: any[] = []): string {
  if (value == null) return '未设置'
  if (value === '') return '未填写'
  if (typeof value === 'boolean') return value ? '开启' : '关闭'
  if ([PREFIX + 'timbre_mode', PREFIX + 'emo_mode'].includes(path) && value === 'dynamic') return '本句原声'
  const options = fields.find(field => field.path === path)?.options ?? []
  const option = options.find((item: any) => (Array.isArray(item) ? item[1] : item.value) === value)
  if (option) {
    const label = String(Array.isArray(option) ? option[0] : option.label)
    return [PREFIX + 'timbre_mode', PREFIX + 'emo_mode'].includes(path)
      ? label.replace(/^(?:preset|dynamic|stable|same|vector|text)\s+/, '') : label
  }
  if (path === PREFIX + 'timbre_mode') return ({ preset: '固定音色参考', stable: '指定句原声', dynamic: '本句原声' } as Record<string, string>)[value] || String(value)
  if (path === PREFIX + 'emo_mode') return ({ same: '沿用音色参考', preset: '独立情感音频', stable: '指定句原声', dynamic: '本句原声', vector: '手动情感向量', text: '情感描述文本' } as Record<string, string>)[value] || String(value)
  if ([PREFIX + 'timbre_reference_line_id', PREFIX + 'emo_reference_line_id'].includes(path)) return `第 ${value} 句`
  if (path === PREFIX + 'emo_vector' && Array.isArray(value)) {
    const labels = ['喜', '怒', '哀', '惧', '厌恶', '低落', '惊喜', '平静']
    const active = value.flatMap((number, index) => Number(number) ? [`${labels[index] ?? index + 1} ${number}`] : [])
    return active.length ? active.join(' · ') : '各项均为 0'
  }
  if (Array.isArray(value)) return value.length ? value.join('、') : '不重试'
  if (path.endsWith('_seconds')) return `${value} 秒`
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}
export function presetComparison(values: Record<string, any>, draft: Record<string, any>, fields: any[] = []) {
  const allowed = new Set(fields.length ? fields.map(field => field.path) : INDEX_TTS_PRESET_PATHS)
  const applied = Object.entries(values).filter(([path]) => PATHS.has(path) && allowed.has(path))
    .map(([path, after]) => ({ path, label: fieldLabel(path, fields), before: draft[path], after,
      changed: JSON.stringify(draft[path]) !== JSON.stringify(after) }))
  // 原生预设没有 DubForge 扩展时保留当前值；服务与运行参数始终保持当前配置。
  const preserved = fields.filter(field => (field.path.startsWith(PREFIX) || field.path === 'tts.backend'
    || field.path.startsWith('runner.')) && (!PATHS.has(field.path) || !Object.hasOwn(values, field.path)))
    .map(field => ({ path: field.path, label: fieldLabel(field.path, fields), before: draft[field.path],
      after: draft[field.path], changed: false }))
  return { changed: applied.filter(row => row.changed), unchanged: applied.filter(row => !row.changed), preserved }
}
