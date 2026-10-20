import assert from 'node:assert/strict'
import test from 'node:test'
import { chronologicalProjectItems, moveProjectBatchItem, projectBatchControls, projectBatchDelivery,
  projectBatchGroupTitle, projectBatchNeedsDateCheck, projectBatchOrderedGroups, projectBatchProgress, projectBatchPublishCount,
  projectBatchStage, projectBatchTargets, projectBatchGroupCounts, projectBatchGroupFailure, projectBatchIssues,
  projectBatchGroupPhase, projectBatchItemPhase, projectBatchFooter,
  type ProjectBatchItem, type ProjectBatchGroup } from '../src/utils/projectBatch.ts'

const item = (uid: string, source_date?: string, mode = 'publish'): ProjectBatchItem => ({ project_uid: uid, title: uid, source_date, delivery_mode: mode })
const group = (items: ProjectBatchItem[], kind: 'collection' | 'independent' = 'collection'): ProjectBatchGroup => ({ id: 'g', kind, items, project_uids: items.map(item => item.project_uid) })

test('原视频日期稳定排序，未知日期不借用导入时间', () => {
  const rows = [item('unknown'), item('later', '2026-02-01'), item('first', '2026-01-01'), item('same', '2026-01-01'), item('unknown2')]
  assert.deepEqual(chronologicalProjectItems(rows).map(row => row.project_uid), ['first', 'same', 'later', 'unknown', 'unknown2'])
  assert.equal(rows[0].project_uid, 'unknown')
})

test('只提醒同一合集多个项目中仍缺失的原视频日期', () => {
  assert.equal(projectBatchNeedsDateCheck(group([item('one')])), false)
  assert.equal(projectBatchNeedsDateCheck(group([item('one'), item('two')], 'independent')), false)
  assert.equal(projectBatchNeedsDateCheck(group([item('one', '2026-01-01'), item('two', '2026-01-02')])), false)
  assert.equal(projectBatchNeedsDateCheck(group([item('one', '2026-01-01'), item('two')])), true)
  assert.equal(projectBatchNeedsDateCheck(group([])), false)
})

test('移动合集组内顺序保持其他组的位置，且不改输入', () => {
  const rows = [item('one'), item('standalone'), item('two'), item('three'), item('other')]
  const current = group([rows[0], rows[2], rows[3]])
  assert.deepEqual(moveProjectBatchItem(rows, current, 1, -1).map(row => row.project_uid), ['two', 'standalone', 'one', 'three', 'other'])
  assert.equal(moveProjectBatchItem(rows, current, 0, -1), rows)
  assert.equal(moveProjectBatchItem(rows, current, 2, 1), rows)
  assert.equal(rows[0].project_uid, 'one')
})

test('交付计数遵从保存终点，更新原BV的上传终点不扩大成投稿', () => {
  const update = { ...item('update'), recipe: { mode: 'update', delivery_mode: 'upload' } }
  const groups = [group([item('p1'), item('p2')]), group([item('produce', undefined, 'produce')], 'independent'), group([update], 'independent')]
  assert.equal(projectBatchDelivery(update), 'upload')
  assert.equal(projectBatchPublishCount(groups), 2)
  assert.equal(projectBatchOrderedGroups(groups).length, 1)
})

test('分组名称只用可读标题，不把ID冒充合集名', () => {
  assert.equal(projectBatchGroupTitle({ ...group([]), scope_display: { account_name: '个人账号', season_title: '历史系列', section_title: '中国史' } }), '个人账号 · 历史系列 · 中国史')
  assert.equal(projectBatchGroupTitle({ ...group([]), scope: { account_id: '123', season_id: 456, section_id: 789 } }), '目标合集')
  assert.equal(projectBatchGroupTitle(group([item('独立视频')], 'independent')), '独立视频')
})

test('后台父批次完成状态计入独立项目，不依赖初始item快照', () => {
  assert.equal(projectBatchProgress({ groups: [{ ...group([item('one'), item('two')]), status: 'running', items: [{ ...item('one'), status: 'done' }, item('two')] },
    { ...group([item('independent')], 'independent'), status: 'done' }] }), '2 / 3 个项目已完成')
})

test('暂停恢复动作与阶段标签保留平台核验区别', () => {
  assert.deepEqual(projectBatchControls('monitoring'), { pause: true, stop: true, resume: false })
  assert.deepEqual(projectBatchControls('attention'), { pause: false, stop: false, resume: true })
  assert.equal(projectBatchStage('monitoring').label, '等待审核与核验')
  assert.equal(projectBatchStage('done').label, '全部完成')
})

test('打开输入去重并保留选择次序', () => {
  assert.deepEqual(projectBatchTargets(['a', { uid: 'b', title: '乙' }, { uid: 'a', title: '甲' }]), [{ uid: 'a', title: '甲' }, { uid: 'b', title: '乙' }])
})

