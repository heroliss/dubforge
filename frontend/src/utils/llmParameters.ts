export type LlmParams = Record<string, any>
export type LlmParameterPath = 'temperature' | 'top_p' | 'max_tokens' | 'reasoning_effort' |
  'thinking.type' | 'thinking.clear_thinking' | 'stream' | 'tool_stream' | 'do_sample'

export function parseLlmParams(raw: string | undefined): { valid: boolean; params: LlmParams | null; error: string } {
  if (!raw?.trim()) return { valid: true, params: {}, error: '' }
  try {
    const value = JSON.parse(raw)
    if (!value || typeof value !== 'object' || Array.isArray(value))
      return { valid: false, params: null, error: '参数必须是 JSON 对象，例如 {"temperature": 1}；不能使用数组、null 或单个值。' }
    return { valid: true, params: value, error: '' }
  } catch {
    return { valid: false, params: null, error: 'JSON 格式有误，请检查引号、逗号和括号。原有内容已保留，修正后即可使用常用选项。' }
  }
}

function isObject(value: any): value is LlmParams {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function llmParameterValue(params: LlmParams | null, path: LlmParameterPath): any {
  if (!params) return undefined
  const [parent, child] = path.split('.')
  if (!Object.hasOwn(params, parent)) return undefined
  if (!child) return params[parent]
  return isObject(params[parent]) && Object.hasOwn(params[parent], child) ? params[parent][child] : undefined
}

export function llmParameterShapeError(params: LlmParams | null, path: LlmParameterPath): string {
  if (!params) return ''
  if (path.startsWith('thinking.') && Object.hasOwn(params, 'thinking') && !isObject(params.thinking))
    return 'thinking 需要是 JSON 对象；当前值保留，请在高级 JSON 中修正后再使用选项。'
  const value = llmParameterValue(params, path)
  if (value === undefined) return ''
  const expected = ['temperature', 'top_p', 'max_tokens'].includes(path) ? 'number' :
    ['thinking.clear_thinking', 'stream', 'tool_stream', 'do_sample'].includes(path) ? 'boolean' : 'string'
  if (typeof value !== expected || expected === 'number' && !Number.isFinite(value))
    return `${path} 当前值的类型不能用选项编辑；值已保留，请在高级 JSON 中调整。`
  if (path === 'max_tokens' && (!Number.isInteger(value) || value <= 0))
    return 'max_tokens 应是正整数；当前值保留，请在高级 JSON 中调整。'
  return ''
}

// 常用选项和高级 JSON 始终共用一份草稿，未知字段及嵌套字段原样保留。
export function writeLlmParameter(raw: string, path: LlmParameterPath, value: any): { json: string; error: string } {
  const parsed = parseLlmParams(raw)
  if (!parsed.valid) return { json: raw, error: parsed.error }
  const params = { ...parsed.params! }
  const [parent, child] = path.split('.')
  const remove = value === null || value === undefined || value === ''
  if (child) {
    if (Object.hasOwn(params, parent) && !isObject(params[parent]))
      return { json: raw, error: llmParameterShapeError(params, path) }
    if (remove && (!Object.hasOwn(params, parent) || !Object.hasOwn(params[parent], child))) return { json: raw, error: '' }
    const nested = { ...(params[parent] ?? {}) }
    if (remove) delete nested[child]
    else nested[child] = value
    if (Object.keys(nested).length) params[parent] = nested
    else delete params[parent]
  } else {
    if (remove && !Object.hasOwn(params, parent)) return { json: raw, error: '' }
    if (remove) delete params[parent]
    else params[parent] = value
  }
  return { json: Object.keys(params).length ? JSON.stringify(params, null, 2) : '', error: '' }
}

export function llmParameterHelp(path: LlmParameterPath, _model = ''): string {
  const defaultNote = '选“服务商默认”或清空数值即不发送该字段。默认值和支持范围由所用模型及接口决定，DubForge 不代填。'
  const notes: Record<LlmParameterPath, string> = {
    temperature: '控制采样随机性。较低通常让表达更集中，较高更容易产生不同表达。建议先沿用服务商默认，确需调整时小幅修改；与 top_p 只重点调整一项。不同模型可能限制或不支持此参数。',
    top_p: '按累计概率保留采样候选，较低时范围通常更窄。建议先沿用服务商默认，确需调整时小幅修改；与 temperature 只重点调整一项。不同模型可能限制或不支持此参数。',
    max_tokens: '限制一次回复的输出 token 数量。上限不等于实际消耗量；过小可能截断整理或翻译结果。部分接口将推理 token 也计入限制。输出上限字段可能使用 max_tokens、max_completion_tokens 或 max_output_tokens，请按所用接口文档填写；不会自动迁移已有字段。',
    reasoning_effort: '控制支持此参数的模型的思考强度。更高通常需要更多推理时间和 token。建议先沿用服务商默认，简单任务可在确认支持后尝试较低强度。候选档位只是便于填写，不保证每个模型均支持。步骤可能覆盖此值，实际发送内容见请求预览。',
    'thinking.type': '部分兼容服务使用此参数控制思考开关。是否支持 enabled、disabled，以及能否关闭思考，取决于所用模型及接口。建议先沿用默认，确认支持后再修改。',
    'thinking.clear_thinking': '部分兼容服务使用此参数控制多轮对话是否清除先前的思考内容。保留历史思考还可能需要回传对应历史字段；独立单轮的整理、翻译通常没有跨轮收益。请先核对接口文档。',
    stream: '是否用流式响应持续接收输出。它改变接收方式，不等于加快模型计算。此处可明确开启或关闭，实际值请查看请求预览。DubForge 未指定时主动开启流式响应。',
    tool_stream: '部分兼容服务使用此参数流式返回工具调用内容，仅对工具调用场景有作用。当前字幕整理、翻译没有工具调用，无需为这些任务开启。请按接口文档确认支持情况。',
    do_sample: '部分兼容服务使用此参数控制随机采样。关闭时 temperature 和 top_p 的数值可能不再参与采样，具体行为以接口文档为准；已有值仍保留。建议先沿用服务商默认。',
  }
  return notes[path] + '\n\n' + (path === 'stream' ? '选“跟随 DubForge”即不写自定义值，DubForge 默认主动发送 stream=true；显式关闭可发送普通响应请求。' : defaultNote)
}

export function llmParameterOptions(path: LlmParameterPath, _model: string, params: LlmParams | null) {
  const common = { label: path === 'stream' ? '跟随 DubForge（默认开启）' : '服务商默认（不发送）', value: '' }
  let values: string[] = []
  if (path === 'reasoning_effort') values = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
  else if (path === 'thinking.type') values = ['enabled', 'disabled']
  else return [common, { label: '开启（true）', value: 'true' }, { label: '关闭（false）', value: 'false' }]
  const current = llmParameterValue(params, path)
  const options = [common, ...values.map(value => ({ label: value, value }))]
  if (typeof current === 'string' && current && !values.includes(current)) options.push({ label: `${current}（已有值，请核对支持范围）`, value: current })
  return options
}

export function llmParameterReference(path: LlmParameterPath, _model: string): string {
  const notes: Record<LlmParameterPath, string> = {
    temperature: '控制表达随机性；通常与 top_p 只重点调整一项。',
    top_p: '调整采样候选范围；通常与温度只重点调整一项。',
    max_tokens: '过小可能截断输出；上限不等于实际消耗。',
    reasoning_effort: '影响推理时间与 token；候选档位需接口支持。',
    'thinking.type': '仅部分接口支持；开关含义以接口文档为准。',
    'thinking.clear_thinking': '用于多轮历史思考；单轮通常没有跨轮收益。',
    stream: 'DubForge 默认开启；可明确关闭，实际值见请求预览。',
    tool_stream: '仅用于工具调用；当前字幕任务不调用工具。',
    do_sample: '仅部分接口支持；可能影响温度和 top_p 是否生效。',
  }
  return notes[path]
}
