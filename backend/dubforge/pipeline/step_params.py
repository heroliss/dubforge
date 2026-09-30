"""步骤参数 schema: 每步骤可在步骤页编辑的配置项 (单一定义处)
================================================================
- path = config.yaml 点分路径; 值实时从 config 读取, 保存走 POST /api/settings
- 各步骤注册表的 cfg_keys 都以整段前缀 (如 "separate") 声明, 新增键自动进
  输入指纹 —— 参数一改, 已完成步骤即标记「已过期」, 重跑按新配置执行
- 前端按 type 渲染控件; 未列入 schema 的 cfg 键在步骤页仍只读展示
"""
from dataclasses import dataclass

from dubforge.backends import asr_faster_whisper, tts_gptsovits, tts_indextts25


@dataclass(frozen=True)
class ParamField:
    path: str
    label: str
    type: str                      # bool | int | float | str | password | text | select
    help: str = ""
    options: tuple[tuple[str, object], ...] = ()   # select 用: (显示文本, 值)
    editable: bool = False         # select 允许手输列表外的自定义值
    min: float | None = None
    max: float | None = None
    step: float | None = None
    group: str = ""
    # 依赖开关: 每项为 (config_path, 要求值); 前端据此置灰不满足条件的参数
    # 例: (("split.use_llm", True),) 表示仅当 use_llm=True 时可用
    depends_on: tuple[tuple[str, object], ...] = ()


# 语言表由各后端模块自己声明 (LANGUAGES) —— 换后端自动适应, 见 language_capabilities()


