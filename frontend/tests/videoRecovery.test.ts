import assert from 'node:assert/strict'
import test from 'node:test'
import { createVideoRecovery, type VideoPreviewState } from '../src/utils/videoRecovery.ts'

function setup() {
  let nextId = 0
  const pending = new Map<number, () => void>(), states: VideoPreviewState[] = [], calls: string[] = []
  const video = {
    currentTime: 42, duration: 100, videoWidth: 1920, videoHeight: 1080, style: { aspectRatio: '' },
    error: null as { code: number } | null,
    pause: () => calls.push('pause'), removeAttribute: () => calls.push('remove'),
    setAttribute: (_key: string, url: string) => calls.push(url), load: () => calls.push('load'),
  }
  const recovery = createVideoRecovery(() => video as unknown as HTMLVideoElement, () => '/final.mp4?v=unchanged',
    value => states.push(value), {
      set: fn => { const id = ++nextId; pending.set(id, fn); return id as any },
      clear: id => { pending.delete(id as any) },
    })
  const expire = () => { const timers = [...pending.values()]; pending.clear(); timers.forEach(fn => fn()) }
  return { video, recovery, pending, states, calls, expire }
}

test('反复拖动只保留一个缓冲计时，不自动重载；超时给出恢复入口', () => {
  const { recovery, pending, calls, states, expire } = setup()
  for (let i = 0; i < 20; i++) recovery.waiting()
  assert.equal(pending.size, 1)
  expire()
  assert.equal(states.at(-1)?.kind, 'stalled')
  assert.deepEqual(calls, [])
  recovery.waiting()
  assert.equal(pending.size, 0)
})

test('恢复先释放旧请求，保留当前位置和内容版本，等待元数据后恢复且不自动播放', () => {
  const { recovery, video, states, calls } = setup()
  recovery.remember(); recovery.retry(); recovery.retry()
  assert.deepEqual(calls, ['pause', 'remove', 'load', '/final.mp4?v=unchanged', 'load'])
  video.currentTime = 0
  recovery.remember() // 加载时的零位置不能覆盖用户原先的42秒。
  recovery.metadata()
  assert.equal(video.currentTime, 42)
  assert.equal(video.style.aspectRatio, '1920 / 1080')
  recovery.ready()
  assert.equal(states.at(-1)?.kind, 'idle')
})

test('重载还未收到元数据便失败或超时，仍允许用户再次恢复，保留原位置', () => {
  for (const fail of [true, false]) {
    const { recovery, video, calls, expire } = setup()
    recovery.retry(); video.currentTime = 0
    if (fail) { video.error = { code: 2 }; recovery.failed() } else expire()
    recovery.retry()
    assert.equal(calls.filter(v => v === '/final.mp4?v=unchanged').length, 2)
    video.duration = 30
    recovery.metadata()
    assert.equal(video.currentTime, 29.99)
  }
})

test('正常加载/离开预览都清理计时；主动中止不提示成片损坏', () => {
  const { recovery, video, states, pending } = setup()
  recovery.waiting(); recovery.ready()
  assert.equal(pending.size, 0)
  video.error = { code: 1 }; recovery.failed()
  assert.equal(states.at(-1)?.kind, 'idle')
  recovery.waiting(); recovery.dispose()
  assert.equal(pending.size, 0)
  const count = states.length
  recovery.failed(); recovery.waiting(); recovery.retry()
  assert.equal(states.length, count)
})

test('仅预读元数据的暂停视频正常就绪，不因没有canplay而误报卡住', () => {
  const { recovery, states, pending, expire } = setup()
  recovery.waiting(); recovery.metadata()
  assert.equal(pending.size, 0)
  expire()
  assert.equal(states.at(-1)?.kind, 'idle')
  recovery.waiting(); expire()
  assert.equal(states.at(-1)?.kind, 'stalled')
})

test('网络与解码错误给出不同提示，不触发任何后台重试', () => {
  const { recovery, video, states, calls } = setup()
  video.error = { code: 2 }; recovery.failed()
  assert.match(states.at(-1)!.message, /后台连接/)
  video.error = { code: 3 }; recovery.failed()
  assert.match(states.at(-1)!.message, /解码/)
  assert.deepEqual(calls, [])
})
