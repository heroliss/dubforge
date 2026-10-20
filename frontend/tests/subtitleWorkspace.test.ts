import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
import { SUBTITLE_STYLE_PATHS, SUBTITLE_WORKSPACE_PATHS, subtitleSettingsLink, subtitleStyleSummary, subtitleTimelineDuration, subtitleTimeLabel } from '../src/utils/subtitleWorkspace.ts'
import { cuePosition } from '../src/utils/previewTimeline.ts'

// 执行组件真实脚本，用响应式测试边界替换界面及挂载生命周期；不接触用户项目。
const root = fileURLToPath(new URL('..', import.meta.url))
async function compile(name: string, exports: string) {
  const source = await readFile(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8')
  const { descriptor } = parse(source)
  const result = await build({
    stdin: { contents: `import { reactive, unmountHooks } from 'vue';
      const fixtureProps = reactive(__TEST_PROPS__), events = [];
      const defineProps = () => fixtureProps, defineEmits = () => (...args) => events.push(args);
      ${descriptor.scriptSetup!.content}
      export { fixtureProps, events, unmountHooks, ${exports} };`, loader: 'ts', resolveDir: `${root}/src/components` },
    bundle: true, write: false, format: 'esm', platform: 'node',
    plugins: [{ name: 'subtitle-test-boundary', setup(builder) {
      builder.onResolve({ filter: /^(vue|naive-ui)$|\.vue$|stores\/nav$/ }, args => ({ path: args.path, namespace: 'subtitle-test' }))
      builder.onLoad({ filter: /.*/, namespace: 'subtitle-test' }, args => ({
        contents: args.path === 'vue'
          ? `export { computed, ref, reactive, watch } from '@vue/reactivity';
             export const unmountHooks = [], onUnmounted = fn => unmountHooks.push(fn);`
          : args.path === 'naive-ui' ? `export const useMessage = () => ({ success() {}, error() {} });`
          : args.path.endsWith('/nav') ? `export const useNavStore = () => ({ poll: async () => {} });`
          : 'export default {};',
        resolveDir: root,
      }))
    } }],
  })
  return result.outputFiles[0].text
}
const formBundle = await compile('ProjectParameterForm', 'draft, dirty, fields, groups, save, discard, resetAll, disabledField, applyTextColors, hasTextColors, fieldSource, editField, inheritField, enableSourceStyle')
const workspaceBundle = await compile('SubtitleWorkspace', 'draft, seconds, image, error, loading, preview, chooseLine, canPreview, signature, rendered, outdated, setPosition, beginScrub, endScrub, previewEnd, timelineDuration, sliderValue, cues')
let fixtureId = 0
async function fixture(t: any, bundle: string, props: any) {
  const script = bundle.replace('__TEST_PROPS__', JSON.stringify(props)) + `\n// fixture ${fixtureId++}`
  const page = await import(`data:text/javascript;base64,${Buffer.from(script).toString('base64')}`)
  t.after(() => page.unmountHooks.forEach((fn: () => void) => fn()))
  return page
}
async function flush() { for (let i = 0; i < 15; i++) await Promise.resolve() }

const defaults = Object.fromEntries(SUBTITLE_WORKSPACE_PATHS.map(path => [path,
  path === 'assemble.subtitle_track' ? 'bilingual' : path === 'subtitles.mode' ? 'all'
    : path === 'subtitles.source_first' || path === 'assemble.subtitle_source_style_enabled' ? false
    : path === 'assemble.subtitle_auto_fit' ? true
    : path === 'assemble.subtitle_min_size' ? 32 : path === 'assemble.subtitle_source_min_size' ? 28
    : path === 'assemble.subtitle_shrink_width_percent' ? 80 : path === 'assemble.subtitle_wrap_width_percent' ? 90
    : path === 'subtitles.min_duration' ? 1 : 48]))
function schema() {
  const fields = [...SUBTITLE_WORKSPACE_PATHS, 'assemble.voice_volume', 'assemble.burn_subtitles'].map(path => ({
    path, label: path, type: ['subtitles.source_first', 'assemble.subtitle_auto_fit', 'assemble.subtitle_source_style_enabled'].includes(path) ? 'bool'
      : ['assemble.subtitle_shrink_width_percent', 'assemble.subtitle_wrap_width_percent'].includes(path) ? 'int' : 'str',
    depends_on: ['assemble.subtitle_min_size', 'assemble.subtitle_shrink_width_percent'].includes(path) ? [['assemble.subtitle_auto_fit', true]]
      : path === 'assemble.subtitle_source_min_size' ? [['assemble.subtitle_source_style_enabled', true], ['assemble.subtitle_auto_fit', true]] : [],
  }))
  return { defaults, steps: Object.fromEntries(['assemble', 'subtitles'].map(key => [key, {
    fields: fields.filter(field => field.path.startsWith(`${key}.`)), values: { ...defaults },
  }])) }
}

test('字幕设置只读取一次，跨两个步骤的字段一次保存；不提交混音或流程开关', async t => {
  const data = schema(), requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => {
    const body = init.body && JSON.parse(init.body)
    requests.push({ url, method: init.method || 'GET', body })
    if (body) for (const step of Object.values(data.steps)) Object.assign(step.values, body.updates)
    return new Response(JSON.stringify(data))
  })
  const form = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', additionalStepKeys: ['subtitles'],
    paths: SUBTITLE_WORKSPACE_PATHS, sectionTitles: { assemble: '烧录内容与样式', subtitles: '字幕文件与时间' } })
  await flush()
  assert.equal(requests.length, 1)
  assert.equal(form.fields.value.length, SUBTITLE_WORKSPACE_PATHS.length)
  assert.deepEqual(form.groups.value.map((g: any) => g.name), ['烧录内容与样式', '译文样式', '原文样式', '字幕文件与时间'])
  assert.ok(form.fields.value.some((field: any) => field.path === 'assemble.subtitle_background_color'))
  assert.ok(form.fields.value.some((field: any) => field.path === 'assemble.subtitle_background_opacity'))
  assert.ok(form.fields.value.some((field: any) => field.path === 'assemble.subtitle_outline_color'))
  assert.ok(form.fields.value.some((field: any) => field.path === 'assemble.subtitle_outline'))
  form.draft.value['assemble.subtitle_size'] = 72
  form.draft.value['subtitles.source_first'] = true
  assert.equal(form.dirty.value, true)
  await form.save()
  assert.deepEqual(requests.filter(r => r.method === 'PUT').map(r => r.body), [{
    updates: { 'assemble.subtitle_size': 72, 'subtitles.source_first': true }, reset: [],
  }])
  assert.equal(form.dirty.value, false)
  assert.equal(form.events.filter(([name]: any) => name === 'saved').length, 1)
})

