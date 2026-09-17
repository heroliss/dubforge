<script setup lang="ts">
// 设置页: M0 已通读/写真实 config.yaml (单一配置源); M4 做完整分组表单
import { onMounted, ref } from 'vue'

const cfg = ref<any>(null)
const saving = ref(false)
const port = ref<number | null>(null)

onMounted(async () => {
  const r = await fetch('/api/settings')
  cfg.value = await r.json()
  port.value = cfg.value?.server?.port ?? null
})

async function savePort() {
  saving.value = true
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { 'server.port': port.value } }),
    })
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <n-layout-content content-style="padding: 24px;">
    <n-h1>设置</n-h1>
    <n-alert type="info" :show-icon="false" style="margin-bottom: 16px">
      配置统一保存在仓库根 <n-code>config.yaml</n-code>（含密钥，不入库）。
      此处为 M0 最小可用版：完整分组表单在 M4 交付。
    </n-alert>

    <n-spin :show="!cfg">
      <n-space vertical size="large" v-if="cfg">
        <n-card title="LLM 翻译" size="small">
          <n-descriptions :column="2" label-placement="left" bordered size="small">
            <n-descriptions-item label="接口">{{ cfg.llm.base_url }}</n-descriptions-item>
            <n-descriptions-item label="模型">{{ cfg.llm.model }}</n-descriptions-item>
            <n-descriptions-item label="密钥">
              <n-tag :type="cfg.llm.api_key?.set ? 'success' : 'warning'" size="small">
                {{ cfg.llm.api_key?.set ? `已配置 (…${cfg.llm.api_key.tail})` : '未配置' }}
              </n-tag>
            </n-descriptions-item>
            <n-descriptions-item label="并发">{{ cfg.llm.max_workers }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card title="IndexTTS2.5" size="small">
          <n-descriptions :column="2" label-placement="left" bordered size="small">
            <n-descriptions-item label="服务地址">{{ cfg.tts.indextts25.url }}</n-descriptions-item>
            <n-descriptions-item label="音色模式">{{ cfg.tts.indextts25.timbre_mode }}</n-descriptions-item>
            <n-descriptions-item label="情感模式">{{ cfg.tts.indextts25.emo_mode }}</n-descriptions-item>
            <n-descriptions-item label="时长系数">{{ cfg.tts.indextts25.duration_factor }}</n-descriptions-item>
          </n-descriptions>
        </n-card>

        <n-card title="服务器" size="small">
          <n-space align="center">
            <n-text>端口</n-text>
            <n-input-number v-model:value="port" :min="1024" :max="65535" style="width: 140px" />
            <n-button size="small" :loading="saving" @click="savePort">保存</n-button>
            <n-text depth="3">改端口后需重启后端生效</n-text>
          </n-space>
        </n-card>
      </n-space>
    </n-spin>
  </n-layout-content>
</template>
