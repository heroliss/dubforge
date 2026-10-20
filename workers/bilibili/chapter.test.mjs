import test from 'node:test'
import assert from 'node:assert/strict'
import { chapterOperation, normalizeChapterCards } from './chapter.mjs'
import { safeError } from './protocol.mjs'

const rules = {limit: 10, stay: 5, space: 0}
const card = (from, to, content = '章节') => ({from, to, content})
const tenChapters = () => [card(0, 1, '开场'), ...Array.from({length: 9}, (_, i) => card(1 + i * 5, 6 + i * 5, `章节${i + 2}`))]

function fixture(currentRules = rules) {
  const calls = []
  const payload = {bvid: 'BV1234567890', aid: 10, cid: 1, account_id: '123'}
  const platform = {
    getArchive: async () => ({archive: {bvid: payload.bvid, aid: 10, mid: 123}, videos: [{cid: 1, duration: 120}]}),
    request: {
      get: async url => url.includes('/viewpoint/rule') ? {chapters: currentRules} : {catalog: []},
      post: async (...args) => {calls.push(args); return {data: {code: 0}}},
    },
  }
  const run = (operation, extra = {}) => chapterOperation(platform, operation, {...payload, ...extra}, {bili_jct: 'test-only'})
  return {run, calls, platform}
}

test('10个章节卡片对应9个内部分界，首章仍从0秒开始且允许仅1秒', () => {
  const result = normalizeChapterCards(tenChapters(), 46, rules)
  assert.equal(result.length, 10)
  assert.equal(result[0].from, 0)
  assert.equal(result[0].to, 1)
  assert.equal(result.length - 1, 9)
})

test('11章在写入前拒绝，错误明确当前数量和实时上限', async () => {
  const {run, calls} = fixture()
  const rows = [...tenChapters(), card(46, 51, '最后章节')]
  await assert.rejects(run('studio_chapter_prepare', {cards: rows}), error => {
    const result = safeError(error)
    assert.equal(result.code, 'PUBLISH_CHAPTER_LIMIT_EXCEEDED')
    assert.deepEqual(result.detail, {stage: 'validate', count: 11, limit: 10})
    assert.match(result.message, /当前有11个章节/)
    assert.match(result.message, /最多允许10个章节（9个内部分界点）/)
    return true
  })
  assert.equal(calls.length, 0)
})

test('平台实时数量规则仍优先，不把不同账号一律固定成10章', () => {
  const rows = [...tenChapters(), card(46, 51, '第十一章')]
  assert.equal(normalizeChapterCards(rows, 51, {...rules, limit: 12}).length, 11)
  assert.throws(() => normalizeChapterCards(tenChapters(), 46, {...rules, limit: 9}), error =>
    error.code === 'PUBLISH_CHAPTER_LIMIT_EXCEEDED' && error.detail.limit === 9)
})

test('后续章节不足动态最短时长时指出具体章节，不能误套首章1秒例外', () => {
  assert.throws(() => normalizeChapterCards([card(0, 1, '开场'), card(1, 5, '过短章节')], 5, rules), error => {
    assert.equal(error.code, 'PUBLISH_CHAPTER_TIME')
    assert.deepEqual(error.detail, {stage: 'validate', index: 2, minimum_seconds: 5, actual_seconds: 4})
    assert.match(safeError(error).message, /第2个章节时长为4秒，至少需要5秒/)
    return true
  })
  assert.equal(normalizeChapterCards([card(0, 1), card(1, 6)], 6, rules).length, 2)
  assert.throws(() => normalizeChapterCards([card(0, 1), card(1, 6)], 6, {...rules, stay: 6}),
    error => error.code === 'PUBLISH_CHAPTER_TIME' && error.detail.minimum_seconds === 6)
})

