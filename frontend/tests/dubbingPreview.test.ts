import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

// 使用真正的 Vue 客户端生命周期和时间轴，只替换浏览器媒体与消息边界。
const root = fileURLToPath(new URL('..', import.meta.url))
const bundle = await build({
  stdin: { contents: `
    import { createRenderer, h, shallowRef, nextTick } from 'vue';
    import { useDubbingPreview } from './src/composables/useDubbingPreview';
    const host = createRenderer({ createComment: () => ({}), insert() {}, remove() {},
      parentNode: () => null, nextSibling: () => null });
    export function mountPreview(initial, firstVideo) {
      const state = shallowRef(initial), video = shallowRef(firstVideo);
      let preview;
      const app = host.createApp({ setup() {
        preview = useDubbingPreview({ state: () => state.value, video, choices: () => ({}) });
        return () => null;
      } });
      app.mount({});
      return { state, video, preview, unmount: () => app.unmount() };
    }
    export { nextTick };
  `, loader: 'ts', resolveDir: root },
  bundle: true, write: false, format: 'esm', platform: 'node',
  plugins: [{ name: 'preview-message-boundary', setup(builder) {
    builder.onResolve({ filter: /^naive-ui$/ }, () => ({ path: 'messages', namespace: 'preview-test' }))
    builder.onLoad({ filter: /.*/, namespace: 'preview-test' }, () => ({ contents: 'export const useMessage = () => ({ error(message) { globalThis.__dubbingPreviewErrors?.push(message) } });' }))
  } }],
})
const { mountPreview, nextTick } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

class Media extends EventTarget {
  currentTime = 0; paused = true; playbackRate = 1; muted = false; volume = 1
  readyState = 4; duration = 15; src = ''; ended = false; seeking = false
  webkitDisplayingFullscreen = false
  pauses = 0; loads = 0
  async play() { this.paused = false; this.ended = false; this.dispatchEvent(new Event('play')) }
  pause() {
    this.pauses++
    if (this.paused) return
    this.paused = true; this.dispatchEvent(new Event('pause'))
  }
  load() { this.loads++ }
  removeAttribute(name: string) { if (name === 'src') this.src = '' }
  event(name: string) { this.dispatchEvent(new Event(name)) }
}
function state() {
  return { lines: [
    { id: 1, start: 0, end: 2, wav: { exists: true, url: 'first', seconds: 2 }, selection: { source: 'dub', take: 1 } },
    { id: 2, start: 3, end: 5, wav: { exists: false, url: 'second', seconds: 2 }, selection: { source: 'dub', take: null } },
  ], synthesis_task: { active: true }, bg: { found: true, url: 'background' }, vocal: { found: true, url: 'vocal' } }
}
function fixture(t: any, firstVideo = new Media()) {
  const frames = new Map<number, () => void>(), audio: Media[] = [], errors: string[] = []; let next = 0
  const originals: [string, PropertyDescriptor | undefined][] = []
  for (const [key, value] of Object.entries({
    __dubbingPreviewErrors: errors,
    requestAnimationFrame: (callback: () => void) => { frames.set(++next, callback); return next },
    cancelAnimationFrame: (id: number) => { frames.delete(id) },
    Audio: class extends Media { constructor(url: string) { super(); this.src = url; this.duration = url === 'first' || url === 'second' ? 2 : 15; audio.push(this) } },
  })) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, key)
    originals.push([key, descriptor])
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
  }
  const f = mountPreview(state(), firstVideo)
  t.after(async () => {
    f.unmount(); await flush()
    originals.forEach(([key, descriptor]) => descriptor ? Object.defineProperty(globalThis, key, descriptor) : Reflect.deleteProperty(globalThis, key))
  })
  const advance = () => {
    const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback())
  }
  return { ...f, audio, frames, advance, firstVideo, errors }
}
const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); await nextTick() }

