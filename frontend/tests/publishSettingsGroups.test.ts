import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider } from 'naive-ui'

let server: ViteDevServer, Form: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/PublishSettingsForm.vue')).default
})
after(() => server.close())

const values = () => ({ title: '旅行视频', description: '完整简介', tags: ['旅行'], dynamic: '发布动态',
  human_type2: 1001, copyright: 3, source: 'https://example.com/original', declaration_id: 1,
  visibility: 'public', publish_at: null, season_id: null, section_id: null,
  watermark: false, close_comments: false, close_danmaku: false, selected_comments: false })

async function render(props: any = {}) {
  let form: any
  const instances: any[] = [], changes: any[] = [], resets: any[] = []
  const inputValues = values(), overrides = { title: '旅行视频' }
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Form, {
    values: inputValues, overrides, metadata: {}, showPlatformAssist: false,
    ...props, onChange: (key: string, value: any) => changes.push({ key, value }),
    onInherit: (key: string) => resets.push(key),
  }))) })
  setup(app)
  app.mixin({ created() {
    instances.push(this.$)
    if (this.$options.__name === 'PublishSettingsForm') form = this.$.setupState
  } })
  const html = await renderToString(app)
  return { html, form, changes, resets, inputValues, overrides, instances }
}

function groupContent(html: string, title: string) {
  const match = html.match(new RegExp(`<section\\b[^>]*aria-label="${title}"[^>]*>([\\s\\S]*?)<\\/section>`))
  assert.ok(match, `缺少资料分组：${title}`)
  return match[1]
}
function labels(html: string) { return [...html.matchAll(/role="group" aria-label="([^"]+)"/g)].map(match => match[1]) }

test('共享表单默认保留单组原字段次序，不为设置或模板新增分组标题', async () => {
  const { html } = await render()
  assert.doesNotMatch(html, /class="field-group-heading"|<h3/)
  assert.deepEqual(labels(html).slice(0, 12), ['标题', '简介', '标签', '分区', '稿件类型', '原视频来源', '创作声明', '可见范围', '合集', '合集小节', '定时发布', '粉丝动态'])
  assert.equal((html.match(/class="publish-fields"/g) || []).length, 1)
  assert.equal((html.match(/更多设置 · 水印与互动/g) || []).length, 1)
})

test('发布页按文案、属性、安排分组且各字段仅出现一次，水印互动仍在最后', async () => {
  const { html } = await render({ grouped: true })
  assert.deepEqual(labels(groupContent(html, '稿件文案')), ['标题', '简介', '标签', '粉丝动态'])
  assert.deepEqual(labels(groupContent(html, '投稿属性')), ['分区', '稿件类型', '原视频来源', '创作声明'])
  assert.deepEqual(labels(groupContent(html, '发布安排')), ['可见范围', '定时发布', '合集', '合集小节'])
  assert.match(html, /所有分P共用/)
  assert.match(html, /实际素材权利/)
  assert.ok(html.indexOf('aria-label="稿件文案"') < html.indexOf('aria-label="投稿属性"'))
  assert.ok(html.indexOf('aria-label="投稿属性"') < html.indexOf('aria-label="发布安排"'))
  assert.ok(html.indexOf('aria-label="发布安排"') < html.indexOf('更多设置 · 水印与互动'))
  assert.equal((html.match(/更多设置 · 水印与互动/g) || []).length, 1)
  assert.equal(labels(html).filter(label => label === '标题').length, 1)
})

test('字段筛选与模板隐藏规则先生效，不显示空分组或额外资料字段', async () => {
  const partial = await render({ grouped: true, fieldKeys: ['title', 'season_id'], showMore: false })
  assert.deepEqual(labels(groupContent(partial.html, '稿件文案')), ['标题'])
  assert.deepEqual(labels(groupContent(partial.html, '发布安排')), ['合集'])
  assert.doesNotMatch(partial.html, /aria-label="投稿属性"|更多设置 · 水印与互动/)
  const template = await render({ grouped: true, template: true, fieldKeys: ['title', 'dynamic', 'publish_at'], showMore: false })
  assert.doesNotMatch(template.html, /class="field-group-heading"|class="publish-field"|aria-label="稿件文案"|aria-label="发布安排"/)
  const templateTags = await render({ grouped: true, template: true, fieldKeys: ['title', 'tags', 'source'], showMore: false })
  assert.deepEqual(labels(groupContent(templateTags.html, '稿件文案')), ['标签'])
  assert.doesNotMatch(templateTags.html, /aria-label="投稿属性"|aria-label="发布安排"/)
})

test('分组前后真实标题输入事件与合集唯一小节联动保持相同，不改写其他字段', async () => {
  for (const grouped of [false, true]) {
    const result = await render({ grouped, metadata: { seasons: [{ id: 9, title: '旅行合集', sections: [{ id: 10, title: '第一辑' }] }] } })
    const input = result.instances.find(instance => instance.type.name === 'Input'
      && (instance.vnode.props?.['input-props'] || instance.vnode.props?.inputProps)?.['aria-label'] === '标题')
    assert.ok(input, '未找到真实标题输入组件')
    input.vnode.props['onUpdate:value']('新的稿件标题')
    result.form.select('season_id', 9)
    assert.deepEqual(result.changes, [{ key: 'title', value: '新的稿件标题' }, { key: 'season_id', value: 9 }, { key: 'section_id', value: 10 }])
    assert.equal(result.inputValues.title, '旅行视频')
    assert.equal(result.inputValues.description, '完整简介')
    assert.deepEqual(result.overrides, { title: '旅行视频' })
  }
})

test('分组保留字段来源、自定义撤销按钮与禁用保护，不将继承设置改成本稿件覆盖', async () => {
  const result = await render({ grouped: true, sources: { title: { kind: 'publication' }, description: { kind: 'series', name: '旅行模板' } },
    restoreLabels: { title: '撤销本次标题' }, resetHint: '恢复已经保存的值' })
  assert.match(result.html, /撤销本次标题/)
  assert.match(result.html, /恢复已经保存的值/)
  assert.match(result.html, /旅行模板/)
  result.form.reset('title'); result.form.reset('description')
  assert.deepEqual(result.resets, ['title'])
  assert.deepEqual(result.changes, [])
  const locked = await render({ grouped: true, disabled: true, sourcePublishedDate: '2026-10-08' })
  assert.doesNotMatch(locked.html, /撤销本次标题|补入原视频日期/)
  const reset = locked.instances.find(instance => instance.type.__name === 'ParameterResetButton' && instance.props.label === '标题')
  assert.ok(reset); assert.equal(reset.props.disabled, true)
  locked.form.reset('title'); locked.form.insertPublishedDate()
  assert.deepEqual(locked.resets, []); assert.deepEqual(locked.changes, [])
  assert.equal(locked.inputValues.description, '完整简介')
})

test('章节自动发布选项默认开启，保留来源与恢复动作，编辑仅发本地change', async () => {
  const result = await render({ grouped:true, overrides:{chapters_enabled:false},
    sources:{chapters_enabled:{kind:'global',frozen:true}} })
  assert.deepEqual(labels(groupContent(result.html,'视频章节')),
    ['投稿后自动设置视频章节','自动翻译章节标题为中文','B站App默认展示章节','章节标题自定义要求（可选）'])
  assert.match(result.html,/源视频无章节时跳过/)
  assert.match(result.html,/保存设置不会立即修改B站/)
  assert.match(result.html,/投稿后自动设置视频章节恢复继承/)
  const toggles = result.instances.filter(instance => instance.type.name==='Switch'
    && String(instance.vnode.props?.['aria-label']||'').includes('章节'))
  assert.equal(toggles.length,3);assert.ok(toggles.every(instance=>instance.vnode.props.value===true))
  result.form.changeChapter('chapters_enabled',false)
  result.form.changeChapter('chapters_translate',false)
  result.form.reset('chapters_enabled')
  assert.deepEqual(result.changes,[{key:'chapters_enabled',value:false},{key:'chapters_translate',value:false}])
  assert.deepEqual(result.resets,['chapters_enabled']);assert.equal(result.inputValues.title,'旅行视频')
})

test('关闭章节或锁定表单时，迟到开关事件不能改动从属选项', async () => {
  const off = await render({values:{...values(),chapters_enabled:false,chapters_translate:false,chapters_permanent:false}})
  off.form.changeChapter('chapters_translate',true);off.form.changeChapter('chapters_permanent',true)
  assert.deepEqual(off.changes,[])
  const locked = await render({disabled:true})
  locked.form.changeChapter('chapters_enabled',false)
  assert.deepEqual(locked.changes,[])
})

test('章节自定义要求只有一个继承文本项，支持内容、格式、表述和术语',async()=>{
  const result=await render({grouped:true,values:{...values(),chapters_instruction:'保留模型英文名'},
    overrides:{chapters_instruction:'保留模型英文名'}})
  assert.equal((result.html.match(/role="group" aria-label="章节标题自定义要求（可选）"/g)||[]).length,1)
  assert.match(result.html,/内容审查、格式、表述与术语/);assert.match(result.html,/硬规则固定校验/)
  const input=result.instances.find(instance=>instance.type.name==='Input'&&instance.vnode.props?.['input-props']?.['aria-label']==='章节标题自定义要求（可选）')
  assert.ok(input);assert.equal(input.vnode.props.maxlength,2000)
  input.vnode.props['onUpdate:value']('标题用简短名词短语')
  assert.deepEqual(result.changes,[{key:'chapters_instruction',value:'标题用简短名词短语'}])
  result.form.reset('chapters_instruction');assert.deepEqual(result.resets,['chapters_instruction'])
})
