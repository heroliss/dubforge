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


STEPS: list[StepDef] = [
    StepDef("download", "获取视频",
            "粘贴 YouTube 等链接下载, 或导入本地视频; 全局去重归档",
            ("download",)),
    StepDef("preprocess", "音轨分离",
            "提取音轨; demucs 分离人声/背景音; 可选说话人分离",
            ("separate",)),
    StepDef("transcribe", "语音转文字",
            "faster-whisper 词级时间戳转录 (源语言 SRT)",
            ("asr",)),
    StepDef("split", "拆分句子",
            "NLP 粗切 + LLM 语义切句",
            ("split", "llm.model", "llm.base_url")),
    StepDef("translate", "翻译",
            "三档策略 (整篇直译/分块+全局审校/滚动摘要), 术语表+风格提示词",
            ("translate", "llm")),
    StepDef("subtitles", "生成字幕",
            "生成 源/译/双语 SRT, 行宽控制",
            ("translate.target_language", "tts.subtitles")),
    StepDef("dubtask", "配音计划",
            "时长估算与语速分析, 逐句配音任务表",
            ("tts",)),
    StepDef("tts", "逐句配音",
            "IndexTTS2.5 / GPT-SoVITS 逐句合成 + 精修工作台",
            ("tts",)),
    StepDef("assemble", "合成成片",
            "响度归一 + 背景混音(ducking) + 可选字幕烧录",
            ("assemble",)),
]

STEP_KEYS = [s.key for s in STEPS]
STEP_MAP = {s.key: s for s in STEPS}


def downstream_keys(step_key: str) -> list[str]:
    """返回 step_key 之后的所有步骤 key (重跑时用于标 stale)"""
    idx = STEP_KEYS.index(step_key)
    return STEP_KEYS[idx + 1:]
