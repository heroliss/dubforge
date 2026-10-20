import assert from 'node:assert/strict'
import test from 'node:test'
import { createCutPreviewPlayback, type CutPreviewJump, type CutPreviewMedia } from '../src/utils/cutPreviewPlayback.ts'
import { buildCutPreviewTimeline } from '../src/utils/cutPreviewTimeline.ts'

class Media implements CutPreviewMedia {
  position = 0; paused = false; seeking = false; playbackRate = 1; readyState = 2; ended = false
  seeks: number[] = []; pauses = 0; sequence = 0
  frames = new Map<number, (now: number, metadata: { mediaTime: number }) => void>()
  get currentTime() { return this.position }
  set currentTime(value: number) { this.position = value; this.seeks.push(value); this.seeking = true }
  pause() { this.paused = true; this.pauses++ }
  requestVideoFrameCallback(callback: (now: number, metadata: { mediaTime: number }) => void) {
    const handle = ++this.sequence; this.frames.set(handle, callback); return handle
  }
  cancelVideoFrameCallback(handle: number) { this.frames.delete(handle) }
  paint(time = this.position) {
    const callbacks = [...this.frames.values()]; this.frames.clear()
    callbacks.forEach(callback => callback(0, { mediaTime: time }))
  }
}
class Clock {
  now = 0; sequence = 0; tasks = new Map<number, { at: number; run: () => void }>()
  private media: Media
  constructor(media: Media) { this.media = media }
  setTimeout = (run: () => void, delay: number) => {
    const handle = ++this.sequence; this.tasks.set(handle, { at: this.now + delay, run }); return handle
  }
  clearTimeout = (handle: unknown) => { this.tasks.delete(handle as number) }
  advance(milliseconds: number) {
    const target = this.now + milliseconds
    let turns = 0
    while (true) {
      const next = [...this.tasks.entries()].filter(([, task]) => task.at <= target + 1e-7)
        .sort((a, b) => a[1].at - b[1].at)[0]
      if (!next) break
      assert.ok(++turns < 100, '调度器不应形成忙循环')
      const [handle, task] = next
      this.move(task.at); this.tasks.delete(handle); task.run()
    }
    this.move(target)
  }
  private move(time: number) {
    if (!this.media.paused && !this.media.seeking) this.media.position += (time - this.now) / 1000 * this.media.playbackRate
    this.now = time
  }
}
function fixture(ranges = [{ start: 2, end: 4 }]) {
  const media = new Media(), clock = new Clock(media)
  let timeline = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: ranges })
  const prepared: CutPreviewJump[] = [], jumped: CutPreviewJump[] = [], settled: CutPreviewJump[] = []
  const player = createCutPreviewPlayback({ getMedia: () => media, getTimeline: () => timeline, clock,
    onPrepareJump: event => prepared.push(event), onJump: event => jumped.push(event), onSettled: event => settled.push(event) })
  return { media, clock, player, prepared, jumped, settled,
    replace: (ranges: Array<{ start: number; end: number }>) => { timeline = buildCutPreviewTimeline({ mediaDuration: 10, fps: 30, deleteRanges: ranges }) } }
}

test('不用timeupdate，单一截止计时器在最后两帧准备并按边界跳过', () => {
  const value = fixture()
  value.player.refresh()
  assert.equal(value.clock.tasks.size, 1)
  assert.equal(value.media.frames.size, 1)
  value.clock.advance(1900)
  assert.equal(value.prepared.length, 0)
  value.clock.advance(100)
  assert.equal(value.prepared.length, 1)
  assert.ok(value.prepared[0]!.from >= 2 - 2 / 30 - .001)
  assert.ok(value.prepared[0]!.from < 2)
  assert.equal(value.jumped.length, 1)
  assert.deepEqual(value.media.seeks, [4])
  assert.equal(value.clock.tasks.size, 0)
  assert.equal(value.settled.length, 0)
  value.media.seeking = false
  value.player.refresh()
  assert.equal(value.settled.length, 1)
  assert.equal(value.clock.tasks.size, 1)
  value.player.destroy()
})

test('视频帧回调发现迟到的删除帧立即跳转且不抓删除帧做遮罩', () => {
  const value = fixture()
  value.player.refresh()
  value.media.position = 2.01
  value.media.paint(2)
  assert.deepEqual(value.media.seeks, [4])
  assert.equal(value.prepared.length, 0)
  assert.equal(value.jumped[0]?.reason, 'cut')
  value.player.destroy()
})

test('seeking期间重复refresh与旧帧回调不能重复写currentTime', () => {
  const value = fixture()
  value.player.refresh()
  const stale = [...value.media.frames.values()][0]!
  value.clock.advance(2000)
  for (let index = 0; index < 10; index++) value.player.refresh()
  stale(0, { mediaTime: 3 })
  assert.deepEqual(value.media.seeks, [4])
  assert.equal(value.settled.length, 0)
  value.media.seeking = false; value.media.readyState = 1
  value.player.refresh()
  assert.equal(value.settled.length, 0)
  value.media.readyState = 2; value.player.refresh()
  assert.equal(value.settled.length, 1)
  value.player.destroy()
})

