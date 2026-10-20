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
class EventMedia extends Media {
  private target = new EventTarget()
  listeners = new Map<string, Set<() => void>>()
  addEventListener(name: string, listener: () => void) {
    this.target.addEventListener(name, listener)
    const callbacks = this.listeners.get(name) ?? new Set<() => void>()
    callbacks.add(listener); this.listeners.set(name, callbacks)
  }
  removeEventListener(name: string, listener: () => void) {
    this.target.removeEventListener(name, listener)
    this.listeners.get(name)?.delete(listener)
  }
  event(name: string) { this.target.dispatchEvent(new Event(name)) }
}
function fixture(withVideo = true, failures: Record<string, number> = {}, createMedia = () => new Media()) {
  const video = new Media(), audio = new Map<string, Media>(), choices: Record<number, VoiceChoice> = {}
  const lines: TimelineLine[] = [
    {id: 1, start: 0, end: 2, wav: {exists: true, url: 'one?v=1', seconds: 3}},
    {id: 2, start: 2, end: 4, wav: {exists: true, url: 'two', seconds: 1}},
    {id: 3, start: 5, end: 7, wav: {exists: false, url: 'three', seconds: null}},
  ]
  let now = 0, errors = 0
  const completed: (number | null)[] = []
  const player = new DubbingTimeline({video: () => withVideo ? video : undefined, lines: () => lines,
    choices: () => choices, sources: () => ({vocal: 'vocal', background: 'bg'}),
    createAudio: url => {
      const m = createMedia(); m.src = url
      m.duration = lines.find(l => l.wav.url === url)?.wav.seconds ?? 30
      const play = m.play.bind(m)
      m.play = () => {
        if (failures[url] > 0) { failures[url]--; m.plays++; return Promise.reject(Object.assign(new Error('测试加载失败'), { name: 'NetworkError' })) }
        return play()
      }
      audio.set(url, m); return m
    },
    now: () => now, error: () => { errors++ }, onEnded: lineId => {
      assert.equal(player.playing, false, '结束通知发送前须先暂停播放器')
      assert.ok([...audio.values()].every(media => media.paused), '结束通知发送前须先停全部音轨')
      completed.push(lineId)
    }})
  return {player, video, audio, lines, choices, completed, advance: (t: number) => { now = t }, errors: () => errors}
}
const flushPlayback = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }

test('逐句正增益不在时间轴层被100%截断，监听静音保留当前播放',async()=>{
  const f=fixture(),gains=new Map<MediaClock,number>()
  ;(f.player as any).options.applyGain=(media:MediaClock,gain:number)=>gains.set(media,gain)
  f.lines[0].audio_gain_db=6;f.player.voiceVolume=.8
  await f.player.play();await flushPlayback()
  const voice=f.audio.get('one?v=1')!,background=f.audio.get('bg')!
  assert.ok(gains.get(voice)!>1,'超过1的真实增益交给支持放大的输出适配层')
  assert.ok(Math.abs(gains.get(voice)!-.8*10**(6/20))<1e-10)
  assert.equal(gains.get(background),1)
  f.player.monitorVolume=.5;f.player.monitorMuted=true;f.player.tick()
  assert.ok(Math.abs(gains.get(voice)!-.8*10**(6/20)*.5)<1e-10)
  assert.equal(voice.muted,true);assert.equal(background.muted,true);assert.equal(voice.paused,false)
  f.player.monitorMuted=false;f.player.tick();assert.equal(voice.muted,false)
  f.player.dispose()
})

function delayPlay(media: Media) {
  const attempts: { resolve: () => void; reject: (error: Error) => void }[] = []
  media.play = () => {
    media.plays++
    return new Promise<void>((resolve, reject) => {
      attempts.push({ resolve: () => { media.paused = false; resolve() }, reject })
    })
  }
  return attempts
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
  assert.deepEqual(timingWarnings(f.lines, {}), {1: {nextId: 2, available: 2, excess: 1, kind: 'overlap'}})
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
  assert.deepEqual(timingWarnings(lines, {}), {1: {nextId: null, available: 2, excess: 1, kind: 'target_overrun'}})
  assert.deepEqual(timingWarnings(lines, {1: 'original'}), {})
  lines[0].wav.seconds = 2.04
  assert.deepEqual(timingWarnings(lines, {}), {})
})

test('区分字幕目标超时、真实人声重叠及视频越界，空位不形成冲突', () => {
  const lines: TimelineLine[] = [
    {id: 1, start: 0, end: 1, selection: {source: 'dub', take: 1}, wav: {exists: true, url: 'one', seconds: 4}},
    {id: 2, start: 2, end: 3, selection: {source: 'dub', take: null}, wav: {exists: false, url: 'empty'}},
    {id: 3, start: 3, end: 5, selection: {source: 'original', take: null}, wav: {exists: false, url: 'original'}},
  ]
  assert.deepEqual(timingWarnings(lines, {}), {1: {nextId: 3, available: 3, excess: 1, kind: 'overlap'}})
  assert.deepEqual(timingWarnings(lines, {}, null, 3.5), {
    1: {nextId: null, available: 3.5, excess: .5, kind: 'video_overrun'},
    3: {nextId: null, available: .5, excess: 1.5, kind: 'video_overrun'},
  })
  const last: TimelineLine[] = [{id: 1, start: 3, end: 5, wav: {exists: true, url: 'last', seconds: 3}}]
  assert.equal(timingWarnings(last, {}, null, 8)[1].kind, 'target_overrun', '视频片尾停顿仍可容纳完整配音')
  last[0].wav.seconds = 5.05
  assert.equal(timingWarnings(last, {}, null, 8)[1].kind, 'target_overrun', '50ms 媒体舍入误差不算视频越界')
})

