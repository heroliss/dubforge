import test, { before, after, mock } from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import https from 'node:https'
import { Auth, Client } from '@renmu/bili-api'
import { execute } from './worker.mjs'
import { safeError } from './protocol.mjs'

before(() => {
  for (const module of [http, https]) mock.method(module, 'request', () => { throw new Error('测试禁止访问真实网络') })
})
after(() => mock.restoreAll())

const episode = (aid, order) => ({ id: aid + 1000, aid, cid: aid + 2000,
  bvid: '', title: `视频${aid}`, order, seasonId: 9, sectionId: 17 })
const payload = { account_id: '123', season_id: 9, section_id: 17 }

// 使用实际 SDK Cookie 注入、错误解包及 rawResponse，只替换 HTTP 适配器。
function fixture(rows = [episode(101, 1)], options = {}) {
  const calls = [], credential = { DedeUserID: '123', SESSDATA: 'fixture-private', bili_jct: 'fixture-csrf' }
  let members = structuredClone(rows), posted = false
  const sdk = { Auth, Client: class extends Client {
    get user() {
      const user = super.user
      user.request.defaults.adapter = async config => ({ config, data: { code: 0,
        data: { isLogin: true, mid: options.mid || 123, uname: '测试账号' } }, status: 200, headers: {} })
      return user
    }
    get platform() {
      const platform = super.platform
      platform.request.defaults.adapter = async config => {
        calls.push(config)
        assert.match(config.headers.cookie, /fixture-private/)
        assert.equal(config.timeout, 15000)
        assert.equal(config.maxRedirects, 0)
        assert.equal(config['axios-retry'].retries, 0)
        let data
        if (config.url.endsWith('/season')) data = { season: { id: 9, mid: options.owner || 123 },
          sections: { sections: [{ id: 17 }] } }
        else if (config.method === 'get' && config.url.endsWith('/section')) {
          if (posted && options.verifyError) throw options.verifyError
          data = { section: { id: 17, type: 1, title: options.title || '正片', seasonId: 9,
            epCount: options.epCount ?? members.length, Episodes: null },
            episodes: options.emptyNull ? null : members }
        } else if (config.url.endsWith('/section/edit')) {
          posted = true
          assert.equal(config.method, 'post')
          assert.deepEqual(config.params, { csrf: 'fixture-csrf' })
          assert.equal(config.extra.rawResponse, true)
          if (options.writeError) throw options.writeError
          if (options.malformedReceipt) return { config, data: {}, status: 200, headers: {} }
          if (options.reject) return { config, data: { code: -400 }, status: 200, headers: {} }
          if (!options.leaveOrder) {
            const body = JSON.parse(config.data)
            members = body.sorts.map(s => ({ ...members.find(e => e.id === s.id), order: s.sort }))
          }
          return { config, data: { code: 0 }, status: 200, headers: {} }
        } else assert.fail('不应调用其他接口')
        return { config, data: { code: 0, data }, status: 200, headers: {} }
      }
      return platform
    }
  }, WebVideoUploader: class { constructor() { assert.fail('排序不上传文件') } } }
  return { calls, run: (operation, extra = {}) => execute({ operation, payload: { ...payload, ...extra }, credential }, sdk) }
}
const rejected = async (promise, code) => assert.rejects(promise, error => {
  assert.equal(safeError(error).code, code)
  assert.doesNotMatch(safeError(error).message, /fixture-private|fixture-csrf|敏感/)
  return true
})

test('完整小节读取提供有界白名单和快照指纹，不发送修改', async () => {
  const f = fixture([episode(202, 2), episode(101, 1)])
  const result = await f.run('collection_order_read')
  assert.deepEqual(result.section, { id: 17, season_id: 9, title: '正片', type: 1 })
  assert.deepEqual(result.episodes.map(e => e.aid), [101, 202])
  assert.match(result.fingerprint, /^[a-f0-9]{64}$/)
  assert.equal(f.calls.length, 2)
  assert.equal(f.calls.filter(c => c.method === 'post').length, 0)
})

test('明确空小节兼容null；数量不齐、重复成员和错误归属不能当作完整', async () => {
  assert.deepEqual((await fixture([], { emptyNull: true }).run('collection_order_read')).episodes, [])
  for (const f of [fixture([episode(101, 1)], { epCount: 2 }),
    fixture([episode(101, 1), episode(101, 2)]), fixture([episode(101, 1)], { owner: 456 }),
    fixture([{ ...episode(101, 1), id: 0 }]), fixture([{ ...episode(101, 1), order: 0 }])]) {
    await rejected(f.run('collection_order_read'), 'PUBLISH_COLLECTION_ORDER_RESPONSE')
    assert.equal(f.calls.filter(c => c.method === 'post').length, 0)
  }
})

test('单次排序保留其他成员、使用全部episode ID并回读确认', async () => {
  const snapshot = await fixture().run('collection_order_read')
  const f = fixture([episode(101, 1), episode(303, 2), episode(202, 3)])
  const result = await f.run('collection_order_apply', { snapshot, order: [202, 303], confirmed: true })
  assert.equal(result.verified, true)
  assert.equal(result.changed, true)
  assert.deepEqual(result.episodes.map(e => e.aid), [101, 202, 303])
  const posts = f.calls.filter(c => c.method === 'post')
  assert.equal(posts.length, 1)
  assert.deepEqual(JSON.parse(posts[0].data), { section: { id: 17, type: 1, seasonId: 9, title: '正片' },
    sorts: [{ id: 1101, sort: 1 }, { id: 1202, sort: 2 }, { id: 1303, sort: 3 }], captcha_token: '' })
  assert.equal(f.calls.length, 5)
})

