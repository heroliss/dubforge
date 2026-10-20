import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { parse } from '@vue/compiler-sfc'
import { compile, parse as parseTemplate } from '@vue/compiler-dom'
import * as Vue from 'vue'
import { setup } from '@css-render/vue3-ssr'
import { NSpace } from 'naive-ui'

// 编译实际工具栏并挂载真实 NSpace；按钮仅替换视觉包装，保留全部条件、Fragment、事件与禁用状态。
const source = await readFile(new URL('../src/views/StepView.vue', import.meta.url), 'utf8')
const template = parse(source).descriptor.template!.content
function findToolbar(node: any): any {
  if (node.type === 1 && node.props.some((prop: any) => prop.type === 6 && prop.name === 'class'
    && prop.value?.content.split(/\s+/).includes('step-action-toolbar'))) return node
  for (const child of node.children || []) { const found = findToolbar(child); if (found) return found }
  return null
}
const toolbarNode = findToolbar(parseTemplate(template))
assert.ok(toolbarNode, '测试必须挂载实际步骤工具栏，不能复制一份测试模板')
const toolbarTemplate = template.slice(toolbarNode.loc.start.offset, toolbarNode.loc.end.offset)
const render = new Function('Vue', compile(toolbarTemplate, { mode: 'function', prefixIdentifiers: true }).code)(Vue)

type HostNode = { type: string; text: string; props: Record<string, any>; children: HostNode[]; parent: HostNode | null }
const host = (type: string, text = ''): HostNode => ({ type, text, props: {}, children: [], parent: null })
function remove(node: HostNode) {
  if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1)
  node.parent = null
}
function mountToolbar(key: string) {
  // insert 实现实际 DOM 的移动语义，不能用只追加的假宿主制造重复节点。
  const renderer = Vue.createRenderer<HostNode, HostNode>({
    createElement: tag => host(tag), createText: text => host('#text', text), createComment: text => host('#comment', text),
    insert(node, parent, anchor = null) {
      remove(node)
      const index = anchor ? parent.children.indexOf(anchor) : -1
      parent.children.splice(index < 0 ? parent.children.length : index, 0, node); node.parent = parent
    }, remove,
    setText(node, text) { node.text = text },
    setElementText(node, text) { for (const child of node.children) child.parent = null; node.children = []; node.text = text },
    parentNode: node => node.parent, nextSibling: node => node.parent?.children[node.parent.children.indexOf(node) + 1] || null,
    patchProp(node, name, _previous, value) { node.props[name] = value },
  })
  const actions: string[] = [], noop = () => {}, state: any = Vue.reactive({
    key, uid: 'isolated-toolbar', actionMode: 'run', running: false, pausing: false, pauseBusy: false,
    step: { status: 'failed', cancelling: false }, nav: { archived: false, busy: false, chainRunning: false, chain: null, title: '隔离工具栏', poll: noop },
    route: { query: {} }, router: { replace: noop }, localRunOptions: [], canRun: true, dirMissing: false,
    busy: false, cancelPending: false, forceBusy: false, dubbingBusy: false, dubbingDirty: false,
    parametersDirty: false, reviewDirty: false, reviewBusy: false, assemblyPending: false,
    dubbingPanel: { canRegenerateAll: true }, actionBlockers: [], dependencyActionLabel: () => '同步依赖',
    stoppingLabel: () => '正在停止', revealStep: noop, setPause: (pause: boolean) => actions.push(pause ? 'pause' : 'resume'),
    stopDialog: () => actions.push('stop'), forceDialog: () => actions.push('force'),
    chooseOutputs: noop, runWithDependencies: noop, run: noop, rerunDialog: noop, localRunAction: noop,
  })
  const Button = Vue.defineComponent({ inheritAttrs: false, props: ['disabled'],
    setup: (props, { attrs, slots }) => () => Vue.h('button', { ...attrs, disabled: props.disabled }, slots.default?.()),
  })
  const Tag = Vue.defineComponent({ setup: (_props, { slots }) => () => Vue.h('span', slots.default?.()) })
  const Dropdown = Vue.defineComponent({ inheritAttrs: false, setup: (_props, { slots }) => () => Vue.h('div', slots.default?.()) })
  const app = renderer.createApp({
    components: { NButton: Button, NDropdown: Dropdown, NSpace, NTag: Tag, BatchWorkflowResume: {
      props: ['label', 'disabled'], setup: (props: any) => () => Vue.h('button', { disabled: props.disabled }, props.label),
    } }, setup: () => state, render,
  })
  setup(app)
  const root = host('root'), warnings: string[] = []
  app.config.warnHandler = message => warnings.push(message)
  app.mount(root)
  const textOf = (node: HostNode): string => node.type === '#comment' ? '' : node.text + node.children.map(textOf).join('')
  const controls = () => {
    const rows: HostNode[] = []
    const visit = (node: HostNode) => { if (node.type === 'button') rows.push(node); for (const child of node.children) visit(child) }
    visit(root)
    return rows
  }
  const label = (node: HostNode) => textOf(node).replace(/\s+/g, ' ').trim()
  const buttons = () => controls().map(label)
  const click = (text: string) => {
    const matches = controls().filter(node => label(node) === text)
    assert.equal(matches.length, 1, `${text} 必须只有一个可操作节点`)
    assert.ok(!matches[0].props.disabled)
    matches[0].props.onClick({ type: 'click' })
  }
  return { state, buttons, click, actions, warnings, unmount: () => app.unmount() }
}

