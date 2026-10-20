import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'
import { indexTtsFieldInactiveReason, indexTtsFieldInactiveSummary } from '../src/utils/indexTtsSettings.ts'

let server: ViteDevServer, ConfigurationForm: any, ProjectForm: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  ConfigurationForm = (await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
  ProjectForm = (await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
})
after(() => server.close())

const prefix = 'tts.indextts25.'
const path = (name: string) => prefix + name
const clone = (value: any) => JSON.parse(JSON.stringify(value))
const defaults: Record<string, any> = {
  'tts.backend': 'indextts25', [path('timbre_mode')]: 'preset', [path('timbre_audio')]: 'voice.wav',
  [path('timbre_reference_line_id')]: 1, [path('emo_reference_line_id')]: 1,
  [path('emo_mode')]: 'text', [path('emo_audio')]: 'feeling.wav', [path('emo_weight')]: .4,
  [path('dynamic_reference_max_seconds')]: 5, [path('emo_vector')]: [.2, 0, 0, 0, 0, 0, 0, .1],
  [path('emo_text')]: '用平静的语气讲解', [path('emo_random')]: true, [path('do_sample')]: true,
  [path('temperature')]: .6, [path('top_p')]: .7, [path('top_k')]: 20,
  [path('num_beams')]: 3, [path('length_penalty')]: -.2, 'assembly.versions': [],
}
const specs = [
  ['timbre_mode', '音色参考来源', 'select'], ['timbre_audio', '音色参考文件', 'str'],
  ['timbre_reference_line_id', '音色参考句', 'int'],
  ['emo_mode', '情感来源', 'select'], ['emo_audio', '情感参考文件', 'str'],
  ['emo_reference_line_id', '情感参考句', 'int'],
  ['dynamic_reference_max_seconds', '本句原声截取上限（秒）', 'float'],
  ['emo_weight', '情感强度', 'float'], ['emo_vector', '手动情感向量', 'vector'],
  ['emo_text', '情感描述', 'text'], ['emo_random', '情感随机采样', 'bool'],
  ['do_sample', '随机采样', 'bool'], ['temperature', '采样温度', 'float'],
  ['top_p', '累计概率上限（top_p）', 'float'], ['top_k', '候选数量上限（top_k）', 'int'],
  ['num_beams', '搜索分支数', 'int'], ['length_penalty', '束搜索长度评分', 'float'],
]
const fields = [{ path: 'tts.backend', label: '配音引擎', type: 'select', options: [{ label: 'IndexTTS', value: 'indextts25' }] },
  ...specs.map(([name, label, type]) => ({ path: path(name), label, type,
    help: `${label}用于控制下一次配音生成，已有数值保留。`,
    options: name.endsWith('mode') ? ['preset', 'dynamic', 'stable', 'same', 'vector', 'text'].map(value => ({ label: value, value })) : undefined,
    depends_on: [['tts.backend', 'indextts25'],
      ...(name === 'timbre_audio' ? [[path('timbre_mode'), 'preset']] : name === 'emo_audio' ? [[path('emo_mode'), 'preset']]
        : name === 'emo_vector' ? [[path('emo_mode'), 'vector']] : name === 'emo_text' ? [[path('emo_mode'), 'text']] : [])],
  }))]
const field = (name: string) => fields.find(value => value.path === path(name))!
type Scope = 'global' | 'template' | 'project' | 'line'

