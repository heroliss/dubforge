"""步骤参数 schema: 每步骤可在步骤页编辑的配置项 (单一定义处)
================================================================
- path 是点分参数名；制作值读取项目固定基础值与覆盖，运行连接仍共享。
- 各步骤注册表的 cfg_keys 都以整段前缀 (如 "separate") 声明, 新增键自动进
  输入范围；指纹再区分制作要求与执行服务，只有制作输入变化才要求更新。
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
    "content_review": (
        ParamField("content_review.enabled", "启用输出内容筛查", "bool",
                   help="检查原文、译文和评论，隐藏命中整句；只筛查文本，需另行核对画面。"),
        ParamField("content_review.mute_audio", "屏蔽命中时段的声音", "bool",
                   help="按下方方式处理命中时段的原声和配音。建议开启；关闭后只隐藏字幕，声音仍可能包含原话。"),
        ParamField("content_review.audio_mode", "声音屏蔽方式", "select",
                   help="建议仅屏蔽语音：移除原声及配音，用同一时段的分离背景音补齐；背景轨不可用时整段静音。全部静音适用于背景仍有可辨识人声的情况。分离效果需试听确认。",
                   options=(("仅屏蔽语音，保留背景", "voice_only"), ("全部声音静音", "all_audio")),
                   depends_on=(("content_review.enabled", True), ("content_review.mute_audio", True))),
        ParamField("content_review.blocked_terms", "补充屏蔽词", "text", help="每行一个，中英文均检查。"),
        ParamField("content_review.blocked_ranges", "人工屏蔽时段", "text",
                   help="每行 开始秒-结束秒:原因；时间按源视频轴，不会自动删改原稿或已上传文件。"),
    ),
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
                   help="短稿可全文一次提交；长稿会按词数自动分块。预算包含提示词和输出预留，需与模型实际容量匹配"),
        ParamField("split.llm_chunk_target_words", "长稿分块核心词数", "int", editable=True, depends_on=(("split.use_llm", True),), min=100, max=2000, step=50,
                   help="长稿超过自动分块阈值后，每块的核心词数；较大可保留更多上下文，较小可降低单次延迟和格式漂移"),
        ParamField("split.llm_chunk_overlap_words", "分块前后文词数", "int", editable=True, depends_on=(("split.use_llm", True),), min=0, max=400, step=10,
                   help="只提供给相邻块用于理解，不直接写入正式产物；适当重叠可减少句子在边界处被截断"),
        ParamField("split.llm_chunk_max_retries", "每块最多尝试次数", "int", editable=True, depends_on=(("split.use_llm", True),), min=1, max=5, step=1,
                   help="单个分块发生格式、时间或静音校验失败时的最多请求次数；只重试失败块，不重做已通过的块"),
        ParamField("split.thinking_mode", "整理思考强度", "select", depends_on=(("split.use_llm", True),),
                   options=(("低思考强度（low，适配模型生效）", "low"), ("跟随自定义 JSON / 服务商默认", "follow_profile")),
                   help="适配模型：本次请求的 low 优先于自定义 JSON 的 reasoning_effort；GLM-5.3 官方接口保留 thinking，方舟已核实的套餐接口移除不兼容的 thinking。未适配模型保留 JSON，只由提示词要求尽快完成；不会修改全局参数"),
        ParamField("split.target_word_count", "语义目标词数（软参考）", "int", min=1, max=80, step=1,
                   depends_on=(("split.use_llm", True),),
                   help="参考范围内尽量形成长度适中、可连贯配音的语意；紧连的短碎片优先归并，有意义的独立短句保留。中文按自然语义词理解，不因超过目标机械拆分，也不设硬性最短限制"),
        ParamField("split.max_split_length", "本地单句长度（字符）", "int", min=8, max=80, step=1,
                   depends_on=(("split.use_llm", False),),
                   help="仅关闭 LLM 时用于本地断句；启用 LLM 时不使用此字符设置"),
        ParamField("split.max_segment_seconds", "目标单句时长（秒）", "float", min=4, max=20, step=0.5,
                   help="LLM 的字幕与配音节奏软参考，允许适度超出以保留完整意思；关闭 LLM 时用于本地时长兜底"),
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
        ParamField("translate.thinking_mode", "翻译思考强度", "select",
                   options=(("低思考强度（low，适配模型生效）", "low"), ("跟随自定义 JSON / 服务商默认", "follow_profile")),
                   help="适配模型：本次请求的 low 优先于自定义 JSON 的 reasoning_effort；GLM-5.3 官方接口保留 thinking，方舟已核实的套餐接口移除不兼容的 thinking。未适配模型保留 JSON，只由提示词要求尽快完成；不会修改全局参数"),
        ParamField("translate.max_context_tokens", "单次翻译上下文上限", "int", min=16000, max=1_000_000, step=10000,
                   help="token 预算优先；超过总预算或单窗行数安全上限时分窗口。预算包含提示词、交接摘要和完整输出预留。默认200000，需与模型真实容量匹配"),
        ParamField("translate.max_window_lines", "单次翻译最多字幕行数", "int", min=0, max=1000, step=10,
                   help="token 预算优先；为防止超大上下文导致响应变慢，默认每个窗口最多100条字幕行。0=关闭行数上限，仅按 token 自适应分窗；不会拆开单条字幕"),
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
                   help="利用句间空隙延长短字幕，遇下一句即停止；0=不延长。手动修改过时间的句子始终保持所设时间。预览会按显示宽度预先换行，播放器仍可继续自动排版"),
    ),
    "dubtask": (),  # 旧任务接口兼容；预估偏差标记已不用于当前编辑页。
    "tts": (
        ParamField("tts.backend", "TTS 后端", "select",
                   help="选择负责生成配音的服务；本机已安装的服务可在配音页快捷启动",
                   options=(("IndexTTS2.5", "indextts25"), ("GPT-SoVITS", "gptsovits"))),
        ParamField("tts.placement.enabled", "自适应排布配音", "bool", group="配音时间排布",
                   help="先在固定障碍之间用完整原速音频共同利用空位，每句仍受最大前后移动限制；原速确实放不下时，才按缩时分析窗口定位不足，IndexTTS 再按超长重试因子重新合成。原文、译文与双语字幕共用最终时间。源稿与原声参考切片保留；保存排布设置不会自动覆盖声音。"),
        ParamField("tts.placement.auto_repair", "自动修复配音时间冲突", "bool", group="配音时间排布",
                   depends_on=(("tts.backend", "indextts25"),),
                   help="先排布，再按缩短因子有限重试；关闭后只停止新旧配音的自动缩时，纯排布仍由「自适应排布配音」控制。旧声音修复沿用其生成参数并保留同槽历史，原声、静音和人工播放位置保留。此设置不使已有音频或成片过期，在下一次配音运行开始时生效。"),
        ParamField("tts.placement.max_shift_seconds", "最大前后移动（秒）", "float", min=0, max=15, step=0.1,
                   group="配音时间排布", depends_on=(("tts.placement.enabled", True),),
                   help="每句相对源起点允许前后移动的最大距离，默认 2 秒。相邻句子可以一起移动，但不会交换顺序、越过原声障碍或视频边界。0 表示固定起点；过大会让配音与画面动作错位。"),
        ParamField("tts.placement.window_lines", "缩时分析窗口（句）", "int", min=3, max=15, step=1,
                   group="配音时间排布", depends_on=(("tts.placement.enabled", True),),
                   help="整段完整原速排布仍不可行时，每个局部缩时分析窗口最多包含的相邻句数，默认 9 句，范围 3～15。原速阶段可跨此窗口共享空位，但每句移动始终受上方秒数限制，且不跨人工位置、原声、静音或缺音频障碍。此值不是全片可移动句数的上限。"),
        ParamField("tts.placement.inter_sentence_gap", "句间安全间隔（秒）", "float", min=0, max=1, step=0.01,
                   group="配音时间排布", depends_on=(("tts.placement.enabled", True),),
                   help="相邻配音之间预留的最小间隔，默认 0.08 秒，避免两句接得过紧。间隔会占用可用时间；设得过大会增加缩时重试。不会裁剪句尾或改写文字。"),
        ParamField("tts.indextts25.url", "IndexTTS 服务地址", "str", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：连接 IndexTTS 2.5 的 Gradio WebUI。\n建议：本机默认 http://127.0.0.1:7860/；远程服务填写实际地址。无需添加 /gen_single。"),
        ParamField("tts.indextts25.project_dir", "IndexTTS 安装目录", "str", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：快捷启动时定位服务环境。\n建议：填写包含 webui.py 的 index-tts 目录；留空自动查找同级目录。它不是模型文件或参考音频目录。"),
        ParamField("tts.indextts25.timbre_mode", "音色参考来源", "select", depends_on=(("tts.backend", "indextts25"),),
                   help="自定义音频使用指定文件；指定句原声固定参考所选源句，默认第一句，最多取 15 秒；本句原声在合成每句时使用该句自身的原声，并受本句原声截取上限限制。请选择清晰、单人且少背景音的参考。",
                   options=(("自定义音频", "preset"), ("指定句原声", "stable"),
                            ("本句原声", "dynamic"))),
        ParamField("tts.indextts25.timbre_audio", "音色参考文件", "str",
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.timbre_mode", "preset")),
                   help="自定义音色参考文件的完整本机路径，模型最多读取前 15 秒，不受本句原声截取上限影响。其他来源时保留文件路径备用。"),
        ParamField("tts.indextts25.timbre_reference_line_id", "音色参考句", "int", min=1, step=1,
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.timbre_mode", "stable")),
                   help="固定使用所选原句的人声作为音色参考，默认第 1 句；按源视频原始时间截取，最多 15 秒。"),
        ParamField("tts.indextts25.emo_mode", "情感来源", "select", depends_on=(("tts.backend", "indextts25"),),
                   help="沿用音色参考无需另一份音频；指定句原声固定参考所选源句，默认第一句；本句原声在合成每句时使用该句自身的原声。文本情感模式需在服务启动时加载情感模型。",
                   options=(("沿用音色参考", "same"), ("自定义情感音频", "preset"), ("指定句原声", "stable"),
                              ("本句原声", "dynamic"), ("情感向量", "vector"), ("文本情感（需加载模型）", "text"))),
        ParamField("tts.indextts25.emo_reference_line_id", "情感参考句", "int", min=1, step=1,
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "stable")),
                   help="固定使用所选原句的人声作为情感参考，可与音色参考分开选择；默认第 1 句，最多 15 秒。"),
        ParamField("tts.indextts25.emo_vector", "情感向量", "vector", min=0, max=1, step=0.05,
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "vector")),
                   help="用途：依次控制喜、怒、哀、惧、厌恶、低落、惊喜、平静。\n建议：先小幅增加一两项试听，没有适用于所有声音的最佳向量。各项不是概率，不要求总和为 1；服务端会调整和归一化。全零保留参考音频本身的情感，不保证中性。"),
        ParamField("tts.indextts25.emo_text", "情感描述", "text",
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "text")),
                   help="用途：文本情感模式下描述语气，例如“压低声音，带一点紧张”；留空时根据待合成文本推断情感。其他模式不会使用此描述。\n建议：描述简短、具体，情感强度从 0.6 或更低试听。服务须在启动时加载 QwenEmotion；切换后保存参数，并按当前模式重启服务（DubForge 自动添加 --qwen_emo）。"),
        ParamField("tts.indextts25.emo_random", "情感随机采样", "bool", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：在向量或文本模式下随机选择情感样本，增加表现差异。\n建议：视频配音保持关闭，以便音色更一致。音频参考模式不使用此项；它与高级参数的随机采样是不同开关。"),
        ParamField("tts.indextts25.emo_audio", "情感参考文件", "str",
                   depends_on=(("tts.backend", "indextts25"), ("tts.indextts25.emo_mode", "preset")),
                   help="用途：自定义情感来源使用此完整本机路径，提供语气而非替换音色。模型读取前 15 秒，不受本句原声截取上限影响。本句原声模式下文件保留为切片失败时的备用。\n建议：使用单人清晰、语气明确的片段，情感强度从 0.5～0.65 试听。与音色使用相同内容时，调节强度基本无效。"),
        ParamField("tts.indextts25.emo_weight", "情感强度", "float", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：控制独立情感参考或注入情感的强度。音频模式下 0 沿用音色参考情感，1 跟随独立情感参考；向量和文本模式下缩放额外情感。\n建议：独立音频从 0.5～0.65 试听；文本采用官方建议的 0.6 或更低。0 不代表消除所有情感。同音色模式及内容相同的双参考中，此值无法调节情感差异。",
                   min=0, max=1, step=0.05),
        ParamField("tts.indextts25.duration_factor", "合成时长因子", "float", depends_on=(("tts.backend", "indextts25"),), min=0.5, max=2, step=0.05,
                   help="用途：每次合成的起始时长因子，小于 1 倾向更快、更短，大于 1 更慢、更长。\n建议：先用 1.0，只对超长句逐步缩短并试听。它调整生成的声学长度，重跑结果不保证按严格比例缩短，也不保证精确匹配字幕。自动缩时不会修改此设置，实际值另行只读显示。"),
        ParamField("tts.indextts25.duration_retry_factors", "超长配音重试因子", "str", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：先按配音时间排布设置协调邻句、利用空位；仍无法容纳时，按顺序临时尝试更小的绝对因子，每次重新排布后再判断。留空只关闭模型缩时重试，不关闭移动。\n建议：可从 0.85, 0.7 渐进试听，避免直接大幅压缩。起始因子已为最低 0.5 时不再缩时。因子不是连续相乘；重试增加耗时，不保证严格按比例缩短，不会裁剪音频，也不会改写保存的设置。"),
        ParamField("tts.indextts25.dynamic_reference_max_seconds", "本句原声截取上限（秒）", "float", min=0.5, max=15, step=0.5,
                   depends_on=(("tts.backend", "indextts25"),),
                   help="仅限制本句原声模式，默认 5 秒；取本句时长与上限中的较短者，不向前后扩展。自定义文件和指定句原声不受此项限制。增加上限可能增加显存和耗时，模型最多读取 15 秒；保存后用于下次合成。"),
        ParamField("tts.indextts25.request_timeout_seconds", "单次配音超时（秒）", "int", min=0, max=86400, step=30,
                   group="运行保护", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：每次调用模型的等待上限，包含服务排队；与整批步骤超时独立。默认 600 秒。单句达到上限即停止本批，避免异常推理长期占用配音服务。运行中保存的新值从下一句/下一次请求边界生效，当前请求仍使用已开始时的值；0 关闭保护。增加超时不会加快合成。超时保留已有音频；服务尚未结束时不能继续堆积请求。"),
        ParamField("tts.indextts25.lang", "合成语言", "select", depends_on=(("tts.backend", "indextts25"),),
                   help="用途：明确待合成文本的语言；参考音频可以来自另一种语言。\n建议：中文配音选择简体中文（ZH），与译文语言保持一致。IndexTTS 2.5 支持中文、英语、日语、阿拉伯语和西班牙语。",
                   options=tuple((label, code) for code, label in tts_indextts25.LANGUAGES)),
        ParamField("tts.indextts25.do_sample", "语音随机采样（do_sample）", "bool", group="高级合成参数",
                   help="用途：开启后从候选中采样，关闭后由模型选择候选。\n建议：自然视频配音先保持开启，这是官方默认。关闭后温度、top_p、top_k 不生效，但整条生成链仍不保证完全确定；它不控制情感样本的随机选择。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.top_p", "采样概率范围（top_p）", "float", min=0, max=1, step=0.01, group="高级合成参数",
                   help="用途：采样时保留累计概率达到此值的候选，调低会缩小候选范围。\n建议：先保持官方默认 0.8，每次只调整一项并试听。仅随机采样开启时生效，更低不保证更自然。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.top_k", "采样候选数量（top_k）", "int", min=0, max=100, group="高级合成参数",
                   help="用途：采样时只考虑概率最高的 K 个候选；0 表示不限制数量，与 top_p 同时生效。\n建议：先保持官方默认 30。仅随机采样开启时生效；调小会缩小候选范围，不保证质量提升。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.temperature", "采样温度（temperature）", "float", min=0.1, max=2, step=0.1, group="高级合成参数",
                   help="用途：控制采样分布的集中程度，较高通常有更多变化。\n建议：先用官方默认 0.8；重跑变化太大可单独试 0.7，再比较自然度和发音。仅随机采样开启时生效，越低不代表越好。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.length_penalty", "序列长度偏好（length_penalty）", "float", min=-2, max=2, step=0.1, group="高级合成参数",
                   help="用途：调整束搜索对序列长度的评分；正值倾向较长，负值倾向较短。\n建议：保持官方默认 0。只有多束搜索时才有意义；它不是语速控制，超长配音优先检查文本和时长因子。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.num_beams", "搜索分支数（num_beams）", "int", min=1, max=10, group="高级合成参数",
                   help="用途：同时保留的候选生成序列数量。\n建议：先保持官方默认 3；需要提速可单独试 1，对相同字幕比较耗时、漏读、重复和音色。增加通常更耗显存和时间，不保证质量更好。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.repetition_penalty", "语音重复惩罚（repetition_penalty）", "float", min=0.1, max=20, step=0.1, group="高级合成参数",
                   help="用途：抑制生成语音 token 的重复；1 表示不额外惩罚。\n建议：保留 IndexTTS 官方默认 10，不套用文字 LLM 的 1.1 等惯例。它不是逐字去重开关；异常复读还需检查文本、参考音频和分段。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.max_mel_tokens", "每段语音 token 上限（max_mel_tokens）", "int", min=50, step=10, group="高级合成参数",
                   help="用途：单次生成的音频 token 上限，不是音频秒数。\n建议：先保持官方默认 1500。过小可能截断，调高不直接提升音质；不可超过当前模型支持的上限。遇到长段末尾异常先检查内部文本分段。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.indextts25.max_text_tokens_per_segment", "内部分段 token 上限（max_text_tokens_per_segment）", "int", min=20, step=2, group="高级合成参数",
                   help="用途：IndexTTS 内部每段文本的 token 上限，不改变 DubForge 的字幕句子、时间和标号。\n建议：中文字幕短句先保持官方默认 120；长段（尤其英文）末尾乱读时可减小再试听。token 不是字符或词组，太小会切碎语音、增加停顿，低显存服务还可能另按字符分段。", depends_on=(("tts.backend", "indextts25"),)),
        ParamField("tts.gptsovits.url", "GPT-SoVITS 地址", "str", depends_on=(("tts.backend", "gptsovits"),), help="GPT-SoVITS api_v2 服务地址，默认 http://127.0.0.1:9880/；不是训练 WebUI 地址。"),
        ParamField("tts.gptsovits.project_dir", "GPT-SoVITS 安装目录", "str", depends_on=(("tts.backend", "gptsovits"),),
                   help="快捷启动使用；填写含 api_v2.py 的目录，留空查找程序目录或同级的 GPT-SoVITS* 目录；其他位置请填写实际路径"),
        ParamField("tts.gptsovits.character", "GPT-SoVITS 角色", "str", depends_on=(("tts.backend", "gptsovits"),), help="保留旧整合包角色配置；当前 api_v2 的音色与模型主要由服务端设置，此字段不会自动切换模型。"),
    ),
    "assemble": (
        ParamField('assemble.voice_volume', '人声音量', 'float', min=0, max=1, step=0.05, group='人声与背景平衡',
                   help='作用：调整配音相对背景的突出程度；1为原音量，0为人声静音。\n建议：先保持1，背景盖住说话时优先降低背景或开启背景避让。最终统一响度共同调整整段混音，不会代替人声与背景比例调整。项目默认与版本专用值分别显示来源；播放器总音量只影响本机监听。'),
        ParamField("assemble.subtitle_track", "烧录字幕类型", "select",
                   help="烧录效果预览及带字幕成品使用此类型；播放器的试听字幕可单独切换。正式烧录前需导出对应文件",
                   options=(("目标语言", "trans"), ("原文", "source"), ("双语", "bilingual"))),
        ParamField("assemble.bgm_volume", "背景音音量", "float", min=0, max=1, step=0.05, group='人声与背景平衡',
                   help="作用：调整已分离背景音在配音成片中的比例；0为关闭背景，1为保留原电平。\n建议：先试听有人声与没人声的相邻片段，保证台词清楚并保留环境感；不要为追求综合响度单独抬高背景。仅配音版生效，原声版保留源混音。"),
        ParamField("assemble.ducking", "人声压低背景 (ducking)", "bool", group='人声与背景平衡',
                   help="作用：说话时自动降低背景，停顿时让背景恢复。\n建议：讲解或背景音乐较突出时开启，若出现明显忽强忽弱可关闭并调低背景比例。只作用于正式配音混音，快速逐句试听不模拟；生成试片可核对实际效果。"),
        ParamField("assemble.loudnorm", "配音人声响度归一", "bool", group='人声与背景平衡',
                   help="先测量完整人声轨，再用统一静态增益接近 −16 LUFS，并保留真峰值空间与语气起伏。只处理混入背景前的人声，不决定最终混音响度；关闭时沿用 −3 dB 峰值归一。试片使用完整人声轨的相同调整。"),
        ParamField('assemble.sentence_leveling', '保守均衡句间音量', 'bool', group='人声与背景平衡',
                   help='仅修正明显的句间能量差异，保留约 ±2 dB 的语气变化，不放大静音；不重生成配音。人工逐句音量修正与此叠加。原声版本不适用。'),
        ParamField('assemble.sentence_leveling_max_db', '句间均衡最大修正（dB）', 'float', min=0, max=6, step=.5, group='人声与背景平衡',
                   depends_on=(('assemble.sentence_leveling', True),), help='每句最多增减此幅度。建议 3 dB；保守算法不会强行让每句一样响。'),
        ParamField('assemble.final_loudness_mode', '最终成片响度', 'select', group='统一输出响度',
                   options=(('保持混音音量', 'off'), ('保留动态（仅固定增益）', 'normalize'), ('统一响度（推荐，必要时控制峰值）', 'uniform')),
                   help='作用：统一不同项目最终成片的总体音量，原片只用于对照，不决定目标。\n建议：跨项目一致优先选“统一响度”，能静态调整时保留动态，峰值不足时对完整音轨进行动态控制并实测；“保留动态”只作整轨固定增益，可能达不到目标；关闭则保持混音。试片读取同一整片处理结果，不单独放大安静段。已有项目和版本专用值保留，修改后需重新生成成片。'),
        ParamField('assemble.final_loudness_lufs', '成片目标响度（LUFS）', 'float', min=-30, max=-9, step=.5, group='统一输出响度',
                   depends_on=(('assemble.final_loudness_mode', ('normalize', 'uniform')),), help='含义：整片的总体听感音量，负值越接近0越响，与瞬间峰值上限不同。\n建议：−16 LUFS是较保守的产品默认，不是B站官方标准；若试听偏轻可尝试−14 LUFS，比−16高2 LU。保留动态模式可能受峰值限制而无法达到目标。跨项目应在全局设置同一目标，并显式应用到已有项目；项目或版本覆盖仍优先。短试片不要求命中整片目标，数值也不能判断人声是否被背景盖住。'),
        ParamField('assemble.final_true_peak_db', '成片真峰值上限（dBTP）', 'float', min=-6, max=0, step=.1, group='统一输出响度',
                   depends_on=(('assemble.final_loudness_mode', ('normalize', 'uniform')),), help='含义：包含采样点之间瞬态峰值的上限，与整片总体响度不同；提高上限不等于整体按同样幅度变响。\n建议：默认−1.5 dBTP，允许最高0；高于−1时有损编码及平台转码余量较小。实际峰值低于上限很正常，无需刻意顶满。统一响度模式必要时控制峰值；保留动态模式只作固定增益，峰值余量不足时限制放大，也会按更低目标衰减。两种模式都预留编码余量，并在AAC编码后重新核对。'),
        ParamField("assemble.burn_subtitles", "流程默认烧录字幕", "bool",
                   help="仅决定总览批量流程的默认配音版是否烧录；成片页的正式成品与试片按当前明确选择的组合运行。烧录需要重新编码，SRT 可单独导出"),
        ParamField("assemble.subtitle_font", "译文字体（通用）", "select", editable=True,
                   options=(("微软雅黑", "Microsoft YaHei"), ("黑体", "SimHei"), ("宋体", "SimSun"), ("思源黑体", "Source Han Sans SC"), ("Arial", "Arial")),
                   help="可输入系统已安装的字体名称；缺字或字体不存在时由渲染器回退，请用烧录效果预览确认"),
        ParamField("assemble.subtitle_size", "译文字号（通用）", "int", min=16, max=120, step=2,
                   help="按 1080 高画面设定，随视频尺寸等比例缩放；开启长字幕自适应后，此值为字号上限。原文未开启独立样式时也跟随此值。"),
        ParamField("assemble.subtitle_auto_fit", "长字幕自适应字号", "bool",
                   help="字幕超过缩小目标宽度时优先缩小字号；到最小字号后，可利用缩小目标与换行上限之间的缓冲区，超过换行上限才换行。关闭后保持标准字号，仍按换行宽度自动换行。原文与译文分别排版，不要求相同的换行位置。字号按 1080 高画面设定。"),
        ParamField("assemble.subtitle_shrink_width_percent", "缩小目标宽度（画面 %）", "int", min=10, max=100, step=1,
                   depends_on=(("assemble.subtitle_auto_fit", True),),
                   help="占完整视频画面的宽度百分比，字幕居中；超过此宽度时尝试缩小到目标范围，默认 80%，不能大于换行上限。到最小字号后可伸入与换行上限之间的缓冲区，不会立即换行。原文与译文共用宽度，但各自排版；关闭自适应时不生效并保留设置。仍保留必要安全边距。"),
        ParamField("assemble.subtitle_wrap_width_percent", "换行上限宽度（画面 %）", "int", min=10, max=100, step=1,
                   help="占完整视频画面的宽度百分比，字幕居中；超过此宽度才自动换行，默认 90%。开启自适应时先缩小字号，达到最小字号后才使用缓冲区；关闭自适应仍按此宽度换行。原文与译文共用宽度，不强行使用相同换行位置。100% 仍保留必要安全边距。"),
        ParamField("assemble.subtitle_min_size", "译文最小字号（通用）", "int", min=16, max=120, step=2,
                   depends_on=(("assemble.subtitle_auto_fit", True),),
                   help="长字幕自适应时允许缩小到的下限，按 1080 高画面设定；上限为译文字号。下限大于标准字号时按标准字号处理。原文未开启独立样式时跟随此范围；达到下限后仍可使用宽度缓冲区，超过换行上限才换行。"),
        ParamField("assemble.subtitle_color", "译文文字颜色", "color", help="原文未开启独立样式时也使用此颜色"),
        ParamField("assemble.subtitle_outline_color", "译文描边颜色", "color", help="深色描边可提高字幕在明亮背景上的可读性"),
        ParamField("assemble.subtitle_outline", "译文描边粗细", "float", min=0, max=10, step=0.5,
                   help="按 1080 高画面设定；0 关闭描边"),
        ParamField("assemble.subtitle_background_color", "文字底色", "color", help="只影响文字后方的背景盒，不改变文字与描边颜色"),
        ParamField("assemble.subtitle_background_opacity", "底色不透明度（%）", "int", min=0, max=100, step=5,
                   help="0 完全透明，100 不透明；双语多行共用一块背景，预览与成片一致"),
        ParamField("assemble.subtitle_margin", "字幕底部距离", "int", min=0, max=360, step=5,
                   help="字幕距画面底边的距离，按 1080 高画面设定；双语整体向上堆叠"),
        ParamField("assemble.video_encoder", "视频编码方式", "select",
                   options=(("软件编码（H.264 / CPU）", "software"), ("NVIDIA 硬件编码（H.264 / NVENC）", "nvenc")),
                   help="只影响需要重新编码的普通完整成片；保留分辨率、帧率、字幕与评论。硬件不可用会明确报错，不自动改用软件。"),
        ParamField("assemble.crf", "软件画质 CRF", "int", min=14, max=28, step=1,
                   depends_on=(("assemble.video_encoder", "software"),),
                   help="越小越清晰、文件通常越大；沿用原有软件medium预设。与硬件CQ是独立刻度。"),
        ParamField("assemble.nvenc_cq", "硬件画质 CQ", "int", min=14, max=28, step=1,
                   depends_on=(("assemble.video_encoder", "nvenc"),),
                   help="NVENC独立画质刻度；越小通常越清晰。CQ18不等同CRF18，请先用试片核对画面与文字；切换方式不复制画质数值。"),
    ),
}


def source_style_fields(prefix, *, comments=False):
    """字幕、评论共用原文样式控件定义，接入相同的配置继承。"""
    enabled = prefix + 'source_style_enabled'
    depends = ((enabled, True),)
    fields = (
        ParamField(enabled, '原文独立样式', 'bool', help='关闭时原文跟随译文；开启后字体、字号、文字色与描边分别设置。位置和底色共用。'),
        ParamField(prefix + 'source_font', '原文字体', 'select', editable=True, options=(('微软雅黑', 'Microsoft YaHei'), ('Arial', 'Arial')), depends_on=depends),
        ParamField(prefix + ('source_font_size' if comments else 'source_size'), '原文最大字号' if comments else '原文字号', 'int', min=18 if comments else 16, max=72 if comments else 120, step=1, depends_on=depends,
                   help='按 1080 高画面设定；超出目标高度时，在原文字号范围内缩小。' if comments else '按 1080 高画面设定，随视频尺寸等比例缩放；开启长字幕自适应后，此值为原文字号上限。'),
    )
    if comments:
        fields += (
            ParamField(prefix + 'source_min_font_size', '原文最小字号', 'int', min=18, max=72, step=1,
                       depends_on=depends, help='不能大于原文最大字号；达到此下限后完整扩高，不再继续缩小或截断。'),
        )
    else:
        fields += (
            ParamField(prefix + 'source_min_size', '原文最小字号', 'int', min=16, max=120, step=2,
                       depends_on=depends + ((prefix + 'auto_fit', True),),
                       help='仅在原文独立样式和长字幕自适应均开启时生效。按 1080 高画面设定，上限为原文字号；下限大于标准字号时按标准字号处理。原文与译文各自缩小、换行，不要求相同的换行位置。'),
        )
    return fields + (
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
    ParamField('comments.overlay.font_size', '译文最大字号（通用）', 'float', min=18, max=72, step=1,
               help='按 1080 高画面设定；先使用最大字号，超出目标高度时才向最小字号收缩。原文未开启独立样式时跟随此范围。'),
    ParamField('comments.overlay.min_font_size', '译文最小字号（通用）', 'float', min=18, max=72, step=1,
               help='不能大于最大字号；达到此下限后允许扩高，完整显示文字。按 1080 高画面设定。'),
    ParamField('comments.overlay.margin', '距边缘', 'float', min=0, max=400, step=1),
    ParamField('comments.overlay.width_percent', '最大宽度（画面 %）', 'float', min=0, max=100, step=1,
               help='0 自动与 100% 均使用扣除左右边距后的可用宽度，短评论保持自然长度。1–99% 可进一步限制排版宽度；百分比相对于整个画面，仍受边距限制。'),
    ParamField('comments.overlay.height_percent', '目标最大高度（画面 %）', 'float', min=1, max=100, step=1,
               help='先按最大宽度换行，再在字号范围内缩小以接近此高度；达到最小字号后仍放不下则完整扩高，不截断文字。位置锚点和边距保持不变。'),
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


# 连接与运行保护继续共享；显式项目覆盖仍优先，不把凭据路径复制进项目基线。
RUNTIME_PARAM_PATHS = frozenset({
    'download.proxy', 'download.cookies_path',
    'tts.indextts25.url', 'tts.indextts25.project_dir', 'tts.indextts25.request_timeout_seconds',
    'tts.gptsovits.url', 'tts.gptsovits.project_dir', 'tts.gptsovits.request_timeout_seconds',
} | {f.path for fields in STEP_PARAM_SCHEMA.values() for f in fields
     if f.path.startswith('runner.step_timeout_seconds.')})


def validate_numeric_updates(updates: dict) -> None:
    """数值控件清空会提交 null；保存前统一校验，避免运行时才报转换错误。"""
    import math
    from dubforge.errors import ConfigError

    def finite_number(value):
        if type(value) not in (int, float):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            # JSON 的大整数仍可解析，但不能在有限浮点校验处变成内部异常。
            return False

    fields = {f.path: f for group in STEP_PARAM_SCHEMA.values() for f in group
              if f.type in {"int", "float"}}
    # 时长诊断仍使用此内部阈值；不是TTS合成速度控件，不在界面展示。
    fields['tts.tolerance'] = ParamField('tts.tolerance', '时长偏差容差', 'float', min=0, max=2)
    for key, value in updates.items():
        field = fields.get(key)
        if field and not finite_number(value):
            raise ConfigError(f"「{field.label}」不能为空，请填写有效数字",
                              suggestion="参数尚未保存；请填写数值，或在项目步骤页使用恢复默认。")
        if field:
            if (field.type == "int" and int(value) != value
                    or field.min is not None and value < field.min
                    or field.max is not None and value > field.max):
                raise ConfigError(f"「{field.label}」超出允许范围或不是整数，请按控件说明填写。")
    vector = updates.get("tts.indextts25.emo_vector", [0] * 8)
    if (not isinstance(vector, list) or len(vector) != 8
            or any(not finite_number(v) or not 0 <= v <= 1 for v in vector)):
        raise ConfigError("情感向量必须是 8 个 0～1 的有效数字。")
    for field in STEP_PARAM_SCHEMA["tts"]:
        if field.path not in updates:
            continue
        value = updates[field.path]
        if ((field.type == "bool" and type(value) is not bool)
                or (field.type == "select" and value not in [v for _, v in field.options])
                or (field.type in {"str", "text"} and not isinstance(value, str))):
            raise ConfigError(f"「{field.label}」的值无效，请重新选择或填写。")
    retry_key = 'tts.indextts25.duration_retry_factors'
    if retry_key in updates:
        from dubforge.pipeline.tts_duration import parse_factors
        parse_factors(updates[retry_key])
    from dubforge.pipeline.subtitle_style import validate_style
    validate_style(updates)
    from dubforge.pipeline.content_redaction import validate_settings as validate_review
    validate_review(updates)


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
    from dubforge.pipeline.text_llm import thinking_profiles
    thinking_pool = profiles
    if not profiles and settings.get('llm', {}).get('base_url'):
        legacy = settings['llm']
        thinking_pool = [dict(name='default', base_url=legacy['base_url'], model=legacy.get('model', ''))]
    thinking_summary = thinking_profiles(thinking_pool)
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
            if f.path in {'split.thinking_mode', 'translate.thinking_mode'}:
                fl[-1]['thinking_profiles'] = thinking_summary
        out[key] = {"fields": fl, "values": {f.path: values.get(f.path) for f in fields}}
    return out
