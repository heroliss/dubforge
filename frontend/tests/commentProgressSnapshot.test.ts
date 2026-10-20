import assert from 'node:assert/strict'
import test from 'node:test'
import { readCommentProgress } from '../src/utils/commentProgressSnapshot.ts'

function deferred<T>() { let resolve!:(value:T)=>void;const promise=new Promise<T>(done=>resolve=done);return{promise,resolve} }
test('导航与评论页合并同项目进行中的进度请求，并短时复用轻量结果',async t=>{
  const pending=deferred<Response>();let reads=0
  t.mock.method(globalThis,'fetch',async()=>{reads++;return pending.promise})
  const first=readCommentProgress('shared-snapshot'),second=readCommentProgress('shared-snapshot')
  assert.equal(reads,1)
  pending.resolve(new Response(JSON.stringify({workspace:{state:'done',confirmed_count:3}})))
  assert.deepEqual(await first,await second)
  assert.equal((await readCommentProgress('shared-snapshot')).value.workspace.confirmed_count,3)
  assert.equal(reads,1)
})
test('离开一个页面只取消本次等待，不中断另一处共享的进度读取',async t=>{
  const pending=deferred<Response>(),controller=new AbortController();let reads=0
  t.mock.method(globalThis,'fetch',async()=>{reads++;return pending.promise})
  const first=readCommentProgress('shared-cancel',controller.signal),other=readCommentProgress('shared-cancel')
  const cancelled=assert.rejects(first,{name:'AbortError'})
  controller.abort();await cancelled
  pending.resolve(new Response(JSON.stringify({data_revision:'latest'})))
  assert.equal((await other).value.data_revision,'latest');assert.equal(reads,1)
})
test('修改后的明确刷新绕过旧快照，失败不会缓存为成功',async t=>{
  let reads=0
  t.mock.method(globalThis,'fetch',async()=>{reads++;return new Response(JSON.stringify({revision:reads}),{status:reads===2?500:200})})
  assert.equal((await readCommentProgress('shared-refresh')).value.revision,1)
  assert.equal((await readCommentProgress('shared-refresh',undefined,true)).ok,false)
  assert.equal((await readCommentProgress('shared-refresh',undefined,true)).value.revision,3)
})
test('旧服务缺少进度接口不解析HTML，允许工作区使用兼容回退',async t=>{
  t.mock.method(globalThis,'fetch',async()=>new Response('<html>not found</html>',{status:404}))
  const snapshot=await readCommentProgress('old-service')
  assert.equal(snapshot.status,404);assert.deepEqual(snapshot.value,{})
})
