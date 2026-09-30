# DubForge 技术选型对比（2026-09-20）

这份表按 DubForge 的实际管线整理。这里的“最新”指截至 2026-09-20 能查到、仍有维护或有明确模型发布记录的方案；“更先进”不等于“更适合本机”。最终选择还要看显存、语言、授权、是否允许联网以及真实素材上的试听结果。

## 先给结论

1. **当前最值得升级的是音轨分离和 ASR 的可插拔性**：项目仍把 Demucs 当默认分离器，但官方仓库已归档；可保留 Demucs 作为稳定回退，同时增加 BS-RoFormer/UVR 和 Fun-ASR-Nano/SenseVoice 的试验适配器。
2. **TTS 不建议直接替换 IndexTTS2.5**：它对中文、情感、时长和参考音频控制仍很契合。更好的路线是新增 CosyVoice 3 作为多语言/方言候选，再用试配工作台对同一组句子做 A/B 测试。
3. **翻译暂时不宜换成单一模型**：LLM 仍最适合术语、风格和上下文；DeepL 适合作为高稳定性 API，NLLB 适合作为离线多语言回退，Qwen3 适合作为自托管/中文场景候选。
4. **真正需要补的不是“再换一个模型”，而是模型适配器、统一评测集和 A/B 试配记录**。没有同一素材、同一参数的对照，无法可靠判断升级是否变好。

## 分步骤对比

