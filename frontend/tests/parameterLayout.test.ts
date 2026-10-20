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
import { parameterHelp, parameterPresentation } from '../src/utils/parameterPresentation.ts'

let server: ViteDevServer, ConfigurationForm: any, ProjectForm: any, Help: any, Label: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  ConfigurationForm = (await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
  ProjectForm = (await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
  Help = (await server.ssrLoadModule('/src/components/ParameterHelp.vue')).default
  Label = (await server.ssrLoadModule('/src/components/ParameterLabel.vue')).default
})
after(() => server.close())

const prefix = 'tts.indextts25.'
const path = (name: string) => prefix + name
const clone = (value: any) => JSON.parse(JSON.stringify(value))
const defaults: Record<string, any> = {
  'tts.backend': 'indextts25', [path('timbre_mode')]: 'preset', [path('timbre_audio')]: 'D:/参考音频/清晰音色.wav',
  [path('emo_mode')]: 'vector', [path('emo_audio')]: 'D:/参考音频/备用情感.wav',
  [path('emo_vector')]: [.05, 0, 0, 0, 0, 0, .1, .2], [path('emo_text')]: '备用文本说明保留，不随切换清空',
  [path('emo_random')]: true, [path('dynamic_reference_max_seconds')]: 5,
  [path('do_sample')]: false, [path('temperature')]: .8, [path('top_p')]: .8,
  [path('max_text_tokens_per_segment')]: 120, [path('num_beams')]: 3, 'assembly.versions': [],
}
const fields = [
  { path: 'tts.backend', label: '配音引擎', type: 'select', options: [{ label: 'IndexTTS2.5', value: 'indextts25' }] },
  { path: path('timbre_mode'), label: '音色参考来源', type: 'select', options: [{ label: '自定义音频', value: 'preset' }, { label: '逐句原声', value: 'dynamic' }] },
  { path: path('timbre_audio'), label: '音色参考文件', type: 'str' },
  { path: path('emo_mode'), label: '情感来源', type: 'select', options: [{ label: '情感向量', value: 'vector' }, { label: '情感描述', value: 'text' }] },
  { path: path('emo_vector'), label: '手动情感向量', type: 'vector' },
  { path: path('emo_text'), label: '情感描述', type: 'text' },
  { path: path('emo_random'), label: '情感随机采样', type: 'bool' },
  { path: path('emo_audio'), label: '情感参考文件', type: 'str' },
  { path: path('dynamic_reference_max_seconds'), label: '本句原声截取上限（秒）', type: 'float' },
  { path: path('do_sample'), label: '语音随机采样（do_sample）', type: 'bool' },
  { path: path('temperature'), label: '采样温度（temperature）', type: 'float' },
  { path: path('top_p'), label: '采样概率范围（top_p）', type: 'float' },
  { path: path('max_text_tokens_per_segment'), label: '内部分段 token 上限（max_text_tokens_per_segment）', type: 'int' },
  { path: path('num_beams'), label: '搜索分支数（num_beams）', type: 'int' },
].map(field => ({ ...field, help: `${field.label}：详细说明应可以完整读取，修改只进入草稿，保存后用于下一次生成。` }))
const field = (name: string) => fields.find(value => value.path === path(name))!
type Scope = 'global' | 'template' | 'project' | 'line'

