import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import postcss from 'postcss'
import { cuePosition, timelineCues } from '../src/utils/previewTimeline.ts'

// 运行实际预览组件脚本，只替换生命周期、网络和时钟；不连接用户项目。
const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/CommentPreview.vue', import.meta.url), 'utf8'))
const result = await build({ stdin: { contents: `import { reactive, mountHooks, unmountHooks } from 'vue';
  const fixtureProps = reactive(__PROPS__), defineProps = () => fixtureProps;
  ${descriptor.scriptSetup!.content}
  export { fixtureProps, mountHooks, unmountHooks, start, end, outsideCount, clipped, seconds, activeIndex,
    axisEnd, previewEnd, image, error, loading, preview, beginScrub, endScrub, setPosition, cues };`, loader: 'ts', resolveDir: `${root}/src/components` },
  bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'test-boundary', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$|\.vue$/ }, a => ({ path: a.path, namespace: 'test' }))
    b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({ contents: a.path === 'vue'
      ? `export { ref, computed, watch, reactive } from '@vue/reactivity';
         export const mountHooks = [], unmountHooks = [], onMounted = f => mountHooks.push(f), onBeforeUnmount = f => unmountHooks.push(f);`
      : a.path.endsWith('.vue') ? 'export default {};' : 'export {};', resolveDir: root }))
  } }] })
