import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { adjacentCue, cuePosition, timelineCues } from '../src/utils/previewTimeline.ts'

test('开始点排序、重叠合并、边界及非法时间不会修改原始内容', () => {
  const raw = [
    { start: 20, end: 25, label: '后' }, { start: 0, end: 2, label: '首' },
    { start: 20, end: 24, label: '重叠' }, { start: 30, end: 32, label: '边界' },
    { start: 31, end: 32, label: '越界' }, { start: NaN, end: 3, label: '无效' },
    { start: -1, end: 3, label: '负数' }, { start: 5, end: 5, label: '零时长' },
    { start: 6, end: Infinity, label: '无效终点' },
  ]
  const copy = structuredClone(raw), cues = timelineCues(raw, 30)
  assert.deepEqual(cues.map(cue => cue.start), [0, 20, 30])
  assert.equal(cues[1].label, '后\n重叠'); assert.equal(cues[1].end, 24)
  assert.deepEqual(raw, copy)
  assert.deepEqual(timelineCues([], 0), [])
})

test('点击稍进入内容区间，短句和视频终点不越界；前后定位可遍历密集刻度', () => {
  const short = { start: 1.02, end: 1.06, label: '短句' }
  assert.equal(cuePosition(short, 20), 1.04)
  assert.equal(cuePosition({ start: 19.95, end: 21, label: '尾' }, 20), 20)
  const cues = timelineCues(Array.from({ length: 1000 }, (_, i) => ({ start: i * .3, end: i * .3 + .2, label: String(i) })), 300)
  assert.equal(cues.length, 1000)
  assert.equal(adjacentCue(cues, 0, -1, 300), undefined)
  assert.equal(adjacentCue(cues, 0, 1, 300), cues[0])
  for (let i = 1; i < cues.length - 1; i++) {
    const position = cuePosition(cues[i], 300)
    assert.equal(adjacentCue(cues, position, -1, 300), cues[i - 1])
    assert.equal(adjacentCue(cues, position, 1, 300), cues[i + 1])
  }
  assert.equal(adjacentCue(cues, 300, 1, 300), undefined)
})

// 执行实际刻度组件事件，不连接浏览器或项目；实际 Naive UI 模板另由 SSR 测试覆盖。
const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/PreviewTimeline.vue', import.meta.url), 'utf8'))
const result = await build({ stdin: { contents: `import { reactive } from 'vue';
  const propsFixture = reactive({ value: 0, max: 30, cues: [{ start: 5, end: 6, label: '第一条' }, { start: 20, end: 22, label: '第二条' }], kind: '字幕 / 评论', disabled: false });
  const events = [], defineProps = () => propsFixture, withDefaults = (props, defaults) => Object.assign(props, defaults), defineEmits = () => (...event) => events.push(event);
  ${descriptor.scriptSetup!.content}
  export { propsFixture, events, visibleCues, marks, seek, previous, next, cueDescription };`, loader: 'ts', resolveDir: `${root}/src/components` },
  bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'test-boundary', setup(b) {
    b.onResolve({ filter: /^(vue|naive-ui)$/ }, a => ({ path: a.path, namespace: 'test' }))
    b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({ contents: a.path === 'vue'
      ? `export { computed, reactive } from '@vue/reactivity'; export const h = (type, props, children) => ({ type, props, children });`
      : 'export {};', resolveDir: root }))
  } }] })

test('刻度点击阻止误拖动、精确定位，禁用后不能触发；时间修改实时更新', async () => {
  const p = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`)
  assert.deepEqual(Object.keys(p.marks.value), ['5', '20'])
  const marker = p.marks.value[20]()
  assert.equal(marker.type, 'button'); assert.match(marker.props.title, /00:20.0 · 第二条/)
  let stopped = 0
  const event = { stopPropagation() { stopped++ } }
  marker.props.onMousedown(event); marker.props.onTouchstart(event); marker.props.onClick(event)
  assert.equal(stopped, 3); assert.deepEqual(p.events, [['update:value', 20.1]])
  p.propsFixture.value = 20.1
  assert.equal(p.next.value, undefined); assert.equal(p.previous.value.start, 5)
  p.seek(p.previous.value); assert.deepEqual(p.events[1], ['update:value', 5.1])
  p.propsFixture.disabled = true; marker.props.onClick(event); assert.equal(p.events.length, 2)
  p.propsFixture.cues[0].start = 7; p.propsFixture.cues[0].end = 8
  assert.deepEqual(Object.keys(p.marks.value), ['7', '20'])
  p.propsFixture.max = 10; assert.deepEqual(Object.keys(p.marks.value), ['7'])
})

test('隐藏刻度只移除标记，前后定位与禁用保护仍然有效', async () => {
  const p = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text + '\n// hidden-markers').toString('base64')}`)
  assert.equal(p.propsFixture.showMarkers, true)
  p.propsFixture.showMarkers = false
  assert.equal(p.marks.value, undefined)
  assert.equal(p.cueDescription.value, '字幕 / 评论共 2处 · 可逐处定位')
  p.seek(p.next.value)
  assert.deepEqual(p.events, [['update:value', 5.1]])
  p.propsFixture.value = 20.1
  p.seek(p.previous.value)
  assert.deepEqual(p.events[1], ['update:value', 5.1])
  p.propsFixture.disabled = true
  p.seek(p.previous.value)
  assert.equal(p.events.length, 2)
  p.propsFixture.cues = []
  assert.equal(p.cueDescription.value, '暂无可预览的字幕 / 评论开始点')
})

let server: ViteDevServer, Timeline: any
before(async () => {
  server = await createServer({ root, configFile: false, plugins: [vue()], appType: 'custom',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Timeline = (await server.ssrLoadModule('/src/components/PreviewTimeline.vue')).default
})
after(() => server.close())

test('真实时间轴可隐藏密集竖线，保留滑块键盘和前后定位的可访问名称；默认仍显示刻度', async () => {
  const props = { value: 10, max: 60, label: '试片开始位置', kind: '字幕 / 评论',
    cues: [{ start: 5, end: 6, label: '第一条' }, { start: 20, end: 22, label: '第二条' }] }
  async function render(extra = {}) {
    const app = createSSRApp({ render: () => h(Timeline, { ...props, ...extra }) })
    setup(app)
    return renderToString(app)
  }
  const hidden = await render({ showMarkers: false })
  assert.doesNotMatch(hidden, /cue-marker|n-slider--with-mark|点击定位/)
  assert.match(hidden, /字幕 \/ 评论共 2处 · 可逐处定位/)
  assert.match(hidden, /role="slider"/)
  assert.match(hidden, /aria-label="试片开始位置"/)
  assert.match(hidden, /aria-label="上一处字幕 \/ 评论"/)
  assert.match(hidden, /aria-label="下一处字幕 \/ 评论"/)
  assert.match(hidden, /tabindex="0"/)
  const regular = await render()
  assert.equal((regular.match(/class="cue-marker/g) || []).length, 2)
  assert.match(regular, /刻度为字幕 \/ 评论开始点 · 点击定位/)
})
