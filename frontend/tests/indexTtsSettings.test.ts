import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider } from 'naive-ui'
import { indexTtsFieldHelp, indexTtsFieldInactiveReason } from '../src/utils/indexTtsSettings.ts'

let server: ViteDevServer, Form: any, ProjectForm: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
  ProjectForm = (await server.ssrLoadModule('/src/components/ProjectParameterForm.vue')).default
})
after(() => server.close())

const prefix = 'tts.indextts25.'
const defaults: Record<string, any> = {
  'tts.backend': 'indextts25', [prefix + 'timbre_mode']: 'preset', [prefix + 'timbre_audio']: 'voice.wav',
  [prefix + 'emo_audio']: 'feeling.wav',
  [prefix + 'emo_mode']: 'text', [prefix + 'emo_weight']: .4, [prefix + 'emo_random']: true,
  [prefix + 'duration_factor']: .85, [prefix + 'do_sample']: true, [prefix + 'top_p']: .7,
  [prefix + 'top_k']: 20, [prefix + 'temperature']: .6, [prefix + 'num_beams']: 3, [prefix + 'length_penalty']: -.2,
  [prefix + 'emo_vector']: [.2, 0, 0, 0, 0, 0, 0, .1], [prefix + 'emo_text']: '用平静的语气解释',
  'assembly.versions': [],
}
const types: Record<string, string> = { timbre_mode: 'str', timbre_audio: 'str', emo_mode: 'str', emo_audio: 'str', emo_weight: 'float',
  emo_random: 'bool', duration_factor: 'float', do_sample: 'bool', top_p: 'float', top_k: 'int', temperature: 'float', num_beams: 'int', length_penalty: 'float', emo_vector: 'vector', emo_text: 'text' }
const fields = Object.entries(types).map(([name, type]) => ({ path: prefix + name, label: name, type,
  help: `${name}的用途与推荐起点`, depends_on: [['tts.backend', 'indextts25'],
    ...(name === 'emo_vector' ? [[prefix + 'emo_mode', 'vector']] : name === 'emo_text' ? [[prefix + 'emo_mode', 'text']]
      : name === 'timbre_audio' ? [[prefix + 'timbre_mode', 'preset']] : name === 'emo_audio' ? [[prefix + 'emo_mode', 'preset']] : [])] }))
function state(overrides: Record<string, any> = {}) {
  return { name: '全局默认', revision: 3, fingerprint: 'fixture', archived: false, defaults, overrides,
    parent_sources: Object.fromEntries(Object.keys(defaults).map(path => [path, { kind: 'global' }])),
    sections: { tts: { label: '配音', fields } }, version_fields: [] }
}
async function render(t: any, edit?: (page: any) => void, overrides: Record<string, any> = {}, scope = 'global') {
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => {
    if (options?.body) writes.push(options.body)
    return new Promise(() => {})
  })
  let page: any
  const controls: Record<string, any> = {}, helps: Record<string, any> = {}, presets: any[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(Form, { scope, sections: ['tts'] }) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ConfigurationForm') {
      page = this.$.setupState; page.adopt(state(overrides)); page.loading = false; edit?.(page)
    }
    if (this.$options.__name === 'ParamControl') controls[this.$props.field.path] = this
    if (this.$options.__name === 'ParameterHelp') helps[this.$props.label] = this
    if (this.$options.__name === 'DubbingPresets') presets.push(this)
  } })
  const html = await renderToString(app)
  return { page, html, writes, controls, helps, presets }
}

test('打开设置保留已有情感、时长与随机值；均衡和速度快捷方案已移除', async t => {
  const { page, html, writes, helps } = await render(t)
  assert.equal(page.dirty, false)
  assert.equal(page.values[prefix + 'emo_weight'], .4)
  assert.equal(page.values[prefix + 'duration_factor'], .85)
  assert.equal(page.values[prefix + 'emo_random'], true)
  assert.doesNotMatch(html, /均衡推荐|速度优先|选择建议/)
  assert.equal(page.applyIndexProfile, undefined)
  assert.match(html, /从预设加载|音色预设|加载预设/)
  assert.equal(Object.keys(helps).length, page.visibleFields({ fields }).length)
  assert.ok(helps.do_sample.$props.help.includes('情感随机采样是独立开关'))
  assert.equal(writes.length, 0)
})

