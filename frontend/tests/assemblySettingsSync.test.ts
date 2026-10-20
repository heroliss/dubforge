import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'

// 执行真实组件的响应式脚本；只替换路由、组件、生命周期和服务边界，不写用户项目。
const root = fileURLToPath(new URL('..', import.meta.url))
async function compile(component: string, exports: string) {
  const { descriptor } = parse(await readFile(new URL(`../src/components/${component}.vue`, import.meta.url), 'utf8'))
  const result = await build({ stdin: { contents: `import {reactive, unmountHooks} from 'vue';
    const fixtureProps=reactive(__PROPS__),events=[],exposed={};
    const window={removeEventListener(){}};
    const defineProps=()=>fixtureProps,defineEmits=()=>((...args)=>events.push(args)),defineExpose=value=>Object.assign(exposed,value);
    ${descriptor.scriptSetup!.content}
    export{fixtureProps,events,exposed,unmountHooks,${exports}};`, loader: 'ts', resolveDir: `${root}/src/components` },
    bundle: true, write: false, format: 'esm', platform: 'node', plugins: [{ name: 'settings-sync-boundary', setup(b) {
      b.onResolve({ filter: /^(vue|vue-router|naive-ui)$|\.vue$|stores\/nav$|utils\/configurationEditability$|composables\/useCommentOverlay$/ }, a => ({ path: a.path, namespace: 'settings-sync' }))
      b.onLoad({ filter: /.*/, namespace: 'settings-sync' }, a => ({ contents: a.path === 'vue'
        ? `export {ref,computed,watch,reactive} from '@vue/reactivity';export const unmountHooks=[],onMounted=()=>{},onUnmounted=f=>unmountHooks.push(f),onBeforeUnmount=f=>unmountHooks.push(f);`
        : a.path === 'vue-router' ? `export const onBeforeRouteLeave=()=>{},onBeforeRouteUpdate=()=>{},RouterLink={};`
        : a.path === 'naive-ui' ? `export const useMessage=()=>({success(){},error(){},info(){},warning(){}}),useDialog=()=>({warning(){return{destroy(){}}}});`
        : a.path.endsWith('/nav') ? `import {reactive} from '@vue/reactivity';export const useNavStore=()=>reactive({archived:false,stepMap:{assemble:{status:'pending'}},poll:async()=>{}});`
        : a.path.endsWith('/configurationEditability') ? `import {ref} from '@vue/reactivity';export const useConfigurationEditability=()=>({locked:ref(false),observing:ref(false),reason:ref(''),isPathLocked:()=>false,canEditPaths:async()=>true});`
        : a.path.endsWith('/useCommentOverlay') ? `import{ref}from'@vue/reactivity';export const useCommentOverlay=()=>({revision:ref('overlay-1'),refresh:async()=>{}});`
        : 'export default {};', resolveDir: root }))
    } }] })
  return result.outputFiles[0].text
}
const formCode = await compile('ProjectParameterForm', 'draft,defaults,dirty,revision,fingerprint,overrides,loading,saving,refreshSavedParameters,inheritField,editField,discard,save')
const workspaceCode = await compile('AssemblyVersionWorkspace', 'adopt,state,versions,active,values,dirty,busy,defaultsBusy,defaultsPanel,defaultsExpanded,refreshSavedSettings,defaultsApplied,save')
let serial = 0
async function fixture(t: any, code: string, props: any) {
  const module = await import(`data:text/javascript;base64,${Buffer.from(code.replace('__PROPS__', JSON.stringify(props)) + `\n// fixture ${serial++}`).toString('base64')}`)
  t.after(() => module.unmountHooks.forEach((hook: () => void) => hook()))
  return module
}
const mode = 'assemble.final_loudness_mode', target = 'assemble.final_loudness_lufs'
const fields = [{ path: mode, label: '最终成片响度', type: 'select', group: '声音' }, { path: target, label: '成片目标响度', type: 'float', group: '声音' }]
function parameters(updated = false) {
  const defaults = { [mode]: updated ? 'uniform' : 'off', [target]: updated ? -14 : -16 }
  return { defaults, steps: { assemble: { fields, values: { ...defaults, [mode]: 'normalize' } } }, overridden: { [mode]: true },
    revision: updated ? 2 : 1, fingerprint: updated ? 'fp-2' : 'fp-1',
    parent_sources: { [mode]: { kind: 'global', frozen: true }, [target]: { kind: 'global', frozen: true } } }
}
function versions(updated = false) {
  return { fields, defaults: parameters(updated).defaults, versions: [{ id: 'dub', name: '配音版', dubbing: true, burn_subtitles: false, include_comments: false, overrides: {} }],
    revision: updated ? 2 : 1, fingerprint: updated ? 'fp-2' : 'fp-1', sources: parameters(updated).parent_sources, source: { kind: 'project' } }
}
const reply = (value: any) => new Response(JSON.stringify(value))
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
async function settle() { for (let i = 0; i < 24; i++) await Promise.resolve() }

test('应用最新基础后就地更新项目默认，恢复使用新 -14/uniform，已有项目覆盖继续保留', async t => {
  const reads: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => { reads.push({ url, options }); return reply(parameters(reads.length > 1)) })
  const page = await fixture(t, formCode, { uid: 'fixture', stepKey: 'assemble', refreshToken: 0 }); await settle()
  assert.equal(page.defaults.value[target], -16)
  page.fixtureProps.refreshToken++
  await settle()
  assert.equal(reads.length, 2); assert.equal(page.defaults.value[target], -14)
  assert.equal(page.defaults.value[mode], 'uniform'); assert.equal(page.draft.value[mode], 'normalize')
  page.inheritField(mode)
  assert.equal(page.draft.value[mode], 'uniform'); assert.equal(page.draft.value[target], -14)
  assert.equal(page.dirty.value, true)
  assert.equal(page.events.filter(([name]: any) => name === 'saved').length, 0)
  assert.ok(reads.every(({ options }) => !options.body))
})