test('监听总音量和静音作用实际人声背景，保留项目混音与播放位置',async t=>{
  const f=fixture(t)
  f.preview.volDub.value=.4;f.preview.volBg.value=.6
  await f.preview.playAll();await flush()
  f.firstVideo.currentTime=.8;f.advance();await flush()
  const voice=f.audio.find(audio=>audio.src==='first')!,background=f.audio.find(audio=>audio.src==='background')!
  f.preview.setMonitorAudio({volume:.5,muted:false});await flush()
  assert.equal(voice.volume,.2);assert.equal(background.volume,.3)
  assert.deepEqual(f.preview.monitorAudio.value,{volume:.5,muted:false})
  const position=voice.currentTime,count=f.audio.length
  f.preview.setMonitorAudio({volume:.5,muted:true});await flush()
  assert.equal(voice.muted,true);assert.equal(background.muted,true)
  assert.equal(voice.paused,false);assert.equal(voice.currentTime,position)
  f.preview.setMonitorAudio({volume:.5,muted:false});await flush()
  assert.equal(voice.muted,false);assert.equal(background.muted,false);assert.equal(f.audio.length,count)
  assert.equal(f.preview.volDub.value,.4);assert.equal(f.preview.volBg.value,.6)
  assert.equal(f.firstVideo.currentTime,.8);assert.equal(f.firstVideo.muted,true)
})

test('仅听人声立即改变当前背景监听，单句和连续播放都保留选择',async t=>{
  const f=fixture(t)
  await f.preview.playAll();await flush()
  const background=f.audio.find(audio=>audio.src==='background')!
  assert.equal(background.paused,false)
  f.preview.solo.value=true;await flush();assert.equal(background.paused,true)
  f.preview.solo.value=false;await flush();assert.equal(background.paused,false)
  f.preview.solo.value=true;await flush()
  f.firstVideo.currentTime=.7;f.firstVideo.event('seeking');f.firstVideo.event('seeked');await flush()
  assert.equal(background.paused,true,'手动拖动接管连续试听也保留仅听人声选择')
  f.preview.pause();await f.preview.playAll();await flush()
  assert.equal(f.preview.solo.value,true);assert.equal(background.paused,true)
  f.preview.solo.value=false;await flush();assert.equal(background.paused,false)
})

test('逐句试听增益与项目混音和监听总音量相乘，不改变保存参数或背景',async t=>{
  const f=fixture(t),data=structuredClone(f.state.value)
  data.lines[0].audio_gain_db=-6;f.state.value=data
  f.preview.volDub.value=.8;f.preview.volBg.value=.6
  f.preview.setMonitorAudio({volume:.5,muted:false})
  await f.preview.playAll();await flush()
  const voice=f.audio.find(audio=>audio.src==='first')!,background=f.audio.find(audio=>audio.src==='background')!
  assert.ok(Math.abs(voice.volume-.8*10**(-6/20)*.5)<1e-10)
  assert.equal(background.volume,.3);assert.equal(f.preview.volDub.value,.8)
  assert.equal(f.state.value.lines[0].audio_gain_db,-6)
})

test('真实预览组合读取服务端排布时间，单句和原生连续播放沿用同一时钟', async t => {
  const f = fixture(t)
  const next = structuredClone(f.state.value)
  next.lines[0] = { ...next.lines[0], playback_start: .5, playback_end: 2.5 }
  f.state.value = next
  await f.preview.preview(next.lines[0]); await flush()
  assert.equal(f.firstVideo.currentTime, .5)
  f.firstVideo.currentTime = 1.25; f.advance(); await flush()
  assert.equal(f.audio.find(audio => audio.src === 'first')!.currentTime, .75)
  assert.equal(f.preview.cursor.value.lineId, 1)
  f.preview.pause()
  assert.equal(f.state.value.lines[0].start, 0)
  f.firstVideo.currentTime = 1.5
  await f.firstVideo.play(); await flush()
  assert.equal(f.audio.find(audio => audio.src === 'first')!.currentTime, 1)
  assert.equal(f.state.value.lines[0].playback_start, .5)
})

test('后台合成轮询补齐未来句子，不暂停当前试听或重建健康音轨', async t => {
  const f = fixture(t)
  await f.preview.playAll(); f.firstVideo.currentTime = .7; f.advance(); await flush()
  const current = f.audio.find(audio => audio.src === 'first')!, background = f.audio.find(audio => audio.src === 'background')!
  f.state.value = { ...f.state.value, lines: f.state.value.lines.map((line: any) => line.id === 2
    ? { ...line, wav: { ...line.wav, exists: true }, selection: { source: 'dub', take: 1 } } : line) }
  await nextTick(); f.advance(); await flush()
  assert.equal(current.paused, false); assert.equal(background.paused, false)
  assert.equal(f.firstVideo.paused, false); assert.equal(f.firstVideo.currentTime, .7)
  assert.equal(f.audio.find(audio => audio.src === 'first'), current)
  assert.equal(f.audio.find(audio => audio.src === 'background'), background)
  current.currentTime = 2; current.ended = true; current.paused = true
  f.firstVideo.currentTime = 3.2; f.firstVideo.event('timeupdate'); await flush()
  assert.equal(current.paused, true)
  assert.equal(f.audio.find(audio => audio.src === 'second')!.paused, false)
  assert.equal(f.preview.cursor.value.lineId, 2)
})

