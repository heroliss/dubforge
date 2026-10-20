import assert from 'node:assert/strict'
import test, { before, after, beforeEach, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createPinia, disposePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { NMessageProvider, NDialogProvider, NLayout, NLayoutSider, NMenu } from 'naive-ui'

// 实际页面、导航 Store 和事件处理器，所有请求只访问离线桩。
let server: ViteDevServer, Projects: any, Layout: any, useNavStore: any, execution: any
const reply = (data: any, ok = true, status = 200) => ({ ok, status, json: async () => data })
const entry = (uid: string, fields: any = {}) => ({ uid, title: `项目${uid}`, status: 'created', steps: [],
  source_kind: 'local', dir_exists: true, archived: false, archived_at: null, ...fields })
function deferred() {
  let resolve: any
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve() }
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Projects = (await server.ssrLoadModule('/src/views/ProjectsView.vue')).default
  Layout = (await server.ssrLoadModule('/src/layouts/MainLayout.vue')).default
  ;({ useNavStore } = await server.ssrLoadModule('/src/stores/nav.ts'))
  execution = await server.ssrLoadModule('/src/utils/configurationEditability.ts')
  mock.method(globalThis, 'fetch', () => { throw new Error('禁止未声明的真实网络请求') })
})
beforeEach(() => execution.setNavigationExecution('', false, true))
after(async () => { mock.restoreAll(); await server.close() })

async function render(t: any, component = Projects, seed: (page: any, nav: any) => void = () => {}, path = '/', navSeed: any = {}) {
  const pinia = createPinia(), nav = useNavStore(pinia)
  nav.$patch(navSeed)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', name: 'projects', component: { render: () => null } },
    { path: '/projects/:uid', name: 'pipeline', component: { render: () => null } },
    { path: '/settings', name: 'settings', component: { render: () => null } },
    { path: '/:pathMatch(.*)*', component: { render: () => null } },
  ] })
  await router.push(path)
  let page: any
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(component))) })
  for (const [name, value] of Object.entries({ 'n-layout': NLayout, 'n-layout-sider': NLayoutSider, 'n-menu': NMenu })) app.component(name, value)
  app.use(pinia); app.use(router); setup(app)
  app.mixin({ created() { if (this.$options.__name === (component === Projects ? 'ProjectsView' : 'MainLayout')) {
    page = this.$.setupState; seed(page, nav)
  } } })
  const html = await renderToString(app)
  t.after(() => { nav.reset(); disposePinia(pinia) })
  return { page, nav, html }
}

test('默认只读当前项目，切换归档取消慢列表请求、清选择，旧响应不能复活', async t => {
  const current = deferred(), archived = deferred(), requests: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (String(url).startsWith('/api/configuration/')) return Promise.resolve(reply({ series: [] }))
    requests.push({ url: String(url), options })
    return String(url).includes('?archive=archived') ? archived.promise : current.promise
  })
  const { page } = await render(t, Projects, page => { page.projects = [entry('old')]; page.sel = new Set(['old']); page.searchText = '旧筛选' })
  const old = page.load()
  assert.equal(requests[0].url, '/api/projects')
  page.switchArchiveScope('archived')
  assert.equal(requests[0].options.signal.aborted, true)
  assert.equal(requests[1].url, '/api/projects?archive=archived&view=summary')
  assert.equal(page.sel.size, 0); assert.equal(page.projects.length, 0); assert.equal(page.searchText, '')
  archived.resolve(reply([entry('saved', { archived: true })])); await flush()
  current.resolve(reply([entry('old')])); await old
  assert.deepEqual(page.projects.map((item: any) => item.uid), ['saved'])
  assert.equal(page.loading, false)
})

test('归档列表只提供查看和恢复，旧选择或旧卡片不能发起制作与模板绑定', async t => {
  const archived = entry('a', { archived: true, archived_at: '2026-10-07T12:00:00Z', chain: { state: 'held', remaining: ['translate'] },
    steps: [{ key: 'translate', label: '翻译', status: 'failed', selected: true, fresh: false }] })
  const failed = entry('failed', { archived: true, status: 'failed', chain: null, workflow_status: 'failed',
    steps: [{ key: 'translate', label: '翻译', status: 'failed', selected: true, fresh: false }] })
  const { page, html } = await render(t, Projects, page => {
    page.archiveScope = 'archived'; page.projects = [archived, failed]; page.sel = new Set(['a']); page.loading = false
  })
  const opened: any[] = []; page.stepRun = { open: (...args: any[]) => opened.push(args) }
  page.runProject(archived); page.runSelected(); page.runAllPending(); page.openStepRun([entry('old')], '旧快照')
  assert.equal(opened.length, 0)
  assert.equal(page.recoveryProjects.length, 0); assert.equal(page.pendingInView.length, 0)
  assert.match(html, /已归档/); assert.match(html, /恢复项目/); assert.match(html, /恢复所选（1）/)
  assert.doesNotMatch(html, /运行或继续…|运行或继续所选|运行或继续待制作|检查恢复方案|绑定系列|顺序发布/)
  assert.ok(!page.projectMenu(archived).some((item: any) => item.key === 'rename' || item.key === 'archive'))
})

