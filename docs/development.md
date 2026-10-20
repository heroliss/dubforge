# 开发与维护

开发前阅读 [AGENTS.md](../AGENTS.md)；首次安装见[安装指南](installation.md)。以下命令在 Windows PowerShell 中使用，默认从仓库根目录开始。本文不声明 Linux/macOS、独立可执行文件或一键容器部署已经验收。

## 环境与依赖

- 后端要求 Python `>=3.13,<3.14`，由 `uv` 和 `backend/uv.lock` 管理。
- 前端与 B站通信组件分别使用各自的 `package-lock.json`。Node.js 必须支持仓库测试脚本使用的 `--experimental-strip-types`。
- FFmpeg 与 FFprobe 需在 PATH。识别、分离是后端可选依赖；TTS 使用独立服务环境，详见安装指南。

优先使用安装脚本，避免精确同步意外卸掉已选装的组件：

```powershell
.\scripts\install.ps1
# 按需一次选装；不会安装 TTS 服务或自动登录B站
.\scripts\install.ps1 -Asr -Separation -Publishing
```

维护依赖时分别处理 `backend/`、`frontend/`、`workers/bilibili/`。`uv sync --locked --inexact` 保留已安装的其他可选组件；精确同步则必须明确列出所需 extra。前端用 `npm ci`，发布组件用 `npm ci --ignore-scripts`。修改依赖应同时检查锁文件，不在依赖目录中修补第三方源码。

## 开发启动

日常使用 `StartDev.bat`，或在根目录执行：

```powershell
.\scripts\dev.ps1
# 不自动打开网页
.\scripts\dev.ps1 -NoBrowser
```

启动器读取配置中的后端端口，将其传给 Vite；前端默认端口为 5273。后端默认保持稳定运行，Python 修改需在任务安全结束后通过原控制台 `restart backend` 载入。显式 `-ReloadBackend` 用于调试，会因代码变化打断后台任务，不适合长时间制作或投稿。

需要分别启动时，明确指定同一个后端端口；下面 8600 是示例，须先确认未被占用。AI/自动验证一律使用隔离的 8611，不能操作用户常驻端口。

```powershell
# 终端一，仓库根目录
uv run --project backend --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8600 --loop app.event_loop:create_event_loop

# 终端二
$env:DUBFORGE_BACKEND_PORT = '8600'
$env:DUBFORGE_FRONTEND_PORT = '5273'
Set-Location frontend
npm run dev
```

不要根据裸 `npm run dev` 的隐含代理默认值判断后端端口；启动器环境变量或上述显式设置才是本轮连接依据。Windows 后端使用项目事件循环工厂，以保持普通启动和重载启动的行为一致。

## 原控制台与服务管理

| 命令 | 用途 |
|---|---|
| `status`、`open` | 查看前后端进程/监听状态，打开已核验可用的网页 |
| `logs backend`、`logs frontend`、`logs launcher` | 查看后端、前端或启动器日志 |
| `watch backend` | 连续查看日志，Esc/Q 返回 |
| `start frontend`、`stop frontend`、`restart backend` | 单独管理本控制台有权管理的服务 |
| `services` | 查看全局与项目配置的配音服务标识 |
| `start indextts25`、`stop indextts25`、`logs indextts25` | 通过既有服务协议管理配音服务；其他标识按 `services` 输出填写 |
| `quit` / `exit` | 停止本控制台管理的前后端；保留复用后端与独立配音服务 |

前后端日志位于程序目录 `data/dev-logs/<后端端口>-<前端端口>/`，包括 `launcher.log`、`backend.{out,err}.log`、`frontend.{out,err}.log` 和生命周期日志；启动器会归档已有非空服务日志。该目录不是项目产物目录，不随 `server.data_root` 自动迁移。

`dev.ps1` 调用的 `dev-console.ps1`、`dev-web.ps1`、`dev-output.ps1` 和 `dev-owner.ps1` 都是运行代码，不是可随意删除的临时脚本。

维护者确需重新连接已登记的前后端时，可使用 `-AttachRegistered` 并传入原端口。此入口先核验工作区、端口树、PID、创建时间及程序/命令身份；两侧必须完整通过，失败不会退回“按端口杀进程”。它不为未登记服务补造归属，也不会热更新仍在运行的旧控制台。避免两个控制台同时争管同一组服务；接管后退出新控制台将停止它接管的前后端。

## 隔离验证

仅换端口不够：应用启动会迁移、扫描并恢复工作目录。创建独立数据和配置后再运行任何验证服务。以下在一个新的 PowerShell 窗口执行，窗口关闭后这些环境变量自然失效：

```powershell
# 仓库根目录；不复制真实 config.yaml 或用户数据库
$checkRoot = Join-Path ([IO.Path]::GetTempPath()) ('DubForge-check-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $checkRoot | Out-Null
$env:DUBFORGE_DATA_ROOT = Join-Path $checkRoot 'data'
$env:DUBFORGE_CONFIG = Join-Path $checkRoot 'config.yaml'
Copy-Item -LiteralPath '.\config.example.yaml' -Destination $env:DUBFORGE_CONFIG
$env:HF_HOME = Join-Path $checkRoot 'hf'
Remove-Item Env:DUBFORGE_DEBUG_DATADIR -ErrorAction SilentlyContinue

# 此命令保持前台运行；只停止本窗口启动的验证实例
uv run --project backend --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8611 --loop app.event_loop:create_event_loop
```