test('放弃修改只恢复本地已保存值；保存失败仍保留字幕草稿', async t => {
  let writes = 0
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => {
    if (init.method === 'PUT') { writes++; return new Response('{"message":"测试保存失败"}', { status: 400 }) }
    return new Response(JSON.stringify(schema()))
  })
  const form = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', additionalStepKeys: ['subtitles'], paths: SUBTITLE_WORKSPACE_PATHS })
  await flush()
  form.draft.value['assemble.subtitle_size'] = 80
  await form.save()
  assert.equal(form.dirty.value, true)
  assert.equal(form.draft.value['assemble.subtitle_size'], 80)
  form.discard()
  assert.equal(form.dirty.value, false)
  assert.equal(form.draft.value['assemble.subtitle_size'], 48)
  assert.equal(writes, 1)
  form.draft.value['subtitles.mode'] = 'source'
  assert.equal(form.disabledField({ path: 'subtitles.source_first' }), false, '烧录选择双语时仍可调整顺序')
  form.draft.value['assemble.subtitle_track'] = 'source'
  assert.equal(form.disabledField({ path: 'subtitles.source_first' }), true)
})

test('字幕配色仅改两色、显式保存才持久化；过滤页无入口', async t => {
  const data = schema(), writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => {
    if (init.method === 'PUT') writes.push(JSON.parse(init.body))
    return new Response(JSON.stringify(data))
  })
  const form = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', paths: SUBTITLE_STYLE_PATHS })
  await flush()
  const before = { ...form.draft.value }
  assert.equal(form.groups.value.some((group:any)=>form.hasTextColors(group)), true)
  form.applyTextColors('sunshine')
  assert.equal(writes.length, 0); assert.equal(form.dirty.value, true)
  assert.deepEqual({ ...form.draft.value }, { ...before, 'assemble.subtitle_color': '#FFD65A', 'assemble.subtitle_outline_color': '#30213A' })
  await form.save()
  assert.deepEqual(writes, [{ updates: { 'assemble.subtitle_color': '#FFD65A', 'assemble.subtitle_outline_color': '#30213A' }, reset: [] }])
  form.fixtureProps.disabled = true
  const saved = { ...form.draft.value }; form.applyTextColors('mint'); assert.deepEqual({ ...form.draft.value }, saved)
  const filtered = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', excludePaths: SUBTITLE_STYLE_PATHS })
  await flush()
  assert.equal(filtered.hasTextColors(filtered.groups.value[0]), false)
  filtered.applyTextColors('sunshine'); assert.equal(filtered.dirty.value, false)
})

