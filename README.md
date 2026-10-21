<div align="center">

<h1>DubForge</h1>

<p><strong>把视频翻译、字幕校对、配音和成片，放进同一个工作台。</strong></p>
<p>从一段短片开始，逐句打磨；再用模板，把同一套制作方式复用到更多视频。</p>

<p>
  <a href="#快速开始">开始使用</a> ·
  <a href="#在工作台里完成制作">看看怎么用</a> ·
  <a href="#技术路线">了解实现</a> ·
  <a href="docs/README.md">全部文档</a>
</p>

</div>

<p align="center">
  <img src="docs/images/project-overview.webp" alt="项目总览：①确认交付终点与目标成片，②检查依赖并运行。" width="960">
</p>

**先确定想得到什么，再运行需要的步骤。** 可以只做字幕，也可以制作配音成片；需要投稿时，再连接 B 站交付。

> **[v1.0.0 正式版](https://github.com/heroliss/dubforge/releases/tag/v1.0.0)** · Windows 源码发行 · 浏览器访问本地工作台

## 一条视频，按你的目标制作

输入视频链接或本地文件，从校对稿逐步得到成片。自动流程按目标计算依赖，也可以进入任一步骤检查和调整。

<img src="docs/images/workflow-overview.svg" alt="制作流程示意：导入与计划、校对文字和声音、组合成片版本，最后本地导出或按需交付到 B 站。" width="960">

| 从这里开始 | 再进一步 |
| --- | --- |
| **一份校对稿**：对照视频修改原文、译文和时间 | 逐句试听、保留原声或选择配音，修改后只更新受影响的声音 |
| **一个成片版本**：组合声音、字幕和上屏评论 | 保留多个版本，分别管理参数、剪辑清单和输出 |
| **一个短片项目**：先试少量，再制作整片 | 保存模板，选择多个项目检查并批量运行 |

## 快速开始

### 1. 安装常用能力

先准备 [Git](https://git-scm.com/downloads)、[uv](https://docs.astral.sh/uv/getting-started/installation/)、[Node.js 24 LTS](https://nodejs.org/) 和 [FFmpeg](https://ffmpeg.org/download.html)（建议 7.1 或更新版）。`ffmpeg`、`ffprobe` 需能在终端中运行；Python 3.13 由 uv 准备。

在 **PowerShell** 中执行，安装主程序、本地识别与音轨分离：

```powershell
git clone https://github.com/heroliss/dubforge.git
cd dubforge
git checkout v1.0.0
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Asr -Separation
```

下载[发行版源码压缩包](https://github.com/heroliss/dubforge/releases/latest)的用户，解压后在项目目录执行最后一行即可。私有仓库的源码与发行版需要已有仓库权限。

<details>
<summary>按需安装：只做字幕、增加投稿，或已有配音服务</summary>

安装选项可以组合，已有配置不会被覆盖：

| 能力 | 安装参数 | 还需准备 |
| --- | --- | --- |
| 基础程序 | 不带选项 | LLM 连接；按需使用已有字幕 |
| 本地语音识别 | `-Asr` | 首次执行时下载识别模型 |
| 人声与背景音分离 | `-Separation` | 分离模型；只提取音轨时可关闭分离 |
| B 站发布组件 | `-Publishing` | 安装后在工作台连接账号 |

**配音服务独立安装。** 使用配音时，按各自官方说明准备 IndexTTS 2.5 或 GPT-SoVITS，再在 DubForge 中填写地址与音色参数；安装脚本不包含这些服务或全部模型。

识别与分离依赖体积较大。GPU、缓存位置、已有服务和补装命令见[安装指南](docs/installation.md)。

</details>

### 2. 启动并打开网页

在项目目录执行：

```powershell
uv run --project backend --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8600 --loop app.event_loop:create_event_loop
```

保持这个终端运行，在浏览器打开 **[http://127.0.0.1:8600](http://127.0.0.1:8600/)**。后端同时托管安装时构建好的网页。

<details>
<summary>需要开发模式或统一控制台？</summary>

运行 `StartDev.bat`，可按配置启动后端与 Vite，通过控制台查看状态、日志和管理服务。默认前端入口为 [http://127.0.0.1:5273](http://127.0.0.1:5273/)；端口及操作说明见[开发指南](docs/development.md)。

</details>

### 3. 先做一段短片

1. **连接服务**：在「设置 → 模型与服务」连接 LLM、检查识别组件；需要配音时，再按[配音服务说明](docs/installation.md#可选配音服务)配置独立服务。
2. **创建项目**：输入一段短视频的链接或本地路径。首次可用默认设置，需要模板时再在项目空间绑定。
3. **检查并运行**：在项目总览核对制作目标，检查计划并确认运行；校对稿就绪后，再进入下一步。
4. **校对与试片**：先试听几句，再生成短试片；确认翻译、音色、字幕和时间后，制作正式成片。

接下来按下面的工作区逐步操作。

## 在工作台里完成制作

### 校对文字，再确定声音

原片、双语句子与逐句设置放在同一页。字幕、试听和成片读取同一份校对稿，减少来回修改。

<img src="docs/images/subtitles-dubbing.webp" alt="字幕与配音：①对照原片，②选择句子与声源，③编辑文字和时间。" width="960">

**① 对照原片 → ② 选择句子与声源 → ③ 编辑文字和时间**

先选有代表性的句子试听，再批量补齐配音。列表勾选决定本次操作哪些句子；原声／配音选择决定成片使用哪种声音。

### 组合版本，再检查剪辑

同一项目可以保留原声版、配音版或不同字幕方案。每个版本各有自己的参数、删除片段清单和输出。

<img src="docs/images/assembly-versions.webp" alt="合成成片：①项目目标版本，②本次手动生成版本，③当前版本的删除片段清单。" width="960">

**① 项目目标版本 · ② 手动生成范围 · ③ 当前版本的剪辑清单**

先在预览里检查剪辑后的时间轴，再生成短试片，最后制作正式版本。**章节与成片共用剪辑时间映射**；新结果核验完成前保留已有有效版本。

### 从模板扩展到批量制作

在「制作与发布」保存一套可复用规则：目标、声音、字幕、成片版本与交付方式都按阶段设置。

<details>
<summary>查看模板设置界面：①选择模板，②按阶段设置，③保存成片方案</summary>

<img src="docs/images/production-settings.webp" alt="模板设置：①全局默认或系列模板，②阶段设置，③可复用的版本方案。" width="960">

新项目保存当时的基础设置；修改模板后，可以检查差异并主动应用到已有项目。正在执行的任务继续使用已确认的输入。

</details>

回到项目空间，**指定模板 → 勾选项目 → 检查并批量运行**。

<img src="docs/images/project-space.webp" alt="项目空间：①为项目指定模板，②勾选项目，③检查计划并批量运行。" width="960">

同一目标合集按确认顺序推进，其他分组独立运行；一组需要处理时，其他组可以继续。一次创建多个项目的方法见[批量导入与 JSON 示例](docs/batch-project-import.md)。

### 按需交付到 B 站

在「发布到 B 站」设置账号、分 P 顺序、文案和合集，再检查计划并确认。先安装 `-Publishing` 组件；本地制作不要求连接 B 站账号。

| 情况 | 工作台如何处理 |
| --- | --- |
| 首次投稿的新项目 | 默认准备新稿，运行前检查交付设置 |
| 已有发布记录，或远端结果不明 | 先核对，明确选择新稿或更新原稿，避免重复投稿 |
| 上传完成，仍在转码、审核或同步章节 | 分别呈现进度；上传完成不等于已经公开发布 |

<details>
<summary>任务中断后，如何继续？</summary>

关闭网页后，后台任务会继续；关闭后端会中断正在执行的工作。恢复前先查看原任务和错误说明。

程序通过输入指纹、冻结快照和持久回执复用有效结果，结束后核验产物；平台操作结果不明时先核对回执。具体处理见[任务恢复与排障](docs/troubleshooting.md)。

</details>

## 技术路线

DubForge 独立开发、独立运行。界面、任务状态和媒体处理在本机协作；模型服务与平台连接按所选目标启用。

<img src="docs/images/local-architecture.svg" alt="运行结构示意：浏览器连接本地 Python 后端，SQLite 与文件保存项目，媒体工具、本地或远程模型服务、B站工作者按需协作。" width="960">

<details>
<summary>展开技术栈与九阶段流程</summary>

| 部分 | 方案 |
| --- | --- |
| 界面 | Vue 3、TypeScript、Naive UI、Pinia |
| 应用与状态 | Python 3.13、FastAPI、SQLModel／SQLite；领域逻辑独立于 HTTP 层 |
| 视频与字幕 | yt-dlp、FFmpeg／FFprobe |
| 识别与分离 | faster-whisper、可选 Demucs |
| 语义整理与翻译 | OpenAI 兼容 LLM，可配置服务与故障回退 |
| 配音 | 独立 IndexTTS 2.5 或 GPT-SoVITS 服务 |
| B 站连接 | 独立 Node.js 工作者，使用 @renmu/bili-api |
| 恢复与复用 | 原子写入、输入指纹、冻结快照、后台任务与持久回执 |

界面按九个阶段组织：

```text
获取视频 → 音轨分离 → 语音转文字 → 语义整理 → 翻译
                                           ↓
发布到 B 站 ← 合成成片 ← 上屏评论 ← 字幕与配音
```

实际执行计划按目标解析依赖，**不要求每个项目运行全部九步**。模块边界、数据流与状态设计见[架构说明](docs/architecture.md)。

</details>

## 继续阅读

| 现在想做什么 | 从这里进入 |
| --- | --- |
| 安装、连接模型，或解决运行问题 | [安装指南](docs/installation.md) · [故障排查](docs/troubleshooting.md) |
| 批量创建项目、复用制作规则 | [批量导入](docs/batch-project-import.md) · 应用内「使用帮助」 |
| 理解源码、参与开发和维护 | [架构说明](docs/architecture.md) · [开发指南](docs/development.md) · [贡献指南](CONTRIBUTING.md) |
| 查看版本、验证与发行方式 | [变更记录](CHANGELOG.md) · [v1.0.0 验证记录](docs/validation-v1.0.0.md) · [发行流程](docs/releasing.md) |

完整导航与历史设计依据见[文档索引](docs/README.md)。

<details>
<summary>截图、数据与许可说明</summary>

- **真实界面截图**来自隔离演示项目，使用自写双语稿和自绘视频，不含真实账号、密钥或私有作品；不代表已完成真实模型或平台交付。圈线标注的原图与复现方法见[截图工具说明](scripts/docs/README.md)。两张流程／架构图是说明性示意图。
- **源码发行不包含**模型、配音服务、账号凭据和视频产物。升级前保留自己的配置与工作目录，先阅读变更记录。
- **仓库保持私有，尚未指定开源许可证。** 第三方组件与模型遵循各自的许可证及使用条款，见[第三方说明](THIRD_PARTY.md)。

</details>
