# 个人技能安装与审查

> 此文记录最初安装和整改前的问题。用户随后已授权修复、共享目录整理及去除备份，当前结果见 [Codex 适配与 CC Switch 整理](skills-codex-adaptation-2026-10-16.md)。文中“本轮未删除/未修改”及原始入口备份描述均属于初次审查时的历史状态。

日期：2026-10-16。范围：用户个人安装的 14 种既有技能、本次新增的 3 种技能，以及与之相关的内置同名项。插件自带技能未逐个重新审查。没有停用、删除或改写既有技能；本轮修改仅是新增安装，以及新装 UI UX Pro Max 的路径兼容处理。

## 安装结果

均使用 Codex skill-installer，从公开源的固定提交下载到 `C:/Users/herol/.codex/skills/`，没有运行各项目的整包安装器。

| 技能 | 固定提交 | 安装与验证 |
|---|---|---|
| web-design-guidelines | `063bee94c3f4df8453406c830b0a7df0f2860278` | Vercel 官方目录，仅 SKILL.md；审查时读取其指定的在线界面规范 |
| impeccable | `6af45ac9a5eb8792f033d3242f52e99812fd91fa` | 技能 4.6.0，含完整 reference、scripts、agents；预置官方 Windows x64 引擎 0.1.15，校验 SHA256 后通过 Windows `.cmd` 启动器执行 `engine-probe` 与 `--help`，退出码均为 0 |
| ui-ux-pro-max | `56ea9cf174ed86786187daa9de0e6247cb0747e4` | 单独安装目标技能及其真实 scripts/data/references 文件；11 处 Claude 插件路径替换为实际安装目录。UX 与 Vue 两项本地搜索均通过，无需 pip 装依赖 |

既有 frontend-design 保留。三个新增技能约 24.3 MiB，其中绝大部分是 Impeccable 引擎。全局 hooks 文件的 SHA256 与安装前一致；没有注册项目 hooks，没有运行 `install`、`init`、`hooks on` 或 `live`。Impeccable 验收使用进程级 `IMPECCABLE_NO_UPDATE_CHECK=1`，没有改写用户环境变量。

Impeccable 默认开启匿名选择统计。确认其用户配置原本不存在后，作为新安装设置执行 `telemetry off` 并以 `telemetry status` 和文件读取核验；`C:/Users/herol/.impeccable/config.json` 中现为 `telemetry: false`。关闭统计不等于离线：`roll` 仍会联网获取设计候选，`context` 仍可检查版本；所查候选请求字段包含候选范围、随机 key、模式与平台，未见源文件内容或项目名字段。没有运行这些联网候选功能，也没有把项目文件发送给它。该结论来自当前客户端源码检查，不能代替远端服务审计。