async function render(t: any, scope: Scope, changes: Record<string, any> = {}, edit?: (page: any) => void) {
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, init: any) => {
    if (init?.body) writes.push(init.body)
    return new Promise(() => {})
  })
  let page: any
  const controls: Record<string, any> = {}, widgets: Record<string, any[]> = {}, helps: Record<string, any[]> = {}, references: Record<string, any> = {}, filePreviews: Record<string, any> = {}, buttons: any[] = [], presets: any[] = []
  const isProject = scope === 'project' || scope === 'line'
  const component = isProject ? ProjectForm : ConfigurationForm
  const props = isProject ? { uid: 'fixture-applicability', stepKey: 'tts', lineId: scope === 'line' ? 7 : undefined }
    : { scope: scope === 'global' ? 'global' : 'series:fixture-applicability', sections: ['tts'] }
  const values = { ...clone(defaults), ...clone(changes) }
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(component, props) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === component.__name) {
      page = this.$.setupState
      if (isProject) {
        page.fields = fields; page.defaults = clone(defaults); page.draft = values
        page.overrides = Object.fromEntries(Object.keys(changes).map(key => [key, true]))
        page.parentSources = Object.fromEntries(fields.map(value => [value.path, { kind: scope === 'line' ? 'project' : 'global', frozen: true }]))
        page.saved = JSON.stringify(values); page.loading = false
      } else {
        page.adopt({ name: '测试配置', revision: 3, fingerprint: 'fixture', defaults: clone(defaults), overrides: clone(changes),
          parent_sources: Object.fromEntries(fields.map(value => [value.path, { kind: 'global' }])),
          sections: { tts: { label: '配音', fields } }, version_fields: [] })
        page.loading = false
      }
      edit?.(page)
    }
    if (this.$options.__name === 'ParamControl') controls[this.$props.field.path] = this
    if (this.$options.__name === 'IndexTtsReferenceSentence') references[this.$parent!.$props.field.path] = this
    if (this.$options.__name === 'ReferenceFilePreview') filePreviews[this.$parent!.$props.field.path] = this
    if (this.$options.__name === 'ParameterHelp') (helps[this.$props.label] ??= []).push(this)
    if (this.$options.__name === 'DubbingPresets') presets.push(this)
    if (this.$options.name === 'Button') buttons.push(this)
    if (['Input', 'InputNumber', 'Select', 'Switch', 'Slider'].includes(this.$options.name ?? '')) {
      let owner = this.$parent
      while (owner && owner.$options.__name !== 'ParamControl') owner = owner.$parent
      if (owner) (widgets[owner.$props.field.path] ??= []).push(this)
    }
  } })
  const html = await renderToString(app)
  return { page, controls, widgets, helps, references, filePreviews, buttons, presets, html, writes,
    values: () => isProject ? page.draft : page.values,
    change: (key: string, value: any) => isProject ? page.editField(key, value) : page.change(key, value),
    disabled: (name: string) => isProject ? page.disabledField(field(name)) : page.fieldDisabled(field(name)),
    inherit: (key: string) => isProject ? page.inheritField(key) : page.inherit(key) }
}

for (const scope of ['global', 'template', 'project', 'line'] as const) test(`${scope}：音色与情感文件均可只读试听当前表单路径，不受来源禁用影响`, async t => {
  const customPath = 'D:\\参考音频\\未保存 音色.wav'
  const fixture = await render(t, scope, { [path('timbre_mode')]: 'stable', [path('timbre_audio')]: customPath,
    [path('emo_mode')]: 'text', [path('emo_audio')]: 'D:\\参考音频\\情感.wav' })
  for (const name of ['timbre_audio', 'emo_audio']) {
    const key = path(name)
    assert.equal(fixture.disabled(name), true, '不适用来源仍禁用编辑')
    assert.equal(fixture.filePreviews[key].$props.path, fixture.values()[key], '试听使用表单当前值，无需先保存')
    assert.equal(fixture.filePreviews[key].$.setupState.disabled, false, '读取保留文件不改配置，可试听')
  }
  assert.equal(fixture.filePreviews['tts.indextts25.project_dir'], undefined)
  assert.deepEqual(fixture.writes, [], '显示试听入口不写入设置或启动合成')
})