test('批量归档分项反馈，成功移出并清选择，失败保留；双击只发送一次', async t => {
  const pending = deferred(), calls: any[] = []
  const { page, nav } = await render(t, Projects, page => {
    page.projects = [entry('one'), entry('two'), entry('running', { publication: { status: 'uploading' } })]
    page.sel = new Set(['one', 'two', 'running']); page.loading = false
  }, '/', { uid: 'one', title: '项目one' })
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url: String(url), method: options?.method, body: JSON.parse(String(options?.body)) })
    return String(url).includes('/one/') ? pending.promise : Promise.resolve(reply({ message: '项目刚刚开始任务' }, false, 409))
  })
  const job = page.changeSelectedArchive(); await page.changeSelectedArchive()
  assert.equal(page.archiving, true); assert.equal(calls.length, 1)
  page.switchArchiveScope('archived'); assert.equal(page.archiveScope, 'active', '发送过程中范围稳定')
  pending.resolve(reply({ ok: true, uid: 'one', archived: true, archived_at: '2026-10-07T12:00:00Z' })); await job
  assert.deepEqual(calls.map(call => call.url), ['/api/projects/one/archive', '/api/projects/two/archive'])
  assert.ok(calls.every(call => call.method === 'PUT' && call.body.archived === true))
  assert.deepEqual(page.projects.map((item: any) => item.uid), ['two', 'running'])
  assert.deepEqual([...page.sel], ['two', 'running']); assert.equal(page.archiveFailures.length, 2)
  assert.equal(page.archiveResults.length, 3); assert.equal(page.archiving, false)
  assert.equal(nav.archived, true); assert.equal(nav.archivedAt, '2026-10-07T12:00:00Z')
  assert.match(page.archiveFailures[1].error, /任务正在执行/)
})

test('归档成功取消旧轮询；旧列表迟到不能把成功条目重新显示', async t => {
  const slow = deferred(), requests: any[] = []
  const { page } = await render(t, Projects, page => { page.projects = [entry('one')]; page.sel = new Set(['one']) })
  t.mock.method(globalThis, 'fetch', (url, options) => {
    requests.push({ url: String(url), options })
    return options?.method === 'PUT' ? Promise.resolve(reply({ ok: true, archived: true, archived_at: null })) : slow.promise
  })
  const read = page.load(); await page.changeSelectedArchive()
  assert.equal(requests[0].options.signal.aborted, true)
  slow.resolve(reply([entry('one')])); await read
  assert.equal(page.projects.length, 0); assert.equal(page.sel.size, 0)
})

test('批量部分失败的真实界面显示成功数量与逐项原因，失败选择保留可重试', async t => {
  const { html } = await render(t, Projects, page => {
    page.projects = [entry('failed')]; page.sel = new Set(['failed']); page.loading = false
    page.archiveResults = [{ uid: 'done', title: '完成项目', ok: true },
      { uid: 'failed', title: '等待结束的项目', ok: false, error: '项目有上传正在执行' }]
  })
  assert.match(html, /归档结果：1 个成功/); assert.match(html, /1 个未完成/)
  assert.match(html, /等待结束的项目：项目有上传正在执行/)
  assert.match(html, /归档所选（1）/)
})

test('单项目恢复沿用独立入口，无目录仍可恢复，失败时保留归档与选择', async t => {
  const saved = entry('saved', { archived: true, dir_exists: false })
  const { page } = await render(t, Projects, page => { page.archiveScope = 'archived'; page.projects = [saved]; page.sel = new Set(['saved']); page.loading = false })
  const calls: any[] = []
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, body: JSON.parse(String(options?.body)) })
    return calls.length === 1 ? reply({ message: '磁盘暂时不可用' }, false, 500) : reply({ ok: true, archived: false, archived_at: null })
  })
  await page.changeSelectedArchive()
  assert.equal(page.projects.length, 1); assert.equal(page.sel.size, 1); assert.match(page.archiveFailures[0].error, /磁盘暂时不可用/)
  await page.changeSelectedArchive()
  assert.equal(page.projects.length, 0); assert.equal(page.sel.size, 0)
  assert.ok(calls.every(call => call.body.archived === false))
})

