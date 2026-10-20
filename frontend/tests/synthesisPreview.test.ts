import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const bundle = await build({ stdin: {
  contents: `export { useSynthesisPreview } from './src/composables/useSynthesisPreview'; export { ref, nextTick, effectScope } from 'vue'`,
  resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'ts',
}, bundle: true, write: false, format: 'esm', platform: 'browser', define: { 'process.env.NODE_ENV': '"production"' } })
const { useSynthesisPreview, ref, nextTick, effectScope } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

const flush = async () => { await nextTick(); await Promise.resolve(); await nextTick() }
function line(id: number, url = `${id}?v=old`) {
  return { id, start: id * 10, end: id * 10 + 3, wav: { exists: true, url, seconds: 2 },
    selection: { source: 'dub', take: 1 as number | null }, versions: [{ id: 1, exists: true, url }] }
}
function fixture(context: any, initial: any = { job_id: 1, active: true,
  lines: [1, 2, 3, 4].map(id => ({ id, status: 'queued', take: 1 })) }, enabledAtStart = true) {
  const scope = effectScope()
  context.after(() => scope.stop())
  const enabled = ref(enabledAtStart), task = ref<any>(initial), lines = ref<any[]>([1, 2, 3, 4].map(id => line(id)))
  const finished = ref({ serial: 0, lineId: null as number | null }), interrupted = ref(0), error = ref('')
  const played: { id: number; url: string }[] = [], selected: number[] = []
  let pauses = 0, rejection: Error | null = null, playGate: Promise<void> | null = null
  let resumeResult = false
  const previewLine = ref<number | null>(null), playing = ref(false), preservePlayback = ref(false)
  const resumed: number[] = []
  const controller = scope.run(() => useSynthesisPreview({
    enabled: () => enabled.value, task: () => task.value, lines: () => lines.value,
    finished: () => finished.value, interrupted: () => interrupted.value, error: () => error.value,
    previewLine: () => previewLine.value, playing: () => playing.value,
    preservePlaybackOnDisable: () => preservePlayback.value,
    select: (id: number) => selected.push(id),
    play: (value: any) => {
      played.push({ id: value.id, url: value.wav.url }); error.value = ''
      if (rejection) return Promise.reject(rejection)
      return playGate ?? Promise.resolve()
    }, resume: (id: number) => { resumed.push(id); return resumeResult }, pause: () => { pauses++ },
  }))!
  const complete = (id: number, url = `${id}?v=new`) => {
    const row = task.value.lines.find((row: any) => row.id === id)
    row.status = 'done'
    const value = lines.value.find(line => line.id === id)
    value.wav.url = url; value.versions[0].url = url
  }
  const finish = (id: number) => { finished.value = { serial: finished.value.serial + 1, lineId: id } }
  return { scope, enabled, task, lines, finished, interrupted, error, played, selected, complete, finish,
    previewLine, playing, preservePlayback,
    get pauses() { return pauses }, get resumed() { return resumed },
    set resumeResult(value: boolean) { resumeResult = value },
    set rejection(value: Error | null) { rejection = value },
    set playGate(value: Promise<void> | null) { playGate = value }, ...controller }
}

test('只试听完成的新配音，播完暂停等待下一句，不播放排队、重试或缓存结果', async context => {
  const f = fixture(context)
  assert.equal(f.status.value, 'waiting')
  f.task.value.lines[0].status = 'running'
  f.task.value.lines[1].status = 'skipped'
  await flush()
  assert.deepEqual(f.played, [])
  f.complete(1)
  await flush()
  assert.deepEqual(f.played, [{ id: 1, url: '1?v=new' }])
  assert.equal(f.status.value, 'playing')
  assert.equal(f.currentLine.value, 1)
  f.finish(1)
  await flush()
  assert.equal(f.status.value, 'waiting')
  assert.equal(f.currentLine.value, null)
  f.complete(3)
  await flush()
  assert.deepEqual(f.selected, [1, 3])
  assert.equal(f.currentLine.value, 3)
})