test('视频 DOM 更换立即停止旧视频和音轨，保留定位并移除旧事件', async t => {
  const f = fixture(t)
  await f.preview.playAll(); f.firstVideo.currentTime = 1.1; f.advance(); await flush()
  const old = [...f.audio], replacement = new Media(); replacement.readyState = 0
  // 原生 beforeUnmount 指令先停止视频并清空资源，ref 随后才通知播放器。
  f.firstVideo.pause(); f.firstVideo.currentTime = 0; f.firstVideo.readyState = 0
  f.video.value = replacement
  assert.equal(f.firstVideo.paused, true); assert.equal(f.firstVideo.loads, 1)
  assert.ok(old.every(audio => audio.paused && audio.loads === 1))
  assert.equal(f.preview.playing.value, false); assert.equal(f.preview.cursor.value, null)
  assert.equal(f.preview.time.value, 1.1)
  f.firstVideo.currentTime = 9; f.firstVideo.event('play'); f.firstVideo.event('timeupdate')
  assert.equal(f.preview.time.value, 1.1); assert.equal(f.preview.playing.value, false)
  replacement.readyState = 4; replacement.event('loadedmetadata')
  assert.equal(replacement.currentTime, 1.1)
  assert.equal(replacement.paused, true)
})

test('视频拖动完成后无需等待下一帧即可恢复同步，暂停立即更新播放状态', async t => {
  const f = fixture(t)
  await f.preview.playAll(); await flush()
  const first = f.audio.find(audio => audio.src === 'first')!
  f.firstVideo.seeking = true; f.firstVideo.currentTime = 1.2; f.firstVideo.event('seeking')
  assert.equal(first.paused, true)
  f.firstVideo.seeking = false; f.firstVideo.event('seeked'); await flush()
  assert.equal(first.paused, false); assert.equal(first.currentTime, 1.2)
  f.preview.pause()
  assert.equal(f.preview.playing.value, false); assert.equal(f.preview.time.value, 1.2)
  assert.equal(first.paused, true)
})

test('全屏切换先触发 pause 时不停止配音，fullscreenchange 到来后恢复同一时间轴', async t => {
  const f = fixture(t)
  const video = f.firstVideo as Media & { requestFullscreen?: () => Promise<void>; webkitDisplayingFullscreen?: boolean }
  // 真实 HTMLVideoElement 具备 requestFullscreen；测试媒体补上这个能力
  // 后即可覆盖仅在原生全屏路径启用的短暂 pause 保护。
  video.requestFullscreen = async () => {}
  await f.preview.playAll(); await flush(); f.advance(); await flush()
  const first = f.audio.find(audio => audio.src === 'first')!
  assert.equal(f.preview.playing.value, true)

  // Chromium 某些版本的事件顺序是 pause → fullscreenchange。
  video.pause()
  assert.equal(f.preview.playing.value, true, '过渡窗口内保留播放状态')
  video.webkitDisplayingFullscreen = true
  video.event('fullscreenchange')
  await flush()
  assert.equal(video.paused, false)
  assert.equal(first.paused, false)
  assert.equal(f.preview.playing.value, true)

  // 退出全屏也可能重复同样的顺序，不能让第二次切换造成双播或丢声。
  video.pause()
  video.webkitDisplayingFullscreen = false
  video.event('fullscreenchange')
  await flush()
  assert.equal(video.paused, false)
  assert.equal(first.paused, false)
  assert.equal(f.preview.playing.value, true)

  // 全屏状态稳定后，用户主动暂停必须仍然生效，不能被过渡保护误恢复。
  video.webkitDisplayingFullscreen = true
  video.event('fullscreenchange')
  await new Promise(resolve => setTimeout(resolve, 300))
  video.pause()
  await new Promise(resolve => setTimeout(resolve, 260)); await flush()
  assert.equal(video.paused, true)
  assert.equal(first.paused, true)
  assert.equal(f.preview.playing.value, false)
})

