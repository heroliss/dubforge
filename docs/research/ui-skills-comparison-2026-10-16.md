# DubForge 的 UI 与工程技能选择

日期：2026-10-16。依据各项目当前官方源码，不以安装量或仓库星数代替质量判断。

后续更新：用户已授权并完成 Vercel、Impeccable、UI UX Pro Max 安装。兼容适配、运行验证和既有技能审查见 [个人技能安装与审查](skills-installation-audit-2026-10-16.md)。下文比较保留初次研究时的取舍记录。

## 推荐组合

Anthropic frontend-design 负责设计方向与层次；Vue 指南按需辅助工程实现；Vercel web-design-guidelines 用于交付前审查；最后用真实浏览器验证。工程结构继续使用已有 improve-codebase-architecture，重点检查领域模型、状态归属、异步边界与恢复机制。技能不能替代测试与实际产物验收。

| 技能 | 实际增量 | DubForge 中的用途与边界 |
|---|---|---|
| Anthropic frontend-design | 视觉方向、排版、文案、层次，以及设计后自我审查 | 已安装。沿用 Vue3、Naive UI 与现有主题，优先强化视频制作的操作次序及状态表达 |
| Vercel web-design-guidelines | 表单、键盘、焦点、可访问性、异步反馈与性能检查 | 最值得补充的轻量审查；通用规则可用，React 示例按 Vue 实现转换。本轮仅研究，未安装 |
| Impeccable | 工作命令、工具界面原则、审查及确定性检测 | 与视觉设计规则重叠，但流程梳理有价值。当前版本包含 launcher、可下载执行引擎和 hooks，并非只有 Markdown；暂不加入常驻基线 |
| UI UX Pro Max | 本地 Python 检索配色、字体、风格、UX 和 Vue 栈资料 | 适合新设计系统或选型查阅，不负责后端架构。默认 CLI 安装范围包含多个同级技能；暂不整包安装 |
| antfu/skills 的 vue | Vue 响应式、watch、生命周期、组件与 TypeScript 参考 | 可按需补充工程正确性；部分是作者偏好，不是 Vue 的强制规范，也不专门针对 Naive UI。本轮未安装 |

## 已安装内容

位置：`C:/Users/herol/.codex/skills/frontend-design`。

来源：`anthropics/skills`，路径 `skills/frontend-design`，固定提交 `dbd4588f9e1033efb41dad4bef2f7947c8993d44`。仅安装 `SKILL.md` 与 `LICENSE.txt`，没有附带脚本或 hooks。下一条消息起可作为已安装技能调用；本轮已阅读其规则用于审查界面。

## 直接来源

- [Anthropic frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design)
- [Vercel 技能定义](https://github.com/vercel-labs/agent-skills/blob/main/skills/web-design-guidelines/SKILL.md) 与 [实际界面规则](https://github.com/vercel-labs/web-interface-guidelines/blob/main/command.md)
- [Impeccable 技能](https://github.com/pbakaus/impeccable/blob/main/.agents/skills/impeccable/SKILL.md)、[工具界面原则](https://github.com/pbakaus/impeccable/blob/main/.agents/skills/impeccable/reference/operate.md)、[安装机制](https://github.com/pbakaus/impeccable#installation) 与 [hooks](https://github.com/pbakaus/impeccable/blob/main/.codex/hooks.json)
- [UI UX Pro Max 技能](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/.claude/skills/ui-ux-pro-max/SKILL.md) 与 [默认安装实现](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/cli/src/utils/template.ts)
- [antfu Vue 技能](https://github.com/antfu/skills/blob/main/skills/vue/SKILL.md)
- [已有架构技能来源](https://github.com/mattpocock/skills)
