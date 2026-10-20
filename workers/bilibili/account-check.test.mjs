import test, {before, after, mock} from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import https from 'node:https'
import {Auth, Client} from '@renmu/bili-api'
import {execute} from './worker.mjs'
import {safeError} from './protocol.mjs'

before(() => {
  for (const module of [http, https]) mock.method(module, 'request', () => {
    throw new Error('测试禁止访问真实网络')
  })
})
after(() => mock.restoreAll())

// 保留真实 SDK 的 Cookie 设置与响应解包，只替换 HTTP 适配器。
function fixture(info, remoteCode = 0) {
  const requests = []
  const sdk = {
    Auth,
    Client:class extends Client {
      get user() {
        const user = super.user
        user.request.defaults.adapter = async config => {
          requests.push(config.url)
          assert.equal(config.url,'https://api.bilibili.com/x/web-interface/nav')
          assert.equal(config.timeout,15000)
          assert.match(config.headers.cookie,/DedeUserID=123/)
          if (info instanceof Error) throw info
          return {config,data:{code:remoteCode,data:info},status:200,statusText:'OK',headers:{}}
        }
        return user
      }
      get platform() { assert.fail('账号检查不读取合集或投稿接口') }
    },
    WebVideoUploader:class {constructor() {assert.fail('账号检查不上传文件')}},
  }
  return {requests,run:() => execute({operation:'account_check',payload:{account_id:'123'},
    credential:{DedeUserID:'123',SESSDATA:'fixture-only'}},sdk)}
}

test('账号检查只读取一次身份，返回白名单，不读取合集或投稿', async () => {
  const f = fixture({isLogin:true,mid:123,uname:' 当前昵称 ',credential:'不得回传',vip:{private:'不得回传'}})
  assert.deepEqual(await f.run(),{id:'123',name:'当前昵称'})
  assert.deepEqual(f.requests,['https://api.bilibili.com/x/web-interface/nav'])
})

test('账号检查兼容字符串UID与缺失昵称，名称保持有界', async () => {
  assert.deepEqual(await fixture({isLogin:true,mid:'123'}).run(),{id:'123',name:'B站用户 123'})
  assert.equal((await fixture({isLogin:true,mid:123,uname:'名'.repeat(200)}).run()).name.length,150)
})

test('明确未登录与异常响应分别报告，不能把缺字段当作登录失效', async () => {
  for (const [info, code] of [
    [{isLogin:false},'PUBLISH_LOGIN_EXPIRED'],
    [null,'PUBLISH_ACCOUNT_RESPONSE'],
    [[], 'PUBLISH_ACCOUNT_RESPONSE'],
    [{mid:123},'PUBLISH_ACCOUNT_RESPONSE'],
    [{isLogin:1,mid:123},'PUBLISH_ACCOUNT_RESPONSE'],
    [{isLogin:true},'PUBLISH_ACCOUNT_RESPONSE'],
    [{isLogin:true,mid:0},'PUBLISH_ACCOUNT_RESPONSE'],
    [{isLogin:true,mid:1.5},'PUBLISH_ACCOUNT_RESPONSE'],
    [{isLogin:true,mid:'invalid'},'PUBLISH_ACCOUNT_RESPONSE'],
    [{isLogin:true,mid:456},'PUBLISH_ACCOUNT_MISMATCH'],
  ]) {
    const f = fixture(info)
    await assert.rejects(f.run(),error => {
      const result = safeError(error)
      assert.equal(result.code,code)
      assert.doesNotMatch(result.message,/未保存凭据/)
      return true
    })
    assert.equal(f.requests.length,1)
  }
})

test('账号检查保留失效、风控、证书与网络错误分类，不重试或泄露异常', async () => {
  const cases = [
    [null,-101,'PUBLISH_LOGIN_EXPIRED'],
    [null,-412,'PUBLISH_VERIFICATION'],
    [Object.assign(new Error('敏感异常'),{code:'CERT_HAS_EXPIRED'}),0,'PUBLISH_TLS'],
    [Object.assign(new Error('敏感异常'),{code:'ETIMEDOUT'}),0,'PUBLISH_REMOTE_ERROR'],
    [Object.assign(new Error('敏感异常'),{response:{status:403}}),0,'PUBLISH_VERIFICATION'],
  ]
  for (const [info, remoteCode, code] of cases) {
    const f = fixture(info,remoteCode)
    await assert.rejects(f.run(),error => {
      const result = safeError(error)
      assert.equal(result.code,code)
      assert.doesNotMatch(result.message,/敏感异常|fixture-only/)
      return true
    })
    assert.equal(f.requests.length,1)
  }
})
