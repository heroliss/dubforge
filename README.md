# DubForge

⚒️ **视频翻译配音工作台** —— 在 VideoLingo 领域经验之上全新设计的一站式流水线：
下载 → 预处理(人声/背景/说话人分离) → 转录 → 分句 → 翻译 → 字幕 → 配音(IndexTTS2.5) → 逐句精修 → 成片。

## 设计原则

1. **步骤常驻可见、可单独重跑** —— 管线是显式步骤状态机，不再有"下一步之后按钮消失"
2. **中断恢复一等公民** —— 产物原子写入、步骤输入指纹、重启自动判中断、全部幂等
3. **领域库与界面解耦** —— `dubforge` 纯 Python 核心库；FastAPI 是壳；Vue 是另一层壳；TTS/ASR/翻译全插件化
4. **中文一等支持** —— UI 全中文、错误码→中文消息+修复建议、注释中文

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.13 (uv) + FastAPI + SQLModel/SQLite |
| 前端 | Vue 3 + Vite + Naive UI + Pinia（构建产物由 FastAPI 托管） |
| 外部服务 | IndexTTS2.5 (:7860) / GPT-SoVITS (:9880) / OpenAI 兼容 LLM (智谱 GLM 等) |

## 快速开始

**日常启动：双击根目录 `StartDev.bat`** —— 自动拉起前后端两个窗口、等端口就绪后打开浏览器，按回车即可全部停止。

首次使用需初始化依赖：

```powershell
# 后端 (自动下载 Python 3.13 并装依赖)
cd backend
uv sync

# 前端
cd frontend
npm install
```

手动方式（等价于 StartDev.bat）：

```powershell
cd backend
uv run uvicorn app.main:app --port 8600 --reload

# 前端 (另一终端)
cd frontend
npm run dev          # http://127.0.0.1:5273, /api 自动代理到 8600

# 生产形态
.\scripts\build.ps1          # 构建 dist
cd backend; uv run uvicorn app.main:app --port 8600   # 单进程托管 API + 页面
```

首次使用：复制 `config.example.yaml` → `config.yaml`，填入 LLM API key（该文件已 gitignore）。

## 冒烟自检

```powershell
.\scripts\smoke.ps1   # pytest + health 检查 + 前端构建, 三关全绿即健康
```

## 里程碑

M0 脚手架 ✅ → M1 项目域 → M2 管线前半（下载/分离/转录/分句/翻译/字幕）→ M3 管线后半（配音/成片）→ M4 前端应用 → M5 精修工作台迁入 → M6 打磨验收。详见 `docs/decisions/`。

## 测试

```powershell
cd backend
uv run pytest        # 含夹具隔离的冒烟测试; ML 重依赖在 M2 起按需引入
```
