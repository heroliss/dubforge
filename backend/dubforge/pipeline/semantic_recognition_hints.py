"""源ASR短项的识别证据提示；不裁决无声、不删词、不改变时间。"""
import math
import unicodedata
from bisect import bisect_left, bisect_right
from numbers import Real

MAX_RECOGNITION_HINTS = 24
_TIME_EPSILON = .002  # SRT 毫秒舍入，不扩大词的实际声学区间。


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _probability(value):
    value = _finite_number(value)
    return value if value is not None and 0 <= value <= 1 else None


def _text_key(text):
    """仅匹配原短语与原词文字；空白、标点和大小写不决定声学归属。"""
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(text)).casefold()
                   if unicodedata.category(ch)[0] in "LMN")


def recognition_hints(lines, words) -> dict[int, list[str]]:
    """给唯一归属、文字相符的1–3词源项少量复核提示。

    旧稿缺少概率保持未知；单项低概率或短时长都不构成提示。重复/重叠
    短语的词归属不明确时跳过，避免把邻句证据贴给本句。源数据始终不改。
    """
    timed = []
    for position, word in enumerate(words or []):
        if not isinstance(word, dict) or not isinstance(word.get("word"), str):
            continue
        start, end = _finite_number(word.get("start")), _finite_number(word.get("end"))
        if start is None or end is None or start < 0 or end < start:
            continue
        if _text_key(word["word"]):
            timed.append((start, end, position, word))
    timed.sort(key=lambda entry: (entry[0], entry[2]))
    starts = [entry[0] for entry in timed]
    candidates, owners, seen_indexes = [], {}, set()
    duplicate_indexes = set()
    for line in lines or []:
        index = getattr(line, "index", None)
        start = _finite_number(getattr(line, "start", None))
        end = _finite_number(getattr(line, "end", None))
        if (not isinstance(index, int) or isinstance(index, bool) or index <= 0
                or start is None or end is None or start < 0 or end < start):
            continue
        if index in seen_indexes:
            duplicate_indexes.add(index)
        seen_indexes.add(index)
        matches = [entry for entry in timed[
            bisect_left(starts, start - _TIME_EPSILON):
            bisect_right(starts, end + _TIME_EPSILON)]
            if entry[1] <= end + _TIME_EPSILON]
        for entry in matches:
            owners[entry[2]] = owners.get(entry[2], 0) + 1
        candidates.append((line, matches))

    result, count = {}, 0
    for line, matches in candidates:
        if line.index in duplicate_indexes or not 1 <= len(matches) <= 3:
            continue
        if any(owners[entry[2]] != 1 for entry in matches):
            continue
        matches.sort(key=lambda entry: entry[2])
        if _text_key(line.text) != _text_key("".join(entry[3]["word"] for entry in matches)):
            continue
        hints = []
        if all(start == end for start, end, _, _ in matches):
            hints.append("原始词轴全部为零时长，不能据此确定独立语音时间；"
                         "仅作识别参考，请结合上下文复核，不能单凭这项信息删除文字或补造时间。")
        evidence = [(_probability(entry[3].get("probability")),
                     _probability(entry[3].get("no_speech_prob"))) for entry in matches]
        if all(prob is not None and prob < .35 and no_speech is not None and no_speech > .6
               for prob, no_speech in evidence):
            hints.append("原始词识别概率偏低，且所在ASR片段的无语音概率偏高；"
                         "两项仅为联合识别疑点，不能确认无声或误词，请结合上下文复核。")
        if hints:
            kept = hints[:MAX_RECOGNITION_HINTS - count]
            result[line.index] = kept
            count += len(kept)
        if count >= MAX_RECOGNITION_HINTS:
            break
    return result