| 管线步骤 | 当前实现 | 可选技术（当前可用） | 优点 | 局限/代价 | 对 DubForge 的建议 |
|---|---|---|---|---|---|
| 获取视频 | yt-dlp 子进程 | 继续使用 yt-dlp；必要时增加站点插件/登录 Cookie 管理 | 站点覆盖广，下载、格式选择和字幕抽取成熟 | 站点规则会变化，部分站点需要 Cookie 或代理 | **保留**。把版本、失败原因和代理状态展示得更清楚即可 |
| 音轨提取 | FFmpeg 16kHz mono + 44.1kHz stereo | FFmpeg 继续作为基础层；可增加响度/采样率检查 | 稳定、跨平台、速度快，不需要模型 | 不是 AI，不能解决混音污染 | **保留**，并在分离前记录输入采样率、声道和响度 |
| 人声/背景分离 | Demucs `htdemucs` / `htdemucs_ft` | BS-RoFormer、Mel-RoFormer、UVR ensemble、Demucs 回退 | RoFormer 系列在音乐源分离上通常有更高上限；UVR 方便组合多模型 | 显存和耗时增加；不同素材最优模型不同；Demucs 官方仓库已归档 | **优先增加适配器**：默认保留 Demucs，新增 RoFormer 高质量档和“速度/质量”预设 |
| 说话人分离（预留） | 当前未启用 | `pyannote.audio 4.x community-1`；云端 Precision-2 | 能输出“谁在什么时候说话”，适合多人视频和按说话人选参考音色 | 模型较重；需要模型下载/授权；错误会影响分段 | 作为**可选步骤**，不要默认开启；多人视频再启用 |
| 语音转文字 | faster-whisper + `large-v3-turbo`，词级时间戳 + VAD | WhisperX；Fun-ASR-Nano / Fun-ASR-MLT-Nano；SenseVoice；NVIDIA Parakeet TDT v3 | WhisperX 可做更精确词级对齐和说话人标注；Fun-ASR 对中文方言/口音有优势；SenseVoice 额外输出情感和音频事件；Parakeet 适合高吞吐欧洲语言 | WhisperX 会增加对齐和 diarization 依赖；Fun-ASR/SenseVoice 语言覆盖不同；Parakeet 不覆盖中文 | **默认保留 large-v3-turbo**；新增“中文/方言”档 Fun-ASR-Nano；需要精确字幕时增加 WhisperX 对齐后处理 |
| 语义整理 | 词级时间锚点 + token 分窗 + LLM 结构化重组 | WhisperX 对齐后切句；LLM 结构化切句；基于 VAD 的停顿切分 | 词级时间信息完整，模型可在全局上下文中合并/拆分；本地硬约束保证可用 | 词级输入更长；窗口边界需要上下文衔接；LLM 结果仍需校验 | **采用词级锚点方案**，保留本地回退与最终长度/时长约束 |
| 翻译 | OpenAI 兼容 LLM，token 窗口整体翻译、串行交接摘要、术语表与风格提示 | Qwen3 自托管；DeepL API；NLLB-200 离线；SeamlessM4T v2 端到端语音/文本 | LLM 最灵活；Qwen3 多语言和中文场景强；DeepL 稳定且有上下文参数；NLLB 覆盖 200+ 语言；SeamlessM4T 可做语音到语音 | API 成本/隐私；自托管需要显存；NLLB 风格控制弱；端到端语音翻译不利于字幕可审校 | **保留 LLM 主路径**，窗口超预算时顺序传递交接摘要，每个窗口一次完成结构化翻译 |
| 字幕 | 自定义 SRT 生成、行宽控制 | ASS/SSA、WebVTT、字幕质量检查器 | 当前实现可控；ASS 可表达样式和定位；WebVTT 适合网页 | 样式和跨播放器兼容性更复杂 | **保留 SRT**，增加可选 ASS 输出和 CPS/CPL/重叠检查 |
| 配音计划 | 时长估算、语速标记、gap | 字符/音素时长预测；TTS dry-run；语言专属规则 | 当前方案无需调用 TTS，速度快 | 估算与真实 TTS 仍有偏差 | **保留**，让试配工作台把真实时长反馈回计划，形成闭环 |
| 试配与参数确认 | 当前工作台逐句合成、试听、满意标记 | CosyVoice 3、OpenAudio-S1-mini、Chatterbox Multilingual、F5-TTS、GPT-SoVITS 作为 A/B 候选 | 可在批量前发现音色、情感、节奏问题 | 多引擎接口和参数命名不同 | **把工作台作为统一评测入口**，保存引擎、模型、参数、参考音频和随机种子 |
| 批量配音 | IndexTTS2.5 / GPT-SoVITS 逐句缓存 | CosyVoice 3；OpenAudio-S1-mini；Chatterbox Multilingual；F5-TTS | IndexTTS2.5 中文情感/时长/音色控制好；CosyVoice 3 多语言、方言、流式和指令控制强；S1-mini 体量小；Chatterbox 有情感和多语言；F5-TTS 流匹配自然度高 | 新引擎的服务协议、授权、显存、稳定性不同；中文表现需实测 | **IndexTTS2.5 保持默认**；优先新增 CosyVoice 3 适配器，S1-mini 作为低显存候选，其他引擎先放实验配置 |
| 混音/成片 | NumPy 铺轨 + FFmpeg、EBU R128、ducking、字幕烧录 | FFmpeg 继续；可选 loudness metering、响度目标预设、ASS 滤镜 | 成熟、可复现、视频流可直拷 | 编码参数和平台播放器差异；混音质量依赖前面分离结果 | **保留**，增加响度/削波/对白清晰度报告，不需要换核心工具 |

## 当前项目与候选技术的重点说明

### ASR

OpenAI 官方 Whisper 仓库仍提供 `large-v3-turbo`；它比 `large-v3` 更快，但在部分语言（例如粤语、泰语）可能有退化。WhisperX 在 faster-whisper 基础上增加 VAD、强制对齐和说话人标注，适合字幕时间轴，而不是简单替换模型。Fun-ASR-Nano-2512 面向中文、英语、日语和中文方言；Fun-ASR-MLT-Nano-2512 另有 31 语言版本。SenseVoiceSmall 还输出语言、情感和音频事件，适合给“语速/情绪/配音计划”提供辅助信号。NVIDIA Parakeet TDT v3 适合 25 种欧洲语言的高吞吐转录，但不应被当作中文方案。

### 音源分离

Demucs 仍然可用、易于集成，但官方仓库已经归档；这不是“立即不能用”，而是维护风险信号。BS-RoFormer/Mel-RoFormer 在音乐源分离上有更高质量上限，但模型、显存和模型文件管理更复杂。对视频配音而言，最实用的设计不是强制所有用户使用最高质量模型，而是提供“快速 Demucs / 高质量 RoFormer / 失败回退”的统一接口。

