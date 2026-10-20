import assert from 'node:assert/strict'
import test from 'node:test'
import { INDEX_TTS_PRESET_PATHS, INDEX_TTS_PRONUNCIATION_PATH, indexTtsParameterGroups, presetComparison, presetValueLabel } from '../src/utils/dubbingPreset.ts'

const prefix = 'tts.indextts25.'
const field = (name: string, label = name) => ({ path: prefix + name, label, type: 'str' })

test('预设控制声音、情感、生成和时长参数；服务地址、安装目录与运行时限独立分组', () => {
  const voice = [field('timbre_mode'), field('timbre_audio'), field('emo_audio'), field('temperature'),
    field('duration_factor'), field('duration_retry_factors'), field('dynamic_reference_max_seconds')]
  const runtime = [field('url'), field('project_dir'), field('request_timeout_seconds'),
    { path: 'tts.backend', label: '配音服务', type: 'str' }, { path: 'runner.step_timeout', label: '步骤超时', type: 'int' }]
  const fields = [runtime[0], voice[0], voice[1], runtime[1], ...voice.slice(2), ...runtime.slice(2)]
  const before = structuredClone(fields), groups = indexTtsParameterGroups(fields)
  assert.deepEqual(groups.map(group => group.name), ['预设参数', '服务与运行'])
  assert.deepEqual(groups[0].fields, voice.map(row => row.path === prefix + 'dynamic_reference_max_seconds'
    ? { ...row, label: '本句原声截取上限（秒）' } : row))
  assert.deepEqual(groups[1].fields, runtime)
  assert.deepEqual(fields, before, '分组不重排或修改原表单schema')
  for (const row of voice) assert.ok(INDEX_TTS_PRESET_PATHS.includes(row.path))
  for (const row of runtime) assert.ok(!INDEX_TTS_PRESET_PATHS.includes(row.path))
  assert.deepEqual(indexTtsParameterGroups([]), [])
  assert.deepEqual(indexTtsParameterGroups(voice).map(group => group.name), ['预设参数'])
})

test('邻句排布是独立项目策略，不受音色预设覆盖或混入服务运行参数', () => {
  const placement = ['enabled', 'auto_repair', 'max_shift_seconds', 'window_lines', 'inter_sentence_gap']
    .map(name => ({ path: 'tts.placement.' + name, label: name, type: 'float' }))
  const groups = indexTtsParameterGroups([field('duration_factor'), ...placement, field('url')])
  assert.deepEqual(groups.map(group => group.name), ['预设参数', '配音时间排布', '服务与运行'])
  assert.deepEqual(groups[1].fields, placement)
  for (const row of placement) assert.ok(!INDEX_TTS_PRESET_PATHS.includes(row.path))
  assert.equal(presetComparison({ 'tts.placement.window_lines': 15, 'tts.placement.auto_repair': false }, {}, placement).changed.length, 0)
})

test('本句朗读内容独立在音色预设之前，不被音色预设覆盖', () => {
  const pronunciation = { path: INDEX_TTS_PRONUNCIATION_PATH, label: '朗读文本', type: 'text' }
  const groups = indexTtsParameterGroups([field('temperature'), field('url'), pronunciation])
  assert.deepEqual(groups.map(group => group.name), ['朗读内容', '预设参数', '服务与运行'])
  assert.deepEqual(groups[0].fields, [pronunciation])
  assert.equal(INDEX_TTS_PRESET_PATHS.includes(INDEX_TTS_PRONUNCIATION_PATH), false)
  assert.equal(presetComparison({ [INDEX_TTS_PRONUNCIATION_PATH]: '<朝|CHAO2>' }, {}, [pronunciation]).changed.length, 0)
})

test('差异逐项区分修改、不变和预设缺失后保留，零值、空路径与数组都按真实值比较', () => {
  const fields = [field('timbre_audio', '音色参考文件'), field('emo_audio', '情感参考文件'), field('emo_weight', '情感权重'),
    field('temperature', '采样温度'), field('emo_vector', '情感向量'), field('duration_factor', '时长因子'),
    field('project_dir', '安装目录'), field('request_timeout_seconds', '单句请求超时')]
  const draft = { [prefix + 'timbre_audio']: 'old.wav', [prefix + 'emo_audio']: 'feeling.wav',
    [prefix + 'emo_weight']: .5, [prefix + 'temperature']: .6,
    [prefix + 'emo_vector']: [.2, 0, 0, 0, 0, 0, 0, .3], [prefix + 'duration_factor']: .95,
    [prefix + 'project_dir']: 'D:\\IndexTTS', [prefix + 'request_timeout_seconds']: 600 }
  const values = { [prefix + 'timbre_audio']: '', [prefix + 'emo_weight']: 0,
    [prefix + 'temperature']: .6, [prefix + 'emo_vector']: [.2, 0, 0, 0, 0, 0, 0, .3] }
  const before = structuredClone({ draft, values }), rows = presetComparison(values, draft, fields)
  assert.deepEqual(rows.changed.map(row => row.path), [prefix + 'timbre_audio', prefix + 'emo_weight'])
  assert.deepEqual(rows.unchanged.map(row => row.path), [prefix + 'temperature', prefix + 'emo_vector'])
  assert.deepEqual(rows.preserved.map(row => row.path), [prefix + 'emo_audio', prefix + 'duration_factor', prefix + 'project_dir', prefix + 'request_timeout_seconds'])
  assert.deepEqual(rows.changed[0], { path: prefix + 'timbre_audio', label: '音色参考文件', before: 'old.wav', after: '', changed: true })
  for (const row of [...rows.unchanged, ...rows.preserved]) { assert.equal(row.changed, false); assert.deepEqual(row.before, row.after) }
  assert.deepEqual({ draft, values }, before, '只读差异不会改动表单或候选')
})