test('关闭采样实际禁用三个采样控件，数值保留且重新开启后继续使用', async t => {
  const { page, controls, writes } = await render(t, page => page.change(prefix + 'do_sample', false))
  for (const name of ['top_p', 'top_k', 'temperature']) {
    assert.equal(controls[prefix + name].$props.disabled, true)
    assert.match(page.fieldHelp(fields.find(field => field.path === prefix + name)), /当前值保留/)
    assert.equal(page.values[prefix + name], defaults[prefix + name])
  }
  assert.equal(controls[prefix + 'num_beams'].$props.disabled, false)
  assert.equal(controls[prefix + 'emo_random'].$props.disabled, false)
  page.change(prefix + 'do_sample', true)
  for (const name of ['top_p', 'top_k', 'temperature']) {
    assert.equal(page.fieldDisabled(fields.find(field => field.path === prefix + name)), false)
    assert.equal(page.values[prefix + name], defaults[prefix + name])
  }
  assert.equal(writes.length, 0)
})

test('单搜索分支时长度惩罚控件停用，恢复多分支后继续使用原值', async t => {
  const { page, controls, writes } = await render(t, page => page.change(prefix + 'num_beams', 1))
  assert.equal(controls[prefix + 'length_penalty'].$props.disabled, true)
  assert.match(page.fieldHelp(fields.find(field => field.path === prefix + 'length_penalty')), /没有束搜索的长度评分/)
  assert.equal(page.values[prefix + 'length_penalty'], -.2)
  page.change(prefix + 'num_beams', 3)
  assert.equal(page.fieldDisabled(fields.find(field => field.path === prefix + 'length_penalty')), false)
  assert.equal(page.values[prefix + 'length_penalty'], -.2)
  assert.equal(writes.length, 0)
})

test('同音色和双动态参考显示权重无效；切换模式不改权重和情感随机的备用值', async t => {
  const { page, controls, html } = await render(t, page => {
    page.change(prefix + 'timbre_mode', 'dynamic'); page.change(prefix + 'emo_mode', 'dynamic')
  })
  assert.match(html, /参考来源相同，无需调节/)
  assert.match(page.fieldHelp(fields.find(field => field.path === prefix + 'emo_weight')), /同一段原声/)
  assert.equal(controls[prefix + 'emo_weight'].$props.disabled, true)
  assert.equal(controls[prefix + 'emo_random'].$props.disabled, true)
  const switchTag = html.match(/<[a-z](?=[^>]*\brole="switch")(?=[^>]*\baria-label="emo_random")[^>]*>/)?.[0]
  assert.ok(switchTag)
  assert.match(switchTag, /aria-disabled="true"/, '停用情感随机开关向辅助技术明确标注不可用')
  page.change(prefix + 'emo_mode', 'same')
  assert.match(page.fieldHelp(fields.find(field => field.path === prefix + 'emo_weight')), /强度不参与合成/)
  page.change(prefix + 'emo_mode', 'text')
  assert.equal(page.fieldDisabled(fields.find(field => field.path === prefix + 'emo_weight')), false)
  assert.equal(page.fieldDisabled(fields.find(field => field.path === prefix + 'emo_random')), false)
  assert.equal(page.values[prefix + 'emo_weight'], .4)
  assert.equal(page.values[prefix + 'emo_random'], true)
})