function assertDisabled(fixture: Awaited<ReturnType<typeof render>>, name: string, disabled: boolean) {
  const key = path(name)
  assert.ok(fixture.controls[key], `${name}应保持可见`)
  assert.equal(fixture.controls[key].$props.disabled, disabled, `${name}的参数控件`)
  assert.ok(fixture.widgets[key]?.length, `${name}应渲染实际输入控件`)
  for (const widget of fixture.widgets[key]) assert.equal(widget.$props.disabled, disabled, `${name}的${widget.$options.name}`)
}

for (const scope of ['global', 'template', 'project', 'line'] as const) {
  test(`${scope}：指定原声参考句只在各自来源启用，同句才停用情感强度`, async t => {
    const fixture = await render(t, scope, { [path('timbre_mode')]: 'stable', [path('emo_mode')]: 'stable' })
    assertDisabled(fixture, 'timbre_reference_line_id', false)
    assertDisabled(fixture, 'emo_reference_line_id', false)
    assertDisabled(fixture, 'emo_weight', true)
    assertDisabled(fixture, 'dynamic_reference_max_seconds', true)
    assert.match(indexTtsFieldInactiveReason(path('emo_weight'), fixture.values()), /同一句原声/)
    fixture.change(path('emo_reference_line_id'), 7)
    assert.equal(fixture.disabled('emo_weight'), false, '不同参考句有独立情感，可调节强度')
    fixture.change(path('emo_mode'), 'same')
    assert.equal(fixture.disabled('timbre_reference_line_id'), false)
    assert.equal(fixture.disabled('emo_reference_line_id'), true)
    assert.equal(fixture.disabled('emo_weight'), true)
    assert.match(indexTtsFieldInactiveReason(path('emo_reference_line_id'), fixture.values()), /沿用音色参考/)
    assert.equal(fixture.values()[path('emo_reference_line_id')], 7, '停用后保留备用句号')
    fixture.change(path('timbre_mode'), 'preset')
    assert.equal(fixture.disabled('timbre_reference_line_id'), true)
    assert.equal(fixture.writes.length, 0)
  })

  test(`${scope}：自定义参考时逐句上限停用，向量和文本保留可见，悬浮与行内说明清楚`, async t => {
    const fixture = await render(t, scope)
    assertDisabled(fixture, 'dynamic_reference_max_seconds', true)
    assertDisabled(fixture, 'emo_vector', true)
    assertDisabled(fixture, 'emo_text', false)
    assertDisabled(fixture, 'emo_random', false)
    assertDisabled(fixture, 'timbre_audio', false)
    assertDisabled(fixture, 'emo_audio', true)
    const reason = indexTtsFieldInactiveReason(path('dynamic_reference_max_seconds'), fixture.values())
    assert.match(reason, /逐句|动态|原声/)
    assert.ok(fixture.html.includes(indexTtsFieldInactiveSummary(path('dynamic_reference_max_seconds'), fixture.values())), '行内显示短提示')
    assert.ok(fixture.html.includes(`title="${reason}"`), '完整停用原因保留在提示中')
    const help = fixture.helps[field('dynamic_reference_max_seconds').label]
    assert.ok(help?.length)
    assert.ok(help.every(value => value.$props.help.includes(reason)), '标签与控件悬浮说明均含当前停用原因')
    assert.match(fixture.html, /本句原声截取上限（秒）说明/)
    assert.equal(fixture.page.dirty, false)
    assert.equal(fixture.writes.length, 0)
    fixture.change(path('timbre_mode'), 'dynamic')
    assert.equal(fixture.disabled('dynamic_reference_max_seconds'), false, '切换来源立即恢复适用性')
    assert.equal(fixture.values()[path('dynamic_reference_max_seconds')], 5)
    fixture.change(path('timbre_mode'), 'preset')
    fixture.change(path('emo_mode'), 'dynamic')
    assert.equal(fixture.disabled('dynamic_reference_max_seconds'), false, '独立逐句情感参考也使用上限')
    assert.equal(fixture.disabled('emo_text'), true)
    assert.equal(fixture.values()[path('emo_text')], defaults[path('emo_text')])
    fixture.change(path('emo_mode'), 'text')
    assert.equal(fixture.disabled('dynamic_reference_max_seconds'), true)
    assert.equal(fixture.disabled('emo_text'), false)
    assert.equal(fixture.writes.length, 0)
  })

  test(`${scope}：沿用逐句音色情感仍使用时长上限，同源权重及情感随机真实禁用`, async t => {
    const fixture = await render(t, scope, { [path('timbre_mode')]: 'dynamic', [path('emo_mode')]: 'same' })
    assertDisabled(fixture, 'dynamic_reference_max_seconds', false)
    for (const name of ['timbre_audio', 'emo_audio', 'emo_weight', 'emo_random', 'emo_vector', 'emo_text']) assertDisabled(fixture, name, true)
    const reason = indexTtsFieldInactiveReason(path('emo_weight'), fixture.values())
    assert.ok(fixture.html.includes(reason))
    assert.match(fixture.html, /aria-label="情感随机采样"[^>]*aria-disabled="true"|aria-disabled="true"[^>]*aria-label="情感随机采样"/)
    fixture.change(path('emo_mode'), 'text')
    assert.equal(fixture.disabled('emo_weight'), false)
    assert.equal(fixture.disabled('emo_random'), false)
    assert.equal(fixture.values()[path('emo_weight')], .4)
    assert.equal(fixture.values()[path('emo_random')], true)
    assert.equal(fixture.writes.length, 0)
  })

  test(`${scope}：向量随机保持独立可编辑，语音采样和束搜索只停用各自的依赖项`, async t => {
    const fixture = await render(t, scope, { [path('emo_mode')]: 'vector', [path('emo_vector')]: Array(8).fill(0),
      [path('do_sample')]: false, [path('num_beams')]: 1 })
    assertDisabled(fixture, 'emo_vector', false)
    assertDisabled(fixture, 'emo_text', true)
    assertDisabled(fixture, 'emo_random', false)
    for (const name of ['temperature', 'top_p', 'top_k', 'length_penalty']) assertDisabled(fixture, name, true)
    assertDisabled(fixture, 'emo_weight', false)
    fixture.change(path('emo_vector'), [.3, 0, 0, 0, 0, 0, 0, 0])
    assert.equal(fixture.disabled('emo_random'), false)
    fixture.change(path('emo_weight'), 0)
    assert.equal(fixture.disabled('emo_random'), false, '零权重不误判随机开关为不可编辑')
    fixture.change(path('emo_weight'), .4)
    assert.equal(fixture.disabled('emo_random'), false)
    fixture.change(path('do_sample'), true)
    for (const name of ['temperature', 'top_p', 'top_k']) {
      assert.equal(fixture.disabled(name), false)
      assert.equal(fixture.values()[path(name)], defaults[path(name)])
    }
    assert.equal(fixture.disabled('length_penalty'), true, '采样开关不改变搜索分支依赖')
    fixture.change(path('num_beams'), 3)
    assert.equal(fixture.disabled('length_penalty'), false)
    assert.equal(fixture.values()[path('length_penalty')], -.2)
    assert.equal(fixture.writes.length, 0)
  })

  test(`${scope}：模式停用不会锁住撤销或恢复继承，操作仅更新草稿`, async t => {
    const fixture = await render(t, scope, { [path('dynamic_reference_max_seconds')]: 9 }, scope === 'global'
      ? page => page.change(path('dynamic_reference_max_seconds'), 11) : undefined)
    assertDisabled(fixture, 'dynamic_reference_max_seconds', true)
    const reset = fixture.buttons.find(button => String(button.$attrs['aria-label'] ?? '').startsWith(field('dynamic_reference_max_seconds').label))
    assert.ok(reset, '备用值依然可以撤销或恢复继承')
    assert.equal(reset.$props.disabled, false)
    fixture.inherit(path('dynamic_reference_max_seconds'))
    assert.equal(fixture.values()[path('dynamic_reference_max_seconds')], scope === 'global' ? 9 : 5)
    assert.equal(fixture.disabled('dynamic_reference_max_seconds'), true)
    assert.equal(fixture.writes.length, 0)
  })
}