test('合成比试听快时完整播完当前句，仅保留最新待播句', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.complete(2); await flush()
  assert.equal(f.pendingLine.value, 2)
  f.complete(3); f.complete(4); await flush()
  assert.equal(f.pendingLine.value, 4)
  assert.equal(f.currentLine.value, 1)
  assert.equal(f.pauses, 0)
  f.finish(1); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 4])
  f.finish(4); f.task.value.active = false; await flush()
  assert.equal(f.status.value, 'complete')
  assert.equal(f.pendingLine.value, null)
  f.task.value = { ...f.task.value }; f.lines.value = [...f.lines.value]; await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 4], '轮询不会倒回已略过或已播句子')
})

test('首次打开只试听活动任务最新完成句，按执行顺序选择而非句子编号', async context => {
  const f = fixture(context, { job_id: 9, active: true,
    lines: [{ id: 4, status: 'done', take: 1 }, { id: 2, status: 'done', take: 1 }, { id: 3, status: 'running', take: 1 }] })
  await flush()
  assert.deepEqual(f.played, [{ id: 2, url: '2?v=old' }])
  f.finish(2); await flush()
  assert.equal(f.status.value, 'waiting')
  assert.equal(f.pendingLine.value, null)
})

test('初次进入不重放历史任务；跟随期间新任务一轮内结束也能播最后完成句', async context => {
  const f = fixture(context, { job_id: 1, active: false, lines: [{ id: 1, status: 'done', take: 1 }] })
  await flush()
  assert.equal(f.status.value, 'waiting')
  assert.deepEqual(f.played, [])
  f.task.value = { job_id: 2, active: false, lines: [{ id: 2, status: 'done', take: 1 }] }
  await flush()
  assert.deepEqual(f.played, [{ id: 2, url: '2?v=old' }])
  f.finish(2); await flush()
  assert.equal(f.status.value, 'complete')
})

test('明确开启合成跟随立即试听当前任务最新完成句，即使任务已经结束且句号未变化', async context => {
  const f = fixture(context, { job_id: 1, active: false, lines: [
    { id: 4, status: 'done', take: 1 }, { id: 2, status: 'done', take: 1 },
  ] }, false)
  await flush(); assert.deepEqual(f.played, [])
  f.enabled.value = true; await flush()
  assert.deepEqual(f.selected, [2]); assert.deepEqual(f.played.map(line => line.id), [2])
  f.finish(2); await flush(); assert.equal(f.status.value, 'complete')
  f.task.value = { ...f.task.value }; await flush()
  assert.equal(f.played.length, 1, '轮询不重放相同完成句')
})

test('明确启用时数据晚到仍接入当前任务，自动初载则继续避免历史重播', async context => {
  const f = fixture(context, null, false)
  f.enabled.value = true; await flush()
  f.task.value = { job_id: 7, active: false, lines: [{ id: 3, status: 'done', take: 1 }] }
  await flush()
  assert.deepEqual(f.played.map(line => line.id), [3])
})

test('正在试听本任务完成句时接管原位置，播完再跟随最新结果，不重复播放或跳头', async context => {
  const f = fixture(context, { job_id: 1, active: true, lines: [
    { id: 1, status: 'done', take: 1 }, { id: 2, status: 'done', take: 1 },
    { id: 3, status: 'running', take: 1 },
  ] }, false)
  f.previewLine.value = 1; f.playing.value = true; f.resumeResult = true
  f.enabled.value = true; await flush()
  assert.equal(f.currentLine.value, 1); assert.equal(f.pendingLine.value, 2)
  assert.deepEqual(f.selected, [1]); assert.deepEqual(f.resumed, [1])
  assert.deepEqual(f.played, []); assert.equal(f.pauses, 0)
  f.task.value = { ...f.task.value }; await flush()
  assert.deepEqual(f.resumed, [1], '轮询不重复接管或重置播放')
  f.finish(1); await flush()
  assert.deepEqual(f.played.map(line => line.id), [2]); assert.deepEqual(f.selected, [1, 2])
})

test('切到预览保留当前声音，只撤销合成控制；未完成句不被误当可接管声音', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.preservePlayback.value = true; f.enabled.value = false; await flush()
  assert.equal(f.status.value, 'off'); assert.equal(f.pauses, 0)
  f.previewLine.value = 3; f.playing.value = true; f.resumeResult = true
  f.enabled.value = true; await flush()
  assert.deepEqual(f.resumed, [], '正在合成/排队句不能用于完成试听')
  assert.equal(f.currentLine.value, 1); assert.equal(f.pauses, 1)
})

