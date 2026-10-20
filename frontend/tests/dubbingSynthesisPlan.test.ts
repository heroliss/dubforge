import assert from 'node:assert/strict'
import test from 'node:test'
import {
  analyzeDubbingSynthesis, dubbingSlots, dubbingReservationSlots, mergeDubbingReservations, dubbingSynthesisSignature, sentenceRanges,
  type SynthesisLine,
} from '../src/utils/dubbingSynthesis.ts'

function line(id: number, take: number | null, versions: number[] = []): SynthesisLine {
  return { id, selection: { source: 'dub', take }, versions: versions.map(id => ({ id })),
    text: `译文 ${id}`, start: id * 3, end: id * 3 + 2, translation_available: true }
}

test('新槽依据本次所选句子的最大槽号，批量使用共同槽号并保留旧版本', () => {
  const lines = [line(1, 1, [1, 2]), line(2, 4, [1, 4]), line(3, null), line(4, 7)]
  const original = structuredClone(lines)
  assert.deepEqual(dubbingSlots(lines), [1, 2, 4, 7])
  const plan = analyzeDubbingSynthesis(lines, [3, 1, 1, 2], { mode: 'new' })
  assert.deepEqual(plan.ids, [1, 2, 3])
  assert.deepEqual(plan.takes, { 1: 5, 2: 5, 3: 5 })
  assert.equal(plan.newSlot, 5)
  assert.equal(plan.create, 3)
  assert.equal(plan.overwrite, 0)
  assert.equal(plan.confirmed, 0)
  assert.equal(plan.all, false)
  assert.deepEqual(analyzeDubbingSynthesis(lines, [3]).takes, { 3: 1 })
  assert.deepEqual(lines, original)
})

test('单句只有槽 1 时新建槽 2，其他句子的槽位不会占用本句编号', () => {
  const lines = [line(1, 1, [1]), line(2, 2, [1, 2])]
  assert.deepEqual(analyzeDubbingSynthesis(lines, [1], { mode: 'new' }).takes, { 1: 2 })
  assert.deepEqual(analyzeDubbingSynthesis(lines, [1, 2], { mode: 'new' }).takes, { 1: 3, 2: 3 })
  assert.equal(analyzeDubbingSynthesis(lines, [1]).destination, '当前配音槽')
})

test('未选择句子的槽 10000 不阻止本句生成，所选范围仍保留槽号上限', () => {
  const lines = [line(1, 1, [1]), line(2, 10000, [10000])]
  const single = analyzeDubbingSynthesis(lines, [1], { mode: 'new' })
  assert.equal(single.newSlot, 2)
  assert.equal(single.invalidReason, '')
  assert.match(analyzeDubbingSynthesis(lines, [1, 2], { mode: 'new' }).invalidReason, /达到上限/)
})

test('所选句子的待生成槽计入新槽编号，历史空位不复用', () => {
  const lines = [line(1, 1, [1]), line(2, 4, [1, 4])]
  assert.equal(analyzeDubbingSynthesis(lines, [1], { mode: 'new' }, [2]).newSlot, 3)
  assert.equal(analyzeDubbingSynthesis(lines, [2], { mode: 'new' }).newSlot, 5)
})

test('按句号筛选待生成预留槽并忽略错误记录', () => {
  const reservations = { 1: [3, 2, 3, 0, -1, 1.5, 10001, '4'], 2: [10000], 3: '4' }
  assert.deepEqual(dubbingReservationSlots(reservations, [1, 1, -1, 1.5, 3, 99]), [2, 3])
  assert.deepEqual(dubbingReservationSlots(reservations, [1, 2]), [2, 3, 10000])
  for (const invalid of [null, undefined, [], 1, '1']) assert.deepEqual(dubbingReservationSlots(invalid, [1]), [])
})

test('接收回执并入逐句预留槽，保持原数据不变并丢弃无效项', () => {
  const reservations = { 1: [3, 2, 3], 2: [10000], bad: [1], '01': [8], 3: [10001] }
  const original = structuredClone(reservations)
  const merged = mergeDubbingReservations(reservations, [{ id: 1, take: 4 }, { id: 1, take: 2 },
    { id: 4, take: 1 }, { id: 0, take: 1 }, { id: 2, take: 10001 }, { id: 4, take: 1.5 }])
  assert.deepEqual(merged, { 1: [2, 3, 4], 2: [10000], 4: [1] })
  assert.deepEqual(reservations, original)
  assert.notEqual(merged[1], reservations[1])
  assert.deepEqual(mergeDubbingReservations(null, [{ id: 1, take: 2 }]), { 1: [2] })
})

test('当前槽计划准确区分覆盖、补齐和新增，并保留原声及句级设置摘要', () => {
  const lines = [line(1, 1, [1, 2]), line(2, 3), line(3, null)]
  lines[0].versions![0].confirmed = true
  lines[0].customized = true
  lines[2].selection.source = 'original'
  const plan = analyzeDubbingSynthesis(lines, [1, 2, 3], { mode: 'current' })
  assert.deepEqual(plan.takes, { 1: 1, 2: 3, 3: 4 })
  assert.equal(plan.overwrite, 1)
  assert.equal(plan.refill, 1)
  assert.equal(plan.create, 1)
  assert.equal(plan.confirmed, 1)
  assert.equal(plan.customized, 1)
  assert.equal(plan.original, 1)
  assert.equal(plan.destination, '各句当前配音槽')
  assert.equal(plan.all, true)
})

