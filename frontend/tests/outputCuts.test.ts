import assert from 'node:assert/strict'
import test from 'node:test'
import { buildVirtualPreviewSegments, cloneCutRanges, formatCutTime, mapOutputCutChapters, mapSourceIntervalsToOutput, mergeCutRanges, nextRetainedTime, outputToSourceTime, parseCutTime, retainedOutputSegments, sourceToOutputTime, subtractCutIntervals, validateCutRanges, type OutputCutRange } from '../src/utils/outputCuts.ts'

const range = (start: number | null, end: number | null, extra: Partial<OutputCutRange> = {}): OutputCutRange => ({ id: 'cut', start, end, reason: '', enabled: true, ...extra })

test('剪辑时间支持秒数和时分秒，拒绝模糊和越界输入', () => {
  assert.equal(parseCutTime('331.125'), 331.125)
  assert.equal(parseCutTime('05:31.125'), 331.125)
  assert.equal(parseCutTime('01:05:31.125'), 3931.125)
  for (const value of ['', '-1', 'Infinity', '1:60', '1:00:60', '1.5:00', '1e3', 'abc']) assert.equal(parseCutTime(value), null)
  assert.equal(formatCutTime(59.9996), '00:01:00.000')
  assert.equal(formatCutTime(3931.125), '01:05:31.125')
  assert.equal(formatCutTime(null), '—')
})

test('乱序重叠与首尾相连范围合并，不计禁用行、不重复计时、不修改草稿', () => {
  const rows = [range(30, 40), range(5, 15), range(10, 20), range(20, 30), range(0, 2, { enabled: false })]
  const before = cloneCutRanges(rows)
  assert.deepEqual(mergeCutRanges(rows, 60), [{ start: 5, end: 40 }])
  assert.deepEqual(rows, before)
  assert.deepEqual(mergeCutRanges(rows, 60, false), [])
  assert.deepEqual(mergeCutRanges(rows, null), [])
})

test('无效和整片删除无法保存；允许清空方案以及只禁用全部范围', () => {
  assert.match(validateCutRanges([range(null, 5)], 60).join(), /有效/)
  assert.match(validateCutRanges([range(5, 5)], 60).join(), /晚于/)
  assert.match(validateCutRanges([range(0, 61)], 60).join(), /原视频范围/)
  assert.match(validateCutRanges([range(0, 30), range(30, 60)], 60).join(), /至少保留/)
  assert.deepEqual(validateCutRanges([], 60), [])
  assert.deepEqual(validateCutRanges([range(0, 60)], 60, false), [])
  assert.match(validateCutRanges([], null).join(), /就绪/)
  assert.match(validateCutRanges(Array.from({ length: 201 }, () => range(0, 1)), 60).join(), /200/)
})

test('跳过删除段只给出下一个保留点，结束边界和保留区间不跳', () => {
  const cuts = [{ start: 5, end: 12 }, { start: 30, end: 40 }]
  assert.equal(nextRetainedTime(5, cuts), 12)
  assert.equal(nextRetainedTime(11.9, cuts), 12)
  assert.equal(nextRetainedTime(12, cuts), 12)
  assert.equal(nextRetainedTime(25, cuts), 25)
})

test('当前成片试听只映射草稿相对已生成方案的新增删除段', () => {
  const base = [{ start: 5, end: 10 }, { start: 30, end: 40 }]
  const draft = [{ start: 5, end: 12 }, { start: 20, end: 22 }, { start: 30, end: 40 }]
  assert.deepEqual(subtractCutIntervals(draft, base), [{ start: 10, end: 12 }, { start: 20, end: 22 }])
  const snapshot = { source_duration: 100, ranges: base }
  // 10–12 在成片接缝 5–10 后映射为 5–7；第二段保持 15–17。
  assert.deepEqual(mapSourceIntervalsToOutput(subtractCutIntervals(draft, base), snapshot), [
    { start: 5, end: 7 }, { start: 15, end: 17 },
  ])
})

