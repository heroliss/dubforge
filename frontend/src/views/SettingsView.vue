<script setup lang="ts">
// 设置页: LLM 多组配置池 (任意 OpenAI 兼容服务商, 按顺序降级) + 其余组只读概览
// 注意: v-for 容器一律用原生 flex div —— naive-ui 2.45 的 n-space 在嵌套 n-card
// 场景会把组件子项渲染成空注释 (已踩坑, 见 git 历史)
import { onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'

const message = useMessage()
const cfg = ref<any>(null)
const saving = ref(false)
const testing = ref(false) // 「并行测试全部」的全局转圈 (与单组测试互不影响)
const loadError = ref('')
const testResults = ref<Record<number, any>>({})
const testingMap = ref<Record<number, boolean>>({}) // 每组独立的测试中状态 → 各组可并行测试

// 组装要发给后端探测的配置 (只含表单当前值, 不读写 config.yaml)
function toWire(p: any) {
  const { paramsJson, ...rest } = p
  return { ...rest, params: parseParams(p) }
}
function toGlobals() {
  return { temperature: form.value.temperature, thinking: form.value.thinking }
}
async function postTest(profileList: any[]) {
  const r = await fetch('/api/settings/test_llm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profiles: profileList, globals: toGlobals() }),
  })
  const j = await r.json()
  if (!r.ok) throw new Error(j.message || j.detail || '测试请求失败')
  return j.results ?? []
}
async function testOne(i: number) {
  delete testResults.value[i]
  testingMap.value[i] = true
  try {
    const results = await postTest([toWire(profiles.value[i])])
    testResults.value[i] = results[0]
  } catch (e: any) {
    // 测试请求本身失败 (后端不可达/接口报错) 也归一成结果, 与成功信息显示在同一个详情框
    const p = profiles.value[i] ?? {}
    testResults.value[i] = {
      ok: false, error: e.message || String(e),
      request: {
        url: (p.base_url || '').replace(/\/+$/, '') + '/chat/completions',
        model: p.model, temperature: form.value.temperature,
        extra_params: parseParams(p), prompt: '请只回复两个字母: OK',
      },
    }
  } finally { testingMap.value[i] = false }
}
// 该组是否正在测试 (单独点测试, 或处于「并行测试全部」中且还没出结果)
function isTesting(i: number): boolean {
  if (testingMap.value[i]) return true
  return testing.value && !testResults.value[i] && (profiles.value[i]?.enabled ?? false)
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

// 专属参数解析: 留空或 JSON 非法都按 {} 处理 (不发任何额外字段)
function parseParams(p: any): any {
  try { return JSON.parse(p.paramsJson || '{}') } catch { return {} }
}

function addProfile() {
  profiles.value.push({ name: `配置${profiles.value.length + 1}`,
                        base_url: '', api_key: '', model: '', enabled: true,
                        params: {}, paramsJson: '' }) // 专属参数默认留空
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
      ...p, params: p.params ?? {},
      // 专属参数留空不显示 "{}" —— 空即不发送任何额外字段
      paramsJson: (p.params && Object.keys(p.params).length)
        ? JSON.stringify(p.params, null, 1) : '',
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
      const { paramsJson, ...rest } = p
      return { ...rest, params: parseParams(p) }
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
  testing.value = true
  testResults.value = {}
  // 只测「启用」的组; 记录它们在表单里的原始下标, 结果按位置映射回去
  const idxMap: number[] = []
  const list = profiles.value
    .map((p, idx) => ({ p, idx }))
    .filter(x => x.p.enabled)
  try {
    const results = await postTest(list.map(x => { idxMap.push(x.idx); return toWire(x.p) }))
    results.forEach((res: any, pos: number) => { testResults.value[idxMap[pos]] = res })
    const okN = results.filter((r: any) => r.ok).length
    message.info(`并行测试完成: ${okN}/${results.length} 组可用`)
  } catch (e: any) {
    message.error(e.message)
  } finally { testing.value = false }
}

function extraParamsStr(r: any): string {
  const ep = r?.request?.extra_params ?? {}
  return Object.keys(ep).length ? JSON.stringify(ep) : ''
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
          <n-text depth="3" style="font-size:12px">
            配置按列表顺序作为降级链：前面的失败时自动切换到下一个。
            每组可用右侧「⚡ 测试」单独验证；各组测试互相独立、可同时进行，详情会显示发送与收到的内容。
            测试只探测表单当前填写的内容，不会保存。
          </n-text>
          <div class="profiles">
            <div v-for="(p, i) in profiles" :key="i" class="profile" :class="{ off: !p.enabled }">
              <!-- 头行: 序号 / 启用 / 名称 / (弹性空隙) / 测试 / 排序 / 删除 -->
              <div class="p-head">
                <n-tag size="small" :bordered="false" round>#{{ i + 1 }}</n-tag>
                <n-switch v-model:value="p.enabled" size="small">
                  <template #checked>启用</template><template #unchecked>停用</template>
                </n-switch>
                <n-input v-model:value="p.name" size="small" placeholder="配置名称"
                         class="name-input" />
                <div class="flex-sp" />
                <n-button size="small" type="primary" secondary :loading="isTesting(i)"
                          @click="testOne(i)">
                  <template #icon><span style="font-size:12px">⚡</span></template>
                  测试
                </n-button>
                <n-button size="small" quaternary :disabled="i === 0"
                          @click="move(i, -1)">↑</n-button>
                <n-button size="small" quaternary :disabled="i === profiles.length - 1"
                          @click="move(i, 1)">↓</n-button>
                <n-button size="small" quaternary type="error"
                          @click="delProfile(i)">删除</n-button>
              </div>
              <!-- 字段区: 行内标签两列布局, 减少换行 -->
              <div class="fields">
                <n-text depth="3" class="lb">接口地址</n-text>
                <n-input v-model:value="p.base_url" size="small" class="c-span"
                         placeholder="https://ark.cn-beijing.volces.com/api/plan/v3" />
                <n-text depth="3" class="lb">模型名</n-text>
                <n-input v-model:value="p.model" size="small"
                         placeholder="如 glm-5.3-flash / deepseek-chat" />
                <n-text depth="3" class="lb">API Key</n-text>
                <n-input v-model:value="p.api_key" type="password"
                         show-password-on="click" size="small"
                         placeholder="密钥保存在本地 config.yaml" />
                <n-text depth="3" class="lb lb-top">专属参数</n-text>
                <div class="c-span">
                  <n-input v-model:value="p.paramsJson" type="textarea" size="small" :rows="2"
                           placeholder='可选, 留空 = 使用服务商默认值; JSON 原样合并进请求, 如 {"temperature":1,"top_p":0.95}' />
                  <div class="param-hint">
                    核心参数各家通用 (OpenAI 协议: temperature / top_p / max_tokens 等)；
                    厂家私有参数 (如 GLM 的 reasoning_effort、thinking) 也写在这里原样发送
                  </div>
                </div>
              </div>
              <!-- 测试过程/结果: 发送了什么 / 过程事件 / 收到了什么 -->
              <div v-if="isTesting(i) || testResults[i]" class="test-box">
                <div v-if="isTesting(i)" class="tl dim">
                  ⏳ 正在发送测试请求… POST {{ (p.base_url || '').replace(/\/+$/, '') }}/chat/completions
                </div>
                <template v-else-if="testResults[i]">
                  <div v-if="testResults[i].ok" class="tl ok">
                    ✓ 测试通过 · {{ testResults[i].latency }}s · 模型 {{ testResults[i].model }}
                    · 收到: “{{ testResults[i].reply }}”
                  </div>
                  <div v-else class="tl bad">✗ 测试失败 · {{ testResults[i].error }}</div>
                  <div class="tl">→ 发送  POST {{ testResults[i].request?.url }}</div>
                  <div class="tl">
                    &nbsp;&nbsp;model={{ testResults[i].request?.model }}
                    · temperature={{ testResults[i].request?.temperature }}<template
                      v-if="extraParamsStr(testResults[i])"> · 专属参数={{ extraParamsStr(testResults[i]) }}</template>
                  </div>
                  <div class="tl">&nbsp;&nbsp;prompt: "{{ testResults[i].request?.prompt }}"</div>
                  <div v-for="(l, li) in testResults[i].logs || []" :key="li"
                       class="tl warn">· {{ l }}</div>
                  <template v-if="!testResults[i].ok">
                    <div v-if="testResults[i].suggestion" class="tl dim">
                    修复建议: {{ testResults[i].suggestion }}
                  </div>
                    <div v-if="testResults[i].detail" class="tl dim">
                      服务端返回: {{ testResults[i].detail }}
                    </div>
                  </template>
                </template>
              </div>
            </div>
            <n-button size="small" dashed block @click="addProfile">＋ 新增一组配置</n-button>
          </div>
          <!-- 通用参数: 全局默认值, 组内"专属参数"可覆盖同名项 -->
          <n-text depth="3" style="font-size:12px; display:block; margin-top:14px">
            通用参数（所有配置组共用；组内「专属参数」可覆盖 Temperature 等同名项）：
          </n-text>
          <div class="globals">
            <div class="g-item">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger><span class="g-lb">Temperature ⓘ</span></template>
                随机性: 越高越发散。翻译建议 0.1~0.3; 思考型模型可按官方推荐值
                (如 GLM-5.3-flash 推荐 1)。只在某组生效的话, 写进该组「专属参数」
              </n-tooltip>
              <n-input-number v-model:value="form.temperature" size="small"
                              :step="0.1" :min="0" :max="2" style="width:110px" />
            </div>
            <div class="g-item">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger><span class="g-lb">并发数 ⓘ</span></template>
                翻译分块时同时发出的请求数; 太大容易触发服务商限流 (429)
              </n-tooltip>
              <n-input-number v-model:value="form.max_workers" size="small"
                              :min="1" :max="8" style="width:90px" />
            </div>
            <div class="g-item">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger><span class="g-lb">读超时(秒) ⓘ</span></template>
                单次请求最长等待秒数; 思考型模型输出慢, 建议 ≥300
              </n-tooltip>
              <n-input-number v-model:value="form.timeout" size="small"
                              :min="10" :max="3600" style="width:100px" />
            </div>
            <div class="g-item">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger><span class="g-lb">思考模式 ⓘ</span></template>
                auto: 不传参数, 跟随服务端默认; disabled/enabled:
                发送 thinking.type 开关思考 (GLM 等部分服务商支持)
              </n-tooltip>
              <n-select v-model:value="form.thinking" size="small"
                        :options="THINKING_OPTIONS" style="width:230px" />
            </div>
          </div>
          <!-- 保存是唯一写入口: 覆盖本卡片全部内容 (配置组 + 通用参数) -->
          <div class="actions">
            <n-button type="primary" :loading="saving" @click="save()">💾 保存全部设置</n-button>
            <n-button :loading="testing" @click="testConnection">🔌 并行测试全部</n-button>
            <n-text depth="3" style="font-size: 12px">
              保存 = 写入 config.yaml（配置组 + 通用参数）；测试只探测表单内容，不会保存
            </n-text>
          </div>
        </n-card>

        <!-- ============ 其余配置 (只读概览, 后续版本开放编辑) ============ -->
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

<style scoped>
.profiles { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
.profile { border: 1px solid #2a343f; border-radius: 8px; padding: 10px 12px; }
.profile.off { opacity: .55; }
.p-head { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }
.name-input { width: 150px; }
.flex-sp { flex: 1; }

/* 字段区: 64px 标签列 + 两列输入, 行内标签减少换行 */
.fields { display: grid; grid-template-columns: 64px 1.1fr 64px 1.6fr; gap: 8px 10px; align-items: center; }
.lb { font-size: 12px; white-space: nowrap; }
.lb-top { align-self: start; padding-top: 6px; }
.c-span { grid-column: 2 / 5; }
.param-hint { font-size: 11px; line-height: 1.6; margin-top: 3px; }

/* 测试详情框: 深色终端风, 展示发送/过程/接收 */
.test-box {
  margin-top: 10px; background: #0d1117; border: 1px solid #2a343f;
  border-radius: 6px; padding: 8px 10px;
  font-family: Consolas, 'Courier New', monospace; font-size: 11.5px; line-height: 1.75;
  max-height: 220px; overflow-y: auto; word-break: break-all;
}
.tl { color: #c9d1d9; white-space: pre-wrap; }
.tl.ok { color: #7ee787; }
.tl.bad { color: #ff7b72; }
.tl.warn { color: #d29922; }
.tl.dim { color: #8b949e; }
.actions { display: flex; align-items: center; gap: 10px; margin-top: 14px; }
.globals { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 8px; }
.g-item { display: flex; flex-direction: column; gap: 4px; }
.g-lb { font-size: 12px; cursor: help; width: fit-content; }
</style>
