import assert from 'node:assert/strict'
import test from 'node:test'
import { effectScope, nextTick, ref } from 'vue'
import { usePublishMetadata } from '../src/composables/usePublishMetadata.ts'

const data = (id: string) => ({human_categories:[{id:1001,name:'旅行'}],declarations:[{value:1,label:'含AI生成内容'}],seasons:[{id,title:id,sections:[]}]})
const tick = async () => { for (let i=0; i<6; i++) await nextTick() }

test('选择账号自动读取，按账号缓存，手动刷新绕过缓存', async () => {
  const scope = effectScope(), account = ref(''), calls: string[] = []
  const state = scope.run(() => usePublishMetadata(account, async id => {calls.push(id);return data(id)}))!
  try {
    assert.deepEqual(calls, [])
    account.value = 'a'; await tick()
    assert.deepEqual(calls, ['a']); assert.equal(state.ready.value, true)
    account.value = 'b'; await tick()
    assert.equal(state.metadata.value.seasons[0].title, 'b')
    account.value = 'a'; await tick()
    assert.deepEqual(calls, ['a','b']); assert.equal(state.metadata.value.seasons[0].title, 'a')
    await state.refresh()
    assert.deepEqual(calls, ['a','b','a'])
  } finally { scope.stop() }
})

test('慢账号响应不覆盖新账号，重复读取合并为一个请求', async () => {
  const scope = effectScope(), account = ref('a'), pending: Record<string, (value:any) => void> = {}, calls: string[] = []
  const state = scope.run(() => usePublishMetadata(account, id => {calls.push(id);return new Promise(resolve => {pending[id]=resolve})}))!
  try {
    const duplicate = state.refresh()
    assert.deepEqual(calls,['a'])
    account.value = 'b'; await tick()
    assert.deepEqual(state.metadata.value,{})
    pending.b(data('b')); await tick()
    pending.a(data('a')); await duplicate; await tick()
    assert.equal(state.metadata.value.seasons[0].title, 'b')
    assert.equal(state.loading.value, false)
  } finally { scope.stop() }
})

test('失败明确提示且可重试，刷新失败保留已有选项，不影响草稿', async () => {
  const scope = effectScope(), account = ref('a'); let fail = true
  const state = scope.run(() => usePublishMetadata(account, async id => { if(fail) throw new Error('登录失效，请重新扫码'); return data(id) }))!
  try {
    await tick()
    assert.match(state.error.value,/登录失效/); assert.equal(state.ready.value,false)
    fail = false; await state.refresh()
    assert.equal(state.error.value,''); assert.equal(state.ready.value,true)
    fail = true; await state.refresh()
    assert.match(state.error.value,/登录失效/)
    assert.equal(state.metadata.value.seasons[0].title,'a')
    account.value = ''; await tick()
    assert.deepEqual(state.metadata.value,{}); assert.equal(state.ready.value,false)
  } finally { scope.stop() }
})

test('离开页面后不再应用迟到响应；异常结构不是成功的空列表', async () => {
  const scope = effectScope(), account = ref('a'); let release: (value:any) => void = () => {}
  const state = scope.run(() => usePublishMetadata(account, () => new Promise(resolve => {release=resolve})))!
  scope.stop(); release(data('a')); await tick()
  assert.equal(state.ready.value,false)
  const second = effectScope()
  const invalid = second.run(() => usePublishMetadata(account, async () => ({})))!
  await tick(); assert.match(invalid.error.value,/响应不完整/); second.stop()
})

test('创建后的刷新不复用创建前的慢列表', async () => {
  const scope=effectScope(), account=ref('a'), pending: ((value:any) => void)[] = []
  const state=scope.run(() => usePublishMetadata(account, () => new Promise(resolve => pending.push(resolve))))!
  try {
    const refresh=state.refreshAfterMutation()
    pending[0]({...data('a'),seasons:[]});await tick()
    assert.equal(pending.length,2)
    pending[1](data('new'));await refresh
    assert.equal(state.metadata.value.seasons[0].title,'new')
  } finally {scope.stop()}
})
