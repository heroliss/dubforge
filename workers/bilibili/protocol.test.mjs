import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { XMLBuilder } from 'fast-xml-parser'
import { declarationsFrom, safeError, submissionOptions } from './protocol.mjs'
import { CollectionFailure, CollectionJoinFailure } from './collection-error.mjs'

test('只提交明确字段，分离上传与投稿，禁止使用高层自动投稿函数', async () => {
  const worker = await readFile(new URL('./worker.mjs', import.meta.url), 'utf8')
  assert.doesNotMatch(worker, /\.addMedia\(|\.onAddMedia\(/)
  assert.match(worker, /payload\.confirmed !== true/)
  assert.match(worker, /operation === 'upload'/)
})
test('锁定版本不关闭证书验证，依赖修复版本可构建XML', async () => {
  const source = await readFile(new URL('./node_modules/@renmu/bili-api/dist/platform/upload.js', import.meta.url), 'utf8')
  assert.doesNotMatch(source, /rejectUnauthorized\s*:\s*false/)
  assert.match(new XMLBuilder().build({ content: '安全测试' }), /安全测试/)
})
test('公开与仅自己可见、明确关闭值及声明都不被默认覆盖', () => {
  const values = { title:'标题', tags:['旅行'], visibility:'self', copyright:3, watermark:false,
    declaration_id:4, close_comments:false, close_danmaku:true, selected_comments:false, arbitrary:'secret' }
  const result = submissionOptions(values, 'https://example.com/cover.jpg')
  assert.equal(result.is_only_self, 1)
  assert.equal(result.copyright, 3)
  assert.deepEqual(result.creation_statement, { id:4 })
  assert.equal(result.watermark.state, 0)
  assert.equal(result.arbitrary, undefined)
  assert.equal(result.dtime, undefined)
  assert.equal(submissionOptions({...values, copyright:2}, 'url').creation_statement, undefined)
  assert.equal(submissionOptions({...values, copyright:2, watermark:true}, 'url').watermark, undefined)
  assert.equal(submissionOptions({...values, copyright:1, watermark:true}, 'url').watermark.state, 1)
  assert.equal(result.up_close_reply, false)
  assert.equal(result.up_close_danmu, true)
  assert.equal(result.up_selection_reply, false)
})
test('声明只来自明确声明区域，不把分区编号当作声明', () => {
  assert.deepEqual(declarationsFrom({ types:[{id:4,name:'生活'}], creation_statement:{list:[{id:1,name:'AI合成'}]} }), [{value:1,label:'AI合成'}])
  assert.deepEqual(declarationsFrom({}), [])
})
test('原始异常不会泄露请求头、Cookie或堆栈', () => {
  const output = JSON.stringify(safeError({ message:'SESSDATA=secret', response:{headers:{Cookie:'secret'},status:403} }))
  assert.doesNotMatch(output, /secret|SESSDATA|Cookie/)
  assert.match(output, /验证/)
})
test('证书错误明确提示，不能退化为忽略证书校验', () => {
  const result = safeError({code:'CERT_HAS_EXPIRED',message:'private-host-detail'})
  assert.equal(result.code, 'PUBLISH_TLS')
  assert.match(result.message, /不会关闭证书校验/)
  assert.doesNotMatch(result.message, /private-host/)
})
test('创作管理未发送失败保留脱敏阶段，便于定位预检问题', () => {
  const result = safeError({code:'PUBLISH_WRITE_NOT_SENT', detail:{stage:'validate',remote_code:-400,secret:'private'}})
  assert.deepEqual(result.detail, {stage:'validate',remote_code:-400})
  assert.doesNotMatch(JSON.stringify(result), /private/)
})

test('合集错误保留失败环节和平台原因，剔除凭据、URL与堆栈', () => {
  const secret = 'fixture-sensitive-token'
  const error = {code:-400,statusCode:200,rawResponse:{data:{code:-400,
    message:`图片格式错误 SESSDATA=${secret} https://example.com/?token=private`}},stack:'private-stack'}
  const result = safeError(new CollectionFailure('cover',error,{SESSDATA:secret}))
  assert.equal(result.code,'PUBLISH_COLLECTION_NOT_SENT')
  assert.deepEqual(result.detail,{stage:'cover',remote_code:-400,http_status:200,network_code:null})
  assert.match(result.message,/图片格式错误/)
  assert.match(result.message,/平台错误码 -400/)
  assert.doesNotMatch(JSON.stringify(result),/fixture-sensitive|example.com|private|SESSDATA/)
  const generic = safeError(new CollectionFailure('auth',new Error('secret_cookie=private')))
  assert.doesNotMatch(JSON.stringify(generic),/secret_cookie|private/)
})

test('合集网络失败保留安全分类，HTTP异常不能误判为未创建', () => {
  const timed = safeError(new CollectionFailure('create',{code:'ETIMEDOUT'}))
  assert.equal(timed.code,'PUBLISH_COLLECTION_UNKNOWN')
  assert.match(timed.message,/ETIMEDOUT/)
  assert.equal(safeError(new CollectionFailure('create',{response:{status:503}})).code,'PUBLISH_COLLECTION_UNKNOWN')
  const rejected=safeError(new CollectionFailure('create',{code:-400,message:'名称无效'}))
  assert.equal(rejected.code,'PUBLISH_COLLECTION_REJECTED')
  assert.match(rejected.message,/名称无效/)
  const expired=safeError(new CollectionFailure('auth',{code:-101}))
  assert.match(expired.message,/重新扫码/)
})

test('加入合集的本地原因不误报网络，平台诊断脱敏且区分已投稿', () => {
  const local=safeError(new CollectionJoinFailure('join_section',null,{},'members'))
  assert.match(local.message,/成员数据不完整/)
  assert.doesNotMatch(local.message,/请检查网络/)
  const secret='fixture-sensitive-cookie'
  const result=safeError(new CollectionJoinFailure('join_add',{code:123,message:`无权限 cookie=${secret} https://example.com/?token=private`},{SESSDATA:secret}))
  assert.equal(result.code,'PUBLISH_COLLECTION_JOIN_REJECTED')
  assert.equal(result.detail.stage,'join_add')
  assert.match(result.message,/无权限/)
  assert.doesNotMatch(JSON.stringify(result),/fixture-sensitive|example.com|private|cookie=/)
  assert.equal(safeError(new CollectionJoinFailure('join_add',{response:{status:503}})).code,'PUBLISH_COLLECTION_JOIN_UNKNOWN')
})
