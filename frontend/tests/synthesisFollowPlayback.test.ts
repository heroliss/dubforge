import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

// 联动真实 Vue 生命周期、预览播放器与完成跟随控制器，只替换浏览器媒体。
const bundle = await build({
  stdin: { contents: `
    import { createRenderer, shallowRef, ref, nextTick } from 'vue';
    import { useDubbingPreview } from './src/composables/useDubbingPreview';
    import { useSynthesisPreview } from './src/composables/useSynthesisPreview';
    import { useDubbingFollow } from './src/composables/useDubbingFollow';
    const host = createRenderer({ createComment: () => ({}), insert() {}, remove() {},
      parentNode: () => null, nextSibling: () => null });
    export function mountFollow(initial, firstVideo) {
      const state = shallowRef(initial), video = shallowRef(firstVideo), enabled = shallowRef(false);
      const selected = [], revealed = [], currentSelection = ref(null);
      let preview, follow, lineFollow;
      const app = host.createApp({ setup() {
        preview = useDubbingPreview({ state: () => state.value, video, choices: () => ({}) });
        lineFollow = useDubbingFollow({ lines: () => state.value.lines, selected: currentSelection,
          previewLine: () => preview.cursor.value?.lineId ?? null, seek: preview.seek,
          reveal: id => revealed.push(id) });
        follow = useSynthesisPreview({ enabled: () => enabled.value || lineFollow.mode.value === 'synthesis',
          task: () => state.value.synthesis_task, lines: () => state.value.lines,
          finished: () => preview.finished.value, interrupted: () => preview.interrupted.value,
          error: () => preview.playbackError.value,
          previewLine: () => preview.cursor.value?.lineId ?? null, playing: () => preview.playing.value,
          preservePlaybackOnDisable: () => lineFollow.mode.value === 'preview',
          select: id => { selected.push(id); lineFollow.locate(id, false) },
          play: preview.preview, resume: preview.resume, pause: preview.pause });
        return () => null;
      } });
      app.mount({});
      return { state, video, enabled, selected, revealed, currentSelection, mode: lineFollow.mode,
        preview, follow, unmount: () => app.unmount() };
    }
    export { nextTick };
  `, loader: 'ts', resolveDir: fileURLToPath(new URL('..', import.meta.url)) },
  bundle: true, write: false, format: 'esm', platform: 'node',
  plugins: [{ name: 'synthesis-message-boundary', setup(builder) {
    builder.onResolve({ filter: /^naive-ui$/ }, () => ({ path: 'messages', namespace: 'synthesis-test' }))
    builder.onLoad({ filter: /.*/, namespace: 'synthesis-test' }, () => ({ contents:
      'export const useMessage = () => ({ error(message) { globalThis.__synthesisPreviewErrors?.push(message) } });' }))
  } }],
})
const { mountFollow, nextTick } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

class Media extends EventTarget {
  position = 0; paused = true; playbackRate = 1; muted = false; volume = 1
  readyState = 4; duration = 30; src = ''; ended = false; seeking = false
  plays = 0
  completePlay?: () => void
  get currentTime() { return this.position }
  set currentTime(value: number) { this.position = value }
  async play() {
    this.plays++; this.paused = false; this.ended = false
    this.dispatchEvent(new Event('play'))
  }
  pause() {
    if (this.paused) return
    this.paused = true; this.dispatchEvent(new Event('pause'))
  }
  load() {}
  removeAttribute(name: string) { if (name === 'src') this.src = '' }
  event(name: string) { this.dispatchEvent(new Event(name)) }
  finish() {
    this.currentTime = this.duration; this.ended = true; this.paused = true; this.event('ended')
  }
}

class AsyncSeekVideo extends Media {
  seekRequests: number[] = []
  get currentTime() { return this.position }
  set currentTime(value: number) {
    if (this.position === value) return
    this.position = value; this.seeking = true; this.seekRequests.push(value)
    queueMicrotask(() => this.event('seeking'))
  }
  seekComplete() { this.seeking = false; this.event('seeked') }
  advanceTo(value: number) { this.position = value; this.event('timeupdate') }
}