test('服务端计划只在时间、声源、版本及媒体匹配时使用，旧计划不覆盖乐观编辑', () => {
  const lines: TimelineLine[] = [{id: 1, start: 0, end: 2, selection: {source: 'dub', take: 1},
    wav: {exists: true, url: 'one?v=1', seconds: 3}}]
  const placement = {schema: 1, video_duration: 6,
    signature: [[1, 0, 2, 'dub', 1, 3, 'one?v=1']],
    issues: [{kind: 'target_overrun' as const, id: 1, next_id: null, available: 2, excess: 1}]}
  assert.equal(timingWarnings(lines, {}, placement)[1].kind, 'target_overrun')
  // 清空服务端提示，用于证明匹配时直接读取服务端计划而非另算一份。
  const quiet = {...placement, issues: []}
  assert.deepEqual(timingWarnings(lines, {}, quiet), {})
  lines[0].wav.url = 'one?v=2'
  assert.equal(timingWarnings(lines, {}, quiet)[1].kind, 'target_overrun', '同名文件替换后旧计划失效')
  assert.deepEqual(timingWarnings(lines, {1: 'original'}, placement), {})
  lines[0].selection!.take = null
  assert.deepEqual(timingWarnings(lines, {}, placement), {})
  lines[0].selection!.take = 2
  assert.equal(timingWarnings(lines, {}, placement, 2.5)[1].kind, 'video_overrun', '实际加载视频时长变化须重新检查')
})

test('原声选择、缺失配音回退和长句重叠按真实时间轴播放', () => {
  const f = fixture()
  assert.deepEqual(frameAt(f.lines, 2.5, {}).dubbed.map(l => l.id), [1, 2])
  assert.equal(frameAt(f.lines, 5.5, {}).original, true)
  assert.deepEqual(frameAt(f.lines, 2.5, {1: 'original'}).dubbed.map(l => l.id), [2])
})

test('服务端共同排布时间驱动连续播放、单句试听与定位，源时间保持不变', async () => {
  const f = fixture()
  Object.assign(f.lines[0], { start: 1, end: 2, playback_start: .5, playback_end: 3.5 })
  Object.assign(f.lines[1], { start: 2, end: 4, playback_start: 3.6, playback_end: 4.6 })
  assert.deepEqual(frameAt(f.lines, 2.5, {}).dubbed.map(line => line.id), [1])
  assert.deepEqual(frameAt(f.lines, 3.8, {}).dubbed.map(line => line.id), [2])
  assert.deepEqual(timingWarnings(f.lines, {}), {}, '已协调的相邻句不再按旧起点误报重叠')
  await f.player.play(f.lines[0]); await flushPlayback()
  assert.equal(f.video.currentTime, .5)
  f.video.currentTime = 1.5; f.player.tick()
  assert.equal(f.audio.get('one?v=1')!.currentTime, 1)
  assert.deepEqual(f.player.cursor, { lineId: 1, phase: 'playing' })
  assert.equal(f.lines[0].start, 1); assert.equal(f.lines[0].end, 2)
})

