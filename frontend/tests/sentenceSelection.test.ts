import assert from 'node:assert/strict'
import test from 'node:test'
import { selectRange } from '../src/utils/sentenceSelection.ts'

test('Shift 正向反向区间选择、取消和筛选保留隐藏选择', () => {
  assert.deepEqual(selectRange([9], [1, 3, 5, 7], 7, true, 3, true), [9, 3, 5, 7])
  assert.deepEqual(selectRange([1, 3, 5, 7, 9], [1, 3, 5, 7], 3, false, 7, true), [1, 9])
  assert.deepEqual(selectRange([9], [3, 5], 5, true, 1, true), [9, 5])
})
