import assert from 'node:assert/strict'
import test, { type TestContext } from 'node:test'
import { createReferenceAudioPreview, referencePreviewMaxSeconds } from '../src/utils/referenceAudioPreview.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

// 模拟浏览器事件与播放承诺，验证媒体生命周期而不依赖真实音频或服务。
class FakeAudio extends EventTarget {
  src: string
  preload = ''
  duration = 200
  paused = true
  seeking = false
  calls: string[] = []
  playedAt: number[] = []
  position = 0
  playResult: Promise<void> = Promise.resolve()
  onloadedmetadata: (() => void) | null = null
  onseeked: (() => void) | null = null
  onplaying: (() => void) | null = null
  onwaiting: (() => void) | null = null
  ontimeupdate: (() => void) | null = null
  onended: (() => void) | null = null
  onpause: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) { super(); this.src = url }
  get currentTime() { return this.position }
  set currentTime(value: number) { this.calls.push(`seek:${value}`); this.position = value; this.seeking = true }
  load() { this.calls.push('load') }
  pause() { this.calls.push('pause'); this.paused = true; this.onpause?.() }
  removeAttribute(name: string) { this.calls.push(`remove:${name}`); if (name === 'src') this.src = '' }
  play() { this.calls.push('play'); this.playedAt.push(this.currentTime); this.paused = false; return this.playResult }
  fire(name: 'loadedmetadata' | 'seeked' | 'playing' | 'waiting' | 'timeupdate' | 'ended' | 'pause' | 'error') {
    if (name === 'seeked') this.seeking = false
    this.dispatchEvent(new Event(name))
    this[`on${name}`]?.()
  }
}

const base = '/api/projects/abc123/workbench'
const clip = (extra: Record<string, unknown> = {}) => ({
  url: `${base}/audio/vocal?v=123`, start: 42.25, end: 47.25, line_id: 7, ...extra,
})
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

function harness(t: TestContext) {
  const states: string[] = [], errors: string[] = [], audios: FakeAudio[] = []
  const controller = createReferenceAudioPreview({
    state: value => states.push(value), error: value => errors.push(value),
    createAudio: url => {
      const audio = new FakeAudio(url)
      audios.push(audio)
      return audio as unknown as HTMLAudioElement
    },
  })
  t.after(() => controller.stop())
  return { ...controller, states, errors, audios, lastState: () => states.at(-1), failures: () => errors.filter(Boolean) }
}

test('连接失败显示中文引导，不把浏览器英文异常直接放进设置表单', async t => {
  t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('Failed to fetch') })
  const preview = harness(t)
  await preview.play('abc123', 7, 15)
  assert.equal(preview.lastState(), 'idle')
  assert.match(preview.failures()[0], /读取参考原声失败.*后台连接/)
  assert.doesNotMatch(preview.failures()[0], /Failed to fetch/)
  assert.equal(preview.audios.length, 0)
})

test('试听长度复用当前逐句上限，固定参考最多十五秒，异常值回退五秒', () => {
  for (const [input, expected] of [
    [15, 15], [30, 15], ['10', 10], [3.5, 3.5], [0, .5], [-1, .5],
    [undefined, 5], [null, 5], [NaN, 5], [Infinity, 5], ['无效', 5],
  ] as const) assert.equal(referencePreviewMaxSeconds(input), expected, String(input))
})

test('获取 canonical 分离原声并在元数据就绪后定位原句时间再播放', async t => {
  const seen: { url: string; init?: RequestInit }[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, init?: RequestInit) => {
    seen.push({ url, init }); return response(clip())
  })
  const preview = harness(t)
  await preview.play('abc123', 7, 15)
  assert.equal(seen[0].url, `${base}/reference-audio?line_id=7&max_seconds=15`)
  assert.equal(seen[0].init?.cache, 'no-store')
  assert.ok(seen[0].init?.signal instanceof AbortSignal)
  assert.equal(preview.lastState(), 'loading')
  const audio = preview.audios[0]
  assert.equal(audio.src, `${base}/audio/vocal?v=123`)
  assert.equal(audio.preload, 'metadata')
  assert.deepEqual(audio.calls, ['load'])
  audio.fire('loadedmetadata')
  assert.deepEqual(audio.calls, ['load', 'seek:42.25'])
  assert.equal(audio.playedAt.length, 0, '定位完成前不能输出旧位置的音频')
  audio.fire('seeked')
  assert.deepEqual(audio.calls, ['load', 'seek:42.25', 'play'])
  assert.deepEqual(audio.playedAt, [42.25])
  audio.fire('loadedmetadata')
  assert.equal(audio.playedAt.length, 1)
  audio.fire('playing')
  assert.equal(preview.lastState(), 'playing')
  assert.deepEqual(preview.failures(), [])
})