for (const scope of ['global', 'template', 'project'] as const) test(`${scope}：预设载入保留当前不生效的备用值，切换模式后即时可用且不保存`, async t => {
  const fixture = await render(t, scope)
  const child = fixture.presets[0].$.setupState
  const preset = { [path('timbre_mode')]: 'preset', [path('emo_mode')]: 'text', [path('dynamic_reference_max_seconds')]: 10,
    [path('emo_vector')]: [.5, .1, 0, 0, 0, 0, 0, 0], [path('emo_text')]: '预设中的说明',
    [path('emo_weight')]: 0, [path('emo_random')]: true }
  const before = clone(fixture.values()), calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
    calls.push([String(url), init?.method ?? 'GET'])
    return new Response(JSON.stringify({ values: preset, warnings: [] }))
  })
  child.selected = JSON.stringify(['local', '备用参数完整预设'])
  await child.readPreview()
  assert.equal(child.candidateReady, true)
  assert.deepEqual(clone(fixture.values()), before, '读取预设不改变当前草稿')
  child.load()
  for (const [key, value] of Object.entries(preset)) assert.deepEqual(fixture.values()[key], value, `${key}应从预设完整载入`)
  assert.equal(fixture.disabled('dynamic_reference_max_seconds'), true)
  assert.equal(fixture.disabled('emo_vector'), true)
  assert.equal(fixture.disabled('emo_random'), false, '文本情感中零权重也保留随机开关')
  fixture.change(path('timbre_mode'), 'dynamic')
  fixture.change(path('emo_mode'), 'vector')
  fixture.change(path('emo_weight'), .4)
  assert.equal(fixture.disabled('dynamic_reference_max_seconds'), false)
  assert.equal(fixture.values()[path('dynamic_reference_max_seconds')], 10)
  assert.equal(fixture.disabled('emo_vector'), false)
  assert.deepEqual(fixture.values()[path('emo_vector')], preset[path('emo_vector')])
  assert.equal(fixture.disabled('emo_random'), false)
  assert.deepEqual(calls, [['/api/workbench-presets/load', 'POST']], '只有预设读取，没有实际配置保存或合成')
})