test('虚拟试听按源时间拼接当前成片与恢复来源，并跳过草稿删除段', () => {
  const base = { source_duration: 20, ranges: [{ start: 5, end: 10 }] }
  const draft = { source_duration: 20, ranges: [{ start: 12, end: 15 }] }
  assert.deepEqual(buildVirtualPreviewSegments(20, base, draft), [
    { source_start: 0, source_end: 5, media_kind: 'target', media_start: 0, media_end: 5, media_version_id: null, virtual_start: 0, virtual_end: 5, available: true },
    { source_start: 5, source_end: 10, media_kind: 'source', media_start: 5, media_end: 10, media_version_id: null, virtual_start: 5, virtual_end: 10, available: true },
    { source_start: 10, source_end: 12, media_kind: 'target', media_start: 5, media_end: 7, media_version_id: null, virtual_start: 10, virtual_end: 12, available: true },
    { source_start: 15, source_end: 20, media_kind: 'target', media_start: 10, media_end: 15, media_version_id: null, virtual_start: 12, virtual_end: 17, available: true },
  ])
})

test('虚拟试听会标出恢复来源自身缺失的区间，不静默回退到错误成片', () => {
  const base = { source_duration: 20, ranges: [{ start: 5, end: 10 }] }
  const draft = { source_duration: 20, ranges: [] }
  const donor = { kind: 'version' as const, version_id: 'v2', cuts: { source_duration: 20, ranges: [{ start: 7, end: 8 }] } }
  assert.deepEqual(buildVirtualPreviewSegments(20, base, draft, donor).filter(row => row.source_start >= 5 && row.source_end <= 10), [
    { source_start: 5, source_end: 7, media_kind: 'version', media_version_id: 'v2', media_start: 5, media_end: 7, virtual_start: 5, virtual_end: 7, available: true },
    { source_start: 7, source_end: 8, media_kind: 'unavailable', media_version_id: 'v2', media_start: null, media_end: null, virtual_start: 7, virtual_end: 8, available: false },
    { source_start: 8, source_end: 10, media_kind: 'version', media_version_id: 'v2', media_start: 7, media_end: 9, virtual_start: 8, virtual_end: 10, available: true },
  ])
})

test('源时间轴与成片时间轴双向映射在多段删除和接缝处保持一致', () => {
  const snapshot = { source_duration: 100, output_duration: 70, ranges: [{ start: 40, end: 60 }, { start: 10, end: 20 }] }
  assert.deepEqual(retainedOutputSegments(snapshot), [
    { start: 0, end: 10, output_start: 0, output_end: 10 },
    { start: 20, end: 40, output_start: 10, output_end: 30 },
    { start: 60, end: 100, output_start: 30, output_end: 70 },
  ])
  assert.equal(sourceToOutputTime(snapshot, 5), 5)
  assert.equal(sourceToOutputTime(snapshot, 10), 10)
  assert.equal(sourceToOutputTime(snapshot, 15), 10)
  assert.equal(sourceToOutputTime(snapshot, 20), 10)
  assert.equal(sourceToOutputTime(snapshot, 50), 30)
  assert.equal(sourceToOutputTime(snapshot, 60), 30)
  assert.equal(sourceToOutputTime(snapshot, 99), 69)
  assert.equal(outputToSourceTime(snapshot, 5), 5)
  assert.equal(outputToSourceTime(snapshot, 10), 20)
  assert.equal(outputToSourceTime(snapshot, 10, 'previous'), 10)
  assert.equal(outputToSourceTime(snapshot, 30), 60)
  assert.equal(outputToSourceTime(snapshot, 30, 'previous'), 40)
  assert.equal(outputToSourceTime(snapshot, 69), 99)
})

test('反向映射会钳制到成片边界，首尾删除仍能定位到第一或最后保留帧', () => {
  const snapshot = { source_duration: 100, ranges: [{ start: 0, end: 10 }, { start: 90, end: 100 }] }
  assert.deepEqual(retainedOutputSegments(snapshot), [{ start: 10, end: 90, output_start: 0, output_end: 80 }])
  assert.equal(sourceToOutputTime(snapshot, -2), 0)
  assert.equal(sourceToOutputTime(snapshot, 4), 0)
  assert.equal(sourceToOutputTime(snapshot, 96), 80)
  assert.equal(outputToSourceTime(snapshot, -2), 10)
  assert.equal(outputToSourceTime(snapshot, 0), 10)
  assert.equal(outputToSourceTime(snapshot, 80), 90)
  assert.equal(outputToSourceTime(snapshot, 81), 90)
})