test('原生全屏切换造成的短暂 pause 不会停止配音，退出全屏后继续同步', async t => {
  const f = fixture(t)
  await f.preview.playAll(); f.firstVideo.currentTime = .8; f.advance(); await flush()
  const voice = f.audio.find(audio => audio.src === 'first')!
  assert.equal(f.preview.playing.value, true)
  // Chromium 的部分版本在全屏过渡中会先暂停 video，再在一个任务后恢复。
  f.firstVideo.webkitDisplayingFullscreen = true
  f.firstVideo.event('fullscreenchange')
  f.firstVideo.pause()
  await new Promise(resolve => setTimeout(resolve, 0)); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(voice.paused, false)
  assert.equal(f.preview.playing.value, true)
  assert.equal(f.preview.interrupted.value, 0)

  // 退出全屏同样可能发生一次 pause，不能把人声轨留在暂停状态。
  f.firstVideo.webkitDisplayingFullscreen = false
  f.firstVideo.event('fullscreenchange')
  f.firstVideo.pause()
  await new Promise(resolve => setTimeout(resolve, 0)); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(voice.paused, false)
  assert.equal(f.preview.interrupted.value, 0)
})

test('全屏过渡结束后用户暂停仍保持暂停，不会被误恢复', async t => {
  const f = fixture(t)
  await f.preview.playAll(); await flush()
  const voice = f.audio.find(audio => audio.src === 'first')!
  f.firstVideo.webkitDisplayingFullscreen = true
  f.firstVideo.event('fullscreenchange')
  // 超过全屏 pause 保护窗口后，这次 pause 明确是用户操作。
  await new Promise(resolve => setTimeout(resolve, 150))
  f.firstVideo.pause(); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(voice.paused, true)
  assert.equal(f.preview.playing.value, false)
})

test('支持全屏 API 的视频快速暂停再播放不会让配音停在首秒', async t => {
  const f = fixture(t)
  // 真实 HTMLVideoElement 都有 requestFullscreen；测试媒体默认没有，
  // 这里显式打开该分支以覆盖原生控件和点击画面的路径。
  Object.assign(f.firstVideo, { requestFullscreen: async () => {} })
  await f.preview.playAll(); f.firstVideo.currentTime = .8; f.advance(); await flush()
  const voice = f.audio.find(audio => audio.src === 'first')!
  assert.equal(voice.currentTime, .8)
  f.firstVideo.pause()
  await f.firstVideo.play(); await flush()
  // 保护窗口结束后仍应沿用同一时间轴，不能由旧 pause 候选再次暂停音频。
  await new Promise(resolve => setTimeout(resolve, 150)); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(voice.paused, false)
  assert.equal(f.preview.playing.value, true)
})

test('原生视频暂停超过保护窗口后再播放仍从暂停位置恢复配音', async t => {
  const f = fixture(t)
  Object.assign(f.firstVideo, { requestFullscreen: async () => {} })
  await f.preview.playAll(); f.firstVideo.currentTime = .8; f.advance(); await flush()
  const voice = f.audio.find(audio => audio.src === 'first')!
  voice.currentTime = .8
  f.firstVideo.pause()
  await new Promise(resolve => setTimeout(resolve, 180)); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(voice.paused, true)
  await f.firstVideo.play(); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(voice.paused, false)
  assert.equal(voice.currentTime, .8)
  assert.equal(f.preview.playing.value, true)
})

for (const outcome of ['resolve', 'reject'] as const) test(`原生暂停使在途视频播放失效，旧 ${outcome} 不重启播放或弹出错误`, async t => {
  const video = new Media(); let finish!: () => void, fail!: (error: Error) => void
  const pending = new Promise<void>((resolve, reject) => { finish = resolve; fail = reject })
  video.play = () => { video.paused = false; video.event('play'); return pending }
  const f = fixture(t, video), started = f.preview.playAll()
  await flush()
  assert.equal(video.paused, false)
  video.pause()
  assert.equal(f.preview.playing.value, false)
  if (outcome === 'resolve') { video.paused = false; finish() }
  else { const error = new Error('浏览器取消旧播放'); error.name = 'AbortError'; fail(error) }
  await started; await flush()
  assert.equal(video.paused, true)
  assert.ok(f.audio.every(audio => audio.paused))
  assert.deepEqual(f.errors, []); assert.equal(f.preview.playbackError.value, '')
})