test('无效或不完整的播放时间完整回退源时间，不产生负起点或部分轴混用', () => {
  const f = fixture()
  for (const values of [{ playback_start: -1, playback_end: 3 }, { playback_start: 1, playback_end: 0 },
    { playback_start: NaN, playback_end: 3 }, { playback_start: .5 }, { playback_end: 3 }]) {
    f.lines[0] = { id: 1, start: 0, end: 2, wav: f.lines[0].wav, ...values }
    assert.deepEqual(frameAt(f.lines, .25, {}).dubbed.map(line => line.id), [1])
  }
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
  f.advance(.2); f.player.tick()
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
  f.advance(.2); f.player.tick()
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

test('无视频的单句试听暂停后resume保持原位置，空视频请求不会被误认作同一请求', async () => {
  const f = fixture(false)
  await f.player.play(f.lines[0]); await flushPlayback()
  f.advance(.8); f.player.tick()
  const voice=f.audio.get('one?v=1')!
  f.player.pause()
  f.advance(9)
  assert.equal(f.player.time,.8);assert.equal(voice.paused,true)
  assert.equal(f.player.resume(1),true)
  await flushPlayback();f.player.tick()
  assert.ok(Math.abs(f.player.time-.8)<1e-9);assert.ok(Math.abs(voice.currentTime-.8)<1e-9);assert.equal(voice.paused,false)
  f.advance(9.2);f.player.tick();assert.ok(Math.abs(f.player.time-1)<1e-9)
  assert.equal(f.player.resume(2),false)
  f.player.dispose()
  const empty=fixture(false)
  assert.equal(empty.player.activate(),true,'没有video或待执行请求时视为人工接管，不读取空请求')
  empty.player.dispose()
})
test('单句暂停后恢复沿用当前时间轴，不重新定位到句首', async () => {
  const f = fixture()
  await f.player.play(f.lines[0])
  f.video.currentTime = .8; f.player.tick()
  const voice = f.audio.get('one?v=1')!
  assert.equal(voice.currentTime, .8)
  f.player.pause()
  assert.equal(f.video.currentTime, .8)
  assert.equal(voice.paused, true)
  assert.equal(f.player.resume(1), true)
  await flushPlayback(); f.player.tick()
  assert.equal(f.video.currentTime, .8)
  assert.equal(voice.currentTime, .8)
  assert.equal(voice.paused, false)
  assert.equal(f.player.resume(2), false, '其它句不能接管当前单句时间轴')
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
  const failures: Record<string, number> = {}, f = fixture(true, failures)
  await f.player.play(); await flushPlayback(); f.player.pause()
  failures['one?v=1'] = 1
  await f.player.play(f.lines[0])
  await Promise.resolve(); await Promise.resolve()
  const failed = f.audio.get('one?v=1')!, background = f.audio.get('bg')!
  assert.equal(f.errors(), 1)
  assert.equal(f.video.paused, true)
  const attempts = failed.plays
  for (let i = 0; i < 20; i++) f.player.tick()
  assert.equal(failed.plays, attempts)
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

test('连续播放不把落后于视频的旧尾音带入下一句，先停旧声音再开新声音', async () => {
  const f = fixture()
  f.lines[0].wav.seconds = 1
  await f.player.play(); await flushPlayback()
  const old = f.audio.get('one?v=1')!, next = f.audio.get('two')!
  old.currentTime = .7
  const play = next.play.bind(next)
  next.play = () => { assert.equal(old.paused, true, '新声音启动前旧续播必须已停止'); return play() }
  f.video.currentTime = 2.1; f.player.tick()
  assert.equal(old.paused, true)
  assert.equal(next.paused, false)
  assert.ok(Math.abs(next.currentTime - .1) < .001)
  assert.equal(f.video.currentTime, 2.1)
})

test('连续时间轴跳过旧句后不在停顿区追播，真实时长重叠仍与成片一致', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback()
  f.video.currentTime = 2.5; f.player.tick()
  assert.equal(f.audio.get('one?v=1')!.paused, false, '实际音频尚未结束的重叠保留')
  assert.equal(f.audio.get('two')!.paused, false)
  f.video.currentTime = 4.5; f.player.tick()
  assert.equal(f.audio.get('one?v=1')?.paused ?? true, true)
  assert.equal(f.audio.get('two')?.paused ?? true, true)
  assert.equal(f.audio.get('bg')!.paused, false)
})

test('运行中新音频未加载元数据时不从零开声，加载完成按最新视频位置接入', async () => {
  const f = fixture()
  f.lines[0].wav.exists = false
  await f.player.play(); await flushPlayback()
  f.lines[0].wav = {exists: true, url: 'live-first', seconds: 2}
  f.video.currentTime = .8
  f.player.pause(); f.player.tick()
  const voice = f.audio.get('live-first')!
  voice.readyState = 0; voice.duration = NaN
  await f.player.play(); await flushPlayback()
  assert.equal(voice.plays, 0)
  assert.equal(voice.paused, true)
  f.video.currentTime = 1.1; voice.readyState = 4; voice.duration = 2; f.player.tick()
  assert.equal(voice.paused, false)
  assert.equal(voice.currentTime, 1.1)
  assert.equal(f.audio.get('vocal')!.paused, true)
  assert.equal(f.video.currentTime, 1.1)
})

test('跳转后旧 play 失败不能中止新请求或污染同一音轨的重试状态', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback(); f.player.pause()
  const voice = f.audio.get('one?v=1')!, attempts = delayPlay(voice)
  await f.player.play(); f.player.seek(1.3)
  assert.equal(attempts.length, 2)
  attempts[0].reject(Object.assign(new Error('旧加载失败'), {name: 'NetworkError'}))
  await flushPlayback()
  assert.equal(f.errors(), 0)
  assert.equal(f.video.paused, false)
  attempts[1].resolve(); await flushPlayback()
  assert.equal(voice.paused, false)
  assert.equal(voice.currentTime, 1.3)
  f.player.pause(); await f.player.play()
  assert.equal(f.audio.get('one?v=1'), voice, '旧失败不能把健康媒体标成错误')
})

test('运行中版本替换后迟到的旧 play 不能与新版本一起播放', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback(); f.player.pause()
  const old = f.audio.get('one?v=1')!, attempts = delayPlay(old)
  await f.player.play()
  f.video.currentTime = .5
  f.lines[0].wav = {exists: true, url: 'replacement', seconds: 2}
  f.player.tick()
  const current = f.audio.get('replacement')!
  assert.equal(current.paused, false)
  assert.equal(current.currentTime, .5)
  attempts[0].resolve(); await flushPlayback()
  assert.equal(old.paused, true)
  assert.equal(old.src, '')
  assert.equal(current.paused, false)
  assert.equal(f.errors(), 0)
})

test('单句试听在定位前确定范围，不瞬间播放同一时刻的另一句', async () => {
  const f = fixture()
  await f.player.play(f.lines[1])
  assert.equal(f.audio.get('one?v=1')?.plays ?? 0, 0)
  assert.equal(f.audio.get('two')!.paused, false)
})

test('暂停后迟到的视频 play 与原生 play 事件不能重启整组，下一次正常播放仍有效', async () => {
  const f = fixture(), attempts = delayPlay(f.video)
  const first = f.player.play()
  f.player.pause()
  f.video.paused = false; f.player.activate(); f.player.tick()
  assert.equal(f.player.playing, false)
  assert.ok([...f.audio.values()].every(media => media.paused))
  attempts[0].resolve(); await first; await flushPlayback()
  assert.equal(f.video.paused, true)
  const second = f.player.play()
  attempts[1].resolve(); await second; await flushPlayback()
  assert.equal(f.player.playing, true)
})

test('元数据加载错误或同步 play 异常都停止整组，帧循环不自动重试', async () => {
  for (const synchronous of [false, true]) {
    const f = fixture()
    await f.player.play(); await flushPlayback(); f.player.pause()
    const voice = f.audio.get('one?v=1')!
    if (synchronous) voice.play = () => { voice.plays++; throw new Error('同步播放失败') }
    else { voice.readyState = 0; Object.assign(voice, {error: {code: 2}}) }
    const videoPlays = f.video.plays
    await f.player.play()
    assert.equal(f.errors(), 1)
    assert.equal(f.video.paused, true)
    assert.equal(f.video.plays, videoPlays, '同步失败后不再调用 video.play 激活刚停止的整组')
    assert.ok([...f.audio.values()].every(media => media.paused))
    const plays = voice.plays
    for (let i = 0; i < 10; i++) f.player.tick()
    assert.equal(voice.plays, plays)
    assert.equal(f.errors(), 1)
  }
})

test('原生暂停失效在途视频请求，AbortError 不提示失败；自然结束仍保留尾音', async () => {
  const f = fixture(), attempts = delayPlay(f.video)
  const request = f.player.play()
  f.video.paused = true; f.player.videoPaused()
  attempts[0].reject(Object.assign(new Error('用户暂停'), {name: 'AbortError'}))
  await request; await flushPlayback()
  assert.equal(f.errors(), 0)
  assert.equal(f.player.playing, false)
  assert.ok([...f.audio.values()].every(media => media.paused))
  f.video.play = Media.prototype.play.bind(f.video)
  await f.player.play(f.lines[0]); await flushPlayback()
  f.video.finish(); f.player.videoPaused()
  assert.equal(f.audio.get('one?v=1')!.paused, false)
  assert.equal(f.player.playing, true)
})

test('连续播放已到视频末尾后再次播放从头开始，未到末尾时保持当前位置', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback()
  f.audio.get('one?v=1')!.finish(); f.video.finish(); f.player.tick()
  assert.equal(f.player.playing, false)
  await f.player.play(); await flushPlayback()
  assert.equal(f.video.currentTime, 0)
  assert.equal(f.video.paused, false)
  assert.equal(f.audio.get('one?v=1')!.paused, false)
  f.video.currentTime = 1.2; f.player.pause(); await f.player.play()
  assert.equal(f.video.currentTime, 1.2)
})