test('暂停和stop取消全部旧计时器与帧回调，恢复不会沿旧截止时间跳转', () => {
  const value = fixture()
  value.player.refresh()
  const stale = [...value.media.frames.values()][0]!
  value.media.paused = true; value.player.refresh()
  assert.equal(value.clock.tasks.size, 0)
  assert.equal(value.media.frames.size, 0)
  stale(0, { mediaTime: 3 }); value.clock.advance(10000)
  assert.deepEqual(value.media.seeks, [])
  value.media.paused = false; value.player.refresh()
  value.player.stop()
  assert.equal(value.clock.tasks.size, 0)
  assert.equal(value.media.frames.size, 0)
})

test('播放速率变化重新计算唯一截止时间', () => {
  const value = fixture()
  value.player.refresh(); value.clock.advance(1000)
  value.media.playbackRate = 2; value.player.refresh()
  assert.equal(value.clock.tasks.size, 1)
  value.clock.advance(500)
  assert.deepEqual(value.media.seeks, [4])
  value.player.destroy()
})

test('实时修改删除清单取消旧回调并按最新边界跳转', () => {
  const value = fixture()
  value.player.refresh()
  const stale = [...value.media.frames.values()][0]!
  value.replace([{ start: 6, end: 8 }]); value.player.refresh()
  stale(0, { mediaTime: 3 })
  value.clock.advance(2000)
  assert.deepEqual(value.media.seeks, [])
  value.clock.advance(4000)
  assert.deepEqual(value.media.seeks, [8])
  value.player.destroy()
})

test('尾部删除停在最后保留帧，不 seek 到媒体终点或形成循环', () => {
  const value = fixture([{ start: 5, end: 10 }])
  value.media.position = 4.9; value.player.refresh(); value.clock.advance(100)
  assert.equal(value.media.paused, true)
  assert.equal(value.media.pauses, 1)
  assert.ok(Math.abs(value.media.seeks[0]! - 149 / 30) < 1e-9)
  assert.equal(value.jumped[0]?.reason, 'tail')
  value.media.seeking = false; value.player.refresh()
  for (let index = 0; index < 5; index++) value.player.refresh()
  assert.equal(value.media.seeks.length, 1)
  assert.equal(value.clock.tasks.size, 0)
  assert.equal(value.settled.length, 1)
  value.player.destroy()
})

test('全删直接暂停且不 seek，空清单无需特殊多播放器', () => {
  const value = fixture([{ start: 0, end: 10 }])
  value.player.refresh(); value.player.refresh()
  assert.equal(value.media.pauses, 1)
  assert.deepEqual(value.media.seeks, [])
  assert.equal(value.clock.tasks.size, 0)
  assert.equal(value.media.frames.size, 0)
  value.player.destroy()
})

test('暂停时用户定位不强跳，播放后立刻转至下一保留帧', () => {
  const value = fixture()
  value.media.position = 3; value.media.paused = true; value.player.refresh()
  assert.deepEqual(value.media.seeks, [])
  value.media.paused = false; value.player.refresh()
  assert.deepEqual(value.media.seeks, [4])
  value.player.destroy()
})

test('destroy后任何旧回调及refresh都不再调度或寻址', () => {
  const value = fixture()
  value.player.refresh()
  const stale = [...value.media.frames.values()][0]!
  value.player.destroy(); stale(0, { mediaTime: 3 })
  value.player.refresh(); value.clock.advance(10000)
  assert.equal(value.clock.tasks.size, 0)
  assert.equal(value.media.frames.size, 0)
  assert.deepEqual(value.media.seeks, [])
})

test('没有rVFC的浏览器仍由截止计时器守住边界，并在seeked恢复', () => {
  const value = fixture()
  value.media.requestVideoFrameCallback = undefined as unknown as Media['requestVideoFrameCallback']
  value.player.refresh(); value.clock.advance(2000)
  assert.deepEqual(value.media.seeks, [4])
  value.media.seeking = false; value.player.refresh()
  assert.equal(value.settled.length, 1)
  assert.equal(value.clock.tasks.size, 1)
  value.player.destroy()
})

test('60fps目标被currentTime微秒舍入后seeked仍能解除遮罩且不再次seek', () => {
  const media = new Media(), clock = new Clock(media)
  Object.defineProperty(media, 'currentTime', {
    get() { return Number(media.position.toFixed(6)) },
    set(value: number) { media.position = Number(value.toFixed(6)); media.seeks.push(value); media.seeking = true },
  })
  const timeline = buildCutPreviewTimeline({ mediaDuration: 10, fps: 60,
    deleteRanges: [{ start: 1, end: 122 / 60 }] })
  let settled = 0
  const player = createCutPreviewPlayback({ getMedia: () => media, getTimeline: () => timeline,
    clock, onSettled: () => { settled++ } })
  player.refresh(); clock.advance(1000)
  assert.equal(media.seeks.length, 1)
  assert.equal(media.currentTime, 2.033333)
  media.seeking = false; player.refresh()
  assert.equal(settled, 1)
  assert.equal(media.seeks.length, 1)
  media.paint(media.currentTime); player.refresh()
  assert.equal(media.seeks.length, 1)
  assert.equal(clock.tasks.size, 1)
  player.destroy()
})