let serial = 0
async function fixture(t: any) {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const props = { uid: 'test', items: [0, 1, 2].map(id => ({ id: String(id), display_text: '中文' })), invalid: false,
    settings: { start: 5, interval: 15, duration: 6, color: '#FFFFFF' }, videoDuration: 60 }
  const page = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text.replace('__PROPS__', JSON.stringify(props)) + `\n// ${serial++}`).toString('base64')}`)
  t.after(() => page.unmountHooks.forEach((fn: () => void) => fn()))
  return page
}
async function flush() { for (let i = 0; i < 25; i++) await Promise.resolve() }
const frame = () => new Response('test-png', { headers: { 'Content-Type': 'image/png' } })

test('预览按原图比例占满宽度，不用固定高度挤出四周黑边，也不裁切内容', () => {
  const css=postcss.parse(descriptor.styles.map(s=>s.content).join('\n'))
  const rules=(selector:string)=>{const values:Record<string,string>={};css.walkRules(selector,r=>{r.walkDecls(d=>{values[d.prop]=d.value})});return values}
  assert.equal(rules('.frame-stage.has-frame')['aspect-ratio'],'auto')
  assert.equal(rules('.frame-stage img').width,'100%')
  assert.equal(rules('.frame-stage img').height,'auto')
  assert.equal(rules('.frame-stage img')['max-height'],undefined)
  assert.notEqual(rules('.frame-stage img')['object-fit'],'cover')
  assert.match(descriptor.template!.content,/'has-frame': !!image/)
})

test('评论整体时间范围准确计算，区分超出视频与末条被截短', async t => {
  const p = await fixture(t)
  assert.equal(p.start.value, 5); assert.equal(p.end.value, 41)
  assert.equal(p.outsideCount.value, 0); assert.equal(p.clipped.value, false)
  p.fixtureProps.videoDuration = 23
  assert.equal(p.outsideCount.value, 1); assert.equal(p.clipped.value, true)
  p.setPosition(24); assert.equal(p.seconds.value, 22.9)
  p.setPosition(15); assert.equal(p.activeIndex.value, -1)
  p.setPosition(20); assert.equal(p.activeIndex.value, 1)
  p.fixtureProps.videoDuration = 0
  assert.equal(p.axisEnd.value, 41); assert.equal(p.outsideCount.value, 0)
})

test('首次自动预览；文字、颜色与节奏连续修改只请求最终草稿', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { requests.push(JSON.parse(init.body)); return frame() })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush()
  assert.equal(requests.length, 1); assert.equal(requests[0].seconds, 5.5)
  p.fixtureProps.settings.color = '#FF0000'; p.fixtureProps.settings.interval = 20
  p.fixtureProps.items[0].display_text = '修改后的评论'
  t.mock.timers.tick(499); await flush(); assert.equal(requests.length, 1)
  t.mock.timers.tick(1); await flush()
  assert.equal(requests.length, 2); assert.equal(requests[1].settings.color, '#FF0000')
  assert.equal(requests[1].items[0].display_text, '修改后的评论'); assert.equal(p.end.value, 51)
  assert.ok(p.image.value)
})

test('FFmpeg 帧请求串行，晚到旧画面丢弃，最多补做最新草稿', async t => {
  const calls: any[] = [], resolvers: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => { calls.push(JSON.parse(init.body)); return new Promise(resolve => resolvers.push(resolve)) })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush()
  p.fixtureProps.settings.color = '#00FF00'; t.mock.timers.tick(500); await flush()
  p.fixtureProps.settings.color = '#0000FF'; t.mock.timers.tick(500); await flush()
  assert.equal(calls.length, 1)
  resolvers[0](frame()); await flush(); assert.equal(p.image.value, '')
  t.mock.timers.tick(500); await flush(); assert.equal(calls.length, 2)
  assert.equal(calls[1].settings.color, '#0000FF')
  resolvers[1](frame()); await flush(); assert.ok(p.image.value)
})

test('拖动期间不请求，松开只预览最终位置；精确输入也自动刷新', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { requests.push(JSON.parse(init.body)); return frame() })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  p.beginScrub(); p.setPosition(20); t.mock.timers.tick(5000); await flush()
  p.setPosition(35); p.endScrub(); t.mock.timers.tick(500); await flush()
  assert.equal(requests.length, 1); assert.equal(requests[0].seconds, 35)
  p.setPosition(45); t.mock.timers.tick(500); await flush()
  assert.equal(requests[1].seconds, 45)
})

test('错误不无限重试；非法草稿暂停，离开后不回写或继续渲染', async t => {
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return new Response('{"message":"视频缺失"}', { status: 400 }) })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush(); assert.equal(p.error.value, '视频缺失')
  t.mock.timers.tick(60000); await flush(); assert.equal(calls, 1)
  p.fixtureProps.invalid = true; p.fixtureProps.settings.color = '#00FF00'
  t.mock.timers.tick(500); await flush(); assert.equal(calls, 1)
  p.fixtureProps.invalid = false; t.mock.timers.tick(500); await flush(); assert.equal(calls, 2)
  p.unmountHooks.forEach((fn: () => void) => fn())
  p.fixtureProps.settings.color = '#0000FF'; t.mock.timers.tick(500); await p.preview()
  assert.equal(calls, 2)
})

test('评论刻度随节奏和排序更新，点击可见开始点自动预览且携带描边草稿', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { requests.push(JSON.parse(init.body)); return frame() })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  assert.deepEqual(p.cues.value.map((cue: any) => cue.start), [5, 20, 35])
  p.fixtureProps.settings.interval = 10; p.fixtureProps.videoDuration = 24
  p.fixtureProps.items[1].display_text = '现在查看第二条'
  p.fixtureProps.settings.outline_color = '#00FF00'; p.fixtureProps.settings.outline = 4.5
  const cues = timelineCues(p.cues.value, p.previewEnd.value)
  assert.deepEqual(cues.map(cue => cue.start), [5, 15]); assert.match(cues[1].label, /现在查看第二条/)
  p.setPosition(cuePosition(cues[1], p.previewEnd.value))
  assert.equal(p.activeIndex.value, 1)
  t.mock.timers.tick(500); await flush()
  assert.equal(requests.length, 1); assert.equal(requests[0].seconds, 15.1)
  assert.equal(requests[0].settings.outline_color, '#00FF00'); assert.equal(requests[0].settings.outline, 4.5)
})
