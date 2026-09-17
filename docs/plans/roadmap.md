# 开发路线图

> 每里程碑结束：可运行 + pytest 绿 + 已推送。完成即勾选并附实现笔记链接。

## M0 脚手架 ✅ 2026-09-17
- [x] monorepo 结构（backend/ frontend/ scripts/ docs/ data/）
- [x] uv + pyproject（Python 3.13）+ FastAPI 骨架 + SQLModel 表结构（Project/StepRun/Artifact/Job）
- [x] config.py 三处同步机制（DEFAULTS/config.yaml/config.example.yaml）+ 密钥隔离
- [x] errors.py 统一错误体系（code + 中文 message + suggestion）
- [x] pipeline/registry.py 九步骤定义（UI 常驻渲染的依据）
- [x] Vue3 + Naive UI 暗色中文壳（项目/工作台/设置/关于 四路由）
- [x] 设置页最小可用（读配置 + 端口写回，验证前后端配置环）
- [x] pytest 冒烟（夹具隔离 DATA_ROOT/CONFIG）
- [x] scripts/dev.ps1 + GitHub 建仓

## M1 项目域
- [ ] Project CRUD API + 项目目录自举（data/projects/<uid>/）
- [ ] 步骤执行器：单步启动/取消/心跳；重跑语义（清产物+下游 stale）
- [ ] 输入指纹（配置键 + 上游产物哈希 → sha256）
- [ ] 崩溃恢复：启动扫描 running→interrupted；dead 判定（心跳超时）
- [ ] 日志：每步日志文件 + API 尾读
- [ ] pytest：执行器状态机全路径 + 恢复测试（kill 模拟）

## M2 管线前半（步骤 1–6）
- [ ] download：yt-dlp 库 + archive 去重 + cookies/代理 + 本地导入
- [ ] preprocess：音轨 16k 提取 + demucs 人声/背景 + 可选 pyannote 说话人
- [ ] transcribe：faster-whisper 词级时间戳 → 源 SRT
- [ ] split：NLP 粗切 + LLM 语义切句
- [ ] translate：三档策略 + 术语表 + 风格提示词 + gpt_log 式续传缓存
- [ ] subtitles：源/译/双语 SRT + 行宽控制
- [ ] pytest：全部步骤用合成媒体夹具跑通

## M3 管线后半（步骤 7–9）
- [ ] dubtask：时长估算（沿用 estimate_duration 思路）+ 语速容忍 + 合并切分
- [ ] tts：IndexTTS2.5 后端（双维度参考/预设同步/句级缓存）+ GPT-SoVITS 后端
- [ ] 精修 API：逐句合成/采纳/预览素材流
- [ ] merge_audio：拼接 + 响度归一
- [ ] assemble：背景混音 + ducking + 可选烧录 + 时间戳归档
- [ ] pytest：夹具级 TTS mock + 成片参数矩阵

## M4 前端应用
- [ ] 新建项目（粘贴链接/本地导入）+ 项目列表（状态卡）
- [ ] 管线页：九步骤常驻卡 + 实时进度/日志 + 单步重跑（含 stale 提示）
- [ ] 完整设置页（分组表单 + 连通性测试按钮）
- [ ] naive-ui 按需引入瘦身

## M5 精修工作台
- [ ] Vue 迁移：逐句列表/合成/试听（WebAudio 混音）/画面定位/采纳
- [ ] 参数面板与设置页同源；预设同步与差异标注
- [ ] 键盘导航 j/k；移动端布局

## M6 打磨验收
- [ ] 错误中心 + 引导文案全量中文化
- [ ] 用户手册 docs/manual.md
- [ ] 端到端验收：真实视频全流程 + 40 分钟长视频 + 中途 kill 恢复测试
