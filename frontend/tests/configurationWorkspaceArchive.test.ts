import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { NMessageProvider, NDialogProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, Workspace: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)), configFile: false,
    plugins: [vue()], appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Workspace = (await server.ssrLoadModule('/src/components/ConfigurationWorkspace.vue')).default
})
after(() => server.close())

const active = () => ({ key: 'series:active', name: '正在使用的系列', revision: 3, archived: false, project_count: 2 })
const archived = (count = 0) => ({ key: 'series:archived', name: '以前的系列', revision: 5, archived: true, project_count: count })
const backup = () => ({ backup_id: 'a'.repeat(32), key: 'series:archived', name: '以前的系列', revision: 5,
  archived: true, deleted_at: '2026-10-08T08:00:00Z', restorable: true })
const response = (data: any, ok = true) => ({ ok, json: async () => data })

async function render(t: any, scope = 'global', seed: (page: any) => void = () => {}) {
  let page: any
  const dialogs: any[] = [], selections: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string) => response(url.endsWith('/catalog') ? { accounts: [] }
    : { key: scope, name: scope === 'series:archived' ? '以前的系列' : '全局默认', archived: scope === 'series:archived',
      overrides: {}, defaults: { 'assembly.versions': [] }, sections: {}, version_fields: [] }))
  const Probe = defineComponent({ setup() {
    useDialog().warning = ((options: any) => { dialogs.push(options); return { destroy() {} } }) as any
    return () => h(Workspace, { scope, onSelect: (key: string) => selections.push({ key, locked: page.locked }) })
  } })
  const app = createSSRApp({ render: () => h(NMessageProvider, () => h(NDialogProvider, () => h(Probe))) })
  setup(app)
  app.mixin({ created() {
    if (this.$options.__name === 'ConfigurationWorkspace') {
      page = this.$.setupState; page.rows = [active(), archived()]; seed(page)
    }
    if (this.$options.__name === 'ConfigurationForm') {
      this.$.setupState.adopt({ key: scope, name: scope === 'series:archived' ? '以前的系列' : '全局默认',
        archived: scope === 'series:archived', overrides: {}, defaults: { 'assembly.versions': [] }, sections: {}, version_fields: [] })
      this.$.setupState.loading = false
    }
  } })
  const html = await renderToString(app)
  await new Promise(resolve => setImmediate(resolve))
  return { page, html, dialogs, selections }
}

test('活动模板独立展示；归档与备份入口在新建下方默认折叠', async t => {
  const { html } = await render(t)
  assert.match(html, /正在使用的系列/)
  assert.doesNotMatch(html, /以前的系列/)
  assert.match(html, /已归档模板[^<]*<span[^>]*>· 1/)
  assert.match(html, /找回已删除模板/)
  assert.doesNotMatch(html, /<details[^>]*\sopen(?:=|\s|>)/)
  assert.ok(html.indexOf('新模板名称') < html.indexOf('已归档模板'))
})

test('当前选中归档模板时自动展开，内容仍然只读；绑定项目会禁用删除', async t => {
  const result = await render(t, 'series:archived', page => { page.rows = [active(), archived(2)] })
  assert.equal(result.page.archivedExpanded, true)
  assert.match(result.html, /<details[^>]*\sopen(?:=|\s|>)/)
  assert.match(result.html, /以前的系列/)
  assert.match(result.html, /模板已归档；已有项目保留原基础值/)
  assert.match(result.html, /仍被2个项目使用，请先解除绑定或删除这些项目（包括已归档项目）/)
  const deleteButton = [...result.html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
    .find(row => row[0].replace(/<[^>]*>/g, '').trim() === '删除')?.[0]
  assert.ok(deleteButton); assert.match(deleteButton, /\sdisabled(?:=|\s|>)/)
  result.page.remove(archived(2)); assert.equal(result.dialogs.length, 0)
})

test('删除明确保留应用备份且不进入回收站；重复确认只提交一次并在解锁后导航', async t => {
  const result = await render(t, 'series:archived'), calls: any[] = []
  let finish: any
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => {
    calls.push({ url, init })
    if (init?.method === 'DELETE') return new Promise(resolve => { finish = () => resolve(response({ deleted: true })) })
    return response([active()])
  })
  result.page.remove(archived()); assert.equal(result.dialogs.length, 1)
  assert.match(result.dialogs[0].content, /trash\/templates/)
  assert.match(result.dialogs[0].content, /不会进入 Windows 回收站/)
  assert.match(result.dialogs[0].content, /不会删除项目、视频或B站合集/)
  const pending = result.dialogs[0].onPositiveClick()
  assert.equal(await result.dialogs[0].onPositiveClick(), false)
  result.page.remove(archived()); assert.equal(result.dialogs.length, 1)
  assert.equal(calls.length, 1)
  assert.deepEqual(JSON.parse(calls[0].init.body), { revision: 5, confirmed: true })
  assert.equal(calls[0].url, '/api/configuration/series%3Aarchived')
  finish(); assert.equal(await pending, true)
  assert.deepEqual(result.page.rows, [active()])
  assert.deepEqual(result.selections, [{ key: 'global', locked: false }])
})