test('其他工作区更新不覆盖参数草稿和提交指纹，放弃草稿后补读最新基础', async t => {
  let reads = 0
  t.mock.method(globalThis, 'fetch', async () => reply(parameters(++reads > 1)))
  const page = await fixture(t, formCode, { uid: 'fixture', stepKey: 'assemble', refreshToken: 0 }); await settle()
  page.editField(target, -12)
  page.fixtureProps.refreshToken++
  await settle()
  assert.equal(reads, 1); assert.equal(page.draft.value[target], -12)
  assert.equal(page.fingerprint.value, 'fp-1'); assert.equal(page.revision.value, 1)
  page.discard(); await settle()
  assert.equal(reads, 2); assert.equal(page.draft.value[target], -14)
  assert.equal(page.fingerprint.value, 'fp-2'); assert.equal(page.dirty.value, false)
})

test('连续更新取消旧回读，迟到旧响应不能把新默认恢复为 -16', async t => {
  const first = deferred<Response>(), second = deferred<Response>(), reads: any[] = []
  t.mock.method(globalThis, 'fetch', (_url: any, options: any) => {
    reads.push(options)
    return reads.length === 1 ? Promise.resolve(reply(parameters())) : reads.length === 2 ? first.promise : second.promise
  })
  const page = await fixture(t, formCode, { uid: 'fixture', stepKey: 'assemble', refreshToken: 0 }); await settle()
  page.fixtureProps.refreshToken++; await settle()
  page.fixtureProps.refreshToken++; await settle()
  assert.equal(reads[1].signal.aborted, true)
  second.resolve(reply(parameters(true))); await settle()
  first.resolve(reply(parameters())); await settle()
  assert.equal(page.defaults.value[target], -14); assert.equal(page.fingerprint.value, 'fp-2')
  assert.equal(page.loading.value, false)
})

test('保存中收到更新会延后回读，不改变当前提交对应的草稿与版本', async t => {
  let reads = 0
  t.mock.method(globalThis, 'fetch', async () => reply(parameters(++reads > 1)))
  const page = await fixture(t, formCode, { uid: 'fixture', stepKey: 'assemble', refreshToken: 0 }); await settle()
  page.saving.value = true
  page.fixtureProps.refreshToken++; await settle()
  assert.equal(reads, 1); assert.equal(page.fingerprint.value, 'fp-1')
  page.saving.value = false; await settle()
  assert.equal(reads, 2); assert.equal(page.defaults.value[target], -14)
})