test('原文配色只改原文，字号和语言顺序保持，单字段恢复继承不改变译文',async t=>{
  const data=schema(),writes:any[]=[]
  t.mock.method(globalThis,'fetch',async(_url:string,init:any)=>{
    if(init.method==='PUT')writes.push(JSON.parse(init.body))
    return new Response(JSON.stringify(data))
  })
  const form=await fixture(t,formBundle,{uid:'fixture',stepKey:'assemble',paths:SUBTITLE_STYLE_PATHS})
  await flush()
  const before={...form.draft.value}
  form.editField('assemble.subtitle_source_style_enabled',true)
  form.applyTextColors('sunshine',true)
  assert.equal(form.draft.value['assemble.subtitle_source_color'],'#FFD65A')
  assert.equal(form.draft.value['assemble.subtitle_color'],before['assemble.subtitle_color'])
  assert.equal(form.draft.value['assemble.subtitle_source_size'],before['assemble.subtitle_source_size'])
  await form.save()
  assert.deepEqual(writes[0].updates,{'assemble.subtitle_source_style_enabled':true,
    'assemble.subtitle_source_color':'#FFD65A','assemble.subtitle_source_outline_color':'#30213A'})
  form.inheritField('assemble.subtitle_source_color')
  assert.equal(form.draft.value['assemble.subtitle_source_color'],data.defaults['assemble.subtitle_source_color'])
})

test('字幕原文快捷入口仅启用独立草稿，保留配色；锁定不修改，恢复继承仍独立', async t => {
  const data=schema(),path='assemble.subtitle_source_style_enabled'
  data.defaults[path]=false
  for(const step of Object.values(data.steps))step.values[path]=false
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url:string,init:any)=>{requests.push(init.method||'GET');return new Response(JSON.stringify(data))})
  const form=await fixture(t,formBundle,{uid:'fixture',stepKey:'assemble',paths:SUBTITLE_WORKSPACE_PATHS})
  await flush()
  const color=form.draft.value['assemble.subtitle_source_color']
  form.enableSourceStyle()
  assert.equal(form.draft.value[path],true)
  assert.equal(form.dirty.value,true)
  assert.equal(form.draft.value['assemble.subtitle_source_color'],color)
  form.inheritField(path)
  assert.equal(form.draft.value[path],false)
  form.fixtureProps.disabled=true
  form.enableSourceStyle()
  assert.equal(form.draft.value[path],false)
  assert.deepEqual(requests,['GET'])
})