本地检测在本机运行；网页检测会访问目标页面及资源。可选 `generate-image` 会将提示词和所选参考图发送到 OpenAI 图片接口并可能使用 API 额度，安装验证未调用此功能。见 [遥测实现](https://github.com/pbakaus/impeccable/blob/6af45ac9a5eb8792f033d3242f52e99812fd91fa/crates/context/src/telemetry.rs)、[版本检查](https://github.com/pbakaus/impeccable/blob/6af45ac9a5eb8792f033d3242f52e99812fd91fa/crates/context/src/context_cli.rs) 与 [图片接口](https://github.com/pbakaus/impeccable/blob/6af45ac9a5eb8792f033d3242f52e99812fd91fa/crates/context/src/generate_image.rs)。

Impeccable 引擎来源：`https://github.com/pbakaus/impeccable/releases/download/engine-v0.1.15/impeccable-windows-x64.exe`。大小 19,672,464 字节，SHA256 `981324ea15cc3c107d46b8133b5be77f639dbcd22e671f1eb7d8c69136e9693b`，与官方发布资产一致。

安装清单和验证输出保存在 `data/diagnostics/skills-audit-20261016/`：`installation-manifest.json`、`impeccable-probe.txt`、`impeccable-help.txt`、`uiux-ux-check.json`、`uiux-vue-check.json`。UIUX 原始入口备份为 `ui-ux-pro-max.original.SKILL.md`。

## 建议优先处理的既有项

| 项目 | 证据与影响 | 建议 |
|---|---|---|
| discernment-nudge | [描述](C:/Users/herol/.agents/skills/discernment-nudge/SKILL.md:4) 覆盖大多数实质建议、分析与推理；要求追加 2–3 个反思追问、固定英文引导语。每个对话最多一次，且代码、审查等有豁免，不能误说成每条回复都追问 | 关闭自动触发或停用；需要反思辅导时再手动使用，比较符合当前用户希望少打断的工作方式 |
| 用户版 skill-creator | 与内置版同名但内容不同。[评估脚本](C:/Users/herol/.agents/skills/skill-creator/scripts/run_eval.py:22) 调用 Claude CLI 并写 `.claude/commands/`；[管道读取](C:/Users/herol/.agents/skills/skill-creator/scripts/run_eval.py:108) 存在 Windows 兼容风险，不能把 Claude 评估结果等同 Codex 的效果 | 优先保留 Codex 内置版；外来版需要时改为独立名称并适配运行环境 |
| 两份 frontend-design | `.agents/skills/frontend-design` 与 `.codex/skills/frontend-design` 是独立实体目录；SKILL.md 与 LICENSE.txt 分别逐字一致，并非符号链接 | 保留一个可见入口，避免重复选择与后续更新分叉；本轮未删除任何一份 |
| improve-codebase-architecture | [正文](C:/Users/herol/.agents/skills/improve-codebase-architecture/SKILL.md:13) 引用未安装的 codebase-design、grilling、domain-modeling；还固定期待 GLOSSARY.md、docs/adr/，与 DubForge 的 docs/decisions/ 不符。现有 metadata 已是手动调用 | 当前不能当作完整即用的架构流程。补齐/移除依赖并适配仓库后再用；修正上次对其可直接复用的判断 |
| web-artifacts-builder | [入口](C:/Users/herol/.agents/skills/web-artifacts-builder/SKILL.md:9) 面向 Claude 独立 React/Tailwind/shadcn artifact，不适合直接重构现有 Vue 应用；[脚本](C:/Users/herol/.agents/skills/web-artifacts-builder/scripts/bundle-artifact.sh:36) 会清理当前目录的 dist、bundle.html；[正文](C:/Users/herol/.agents/skills/web-artifacts-builder/SKILL.md:66) 建议先交付再测试 | 不建议作为 DubForge 的通用前端技能；保留作隔离目录中的专项原型工具即可 |
| webapp-testing | [服务器辅助器](C:/Users/herol/.agents/skills/webapp-testing/scripts/with_server.py:68) 把输出设为 PIPE 却不读取，可能堵塞；就绪检查只连端口，可能误认已有服务；结束只杀 shell，后代服务可能残留 | 测试思路可参考，辅助器修复前不直接用于生产常驻服务；当前 Playwright/computer-use 能覆盖主要需求 |

## 其余既有技能

| 技能 | 用途与边界 |
|---|---|
| frontend-design | 保留主要版本，用于视觉方向、层次和文案；服从项目现有技术栈与用户要求 |
| find-skills | 保留，主要用于明确找、比较、安装技能；安装量和星数只是发现线索，不能代替源内容审查 |
| mermaid-diagrams | 保留，适合确实需要解释流程与架构的场景；不机械执行其“所有新功能/重构都画图”的宽泛要求 |
| playwright | 可保留。现有封装依赖 Bash 和 npx，Windows 使用时需选择可用入口；浏览器操作仍遵从当前环境提供的工具要求 |
| mcp-builder | MCP 设计说明有用；附带评估器调用 Anthropic API，会发送题目和工具返回内容并产生费用。工具循环缺调用预算、只处理首个 tool_use，且没有代码层只读工具白名单；只在隔离测试服务中使用，不能当作通用 Codex 评估器 |
| algorithmic-art | p5.js 生成艺术专项工具，模板固定部分视觉与界面布局；依赖 CDN，不能把其 HTML 视为完全离线自包含 |
| canvas-design | 海报与静态视觉专项工具，输出偏 PNG/PDF；附带设计哲学文档和多轮打磨要求，不适合普通产品 UI 修改 |
| theme-factory | 十套字体/配色参考，按需即可；其展示主题并等待选择的流程可能给简单任务增加交互 |
| remotion-best-practices | 只在 Remotion 项目或用户选择该路线时使用，避免把 DubForge 现有 FFmpeg 制作流程改成新建 Remotion；入口与所查 130 个本地文档链接齐全 |

## 来源与审查边界

六类 Anthropic 技能的 15 个入口/关键脚本/模板文件，以及另查的 frontend-design、discernment-nudge、skill-creator 入口，与 `anthropics/skills@dbd4588f9e1033efb41dad4bef2f7947c8993d44` 的公开文件一致。上述脚本问题是所查官方版本自带的限制，未发现这些样本被本地篡改的证据。没有上传本地内容；字体、全部参考资料和依赖包未做完整供应链审计。静态检查不等同运行安全认证。

`.codex/skills` 中大部分既有目录是指向 `.agents/skills` 的符号链接，属于同一份内容的入口，不能当作重复包误删。真正的重复内容是 frontend-design，真正的同名不同实现是用户版与内置版 skill-creator。

建议按任务选择技能：frontend-design 负责设计方向，Vercel 负责交付前审查；复杂交互专项使用 Impeccable，检索资料时使用 UI UX Pro Max。安装全部技能不意味着每次启动全部工作流。

官方说明指出，初始上下文主要载入技能名称与描述，选中后才读取全文；同名技能不会合并，可能同时出现在选择器。支持在不删除文件的情况下禁用技能，也支持通过 `agents/openai.yaml` 关闭隐式调用。见 [OpenAI 技能文档](https://learn.chatgpt.com/docs/build-skills)。本轮没有修改这些策略。

## 直接来源

- [Vercel 技能](https://github.com/vercel-labs/agent-skills/tree/063bee94c3f4df8453406c830b0a7df0f2860278/skills/web-design-guidelines)
- [Impeccable 技能](https://github.com/pbakaus/impeccable/tree/6af45ac9a5eb8792f033d3242f52e99812fd91fa/.agents/skills/impeccable)
- [UI UX Pro Max 技能](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/tree/56ea9cf174ed86786187daa9de0e6247cb0747e4/.claude/skills/ui-ux-pro-max)
- [Anthropic 被比对版本](https://github.com/anthropics/skills/tree/dbd4588f9e1033efb41dad4bef2f7947c8993d44/skills)
