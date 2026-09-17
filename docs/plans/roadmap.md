# 开发路线图

> 每里程碑结束：可运行 + pytest 绿 + 已推送。完成即勾选并附实现笔记链接。

## M0 脚手架 ✅ 2026-09-17
- [x] monorepo 结构（backend/ frontend/ scripts/ docs/ data/）
- [x] uv + pyproject（Python 3.13）+ FastAPI 骨架 + SQLModel 表结构（Project/StepRun/Artifact/Job）
- [x] config.py 三处同步机制（DEFAULTS/config.yaml/config.example.yaml）+ 密钥隔离
- [x] errors.py 统一错误体系（code + 中文 message + suggestion）
- [x] pipeline/registry.py 九步骤定义（UI 常驻渲染的依据）
- [x] Vue3 + Naive UI 暗色中文壳（项目/工作台/设置/关于 四路由）
- [x] 设置页最小可用（读配置 + 端口写回，验证前后端配置环）
- [x] pytest 冒烟（夹具隔离 DATA_ROOT/CONFIG）
- [x] scripts/dev.ps1 + GitHub 建仓

## M1 项目域 ✅ 2026-09-17
- [x] Project CRUD API + 项目目录自举（data/projects/<uid>/，manifest.json 双写）
- [x] 步骤执行器：单步启动/取消/心跳；重跑语义（清产物+下游 stale）；future 异常观测
- [x] 输入指纹（cfg_keys 配置快照 + 上游产物哈希 → sha256）；fresh 标记 + 指纹跳过
- [x] 崩溃恢复：启动扫描 running→failed(STEP_INTERRUPTED) / job→dead
- [x] 日志：每步 logs/<step>.log + API 尾读
- [x] 项目状态聚合（processing/failed/done）随步骤联动
- [x] pytest 14 项：状态机全路径 + 取消/失败/恢复/API E2E（夹具隔离 DATA_ROOT）

## M2 管线前半（步骤 1–6）

### M2a 媒体链 ✅ 2026-09-17（download/preprocess/transcribe）
- [x] download：yt-dlp 库 + archive 去重 + cookies/代理 + 本地导入；media/ 源视频不受 force 删除影响（获取无过期概念）
- [x] preprocess：ffmpeg 16k 提取（显式 -f wav）+ demucs htdemucs 分离人声/背景（单声道自动转立体声；soundfile 读取避开 torchcodec）
- [x] transcribe：faster-whisper 词级时间戳 + 标点/停顿/时长三重断句 → src.srt；HF 缓存自动复用 vl_cache
- [x] 测试：SRT 读写、断句纯函数、本地导入幂等、真实 demucs+whisper 端到端（2.5s 样片）

### M2b 文本链 ✅ 2026-09-17
- [x] llm 后端：OpenAI 兼容 (httpx 直连任意服务商)，JSON 容错提取+校验重试，429/5xx/读超时指数退避，thinking 请求级开关，超时可配 (思考型模型友好)
- [x] split：超长行规则切分 (token 保真) + LLM 语义切 (校验失败自动回退)；词级时间戳保时间轴
- [x] translate：三档策略 (整篇直译/分块两步+全局审校/滚动摘要) + 术语表 + 风格提示词 + 按行号 id 对齐 (根治 JSON 错位) + 块级续传缓存
- [x] subtitles：源/译/双语 SRT + 行宽硬换行
- [x] 测试 6 项 (全 mock 零 token) + 真实 GLM-5.3-flash 全链冒烟 (6 句 17s)
- [x] 设置面板提前交付 (原 M4)：LLM 全参数可配 + 测试连接按钮，适配任意 OpenAI 兼容服务商
- [ ] split：NLP 粗切 + LLM 语义切句
- [ ] translate：三档策略 + 术语表 + 风格提示词 + gpt_log 式续传缓存
- [ ] subtitles：源/译/双语 SRT + 行宽控制
- [ ] pytest：全部步骤用合成媒体夹具跑通

## M3 管线后半（步骤 7–9）

### M3a 配音域 ✅ 2026-09-17（dubtask/tts/工作台 API）
- [x] dubtask：启发式时长估算 (CJK 4.7字/s 拉丁 3词/s) + 语速容忍分级 + gap 计算
- [x] tts：IndexTTS2.5 后端（双维度参考 preset/stable/dynamic × 情感含向量，短行自动回退 stable）+ GPT-SoVITS 后端；句级缓存；单句失败不阻断整批
- [x] 精修 API：workbench state/逐句合成/采纳/音频视频流 (Range)；与正式步骤共享句级缓存
- [x] 真机验证：IndexTTS 真实合成 3.6s 中文配音 ✓
- [x] pytest 27 项全绿

### M3b 成片 ✅ 2026-09-17
- [x] assemble 两阶段：A) numpy 铺轨 (32k 单声道统一, 句间淡入淡出, 峰值归一) + EBU R128 响度归一 → dub_full.wav；B) ffmpeg 封装 (背景音量 + 人声 ducking sidechaincompress + 可选字幕烧录重编码, 视频流默认直拷)
- [x] 产物：output/final_时间戳.mp4 (多版本并存) + 伴生 .srt
- [x] 测试：铺轨纯函数 + 合成样片端到端 (混音/烧录两分支)
- [x] 真机验证：真实 IndexTTS 两句 → 出片 6.0s/1.4MB ✓
- [x] 测试基建：stub_heavy_steps 夹具 —— 状态机测试与真实现永久解耦 (九步全真后不再有"桩步骤"可用)

## M4 前端应用
- [x] 新建项目（粘贴链接/本地导入，模态表单）+ 项目列表（状态卡/删除确认）
- [x] 管线页：九步骤常驻卡（状态/stale/fresh/错误三件套展开）+ 日志尾读面板 + 单步运行/强制重跑（下游过期确认弹窗）/取消 + 2s 轮询
- [x] 设置页：LLM 组全可配 + 测试连接（M2b 提前交付）；其余组只读概览
- [ ] naive-ui 按需引入瘦身（构建体积优化，低优先级）
- [ ] WebSocket 推送（当前 2s 轮询，M6 评估）

## M5 精修工作台 ✅ 2026-09-18
- [x] Vue 迁移：逐句列表（过滤/语速警告徽标）/合成本句+强制重合成/试听混音+只听配音（WebAudio 实时混音，不落盘）/画面同步定位/采纳
- [x] 路由：项目内嵌 /projects/:uid/workbench + 独立入口 /workbench（项目选择器）；管线页入口卡
- [x] 键盘导航 j/k；混音音量滑条实时生效
- [ ] 工作台内嵌参数面板（与设置页同源，M6 评估——当前用设置页全局参数合成）
- [ ] 预设同步与差异标注（复用 apply_preset，M6）

## M6 打磨验收
- [ ] 错误中心 + 引导文案全量中文化
- [ ] 用户手册 docs/manual.md
- [ ] 端到端验收：真实视频全流程 + 40 分钟长视频 + 中途 kill 恢复测试
