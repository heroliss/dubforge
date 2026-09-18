<script setup lang="ts">
// 设置页: LLM 多组配置池 (任意 OpenAI 兼容服务商, 按顺序降级) + 其余组只读概览
// 注意: v-for 容器一律用原生 flex div —— naive-ui 2.45 的 n-space 在嵌套 n-card
// 场景会把组件子项渲染成空注释 (已踩坑, 见 git 历史)
import { onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'

const message = useMessage()
const cfg = ref<any>(null)
const saving = ref(false)
const testing = ref(false)
const testResult = ref<null | { ok: boolean; text: string }>(null)
const loadError = ref('')
const testResults = ref<Record<number, any>>({})
const testingIdx = ref<number | 'all' | null>(null)

async function runTest(body: any) {
  const r = await fetch('/api/settings/test_llm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  const j = await r.json()
  if (!r.ok) throw new Error(j.message || j.detail || '测试请求失败')
  for (const res of (j.results ?? [])) {
    testResults.value[res.index] = res
  }
  return j
}
async function testOne(i: number) {
  await save(false)
  testingIdx.value = i
  try { await runTest({ index: i }) }
  catch (e: any) { message.error(e.message) }
  finally { testingIdx.value = null }
}

const profiles = ref<any[]>([])
const form = ref({
  temperature: 0.1 as number, max_workers: 1 as number,
  timeout: 300 as number, thinking: 'auto',
})

const THINKING_OPTIONS = [
  { label: 'auto — 不传参数（跟随服务端默认）', value: 'auto' },
  { label: 'disabled — 关闭思考（若服务端支持）', value: 'disabled' },
  { label: 'enabled — 开启思考', value: 'enabled' },
]

// 官方依据的参数模板 (选定后自动填入, 可再手动改; 其余服务商请查其官方文档)
const PARAM_TPLS: Record<string, { label: string, params: any }> = {
  glm53: { label: 'GLM-5.3-flash 官方推荐 (思考型)',
           params: { temperature: 1, top_p: 0.95, reasoning_effort: 'max' } },
  low_random: { label: '翻译通用 (低随机, 更稳定)', params: { temperature: 0.2 } },
  clear: { label: '清空', params: {} },
}
function applyTpl(p: any, key: string) {
  if (key && PARAM_TPLS[key]) p.paramsJson = JSON.stringify(PARAM_TPLS[key].params, null, 1)
}

function addProfile() {
  profiles.value.push({ name: `配置${profiles.value.length + 1}`,
                        base_url: '', api_key: '', model: '', enabled: true,
                        params: {}, paramsJson: '{}' })
}
function delProfile(i: number) { profiles.value.splice(i, 1) }
function move(i: number, d: number) {
  const j = i + d
  if (j < 0 || j >= profiles.value.length) return
  ;[profiles.value[i], profiles.value[j]] = [profiles.value[j], profiles.value[i]]
}

onMounted(async () => {
  try {
    const _r = await fetch('/api/settings')
    if (!_r.ok) throw new Error('HTTP ' + _r.status)
    cfg.value = await _r.json()
    const llm = cfg.value.llm
    profiles.value = (llm.profiles?.length ? llm.profiles :
      [{ name: '默认', base_url: llm.base_url, api_key: llm.api_key,
         model: llm.model, enabled: true }]).map((p: any) => ({
      ...p, params: p.params ?? {}, paramsJson: JSON.stringify(p.params ?? {}, null, 1),
    }))
    form.value.temperature = Number(llm.temperature)
    form.value.max_workers = Number(llm.max_workers)
    form.value.timeout = Number(llm.timeout ?? 300)
    form.value.thinking = llm.thinking ?? 'auto'
  } catch (e: any) {
    loadError.value = `加载失败 (${e.message}) —— 请确认后端窗口正在运行 (8600)`
  }
})

async function save(showTip = true) {
  saving.value = true
  try {
    const list = profiles.value.map((p: any) => {
      let params: any = {}
      try { params = JSON.parse(p.paramsJson || '{}') } catch { /* 保持空 */ }
      const { paramsJson, ...rest } = p
      return { ...rest, params }
    })
    await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: {
        'llm.profiles': list,
        'llm.temperature': form.value.temperature,
        'llm.max_workers': form.value.max_workers,
        'llm.timeout': form.value.timeout,
        'llm.thinking': form.value.thinking,
      } }),
    })
    if (showTip) message.success('已保存')
  } finally { saving.value = false }
}