STEP_PARAM_SCHEMA: dict[str, tuple[ParamField, ...]] = {
    "download": (
        ParamField("download.resolution", "最高分辨率", "select",
                   help="下载视频的最大高度",
                   options=(("360p", 360), ("480p", 480), ("720p", 720),
                            ("1080p", 1080), ("1440p", 1440), ("2160p", 2160))),
        ParamField("download.proxy", "代理", "str",
                   help="例 http://127.0.0.1:7897; 留空 = 读环境变量"),
        ParamField("download.cookies_path", "Cookies 文件", "str",
                   help="YouTube cookies.txt 绝对路径 (年龄限制/会员视频需要)"),
    ),
    "preprocess": (
        ParamField("separate.enabled", "人声/背景分离", "bool",
                   help="关闭后只提取音轨, 不跑 demucs"),
        ParamField("separate.model", "分离模型", "select", depends_on=(("separate.enabled", True),),
                   help="htdemucs_ft 为精调版, 效果更好但约慢一倍",
                   options=(("htdemucs (标准)", "htdemucs"),
                            ("htdemucs_ft (精调, 更优)", "htdemucs_ft"))),
        ParamField("separate.shifts", "位移平均次数", "int", depends_on=(("separate.enabled", True),), min=0, max=5, step=1,
                   help="同一音频多次微小位移分离后取平均; 0=最快, 1~2 明显更好且按倍变慢"),
        ParamField("separate.overlap", "分块重叠比例", "float", depends_on=(("separate.enabled", True),), min=0.05, max=0.5, step=0.05,
                   help="长音频分块处理的重叠比例, 越大接缝越平滑越慢"),
    ),
    "transcribe": (
        ParamField("asr.input_mode", "原文获取方式", "select",
                   options=(("识别视频语音", "speech"), ("已导入的平台字幕", "platform_subtitle")),
                   help="平台字幕需先在发布中心的B站稿件工作区导入，核对语言及分P时间轴后再运行；运行会更新原文并使依赖它的下游过期"),
        ParamField("asr.model", " Whisper 模型", "select",
                   help="更大更准更慢; large-v3-turbo 为速度/精度均衡的默认",
                   options=(("large-v3-turbo", "large-v3-turbo"), ("large-v3", "large-v3"),
                            ("large-v2", "large-v2"), ("medium", "medium"),
                            ("small", "small"), ("base", "base"))),
        ParamField("asr.language", "源语言", "select",
                   help="Whisper 支持的语言; auto=自动检测。指定语言更快更稳",
                   options=asr_faster_whisper.LANGUAGES),
        ParamField("asr.segmentation_mode", "发送给 LLM 的时间轴粒度", "select",
                   help="短语级只发送约 2 秒短语时间和停顿提示，速度/成本较低；词级发送每个词的时间和停顿提示，时间边界更细但请求更长。它只影响下一步语义整理的 LLM 输入；语音转文字预览会按当前模式显示短语或词级时间轴",
                   options=(("短语级（推荐）", "phrase"), ("词级（更精确）", "word"))),
        ParamField("asr.pause_threshold_seconds", "停顿断句阈值", "float", min=0.3, max=2, step=0.1,
                   help="转录时词间静音超过此秒数才先分成短语锚点; 标点也会先切出锚点，但最终是否合并由语义整理决定"),
        ParamField("asr.max_segment_seconds", "识别后短语锚点时长上限", "float", min=1.5, max=30, step=0.5,
                   help="只整理 Whisper 已识别出的词级时间戳，不会把音频切成小块，也不会减少识别上下文；默认 2 秒。越小时间边界越细，但会增加送给 LLM 的锚点数量；这不是最终字幕时长"),
    ),
    "split": (
        ParamField("split.use_llm", "启用 LLM 语义整理", "bool",
                   help="把短语级或词级时间轴、停顿提示和上下文交给 LLM 理解整理；语义边界由模型自主判断，关闭后只用本地规则"),
        ParamField("split.llm_profile", "语义整理模型", "select", editable=True, depends_on=(("split.use_llm", True),),
                   help="留空=按设置页启用顺序自动降级；也可固定某个配置组。接口地址、密钥和模型名在设置页维护"),
        ParamField("split.llm_context_tokens", "单次语义整理上下文预算", "int", depends_on=(("split.use_llm", True),), min=8000, max=1_000_000, step=10000,
                   help="控制一次语义整理请求可容纳的正文规模；超过预算后自动分窗，系统会另外预留提示词和输出空间"),
        ParamField("split.context_lines", "分窗前后参考句数", "int", depends_on=(("split.use_llm", True),), min=0, max=4, step=1,
                   help="长稿分窗时，给每个窗口前后各 N 句作为理解锚点，帮助判断代词、转折和边界；只作参考，不会输出"),
        ParamField("split.max_split_length", "目标单句长度（字符）", "int", min=8, max=80, step=1,
                   help="主要作为 LLM 的阅读节奏参考；本地只兜底处理明显过长的结果，不是逐句硬性限制"),
        ParamField("split.max_segment_seconds", "目标单句时长（秒）", "float", min=4, max=20, step=0.5,
                   help="帮助 LLM 保持配音和字幕节奏；本地只会在明显超长时参考词级时间拆分"),
    ),
    "translate": (
        ParamField("translate.target_language", "目标语言", "select", editable=True,
                   help="可下拉选择, 也可直接输入任意语言名称 (喂给 LLM)",
                   options=(("简体中文", "简体中文"), ("繁體中文", "繁體中文"),
                            ("English", "English"), ("日本語", "日本語"),
                            ("한국어", "한국어"), ("Deutsch", "Deutsch"),
                            ("Français", "Français"), ("Español", "Español"),
                            ("Русский", "Русский"), ("Português", "Português"))),
        ParamField("translate.style_prompt", "风格提示词", "text",
                   help="说明期望的人设、语气和表达方式；支持润色、本地化、方言、幽默或网络流行语。留空时采用贴近原语气的自然口语"),
        ParamField("translate.llm_profile", "翻译模型配置", "select", editable=True,
                   help="留空=按设置页启用顺序自动降级; 也可固定某个配置组。接口地址、密钥和模型名在设置页维护"),
        ParamField("translate.max_context_tokens", "单次翻译上下文上限", "int", min=16000, max=1_000_000, step=10000,
                   help="每个翻译窗口的总 token 安全预算，包含提示词、交接摘要和预留输出；正文会略小于此值。默认 200000 (约 0.2M), 最高可调到 1M"),
        ParamField("translate.custom_terms_path", "术语表路径", "str",
                   help="xlsx 绝对路径 (三列: 原文/译文/备注)"),
    ),
    "subtitles": (
        ParamField("subtitles.mode", "导出内容", "select",
                   help="使用语义整理后的原文及已保存的校对修改；仅导出原文不需要翻译或配音。识别原稿可在语音转文字步骤下载",
                   options=(("全部三种", "all"), ("仅原文", "source"),
                            ("仅目标语言", "target"), ("仅双语", "bilingual"))),
        ParamField("subtitles.source_first", "双语字幕原文在上", "bool",
                   help="默认译文在上、原文在下"),
        ParamField("subtitles.min_duration", "最短显示时长（秒）", "float", min=0, max=3, step=0.1,
                   help="利用句间空隙延长短字幕，遇下一句即停止；0=不延长。手动修改过时间的句子始终保持所设时间。文字换行由播放器处理"),
    ),
    "dubtask": (),  # 旧任务接口兼容；预估偏差标记已不用于当前编辑页。
    "tts": (
        ParamField("tts.backend", "TTS 后端", "select",
                   help="选择负责生成配音的服务；本机已安装的服务可在配音页快捷启动",
                   options=(("IndexTTS2.5", "indextts25"), ("GPT-SoVITS", "gptsovits"))),
        ParamField("tts.indextts25.url", "IndexTTS 地址", "str", depends_on=(("tts.backend", "indextts25"),),
                   help="IndexTTS2.5 WebUI 地址"),
        ParamField("tts.indextts25.project_dir", "IndexTTS 安装目录", "str", depends_on=(("tts.backend", "indextts25"),),
                   help="快捷启动使用；留空自动查找同级 index-tts 目录"),
        ParamField("tts.indextts25.timbre_mode", "音色参考", "select", depends_on=(("tts.backend", "indextts25"),),
                   help="preset=自定义参考音频；stable=视频开头 8 秒；dynamic=严格截取本句起止时间内的原声，短句也不改用视频开头。参考过短或包含多人时，建议给本句指定同一说话人的清晰参考音频。",
                   options=(("preset 自定义参考", "preset"), ("stable 开头切片", "stable"),
                            ("dynamic 每句原声", "dynamic"))),
        ParamField("tts.indextts25.timbre_audio", "音色参考音频", "str",
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.timbre_mode", "preset")),
                   help="本机参考音频的完整路径，用于确定配音音色"),
        ParamField("tts.indextts25.emo_mode", "情感参考", "select", depends_on=(("tts.backend", "indextts25"),),
                   help="同音色=沿用音色参考的情感；音频参考=模仿指定音频；开头=前 8 秒原声，逐句=本句原声，短句也不改用开头；向量=手动组合八种情绪；文本=按描述理解情绪，需要服务端加载文本情感模型。",
                   options=(("同音色参考", "same"), ("自定义情感音频", "preset"), ("视频开头原声", "stable"),
                              ("逐句原声", "dynamic"), ("情感向量", "vector"), ("文本情感（需模型支持）", "text"))),
        ParamField("tts.indextts25.emo_vector", "情感向量", "vector", min=0, max=1, step=0.05,
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "vector")),
                   help="依次为喜、怒、哀、惧、厌恶、低落、惊喜、平静，各项 0～1，可混合使用。建议先小幅增加一两项试听；它们不是概率，不要求总和为 1，服务端会归一化并调整强度。"),
        ParamField("tts.indextts25.emo_text", "情感描述", "text",
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "text")),
                   help="描述语气，例如“压低声音，带一点紧张”；留空时使用待合成文本理解情感。IndexTTS 2.5 需加载 QwenEmotion，低显存默认可能不加载；可按其说明使用 --qwen_emo 启动。"),
        ParamField("tts.indextts25.emo_random", "情感随机采样", "bool", depends_on=(("tts.backend", "indextts25"),),
                   help="向量或文本情感模式下，增加同一情绪的表现差异；可能降低音色一致性。希望整段配音稳定时建议关闭，音频参考模式下此项不参与合成。"),
        ParamField("tts.indextts25.emo_audio", "情感参考音频", "str",
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "preset")),
                   help="本机参考音频的完整路径，用于参考语气和情感"),
        ParamField("tts.indextts25.emo_weight", "情感权重", "float", depends_on=(("tts.backend", "indextts25"),),
                   help="情感参考对语气的影响程度; 0=基本不用情感参考, 1=完全跟随参考",
                   min=0, max=1, step=0.05),
        ParamField("tts.indextts25.duration_factor", "时长因子", "float", depends_on=(("tts.backend", "indextts25"),), min=0.5, max=2, step=0.05,
                   help="控制模型生成时长，1.0 为正常；小于 1 倾向更快、更短，大于 1 倾向更慢、更长。不是对成品音频机械拉伸，也不保证精确匹配字幕时长。"),
        ParamField("tts.indextts25.duration_retry_factors", "超长重试因子", "str", depends_on=(("tts.backend", "indextts25"),),
                   help="例：0.7, 0.5；留空关闭。配音超过可用时长时，按顺序尝试更小的因子（0.5～2）。轻微超长（不超过 5%，最多 0.15 秒）不重复调用模型，仍显示超长提示；音频不会裁剪。最终采用的较小因子保存到本句设置，后续合成沿用。"),
        ParamField("tts.indextts25.request_timeout_seconds", "单次配音超时（秒）", "int", min=0, max=86400, step=30,
                   group="运行保护", depends_on=(("tts.backend", "indextts25"),),
                   help="每次调用模型的等待上限，包含服务排队；默认 300 秒，0 关闭。与整批配音的 7200 秒等步骤超时独立。超时保留已有音频并停止整批任务；服务端尚未结束时禁止继续堆积请求，可等待完成或在空闲后重启服务。"),
        ParamField("tts.indextts25.lang", "合成语言", "select", depends_on=(("tts.backend", "indextts25"),),
                   help="IndexTTS2.5 支持的语言 (取自其 WebUI 选项)",
                   options=tuple((label, code) for code, label in tts_indextts25.LANGUAGES)),
        ParamField("tts.indextts25.do_sample", "随机采样 · do_sample", "bool", group="高级合成参数",
                   help="开启后从候选中采样，语音表现更丰富，同样参数重跑也可能不同；关闭后使用确定性选择，但不保证所有硬件上完全一致。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.top_p", "累计概率 · top_p", "float", min=0, max=1, step=0.01, group="高级合成参数",
                   help="采样时保留累计概率达到此值的候选。降低通常更保守，提高允许更多变化；仅随机采样开启时起作用，建议从 0.8 附近小幅调整。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.top_k", "候选数量 · top_k", "int", min=0, max=100, group="高级合成参数",
                   help="采样时只考虑概率最高的 K 个候选；0 表示不按数量限制。越小越保守，越大越多样，与 top_p 同时生效。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.temperature", "采样温度 · temperature", "float", min=0.1, max=2, step=0.1, group="高级合成参数",
                   help="控制采样分布。较低更稳定，较高更有变化，也更可能发音不稳或重复；仅随机采样开启时起作用，默认 0.8。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.length_penalty", "长度惩罚 · length_penalty", "float", min=-2, max=2, step=0.1, group="高级合成参数",
                   help="束搜索的长度评分系数；正值倾向较长序列，负值倾向较短序列，0 不调整。不是语速控制，优先保持默认，语速请用时长因子。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.num_beams", "搜索分支 · num_beams", "int", min=1, max=10, group="高级合成参数",
                   help="同时保留多少条候选生成序列。1 为单路径，增大通常占用更多显存、耗时更长，质量不一定更好；默认 3。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.repetition_penalty", "重复惩罚 · repetition_penalty", "float", min=0.1, max=20, step=0.1, group="高级合成参数",
                   help="1 不额外惩罚重复，越大越抑制已出现的生成符号；过高也可能损伤发音连贯性。不是逐字去重开关，默认沿用 IndexTTS 的 10。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.max_mel_tokens", "音频生成上限 · max_mel_tokens", "int", min=50, step=10, group="高级合成参数",
                   help="单次生成的音频 token 上限；太小可能截断语音，太大增加异常长输出的等待时间。不是秒数，且不能超过所用模型支持的上限；默认 1500。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.max_text_tokens_per_segment", "模型内分段上限 · max_text_tokens_per_segment", "int", min=20, step=2, group="高级合成参数",
                   help="IndexTTS 内部每段文本的 token 数，不改变 DubForge 的字幕句子和标号。太小会把语音切碎，太大可能影响质量或超出模型上限；建议 80～200，默认 120。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.gptsovits.url", "GPT-SoVITS 地址", "str", depends_on=(("tts.backend", "gptsovits"),), help="GPT-SoVITS api_v2 服务地址，默认 http://127.0.0.1:9880/；不是训练 WebUI 地址。"),
        ParamField("tts.gptsovits.project_dir", "GPT-SoVITS 安装目录", "str", depends_on=(("tts.backend", "gptsovits"),),
                   help="快捷启动使用；填写含 api_v2.py 的目录，留空自动查找同级或 VideoLingo-Workspace 内的整合包"),
        ParamField("tts.gptsovits.character", "GPT-SoVITS 角色", "str", depends_on=(("tts.backend", "gptsovits"),), help="保留旧整合包角色配置；当前 api_v2 的音色与模型主要由服务端设置，此字段不会自动切换模型。"),
    ),
    "assemble": (
        ParamField('assemble.voice_volume', '人声音量', 'float', min=0, max=1, step=0.05,
                   help='与配音编辑页共用，1 为原音量，0 为人声静音'),
        ParamField("assemble.subtitle_track", "烧录字幕类型", "select",
                   help="烧录效果预览及带字幕成品使用此类型；播放器的试听字幕可单独切换。正式烧录前需导出对应文件",
                   options=(("目标语言", "trans"), ("原文", "source"), ("双语", "bilingual"))),
        ParamField("assemble.bgm_volume", "背景音音量", "float", min=0, max=1, step=0.05,
                   help="合成成片时背景音轨的混入音量"),
        ParamField("assemble.ducking", "人声压低背景 (ducking)", "bool",
                   help="说话时自动压低背景音 (sidechaincompress)"),
        ParamField("assemble.loudnorm", "配音人声响度归一", "bool",
                   help="使用 EBU R128 响度测量的单遍动态处理：配音人声轨目标 −16 LUFS，真峰值上限 −1.5 dBTP，响度范围目标 11 LU。仅处理混入背景前的人声轨，不作用于原声版，也不保证成片混音达到该响度；关闭后仍保留铺轨时的峰值归一。"),
        ParamField("assemble.burn_subtitles", "流程默认烧录字幕", "bool",
                   help="仅决定总览批量流程的默认配音版是否烧录；成片页的正式成品与试片按当前明确选择的组合运行。烧录需要重新编码，SRT 可单独导出"),
        ParamField("assemble.subtitle_font", "译文字体（通用）", "select", editable=True,
                   options=(("微软雅黑", "Microsoft YaHei"), ("黑体", "SimHei"), ("宋体", "SimSun"), ("思源黑体", "Source Han Sans SC"), ("Arial", "Arial")),
                   help="可输入系统已安装的字体名称；缺字或字体不存在时由渲染器回退，请用烧录效果预览确认"),
        ParamField("assemble.subtitle_size", "译文字号（通用）", "int", min=16, max=120, step=2,
                   help="按 1080 高画面设定，随视频尺寸等比例缩放；双语与长句建议适当调小"),
        ParamField("assemble.subtitle_color", "译文文字颜色", "color", help="原文未开启独立样式时也使用此颜色"),
        ParamField("assemble.subtitle_outline_color", "译文描边颜色", "color", help="深色描边可提高字幕在明亮背景上的可读性"),
        ParamField("assemble.subtitle_outline", "译文描边粗细", "float", min=0, max=10, step=0.5,
                   help="按 1080 高画面设定；0 关闭描边"),
        ParamField("assemble.subtitle_background_color", "文字底色", "color", help="只影响文字后方的背景盒，不改变文字与描边颜色"),
        ParamField("assemble.subtitle_background_opacity", "底色不透明度（%）", "int", min=0, max=100, step=5,
                   help="0 完全透明，100 不透明；双语多行共用一块背景，预览与成片一致"),
        ParamField("assemble.subtitle_margin", "字幕底部距离", "int", min=0, max=360, step=5,
                   help="字幕距画面底边的距离，按 1080 高画面设定；双语整体向上堆叠"),
        ParamField("assemble.crf", "画面质量 CRF", "int", min=14, max=28, step=1,
                   help="越小越清晰越大文件; 18 为视觉无损附近"),
    ),
}


