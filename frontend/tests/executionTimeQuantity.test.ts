import assert from 'node:assert/strict'
import test from 'node:test'
import { normalizeExecution } from '../src/utils/executionProgress.ts'
import { executionTimeQuantity } from '../src/utils/executionTimeQuantity.ts'

const execution = (patch: Record<string, unknown> = {}) => ({
  schema: 1, state: 'running', phase: 'encoding', label: '编码成片', ...patch,
})

test('成片工作量显示时分秒，原始小数秒和当前编码比例保持真实', () => {
  const source = execution({ completed: 350.96, total: 3266.76, unit: '成片秒', elapsed_seconds: 65,
    metrics: { sentence_elapsed_seconds: 1.25, sentence_progress: 0.844 } })
  const before = structuredClone(source), current = normalizeExecution(source)!
  assert.equal(current.completedText, '00:05:51')
  assert.equal(current.totalText, '00:54:27')
  assert.equal(current.displayUnit, '成片时长')
  assert.equal(current.completed, 350.96); assert.equal(current.total, 3266.76)
  assert.equal(current.unit, '成片秒')
  assert.equal(current.workPercentage, 350.96 / 3266.76 * 100)
  assert.equal(current.workProgressLabel, '当前编码阶段')
  assert.equal(current.elapsed, '1 分 5 秒'); assert.equal(current.sentenceProgress, 84.4)
  assert.deepEqual(source, before)
})

test('只有明确秒量纲转换，保留时长语义标签并使用整秒进位', () => {
  for (const [unit, displayUnit] of [['成片秒', '成片时长'], ['音频秒', '音频时长'], ['片段秒', '片段时长'],
    ['秒', ''], ['s', ''], ['second', ''], ['seconds', '']]) {
    assert.deepEqual(executionTimeQuantity(0, 3599.9996, unit), {
      completedText: '00:00:00', totalText: '01:00:00', displayUnit,
    })
    const current = normalizeExecution(execution({ completed: 0, total: 3599.9996, unit }))!
    assert.equal(current.completedText, '00:00:00'); assert.equal(current.totalText, '01:00:00')
    assert.equal(current.displayUnit, displayUnit); assert.equal(current.total, 3599.9996)
  }
})

test('小数和分钟小时边界不显示60秒字段，超过一天不回绕', () => {
  for (const [seconds, expected] of [[0.125, '00:00:00'], [1.2, '00:00:01'], [59.9996, '00:01:00'],
    [3599.9996, '01:00:00'], [90061.25, '25:01:01']]) {
    const current = normalizeExecution(execution({ completed: seconds, total: seconds, unit: '音频秒' }))!
    assert.equal(current.completedText, expected); assert.equal(current.totalText, expected)
    assert.equal(current.completed, seconds); assert.doesNotMatch(current.completedText, /:60(?:\.|$)/)
  }
})

test('未知工作量与已知零分开，非法输入不伪造0或总时长', () => {
  for (const value of [null, undefined, NaN, Infinity, -1, '350.96', false, {}]) {
    const current = normalizeExecution(execution({ completed: value, total: value, unit: '成片秒' }))!
    assert.equal(current.completed, null); assert.equal(current.total, null)
    assert.equal(current.completedText, ''); assert.equal(current.totalText, '')
  }
  const partial = normalizeExecution(execution({ completed: 0, total: null, unit: '音频秒' }))!
  assert.equal(partial.completedText, '00:00:00'); assert.equal(partial.totalText, '')
  assert.equal(partial.workPercentage, null)
  const totalOnly = normalizeExecution(execution({ completed: undefined, total: 3600, unit: '成片秒' }))!
  assert.equal(totalOnly.completedText, ''); assert.equal(totalOnly.totalText, '01:00:00')
  assert.equal(executionTimeQuantity(Number.MAX_VALUE, null, '秒')!.completedText, '—')
})

test('句数、比例、百分比、速率及未知单位继续使用原计量', () => {
  for (const [unit, completed, total] of [['句', 69, 305], ['块', 2, 10], ['片段', 1, 6],
    ['%', 12.5, 100], ['比例', 0.25, 1], ['帧/秒', 24, 60], ['秒内失败次数', 3, 4], ['未知', 1.25, 8.5]]) {
    assert.equal(executionTimeQuantity(completed, total, unit), null)
    const current = normalizeExecution(execution({ completed, total, unit }))!
    assert.equal(current.completedText, String(completed)); assert.equal(current.totalText, String(total))
    assert.equal(current.displayUnit, unit); assert.equal(current.completed, completed); assert.equal(current.total, total)
  }
})

test('字节工作量保留文件大小格式和零值，不按秒转换', () => {
  for (const unit of ['字节', 'bytes', 'byte', 'B']) {
    const current = normalizeExecution(execution({ completed: 2048, total: 4096, unit }))!
    assert.equal(current.completedText, '2.0 KB'); assert.equal(current.totalText, '4.0 KB')
    assert.equal(current.displayUnit, ''); assert.equal(current.completed, 2048); assert.equal(current.total, 4096)
    assert.equal(normalizeExecution(execution({ completed: 0, total: null, unit }))!.completedText, '0 B')
  }
})

test('观察时钟只更新既有时间指标，不推进音频工作量或修改历史', () => {
  for (const state of ['queued', 'running', 'paused', 'completed', 'failed', 'cancelled']) {
    const source = execution({ state, completed: 0.25, total: 3601.5, unit: '音频秒', elapsed_seconds: 60,
      history: [{ phase: 'queue', state: 'completed', label: '等待资源' }] })
    const before = structuredClone(source), first = normalizeExecution(source)!, later = normalizeExecution(source, 10)!
    assert.equal(later.completedText, '00:00:00'); assert.equal(later.totalText, '01:00:02')
    assert.equal(later.completed, first.completed); assert.equal(later.total, first.total)
    assert.deepEqual(later.history, first.history); assert.equal(later.workPercentage, first.workPercentage)
    assert.deepEqual(source, before)
  }
})
