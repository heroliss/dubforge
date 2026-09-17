# -*- coding: utf-8 -*-
"""
管线步骤注册表
==============
九个步骤的静态定义 (顺序/中文名/说明)。M1 起每个 key 对应 pipeline/ 下的实现函数;
UI 依据此表常驻渲染步骤卡 (吸取 VideoLingo "下一步后上一步按钮消失" 的教训)。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class StepDef:
    key: str
    label: str
    description: str


STEPS: list[StepDef] = [
    StepDef("download", "下载/导入", "粘贴 YouTube 等链接下载, 或导入本地视频; 全局去重归档"),
    StepDef("preprocess", "预处理", "提取音轨; demucs 分离人声/背景音; 可选说话人分离"),
    StepDef("transcribe", "转录", "faster-whisper 词级时间戳转录 (源语言 SRT)"),
    StepDef("split", "分句", "NLP 粗切 + LLM 语义切句"),
    StepDef("translate", "翻译", "三档策略 (整篇直译/分块+全局审校/滚动摘要), 术语表+风格提示词"),
    StepDef("subtitles", "字幕生成", "生成 源/译/双语 SRT, 行宽控制"),
    StepDef("dubtask", "配音任务", "时长估算与语速分析, 逐句配音任务表"),
    StepDef("tts", "逐句配音", "IndexTTS2.5 / GPT-SoVITS 逐句合成 + 精修工作台"),
    StepDef("assemble", "成片合成", "响度归一 + 背景混音(ducking) + 可选字幕烧录"),
]

STEP_KEYS = [s.key for s in STEPS]
STEP_MAP = {s.key: s for s in STEPS}


def downstream_keys(step_key: str) -> list[str]:
    """返回 step_key 之后的所有步骤 key (重跑时用于标 stale)"""
    idx = STEP_KEYS.index(step_key)
    return STEP_KEYS[idx + 1:]