test('预设差异只包含允许的声音参数，未知字段及连接运行参数不能混入修改清单', () => {
  const values = { [prefix + 'timbre_audio']: 'new.wav', [prefix + 'temperature']: .7,
    [prefix + 'url']: 'http://other-server', [prefix + 'project_dir']: 'E:\\IndexTTS',
    'tts.backend': 'other', 'publish.title': '无关标题', [prefix + 'future_unknown']: true }
  const schema = [field('timbre_audio'), field('url'), field('project_dir')]
  const rows = presetComparison(values, {}, schema)
  assert.deepEqual(rows.changed.map(row => row.path), [prefix + 'timbre_audio'])
  const fallback = presetComparison(values, {})
  assert.deepEqual(fallback.changed.map(row => row.path), [prefix + 'timbre_audio', prefix + 'temperature'])
  assert.equal(fallback.changed[0].label, '音色参考文件', '没有schema也给出中文参数名')
})

test('差异值采用schema名称、开关语义、秒数和有意义的情感向量，避免裸英文枚举', () => {
  const fields = [
    { ...field('timbre_mode'), options: [['固定自定义音色', 'preset'], ['每句原声', 'dynamic']] },
    { ...field('lang'), options: [{ value: 'zh', label: '中文' }] },
  ]
  assert.equal(presetValueLabel(prefix + 'timbre_mode', 'preset', fields), '固定自定义音色')
  assert.equal(presetValueLabel(prefix + 'lang', 'zh', fields), '中文')
  assert.equal(presetValueLabel(prefix + 'emo_mode', 'same'), '沿用音色参考')
  assert.equal(presetValueLabel(prefix + 'emo_mode', 'text'), '情感描述文本')
  assert.equal(presetValueLabel(prefix + 'timbre_mode', 'stable'), '指定句原声')
  assert.equal(presetValueLabel(prefix + 'emo_mode', 'stable'), '指定句原声')
  assert.equal(presetValueLabel(prefix + 'timbre_mode', 'dynamic', fields), '本句原声', '旧schema选项不能覆盖统一名称')
  assert.equal(presetValueLabel(prefix + 'emo_mode', 'dynamic'), '本句原声')
  assert.equal(presetValueLabel(prefix + 'timbre_reference_line_id', 7), '第 7 句')
  assert.equal(presetValueLabel(prefix + 'emo_reference_line_id', 1), '第 1 句')
  assert.equal(presetValueLabel(prefix + 'do_sample', true), '开启')
  assert.equal(presetValueLabel(prefix + 'do_sample', false), '关闭')
  assert.equal(presetValueLabel(prefix + 'timbre_audio', ''), '未填写')
  assert.equal(presetValueLabel(prefix + 'timbre_audio', undefined), '未设置')
  assert.equal(presetValueLabel(prefix + 'timbre_audio', 'D:\\音色库\\声音.wav'), 'D:\\音色库\\声音.wav')
  assert.equal(presetValueLabel(prefix + 'dynamic_reference_max_seconds', 5), '5 秒')
  assert.equal(presetValueLabel(prefix + 'duration_factor', .95), '0.95')
  assert.equal(presetValueLabel(prefix + 'duration_retry_factors', []), '不重试')
  assert.equal(presetValueLabel(prefix + 'emo_vector', [.2, 0, 0, 0, 0, .1, 0, .3]), '喜 0.2 · 低落 0.1 · 平静 0.3')
  assert.equal(presetValueLabel(prefix + 'emo_vector', Array(8).fill(0)), '各项均为 0')
})

test('参考句属于声音预设范围，旧预设未包含时保留当前参考句', () => {
  const fields = [field('timbre_mode'), field('timbre_reference_line_id'), field('emo_reference_line_id')]
  const draft = { [prefix + 'timbre_mode']: 'preset', [prefix + 'timbre_reference_line_id']: 7, [prefix + 'emo_reference_line_id']: 3 }
  const old = presetComparison({ [prefix + 'timbre_mode']: 'stable' }, draft, fields)
  assert.deepEqual(old.changed.map(row => row.path), [prefix + 'timbre_mode'])
  assert.deepEqual(old.preserved.map(row => row.before), [7, 3])
  const next = presetComparison({ [prefix + 'timbre_reference_line_id']: 1, [prefix + 'emo_reference_line_id']: 7 }, draft)
  assert.deepEqual(next.changed.map(row => row.label), ['音色参考句', '情感参考句'])
})

test('旧schema在显示分组中统一本句原声名称，不修改枚举、参考句或原schema', () => {
  const fields = [{ ...field('timbre_mode', '音色参考来源'), type: 'select',
    options: [{ value: 'dynamic', label: '逐句原声' }, { value: 'stable', label: '指定句原声' }] },
  { ...field('emo_mode', '情感来源'), type: 'select',
    options: [{ value: 'dynamic', label: '逐句原声' }, { value: 'preset', label: '独立情感音频' }] },
  field('dynamic_reference_max_seconds', '逐句原声截取上限（秒）'), field('timbre_reference_line_id', '音色参考句')]
  const before = structuredClone(fields), group = indexTtsParameterGroups(fields)[0]
  assert.equal(group.fields[0].options[0].label, '本句原声')
  assert.equal(group.fields[1].options[0].label, '本句原声')
  assert.equal(group.fields[2].label, '本句原声截取上限（秒）')
  assert.equal(group.fields[0].options[0].value, 'dynamic')
  assert.equal(group.fields[0].options[1].label, '指定句原声')
  assert.deepEqual(group.fields[3], fields[3])
  assert.deepEqual(fields, before)
})
