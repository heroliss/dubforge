import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createRenderer, createSSRApp, h, nextTick, reactive, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'

let server: ViteDevServer, Presets: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Presets = (await server.ssrLoadModule('/src/components/DubbingPresets.vue')).default
})
after(() => server.close())

const prefix = 'tts.indextts25.'
const key = (name: string) => JSON.stringify(['shared', name])
const fields = [
  { path: prefix + 'timbre_mode', label: '音色参考来源', type: 'str', options: [['本句原声', 'dynamic'], ['自定义参考音频', 'preset']] },
  { path: prefix + 'timbre_audio', label: '音色参考文件', type: 'str' },
  { path: prefix + 'emo_audio', label: '情感参考文件', type: 'str' },
  { path: prefix + 'do_sample', label: '随机采样', type: 'bool' },
  { path: prefix + 'temperature', label: '采样温度', type: 'float' },
  { path: prefix + 'emo_vector', label: '情感向量', type: 'vector' },
  { path: prefix + 'request_timeout_seconds', label: '单句请求超时', type: 'int' },
]
const baseDraft = () => ({ [prefix + 'project_dir']: 'D:\\IndexTTS', [prefix + 'timbre_mode']: 'dynamic',
  [prefix + 'timbre_audio']: '', [prefix + 'emo_audio']: 'before-feeling.wav',
  [prefix + 'do_sample']: true, [prefix + 'temperature']: .6,
  [prefix + 'emo_vector']: [0, 0, 0, 0, 0, 0, 0, 0], [prefix + 'request_timeout_seconds']: 600 })
const presetValues = () => ({ [prefix + 'timbre_mode']: 'preset', [prefix + 'timbre_audio']: 'D:\\预设库\\声音.wav',
  [prefix + 'emo_audio']: 'D:\\预设库\\情感.wav', [prefix + 'do_sample']: false,
  [prefix + 'temperature']: .9, [prefix + 'emo_vector']: [.3, 0, 0, 0, 0, 0, 0, .2] })
const listing = () => ({ presets: ['甲', '乙', '损坏'].map(name => ({ name, storage: 'shared' })),
  directories: { shared: 'D:\\IndexTTS\\outputs\\presets', local: 'D:\\DubForge\\presets' } })
const response = (data: any, ok = true) => ({ ok, json: async () => data })
function deferred() {
  let resolve!: (value: any) => void
  const promise = new Promise<any>(yes => { resolve = yes })
  return { promise, resolve }
}
async function settle() { await nextTick(); await new Promise(resolve => setImmediate(resolve)); await nextTick() }

// 保留真实 Vue 客户端 watch、取消请求和事件顺序；空宿主只移除浏览器布局依赖。
const host = createRenderer<any, any>({
  createElement: () => ({ children: [] }), createText: () => ({}), createComment: () => ({}),
  setText() {}, setElementText() {}, patchProp() {}, parentNode: node => node.parent,
  nextSibling(node) { return node.parent?.children[node.parent.children.indexOf(node) + 1] || null },
  remove(node) { if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1); node.parent = null },
  insert(node, parent, anchor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
    node.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    parent.children.splice(index < 0 ? parent.children.length : index, 0, node)
  },
})
async function client(t: any, read: (body: any, options: any) => any = () => response({ values: presetValues(), warnings: [] })) {
  const requests: any[] = [], applied: any[] = [], busyEvents: boolean[] = []
  const props = reactive({ draft: baseDraft(), fields, disabled: false, scopeLabel: '项目参数' })
  let page: any, parentBusy = false
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    requests.push({ url: String(url), options })
    if (!options?.body) return response(listing())
    return read(JSON.parse(options.body), options)
  })
  const Logic = { ...Presets, render: () => null, ssrRender: undefined }
  const app = host.createApp({ render: () => h(NMessageProvider, null, { default: () => h(Logic, {
    ...props, onBusyChange: (busy: boolean) => { parentBusy = busy; busyEvents.push(busy) },
    onLoad: (values: any) => {
      // 父表单运行/保存时会拒绝修改，因此载入事件必须在操作忙状态结束后发出。
      if (parentBusy) return
      applied.push(JSON.parse(JSON.stringify(values))); Object.assign(props.draft, values)
    },
  }) }) })
  app.provide(ssrContextKey, {})
  app.mixin({ created() { if (this.$options.__name === 'DubbingPresets') page = this.$.setupState } })
  app.mount({ children: [] }); t.after(() => app.unmount()); await settle()
  return { page, props, requests, applied, busyEvents, unmount: () => app.unmount() }
}

test('选择预设只读取差异，明确点击载入才逐项填入完整表单，未覆盖项继续保留', async t => {
  const f = await client(t), before = JSON.parse(JSON.stringify(f.props.draft))
  f.page.selected = key('甲'); await settle()
  assert.deepEqual(f.applied, [])
  assert.deepEqual(f.props.draft, before)
  assert.deepEqual(f.busyEvents, [], '只读预览不会将父表单锁成不可修改')
  assert.equal(f.page.previewLoading, false); assert.equal(f.page.busy, false)
  assert.equal(f.requests.filter(request => request.options?.body).length, 1)
  assert.match(f.requests.find(request => request.options?.body)!.url, /\/load$/)
  await f.page.load(); await settle()
  assert.deepEqual(f.applied, [presetValues()])
  for (const [path, value] of Object.entries(presetValues())) assert.deepEqual(f.props.draft[path], value, path)
  assert.equal(f.props.draft[prefix + 'request_timeout_seconds'], 600, '运行策略未被预设覆盖')
  assert.equal(f.props.draft[prefix + 'project_dir'], 'D:\\IndexTTS')
  assert.equal(f.requests.filter(request => request.options?.body).length, 1, '应用已读取候选不重复调用后端')
  assert.equal(f.page.busy, false)
  assert.equal(f.page.displayedChanges.length, 6, '载入后保留此次变化，不因当前值相同而隐藏')
  assert.equal(f.page.displayedUnchanged.length, 0, '载入前变化不重复列入相同设置')
  Object.assign(f.props.draft, before); await settle()
  assert.equal(f.page.loadedKey, null, '丢弃或重新编辑后不再声称已载入')
  assert.equal(f.page.note, '')
})