test('动态参考上限传给读取接口，短句按原句结束而不补足上限', async t => {
  const urls: string[] = []
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    urls.push(url); return response(clip({ end: 44.25 }))
  })
  const preview = harness(t)
  await preview.play('abc123', 7, referencePreviewMaxSeconds(3))
  assert.equal(urls[0], `${base}/reference-audio?line_id=7&max_seconds=3`)
  assert.equal(preview.audios[0].src, `${base}/audio/vocal?v=123`)
})

test('播放到片段终点立即停止并释放音频请求和事件处理器', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('loadedmetadata'); audio.fire('seeked'); audio.fire('playing')
  audio.position = 47.25; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'idle')
  assert.deepEqual(audio.calls.slice(-3), ['pause', 'remove:src', 'load'])
  assert.equal(audio.src, '')
  assert.equal(audio.ontimeupdate, null)
  assert.equal(audio.onloadedmetadata, null)
  assert.equal(audio.onseeked, null)
  assert.equal(audio.onpause, null)
})

test('带缓冲的试听从前置时间播放，不会在实际参考终点或提前二十毫秒停止', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip({
    start: 42.1, end: 47.5, reference_start: 42.25, reference_end: 47.25,
  })))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('loadedmetadata'); audio.fire('seeked'); audio.fire('playing')
  assert.deepEqual(audio.playedAt, [42.1])
  audio.position = 47.25; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'playing', '核心参考结束后仍应播放尾部缓冲')
  audio.position = 47.49; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'playing', '不能提前二十毫秒关闭播放器')
  audio.position = 47.5; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'idle')
})

test('第一句从音轨零点直接播放完整缓冲，不等待不会出现的 seeked', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip({
    start: 0, end: .69, reference_start: 0, reference_end: .44,
  })))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('loadedmetadata'); audio.fire('playing')
  assert.deepEqual(audio.playedAt, [0])
  audio.position = .44; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'playing')
  audio.position = .69; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'idle')
})

test('定位或加载未完成有超时提示，清理后迟到 seeked 不能启动旧媒体', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('loadedmetadata')
  assert.equal(audio.playedAt.length, 0)
  t.mock.timers.tick(20000)
  assert.equal(preview.lastState(), 'idle')
  assert.match(preview.failures()[0], /定位或加载超时/)
  audio.fire('seeked')
  assert.equal(audio.playedAt.length, 0)
})

test('核心参考和试听缓冲分别受限，畸形缓冲合同不会播放', async t => {
  const candidates = [
    { reference_start: 42.25 }, { reference_start: null, reference_end: 47.25 },
    { reference_start: 42.25, reference_end: 47.25, start: 42.09 },
    { reference_start: 42.25, reference_end: 47.25, end: 47.51 },
    { reference_start: 42.25, reference_end: 47.26, end: 47.5 },
    { reference_start: 42.24, reference_end: 47.25 },
    { reference_start: 42.25, reference_end: 47.26 },
  ]
  let index = 0
  t.mock.method(globalThis, 'fetch', async () => response(clip(candidates[index++])))
  const preview = harness(t)
  for (const candidate of candidates) {
    await preview.play('abc123', 7, 5)
    assert.equal(preview.lastState(), 'idle', JSON.stringify(candidate))
    assert.match(preview.errors.at(-1)!, /时间信息无效/, JSON.stringify(candidate))
  }
  assert.equal(preview.audios.length, 0)
})

test('没有 timeupdate 事件时终点计时器仍会结束片段', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('loadedmetadata'); audio.fire('seeked'); audio.fire('playing')
  audio.position = 47.25
  t.mock.timers.tick(5000)
  assert.equal(preview.lastState(), 'idle')
  assert.equal(audio.src, '')
})

test('缓冲时显示正在加载，恢复后继续在当前片段内播放', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('loadedmetadata'); audio.fire('seeked'); audio.fire('playing'); audio.fire('waiting')
  assert.equal(preview.lastState(), 'loading')
  audio.fire('playing')
  assert.equal(preview.lastState(), 'playing')
  assert.deepEqual(audio.playedAt, [42.25])
})