const llmError = (account = '2126017182') => `split 未完成：全部启用的 API 配置均失败 —— 【智谱GLM】：LLM 请求失败 HTTP 429 —— 请求过于频繁 (限流)（可能是并发太高或配额用完）；【火山方舟-agent-plan】：服务商返回错误：InvalidSubscription Your account (${account}) does not have a valid AgentPlan subscription. Please visit https://example.com/${account}`
const interrupted = (id: string, error = llmError()): ProjectBatchGroup => ({ ...group([item(id)]), id, status: 'attention', phase: '批次已暂停，等待处理', error })

test('两个组同类模型问题合并，账户和地址差异不造成重复提示', () => {
  const batch = { groups: [interrupted('one'), interrupted('two', llmError('9999999999'))] }
  assert.deepEqual(projectBatchGroupCounts(batch), [{ key: 'attention', label: '2 组待处理', count: 2 }])
  const issues = projectBatchIssues(batch)
  assert.equal(issues.length, 2)
  assert.deepEqual(issues[0].groupIds, ['one', 'two'])
  assert.equal(issues[0].summary, '智谱GLM：上次请求受限，需核对限流或配额')
  assert.equal(issues[1].summary, '火山方舟-agent-plan：上次订阅无效、未生效或已过期')
  assert.doesNotMatch(JSON.stringify(issues), /2126017182|9999999999|https:|余额不足/)
  assert.equal(projectBatchGroupFailure(batch.groups[0])?.step, '语义整理')
  assert.equal(projectBatchGroupPhase(batch.groups[0]), '上次在语义整理中断')
  assert.match(projectBatchGroupFailure(batch.groups[0])!.technical, /2126017182/)
  assert.doesNotMatch(projectBatchFooter(batch), /其他组继续|其余/)
})

test('失败与运行混合时只收集失败组，运行组的历史错误不制造警告', () => {
  const running = { ...interrupted('running'), status: 'running', phase: '语音转文字' }
  const batch = { groups: [interrupted('failed'), running] }
  assert.equal(projectBatchGroupFailure(running), null)
  assert.equal(projectBatchGroupPhase(running), '语音转文字')
  assert.ok(projectBatchIssues(batch).every(issue => JSON.stringify(issue.groupIds) === '["failed"]'))
  assert.deepEqual(projectBatchGroupCounts(batch).map(row => row.label), ['1 组执行中', '1 组待处理'])
  assert.equal(projectBatchFooter(batch), '待处理的组已中断，其余 1 组继续执行。')
})

test('明确配额耗尽与普通429保持区别，不同原因分别呈现影响范围', () => {
  const batch = { groups: [interrupted('quota', 'split 未完成：全部启用的 API 配置均失败 —— 【智谱GLM】：LLM_QUOTA_EXHAUSTED 余额不足或无可用资源包'),
    interrupted('limited', 'split 未完成：全部启用的 API 配置均失败 —— 【智谱GLM】：LLM 请求失败 HTTP 429')] }
  const issues = projectBatchIssues(batch)
  assert.equal(issues.length, 2)
  assert.equal(issues[0].summary, '智谱GLM：上次余额不足或可用配额已耗尽')
  assert.deepEqual(issues[0].groupIds, ['quota'])
  assert.equal(issues[1].summary, '智谱GLM：上次请求受限，需核对限流或配额')
  assert.deepEqual(issues[1].groupIds, ['limited'])
})

test('未知非模型错误保留完整详情，不猜模型问题或把主动暂停当失败', () => {
  const failed = interrupted('unknown', 'compose 未完成：encoder failed request 1234567890 at https://example.com/private')
  const failure = projectBatchGroupFailure(failed)!
  assert.equal(failure.model, false); assert.equal(failure.summary, '上次在合成成片中断')
  assert.match(failure.technical, /encoder failed request 1234567890/)
  assert.deepEqual(projectBatchIssues({ groups: [failed] }), [])
  assert.equal(projectBatchGroupFailure({ ...failed, status: 'paused' }), null)
  assert.equal(projectBatchItemPhase({ ...item('failed'), status: 'failed', phase: '需要处理后重新确认' }, failed), '等待处理后继续')
  assert.equal(projectBatchItemPhase({ ...item('done'), status: 'done', phase: '已完成' }, failed), '已完成')
  assert.equal(projectBatchItemPhase({ ...item('done-without-phase'), status: 'completed' }, failed), '已完成')
})

test('识别实际提供商链的引号与新的中文订阅原因，未知模型错误不跨组合并', () => {
  const groups = [interrupted('one', 'split 未完成：全部启用的 API 配置均失败 —— 「火山方舟」: 模型服务订阅未开通或已失效'),
    interrupted('two', 'split 未完成：全部启用的 API 配置均失败 —— 「智谱」: unknown response alpha'),
    interrupted('three', 'split 未完成：全部启用的 API 配置均失败 —— 「智谱」: unknown response beta')]
  const issues = projectBatchIssues({ groups })
  assert.equal(issues[0].summary, '火山方舟：上次订阅无效、未生效或已过期')
  assert.equal(issues.length, 3)
  assert.deepEqual(issues.slice(1).map(issue => issue.groupIds), [['two'], ['three']])
})
