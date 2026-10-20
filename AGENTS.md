# AGENTS.md — DubForge AI 协作规则

AI 助手在本仓库工作前必读。当前架构见 `docs/architecture.md`，开发与验证见 `docs/development.md`，发行流程见 `docs/releasing.md`。历史架构决策保留在 `docs/decisions/`。

## 仓库布局与运行

- `backend/`：Python 3.13（uv 管理）。`uv sync` 装基础依赖；完整测试需 `uv sync --locked --extra asr --extra separation`。稳定运行服务用 `uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8600 --loop app.event_loop:create_event_loop`（在 `backend/` 执行，端口以配置为准）；完整测试用 `uv run --locked --extra asr --extra separation pytest`。`--reload` 仅用于明确的开发调试，会中断后台任务。
- `frontend/`：Vue3 + Vite + Naive UI。`npm install` / `npm run dev`（默认 :5273，代理端口由 `DUBFORGE_BACKEND_PORT` 传入；直接运行时默认 8600）/ `npm run build`（产物 dist/ 由 FastAPI 托管）。
- **AI 测试纪律：起服务验证一律用 8611 端口**（`--port 8611`），8600 属于用户的常驻实例，禁止占用/杀进程。
- GitHub 仓库：`heroliss/dubforge`。DubForge 独立开发、独立运行；不要更改仓库公开性，或修改项目之外的程序与服务。

## 铁律

1. **领域库与界面解耦**：`dubforge/` 包内禁止 import fastapi/starlette；API 逻辑只写在 `app/`。
2. **config.yaml 含密钥永不入库**；新增配置项必须同步改 `config.example.yaml` + `dubforge/config.py::DEFAULTS` 三处。
3. **产物原子写**：所有步骤产物先写 `*.tmp` 再 `os.replace`；禁止直接写最终名（中断恢复的根基）。
4. **步骤幂等**：每个 pipeline 步骤实现开头先检查产物是否已完成（输入指纹匹配即跳过）。
5. **中文一等**：用户可见消息（错误/提示/引导）用中文；代码注释中文；错误必须用 `DubForgeError` 子类（错误码稳定）。
6. **测试随行**：新领域逻辑必须有 pytest（`DUBFORGE_DATA_ROOT` 夹具隔离）；提交前跑 `uv run pytest`。
7. **提交规范**：conventional 风格（feat/fix/docs/chore/test）；提交前 staged diff 扫描密钥（正则 `[0-9a-f]{32}\.[A-Za-z0-9]{8}`、`sk-`、`ghp_`）。

## 稳定版本约定

- `docs/plans/` 是历史计划，不能据此把已完成能力标成尚未实现。
- 版本发布前完成后端完整测试、前端测试/类型检查/构建、发布工作者测试、文档链接与密钥检查；准确报告未覆盖的真实模型或平台验收。
- 不删除用户的 `config.yaml`、工作目录、模型缓存、账号和发布回执；清理范围优先限于可再生构建产物及已确认的冗余文件。
- 制作、上传或后台收尾进行时，不重启常驻后端；测试使用独立配置和数据目录，不得复用真实账号或模型服务。
- 版本号、变更记录和 GitHub Release 一致；提交与推送前检查完整暂存区，禁止覆盖其他工作或强推历史。
- 新技术决策写 ADR 到 `docs/decisions/NNNN-标题.md`。

## 外部服务

| 服务 | 地址 | 说明 |
|---|---|---|
| IndexTTS2.5 | http://127.0.0.1:7860 | 独立安装的 Gradio 服务；地址和安装目录在设置中指定 |
| GPT-SoVITS | http://127.0.0.1:9880 | 独立安装的 API 服务，不与 DubForge 共用 Python 环境 |
| LLM | OpenAI 兼容接口 | 通过模型配置连接，不预置真实密钥 |
| 代理 | 用户自行配置 | 网络视频和模型下载按需使用，不硬编码本机代理端口 |

## 必须保持的行为

- 缓存复用必须绑定输入指纹，不能仅根据文件存在判断完成
- 换 TTS 引擎/音色必须使下游全部失效（stale），不能只删单步
- 长视频 amix 要 `normalize=0`，否则人声被平均衰减 6dB
- IndexTTS 探测用 `trust_env=False` 绕代理
- 已有 BV 和未知投稿回执必须先核对原稿，不能自动新建重复稿件；章节失败只重试未完成的章节任务。
- 运行时使用冻结输入；任务结束后先核验产物再解除操作锁，不能把轻量进度摘要当作完整核验结果。