test('音色和情感试听互斥，开始后一个会停止和释放前一个', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const voice = harness(t), emotion = harness(t)
  await voice.play('abc123', 7, 5)
  voice.audios[0].fire('loadedmetadata'); voice.audios[0].fire('seeked'); voice.audios[0].fire('playing')
  await emotion.play('abc123', 7, 5)
  assert.equal(voice.lastState(), 'idle')
  assert.equal(voice.audios[0].src, '')
  emotion.audios[0].fire('loadedmetadata'); emotion.audios[0].fire('seeked'); emotion.audios[0].fire('playing')
  assert.equal(emotion.lastState(), 'playing')
  voice.stop()
  assert.equal(emotion.lastState(), 'playing')
  assert.notEqual(emotion.audios[0].src, '')
  assert.deepEqual(voice.failures(), [])
})

test('切换试听也取消前一个未完成请求，迟到读取不会抢回播放权', async t => {
  const pending = deferred<Response>()
  const signals: AbortSignal[] = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: RequestInit) => {
    signals.push(init.signal as AbortSignal)
    return signals.length === 1 ? pending.promise : Promise.resolve(response(clip()))
  })
  const voice = harness(t), emotion = harness(t)
  const old = voice.play('abc123', 7, 5)
  await emotion.play('abc123', 7, 5)
  assert.equal(signals[0].aborted, true)
  pending.resolve(response(clip())); await old
  assert.equal(voice.audios.length, 0)
  assert.equal(emotion.audios.length, 1)
  assert.equal(voice.lastState(), 'idle')
  assert.equal(emotion.lastState(), 'loading')
  assert.deepEqual(voice.failures(), [])
})

test('离开页面式 stop 取消读取，迟到网络失败不覆盖其他试听状态', async t => {
  const pending = deferred<Response>()
  let signal: AbortSignal | undefined
  t.mock.method(globalThis, 'fetch', (_url: string, init: RequestInit) => {
    signal = init.signal as AbortSignal; return pending.promise
  })
  const preview = harness(t)
  const playing = preview.play('abc123', 7, 5)
  preview.stop()
  assert.equal(signal?.aborted, true)
  pending.reject(new Error('旧读取失败')); await playing
  assert.equal(preview.lastState(), 'idle')
  assert.equal(preview.audios.length, 0)
  assert.deepEqual(preview.failures(), [])
})

test('取消旧播放后迟到的 play 拒绝不能停止新试听或显示过期错误', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const oldAudio = preview.audios[0], oldPlay = deferred<void>()
  oldAudio.playResult = oldPlay.promise
  oldAudio.fire('loadedmetadata'); oldAudio.fire('seeked')
  await preview.play('abc123', 7, 5)
  const newAudio = preview.audios[1]
  newAudio.fire('loadedmetadata'); newAudio.fire('seeked'); newAudio.fire('playing')
  oldPlay.reject(Object.assign(new Error('旧播放被阻止'), { name: 'NotAllowedError' }))
  await Promise.resolve(); await Promise.resolve()
  assert.equal(preview.lastState(), 'playing')
  assert.notEqual(newAudio.src, '')
  assert.deepEqual(preview.failures(), [])
})

test('参考缺失或过期显示后端中文原因，重新点击可清除错误并重试', async t => {
  const replies = [
    response({ message: '尚无可试听的分离人声，请先完成「音轨分离」' }, 400),
    response({ message: '分离人声已过期或正在更新，请先完成「音轨分离」' }, 409),
    response(clip()),
  ]
  t.mock.method(globalThis, 'fetch', async () => replies.shift()!)
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  assert.equal(preview.lastState(), 'idle')
  assert.match(preview.errors.at(-1)!, /尚无可试听.*音轨分离/)
  await preview.play('abc123', 7, 5)
  assert.match(preview.errors.at(-1)!, /已过期.*音轨分离/)
  await preview.play('abc123', 7, 5)
  assert.equal(preview.lastState(), 'loading')
  assert.equal(preview.errors.at(-1), '')
  assert.equal(preview.audios.length, 1)
})

