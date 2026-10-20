import assert from 'node:assert/strict'
import test from 'node:test'
import {
  llmParameterHelp, llmParameterOptions,
  llmParameterReference, llmParameterShapeError, llmParameterValue, parseLlmParams, writeLlmParameter,
} from '../src/utils/llmParameters.ts'

test('空白为不添加参数；非法 JSON、数组或单值必须报错，不能变成空配置', () => {
  for (const raw of ['', '  ', '{}']) assert.deepEqual(parseLlmParams(raw), { valid: true, params: {}, error: '' })
  for (const raw of ['{bad', '[]', 'null', 'false', '1', '"value"']) {
    const result = parseLlmParams(raw)
    assert.equal(result.valid, false); assert.equal(result.params, null); assert.ok(result.error)
    assert.equal(writeLlmParameter(raw, 'temperature', 1).json, raw)
  }
})

test('常用控件修改保留高级未知字段，嵌套 thinking 合并，false 和 0 不等于删除', () => {
  const original = '{"vendor":{"nested":[1,2]},"thinking":{"type":"enabled","budget":4096},"temperature":1}'
  let raw = writeLlmParameter(original, 'thinking.clear_thinking', false).json
  raw = writeLlmParameter(raw, 'temperature', 0).json
  raw = writeLlmParameter(raw, 'stream', false).json
  assert.deepEqual(JSON.parse(raw), { vendor: { nested: [1, 2] }, thinking: { type: 'enabled', budget: 4096, clear_thinking: false }, temperature: 0, stream: false })
  raw = writeLlmParameter(raw, 'thinking.type', '').json
  assert.deepEqual(JSON.parse(raw).thinking, { budget: 4096, clear_thinking: false })
  raw = writeLlmParameter(raw, 'temperature', null).json
  assert.equal(Object.hasOwn(JSON.parse(raw), 'temperature'), false)
  assert.deepEqual(JSON.parse(raw).vendor, { nested: [1, 2] })
})

test('删除最后一个自定义字段恢复空草稿；删除不存在字段不改原文；格式异常的嵌套值原样保留', () => {
  assert.equal(writeLlmParameter('{"thinking":{"type":"enabled"}}', 'thinking.type', '').json, '')
  assert.equal(writeLlmParameter(' { "custom" : 1 } ', 'top_p', null).json, ' { "custom" : 1 } ')
  for (const thinking of [false, null, 'enabled', []]) {
    const raw = JSON.stringify({ thinking, custom: 5 })
    const next = writeLlmParameter(raw, 'thinking.type', 'enabled')
    assert.equal(next.json, raw); assert.ok(next.error)
    assert.ok(llmParameterShapeError(JSON.parse(raw), 'thinking.clear_thinking'))
  }
})

test('高级值类型错误只提示，不将 null / 字符串 / 非整数伪装成可选默认', () => {
  for (const [key, value] of [['temperature', null], ['top_p', '0.95'], ['stream', 'false'], ['max_tokens', 1.5]] as const)
    assert.ok(llmParameterShapeError({ [key]: value }, key))
  assert.equal(llmParameterValue({ thinking: { clear_thinking: false } }, 'thinking.clear_thinking'), false)
  assert.equal(llmParameterShapeError({ temperature: 0, max_tokens: 1 }, 'temperature'), '')
})

test('不同型号和未知代理共用通用说明与选项，不固化型号默认、推荐或支持范围', () => {
  const paths = ['temperature', 'top_p', 'max_tokens', 'reasoning_effort', 'thinking.type', 'thinking.clear_thinking', 'stream', 'tool_stream', 'do_sample'] as const
  for (const model of ['glm-5.3-flash', 'glm-4.7', 'gpt-5.2', 'future-model', 'ark-code-latest', 'ep-123']) {
    for (const path of paths) {
      assert.equal(llmParameterHelp(path, model), llmParameterHelp(path))
      assert.equal(llmParameterReference(path, model), llmParameterReference(path, ''))
      assert.deepEqual(llmParameterOptions(path, model, {}), llmParameterOptions(path, '', {}))
      assert.doesNotMatch(llmParameterHelp(path, model), /GLM|GPT|65536|131072|官方推荐|官方默认/)
    }
  }
  assert.match(llmParameterHelp('temperature'), /先沿用服务商默认.*小幅修改/)
  assert.match(llmParameterHelp('reasoning_effort'), /不保证每个模型均支持/)
})

test('通用思考候选只是填写辅助，未知旧值保留可见，软件流式默认准确', () => {
  const values = (path: any, model: string, params: any = {}) => llmParameterOptions(path, model, params).map(option => option.value)
  assert.deepEqual(values('reasoning_effort', 'glm-5.3-flash'), ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
  assert.deepEqual(values('thinking.type', 'glm-5.3'), ['', 'enabled', 'disabled'])
  assert.deepEqual(values('reasoning_effort', 'gpt-future', { reasoning_effort: 'vendor-custom' }), ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'vendor-custom'])
  assert.deepEqual(values('thinking.type', 'proxy', { thinking: { type: 'vendor-custom' } }), ['', 'enabled', 'disabled', 'vendor-custom'])
  assert.match(llmParameterHelp('thinking.clear_thinking'), /独立单轮.*通常没有跨轮收益/)
  assert.match(llmParameterHelp('stream'), /DubForge 未指定时主动开启/)
  assert.match(llmParameterOptions('stream', '', {})[0].label, /DubForge/)
  assert.match(llmParameterHelp('tool_stream'), /当前字幕整理、翻译没有工具调用/)
  assert.match(llmParameterHelp('max_tokens'), /max_completion_tokens.*max_output_tokens.*不会自动迁移/)
})

test('高级 JSON 含同名原型字段也按普通字段保留，不污染对象原型', () => {
  const next = JSON.parse(writeLlmParameter('{"__proto__":{"polluted":true},"thinking":{"__proto__":{"x":1}}}', 'thinking.type', 'enabled').json)
  assert.deepEqual(next.__proto__, { polluted: true })
  assert.deepEqual(next.thinking.__proto__, { x: 1 })
  assert.equal(({} as any).polluted, undefined)
})