def source_style_fields(prefix, *, comments=False):
    """字幕、评论共用原文样式控件定义，接入相同的配置继承。"""
    enabled = prefix + 'source_style_enabled'
    depends = ((enabled, True),)
    return (
        ParamField(enabled, '原文独立样式', 'bool', help='关闭时原文跟随译文；开启后字体、字号、文字色与描边分别设置。位置和底色共用。'),
        ParamField(prefix + 'source_font', '原文字体', 'select', editable=True, options=(('微软雅黑', 'Microsoft YaHei'), ('Arial', 'Arial')), depends_on=depends),
        ParamField(prefix + ('source_font_size' if comments else 'source_size'), '原文字号', 'int', min=18 if comments else 16, max=72 if comments else 120, step=1, depends_on=depends),
        ParamField(prefix + 'source_color', '原文文字颜色', 'color', depends_on=depends),
        ParamField(prefix + 'source_outline_color', '原文描边颜色', 'color', depends_on=depends),
        ParamField(prefix + 'source_outline', '原文描边粗细', 'float', min=0, max=10, step=.5, depends_on=depends),
    )


STEP_PARAM_SCHEMA['assemble'] += source_style_fields('assemble.subtitle_')

# 每一步都提供同一项“超时兜底”设置，既能在设置页统一调整，也能在项目步骤页单独覆盖。
# 该参数只影响运行保护，不参与步骤输入指纹，避免改保护时误使正式产物过期。
# 评论方案不是流水线步骤：只在全局默认页暴露，不加入步骤依赖或项目参数覆盖。
COMMENT_DEFAULT_FIELDS = (
    ParamField('comments.overlay.language_mode', '评论语言', 'select', options=(("中文译文", "translated"), ("双语", "bilingual"), ("原文", "original"))),
    ParamField('comments.overlay.source_first', '双语原文在上', 'bool', depends_on=(("comments.overlay.language_mode", "bilingual"),)),
    ParamField('comments.overlay.start', '首次出现（秒）', 'float', min=0, max=86400, step=1),
    ParamField('comments.overlay.interval', '出现间隔（秒）', 'float', min=1, max=600, step=1),
    ParamField('comments.overlay.duration', '每条停留（秒）', 'float', min=1, max=60, step=1, help='停留时长不能大于出现间隔。'),
    ParamField('comments.overlay.position', '位置', 'select', options=(("左上", "top_left"), ("顶部居中", "top"), ("右上", "top_right"), ("左下", "bottom_left"), ("底部居中", "bottom"), ("右下", "bottom_right"))),
    ParamField('comments.overlay.font', '译文字体（通用）', 'str'),
    ParamField('comments.overlay.font_size', '译文字号（通用）', 'float', min=18, max=72, step=1),
    ParamField('comments.overlay.margin', '距边缘', 'float', min=0, max=400, step=1),
    ParamField('comments.overlay.width_percent', '最大宽度（画面 %）', 'float', min=0, max=100, step=1,
               help='0 为自动。数值是占整个画面的宽度百分比，受边距限制；短评论不会强行撑满。'),
    ParamField('comments.overlay.color', '文字颜色', 'color'),
    ParamField('comments.overlay.outline_color', '描边颜色', 'color'),
    ParamField('comments.overlay.outline', '描边粗细', 'float', min=0, max=10, step=.5),
    ParamField('comments.overlay.background_color', '文字底色', 'color'),
    ParamField('comments.overlay.background_opacity', '底色不透明度', 'float', min=0, max=100, step=5, help='0 完全透明，100 实心底色。'),
    ParamField('comments.overlay.show_author', '显示作者昵称', 'bool'),
) + source_style_fields('comments.overlay.', comments=True)


