"""从词级时间轴的有声区并集提取无台词区；它不等同于波形静音。"""
import math
from dataclasses import dataclass

LONG_SPEECH_GAP_SECONDS = 2.0
GAP_TIME_EPSILON = 1e-9


@dataclass(frozen=True)
class SpeechGap:
    """原输入索引保留给文字定位；boundary_index 为右侧首词之前的词数。"""

    start: float
    end: float
    left_index: int
    right_index: int
    boundary_index: int


def speech_gaps(words, threshold=LONG_SPEECH_GAP_SECONDS, *, cancel_check=None) -> list[SpeechGap]:
    """按有序词轴累计 max_end，避免嵌套/重叠词制造不存在的空档。

    输入包含待定位文字对应的全部词，索引不会因零时长词改变。零时长词
    不建立或延长有声区；恰好位于右边界的零词可随右侧文字，其余文字
    切口默认在下一有声词之前。空档内部零词的归属由文字修复调用方拒绝
    猜测，本函数只报告确定的无台词时间区间，不替这些词编造声学位置。
    """
    try:
        threshold = float(threshold)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("无台词区阈值必须是有限的正秒数") from exc
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError("无台词区阈值必须是有限的正秒数")
    if cancel_check:
        cancel_check()
    result, zero_words = [], []
    previous_start = covered_end = left_index = None
    for index, word in enumerate(words):
        if cancel_check and index % 64 == 0:
            cancel_check()
        try:
            start, end = float(word["start"]), float(word["end"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"第 {index + 1} 个词的时间轴无效，请核对语音识别结果") from exc
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start
                or (previous_start is not None and start < previous_start)):
            raise ValueError(f"第 {index + 1} 个词的时间轴无效，请核对语音识别结果")
        previous_start = start
        if end == start:
            zero_words.append((index, start))
            continue
        if covered_end is not None and start - covered_end >= threshold - GAP_TIME_EPSILON:
            boundary = next((i for i, time in zero_words if time == start), index)
            result.append(SpeechGap(covered_end, start, left_index, index, boundary))
        if covered_end is None or end > covered_end:
            covered_end, left_index = end, index
        zero_words.clear()
    return result
