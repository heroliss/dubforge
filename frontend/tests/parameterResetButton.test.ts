import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server:ViteDevServer,Reset:any
before(async()=>{
  server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
  Reset=(await server.ssrLoadModule('/src/components/ParameterResetButton.vue')).default
})
after(async()=>{await server.close()})
async function render(disabled=false,action?:string){
  let instance:any,count=0
  const app=createSSRApp({render:()=>h(Reset,{label:'人声音量',disabled,action,help:'移除此项覆盖，保存后跟随项目默认值',onReset:()=>count++})})
  setup(app)
  app.mixin({created(){if(this.$options.__name==='ParameterResetButton')instance=this}})
  return {html:await renderToString(app),reset:()=>instance.$.setupState.reset(),count:()=>count}
}
test('统一恢复按钮使用语义标签与装饰SVG，点击只发出对应恢复事件',async()=>{
  const result=await render(false,'恢复项目默认')
  assert.match(result.html,/aria-label="人声音量恢复项目默认"/)
  assert.match(result.html,/title="移除此项覆盖，保存后跟随项目默认值"/)
  assert.match(result.html,/<svg[^>]*aria-hidden="true"/)
  result.reset();assert.equal(result.count(),1)
})
test('继承或锁定时恢复入口保持可见并禁用，直接触发也不发出恢复事件',async()=>{
  const result=await render(true)
  assert.match(result.html,/aria-label="人声音量恢复继承值"/)
  assert.match(result.html,/<button[^>]*disabled/)
  result.reset();assert.equal(result.count(),0)
})