test('全局和模板保留不适用的向量和文本控件并禁用，切回时保留已有覆盖和草稿', async t => {
  const vector = [.35, .1, 0, 0, 0, 0, 0, .2], description = '保留原有情感描述'
  const overrides = { [prefix + 'timbre_mode']: 'dynamic', [prefix + 'emo_mode']: 'dynamic', [prefix + 'emo_vector']: vector, [prefix + 'emo_text']: description }
  for (const scope of ['global', 'series:fixture']) {
    const dynamic = await render(t, undefined, overrides, scope)
    assert.equal(dynamic.controls[prefix + 'emo_vector'].$props.disabled, true)
    assert.equal(dynamic.controls[prefix + 'emo_text'].$props.disabled, true)
    assert.match(dynamic.html, /aria-label="情感向量：|aria-label="emo_text"/)
    assert.ok(dynamic.controls[prefix + 'timbre_audio'], '参考文件依赖仍使用灰显控件')
    assert.equal(dynamic.controls[prefix + 'timbre_audio'].$props.disabled, true)
    assert.equal(dynamic.controls[prefix + 'emo_audio'].$props.disabled, true)
    assert.match(dynamic.page.fieldHelp(fields.find(field => field.path === prefix + 'timbre_audio')), /路径保留，切换为自定义音色参考后生效/)
    assert.match(dynamic.page.fieldHelp(fields.find(field => field.path === prefix + 'emo_audio')), /路径保留，切换为自定义情感音频后生效/)
    assert.equal(dynamic.page.dirty, false)
    const vectorMode = await render(t, page => page.change(prefix + 'emo_mode', 'vector'), overrides, scope)
    assert.ok(vectorMode.controls[prefix + 'emo_vector'])
    assert.equal(vectorMode.controls[prefix + 'emo_text'].$props.disabled, true)
    assert.match(vectorMode.html, /aria-label="情感向量：喜"/)
    assert.deepEqual(vectorMode.page.values[prefix + 'emo_vector'], vector)
    vectorMode.page.change(prefix + 'emo_vector', [.5, 0, 0, 0, 0, 0, 0, 0])
    vectorMode.page.change(prefix + 'emo_mode', 'dynamic')
    for (const field of vectorMode.page.visibleFields({ fields }).filter((field: any) => field.type === 'vector' || field.path === prefix + 'emo_text'))
      assert.equal(vectorMode.page.fieldDisabled(field), true)
    vectorMode.page.change(prefix + 'emo_mode', 'vector')
    assert.deepEqual(vectorMode.page.values[prefix + 'emo_vector'], [.5, 0, 0, 0, 0, 0, 0, 0])
    assert.equal(vectorMode.page.values[prefix + 'emo_text'], description)
    const textMode = await render(t, page => page.change(prefix + 'emo_mode', 'text'), overrides, scope)
    assert.equal(textMode.controls[prefix + 'emo_vector'].$props.disabled, true)
    assert.ok(textMode.controls[prefix + 'emo_text'])
    assert.match(textMode.html, /保留原有情感描述/)
    assert.deepEqual(textMode.page.patch[prefix + 'emo_vector'], vector)
    assert.equal(dynamic.writes.length + vectorMode.writes.length + textMode.writes.length, 0)
  }
})

test('运行锁保留草稿，单项撤销和模板恢复继承继续使用原来的配置规则', async t => {
  const { page, writes } = await render(t, undefined, { [prefix + 'temperature']: .5 })
  page.change(prefix + 'temperature', .9)
  page.busy = true
  page.change(prefix + 'temperature', .8); page.inherit(prefix + 'temperature')
  assert.equal(page.values[prefix + 'temperature'], .9)
  page.busy = false; page.inherit(prefix + 'temperature')
  assert.equal(page.values[prefix + 'temperature'], .5)
  assert.equal(page.dirty, false)
  const template = await render(t, undefined, { [prefix + 'temperature']: .5 }, 'series:fixture')
  template.page.inherit(prefix + 'temperature')
  assert.equal(template.page.values[prefix + 'temperature'], .6)
  assert.equal(Object.hasOwn(template.page.patch, prefix + 'temperature'), false)
  assert.equal(writes.length, 0)
})

test('参考提示区别逐句短片段、指定句和自选优质参考，不将工程建议冒充官方最低时长', () => {
  const dynamic = indexTtsFieldHelp(prefix + 'timbre_mode', '参考用途', { ...defaults, [prefix + 'timbre_mode']: 'dynamic' })
  assert.match(dynamic, /合成每句时参考该句原声/); assert.match(dynamic, /不足 2 秒/)
  const selected = indexTtsFieldHelp(prefix + 'timbre_mode', '', { ...defaults, [prefix + 'timbre_mode']: 'stable' })
  assert.match(selected, /所选原文句子/); assert.match(selected, /默认第 1 句/)
  assert.doesNotMatch(selected, /0–8 秒|开头参考/)
  const custom = indexTtsFieldHelp(prefix + 'timbre_audio', '', defaults)
  assert.match(custom, /5–12 秒/); assert.match(custom, /这是配音建议/); assert.match(custom, /只读取前 15 秒/)
  assert.equal(indexTtsFieldInactiveReason('assemble.voice_volume', defaults), '')
  assert.equal(indexTtsFieldHelp('assemble.voice_volume', '混音音量用途', defaults), '混音音量用途')
})

async function renderProject(t: any, lineId?: number, edit?: (page: any) => void, busyEvents: boolean[] = []) {
  const writes: any[] = [], reads: string[] = []
  t.mock.method(globalThis, 'fetch', (url: any, options: any) => {
    if (options?.body) writes.push(options.body)
    else reads.push(String(url))
    return new Promise(() => {})
  })
  let page: any
  const controls: Record<string, any> = {}, helps: Record<string, any[]> = {}, presets: any[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, null, {
    default: () => h(ProjectForm, { uid: 'fixture', stepKey: 'tts', lineId, onBusyChange: (value: boolean) => busyEvents.push(value) }),
  }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ProjectParameterForm') {
      page = this.$.setupState; page.fields = fields; page.draft = { ...defaults,
        [prefix + 'do_sample']: false, [prefix + 'num_beams']: 1,
        [prefix + 'timbre_mode']: 'dynamic', [prefix + 'emo_mode']: 'dynamic' }
      page.defaults = { ...defaults }; page.saved = JSON.stringify(page.draft); page.loading = false
      edit?.(page)
    }
    if (this.$options.__name === 'ParamControl') controls[this.$props.field.path] = this
    if (this.$options.__name === 'ParameterHelp') (helps[this.$props.label] ??= []).push(this)
    if (this.$options.__name === 'DubbingPresets') presets.push(this)
  } })
  const html = await renderToString(app)
  return { page, controls, helps, html, writes, reads, presets }
}

