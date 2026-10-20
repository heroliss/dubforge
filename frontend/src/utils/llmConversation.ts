export type ConversationRow = { id:string; role:string; content:string; [key:string]:any }
// 事件时间与对话列表都按本机时区显示，不能直接截取ISO字符串中的UTC时刻。
export function formatLlmEventTime(value: unknown) {
  if (value == null || value === '') return ''
  const text = String(value)
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(text)
  return Number.isFinite(date.getTime()) ? date.toLocaleTimeString('zh-CN', { hour12: false }) : text
}
// 后端按长消息分片存储；跨页累积后先还原同一事件，完整回复收齐后再替换流式正文。
export function reassembleLlmEvents(events:any[]) {
  const result:any[]=[], messages=new Map<string,{event:any;parts:Map<number,string>;total:number}>()
  for(const event of events){
    if(!event.message_id || !Number.isInteger(event.content_part) || !Number.isInteger(event.content_parts)) {result.push(event);continue}
    let item=messages.get(String(event.message_id))
    if(!item){item={event:{...event,content:''},parts:new Map(),total:event.content_parts};messages.set(String(event.message_id),item);result.push(item.event)}
    item.parts.set(event.content_part,String(event.content||''))
    item.event.content=[...item.parts].sort((a,b)=>a[0]-b[0]).map(([,part])=>part).join('')
    item.event.multipartIncomplete=item.parts.size<item.total
  }
  return result.filter(event=>!(event.event==='assistant_response'&&event.multipartIncomplete))
}
/** 步骤和独立操作复用同一事件聚合；完整响应替换增量，思考与正文始终分开。 */
export function stepConversationState(events:any[], validationMethod='校验方法：检查结构、字段与任务业务规则') {

  const rows: any[] = []
  const assistantByRequest = new Map<string, any>()
  const reasoningByRequest = new Map<string, any>()
  const retryInfo = new Map<string, { maxAttempt: number, total: number, failures: string[] }>()
  // Chat Completions 的重试是一次新的 HTTP 请求。为了让模型知道此前任务，请求体
  // 会重复携带 system/历史消息；界面按内容去重，只保留这些消息第一次出现的顺序。
  const seenContext = new Set<string>()
  const retryMeta = (event: any) => {
    const group = String(event.retry_group || '')
    if (!group) return null
    const attempt = Number(event.retry_attempt) || 1
    const total = Number(event.retry_total) || 1
    const info = retryInfo.get(group) || { maxAttempt: 1, total, failures: [] }
    info.maxAttempt = Math.max(info.maxAttempt, attempt)
    info.total = Math.max(info.total, total)
    if (event.event === 'validation_error' && event.content) info.failures.push(String(event.content))
    retryInfo.set(group, info)
    return { group, attempt, total }
  }
  let lastRequestId = 'unscoped'
  for (const event of reassembleLlmEvents(events)) {
    if (event.event === 'request' && !event.request_id && event.role === 'user') lastRequestId = `anonymous-${rows.length}`
    const retry = retryMeta(event)
    const requestId = String(event.request_id || lastRequestId)
    if (event.request_id) lastRequestId = requestId
    if (event.event === 'json_repair') {
      rows.push({
        id: `json-repair-${rows.length}`, requestId, role: 'json_repair',
        kind: 'repaired', content: String(event.content || ''),
        status: '已本地修复', ts: event.ts,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'retry_strategy') {
      rows.push({
        id: `strategy-${rows.length}`, requestId, role: 'strategy',
        kind: event.mode === 'fresh' ? 'fresh' : 'conversation',
        content: String(event.content || ''),
        status: '已选择', ts: event.ts,
        retryAttempt: Number(event.retry_attempt) || 1,
        retryTotal: Number(event.retry_total) || 1,
      })
    } else if (event.event === 'merge_summary') {
      rows.push({
        id: `merge-${rows.length}`, requestId, role: 'merge',
        kind: 'success', content: String(event.content || ''),
        status: '合并完成', ts: event.ts,
      })
    } else if (event.event === 'request') {
      const role = event.role === 'assistant' ? 'history' : event.role === 'system' ? 'system' : 'user'
      const content = String(event.content || '')
      // 只去重同一个逻辑请求内重复携带的上下文；不同窗口允许出现相同 role。
      const contextKey = retry ? `${retry.group}:${role}:${content}` : ''
      if (contextKey && seenContext.has(contextKey)) continue
      if (contextKey) seenContext.add(contextKey)
      rows.push({
        id: `${requestId}-${rows.length}`,
        requestId, role, content,
        profile: event.profile || '', model: event.model || '',
        status: '等待首字节', ts: event.ts,
        inputChars: Number(event.input_chars) || 0,
        requestKind: content.includes('局部修复请求')
          ? 'line-repair'
          : (retry?.attempt || 1) > 1 ? 'format-repair' : 'initial',
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'assistant_delta') {
      let row = assistantByRequest.get(requestId)
      if (!row) {
        row = { id: `${requestId}-assistant`, requestId, role: 'assistant',
          content: '', profile: event.profile || '', model: '',
          status: '生成中', ts: event.ts,
          retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
          retryTotal: retry?.total || 1 }
        rows.push(row)
        assistantByRequest.set(requestId, row)
      }
      row.content += String(event.content || '')
      row.status = '生成中'
    } else if (event.event === 'reasoning_delta') {
      let row = reasoningByRequest.get(requestId)
      if (!row) {
        row = { id: `${requestId}-reasoning`, requestId, role: 'reasoning',
          content: '', profile: event.profile || '', model: '',
          status: '思考中', ts: event.ts,
          retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
          retryTotal: retry?.total || 1 }
        rows.push(row)
        reasoningByRequest.set(requestId, row)
      }
      row.content += String(event.content || '')
      row.status = '思考中'
    } else if (event.event === 'assistant_response') {
      let row = assistantByRequest.get(requestId)
      if (!row) {
        row = { id: `${requestId}-assistant`, requestId, role: 'assistant',
          content: '', profile: event.profile || '', model: '',
          status: '已完成', ts: event.ts,
          retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
          retryTotal: retry?.total || 1 }
        rows.push(row)
        assistantByRequest.set(requestId, row)
      }
      row.content = String(event.content || '')
      row.status = '已完成'
      row.model = event.model || row.model
      row.outputChars = Number(event.output_chars) || row.content.length
      const startedAt = Date.parse(String(row.ts || ''))
      const finishedAt = Date.parse(String(event.ts || ''))
      if (Number.isFinite(startedAt) && Number.isFinite(finishedAt)) {
        row.elapsedMs = Math.max(0, finishedAt - startedAt)
      }
      const reasoning = reasoningByRequest.get(requestId)
      if (reasoning) {
        reasoning.status = '思考完成'
        const reasoningStart = Date.parse(String(reasoning.ts || ''))
        if (Number.isFinite(reasoningStart) && Number.isFinite(finishedAt)) {
          reasoning.elapsedMs = Math.max(0, finishedAt - reasoningStart)
        }
      }
    } else if (event.event === 'validation_error' || event.event === 'validation_success') {
      const eventText = String(event.content || '')
      const parseFailure = event.event === 'validation_error'
        && /未找到.*JSON|JSONDecodeError/i.test(eventText)
      rows.push({
        id: `validation-${rows.length}`, requestId, role: 'validation',
        kind: event.event === 'validation_error' ? 'error' : 'success',
        content: parseFailure
          ? `JSON 解析失败：${eventText || '未知原因'}`
          : event.event === 'validation_error'
            ? `校验失败：${eventText || '未知原因'}\n${validationMethod}`
            : `${eventText || '校验通过'}\n${validationMethod}`,
        status: parseFailure ? 'JSON 需修复' : event.event === 'validation_error' ? '需自动修复' : '已通过',
        ts: event.ts,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'fallback') {
      rows.push({
        id: `fallback-${rows.length}`, requestId, role: 'fallback',
        kind: 'warning',
        content: String(event.content || '已回退本地规则切分'),
        status: '已回退',
        ts: event.ts,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1,
      })
    } else if (event.event === 'request_aborted') {
      for (const row of rows) {
        if (row.requestId === requestId) {
          row.aborted = true
          row.status = '本次候选未采用'
        }
      }
      const reason = String(event.content || '响应偏离任务要求或超过请求时限，已关闭本地连接。')
      const code = event.code ? `\n错误码：${String(event.code)}` : ''
      const confirmation = event.server_confirmed === false
        ? '\n已关闭本地连接；服务端停止计算或计费尚未确认。' : ''
      rows.push({ id: `aborted-${rows.length}`, requestId, role: 'aborted', kind: 'error', aborted: true,
        content: `${reason}${code}\n本次候选未采用。${confirmation}`,
        status: '本次候选未采用', ts: event.ts, code: event.code, detail: event.detail,
        retryGroup: retry?.group || '', retryAttempt: retry?.attempt || 1,
        retryTotal: retry?.total || 1 })
    } else if (event.event === 'request_cancelled') {
      for (const row of rows) {
        if (row.requestId === requestId) row.status = '本地已取消'
      }
      rows.push({ id: `cancelled-${rows.length}`, requestId, role: 'cancellation', kind: 'warning',
        content: String(event.content || '本地请求已取消；服务端停止计算尚未确认'),
        status: '本地已取消 · 服务端未确认', ts: event.ts })
    } else if (['batch_start','retry','response_metadata','provider_error','error','run_end','item_skipped','recovery_stop'].includes(event.event)) {
      rows.push({ id: `event-${rows.length}`, requestId, role: event.event, content: String(event.content || ''),
        ts: event.ts, profile: event.profile, model: event.model, code: event.code, retryAttempt: retry?.attempt || 1 })
    } else if (event.event === 'status') {
      for (const row of rows) {
        if (row.requestId === requestId
            && ['system', 'user', 'reasoning'].includes(row.role)) {
          row.status = String(event.content || '')
        }
      }
    }
  }
  for (const row of rows) {
    const info = row.retryGroup ? retryInfo.get(row.retryGroup) : null
    if (info) {
      row.retryFailures = info.failures.length
      row.retryMaxAttempt = info.maxAttempt
    }
  }
  // 不再“只显示最后一次尝试”：完整对话按时间线展示，用户能看到首轮大输出
  // 和后续短修复输出之间的关系；重复携带的历史上下文已在上面被去重。
  return { allRows: rows, visibleRows: rows, retryInfo }

}

export function conversationRows(events:any[]):ConversationRow[] {
  const roles:Record<string,string>={strategy:'retry_strategy',merge:'merge_summary',aborted:'request_aborted',cancellation:'request_cancelled'}
  return stepConversationState(events).visibleRows.map(row=>({...row,
    role:row.role==='validation' ? `validation_${row.kind}` : roles[row.role] || row.role,
    attempt:row.retryAttempt>1 ? row.retryAttempt : undefined }))
}
export const conversationLabels:Record<string,string>={system:'系统提示',user:'发送内容',history:'历史回复',assistant:'模型回复',reasoning:'模型返回的思考',
  batch_start:'处理批次',retry:'自动重试',retry_strategy:'纠错策略',validation_error:'校验未通过',validation_success:'校验通过',json_repair:'本地修复',response_metadata:'响应结束',provider_error:'服务错误',error:'任务失败',run_end:'处理结果',item_skipped:'逐条处理状态',recovery_stop:'已停止自动尝试',merge_summary:'合并结果',fallback:'回退处理',request_aborted:'本地已中止响应',request_cancelled:'取消请求'}
