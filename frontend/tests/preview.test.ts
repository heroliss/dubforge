import assert from 'node:assert/strict'
import test from 'node:test'
import { createLatestRefresh, finalVideoKind, finalVideoLabel, groupFinalVideos, latestFinalVideo, releaseMedia, releaseMediaIn } from '../src/utils/preview.ts'

test('成品被外部删除后不能选为默认预览，恢复后重新进入可用列表', () => {
  const old = {name:'final_dub_20260927_120000_123456.mp4',exists:true}
  const latest = {name:'final_dub_20260928_120000_123456.mp4',exists:false}
  assert.equal(latestFinalVideo([old,latest]), old)
  assert.deepEqual(groupFinalVideos([old,latest]).dub, [old])
  latest.exists = true
  assert.equal(latestFinalVideo([old,latest]), latest)
  assert.equal(groupFinalVideos([old,latest]).dub.length, 2)
})

test('覆盖成品保留名称，但最新预览和展示时间采用更新后的产物时间', () => {
  const updated = {name:'final_dub_20260926_120000_123456.mp4',created_at:'2026-09-27T15:00:00Z'}
  const other = {name:'final_src_hardsub_20260927_130000_123456.mp4',created_at:'2026-09-27T13:00:00Z'}
  assert.equal(latestFinalVideo([other, updated]), updated)
  assert.ok(finalVideoLabel(updated.name, updated.created_at).endsWith(new Date(updated.created_at).toLocaleTimeString('zh-CN', {hourCycle:'h23'})))
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

test('成片保留多个历史版本时，预览选择最新时间戳且不改变产物列表', () => {
  const old = { name: 'final_20260924_120000_123456.mp4' }
  const latest = { name: 'final_20260924_120000_987654.mp4' }
  const audio = { name: 'dub_full.wav' }
  const subtitle = { name: 'final_20260925_120000_123456.srt' }
  const arts = [old, audio, latest, subtitle]
  assert.equal(latestFinalVideo(arts), latest)
  assert.equal(latestFinalVideo([...arts].reverse()), latest)
  assert.deepEqual(arts, [old, audio, latest, subtitle])
  assert.equal(latestFinalVideo([audio, subtitle]), undefined)
})

test('版本更新后丢弃晚到的旧响应', async () => {
  const old = deferred<string>(), latest = deferred<string>()
  const signals: AbortSignal[] = []
  const shown: string[] = []
  const loader = createLatestRefresh(signal => {
    signals.push(signal)
    return signals.length === 1 ? old.promise : latest.promise
  }, value => shown.push(value), assert.fail)
  const first = loader.refresh()
  const second = loader.refresh(true)
  assert.equal(signals[0].aborted, true)
  latest.resolve('新预览')
  await second
  old.resolve('旧预览')
  await first
  assert.deepEqual(shown, ['新预览'])
})

test('运行结束时仍有读取请求，合并补读而不丢掉最后更新', async () => {
  const old = deferred<string>(), final = deferred<string>()
  let count = 0
  const shown: string[] = []
  const loader = createLatestRefresh(() => ++count === 1 ? old.promise : final.promise,
    value => shown.push(value), assert.fail)
  const first = loader.refresh()
  const last = loader.refresh()
  void loader.refresh()
  assert.equal(count, 1)
  old.resolve('部分结果')
  await Promise.resolve()
  assert.equal(count, 2)
  final.resolve('完整结果')
  await Promise.all([first, last])
  assert.deepEqual(shown, ['部分结果', '完整结果'])
  assert.equal(count, 2)
})

test('离开页面或清除产物后，晚到响应不能重建预览', async () => {
  const pending = deferred<string>()
  const shown: string[] = []
  const errors: unknown[] = []
  const loader = createLatestRefresh(() => pending.promise, value => shown.push(value), e => errors.push(e))
  const first = loader.refresh()
  void loader.refresh()
  loader.cancel()
  pending.resolve('已过期')
  await first
  assert.deepEqual(shown, [])
  assert.deepEqual(errors, [])
})

test('被取消请求的失败不覆盖新版本成功状态', async () => {
  const old = deferred<string>()
  let count = 0
  const shown: string[] = []
  const errors: unknown[] = []
  const loader = createLatestRefresh(() => ++count === 1 ? old.promise : Promise.resolve('新预览'),
    value => shown.push(value), e => errors.push(e))
  const first = loader.refresh()
  await loader.refresh(true)
  old.reject(new Error('旧请求失败'))
  await first
  assert.deepEqual(shown, ['新预览'])
  assert.deepEqual(errors, [])
})

test('首次读取失败后可以重新读取成功', async () => {
  let count = 0
  const shown: string[] = []
  const errors: unknown[] = []
  const loader = createLatestRefresh(async () => {
    if (++count === 1) throw new Error('产物尚未就绪')
    return '首次完整预览'
  }, value => shown.push(value), e => errors.push(e))
  await loader.refresh()
  await loader.refresh()
  assert.equal(errors.length, 1)
  assert.deepEqual(shown, ['首次完整预览'])
})

test('释放播放器时暂停播放并撤销媒体请求，页面内视频和音轨一起释放', () => {
  const calls: string[] = []
  const media = {
    pause: () => calls.push('暂停'),
    removeAttribute: (name: string) => calls.push(`移除${name}`),
    load: () => calls.push('重置'),
  } as unknown as HTMLMediaElement
  releaseMedia(media)
  assert.deepEqual(calls, ['暂停', '移除src', '重置'])
  calls.length = 0
  releaseMediaIn({ querySelectorAll: () => [media, media] } as unknown as ParentNode)
  assert.equal(calls.length, 6)
  releaseMediaIn(null)
})

test('成片按组合后缀分组，无法识别的旧命名进历史列表', () => {
  const arts = [
    { name: 'final_dub_hardsub_20260903_100000_1.mp4' },
    { name: 'final_dub_20260902_100000_1.mp4' },
    { name: 'final_src_hardsub_20260904_100000_1.mp4' },
    { name: 'final_20260901_100000_1.mp4' },
    { name: 'preview_20260905_100000_1.mp4' },
    { name: 'dub_full.wav' },
  ]
  const groups = groupFinalVideos(arts)
  assert.equal(groups.dub.length, 1)
  assert.equal(groups.dubHardsub.length, 1)
  assert.equal(groups.srcHardsub.length, 1)
  assert.deepEqual(groups.legacy.map(a => a.name), ['final_20260901_100000_1.mp4'])
})

test('成片选择器按组合命名并解析生成时间', () => {
  assert.equal(finalVideoKind('final_dub_20260903_100000_2.mp4'), 'dub')
  assert.equal(finalVideoKind('final_unknown_20260903_100000_2.mp4'), null)
  assert.equal(finalVideoLabel('final_dub_hardsub_20260903_103005_2.mp4'),
               '配音 · 烧录字幕 · 10:30:05')
})

test('同组多版本时最新时间戳排在最前', () => {
  const groups = groupFinalVideos([
    { name: 'final_dub_20260902_100000_1.mp4' },
    { name: 'final_dub_20260903_100000_2.mp4' },
  ])
  assert.equal(groups.dub[0].name, 'final_dub_20260903_100000_2.mp4')
})

test('评论版可识别且显式标注，跨命名版本按生成时间而非前缀排序', () => {
  const arts = [
    { name: 'final_dub_comments_20260920_100000_1.mp4' },
    { name: 'final_dub_20260927_100000_1.mp4' },
    { name: 'final_src_comments_20260921_100000_1.mp4' },
  ]
  assert.equal(groupFinalVideos(arts).dub[0], arts[1])
  assert.equal(groupFinalVideos(arts).srcComments[0], arts[2])
  assert.equal(finalVideoKind(arts[0].name), 'dub_comments')
  assert.equal(finalVideoKind(arts[1].name), 'dub')
  assert.match(finalVideoLabel(arts[0].name), /叠加评论/)
  assert.match(finalVideoLabel(arts[2].name), /仅叠加评论/)
  assert.equal(latestFinalVideo(arts), arts[1])
})
