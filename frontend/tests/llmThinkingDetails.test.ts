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
  { name: '方舟', enabled: true, supports_low: true, removes_thinking: true, params_valid: true, effort: 'high', has_thinking: true, thinking_type: 'enabled' },
  { name: 'GLM', enabled: true, supports_low: true, removes_thinking: false, params_valid: true, effort: 'max', has_thinking: true, thinking_type: 'enabled' },
  { name: '备用', enabled: true, supports_low: false, params_valid: true, effort: 'high', has_thinking: false },
  { name: '已停用', enabled: false, supports_low: true, params_valid: true, effort: 'medium', has_thinking: false },
]
async function render(mode: string, profile: string, path = 'split.thinking_mode', currentProfiles: any[] = profiles) {
  const field = { path, label: '思考强度', type: 'select', options: [
    { label: '低思考强度', value: 'low' }, { label: '跟随自定义 JSON', value: 'follow_profile' } ], thinking_profiles: currentProfiles }
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

test('GLM低思考仅覆盖 effort 并保留 thinking，缺少接口字段的旧摘要不误报移除', async () => {
  for (const path of ['split.thinking_mode', 'translate.thinking_mode']) {
    const glm = await render('low', 'GLM', path)
    assert.match(glm, /实际请求：low/); assert.match(glm, /覆盖 JSON 的 max/)
    assert.match(glm, /保留 thinking=enabled/); assert.doesNotMatch(glm, /移除 thinking/)
  }
  const legacy = await render('low', '旧摘要', 'split.thinking_mode', [{ name: '旧摘要', enabled: true,
    supports_low: true, params_valid: true, effort: 'high', has_thinking: true, thinking_type: 'enabled' }])
  assert.match(legacy, /刷新配置预览确认/); assert.doesNotMatch(legacy, /移除 thinking|保留 thinking/)
})