test('浏览器播放权限错误有明确中文重试指引，媒体已被释放', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.playResult = Promise.reject(Object.assign(new Error('permission denied'), { name: 'NotAllowedError' }))
  audio.fire('loadedmetadata'); audio.fire('seeked')
  await Promise.resolve(); await Promise.resolve()
  assert.equal(preview.lastState(), 'idle')
  assert.equal(audio.src, '')
  assert.match(preview.errors.at(-1)!, /浏览器.*再点一次试听/)
})

test('读取超时给出中文重试信息，不泄漏底层异常文字', async t => {
  t.mock.method(globalThis, 'fetch', async () => {
    throw Object.assign(new Error('signal timed out'), { name: 'TimeoutError' })
  })
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  assert.equal(preview.lastState(), 'idle')
  assert.equal(preview.audios.length, 0)
  assert.equal(preview.errors.at(-1), '读取参考原声超时，请重试。')
})

test('媒体加载失败会释放播放器，并提示检查音轨分离和后台连接', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.fire('error')
  assert.equal(preview.lastState(), 'idle')
  assert.equal(audio.src, '')
  assert.match(preview.errors.at(-1)!, /加载或播放失败.*音轨分离.*后台连接/)
})

test('拒绝错误句号、非法时间及超出本次时长的片段，不创建播放器', async t => {
  const candidates = [
    { line_id: 8 }, { start: -1 }, { start: null }, { start: '42.25' },
    { end: 42.25 }, { end: 41 }, { end: null }, { end: 47.5 },
    { url: '/api/projects/other/workbench/audio/vocal?v=1' },
    { url: 'https://example.com/vocal.wav' },
    { url: `${base}/audio/vocalevil` }, { url: `${base}/audio/vocal/another` },
    { url: `${base}/audio/vocal#t=0,60` }, { url: `${base}/audio/vocal?v=1#t=0,60` },
  ]
  let index = 0
  t.mock.method(globalThis, 'fetch', async () => response(clip(candidates[index++])))
  const preview = harness(t)
  for (const candidate of candidates) {
    await preview.play('abc123', 7, 5)
    assert.equal(preview.lastState(), 'idle', JSON.stringify(candidate))
    assert.match(preview.errors.at(-1)!, /时间信息无效/, JSON.stringify(candidate))
  }
  assert.equal(preview.audios.length, 0)
})

test('元数据中的音轨结束早于原句起点时停止并说明越界原因', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.duration = 40; audio.fire('loadedmetadata')
  assert.equal(audio.playedAt.length, 0)
  assert.equal(audio.src, '')
  assert.equal(preview.lastState(), 'idle')
  assert.match(preview.errors.at(-1)!, /超出参考原声音轨范围/)
})

test('音轨比句子略短时裁到实际终点，不能越过音轨继续播放', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  await preview.play('abc123', 7, 5)
  const audio = preview.audios[0]
  audio.duration = 45; audio.fire('loadedmetadata'); audio.fire('seeked'); audio.fire('playing')
  audio.position = 45; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'idle')
  assert.deepEqual(preview.failures(), [])
})

test('无效媒体时长不能开始播放，错误可由再次选择参考句恢复', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(clip()))
  const preview = harness(t)
  for (const duration of [NaN, Infinity, 0, -1]) {
    await preview.play('abc123', 7, 5)
    const audio = preview.audios.at(-1)!
    audio.duration = duration; audio.fire('loadedmetadata')
    assert.equal(preview.lastState(), 'idle', String(duration))
    assert.equal(audio.playedAt.length, 0, String(duration))
    assert.ok(preview.errors.at(-1), String(duration))
  }
  await preview.play('abc123', 7, 5)
  preview.audios.at(-1)!.fire('loadedmetadata'); preview.audios.at(-1)!.fire('seeked'); preview.audios.at(-1)!.fire('playing')
  assert.equal(preview.lastState(), 'playing')
  assert.equal(preview.errors.at(-1), '')
})

const fileResponse = (path = 'D:\\参考音频\\voice.wav') => response({
  url: `/api/files/reference-audio/content?path=${encodeURIComponent(path)}&v=123`, filename: 'voice.wav',
})

