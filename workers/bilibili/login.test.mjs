import test, { before, after, mock } from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import https from 'node:https'
import { Client, WebQrcodeLogin } from '@renmu/bili-api'
import { execute } from './worker.mjs'
import { safeError } from './protocol.mjs'

before(() => {
  for (const module of [http, https]) mock.method(module, 'request', () => {throw new Error('测试禁止访问真实网络')})
})
after(() => mock.restoreAll())

// 使用真实 SDK 的 Axios 拦截器；仅替换 HTTP 适配器，防止把 poll 的嵌套层级模拟错。
const callback = 'https://passport.bilibili.com/crossDomain?DedeUserID=123&SESSDATA=fixture%252Csession&bili_jct=fixture-csrf&unrelated=ignored'
function fixture(responses, account = { isLogin: true, mid: 123, uname: '测试账号' }, crossDomain) {
  const events = [], requests = [], sleeps = []
  let navCalls = 0
  const reply = (config, data) => ({ config, data, status: 200, statusText: 'OK', headers: {} })
  const sdk = {
    WebQrcodeLogin: class extends WebQrcodeLogin {
      constructor() {
        super()
        this.request.defaults.adapter = async config => {
          requests.push(config.url)
          if (config.url.endsWith('/generate')) return reply(config, {code:0,data:{url:'https://passport.bilibili.com/fixture',qrcode_key:'fixture-key'}})
          if (!config.url.endsWith('/poll')) {
            assert.equal(config.maxRedirects, 0)
            assert.equal(config.extra.rawResponse, true)
            assert.ok(config.headers['User-Agent'])
            assert.equal(config.headers.Referer, 'https://passport.bilibili.com/')
            assert.equal(config.headers.cookie, undefined)
            assert.ok(crossDomain, '不应兑换跨域票据')
            const response = typeof crossDomain === 'function' ? crossDomain(config) : crossDomain
            return {...reply(config, ''),...response}
          }
          assert.equal(config.params.qrcode_key, 'fixture-key')
          const data = responses.shift()
          if (data instanceof Error) throw data
          assert.notEqual(data, undefined, '不应继续轮询')
          return data.httpResponse ? {...reply(config, data.body),headers:data.headers} : reply(config, data)
        }
      }
    },
    Client: class extends Client {
      get user() {
        const user = super.user
        user.request.defaults.adapter = async config => {
          requests.push(config.url); navCalls++
          assert.equal(config.url, 'https://api.bilibili.com/x/web-interface/nav')
          assert.match(config.headers.cookie, /SESSDATA=fixture%2Csession/)
          assert.match(config.headers.cookie, /DedeUserID=123/)
          const info = typeof account === 'function' ? account(navCalls) : account
          return reply(config, {code:0,data:info})
        }
        return user
      }
    },
  }
  return { events, requests, sleeps, run: () => execute({operation:'login',payload:{}}, sdk,
    (event, data) => events.push({event,...data}), async ms => {sleeps.push(ms)}) }
}
const poll = (code, extra = {}) => ({code:0,data:{code,...extra}})

test('未扫码→手机待确认→已确认→账号校验：真实 SDK 响应形状与 Cookie 解码', async () => {
  const f = fixture([poll(86101), poll(86090), poll(0, {url:callback})])
  const result = await f.run()
  assert.deepEqual(f.events.map(e => e.event), ['qr', 'scanned', 'verifying'])
  assert.equal(result.id, '123'); assert.equal(result.name, '测试账号')
  assert.deepEqual(Object.keys(result.credential).sort(), ['DedeUserID', 'SESSDATA', 'bili_jct'])
  assert.doesNotMatch(JSON.stringify(f.events), /SESSDATA|fixture-csrf/)
})

test('已确认不必先经历 scanned；昵称缺失不阻断已验证登录', async () => {
  const f = fixture([poll(0, {url:callback})], {isLogin:true,mid:123})
  assert.equal((await f.run()).name, 'B站用户 123')
  assert.deepEqual(f.events.map(e => e.event), ['qr', 'verifying'])
})

test('二维码过期、未知返回、账号不匹配和未登录均不产生成功凭据', async () => {
  for (const [responses, account, code] of [
    [[poll(86038)], undefined, 'PUBLISH_QR_EXPIRED'],
    [[{code:0,data:null}], undefined, 'PUBLISH_QR_RESPONSE'],
    [[poll(0,{url:callback})], {isLogin:true,mid:456}, 'PUBLISH_ACCOUNT_MISMATCH'],
    [[poll(0,{url:callback})], {isLogin:false,mid:123}, 'PUBLISH_LOGIN_EXPIRED'],
    [[poll(0,{url:'invalid'})], undefined, 'PUBLISH_QR_RESPONSE'],
    [[poll(0,{url:'https://passport.bilibili.com/crossDomain?DedeUserID=123'})], undefined, 'PUBLISH_QR_RESPONSE'],
  ]) {
    await assert.rejects(fixture(responses, account).run(), error => safeError(error).code === code)
  }
})