test('单句配音完整结束后短暂留白再暂停，不等待字幕长留白；自然完成只通知一次', async () => {
  const f = fixture()
  await f.player.play(f.lines[1]); await flushPlayback()
  const voice = f.audio.get('two')!
  voice.duration = .97; voice.finish()
  f.video.currentTime = 2.96; f.player.tick()
  assert.equal(f.video.paused, false, '完整音频结束后保留两百毫秒听后空隙')
  assert.equal(voice.plays, 1, '听后空隙不能重播已结束的配音')
  assert.deepEqual(f.completed, [])
  f.advance(.2); f.player.tick()
  assert.equal(f.video.paused, true, '短暂空隙后停下，不必走完四秒字幕区间')
  assert.deepEqual(f.completed, [2])
  for (let i = 0; i < 5; i++) f.player.tick()
  f.player.videoPaused()
  assert.deepEqual(f.completed, [2])
  await f.player.play(f.lines[1]); await flushPlayback()
  assert.equal(f.audio.get('two')!.paused, false)
  f.audio.get('two')!.finish(); f.player.tick()
  f.advance(.4); f.player.tick()
  assert.deepEqual(f.completed, [2, 2], '同一句重听是新的完成事件')
})

test('单句尾音未播完、手动暂停、跳转和释放均不报自然完成', async () => {
  const f = fixture()
  await f.player.play(f.lines[0]); await flushPlayback()
  f.video.currentTime = 3.05; f.player.tick()
  assert.deepEqual(f.completed, [])
  f.player.pause(); f.audio.get('one?v=1')!.finish(); f.player.tick()
  assert.deepEqual(f.completed, [], '暂停后的旧结束状态不能冒充本次自然完成')
  await f.player.play(f.lines[0]); f.player.seek(5.5)
  f.audio.get('one?v=1')?.finish(); f.player.tick()
  assert.deepEqual(f.completed, [])
  f.player.release(); f.video.finish(); f.player.tick()
  assert.deepEqual(f.completed, [])
})

test('连续预览视频结束只报告一次 lineId=null，手动 pause 不报告', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback()
  f.player.pause(); f.player.tick()
  assert.deepEqual(f.completed, [])
  await f.player.play(); await flushPlayback()
  f.audio.get('one?v=1')!.finish(); f.video.finish(); f.player.tick()
  f.player.tick(); f.player.videoPaused()
  assert.deepEqual(f.completed, [null])
})