test('单句本句原声的禁用选择器显示当前句与跟随标记，打开不改草稿或参数来源', async t => {
  const fixture = await render(t, 'line', { [path('timbre_mode')]: 'dynamic', [path('emo_mode')]: 'dynamic',
    [path('timbre_reference_line_id')]: 3, [path('emo_reference_line_id')]: 4 })
  for (const source of ['timbre', 'emo']) {
    const key = path(source + '_reference_line_id')
    assert.equal(fixture.controls[key].$props.modelValue, 7)
    assert.equal(fixture.controls[key].$props.referenceFollowsCurrent, true)
    assert.equal(fixture.references[key].$props.modelValue, 7)
    assert.equal(fixture.references[key].$props.followCurrent, true)
    assert.equal(fixture.references[key].$props.disabled, true)
    assert.equal(fixture.page.fieldValue(key), 7)
    assert.equal(fixture.page.edited[key], undefined, '派生显示不能伪装为本句覆盖')
    assert.equal(fixture.page.resetKeys.has(key), false)
  }
  assert.equal(fixture.values()[path('timbre_reference_line_id')], 3)
  assert.equal(fixture.values()[path('emo_reference_line_id')], 4)
  assert.equal((fixture.html.match(/class="reference-follow-label"/g) ?? []).length, 2)
  assert.equal(fixture.page.dirty, false)
  assert.deepEqual(fixture.writes, [])
})

