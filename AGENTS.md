# AGENTS.md — DubForge AI 协作规则

AI 助手在本仓库工作前必读。架构决策见 `docs/decisions/`。

## 仓库布局与运行

- `backend/`：Python 3.13（uv 管理）。`uv sync` 装依赖；跑服务 `uv run uvicorn app.main:app --port 8600 --reload`（实际端口以 `config.yaml` 的 `server.port` 为准）；跑测试 `uv run pytest`。
- `frontend/`：Vue3 + Vite + Naive UI。`npm install` / `npm run dev`（默认 :5273，代理端口由 `DUBFORGE_BACKEND_PORT` 传入；直接运行时默认 8600）/ `npm run build`（产物 dist/ 由 FastAPI 托管）。
- **AI 测试纪律：起服务验证一律用 8611 端口**（`--port 8611`），8600 属于用户的常驻实例，禁止占用/杀进程。
- 两个 GitHub 仓库约定：本仓 `heroliss/dubforge`（private）；videoLingo-Workspace 是旧系统，保持生产可用，不在本仓维护其代码。

## 铁律

1. **领域库与界面解耦**：`dubforge/` 包内禁止 import fastapi/starlette；API 逻辑只写在 `app/`。
2. **config.yaml 含密钥永不入库**；新增配置项必须同步改 `config.example.yaml` + `dubforge/config.py::DEFAULTS` 三处。
3. **产物原子写**：所有步骤产物先写 `*.tmp` 再 `os.replace`；禁止直接写最终名（中断恢复的根基）。
4. **步骤幂等**：每个 pipeline 步骤实现开头先检查产物是否已完成（输入指纹匹配即跳过）。
5. **中文一等**：用户可见消息（错误/提示/引导）用中文；代码注释中文；错误必须用 `DubForgeError` 子类（错误码稳定）。
6. **测试随行**：新领域逻辑必须有 pytest（`DUBFORGE_DATA_ROOT` 夹具隔离）；提交前跑 `uv run pytest`。
7. **提交规范**：conventional 风格（feat/fix/docs/chore/test）；提交前 staged diff 扫描密钥（正则 `[0-9a-f]{32}\.[A-Za-z0-9]{8}`、`sk-`、`ghp_`）。

## 里程碑约定

M0–M6 见 README 与 `docs/plans/roadmap.md`。每里程碑结束必须：可运行 + 测试绿 + 已推送。做技术决策写 ADR 到 `docs/decisions/NNNN-标题.md`。

## 外部服务

| 服务 | 地址 | 说明 |
|---|---|---|
| IndexTTS2.5 | http://127.0.0.1:7860 | gradio WebUI；项目根 `D:\Program Files\index-tts`；预设在其 `outputs/presets/` |
| GPT-SoVITS | http://127.0.0.1:9880 | 整合包在 VideoLingo-Workspace |
| LLM | 智谱 OpenAI 兼容 | config.yaml llm 段 |
| 代理 | http://127.0.0.1:7897 | Clash Verge 规则模式；yt-dlp/HF 下载需要 |

## 已知经验（来自 VideoLingo 实战，设计时已吸收）

- 缓存"存在即跳过"必须绑定输入指纹，否则改配置不生效（VideoLingo 踩过）
- 换 TTS 引擎/音色必须使下游全部失效（stale），不能只删单步
- 长视频 amix 要 `normalize=0`，否则人声被平均衰减 6dB
- edge-tts 走代理要直连规则；IndexTTS 探测用 `trust_env=False` 绕代理