test('已符合批次顺序时仅核对，不重复发送写请求', async () => {
  const snapshot = await fixture().run('collection_order_read')
  const f = fixture([episode(101, 1), episode(202, 2)])
  const result = await f.run('collection_order_apply', { snapshot, order: [202], confirmed: true })
  assert.equal(result.changed, false)
  assert.equal(result.verified, true)
  assert.equal(f.calls.length, 2)
})

test('未确认、快照被修改和重复aid均在发送修改前拒绝', async () => {
  const snapshot = await fixture().run('collection_order_read')
  for (const extra of [{ snapshot, order: [202] }, { snapshot, order: [202, 202], confirmed: true },
    { snapshot: { ...snapshot, fingerprint: 'a'.repeat(64) }, order: [202], confirmed: true }]) {
    const f = fixture([episode(101, 1), episode(202, 2)])
    await rejected(f.run('collection_order_apply', extra), 'PUBLISH_COLLECTION_ORDER_INVALID')
    assert.equal(f.calls.filter(c => c.method === 'post').length, 0)
  }
})

test('外部增删、改名、改顺序和遗漏批次aid均停止，保护已确认成员', async () => {
  const snapshot = await fixture([episode(101, 1), episode(102, 2)]).run('collection_order_read')
  for (const [rows, options] of [
    [[episode(101, 1), episode(102, 2), episode(999, 3), episode(202, 4)], {}],
    [[episode(101, 1), episode(202, 2)], {}],
    [[episode(102, 1), episode(101, 2), episode(202, 3)], {}],
    [[{ ...episode(101, 1), title: '平台新标题' }, episode(102, 2), episode(202, 3)], {}],
    [[episode(101, 1), episode(102, 2), episode(202, 3)], { title: '小节新标题' }],
    [[episode(101, 1), episode(102, 2)], {}],
  ]) {
    const f = fixture(rows, options)
    await rejected(f.run('collection_order_apply', { snapshot, order: [202], confirmed: true }), 'PUBLISH_COLLECTION_ORDER_CHANGED')
    assert.equal(f.calls.filter(c => c.method === 'post').length, 0)
  }
})

test('写后超时、异常响应、回读失败或不匹配保留未知；明确拒绝单独报告', async () => {
  const snapshot = await fixture().run('collection_order_read')
  for (const [options, code] of [
    [{ writeError: Object.assign(new Error('敏感请求'), { code: 'ETIMEDOUT' }) }, 'PUBLISH_COLLECTION_ORDER_UNKNOWN'],
    [{ writeError: Object.assign(new Error('敏感请求'), { response: { status: 503 } }) }, 'PUBLISH_COLLECTION_ORDER_UNKNOWN'],
    [{ malformedReceipt: true }, 'PUBLISH_COLLECTION_ORDER_UNKNOWN'],
    [{ verifyError: new Error('敏感请求') }, 'PUBLISH_COLLECTION_ORDER_UNKNOWN'],
    [{ leaveOrder: true }, 'PUBLISH_COLLECTION_ORDER_UNKNOWN'],
    [{ reject: true }, 'PUBLISH_COLLECTION_ORDER_REJECTED'],
  ]) {
    const f = fixture([episode(101, 1), episode(303, 2), episode(202, 3)], options)
    await rejected(f.run('collection_order_apply', { snapshot, order: [202, 303], confirmed: true }), code)
    assert.equal(f.calls.filter(c => c.method === 'post').length, 1)
  }
})

test('账号不匹配时不读取或修改合集', async () => {
  const f = fixture([], { mid: 456 })
  await rejected(f.run('collection_order_read'), 'PUBLISH_ACCOUNT_MISMATCH')
  assert.equal(f.calls.length, 0)
})

test('未知写结果可只读核对原有batch成员的新位置，绝不再次发送排序', async () => {
  const snapshot = await fixture([episode(303, 1), episode(101, 2), episode(202, 3)]).run('collection_order_read')
  const alreadyApplied = fixture([episode(101, 1), episode(202, 2), episode(303, 3)])
  const result = await alreadyApplied.run('collection_order_verify', { snapshot, order: [202, 303] })
  assert.equal(result.verified, true)
  assert.deepEqual(result.episodes.map(e => e.aid), [101, 202, 303])
  assert.equal(alreadyApplied.calls.filter(c => c.method === 'post').length, 0)
  const notApplied = fixture([episode(303, 1), episode(101, 2), episode(202, 3)])
  assert.equal((await notApplied.run('collection_order_verify', { snapshot, order: [202, 303] })).verified, false)
  assert.equal(notApplied.calls.filter(c => c.method === 'post').length, 0)
})

test('只读核对仍拒绝既有其他成员被改动或新增无关成员', async () => {
  const snapshot = await fixture([episode(101, 1), episode(102, 2)]).run('collection_order_read')
  for (const members of [[episode(102, 1), episode(101, 2), episode(202, 3)],
    [episode(101, 1), episode(102, 2), episode(999, 3), episode(202, 4)]]) {
    const f = fixture(members)
    await rejected(f.run('collection_order_verify', { snapshot, order: [202] }), 'PUBLISH_COLLECTION_ORDER_CHANGED')
    assert.equal(f.calls.filter(c => c.method === 'post').length, 0)
  }
})
