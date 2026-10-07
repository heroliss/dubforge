import assert from 'node:assert/strict'
import test, { before, after } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createServer, type ViteDevServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { setup } from '@css-render/vue3-ssr'

let server: ViteDevServer, Control: any
before(async () => {
  server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, plugins: [vue()], appType: 'custom',
    optimizeDeps: { noDiscovery: true, include: [] }, cacheDir: '.vite-test-thinking',
    server: { middlewareMode: true, hmr: false, watch: null, ws: false } })
  Control = (await server.ssrLoadModule('/src/components/ParamControl.vue')).default
})
after(() => server.close())

const profiles = [
  { name: '方舟', enabled: true, supports_low: true, params_valid: true, effort: 'high', has_thinking: true, thinking_type: 'enabled' },
  { name: '备用', enabled: true, supports_low: false, params_valid: true, effort: 'high', has_thinking: false },
  { name: '已停用', enabled: false, supports_low: true, params_valid: true, effort: 'medium', has_thinking: false },
]
async function render(mode: string, profile: string, path = 'split.thinking_mode') {
  const field = { path, label: '思考强度', type: 'select', options: [
    { label: '低思考强度', value: 'low' }, { label: '跟随自定义 JSON', value: 'follow_profile' } ], thinking_profiles: profiles }
  const app = createSSRApp({ render: () => h(Control, { field, modelValue: mode,
    values: { [path.replace('thinking_mode', 'llm_profile')]: profile } }) })
  setup(app)
  return renderToString(app)
}

test('真实控件显示自定义思考参数覆盖和移除，支持整理与翻译', async () => {
  for (const path of ['split.thinking_mode', 'translate.thinking_mode']) {
    const html = await render('low', '方舟', path)
    assert.match(html, /实际请求：low/)
    assert.match(html, /覆盖 JSON 的 high/)
    assert.match(html, /移除 thinking/)
    assert.match(html, /运行中的任务使用启动时配置/)
    assert.doesNotMatch(html, /自动 low 未适配|已停用/)
  }
})

test('跟随 JSON 保留 high 与 thinking，未适配模型不声称已降低强度', async () => {
  const follow = await render('follow_profile', '方舟')
  assert.match(follow, /跟随 JSON：high/); assert.match(follow, /thinking=enabled/)
  assert.doesNotMatch(follow, /覆盖 JSON|移除 thinking/)
  const unsupported = await render('low', '备用')
  assert.match(unsupported, /保留 JSON：high/); assert.match(unsupported, /自动 low 未适配/)
  assert.doesNotMatch(unsupported, /实际请求：low/)
})

test('降级链逐个预览，固定不存在或停用的组显示清楚', async () => {
  const chain = await render('low', '')
  assert.match(chain, /实际请求：low/); assert.match(chain, /自动 low 未适配/)
  assert.doesNotMatch(chain, /已停用/)
  for (const name of ['不存在', '已停用']) {
    assert.match(await render('low', name), /未找到已启用的模型配置/)
  }
})