test('单句音频未就绪先停视频等待，准备完成后从句首一起播而不是追播', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback(); f.player.pause()
  const voice = f.audio.get('two')!, background = f.audio.get('bg')!
  voice.readyState = 0; voice.duration = NaN
  const videoPlays = f.video.plays, voicePlays = voice.plays
  await f.player.play(f.lines[1]); await flushPlayback()
  assert.equal(f.video.paused, true)
  assert.equal(f.video.currentTime, 2)
  assert.equal(f.player.playing, false)
  assert.equal(voice.plays, voicePlays)
  assert.equal(background.paused, true)
  f.player.tick(); f.player.videoPaused()
  assert.deepEqual(f.completed, [])
  voice.readyState = 1; voice.duration = 1; f.player.tick()
  assert.equal(f.video.plays, videoPlays, '仅元数据不够，等待音频可播放再启视频')
  voice.readyState = 4; f.player.tick(); await flushPlayback()
  assert.equal(f.video.currentTime, 2)
  assert.equal(voice.currentTime, 0)
  assert.equal(voice.paused, false)
  assert.equal(f.video.paused, false)
  assert.equal(f.video.plays, videoPlays + 1)
  voice.finish(); f.player.tick()
  f.advance(.2); f.player.tick()
  assert.deepEqual(f.completed, [2])
})

for (const control of ['pause', 'seek', 'newPlay', 'release', 'dispose'] as const)
  test(`单句准备中 ${control} 清除待播放请求，迟到元数据不会恢复旧句`, async () => {
    const f = fixture()
    await f.player.play(); await flushPlayback(); f.player.pause()
    const voice = f.audio.get('one?v=1')!
    voice.readyState = 0; voice.duration = NaN
    await f.player.play(f.lines[0])
    if (control === 'pause') f.player.pause()
    if (control === 'seek') f.player.seek(5.2)
    if (control === 'newPlay') await f.player.play(f.lines[1])
    if (control === 'release') f.player.release()
    if (control === 'dispose') f.player.dispose()
    const plays = voice.plays
    voice.readyState = 4; voice.duration = 3; f.player.tick(); await flushPlayback()
    assert.equal(voice.plays, plays)
    assert.equal(voice.paused, true)
    assert.deepEqual(f.completed, [])
    if (control === 'newPlay') assert.equal(f.audio.get('two')!.paused, false)
    else assert.equal(f.player.playing, false)
  })

test('单句等待元数据时加载失败停止等待并反馈一次，不自动刷请求', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback(); f.player.pause()
  const voice = f.audio.get('one?v=1')!
  voice.readyState = 0; voice.duration = NaN
  await f.player.play(f.lines[0])
  Object.assign(voice, { error: { code: 2 } }); f.player.tick()
  assert.equal(f.errors(), 1)
  assert.equal(f.player.playing, false)
  assert.equal(f.video.paused, true)
  for (let i = 0; i < 10; i++) f.player.tick()
  assert.equal(f.errors(), 1)
  assert.deepEqual(f.completed, [])
  await f.player.play(f.lines[0]); await flushPlayback()
  assert.notEqual(f.audio.get('one?v=1'), voice)
  assert.equal(f.audio.get('one?v=1')!.paused, false)
})

test('视频元数据迟到后单句准确定位句首再启动，无视频准备期间时钟不前进', async () => {
  const f = fixture()
  f.video.readyState = 0
  await f.player.play(f.lines[1])
  f.video.currentTime = 0; f.player.tick()
  assert.equal(f.player.time, 2)
  assert.equal(f.video.paused, true)
  f.video.readyState = 4; f.player.tick(); await flushPlayback()
  assert.equal(f.video.currentTime, 2)
  assert.equal(f.video.paused, false)
  const g = fixture(false)
  await g.player.play(); await flushPlayback(); g.player.pause()
  const voice = g.audio.get('two')!
  voice.readyState = 0
  await g.player.play(g.lines[1]); g.advance(5); g.player.tick()
  assert.equal(g.player.time, 2)
  assert.equal(g.player.playing, false)
  voice.readyState = 4; g.player.tick(); await flushPlayback()
  assert.equal(g.player.time, 2)
  assert.equal(voice.currentTime, 0)
  g.advance(5.5); g.player.tick()
  assert.equal(g.player.time, 2.5)
})

test('旧视频 play 晚到不能越过新单句准备状态，准备完成后只启动新句', async () => {
  const f = fixture()
  await f.player.play(); await flushPlayback(); f.player.pause()
  const attempts = delayPlay(f.video)
  const old = f.player.play(f.lines[0])
  await flushPlayback()
  const next = f.audio.get('two')!
  next.readyState = 0
  await f.player.play(f.lines[1])
  f.video.paused = false
  assert.equal(f.player.activate(), false, '旧请求对应的 play 事件不能冒充人工接管')
  attempts[0].resolve(); await old; await flushPlayback()
  assert.equal(f.video.paused, true)
  assert.equal(f.video.currentTime, 2)
  assert.equal(f.player.playing, false)
  assert.deepEqual(f.completed, [])
  f.video.play = Media.prototype.play.bind(f.video)
  next.readyState = 4; f.player.tick(); await flushPlayback()
  assert.equal(f.video.paused, false)
  assert.equal(next.currentTime, 0)
  assert.equal(next.paused, false)
})