test('直接链接归档提示只在对应项目页出现，全局页旧导航快照不显示恢复提示', async t => {
  t.mock.method(globalThis, 'fetch', () => new Promise(() => {}))
  const snapshot = { uid: 'saved', title: '已归档工程', archived: true }
  const project = await render(t, Layout, () => {}, '/projects/saved', snapshot)
  assert.match(project.html, /data-testid="project-archived"/); assert.match(project.html, /恢复项目/)
  const global = await render(t, Layout, () => {}, '/', snapshot)
  assert.equal(global.nav.suspended, true)
  assert.doesNotMatch(global.html, /data-testid="project-archived"|恢复项目/)
  assert.match(global.html, /已归档工程/)
})

test('主布局恢复防止重复提交，成功同步导航并重新读取状态；旧导航响应不能反转结果', async t => {
  const pending = deferred(), reads: any[] = [], writes: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (options?.method === 'PUT') { writes.push({ url: String(url), body: JSON.parse(String(options.body)) }); return pending.promise }
    const next = deferred(); reads.push({ url: String(url), options, ...next }); return next.promise
  })
  const { page, nav } = await render(t, Layout, () => {}, '/projects/saved', { uid: 'saved', title: '已归档工程', archived: true, suspended: true })
  const old = reads.find(call => call.url.endsWith('/navigation'))
  const restoring = page.restoreArchivedProject(); await page.restoreArchivedProject()
  assert.equal(writes.length, 1); assert.equal(page.restoringArchive, true)
  assert.deepEqual(writes[0], { url: '/api/projects/saved/archive', body: { archived: false } })
  pending.resolve(reply({ ok: true, archived: false, archived_at: null })); await flush()
  assert.equal(old.options.signal.aborted, true); assert.equal(nav.archived, false)
  old.resolve(reply({ title: '已归档工程', archived: true })); await flush()
  assert.equal(nav.archived, false)
  // 只有旧导航响应需要刻意延迟；其余状态读取可能被恢复轮询复用，
  // 不按接口数量截取，以免新增评论状态等只读来源后留下永不完成的请求。
  for (const read of reads.filter(read => read !== old)) read.resolve(reply(read.url.endsWith('/steps') ? [] : read.url.endsWith('/navigation')
    ? { title: '恢复的工程', archived: false } : {}))
  await restoring
  assert.equal(page.restoringArchive, false); assert.equal(nav.title, '恢复的工程')
  assert.equal(page.archiveFeedback.type, 'success'); assert.match(page.archiveFeedback.message, /项目已恢复/)
})

test('主布局恢复失败保留只读状态和错误，切到新项目或reset清掉归档状态', async t => {
  t.mock.method(globalThis, 'fetch', (_url, options) => options?.method === 'PUT'
    ? Promise.resolve(reply({ message: '项目仍在执行上传' }, false, 409)) : new Promise(() => {}))
  const { page, nav } = await render(t, Layout, () => {}, '/projects/saved', { uid: 'saved', archived: true, archivedAt: '2026-10-07' })
  await page.restoreArchivedProject()
  assert.equal(nav.archived, true); assert.equal(page.archiveFeedback.type, 'error'); assert.match(page.archiveFeedback.message, /执行上传/)
  nav.enter('other'); assert.equal(nav.archived, false); assert.equal(nav.archivedAt, null)
  nav.$patch({ archived: true, archivedAt: '2026-10-07' }); nav.reset()
  assert.equal(nav.archived, false); assert.equal(nav.archivedAt, null)
})

test('重新归档隐藏旧恢复成功提示，当前已恢复项目仍显示成功反馈，错误保持可见', async t => {
  const feedback = { uid: 'saved', type: 'success', message: '项目已恢复，可以继续编辑、制作和发布。' }
  const snapshot = { uid: 'saved', title: '同一个项目', archived: false }
  const restored = await render(t, Layout, page => { page.archiveFeedback = { ...feedback } }, '/projects/saved', snapshot)
  assert.match(restored.html, /项目已恢复，可以继续编辑、制作和发布。/)
  assert.doesNotMatch(restored.html, /data-testid="project-archived"/)
  const rearchived = await render(t, Layout, (page, nav) => {
    page.archiveFeedback = { ...feedback }
    nav.archiveChanged('saved', true, '2026-10-07T12:00:00Z')
  }, '/projects/saved', snapshot)
  assert.match(rearchived.html, /data-testid="project-archived"/)
  assert.doesNotMatch(rearchived.html, /项目已恢复，可以继续编辑、制作和发布。/)
  const failed = await render(t, Layout, page => {
    page.archiveFeedback = { uid: 'saved', type: 'error', message: '恢复失败，请稍后重试' }
  }, '/projects/saved', { ...snapshot, archived: true })
  assert.match(failed.html, /恢复失败，请稍后重试/)
})