const serviceFields = [
  { path: 'tts.backend', label: '配音引擎', type: 'str' },
  { path: prefix + 'url', label: '服务地址', type: 'str' },
  { path: prefix + 'project_dir', label: '安装目录', type: 'str' },
  { path: prefix + 'request_timeout', label: '单句超时', type: 'int' },
]
const extraPresetFields = [
  { path: prefix + 'lang', label: '合成语言', type: 'str' },
  { path: prefix + 'duration_retry_factors', label: '时长重试因子', type: 'str' },
  { path: prefix + 'dynamic_reference_max_seconds', label: '动态参考时长上限', type: 'float' },
]

test('全局和模板将预设参数与服务运行分开；灰显情感字段仍计入预设完整范围', async t => {
  for (const scope of ['global', 'series:fixture']) {
    const allFields = [serviceFields[0], fields[0], serviceFields[1], ...fields.slice(1), ...extraPresetFields, ...serviceFields.slice(2)]
    const { page, html, presets, writes } = await render(t, page => {
      page.adopt({ ...state(), sections: { tts: { label: '配音', fields: allFields } } })
    }, {}, scope)
    const groups = page.styleGroups(page.sections[0][1], 'tts')
    assert.deepEqual(groups.map((group: any) => group.name), ['预设参数', '服务与运行'])
    assert.deepEqual(groups[0].fields.map((field: any) => field.path),
      page.visibleFields({ fields: allFields }).filter((field: any) => !serviceFields.some(service => service.path === field.path)).map((field: any) => field.path))
    assert.deepEqual(groups[1].fields.map((field: any) => field.path), serviceFields.map(field => field.path))
    assert.match(html, /aria-label="预设参数"/)
    assert.match(html, /aria-label="服务与运行"/)
    assert.equal(presets.length, 1)
    assert.deepEqual(presets[0].$props.fields.map((field: any) => field.path), allFields.map(field => field.path))
    assert.ok(presets[0].$props.fields.some((field: any) => field.path === prefix + 'emo_vector'))
    assert.ok(groups[0].fields.some((field: any) => field.path === prefix + 'emo_vector'), '灰显控件仍计入预设影响范围')
    assert.equal(page.fieldDisabled(groups[0].fields.find((field: any) => field.path === prefix + 'emo_vector')), true)
    assert.equal(writes.length, 0)
  }
})