test('视频在单句准备中加载失败反馈一次，停止等待后元数据不再触发播放', async () => {
  const f = fixture()
  f.video.readyState = 0
  await f.player.play(f.lines[1])
  Object.assign(f.video, { error: { code: 2 } }); f.player.tick()
  assert.equal(f.errors(), 1)
  assert.equal(f.video.paused, true)
  Object.assign(f.video, { error: null }); f.video.readyState = 4
  f.player.tick(); await flushPlayback()
  assert.equal(f.video.plays, 0)
  assert.equal(f.errors(), 1)
  assert.deepEqual(f.completed, [])
})

test('旧媒体退役先解绑事件，已经排队的旧回调也被对象身份隔离', async () => {
  const f = fixture(true, {}, () => new EventMedia())
  await f.player.play(f.lines[0]); await flushPlayback()
  const old = f.audio.get('one?v=1')! as EventMedia
  const queued = [...old.listeners.get('ended')!][0]!
  f.lines[0].wav = { exists: true, url: 'replacement', seconds: 2 }
  f.video.currentTime = .3; f.player.tick(); await flushPlayback()
  const current = f.audio.get('replacement')! as EventMedia
  assert.ok([...old.listeners.values()].every(callbacks => callbacks.size === 0))
  old.finish(); old.event('ended'); queued()
  assert.equal(current.paused, false)
  assert.deepEqual(f.completed, [])
  current.finish(); current.event('ended')
  assert.deepEqual(f.completed, [], '当前音频事件开启短暂听后空隙')
  f.advance(.2); f.player.tick()
  assert.deepEqual(f.completed, [1], '听后空隙结束仅完成一次')
  f.player.release()
  assert.ok([...f.audio.values()].every(media => [...(media as EventMedia).listeners.values()].every(callbacks => callbacks.size === 0)))
  const completed = [...f.completed]
  current.event('canplay'); current.event('ended')
  assert.deepEqual(f.completed, completed)
})

test('失败媒体重试时解绑旧事件，dispose 也清空健康媒体监听', async () => {
  const f = fixture(true, { 'one?v=1': 1 }, () => new EventMedia())
  await f.player.play(f.lines[0]); await flushPlayback()
  const failed = f.audio.get('one?v=1')! as EventMedia
  const queued = [...failed.listeners.get('error')!][0]!
  await f.player.play(f.lines[0]); await flushPlayback()
  const current = f.audio.get('one?v=1')! as EventMedia
  assert.ok([...failed.listeners.values()].every(callbacks => callbacks.size === 0))
  Object.assign(failed, { error: { code: 2 } }); queued()
  assert.equal(current.paused, false)
  assert.equal(f.errors(), 1)
  f.player.dispose()
  assert.ok([...f.audio.values()].every(media => [...(media as EventMedia).listeners.values()].every(callbacks => callbacks.size === 0)))
})

// 浏览器的音频定位不是同步赋值：解码/网络请求尚未完成时，getter 可能
// 仍返回旧位置；重复赋值会取消在途定位，不能用同步 Media 掩盖这个过程。
class AsyncSeekMedia extends EventMedia {
  private decodedPosition = 0
  private requestedPosition: number | null = null
  deferSeek = false
  seekWrites = 0
  override get currentTime() { return this.decodedPosition }
  override set currentTime(value: number) {
    this.ended = false
    if (!this.deferSeek) { this.decodedPosition = value; return }
    this.requestedPosition = value
    this.seekWrites++
    this.seeking = true
  }
  completeSeek() {
    assert.notEqual(this.requestedPosition, null)
    this.decodedPosition = this.requestedPosition!
    this.requestedPosition = null
    this.seeking = false
    this.event('seeked')
  }
}

test('音频异步定位尚未完成时，帧循环和 canplay 不重复追 seek', async () => {
  const f = fixture(true, {}, () => new AsyncSeekMedia())
  await f.player.play(); await flushPlayback()
  const voice = f.audio.get('one?v=1')! as AsyncSeekMedia
  f.video.currentTime = .8; voice.currentTime = .8; f.player.tick()
  f.video.pause(); f.player.videoPaused()
  // 网络重新定位期间音频仍报告旧解码位置；模拟真实浏览器中恢复时
  // 视频已在句内向前，而音频还没有完成首个定位请求的情况。
  voice.currentTime = 0; voice.deferSeek = true
  await f.video.play(); f.player.activate(); f.player.tick()
  assert.equal(voice.seekWrites, 1)
  assert.equal(voice.seeking, true)
  for (const time of [.9, 1, 1.1]) {
    f.video.currentTime = time
    f.player.tick()
    voice.event('canplay')
  }
  assert.equal(voice.seekWrites, 1, '在途定位必须完成后才能重新评估，不能每帧取消并重发')
  assert.equal(voice.paused, true, '定位完成前不要从旧解码位置开声')
  voice.completeSeek(); await flushPlayback()
  assert.equal(voice.currentTime, .8)
  assert.equal(voice.paused, false, 'seeked 应直接恢复音频，不必等下一个帧或 canplay')
  assert.equal(f.video.currentTime, 1.1, '音频定位不得把视频重置到句首')
})

