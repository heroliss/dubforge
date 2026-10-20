# 版本发布

首次稳定版使用 `v1.0.0` 标签，按**源码发行**准备：仓库源码、文档和依赖锁文件是交付内容，前端需要构建，模型与配音服务按安装指南另行配置。不要把 GitHub 自动生成的源码压缩包称为含全部依赖的安装包或独立 EXE。

仓库当前保持 private。创建 tag 或 GitHub Release 不包含变更公开性的授权；开源许可证尚未指定，也不在发布时自行增加新的授权条款。第三方软件与模型的归属及条件按其项目和仓库说明保留。

## 1. 确认发布范围

- 对照 [CHANGELOG](../CHANGELOG.md) 核对能力、修复及已知限制；以真实验证过的 Windows 环境描述支持范围。
- 检查版本字段、健康响应和界面版本是否一致，由发布检查脚本复核。新版本要保留已有项目、参数、产物及原稿回执的兼容边界。
- 清理临时调试入口与明显重复内容；保留有用的历史 ADR、研究记录与第三方归属，不把删除历史文档当作程序瘦身。
- 截图应来自当前界面，隐去账号、二维码、密钥、私有来源和用户工作目录。演示夹具内部路径可保留，但不能包含私人身份信息；截图明确标注为演示，不冒充真实模型或平台交付。[截图工具说明](../scripts/docs/README.md)记录隔离数据、原图、坐标与 WebP 复现方式。

## 2. 完整检查

在隔离配置/数据环境中执行，具体安装与运行纪律见[开发与维护](development.md)。全量后端测试需安装识别和分离可选依赖；默认安装不等于完整测试环境已齐备。

```powershell
uv sync --locked --directory backend --extra asr --extra separation
uv run --locked --directory backend --extra asr --extra separation pytest

Push-Location frontend
npm ci
npm test
npm run typecheck
npm run build
Pop-Location

Push-Location workers/bilibili
npm ci --ignore-scripts
npm test
Pop-Location

# 只核对版本与文档，不启动产品服务
uv run --directory backend --no-sync python ../scripts/check_release.py
# 测试与构建确已完成后，可只补隔离的真实健康检查
.\scripts\smoke.ps1 -SkipTests -SkipFrontendBuild
```

`smoke.ps1` 总是使用新建临时配置、数据、模型缓存和 TEMP，并固定在 8611；占用时拒绝启动，不终止现有服务。健康响应必须匹配源码版本，隔离项目列表必须为空，并核验监听确属本次子进程。默认还会跑完整 pytest，并将前端构建输出到临时目录，不替换原 `frontend/dist/`。`-SkipTests` / `-SkipFrontendBuild` 只复用已有检查，不能把被跳过的内容记成本次通过；失败日志保留在命令输出的临时目录。

[`ci.yml`](../.github/workflows/ci.yml) 在 Windows runner 上执行 Python 3.13、Node 24、锁定依赖、三个测试集合、前端类型与构建、发布文档检查和隔离健康检查。Action 固定提交 SHA，工作流仅有 `contents: read`，不自动创建版本、不上传稿件、不修改仓库设置。更新 Action 时从其官方仓库核对稳定 tag 对应的提交，不粘贴未经验证的 SHA。

自动检查不能替代真实界面、媒体和外部服务验收。记录每个检查的实际范围、命令、退出码与限制；聚焦回归不能当成全仓通过，已上传也不能当成平台公开成功。外部平台内容审核退回与软件功能验收分别说明，不为凑通过重投或更改平台资料。

## 3. 检查暂存内容与凭据

维护者先检查 diff 和待发布文件，确认没有 `config.yaml`、运行数据库、Cookie、登录凭据、个人日志、模型权重、私有音频或未脱敏截图。不要仅依赖 `.gitignore` 判断历史或暂存内容安全。

```powershell
# 先正常暂存本次有意发布的文件，再运行
uv run --directory backend --no-sync python ../scripts/check_release.py --staged
```

仅检查文档链接时可用 `--docs-only`；该选项不能代替正式发布的版本与暂存内容检查。真实配置不用于生成示例或展示完整诊断输出。

## 4. 提交、标签与 Release

以下由有仓库权限的维护者，在检查完成、提交内容可审阅后执行；不要在 CI 中加入自动发布 token 或扩大写权限。

1. 按 conventional 风格提交并推送经核验的修改，确认远端 CI 的实际结果。当前无法执行或未完成的检查需明确说明，不能记成成功。
2. 对准备发行的明确提交创建带说明的 `v1.0.0` tag，并推送该 tag；不要复用或强制移动已有稳定版标签。
3. 用 GitHub 页面或 CLI 创建 Release，先核对 tag 指向、变更摘要、安装方式、截图标注及已知限制，再发布。

```powershell
# 示例仅适用于首次稳定版已完成核验的提交
git tag -a v1.0.0 -m "DubForge v1.0.0"
git push origin v1.0.0
gh release create v1.0.0 --verify-tag --draft --title "DubForge v1.0.0" --notes-file CHANGELOG.md
```

Release 说明应包含源码安装入口、变更和验证范围，以及不包含模型/独立安装器的事实。若没有另外制作并验收的二进制产物，就不添加相应下载承诺。发布后只读核对版本、tag/commit、文件与 Release 状态；私有仓库仍按其原有访问权限访问。

后续版本发现问题时保留已发布 tag，使用修订版本与说明；不要静默替换版本标签或掩盖失败验收记录。回滚程序与恢复数据备份的边界见[开发与维护](development.md#备份升级与迁移)。