async function render(t: any, scope: Scope, changes: Record<string, any> = {}, edit?: (page: any) => void) {
  const writes: string[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, init: any) => {
    if (init?.body) writes.push(init.body)
    return new Promise(() => {})
  })
  const isProject = scope === 'project' || scope === 'line'
  const Component = isProject ? ProjectForm : ConfigurationForm
  const props = isProject ? { uid: 'fixture-parameter-layout', stepKey: 'tts', lineId: scope === 'line' ? 7 : undefined }
    : { scope: scope === 'global' ? 'global' : 'series:fixture-parameter-layout', sections: ['tts'] }
  const values = { ...clone(defaults), ...clone(changes) }
  const controls: Record<string, any> = {}, helps: Record<string, any[]> = {}, labels: Record<string, any> = {}, buttons: any[] = [], badges: any[] = []
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(Component, props, isProject ? {
      'field-reference': ({ field: spec }: any) => spec.path === path('timbre_audio')
        ? h('small', { 'data-reference-context': spec.path }, '参考文件预览与说明不会改变参数值') : undefined,
    } : undefined),
  }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === Component.__name) {
      page = this.$.setupState
      if (isProject) {
        page.fields = fields; page.defaults = clone(defaults); page.draft = values
        page.overrides = Object.fromEntries(Object.keys(changes).map(key => [key, true]))
        page.parentSources = Object.fromEntries(fields.map(spec => [spec.path, { kind: scope === 'line' ? 'project' : 'global', frozen: true }]))
        page.sources = clone(page.parentSources)
        page.saved = JSON.stringify(values); page.loading = false
      } else {
        page.adopt({ name: '长标签布局测试', revision: 3, fingerprint: 'fixture', defaults: clone(defaults), overrides: clone(changes),
          parent_sources: Object.fromEntries(fields.map(spec => [spec.path, { kind: 'global' }])),
          sections: { tts: { label: '配音', fields } }, version_fields: [] })
        page.loading = false
      }
      edit?.(page)
    }
    if (this.$options.__name === 'ParamControl') controls[this.$props.field.path] = this
    if (this.$options.__name === 'ParameterHelp') (helps[this.$props.label] ??= []).push(this)
    if (this.$options.__name === 'ParameterLabel') labels[this.$props.field.path] = this
    if (this.$options.__name === 'ConfigurationSourceBadge') badges.push(this)
    if (this.$options.name === 'Button') buttons.push(this)
  } })
  const html = await renderToString(app)
  return { page, controls, helps, labels, buttons, badges, html, writes,
    values: () => isProject ? page.draft : page.values,
    disabled: (name: string) => isProject ? page.disabledField(field(name)) : page.fieldDisabled(field(name)),
    change: (key: string, value: any) => isProject ? page.editField(key, value) : page.change(key, value),
    inherit: (key: string) => isProject ? page.inheritField(key) : page.inherit(key) }
}

