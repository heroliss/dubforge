import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const bundle = await build({ stdin: {
  contents: `export { useDubbingFollow } from './src/composables/useDubbingFollow'; export { ref, nextTick, effectScope } from 'vue'`,
  resolveDir: fileURLToPath(new URL('..', import.meta.url)), loader: 'ts',
}, bundle: true, write: false, format: 'esm', platform: 'browser', define: { 'process.env.NODE_ENV': '"production"' } })
const { useDubbingFollow, ref, nextTick, effectScope } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

function fixture(context: any) {
  const scope = effectScope()
  context.after(() => scope.stop())
  const lines = [{ id: 1, start: 3 }, { id: 2, start: 10 }, { id: 3, start: 20 }]
  const synthesis = ref({ job: 'first', line: null as number | null, progress: 0, attempt: 0 })
  const preview = ref(null as number | null), selected = ref(null as number | null)
  const seeks: number[] = [], revealed: number[] = []
  const follow = scope.run(() => useDubbingFollow({ lines: () => lines,
    previewLine: () => preview.value, selected,
    reveal: (id: number) => revealed.push(id),
    seek: (time: number) => { seeks.push(time); preview.value = lines.find(line => line.start === time)!.id },
  }))!
  return { synthesis, preview, selected, seeks, revealed, ...follow }
}

test('关闭跟随仍可手动定位合成句：同步选中、列表与视频', async context => {
  const f = fixture(context)
  f.synthesis.value.line = 2; f.preview.value = 1
  await nextTick()
  assert.deepEqual(f.seeks, [])
  assert.equal(f.selected.value, null)
  f.locate(f.synthesis.value.line)
  await nextTick()
  assert.equal(f.selected.value, 2)
  assert.deepEqual(f.revealed, [2])
  assert.deepEqual(f.seeks, [10])
})

test('合成进度不自行定位视频，避免打断正在试听的已完成句', async context => {
  const f = fixture(context)
  f.synthesis.value.line = 2
  f.mode.value = 'synthesis'
  await nextTick()
  assert.deepEqual(f.seeks, [])
  f.synthesis.value = { job: 'first', line: 2, progress: .8, attempt: 1 }
  f.preview.value = 3
  await nextTick()
  assert.deepEqual(f.seeks, [])
  assert.equal(f.selected.value, null, '合成跟随由完成结果控制选择')
  f.synthesis.value.line = 3
  await nextTick()
  assert.deepEqual(f.seeks, [])
  assert.equal(f.selected.value, null)
  f.synthesis.value.job = 'second'
  await nextTick()
  assert.deepEqual(f.seeks, [], '新任务也不能抢先定位未完成句子')
})

test('跟随预览只选中和滚动，不回写视频；暂停或句间停顿保留选中项', async context => {
  const f = fixture(context)
  f.mode.value = 'preview'; f.preview.value = 1
  await nextTick()
  f.preview.value = 2
  await nextTick()
  assert.equal(f.selected.value, 2)
  assert.deepEqual(f.revealed, [1, 2])
  assert.deepEqual(f.seeks, [])
  f.preview.value = null
  await nextTick()
  assert.equal(f.selected.value, 2)
  assert.deepEqual(f.revealed, [1, 2])
})

test('切换到预览立即跟随同一句，无需等待句号变化，退出控制器清cursor前已完成定位', async context => {
  const f = fixture(context)
  f.preview.value = 2; f.selected.value = 1
  f.mode.value = 'preview'
  assert.equal(f.selected.value, 2, '模式切换同步采样当前句')
  assert.deepEqual(f.revealed, [2]); assert.deepEqual(f.seeks, [])
  f.mode.value = 'off'; f.selected.value = 3
  f.mode.value = 'preview'
  assert.equal(f.selected.value, 2, '同一preview id重新启用也立即跟随')
  f.preview.value = null
  await nextTick()
  assert.equal(f.selected.value, 2)
  assert.deepEqual(f.revealed, [2, 2]); assert.deepEqual(f.seeks, [])
})

test('两种模式互斥，可在任务运行中切换；关闭后两种事件都不再定位', async context => {
  const f = fixture(context)
  f.synthesis.value.line = 3; f.preview.value = 1; f.mode.value = 'preview'
  await nextTick()
  assert.equal(f.selected.value, 1)
  f.synthesis.value.line = 2
  await nextTick()
  assert.equal(f.selected.value, 1)
  f.mode.value = 'synthesis'
  await nextTick()
  assert.equal(f.selected.value, 1)
  assert.deepEqual(f.seeks, [])
  f.preview.value = 3; f.mode.value = 'preview'
  await nextTick()
  assert.equal(f.selected.value, 3)
  assert.deepEqual(f.seeks, [])
  f.mode.value = 'off'
  await nextTick()
  f.synthesis.value.line = 1; f.preview.value = 2
  await nextTick()
  assert.equal(f.selected.value, 3)
  assert.deepEqual(f.seeks, [])
})

test('任务完成或句子不存在时保持选择，不误定位视频', async context => {
  const f = fixture(context)
  f.synthesis.value.line = 1; f.mode.value = 'synthesis'
  await nextTick()
  f.synthesis.value.line = null
  await nextTick()
  f.locate(99)
  assert.equal(f.selected.value, null)
  assert.deepEqual(f.seeks, [])
})
