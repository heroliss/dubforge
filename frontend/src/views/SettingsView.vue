<script setup lang="ts">
// 设置页: LLM 多组配置池 (任意 OpenAI 兼容服务商, 按顺序降级) + 其余组只读概览
import { onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'

const message = useMessage()
const cfg = ref<any>(null)
const saving = ref(false)
const testing = ref(false)
const testResult = ref<null | { ok: boolean; text: string }>(null)

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

function addProfile() {
  profiles.value.push({ name: `配置${profiles.value.length + 1}`,
                        base_url: '', api_key: '', model: '', enabled: true })
}
function delProfile(i: number) { profiles.value.splice(i, 1) }
function move(i: number, d: number) {
  const j = i + d
  if (j < 0 || j >= profiles.value.length) return
  ;[profiles.value[i], profiles.value[j]] = [profiles.value[j], profiles.value[i]]
}

onMounted(async () => {
  cfg.value = await (await fetch('/api/settings')).json()
  const llm = cfg.value.llm
  profiles.value = (llm.profiles?.length ? llm.profiles :
    [{ name: '默认', base_url: llm.base_url, api_key: llm.api_key,
       model: llm.model, enabled: true }]).map((p: any) => ({ ...p }))
  form.value.temperature = Number(llm.temperature)
  form.value.max_workers = Number(llm.max_workers)
  form.value.timeout = Number(llm.timeout ?? 300)
  form.value.thinking = llm.thinking ?? 'auto'
})

async function save(showTip = true) {
  saving.value = true
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: {
        'llm.profiles': profiles.value,
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
  testResult.value = null
  try {
    const r = await fetch('/api/settings/test_llm', { method: 'POST' })
    const j = await r.json()
    testResult.value = j.ok
      ? { ok: true, text: `${j.latency}s · 回复: ${j.reply}` }
      : { ok: false, text: j.message + (j.suggestion ? ` — ${j.suggestion}` : '') }
  } catch (e: any) {
    testResult.value = { ok: false, text: `请求失败: ${e.message}` }
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

    <n-spin :show="!cfg">
      <n-space vertical size="large" v-if="cfg">
        <!-- ============ LLM 配置池 (可编辑) ============ -->
        <n-card title="🌐 大模型 (LLM) — 翻译与分句" size="small">
          <n-space vertical size="small">
            <n-card v-for="(p, i) in profiles" :key="i" size="tiny"
                    :style="p.enabled ? '' : 'opacity:.55'">
              <n-space align="center" style="margin-bottom: 6px">
                <n-switch v-model:value="p.enabled" size="small">
                  <template #checked>启用</template><template #unchecked>停用</template>
                </n-switch>
                <n-input v-model:value="p.name" size="small" placeholder="名称"
                         style="width: 140px" />
                <n-button size="tiny" quaternary :disabled="i === 0"
                          @click="move(i, -1)">↑</n-button>
                <n-button size="tiny" quaternary :disabled="i === profiles.length - 1"
                          @click="move(i, 1)">↓</n-button>
                <n-button size="tiny" quaternary type="error"
                          @click="delProfile(i)">删除</n-button>
                <n-text depth="3" style="font-size: 11px">
                  按此列表顺序依次尝试，前面的不可用自动切到下一个
                </n-text>
              </n-space>
              <n-grid :cols="2" :x-gap="12">
                <n-form-item-gi label="接口地址">
                  <n-input v-model:value="p.base_url" size="small"
                           placeholder="https://ark.cn-beijing.volces.com/api/plan/v3" />
                </n-form-item-gi>
                <n-form-item-gi label="模型名">
                  <n-input v-model:value="p.model" size="small"
                           placeholder="如 glm-5.3-flash / deepseek-chat" />
                </n-form-item-gi>
              </n-grid>
              <n-form-item label-placement="left" label="API Key">
                <n-input v-model:value="p.api_key" type="password"
                         show-password-on="click" size="small"
                         placeholder="密钥直接保存在本地 config.yaml" />
              </n-form-item>
            </n-card>
            <n-button size="small" dashed block @click="addProfile">＋ 新增一组配置</n-button>
          </n-space>
        </n-card>

        <!-- ============ 全局生成参数 (可编辑 + 悬浮说明) ============ -->
        <n-card title="生成参数" size="small">
          <n-grid :cols="4" :x-gap="12">
            <n-form-item-gi label-placement="left" label="Temperature">
              <n-tooltip trigger="hover" placement="top" style="max-width: 280px">
                <template #trigger>
                  <n-input-number v-model:value="form.temperature" :min="0" :max="2" :step="0.05" style="width:100%" />
                </template>
                控制 LLM 输出的随机性：越低越稳定、越贴合原文；越高越发散。<br />
                字幕翻译建议 <b>0.1~0.3</b>。出现漏译/错位时优先调低。
              </n-tooltip>
            </n-form-item-gi>
            <n-form-item-gi label-placement="left" label="并发数">
              <n-tooltip trigger="hover" placement="top" style="max-width: 280px">
                <template #trigger>
                  <n-input-number v-model:value="form.max_workers" :min="1" :max="16" :step="1" style="width:100%" />
                </template>
                同时发出的 LLM 请求数。<br />
                <b>1</b> = 串行最稳（限流严的服务商首选）；配额充裕可设 <b>2~4</b> 加速翻译。
              </n-tooltip>
            </n-form-item-gi>
            <n-form-item-gi label-placement="left" label="读超时(秒)">
              <n-tooltip trigger="hover" placement="top" style="max-width: 280px">
                <template #trigger>
                  <n-input-number v-model:value="form.timeout" :min="30" :max="900" :step="30" style="width:100%" />
                </template>
                单次请求最长等待秒数。<br />
                思考型模型（GLM-4.7 / 5.3 等）单次可能超过 120 秒，建议 <b>≥300</b>。
              </n-tooltip>
            </n-form-item-gi>
            <n-form-item-gi label-placement="left" label="思考模式">
              <n-tooltip trigger="hover" placement="top" style="max-width: 280px">
                <template #trigger>
                  <n-select v-model:value="form.thinking" :options="THINKING_OPTIONS" />
                </template>
                思考型模型（GLM-4.7/5.3 等）会先长推理再输出，慢但更聪明。<br />
                字幕翻译建议 <b>auto</b>；若服务端支持 disabled 可大幅提速。
              </n-tooltip>
            </n-form-item-gi>
          </n-grid>
          <n-space>
            <n-button type="primary" :loading="saving" @click="save()">保存全部</n-button>
            <n-button :loading="testing" @click="testConnection">🔌 测试连接</n-button>
            <n-text depth="3" style="font-size: 12px">
              测试连接先保存，再按配置池顺序发一个最小翻译请求
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
      </n-space>
    </n-spin>
  </n-layout-content>
</template>