test('删除失败保留归档模板、当前编辑范围与备份记录', async t => {
  const result = await render(t, 'series:archived', page => { page.trashRows = [backup()]; page.trashLoaded = true })
  t.mock.method(globalThis, 'fetch', async () => response({ message: '恢复备份无法写入，未删除模板' }, false))
  result.page.remove(archived())
  assert.equal(await result.dialogs[0].onPositiveClick(), false)
  assert.deepEqual(result.page.rows, [active(), archived()])
  assert.deepEqual(result.page.trashRows, [backup()])
  assert.match(result.page.error, /未删除模板/)
  assert.deepEqual(result.selections, [])
  assert.equal(result.page.busy, false)
})

test('确认窗口打开后模板修订或绑定变化，删除前保守拦截', async t => {
  const result = await render(t), writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (...args: any[]) => { writes.push(args); return response({}) })
  result.page.remove(archived())
  result.page.rows = [active(), { ...archived(), revision: 6, project_count: 1 }]
  assert.equal(await result.dialogs[0].onPositiveClick(), false)
  assert.equal(writes.length, 0)
  assert.match(result.page.error, /状态或绑定项目已变化/)
})

test('未保存表单和操作进行中均阻止归档、删除、找回，不丢失草稿', async t => {
  const result = await render(t), writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (...args: any[]) => { writes.push(args); return response({}) })
  for (const lock of ['dirty', 'editorBusy', 'busy']) {
    result.page[lock] = true
    result.page.archive(archived()); result.page.remove(archived()); await result.page.restore(backup())
    assert.equal(result.dialogs.length, 0); assert.equal(writes.length, 0)
    assert.equal(result.page[lock], true)
    result.page[lock] = false
  }
})

test('备份只在展开后读取，重复展开不会重复请求，失败可重试且保留既有历史', async t => {
  const result = await render(t), reads: string[] = []
  let finish: any
  t.mock.method(globalThis, 'fetch', async (url: string) => {
    reads.push(url); return new Promise(resolve => { finish = () => resolve(response([backup()])) })
  })
  assert.equal(reads.length, 0)
  result.page.toggleTrash({ currentTarget: { open: true } })
  result.page.toggleTrash({ currentTarget: { open: true } })
  assert.deepEqual(reads, ['/api/configuration/templates/trash'])
  finish(); await new Promise(resolve => setImmediate(resolve))
  result.page.toggleTrash({ currentTarget: { open: false } })
  result.page.toggleTrash({ currentTarget: { open: true } })
  assert.equal(reads.length, 1)
  t.mock.method(globalThis, 'fetch', async () => response({ message: '备份读取失败' }, false))
  await result.page.refreshTrash()
  assert.deepEqual(result.page.trashRows, [backup()])
  assert.equal(result.page.trashError, '备份读取失败')
  assert.equal(result.page.trashLoading, false)
})