test('切换项目强制读取新上下文，不保留旧项目脏稿或它排队的刷新', async t => {
  const reads: string[] = []
  t.mock.method(globalThis, 'fetch', async (url: any) => { reads.push(String(url)); return reply(parameters(reads.length > 1)) })
  const page = await fixture(t, formCode, { uid: 'fixture', stepKey: 'assemble', refreshToken: 0 }); await settle()
  page.editField(target, -12); page.fixtureProps.refreshToken++
  assert.equal(page.dirty.value, true)
  page.fixtureProps.uid = 'another-project'; await settle()
  assert.equal(reads.length, 2); assert.match(reads[1], /another-project\/step_params$/)
  assert.equal(page.draft.value[target], -14); assert.equal(page.dirty.value, false)
  assert.equal(page.fingerprint.value, 'fp-2')
})

test('切换项目取消旧上下文请求，迟到响应不会覆盖新项目参数', async t => {
  const old = deferred<Response>(), reads: any[] = []
  t.mock.method(globalThis, 'fetch', (url: any, options: any) => {
    reads.push({ url, options }); return reads.length === 1 ? old.promise : Promise.resolve(reply(parameters(true)))
  })
  const page = await fixture(t, formCode, { uid: 'fixture', stepKey: 'assemble', refreshToken: 0 })
  page.fixtureProps.uid = 'another-project'; await settle()
  assert.equal(reads[0].options.signal.aborted, true)
  old.resolve(reply(parameters())); await settle()
  assert.equal(page.defaults.value[target], -14); assert.equal(page.fingerprint.value, 'fp-2')
  assert.equal(page.loading.value, false)
})

test('项目参数保存后版本工作台回读新默认，保留未保存版本精调及原提交指纹', async t => {
  const reads: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: any, options: any) => { reads.push({ url, options }); return reply(versions(true)) })
  const page = await fixture(t, workspaceCode, { uid: 'fixture', refreshToken: 0 })
  page.adopt(versions())
  page.active.value.overrides[target] = -12
  page.fixtureProps.refreshToken++; await settle()
  assert.equal(reads.length, 1); assert.equal(page.state.value.defaults[target], -14)
  assert.equal(page.active.value.overrides[target], -12); assert.equal(page.dirty.value, true)
  assert.equal(page.state.value.revision, 1); assert.equal(page.state.value.fingerprint, 'fp-1')
  assert.equal(page.events.filter(([name]: any) => name === 'saved').length, 0)
})

test('工作台提交中收到更新在完成后补读，旧后台读不能覆盖这次结果', async t => {
  const old = deferred<Response>(), reads: any[] = []
  t.mock.method(globalThis, 'fetch', (url: any, options: any) => {
    reads.push({ url, options }); return reads.length === 1 ? old.promise : Promise.resolve(reply(versions(true)))
  })
  const page = await fixture(t, workspaceCode, { uid: 'fixture', refreshToken: 0 }); page.adopt(versions())
  page.refreshSavedSettings(); await settle()
  page.busy.value = true; page.fixtureProps.refreshToken++; await settle()
  assert.equal(reads.length, 1)
  page.busy.value = false; await settle()
  assert.equal(reads.length, 2); assert.equal(reads[0].options.signal.aborted, true)
  old.resolve(reply(versions())); await settle()
  assert.equal(page.state.value.defaults[target], -14); assert.equal(page.state.value.fingerprint, 'fp-2')
})

test('参数区入口复用同一基础检查抽屉且遵守锁定，应用回执继续向父页面通知', async t => {
  t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  const page = await fixture(t, workspaceCode, { uid: 'fixture', refreshToken: 0 }); page.adopt(versions())
  let opened = 0
  page.defaultsPanel.value = { inspect: () => { opened++ } }
  page.exposed.openDefaults()
  assert.equal(opened, 1); assert.equal(page.defaultsExpanded.value, true)
  page.active.value.overrides[target] = -12
  page.exposed.openDefaults(); assert.equal(opened, 1)
  page.active.value.overrides = {}
  page.defaultsApplied(versions(true))
  assert.equal(page.state.value.defaults[target], -14)
  assert.equal(page.events.filter(([name]: any) => name === 'saved').length, 1)
})
