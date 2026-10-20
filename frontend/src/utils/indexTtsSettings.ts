const PREFIX = 'tts.indextts25.'

// 仅在字段问号中展开用法，避免逐句表单重复铺开说明。
const PRONUNCIATION_HELP = [
  '默认显示并跟随当前译文，可自由修改标点、数字、措辞或整句；只影响配音，字幕和时间不变。保存自定义后独立保留，后续修改字幕不会自动改写。',
  '可选标音：没有<朝|CHAO2>，只指定该处“朝”，2 表示第二声。普通标点可以引导自然停顿，具体停顿由模型决定。',
  '保存后重新生成本句，或在合成运行中选择“保存并更新本句配音”。“恢复跟随译文”立即还原文字，保存后取消自定义并继续跟随译文。',
].join('\n\n')

const SAMPLING_KEYS = new Set(['top_p', 'top_k', 'temperature'].map(key => PREFIX + key))

function sameReferenceReason(values: Record<string, any>): string {
  const emotion = values[PREFIX + 'emo_mode'] ?? 'preset'
  const timbre = values[PREFIX + 'timbre_mode'] ?? 'preset'
  if (emotion === 'same') return '当前沿用音色参考的完整情感，情感强度不参与合成；切换为独立情感音频、向量或文本后才可调节。'
  if (emotion === 'dynamic' && timbre === 'dynamic') return '音色与情感都取本句同一段原声，调节强度不会产生情感差异；需要调节时，请使用独立的音色或情感参考。'
  if (emotion === 'stable' && timbre === 'stable'
      && Number(values[PREFIX + 'timbre_reference_line_id'] ?? 1) === Number(values[PREFIX + 'emo_reference_line_id'] ?? 1))
    return '音色与情感都使用同一句原声，调节强度不会产生情感差异；选择不同参考句后可调节。'
  if (emotion === 'preset' && timbre === 'preset') {
    const voice = String(values[PREFIX + 'timbre_audio'] ?? '').trim()
    const feeling = String(values[PREFIX + 'emo_audio'] ?? '').trim()
    if (voice && voice === feeling) return '音色与情感填写了同一参考文件，调节强度不会产生情感差异；需要调节时，请选择独立的情感音频。'
  }
  return ''
}

export function indexTtsFieldInactiveReason(path: string, values: Record<string, any>): string {
  if (!path.startsWith(PREFIX)) return ''
  const timbre = values[PREFIX + 'timbre_mode'] ?? 'preset'
  const emotion = values[PREFIX + 'emo_mode'] ?? 'preset'
  if ((path === PREFIX + 'timbre_reference_line_id' && timbre === 'dynamic')
      || (path === PREFIX + 'emo_reference_line_id' && emotion === 'dynamic'))
    return '本句原声自动使用正在合成句子的原声，无需手动选择；单句设置显示本句，切换为指定句原声时沿用此句。'
  if (path === PREFIX + 'timbre_reference_line_id' && timbre !== 'stable')
    return '当前音色来源不使用指定句原声；保留句子编号，切换来源后生效。'
  if (path === PREFIX + 'emo_reference_line_id' && emotion !== 'stable')
    return emotion === 'same' ? '当前情感沿用音色参考，无需单独选择参考句；当前值保留。'
      : '当前情感来源不使用指定句原声；保留句子编号，切换来源后生效。'
  if (path === PREFIX + 'dynamic_reference_max_seconds' && timbre !== 'dynamic' && emotion !== 'dynamic')
    return '当前没有使用本句原声参考，此上限不生效；自定义文件和指定句原声不受此项限制。当前值保留。'
  if (path === PREFIX + 'emo_vector' && emotion !== 'vector')
    return '当前情感来源不使用手动向量；当前值保留，切换为情感向量后生效。'
  if (path === PREFIX + 'emo_text' && emotion !== 'text')
    return '当前情感来源不使用文字描述；当前值保留，切换为文本情感后生效。'
  if (path === PREFIX + 'timbre_audio' && (values[PREFIX + 'timbre_mode'] ?? 'preset') !== 'preset')
    return '当前音色参考来源不直接读取此备用文件；路径保留，切换为自定义音色参考后生效。逐句切片失败时，可能回退到已保存的文件。'
  if (path === PREFIX + 'emo_audio' && (values[PREFIX + 'emo_mode'] ?? 'preset') !== 'preset')
    return '当前情感参考来源不直接读取此备用文件；路径保留，切换为自定义情感音频后生效。逐句切片失败时，可能回退到已保存的文件。'
  if (SAMPLING_KEYS.has(path) && values[PREFIX + 'do_sample'] === false)
    return '随机采样已关闭，此项不参与生成；当前值保留，重新开启采样后继续使用。'
  if (path === PREFIX + 'length_penalty' && Number(values[PREFIX + 'num_beams'] ?? 3) === 1)
    return '当前搜索分支为 1，没有束搜索的长度评分，此项不参与生成；当前值保留，增加搜索分支后继续使用。'
  if (path === PREFIX + 'emo_random' && !['vector', 'text'].includes(emotion))
    return '情感随机采样只用于向量或文本情感；当前音频参考模式不使用此项，已有设置保留。'
  if (path === PREFIX + 'emo_weight') return sameReferenceReason(values)
  return ''
}

