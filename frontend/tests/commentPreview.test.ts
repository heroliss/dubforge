import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import postcss from 'postcss'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { cuePosition, timelineCues } from '../src/utils/previewTimeline.ts'

// 运行实际预览组件脚本，只替换生命周期、网络和时钟；不连接用户项目。
const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/CommentPreview.vue', import.meta.url), 'utf8'))
const result = await build({ stdin: { contents: `import { reactive, mountHooks, unmountHooks } from 'vue';
  const fixtureProps = reactive(__PROPS__), defineProps = () => fixtureProps;
  const events = [], defineEmits = () => (...event) => events.push(event);
  ${descriptor.scriptSetup!.content}
  export { fixtureProps, mountHooks, unmountHooks, start, end, outsideCount, clipped, seconds, activeIndex,
    axisEnd, previewEnd, image, error, loading, preview, beginScrub, endScrub, setPosition, cues,
    activeItem, canLocateComment, locateComment, events, previewPending, pendingMessage, outdated, layoutDetails, layoutNotice };`, loader: 'ts', resolveDir: `${root}/src/components` },
  bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'test-boundary', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$|\.vue$/ }, a => ({ path: a.path, namespace: 'test' }))
    b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({ contents: a.path === 'vue'
      ? `export { ref, computed, watch, reactive } from '@vue/reactivity';
         export const mountHooks = [], unmountHooks = [], onMounted = f => mountHooks.push(f), onBeforeUnmount = f => unmountHooks.push(f);`
      : a.path.endsWith('.vue') ? 'export default {};' : 'export {};', resolveDir: root }))
  } }] })
