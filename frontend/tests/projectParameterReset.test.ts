import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'
import { INDEX_TTS_PRONUNCIATION_PATH } from '../src/utils/dubbingPreset.ts'

let server: ViteDevServer, Form: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
})
after(() => server.close())

const prefix = 'tts.indextts25.', temperature = prefix + 'temperature', url = prefix + 'url'
const reference = prefix + 'timbre_reference_line_id', reading = INDEX_TTS_PRONUNCIATION_PATH
const fields = [
  { path: temperature, label: '采样温度', type: 'float' },
  { path: url, label: '服务地址', type: 'str' },
  { path: reference, label: '音色参考句', type: 'int' },
  { path: reading, label: '朗读文本', type: 'text', group: '朗读内容' },
]
const defaults = { 'tts.backend': 'indextts25', [prefix + 'timbre_mode']: 'stable',
  [temperature]: .8, [url]: 'http://localhost:7860', [reference]: 1, [reading]: '' }
const clone = (value: any) => JSON.parse(JSON.stringify(value))

async function render(t: any, options: { lineId?: number; changes?: Record<string, any>; disabled?: boolean; dynamic?: boolean } = {}) {
  const writes: any[] = [], controls: Record<string, any> = {}
  t.mock.method(globalThis, 'fetch', (_url: any, init: any) => {
    if (init?.body) writes.push(init.body)
    return new Promise(() => {})
  })
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Form,
    { uid: 'parameter-reset-fixture', stepKey: 'tts', lineId: options.lineId, disabled: options.disabled }) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ProjectParameterForm') {
      page = this.$.setupState
      page.fields = fields; page.defaults = clone(defaults)
      page.draft = { ...clone(defaults), ...options.changes,
        ...(options.dynamic ? { [prefix + 'timbre_mode']: 'dynamic' } : {}) }
      page.parentSources = Object.fromEntries(fields.map(field => [field.path,
        { kind: options.lineId == null ? 'series' : 'project', frozen: field.path !== url, name: '测试系列' }]))
      page.sources = clone(page.parentSources)
      page.overrides = Object.fromEntries(Object.keys(options.changes || {}).map(path => [path, true]))
      page.lineText = '当前字幕译文。'; page.saved = JSON.stringify(page.draft); page.loading = false
    }
    if (this.$options.__name === 'ParameterResetButton') controls[this.$props.label] = this
  } })
  const html = await renderToString(app)
  return { page, controls, html, writes }
}

test('步骤与本句的每项恢复入口始终存在；已继承项禁用且打开不会制造覆盖或写入', async t => {
  for (const lineId of [undefined, 7]) await t.test(lineId == null ? '步骤' : '本句', async t => {
    const { page, controls, html, writes } = await render(t, { lineId })
    assert.deepEqual(Object.keys(controls).sort(), fields.map(field => field.label).sort())
    for (const field of fields) {
      assert.equal(controls[field.label].$props.disabled, true, field.label)
      assert.equal(page.fieldResetDisabled(field.path), true)
    }
    assert.equal(page.dirty, false)
    assert.deepEqual(page.overrides, {})
    assert.equal(page.resetKeys.size, 0)
    assert.deepEqual(writes, [])
    assert.match(html, /aria-label="采样温度恢复/)
  })
})

test('恢复使用各自基础值，只清除目标覆盖并等待保存，不改其他句或参数', async t => {
  const { page, controls, writes } = await render(t, { changes: { [temperature]: .5, [url]: 'http://localhost:9000' } })
  assert.equal(controls['采样温度'].$props.disabled, false)
  assert.equal(controls['采样温度'].$props.action, '恢复项目基础值')
  assert.match(controls['采样温度'].$props.help, /保留创建或上次应用时的值/)
  assert.equal(controls['服务地址'].$props.action, '恢复服务设置')
  controls['采样温度'].$emit('reset')
  assert.equal(page.draft[temperature], .8)
  assert.equal(page.draft[url], 'http://localhost:9000')
  assert.equal(page.changed(temperature), false)
  assert.equal(page.changed(url), true)
  assert.equal(page.fieldResetDisabled(temperature), true)
  assert.deepEqual([...page.resetKeys], [temperature])
  assert.equal(page.dirty, true)
  assert.deepEqual(writes, [])
})

test('本句恢复项目参数，朗读文本单独恢复跟随译文，并移除组标题重复入口', async t => {
  const { page, controls, html, writes } = await render(t, { lineId: 7,
    changes: { [temperature]: .5, [reading]: '自定义朗读。' } })
  assert.equal(controls['采样温度'].$props.action, '恢复项目设置')
  assert.equal(controls['朗读文本'].$props.action, '恢复跟随译文')
  assert.equal(controls['朗读文本'].$props.disabled, false)
  assert.match(controls['朗读文本'].$props.help, /保存后继续跟随字幕修改/)
  controls['朗读文本'].$emit('reset')
  assert.equal(page.draft[reading], '')
  assert.equal(page.fieldValue(reading), '当前字幕译文。')
  assert.equal(page.draft[temperature], .5)
  assert.equal(page.fieldResetDisabled(reading), true)
  assert.equal((html.match(/aria-label="朗读文本恢复跟随译文"/g) || []).length, 1)
  assert.deepEqual(writes, [])
})

test('本句原声的参考句是派生显示，入口保留但禁用，保留未生效的指定句', async t => {
  const { page, controls, writes } = await render(t, { lineId: 7, dynamic: true, changes: { [reference]: 3 } })
  assert.equal(page.fieldValue(reference), 7)
  assert.equal(page.draft[reference], 3)
  assert.equal(controls['音色参考句'].$props.disabled, true)
  assert.match(controls['音色参考句'].$props.help, /自动使用当前句子/)
  assert.equal(page.dirty, false)
  assert.deepEqual(writes, [])
})

test('锁定表单仍展示各项恢复入口但不接受修改', async t => {
  const { page, controls, writes } = await render(t, { disabled: true, changes: { [temperature]: .5 } })
  assert.equal(controls['采样温度'].$props.disabled, true)
  page.inheritField(temperature)
  assert.equal(page.draft[temperature], .5)
  assert.equal(page.resetKeys.size, 0)
  assert.equal(page.dirty, false)
  assert.deepEqual(writes, [])
})