for (const scope of ['global', 'template', 'project', 'line'] as const) {
  test(`${scope}：长标题、来源与多种参数控件完整保留，额外说明不更改值`, async t => {
    const fixture = await render(t, scope)
    for (const spec of fields) {
      assert.ok(fixture.controls[spec.path], `${spec.label}应有独立控件`)
      assert.equal(fixture.controls[spec.path].$props.field.label, spec.label)
      assert.deepEqual(fixture.controls[spec.path].$props.modelValue, defaults[spec.path], `${spec.label}的值不能被布局变更重置`)
      const helps = fixture.helps[spec.label]
      assert.ok(helps?.length, `${spec.label}仍可查看说明`)
      assert.ok(helps.every(help => help.$props.help.includes(spec.help)))
    }
    assert.ok(fixture.badges.length >= fields.length, '每个参数仍展示设置来源')
    const title = fixture.labels[path('max_text_tokens_per_segment')]
    assert.ok(title, '长参数名使用共享标题组件')
    assert.equal(title.$.setupState.presentation.title, '内部分段 token 上限')
    assert.equal(title.$props.field.label, field('max_text_tokens_per_segment').label, '完整名称仍作为原始标题保留')
    assert.ok(fixture.helps[field('max_text_tokens_per_segment').label].every(help => help.$props.help.includes('参数名：max_text_tokens_per_segment')))
    assert.match(fixture.html, />内部分段 token 上限<\/span>/)
    assert.equal(fixture.labels[path('dynamic_reference_max_seconds')].$.setupState.presentation.title, '本句原声截取上限（秒）', '单位不能随英文技术后缀一起被收起')
    assert.match(fixture.html, /aria-label="情感向量：喜"/)
    assert.match(fixture.html, /aria-label="语音随机采样（do_sample）"/)
    assert.match(fixture.html, /D:\/参考音频\/清晰音色.wav/)
    if (scope === 'project' || scope === 'line') assert.match(fixture.html, /参考文件预览与说明不会改变参数值/)
    assert.equal(fixture.page.dirty, false)
    assert.equal(fixture.writes.length, 0)
  })

  test(`${scope}：灰显控件保留完整悬浮原因，来源切换不会清空备用参数`, async t => {
    const fixture = await render(t, scope)
    for (const [name, disabled] of [['emo_text', true], ['emo_vector', false], ['emo_random', false],
      ['dynamic_reference_max_seconds', true], ['temperature', true], ['top_p', true], ['timbre_audio', false]] as const) {
      assert.equal(fixture.controls[path(name)].$props.disabled, disabled, name)
      assert.equal(fixture.disabled(name), disabled, name)
      if (disabled) {
        const reason = indexTtsFieldInactiveReason(path(name), fixture.values())
        const summary = indexTtsFieldInactiveSummary(path(name), fixture.values())
        assert.ok(reason)
        assert.ok(fixture.helps[field(name).label].every(help => help.$props.help.includes(reason)), `${name}的详细停用原因仍可查看`)
        assert.ok(summary.includes('保留原值'))
        assert.ok(summary.length < reason.length, `${name}的行内提示简短，但不丢弃完整原因`)
        assert.ok(fixture.html.includes(`>${summary}</small>`), `${name}行内显示条件摘要`)
        assert.ok(fixture.html.includes(`title="${reason}"`), `${name}仍保留完整悬浮说明`)
        assert.ok(!fixture.html.includes(`>${reason}</small>`), `${name}不再将完整说明段落塞入行内`)
      }
    }
    fixture.change(path('emo_mode'), 'text')
    fixture.change(path('do_sample'), true)
    fixture.change(path('timbre_mode'), 'dynamic')
    assert.equal(fixture.disabled('emo_text'), false)
    assert.equal(fixture.disabled('emo_vector'), true)
    assert.equal(fixture.disabled('temperature'), false)
    assert.equal(fixture.disabled('dynamic_reference_max_seconds'), false)
    for (const name of ['emo_text', 'emo_vector', 'temperature', 'dynamic_reference_max_seconds', 'timbre_audio'])
      assert.deepEqual(fixture.values()[path(name)], defaults[path(name)], name)
    assert.equal(fixture.writes.length, 0)
  })

  test(`${scope}：查看长标签说明可键盘打开关闭，灰显项的撤销仍独立可用`, async t => {
    const fixture = await render(t, scope, { [path('dynamic_reference_max_seconds')]: 9 }, scope === 'global'
      ? page => page.change(path('dynamic_reference_max_seconds'), 11) : undefined)
    const before = clone(fixture.values()), dirty = fixture.page.dirty
    const longLabel = field('max_text_tokens_per_segment').label
    const helper = fixture.helps[longLabel].find(help => help.$props.showIcon !== false)
    assert.ok(helper, '长标签保留独立说明按钮')
    const state = helper.$.setupState
    state.focus()
    assert.equal(state.shown, true, '焦点进入即可查看说明')
    state.toggle()
    assert.equal(state.shown, true, '点击后可固定打开')
    state.close()
    assert.equal(state.shown, false, 'Escape关闭路径能关闭说明')
    state.updateHover(true)
    assert.equal(state.shown, true, '关闭之后再次进入可恢复悬浮说明')
    assert.deepEqual(clone(fixture.values()), before)
    assert.equal(fixture.page.dirty, dirty, '说明交互不改变草稿状态')
    const reset = fixture.buttons.find(button => String(button.$attrs['aria-label'] ?? '').startsWith(field('dynamic_reference_max_seconds').label)
      && !String(button.$attrs['aria-label']).endsWith('说明'))
    assert.ok(reset, '无效参数的来源重置仍独立存在')
    assert.equal(reset.$props.disabled, false)
    fixture.inherit(path('dynamic_reference_max_seconds'))
    assert.equal(fixture.values()[path('dynamic_reference_max_seconds')], scope === 'global' ? 9 : 5)
    assert.equal(fixture.disabled('dynamic_reference_max_seconds'), true)
    assert.equal(fixture.writes.length, 0)
  })
}

test('共享说明容器无说明时仍渲染输入，空说明不会留下无用的帮助按钮', async () => {
  let state: any
  const app = createSSRApp({ render: () => h(Help, { label: '没有说明的字段', help: '  ' }, {
    default: () => h('input', { 'aria-label': '独立参数输入', value: '保留值' }),
  }) })
  setup(app)
  app.mixin({ created() { if (this.$options.__name === 'ParameterHelp') state = this.$.setupState } })
  const html = await renderToString(app)
  assert.match(html, /aria-label="独立参数输入" value="保留值"/)
  assert.doesNotMatch(html, /没有说明的字段说明/)
  state.focus(); state.toggle()
  assert.equal(state.shown, false)
})

