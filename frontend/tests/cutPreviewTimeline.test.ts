import assert from 'node:assert/strict'
import test from 'node:test'
import { buildCutPreviewTimeline } from '../src/utils/cutPreviewTimeline.ts'

const close = (actual: number, expected: number) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`)
const standard = () => buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: [{ start: 2, end: 4 }] })

test('删除向外按帧对齐，逻辑时长只计保留帧', () => {
  const input = { mediaDuration: 6, fps: 30, deleteRanges: [{ start: 2.411, end: 2.589 }] }
  const before = JSON.stringify(input)
  const value = buildCutPreviewTimeline(input)
  assert.equal(value.valid, true)
  assert.equal(value.empty, false)
  assert.equal(value.frameCount, 180)
  assert.equal(value.keptFrameCount, 174)
  assert.equal(value.duration, 5.8)
  assert.deepEqual(value.deletedRanges, [{ startFrame: 72, endFrame: 78, start: 2.4, end: 2.6 }])
  assert.equal(JSON.stringify(input), before)
})

test('删除区折叠接缝，保留区内进度保持连续', () => {
  const value = standard()
  for (const [media, preview] of [[0, 0], [1.234, 1.234], [2, 2], [3, 2], [4, 2], [4.123, 2.123], [10, 8]]) close(value.fromMedia(media!), preview!)
  close(value.toMedia(1.234), 1.234)
  close(value.toMedia(2.123), 4.123)
  assert.equal(value.toMedia(2), 4)
  close(value.toMedia(2, 'previous'), 59 / 30)
})

test('向前和向后逐帧跨接缝，不重播前一帧或跳掉后一帧', () => {
  const value = standard()
  close(value.stepMedia(59 / 30, 1), 4)
  close(value.stepMedia(4, -1), 59 / 30)
  close(value.stepMedia(4, 1), 121 / 30)
  close(value.stepMedia(2.5, 1), 4)
  close(value.stepMedia(2.5, -1), 59 / 30)
  close(value.stepMedia(2, -1), 59 / 30)
  close(value.stepMedia(2, 1), 4)
  close(value.stepMedia(4, -2), 58 / 30)
})

test('快速连续请求传入上一目标，不依赖浏览器完成seek', () => {
  const value = standard()
  let target = 58 / 30
  const expected = [59 / 30, 4, 121 / 30, 122 / 30]
  for (const next of expected) { target = value.stepMedia(target, 1); close(target, next) }
  for (const next of expected.slice(0, -1).reverse()) { target = value.stepMedia(target, -1); close(target, next) }
})

test('首尾删除和逻辑终点都只定位到真实保留帧', () => {
  const value = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: [{ start: 0, end: 2 }, { start: 8, end: 10 }] })
  assert.equal(value.toMedia(0), 2)
  assert.equal(value.toMedia(0, 'previous'), 2)
  close(value.toMedia(value.duration), 239 / 30)
  close(value.toMedia(100), 239 / 30)
  assert.equal(value.fromMedia(1), 0)
  assert.equal(value.fromMedia(10), 6)
  assert.equal(value.stepMedia(0, 1), 2)
  assert.equal(value.stepMedia(2, -1), 2)
  close(value.stepMedia(10, -1), 239 / 30)
  close(value.stepMedia(239 / 30, 1), 239 / 30)
})

test('半开删除区间恰好对齐帧时不额外删除边缘', () => {
  const value = buildCutPreviewTimeline({ mediaDuration: 1, fps: 30, deleteRanges: [{ start: 10 / 30, end: 20 / 30 }] })
  assert.equal(value.deletedRanges[0]?.startFrame, 10)
  assert.equal(value.deletedRanges[0]?.endFrame, 20)
  assert.equal(value.keptFrameCount, 20)
  close(value.stepMedia(9 / 30, 1), 20 / 30)
  close(value.stepMedia(20 / 30, -1), 9 / 30)
})

test('合并乱序重叠及相邻删除，忽略禁用和无效输入', () => {
  const value = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: [
    { start: 3, end: 4 }, { start: 2, end: 3 }, { start: 2.5, end: 3.5 },
    { start: 5, end: 6, enabled: false }, { start: Number.NaN, end: 7 }, { start: 8, end: 7 },
  ] })
  assert.equal(value.duration, 8)
  assert.deepEqual(value.deletedRanges.map(row => [row.start, row.end]), [[2, 4]])
})

test('越界删除裁到媒体范围，不能形成负帧或超出尾帧', () => {
  const value = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: [{ start: -10, end: 1 }, { start: 9, end: 100 }] })
  assert.deepEqual(value.deletedRanges.map(row => [row.startFrame, row.endFrame]), [[0, 30], [270, 300]])
  assert.equal(value.duration, 8)
})

test('删除全部与不足一帧的残留均显式为空，不误映射到被删帧', () => {
  for (const ranges of [[{ start: 0, end: 10 }], [{ start: 0, end: .005 }, { start: .02, end: 10 }]]) {
    const value = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: ranges })
    assert.equal(value.valid, true)
    assert.equal(value.empty, true)
    assert.equal(value.duration, 0)
    assert.equal(value.fromMedia(5), 0)
    assert.equal(value.toMedia(0), 0)
    assert.equal(value.stepMedia(5, 1), 0)
  }
})

test('分数帧率、多处接缝及长视频尾帧不累积浮点步进误差', () => {
  const fps = 30000 / 1001, count = 60000
  const value = buildCutPreviewTimeline({ mediaDuration: count / fps, fps, deleteRanges: [
    { start: 60 / fps, end: 90 / fps }, { start: 120 / fps, end: 150 / fps },
  ] })
  assert.equal(value.frameCount, count)
  assert.equal(value.keptFrameCount, count - 60)
  close(value.stepMedia(59 / fps, 1), 90 / fps)
  close(value.stepMedia(150 / fps, -1), 119 / fps)
  let current = 0
  for (let index = 0; index < 1000; index++) current = value.stepMedia(current, 1)
  close(current, 1060 / fps)
  close(value.toMedia(value.duration), (count - 1) / fps)
})

test('容器尾部不足一整帧的余量不冒充可定位的额外画面', () => {
  const value = buildCutPreviewTimeline({ mediaDuration: 1.02, fps: 30 })
  assert.equal(value.frameCount, 30)
  assert.equal(value.duration, 1)
  close(value.toMedia(1), 29 / 30)
  assert.equal(value.fromMedia(1.02), 1)
})

test('六位小数时长不会把近整数的最后一帧向下丢掉', () => {
  const value = buildCutPreviewTimeline({ mediaDuration: 1916.133333, fps: 60 })
  assert.equal(value.frameCount, 114968)
  close(value.toMedia(value.duration), 114967 / 60)
  const fps = 30000 / 1001, count = 179
  const fractional = buildCutPreviewTimeline({ mediaDuration: Number((count / fps).toFixed(6)), fps })
  assert.equal(fractional.frameCount, count)
  // 总时长容差不能使不足 .001 帧的真实删除被忽略。
  const strict = buildCutPreviewTimeline({ mediaDuration: 1, fps: 30, deleteRanges: [{ start: 10 / 30 + .00001, end: 20 / 30 + .00001 }] })
  assert.equal(strict.deletedRanges[0]?.startFrame, 10)
  assert.equal(strict.deletedRanges[0]?.endFrame, 21)
})

test('60fps媒体getter微秒舍入后仍可在接缝连续前进，不反复返回第一保留帧', () => {
  const fps = 60, seamFrame = 114254
  const value = buildCutPreviewTimeline({ mediaDuration: 1921.841, fps,
    deleteRanges: [{ start: 1898.55, end: seamFrame / fps }] })
  let observed = Number(((113913 - 1) / fps).toFixed(6))
  for (const frame of [seamFrame, seamFrame + 1, seamFrame + 2, seamFrame + 3]) {
    const target = value.stepMedia(observed, 1)
    close(target, frame / fps)
    observed = Number(target.toFixed(6))
  }
  for (const frame of [seamFrame + 2, seamFrame + 1, seamFrame, 113912]) {
    const target = value.stepMedia(observed, -1)
    close(target, frame / fps)
    observed = Number(target.toFixed(6))
  }
})

test('分数fps微秒媒体读数也按最近整数帧继续，普通帧内时间仍向下取当前帧', () => {
  const fps = 30000 / 1001
  const value = buildCutPreviewTimeline({ mediaDuration: 60000 / fps, fps,
    deleteRanges: [{ start: 100 / fps, end: 200 / fps }] })
  let observed = Number((99 / fps).toFixed(6))
  for (const frame of [200, 201, 202, 203]) {
    const target = value.stepMedia(observed, 1)
    close(target, frame / fps)
    observed = Number(target.toFixed(6))
  }
  close(value.stepMedia(250.5 / fps, 1), 251 / fps)
  close(value.stepMedia(250.5 / fps, -1), 249 / fps)
})

test('无效时长帧率或溢出输入不抛错，映射始终有限', () => {
  for (const [mediaDuration, fps] of [[0, 30], [-1, 30], [1, 0], [1, -30], [Infinity, 30], [1, NaN], [1e20, 30]]) {
    const value = buildCutPreviewTimeline({ mediaDuration: mediaDuration!, fps: fps! })
    assert.equal(value.valid, false)
    assert.equal(value.empty, true)
    assert.equal(value.duration, 0)
    assert.ok(value.error)
    assert.equal(value.fromMedia(Infinity), 0)
    assert.equal(value.toMedia(NaN), 0)
    assert.equal(value.stepMedia(NaN, Infinity), 0)
  }
  const value = standard()
  assert.equal(value.fromMedia(NaN), 0)
  assert.equal(value.toMedia(Infinity), 0)
  assert.equal(value.stepMedia(NaN, 1), 1 / 30)
})

test('构建结果与时间范围不可变，各次请求互不污染', () => {
  const value = standard()
  assert.ok(Object.isFrozen(value))
  assert.ok(Object.isFrozen(value.deletedRanges))
  assert.ok(Object.isFrozen(value.keptRanges[0]))
  const first = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30 })
  assert.equal(first.duration, 10)
  assert.equal(value.duration, 8)
})