test('导航从接口取得真实归档状态，archiveChanged只修改对应项目并保留文件步骤', async t => {
  const reads: any[] = []
  t.mock.method(globalThis, 'fetch', (url, options) => {
    reads.push({ url, options })
    return Promise.resolve(reply(String(url).endsWith('/navigation') ? {
      title: '已有结果的归档项目', archived: true, archived_at: '2026-10-07T12:00:00Z',
    } : String(url).endsWith('/steps') ? [{ key: 'assemble', selected: true, status: 'done', fresh: true }] : {}))
  })
  const { nav } = await render(t)
  nav.enter('saved'); await flush()
  assert.equal(nav.archived, true); assert.equal(nav.archivedAt, '2026-10-07T12:00:00Z')
  assert.equal(nav.stepMap.assemble.fresh, true)
  nav.archiveChanged('different', false); assert.equal(nav.archived, true)
  nav.archiveChanged('saved', false)
  assert.equal(nav.archived, false); assert.equal(nav.archivedAt, null)
  assert.equal(nav.stepMap.assemble.fresh, true, '恢复不修改任何产物或步骤状态')
})

test('归档项目可删除，确认说明安全回收和找回；重复确认只提交一次', async t => {
  const row = entry('saved', { archived: true })
  const { page, nav } = await render(t, Projects, page => {
    page.archiveScope = 'archived'; page.projects = [row]; page.sel = new Set(['saved']); page.loading = false
  }, '/', { uid: 'saved' })
  const dialogs: any[] = [], calls: any[] = [], pending = deferred()
  t.mock.method(page.dialog, 'warning', (options: any) => { dialogs.push(options) })
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url: String(url), method: options?.method })
    return options?.method === 'DELETE' ? pending.promise : Promise.resolve(reply([]))
  })
  assert.ok(page.projectMenu(row).some((item: any) => item.key === 'delete'))
  page.remove(row); page.remove(row)
  assert.equal(dialogs.length, 1)
  assert.match(dialogs[0].title, /已归档项目/)
  assert.match(dialogs[0].content, /无法回收时会停止删除/)
  assert.match(dialogs[0].content, /找回项目/)
  assert.doesNotMatch(dialogs[0].content, /将永久删除/)
  const job = dialogs[0].onPositiveClick()
  await dialogs[0].onPositiveClick(); page.remove(row)
  page.switchArchiveScope('active')
  assert.equal(page.archiveScope, 'archived'); assert.equal(page.deletingUids.has('saved'), true)
  assert.equal(calls.filter(call => call.method === 'DELETE').length, 1)
  pending.resolve(reply({ ok: true })); assert.equal(await job, true)
  assert.equal(page.deletingUids.size, 0); assert.equal(page.projects.length, 0)
  assert.equal(page.sel.size, 0); assert.equal(nav.uid, '')
})

test('回收失败或网络错误保留归档项目与选择，取消后可以重新尝试', async t => {
  for (const network of [false, true]) {
    const row = entry('saved', { archived: true })
    const { page } = await render(t, Projects, page => {
      page.archiveScope = 'archived'; page.projects = [row]; page.sel = new Set(['saved']); page.loading = false
    })
    const dialogs: any[] = []
    t.mock.method(page.dialog, 'warning', (options: any) => { dialogs.push(options) })
    t.mock.method(globalThis, 'fetch', async () => {
      if (network) throw new Error('网络中断')
      return reply({ message: '无法移入回收站' }, false, 400)
    })
    page.remove(row); assert.equal(await dialogs[0].onPositiveClick(), false)
    assert.equal(page.projects[0].uid, 'saved'); assert.equal(page.sel.has('saved'), true)
    assert.equal(page.deletingUids.size, 0); assert.equal(page.loading, false)
    dialogs[0].onNegativeClick(); page.remove(row); assert.equal(dialogs.length, 2)
  }
})

