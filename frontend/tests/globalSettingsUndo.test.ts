import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NDialogProvider, NMessageProvider } from 'naive-ui'

// 根设置撤销使用已保存快照；全部请求隔离，避免读取或修改用户设置。
let server: ViteDevServer, Form: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Form = (await server.ssrLoadModule('/src/components/ConfigurationForm.vue')).default
})
after(() => server.close())

const defaults: Record<string, any> = {
  'assemble.voice_volume': 1, 'assemble.loudnorm': true,
  'assembly.versions': [{ id: 'dub', name: '配音版', dubbing: true, burn_subtitles: true, include_comments: false, overrides: {} }],
  'publish.account_id': '', 'publish.version_ids': null, 'publish.part_titles': {},
  'publish.cover_source': 'platform', 'publish.cover_id': '', 'publish.close_comments': true,
}
const fields = [
  { path: 'assemble.voice_volume', label: '人声音量', type: 'float', min: 0, max: 1 },
  { path: 'assemble.loudnorm', label: '归一化', type: 'bool' },
]
function saved(overrides: Record<string, any> = {}) {
  return { key: 'global', name: '全局默认', revision: 4, fingerprint: 'global-4', archived: false,
    defaults, overrides, parent_sources: Object.fromEntries(Object.keys(defaults).map(key => [key, { kind: 'global' }])),
    sections: { assemble: { label: '成片', fields } }, version_fields: [] }
}
async function render(t: any, state = saved(), edit?: (page: any) => void, scope = 'global') {
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => {
    if (options?.body) writes.push(JSON.parse(options.body))
    return new Promise(() => {})
  })
  let page: any
  const buttons: any[] = []
  const resets: any[] = []
  const app = createSSRApp({ render: () => h(NMessageProvider, null, { default: () => h(NDialogProvider, null, { default: () => h(Form, { scope }) }) }) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ConfigurationForm') {
      page = this.$.setupState; page.adopt(state); page.loading = false; edit?.(page)
    }
    if (this.$options.name === 'Button') buttons.push(this)
    if (this.$options.__name === 'ParameterResetButton') resets.push(this)
  } })
  const html = await renderToString(app)
  return { page, buttons, resets, writes, html }
}

test('全局已保存字段保留禁用的撤销入口，不显示自我继承，已保存值是撤销基准', async t => {
  const { html, page, resets, writes } = await render(t, saved({ 'assemble.voice_volume': .7 }))
  assert.match(html, /全局没有上级设置/)
  assert.match(html, /0 项修改/)
  const reset = resets.find(button => button.$props.label === '人声音量' && button.$props.action === '撤销修改')
  assert.ok(reset)
  assert.equal(reset.$props.disabled, true)
  assert.doesNotMatch(html, /aria-label="人声音量恢复继承"/)
  assert.doesNotMatch(html, /继承自全局默认/)
  assert.equal(page.canReset('assemble.voice_volume'), false)
  assert.equal(writes.length, 0)
})

test('真实单项撤销按钮回到已保存全局值，保留其它修改，不清除保存记录或自动请求', async t => {
  const { html, page, buttons, writes } = await render(t, saved({ 'assemble.voice_volume': .7 }), page => {
    page.change('assemble.voice_volume', .2); page.change('assemble.loudnorm', false)
  })
  assert.match(html, /aria-label="人声音量撤销修改"/)
  assert.match(html, /2 项修改/)
  const reset = buttons.find(button => button.$attrs['aria-label'] === '人声音量撤销修改')
  assert.ok(reset)
  reset.$.setupState.handleClick({ preventDefault() {}, stopPropagation() {} })
  assert.equal(page.values['assemble.voice_volume'], .7)
  assert.equal(page.patch['assemble.voice_volume'], .7)
  assert.equal(page.values['assemble.loudnorm'], false)
  assert.equal(page.canReset('assemble.voice_volume'), false)
  assert.equal(page.dirty, true)
  assert.equal(writes.length, 0)
})

test('全局未单独保存的字段撤销时移除新草稿，false、0、null和空串已有值准确还原', async t => {
  const values = { 'assemble.voice_volume': 0, 'assemble.loudnorm': false, 'publish.account_id': '', 'publish.version_ids': null }
  const { page, writes } = await render(t, saved(values))
  for (const [key, value] of Object.entries(values)) {
    page.change(key, key === 'publish.version_ids' ? ['dub'] : key === 'publish.account_id' ? 'other' : 1)
    page.inherit(key)
    assert.equal(page.patch[key], value)
  }
  page.change('publish.part_titles', { dub: '新名称' }); page.inherit('publish.part_titles')
  assert.equal(Object.hasOwn(page.patch, 'publish.part_titles'), false)
  assert.equal(page.dirty, false)
  assert.equal(writes.length, 0)
})

test('全局保存成功后新值成为撤销基准，撤销不再发送reset清除原全局值', async t => {
  const { page } = await render(t, saved({ 'assemble.voice_volume': .7 }))
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    const body = JSON.parse(options.body); writes.push(body)
    return new Response(JSON.stringify({ ...saved(body.updates), revision: 5, fingerprint: 'global-5' }))
  })
  page.change('assemble.voice_volume', .4); await page.save()
  assert.deepEqual(writes[0].reset, [])
  assert.equal(page.dirty, false)
  page.change('assemble.voice_volume', .1); page.inherit('assemble.voice_volume')
  assert.equal(page.values['assemble.voice_volume'], .4)
  assert.equal(page.dirty, false)
  assert.equal(writes.length, 1)
})

