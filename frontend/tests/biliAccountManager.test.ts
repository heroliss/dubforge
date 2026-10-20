import assert from 'node:assert/strict'
import test, { before, after, mock } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, defineComponent, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
import { createMemoryHistory, createRouter, RouterView } from 'vue-router'
import { NDialogProvider, NMessageProvider, useDialog } from 'naive-ui'

let server: ViteDevServer, BiliAccountManager: any
before(async () => {
  server = await createServer({ root:fileURLToPath(new URL('..', import.meta.url)),
    configFile:false, plugins:[vue()], appType:'custom', server:{middlewareMode:true,hmr:false,watch:null,ws:false} })
  ;({default:BiliAccountManager} = await server.ssrLoadModule('/src/components/BiliAccountManager.vue'))
  mock.method(globalThis, 'fetch', () => { throw new Error('测试禁止未声明网络请求') })
})
after(async () => { mock.restoreAll(); await server?.close() })

async function render(seed:(page:any)=>void=()=>{}, active=true) {
  let page:any, props:any
  const dialogs:any[]=[], events:{guard:boolean[];busy:boolean[];dirty:boolean[]}={guard:[],busy:[],dirty:[]}
  const router=createRouter({history:createMemoryHistory(),routes:[
    {path:'/settings',component:BiliAccountManager,props:{active,
      onGuard:(value:boolean)=>events.guard.push(value),
      'onBusy-change':(value:boolean)=>events.busy.push(value),
      'onDirty-change':(value:boolean)=>events.dirty.push(value)}}
  ]})
  await router.push('/settings?section=bilibili')
  const Probe=defineComponent({setup(){
    const dialog=useDialog()
    dialog.warning=dialog.info=((options:any)=>{dialogs.push(options);return {destroy(){}}}) as any
    return ()=>h(RouterView)
  }})
  const app=createSSRApp({render:()=>h(NMessageProvider,null,{default:()=>h(NDialogProvider,null,{default:()=>h(Probe)})})})
  app.use(router);setup(app)
  app.mixin({created(){if(this.$options.__name==='BiliAccountManager'){
    page=this.$.setupState;props=this.$.props
    page.catalog={accounts:[{id:'123',name:'测试账号'}],templates:[],defaults:{},worker_ready:true}
    page.catalogReady=true;page.catalogLoading=false;seed(page)
  }}})
  return {html:await renderToString(app),page,props,dialogs,events}
}

const verifiedAccount = (id='123') => ({status:'verified',id,name:'已核验昵称',checked_at:'2026-10-08T02:03:04+00:00'})

test('账号仅人工检查，成功显示最近验证时间，不读取合集或修改账号目录',async t=>{
  const {page,html}=await render(p=>{p.tab='accounts'})
  assert.match(html,/尚未检查登录状态/)
  assert.match(html,/data-account-check="unverified"/)
  const requests:any[]=[]
  t.mock.method(globalThis,'fetch',async(url,options)=>{requests.push({url:String(url),method:options?.method,body:JSON.parse(String(options?.body))});return {ok:true,json:async()=>verifiedAccount()}})
  await page.checkAccount('123')
  assert.deepEqual(requests,[{url:'/api/publishing/accounts/123/check',method:'POST',body:{}}])
  assert.deepEqual({...page.accountChecks['123']},{status:'verified',name:'已核验昵称',checked_at:'2026-10-08T02:03:04+00:00'})
  assert.match(page.accountCheckLabel('123'),/最近验证成功/)
  const completed=await render(p=>{p.tab='accounts';p.accountChecks={'123':page.accountChecks['123']}})
  assert.match(completed.html,/data-account-check="verified"/)
  assert.match(completed.html,/最近验证成功/)
  assert.match(completed.html,/当时有效/)
})

test('仅明确登录过期显示失效，网络、证书、凭据和平台响应错误保持未确认',async t=>{
  for(const code of ['PUBLISH_LOGIN_EXPIRED','PUBLISH_TLS','PUBLISH_CREDENTIAL','PUBLISH_ACCOUNT_RESPONSE','PUBLISH_ACCOUNT_MISMATCH','PUBLISH_VERIFICATION','NETWORK_ERROR']) {
    const {page}=await render(p=>{p.tab='accounts'})
    t.mock.method(globalThis,'fetch',async()=>({ok:false,json:async()=>({code,message:'检查服务暂未返回有效账号<script>内容</script>'})}))
    await page.checkAccount('123')
    assert.equal(page.accountChecks['123'].status,code==='PUBLISH_LOGIN_EXPIRED'?'expired':'failed')
    assert.equal(page.accountChecks['123'].code,code)
    assert.equal(page.catalog.accounts[0].id,'123')
    const result=await render(p=>{p.tab='accounts';p.accountChecks=page.accountChecks})
    assert.match(result.html,/&lt;script&gt;内容&lt;\/script&gt;/)
    assert.doesNotMatch(result.html,/<script>内容/)
    if(code==='PUBLISH_LOGIN_EXPIRED')assert.match(result.html,/登录已失效，请重新登录/)
    else {assert.match(result.html,/登录是否有效尚未确认/);assert.doesNotMatch(result.html,/登录已失效/)}
  }
})

