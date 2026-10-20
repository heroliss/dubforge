# 第三方组件与许可

DubForge 独立开发，通过公开接口和工具连接下列组件。本页用于说明技术依赖和维护边界，不替代组件、模型或素材的许可证。

本仓库目前尚未指定开源许可证；第三方依赖分别遵循各自的许可证与使用条款。

| 组件 | 用途 | 上游 |
| --- | --- | --- |
| FastAPI、Uvicorn | HTTP 接口和本地服务 | [FastAPI](https://github.com/fastapi/fastapi)、[Uvicorn](https://github.com/encode/uvicorn) |
| SQLModel、SQLite | 项目、任务和发布记录 | [SQLModel](https://github.com/fastapi/sqlmodel)、[SQLite](https://www.sqlite.org/) |
| Vue、Pinia、Vue Router、Naive UI、Vite | 浏览器界面及构建 | [Vue](https://github.com/vuejs/core)、[Pinia](https://github.com/vuejs/pinia)、[Vue Router](https://github.com/vuejs/router)、[Naive UI](https://github.com/tusen-ai/naive-ui)、[Vite](https://github.com/vitejs/vite) |
| yt-dlp | 网络视频与元信息获取 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| FFmpeg / FFprobe | 音视频、字幕与合成 | [FFmpeg](https://ffmpeg.org/legal.html) |
| faster-whisper、Demucs、PyTorch | 可选识别与音轨分离 | [faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[Demucs](https://github.com/facebookresearch/demucs)、[PyTorch](https://github.com/pytorch/pytorch) |
| IndexTTS、GPT-SoVITS、Gradio Client | 外部语音合成服务及客户端 | [IndexTTS](https://github.com/index-tts/index-tts)、[GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)、[Gradio](https://github.com/gradio-app/gradio) |
| @renmu/bili-api | B 站 Node.js 工作者；上游许可证为 GPL-3.0 | [biliAPI](https://github.com/renmu123/biliAPI)、[GPL-3.0 原文](https://github.com/renmu123/biliAPI/blob/master/LICENSE) |
| Sharp | 仅文档截图的 SVG 标注合成 | [Sharp](https://github.com/lovell/sharp) |

## 发布包的范围

源码发行不附带上述组件的完整模型、FFmpeg 二进制、Python/Node.js 环境、参考声音、网络视频或账号凭据。安装时由包管理器解析锁定依赖；模型和外部语音服务单独安装。

不同模型权重、声音参考和视频素材可能有不同的授权要求。调用外部服务不会改变素材本身的使用条件。

## 文档图片

入门截图来自本仓库的隔离演示项目。演示台词和几何测试视频由文档夹具生成，不读取实际项目或真实账户。圈线、箭头和编号由本地 SVG 图层合成；原图、坐标与生成收据保留在 `docs/images/`，工具及[复现说明](scripts/docs/README.md)位于 `scripts/docs/`。

标注实现使用 Sharp 的公开合成接口。研究时参考了 [Sharp 文档](https://sharp.pixelplumbing.com/api-composite/)、[WordPress 的文档截图 skill](https://github.com/WordPress/wordpress-playground/tree/trunk/.agents/skills/doc-screenshots) 和 [image-annotator-mcp](https://github.com/vapvarun/image-annotator-mcp) 的工作流说明；未捆绑其服务，也未安装全局 skill。
