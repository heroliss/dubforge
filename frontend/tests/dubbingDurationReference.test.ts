import assert from 'node:assert/strict'
import test from 'node:test'
import { dubbingDurationReference } from '../src/utils/dubbingDurationReference.ts'

test('实际因子读取采用结果，起始因子读取首尝试，不误用最后一次较差尝试', () => {
  const view = dubbingDurationReference({ selection: { take: 2 }, wav: { exists: true,
    duration_retry: { used_factor: .85, retries: 2, attempts: [{ factor: 1.1 }, { factor: .85 }, { factor: .7 }] } } }, 1.2)
  assert.equal(view.requested, 1.1); assert.equal(view.used, .85); assert.equal(view.configured, 1.2)
  assert.equal(view.take, 2); assert.equal(view.retries, 2)
})

test('无历史记录不能把配置值当成配音实际值，空位不能沿用旧版本记录', () => {
  const old = dubbingDurationReference({ selection: { take: 1 }, wav: { exists: true } }, 1)
  assert.equal(old.used, null); assert.equal(old.requested, null); assert.equal(old.generated, true)
  const empty = dubbingDurationReference({ selection: { take: null }, wav: { exists: false,
    duration_retry: { used_factor: .7, attempts: [{ factor: 1 }] } } }, 1)
  assert.equal(empty.used, null); assert.equal(empty.requested, null); assert.equal(empty.generated, false)
})

test('所选版本改变时读对应记录，已有旧版本记录与缺失音频分别保留提示', () => {
  const first = dubbingDurationReference({ selection: { take: 1 }, wav: { exists: false, stale: true,
    duration_retry: { used_factor: .7, attempts: [{ factor: 1 }] } } })
  const second = dubbingDurationReference({ selection: { take: 2 }, wav: { exists: true,
    duration_retry: { used_factor: 1.1, attempts: [{ factor: 1.1 }] } } })
  assert.equal(first.used, .7); assert.equal(first.stale, true); assert.equal(first.generated, true)
  assert.equal(second.used, 1.1); assert.equal(second.stale, false)
})

test('异常或缺失记录保守显示未知，失败重试保留采用音频的实际值', () => {
  for (const value of [true, '0.7', NaN, Infinity, 0, 3]) {
    const view = dubbingDurationReference({ selection: { take: 1 }, wav: { exists: true,
      duration_retry: { used_factor: value, attempts: [{ factor: value }], retries: -1 } } }, value)
    assert.equal(view.used, null); assert.equal(view.requested, null); assert.equal(view.configured, null); assert.equal(view.retries, 0)
  }
  const failed = dubbingDurationReference({ selection: { take: 1 }, wav: { exists: true,
    duration_retry: { used_factor: .85, retries: 2, error: '重试失败', attempts: [{ factor: 1 }] } } })
  assert.equal(failed.retryFailed, true); assert.equal(failed.used, .85)
})