test('视频 ref 经空档和多帧后重挂，加载前后仍保留七秒定位', async t => {
  const f = fixture(t)
  await f.preview.playAll(); f.firstVideo.currentTime = 7; f.advance(); await flush()
  assert.equal(f.preview.time.value, 7)
  f.video.value = undefined; await nextTick(); f.advance(); await flush()
  assert.equal(f.preview.time.value, 7)
  const replacement = new Media(); replacement.readyState = 0
  f.video.value = replacement; await nextTick(); f.advance(); await flush()
  assert.equal(f.preview.time.value, 7); assert.equal(replacement.paused, true)
  replacement.readyState = 4; replacement.event('loadedmetadata'); f.advance(); await flush()
  assert.equal(replacement.currentTime, 7); assert.equal(f.preview.time.value, 7)
  assert.equal(replacement.paused, true)
})

test('卸载清空媒体和帧调度，旧帧和事件不能重启预览', async t => {
  const f = fixture(t)
  await f.preview.playAll(); await flush()
  const staleFrame = [...f.frames.values()][0]!
  f.unmount()
  await flush()
  assert.equal(f.frames.size, 0)
  assert.equal(f.firstVideo.paused, true); assert.equal(f.firstVideo.loads, 1)
  assert.ok(f.audio.every(audio => audio.paused && audio.loads === 1))
  staleFrame(); f.firstVideo.event('play'); f.firstVideo.event('timeupdate')
  assert.equal(f.frames.size, 0); assert.equal(f.preview.playing.value, false)
})

test('单句自然结束先暂停全部媒体，再递增只读 finished，后续帧不重复完成', async t => {
  const f = fixture(t)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  assert.equal(f.preview.interrupted.value, 0, '自动播放产生的原生 play 不是人工接管')
  const first = f.audio.find(audio => audio.src === 'first')!
  first.currentTime = 2; first.ended = true; first.paused = true
  f.firstVideo.currentTime = 1.98; f.advance(); await flush()
  await new Promise(resolve => setTimeout(resolve, 230)); await flush()
  assert.equal(f.preview.playing.value, false)
  assert.equal(f.firstVideo.paused, true)
  assert.ok(f.audio.every(audio => audio.paused))
  assert.deepEqual(f.preview.finished.value, { serial: 1, lineId: 1 })
  assert.equal(f.preview.interrupted.value, 0, '自动结束产生的 pause 不是人工暂停')
  f.advance(); f.advance(); f.firstVideo.event('timeupdate'); await flush()
  assert.deepEqual(f.preview.finished.value, { serial: 1, lineId: 1 })
})

test('缓冲、内部定位与程序暂停不算人工干预，原生暂停和播放才递增 interrupted', async t => {
  const f = fixture(t)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  f.firstVideo.seeking = true; f.firstVideo.event('seeking')
  f.firstVideo.seeking = false; f.firstVideo.event('seeked')
  f.firstVideo.readyState = 2; f.firstVideo.event('waiting')
  f.firstVideo.readyState = 4; f.firstVideo.event('playing'); await flush()
  assert.equal(f.preview.interrupted.value, 0)
  assert.equal(f.preview.finished.value.serial, 0)
  f.preview.pause(); await flush()
  assert.equal(f.preview.interrupted.value, 0)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  f.firstVideo.pause(); await flush()
  assert.equal(f.preview.interrupted.value, 1)
  assert.equal(f.preview.finished.value.serial, 0)
  await f.firstVideo.play(); await flush()
  assert.equal(f.preview.interrupted.value, 2)
  assert.equal(f.preview.playing.value, true)
})

test('自动单句定位与 DOM 恢复不报人工拖动，真正拖动接管连续播放', async t => {
  const f = fixture(t)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  f.firstVideo.seeking = true; f.firstVideo.event('seeking')
  f.firstVideo.seeking = false; f.firstVideo.event('seeked')
  assert.equal(f.preview.interrupted.value, 0)
  f.firstVideo.seeking = true; f.firstVideo.currentTime = 1.3; f.firstVideo.event('seeking')
  assert.equal(f.preview.interrupted.value, 1)
  f.firstVideo.seeking = false; f.firstVideo.event('seeked'); await flush()
  assert.equal(f.preview.playing.value, true)
  assert.equal(f.preview.finished.value.serial, 0)
  f.preview.pause()
  const replacement = new Media(); replacement.readyState = 0
  f.video.value = replacement; await nextTick()
  replacement.readyState = 4; replacement.event('loadedmetadata')
  replacement.seeking = true; replacement.event('seeking')
  replacement.seeking = false; replacement.event('seeked')
  assert.equal(replacement.currentTime, 1.3)
  assert.equal(f.preview.interrupted.value, 1)
})

