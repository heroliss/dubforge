import assert from 'node:assert/strict'
import test from 'node:test'
import { legacyPublishTarget } from '../src/utils/publishNavigation.ts'

test('旧发布中心入口去次级记录页，明确稿件编号可直达保留记录',()=>{
  assert.deepEqual(legacyPublishTarget({query:{},hash:''}),{path:'/publications',query:{},hash:''})
  assert.deepEqual(legacyPublishTarget({query:{publication:'receipt a',filter:'failed'},hash:'#receipt'}),{
    path:'/publications/receipt%20a',query:{publication:'receipt a',filter:'failed'},hash:'#receipt',
  })
})

test('旧账号与原BV链接迁往设置并保留账号、BV及其他上下文',()=>{
  assert.deepEqual(legacyPublishTarget({query:{tab:'studio',account:'123',bvid:'BV123'},hash:''}),{
    path:'/settings',query:{tab:'studio',account:'123',bvid:'BV123',section:'bilibili',manage:'studio'},hash:'',
  })
  assert.deepEqual(legacyPublishTarget({query:{account_id:'123'},hash:'#accounts'}),{
    path:'/settings',query:{account_id:'123',section:'bilibili'},hash:'#accounts',
  })
  assert.deepEqual(legacyPublishTarget({query:{view:'studio',account:'123'},hash:''}),{
    path:'/settings',query:{view:'studio',account:'123',section:'bilibili',manage:'studio'},hash:'',
  })
})
