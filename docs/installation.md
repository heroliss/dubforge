# 安装与环境配置

DubForge 主程序与配音服务使用独立 Python 环境。按所需能力安装即可，不需要复刻开发者电脑的目录。

## 首次安装（Windows）

1. 下载或克隆仓库，解压到可写目录。
2. 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Node.js LTS](https://nodejs.org/)。uv 会按项目要求准备 Python 3.13；Node.js 用于构建前端，启用 B站发布后也用于运行发布通信组件。
3. 安装 [FFmpeg](https://ffmpeg.org/download.html)，将包含 `ffmpeg.exe` 和 `ffprobe.exe` 的目录添加到 PATH，然后重新打开 PowerShell。
4. 在 DubForge 根目录执行所需命令：

```powershell
# 基础程序：下载、音轨提取、文本处理、字幕、调用外部配音服务及成片处理
.\scripts\install.ps1

# 基础程序 + 本地识别
.\scripts\install.ps1 -Asr

# 基础程序 + 本地识别 + 人声/背景音分离
.\scripts\install.ps1 -Asr -Separation

# 已安装主程序后，补装一项；请先关闭 DubForge
.\scripts\install.ps1 -Separation -SkipFrontend

# 可选 B站发布组件；不登录账号、不上传视频
.\scripts\install.ps1 -Publishing -SkipFrontend
```

脚本采用锁文件与 `--inexact`，保留已安装的其他可选依赖，不覆盖已有 `config.yaml`。直接使用 `uv sync` 则会精确同步所选依赖：如需保留其他组件，应添加 `--inexact` 或同时列出所需 `--extra asr --extra separation`。

首次安装会创建配置文件，构建前端；不会安装配音服务或提前下载全部模型。若 PowerShell 的脚本执行策略阻止运行，可在符合自己机器管理规则的情况下使用 `powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1`，这只对该进程生效。

5. 双击 `StartDev.bat`。在「设置 → 环境与服务」检查工具和组件，然后填写大模型及配音连接参数。也可用构建后的单服务方式：`uv run --project backend uvicorn app.main:app --port 8600`。

基础程序中不含 faster-whisper、Demucs、PyTorch。识别与分离选项按需安装这些依赖；当前本地 ML 依赖沿用已验证的 CUDA 12.6 PyTorch 源，体积较大，即使以 CPU 运行也不是精简 CPU 包。NVIDIA GPU 还需兼容驱动，faster-whisper 的 GPU 推理需要对应 CUDA/cuDNN 动态库，详见其[官方要求](https://github.com/SYSTRAN/faster-whisper#gpu)。环境页的“依赖已安装”仅代表发现 Python 包，不保证 GPU 动态库、网络和模型权重已可用。模型在首次执行相应步骤时下载，失败可检查代理后重试。

只想提取音轨而不做人声分离时，在「音轨分离」参数中关闭分离选项，无需安装 Demucs。

## 可选配音服务

### IndexTTS 2.5

按[官方安装说明](https://github.com/index-tts/index-tts#-getting-started)在独立目录安装依赖并下载对应权重。Windows 可只选择 WebUI 依赖，避免额外安装并非必需的 DeepSpeed：

```powershell
# 在 index-tts 自己的目录执行，不在 DubForge/backend 执行
uv sync --extra webui
```

随后按官方说明下载 IndexTTS-2.5 权重到其 `checkpoints` 目录，先确认官方 WebUI 可以启动。DubForge 默认使用 `http://127.0.0.1:7860/`，安装目录填写含 `webui.py` 和 `.venv` 的目录。目录位于 DubForge 同级并命名为 `index-tts` 时可自动发现。

从 DubForge 页面或控制台快捷启动 IndexTTS 时，会在服务进程内启用 Gradio 的迭代进度跟踪，不修改 IndexTTS 文件。每句进度将结合服务端实际迭代次数细化；语音 token 生成等没有细分进度的阶段仍会等待，不按时间自动增长。升级前已经运行的服务需在任务结束后停止并重新快捷启动才能启用；从原始 WebUI 启动的服务仍可能只返回少量阶段节点。

使用文本情感需要服务端加载 QwenEmotion。DubForge 在当前项目已保存为文本情感模式、服务尚未启动时，会使用 `--qwen_emo` 启动；已运行的服务不会自动重启，需要自行按该参数重启。模型加载可能增加显存占用。

### 网页启动、查看日志与关闭服务

配音页「启动服务」在后台运行服务，不弹出终端窗口。「查看日志」每 3 秒刷新最后 64 KB 输出，可查看模型加载、错误和合成信息；完整日志位于工作目录 `logs/<后端>-<端口>.log`。

StartDev 主控制台也可以统一管理这些服务：`services` 列出全局及项目独立地址的服务标识，`start indextts25` / `stop indextts25` 启停，`logs indextts25` 查看最近输出，`watch indextts25` 连续跟随（Esc 或 Q 返回）。GPT-SoVITS 的标识为 `gptsovits`；其他地址请照抄列表中的标识。命令通过当前后端接口执行，沿用网页的运行保护，不修改项目选用的后端。

`quit`、`exit`、不带服务名的 `stop` 和 Ctrl+C 只关闭本次启动的前后端，保留复用的后端与独立配音服务。前后端日志按端口分别存于 `data/dev-logs/<后端端口>-<前端端口>/`，再次启动前会归档旧日志；启动失败会在主控制台显示最近错误。

隐藏窗口不会丢弃已输出的标准输出和错误，但终端专有的颜色、动态进度条可能变化，需要输入的确认、登录、安装向导也无法在后台操作。首次安装或此类调试应先用第三方原始终端完成；常规模型加载和合成可通过日志查看。现阶段统一管理项目已接入的两个配音服务，远程 LLM、代理及其他软件仍由其原有工具管理。

由 DubForge 启动的本机服务会显示 PID 和「停止服务」，启动尚未完成时也可停止。服务由多个项目共用，正在配音时需先结束任务；停止会关闭该服务及其 Python 子进程，释放模型资源。关闭网页或退出 DubForge 不会自动停止服务；进程管理记录在重启后通过 PID、创建时间、程序路径和命令行重新核验，只接管身份匹配的原进程。外部启动、远程连接或旧版本未记录的服务需在原启动位置关闭。

### GPT-SoVITS

Windows 可从[官方说明](https://github.com/RVC-Boss/GPT-SoVITS#installation)下载整合包并解压。填写含 `api_v2.py` 的目录；快捷入口支持整合包的 `runtime/python.exe` 或该目录的 `.venv`。默认 API 地址为 `http://127.0.0.1:9880/`，它与训练 WebUI 是不同服务。模型及参考音频仍需按该服务要求配置，启动成功不代表音色参数已齐备。

安装在其他目录时直接填写路径即可；旧 `VideoLingo-Workspace` 搜索只是兼容已有安装，不是新用户的必备目录。

### 已有服务或远程服务

在「设置 → 全局步骤参数 → 配音」填写地址和安装目录；项目配音页可覆盖全局值。使用远程服务时无需本机安装对应 TTS 环境，启动由远程机器管理。`127.0.0.1` 指运行 DubForge 后端的机器。

## 参数与预设

- 全局设置写入 `config.yaml`，项目覆盖保存在项目数据中；GitHub 只提供 `config.example.yaml`。
- 配音页显示当前后端的全部合成参数，悬停控件或点击 `?` 查看说明；向量、文本情感按所选模式显示。
- 载入预设只覆盖当前表单，点击「保存项目参数」后才用于合成；未保存时阻止合成。预设不会在合成时被重新读取。
- 保存预设记录当前表单，不自动保存项目参数。参考音频复制到预设目录，JSON 使用相对路径。
- `IndexTTS 共享预设库` 对应其 `outputs/presets/<名称>/preset.json`，使用 WebUI v1.0 通用字段；IndexTTS 刷新预设列表后即可读取。
- DubForge 用 `dubforge` 扩展字段保存语言、时长因子和超长重试因子，原生 WebUI 忽略此扩展，重新保存时可能去掉它。载入缺少这些字段的原生预设时，页面对应设置保持不变。
- 视频开头/逐句原声属于 DubForge 的项目规则；只保存到工作目录 `presets/indextts25`，不能作为等价的共享音色预设。要两边通用，请使用固定参考音频。
- 同名覆盖需显式勾选；服务地址、安装目录和运行超时不会随音色预设切换。

发布前应在干净机器上完成所支持版本的真实识别、配音和成片验证；依赖安装、服务在线检查、模拟测试不能代替模型端到端验证。