test('上句自动 pause 排队到下一句 play 之后，不会误判为人工暂停', async t => {
  const video = new Media(), pauses: (() => void)[] = []
  video.pause = () => {
    video.pauses++
    if (video.paused) return
    video.paused = true; pauses.push(() => video.event('pause'))
  }
  const f = fixture(t, video)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  const first = f.audio.find(audio => audio.src === 'first')!
  first.currentTime = 2; first.ended = true; first.paused = true
  f.firstVideo.currentTime = 2; f.advance(); await flush()
  await new Promise(resolve => setTimeout(resolve, 230)); await flush()
  assert.deepEqual(f.preview.finished.value, { serial: 1, lineId: 1 })
  f.state.value = { ...f.state.value, lines: f.state.value.lines.map((line: any) => line.id === 2
    ? { ...line, wav: { ...line.wav, exists: true }, selection: { source: 'dub', take: 1 } } : line) }
  await f.preview.preview(f.state.value.lines[1]); await flush()
  assert.equal(video.paused, false)
  pauses.shift()!(); await flush()
  assert.equal(f.preview.interrupted.value, 0)
  assert.equal(f.preview.playing.value, true)
  assert.equal(f.audio.find(audio => audio.src === 'second')!.paused, false)
})

test('后台帧循环不运行时，音频 canplay 自动从句首启动，ended 后短暂留白再通知完成', async t => {
  const f = fixture(t)
  await f.preview.playAll(); await flush(); f.preview.pause()
  const first = f.audio.find(audio => audio.src === 'first')!
  first.readyState = 0; first.duration = NaN
  await f.preview.preview(f.state.value.lines[0]); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.preview.playing.value, false)
  first.readyState = 1; first.duration = 2; first.event('loadedmetadata'); await flush()
  assert.equal(f.firstVideo.paused, true)
  first.readyState = 4; first.event('canplay'); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(first.currentTime, 0)
  assert.equal(first.paused, false)
  assert.equal(f.preview.playing.value, true)
  f.firstVideo.currentTime = 1.97
  first.currentTime = 2; first.ended = true; first.paused = true; first.event('ended'); await flush()
  assert.equal(f.firstVideo.paused, false, '声音自然结束后保留短暂听后空隙')
  assert.deepEqual(f.preview.finished.value, { serial: 0, lineId: null })
  await new Promise(resolve => setTimeout(resolve, 230)); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.firstVideo.currentTime, 1.97, '后台不依赖下一帧，也能结束听后空隙')
  assert.equal(f.preview.playing.value, false)
  assert.deepEqual(f.preview.finished.value, { serial: 1, lineId: 1 })
  assert.equal(f.preview.interrupted.value, 0)
})

test('后台无帧循环时视频元数据也能解除单句准备，音频 error 及时停止等待', async t => {
  const f = fixture(t)
  f.firstVideo.readyState = 0
  await f.preview.preview(f.state.value.lines[0]); await flush()
  assert.equal(f.firstVideo.paused, true)
  f.firstVideo.readyState = 4; f.firstVideo.event('loadedmetadata'); await flush()
  assert.equal(f.firstVideo.paused, false)
  f.preview.pause()
  const first = f.audio.find(audio => audio.src === 'first')!
  first.readyState = 0
  await f.preview.preview(f.state.value.lines[0]); await flush()
  Object.assign(first, { error: { code: 2 } }); first.event('error'); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.errors.length, 1)
  assert.ok(f.preview.playbackError.value)
  first.event('error'); first.readyState = 4; first.event('canplay'); await flush()
  assert.equal(f.errors.length, 1)
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.preview.finished.value.serial, 0)
})

test('版本退役及卸载后的旧音频事件不启动媒体或发完成通知', async t => {
  const f = fixture(t)
  await f.preview.preview(f.state.value.lines[0]); await flush()
  const old = f.audio.find(audio => audio.src === 'first')!
  f.state.value = { ...f.state.value, lines: f.state.value.lines.map((line: any) => line.id === 1
    ? { ...line, wav: { ...line.wav, url: 'replacement' } } : line) }
  await nextTick()
  f.firstVideo.event('timeupdate'); await flush()
  const current = f.audio.find(audio => audio.src === 'replacement')!
  assert.equal(old.src, '')
  old.ended = true; old.event('ended'); old.event('canplay'); old.event('loadedmetadata')
  Object.assign(old, { error: { code: 2 } }); old.event('error'); await flush()
  assert.equal(current.paused, false)
  assert.equal(f.preview.finished.value.serial, 0)
  assert.equal(f.errors.length, 0)
  f.unmount(); await flush()
  current.ended = true; current.event('ended'); current.event('canplay'); await flush()
  assert.equal(f.preview.finished.value.serial, 0)
  assert.equal(f.preview.playing.value, false)
  assert.equal(f.errors.length, 0)
})

