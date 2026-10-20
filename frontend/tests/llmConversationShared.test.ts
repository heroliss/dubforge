import assert from 'node:assert/strict'
import test from 'node:test'
import { conversationRows, stepConversationState } from '../src/utils/llmConversation.ts'
import { conversationListUrl, openLlmConversations, LLM_DIALOG_EVENT } from '../src/utils/llmDialog.ts'

test('长完整回复跨页拼接前保留已有增量，收齐后一次替换且不双计',()=>{
  const events:any[]=[{event:'request',role:'user',request_id:'r',content:'输入'},
    {event:'assistant_delta',request_id:'r',content:'已有完整增量'},
    {event:'assistant_response',request_id:'r',message_id:'m',content_part:0,content_parts:2,content:'最终',continuation:false}]
  assert.equal(conversationRows(events).find(row=>row.role==='assistant')?.content,'已有完整增量')
  events.push({event:'assistant_response',request_id:'r',message_id:'m',content_part:1,content_parts:2,content:'完整回复',continuation:true})
  assert.equal(conversationRows(events).find(row=>row.role==='assistant')?.content,'最终完整回复')
  events.push(events.at(-1))
  assert.equal(conversationRows(events).find(row=>row.role==='assistant')?.content,'最终完整回复')
})

test('长请求与思考增量按message_id合并，历史上下文不会成为当前回答',()=>{
  const rows=conversationRows([
    {event:'request',role:'assistant',content:'历史回复',retry_group:'g'},
    {event:'request',role:'user',request_id:'r',message_id:'q',content_part:0,content_parts:2,content:'前半'},
    {event:'request',role:'user',request_id:'r',message_id:'q',content_part:1,content_parts:2,content:'后半'},
    {event:'reasoning_delta',request_id:'r',message_id:'t',content_part:0,content_parts:2,content:'实际'},
    {event:'reasoning_delta',request_id:'r',message_id:'t',content_part:1,content_parts:2,content:'返回的思考'},
    {event:'assistant_response',request_id:'r',content:'最终输出'},
  ])
  assert.equal(rows.find(row=>row.role==='user')?.content,'前半后半')
  assert.equal(rows.find(row=>row.role==='reasoning')?.content,'实际返回的思考')
  assert.equal(rows.find(row=>row.role==='history')?.content,'历史回复')
  assert.equal(rows.filter(row=>row.role==='assistant').length,1)
})

test('步骤与独立任务共用取消和中止语义，兼容评论、批次与服务异常事件',()=>{
  const events=[{event:'assistant_delta',request_id:'r',content:'候选'},
    {event:'request_aborted',request_id:'r',content:'格式偏离',code:'LLM_OUTPUT_CONTRACT',server_confirmed:false},
    {event:'batch_start',content:'第二组'},{event:'provider_error',content:'服务错误',profile:'备用',code:'HTTP_429'},
    {event:'merge_summary',content:'已经合并'}]
  const state=stepConversationState(events)
  assert.equal(state.visibleRows.find(row=>row.role==='assistant').aborted,true)
  const rows=conversationRows(events)
  assert.ok(rows.some(row=>row.role==='request_aborted'&&row.content.includes('服务端停止计算或计费尚未确认')))
  assert.ok(rows.some(row=>row.role==='batch_start'));assert.ok(rows.some(row=>row.code==='HTTP_429'))
  assert.ok(rows.some(row=>row.role==='merge_summary'))
})

test('共享入口只请求观察，筛选编码完整，打开不会产生模型或发布请求',t=>{
  const calls:any[]=[]
  t.mock.method(globalThis,'fetch',()=>{throw new Error('入口不得发模型请求')})
  const windowDescriptor=Object.getOwnPropertyDescriptor(globalThis,'window'),eventDescriptor=Object.getOwnPropertyDescriptor(globalThis,'CustomEvent')
  Object.defineProperty(globalThis,'window',{configurable:true,value:{dispatchEvent:(event:any)=>{calls.push(event);return true}}})
  Object.defineProperty(globalThis,'CustomEvent',{configurable:true,value:class{type:string;detail:any;constructor(type:string,options:any){this.type=type;this.detail=options.detail}}})
  t.after(()=>{if(windowDescriptor)Object.defineProperty(globalThis,'window',windowDescriptor);else delete(globalThis as any).window;if(eventDescriptor)Object.defineProperty(globalThis,'CustomEvent',eventDescriptor);else delete(globalThis as any).CustomEvent})
  openLlmConversations({projectUid:'中文/项目',operation:'chapters',ownerId:'pub 1'})
  assert.equal(calls[0].type,LLM_DIALOG_EVENT);assert.equal(calls[0].detail.ownerId,'pub 1')
  const url=new URL(conversationListUrl(calls[0].detail),'http://localhost')
  assert.equal(url.searchParams.get('project_uid'),'中文/项目');assert.equal(url.searchParams.get('operation'),'chapters')
  assert.equal(url.searchParams.get('owner_id'),'pub 1')
})
