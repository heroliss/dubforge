# 架构说明

本文说明当前源码的模块边界与数据合同；安装操作见[安装指南](installation.md)，改动与验证方式见[开发与维护](development.md)。

## 运行形态

DubForge 面向本机、单用户的视频制作工作流，主程序为 Python 后端和 Vue 前端。开发时 Vite 与后端分别运行；`npm run build` 生成 `frontend/dist/`，后端发现该目录后托管页面、静态资源和 API。当前维护入口以 Windows 为准，B站账号凭据保护使用 Windows 当前用户的 DPAPI；不将其他操作系统或公网多用户部署列为已验证支持范围。

| 层 | 主要位置 | 职责 |
|---|---|---|
| 界面 | `frontend/src/views/`、`components/`、`stores/`、`utils/` | 编辑草稿、展示参数来源、预检确认、进度和产物状态 |
| HTTP 适配 | `backend/app/main.py`、`app/api/` | 请求校验、错误响应、文件流与前端托管；应用生命周期负责恢复和关闭执行器 |
| 领域逻辑 | `backend/dubforge/` | 配置、项目、依赖、制作、恢复、评论、成片与发布；不依赖 FastAPI/Starlette |
| 平台通信 | `workers/bilibili/`、`dubforge/publishing/transport.py` | 通过有界 JSONL 子进程调用 Node 发布组件；凭据经匿名管道传递 |
| 外部工具与模型 | FFmpeg/FFprobe、识别/分离依赖、TTS 服务、OpenAI 兼容 LLM | 按所需能力安装与配置；不随主程序捆绑全部模型 |

独立的 IndexTTS / GPT-SoVITS 服务有自己的环境和生命周期。关闭页面不会停止后台制作；退出主程序也不会自动关闭独立托管的配音服务。

首个正式版本的源码交付、统一版本号、显式运行目录和 Windows 媒体读取约定见 [ADR 0232](decisions/0232-first-stable-source-release.md)。

完整音轨缓存与服务登记的文件锁直接协调空文件，不在加锁前写入占位字节；并发、取消与释放约定见 [ADR 0233](decisions/0233-empty-file-locks-without-prewrites.md)。

## 制作与交付是两个层次

制作步骤定义在 [`pipeline/registry.py`](../backend/dubforge/pipeline/registry.py)：获取视频、音轨分离、语音转文字、语义整理、翻译、字幕与配音、单独字幕导出、合成成片。内部 `dubtask` 键保留兼容，准备任务表已归入配音操作；发布使用独立交付状态机。

执行计划按实际目标解析依赖，不能把展示顺序当成所有步骤必须执行的线性链。例如烧录字幕可以直接消费校对稿，不要求先单独导出 SRT；原声版本不应为未使用的配音重跑整个上游。相关入口是 [`workflow_plan.py`](../backend/dubforge/workflow_plan.py) 与 [`project_execution.py`](../backend/dubforge/project_execution.py)。

| 对象 | 主要模块 | 说明 |
|---|---|---|
| 项目、步骤、任务、产物 | `domain/models.py`、`domain/crud.py` | `Project`、`StepRun`、`Job`、`Artifact` 保存不同层次的事实；任务结束不等于产物已通过当前配置核验 |
| 单步执行 | `runner.py` | 创建任务、资源准入、暂停/停止、日志、阶段进度和最终登记 |
| 项目完整流程 | `workflows.py`、`workflow_plan.py` | 保存确认的计划，复用有效结果，并按选择推进至制作、草稿、上传或投稿终点 |
| 多项目与顺序发布 | `project_batches.py`、`release_batches.py` | 独立分组推进、合集内顺序、项目占用与已确认稿件复用 |
| 评论准备 | `comment_studio.py`、`comment_preparation.py`、`comment_recovery.py` | 采集、评分、按需翻译与上屏选择；保留成功结果，区分内容拒绝和服务故障 |
| 发布、原稿更新与章节 | `publishing/service.py`、`updates.py`、`chapter_dispatch.py` | 原始投稿、保留原 BV 的更新及发布后章节各有状态和回执，不混成一次上传重试 |

预检只说明将执行什么，确认请求才授权执行。恢复保留原流程身份和已接受请求回执；回执不明时先核对原操作，不能通过新建流程规避防重保护。

## 设置、冻结快照与有效性