function initialState() {
  return { lines: [1, 2, 3].map(id => {
    const start = [3, 8, 15][id - 1], version = id === 1 ? 2 : 1
    const url = `sentence-${id}?v=${version}`
    return { id, start, end: start + 4, wav: { exists: true, url, seconds: id === 1 ? 1.2 : 2 },
      selection: { source: 'dub', take: 1 }, versions: [{ id: 1, exists: true, url }] }
  }), synthesis_task: { job_id: 41, active: true, lines: [
    { id: 1, take: 1, status: 'done' }, { id: 2, take: 1, status: 'running' },
    { id: 3, take: 1, status: 'queued' },
  ] }, bg: { found: true, url: 'background' }, vocal: { found: true, url: 'vocal' } }
}

const flush = async () => { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
// 单句自然结束后保留 200ms 听后缓冲，完成跟随只在缓冲结束后接入下一句。
const settleEnd = async () => { await new Promise(resolve => setTimeout(resolve, 230)); await flush() }
function fixture(t: any, delayedFirst = false, initialVideo = new Media(), deferredFirst = false) {
  const frames = new Map<number, () => void>(), audio: Media[] = [], errors: string[] = []
  const descriptors: [string, PropertyDescriptor | undefined][] = []; let sequence = 0, deferredConsumed = false
  for (const [key, value] of Object.entries({
    __synthesisPreviewErrors: errors,
    requestAnimationFrame: (callback: () => void) => { frames.set(++sequence, callback); return sequence },
    cancelAnimationFrame: (id: number) => frames.delete(id),
    Audio: class extends Media {
      constructor(url: string) {
        super(); this.src = url; this.duration = url.startsWith('sentence-1?') ? 1.2
          : url.startsWith('sentence-') ? 2 : 30
        if (delayedFirst && url === 'sentence-1?v=2') { this.readyState = 0; this.duration = NaN }
        audio.push(this)
      }
      play(): Promise<void> {
        if (!deferredFirst || deferredConsumed || this.src !== 'sentence-1?v=2') return super.play()
        deferredConsumed = true; this.plays++
        return new Promise(resolve => {
          this.completePlay = () => {
            this.paused = false; this.ended = false; this.event('play'); resolve()
          }
        })
      }
    },
  })) {
    descriptors.push([key, Object.getOwnPropertyDescriptor(globalThis, key)])
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  const video = initialVideo, f = mountFollow(initialState(), video)
  t.after(async () => {
    f.unmount(); await flush()
    descriptors.forEach(([key, descriptor]) => descriptor
      ? Object.defineProperty(globalThis, key, descriptor) : Reflect.deleteProperty(globalThis, key))
  })
  function pollDone(id: number) {
    f.state.value = { ...f.state.value,
      lines: f.state.value.lines.map((line: any) => line.id !== id ? line : {
        ...line, wav: { ...line.wav, url: `sentence-${id}?v=2` },
        versions: [{ id: 1, exists: true, url: `sentence-${id}?v=2` }],
      }), synthesis_task: { ...f.state.value.synthesis_task,
        lines: f.state.value.synthesis_task.lines.map((row: any) => row.id === id
          ? { ...row, status: 'done' } : row),
      },
    }
  }
  function finish(id: number) {
    const voice = audio.find(item => item.src === `sentence-${id}?v=2`)!
    voice.finish()
    const position = f.state.value.lines.find((line: any) => line.id === id)!.start + voice.duration
    if (video instanceof AsyncSeekVideo) video.advanceTo(position)
    else { video.currentTime = position; video.event('timeupdate') }
  }
  function advance() {
    const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback())
  }
  return { ...f, video, audio, errors, frames, pollDone, finish, advance }
}

test('完成跟随在正确句首开声，短音频自然结束后保留缓冲，再暂停或自动续播', async t => {
  const f = fixture(t)
  f.enabled.value = true; await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  assert.equal(f.follow.status.value, 'playing')
  assert.equal(f.follow.currentLine.value, 1)
  assert.equal(f.video.currentTime, 3); assert.equal(f.video.paused, false)
  assert.equal(first.currentTime, 0); assert.equal(first.paused, false)
  assert.equal(f.preview.interrupted.value, 0)
  assert.deepEqual(f.selected, [1])
  f.finish(1); await flush()
  assert.equal(f.follow.status.value, 'playing', '声音结束后先留出短暂听后缓冲')
  assert.equal(f.video.paused, false)
  assert.deepEqual(f.preview.finished.value, { serial: 0, lineId: null }, '缓冲结束前不通知跟随控制器切句')
  await settleEnd()
  assert.equal(f.follow.status.value, 'waiting')
  assert.equal(f.video.currentTime, 4.2, '只保留短暂缓冲，不继续播放字幕剩余空白；测试媒体位置由用例显式推进')
  assert.equal(f.video.paused, true); assert.ok(f.audio.every(voice => voice.paused))
  assert.deepEqual(f.preview.finished.value, { serial: 1, lineId: 1 })
  assert.equal(f.preview.interrupted.value, 0, '自动结束发出的原生 pause 不算人工暂停')
  f.pollDone(2); await flush()
  const second = f.audio.find(voice => voice.src === 'sentence-2?v=2')!
  assert.equal(f.follow.currentLine.value, 2)
  assert.equal(f.video.currentTime, 8); assert.equal(second.currentTime, 0)
  assert.equal(second.paused, false); assert.equal(first.paused, true)
  assert.deepEqual(f.selected, [1, 2]); assert.equal(f.preview.interrupted.value, 0)
  f.finish(2); await settleEnd()
  assert.equal(f.follow.status.value, 'waiting'); assert.equal(f.video.paused, true)
  assert.deepEqual(f.errors, [])
})

test('轮询快速完成多句只试听最新待播句，当前句保留完整自然尾音', async t => {
  const f = fixture(t)
  f.enabled.value = true; await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  first.currentTime = .5; f.video.currentTime = 3.5; f.video.event('timeupdate'); await flush()
  f.pollDone(2); await flush()
  f.pollDone(3); await flush()
  assert.equal(f.follow.currentLine.value, 1); assert.equal(f.follow.pendingLine.value, 3)
  assert.equal(first.paused, false); assert.equal(first.currentTime, .5)
  assert.equal(f.video.currentTime, 3.5)
  assert.ok(f.audio.filter(voice => voice.src.startsWith('sentence-2?') || voice.src.startsWith('sentence-3?'))
    .every(voice => voice.plays === 0), '新句完成不会抢播或打断当前句')
  f.finish(1); await settleEnd()
  assert.equal(f.follow.currentLine.value, 3); assert.equal(f.video.currentTime, 15)
  assert.equal(f.audio.find(voice => voice.src === 'sentence-3?v=2')!.paused, false)
  assert.equal(first.paused, true); assert.equal(first.ended, true)
  assert.deepEqual(f.selected, [1, 3])
  assert.deepEqual(f.preview.finished.value, { serial: 1, lineId: 1 })
  assert.equal(f.preview.interrupted.value, 0)
})

test('模式切换立即选中并滚动当前句，合成与预览来回切换不重播、不跳头', async t => {
  const f = fixture(t)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  first.currentTime = .5; f.video.currentTime = 3.5; f.video.event('timeupdate'); await flush()
  const videoPlays = f.video.plays, audioPlays = first.plays
  f.currentSelection.value = 3
  f.mode.value = 'preview'
  assert.equal(f.currentSelection.value, 1, '点击跟随预览即刻选中当前句，无需等下一句')
  assert.deepEqual(f.revealed, [1]); assert.equal(f.video.currentTime, 3.5)
  f.mode.value = 'off'; f.currentSelection.value = 3; f.mode.value = 'preview'
  assert.equal(f.currentSelection.value, 1, '同句id重新启用也立即滚动')
  await flush()
  f.mode.value = 'synthesis'; await flush()
  assert.equal(f.follow.currentLine.value, 1); assert.deepEqual(f.selected, [1])
  assert.equal(f.video.currentTime, 3.5); assert.equal(first.currentTime, .5)
  assert.equal(f.video.plays, videoPlays); assert.equal(first.plays, audioPlays)
  f.pollDone(2); await flush()
  assert.equal(f.follow.pendingLine.value, 2)
  f.currentSelection.value = 3; f.mode.value = 'preview'
  assert.equal(f.currentSelection.value, 1)
  await flush()
  assert.equal(first.paused, false); assert.equal(f.video.paused, false)
  assert.equal(f.video.currentTime, 3.5); assert.equal(f.video.plays, videoPlays)
  f.mode.value = 'synthesis'; await flush()
  assert.equal(f.follow.currentLine.value, 1); assert.equal(f.follow.pendingLine.value, 2)
  assert.equal(first.currentTime, .5); assert.equal(first.plays, audioPlays)
  f.finish(1); await settleEnd()
  assert.equal(f.follow.currentLine.value, 2); assert.equal(f.video.currentTime, 8)
  assert.deepEqual(f.selected, [1, 1, 2])
  assert.equal(f.audio.find(voice => voice.src === 'sentence-2?v=2')!.plays, 1)
  assert.deepEqual(f.errors, [])
})

test('最新音频加载前视频停在句首，加载后同步启动，等待期间程序暂停取消迟到启动', async t => {
  const f = fixture(t, true)
  f.enabled.value = true; await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  assert.equal(f.video.paused, true); assert.equal(first.paused, true)
  assert.equal(f.video.currentTime, 3); assert.equal(f.preview.interrupted.value, 0)
  f.follow.suspend(); await flush()
  first.readyState = 4; first.duration = 1.2; first.event('loadedmetadata')
  f.advance(); await flush()
  assert.equal(f.follow.status.value, 'paused')
  assert.equal(f.video.paused, true); assert.equal(first.plays, 0)
  assert.equal(f.preview.finished.value.serial, 0)
  f.follow.resume(); await flush()
  assert.equal(f.follow.status.value, 'playing')
  assert.equal(f.video.paused, false); assert.equal(f.video.currentTime, 3)
  assert.equal(first.paused, false); assert.equal(first.currentTime, 0)
  assert.equal(f.preview.interrupted.value, 0)
  f.finish(1); await settleEnd()
  assert.equal(f.follow.status.value, 'waiting'); assert.equal(f.video.paused, true)
})

test('视频异步定位尚未 seeked 时音频先就绪也不抢跑，定位完成才同步开声并自动续播', async t => {
  const video = new AsyncSeekVideo(), f = fixture(t, true, video)
  f.enabled.value = true; await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  assert.equal(video.seeking, true); assert.equal(video.currentTime, 3)
  assert.equal(video.paused, true); assert.equal(video.plays, 0)
  assert.equal(first.plays, 0)
  first.duration = 1.2; first.readyState = 4
  first.event('loadedmetadata'); first.event('canplay'); f.advance(); await flush()
  assert.equal(video.seeking, true)
  assert.equal(video.paused, true); assert.equal(video.plays, 0)
  assert.equal(first.plays, 0, '音频先就绪必须等待视频定位结束')
  assert.equal(f.preview.interrupted.value, 0, '异步程序定位不会误判为用户拖动')
  video.seekComplete(); await flush()
  assert.equal(video.seeking, false); assert.equal(video.paused, false)
  assert.equal(first.paused, false); assert.equal(first.currentTime, 0)
  assert.deepEqual(video.seekRequests, [3], '完成准备不重复定位造成新一轮 seeking')
  f.finish(1); await settleEnd()
  assert.equal(f.follow.status.value, 'waiting'); assert.equal(video.paused, true)
  assert.equal(f.preview.interrupted.value, 0)
  f.pollDone(2); await flush()
  const second = f.audio.find(voice => voice.src === 'sentence-2?v=2')!
  assert.equal(video.seeking, true); assert.equal(video.currentTime, 8)
  assert.equal(video.paused, true); assert.equal(second.plays, 0)
  video.seekComplete(); await flush()
  assert.equal(video.paused, false); assert.equal(second.paused, false)
  assert.equal(second.currentTime, 0); assert.deepEqual(video.seekRequests, [3, 8])
  assert.equal(f.preview.interrupted.value, 0)
  f.finish(2); await settleEnd()
  assert.equal(f.follow.status.value, 'waiting'); assert.equal(video.paused, true)
  assert.deepEqual(f.errors, [])
})

test('音频 play 请求尚未成功时视频保持暂停，成功后才从句首启动', async t => {
  const f = fixture(t, false, new Media(), true)
  f.enabled.value = true; await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  assert.equal(first.plays, 1)
  assert.equal(f.video.currentTime, 3); assert.equal(f.video.plays, 0)
  assert.equal(f.video.paused, true); assert.equal(f.preview.finished.value.serial, 0)
  f.advance(); await flush()
  assert.equal(first.plays, 1, '等待音频启动的帧不能重复提交播放')
  assert.equal(f.video.paused, true)
  first.completePlay!(); await flush()
  assert.equal(first.paused, false); assert.equal(first.currentTime, 0)
  assert.equal(f.video.paused, false); assert.equal(f.video.currentTime, 3)
  assert.equal(f.preview.interrupted.value, 0)
  f.finish(1); await settleEnd()
  assert.equal(f.follow.status.value, 'waiting'); assert.equal(f.video.paused, true)
  assert.deepEqual(f.errors, [])
})

test('准备阶段暂停后迟到的 seeked 或音频播放成功都不能重启视频', async t => {
  const video = new AsyncSeekVideo(), f = fixture(t, false, video, true)
  f.enabled.value = true; await flush()
  const first = f.audio.find(voice => voice.src === 'sentence-1?v=2')!
  assert.equal(video.seeking, true); assert.equal(first.plays, 0)
  f.follow.suspend(); await flush()
  video.seekComplete(); f.advance(); await flush()
  assert.equal(video.paused, true); assert.equal(video.plays, 0)
  assert.equal(first.plays, 0); assert.equal(f.follow.status.value, 'paused')
  f.follow.resume(); await flush()
  assert.equal(first.plays, 1); assert.equal(video.paused, true)
  f.follow.suspend(); await flush()
  first.completePlay!(); f.advance(); await flush()
  assert.equal(first.paused, true); assert.equal(video.paused, true)
  assert.equal(video.plays, 0); assert.equal(f.follow.status.value, 'paused')
  assert.equal(f.preview.finished.value.serial, 0); assert.equal(f.preview.interrupted.value, 0)
  assert.deepEqual(f.errors, [])
})

for (const intervention of ['pause', 'seeking'] as const) test(`用户原生 ${intervention} 暂停自动跟随，轮询不抢播，明确继续后播放最新完成句`, async t => {
  const f = fixture(t)
  f.enabled.value = true; await flush()
  if (intervention === 'pause') f.video.pause()
  else {
    f.video.currentTime = 3.6; f.video.seeking = true; f.video.event('seeking')
    f.video.seeking = false; f.video.event('seeked')
  }
  await flush()
  assert.equal(f.follow.status.value, 'paused')
  assert.equal(f.preview.interrupted.value, 1)
  assert.equal(f.preview.finished.value.serial, 0)
  f.pollDone(2); await flush()
  assert.equal(f.follow.status.value, 'paused')
  assert.equal(f.follow.pendingLine.value, 2)
  assert.deepEqual(f.selected, [1]); assert.equal(f.video.currentTime, intervention === 'pause' ? 3 : 3.6)
  assert.ok(f.audio.filter(voice => voice.src === 'sentence-2?v=2').every(voice => voice.plays === 0))
  f.follow.resume(); await flush()
  assert.equal(f.follow.status.value, 'playing'); assert.equal(f.follow.currentLine.value, 2)
  assert.equal(f.video.currentTime, 8)
  assert.equal(f.audio.find(voice => voice.src === 'sentence-2?v=2')!.paused, false)
  assert.deepEqual(f.selected, [1, 2]); assert.equal(f.preview.interrupted.value, 1)
  f.finish(2); await settleEnd()
  assert.equal(f.follow.status.value, 'waiting'); assert.equal(f.video.paused, true)
  assert.equal(f.preview.interrupted.value, 1)
  assert.deepEqual(f.errors, [])
})