let serial = 0
async function fixture(t: any, overrides: Record<string, any> = {}) {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const props = { uid: 'test', items: [0, 1, 2].map(id => ({ id: String(id), display_text: '中文' })), invalid: false,
    settings: { start: 5, interval: 15, duration: 6, color: '#FFFFFF' }, videoDuration: 60, ...overrides }
  const page = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text.replace('__PROPS__', JSON.stringify(props)) + `\n// ${serial++}`).toString('base64')}`)
  t.after(() => page.unmountHooks.forEach((fn: () => void) => fn()))
  return page
}
async function flush() { for (let i = 0; i < 25; i++) await Promise.resolve() }
const frame = (layout?: Record<string, any>) => new Response('test-png', { headers: { 'Content-Type': 'image/png', ...(layout ? { 'X-DubForge-Comment-Layout': JSON.stringify(layout) } : {}) } })

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
  assert.equal(requests.length, 1); assert.equal(requests[0].seconds, 5)
  p.fixtureProps.settings.color = '#FF0000'; p.fixtureProps.settings.interval = 20
  p.fixtureProps.items[0].display_text = '修改后的评论'
  t.mock.timers.tick(499); await flush(); assert.equal(requests.length, 1)
  t.mock.timers.tick(1); await flush()
  assert.equal(requests.length, 2); assert.equal(requests[1].settings.color, '#FF0000')
  assert.equal(requests[1].items[0].display_text, '修改后的评论'); assert.equal(p.end.value, 51)
  assert.ok(p.image.value)
})

test('防抖等待即显示准备状态，渲染时保留旧图并及时结束转圈', async t => {
  const calls: any[] = [], resolvers: Array<(value: Response) => void> = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => {
    calls.push(JSON.parse(init.body)); return new Promise<Response>(resolve => resolvers.push(resolve))
  })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  assert.equal(p.previewPending.value, true)
  assert.equal(p.pendingMessage.value, '正在准备预览…')
  assert.equal(calls.length, 0)
  t.mock.timers.tick(500)
  assert.equal(p.loading.value, true); assert.equal(p.pendingMessage.value, '正在生成预览…')
  resolvers[0](frame()); await flush()
  const oldImage = p.image.value
  assert.ok(oldImage); assert.equal(p.previewPending.value, false)
  p.fixtureProps.settings.color = '#00FF00'
  assert.equal(p.previewPending.value, true); assert.equal(p.pendingMessage.value, '正在准备预览…')
  assert.equal(p.image.value, oldImage); assert.equal(p.outdated.value, true)
  t.mock.timers.tick(499); assert.equal(calls.length, 1)
  t.mock.timers.tick(1)
  assert.equal(p.pendingMessage.value, '正在更新预览…'); assert.equal(p.image.value, oldImage)
  resolvers[1](frame()); await flush()
  assert.equal(p.previewPending.value, false); assert.equal(p.outdated.value, false)
  t.mock.timers.tick(5000); await flush(); assert.equal(calls.length, 2)
})

test('预览转圈不阻挡时间轴，拖动、非法草稿与错误都会停止等待提示', async t => {
  const calls: any[] = [], resolvers: Array<(value: Response) => void> = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => {
    calls.push(JSON.parse(init.body)); return new Promise<Response>(resolve => resolvers.push(resolve))
  })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500)
  p.setPosition(20)
  assert.equal(p.seconds.value, 20); assert.equal(p.previewPending.value, true)
  p.beginScrub(); p.setPosition(35)
  assert.equal(p.seconds.value, 35); assert.equal(p.previewPending.value, false)
  resolvers[0](frame()); await flush(); assert.equal(calls.length, 1)
  p.endScrub(); assert.equal(p.previewPending.value, true); assert.equal(calls.length, 2)
  resolvers[1](new Response('{"message":"画面生成失败"}', { status: 400 })); await flush()
  assert.equal(p.error.value, '画面生成失败'); assert.equal(p.previewPending.value, false)
  t.mock.timers.tick(5000); await flush(); assert.equal(calls.length, 2)
  p.fixtureProps.settings.color = '#00FF00'
  assert.equal(p.previewPending.value, true); assert.equal(p.error.value, '')
  p.fixtureProps.invalid = true
  assert.equal(p.previewPending.value, false)
  t.mock.timers.tick(5000); await flush(); assert.equal(calls.length, 2)
  p.fixtureProps.invalid = false; assert.equal(p.previewPending.value, true)
  p.unmountHooks.forEach((fn: () => void) => fn())
  assert.equal(p.previewPending.value, false)
  t.mock.timers.tick(5000); await flush(); assert.equal(calls.length, 2)
})

test('等待过渡提供可读状态并尊重减少动态效果设置', () => {
  const template = descriptor.template!.content
  assert.match(template, /:aria-busy="previewPending"/)
  assert.match(template, /v-if="previewPending"[^>]*role="status"[^>]*aria-live="polite"/)
  const css = postcss.parse(descriptor.styles.map(s => s.content).join('\n'))
  let overlayPassesPointer = false, respectsReducedMotion = false
  css.walkRules('.frame-transition', rule => rule.walkDecls('pointer-events', d => { overlayPassesPointer = d.value === 'none' }))
  css.walkAtRules('media', media => {
    if (media.params === '(prefers-reduced-motion: reduce)') media.walkDecls('animation', d => { respectsReducedMotion = d.value === 'none' })
  })
  assert.equal(overlayPassesPointer, true); assert.equal(respectsReducedMotion, true)
})

test('只显示当前有效预览的展开说明，晚到旧帧不能覆盖布局信息', async t => {
  const resolvers: Array<(value: Response) => void> = []
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => resolvers.push(resolve)))
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500)
  resolvers[0](frame({ min_reached: true, height_exceeded: true, video_height_exceeded: false })); await flush()
  assert.equal(p.layoutNotice.value, '已用最小字号，超出目标高度并完整展开')
  p.setPosition(20)
  assert.equal(p.layoutNotice.value, '')
  p.setPosition(35)
  resolvers[1](frame({ min_reached: true, height_exceeded: true, video_height_exceeded: true })); await flush()
  assert.equal(p.layoutNotice.value, '')
  assert.equal(p.layoutDetails.value?.video_height_exceeded, false)
  resolvers[2](frame({ min_reached: true, height_exceeded: true, video_height_exceeded: true })); await flush()
  assert.equal(p.layoutNotice.value, '已完整展开，部分文字可能超出画面；可增大宽度或调整字号范围')
  p.setPosition(5)
  resolvers[3](new Response('{"message":"画面生成失败"}', { status: 400 })); await flush()
  assert.equal(p.layoutNotice.value, ''); assert.equal(p.layoutDetails.value, null)
})

test('缺失或无效的布局响应头不影响图片预览，也不保留上个画面的提示', async t => {
  const responses = [frame({ min_reached: true, height_exceeded: true }), new Response('image', { headers: { 'X-DubForge-Comment-Layout': 'not-json' } }), frame()]
  t.mock.method(globalThis, 'fetch', async () => responses.shift()!)
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush(); assert.ok(p.layoutNotice.value)
  p.setPosition(20); await flush()
  assert.ok(p.image.value); assert.equal(p.layoutDetails.value, null); assert.equal(p.layoutNotice.value, '')
  p.setPosition(35); await flush()
  assert.ok(p.image.value); assert.equal(p.layoutDetails.value, null); assert.equal(p.error.value, '')
})

test('FFmpeg 帧请求串行，晚到旧画面丢弃，最多补做最新草稿', async t => {
  const calls: any[] = [], resolvers: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => { calls.push(JSON.parse(init.body)); return new Promise(resolve => resolvers.push(resolve)) })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush()
  p.fixtureProps.settings.color = '#00FF00'; t.mock.timers.tick(500); await flush()
  p.fixtureProps.settings.color = '#0000FF'; t.mock.timers.tick(500); await flush()
  assert.equal(calls.length, 1)
  assert.equal(p.previewPending.value, true)
  resolvers[0](frame()); await flush(); assert.equal(p.image.value, '')
  // 最新草稿已经结束防抖，旧请求完成后直接接续，不再多等一轮。
  assert.equal(calls.length, 2)
  assert.equal(p.previewPending.value, true)
  assert.equal(calls[1].settings.color, '#0000FF')
  resolvers[1](frame()); await flush(); assert.ok(p.image.value); assert.equal(p.previewPending.value, false)
})

test('拖动期间不请求，松开只预览最终位置；精确输入也自动刷新', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { requests.push(JSON.parse(init.body)); return frame() })
  const p = await fixture(t); p.mountHooks.forEach((fn: () => void) => fn())
  p.beginScrub(); p.setPosition(20); t.mock.timers.tick(5000); await flush()
  p.setPosition(35); p.endScrub(); await flush()
  assert.equal(requests.length, 1); assert.equal(requests[0].seconds, 35)
  p.setPosition(45); await flush()
  assert.equal(requests[1].seconds, 45)
  t.mock.timers.tick(500); await flush(); assert.equal(requests.length, 2)
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

test('按评论 ID 定位到实际上屏时刻，可重复定位同一条且不改变方案', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => { requests.push({ url, body: JSON.parse(init.body) }); return frame() })
  const p = await fixture(t, { focusId: '1', focusRequest: 1 })
  const originalItems = JSON.stringify(p.fixtureProps.items)
  p.mountHooks.forEach((fn: () => void) => fn())
  assert.equal(p.seconds.value, 20); assert.equal(p.activeItem.value.id, '1')
  assert.equal(requests.length, 1)
  await flush()
  assert.equal(requests[0].body.seconds, 20)
  assert.equal(p.canLocateComment.value, true)
  p.locateComment(); assert.deepEqual(p.events, [['locate-comment', '1']])
  p.fixtureProps.focusRequest = 2
  assert.equal(p.seconds.value, 20)
  await flush()
  // 时间和草稿未变，重复定位不需要多一次渲染。
  assert.equal(requests.length, 1)
  p.setPosition(35.5); p.fixtureProps.focusRequest = 3
  assert.equal(p.seconds.value, 20)
  await flush(); t.mock.timers.tick(500); await flush()
  assert.equal(requests.length, 2)
  assert.equal(p.activeItem.value.id, '1')
  assert.equal(JSON.stringify(p.fixtureProps.items), originalItems)
  assert.ok(requests.every(request => request.url === '/api/projects/test/comments/preview'))
})

test('定位立即移动游标，在途画面完成后直接请求最新位置，不多等防抖也不并发', async t => {
  const calls: any[] = [], resolvers: Array<(value: Response) => void> = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => {
    calls.push(JSON.parse(init.body)); return new Promise<Response>(resolve => resolvers.push(resolve))
  })
  const p = await fixture(t)
  p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500)
  assert.equal(calls.length, 1)
  p.fixtureProps.focusId = '1'; p.fixtureProps.focusRequest = 1
  assert.equal(p.seconds.value, 20)
  p.fixtureProps.focusId = '2'; p.fixtureProps.focusRequest = 2
  assert.equal(p.seconds.value, 35); assert.equal(calls.length, 1)
  resolvers[0](frame()); await flush()
  assert.equal(p.image.value, '')
  assert.equal(calls.length, 2); assert.equal(calls[1].seconds, 35)
  resolvers[1](frame()); await flush()
  assert.ok(p.image.value); assert.equal(p.canLocateComment.value, true)
  t.mock.timers.tick(500); await flush(); assert.equal(calls.length, 2)
})

test('旧请求结束不会提前执行或延后连续参数修改的防抖', async t => {
  const calls: any[] = [], resolvers: Array<(value: Response) => void> = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => {
    calls.push(JSON.parse(init.body)); return new Promise<Response>(resolve => resolvers.push(resolve))
  })
  const p = await fixture(t, { focusId: '1', focusRequest: 1 })
  p.mountHooks.forEach((fn: () => void) => fn())
  assert.equal(calls.length, 1)
  p.fixtureProps.settings.color = '#00FF00'
  t.mock.timers.tick(100); resolvers[0](frame()); await flush()
  assert.equal(calls.length, 1)
  t.mock.timers.tick(399); await flush(); assert.equal(calls.length, 1)
  t.mock.timers.tick(1); await flush()
  assert.equal(calls.length, 2); assert.equal(calls[1].settings.color, '#00FF00')
  resolvers[1](frame()); await flush(); assert.ok(p.image.value)
})

test('小数节奏精确定位起点时也识别为对应评论', async t => {
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { calls.push(JSON.parse(init.body)); return frame() })
  const p = await fixture(t, { focusId: '2', focusRequest: 1, settings: { start: .1, interval: .2, duration: .1 } })
  p.mountHooks.forEach((fn: () => void) => fn())
  assert.equal(p.seconds.value, p.cues.value[2].start)
  assert.equal(p.activeIndex.value, 2)
  assert.equal(calls[0].seconds, .5)
  await flush()
})

test('定位跟随评论实际顺序变化，并在视频边界内安全截取', async t => {
  t.mock.method(globalThis, 'fetch', async () => frame())
  const p = await fixture(t, { focusId: '2', focusRequest: 1 })
  p.mountHooks.forEach((fn: () => void) => fn())
  assert.equal(p.seconds.value, 35)
  p.fixtureProps.items = [p.fixtureProps.items[2], p.fixtureProps.items[0], p.fixtureProps.items[1]]
  assert.equal(p.seconds.value, 5); assert.equal(p.activeItem.value.id, '2')
  p.fixtureProps.videoDuration = 4
  p.fixtureProps.focusRequest = 2
  assert.equal(p.seconds.value, 3.9); assert.equal(p.activeIndex.value, -1)
  t.mock.timers.tick(500); await flush()
  p.locateComment(); assert.deepEqual(p.events, [])
})

test('定位后仍能自由浏览，更新文字但顺序不变时不会跳回之前定位的评论', async t => {
  t.mock.method(globalThis, 'fetch', async () => frame())
  const p = await fixture(t, { focusId: '1', focusRequest: 1 })
  p.mountHooks.forEach((fn: () => void) => fn())
  p.setPosition(35.5)
  p.fixtureProps.items = p.fixtureProps.items.map((item: any) => ({ ...item, display_text: '更新文字' }))
  assert.equal(p.seconds.value, 35.5); assert.equal(p.activeItem.value.id, '2')
  t.mock.timers.tick(500); await flush()
  p.locateComment(); assert.deepEqual(p.events, [['locate-comment', '2']])
})

test('临时试放保留完整时间轴，精确定位候选且不保存或翻译评论', async t => {
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => { requests.push({ url, body: JSON.parse(init.body) }); return frame() })
  const item = { id: 'not-selected', source_hash: 'hash-1', display_text: '仅预览此评论' }
  const items = [{ id: 'selected-before', display_text: '前一条' }, item, { id: 'selected-after', display_text: '后一条' }]
  const p = await fixture(t, { items, focusId: item.id, focusRequest: 1, temporary: true })
  const originalItems = JSON.stringify(p.fixtureProps.items)
  p.mountHooks.forEach((fn: () => void) => fn())
  await flush()
  assert.equal(p.seconds.value, 20); assert.equal(p.activeIndex.value, 1)
  assert.deepEqual(p.cues.value.map((cue: any) => cue.start), [5, 20, 35])
  assert.equal(p.end.value, 41); assert.equal(p.axisEnd.value, 60)
  assert.equal(requests.length, 1)
  assert.deepEqual(requests[0].body.items.map((row: any) => row.id), items.map(row => row.id))
  p.locateComment(); assert.deepEqual(p.events, [['locate-comment', item.id]])
  assert.equal(JSON.stringify(p.fixtureProps.items), originalItems)
  assert.ok(requests.every(request => request.url.endsWith('/comments/preview')))
  assert.match(descriptor.template!.content, /<strong>整体上屏范围<\/strong>/)
  assert.match(descriptor.template!.content, /含待选评论试放，不改变上屏方案/)
  assert.doesNotMatch(descriptor.template!.content, /单条预览 ·/)
})

test('旧画面未更新和评论间空档不允许误定位，更新后返回当前实际 ID', async t => {
  const resolvers: Array<(value: Response) => void> = []
  t.mock.method(globalThis, 'fetch', () => new Promise<Response>(resolve => resolvers.push(resolve)))
  const p = await fixture(t)
  p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush(); resolvers[0](frame()); await flush()
  assert.equal(p.canLocateComment.value, true)
  p.setPosition(20.5)
  assert.equal(p.activeItem.value.id, '1'); assert.equal(p.canLocateComment.value, false)
  p.locateComment(); assert.deepEqual(p.events, [])
  t.mock.timers.tick(500); await flush()
  p.locateComment(); assert.deepEqual(p.events, [])
  resolvers[1](frame()); await flush()
  p.locateComment(); assert.deepEqual(p.events, [['locate-comment', '1']])
  p.setPosition(12)
  assert.equal(p.activeItem.value, null); assert.equal(p.canLocateComment.value, false)
  p.locateComment(); assert.equal(p.events.length, 1)
})

test('空方案或失效评论 ID 不跳到错误评论，也不触发额外预览', async t => {
  let calls = 0
  t.mock.method(globalThis, 'fetch', async () => { calls++; return frame() })
  const p = await fixture(t, { items: [], focusId: 'missing', focusRequest: 1, temporary: true })
  p.mountHooks.forEach((fn: () => void) => fn())
  t.mock.timers.tick(500); await flush()
  assert.equal(calls, 0); assert.equal(p.activeIndex.value, -1)
  p.fixtureProps.focusRequest = 2
  p.locateComment(); assert.deepEqual(p.events, [])
})

let controlServer: ViteDevServer | undefined, Preview: any
after(async () => { await controlServer?.close() })
async function renderPreviewControls(t: any, options: { invalid?: boolean; seed?: (page: any) => void } = {}) {
  if (!controlServer) {
    controlServer = await createServer({ root, configFile: false, plugins: [vue()], appType: 'custom',
      server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
    Preview = (await controlServer.ssrLoadModule('/src/components/CommentPreview.vue')).default
  }
  const requests: any[] = [], buttons: any[] = [], icons: any[] = [], events: string[] = []
  let page: any, timeInput: any
  t.mock.method(globalThis, 'fetch', (...args: any[]) => { requests.push(args); throw new Error('展示预览工具条不得启动网络请求') })
  const props = { uid: 'fixture', invalid: options.invalid || false, videoDuration: 60,
    items: [0, 1, 2].map(id => ({ id: String(id), display_text: '中文评论', selection_origin: id === 1 ? 'manual' : 'automatic' })),
    settings: { start: 5, interval: 15, duration: 6 }, onLocateComment: (id: string) => events.push(id) }
  const beforeInput = JSON.stringify(props)
  const app = createSSRApp({ render: () => h(Preview, props) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'CommentPreview') {
      page = this.$.setupState; page.seconds = 20.5; page.image = 'blob:preview-fixture'; page.rendered = page.signature
      options.seed?.(page)
    }
    if (this.$options.name === 'Button') buttons.push(this)
    if (this.$options.__name === 'AppIcon') icons.push(this)
    if (this.$options.name === 'InputNumber' && this.$props.inputProps?.['aria-label'] === '评论预览秒数') timeInput = this
  } })
  const html = await renderToString(app)
  assert.equal(JSON.stringify(props), beforeInput)
  return { html, page, buttons, icons, timeInput, events, requests, props }
}
const clickControl = (button: any) => button.$props.onClick({ preventDefault() {}, stopPropagation() {} })

test('真实预览按钮使用图标和短文本，保留可访问名称及当前评论定位事件', async t => {
  const result = await renderPreviewControls(t)
  const locate = result.buttons.find(button => button.$attrs['aria-label'] === '定位评论列表')
  const start = result.buttons.find(button => button.$attrs['aria-label'] === '定位到开始')
  assert.ok(locate); assert.ok(start)
  assert.equal(locate.$props.size, 'small'); assert.equal(locate.$props.secondary, true)
  assert.equal(start.$props.quaternary, true)
  assert.ok(result.icons.some(icon => icon.$props.name === 'locate'))
  assert.ok(result.icons.some(icon => icon.$props.name === 'skip-back'))
  assert.match(result.html, /返回列表/); assert.match(result.html, /上屏起点/)
  assert.match(result.html, /aria-label="评论预览定位操作"/)
  assert.match(result.html, /<svg[^>]*aria-hidden="true"/)
  assert.equal(locate.$props.disabled, false)
  clickControl(locate)
  assert.deepEqual(result.events, ['1'])
  assert.equal(result.props.items[1].selection_origin, 'manual', '工具条不能改变手动选择来源')
  assert.equal(result.requests.length, 0)
})

test('真实起点按钮与精确秒数输入仍驱动同一定位逻辑，不保存或改写方案', async t => {
  const result = await renderPreviewControls(t)
  const start = result.buttons.find(button => button.$attrs['aria-label'] === '定位到开始')
  clickControl(start)
  assert.equal(result.page.seconds, 5)
  assert.ok(result.timeInput); assert.equal(result.timeInput.$props.size, 'small')
  assert.equal(result.timeInput.$props.updateValueOnInput, true); assert.equal(result.timeInput.$props.step, .1)
  assert.equal(result.timeInput.$props.min, 0); assert.equal(result.timeInput.$props.max, 59.9)
  result.timeInput.$emit('update:value', 35.1)
  assert.equal(result.page.seconds, 35.1)
  assert.equal(result.page.activeItem.id, '2')
  assert.equal(result.requests.length, 0)
})

test('旧帧和等待中的真实列表定位按钮保持禁用，不能误发定位事件', async t => {
  for (const seed of [(page: any) => { page.rendered = 'old-frame' }, (page: any) => { page.pending = true }]) {
    const result = await renderPreviewControls(t, { seed })
    const locate = result.buttons.find(button => button.$attrs['aria-label'] === '定位评论列表')
    assert.equal(locate.$props.disabled, true)
    clickControl(locate)
    assert.deepEqual(result.events, [])
  }
})

test('真实错误操作以刷新图标呈现重试，保留错误文本与禁用加载状态', async t => {
  for (const state of [{ invalid: false, loading: false }, { invalid: true, loading: false }, { invalid: false, loading: true }]) {
    const result = await renderPreviewControls(t, { invalid: state.invalid, seed: page => { page.error = '画面生成失败'; page.loading = state.loading } })
    const retry = result.buttons.find(button => button.$attrs['aria-label'] === '重试预览')
    assert.ok(retry); assert.equal(retry.$props.secondary, true); assert.equal(retry.$props.size, 'small')
    assert.equal(retry.$props.disabled, state.invalid || state.loading)
    assert.equal(retry.$props.loading, state.loading)
    assert.equal(retry.$props.onClick, result.page.preview)
    if (!state.loading) assert.ok(result.icons.some(icon => icon.$props.name === 'refresh'))
    assert.match(result.html, /画面生成失败/)
    assert.equal(result.requests.length, 0)
  }
})
