# -*- coding: utf-8 -*-
"""
管线步骤注册表
==============
九个步骤的静态定义 (顺序/中文名/说明/配置指纹键)。
M1 起每个 key 对应 pipeline/steps.py 的实现函数;
UI 依据此表常驻渲染步骤卡 (吸取 VideoLingo "下一步后上一步按钮消失" 的教训)。

cfg_keys: 该步骤依赖的配置键 (点分路径, 支持前缀匹配段)。
输入指纹 = sha256(cfg_keys 对应配置快照 + 全部上游产物哈希)。
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
                     ("meta.json", "标题/时长/大小元数据"))),
    StepDef("preprocess", "音轨分离",
            "提取音轨; demucs 分离人声/背景音; 可选说话人分离",
            ("separate",), "audio",
            inputs=(("download", "video.mp4", "源视频 (提取音轨与分离输入)"),),
            outputs=(("raw.wav", "16kHz 单声道原始音轨 (转录输入)"),
                     ("vocal.wav", "分离人声 44.1kHz (配音参考源)"),
                     ("background.wav", "分离背景音 44.1kHz (成片混音)"),
                     ("separate.json", "分离元信息"))),
    StepDef("transcribe", "语音转文字",
            "faster-whisper 词级时间戳转录 (源语言 SRT)",
            ("asr",), "artifacts/transcribe",
            inputs=(("preprocess", "raw.wav", "16kHz 单声道音轨"),),
            outputs=(("src.srt", "源语言字幕 (逐句)"),
                     ("words.json", "词级时间戳 (拆句保时间轴)"),
                     ("transcribe.json", "语言/时长统计"))),
    StepDef("split", "拆分句子",
            "NLP 粗切 + LLM 语义切句",
            ("split", "llm.model", "llm.base_url"), "artifacts/split",
            inputs=(("transcribe", "src.srt", "源语言字幕"),
                    ("transcribe", "words.json", "词级时间戳")),
            outputs=(("split.json", "拆分结果 {id, text, start, end}"),)),
    StepDef("translate", "翻译",
            "三档策略 (整篇直译/分块+全局审校/滚动摘要), 术语表+风格提示词",
            ("translate", "llm"), "artifacts/translate",
            inputs=(("split", "split.json", "拆分后的源文行"),),
            outputs=(("translate.json", "译文 {id, src, dst}"),)),
    StepDef("subtitles", "生成字幕",
            "生成 源/译/双语 SRT, 行宽控制",
            ("translate.target_language", "tts.subtitles"), "srt",
            inputs=(("split", "split.json", "时间轴与原文"),
                    ("translate", "translate.json", "译文")),
            outputs=(("trans.srt", "目标语言字幕"),
                     ("bilingual.srt", "双语字幕 (译文上/原文下)"),
                     ("subtitles.json", "生成统计"))),
    StepDef("dubtask", "配音计划",
            "时长估算与语速分析, 逐句配音任务表",
            ("tts",), "artifacts/dubtask",
            inputs=(("split", "split.json", "逐句文本与时间轴"),
                    ("transcribe", "transcribe.json", "视频总时长")),
            outputs=(("dubtask.json", "逐句任务表 (时长/语速/间隙)"),)),
    StepDef("tts", "逐句配音",
            "IndexTTS2.5 / GPT-SoVITS 逐句合成 + 精修工作台",
            ("tts",), "artifacts/tts",
            inputs=(("dubtask", "dubtask.json", "逐句任务表"),
                    ("preprocess", "vocal.wav", "音色/情感参考源 (stable/dynamic)")),
            outputs=(("*_<句id>.wav", "逐句音频 (artifacts/tts/), 已合成的重跑自动复用"),)),
    StepDef("assemble", "合成成片",
            "响度归一 + 背景混音(ducking) + 可选字幕烧录",
            ("assemble",), "output",
            inputs=(("dubtask", "dubtask.json", "时间轴 (铺轨依据)"),
                    ("preprocess", "background.wav", "背景音 (混音)"),
                    ("download", "video.mp4", "源视频画面")),
            outputs=(("final_*.mp4", "成片 (output/, 按时间戳多版本并存)"),
                     ("dub_full.wav", "铺轨后的完整人声轨"))),
]

STEP_KEYS = [s.key for s in STEPS]
STEP_MAP = {s.key: s for s in STEPS}


def downstream_keys(step_key: str) -> list[str]:
    """返回 step_key 之后的所有步骤 key (重跑时用于标 stale)"""
    idx = STEP_KEYS.index(step_key)
    return STEP_KEYS[idx + 1:]