test('真实挂载工具栏从失败切到运行再结束，只有当前分支控件，不残留更多或重复停止', async t => {
  const fixture = mountToolbar('assemble'); t.after(fixture.unmount)
  assert.deepEqual(fixture.buttons(), ['📁', '选择成品方案', '更多运行操作'])
  Object.assign(fixture.state, { actionMode: 'running', running: true })
  fixture.state.step.status = 'running'; await Vue.nextTick()
  assert.deepEqual(fixture.buttons(), ['📁', '⏸ 暂停', '⏹ 停止'])
  Object.assign(fixture.state, { actionMode: 'run', running: false })
  fixture.state.step.status = 'failed'; await Vue.nextTick()
  assert.deepEqual(fixture.buttons(), ['📁', '选择成品方案', '更多运行操作'])
})

test('实际工具栏运行到核验再到完整终态，核验时不保留停止/暂停/重跑入口',async t=>{
  const fixture=mountToolbar('assemble');t.after(fixture.unmount)
  Object.assign(fixture.state,{actionMode:'running',running:true});fixture.state.step.status='running';await Vue.nextTick()
  assert.ok(fixture.buttons().includes('⏹ 停止'))
  Object.assign(fixture.state,{actionMode:'checking',running:false});fixture.state.step.status='checking';fixture.state.nav.busy=true;await Vue.nextTick()
  assert.deepEqual(fixture.buttons(),['📁','产物核验中'])
  assert.throws(()=>fixture.click('产物核验中'))
  Object.assign(fixture.state,{actionMode:'fresh-done',running:false});fixture.state.step.status='done';fixture.state.nav.busy=false;await Vue.nextTick()
  assert.deepEqual(fixture.buttons(),['📁','选择成品方案','更多运行操作'])
  assert.deepEqual(fixture.actions,[])
})

for (const key of ['assemble', 'tts', 'split']) test(`${key}工具栏反复切换执行、暂停、取消与终态，节点和事件保持唯一`, async t => {
  const fixture = mountToolbar(key); t.after(fixture.unmount)
  const { state } = fixture
  const change = async (actionMode: string, status: string, extra: any = {}) => {
    const running = ['running', 'queued'].includes(status)
    Object.assign(state, { actionMode, running, pausing: false, cancelPending: false, ...extra })
    Object.assign(state.step, { status, cancelling: false })
    state.nav.busy = running; await Vue.nextTick()
  }
  for (let iteration = 0; iteration < 10; iteration++) {
    await change('running', 'queued')
    assert.deepEqual(fixture.buttons(), ['📁', '⏸ 暂停', '⏹ 停止'])
    await change('running', 'running')
    state.nav.chainRunning = iteration % 2 === 0; await Vue.nextTick()
    assert.deepEqual(fixture.buttons(), ['📁', '⏸ 暂停', '⏹ 停止'])
    fixture.click('⏸ 暂停'); fixture.click('⏹ 停止')
    state.pausing = true; await Vue.nextTick()
    assert.deepEqual(fixture.buttons(), ['📁', '▶ 继续', '⏹ 停止']); fixture.click('▶ 继续')
    await change('cancelling', 'running')
    state.step.cancelling = true; await Vue.nextTick()
    assert.deepEqual(fixture.buttons(), ['📁', '⛔ 强制结束']); fixture.click('⛔ 强制结束')
    state.cancelPending = true; await Vue.nextTick()
    assert.deepEqual(fixture.buttons(), ['📁'])
    await change('run', 'failed')
    const failed = key === 'assemble' ? ['📁', '选择成品方案', '更多运行操作']
      : key === 'tts' ? ['📁', '补齐未完成配音', '生成全部配音…', '更多运行操作']
        : ['📁', '重新运行本步', '更多运行操作']
    assert.deepEqual(fixture.buttons(), failed)
    await change('fresh-done', 'done')
    assert.deepEqual(fixture.buttons(), key === 'assemble' ? ['📁', '选择成品方案', '更多运行操作']
      : key === 'tts' ? ['📁', '生成全部配音…', '更多运行操作'] : ['📁', '重做本步产物…', '更多运行操作'])
  }
  assert.deepEqual(fixture.actions, Array.from({ length: 10 }, () => ['pause', 'stop', 'resume', 'force']).flat())
  assert.deepEqual(fixture.warnings, [])
})

test('暂停链与依赖恢复分支切到执行后不保留恢复或重试操作', async t => {
  const fixture = mountToolbar('tts'); t.after(fixture.unmount)
  const { state } = fixture
  Object.assign(state, { actionMode: 'chain-hold', running: false })
  state.nav.chain = { remaining: ['tts', 'assemble'] }; await Vue.nextTick()
  assert.deepEqual(fixture.buttons(), ['📁', '检查恢复方案…', '仅补齐配音', '更多运行操作'])
  Object.assign(state, { actionMode: 'deps', running: false }); await Vue.nextTick()
  assert.deepEqual(fixture.buttons(), ['📁', '同步全项目字幕与配音…', '更多运行操作'])
  Object.assign(state, { actionMode: 'running', running: true }); state.step.status = 'running'; await Vue.nextTick()
  assert.deepEqual(fixture.buttons(), ['📁', '⏸ 暂停', '⏹ 停止'])
})
