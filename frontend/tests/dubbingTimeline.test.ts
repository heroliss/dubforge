import assert from 'node:assert/strict'
import test from 'node:test'
import { DubbingTimeline, frameAt, timingWarnings, type MediaClock, type TimelineLine, type VoiceChoice } from '../src/utils/dubbingTimeline.ts'

class Media implements MediaClock {
  private position = 0
  get currentTime() { return this.position }
  set currentTime(value: number) { this.position = value; this.ended = false }
  paused = true; ended = false; playbackRate = 1; muted = false; volume = 1
  readyState = 4; duration = 30; src = ''; plays = 0; pauses = 0; loads = 0; seeking = false
  async play() { this.paused = false; this.plays++ }
  pause() { this.paused = true; this.pauses++ }
  load() { this.loads++ }
  removeAttribute() { this.src = '' }
  finish() { this.currentTime = this.duration; this.ended = true; this.paused = true }
}
function fixture(withVideo = true, failures: Record<string, number> = {}) {
  const video = new Media(), audio = new Map<string, Media>(), choices: Record<number, VoiceChoice> = {}
  const lines: TimelineLine[] = [
    {id: 1, start: 0, end: 2, wav: {exists: true, url: 'one?v=1', seconds: 3}},
    {id: 2, start: 2, end: 4, wav: {exists: true, url: 'two', seconds: 1}},
    {id: 3, start: 5, end: 7, wav: {exists: false, url: 'three', seconds: null}},
  ]
  let now = 0, errors = 0
  const player = new DubbingTimeline({video: () => withVideo ? video : undefined, lines: () => lines,
    choices: () => choices, sources: () => ({vocal: 'vocal', background: 'bg'}),
    createAudio: url => {
      const m = new Media(); m.src = url
      m.duration = lines.find(l => l.wav.url === url)?.wav.seconds ?? 30
      const play = m.play.bind(m)
      m.play = () => {
        if (failures[url] > 0) { failures[url]--; m.plays++; return Promise.reject(Object.assign(new Error('测试加载失败'), { name: 'NetworkError' })) }
        return play()
      }
      audio.set(url, m); return m
    },
    now: () => now, error: () => { errors++ }})
  return {player, video, audio, lines, choices, advance: (t: number) => { now = t }, errors: () => errors}
}

test('播放标记跟随实际音频，句间保留到下一句开始，定位和暂停立即更新', async () => {
  const f = fixture()
  await f.player.play()
  f.video.currentTime = 2.2; f.player.tick()
  assert.deepEqual(f.player.cursor, {lineId: 2, phase: 'playing'})
  f.audio.get('two')!.duration = .97; f.audio.get('two')!.finish()
  f.video.currentTime = 2.99; f.player.tick()
  assert.deepEqual(f.player.cursor, {lineId: 2, phase: 'gap'}, '不必等原字幕结束才显示停顿')
  f.audio.get('one?v=1')?.finish()
  f.video.currentTime = 4.5; f.player.tick()
  assert.deepEqual(f.player.cursor, {lineId: 2, phase: 'gap'}, '原字幕结束后仍保留位置')
  f.video.currentTime = 5; f.player.tick()
  assert.deepEqual(f.player.cursor, {lineId: 3, phase: 'playing'})
  f.player.seek(1)
  assert.deepEqual(f.player.cursor, {lineId: 1, phase: 'playing'})
  f.player.pause()
  assert.equal(f.player.cursor, null)
})

test('单句试听的标记不跳到未播放的下一句，空位明确显示无声', async () => {
  const f = fixture()
  await f.player.play(f.lines[0])
  f.video.currentTime = 2.5; f.player.tick()
  assert.deepEqual(f.player.cursor, {lineId: 1, phase: 'playing'})
  f.player.release()
  f.choices[2] = 'silent'
  await f.player.play(f.lines[1])
  assert.deepEqual(f.player.cursor, {lineId: 2, phase: 'silent'})
  f.player.release()
  assert.equal(f.player.cursor, null)
})

test('首句开始前没有播放标记，原声结束后保留停顿位置', async () => {
  const f = fixture()
  f.lines.splice(0, 2)
  await f.player.play()
  assert.equal(f.player.cursor, null)
  f.video.currentTime = 7.5; f.player.tick()
  assert.deepEqual(f.player.cursor, {lineId: 3, phase: 'gap'})
})