test('目录缺失时删除只清登记，不误报文件已进入回收站', async t => {
  const row = entry('missing', { archived: true, dir_exists: false })
  const { page } = await render(t)
  const dialogs: any[] = []
  t.mock.method(page.dialog, 'warning', (options: any) => { dialogs.push(options) })
  page.remove(row)
  assert.match(dialogs[0].content, /只清理本地登记/)
  assert.equal(dialogs[0].positiveText, '清理登记')
  dialogs[0].onNegativeClick()
})

test('找回项目扫描执行中拒绝重复确认，只提交一次索引重建', async t => {
  const { page } = await render(t)
  const dialogs: any[] = [], calls: string[] = [], pending = deferred()
  t.mock.method(page.dialog, 'info', (options: any) => { dialogs.push(options) })
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push(String(url))
    return options?.method === 'POST' ? pending.promise : Promise.resolve(reply([]))
  })
  page.recoverProjects()
  const first = dialogs[0].onPositiveClick()
  assert.equal(await dialogs[0].onPositiveClick(), false)
  page.recoverProjects(); assert.equal(dialogs.length, 1)
  assert.deepEqual(calls, ['/api/settings/rebuild_db'])
  pending.resolve(reply({ projects: 1, steps: 8 })); await first
  assert.equal(page.recovering, false)
  assert.equal(calls.filter(path => path === '/api/settings/rebuild_db').length, 1)
})

test('放弃导入收到HTTP失败回执保留项目、文件和原始错误，重试成功后才清理',async t=>{
  const {page}=await render(t)
  page.newUid='import-fixture';page.pendingFile={name:'source.mp4',size:16};page.ingestErr='原始上传失败';page.showNew=true
  page.form={source_kind:'local',source_url:'',title:'未完成导入'}
  const file=page.pendingFile,messages:string[]=[],requests:any[]=[]
  t.mock.method(page.message,'info',(text:string)=>messages.push(text))
  t.mock.method(globalThis,'fetch',async(url,options)=>{requests.push({url:String(url),method:options?.method});return reply({message:'服务器拒绝删除'},false,500)})
  await page.abortIngest()
  assert.equal(page.newUid,'import-fixture');assert.equal(page.pendingFile,file)
  assert.equal(page.ingestErr,'原始上传失败');assert.equal(page.showNew,true)
  assert.equal(page.form.title,'未完成导入');assert.match(page.ingestAbortError,/服务器拒绝删除/)
  assert.equal(page.abortingIngest,false);assert.deepEqual(messages,[])
  t.mock.method(globalThis,'fetch',async(url,options)=>{requests.push({url:String(url),method:options?.method});return reply({deleted:true})})
  await page.abortIngest()
  assert.equal(page.newUid,null);assert.equal(page.pendingFile,null);assert.equal(page.showNew,false)
  assert.equal(page.ingestErr,'');assert.equal(page.ingestAbortError,'')
  assert.equal(messages.length,1);assert.match(messages[0],/项目已删除/)
  assert.deepEqual(requests,[{url:'/api/projects/import-fixture',method:'DELETE'},{url:'/api/projects/import-fixture',method:'DELETE'}])
})

test('放弃导入等待回执时防止重复删除和重新导入，网络失败仍可重试',async t=>{
  const {page}=await render(t),pending=deferred(),requests:string[]=[],messages:string[]=[]
  page.newUid='import-fixture';page.pendingFile={name:'source.mp4',size:16};page.ingestErr='上传失败';page.showNew=true
  const file=page.pendingFile
  t.mock.method(page.message,'info',(text:string)=>messages.push(text))
  t.mock.method(globalThis,'fetch',(url)=>{requests.push(String(url));return pending.promise})
  const deleting=page.abortIngest()
  assert.equal(page.abortingIngest,true);assert.equal(page.newUid,'import-fixture');assert.equal(page.pendingFile,file)
  await page.abortIngest();await page.runIngest();await page.create()
  assert.deepEqual(requests,['/api/projects/import-fixture'])
  pending.resolve(reply({deleted:true}));await deleting
  assert.equal(page.abortingIngest,false);assert.equal(messages.length,1)
  page.newUid='retry-fixture';page.pendingFile=file;page.ingestErr='上传失败';page.showNew=true
  t.mock.method(globalThis,'fetch',async()=>{throw new Error('Failed to fetch')})
  await page.abortIngest()
  assert.equal(page.newUid,'retry-fixture');assert.equal(page.pendingFile,file);assert.equal(page.showNew,true)
  assert.ok(page.ingestAbortError);assert.equal(page.abortingIngest,false);assert.equal(messages.length,1)
})
