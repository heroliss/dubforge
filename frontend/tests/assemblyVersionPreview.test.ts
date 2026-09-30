import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { build } from 'esbuild'
import { parse } from '@vue/compiler-sfc'
const root=fileURLToPath(new URL('..',import.meta.url))
const {descriptor}=parse(await readFile(new URL('../src/components/AssemblyVersionPreview.vue',import.meta.url),'utf8'))
const result=await build({stdin:{contents:`import { reactive, unmountHooks } from 'vue';
 const fixtureProps=reactive(__PROPS__),defineProps=()=>fixtureProps;
 ${descriptor.scriptSetup!.content}
 export {fixtureProps,context,seconds,image,outdated,error,loading,render,beginScrub,endScrub,unmountHooks};`,loader:'ts',resolveDir:`${root}/src/components`},bundle:true,write:false,format:'esm',platform:'node',plugins:[{name:'boundaries',setup(b){
 b.onResolve({filter:/^(vue|naive-ui)$|\.vue$|\/stores\/nav$/},a=>({path:a.path,namespace:'test'}))
 b.onLoad({filter:/.*/,namespace:'test'},a=>({contents:a.path==='vue'?`export {ref,computed,watch,reactive} from '@vue/reactivity';export const unmountHooks=[],onMounted=()=>{},onBeforeUnmount=f=>unmountHooks.push(f);`:
  a.path==='naive-ui'?`export const useMessage=()=>({success(){}});`:a.path.endsWith('/stores/nav')?`export const useNavStore=()=>({stepMap:{assemble:{artifacts:[]}}});`:'export default {};',resolveDir:root}))
}}]})
let serial=0
async function fixture(t:any){
 t.mock.timers.enable({apis:['setTimeout']})
 const props={uid:'fixture',version:{id:'p1',name:'中文字幕',dubbing:false,burn_subtitles:true,include_comments:false,overrides:{}},overlayRevision:'r1'}
 const page=await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text.replace('__PROPS__',JSON.stringify(props))+`\n//${serial++}`).toString('base64')}`)
 page.context.value={duration:60,subtitles:[],comments:[]};t.after(()=>page.unmountHooks.forEach((f:()=>void)=>f()));return page
}
async function flush(){for(let i=0;i<25;i++)await Promise.resolve()}
test('版本预览串行且丢弃旧响应，零透明度和不同字幕类型保留到请求',async t=>{
 const calls:any[]=[],replies:any[]=[]
 t.mock.method(globalThis,'fetch',(_url:any,init:any)=>{calls.push(JSON.parse(init.body));return new Promise(resolve=>replies.push(resolve))})
 const p=await fixture(t);const first=p.render()
 p.fixtureProps.version.overrides['assemble.subtitle_track']='bilingual';p.fixtureProps.version.overrides['comments.background_opacity']=0
 t.mock.timers.tick(500);await flush();assert.equal(calls.length,1)
 replies[0](new Response('old'));await first;await flush();assert.equal(p.image.value,'')
 t.mock.timers.tick(500);await flush();assert.equal(calls.length,2)
 assert.equal(calls[1].version.overrides['assemble.subtitle_track'],'bilingual');assert.equal(calls[1].version.overrides['comments.background_opacity'],0)
 replies[1](new Response('new'));await flush();assert.ok(p.image.value);assert.equal(p.outdated.value,false)
 p.seconds.value=10;assert.equal(p.outdated.value,true)
})
test('拖动期间不编码；松开只预览最终位置，失败不循环重试',async t=>{
 const calls:any[]=[]
 t.mock.method(globalThis,'fetch',async(_url:any,init:any)=>{calls.push(JSON.parse(init.body));return new Response(JSON.stringify({message:'预览失败'}),{status:400})})
 const p=await fixture(t);p.beginScrub();p.seconds.value=4;t.mock.timers.tick(1000);await flush();assert.equal(calls.length,0)
 p.seconds.value=12;p.endScrub();t.mock.timers.tick(500);await flush();assert.equal(calls.length,1);assert.equal(calls[0].seconds,12);assert.match(p.error.value,/预览失败/)
 t.mock.timers.tick(5000);await flush();assert.equal(calls.length,1)
})