test('超时提示按实际选中配音与下一句起点计算，原声和空位不误报', () => {
  const f = fixture()
  assert.deepEqual(timingWarnings(f.lines, {}), {1: {nextId: 2, available: 2, excess: 1}})
  assert.deepEqual(timingWarnings(f.lines, {1: 'original'}), {})
  f.lines[0].selection = {source: 'dub', take: null}
  assert.deepEqual(timingWarnings(f.lines, {}), {})
  f.lines[0].selection.take = 1
  f.lines[0].wav.seconds = 2.01
  assert.deepEqual(timingWarnings(f.lines, {}), {})
})

test('末句超时按自身结束时间提示，与自动缩时重试一致', () => {
  const lines: TimelineLine[] = [{id: 1, start: 3, end: 5,
    wav: {exists: true, url: 'last', seconds: 3}}]
  assert.deepEqual(timingWarnings(lines, {}), {1: {nextId: null, available: 2, excess: 1}})
  assert.deepEqual(timingWarnings(lines, {1: 'original'}), {})
  lines[0].wav.seconds = 2.04
  assert.deepEqual(timingWarnings(lines, {}), {})
})

test('原声选择、缺失配音回退和长句重叠按真实时间轴播放', () => {
  const f = fixture()
  assert.deepEqual(frameAt(f.lines, 2.5, {}).dubbed.map(l => l.id), [1, 2])
  assert.equal(frameAt(f.lines, 5.5, {}).original, true)
  assert.deepEqual(frameAt(f.lines, 2.5, {1: 'original'}).dubbed.map(l => l.id), [2])
})
test('点击定位保持播放和暂停状态，所有音轨跟随时间、变速与音量', async () => {
  const f = fixture()
  f.player.seek(2)
  assert.equal(f.video.paused, true)
  await f.player.play()
  f.video.playbackRate = 1.5; f.player.backgroundVolume = .6
  f.player.seek(5.5)
  assert.equal(f.video.paused, false)
  assert.equal(f.audio.get('vocal')!.currentTime, 5.5)
  assert.equal(f.audio.get('bg')!.currentTime, 5.5)
  assert.equal(f.audio.get('bg')!.volume, .6)
  assert.equal(f.audio.get('bg')!.playbackRate, 1.5)
  f.video.pause(); f.player.tick()
  assert.ok([...f.audio.values()].every(m => m.paused))
  f.player.seek(1)
  assert.equal(f.video.paused, true)
})
test('单句以实际语音末尾停止，整段使用同一播放器继续', async () => {
  const f = fixture()
  await f.player.play(f.lines[0])
  const voice = f.audio.get('one?v=1')!
  f.video.currentTime = 2.5; f.player.tick()
  assert.equal(f.video.paused, false)
  voice.currentTime = 2.85
  f.video.currentTime = 3.01; f.player.tick()
  assert.equal(f.video.paused, false)
  assert.equal(voice.paused, false)
  assert.equal(voice.currentTime, 2.85, '时间轴到达末尾后不跳过尚未播放的尾音')
  voice.finish(); f.player.tick()
  assert.equal(f.video.paused, true)
  await f.player.play()
  f.video.currentTime = 5.2; f.player.tick()
  assert.equal(f.audio.get('vocal')!.paused, false)
})

test('连续播放保留前句尾音，音频自然结束后才释放', async () => {
  const f = fixture()
  await f.player.play()
  const voice = f.audio.get('one?v=1')!
  voice.currentTime = 2.8
  f.video.currentTime = 3.02; f.player.tick()
  assert.equal(voice.paused, false)
  assert.equal(voice.src, 'one?v=1')
  assert.equal(voice.currentTime, 2.8)
  assert.equal(f.video.currentTime, 3.02)
  voice.finish(); f.player.tick()
  assert.equal(voice.src, '')
  assert.equal(f.video.paused, false)
})

test('使用媒体实际时长，已结束的短音频不会在句子区间内反复重播', async () => {
  const f = fixture()
  await f.player.play(f.lines[1])
  const voice = f.audio.get('two')!
  voice.duration = .97; voice.finish()
  f.video.currentTime = 2.99; f.player.tick(); f.player.tick()
  assert.equal(voice.plays, 1)
  assert.equal(voice.paused, true)
  f.video.currentTime = 4.01; f.player.tick()
  assert.equal(f.player.playing, false)
  await f.player.play(f.lines[1])
  assert.equal(f.audio.get('two')!.paused, false, '再次试听从头播放')
})