test('合成页过滤重复字幕控件，保留自己的混音与流程参数', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(schema())))
  const form = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', excludePaths: SUBTITLE_STYLE_PATHS })
  await flush()
  assert.deepEqual(form.fields.value.map((f: any) => f.path), ['assemble.voice_volume', 'assemble.burn_subtitles'])
  assert.deepEqual(subtitleSettingsLink('fixture'), { path: '/projects/fixture/steps/tts', query: { panel: 'subtitles', from: 'assemble' } })
  assert.match(subtitleStyleSummary({ subtitle_track: 'bilingual', subtitle_size: 80 }), /双语.*字号 32–80.*优先缩小/)
})

test('字幕摘要显示实际字号范围，关闭缩放和原文独立样式有对应说明', () => {
  const independent = { subtitle_track: 'bilingual', subtitle_size: 80, subtitle_min_size: 34,
    subtitle_source_style_enabled: true, subtitle_source_size: 40, subtitle_source_min_size: 28,
    subtitle_source_font: 'Arial' }
  assert.match(subtitleStyleSummary(independent), /双语.*译文 34–80 \/ 原文 28–40.*优先缩小/)
  const fixed = subtitleStyleSummary({ ...independent, subtitle_auto_fit: false })
  assert.match(fixed, /双语.*译文 80 \/ 原文 40.*自动换行/)
  assert.doesNotMatch(fixed, /34–80|28–40|优先缩小/)
  assert.match(subtitleStyleSummary({ ...independent, subtitle_track: 'source' }), /原文 · Arial · 字号 28–40/)
  assert.match(subtitleStyleSummary({ ...independent, subtitle_source_style_enabled: false }), /双语.*字号 34–80/)
})

test('字幕摘要区分缩小目标与换行上限，关闭自适应仅显示换行宽度', () => {
  assert.match(subtitleStyleSummary(), /缩小 80% \/ 换行 90%/)
  const cfg = { subtitle_shrink_width_percent: 75, subtitle_wrap_width_percent: 92 }
  assert.match(subtitleStyleSummary(cfg), /缩小 75% \/ 换行 92%/)
  const disabled = subtitleStyleSummary({ ...cfg, subtitle_auto_fit: false })
  assert.match(disabled, /换行 92%/)
  assert.doesNotMatch(disabled, /缩小 75%/)
})

test('两项宽度读取一次并归整体设置，关闭自适应只禁用缩小目标且保值，一次保存全部修改', async t => {
  const data = schema(), requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => {
    const body = init.body && JSON.parse(init.body)
    requests.push({ url, method: init.method || 'GET', body })
    if (body) for (const step of Object.values(data.steps)) Object.assign(step.values, body.updates)
    return new Response(JSON.stringify(data))
  })
  const form = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', paths: SUBTITLE_STYLE_PATHS })
  await flush()
  assert.equal(requests.length, 1)
  const shrink = form.fields.value.find((field: any) => field.path === 'assemble.subtitle_shrink_width_percent')
  const wrap = form.fields.value.find((field: any) => field.path === 'assemble.subtitle_wrap_width_percent')
  assert.ok(shrink); assert.ok(wrap)
  assert.equal(form.draft.value[shrink.path], 80)
  assert.equal(form.draft.value[wrap.path], 90)
  const common = form.groups.value.find((group: any) => group.name === '')
  assert.ok(common.fields.some((field: any) => field.path === shrink.path))
  assert.ok(common.fields.some((field: any) => field.path === wrap.path))
  assert.equal(form.disabledField(shrink), false)
  assert.equal(form.disabledField(wrap), false)
  form.editField(shrink.path, 76)
  form.editField(wrap.path, 94)
  form.editField('assemble.subtitle_auto_fit', false)
  assert.equal(form.disabledField(shrink), true)
  assert.equal(form.disabledField(wrap), false)
  assert.equal(form.draft.value[shrink.path], 76)
  assert.equal(form.draft.value[wrap.path], 94)
  assert.equal(requests.length, 1, '切换与修改不应自动保存或预览')
  await form.save()
  assert.deepEqual(requests.filter(request => request.method === 'PUT').map(request => request.body), [{
    updates: {
      'assemble.subtitle_auto_fit': false,
      'assemble.subtitle_shrink_width_percent': 76,
      'assemble.subtitle_wrap_width_percent': 94,
    }, reset: [],
  }])
  assert.equal(form.dirty.value, false)
  assert.equal(form.draft.value[shrink.path], 76)
  assert.equal(form.draft.value[wrap.path], 94)
})