test('畸形成功响应与账号不一致不能冒充已验证，保留原已保存账号',async t=>{
  const {page}=await render(p=>{p.tab='accounts'})
  for(const result of [{}, {...verifiedAccount(),name:''}, {...verifiedAccount(),checked_at:'not-date'}, verifiedAccount('456')]) {
    t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>result}))
    await page.checkAccount('123')
    assert.equal(page.accountChecks['123'].status,'failed')
    assert.equal(page.accountChecks['123'].code,result.id==='456'?'PUBLISH_ACCOUNT_MISMATCH':'PUBLISH_ACCOUNT_RESPONSE')
    assert.equal(page.catalog.accounts[0].id,'123')
  }
})

test('账号检查重复点击只发一次；切换检查账号后迟到的旧成功不能覆盖新账号',async t=>{
  const {page}=await render(p=>{p.tab='accounts';p.catalog.accounts.push({id:'456',name:'第二账号'})})
  const waits:{url:string;finish:(value:any)=>void}[]=[]
  t.mock.method(globalThis,'fetch',url=>new Promise(resolve=>{waits.push({url:String(url),finish:resolve})}))
  const first=page.checkAccount('123');await page.checkAccount('123')
  assert.equal(waits.length,1);assert.equal(page.checkingAccount,'123')
  const second=page.checkAccount('456')
  assert.equal(waits.length,2);assert.equal(page.checkingAccount,'456');assert.equal(page.accountChecks['123'],undefined)
  waits[1].finish({ok:true,json:async()=>verifiedAccount('456')});await second
  waits[0].finish({ok:true,json:async()=>verifiedAccount()});await first
  assert.equal(page.accountChecks['123'],undefined);assert.equal(page.accountChecks['456'].status,'verified')
  assert.equal(page.checkingAccount,'')
})

test('离开再返回账号工作区后旧请求无效；忙态、未读取或非账号工作区不发检查',async t=>{
  const {page}=await render(p=>{p.tab='accounts'})
  const waits:((value:any)=>void)[]=[]
  t.mock.method(globalThis,'fetch',url=>String(url)==='/api/projects'
    ?Promise.resolve({ok:true,json:async()=>[]})
    :new Promise(resolve=>{waits.push(resolve)}))
  const pending=page.checkAccount('123')
  page.switchWorkspace('studio');page.switchWorkspace('accounts')
  waits[0]({ok:true,json:async()=>verifiedAccount()});await pending
  assert.equal(page.accountChecks['123'],undefined);assert.equal(page.checkingAccount,'')
  page.busy=true;await page.checkAccount('123');page.busy=false
  page.catalogReady=false;await page.checkAccount('123');page.catalogReady=true
  page.tab='studio';await page.checkAccount('123')
  assert.equal(waits.length,1)
})

test('重新登录清除旧验证并忽略先前检查，相同目录重读保留后来的完成结果',async t=>{
  const {page}=await render(p=>{p.tab='accounts';p.accountChecks={'123':{status:'verified',name:'原昵称',checked_at:verifiedAccount().checked_at}}})
  let completeCheck:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',async url=>{
    const path=String(url)
    if(path.endsWith('/check'))return new Promise(resolve=>{completeCheck=resolve})
    if(path.endsWith('/login'))return {ok:true,json:async()=>({id:'new-login'})}
    if(path.endsWith('/catalog'))return {ok:true,json:async()=>({accounts:[{id:'123',name:'测试账号'}],templates:[],defaults:{},worker_ready:true})}
    throw new Error(`不应调用：${path}`)
  })
  const pending=page.checkAccount('123');await page.login()
  assert.deepEqual(page.accountChecks,{})
  completeCheck({ok:true,json:async()=>verifiedAccount()});await pending
  assert.deepEqual(page.accountChecks,{})
  assert.equal(page.loginId,'new-login');page.loginId=''
  page.accountChecks={'123':{status:'verified',name:'原昵称',checked_at:verifiedAccount().checked_at}}
  await page.refreshCatalog()
  assert.deepEqual(page.accountChecks,{'123':{status:'verified',name:'原昵称',checked_at:verifiedAccount().checked_at}})
  assert.match(page.accountCheckLabel('123'),/最近验证成功/)
})

