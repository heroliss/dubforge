# B站通信组件

仅负责 B站登录、上传、投稿、合集和状态通信，不负责视频制作或决定发布范围。

安装 Node.js LTS，在本目录执行：

```powershell
npm ci --ignore-scripts
npm test
npm audit --omit=dev
```

或在仓库根目录执行 `./scripts/install.ps1 -Publishing -SkipFrontend`。运行阶段也需要 Node.js，不能仅在构建时安装它。

当前固定 `@renmu/bili-api@2.15.0`。最新的 2.15.2 包含自 2.15.1 引入的部分上传请求关闭证书校验改动，因此未直接升级。不能通过设置 `NODE_TLS_REJECT_UNAUTHORIZED=0` 解决连接问题。锁文件覆盖 `fast-xml-parser@5.7.0`，升级后需重新审计和回归。

工作进程只接受 stdin 的一条 JSON 命令，stdout 是受控 JSONL；不要在终端手工传入、打印或保存登录凭据。`probe` 命令不请求网络。其他命令由后端调度，不建议手工使用。

上游项目为 [renmu123/biliAPI](https://github.com/renmu123/biliAPI)，许可证见其 GPL LICENSE。对外分发前核实和遵守相关义务。声明映射依据同一作者的 [PR #454](https://github.com/renmu123/biliLive-tools/pull/454/files)，缺少平台枚举时使用该版本化映射，真实账号行为仍待联调。

详细状态、恢复、隐私和已知限制见 [ADR-0041](../../docs/decisions/0041-bilibili-publication-boundary.md)。
