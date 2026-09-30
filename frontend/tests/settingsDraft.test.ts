import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'

// 加载页面真实的草稿/保存逻辑；替换界面与生命周期入口，不访问用户配置。
const root = fileURLToPath(new URL('..', import.meta.url))
const source = await readFile(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const bundle = await build({
  stdin: { contents: descriptor.scriptSetup!.content + `
    export { profiles, form, markApplied, isDirty, rootDirty, displayDirty, concDirty,
      applySettings, applyConc, applyDisplay, delProfile, route, router, dialog, sections,
      section, selectedScope, visited, productionDirty, productionBusy, warnUnload, loadSettings };`,
    loader: 'ts', resolveDir: fileURLToPath(new URL('../src/views', import.meta.url)) },
  bundle: true, write: false, format: 'esm', platform: 'node',
  plugins: [{ name: 'page-test-boundary', setup(build) {
    build.onResolve({ filter: /^(naive-ui|vue-router|vue)$/ }, args => ({ path: args.path, namespace: 'page-test' }))
    build.onLoad({ filter: /.*/, namespace: 'page-test' }, args => ({
      contents: args.path === 'vue'
        ? `export { computed, ref, watch } from '@vue/reactivity'; export const onMounted = () => {}, onUnmounted = () => {};`
        : args.path === 'vue-router' ? `import { reactive } from '@vue/reactivity';
           const router = { update: null, leave: null, pushed: [] };
           export const onBeforeRouteLeave = fn => router.leave = fn, onBeforeRouteUpdate = fn => router.update = fn;
           export const useRoute = () => reactive({ query: {} }), useRouter = () => router;`
        : `export const useMessage = () => ({ success() {}, error() {}, warning() {}, info() {} });
           export const useDialog = () => ({ prompts: [], warning(options) { this.prompts.push(options) } });`,
      resolveDir: root,
    }))
  } }],
})
let fixtureId = 0
async function fixture(t: any) {
  const page = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text + `\n// ${fixtureId++}`).toString('base64')}`)
  page.profiles.value = [{ name: '测试', base_url: '', model: '', api_key: '', enabled: true, paramsJson: '' }]
  page.form.value.conc = { download: 2 }
  page.markApplied()
  const requests: any[] = []
  t.mock.method(globalThis, 'fetch', async (_url: string, init: any) => {
    requests.push(JSON.parse(init.body))
    return new Response('{}')
  })
  return { page, requests }
}

test('默认空存储路径可编辑；保存模型不会吞掉其他分区草稿', async t => {
  const { page, requests } = await fixture(t)
  page.form.value.data_root = 'D:\\新工作目录'
  page.form.value.conc.download = 3
  page.productionDirty.value = true
  page.form.value.stream_reference_distance = 700
  page.profiles.value[0].name = '修改名称'
  assert.equal(page.rootDirty.value, true)
  await page.applySettings()
  assert.equal(page.isDirty.value, false)
  assert.equal(page.rootDirty.value, true)
  assert.equal(page.concDirty.value, true)
  assert.equal(page.productionDirty.value, true)
  assert.equal(page.displayDirty.value, true)
  assert.equal('server.data_root' in requests[0].updates, false)
})

test('删除最后一组也标记未保存；非法 JSON 阻止发送', async t => {
  const { page, requests } = await fixture(t)
  page.profiles.value[0].paramsJson = '{'
  await page.applySettings()
  assert.equal(requests.length, 0)
  assert.equal(page.isDirty.value, true)
  page.delProfile(0)
  assert.equal(page.isDirty.value, true)
})

test('慢保存期间的新编辑继续显示未保存', async t => {
  const { page } = await fixture(t)
  let finish: (response: Response) => void
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { finish = resolve }))
  page.profiles.value[0].name = '第一次'
  const saving = page.applySettings()
  page.profiles.value[0].name = '第二次'
  finish!(new Response('{}'))
  await saving
  assert.equal(page.isDirty.value, true)
  assert.equal(page.profiles.value[0].name, '第二次')
})

test('显示偏好独立保存，既不提交模型密钥，也不吞掉模型或制作草稿', async t => {
  const { page, requests } = await fixture(t)
  page.profiles.value[0].name = '未保存模型'
  page.productionDirty.value = true
  page.form.value.stream_reference_distance = 800
  await page.applyDisplay()
  assert.deepEqual(requests[0].updates, { 'ui.llm_stream.reference_distance_chars': 800 })
  assert.equal(page.displayDirty.value, false)
  assert.equal(page.isDirty.value, true)
  assert.equal(page.productionDirty.value, true)
})

test('显示偏好保存失败保留草稿；慢保存不吞掉新输入，重复点击只发一次', async t => {
  const { page } = await fixture(t)
  page.form.value.stream_reference_distance = 600
  t.mock.method(globalThis, 'fetch', async () => new Response('{}', { status: 500 }))
  await page.applyDisplay()
  assert.equal(page.displayDirty.value, true)
  let finish: (response: Response) => void
  let count = 0
  t.mock.method(globalThis, 'fetch', () => { count++; return new Promise(resolve => { finish = resolve }) })
  const saving = page.applyDisplay()
  await page.applyDisplay()
  page.form.value.stream_reference_distance = 900
  finish!(new Response('{}'))
  await saving
  assert.equal(count, 1)
  assert.equal(page.displayDirty.value, true)
})

test('只有三个设置分类；URL 直达模板，分类切换保留制作草稿而无需确认', async t => {
  const { page, requests } = await fixture(t)
  assert.deepEqual(page.sections.value.map((s:any) => s.label), ['制作与发布', '模型与服务', '运行与存储'])
  assert.equal(page.section.value, 'production')
  assert.equal(page.selectedScope.value, 'global')
  page.route.query = { section: 'production', template: 'travel' }
  assert.equal(page.selectedScope.value, 'series:travel')
  page.productionDirty.value = true
  assert.equal(page.router.update({ query: { section: 'models', template: 'travel' } }), true)
  assert.equal(page.productionDirty.value, true)
  assert.equal(page.dialog.prompts.length, 0)
  assert.equal(requests.length, 0)
})

test('切换模板及离开受统一保护；取消保留草稿，关闭/遮罩也终止导航', async t => {
  const { page } = await fixture(t)
  page.productionDirty.value = true
  const switching = page.router.update({ query: { template: 'travel' } })
  assert.equal(page.dialog.prompts.length, 1)
  page.dialog.prompts.at(-1).onNegativeClick()
  assert.equal(await switching, false)
  assert.equal(page.productionDirty.value, true)
  const leaving = page.router.leave()
  page.dialog.prompts.at(-1).onClose()
  assert.equal(await leaving, false)
  const masked = page.router.leave()
  page.dialog.prompts.at(-1).onMaskClick()
  assert.equal(await masked, false)
  const confirmed = page.router.update({ query: { template: 'travel' } })
  page.dialog.prompts.at(-1).onPositiveClick()
  assert.equal(await confirmed, true)
})

test('保存中禁止切换范围和离开，但分类切换不销毁表单；刷新关闭受保护', async t => {
  const { page } = await fixture(t)
  page.productionBusy.value = true
  assert.equal(page.router.update({ query: { template: 'travel' } }), false)
  assert.equal(page.router.leave(), false)
  assert.equal(page.router.update({ query: { section: 'runtime' } }), true)
  assert.equal(page.dialog.prompts.length, 0)
  let prevented = false
  const event = { preventDefault() { prevented = true }, returnValue: 'x' }
  page.warnUnload(event)
  assert.equal(prevented, true)
  assert.equal(event.returnValue, '')
})

test('默认制作页不请求系统配置，只有进入系统分类才按需读取', async t => {
  const { page } = await fixture(t)
  const reads:string[] = []
  t.mock.method(globalThis, 'fetch', async (url:string) => {
    reads.push(url)
    return new Response(JSON.stringify(url === '/api/settings' ? { llm: { profiles: [] }, server: {}, runner: {} } : {}))
  })
  assert.equal(reads.length, 0)
  page.route.query = { section: 'runtime' }
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(reads.filter(url => url === '/api/settings').length, 1)
  page.route.query = { section: 'models' }
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(reads.filter(url => url === '/api/settings').length, 1)
  assert.equal(page.visited.value.has('production'), true)
  assert.equal(page.visited.value.has('runtime'), true)
})
