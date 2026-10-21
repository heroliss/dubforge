# 文档索引

DubForge 是独立开发的视频翻译配音工作台。当前发行提供源码、依赖锁文件和 Windows PowerShell 脚本；前端构建后由后端托管，不是包含全部模型的独立安装包。

## 从哪里开始

- **第一次了解**：[首页图解](../README.md#一条视频按你的目标制作) → [快速开始](../README.md#快速开始) → [分主题教学](tutorials/README.md)。
- **已经在使用**：按下面的使用与安装表，查找模型配置、批量导入或故障处理。
- **准备开发维护**：先读架构与开发指南，再按具体问题查阅 ADR；历史计划不代表当前待办。

## 使用与安装

| 需要做什么 | 阅读位置 |
|---|---|
| 了解能力、界面与基本流程 | [项目首页](../README.md)；应用内「使用帮助」 |
| 由浅入深学习：短片、手动步骤、自动流程与设置 | [教学目录](tutorials/README.md) · [第一条视频](tutorials/first-video.md) · [手动制作](tutorials/manual-workflow.md) · [自动流程与模板](tutorials/automation-and-templates.md) · [设置说明](tutorials/settings.md) |
| 首次安装、选装识别/分离/发布组件、连接配音服务 | [安装与环境配置](installation.md) |
| 从文本或 JSON 批量创建项目 | [批量创建项目](batch-project-import.md)；[基础样板](examples/batch-projects.json)、[双分P样板](examples/batch-projects-two-parts.json) |
| 处理启动、服务、缓存、恢复及投稿问题 | [故障排查](troubleshooting.md) |

## 开发与维护

| 需要了解什么 | 阅读位置 |
|---|---|
| 模块边界、产物有效性、任务与发布状态 | [架构说明](architecture.md) |
| 开发启动、隔离测试、构建、升级与备份 | [开发与维护](development.md) |
| 贡献代码与报告问题 | [参与开发](../CONTRIBUTING.md) |
| 完整检查、版本核对、tag 与源码 Release | [版本发布](releasing.md) |
| v1.0.0 的实际测试、隔离启动与验证边界 | [v1.0.0 验证记录](validation-v1.0.0.md) |
| 重现演示界面截图及圈线编号 | [截图工具说明](../scripts/docs/README.md) |
| AI 协作约束、测试端口与提交规则 | [AGENTS.md](../AGENTS.md) |
| 某项设计为何这样实现 | [架构决策目录](decisions/) |

## 历史资料

- [decisions/](decisions/) 保存带编号的设计决策。早期 ADR 描述的是当时的方案；查当前行为时，应结合后续 ADR、上述维护指南和实际源码。
- [plans/roadmap.md](plans/roadmap.md) 是早期 M0–M6 规划快照，不是当前版本的待办清单或完成声明。
- [research/](research/) 保存按日期记录的研究、诊断和验收。测试数量、机器性能、平台状态和技术推荐仅代表记录当时的范围，不能直接当成当前全量结论。
- [技术路线研究](research/technology-landscape-2026.md) 用于了解当时的候选比较；安装依赖与支持范围以当前安装指南、锁文件和源码为准。

历史文档中的开发机器目录、实验参数和第三方系统名称不是新用户的安装前提。第三方软件与模型的安装、授权及使用条件仍由各自项目说明。