test('短暂网络故障只重试读取，不重新生成二维码；确认后也可以恢复校验', async () => {
  const reset = () => Object.assign(new Error('敏感原始异常'), {code:'ECONNRESET'})
  const f = fixture([reset(), poll(0,{url:callback})], count => {
    if (count === 1) throw reset()
    return {isLogin:true,mid:123,uname:'测试账号'}
  })
  assert.equal((await f.run()).id, '123')
  assert.equal(f.requests.filter(url => url.endsWith('/generate')).length, 1)
  assert.equal(f.sleeps.filter(ms => ms === 1000).length, 2)
})

test('风控与证书失败不重试、不关闭证书检查', async () => {
  for (const code of [-412, 'CERT_HAS_EXPIRED']) {
    const f = fixture([poll(0,{url:callback})], () => {throw Object.assign(new Error('敏感 Cookie'), {code})})
    await assert.rejects(f.run())
    assert.equal(f.requests.filter(url => url.endsWith('/nav')).length, 1)
    assert.equal(f.sleeps.filter(ms => ms === 1000).length, 0)
  }
})

const issuedCookies = [
  'SESSDATA=fixture%2Csession; Path=/; Domain=.bilibili.com; Expires=Fri, 01 Jan 2027 00:00:00 GMT; Secure; HttpOnly',
  'bili_jct=fixture-csrf; Path=/; Domain=.bilibili.com',
  'DedeUserID=123; Path=/; Domain=.bilibili.com',
  'unrelated=never-save-this; Path=/',
]
const ticketUrl = 'https://passport.biligame.com/x/passport-login/web/crossDomain?ticket=fixture-ticket&gourl=https%3A%2F%2Fwww.bilibili.com'

test('新版只有票据：兑换一次并读取302响应头，不跟随跳转、不重新轮询已完成二维码', async () => {
  const f = fixture([poll(0, {url:ticketUrl})], undefined,
    {status:302,headers:{'set-cookie':issuedCookies,location:'https://example.com/do-not-follow'}})
  const result = await f.run()
  assert.equal(result.id, '123')
  assert.equal(result.credential.SESSDATA, 'fixture%2Csession')
  assert.equal(result.credential.unrelated, undefined)
  assert.equal(f.requests.filter(url => url.includes('ticket=')).length, 1)
  assert.equal(f.requests.filter(url => url.endsWith('/poll')).length, 1)
  assert.doesNotMatch(JSON.stringify(f.events), /fixture-ticket|fixture-csrf|SESSDATA/)
})

test('新版轮询直接下发Cookie时无需兑现票据，即使成功URL为空也可完成', async () => {
  for (const url of ['',ticketUrl]) {
    const f = fixture([{httpResponse:true,body:poll(0,{url}),headers:{'set-cookie':issuedCookies}}])
    assert.equal((await f.run()).id, '123')
    assert.equal(f.requests.filter(url => url.includes('ticket=')).length, 0)
  }
})

test('票据响应缺少凭据时不假报成功，不把票据、URL或Cookie写进错误', async () => {
  const f = fixture([poll(0,{url:ticketUrl})], undefined, {headers:{'set-cookie':['unrelated=sensitive']}})
  await assert.rejects(f.run(), error => {
    assert.equal(safeError(error).code, 'PUBLISH_QR_RESPONSE')
    assert.doesNotMatch(JSON.stringify(safeError(error)), /fixture-ticket|sensitive|https:/)
    return true
  })
  assert.equal(f.requests.filter(url => url.endsWith('/nav')).length, 0)
})

test('非法票据目标在发出请求前拒绝，不能把票据送往外站、本机或HTTP', async () => {
  for (const url of ['http://passport.biligame.com/crossDomain?ticket=secret',
    'https://127.0.0.1/crossDomain?ticket=secret', 'https://passport.biligame.com.evil.example/crossDomain?ticket=secret',
    'https://passport.biligame.com:8443/crossDomain?ticket=secret',
    'https://user:pass@passport.biligame.com/crossDomain?ticket=secret',
    'https://passport.biligame.com/other?ticket=secret']) {
    const f = fixture([poll(0,{url})])
    await assert.rejects(f.run(), error => safeError(error).code === 'PUBLISH_QR_DESTINATION')
    assert.equal(f.requests.length, 2)
  }
})

test('票据兑换遭遇风控不重试、不返回伪成功', async () => {
  const f = fixture([poll(0,{url:ticketUrl})], undefined, {data:{code:-412,message:'不应暴露的原始响应'}})
  await assert.rejects(f.run(), error => safeError(error).code === 'PUBLISH_VERIFICATION')
  assert.equal(f.requests.filter(url => url.includes('ticket=')).length, 1)
})