test('新 DOM 元数据到来时恢复旧定位不能覆盖期间选中的新单句', async t => {
  const f = fixture(t)
  await f.preview.playAll(); f.firstVideo.currentTime = 7; f.firstVideo.event('timeupdate'); await flush()
  const replacement = new Media(); replacement.readyState = 0
  f.video.value = replacement; await nextTick()
  assert.equal(f.preview.time.value, 7)
  f.state.value = { ...f.state.value, lines: f.state.value.lines.map((line: any) => line.id === 2
    ? { ...line, wav: { ...line.wav, exists: true }, selection: { source: 'dub', take: 1 } } : line) }
  await f.preview.preview(f.state.value.lines[1]); await flush()
  replacement.currentTime = 0
  replacement.readyState = 4; replacement.event('loadedmetadata'); await flush()
  assert.equal(replacement.currentTime, 3, '迟到元数据沿用本次句首，不恢复七秒旧定位')
  assert.equal(replacement.paused, false)
  assert.equal(f.preview.interrupted.value, 0)
})

// 输出恢复故意停留在 suspended，重现浏览器允许 play() 但尚未开声的窗口。
function deferredListeningGain(t: any) {
  const contexts: any[] = [], sources: any[] = []
  class Context {
    state = 'suspended'; destination = {}; resumes = 0
    complete!: () => void
    fail!: (error: Error) => void
    pending = new Promise<void>((resolve, reject) => {
      this.complete = () => { this.state = 'running'; resolve() }; this.fail = reject
    })
    constructor() { contexts.push(this) }
    createMediaElementSource(media: any) {
      const source = { media, connect() {}, disconnect() {} }; sources.push(source); return source
    }
    createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} } }
    resume() { this.resumes++; return this.pending }
    async close() { this.state = 'closed' }
    suspendAgain() {
      this.state = 'suspended'
      this.pending = new Promise<void>((resolve, reject) => {
        this.complete = () => { this.state = 'running'; resolve() }; this.fail = reject
      })
    }
  }
  const original = Object.getOwnPropertyDescriptor(globalThis, 'AudioContext')
  Object.defineProperty(globalThis, 'AudioContext', { configurable: true, value: Context })
  t.after(() => original ? Object.defineProperty(globalThis, 'AudioContext', original) : Reflect.deleteProperty(globalThis, 'AudioContext'))
  return { contexts, sources }
}

for (const mode of ['single', 'continuous'] as const) test(`正增益${mode}先等待输出恢复，再启动媒体和时钟`, async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  f.preview.volDub.value = 2
  const started = mode === 'single' ? f.preview.preview(f.state.value.lines[0]) : f.preview.playAll()
  await flush(); f.advance(); await flush()
  assert.equal(gain.contexts.length, 1)
  assert.equal(gain.contexts[0].resumes, 1)
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.firstVideo.currentTime, 0)
  assert.equal(f.audio.length, 0, '输出尚未恢复时不启动任何媒体')
  gain.contexts[0].complete(); await started; await flush()
  assert.equal(f.firstVideo.paused, false)
  const voice = f.audio.find(audio => audio.src === 'first')!
  assert.equal(voice.paused, false)
  assert.equal(voice.currentTime, 0)
  assert.ok(gain.sources.some(source => source.media === voice))
  assert.equal(gain.contexts[0].resumes, 1)
  assert.deepEqual(f.errors, [])
})

test('连续预览预先考虑后续逐句正增益，默认百分百监听不创建输出节点', async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  await f.preview.playAll(); await flush()
  assert.equal(gain.contexts.length, 0)
  f.preview.pause()
  const data = structuredClone(f.state.value)
  data.lines[1] = { ...data.lines[1], audio_gain_db: 6, wav: { ...data.lines[1].wav, exists: true }, selection: { source: 'dub', take: 1 } }
  f.state.value = data
  const started = f.preview.playAll(); await flush()
  assert.equal(gain.contexts.length, 1)
  assert.equal(f.firstVideo.paused, true)
  gain.contexts[0].complete(); await started; await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(gain.contexts[0].resumes, 1)
})