test('音频 seeked 单独触发时，按最新视频时钟重新评估过大的漂移', async () => {
  const f = fixture(true, {}, () => new AsyncSeekMedia())
  await f.player.play(); await flushPlayback()
  const voice = f.audio.get('one?v=1')! as AsyncSeekMedia
  f.video.currentTime = .8; voice.currentTime = .8; f.player.tick()
  f.video.pause(); f.player.videoPaused()
  voice.currentTime = 0; voice.deferSeek = true
  await f.video.play(); f.player.activate(); f.player.tick()
  f.video.currentTime = 1.3; f.player.tick()
  assert.equal(voice.seekWrites, 1)
  voice.completeSeek()
  assert.equal(voice.seekWrites, 2, '完成旧定位时应立即补齐最新时钟，而不是停在旧目标')
  assert.equal(voice.seeking, true)
  voice.completeSeek(); await flushPlayback()
  assert.equal(voice.seekWrites, 2)
  assert.equal(voice.currentTime, 1.3)
  assert.equal(voice.paused, false)
})

for (const control of ['pause', 'seek', 'newPlay', 'dispose'] as const)
  test(`单句等待 Audio.play 实际成功期间 ${control} 取消后，旧成功不再启动视频`, async () => {
    const f = fixture()
    await f.player.play(); await flushPlayback(); f.player.pause()
    const voice = f.audio.get('one?v=1')!, attempts = delayPlay(voice)
    await f.player.play(f.lines[0])
    assert.equal(attempts.length, 1)
    assert.equal(f.video.paused, true)
    if (control === 'pause') f.player.pause()
    if (control === 'seek') f.player.seek(5.2)
    if (control === 'newPlay') { await f.player.play(f.lines[1]); await flushPlayback() }
    if (control === 'dispose') f.player.dispose()
    const videoPlays = f.video.plays
    attempts[0].resolve(); await flushPlayback()
    assert.equal(f.video.plays, videoPlays)
    assert.equal(voice.paused, true)
    assert.deepEqual(f.completed, [])
    if (control === 'newPlay') assert.equal(f.audio.get('two')!.paused, false)
    else assert.equal(f.video.paused, true)
  })

test('单句人声音量为零时不等待 Audio.play，无视频时仍按同一时钟完成静音试听', async () => {
  for (const withVideo of [true, false]) {
    const f = fixture(withVideo)
    f.player.voiceVolume = 0
    await f.player.play(f.lines[1]); await flushPlayback()
    const voice = f.audio.get('two')!
    assert.equal(voice.plays, 0)
    assert.equal(f.player.playing, true)
    if (withVideo) f.video.currentTime = 3.1
    else f.advance(1.1)
    f.player.tick()
    assert.equal(f.player.playing, false)
    assert.deepEqual(f.completed, [2])
  }
})

test('原声单句试听保留前150毫秒与后250毫秒上下文，不带入相邻配音或修改源时间', async () => {
  const f = fixture()
  f.choices[2] = 'original'
  await f.player.play(f.lines[1]); await flushPlayback()
  assert.equal(f.video.currentTime, 1.85)
  assert.equal(f.audio.get('vocal')!.currentTime, 1.85)
  assert.equal(f.audio.get('one?v=1')?.plays ?? 0, 0)
  assert.deepEqual(f.player.cursor, { lineId: 2, phase: 'playing' })
  f.video.currentTime = 4.18; f.player.tick()
  assert.equal(f.audio.get('vocal')!.paused, false, '原字幕结束后仍可听到尾音上下文')
  assert.equal(f.video.paused, false)
  f.video.currentTime = 4.25; f.player.tick()
  assert.equal(f.video.paused, true)
  assert.deepEqual(f.completed, [2])
  assert.equal(f.lines[1].start, 2); assert.equal(f.lines[1].end, 4)
  f.player.dispose()
})

test('原声单句上下文限制在视频首尾，连续播放不继承试听缓冲', async () => {
  const f = fixture()
  f.choices[1] = 'original'; f.choices[3] = 'original'
  await f.player.play(f.lines[0]); await flushPlayback()
  assert.equal(f.video.currentTime, 0)
  f.player.pause(); f.video.duration = 7.1
  await f.player.play(f.lines[2]); await flushPlayback()
  assert.equal(f.video.currentTime, 4.85)
  f.video.currentTime = 7.1; f.player.tick()
  assert.equal(f.video.paused, true)
  f.player.seek(1.9); await f.player.play(); await flushPlayback()
  assert.equal(f.audio.get('vocal')!.paused, false)
  f.video.currentTime = 2.1; f.player.tick()
  assert.equal(f.audio.get('vocal')!.paused, true, '连续模式的原声仍严格使用原始句子区间')
  assert.equal(f.audio.get('two')!.paused, false)
  f.player.dispose()
})