test('文件试听读取当前未保存路径，保留中文及路径符号，元数据就绪后从头播放完整文件', async t => {
  const requests: { url: string; init?: RequestInit }[] = []
  const path = 'D:\\参考音频\\a & b #1.wav'
  t.mock.method(globalThis, 'fetch', async (url: string, init?: RequestInit) => {
    requests.push({ url, init }); return fileResponse(path)
  })
  const preview = harness(t)
  await preview.playFile(`  ${path}  `)
  assert.equal(requests[0].url, `/api/files/reference-audio?path=${encodeURIComponent(path)}`)
  assert.equal(requests[0].init?.cache, 'no-store')
  assert.ok(requests[0].init?.signal instanceof AbortSignal)
  const audio = preview.audios[0]
  assert.deepEqual(audio.calls, ['load'])
  audio.fire('loadedmetadata'); audio.fire('playing')
  assert.deepEqual(audio.calls, ['load', 'play'], '完整文件不重复定位，也不添加媒体片段')
  assert.deepEqual(audio.playedAt, [0])
  assert.equal(audio.src.includes('#'), false)
  audio.position = 60; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'playing', '超过模型参考上限仍继续完整试听')
  audio.position = audio.duration; audio.fire('timeupdate')
  assert.equal(preview.lastState(), 'playing', '完整文件只由自然 ended 结束')
  audio.fire('ended')
  assert.equal(preview.lastState(), 'idle')
  assert.equal(audio.src, '')
  assert.deepEqual(preview.failures(), [])
})

test('文件与参考句试听共用播放权，双向切换停止并释放旧播放器', async t => {
  t.mock.method(globalThis, 'fetch', async (url: string) => url.startsWith('/api/files/') ? fileResponse() : response(clip()))
  const sentence = harness(t), file = harness(t)
  await sentence.play('abc123', 7, 5)
  const original = sentence.audios[0]
  original.fire('loadedmetadata'); original.fire('seeked'); original.fire('playing')
  await file.playFile('D:\\voice.wav')
  assert.equal(original.src, '')
  assert.equal(sentence.lastState(), 'idle')
  const full = file.audios[0]
  full.fire('loadedmetadata'); full.fire('playing')
  assert.equal(file.lastState(), 'playing')
  await sentence.play('abc123', 7, 5)
  assert.equal(full.src, '')
  assert.equal(file.lastState(), 'idle')
  assert.equal(sentence.lastState(), 'loading')
  file.stop()
  assert.equal(sentence.lastState(), 'loading', '旧播放器停止不会取消新播放器')
})

test('从尚未返回的文件试听切到句试听，取消旧读取且忽略迟到结果', async t => {
  const pending = deferred<Response>()
  let signal: AbortSignal | undefined
  t.mock.method(globalThis, 'fetch', (url: string, init: RequestInit) => {
    if (url.startsWith('/api/files/')) { signal = init.signal as AbortSignal; return pending.promise }
    return Promise.resolve(response(clip()))
  })
  const file = harness(t), sentence = harness(t)
  const old = file.playFile('D:\\voice.wav')
  await sentence.play('abc123', 7, 5)
  assert.equal(signal?.aborted, true)
  pending.resolve(fileResponse()); await old
  assert.equal(file.audios.length, 0)
  assert.equal(sentence.audios.length, 1)
  assert.deepEqual(file.failures(), [])
})

test('文件试听按停止立即取消读取，迟到失败不覆盖界面', async t => {
  const pending = deferred<Response>()
  let signal: AbortSignal | undefined
  t.mock.method(globalThis, 'fetch', (_url: string, init: RequestInit) => {
    signal = init.signal as AbortSignal; return pending.promise
  })
  const preview = harness(t), playing = preview.playFile('D:\\voice.wav')
  preview.stop()
  assert.equal(signal?.aborted, true)
  pending.reject(new Error('late failure')); await playing
  assert.deepEqual(preview.failures(), [])
  assert.equal(preview.audios.length, 0)
  assert.equal(preview.lastState(), 'idle')
})

test('文件错误显示具体后端原因，重新点击清错并复用相同播放入口', async t => {
  let count = 0
  t.mock.method(globalThis, 'fetch', async () => ++count === 1
    ? response({ message: '参考文件不存在，请重新选择文件。' }, 404) : fileResponse())
  const preview = harness(t)
  await preview.playFile('missing.wav')
  assert.equal(preview.errors.at(-1), '参考文件不存在，请重新选择文件。')
  assert.equal(preview.audios.length, 0)
  await preview.playFile('voice.wav')
  assert.equal(preview.errors.at(-1), '')
  assert.equal(preview.lastState(), 'loading')
})

