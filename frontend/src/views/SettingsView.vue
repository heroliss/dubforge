<script setup lang="ts">
import FilePathLink from '../components/FilePathLink.vue'
// 设置入口：制作规则复用统一配置编辑器，本机连接与运行偏好分别保存。
// 注意: v-for 容器一律用原生 flex div —— naive-ui 2.45 的 n-space 在嵌套 n-card
// 场景会把组件子项渲染成空注释 (已踩坑, 见 git 历史)
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import {
  NAlert, NButton, NCard, NH1, NInput,
  NInputNumber, NSpin, NSwitch, NTag, NText, NTooltip, useDialog, useMessage,
} from 'naive-ui'
import EnvironmentPanel from '../components/EnvironmentPanel.vue'
import ConfigurationWorkspace from '../components/ConfigurationWorkspace.vue'

const message = useMessage()
const cfg = ref<any>(null)
const saving = ref(false)
const testing = ref(false) // 「并行测试全部」的全局转圈 (与单组测试互不影响)
const loadError = ref('')
// 分类只改变可见区域，已打开区域的草稿持续保留；模板范围变化才需确认。
const route = useRoute(), router = useRouter(), dialog = useDialog()
const section = computed(() => ['models', 'runtime'].includes(String(route.query.section)) ? String(route.query.section) : 'production')
const scopeFromQuery = (query: typeof route.query) => typeof query.template === 'string' && query.template ? 'series:' + query.template : 'global'
const selectedScope = computed(() => scopeFromQuery(route.query))
const visited = ref(new Set<string>())
watch(section, key => visited.value.add(key), { immediate: true })
const productionDirty = ref(false), productionBusy = ref(false)
const sections = computed(() => [
  { key: 'production', label: '制作与发布', dirty: productionDirty.value },
  { key: 'models', label: '模型与服务', dirty: isDirty.value },
  { key: 'runtime', label: '运行与存储', dirty: concDirty.value || rootDirty.value || displayDirty.value },
])
function selectSection(key: string) { void router.push({ path: '/settings', query: { ...route.query, section: key } }) }
function selectScope(scope: string) { void router.push({ path: '/settings', query: { section: 'production', ...(scope === 'global' ? {} : { template: scope.slice(7) }) } }) }
const testResults = ref<Record<number, any>>({})
const testingMap = ref<Record<number, boolean>>({}) // 每组独立的测试中状态 → 各组可并行测试