test('全局撤销成片清单使用保存快照；版本内部样式仍可恢复继承共同设置', async t => {
  const versions = [{ ...defaults['assembly.versions'][0], name: '已保存版本', overrides: { 'assemble.voice_volume': .5 } }]
  const state = { ...saved({ 'assembly.versions': versions }), version_fields: [{ ...fields[0], group: '混音' }] }
  const { html, page } = await render(t, state, page => page.editVersion(0, 'name', '未保存版本'))
  assert.match(html, /撤销清单修改/)
  assert.match(html, /aria-label="人声音量恢复共同设置"/)
  page.inherit('assembly.versions')
  assert.equal(page.values['assembly.versions'][0].name, '已保存版本')
  assert.equal(page.canReset('assembly.versions'), false)
  page.editVersionOverride(0, 'assemble.voice_volume', null, true)
  assert.equal(Object.hasOwn(page.values['assembly.versions'][0].overrides, 'assemble.voice_volume'), false)
  assert.equal(page.dirty, true)
  assert.equal(versions[0].overrides['assemble.voice_volume'], .5)
})

test('发布表单全局只暴露未保存字段的撤销，根来源不会标继承自全局', async t => {
  const state = { ...saved({ 'publish.close_comments': false, 'publish.account_id': 'saved-account' }),
    sections: { publication: { label: '发布规则', fields: [{path:'publish.close_comments',label:'关闭评论',type:'bool'}] } } }
  const { html, page } = await render(t, state, page => {
    page.change('publish.close_comments', true); page.change('publish.account_id', 'new-account')
  })
  assert.match(html, /aria-label="关闭评论撤销修改"/)
  assert.doesNotMatch(html, /继承自全局默认/)
  assert.deepEqual({ ...page.publishResetOverrides }, { close_comments: true, account_id: 'new-account' })
  page.inherit('publish.close_comments')
  assert.equal(page.values['publish.close_comments'], false)
  assert.equal(Object.hasOwn(page.publishResetOverrides, 'close_comments'), false)
  assert.equal(page.values['publish.account_id'], 'new-account')
})

test('模板仍恢复真实上级，运行锁定时全局撤销保留草稿', async t => {
  const template = await render(t, saved({ 'assemble.voice_volume': .7 }), undefined, 'series:travel')
  assert.match(template.html, /aria-label="人声音量恢复继承"/)
  template.page.inherit('assemble.voice_volume')
  assert.equal(template.page.values['assemble.voice_volume'], 1)
  const { page } = await render(t, saved({ 'assemble.voice_volume': .7 }))
  page.change('assemble.voice_volume', .2); page.busy = true; page.inherit('assemble.voice_volume')
  assert.equal(page.values['assemble.voice_volume'], .2)
})

test('每个分P名称可单独撤销到已保存的有效值，保留其他名称和已有清单',async t=>{
  const versions=[...defaults['assembly.versions'],{...defaults['assembly.versions'][0],id:'source',name:'原声版'}]
  const titles={dub:'已保存配音名称',source:'已保存原声名称'}
  const state={...saved({'publish.part_titles':titles}),defaults:{...defaults,'assembly.versions':versions},
    sections:{publication:{label:'发布',fields:[{path:'publish.account_id',label:'发布账号',type:'str'}]}}}
  const {page,resets,writes}=await render(t,state,page=>page.partTitle('dub','修改配音名称'))
  const first=resets.find(button=>button.$props.label==='P1名称'),second=resets.find(button=>button.$props.label==='P2名称')
  assert.ok(first);assert.ok(second)
  assert.equal(first.$props.action,'撤销本项修改');assert.equal(first.$props.disabled,false);assert.equal(second.$props.disabled,true)
  page.partTitle('source','保留原声修改')
  first.$emit('reset')
  assert.deepEqual({...page.values['publish.part_titles']},{dub:'已保存配音名称',source:'保留原声修改'})
  assert.deepEqual(titles,{dub:'已保存配音名称',source:'已保存原声名称'})
  page.busy=true;second.$emit('reset')
  assert.equal(page.values['publish.part_titles'].source,'保留原声修改')
  assert.equal(writes.length,0)
})

test('逐行撤销分P名称时保留上级保存值，全部还原后不留下本次新建的对象覆盖',async t=>{
  const versions=[...defaults['assembly.versions'],{...defaults['assembly.versions'][0],id:'source',name:'原声版'}]
  const state={...saved(),defaults:{...defaults,'assembly.versions':versions,'publish.part_titles':{dub:'基础配音名称'}},
    sections:{publication:{label:'发布',fields:[{path:'publish.account_id',label:'发布账号',type:'str'}]}}}
  const {page,writes}=await render(t,state,undefined,'series:travel')
  page.partTitle('dub','修改配音名称');page.partTitle('source','新增原声名称')
  page.restorePartTitle('dub')
  assert.deepEqual({...page.patch['publish.part_titles']},{dub:'基础配音名称',source:'新增原声名称'})
  page.restorePartTitle('source')
  assert.equal(Object.hasOwn(page.patch,'publish.part_titles'),false)
  assert.equal(page.values['publish.part_titles'].dub,'基础配音名称')
  assert.equal(page.dirty,false);assert.equal(writes.length,0)
})