test('分P末章先规范到真实结尾，再应用5秒时长检查', () => {
  assert.equal(normalizeChapterCards([card(0, 1), card(1, 3)], 6, rules).at(-1).to, 6)
  assert.throws(() => normalizeChapterCards([card(0, 1), card(1, 6)], 5, rules),
    error => error.code === 'PUBLISH_CHAPTER_TIME' && error.detail.actual_seconds === 4)
  assert.throws(() => normalizeChapterCards([card(0, 10), card(10, 11)], 10, rules),
    error => error.code === 'PUBLISH_CHAPTER_TIME')
})

test('标题按官方JS表单16字符边界校验，不能用码点数放过超长emoji', () => {
  assert.equal(normalizeChapterCards([card(0, 10, '中'.repeat(16))], 10, rules)[0].content.length, 16)
  assert.equal(normalizeChapterCards([card(0, 10, '😀'.repeat(8))], 10, rules)[0].content.length, 16)
  for (const content of ['中'.repeat(17), '😀'.repeat(9)]) {
    assert.throws(() => normalizeChapterCards([card(0, 10, content)], 10, rules), error => {
      assert.equal(error.code, 'PUBLISH_CHAPTER_TITLE_TOO_LONG')
      assert.equal(error.detail.index, 1)
      assert.equal(error.detail.max_title_length, 16)
      assert.match(safeError(error).message, /第1个章节标题超过16字符/)
      return true
    })
  }
})

test('原有读路径不因新限制拒绝远端旧标题或首个非0卡片，单章与清空仍可用', () => {
  assert.equal(normalizeChapterCards([card(5, 10, '旧'.repeat(17))]).length, 1)
  assert.equal(normalizeChapterCards([card(5, 10)], 10, rules)[0].from, 5)
  assert.equal(normalizeChapterCards([card(0, 10)], 10, rules).length, 1)
  assert.deepEqual(normalizeChapterCards([], 10, {...rules, limit: 0}), [])
})

test('重叠与平台间隔规则使用明确错误，不泛化成未知写入状态', () => {
  assert.throws(() => normalizeChapterCards([card(0, 10), card(9, 20)], 20, rules),
    error => error.code === 'PUBLISH_CHAPTER_OVERLAP' && error.detail.index === 2)
  assert.throws(() => normalizeChapterCards([card(0, 10), card(11, 20)], 20, {...rules, space: 2}),
    error => error.code === 'PUBLISH_CHAPTER_INTERVAL' && error.detail.actual_seconds === 1)
})

test('确认后若修改为过短章节，在真正发请求前再次拒绝并保留边界错误', async () => {
  const {run, calls} = fixture()
  const preview = await run('studio_chapter_prepare', {cards: [card(0, 1), card(1, 120)]})
  await assert.rejects(run('studio_chapter_write', {confirmed: true, prepared: preview.snapshot,
    cards: [card(0, 116), card(116, 120)]}), error => error.code === 'PUBLISH_CHAPTER_TIME')
  assert.equal(calls.length, 0)
})

test('非法规则数量不会被Number布尔值强转成允许写入的规则', async () => {
  for (const limit of [true, false, '', ' 10 ', -1, 1.5]) {
    const {run, calls} = fixture({...rules, limit})
    await assert.rejects(run('studio_chapter_prepare', {cards: [card(0, 120)]}),
      error => error.code === 'PUBLISH_CHAPTER_RULE_UNAVAILABLE')
    assert.equal(calls.length, 0)
  }
})

test('章节错误细节只保留有界数字，标题、URL、凭据及任意字段均不外传', () => {
  const output = safeError({code: 'PUBLISH_CHAPTER_LIMIT_EXCEEDED', detail: {stage: 'validate',
    count: 11, limit: 10, index: -1, minimum_seconds: Infinity, actual_seconds: 'secret',
    title: 'private-title', url: 'https://private/?token=secret', cookie: 'SESSDATA=secret'}})
  assert.deepEqual(output.detail, {stage: 'validate', count: 11, limit: 10})
  assert.doesNotMatch(JSON.stringify(output), /secret|private-title|https:/)
})