test('用户从本句切到指定句沿用当前句，再手选与其他模式往返保留手选句号', async t => {
  const fixture = await render(t, 'line', { [path('timbre_mode')]: 'dynamic', [path('emo_mode')]: 'dynamic',
    [path('timbre_reference_line_id')]: 3, [path('emo_reference_line_id')]: 4 })
  for (const source of ['timbre', 'emo']) {
    const key = path(source + '_reference_line_id'), mode = path(source + '_mode')
    fixture.change(mode, 'stable')
    assert.equal(fixture.values()[key], 7)
    assert.equal(fixture.page.fieldValue(key), 7)
    assert.equal(fixture.disabled(source + '_reference_line_id'), false)
    fixture.change(key, 11)
    fixture.change(mode, 'preset')
    assert.equal(fixture.values()[key], 11)
    fixture.change(mode, 'stable')
    assert.equal(fixture.values()[key], 11, '离开指定模式再返回不重置为第一句')
  }
  assert.equal(fixture.page.dirty, true)
  fixture.page.discard()
  assert.equal(fixture.page.dirty, false)
  assert.equal(fixture.values()[path('timbre_reference_line_id')], 3)
  assert.equal(fixture.values()[path('emo_reference_line_id')], 4)
  assert.equal(fixture.page.fieldValue(path('timbre_reference_line_id')), 7)
  assert.deepEqual(fixture.writes, [])
})

test('重新载入或恢复来源不隐式携带参考句，完整预设以自带句号为准', async t => {
  const fixture = await render(t, 'line', { [path('timbre_mode')]: 'dynamic', [path('emo_mode')]: 'dynamic',
    [path('timbre_reference_line_id')]: 3, [path('emo_reference_line_id')]: 4 })
  fixture.page.applyPreset({ [path('timbre_mode')]: 'stable', [path('timbre_reference_line_id')]: 11,
    [path('emo_mode')]: 'stable', [path('emo_reference_line_id')]: 12 })
  assert.equal(fixture.values()[path('timbre_reference_line_id')], 11, '预设参考句不会被当前第7句覆盖')
  assert.equal(fixture.values()[path('emo_reference_line_id')], 12)
  fixture.page.discard()
  const values = { ...clone(defaults), [path('timbre_mode')]: 'dynamic', [path('emo_mode')]: 'dynamic',
    [path('timbre_reference_line_id')]: 9, [path('emo_reference_line_id')]: 10 }
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ defaults: clone(defaults),
    overridden: {}, steps: { tts: { fields, values } } })))
  await fixture.page.load()
  assert.equal(fixture.page.dirty, false)
  assert.equal(fixture.values()[path('timbre_reference_line_id')], 9)
  assert.equal(fixture.page.fieldValue(path('timbre_reference_line_id')), 7)
  fixture.page.defaults[path('timbre_mode')] = 'stable'
  fixture.inherit(path('timbre_mode'))
  assert.equal(fixture.values()[path('timbre_reference_line_id')], 9, '恢复模式来源不等同用户切换参考模式')
  assert.deepEqual(fixture.writes, [])
})

for (const scope of ['global', 'template', 'project'] as const) test(`${scope}：没有当前句时不显示跟随也不携带虚构句号`, async t => {
  const fixture = await render(t, scope, { [path('timbre_mode')]: 'dynamic', [path('emo_mode')]: 'dynamic',
    [path('timbre_reference_line_id')]: 3, [path('emo_reference_line_id')]: 4 })
  for (const source of ['timbre', 'emo']) {
    const key = path(source + '_reference_line_id')
    assert.equal(fixture.controls[key].$props.modelValue, source === 'timbre' ? 3 : 4)
    assert.equal(!!fixture.controls[key].$props.referenceFollowsCurrent, false)
    fixture.change(path(source + '_mode'), 'stable')
    assert.equal(fixture.values()[key], source === 'timbre' ? 3 : 4)
  }
  assert.doesNotMatch(fixture.html, /class="reference-follow-label"/)
  assert.deepEqual(fixture.writes, [])
})