test('无剪辑或无效输入不制造额外偏移', () => {
  assert.deepEqual(retainedOutputSegments(null), [])
  assert.equal(sourceToOutputTime(null, 12.5), 12.5)
  assert.equal(outputToSourceTime(null, 12.5), 12.5)
  assert.equal(sourceToOutputTime({ source_duration: 100, ranges: [] }, 12.5), 12.5)
  assert.equal(outputToSourceTime({ source_duration: 100, ranges: [] }, 12.5), 12.5)
  assert.equal(sourceToOutputTime({ source_duration: 100, ranges: [{ start: 20, end: 10 }] }, 12.5), 12.5)
  assert.equal(sourceToOutputTime({ source_duration: 100, ranges: [{ start: 20, end: 10 }] }, Number.NaN), null)
})

const cuts = { source_duration: 100, output_duration: 70, ranges: [{ start: 10, end: 20 }, { start: 40, end: 60 }] }

test('成片章节以保留区间求交并移除完整删除章，跨删除区间保持完整剩余章节', () => {
  const chapters = [
    { title: '开场', start_time: 0, end_time: 10 },
    { title: '广告', start_time: 10, end_time: 20 },
    { title: '正文', start_time: 20, end_time: 50 },
    { title: '结尾', start_time: 50 },
  ]
  const before = JSON.stringify(chapters)
  assert.deepEqual(mapOutputCutChapters(chapters, cuts), [
    { title: '开场', start_time: 0, end_time: 10 },
    { title: '正文', start_time: 10, end_time: 30 },
    { title: '结尾', start_time: 30, end_time: 70 },
  ])
  assert.equal(JSON.stringify(chapters), before)
  assert.deepEqual(mapOutputCutChapters([{ title: '跨越两个删除段', start_time: 5, end_time: 65 }], cuts), [
    { title: '跨越两个删除段', start_time: 5, end_time: 35 },
  ])
})

test('缺结束时间按排序后下一章或原片终点补齐，删除前后及首尾章位置正确', () => {
  const snapshot = { source_duration: 100, ranges: [{ start: 0, end: 10 }, { start: 90, end: 100 }] }
  assert.deepEqual(mapOutputCutChapters([
    { title: '尾部广告', start_time: 90 }, { title: '正文后半', start_time: 40, end_time: null },
    { title: '开头广告', start_time: 0 }, { title: '正文前半', start_time: 10 },
  ], snapshot), [
    { title: '正文前半', start_time: 0, end_time: 30 },
    { title: '正文后半', start_time: 30, end_time: 80 },
  ])
})

test('章节兼容旧start/end字段并保留标题与附加信息，无剪辑时不排序且复制对象', () => {
  const chapter = { title: '旧格式', start: 30, end: 70, translated: true }
  assert.deepEqual(mapOutputCutChapters([chapter], cuts), [{ title: '旧格式', start: 20, end: 40, translated: true, start_time: 20, end_time: 40 }])
  const original = [{ title: '后', start_time: 50 }, { title: '前', start_time: 0 }]
  const copied = mapOutputCutChapters(original, null)
  assert.deepEqual(copied, original); assert.notEqual(copied, original); assert.notEqual(copied[0], original[0])
})

test('开始位于删除段的残余章从接缝开始，重复接缝章不产生重叠标记', () => {
  assert.deepEqual(mapOutputCutChapters([
    { title: '保留主章', start_time: 12, end_time: 30 },
    { title: '重复起点', start_time: 15, end_time: 35 },
    { title: '后续', start_time: 30, end_time: 40 },
  ], cuts), [
    { title: '保留主章', start_time: 10, end_time: 20 },
    { title: '后续', start_time: 20, end_time: 30 },
  ])
})