export function indexTtsFieldHelp(path: string, help: string | undefined, values: Record<string, any>): string {
  if (path === PREFIX + 'pronunciation_text') return PRONUNCIATION_HELP
  const followsCurrent = (path === PREFIX + 'timbre_reference_line_id' && values[PREFIX + 'timbre_mode'] === 'dynamic')
    || (path === PREFIX + 'emo_reference_line_id' && values[PREFIX + 'emo_mode'] === 'dynamic')
  const notes = [followsCurrent ? '按源稿时间截取本句原声；切换为指定句原声后，可手动选择其他句子。' : (help ?? '').replaceAll('逐句原声', '本句原声'), indexTtsFieldInactiveReason(path, values)]
  if (!followsCurrent && !help?.trim() && (path === PREFIX + 'timbre_reference_line_id' || path === PREFIX + 'emo_reference_line_id'))
    notes.push('按原文句子选择参考，默认第 1 句；使用该句原声，最长读取前 15 秒。保存后用于再次合成，不改动已有配音。')
  if (path === PREFIX + 'timbre_mode' || path === PREFIX + 'timbre_audio') {
    const mode = values[PREFIX + 'timbre_mode'] ?? 'preset'
    if (mode === 'dynamic') notes.push('合成每句时参考该句原声，不自动扩展或换人。短句不足 2 秒、多人重叠或噪声较强时，音色更容易变化；可切换为指定句原声，选择同一说话人的较长清晰参考。切片失败时会按备用方案重试，实际来源记录在运行日志中。')
    else if (mode === 'stable') notes.push('使用所选原文句子的原声，默认第 1 句；请选择同一说话人清晰、少噪声的语句，配音排布不会改变参考位置。')
    else if (!help) notes.push('整段音色一致时，建议选同一说话人清晰、少噪声的 5–12 秒语音作为起点；这是配音建议。服务端只读取前 15 秒，开头应已有有效人声。')
  }
  if (path === PREFIX + 'dynamic_reference_max_seconds' && !indexTtsFieldInactiveReason(path, values)) {
    const timbre = values[PREFIX + 'timbre_mode'] === 'dynamic'
    const emotion = values[PREFIX + 'emo_mode'] === 'dynamic'
    const affected = timbre && emotion ? '音色和情感' : timbre
      ? values[PREFIX + 'emo_mode'] === 'same' ? '音色及沿用它的情感' : '音色' : '情感'
    notes.push(`当前限制本句${affected}参考：截取本句时长与设置上限中的较短者；不会把短句向前后扩展。自定义参考文件和指定句原声不受此项限制。`)
  }
  if (path === PREFIX + 'emo_mode' || path === PREFIX + 'emo_text') {
    if (values[PREFIX + 'emo_mode'] === 'text') notes.push('文本情感需要服务启动时加载 QwenEmotion；从其它模式切换后，请保存参数并按当前模式重启配音服务。正在运行的服务不会因修改设置自动加载模块。')
    else if (values[PREFIX + 'emo_mode'] === 'stable') notes.push('使用所选原文句子的原声，默认第 1 句；可与音色选择不同的参考句。')
  }
  if (path === PREFIX + 'emo_weight' && !sameReferenceReason(values)) {
    if (values[PREFIX + 'emo_mode'] === 'text') notes.push('当前为文本情感，强度值缩放模型理解出的情感向量；不会自动改写当前设置。')
    else if (values[PREFIX + 'emo_mode'] === 'vector') notes.push('当前为向量情感，强度值缩放手动情感向量；0 保留音色参考本身的情感，不代表完全无情感。')
    else notes.push('当前为独立音频参考，0 沿用音色参考情感，1 跟随情感参考。')
  }
  if (path === PREFIX + 'do_sample') notes.push(values[PREFIX + 'do_sample'] === false
    ? '当前随机采样已关闭，温度、top_p 和 top_k 的备用值保留；情感随机采样是独立开关。'
    : '当前随机采样已开启，温度、top_p 和 top_k 共同限制候选；情感随机采样是独立开关。')
  return [...new Set(notes.filter(Boolean))].join('\n\n')
}

/** 行内仅显示当前条件，完整原因仍由帮助浮层说明。 */
export function indexTtsFieldInactiveSummary(path: string, values: Record<string, any>): string {
  if (!indexTtsFieldInactiveReason(path, values)) return ''
  if ((path === PREFIX + 'timbre_reference_line_id' && values[PREFIX + 'timbre_mode'] === 'dynamic')
      || (path === PREFIX + 'emo_reference_line_id' && values[PREFIX + 'emo_mode'] === 'dynamic'))
    return '自动跟随本句 · 切换为指定句后可修改'
  const summaries: Record<string, string> = {
    dynamic_reference_max_seconds: '仅本句原声参考使用',
    timbre_reference_line_id: '指定句原声音色来源时启用', emo_reference_line_id: '指定句原声情感来源时启用',
    timbre_audio: '自定义音色来源时启用', emo_audio: '自定义情感音频时启用',
    emo_vector: '情感向量模式下启用', emo_text: '文本情感模式下启用',
    emo_random: '向量或文本情感时启用', emo_weight: '参考来源相同，无需调节',
    length_penalty: '搜索分支大于 1 时可调',
    top_p: '开启语音随机采样后可调', top_k: '开启语音随机采样后可调', temperature: '开启语音随机采样后可调',
  }
  return `${summaries[path.slice(PREFIX.length)] || '当前模式不使用此项'} · 保留原值`
}