test('长字幕自适应范围随草稿生效，关闭只禁用下限且保留值，保存一次提交', async t => {
  const data = schema(), writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => {
    if (init.method === 'PUT') {
      const body = JSON.parse(init.body); writes.push(body)
      for (const step of Object.values(data.steps)) Object.assign(step.values, body.updates)
    }
    return new Response(JSON.stringify(data))
  })
  const form = await fixture(t, formBundle, { uid: 'fixture', stepKey: 'assemble', paths: SUBTITLE_STYLE_PATHS })
  await flush()
  for (const path of ['assemble.subtitle_auto_fit', 'assemble.subtitle_min_size', 'assemble.subtitle_source_min_size']) {
    assert.ok(form.fields.value.some((field: any) => field.path === path))
  }
  const translated = form.fields.value.find((field: any) => field.path === 'assemble.subtitle_min_size')
  const original = form.fields.value.find((field: any) => field.path === 'assemble.subtitle_source_min_size')
  assert.equal(form.disabledField(translated), false)
  assert.equal(form.disabledField(original), true)
  form.enableSourceStyle()
  assert.equal(form.disabledField(original), false)
  form.editField('assemble.subtitle_min_size', 34)
  form.editField('assemble.subtitle_source_min_size', 30)
  form.editField('assemble.subtitle_auto_fit', false)
  assert.equal(form.disabledField(translated), true)
  assert.equal(form.disabledField(original), true)
  assert.equal(form.draft.value['assemble.subtitle_min_size'], 34)
  assert.equal(form.draft.value['assemble.subtitle_source_min_size'], 30)
  assert.equal(form.disabledField({ path: 'assemble.subtitle_size' }), false)
  assert.equal(form.draft.value['assemble.subtitle_size'], 48)
  assert.equal(writes.length, 0)
  await form.save()
  assert.deepEqual(writes, [{ updates: {
    'assemble.subtitle_source_style_enabled': true,
    'assemble.subtitle_min_size': 34,
    'assemble.subtitle_source_min_size': 30,
    'assemble.subtitle_auto_fit': false,
  }, reset: [] }])
  assert.equal(form.dirty.value, false)
})

test('步骤和本句来源随编辑、恢复继承、放弃修改准确切换，同值也保留明确覆盖', async t => {
  const data = schema() as any, path = 'assemble.subtitle_size'
  data.sources = { [path]: {kind:'series',name:'全局默认'} }
  data.parent_sources = data.sources
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(data)))
  const form = await fixture(t, formBundle, {uid:'fixture',stepKey:'assemble'})
  await flush()
  assert.equal(form.fieldSource(path).kind, 'series')
  form.editField(path, form.draft.value[path])
  assert.equal(form.fieldSource(path).kind, 'project')
  form.inheritField(path)
  assert.equal(form.fieldSource(path).kind, 'series')
  form.discard()
  assert.equal(form.fieldSource(path).kind, 'series')
  const line = await fixture(t, formBundle, {uid:'fixture',stepKey:'assemble',lineId:1})
  await flush()
  line.editField(path, 0)
  assert.equal(line.fieldSource(path).kind, 'line')
  line.inheritField(path)
  assert.equal(line.fieldSource(path).kind, 'series')
})