test('视频先到末尾时允许配音尾音播完，仍可随时暂停', async () => {
  const f = fixture()
  f.video.duration = 2.9
  await f.player.play(f.lines[0])
  const voice = f.audio.get('one?v=1')!
  voice.currentTime = 2.75
  f.video.finish(); f.player.tick()
  assert.equal(f.player.playing, true)
  assert.equal(voice.paused, false)
  assert.equal(voice.currentTime, 2.75)
  f.player.pause(); f.player.tick()
  assert.equal(voice.paused, true)
  assert.equal(f.player.playing, false)
})

test('无视频单句也等音频结束，跳转则立即丢弃旧尾音', async () => {
  const f = fixture(false)
  await f.player.play(f.lines[0])
  const voice = f.audio.get('one?v=1')!
  voice.currentTime = 2.8
  f.advance(3.03); f.player.tick()
  assert.equal(f.player.playing, true)
  assert.equal(voice.paused, false)
  f.player.seek(5.5)
  assert.equal(voice.paused, true)
  assert.equal(voice.src, '')
})
test('仅人声试听不播放背景，媒体重建后旧版本释放且不会被帧循环重载', async () => {
  const f = fixture()
  await f.player.play(f.lines[0], true)
  assert.equal(f.audio.has('bg'), false)
  const old = f.audio.get('one?v=1')!
  f.player.release(); f.player.tick()
  assert.equal(old.paused, true); assert.equal(old.src, '')
  assert.equal(f.audio.has('bg'), false)
  f.lines[0].wav.url = 'one?v=2'
  await f.player.play(f.lines[0])
  assert.equal(f.audio.get('one?v=2')!.paused, false)
})
test('视频缓冲或拖动时暂停音轨，恢复后按视频时间重新对齐', async () => {
  const f = fixture()
  await f.player.play()
  f.video.readyState = 2; f.player.tick()
  assert.ok([...f.audio.values()].every(m => m.paused))
  f.video.readyState = 4; f.video.currentTime = 1.2; f.player.tick()
  assert.equal(f.audio.get('bg')!.currentTime, 1.2)
  f.video.seeking = true; f.player.tick()
  assert.ok([...f.audio.values()].every(m => m.paused))
})
test('无视频时使用同一时间轴试听，暂停与恢复不倒退', async () => {
  const f = fixture(false)
  await f.player.play(f.lines[1])
  f.advance(1); f.player.tick()
  assert.equal(f.player.time, 3)
  f.player.pause(); f.advance(9)
  assert.equal(f.player.time, 3)
  await f.player.play(); f.advance(10)
  assert.equal(f.player.time, 4)
})
test('异步 play 晚到不能在用户暂停后恢复声音', async () => {
  const f = fixture()
  await f.player.play()
  f.player.pause()
  await Promise.resolve(); await Promise.resolve()
  assert.ok([...f.audio.values()].every(m => m.paused))
})

test('空位和失效的选中版本保持人声静音，背景照常播放', async () => {
  const f = fixture()
  f.lines[0].selection = {source: 'dub', take: null}
  f.lines[2].selection = {source: 'dub', take: 1}
  await f.player.play()
  assert.equal(f.audio.has('one?v=1'), false)
  assert.equal(f.audio.get('bg')!.paused, false)
  f.video.currentTime = 5.5; f.player.tick()
  assert.equal(f.audio.has('vocal'), false)
  assert.equal(f.video.muted, true)
})

test('连续播放与版本切换从不改写视频时间，小幅音频漂移不跳转', async () => {
  const f = fixture()
  await f.player.play()
  let current = 0, writes = 0
  Object.defineProperty(f.video, 'currentTime', {get: () => current, set: v => {writes++; current = v}})
  current = .2
  f.audio.get('bg')!.currentTime = .1
  f.player.tick()
  assert.equal(f.audio.get('bg')!.currentTime, .1)
  assert.ok(f.audio.get('bg')!.playbackRate > 1)
  f.lines[0].wav.url = 'another-take'
  f.player.tick()
  assert.equal(f.audio.get('another-take')!.currentTime, .2)
  assert.equal(f.video.paused, false)
  assert.equal(writes, 0)
})