_STEP_TIMEOUT_LABELS = {
    "download": "获取视频", "preprocess": "音轨分离", "transcribe": "语音转文字",
    "split": "语义整理", "translate": "翻译", "subtitles": "字幕导出",
    "dubtask": "配音计划", "tts": "字幕与配音", "assemble": "合成成片",
}
_STEP_TIMEOUT_HELP = (
    "从本步骤真正开始运行后计时，排队等待和用户暂停不计入；超时会请求安全取消并清理临时文件。"
    "单位为秒，0=关闭兜底。默认值按本步骤常见耗时设置，长视频可适当调大。"
)
for _step_key, _step_label in _STEP_TIMEOUT_LABELS.items():
    STEP_PARAM_SCHEMA[_step_key] = STEP_PARAM_SCHEMA[_step_key] + (
        ParamField(f"runner.step_timeout_seconds.{_step_key}",
                   f"{_step_label}超时兜底（秒）", "int", help=_STEP_TIMEOUT_HELP,
                   min=0, max=86400, step=60),
    )


def validate_numeric_updates(updates: dict) -> None:
    """数值控件清空会提交 null；保存前统一校验，避免运行时才报转换错误。"""
    import math
    from dubforge.errors import ConfigError

    fields = {f.path: f for group in STEP_PARAM_SCHEMA.values() for f in group
              if f.type in {"int", "float"}}
    # 时长诊断仍使用此内部阈值；不是TTS合成速度控件，不在界面展示。
    fields['tts.tolerance'] = ParamField('tts.tolerance', '时长偏差容差', 'float', min=0, max=2)
    for key, value in updates.items():
        field = fields.get(key)
        if field and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ConfigError(f"「{field.label}」不能为空，请填写有效数字",
                              suggestion="参数尚未保存；请填写数值，或在项目步骤页使用恢复默认。")
        if field and (key.startswith(("tts.", "subtitles.", "assemble.subtitle_")) or key in {'assemble.voice_volume', 'assemble.bgm_volume'}):
            if (field.type == "int" and int(value) != value
                    or field.min is not None and value < field.min
                    or field.max is not None and value > field.max):
                raise ConfigError(f"「{field.label}」超出允许范围或不是整数，请按控件说明填写。")
    vector = updates.get("tts.indextts25.emo_vector", [0] * 8)
    if (not isinstance(vector, list) or len(vector) != 8
            or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in vector)):
        raise ConfigError("情感向量必须是 8 个 0～1 的有效数字。")
    for field in STEP_PARAM_SCHEMA["tts"]:
        if field.path not in updates:
            continue
        value = updates[field.path]
        if ((field.type == "bool" and type(value) is not bool)
                or (field.type == "select" and value not in {v for _, v in field.options})
                or (field.type in {"str", "text"} and not isinstance(value, str))):
            raise ConfigError(f"「{field.label}」的值无效，请重新选择或填写。")
    retry_key = 'tts.indextts25.duration_retry_factors'
    if retry_key in updates:
        from dubforge.pipeline.tts_duration import parse_factors
        parse_factors(updates[retry_key])
    from dubforge.pipeline.subtitle_style import validate_style
    validate_style(updates)


