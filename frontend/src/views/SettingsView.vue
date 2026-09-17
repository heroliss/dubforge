<script setup lang="ts">
// 设置页: LLM 全参数可配 (适配任意 OpenAI 兼容服务商) + 连通性测试
// 其余分组 (下载/分离/TTS/成片) 完整表单在 M4 交付, 当前只读展示
import { onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'

const message = useMessage()
const cfg = ref<any>(null)
const saving = ref(false)
const testing = ref(false)
const testResult = ref<null | { ok: boolean; text: string }>(null)

// 表单绑定 (api_key 特殊: 只显示占位, 输入新值才提交)
const form = ref({
  base_url: '', api_key: '', model: '',
  temperature: 0.1 as number, max_workers: 1 as number,
  timeout: 300 as number, thinking: 'auto',
})

const THINKING_OPTIONS = [
  { label: 'auto — 不传参数（跟随服务端默认）', value: 'auto' },
  { label: 'disabled — 关闭思考（翻译任务推荐，大幅提速）', value: 'disabled' },
  { label: 'enabled — 开启思考', value: 'enabled' },
]

onMounted(async () => {
  const r = await fetch('/api/settings')
  cfg.value = await r.json()
  const llm = cfg.value.llm
  form.value.base_url = llm.base_url
  form.value.model = llm.model
  form.value.temperature = Number(llm.temperature)
  form.value.max_workers = Number(llm.max_workers)
  form.value.timeout = Number(llm.timeout ?? 300)
  form.value.thinking = llm.thinking ?? 'auto'
})

async function save(showTip = true) {
  saving.value = true
  try {
    const updates: Record<string, unknown> = {
      'llm.base_url': form.value.base_url.trim(),
      'llm.model': form.value.model.trim(),
      'llm.temperature': form.value.temperature,
      'llm.max_workers': form.value.max_workers,
      'llm.timeout': form.value.timeout,
      'llm.thinking': form.value.thinking,
    }
    if (form.value.api_key.trim()) updates['llm.api_key'] = form.value.api_key.trim()
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates }),
    })
    const j = await r.json()
    if (showTip) message.success(`已保存 ${Object.keys(j.applied || {}).length} 项`)
    form.value.api_key = ''   // 清空输入框 (已落盘)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  await save(false)
  testing.value = true
  testResult.value = null
  try {
    const r = await fetch('/api/settings/test_llm', { method: 'POST' })
    const j = await r.json()
    testResult.value = j.ok
      ? { ok: true, text: `${j.latency}s · ${j.model} · 回复: ${j.reply}` }
      : { ok: false, text: j.message + (j.suggestion ? ` — ${j.suggestion}` : '') }
  } catch (e: any) {
    testResult.value = { ok: false, text: `请求失败: ${e.message}` }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-h1>设置</n-h1>
    <n-alert type="info" :show-icon="false" style="margin-bottom: 16px">
      配置统一保存在仓库根 <n-code>config.yaml</n-code>（含密钥，不入库）。
      LLM 采用 OpenAI 兼容协议 —— 修改 <n-code>base_url</n-code> + <n-code>api_key</n-code> + 模型名
      即可接入任意服务商（智谱 / 火山方舟 / DeepSeek / OpenAI / 本地 vLLM 等）。
    </n-alert>

    <n-spin :show="!cfg">
      <n-space vertical size="large" v-if="cfg">
        <!-- ============ LLM (可编辑) ============ -->
        <n-card title="🌐 大模型 (LLM) — 翻译与分句" size="small">
          <n-form label-placement="left" label-width="110">
            <n-form-item label="接口地址">
              <n-input v-model:value="form.base_url"
                       placeholder="https://ark.cn-beijing.volces.com/api/coding/v3" />
            </n-form-item>
            <n-form-item label="API Key">
              <n-input v-model:value="form.api_key" type="password" show-password-on="click"
                       :placeholder="cfg.llm.api_key?.set
                         ? `已配置 (…${cfg.llm.api_key.tail})，留空保持不变`
                         : '填入 API Key'" />
            </n-form-item>
            <n-form-item label="模型名">
              <n-input v-model:value="form.model" placeholder="如 glm-5.3-flash / deepseek-chat / gpt-4o" />
            </n-form-item>
            <n-form-item label="思考模式">
              <n-select v-model:value="form.thinking" :options="THINKING_OPTIONS" />
            </n-form-item>
            <n-grid :cols="3" :x-gap="12">
              <n-form-item-gi label="Temperature">
                <n-input-number v-model:value="form.temperature" :min="0" :max="2" :step="0.05" />
              </n-form-item-gi>
              <n-form-item-gi label="并发数">
                <n-input-number v-model:value="form.max_workers" :min="1" :max="16" :step="1" />
              </n-form-item-gi>
              <n-form-item-gi label="读超时(秒)">
                <n-input-number v-model:value="form.timeout" :min="30" :max="900" :step="30" />
              </n-form-item-gi>
            </n-grid>
            <n-space>
              <n-button type="primary" :loading="saving" @click="save()">保存</n-button>
              <n-button :loading="testing" @click="testConnection">🔌 测试连接</n-button>
              <n-text depth="3" style="font-size: 12px">
                测试连接会先用当前表单值保存，再发一个最小翻译请求
              </n-text>
            </n-space>
            <n-alert v-if="testResult" :type="testResult.ok ? 'success' : 'error'"
                     :show-icon="false" style="margin-top: 10px">
              {{ testResult.text }}
            </n-alert>
          </n-form>
        </n-card>

        <!-- ============ 只读概览 (M4 做成可编辑) ============ -->
        <n-card title="下载 / 分离 / 转录" size="small">
          <n-descriptions :column="2" label-placement="left" bordered size="small">
            <n-descriptions-item label="最高分辨率">{{ cfg.download.resolution }}p</n-descriptions-item>
            <n-descriptions-item label="Cookies">
              <n-tag size="small" :type="cfg.download.cookies_path ? 'success' : 'default'">
                {{ cfg.download.cookies_path ? '已配置' : '未配置' }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="代理">{{ cfg.download.proxy || '(环境变量)' }}</n-descriptions-item>
            <n-descriptions-item label="人声分离">{{ cfg.separate.enabled ? '开启' : '关闭' }}</n-descriptions-item>
            <n-descriptions-item label="说话人分离">
              {{ cfg.separate.speaker_diarization ? '开启' : '关闭' }}
            </n-descriptions-item>
            <n-descriptions-item label="ASR 模型">{{ cfg.asr.model }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card title="配音 (IndexTTS2.5 / GPT-SoVITS)" size="small">
          <n-descriptions :column="2" label-placement="left" bordered size="small">
            <n-descriptions-item label="TTS 后端">{{ cfg.tts.backend }}</n-descriptions-item>
            <n-descriptions-item label="IndexTTS 地址">{{ cfg.tts.indextts25.url }}</n-descriptions-item>
            <n-descriptions-item label="音色模式">{{ cfg.tts.indextts25.timbre_mode }}</n-descriptions-item>
            <n-descriptions-item label="情感模式">{{ cfg.tts.indextts25.emo_mode }}</n-descriptions-item>
            <n-descriptions-item label="时长系数">{{ cfg.tts.indextts25.duration_factor }}</n-descriptions-item>
            <n-descriptions-item label="GPT-SoVITS">{{ cfg.tts.gptsovits.url }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card title="成片合成" size="small">
          <n-descriptions :column="3" label-placement="left" bordered size="small">
            <n-descriptions-item label="背景音量">{{ cfg.assemble.bgm_volume }}</n-descriptions-item>
            <n-descriptions-item label="人声 Ducking">
              {{ cfg.assemble.ducking ? '开' : '关' }}
            </n-descriptions-item>
            <n-descriptions-item label="响度归一">
              {{ cfg.assemble.loudnorm ? '开' : '关' }}
            </n-descriptions-item>
            <n-descriptions-item label="烧录字幕">
              {{ cfg.assemble.burn_subtitles ? '开' : '关（外挂）' }}
            </n-descriptions-item>
            <n-descriptions-item label="CRF">{{ cfg.assemble.crf }}</n-descriptions-item>
          </n-descriptions>
        </n-card>
      </n-space>
    </n-spin>
  </n-layout-content>
</template>