另一个终端可只读检查 `http://127.0.0.1:8611/api/health`。健康响应证明 API 已启动，不证明模型可用、前端构建完整或真实视频交付成功。发行冒烟还核对源码版本、同一子进程的监听归属和空项目列表。真实模型、媒体和平台联调应另外明确范围，不能沿用生产账号、真实目录或付费服务做默认测试。

## 测试与构建

```powershell
# 全量后端测试需要识别与分离可选依赖；夹具隔离配置与数据
uv sync --locked --directory backend --extra asr --extra separation
Push-Location backend
uv run --locked --extra asr --extra separation pytest
Pop-Location

# 前端：串行执行各测试文件，限制 Node 堆
Push-Location frontend
npm test
npm run typecheck
npm run build
Pop-Location

# B站通信组件的本地测试
Push-Location workers/bilibili
npm test
Pop-Location
```

`npm run typecheck` 使用 `tsc`，不能替代 Vue 模板与交互验证；还需构建、真实组件测试和相应浏览器验收。后端模型/媒体测试、Node 平台组件测试与真实平台联调也应分别报告，聚焦测试数量不能相加冒充全仓通过。

冒烟执行器自身的安全回归不启动服务，可用 `backend/.venv/Scripts/python.exe -m unittest discover -s scripts/tests -p test_smoke.py -v` 从根目录执行。

`scripts/build.ps1` 执行前端构建及后端模块导入检查；它不生成独立 EXE、不安装依赖、不打包模型，也不代替测试。`scripts/smoke.ps1` 默认执行完整后端 pytest、固定 8611 的真实健康检查、前端构建；它会新建临时配置/数据/HF缓存/TEMP，且只复制示例配置，不读取真实配置。冒烟的前端构建输出也位于临时目录，不替换用户正在使用的 `frontend/dist/`。占用 8611 时拒绝，不结束已有服务；关闭时只使用本次创建的精确进程句柄。`-SkipTests` / `-SkipFrontendBuild` 可复用此前确已完成的检查，跳过项不算本次通过。失败保留日志，`-KeepTemporaryData` 可显式保留成功时的隔离目录。它会启动进程和执行构建，仍不是只读检查。

构建后可以只启动后端，通过同一端口访问 API 和已构建页面；仍需 Python/Node 发布组件及所选外部工具。修改前端后需重新构建，旧 `dist/` 不是当前源码的验证证据。

## 改动合同

- 新领域逻辑放在 `dubforge/`，不要在其中导入 FastAPI/Starlette；HTTP 适配留在 `app/`。
- 新配置项同步默认值、示例及相应参数 schema；区分项目设置、服务连接和真正影响产物的值。
- 正式文件先写临时文件，再原子替换；完整核验和登记成功前保留原有有效产物。
- 改消费范围、编码、字幕、选槽或音频策略时核对输入指纹、冻结计划与 manifest，不只验证 UI 保存成功。
- 中文业务错误提供稳定 `code`、`message`、`suggestion`；区分失败、取消、排队、已受理、核验中和远端结果不明。
- 恢复复用原任务与请求身份，不能通过清数据库、删除回执或新建稿件“解决”未知结果。
- 新行为附带隔离测试；影响界面的状态迁移需验证真实组件或浏览器行为，SSR 静态文字不能替代全部交互。

设计变化记录在 [ADR 目录](decisions/)，历史研究与验收不当成活动待办。[架构说明](architecture.md)列出主要模块入口。

## 备份、升级与迁移

依赖安装、代码升级、配置目录迁移和后端重启应安排在真实任务空闲后。先检查制作、评论、试片、流程、批次、上传、稿件更新和章节后处理；仅看到某个 Job 已完成不足以证明全局空闲。远端请求仍在途时等待真实回执，不删除历史未知凭证。

停止有权管理的主程序后，备份完整工作目录、`config.yaml` 和仍被引用的外部源视频/参考音频。SQLite 使用 WAL，不能在活动写入时只复制 `dubforge.db` 并假定备份完整。项目 manifest 可以补回部分索引，但发布回执、流程、批次和账号资料也依赖数据库。

更换工作目录应先完整迁移并确认路径，再在下次启动时使用；保留原目录直到验证结束。回滚代码前也应检查数据结构兼容性，必要时同时恢复升级前的数据库/配置/数据备份。Windows 当前用户加密的B站凭据不能承诺跨机器解密，迁移后可能需要重新扫码。

发布时保留依赖锁文件，排除真实配置、运行数据、Cookie、日志、预设中的私有音频及模型权重。真实截图应脱敏并注明示例/演示边界；具体版本的测试、构建和实际联调结果写在发布记录，不在通用指南中永久声称“全部通过”。
