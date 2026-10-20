import assert from 'node:assert/strict'
import test from 'node:test'
import { dubbingTimingView, dubbingFactorSummary, playbackDraftError, timeRange } from '../src/utils/dubbingTimingPresentation.ts'

function line() {
  return { start: 10, end: 12, base: { start: 9.8, end: 12 }, playback_start: 9.5, playback_end: 12.5,
    selection: { source: 'dub', take: 1 }, wav: { exists: true, seconds: 3 },
    timing: { automatic_start: 9, automatic_end: 12, manual_start: 9.5, manual_end: null,
      manual_active: true, timing_revision: 'revision', effective_mode: 'manual' } }
}

test('三层时间保持独立，人工位置相对当前校对源轴披露，结束按实际音频', () => {
  const view = dubbingTimingView(line())
  assert.deepEqual(view.source, { start: 9.8, end: 12 })
  assert.deepEqual(view.automatic, { start: 9, end: 12 })
  assert.deepEqual(view.effective, { start: 9.5, end: 12.5 })
  assert.equal(view.label, '人工提前 0.50 秒'); assert.equal(view.editable, true)
  assert.equal(view.mode, 'manual'); assert.equal(view.reservedEnd, null)
  assert.equal(timeRange(view.effective), '00:09.500 — 00:12.500')
})

test('过期人工位置不冒充实际采用，缺失音频与原声不允许人工配音排布', () => {
  const input: any = line()
  input.timing.manual_active = false; input.timing.manual_stale = true; input.timing.effective_mode = 'automatic'
  const view = dubbingTimingView(input)
  assert.equal(view.label, '人工位置待核对'); assert.equal(view.mode, 'automatic')
  input.wav.exists = false; assert.equal(dubbingTimingView(input).editable, false)
  input.wav.exists = true; input.selection.source = 'original'; assert.equal(dubbingTimingView(input).editable, false)
  input.selection.source = 'dub'; input.selection.take = null; assert.equal(dubbingTimingView(input).editable, false)
})

test('旧响应缺少时间层仍可显示有效播放范围，不猜测人工覆盖', () => {
  const view = dubbingTimingView({ start: 10, end: 12, playback_start: 9.5, playback_end: 12.5 })
  assert.equal(view.label, '自动提前 0.50 秒'); assert.equal(view.manual, null)
  assert.equal(view.editable, false); assert.deepEqual(view.source, { start: 10, end: 12 })
})

test('播放编辑允许自动完整长度或延长字幕，禁止用更早结束伪造缩短声音', () => {
  assert.equal(playbackDraftError(10, null, 3), '')
  assert.equal(playbackDraftError(10, 15, 3), '')
  assert.match(playbackDraftError(10, 12, 3), /不能早于完整配音结束/)
  assert.match(playbackDraftError(-1, 10, 3), /有效开始时间/)
  assert.match(playbackDraftError(10, null, null), /尚无有效配音/)
  assert.match(playbackDraftError(NaN, 12, 3), /有效开始时间/)
  assert.match(playbackDraftError(10, 11, 1.00003125), /11.001 秒/)
  assert.equal(playbackDraftError(10, 11.001, 1.00003125), '')
})

test('缩时摘要读取实际采用记录而非最后请求，不把正常生成渲染为自动修改', () => {
  const input: any = line()
  input.wav.duration_retry = { attempts: [{ factor: 1 }, { factor: .85 }, { factor: .7 }], used_factor: .85, retries: 2 }
  assert.equal(dubbingFactorSummary(input), '因子 1 → 0.85 · 自动重试 2 次')
  input.wav.duration_retry.used_factor = 1; input.wav.duration_retry.retries = 0
  assert.equal(dubbingFactorSummary(input), '')
  input.selection.take = null; assert.equal(dubbingFactorSummary(input), '')
})
