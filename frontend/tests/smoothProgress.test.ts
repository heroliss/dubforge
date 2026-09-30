import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const bundle = await build({ stdin: { contents: `export { useSmoothProgressMap } from './src/utils/smoothProgress';
  export { ref, nextTick, effectScope } from 'vue';`,
  resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'ts' },
  bundle: true, write: false, format: 'esm', platform: 'browser', define: {'process.env.NODE_ENV': '"production"'} })
const { useSmoothProgressMap, ref, nextTick, effectScope } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

test('逐句真实进度平滑追赶，不超前；切换任务/句子清理旧进度', async t => {
  let clock = 0, id = 0
  const frames = new Map<number, (time: number) => void>()
  const oldWindow = (globalThis as any).window
  ;(globalThis as any).window = {
    requestAnimationFrame: (callback: (time: number) => void) => { frames.set(++id, callback); return id },
    cancelAnimationFrame: (key: number) => frames.delete(key),
  }
  t.mock.method(performance, 'now', () => clock)
  const scope = effectScope()
  const items = ref([{ key: 'job1:line1', progress: 60, status: 'running' }])
  const progress = scope.run(() => useSmoothProgressMap(() => items.value))
  function frame(time: number) {
    clock = time
    const pending = [...frames.values()]; frames.clear()
    pending.forEach(callback => callback(time))
  }
  try {
    frame(750)
    assert.equal(progress.value['job1:line1'], 30)
    frame(1500)
    assert.equal(progress.value['job1:line1'], 60)
    frame(3000)
    assert.equal(progress.value['job1:line1'], 60)
    items.value = [{key: 'job1:line2', progress: 20, status: 'running'}]
    await nextTick()
    assert.equal(progress.value['job1:line1'], undefined)
    frame(3750)
    assert.equal(progress.value['job1:line2'], 10)
    items.value = [{key: 'job2:line2', progress: 0, status: 'running'}]
    await nextTick()
    frame(5250)
    assert.deepEqual(progress.value, {'job2:line2': 0})
    items.value = []
    await nextTick()
    assert.deepEqual(progress.value, {})
  } finally {
    scope.stop()
    ;(globalThis as any).window = oldWindow
  }
})