test('项目与句级表单使用同一预设分组；项目选择器不在服务运行组重复出现', async t => {
  for (const lineId of [undefined, 7]) {
    const allFields = [fields[0], serviceFields[0], fields[1], ...serviceFields.slice(1), ...fields.slice(2), ...extraPresetFields]
    const { page, html, presets, writes } = await renderProject(t, lineId, page => {
      page.fields = allFields
    })
    assert.deepEqual(page.groups.map((group: any) => group.name), ['预设参数', '服务与运行'])
    assert.deepEqual(page.groups[1].fields.map((field: any) => field.path), serviceFields.map(field => field.path))
    assert.match(html, /aria-label="预设参数"/)
    assert.match(html, /aria-label="服务与运行"/)
    assert.equal(presets.length, lineId == null ? 1 : 0)
    if (lineId == null) {
      assert.deepEqual(presets[0].$props.fields.map((field: any) => field.path), allFields.map(field => field.path))
      assert.equal(presets[0].$props.scopeLabel, '项目参数')
    }
    assert.equal(writes.length, 0)
  }
})

test('保存预设期间锁住项目参数保存和草稿丢弃；载入仍只写可编辑schema字段', async t => {
  const busyEvents: boolean[] = []
  const { page, writes } = await renderProject(t, undefined, page => {
    // SSR 会在渲染结束清理监听；在真实 setup 生命周期内验证状态通知。
    page.presetBusy = true; page.presetBusy = false
  }, busyEvents)
  page.editField(prefix + 'temperature', .9)
  page.presetBusy = true
  assert.equal(page.locked, true)
  await page.save()
  page.discard()
  page.applyPreset({ [prefix + 'temperature']: .1 })
  assert.equal(page.draft[prefix + 'temperature'], .9)
  assert.equal(writes.length, 0)
  page.presetBusy = false
  assert.deepEqual(busyEvents, [true, false])
  t.mock.method(page.editability, 'isPathLocked', (path: string) => path === prefix + 'timbre_audio')
  assert.equal(page.presetDisabled, true, '单个预设参数冻结也禁止整套预设操作')
  page.applyPreset({ [prefix + 'timbre_audio']: 'blocked.wav', [prefix + 'temperature']: .8, 'server.data_root': 'unexpected' })
  assert.equal(page.draft[prefix + 'timbre_audio'], 'voice.wav')
  assert.equal(page.draft[prefix + 'temperature'], .8)
  assert.equal(page.draft['server.data_root'], undefined)
})

test('配置表单的预设载入尊重路径冻结，不绕过参数控件的运行保护', async t => {
  const { page } = await render(t)
  t.mock.method(page.editability, 'isPathLocked', (path: string) => path === prefix + 'timbre_audio')
  assert.equal(page.presetDisabled({ fields }), true)
  page.applyPreset({ [prefix + 'timbre_audio']: 'blocked.wav', [prefix + 'temperature']: .8, 'server.data_root': 'unexpected' })
  assert.equal(page.values[prefix + 'timbre_audio'], 'voice.wav')
  assert.equal(page.values[prefix + 'temperature'], .8)
  assert.equal(page.values['server.data_root'], undefined)
})