test('找回失败保留历史；成功仍在归档区且不自动切换或绑定项目', async t => {
  const result = await render(t, 'global', page => { page.rows = [active()]; page.trashRows = [backup()]; page.trashLoaded = true })
  t.mock.method(globalThis, 'fetch', async () => response({ message: '对应模板已存在，不能覆盖' }, false))
  await result.page.restore(backup())
  assert.deepEqual(result.page.trashRows, [backup()])
  assert.deepEqual(result.page.rows, [active()])
  assert.equal(result.page.trashError, '对应模板已存在，不能覆盖')
  let finish: any
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => {
    if (init?.body) { writes.push({ url, body: JSON.parse(init.body) }); return new Promise(resolve => { finish = () => resolve(response({ key: 'series:archived', name: '以前的系列', archived: true })) }) }
    return response(url.endsWith('/trash') ? [{ ...backup(), restorable: false }] : [active(), { ...archived(), revision: 6 }])
  })
  const pending = result.page.restore(backup()); await result.page.restore(backup())
  assert.equal(writes.length, 1)
  assert.deepEqual(writes[0], { url: `/api/configuration/templates/trash/${'a'.repeat(32)}/restore`, body: { confirmed: true } })
  finish(); await pending
  assert.equal(result.page.archivedExpanded, true)
  assert.equal(result.page.archivedRows.length, 1)
  assert.equal(result.page.archivedRows[0].archived, true)
  assert.equal(result.page.trashRows[0].restorable, false)
  assert.match(result.page.trashNotice, /已找回.*保留在已归档模板/)
  assert.deepEqual(result.selections, [])
})

test('模板和备份列表均遵循最新响应，晚到的旧请求不会覆盖较新的列表', async t => {
  const result = await render(t), finish: any[] = []
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => finish.push((data: any) => resolve(response(data)))))
  const first = result.page.refresh(), latest = result.page.refresh()
  finish[1]([active()]); await latest
  finish[0]([archived()]); await first
  assert.deepEqual(result.page.rows, [active()])
  const olderTrash = result.page.refreshTrash(), newerTrash = result.page.refreshTrash()
  finish[3]([]); await newerTrash
  finish[2]([backup()]); await olderTrash
  assert.deepEqual(result.page.trashRows, [])
  assert.equal(result.page.trashLoaded, true)
  assert.equal(result.page.trashLoading, false)
})

test('恢复成功后的备份重读失败，也立即移除旧可恢复项并保留成功事实', async t => {
  const result = await render(t, 'global', page => { page.rows = [active()]; page.trashRows = [backup()]; page.trashLoaded = true })
  const writes: string[] = []
  let finish: any
  t.mock.method(globalThis, 'fetch', async (url: string, init: any) => {
    if (init?.body) { writes.push(url); return response({ key: 'series:archived', name: '以前的系列', archived: true }) }
    if (url.endsWith('/trash')) return new Promise(resolve => { finish = () => resolve(response({ message: '备份列表重读失败' }, false)) })
    return response([active(), archived()])
  })
  const pending = result.page.restore(backup())
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(result.page.trashRows, [])
  assert.match(result.page.trashNotice, /已找回/)
  finish(); await pending
  assert.equal(result.page.busy, false)
  assert.deepEqual(result.page.trashRows, [])
  assert.equal(result.page.trashError, '备份列表重读失败')
  assert.match(result.page.trashNotice, /已找回/)
  assert.equal(writes.length, 1)
})

test('损坏与绑定冲突备份明确显示原因和定位入口，不误报模板已存在或尝试恢复', async t => {
  const damaged = { backup_id: null, backup_file: 'broken.json', backup_path: 'D:\\DubForge-data\\trash\\templates\\broken.json',
    key: '', name: '无法读取的模板备份', deleted_at: '', restorable: false, error: '备份内容损坏，无法解析 JSON。' }
  const linked = { ...backup(), restorable: false, reason: '仍有项目引用此模板标识，请先解除绑定再找回。' }
  const result = await render(t, 'global', page => { page.trashExpanded = true; page.trashLoaded = true; page.trashRows = [linked, damaged] })
  assert.match(result.html, /备份内容损坏，无法解析 JSON/)
  assert.match(result.html, /仍有项目引用此模板标识，请先解除绑定再找回/)
  assert.match(result.html, /定位备份文件/)
  assert.ok(result.html.includes(damaged.backup_path))
  assert.doesNotMatch(result.html, /对应模板已存在|模板已存在|Invalid Date/)
  const buttons = [...result.html.matchAll(/<button\b[^>]*>[\s\S]*?<\/button>/g)]
    .filter(row => row[0].replace(/<[^>]*>/g, '').trim() === '暂不能找回')
  assert.equal(buttons.length, 2)
  for (const button of buttons) assert.match(button[0], /\sdisabled(?:=|\s|>)/)
  const writes: any[] = []
  t.mock.method(globalThis, 'fetch', async (...args: any[]) => { writes.push(args); return response({}) })
  await result.page.restore(damaged); await result.page.restore(linked)
  assert.equal(writes.length, 0)
})