async function testConnection() {
  await save(false)
  testing.value = true
  testResults.value = {}
  try {
    const j = await runTest({})
    const okN = (j.results ?? []).filter((r: any) => r.ok).length
    message.info(`并行测试完成: ${okN}/${j.results.length} 组可用`)
  } catch (e: any) {
    message.error(e.message)
  } finally { testing.value = false }
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-h1>设置</n-h1>
    <n-alert type="info" :show-icon="false" style="margin-bottom: 16px">
      配置统一保存在仓库根 <n-code>config.yaml</n-code>（含密钥，不入库）。
      LLM 采用 OpenAI 兼容协议 —— 修改 <n-code>base_url</n-code> + <n-code>api_key</n-code> + 模型名
      即可接入任意服务商（智谱 / 火山方舟 / DeepSeek / OpenAI / 本地 vLLM 等）。
      可保存多组配置并按顺序启用：前面的失败时自动切换到下一个。
    </n-alert>

    <n-alert v-if="loadError" type="error">{{ loadError }}</n-alert>
    <n-spin :show="!cfg && !loadError">
      <div v-if="cfg" style="display:flex;flex-direction:column;gap:16px">
        <!-- ============ LLM 配置池 (可编辑) ============ -->
        <n-card title="🌐 大模型 (LLM) — 翻译与分句" size="small">
          <div style="display:flex;flex-direction:column;gap:8px">
            <div v-for="(p, i) in profiles" :key="i"
                 style="border:1px solid #2a343f; border-radius:8px; padding:10px 12px;
                        opacity: p.enabled ? 1 : .55">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
                <n-switch v-model:value="p.enabled" size="small">
                  <template #checked>启用</template><template #unchecked>停用</template>
                </n-switch>
                <n-input v-model:value="p.name" size="small" placeholder="名称"
                         style="width: 150px" />
                <n-button size="tiny" quaternary :disabled="i === 0"
                          @click="move(i, -1)">↑</n-button>
                <n-button size="tiny" quaternary :disabled="i === profiles.length - 1"
                          @click="move(i, 1)">↓</n-button>
                <n-button size="tiny" quaternary type="error"
                          @click="delProfile(i)">删除</n-button>
                <n-button size="tiny" quaternary
                          :loading="testingIdx === i || testingIdx === 'all'"
                          @click.stop="testOne(i)">测试</n-button>
                <n-text depth="3" style="font-size: 11px">
                  按此列表顺序依次尝试，前面的不可用自动切到下一个
                </n-text>
              </div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px 12px">
                <div>
                  <n-text depth="3" style="font-size:12px">接口地址</n-text>
                  <n-input v-model:value="p.base_url" size="small"
                           placeholder="https://ark.cn-beijing.volces.com/api/plan/v3" />
                </div>
                <div>
                  <n-text depth="3" style="font-size:12px">模型名</n-text>
                  <n-input v-model:value="p.model" size="small"
                           placeholder="如 glm-5.3-flash / deepseek-chat" />
                </div>
              </div>
              <div style="margin-top:8px">
                <n-text depth="3" style="font-size:12px">API Key</n-text>
                <n-input v-model:value="p.api_key" type="password"
                         show-password-on="click" size="small"
                         placeholder="密钥保存在本地 config.yaml" />
              </div>
              <div style="margin-top:8px">
                <n-text depth="3" style="font-size:12px">专属参数 (该服务商的模型参数, 原样合并进请求体)</n-text>
                <n-select size="small" :value="''" style="margin-top:2px"
                          :options="Object.entries(PARAM_TPLS).map(([k, v]) => ({ label: v.label, value: k }))"
                          placeholder="选择参数模板 (官方推荐值)…"
                          @update:value="(v: string) => applyTpl(p, v)" />
                <n-input v-model:value="p.paramsJson" type="textarea" size="small" :rows="2"
                         style="margin-top:4px"
                         placeholder='JSON, 如 {"temperature":1,"top_p":0.95,"reasoning_effort":"max"}' />
              </div>
              <n-alert v-if="testResults[i]" :type="testResults[i].ok ? 'success' : 'error'"
                       :show-icon="false" size="small" style="margin-top:6px">
                {{ testResults[i].ok
                   ? `✓ ${testResults[i].latency}s · ${testResults[i].model} · ${testResults[i].reply}`
                   : `✗ ${testResults[i].error}` }}
              </n-alert>
            </div>
            <n-button size="small" dashed block @click="addProfile">＋ 新增一组配置</n-button>
          </div>
          <n-space style="margin-top:12px">
            <n-button type="primary" :loading="saving" @click="save()">保存全部</n-button>
            <n-button :loading="testing" @click="testConnection">🔌 并行测试全部</n-button>
            <n-text depth="3" style="font-size: 12px">
              先保存当前表单值，再并行测试所有「启用」的配置；也可在每组配置卡上单独测试
            </n-text>
          </n-space>
          <n-alert v-if="testResult" :type="testResult.ok ? 'success' : 'error'"
                   :show-icon="false" style="margin-top: 10px">
            {{ testResult.text }}
          </n-alert>
        </n-card>

        <!-- ============ 只读概览 (M6 剩余) ============ -->
        <n-card title="下载 / 分离 / 转录 / 配音 / 成片" size="small">
          <n-descriptions :column="3" label-placement="left" bordered size="small">
            <n-descriptions-item label="最高分辨率">{{ cfg.download.resolution }}p</n-descriptions-item>
            <n-descriptions-item label="代理">{{ cfg.download.proxy || '(环境变量)' }}</n-descriptions-item>
            <n-descriptions-item label="ASR 模型">{{ cfg.asr.model }}</n-descriptions-item>
            <n-descriptions-item label="TTS 后端">{{ cfg.tts.backend }}</n-descriptions-item>
            <n-descriptions-item label="IndexTTS">{{ cfg.tts.indextts25.url }}</n-descriptions-item>
            <n-descriptions-item label="成片 Ducking">{{ cfg.assemble.ducking ? '开' : '关' }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </div>
    </n-spin>
  </n-layout-content>
</template>