def language_capabilities(*, settings: dict | None = None) -> dict:
    """当前配置的后端各自的语言能力 (换后端自动适应):
    {asr: {backend, langs}, tts: {backend, langs}}
    langs = [(code, 中文名)]; 中文名与「翻译目标语言」取值对齐,
    交集即"可贯穿全流程"的语言 (识别→翻译→配音)。"""
    from dubforge import config
    settings = config.load_config() if settings is None else settings
    asr_backend = str(config.get("asr.backend", settings=settings) or "faster_whisper")
    tts_backend = str(config.get("tts.backend", settings=settings) or "indextts25")
    asr_mod = {"faster_whisper": asr_faster_whisper}.get(asr_backend)
    tts_mod = {"indextts25": tts_indextts25, "gptsovits": tts_gptsovits}.get(tts_backend)
    return {
        "asr": {"backend": asr_backend,
                "langs": list(getattr(asr_mod, "LANGUAGES", ()) or ())},
        "tts": {"backend": tts_backend,
                "langs": list(getattr(tts_mod, "LANGUAGES", ()) or ())},
    }


def schema_view(values: dict, caps: dict | None = None, *, settings: dict | None = None, global_defaults: bool = False) -> dict:
    """序列化给前端: {step_key: {fields: [...], values: {path: 当前值}}}
    values 由调用方按 path 从 config 提取"""
    from dubforge import config
    settings = config.load_config() if settings is None else settings
    caps = caps or language_capabilities(settings=settings)
    profiles = settings.get("llm", {}).get("profiles") or []
    profile_options = [("跟随全局降级链", "")]
    profile_options.extend(
        (str(p.get("name") or f"配置{i + 1}"), str(p.get("name") or f"配置{i + 1}"))
        for i, p in enumerate(profiles)
        if p.get("name")
    )
    if not profiles:
        legacy = settings.get("llm", {})
        if legacy.get("base_url"):
            profile_options.append(("默认", "default"))
    tts_names = {name for _, name in caps["tts"]["langs"]}
    out: dict[str, dict] = {}
    schemas = {**STEP_PARAM_SCHEMA, **({'comments': COMMENT_DEFAULT_FIELDS} if global_defaults else {})}
    for key, fields in schemas.items():
        fl = []
        for f in fields:
            options = [{"label": lb, "value": v} for lb, v in f.options]
            if f.path in {"split.llm_profile", "translate.llm_profile"}:
                options = [{"label": lb, "value": v} for lb, v in profile_options]
            # 语言能力注入 (取自当前后端模块的声明, 换后端自动适应):
            if f.path == "asr.language" and caps["asr"]["langs"]:
                options = [{"label": lb, "value": v} for v, lb in caps["asr"]["langs"]]
            elif f.path == "translate.target_language":
                # 分组: TTS 能发音的目标语言 = 可贯穿全流程 (识别→翻译→配音);
                # 其余仅能出字幕。editable 仍允许输入任意语言名。
                dub, sub = [], []
                for lb, v in f.options:
                    (dub if v in tts_names else sub).append({"label": lb, "value": v})
                options = []
                if dub:
                    options.append({"type": "group", "label": "可配音 · 贯穿全流程",
                                    "key": "dub", "children": dub})
                if sub:
                    options.append({"type": "group", "label": "仅字幕 (当前配音引擎不支持发音)",
                                    "key": "sub", "children": sub})
            fl.append({"path": f.path, "label": f.label, "type": f.type, "help": f.help, "depends_on": [[p, v] for p, v in f.depends_on],
                       "options": options, "editable": f.editable,
                       "min": f.min, "max": f.max, "step": f.step, "group": f.group})
        out[key] = {"fields": fl, "values": {f.path: values.get(f.path) for f in fields}}
    return out
