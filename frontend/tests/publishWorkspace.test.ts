import test from 'node:test'
import assert from 'node:assert/strict'
import { publishWorkspace, resolvePublishTask } from '../src/utils/publishWorkspace.ts'

const row = (status: string, extra: Record<string, any> = {}) => ({ status, lifecycle: 'active', receipt: {}, ...extra })
const remote = (status: string, extra: Record<string, any> = {}) => row(status, { receipt: { bvid: 'BVTest' }, ...extra })

test('新稿只显示准备任务，不把保存解释为开始上传', () => {
  const work = publishWorkspace(row('draft'))
  assert.equal(work.mode, 'draft'); assert.equal(work.defaultTask, 'video')
  assert.deepEqual(work.tasks.map(task => task.key), ['video', 'metadata', 'confirm', 'status'])
  assert.match(work.description, /保存.*不会开始上传/)
  assert.equal(publishWorkspace(row('draft'), { preparationStep: 2 }).defaultTask, 'metadata')
  assert.equal(publishWorkspace(row('draft'), { preparationStep: 3 }).defaultTask, 'confirm')
})

test('已上传先补资料再确认，暂停或失败先看运行结果', () => {
  assert.equal(publishWorkspace(row('uploaded')).defaultTask, 'metadata')
  assert.equal(publishWorkspace(row('uploaded'), { preparationStep: 3 }).defaultTask, 'confirm')
  assert.deepEqual(publishWorkspace(row('uploaded')).tasks.map(task => task.key), ['video', 'metadata', 'confirm', 'status'])
  assert.match(publishWorkspace(row('uploaded')).description, /视频、封面和顺序保持不变/)
  for (const state of ['paused', 'failed']) {
    const work = publishWorkspace(row(state))
    assert.equal(work.mode, 'resume'); assert.equal(work.defaultTask, 'status')
    assert.deepEqual(work.tasks.map(task => task.key), ['confirm', 'status', 'snapshot'])
    assert.match(work.description, /复用.*上传/)
  }
})

test('投稿结果未知即使已有BV也不展示修改或新稿入口', () => {
  const work = publishWorkspace(remote('unknown', { latest_update: { status: 'failed' } }), { originalUpdateDirty: true })
  assert.equal(work.mode, 'verify'); assert.equal(work.tone, 'warning')
  assert.deepEqual(work.tasks.map(task => task.key), ['status', 'snapshot'])
  assert.equal(resolvePublishTask('update', work), 'status')
})

test('后台上传或提交仅呈现进度与冻结资料', () => {
  for (const state of ['queued', 'uploading', 'submitting', 'joining', 'checking']) {
    const work = publishWorkspace(remote(state))
    assert.equal(work.mode, 'running'); assert.equal(work.defaultTask, 'status')
    assert.deepEqual(work.tasks.map(task => task.key), ['status', 'snapshot'])
  }
})

test('有BV稿件把状态、原稿更新、独立同步和快照分成明确任务', () => {
  for (const state of ['submitted', 'published', 'private', 'reviewed', 'rejected', 'collection_pending']) {
    const work = publishWorkspace(remote(state))
    assert.equal(work.mode, 'remote'); assert.equal(work.defaultTask, state === 'rejected' ? 'update' : 'status')
    assert.deepEqual(work.tasks.map(task => task.key), ['status', 'update', 'sync', 'snapshot'])
  }
  assert.equal(publishWorkspace(remote('rejected')).tone, 'error')
  assert.match(publishWorkspace(remote('rejected')).description, /保留原BV/)
})

test('没有BV的旧记录不推断远端身份或展示同步任务', () => {
  const work = publishWorkspace(row('published'))
  assert.equal(work.hasRemote, false); assert.equal(work.mode, 'readonly')
  assert.deepEqual(work.tasks.map(task => task.key), ['status', 'snapshot'])
})