test('首次数据尚未返回时也不把稍后读到的历史任务当成新合成', async context => {
  const f = fixture(context, null)
  f.task.value = { job_id: 1, active: false, lines: [{ id: 1, status: 'done', take: 1 }] }
  await flush()
  assert.deepEqual(f.played, [])
  f.task.value = { job_id: 2, active: true, lines: [{ id: 2, status: 'done', take: 1 }] }
  await flush()
  assert.deepEqual(f.played.map(line => line.id), [2])
})

test('done 配旧 URL 时等待；音频先发布但状态晚更新不会污染首次 URL 基线', async context => {
  const f = fixture(context)
  f.task.value.lines[0].status = 'running'; await flush()
  f.task.value.lines[0].status = 'done'; await flush()
  assert.deepEqual(f.played, [])
  assert.equal(f.pendingLine.value, 1)
  f.lines.value[0].versions[0].url = '1?v=new'; await flush()
  assert.deepEqual(f.played, [], '所选 wav 尚未与版本一致时不接入')
  f.lines.value[0].wav.url = '1?v=new'; await flush()
  assert.deepEqual(f.played.map(line => line.id), [1])
  f.finish(1); await flush()
  f.lines.value[1].versions[0].url = '2?v=new'; f.lines.value[1].wav.url = '2?v=new'
  f.task.value.lines[1].status = 'running'; await flush()
  f.task.value.lines[1].status = 'done'; await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 2], '排队时捕获的旧 URL 基线保持不变')
})

test('音频或选择信息稍后同步时只等待最新句，不倒播旧句', async context => {
  const f = fixture(context)
  f.complete(1)
  f.lines.value[0].wav.exists = false
  await flush()
  assert.equal(f.pendingLine.value, 1)
  f.complete(2)
  f.lines.value[1].selection.take = 2
  await flush()
  assert.equal(f.pendingLine.value, 2)
  f.lines.value[0].wav.exists = true; await flush()
  assert.deepEqual(f.played, [])
  f.lines.value[1].selection.take = 1; await flush()
  assert.deepEqual(f.played.map(line => line.id), [2])
})

test('尊重用户原声与无声选择，不强切配音；skipped 不参与最新待播', async context => {
  const f = fixture(context)
  f.lines.value[0].selection.source = 'original'
  f.complete(1); await flush()
  assert.deepEqual(f.played, [])
  f.lines.value[0] = { ...f.lines.value[0] }; await flush()
  assert.equal(f.pendingLine.value, null, '新快照仍明确选原声才略过')
  f.lines.value[1].selection.take = null
  f.complete(2); await flush()
  assert.deepEqual(f.played, [])
  f.lines.value[1] = { ...f.lines.value[1] }; await flush()
  assert.equal(f.pendingLine.value, null, '新快照仍明确选无声才略过')
  f.complete(3); f.task.value.lines[3].status = 'skipped'; await flush()
  assert.deepEqual(f.played.map(line => line.id), [3])
})

test('首次生成 done 配上旧空位/原声时不丢结果，后续正确选择到位后接入', async context => {
  const f = fixture(context)
  f.lines.value[0].selection.take = null
  f.lines.value[0].wav.exists = false
  f.task.value.lines[0].status = 'done'
  f.lines.value[0].versions[0].exists = false
  await flush()
  assert.equal(f.pendingLine.value, 1)
  assert.deepEqual(f.played, [])
  f.lines.value[0].versions[0] = { id: 1, exists: true, url: '1?v=new' }
  await flush()
  f.finish(4); await flush()
  assert.equal(f.pendingLine.value, 1, '同一份媒体快照的其他事件不能确认旧空位')
  f.lines.value[0] = { ...line(1, '1?v=new') }; await flush()
  assert.deepEqual(f.played.map(line => line.id), [1])
  f.finish(1); await flush()
  f.lines.value[1].selection.source = 'original'
  f.complete(2); await flush()
  assert.equal(f.pendingLine.value, 2)
  f.lines.value[1] = { ...line(2, '2?v=new') }; await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 2])
})

test('用户暂停后轮询不抢播，继续时优先最新待播结果', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.suspend()
  assert.equal(f.status.value, 'paused')
  assert.equal(f.pauses, 1)
  f.complete(2); f.complete(3); await flush()
  f.finish(1); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1])
  assert.equal(f.pendingLine.value, 3)
  f.resume(); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 3])
  assert.equal(f.status.value, 'playing')
})