test('预览串行且丢弃旧响应，合并快速修改并传入草稿顺序与时长', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const requests: any[] = [], completed: Array<(response: Response) => void> = []
  t.mock.method(globalThis, 'fetch', (_url: string, init: any) => {
    requests.push(JSON.parse(init.body))
    return new Promise(resolve => completed.push(resolve))
  })
  const page = await fixture(t, workspaceBundle, { uid: 'fixture', active: false, position: 0, lines: [], subtitleState: {} })
  page.fixtureProps.lines = [{ id: 1, start: 1, end: 2, text: '字幕', source_text: 'text' }]
  page.draft.value = { ...defaults }
  page.fixtureProps.active = true
  assert.equal(page.seconds.value, 1.5)
  t.mock.timers.tick(650)
  assert.equal(requests.length, 1)
  page.draft.value['subtitles.source_first'] = true
  page.draft.value['subtitles.min_duration'] = 2
  page.draft.value['assemble.subtitle_shrink_width_percent'] = 76
  page.draft.value['assemble.subtitle_wrap_width_percent'] = 94
  t.mock.timers.tick(650)
  assert.equal(requests.length, 1, '旧请求未完成时不能并发调用 FFmpeg')
  completed[0](new Response('old-image'))
  await flush()
  assert.equal(page.image.value, '', '不能展示旧草稿的响应')
  t.mock.timers.tick(650)
  assert.equal(requests.length, 2)
  assert.equal(requests[1].settings['subtitles.source_first'], true)
  assert.equal(requests[1].settings['subtitles.min_duration'], 2)
  assert.equal(requests[1].settings['assemble.subtitle_auto_fit'], true)
  assert.equal(requests[1].settings['assemble.subtitle_min_size'], 32)
  assert.equal(requests[1].settings['assemble.subtitle_source_min_size'], 28)
  assert.equal(requests[1].settings['assemble.subtitle_shrink_width_percent'], 76)
  assert.equal(requests[1].settings['assemble.subtitle_wrap_width_percent'], 94)
  assert.equal('subtitles.mode' in requests[1].settings, false)
  completed[1](new Response('new-image'))
  await flush()
  assert.match(page.image.value, /^blob:/)
  assert.equal(page.outdated.value, false)
  page.fixtureProps.active = false
  page.draft.value['assemble.subtitle_size'] = 90
  t.mock.timers.tick(1000)
  assert.equal(requests.length, 2, '隐藏的工作区不得继续渲染')
  assert.equal(page.draft.value['assemble.subtitle_size'], 90, '切换标签不能丢失草稿')
  page.fixtureProps.active = true
  t.mock.timers.tick(650)
  assert.equal(requests.length, 3)
  completed[2](new Response('latest-image'))
  await flush()
})

test('没有视频不发起预览，预览失败保留草稿且不无限重试', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let requests = 0
  t.mock.method(globalThis, 'fetch', async () => { requests++; return new Response('{"message":"没有译文"}', { status: 400 }) })
  const page = await fixture(t, workspaceBundle, { uid: 'fixture', active: true, noVideo: true, position: 0,
    lines: [{ id: 1, start: 1, end: 2, text: '字幕' }], subtitleState: {} })
  page.draft.value = { ...defaults }
  t.mock.timers.tick(1000)
  await page.preview()
  assert.equal(requests, 0)
  page.fixtureProps.noVideo = false
  t.mock.timers.tick(650)
  await flush()
  assert.equal(requests, 1)
  assert.equal(page.error.value, '没有译文')
  assert.deepEqual(page.draft.value, defaults)
  t.mock.timers.tick(5000)
  assert.equal(requests, 1)
})