test('文件试听网络错误和加载超时使用中文引导，清理后不能迟到播放', async t => {
  t.mock.method(globalThis, 'fetch', async () => { throw new TypeError('Failed to fetch') })
  const preview = harness(t)
  await preview.playFile('voice.wav')
  assert.equal(preview.errors.at(-1), '读取参考文件失败，请检查后台连接后重试。')
  t.mock.method(globalThis, 'fetch', async () => { throw Object.assign(new Error('timeout'), { name: 'TimeoutError' }) })
  await preview.playFile('voice.wav')
  assert.equal(preview.errors.at(-1), '读取参考文件超时，请重试。')
  t.mock.method(globalThis, 'fetch', async () => fileResponse())
  t.mock.timers.enable({ apis: ['setTimeout'] })
  await preview.playFile('voice.wav')
  const audio = preview.audios[0]
  t.mock.timers.tick(20000)
  assert.equal(preview.errors.at(-1), '参考文件加载超时，请重试。')
  audio.fire('loadedmetadata')
  assert.equal(audio.playedAt.length, 0)
  assert.equal(audio.src, '')
})

test('文件播放格式错误、无效时长和浏览器权限错误可恢复且释放媒体', async t => {
  t.mock.method(globalThis, 'fetch', async () => fileResponse())
  const preview = harness(t)
  await preview.playFile('voice.wav')
  preview.audios[0].fire('error')
  assert.match(preview.errors.at(-1)!, /参考文件加载或播放失败.*音频格式/)
  await preview.playFile('voice.wav')
  preview.audios[1].duration = NaN; preview.audios[1].fire('loadedmetadata')
  assert.match(preview.errors.at(-1)!, /参考文件时长无法读取/)
  await preview.playFile('voice.wav')
  const audio = preview.audios[2]
  audio.playResult = Promise.reject(Object.assign(new Error('permission'), { name: 'NotAllowedError' }))
  audio.fire('loadedmetadata')
  await Promise.resolve(); await Promise.resolve()
  assert.match(preview.errors.at(-1)!, /浏览器.*再点一次试听/)
  assert.equal(preview.lastState(), 'idle')
  assert.equal(audio.src, '')
})

test('文件试听只接受受控文件入口，拒绝外部地址、片段以及畸形响应', async t => {
  const candidates = [null, {}, { filename: 'voice.wav', url: 'https://example.com/a.wav' },
    { filename: 'voice.wav', url: '/api/files/reference-audio/content?path=a.wav#t=0,15' },
    { filename: 'voice.wav', url: '/api/files/reference-audio/contents?path=a.wav' },
    { filename: 'voice.wav', url: '/api/files/reference-audio/content?v=1' },
    { filename: '', url: '/api/files/reference-audio/content?path=a.wav' }]
  let index = 0
  t.mock.method(globalThis, 'fetch', async () => response(candidates[index++]))
  const preview = harness(t)
  for (const candidate of candidates) {
    await preview.playFile('voice.wav')
    assert.match(preview.errors.at(-1)!, /参考文件的试听地址无效/, JSON.stringify(candidate))
  }
  assert.equal(preview.audios.length, 0)
})

test('空路径和空字符路径不会请求后端或创建媒体', async t => {
  let reads = 0
  t.mock.method(globalThis, 'fetch', async () => { reads++; return fileResponse() })
  const preview = harness(t)
  for (const path of ['', '   ', 'a\0.wav']) await preview.playFile(path)
  assert.equal(reads, 0)
  assert.equal(preview.audios.length, 0)
  assert.equal(preview.lastState(), 'idle')
  assert.match(preview.errors.at(-1)!, /请选择有效的参考音频文件/)
})

test('旧后端缺少试听接口时说明需要重启，不混淆文件不存在的具体错误', async t => {
  const replies = [response({ code: 'API_NOT_FOUND', message: '请求的接口不存在，请刷新页面后重试' }, 404),
    response({ detail: 'Not Found' }, 404),
    response({ code: 'REFERENCE_FILE_NOT_FOUND', message: '参考文件不存在，请重新选择文件。' }, 404)]
  t.mock.method(globalThis, 'fetch', async () => replies.shift()!)
  const preview = harness(t)
  await preview.playFile('voice.wav')
  assert.match(preview.errors.at(-1)!, /重启后端并刷新页面/)
  await preview.play('abc123', 7, 5)
  assert.match(preview.errors.at(-1)!, /重启后端并刷新页面/)
  await preview.playFile('missing.wav')
  assert.equal(preview.errors.at(-1), '参考文件不存在，请重新选择文件。')
})