test('预览尚未完成、父表单禁用或选择已清除时，直接调用载入也不能写入', async t => {
  const pending = deferred(), f = await client(t, () => pending.promise)
  f.page.selected = key('甲'); await settle()
  assert.equal(f.page.previewLoading, true)
  await f.page.load(); assert.deepEqual(f.applied, [])
  pending.resolve(response({ values: presetValues(), warnings: [] })); await settle()
  f.props.disabled = true; await nextTick(); await f.page.load()
  assert.deepEqual(f.applied, [])
  f.props.disabled = false; f.page.selected = null; await nextTick(); await f.page.load()
  assert.deepEqual(f.applied, [])
})

test('快速切换选择取消旧预览，即使旧响应迟到也不能覆盖新预设', async t => {
  const first = deferred(), second = deferred(), f = await client(t, body => body.name === '甲' ? first.promise : second.promise)
  f.page.selected = key('甲'); await settle()
  const firstRequest = f.requests.find(request => request.options?.body)!
  f.page.selected = key('乙'); await settle()
  assert.equal(firstRequest.options.signal.aborted, true)
  const values = { ...presetValues(), [prefix + 'timbre_audio']: '乙.wav' }
  second.resolve(response({ values, warnings: ['参考文件请试听'] })); await settle()
  assert.equal(f.page.candidate.name, '乙')
  first.resolve(response({ values: presetValues(), warnings: [] })); await settle()
  assert.equal(f.page.candidate.name, '乙')
  await f.page.load()
  assert.deepEqual(f.applied, [values]); assert.equal(f.props.draft[prefix + 'timbre_audio'], '乙.wav')
})

test('选中新预设读取失败会清除旧候选，不能误载入上一次成功预览', async t => {
  const f = await client(t, body => body.name === '损坏'
    ? response({ message: '预设参考音频已缺失' }, false) : response({ values: presetValues(), warnings: [] }))
  f.page.selected = key('甲'); await settle(); assert.ok(f.page.candidate)
  f.page.selected = key('损坏'); await settle()
  assert.equal(f.page.candidate, null); assert.equal(f.page.previewLoading, false)
  await f.page.load(); assert.deepEqual(f.applied, [])
})

test('IndexTTS安装目录变化使旧目录候选失效，旧响应无法应用到新表单上下文', async t => {
  const old = deferred(), fresh = deferred(), f = await client(t, body => body.project_dir === 'D:\\IndexTTS' ? old.promise : fresh.promise)
  f.page.selected = key('甲'); await settle()
  const oldRequest = f.requests.find(request => request.options?.body)!
  f.props.draft[prefix + 'project_dir'] = 'E:\\IndexTTS'; await settle()
  assert.equal(oldRequest.options.signal.aborted, true)
  await f.page.load(); assert.deepEqual(f.applied, [])
  old.resolve(response({ values: presetValues(), warnings: [] })); await settle()
  assert.equal(f.page.candidate, null)
  fresh.resolve(response({ values: { ...presetValues(), [prefix + 'timbre_audio']: 'E:\\voice.wav' }, warnings: [] })); await settle()
  // 目录刷新可以清除当前选择；若保留选择，只有重新读取的新上下文候选可载入。
  if (f.page.candidate) {
    assert.equal(f.page.candidate.projectDir, 'E:\\IndexTTS')
    await f.page.load(); assert.equal(f.applied[0][prefix + 'timbre_audio'], 'E:\\voice.wav')
  }
  assert.equal(f.props.draft[prefix + 'project_dir'], 'E:\\IndexTTS')
})

test('卸载取消进行中的预览，迟到响应不产生载入或忙事件', async t => {
  const pending = deferred(), f = await client(t, () => pending.promise)
  f.page.selected = key('甲'); await settle()
  const request = f.requests.find(request => request.options?.body)!
  f.unmount(); assert.equal(request.options.signal.aborted, true)
  pending.resolve(response({ values: presetValues(), warnings: [] })); await settle()
  assert.deepEqual(f.applied, []); assert.deepEqual(f.busyEvents, [])
})

test('差异区用参数名称和参考文件展示当前值、预设值与未覆盖项，载入语义清楚', async t => {
  t.mock.method(globalThis, 'fetch', async () => response(listing()))
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(Presets, { draft: baseDraft(), fields, scopeLabel: '项目参数' }),
  }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name !== 'DubbingPresets') return
    const page = this.$.setupState
    page.entries = listing().presets; page.selected = key('甲')
    page.candidate = { key: key('甲'), name: '甲', projectDir: 'D:\\IndexTTS', values: presetValues(), warnings: ['参考文件请试听'] }
  } })
  const html = await renderToString(app)
  assert.match(html, /将修改 6 项/); assert.match(html, /当前值/); assert.match(html, /预设值/)
  assert.match(html, /音色参考文件/); assert.match(html, /声音\.wav/); assert.match(html, /情感参考文件/)
  assert.match(html, /本句原声/); assert.match(html, /自定义参考音频/)
  assert.match(html, /保持当前值/); assert.match(html, /单句请求超时/)
  assert.match(html, /选择预设查看变化/); assert.match(html, /载入到当前表单 → 保存项目参数/)
  assert.match(html, /参考文件请试听/)
})