test('拖动期间不请求画面，松手后只渲染最终位置；键盘/秒数输入同样防抖', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => {
    requests.push(JSON.parse(init.body)); return new Response('frame')
  })
  const page = await fixture(t, workspaceBundle, { uid: 'fixture', active: true, duration: 120, position: 0,
    lines: [{ id: 1, start: 1, end: 5, text: '字幕' }], subtitleState: {} })
  page.draft.value = { ...defaults }
  page.beginScrub()
  page.setPosition(15)
  t.mock.timers.tick(1000)
  page.setPosition(80)
  t.mock.timers.tick(1000)
  assert.equal(requests.length, 0)
  assert.equal(page.seconds.value, 80)
  assert.equal(page.sliderValue.value, 80)
  assert.equal(page.fixtureProps.position, 0, '拖动不改变播放器位置')
  page.endScrub()
  t.mock.timers.tick(650)
  await flush()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].seconds, 80)
  assert.equal(page.outdated.value, false)
  page.setPosition(81)
  t.mock.timers.tick(300)
  page.setPosition(82)
  t.mock.timers.tick(650)
  await flush()
  assert.equal(requests.length, 2)
  assert.equal(requests[1].seconds, 82)
  assert.ok(requests.every(body => !('lines' in body)), '不提交或修改字幕时间轴')
})

test('定位范围优先视频时长，未就绪回退字幕终点；末尾、空值及超长输入有边界', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const page = await fixture(t, workspaceBundle, { uid: 'fixture', active: false, position: 0,
    lines: [{ id: 1, start: 1, end: 5, text: '字幕' }], subtitleState: {} })
  assert.equal(page.timelineDuration.value, 5)
  page.fixtureProps.duration = 120
  assert.equal(page.timelineDuration.value, 120)
  page.setPosition(200)
  assert.equal(page.seconds.value, 119.9)
  page.fixtureProps.duration = 90
  assert.equal(page.seconds.value, 89.9)
  page.setPosition(-20)
  assert.equal(page.seconds.value, 0)
  page.setPosition(null)
  assert.equal(page.seconds.value, null)
  assert.equal(page.canPreview.value, false)
  assert.equal(page.sliderValue.value, 0)
  assert.equal(subtitleTimelineDuration(Infinity, [{ end: 5 }, { end: NaN }]), 5)
  assert.equal(subtitleTimelineDuration(undefined, []), 0)
  assert.equal(subtitleTimeLabel(3671.2), '01:01:11.2')
  assert.equal(subtitleTimeLabel(null), '—')
})

test('字幕开始点准确跟随编辑，定位只更新预览而不改字幕或播放器', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { requests.push(JSON.parse(init.body)); return new Response('frame') })
  const lines = [{ id: 1, start: 1.02, end: 2, text: '第一句' }, { id: 2, start: 10, end: 12, source_text: 'Second' }]
  const p = await fixture(t, workspaceBundle, { uid: 'fixture', active: true, position: 0, duration: 30, lines, subtitleState: {} })
  p.draft.value = { ...defaults }
  assert.deepEqual(p.cues.value.map((cue: any) => cue.start), [1.02, 10])
  p.fixtureProps.lines[1].start = 11
  assert.match(p.cues.value[1].label, /第 2 句 · Second/)
  p.setPosition(cuePosition(p.cues.value[1], p.previewEnd.value))
  t.mock.timers.tick(650); await flush()
  assert.equal(requests.length, 1); assert.equal(requests[0].seconds, 11.1)
  assert.equal(p.fixtureProps.position, 0); assert.equal(p.fixtureProps.lines[1].start, 11)
  assert.equal(p.events.length, 0)
})

test('字幕工作区定位使用播放排布，选句、拖动条与实际烧录预览时钟一致', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => { requests.push(JSON.parse(init.body)); return new Response('frame') })
  const lines = [{ id: 1, start: 1, end: 2, playback_start: .5, playback_end: 3, text: '较长配音' }]
  const p = await fixture(t, workspaceBundle, { uid: 'fixture', active: true, position: 0, duration: 30, lines, subtitleState: {} })
  p.draft.value = { ...defaults }
  assert.equal(p.cues.value[0].start, .5); assert.equal(p.cues.value[0].end, 3)
  p.chooseLine(1)
  assert.equal(p.seconds.value, 1.75)
  t.mock.timers.tick(650); await flush()
  assert.equal(requests[0].seconds, 1.75)
  assert.equal(p.fixtureProps.lines[0].start, 1); assert.equal(p.fixtureProps.lines[0].end, 2)
})