全局配置位于不入库的 `config.yaml`，模板见 [`config.example.yaml`](../config.example.yaml)。全局/系列模板、项目基础值与项目单独覆盖由配置领域统一解析；已有项目的制作基础值不会因全局默认或模板后来变化而悄悄替换，更新基础值需要主动应用。服务连接等运行参数按其配置来源解析，不应与冻结的制作基础值混为一谈。

制作和恢复使用确认范围内的配置与版本快照。成片版本有独立身份、配置、制作目标和正式结果；原声/配音、字幕、评论及编码方式不能只靠文件名猜测。软件编码与 NVENC 的质量参数含义不同，硬件编码是显式选择，不静默降级或把 CQ 与 CRF 当成等价值。

[`pipeline/dependencies.py`](../backend/dubforge/pipeline/dependencies.py) 按实际消费合同核对上游、当前参数、文件及所选音频。输入指纹和输出登记共同决定有效性：

- 文件存在不等于可以复用；任务曾经成功不等于当前参数下仍是最新。
- 参数变化与上游变化分别解释，未消费的可选设置不应使无关版本失效。
- 临时文件完成后再原子替换正式文件；中断不能把半成品登记为成功。
- 局部修订、剪辑与试片有独立范围，不能把预览成功当成正式版本已更新。

相关决策：[产物完整性](decisions/0002-artifact-integrity.md)、[依赖合同](decisions/0016-artifact-dependencies.md)、[当前目标完成判定](decisions/0227-current-goal-production-completion.md)、[显式编码与资源准入](decisions/0231-explicit-video-encoder-and-render-admission.md)。

## 状态读取与界面交接

页面使用轮询，不依赖 WebSocket。轻量执行快照用于及时显示当前任务，完整读取负责配置与产物有效性核验。项目列表先读取明确标注的摘要；摘要未核验的产物保持未知，不能据此显示“已是最新”。

执行快照按项目、任务身份和代次合并。已知同一任务结束、但完整核验尚未返回时，界面显示“产物核验中”，保留只读检查入口并阻止重复运行；完整结果返回后再显示完成、过期或真实失败。迟到的旧任务/旧完整响应不能使新任务倒退，也不能提前解除核验锁。

进度百分比、当前阶段 ETA 和版本范围必须同源。执行累计时间可能包括排队，活动时间与阶段时间不能混用；媒体秒量读数显示时分秒，原始数值仍用于计算。详见 [执行时钟](decisions/0224-execution-clock-semantics.md)、[轻量刷新](decisions/0221-lightweight-execution-refresh.md)、[摘要与后台核验](decisions/0230-project-list-summary-and-background-verification.md)。

## 数据与恢复边界

工作目录由 `DUBFORGE_DATA_ROOT`、`server.data_root` 或默认 `data/` 决定，配置文件可由 `DUBFORGE_CONFIG` 指定。数据库打开后绑定本次工作目录，改设置不能在运行中将数据库和产物拆到两处。

SQLite `dubforge.db` 使用 WAL。项目目录中保存 manifest、媒体引用、模型与校对结果、音频、成片和日志。启动可以从项目目录补回项目索引，但流程、批次、发布回执、配置和账号记录也保存在数据库中，**manifest 不是完整数据库备份**。本地导入可能引用目录外的视频，项目备份不能代替外部源文件备份。

应用启动会初始化/迁移数据并进行中断恢复，不能将第二个服务指向用户正在使用的同一工作目录做测试。重启中断中的远端 TTS、投稿或章节写入可能留下待核对结果；原子文件不能消除远端副作用的不确定性。数据库和原回执必须保留，按已有恢复入口继续。

## 发布边界

上传完成、取得 BV、审核通过和公开可见是不同事实。退回稿件显示真实原因并等待人工处理；自动监控只核对原稿状态，不将退回或未知结果自动改为再次投稿。更新原稿、复制新稿和重试章节都需要各自明确的检查与确认。

账号凭据由 Windows DPAPI 加密，迁移到其他 Windows 用户或机器后可能需要重新登录。自动文本审查与章节适配只是辅助，不能保证平台审核通过，也不能代替内容授权。相关决策：[发布边界](decisions/0041-bilibili-publication-boundary.md)、[远端核对](decisions/0141-bilibili-remote-reconciliation-boundary.md)、[原稿可恢复交付](decisions/0182-original-bv-update-and-recoverable-delivery.md)。