test('再次检查同账号以新请求结果为准，旧账号目录下的迟到结果不能恢复验证',async t=>{
  const {page}=await render(p=>{p.tab='accounts'})
  const waits:((value:any)=>void)[]=[]
  t.mock.method(globalThis,'fetch',url=>String(url).endsWith('/catalog')
    ?Promise.resolve({ok:true,json:async()=>({accounts:[{id:'123',name:'重新读取账号'}],templates:[],defaults:{},worker_ready:true})})
    :new Promise(resolve=>{waits.push(resolve)}))
  const old=page.checkAccount('123');await page.refreshCatalog()
  const current=page.checkAccount('123')
  waits[1]({ok:false,json:async()=>({code:'PUBLISH_LOGIN_EXPIRED',message:'请重新登录'})});await current
  waits[0]({ok:true,json:async()=>verifiedAccount()});await old
  assert.equal(page.accountChecks['123'].status,'expired');assert.equal(page.checkingAccount,'')
  assert.equal(page.catalog.accounts[0].name,'重新读取账号')
})

test('登录成功目录写回后，先前慢读取不能回滚账号列表或卡住加载状态',async t=>{
  const {page}=await render(p=>{p.tab='accounts'})
  let finish:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',()=>new Promise(resolve=>{finish=resolve}))
  const old=page.refreshCatalog();assert.equal(page.catalogLoading,true)
  page.applyCatalog({accounts:[{id:'456',name:'新登录账号'}],templates:[],defaults:{},worker_ready:true})
  assert.equal(page.catalogLoading,false)
  finish({ok:true,json:async()=>({accounts:[{id:'123',name:'旧账号'}],templates:[],defaults:{},worker_ready:true})});await old
  assert.equal(page.catalog.accounts[0].id,'456');assert.equal(page.catalogLoading,false)
})

test('同账号身份与revision不变时重读保留验证时间，目录顺序和其他设置无关',async t=>{
  const completed={status:'verified',name:'已核验昵称',checked_at:verifiedAccount().checked_at}
  const {page}=await render(p=>{
    p.tab='accounts';p.catalog.accounts=[{id:'123',name:'测试账号',revision:3},{id:'456',name:'第二账号',revision:8}]
    p.accountChecks={'123':completed,'456':{status:'expired',code:'PUBLISH_LOGIN_EXPIRED',error:'上次检查已失效'}}
  })
  t.mock.method(globalThis,'fetch',async()=>({ok:true,json:async()=>({accounts:[{id:'456',name:'第二账号',revision:8},{id:'123',name:'测试账号',revision:3}],templates:[{name:'新模板'}],defaults:{title:'目录刷新文案'},worker_ready:true})}))
  await page.refreshCatalog()
  assert.deepEqual({...page.accountChecks['123']},completed)
  assert.equal(page.accountChecks['456'].status,'expired')
  assert.match(page.accountCheckLabel('123'),/当时有效/)
  assert.equal(page.catalogLoading,false)
})

test('目录中的账号删除、身份或revision变化清除对应结果，保留未变化的账号',async()=>{
  for(const mode of ['removed','renamed','revision']) {
    const completed={status:'verified',name:'已核验昵称',checked_at:verifiedAccount().checked_at}
    const {page}=await render(p=>{
      p.tab='accounts';p.catalog.accounts=[{id:'123',name:'测试账号',revision:3},{id:'456',name:'第二账号',revision:8}]
      p.accountChecks={'123':{...completed},'456':{...completed}}
    })
    const changed=mode==='removed'?[]:[{id:'123',name:mode==='renamed'?'更名账号':'测试账号',revision:mode==='revision'?4:3}]
    page.applyCatalog({accounts:[...changed,{id:'456',name:'第二账号',revision:8}],templates:[],defaults:{},worker_ready:true})
    assert.equal(page.accountChecks['123'],undefined)
    assert.deepEqual({...page.accountChecks['456']},completed)
    if(mode!=='removed')assert.match(page.accountCheckLabel('123'),/尚未检查/)
  }
})