test('后台生成当前句配音后按视频当前位置接入，后续句完成后自动预载', async () => {
  const f = fixture()
  f.lines[0].wav.exists = false
  f.lines[1].wav.exists = false
  await f.player.play()
  const background = f.audio.get('bg')!, original = f.audio.get('vocal')!
  f.video.currentTime = .8
  f.lines[0].wav = {exists: true, url: 'new-first', seconds: 1.5}
  f.lines[0].selection = {source: 'dub', take: 1}
  f.player.tick()
  assert.equal(f.audio.get('new-first')!.currentTime, .8)
  assert.equal(f.audio.get('new-first')!.paused, false)
  assert.equal(original.paused, true)
  assert.equal(f.audio.get('bg'), background)
  assert.equal(background.paused, false)
  assert.equal(f.video.currentTime, .8)
  assert.equal(f.video.paused, false)
  f.lines[1].wav = {exists: true, url: 'new-second', seconds: 1}
  f.lines[1].selection = {source: 'dub', take: 1}
  f.player.tick()
  assert.equal(f.audio.get('new-second')!.paused, true)
  f.video.currentTime = 2.3; f.player.tick()
  assert.ok(Math.abs(f.audio.get('new-second')!.currentTime - .3) < .001)
  assert.equal(f.audio.get('new-second')!.paused, false)
})

test('覆盖配音释放旧缓存，保持暂停状态和单句范围', async () => {
  const f = fixture()
  await f.player.play(f.lines[0])
  const old = f.audio.get('one?v=1')!
  f.video.currentTime = .7; f.player.pause()
  f.lines[0].wav = {exists: true, url: 'one?v=2', seconds: 1.2}
  f.player.tick()
  assert.equal(old.src, '')
  assert.equal(old.paused, true)
  assert.equal(f.audio.get('one?v=2')!.currentTime, .7)
  assert.equal(f.audio.get('one?v=2')!.paused, true)
  assert.equal(f.video.currentTime, .7)
  assert.equal(f.video.paused, true)
})

test('音频加载失败后不自动刷请求，重新试听重建失败音轨并恢复声音', async () => {
  const f = fixture(true, { 'one?v=1': 1 })
  await f.player.play(f.lines[0])
  await Promise.resolve(); await Promise.resolve()
  const failed = f.audio.get('one?v=1')!, background = f.audio.get('bg')!
  assert.equal(f.errors(), 1)
  assert.equal(f.video.paused, true)
  for (let i = 0; i < 20; i++) f.player.tick()
  assert.equal(failed.plays, 1)
  await f.player.play(f.lines[0])
  assert.notEqual(f.audio.get('one?v=1'), failed)
  assert.equal(failed.src, '', '失败媒体必须释放，不能继续复用浏览器的错误状态')
  assert.equal(f.audio.get('one?v=1')!.paused, false)
  assert.equal(f.audio.get('bg'), background, '健康音轨不必重新加载')
  assert.equal(f.video.paused, false)
  assert.equal(f.errors(), 1)
  assert.equal(f.lines[0].wav.url, 'one?v=1', '不更换用户选定的配音版本')
  f.player.dispose()
})

test('背景与配音同时失败后，原生视频按钮也能恢复全部失败音轨', async () => {
  const f = fixture(true, { bg: 1, 'one?v=1': 1 })
  await f.player.play()
  await Promise.resolve(); await Promise.resolve()
  const failedVoice = f.audio.get('one?v=1')!, failedBackground = f.audio.get('bg')!
  assert.equal(f.errors(), 1, '一组失败不重复弹出多条提示')
  // 对应原生视频 play 事件，而不是界面提供的连续播放按钮。
  await f.video.play(); f.player.activate(); f.player.tick()
  assert.notEqual(f.audio.get('one?v=1'), failedVoice)
  assert.notEqual(f.audio.get('bg'), failedBackground)
  assert.equal(f.audio.get('one?v=1')!.paused, false)
  assert.equal(f.audio.get('bg')!.paused, false)
  assert.equal(failedVoice.src, '')
  assert.equal(failedBackground.src, '')
  assert.equal(f.errors(), 1)
  f.player.dispose()
})

test('重试仍失败时保持暂停，不进入无限加载；无视频试听同样可以再次恢复', async () => {
  const f = fixture(false, { 'one?v=1': 2 })
  await f.player.play(f.lines[0], true)
  await Promise.resolve(); await Promise.resolve()
  await f.player.play(f.lines[0], true)
  await Promise.resolve(); await Promise.resolve()
  assert.equal(f.errors(), 2)
  assert.equal(f.player.playing, false)
  for (let i = 0; i < 20; i++) f.player.tick()
  assert.equal(f.errors(), 2)
  await f.player.play(f.lines[0], true)
  assert.equal(f.audio.get('one?v=1')!.paused, false)
  assert.equal(f.player.playing, true)
  assert.equal(f.audio.has('bg'), false)
  f.player.dispose()
})
