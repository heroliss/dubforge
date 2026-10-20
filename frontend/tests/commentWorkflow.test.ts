import assert from 'node:assert/strict'
import test from 'node:test'
import { commentAction, commentFailure, commentIssueContext, commentIssueCounts, commentIssueLabel, commentJobFeedback, commentJobSummary, commentWorkspaceLink, hasCommentTranslation, preferredCommentAction } from '../src/utils/commentWorkflow.ts'

test('诊断动作只接受已知值，并优先对应实际任务', () => {
  assert.equal(commentAction('translate'), 'translate')
  assert.equal(commentAction('unknown'), undefined)
  assert.equal(preferredCommentAction({ action:'translate', status:'failed' }), 'translate')
  assert.equal(preferredCommentAction({ action:'filter', status:'running' }, [{ ai_issues:{ translate:{status:'failed'} } }]), 'filter')
  assert.equal(preferredCommentAction(null, [{ ai_issues:{ translate:{status:'failed'} } }]), 'translate')
  assert.equal(preferredCommentAction({action:'filter',status:'done'}, [{ ai_issues:{ translate:{status:'failed'} } }]), 'translate')
  assert.equal(preferredCommentAction(null, [{ai_issues:{filter:{status:'failed',updated_at:'2026-10-07T10:00:00Z'},translate:{status:'blocked',updated_at:'2026-10-07T09:00:00Z'}}}]), 'filter')
  assert.equal(preferredCommentAction(null), undefined)
})

test('评论问题按动作区分审核拦截、暂缓和失败，不把它们混成一个错误', () => {
  const rows = [{ai_issues:{translate:{status:'blocked'}}}, {ai_issues:{translate:{status:'failed'}}},
    {ai_issues:{translate:{status:'deferred'},filter:{status:'blocked'}}}, {}]
  assert.deepEqual(commentIssueCounts(rows,'translate'),{blocked:1,deferred:1,failed:1})
  assert.deepEqual(commentIssueCounts(rows,'filter'),{blocked:1,deferred:0,failed:0})
})

test('审核拦截保留请求范围，旧记录和批次请求不能断言独立评论被拒绝', () => {
  assert.equal(commentIssueLabel({status:'blocked'}),'服务商审核拦截（请求范围未记录）')
  assert.equal(commentIssueLabel({status:'blocked',request_scope:'unknown'}),'服务商审核拦截（请求范围未记录）')
  assert.equal(commentIssueLabel({status:'blocked',request_scope:'batch'}),'所在请求被服务商审核拦截')
  assert.match(commentIssueContext({status:'blocked',request_scope:'batch'}),/不能据此确定哪条评论/)
  assert.match(commentIssueContext({status:'blocked'}),/不能确定这条评论是否被服务商单独拒绝/)
  assert.equal(commentIssueLabel({status:'blocked',request_scope:'item'}),'服务商审核拦截（单条请求）')
})

test('有效译文不包括空白，结果区分新增、复用、旧问题和暂缓', () => {
  assert.equal(hasCommentTranslation({text_zh:' \n '}),false);assert.equal(hasCommentTranslation({text_zh:'中文'}),true)
  const job={action:'translate',status:'partial',processed:0,cached:31,blocked:4,blocked_cached:4,failed:0,deferred:0,skipped:4}
  assert.match(commentJobSummary(job),/本次新增译文 0 条；复用有效译文 31 条；审核拦截记录 4 条/)
  assert.equal(commentJobFeedback(job,'translate').type,'warning')
  const cached=commentJobFeedback({...job,status:'done',blocked:0,blocked_cached:0,skipped:0},'translate')
  assert.equal(cached.type,'info');assert.match(cached.text,/本次没有新增译文/)
  const legacy=commentJobSummary({processed:0,cached:31,skipped:4},'translate')
  assert.match(legacy,/复用有效译文 31 条；跳过问题项 4 条/)
  assert.doesNotMatch(legacy,/旧记录未区分有效结果与跳过/)
  assert.equal(commentJobFeedback({...job,status:'done',processed:3,blocked:0,blocked_cached:0,skipped:0},'translate').type,'success')
})

test('评论入口使用已支持的工作区与诊断锚点，不把非评论错误引到评论页', () => {
  assert.equal(commentWorkspaceLink('a b'), '/projects/a%20b/comments')
  assert.equal(commentWorkspaceLink('fixture',true,'translate'), '/projects/fixture/comments?comment_action=translate#llm-conversation')
  assert.equal(commentWorkspaceLink('fixture',true), '/projects/fixture/comments#llm-conversation')
  assert.equal(commentFailure({code:'COMMENTS_EMPTY',message:'缺少资料'}),true)
  assert.equal(commentFailure('评论 AI 处理失败'),true)
  assert.equal(commentFailure({code:'ASSEMBLE_FAILED',message:'编码失败'}),false)
})
