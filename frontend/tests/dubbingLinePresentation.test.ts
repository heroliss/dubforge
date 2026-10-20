import assert from 'node:assert/strict'
import test from 'node:test'
import { dubbingStatusLabel, hasPendingDubbingChanges, pendingDubbingChangeReason } from '../src/utils/dubbingLinePresentation.ts'

test('选中配音版本过期时明确说明过期且预览无声', () => {
  assert.equal(dubbingStatusLabel({ wav: { exists: false, stale: true } }, {
    silent: true, emptySlot: false,
  }), '配音过期 · 预览无声')
})

test('主动选择空位仍显示人声静音', () => {
  assert.equal(dubbingStatusLabel({ wav: { exists: false, stale: false } }, {
    silent: true, emptySlot: true,
  }), '人声静音')
})

test('正常有效配音和失败优先级保持不变', () => {
  assert.equal(dubbingStatusLabel({ wav: { exists: true, stale: false }, confirmed: true }, {
    silent: false, emptySlot: false,
  }), '已满意')
  assert.equal(dubbingStatusLabel({ wav: { exists: true, stale: true } }, {
    error: '服务失败', silent: true, emptySlot: false,
  }), '合成失败')
})

test('老后端仅选中有效配音且参数不同时回退为配音待更新', () => {
  const line = () => ({ selection: { source: 'dub', take: 1 },
    wav: { exists: true, stale: false, settings_changed: true } })
  assert.equal(hasPendingDubbingChanges(line()), true)
  assert.equal(pendingDubbingChangeReason(line()), '配音参数已修改')
  assert.equal(hasPendingDubbingChanges({ ...line(), confirmed: true }), true,
    '满意与参数是否应用是独立状态，不能互相隐藏')
  for (const value of [undefined, null, {}, { wav: line().wav },
    { ...line(), selection: { source: 'original', take: 1 } },
    { ...line(), selection: { source: 'dub', take: null } },
    ...[0, -1, 1.5, '1', NaN].map(take => ({ ...line(), selection: { source: 'dub', take } })),
    { ...line(), wav: { ...line().wav, exists: false } },
    { ...line(), wav: { ...line().wav, stale: true } },
    { ...line(), wav: { ...line().wav, settings_changed: false } },
    { ...line(), wav: undefined },
  ]) assert.equal(hasPendingDubbingChanges(value), false, JSON.stringify(value))
})

test('旧文字配音不可播放但文件仍在时，明确文字变更也提示配音待更新', () => {
  const line = { selection: { source: 'dub', take: 1 },
    wav: { file_exists: true, exists: false, stale: true, settings_changed: false,
      changes_pending: true, change_reasons: ['朗读文字已修改'] } }
  assert.equal(hasPendingDubbingChanges(line), true)
  assert.equal(pendingDubbingChangeReason(line), '朗读文字已修改')
  assert.equal(dubbingStatusLabel(line, { silent: true, emptySlot: false }), '配音过期 · 预览无声')
})

test('有效旧声音仍可沿用，参数和参考音频变化统一显示原因', () => {
  const line = { selection: { source: 'dub', take: 2 },
    wav: { file_exists: true, exists: true, stale: false, changes_pending: true,
      change_reasons: ['配音参数已修改', '参考音频已变化'] } }
  assert.equal(hasPendingDubbingChanges(line), true)
  assert.equal(pendingDubbingChangeReason(line), '配音参数已修改；参考音频已变化')
  assert.equal(dubbingStatusLabel(line, { silent: false, emptySlot: false }), '已合成')
})

test('原声、空槽和物理文件缺失不显示配音待更新', () => {
  const wav = { file_exists: true, exists: false, stale: true, changes_pending: true,
    change_reasons: ['朗读文字已修改'] }
  for (const line of [
    { selection: { source: 'original', take: 1 }, wav },
    { selection: { source: 'silent', take: 1 }, wav },
    { selection: { source: 'dub', take: null }, wav },
    { selection: { source: 'dub', take: 0 }, wav },
    { selection: { source: 'dub', take: 1 }, wav: { ...wav, file_exists: false } },
    { selection: { source: 'dub', take: 1 }, wav: { ...wav, file_exists: false, exists: true } },
  ]) {
    assert.equal(hasPendingDubbingChanges(line), false)
    assert.equal(pendingDubbingChangeReason(line), '')
  }
})

test('后端明确没有待更新时，兼容字段不能重新制造警告', () => {
  const line = { selection: { source: 'dub', take: 1 },
    wav: { file_exists: true, exists: true, stale: false, settings_changed: true,
      changes_pending: false, change_reasons: ['配音参数已修改'], reading_caption_independent: true } }
  assert.equal(hasPendingDubbingChanges(line), false)
  assert.equal(pendingDubbingChangeReason(line), '')
})

test('固定自定义朗读仅改字幕时遵从后端无配音变化的结论', () => {
  const line = { text: '修改后的字幕', selection: { source: 'dub', take: 1 },
    wav: { file_exists: true, exists: true, stale: false, changes_pending: false,
      change_reasons: [], reading_caption_independent: true } }
  assert.equal(hasPendingDubbingChanges(line), false)
  assert.equal(dubbingStatusLabel(line, { silent: false, emptySlot: false }), '已合成')
})

test('固定自定义朗读本身修改后仍正常显示待更新', () => {
  const line = { selection: { source: 'dub', take: 1 },
    wav: { file_exists: true, exists: false, stale: true, changes_pending: true,
      change_reasons: ['朗读文字已修改'], reading_caption_independent: true } }
  assert.equal(hasPendingDubbingChanges(line), true)
  assert.equal(pendingDubbingChangeReason(line), '朗读文字已修改')
})

test('未知过期原因不误称文字变化或参数变化', () => {
  for (const wav of [
    { file_exists: true, exists: false, stale: true, settings_changed: true },
    { file_exists: true, exists: false, stale: true, changes_pending: true },
    { file_exists: true, exists: false, stale: true, changes_pending: true, change_reasons: [] },
    { exists: false, stale: true, changes_pending: true, change_reasons: ['朗读文字已修改'] },
  ]) {
    const line = { selection: { source: 'dub', take: 1 }, wav }
    assert.equal(hasPendingDubbingChanges(line), false)
    assert.equal(pendingDubbingChangeReason(line), '')
  }
})

test('原因只展示非空中文文本并去重，不修改后端快照', () => {
  const reasons: unknown[] = ['  句子时间已修改 ', null, '配音参数已修改', '', 3, '句子时间已修改']
  const line = { selection: { source: 'dub', take: 1 },
    wav: { file_exists: true, exists: false, stale: true, changes_pending: true, change_reasons: reasons } }
  assert.equal(pendingDubbingChangeReason(line), '句子时间已修改；配音参数已修改')
  assert.equal(reasons.length, 6)
  assert.equal(reasons[0], '  句子时间已修改 ')
})

test('新后端有效配音暂无细分原因时保留简洁的参数提示', () => {
  const line = { selection: { source: 'dub', take: 1 },
    wav: { file_exists: true, exists: true, stale: false, changes_pending: true } }
  assert.equal(hasPendingDubbingChanges(line), true)
  assert.equal(pendingDubbingChangeReason(line), '配音参数已修改')
})
