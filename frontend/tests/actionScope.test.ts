import assert from 'node:assert/strict'
import test from 'node:test'
import { defaultOutputLabel, hiddenSelectionCount, runPlanDescription, selectedOutputVariants } from '../src/utils/actionScope.ts'

test('成品空选不隐式选择默认方案；选择独立去重，快照不受后续勾选影响', () => {
  assert.deepEqual(selectedOutputVariants([]), [])
  const selected = ['src_hardsub', 'src_hardsub', 'invalid']
  const snapshot = selectedOutputVariants(selected)
  selected.push('dub')
  assert.deepEqual(snapshot, ['src_hardsub'])
  assert.equal(defaultOutputLabel(false), '配音 · 不烧录字幕')
  assert.equal(defaultOutputLabel(true), '配音 · 烧录字幕')
})

test('筛选不取消隐藏勾选，摘要准确区分当前列表和全部勾选', () => {
  assert.equal(hiddenSelectionCount([1, 2, 3], [2]), 2)
  assert.equal(hiddenSelectionCount(['a', 'b'], ['a', 'b']), 0)
  assert.equal(hiddenSelectionCount([], []), 0)
})

test('运行摘要明确步骤顺序、保存语义及默认成品范围', () => {
  const text = runPlanDescription({ steps: ['tts', 'assemble'], labels: { tts: '配音', assemble: '合成成片' }, effects: ['默认方案：配音 · 不烧录字幕（1 种）'] })
  assert.match(text, /配音 → 合成成片/)
  assert.match(text, /不使用未保存的草稿/)
  assert.match(text, /默认方案：配音 · 不烧录字幕（1 种）/)
})