test('参数标题仅收起与字段键匹配的英文技术后缀，单位及不匹配的文本保持完整', () => {
  for (const label of ['采样温度（temperature）', '采样温度 (temperature)'])
    assert.deepEqual(parameterPresentation({ path: path('temperature'), label }), { title: '采样温度', key: 'temperature' })
  for (const spec of [
    { path: path('request_timeout'), label: '单句等待超时（秒）' },
    { path: path('emo_vector'), label: '情感向量（JSON）' },
    { path: path('temperature'), label: '采样温度（top_p）' },
    { path: path('temperature'), label: '采样温度（temperature）与建议值' },
    { label: '采样温度（temperature）' },
  ]) assert.deepEqual(parameterPresentation(spec), { title: spec.label, key: undefined })
  assert.equal(parameterHelp({ path: path('temperature'), label: '采样温度（temperature）', help: '调节随机程度' }),
    '调节随机程度\n\n参数名：temperature')
  assert.equal(parameterHelp({ path: path('request_timeout'), label: '单句等待超时（秒）', help: '请求兜底时限' }), '请求兜底时限')
})

test('共享参数标题保留完整名称和来源、恢复操作插槽，技术键只在说明中展开', async () => {
  const spec = field('max_text_tokens_per_segment')
  const app = createSSRApp({ render: () => h(Label, { field: spec }, {
    default: () => [h('span', { role: 'note', 'aria-label': '本项目单独设置' }, '项目设置'),
      h('button', { type: 'button', 'aria-label': `${spec.label}恢复继承` }, '↶')],
  }) })
  const html = await renderToString(app)
  assert.match(html, />内部分段 token 上限<\/span>/)
  assert.ok(html.includes(`title="${spec.label}"`), '鼠标悬停仍可读完整标签')
  assert.match(html, /role="note" aria-label="本项目单独设置">项目设置/)
  assert.ok(html.includes(`aria-label="${spec.label}恢复继承"`), '恢复操作的完整可访问名称仍保留')
  assert.ok(parameterHelp(spec).includes('参数名：max_text_tokens_per_segment'))
})

for (const scope of ['global', 'template', 'project'] as const) {
  test(`${scope}：自动修复作为配音时间排布普通设置，可编辑并恢复来源`, async t => {
    const key = 'tts.placement.auto_repair'
    const spec = { path: key, label: '自动修复配音时间冲突', type: 'bool', group: '配音时间排布', help: '先利用附近空位，仍放不下时有限缩短；保存后用于之后的运行。' }
    const source = { kind: scope === 'project' ? 'series' : 'global', name: scope === 'project' ? '测试模板' : '', frozen: scope === 'project' }
    const base = { 'tts.backend': 'indextts25', [key]: true }
    const result = await render(t, scope, {}, page => {
      if (scope === 'project') {
        page.fields = [spec]; page.defaults = base; page.draft = { ...base }; page.saved = JSON.stringify(base)
        page.sources = { [key]: source }; page.parentSources = { [key]: source }; page.overrides = {}
      } else {
        page.adopt({ name: '普通配音设置', revision: 3, fingerprint: 'fixture', defaults: base, overrides: {},
          parent_sources: { [key]: source }, sources: { [key]: source }, sections: { tts: { label: '配音', fields: [spec] } }, version_fields: [] })
        page.loading = false
      }
    })
    assert.match(result.html, /配音时间排布/)
    assert.equal(result.controls[key].$props.modelValue, true)
    assert.equal(result.controls[key].$props.disabled, false)
    assert.ok(result.badges.some(badge => badge.$props.source?.kind === source.kind), '来源与其他普通设置共用继承标记')
    result.change(key, false); assert.equal(result.values()[key], false)
    result.inherit(key); assert.equal(result.values()[key], true, '恢复操作使用当前范围的基础值')
    assert.deepEqual(result.writes, [], '编辑和恢复仅改变草稿，不启动配音或隐式保存')
  })
}