// 组装要发给后端探测的配置 (只含表单当前值, 不读写 config.yaml)
function toWire(p: any) {
  const { paramsJson, ...rest } = p
  return { ...rest, params: parseParams(p) }
}
async function postTest(profileList: any[]) {
  const r = await fetch('/api/settings/test_llm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ profiles: profileList }),
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
        model: p.model,
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
const form = ref({ timeout: 300 as number,
                   stream_reference_distance: 500 as number,
                   data_root: '' as string,
                   conc: {} as Record<string, number> })

// 进度算法固定，设置页只展示真实耗时记录。
const progressTiming = ref<any>(null)
function fmtSeconds(value: any): string {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(1)} 秒` : '—'
}
function fmtSampleTime(value: any): string {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return '—'
  return new Date(n * 1000).toLocaleString()
}
async function loadProgressTiming() {
  try {
    const r = await fetch('/api/settings/progress_timing', { cache: 'no-store' })
    if (r.ok) progressTiming.value = await r.json()
  } catch { /* 暂时保留上次显示的记录 */ }
}

// 每步骤并行上限 (GPU 类保持 1; 修改应用后对新启动的步骤即时生效)
const CONC_ITEMS = [
  { k: 'download', t: '获取视频' }, { k: 'preprocess', t: '音轨分离' },
  { k: 'transcribe', t: '语音转文字' }, { k: 'split', t: '语义整理' },
  { k: 'translate', t: '翻译' }, { k: 'subtitles', t: '生成字幕' },
  { k: 'tts', t: '配音' },
  { k: 'assemble', t: '合成成片' },
]

// —— 未应用修改的追踪: 每张卡独立 —— 表单是草稿, 各卡的「应用」才是唯一写入口 ——
const dirtyOn = ref(false)          // 载入完成/应用成功后开启追踪
const savedProfiles = ref<string[]>([])   // 已应用状态: 每组配置的序列化值 (按下标)
const savedForm = ref('')                 // 已应用状态: LLM 请求参数
const savedConc = ref('')                 // 已应用状态: 运行并行度
const savedRoot = ref('')                 // 已应用状态: 总工作目录
const savedDisplay = ref(500)
const cardDirty = computed(() => profiles.value.map((p, i) => {
  if (!dirtyOn.value) return false
  // JSON 非法也算"未保存" —— 否则 toWire 归一成 {} 后可能恰好等于基线,
  // 应用按钮被禁、applySettings 里的格式警告永远走不到
  return i >= savedProfiles.value.length || paramsJsonBad(p) ||
         JSON.stringify(toWire(p)) !== savedProfiles.value[i]
}))
const formDirty = computed(() =>
  dirtyOn.value && savedForm.value !== '' &&
  JSON.stringify({ timeout: form.value.timeout }) !== savedForm.value)
const displayDirty = computed(() => dirtyOn.value && form.value.stream_reference_distance !== savedDisplay.value)
const concDirty = computed(() =>
  dirtyOn.value && savedConc.value !== '' &&
  JSON.stringify(form.value.conc) !== savedConc.value)
const rootDirty = computed(() =>
  dirtyOn.value && form.value.data_root.trim() !== savedRoot.value)
const isDirty = computed(() =>
  dirtyOn.value && (formDirty.value || profiles.value.length !== savedProfiles.value.length || cardDirty.value.some(Boolean)))
const anyDirty = computed(() =>
  isDirty.value || concDirty.value || rootDirty.value || displayDirty.value || productionDirty.value)
const anySaving = computed(() => saving.value || savingConc.value || savingRoot.value || savingDisplay.value || productionBusy.value)
function markApplied() {
  savedProfiles.value = profiles.value.map(p => JSON.stringify(toWire(p)))
  savedForm.value = JSON.stringify({ timeout: form.value.timeout })
  savedDisplay.value = form.value.stream_reference_distance
  savedConc.value = JSON.stringify(form.value.conc)
  savedRoot.value = form.value.data_root.trim()
  dirtyOn.value = true
}
function confirmDiscard() {
  return new Promise<boolean>(resolve => dialog.warning({
    title: '放弃未保存的修改？', content: '离开后这些修改不会保留。', positiveText: '放弃并离开', negativeText: '继续编辑',
    onPositiveClick: () => resolve(true), onNegativeClick: () => resolve(false),
    onClose: () => resolve(false), onMaskClick: () => resolve(false),
  }))
}
function waitForSave() { message.info('正在保存，请完成后再切换'); return false }
onBeforeRouteLeave(() => anySaving.value ? waitForSave() : !anyDirty.value || confirmDiscard())
onBeforeRouteUpdate(to => {
  if (scopeFromQuery(to.query) === selectedScope.value) return true
  if (productionBusy.value) return waitForSave()
  return !productionDirty.value || confirmDiscard()
})
function warnUnload(e: BeforeUnloadEvent) {
  if (anyDirty.value || anySaving.value) { e.preventDefault(); e.returnValue = '' }
}
onMounted(() => window.addEventListener('beforeunload', warnUnload))
onUnmounted(() => window.removeEventListener('beforeunload', warnUnload))

// 专属参数解析: 留空或 JSON 非法都按 {} 处理 (不发任何额外字段)
function parseParams(p: any): any {
  try { return JSON.parse(p.paramsJson || '{}') } catch { return {} }
}
// 专属参数 JSON 是否填了但格式非法 (非法 ≠ 未填: 前者必须提示, 且算"未保存")
function paramsJsonBad(p: any): boolean {
  if (!(p.paramsJson || '').trim()) return false
  try { JSON.parse(p.paramsJson); return false } catch { return true }
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

const loadingConfig = ref(false)
async function loadSettings() {
  if (loadingConfig.value) return
  loadingConfig.value = true
  loadError.value = ''
  try {
    const _r = await fetch('/api/settings', { cache: 'no-store' })
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
    form.value.timeout = Number(llm.timeout ?? 300)
    form.value.stream_reference_distance = Number(
      cfg.value.ui?.llm_stream?.reference_distance_chars ?? 500) || 500
    form.value.data_root = String(cfg.value.server?.data_root ?? '')
    form.value.conc = { ...(cfg.value.runner?.step_concurrency ?? {}) }
    markApplied()                     // 以载入状态为"已应用"基线
    void loadProgressTiming()
  } catch (e: any) {
    loadError.value = `加载失败 (${e.message}) —— 请确认后端服务正在运行，并使用启动脚本打开的地址`
  } finally { loadingConfig.value = false }
}
watch(section, key => { if (key !== 'production' && !cfg.value && !loadError.value) void loadSettings() }, { immediate: true })

// 「应用」是唯一写入口: 只保存本卡内容 (LLM 配置组 + 通用参数)
async function applySettings() {
  if (saving.value) return
  const bad = profiles.value.filter(paramsJsonBad).map(p => p.name?.trim() || '未命名配置')
  if (bad.length) { message.warning(`请先修正专属参数的 JSON 格式：${bad.join('、')}`); return }
  saving.value = true
  // 仅记录本次请求提交的快照，不吞掉其他分区或请求期间的新修改。
  const submittedProfiles = profiles.value.map(p => toWire(p))
  const submittedForm = { timeout: form.value.timeout }
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: {
        'llm.profiles': submittedProfiles,
        'llm.timeout': submittedForm.timeout,
      } }),
    })
    if (!r.ok) throw new Error('HTTP ' + r.status)
    savedProfiles.value = submittedProfiles.map(p => JSON.stringify(p))
    savedForm.value = JSON.stringify(submittedForm)
    message.success('已应用并写入 config.yaml')
  } catch (e: any) {
    message.error(`应用失败: ${e.message}`)
  } finally { saving.value = false }
}

// —— 各卡独立的应用入口 (就近保存, 互不牵连) ——
const savingConc = ref(false)
async function applyConc() {
  if (savingConc.value) return
  savingConc.value = true
  const submitted = { ...form.value.conc }
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { 'runner.step_concurrency': submitted } }),
    })
    if (!r.ok) throw new Error('HTTP ' + r.status)
    savedConc.value = JSON.stringify(submitted)
    message.success('并行度已写入 config.yaml —— 之后新启动的步骤即时生效')
  } catch (e: any) {
    message.error(`应用失败: ${e.message}`)
  } finally { savingConc.value = false }
}

const savingRoot = ref(false)
async function applyRoot() {
  if (savingRoot.value) return
  savingRoot.value = true
  const submitted = form.value.data_root.trim()
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { 'server.data_root': submitted } }),
    })
    if (!r.ok) throw new Error('HTTP ' + r.status)
    savedRoot.value = submitted
    message.success('已写入 config.yaml —— 需重启程序生效')
  } catch (e: any) {
    message.error(`应用失败: ${e.message}`)
  } finally { savingRoot.value = false }
}

function concChanged(k: string) {
  if (!dirtyOn.value || savedConc.value === '') return false
  const base = JSON.parse(savedConc.value)
  return JSON.stringify(form.value.conc[k]) !== JSON.stringify(base[k])
}

const savingDisplay = ref(false)
async function applyDisplay() {
  if (savingDisplay.value) return
  savingDisplay.value = true
  const submitted = form.value.stream_reference_distance
  try {
    const r = await fetch('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates: { 'ui.llm_stream.reference_distance_chars': submitted } }),
    })
    if (!r.ok) throw new Error('HTTP ' + r.status)
    savedDisplay.value = submitted
    message.success('已保存显示偏好')
  } catch (e: any) { message.error(`保存失败：${e.message}`) }
  finally { savingDisplay.value = false }
}

async function testConnection() {
  testing.value = true
  // 每组独立发请求: 谁先返回谁先显示, 不互相等待最慢的
  const targets = profiles.value
    .map((p, idx) => ({ p, idx }))
    .filter(x => x.p.enabled)
  targets.forEach(x => { delete testResults.value[x.idx] })
  try {
    await Promise.allSettled(targets.map(x => testOne(x.idx)))
    const okN = targets.filter(x => testResults.value[x.idx]?.ok).length
    message.info(`并行测试完成: ${okN}/${targets.length} 组可用`)
  } finally { testing.value = false }
}

function extraParamsStr(r: any): string {
  const ep = r?.request?.extra_params ?? {}
  return Object.keys(ep).length ? JSON.stringify(ep) : ''
}
</script>

<template>
  <div class="workspace-page settings-page">
    <header class="page-head"><span class="page-kicker">偏好与配置</span><n-h1>设置</n-h1><p class="page-description">制作规则统一管理，服务连接与本机偏好独立设置。</p></header>
    <nav class="section-nav" aria-label="设置分类"><button v-for="item in sections" :key="item.key" :aria-pressed="section === item.key" @click="selectSection(item.key)">{{ item.label }}<span v-if="item.dirty" class="dirty-dot" aria-label="有未保存修改">●</span></button></nav>
    <ConfigurationWorkspace v-if="visited.has('production')" v-show="section === 'production'" :scope="selectedScope" @select="selectScope" @dirty-change="productionDirty = $event" @busy-change="productionBusy = $event" />
    <div v-show="section !== 'production'">
    <n-alert v-if="section === 'models'" type="info" :show-icon="false" style="margin-bottom: 20px">
      首次使用：填写接口地址、密钥和模型名 → 测试连接 → 保存模型设置。支持 OpenAI 兼容接口；多个配置按顺序自动切换。
    </n-alert>

    <n-alert v-if="loadError" type="error">{{ loadError }} <n-button text @click="loadSettings">重试</n-button></n-alert>
    <n-spin :show="loadingConfig">
      <div v-if="cfg" style="display:flex;flex-direction:column;gap:16px">
        <!-- ============ LLM 配置池 (可编辑) ============ -->
        <n-card v-show="section === 'models'" title="大模型连接" size="small">
          <n-text depth="3" style="font-size:12px">
            配置从上到下依次尝试，可用箭头调整优先级。测试不会保存修改；翻译和语义整理步骤可指定某一组。
          </n-text>
          <div class="profiles">
            <div v-for="(p, i) in profiles" :key="i" class="profile" :class="{ off: !p.enabled }">
              <!-- 头行: 序号 / 启用 / 名称 / (弹性空隙) / 测试 / 排序 / 删除 -->
              <div class="p-head">
                <n-tag size="small" :bordered="false" round>#{{ i + 1 }}</n-tag>
                <n-tag v-if="cardDirty[i]" size="small" type="warning"
                       :bordered="false">未应用</n-tag>
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
                          aria-label="提高配置优先级" @click="move(i, -1)">↑</n-button>
                <n-button size="small" quaternary :disabled="i === profiles.length - 1"
                          aria-label="降低配置优先级" @click="move(i, 1)">↓</n-button>
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
                           placeholder='可选，留空=服务商默认；例如 {"temperature":1,"top_p":0.95} 或 {"reasoning_effort":"low"}' />
                  <div class="param-hint">
                    核心参数各家通用 (OpenAI 协议: temperature / top_p / max_tokens 等)；
                    厂家私有参数 (如 GLM 的 reasoning_effort、thinking) 也写在这里原样发送；
                    GLM-5.3/5.3-Flash 通常不能关闭思考，建议用
                    <code>{"reasoning_effort":"low"}</code> 降低思考量；其他支持开关的模型才使用
                    <code>{"thinking":{"type":"disabled"}}</code>。具体字段以该接口文档为准，返回 400 时删除不支持的字段即可
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
                    · temperature={{ testResults[i].request?.temperature ??
                      '未发送 (用服务商默认)' }}<template
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
          <!-- LLM 运行设置: DubForge 自身参数 (非模型请求参数), 对所有配置组生效 -->
          <div class="globals-cap">
            <n-text depth="3" style="font-size:12px">
              请求超时对所有模型配置生效。
            </n-text>
            <n-tag v-if="formDirty" size="small" type="warning"
                   :bordered="false">未应用</n-tag>
          </div>
          <div class="globals">
            <div class="g-item">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger><span class="g-lb">读超时(秒) ⓘ</span></template>
                单次请求最长等待秒数; 思考型模型输出慢, 建议 ≥300
              </n-tooltip>
              <n-input-number v-model:value="form.timeout" size="small"
                              :min="10" :max="3600" style="width:100px" />
            </div>
          </div>
          <!-- 「应用」是唯一写入口: 覆盖本卡片全部内容 (配置组 + 通用参数) -->
          <div class="actions">
            <n-button type="primary" :loading="saving" :disabled="!isDirty"
                      @click="applySettings()">保存模型设置</n-button>
            <n-button :loading="testing" @click="testConnection">🔌 并行测试全部</n-button>
            <n-text v-if="isDirty" type="warning" style="font-size: 12px">
              ● 有未应用的修改 —— 不应用不生效
            </n-text>
            <n-text v-else depth="3" style="font-size: 12px">
              ✓ 无未应用的修改；测试只探测表单内容，不写入
            </n-text>
          </div>
        </n-card>

        <div v-if="visited.has('models')" v-show="section === 'models'"><EnvironmentPanel /></div>

        <n-card v-show="section === 'runtime'" title="显示偏好" size="small">
          <div class="globals">
            <div class="g-item">
              <n-tooltip trigger="hover" placement="top">
                <template #trigger><span class="g-lb">流式追赶距离(字符) ⓘ</span></template>
                显示文字落后最新模型文字多少字符时，追赶速度达到参考生成速度
              </n-tooltip>
              <n-input-number v-model:value="form.stream_reference_distance" size="small"
                              :min="50" :max="5000" :step="50" style="width:130px" />
            </div>
          </div>
          <n-text depth="3" class="display-note">默认 500。调大更平滑，调小更跟手；仅影响网页文字显示，不影响生成速度和产物。</n-text>
          <div class="actions"><n-button type="primary" size="small" :loading="savingDisplay" :disabled="!displayDirty" @click="applyDisplay">保存显示偏好</n-button><n-text v-if="displayDirty" type="warning">有未保存的修改</n-text></div>
        </n-card>

        <!-- ============ 运行并行度 ============ -->
        <n-card v-show="section === 'runtime'" title="运行并行度" size="small"
                style="margin-top: 14px">
          <div class="conc">
            <div v-for="c in CONC_ITEMS" :key="c.k" class="c-item">
              <n-text depth="3" style="font-size: 12px">{{ c.t }}
                <n-tag v-if="concChanged(c.k)" size="tiny" type="warning"
                       :bordered="false">已改</n-tag>
              </n-text>
              <n-input-number v-model:value="form.conc[c.k]" size="small"
                              :min="1" :max="4" style="width: 76px" />
            </div>
          </div>
          <n-text depth="3" style="font-size:12px; display:block; margin-top: 8px">
            勾选多个项目点「运行选中」时：下载/LLM 类可并行推进，GPU 类
            (音轨分离/语音转文字/批量配音/合成成片) 保持 1 排队保护显存。
            应用后对之后新启动的步骤即时生效。
          </n-text>
          <div class="actions" style="margin-top: 8px">
            <n-button type="primary" size="small" :loading="savingConc"
                      :disabled="!concDirty" @click="applyConc">💾 应用并行度</n-button>
            <n-text v-if="concDirty" type="warning" style="font-size:12px">
              ● 有未应用的修改
            </n-text>
          </div>
        </n-card>

        <!-- ============ 存储 ============ -->
        <n-card v-show="section === 'runtime'" title="存储位置" size="small">
          <div class="g-item">
            <n-tooltip trigger="hover" placement="top">
              <template #trigger><span class="g-lb">总工作目录 ⓘ</span></template>
              所有项目数据 (项目目录 / 数据库) 的根目录
            </n-tooltip>
            <n-input v-model:value="form.data_root" size="small" style="width: min(420px, 100%)"
                     placeholder="留空 = 默认 程序目录\data" />
            <FilePathLink v-if="form.data_root" :path="form.data_root" label="定位目录" />
          </div>
          <n-text depth="3" style="font-size:12px; display:block; margin-top:6px">
            应用后需重启程序才生效；已有数据不会自动搬家，需手动移动后重启。详见「❓ 帮助」页。
          </n-text>
          <div class="actions" style="margin-top: 8px">
            <n-button type="primary" size="small" :loading="savingRoot"
                      :disabled="!rootDirty" @click="applyRoot">💾 应用存储位置</n-button>
            <n-text v-if="rootDirty" type="warning" style="font-size:12px">
              ● 有未应用的修改
            </n-text>
          </div>
          <n-text depth="3" style="font-size:12px; display:block; margin-top:6px">
            回收站还原的项目目录会在进入「项目」列表时自动重新登记 (也可点列表右上角「刷新」)。
          </n-text>
        </n-card>

        <details v-show="section === 'runtime'" class="timing-details"><summary>耗时记录与进度估算</summary>
        <n-card title="进度与剩余时间" size="small">
          <details><summary>进度如何估算？</summary>
          <n-text depth="3" style="font-size:12px; display:block">
            下载显示实际字节进度，剩余时间 = 剩余字节 ÷ 本次累计平均速度；转录、分离和配音继续按实际处理量计进度。
            语义整理和翻译显示“预计进度”：无相近记录时整步默认 10 分钟，准备 1%、思考 60%、输出 38%、收尾 1%；阶段按实际事件切换，成功后才到 100%。
            同模型、同任务保留最近 20 条正常请求记录，相近输入长度按单位字符耗时比较；两端各去掉三分之一（向下取整），其余取平均，1～2 条时直接平均。
            多窗口共享整步预算。超出预估或重试时显示状态和已用时间，撤下倒计时；进度不倒退，缓存、重试和失败不计入正常耗时记录。
            标有“当前阶段”的时间只包含正在下载、分离或封装的阶段。
          </n-text>
          </details>
          <div class="timing-history-head">
            <n-text strong>已记录的真实耗时</n-text>
            <n-text depth="3" style="font-size:12px">共 {{ progressTiming?.sample_count ?? 0 }} 条，跨项目复用</n-text>
            <n-button size="tiny" quaternary @click="loadProgressTiming">↻ 刷新记录</n-button>
          </div>
          <div v-if="!(progressTiming?.profiles?.length)" class="timing-empty">
            暂无记录，完成正常模型请求后自动积累。
          </div>
          <div v-for="group in progressTiming?.profiles ?? []" :key="group.key" class="timing-group">
            <div class="timing-group-title">{{ group.label }}</div>
            <div class="timing-summary">样本耗时截尾平均 {{ fmtSeconds(group.trimmed_mean_seconds) }}（记录概览；实际估算按输入长度换算）</div>
            <div v-for="(sample, si) in group.samples" :key="`${group.key}-${sample.ts}-${si}`" class="timing-sample">
              <span>{{ fmtSampleTime(sample.ts) }}</span>
              <span>原文 {{ sample.work }} 字符</span>
              <span>耗时 {{ fmtSeconds(sample.seconds) }}</span>
            </div>
          </div>
        </n-card>

        </details>
      </div>
    </n-spin>
    </div>
  </div>
</template>

<style scoped>
.settings-page { max-width: 1240px; }
.settings-page .profile { padding: 18px; border-color: var(--df-border); border-radius: 10px; background: #161c24; }
.settings-page .profiles { gap: 16px; margin-top: 18px; }
.settings-page .fields { gap: 14px; }
.settings-page .conc { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.settings-page .c-item { justify-content: space-between; padding: 12px; border: 1px solid var(--df-border); border-radius: 8px; }
.settings-page .actions { padding-top: 16px; border-top: 1px solid var(--df-border); flex-wrap: wrap; }
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
.globals-cap { display: flex; align-items: center; gap: 8px; margin-top: 14px; }
.globals { display: flex; gap: 18px; flex-wrap: wrap; margin-top: 8px; }
.g-item { display: flex; flex-direction: column; gap: 4px; }
.g-lb { font-size: 12px; cursor: help; width: fit-content; }
.conc { display: flex; flex-wrap: wrap; gap: 10px 18px; }
.c-item { display: flex; align-items: center; gap: 6px; }
.timing-history-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
.timing-history-head > :last-child { margin-left: auto; }
.timing-empty { margin-top: 8px; padding: 10px; color: #8b949e; border: 1px dashed #2a343f; border-radius: 6px; font-size: 12px; }
.timing-group { margin-top: 10px; border: 1px solid #2a343f; border-radius: 6px; overflow: hidden; }
.timing-group-title { padding: 6px 9px; background: rgba(77,163,255,.07); font-size: 12px; word-break: break-word; }
.timing-summary { padding: 5px 9px; color: #8b949e; font-size: 11px; }
.timing-sample { display: flex; flex-wrap: wrap; gap: 4px 12px; padding: 5px 9px; border-top: 1px solid #202832; color: #c9d1d9; font-size: 11px; }
.timing-details > summary { cursor:pointer; color:var(--df-muted); padding:14px 0; font-size:13px; }
.timing-details details { font-size:12px; color:var(--df-muted); margin-bottom:12px; }
.display-note { display:block; font-size:12px; margin-top:10px; }
@media (max-width: 720px) {
  .p-head { flex-wrap: wrap; align-items: center; }
  .p-head .flex-sp { display: none; }
  .name-input { width: min(150px, 100%); }
  .fields { grid-template-columns: 1fr; gap: 4px; }
  .fields > .c-span { grid-column: auto; }
  .fields > .lb { white-space: normal; margin-top: 4px; }
  .actions { flex-wrap: wrap; align-items: flex-start; }
  .globals-cap { align-items: flex-start; flex-wrap: wrap; }
  .globals { gap: 12px; }
  .timing-sample { display: grid; grid-template-columns: 1fr 1fr; }
}
</style>