test('原稿任务进行中或需核对时优先呈现本次更新，不能被原稿成功色掩盖', () => {
  for (const state of ['queued', 'uploading', 'submitting', 'waiting', 'delivering']) {
    const work = publishWorkspace(remote('published', { latest_update: { status: state } }))
    assert.equal(work.defaultTask, 'update'); assert.equal(work.tone, 'info')
    assert.equal(work.tasks.find(task => task.key === 'update')?.badge, '进行中')
  }
  for (const state of ['prepared', 'failed', 'paused', 'unknown']) {
    const work = publishWorkspace(remote('published', { latest_update: { status: state } }))
    assert.equal(work.defaultTask, 'update'); assert.equal(work.tone, 'warning')
    assert.equal(work.tasks.find(task => task.key === 'update')?.badge, '需处理')
  }
  assert.equal(publishWorkspace(remote('published', { latest_update: { status: 'completed' } })).defaultTask, 'status')
})

test('页内未保存原稿选择和待同步文案分别引导到对应任务', () => {
  assert.equal(publishWorkspace(remote('published'), { originalUpdateDirty: true }).defaultTask, 'update')
  assert.equal(publishWorkspace(remote('published'), { remoteCopyDirty: true }).defaultTask, 'sync')
  assert.equal(publishWorkspace(remote('published'), { originalUpdateActive: true }).defaultTask, 'update')
})

test('只读、归档、回收优先于任何远端或本次更新任务', () => {
  for (const input of [remote('published', { lifecycle: 'archived' }), remote('draft', { lifecycle: 'deleted' }),
    { status: 'published', receipt: { bvid: 'BVTest' }, archived_at: '2026-10-17' }]) {
    const work = publishWorkspace(input, { originalUpdateActive: true })
    assert.equal(work.mode, 'readonly'); assert.equal(work.readOnly, true)
    assert.deepEqual(work.tasks.map(task => task.key), ['status', 'snapshot'])
  }
  assert.equal(publishWorkspace(remote('published'), { recordOnly: true }).mode, 'readonly')
})

test('有效主动任务不被轮询默认覆盖；不合法的任务回退且不修改原记录', () => {
  const input = remote('published', { latest_update: { status: 'waiting' } }), original = structuredClone(input)
  const work = publishWorkspace(input)
  assert.equal(resolvePublishTask('snapshot', work), 'snapshot')
  assert.equal(resolvePublishTask('prepare', work), 'update')
  assert.equal(resolvePublishTask(null, work), 'update')
  assert.deepEqual(input, original)
  assert.equal(publishWorkspace(null).mode, 'empty')
})

test('视频已发布但章节失败或结果未知时显示后续待办，不把整次交付标绿', () => {
  const failed=publishWorkspace(remote('published', { chapter_sync: { status:'failed', enabled:true } }))
  assert.equal(failed.title,'视频已发布，章节还需处理')
  assert.equal(failed.tone,'warning');assert.equal(failed.defaultTask,'status')
  assert.equal(failed.tasks.find(task=>task.key==='status')?.badge,'需处理')
  assert.match(failed.description,/单独重试章节.*不重新上传或重复投稿/)
  const unknown=publishWorkspace(remote('published', { chapter_sync: { status:'unknown', enabled:true } }))
  assert.equal(unknown.title,'视频已发布，章节结果待核对')
  assert.equal(unknown.tone,'warning');assert.match(unknown.description,/先核对已有章节/)
})

test('章节后台处理中用进行中语义，通过与仅自己可见不误称公开发布', () => {
  for(const state of ['published','private','reviewed']) {
    const working=publishWorkspace(remote(state,{chapter_sync:{status:'pending',enabled:true}}))
    assert.equal(working.tone,'info');assert.equal(working.defaultTask,'status')
    assert.equal(working.tasks.find(task=>task.key==='status')?.badge,'进行中')
    assert.match(working.title,state==='published'?/视频已发布/:/视频已通过/)
  }
  for(const status of ['completed','skipped','superseded',''])
    assert.equal(publishWorkspace(remote('published',{chapter_sync:{status}})).tone,'success')
})

test('本次原稿更新优先于旧稿章节异常，驳回仍优先修订原稿', () => {
  for(const update of ['waiting','failed','unknown']) {
    const work=publishWorkspace(remote('published',{chapter_sync:{status:'failed'},latest_update:{status:update}}))
    assert.equal(work.defaultTask,'update');assert.match(work.title,/原稿更新/)
  }
  const rejected=publishWorkspace(remote('rejected',{chapter_sync:{status:'failed'}}))
  assert.equal(rejected.defaultTask,'update');assert.equal(rejected.tone,'error')
})
