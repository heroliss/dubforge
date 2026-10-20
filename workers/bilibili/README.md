# B站通信组件

仅负责 B站登录、上传、投稿、合集和状态通信，不负责视频制作或决定发布范围。创作管理中的 `studio_chapters` 只读播放器 `view_points`，用于读取已有视频章节；`studio_chapter_rules`、`studio_chapter_read`、`studio_chapter_prepare` 与 `studio_chapter_write` 对接官方章节编辑协议。写操作只在用户明确确认、规则和快照未变化时发送一次 POST，并在发送后回读 `allcards` 验证；超时结果标为未知且绝不自动重试。章节按分P CID 管理。

安装 Node.js LTS，在本目录执行：

```powershell
npm ci --ignore-scripts
npm test
npm audit --omit=dev
```

或在仓库根目录执行 `./scripts/install.ps1 -Publishing -SkipFrontend`。运行阶段也需要 Node.js，不能仅在构建时安装它。

当前固定 `@renmu/bili-api@2.15.0`。最新的 2.15.2 包含自 2.15.1 引入的部分上传请求关闭证书校验改动，因此未直接升级。不能通过设置 `NODE_TLS_REJECT_UNAUTHORIZED=0` 解决连接问题。锁文件覆盖 `fast-xml-parser@5.7.0`，升级后需重新审计和回归。

工作进程只接受 stdin 的一条 JSON 命令，stdout 是受控 JSONL；不要在终端手工传入、打印或保存登录凭据。`probe` 命令不请求网络。其他命令由后端调度，不建议手工使用。

已有稿件的 `studio_write` 支持保留原 BV 编辑标题、简介、标签、动态、创作声明、分区和转载来源，以及使用已上传的 B站 HTTPS 封面。版权与来源、声明必须组成有效组合，具体是否接受仍取决于平台规则和账号权限。精选评论、关闭评论、关闭弹幕等参数没有可靠的编辑契约，不作为已同步字段；私密、联合、付费等特殊稿件继续要求到官网操作。

分P更新以冻结的完整清单为准：原CID用于定位替换、改名与排序；`old_cid=0` 仅表示本次追加，新CID必须来自上传回执。顺序必须包含每个原P各一次，再加全部本次追加P，禁止隐式删P，最多100个P。重排时原P说明按CID保留。工作进程重新构建并核对完整清单后调用 SDK `replace`，不依赖 SDK 对遗漏排序项的隐式追加行为。即使只编辑文案也必须真实回读资料、分P与封面后才返回 `verified:true`；接受后暂未生效、资料不一致或回读失败一律标为结果未知，不自动重复写入。协议测试使用真实固定版本 SDK 与本地模拟响应，不代表真实账号端到端验收。

上游项目为 [renmu123/biliAPI](https://github.com/renmu123/biliAPI)，许可证见其 GPL LICENSE。对外分发前核实和遵守相关义务。声明映射依据同一作者的 [PR #454](https://github.com/renmu123/biliLive-tools/pull/454/files)，缺少平台枚举时使用该版本化映射，真实账号行为仍待联调。

详细状态、恢复、隐私和已知限制见 [ADR-0041](../../docs/decisions/0041-bilibili-publication-boundary.md)。