test('原生干预也暂停自动跟随，恢复时可重听尚未播完的句子', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.interrupted.value++; await flush()
  assert.equal(f.status.value, 'paused')
  assert.equal(f.currentLine.value, null)
  assert.equal(f.pauses, 0, '原生干预已交给播放器处理，不重复暂停新的手动试听')
  f.task.value = { ...f.task.value }; await flush()
  assert.equal(f.played.length, 1)
  f.resume(); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 1])
})

test('当前单句仍在同一时间轴时，继续跟随恢复位置而不是重播句首', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.suspend(false); await flush()
  f.resumeResult = true
  f.resume(); await flush()
  assert.deepEqual(f.resumed, [1])
  assert.deepEqual(f.played.map(line => line.id), [1])
  assert.equal(f.currentLine.value, 1)
  assert.equal(f.status.value, 'playing')
})

test('等待时手动播放也暂停跟随；继续等待不会重放已完整播过的句子', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.finish(1); await flush()
  f.suspend(false); await flush()
  f.resume(); await flush()
  assert.equal(f.status.value, 'waiting')
  assert.deepEqual(f.played.map(line => line.id), [1])
})

test('播放失败不会轮询重试，只在继续时重试最新有效结果', async context => {
  const f = fixture(context)
  f.rejection = new Error('解码失败')
  f.complete(1); await flush()
  assert.equal(f.status.value, 'error')
  assert.equal(f.pauses, 1)
  f.task.value = { ...f.task.value }; await flush()
  assert.equal(f.played.length, 1)
  f.complete(2); await flush()
  assert.equal(f.played.length, 1)
  f.rejection = null; f.resume(); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 2])
  assert.equal(f.status.value, 'playing')
})

test('启动成功后才出现媒体错误也只暂停一次，可显式重试同句', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.error.value = '音频加载失败'; await flush()
  assert.equal(f.status.value, 'error')
  assert.equal(f.pauses, 1)
  f.lines.value = [...f.lines.value]; await flush()
  assert.equal(f.pauses, 1)
  f.resume(); await flush()
  assert.equal(f.error.value, '')
  assert.deepEqual(f.played.map(line => line.id), [1, 1])
})

test('更换任务停止旧自动试听，旧启动失败与旧完成信号不能干扰新任务', async context => {
  const f = fixture(context)
  let reject!: (error: Error) => void
  f.playGate = new Promise<void>((_, fail) => { reject = fail })
  f.complete(1); await flush()
  f.complete(2); await flush()
  f.playGate = null
  f.task.value = { job_id: 2, active: true, lines: [{ id: 3, status: 'done', take: 1 }] }
  await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 3])
  assert.equal(f.pauses, 1)
  reject(new Error('旧任务播放失败')); f.finish(1); await flush()
  assert.equal(f.status.value, 'playing')
  assert.equal(f.currentLine.value, 3)
  assert.equal(f.pendingLine.value, null)
})

test('任务暂停或结束后仍完整试听已完成句，未完成句失败不会自动播放', async context => {
  const f = fixture(context)
  f.complete(1); await flush()
  f.task.value.paused = true
  f.task.value.lines[1].status = 'failed'
  await flush()
  assert.equal(f.currentLine.value, 1)
  f.complete(3); f.task.value.active = false; await flush()
  f.finish(1); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 3])
  f.finish(3); await flush()
  assert.equal(f.status.value, 'complete')
})

test('关闭模式和卸载都取消自动试听，迟到回调不能重新启动', async context => {
  const f = fixture(context)
  let resolve!: () => void
  f.playGate = new Promise<void>(done => { resolve = done })
  f.complete(1); await flush()
  f.enabled.value = false; await flush()
  assert.equal(f.status.value, 'off')
  assert.equal(f.pauses, 1)
  f.complete(2); resolve(); await flush()
  assert.equal(f.played.length, 1)
  f.enabled.value = true; f.playGate = null; await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 2])
  f.scope.stop()
  assert.equal(f.pauses, 2)
  f.finish(2); f.complete(3); await flush()
  assert.deepEqual(f.played.map(line => line.id), [1, 2])
})
