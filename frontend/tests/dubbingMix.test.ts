import assert from 'node:assert/strict'
import test from 'node:test'
import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

const bundle = await build({ entryPoints: [fileURLToPath(new URL('../src/composables/useDubbingMix.ts', import.meta.url))],
  bundle: true, write: false, format: 'esm', platform: 'browser', define: {'process.env.NODE_ENV': '"production"'} })
const { useDubbingMix } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`)

function fixture() {
  const voice = {value: 1}, background = {value: 1}, requests: any[] = []
  let saved = 0
  const edits = useDubbingMix({ voice, background,
    save: (mix: any) => new Promise((resolve, reject) => requests.push({mix, resolve, reject})),
    saved: () => { saved++ },
  })
  return { voice, background, requests, edits, saved: () => saved }
}

test('音量立即生效，连续变化停下后自动保存；点击、键盘均无需依赖拖动结束事件', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const f = fixture()
  f.edits.change('voice', .8)
  context.mock.timers.tick(200)
  f.edits.change('background', .35)
  assert.equal(f.voice.value, .8)
  assert.equal(f.background.value, .35)
  assert.equal(f.edits.pending.value, true)
  context.mock.timers.tick(299)
  assert.equal(f.requests.length, 0)
  context.mock.timers.tick(1)
  assert.deepEqual(f.requests[0].mix, {voice: .8, background: .35})
  f.requests[0].resolve()
  await f.edits.save()
  assert.equal(f.edits.pending.value, false)
  assert.equal(f.edits.saving.value, false)
  assert.equal(f.saved(), 1)
})

test('慢保存期间仍可拖动两条音轨，后续请求串行且只提交最新值', async () => {
  const f = fixture()
  f.edits.change('voice', .8)
  const saving = f.edits.save()
  f.edits.change('voice', .5)
  f.edits.change('background', .4)
  f.edits.change('voice', .9)
  f.edits.change('background', .2)
  assert.equal(f.requests.length, 1)
  assert.equal(f.voice.value, .9)
  f.requests[0].resolve()
  await Promise.resolve()
  assert.deepEqual(f.requests[1].mix, {voice: .9, background: .2})
  assert.equal(f.edits.pending.value, true)
  f.requests[1].resolve()
  await saving
  assert.equal(f.requests.length, 2)
  assert.equal(f.edits.pending.value, false)
  assert.equal(f.saved(), 1, '全部保存完成后才后台刷新一次，不等待刷新才能解除保存状态')
  await f.edits.dispose()
})

test('保存期间及确认前发出的旧轮询不覆盖试听值，新轮询仍可接收外部修改', async () => {
  const f = fixture()
  const oldRead = f.edits.revision()
  f.edits.change('voice', .6)
  const duringSave = f.edits.revision()
  const saving = f.edits.save()
  f.edits.reconcile({voice: 1, background: 1}, oldRead)
  assert.equal(f.voice.value, .6)
  f.requests[0].resolve(); await saving
  f.edits.reconcile({voice: 1, background: 1}, duringSave)
  assert.equal(f.voice.value, .6)
  f.edits.reconcile({voice: .7, background: .8}, f.edits.revision())
  assert.equal(f.voice.value, .7)
  assert.equal(f.background.value, .8)
})

test('保存失败保留试听值和未保存标记，重试成功后恢复；旧值失败不丢弃新拖动', async () => {
  const f = fixture()
  f.edits.change('voice', .25)
  const failed = f.edits.save()
  f.requests[0].reject(new Error('保存失败')); await failed
  assert.equal(f.voice.value, .25)
  assert.equal(f.edits.error.value, '保存失败')
  assert.equal(f.edits.pending.value, true)
  assert.equal(f.edits.saving.value, false)
  f.edits.reconcile({voice: 1, background: 1}, f.edits.revision())
  assert.equal(f.voice.value, .25)
  const retry = f.edits.save()
  f.edits.change('background', .7)
  f.requests[1].reject(new Error('旧请求失败'))
  await Promise.resolve()
  assert.deepEqual(f.requests[2].mix, {voice: .25, background: .7})
  f.requests[2].resolve(); await retry
  assert.equal(f.edits.pending.value, false)
  assert.equal(f.edits.error.value, '')
  await f.edits.dispose()
})

test('离开页面仍提交最终音量，不再刷新旧页面或接收新编辑', async () => {
  const f = fixture()
  f.edits.change('voice', .4)
  const saving = f.edits.save()
  f.edits.change('voice', .3)
  const leaving = f.edits.dispose()
  f.edits.change('voice', .1)
  f.requests[0].resolve()
  await Promise.resolve()
  assert.deepEqual(f.requests[1].mix, {voice: .3, background: 1})
  f.requests[1].resolve()
  await Promise.all([saving, leaving])
  assert.equal(f.saved(), 0)
  const debounce = fixture()
  debounce.edits.change('voice', .2)
  const flush = debounce.edits.dispose()
  assert.equal(debounce.requests.length, 1)
  debounce.requests[0].resolve(); await flush
  assert.equal(debounce.saved(), 0)
})
