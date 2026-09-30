"""
管线步骤注册表
==============
可执行步骤的静态定义 (顺序/中文名/说明/配置指纹键)。
M1 起每个 key 对应 pipeline/steps.py 的实现函数;
UI 依据此表常驻渲染步骤卡 (吸取 VideoLingo "下一步后上一步按钮消失" 的教训)。

cfg_keys: 该步骤依赖的配置键 (点分路径, 支持前缀匹配段)。
输入指纹 = sha256(cfg_keys 对应配置快照 + 实际输入产物哈希；download 另含源输入快照)。
配置或上游任一变化 → 指纹变化 → 重跑时不会误跳过。
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StepDef:
    key: str
    label: str
    description: str
    cfg_keys: tuple[str, ...] = field(default=())
    dir: str = ""                  # 本步骤的主产物目录 (相对项目目录; 「在文件浏览器中打开」用)
    # 本步骤消费的上游产物 (step_key, 产物名, 用途说明) —— 步骤页「上游文件」清单
    inputs: tuple[tuple[str, str, str], ...] = field(default=())
    # 本步骤的计划产物 (产物名, 说明) —— 步骤页「计划产物」清单, 与实际产物对照
    outputs: tuple[tuple[str, str], ...] = field(default=())


STEPS: list[StepDef] = [
    StepDef("download", "获取视频",
            "粘贴 YouTube 等链接下载, 或导入本地视频; 全局去重归档",
            ("download",), "media",
            outputs=(("video.mp4", "源视频文件 (本地导入为原文件零拷贝引用)"),
                     ("meta.json", "标题/时长/大小元数据"),
                     ("video_info.json", "视频内容资料（标题/简介/章节/字幕语言/AI 概要/可选评论）"))),
    StepDef("preprocess", "音轨分离",
            "提取音轨; demucs 分离人声/背景音; 可选说话人分离",
            ("separate",), "audio",
            inputs=(("download", "video.mp4", "源视频 (提取音轨与分离输入)"),),
            outputs=(("raw.wav", "16kHz 单声道原始音轨 (转录输入)"),
                     ("vocal.wav", "分离人声 44.1kHz (配音参考源)"),
                     ("background.wav", "分离背景音 44.1kHz (成片混音)"),
                     ("separate.json", "分离元信息"))),
    StepDef("transcribe", "语音转文字",
            "faster-whisper 词级时间戳转录；先形成约 2 秒短语锚点和标点锚点，最终句子交给下一步整理",
            ("asr",), "artifacts/transcribe",
            inputs=(("preprocess", "raw.wav", "16kHz 单声道音轨"),),
            outputs=(("src.srt", "识别原稿（短语级，可直接下载）"),
                     ("words.json", "词级时间戳 (拆句保时间轴)"),
                     ("transcribe.json", "语言/时长统计"))),
    StepDef("split", "语义整理",
             "按选择的短语级/词级时间轴和停顿提示交给 LLM，合并或拆分为自然字幕句子并重新对齐时间",
            ("split", "llm", "asr.segmentation_mode"), "artifacts/split",
            inputs=(("transcribe", "src.srt", "源语言字幕"),
                    ("transcribe", "words.json", "词级时间戳")),
            outputs=(("split.txt", "编号时间轴协议文本 (序号. 开始-结束: 片段)"),)),
    StepDef("translate", "翻译",
            "翻译语义整理后的原文；超出上下文预算才分窗口，支持术语表与风格提示词",
            ("translate", "llm"), "artifacts/translate",
            inputs=(("split", "split.txt", "语义整理后的原文与时间轴"),),
            outputs=(("translate.txt", "编号译文协议文本 (序号. 译文)"),)),
    StepDef("subtitles", "字幕导出",
            "在字幕与配音页导出校对后的原文、译文或双语 SRT；仅原文不需要翻译，导出不需要配音",
            ("subtitles",), "srt",
            inputs=(("split", "split.txt", "语义整理后的原文与时间轴"),
                    ("translate", "translate.txt", "与原文逐句对应的译文")),
            outputs=(("source.srt", "语义整理后的原文字幕（保留识别原稿 src.srt）"),
                     ("trans.srt", "目标语言字幕"),
                     ("bilingual.srt", "双语字幕（上下顺序按设置）"),
                     ("subtitles.json", "生成统计"))),
    StepDef("dubtask", "配音计划",
            "使用译文和语义整理后的时间轴，估算时长与语速，生成句级配音任务表",
            ("tts.tolerance",), "artifacts/dubtask",
            inputs=(("split", "split.txt", "语义整理后的原文与时间轴"),
                    ("translate", "translate.txt", "与原文逐句对应的译文")),
            outputs=(("dubtask.json", "逐句任务表 (时长/语速/间隙)"),)),
    StepDef("tts", "字幕与配音",
            "校对双语文字和时间，导出字幕；按需合成配音并选择成片使用的声源",
            ("tts",), "artifacts/tts",
            inputs=(("split", "split.txt", "语义时间轴"),
                    ("translate", "translate.txt", "逐句译文"),
                    ("preprocess", "vocal.wav", "音色/情感参考源 (stable/dynamic)")),
            outputs=(("<句id>.wav", "逐句配音；更多版本为 <句id>_v<版本>.wav (artifacts/tts/)"),)),
    StepDef("assemble", "合成成片",
            "独立成片版本：原声/配音、字幕、评论及样式可分别覆盖，成功后替换同版本",
            ("assemble",), "output",
            inputs=(("split", "split.txt", "语义时间轴"),
                    ("translate", "translate.txt", "逐句译文"),
                    ("preprocess", "background.wav", "背景音 (混音)"),
                    ("download", "video.mp4", "源视频画面")),
            outputs=(("final_*.mp4", "正式成片版本（再次生成替换自身，旧组合文件兼容保留）"),
                     ("dub_full.wav", "铺轨后的完整人声轨"))),
]

# 字幕导出是编辑页内的独立操作；内部任务键保留，兼容已有工程和自动运行选择。
_order = ['download', 'preprocess', 'transcribe', 'split', 'translate', 'dubtask', 'tts', 'subtitles', 'assemble']
STEPS.sort(key=lambda step: _order.index(step.key))

# dubtask 仅保留旧接口兼容；任务准备和逐句试配已归入配音步骤。
PIPELINE_STEPS = [step for step in STEPS if step.key != "dubtask"]
PIPELINE_KEYS = [step.key for step in PIPELINE_STEPS]
STEP_KEYS = [step.key for step in STEPS]
STEP_MAP = {step.key: step for step in STEPS}