test('配音听后缓冲保留背景，整段播放不会加入逐句停顿或重播尾音', async () => {
  const f = fixture()
  f.lines[1].start = 3
  await f.player.play(f.lines[0]); await flushPlayback()
  const voice = f.audio.get('one?v=1')!, background = f.audio.get('bg')!
  voice.finish(); f.player.tick()
  assert.equal(background.paused, false)
  f.video.currentTime = 3.1; f.advance(.1); f.player.tick()
  assert.equal(background.paused, false)
  assert.equal(f.audio.get('two')?.plays ?? 0, 0, '听后缓冲不播放邻句配音')
  assert.equal(voice.plays, 1)
  f.advance(.201); f.player.tick()
  assert.equal(background.paused, true)
  assert.deepEqual(f.completed, [1])
  f.player.seek(0); await f.player.play(); await flushPlayback()
  f.video.currentTime = 3; voice.finish(); f.player.tick()
  assert.equal(f.video.paused, false)
  assert.equal(f.audio.get('two')!.paused, false, '连续模式下一句按正常时间立即开声')
  assert.deepEqual(f.completed, [1])
  f.player.dispose()
})

for (const control of ['pause', 'seek', 'nativeSeek', 'newPlay', 'release', 'dispose'] as const)
  test(`单句听后缓冲中 ${control} 取消旧完成通知，不干扰新播放`, async () => {
    const f = fixture()
    await f.player.play(f.lines[0]); await flushPlayback()
    f.audio.get('one?v=1')!.finish(); f.player.tick()
    if (control === 'pause') f.player.pause()
    if (control === 'seek') f.player.seek(5.2)
    if (control === 'nativeSeek') { f.video.currentTime = 5.2; f.player.videoSeeking() }
    if (control === 'newPlay') { await f.player.play(f.lines[1]); await flushPlayback() }
    if (control === 'release') f.player.release()
    if (control === 'dispose') f.player.dispose()
    f.advance(.3); f.player.tick()
    assert.deepEqual(f.completed, [])
    if (control === 'newPlay') {
      assert.equal(f.audio.get('two')!.paused, false)
      assert.equal(f.video.paused, false)
    }
    f.player.dispose()
  })

test('没有动画帧且视频已结束时，配音的听后空隙仍自然结束且只报告一次', async () => {
  const f = fixture(true, {}, () => new EventMedia())
  await f.player.play(f.lines[0]); await flushPlayback()
  f.video.finish()
  const voice = f.audio.get('one?v=1')! as EventMedia
  voice.finish(); voice.event('ended')
  assert.equal(f.player.playing, true, '视频已到末尾时仍保留短暂听后状态')
  assert.deepEqual(f.completed, [])
  await new Promise(resolve => setTimeout(resolve, 230))
  assert.equal(f.player.playing, false)
  assert.deepEqual(f.completed, [1])
  voice.event('ended'); f.player.tick()
  assert.deepEqual(f.completed, [1])
  f.player.dispose()
})

test('原声单句等外部人声元数据与可播放数据就绪，再从上下文起点一起播放', async () => {
  const f = fixture(true, {}, () => { const media = new EventMedia(); media.readyState = 0; return media })
  await f.player.play(f.lines[2]); await flushPlayback()
  const original = f.audio.get('vocal')! as EventMedia
  assert.equal(f.video.currentTime, 4.85)
  assert.equal(f.video.paused, true)
  assert.equal(original.plays, 0)
  f.advance(4); f.player.tick()
  assert.equal(f.player.time, 4.85, '加载等待不能让原声试听时间先走')
  original.readyState = 1; original.event('loadedmetadata'); await flushPlayback()
  assert.equal(f.video.paused, true, '仅元数据不足以启动视频')
  original.readyState = 4; original.event('canplay'); await flushPlayback()
  assert.equal(original.currentTime, 4.85)
  assert.equal(original.paused, false)
  assert.equal(f.video.paused, false)
  assert.equal(f.video.currentTime, 4.85, '原声准备完成才启动视频，不追播句内位置')
  f.player.dispose()
})

test('原声单句等非零异步定位与play成功，期间不反复seek或提前启动视频', async () => {
  const f = fixture(true, {}, () => { const media = new AsyncSeekMedia(); media.deferSeek = true; return media })
  await f.player.play(f.lines[2]); await flushPlayback()
  const original = f.audio.get('vocal')! as AsyncSeekMedia
  const attempts = delayPlay(original)
  assert.equal(original.seekWrites, 1)
  assert.equal(original.seeking, true)
  assert.equal(f.video.paused, true)
  assert.equal(original.plays, 0)
  for (let i = 0; i < 3; i++) { f.player.tick(); original.event('canplay') }
  assert.equal(original.seekWrites, 1, '定位未完成不能每帧重发')
  original.completeSeek(); await flushPlayback()
  assert.equal(original.currentTime, 4.85)
  assert.equal(attempts.length, 1)
  assert.equal(f.video.paused, true, 'Audio.play尚未成功时视频仍等待')
  attempts[0].resolve(); await flushPlayback()
  assert.equal(f.video.paused, false)
  assert.equal(f.video.currentTime, 4.85)
  assert.equal(original.seekWrites, 1)
  f.player.dispose()
})

test('直接使用视频原声的单句无需等待不存在的外部人声轨', async () => {
  const f = fixture()
  ;(f.player as any).options.sources = () => ({})
  await f.player.play(f.lines[2]); await flushPlayback()
  assert.equal(f.video.paused, false)
  assert.equal(f.video.muted, false)
  assert.equal(f.video.currentTime, 4.85)
  assert.equal(f.audio.size, 0)
  f.player.dispose()
})
