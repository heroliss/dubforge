# ADR-0001: DubForge 架构基线

- 状态：已采纳（2026-09-17）
- 背景：VideoLingo 工作区实战数月后立项重写。吸收其领域知识（管线阶段、缓存语义、坑），摒弃其结构性问题。

## 决策

### 1. 四条设计原则（不可妥协）

1. **步骤常驻可见、可单独重跑**：VideoLingo 的 Streamlit 页面按产物存在性决定显示哪个按钮，导致"进入下一步后上一步按钮消失"，用户只能靠外部 PowerShell 脚本回退。DubForge 把九个步骤做成常驻状态机，UI 永远显示全部步骤；重跑某步 = 置 pending + 清产物 + 下游置 stale。
2. **中断恢复一等公民**：产物临时名+原子改名；步骤记录输入指纹（配置+上游产物哈希）——修复 VideoLingo "改了配置不生效=旧缓存保护" 和 "normalized_dub.wav 漏网 10 小时" 这类血泪问题；进程重启时 running 一律判 interrupted。
3. **领域库与界面解耦**：`dubforge` 包纯 Python 不 import FastAPI。管线即库，可被 API/CLI/测试复用；UI 框架可替换。
4. **中文一等支持**：错误体系带 code+中文 message+修复建议；UI 全中文；注释中文。

### 2. 技术选型

| 项 | 选择 | 理由 |
|---|---|---|
| 后端语言/版本 | Python 3.13 + uv | 本机 VideoLingo venv 已验证 torch 全家桶可跑 3.13；uv 快且锁版本 |
| Web 框架 | FastAPI + uvicorn | 单进程托管 API+前端静态；异步友好 |
| 持久化 | SQLModel + SQLite | 单机单用户，零运维；表结构见 domain/models.py |
| 前端 | Vue3 + Vite + Naive UI | 用户拍板；中文生态最好的组件库；构建后静态托管部署不变复杂 |
| 状态推送 | 先轮询，M4 评估 WebSocket | M0 不过度设计 |
| TTS | IndexTTS2.5 主 + GPT-SoVITS 备 | 外部服务 URL 接入（gradio /gen_single），延续工作台验证过的双维度参考设计 |
| ASR | faster-whisper (large-v3-turbo) | 官方推荐档，8G 显存可跑；词级时间戳 |
| 分离 | demucs（人声/背景）+ pyannote 3.1（可选说话人） | VideoLingo 同款，已验证 |
| 翻译 | OpenAI 兼容接口（智谱 GLM）+ 三档策略（ADR-0002 of workspace，即整篇直译/分块+全局审校/滚动摘要） | 质量与抗截断平衡，已有设计论证 |
| 下载 | yt-dlp 库内嵌 + --download-archive 去重 | 用户拍板 V1 含 YouTube 集成 |

### 3. 不做（V1）

不捆绑模型运行时（外部服务）、不做多用户鉴权、不做非中文 UI、不迁移旧项目数据。

## 后果

- 正面：全新架构无历史包袱；每里程碑可运行可验证；旧系统并行生产不受影响。
- 代价：重复实现 VideoLingo 已有能力（约 M2–M5 工作量），属已接受的重写成本。
