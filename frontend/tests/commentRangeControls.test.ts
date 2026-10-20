import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'
let server:ViteDevServer, Control:any
before(async()=>{
 server=await createServer({root:fileURLToPath(new URL('..',import.meta.url)),configFile:false,plugins:[vue()],appType:'custom',server:{middlewareMode:true,hmr:false,watch:null,ws:false}})
 Control=(await server.ssrLoadModule('/src/components/ParamControl.vue')).default
})
after(()=>server.close())
for(const prefix of ['comments.','comments.overlay.'])test(prefix+'高度用可扩展百分比控件，输入与滑块同步传值',async()=>{
 let slider:any,number:any
 const changes:number[]=[]
 const field={path:prefix+'height_percent',label:'目标最大高度（画面 %）',type:'float',min:1,max:100,step:1}
 const app=createSSRApp({render:()=>h(Control,{field,modelValue:30,'onUpdate:modelValue':(value:number)=>changes.push(value)})})
 setup(app)
 app.mixin({created(){if(this.$options.name==='Slider')slider=this;if(this.$options.name==='InputNumber')number=this}})
 const html=await renderToString(app)
 assert.ok(slider);assert.ok(number)
 assert.equal(slider.$props.min,1);assert.equal(slider.$props.max,100)
 assert.equal(slider.$props.formatTooltip(40),'40% 画面高度（可扩展）')
 assert.equal(number.$props.min,1);assert.equal(number.$props.showButton,false)
 assert.doesNotMatch(html,/设为自动|>自动</)
 slider.$emit('update:value',40);number.$emit('update:value',55)
 assert.deepEqual(changes,[40,55])
})
