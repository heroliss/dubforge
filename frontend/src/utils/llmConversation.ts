export type ConversationRow = {id:string;role:string;content:string;ts?:string;model?:string;profile?:string;attempt?:number;code?:string}
/** 相同请求的流式输出只追加一次，完整响应替换增量；重复历史提示不刷屏。 */
export function conversationRows(events:any[]):ConversationRow[] {
  const rows:ConversationRow[]=[], outputs=new Map<string,ConversationRow>(), seen=new Set<string>()
  const append=(event:any,role:string)=>{
    const row={id:String(rows.length),role,content:String(event.content||''),ts:event.ts,
      model:event.model,profile:event.profile,attempt:Number(event.retry_attempt)||undefined,code:event.code}
    rows.push(row);return row
  }
  for(const event of events){
    if(event.event==='request'){
      const key=event.retry_group?JSON.stringify([event.retry_group,event.role,event.content]):''
      if(key&&seen.has(key))continue
      if(key)seen.add(key)
      append(event,event.role==='assistant'?'history':event.role==='system'?'system':'user')
    }else if(['assistant_delta','assistant_response','reasoning_delta'].includes(event.event)){
      const role=event.event==='reasoning_delta'?'reasoning':'assistant',key=event.request_id+':'+role
      let row=outputs.get(key)
      if(!row){row=append({...event,content:''},role);outputs.set(key,row)}
      row.content=event.event==='assistant_response'?String(event.content||''):row.content+String(event.content||'')
      row.model=event.model||row.model
    }else if(['batch_start','retry','retry_strategy','validation_error','validation_success','json_repair','response_metadata','provider_error','error','run_end','item_skipped','recovery_stop'].includes(event.event)){
      append(event,event.event)
    }
  }
  return rows
}
export const conversationLabels:Record<string,string>={system:'系统提示',user:'发送内容',history:'历史回复',assistant:'模型回复',reasoning:'模型返回的思考',
  batch_start:'处理批次',retry:'自动重试',retry_strategy:'纠错策略',validation_error:'校验未通过',validation_success:'校验通过',json_repair:'本地修复',response_metadata:'响应结束',provider_error:'服务错误',error:'任务失败',run_end:'处理结果',item_skipped:'逐条处理状态',recovery_stop:'已停止自动尝试'}