test('同一目录重读仍丢弃进行中的checking与迟到响应，不影响其他账号的完成验证',async t=>{
  const completed={status:'verified',name:'第二账号',checked_at:verifiedAccount().checked_at}
  const {page}=await render(p=>{
    p.tab='accounts';p.catalog.accounts=[{id:'123',name:'测试账号',revision:3},{id:'456',name:'第二账号',revision:8}]
    p.accountChecks={'456':completed}
  })
  let finish:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',url=>String(url).endsWith('/catalog')
    ?Promise.resolve({ok:true,json:async()=>({accounts:[{id:'123',name:'测试账号',revision:3},{id:'456',name:'第二账号',revision:8}],templates:[],defaults:{},worker_ready:true})})
    :new Promise(resolve=>{finish=resolve}))
  const pending=page.checkAccount('123');assert.equal(page.accountChecks['123'].status,'checking')
  await page.refreshCatalog()
  assert.equal(page.accountChecks['123'],undefined);assert.equal(page.checkingAccount,'')
  assert.deepEqual({...page.accountChecks['456']},completed)
  finish({ok:true,json:async()=>verifiedAccount()});await pending
  assert.equal(page.accountChecks['123'],undefined)
  assert.deepEqual({...page.accountChecks['456']},completed)
})

test('二维码使用独立白底与零内边距SVG，状态明确区分手机确认和本地保存', async () => {
  const {html} = await render(p => {
    p.tab = 'accounts'; p.loginId = 'fixture'
    p.loginState = {status:'waiting',url:'https://passport.bilibili.com/fixture'}
  })
  assert.match(html, /class="qr-frame"/)
  assert.match(html, /padding:0px/)
  assert.match(html, /<svg(?=[^>]*width="180")(?=[^>]*height="180")/)
  assert.match(html, /请使用B站App扫码/)
  for (const [status, text] of [['verifying','手机端已确认，正在校验账号'],['saving','账号已验证，正在安全保存'],['connected','账号已连接']]) {
    const result = await render(p => {p.tab = 'accounts'; p.loginState = {status}})
    assert.ok(result.html.includes(text))
    assert.doesNotMatch(result.html, /class="qr-frame"/)
  }
})

test('登录失败显示真实脱敏原因；迟到的旧轮询不覆盖新二维码', async t => {
  const {html,page} = await render(p => {
    p.tab = 'accounts'; p.loginId = 'old'
    p.loginState = {status:'failed',error:'手机端已确认，但网页连接未完成：HTTPS证书校验失败',code:'PUBLISH_TLS'}
  })
  assert.match(html, /HTTPS证书校验失败/)
  assert.match(html, /PUBLISH_TLS/)
  let release: (value: any) => void = () => {}
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => {release = resolve}))
  const pending = page.pollLogin()
  page.loginId = 'new'; page.loginState = {status:'generating'}
  release({ok:true,json:async () => ({status:'connected'})})
  await pending
  assert.equal(page.loginId, 'new')
  assert.equal(page.loginState.status, 'generating')
})

test('后端重启丢失会话后，清除旧二维码并解锁重新获取按钮', async t => {
  const {page} = await render(p => {p.loginId = 'old';p.loginState = {status:'waiting',url:'old-code'}})
  t.mock.method(globalThis, 'fetch', async () => ({ok:false,json:async () => ({code:'PUBLISH_LOGIN_NOT_FOUND',message:'登录会话已结束，请重新获取二维码'})}))
  await page.pollLogin()
  assert.equal(page.loginId, '')
  assert.equal(page.loginState.status, 'expired')
  assert.equal(page.loginState.url, undefined)
})


test('账号页明确区分保存与平台验证，管理已有稿件入口不自动加载远端资料',async()=>{
  const {page,html}=await render()
  assert.match(html,/UID 123 · 已保存/)
  assert.match(html,/登录是否仍有效，以平台校验结果为准/)
  assert.match(html,/管理已有稿件/)
  assert.doesNotMatch(html,/已连接/)
  assert.equal(page.tab,'accounts')
  assert.deepEqual(page.accountChecks,{})
})

test('账户区域隐藏后拒绝新的平台检查或二维码请求',async t=>{
  const {page}=await render(()=>{},false)
  const requests:string[]=[]
  t.mock.method(globalThis,'fetch',url=>{requests.push(String(url));throw new Error('隐藏区域不应发起请求')})
  await page.checkAccount('123');await page.login()
  assert.deepEqual(requests,[])
  assert.equal(page.loginId,'');assert.deepEqual(page.accountChecks,{})
})

test('隐藏账号区域期间抵达的旧验证响应不能宣布登录成功',async t=>{
  const {page,props}=await render()
  let finish:(value:any)=>void=()=>{}
  t.mock.method(globalThis,'fetch',()=>new Promise(resolve=>{finish=resolve}))
  const pending=page.checkAccount('123')
  // SSR 不执行普通 watcher；响应适用条件本身必须检查区域仍处于活动状态。
  props.active=false
  finish({ok:true,json:async()=>verifiedAccount()});await pending
  assert.notEqual(page.accountChecks['123']?.status,'verified')
})
