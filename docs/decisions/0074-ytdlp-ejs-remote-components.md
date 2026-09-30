# ADR-0074：yt-dlp 启用 EJS 挑战求解组件

日期：2026-09-29。状态：采用。

## 背景

yt-dlp 2026.08 起，YouTube 的 n-challenge 求解需要 JS 运行时（deno，装机前置已满足）加挑战求解脚本（EJS），且远程组件改为**显式启用**（供应链安全考量，不再自动下载）。未启用时日志出现 `[jsc] Remote components ... skipped` 警告：部分格式缺失，选格式退化，googlevideo 下载连接易被限流掐断（表现为 `SSL: UNEXPECTED_EOF_WHILE_READING`，重试 5 次后失败）。

## 决策

`step_download._ytdlp_cmd` 固定追加 `--remote-components ejs:github`（官方推荐值）：求解脚本由 yt-dlp 从 GitHub 拉取并校验，走下载配置中的同一代理；组件失败仅退化为警告，不阻塞下载。不在配置面新增开关——该参数是 yt-dlp 官方推荐默认，环境差异（无 deno）时行为与现状一致。

同时为下载错误映射补充「连接被中断」分支（SSL/EOF/connection reset）：提示更换代理节点，与「无法连接代理」区分。

## 验证

命令参数用 `parse_options` 解析真实安装版本断言（`remote_components == ['ejs:github']`）；对实际失败的视频 (vPoYu-uHQPg) 做提取验证：启用后格式列表完整（含高画质）、警告消失。错误映射参数化用例覆盖 SSL EOF 真实日志文本。
