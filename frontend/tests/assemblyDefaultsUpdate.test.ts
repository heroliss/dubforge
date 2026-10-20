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

const root = fileURLToPath(new URL('..', import.meta.url))
const { descriptor } = parse(await readFile(new URL('../src/components/AssemblyDefaultsUpdate.vue', import.meta.url), 'utf8'))
const result = await build({ stdin: { contents: `import {reactive,unmountHooks} from 'vue';
 const fixtureProps=reactive({uid:'fixture',disabled:false}),defineProps=()=>fixtureProps;
 const events=[],defineEmits=()=>((name,...args)=>events.push({name,args})),defineExpose=()=>{};
 ${descriptor.scriptSetup!.content}
 export{fixtureProps,events,show,loading,applying,plan,error,inspect,apply,close,canApply,unmountHooks};`, loader: 'ts', resolveDir: `${root}/src/components` }, bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'boundaries', setup(b) {
   b.onResolve({ filter: /^vue$|^naive-ui$|\.vue$/ }, a => ({ path: a.path, namespace: 'test' }))
   b.onLoad({ filter: /.*/, namespace: 'test' }, a => ({ contents: a.path === 'vue' ? `export{ref,computed,watch,reactive}from'@vue/reactivity';export const unmountHooks=[],onBeforeUnmount=f=>unmountHooks.push(f);` : 'export default {};', resolveDir: root }))
 } }] })
let serial = 0, server: ViteDevServer, Panel: any
before(async () => {
  server = await createServer({ root, configFile: false, plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Panel = (await server.ssrLoadModule('/src/components/AssemblyDefaultsUpdate.vue')).default
})
after(() => server.close())
async function fixture(t: any) {
  const p = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text + `\n//${serial++}`).toString('base64')}`)
  t.after(() => p.unmountHooks.forEach((hook: () => void) => hook()))
  return p
}
const summary = () => ({ source: { kind: 'global', name: '全局默认' }, source_name: '全局默认', changed_count: 2, effective_count: 1, preserved_count: 1, version_list_fixed: true, preserved_version_count: 2, fingerprint: 'fp-1' })
const review = () => ({ ...summary(), changes: [{ path: 'assemble.voice_volume', label: '人声音量', before: 1, after: .8, overridden: true }], versions: [{ id: 'v1', name: '原声双语', changes: [{ path: 'assemble.output_crf', label: '输出画质', before: 22, after: 20 }], preserved_overrides: [{ path: 'assemble.subtitle_font_size', label: '字幕字号', value: 40, source: { kind: 'global', frozen: true } }] }] })
const response = (value: any, status = 200) => new Response(JSON.stringify(value), { status })

test('真实抽屉按版本显示实际变化与保留专用值，摘要说明整份项目清单和基础快照', async () => {
  const app = createSSRApp({ render: () => h(Panel, { uid: 'fixture', summary: summary() }) }); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'AssemblyDefaultsUpdate') { this.$.setupState.show = true; this.$.setupState.plan = review() } } })
  const context: any = {}, html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
  for (const label of ['已存全局值', '上级有 2 项更新', '整份版本清单', '原声双语', '输出画质', '保留的版本专用值', '字幕字号', '确认应用合成设置']) assert.ok(html.includes(label), label)
  assert.doesNotMatch(html, /基础值已是最新/)
})

test('目标响度更新说明旧基础与当前项目值，明确保留项目模式并用中文展示模式变化', async () => {
  const base = { kind: 'global', frozen: true }, latest = { kind: 'global' }
  const loudnessPlan = {
    ...summary(),
    changes: [
      { path: 'assemble.final_loudness_mode', label: '最终成片响度', before: 'off', after: 'uniform', overridden: true,
        effective_before: 'normalize', effective_after: 'normalize', before_source: base, after_source: latest },
      { path: 'assemble.final_loudness_lufs', label: '成片目标响度', before: -16, after: -14, overridden: false,
        effective_before: -16, effective_after: -14, before_source: base, after_source: latest },
    ],
    versions: [{ id: 'v1', name: '配音版', changes: [
      { path: 'assemble.final_loudness_lufs', label: '成片目标响度', before: -16, after: -14 },
    ], preserved_overrides: [] }],
  }
  const app = createSSRApp({ render: () => h(Panel, { uid: 'fixture', summary: summary() }) }); setup(app)
  app.mixin({ created() { if (this.$options.__name === 'AssemblyDefaultsUpdate') { this.$.setupState.show = true; this.$.setupState.plan = loudnessPlan } } })
  const context: any = {}, html = await renderToString(app, context) + Object.values(context.teleports || {}).join('')
  const visible = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]*>/g, '')
  for (const label of ['已存全局值', '全局默认', '保持混音音量', '统一响度（必要时控制峰值）', '保留项目单独值：保留动态（仅固定增益）', '成片目标响度', '-16', '-14']) {
    assert.ok(visible.includes(label), label)
  }
  assert.doesNotMatch(html, /<pre[^>]*>\s*(?:off|uniform|normalize)\s*<\/pre>/, '可见的响度模式应呈现中文名称')
})

test('先读差异后明确确认，仅发送冻结fingerprint，应用回执直接向上交付且不启动制作', async t => {
  const p = await fixture(t), requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => {
    requests.push({ url: String(url), ...options })
    return response(options.method === 'POST' ? { revision: 2, versions: [] } : review())
  })
  await p.inspect(); assert.equal(requests.length, 1); assert.match(requests[0].url, /\/defaults-plan$/); assert.equal(p.canApply.value, true)
  await p.apply(); assert.equal(requests.length, 2); assert.match(requests[1].url, /\/apply-defaults$/)
  assert.deepEqual(JSON.parse(requests[1].body), { fingerprint: 'fp-1', confirmed: true })
  assert.ok(p.events.some((event: any) => event.name === 'applied' && event.args[0].revision === 2))
  assert.equal(p.show.value, false); assert.ok(requests.every(row => !/\/run|\/start/.test(row.url)))
})

test('预检后草稿或字段锁出现时不能应用，冲突撤销旧确认并保留现有项目设置', async t => {
  const p = await fixture(t), writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: any, options: any) => {
    if (options.method === 'POST') { writes.push(options); return response({ message: '来源已变化，请重新检查' }, 400) }
    return response(review())
  })
  await p.inspect(); p.fixtureProps.disabled = true; await p.apply(); assert.equal(writes.length, 0)
  p.fixtureProps.disabled = false; await p.apply(); assert.equal(writes.length, 1)
  assert.equal(p.plan.value, null); assert.equal(p.canApply.value, false); assert.match(p.error.value, /来源已变化/)
  assert.ok(!p.events.some((event: any) => event.name === 'applied'))
})

test('取消读取或切换项目后旧响应不回写，退出中止请求', async t => {
  const p = await fixture(t), replies: any[] = [], requests: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => { requests.push(options); return new Promise(resolve => replies.push(resolve)) })
  const pending = p.inspect(); p.close(); assert.equal(requests[0].signal.aborted, true)
  replies[0](response(review())); await pending; assert.equal(p.plan.value, null)
  const old = p.inspect(); p.fixtureProps.uid = 'next'; assert.equal(requests[1].signal.aborted, true)
  replies[1](response(review())); await old; assert.equal(p.plan.value, null)
  const next = p.inspect(); p.unmountHooks.forEach((hook: () => void) => hook()); assert.equal(requests[2].signal.aborted, true)
  replies[2](response(review())); await next; assert.equal(p.plan.value, null)
})