test('全局、模板和项目真实预设组件先只读预览，再同步载入所有适用字段，不被父busy锁丢弃', async t => {
  const presetValues = {
    [prefix + 'timbre_mode']: 'preset', [prefix + 'timbre_audio']: 'preset/voice.wav',
    [prefix + 'emo_mode']: 'vector', [prefix + 'emo_audio']: 'preset/feeling.wav',
    [prefix + 'emo_vector']: [.5, .1, 0, 0, 0, 0, 0, 0], [prefix + 'emo_text']: '预设备用情感文本',
    [prefix + 'temperature']: .9, [prefix + 'duration_factor']: .92,
    [prefix + 'url']: 'http://unexpected', 'server.data_root': 'unexpected',
  }
  for (const scope of ['global', 'series:fixture', 'project']) {
    const serviceValues = { [prefix + 'url']: 'http://127.0.0.1:7860', [prefix + 'project_dir']: 'D:/IndexTTS', [prefix + 'request_timeout']: 600 }
    const form = scope === 'project' ? await renderProject(t, undefined, page => {
      page.fields = [...fields, ...serviceFields]
      page.draft = { ...page.draft, ...serviceValues }; page.saved = JSON.stringify(page.draft)
    }) : await render(t, page => page.adopt({ ...state(), defaults: { ...defaults, ...serviceValues },
      sections: { tts: { label: '配音', fields: [...fields, ...serviceFields] } } }), {}, scope)
    const child = form.presets[0].$.setupState, page = form.page
    const draft = scope === 'project' ? () => page.draft : () => page.values
    const before = JSON.parse(JSON.stringify(draft()))
    const calls: any[] = []
    t.mock.method(globalThis, 'fetch', async (url: any, init: any) => {
      calls.push([String(url), init?.method ?? 'GET'])
      return new Response(JSON.stringify({ values: presetValues, warnings: [] }))
    })
    child.selected = JSON.stringify(['local', '完整音色'])
    await child.readPreview()
    assert.equal(child.candidateReady, true)
    assert.equal(page.locked, false, '只读预览不能锁住父表单并阻断后续载入')
    assert.equal(page.presetBusy, false)
    assert.deepEqual(JSON.parse(JSON.stringify(draft())), before, '选择和预览不会暗中修改草稿')
    assert.equal(page.dirty, false)
    child.load()
    for (const [path, value] of Object.entries(presetValues).filter(([path]) => fields.some(field => field.path === path)))
      assert.deepEqual(draft()[path], value, `${scope}应该载入${path}`)
    assert.equal(draft()[prefix + 'url'], serviceValues[prefix + 'url'], '服务连接不随预设改变')
    assert.equal(draft()['server.data_root'], undefined, '表单范围外字段不得写入')
    assert.equal(page.dirty, true)
    assert.deepEqual(calls, [['/api/workbench-presets/load', 'POST']], '只有预设读取，没有配置保存')
  }
})

for (const lineId of [undefined, 7]) test(`实际${lineId == null ? '项目' : '句级'}参数表单按模式停用，切换保留原值，查看说明不保存`, async t => {
  const { page, controls, helps, html, writes, reads } = await renderProject(t, lineId)
  assert.ok(reads.includes(lineId == null ? '/api/projects/fixture/step_params' : '/api/projects/fixture/workbench/lines/7/params'))
  assert.match(html, lineId == null ? /保存项目参数/ : /保存本句参数/)
  for (const name of ['top_p', 'top_k', 'temperature', 'length_penalty', 'emo_weight', 'emo_random'])
    assert.equal(controls[prefix + name].$props.disabled, true, name)
  assert.equal(helps.top_p.length, 2, '标签和控件都可悬停查看')
  assert.match(helps.top_p[0].$props.help, /随机采样已关闭/)
  assert.match(helps.emo_weight[1].$props.help, /同一段原声/)
  const switchTag = html.match(/<[a-z](?=[^>]*\brole="switch")(?=[^>]*\baria-label="emo_random")[^>]*>/)?.[0]
  assert.ok(switchTag); assert.match(switchTag, /aria-disabled="true"/)
  assert.equal(controls[prefix + 'emo_vector'].$props.disabled, true)
  assert.equal(controls[prefix + 'emo_text'].$props.disabled, true)
  helps.top_p[0].$.setupState.focus(); helps.top_p[0].$.setupState.toggle()
  assert.equal(page.dirty, false)
  controls[prefix + 'do_sample'].$.setupState.val = true
  controls[prefix + 'num_beams'].$.setupState.val = 3
  controls[prefix + 'emo_mode'].$.setupState.val = 'text'
  for (const name of ['top_p', 'top_k', 'temperature', 'length_penalty', 'emo_weight', 'emo_random']) {
    assert.equal(page.disabledField(fields.find(field => field.path === prefix + name)), false, name)
    assert.equal(page.draft[prefix + name], defaults[prefix + name], `${name}备用值保留`)
  }
  assert.match(page.fieldHelp(fields.find(field => field.path === prefix + 'emo_weight')), /当前为文本情感/)
  assert.equal(page.dirty, true)
  page.saving = true
  controls[prefix + 'do_sample'].$.setupState.val = false
  assert.equal(page.draft[prefix + 'do_sample'], true, '运行保存锁阻止控件事件修改草稿')
  assert.equal(writes.length, 0)
})
