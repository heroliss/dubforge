# 共享技能的 Codex 适配与 CC Switch 整理

日期：2026-10-16。用户授权执行先前审查建议、不保留旧版备份；以 Codex 为主，同时保留其他代理共享能力和 CC Switch 的预览、管理入口。

## 最终组织方式

- 个人技能实体统一放在 `C:/Users/herol/.agents/skills/`。Codex 目录只保留对应链接；内置 `.system` 原样保留。
- 保留 16 项个人技能。删除 Claude 专用的用户版 `skill-creator`，使用 Codex 内置同名技能；两份相同的 `frontend-design` 合并为一个实体。
- 原来仅在 Codex 目录的三个新 UI 技能及 Playwright 迁入共享目录，补登记 Remotion。CC Switch 中每项仅一条记录，Codex 全部启用。
- 已修改项登记为本地适配版，保存当前内容 hash，避免 CC Switch 按旧上游记录覆盖适配。四个由旧 skills CLI 跟踪的现存技能解除旧更新登记，由 CC Switch 管理；其他历史登记和客户端开关保留。
- `discernment-nudge` 改为手动调用；`improve-codebase-architecture` 保持原手动调用策略。需要时通过 `$技能名` 或显式选择使用。
- 共享模式存在 CC Switch 本身的边界：Codex 会直接读取 `.agents/skills`，CC Switch 的客户端开关主要控制投影链接，不能保证阻止共享实体被发现。两个手动技能使用 Codex 自身的 `allow_implicit_invocation: false` 控制；若需要完全停用某项，应在 Codex 自身技能配置中处理，而不是只删除链接。该边界已由 [CC Switch 官方手册](https://github.com/farion1231/cc-switch/blob/5de6feb4e5adda61f4ad824ce0df2c449245f447/docs/user-manual/zh/3-extensions/3.3-skills.md#L202) 明确说明。本次全部启用，不受影响。
- 没有保留旧版技能副本；删除上一轮产生的 UIUX 原始入口备份。测试、hash、结果记录不包含旧代码备份。

## 修复内容

| 范围 | 结果 |
|---|---|
| 架构审查 | 移除未安装技能依赖，按仓库实际文档及 ADR 约定分析，不再强制额外 HTML/术语表或连续追问 |
| 技能发现、反思辅导、Mermaid、Remotion | 收窄触发范围，减少无关任务介入，不将 FFmpeg 项目改成 Remotion；普通回答不再自动追加反思问题 |
| frontend-design / Impeccable / UIUX | 优先现有产品、技术栈和设计令牌；小改不重建设计系统；复杂概念选择、额外文档和外部设计调用改为按任务需要的可选流程 |
| Vercel 审查 | 使用当前宿主的网络能力，不要求名为 WebFetch 的工具；从项目确定文件范围，按宿主审查格式输出 |
| 算法艺术、静态视觉、主题 | 保留参考资料、字体和主题，取消固定品牌、额外哲学长文及不必要的等待；修复调色数组重置问题 |
| Web 测试服务辅助器 | 日志不再使用无人读取的 PIPE；拒绝已占端口；清理自身进程树，覆盖启动失败及超时场景 |
| 独立 Web 原型 | 限定新建隔离原型，防止写入现有 Vue 应用；移除全局 pnpm 安装和清空 dist 的行为，输出写独占目录并先验证再交付 |
| MCP 示例评估器 | 明确属于可选 Anthropic 适配器；默认不读密钥、不访问模型；增加工具允许名单与预算，正确处理一轮内全部工具请求 |
| Playwright Windows 入口 | 补充 Windows 入口；验证并修复 PowerShell 编码与参数引号转发问题；保留现有浏览器工具优先和隔离测试原则 |

Impeccable 保留此前安装的 Windows 引擎及校验结果，统计仍关闭，没有初始化项目 hooks。其可选在线设计与图像功能仍遵从任务范围，并不因安装而自动执行。

## 验证与边界

验证记录位于 `data/diagnostics/skills-codex-adaptation-20261016/`、`data/diagnostics/skill-fixes-20261016/` 和 `D:/Temp/codex-skill-repair-20261016/`。

- 三个辅助器的隔离回归：27 项通过。Windows 进程清理真实运行；POSIX 分支在本机做模拟验证。Git Bash 执行使用假 npm/pnpm，没有下载或安装包。
- 八个共享技能的入口、本地引用及算法模板检查通过；独立任务路由审查未发现实质矛盾。
- 最终 16 项入口、共享链接、手动策略验证全部通过；内置 creator 保留，全局 hooks hash 未变。见诊断目录内 `validation.json`。
- Windows Playwright 在 PowerShell 7 与 Windows PowerShell 5.1 的离线假 npm 矩阵共 **32/32 通过**：含引号、中文、空格、空参数、换行、shell 元字符、session 优先级、真实退出码、缺失依赖及调用方原生命令异常偏好。没有运行真实 npm 或启动浏览器。
- 迁移后再次运行 Impeccable `engine-probe` 成功，输出 0.1.15；引擎 SHA256 与官方发布资产一致。UIUX 的 UX 和 Vue 两项本地查询成功。
- CC Switch 正常退出、维护后重开，实际显示 **已安装 16、Codex 16/16**，没有未纳管技能横幅；已显示更新后的本地适配描述。列表截图见 `data/diagnostics/skills-codex-adaptation-20261016/cc-switch-skills.png`，对应可访问性记录为同目录 `.txt`。

CC Switch 4.0.14 的已安装列表没有本地全文预览：技能标题本身不响应点击，菜单中的“打开文档”属于外部链接，不能证明它显示的是本机适配内容。本次未将上游文档冒充本地修订版预览。当前全文以共享目录各项 `SKILL.md` 为准。见 [已安装界面实现](https://github.com/farion1231/cc-switch/blob/5de6feb4e5adda61f4ad824ce0df2c449245f447/src/components/skills/UnifiedSkillsPanel.tsx#L1950)。

该外部链接入口只允许 HTTP/HTTPS，不接受本机 `file://` 链接，因此没有填入无效链接来假装支持本地预览。[URL 校验实现](https://github.com/farion1231/cc-switch/blob/5de6feb4e5adda61f4ad824ce0df2c449245f447/src/lib/api/settings.ts#L243)。已完成列表、描述、计数与登记核验，保留此版本功能边界。

没有进行完整创作流程或付费模型评估，也没有把脚本模拟验证说成 Linux 实机验收。CC Switch 管理界面验收与 Codex 新会话技能加载是不同环节；当前对话已载入的技能描述不会被文件修改追溯替换。

## 来源

上游基线、固定提交和初次安装验证见 [最初安装审查](skills-installation-audit-2026-10-16.md)。该报告描述整改前的状态；整改后的本地文件不再与上游逐字一致。

CC Switch 4.0.14 的登记结构与内容 hash 对照其 [技能服务实现](https://github.com/farion1231/cc-switch/blob/5de6feb4e5adda61f4ad824ce0df2c449245f447/src-tauri/src/services/skill.rs) 与 [技能 DAO](https://github.com/farion1231/cc-switch/blob/5de6feb4e5adda61f4ad824ce0df2c449245f447/src-tauri/src/database/dao/skills.rs)。仅在正常退出应用后用 SQLite 单事务修改 skills 表，随后重开应用核验。未修改服务商、凭据或数据库其他业务表。