for (const action of ['pause', 'seek', 'stop', 'replace', 'unmount'] as const) test(`等待输出期间${action}使旧播放失效，迟到恢复不重新开声`, async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  f.preview.volDub.value = 2
  const started = f.preview.preview(f.state.value.lines[0]); await flush()
  if (action === 'pause') f.preview.pause()
  if (action === 'seek') f.preview.seek(1.2)
  if (action === 'stop') f.preview.stopAll()
  if (action === 'replace') f.video.value = new Media()
  if (action === 'unmount') f.unmount()
  gain.contexts[0].complete(); await started; await flush(); f.advance(); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.preview.playing.value, false)
  assert.ok(f.audio.every(audio => audio.paused))
  assert.deepEqual(f.errors, [])
})

test('等待输出期间切换句子只启动最新选择，恢复同句仍保留暂停位置', async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  f.preview.volDub.value = 2
  const data = structuredClone(f.state.value)
  data.lines[1] = { ...data.lines[1], wav: { ...data.lines[1].wav, exists: true }, selection: { source: 'dub', take: 1 } }
  f.state.value = data
  const first = f.preview.preview(data.lines[0]), second = f.preview.preview(data.lines[1]); await flush()
  assert.equal(gain.contexts[0].resumes, 1)
  gain.contexts[0].complete(); await Promise.all([first, second]); await flush()
  assert.equal(f.firstVideo.currentTime, 3)
  assert.equal(f.preview.cursor.value.lineId, 2)
  assert.ok(f.audio.filter(audio => audio.src === 'first').every(audio => audio.paused))
  const voice = f.audio.find(audio => audio.src === 'second')!
  f.firstVideo.currentTime = 3.7; voice.currentTime = .7; f.advance(); await flush()
  f.preview.pause(); gain.contexts[0].suspendAgain()
  assert.equal(f.preview.resume(2), true); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.firstVideo.currentTime, 3.7)
  gain.contexts[0].complete(); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(f.firstVideo.currentTime, 3.7)
  assert.ok(Math.abs(voice.currentTime - .7) < 1e-9)
})

test('原生视频播放在输出恢复前保持原位，恢复后不吞掉开头', async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  f.preview.volDub.value = 2
  await f.firstVideo.play(); await flush()
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.firstVideo.currentTime, 0)
  gain.contexts[0].complete(); await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(f.audio.find(audio => audio.src === 'first')!.currentTime, 0)
  assert.equal(gain.contexts[0].resumes, 1)
  assert.equal(f.preview.interrupted.value, 1, '原生播放依然通知跟随控制器本次人工接管')
})

test('暂停同句的输出恢复仍可取消，迟到恢复不推翻再次暂停', async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  f.preview.volDub.value = 2
  const started = f.preview.preview(f.state.value.lines[0]); await flush()
  gain.contexts[0].complete(); await started; await flush()
  f.firstVideo.currentTime = .6; f.advance(); await flush()
  f.preview.pause(); gain.contexts[0].suspendAgain()
  assert.equal(f.preview.resume(1), true); await flush()
  f.preview.pause(); gain.contexts[0].complete(); await flush()
  assert.equal(f.firstVideo.currentTime, .6)
  assert.equal(f.firstVideo.paused, true)
  assert.ok(f.audio.every(audio => audio.paused))
})

test('输出恢复失败不先走时钟，用户重试成功后从句首开始', async t => {
  const gain = deferredListeningGain(t), f = fixture(t)
  f.preview.volDub.value = 2
  const first = f.preview.preview(f.state.value.lines[0]); await flush()
  gain.contexts[0].fail(new Error('浏览器尚未允许开声')); await first; await flush()
  assert.equal(f.firstVideo.currentTime, 0)
  assert.equal(f.firstVideo.paused, true)
  assert.equal(f.audio.length, 0)
  assert.equal(f.errors.length, 1)
  gain.contexts[0].suspendAgain()
  const retry = f.preview.preview(f.state.value.lines[0]); await flush()
  gain.contexts[0].complete(); await retry; await flush()
  assert.equal(f.firstVideo.paused, false)
  assert.equal(f.audio.find(audio => audio.src === 'first')!.currentTime, 0)
  assert.equal(f.preview.playbackError.value, '')
})