### TTS

IndexTTS-2.5 已具备单参考音频零样本克隆、中文/英语/日语/西班牙语/阿拉伯语、情感和速度控制，仍适合作为本项目的中文默认引擎。CosyVoice 3 的优势是多语言、中文方言、跨语言克隆、发音修补、指令控制以及流式输出；它是最值得优先接入的候选。OpenAudio-S1-mini 体量较小，适合显存有限的本地部署。Chatterbox Multilingual 覆盖 23+ 语言并提供表达控制；F5-TTS 的流匹配方案自然度和跨语言能力突出，但工程接口和中文实用性需要实测。GPT-SoVITS 仍适合已有训练音色或需要 few-shot 微调的用户，但部署复杂度高于纯零样本引擎。

## 建议的升级顺序

| 优先级 | 建议 | 原因 | 验收方式 |
|---|---|---|---|
| P0 | 保持当前默认模型，同时修正“试配在批量前”的流程 | 立即改善用户体验，不引入模型风险 | 3–5 句试配 → 调参 → 批量；批量复用试配缓存 |
| P1 | 增加统一 TTS Adapter，先接 CosyVoice 3 | 当前最可能带来多语言/方言/流式收益 | 同一组句子比较音色相似度、自然度、时长偏差、显存和 RTF |
| P1 | ASR 增加 Fun-ASR-Nano 与 WhisperX 对齐档 | 中文方言和字幕时间轴是高频痛点 | 中文、粤语、英语各一段，比较 CER/WER、断句和时间轴 |
| P1 | 分离增加 BS-RoFormer 高质量档 | Demucs 已归档，且人声残留直接影响 TTS 参考和背景混音 | 比较残留人声、音乐损伤、显存、耗时和成片对白清晰度 |
| P2 | 翻译增加 DeepL/NLLB 适配器 | 给 LLM 增加可比较的稳定 API 与离线回退 | 术语准确率、上下文一致性、成本、隐私和失败恢复 |
| P2 | 建立固定评测集和 A/B 报告 | 没有同一数据无法判断“升级” | 每个引擎保存模型版本、参数、样例音频和人工评分 |
| P3 | pyannote community-1 作为多人视频可选步骤 | 只有多人/多音色项目才值得付出额外计算和依赖 | 多人片段的说话人纯度、错分率和运行时 |

## 参考资料

- [OpenAI Whisper 官方仓库与模型卡](https://github.com/openai/whisper)
- [WhisperX 官方仓库](https://github.com/m-bain/whisperX)
- [Fun-ASR 官方仓库](https://github.com/QwenAudio/Fun-ASR)
- [SenseVoice 官方仓库](https://github.com/QwenAudio/SenseVoice)
- [NVIDIA Parakeet TDT v3 模型卡](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
- [Demucs 官方仓库（已归档）](https://github.com/facebookresearch/demucs)
- [BS-RoFormer 官方实现](https://github.com/asriverwang/BS-RoFormer)
- [pyannote.audio 官方仓库](https://github.com/pyannote/pyannote-audio)
- [Qwen3 官方仓库](https://github.com/QwenLM/Qwen3)
- [NLLB Transformers 文档](https://huggingface.co/docs/transformers/model_doc/nllb)
- [SeamlessM4T v2 Transformers 文档](https://huggingface.co/docs/transformers/model_doc/seamless_m4t_v2)
- [DeepL 开发者文档](https://www.deepl.com/en/developers)
- [IndexTTS-2.5 官方仓库](https://github.com/index-tts/index-tts)
- [CosyVoice 官方仓库](https://github.com/FunAudioLLM/CosyVoice)
- [OpenAudio/Fish Speech 官方仓库](https://github.com/fishaudio/fish-speech)
- [Chatterbox 官方仓库](https://github.com/resemble-ai/chatterbox)
- [F5-TTS 官方仓库](https://github.com/SWivid/F5-TTS)
- [GPT-SoVITS 官方仓库](https://github.com/RVC-Boss/GPT-SoVITS)