test('指定槽满意数量使用目标版本的记录，不使用当前句子满意状态', () => {
  const lines = [line(1, 1, [1, 2]), line(2, 1, [1, 2]), line(3, null)]
  lines[0].confirmed = true
  lines[0].versions![1].confirmed = false
  lines[1].confirmed = false
  lines[1].versions![1].confirmed = true
  const plan = analyzeDubbingSynthesis(lines, [1, 2, 3], { mode: 'slot', slot: 2 })
  assert.deepEqual(plan.takes, { 1: 2, 2: 2, 3: 2 })
  assert.equal(plan.overwrite, 2)
  assert.equal(plan.create, 1)
  assert.equal(plan.refill, 0)
  assert.equal(plan.confirmed, 1)
  assert.equal(plan.destination, '配音槽 2')
  // 老数据没有逐版本满意字段时，仅当前选中的同一槽可使用句级标记。
  delete lines[0].versions![0].confirmed
  assert.equal(analyzeDubbingSynthesis(lines, [1], { mode: 'current' }).confirmed, 1)
})

test('槽位边界拒绝无效目标，最大槽存在时仍允许覆盖已有槽', () => {
  const lines = [line(1, 10000, [10000]), line(2, null)]
  const newlyAllocated = analyzeDubbingSynthesis(lines, [1, 2], { mode: 'new' })
  assert.equal(newlyAllocated.newSlot, 10001)
  assert.match(newlyAllocated.invalidReason, /达到上限/)
  assert.match(analyzeDubbingSynthesis(lines, [1, 2], { mode: 'current' }).invalidReason, /达到上限/)
  assert.equal(analyzeDubbingSynthesis(lines, [1], { mode: 'current' }).invalidReason, '')
  const overwrite = analyzeDubbingSynthesis(lines, [1, 2], { mode: 'slot', slot: 10000 })
  assert.equal(overwrite.invalidReason, '')
  assert.equal(overwrite.overwrite, 1)
  assert.equal(overwrite.create, 1)
  for (const slot of [null, undefined, 0, -1, 1.5, 10001, Number.NaN]) {
    assert.match(analyzeDubbingSynthesis(lines, [1], { mode: 'slot', slot }).invalidReason, /有效的目标/)
  }
})

test('项目槽清单去重排序，忽略错误槽号，保留仅选择但还没有音频的槽', () => {
  const lines = [line(1, 4, [2, 4, 2, 0, -3, 1.5, 10001]), line(2, 10000), line(3, null)]
  assert.deepEqual(dubbingSlots(lines), [2, 4, 10000])
  assert.deepEqual(dubbingSlots([]), [])
  assert.equal(analyzeDubbingSynthesis([line(1, null)], [1]).newSlot, 1)
})

test('待核对、缺译文和未就绪内容保留在范围中，调用者必须处理而非默默跳过', () => {
  const lines = [line(1, null), line(2, null), line(3, null), line(4, null)]
  lines[0].translation_available = false
  lines[1].edit_stale = true
  lines[2].unready_inputs = ['split']
  const plan = analyzeDubbingSynthesis(lines, [4, 3, 2, 1, 1, 99])
  assert.deepEqual(plan.ids, [1, 2, 3, 4])
  assert.deepEqual(plan.blockedIds, [1, 2, 3])
  assert.equal(plan.total, 4)
  assert.equal(plan.create, 4)
  assert.equal(analyzeDubbingSynthesis([], []).all, false)
})

test('确认签名发现选中内容、声源、音频和满意变化，并发现范围外占用新槽', () => {
  const original = [line(1, 1, [1]), line(2, 1, [1])]
  const baseline = dubbingSynthesisSignature(original, [1])
  const changes = [
    (lines: SynthesisLine[]) => { lines[0].text = '新译文' },
    (lines: SynthesisLine[]) => { lines[0].start = 8 },
    (lines: SynthesisLine[]) => { lines[0].end = 9 },
    (lines: SynthesisLine[]) => { lines[0].selection.source = 'original' },
    (lines: SynthesisLine[]) => { lines[0].selection.take = 3 },
    (lines: SynthesisLine[]) => { lines[0].versions![0].url = '/changed.wav' },
    (lines: SynthesisLine[]) => { lines[0].versions![0].confirmed = true },
    (lines: SynthesisLine[]) => { lines[0].confirmed = true },
    (lines: SynthesisLine[]) => { lines[1].versions!.push({ id: 2 }) },
  ]
  for (const change of changes) {
    const lines = structuredClone(original); change(lines)
    assert.notEqual(dubbingSynthesisSignature(lines, [1]), baseline)
  }
  const irrelevant = structuredClone(original)
  irrelevant[1].text = '范围外译文'
  assert.equal(dubbingSynthesisSignature(irrelevant, [1]), baseline)
  assert.equal(dubbingSynthesisSignature(original, [1, 1]), baseline)
})

test('句子范围压缩连续编号，保留离散编号，避免几百句确认内容刷屏', () => {
  assert.equal(sentenceRanges([4, 1, 2, 2, 3, 8, 10, 11]), '1–4、8、10–11')
  assert.equal(sentenceRanges(Array.from({ length: 249 }, (_, index) => index + 1)), '1–249')
  assert.equal(sentenceRanges([5]), '5')
  assert.equal(sentenceRanges([]), '')
})
