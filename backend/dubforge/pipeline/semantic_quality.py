"""整理稿的时间门禁与内容复核提示；词形启发不作为整稿失败依据。"""
import re
import unicodedata


# 不把字幕目标长度或英文词形当作语法正确性的证明。
_SENTENCE_END = re.compile(r"[.!?。！？…][\"'’”）)\]]*\s*$")
_LATIN_WORDS = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")
_DEPENDENT_ENDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "from", "for",
    "with", "by", "about", "through", "into", "onto", "between", "among",
    "and", "or", "because", "although", "whether", "whose",
    "my", "your", "our", "their", "its",
}
_STRICT_DEPENDENT_ENDS = {
    "a", "an", "the", "my", "your", "our", "their", "its", "whose",
    "and", "or", "because", "although", "whether", "i",
}
_SHORT_RESPONSES = {
    "yes", "no", "ok", "okay", "oh", "ah", "hey", "hi", "hello", "bye",
    "wow", "huh", "thanks", "sorry", "right", "sure", "exactly", "great",
    "嗯", "哦", "啊", "好", "好的", "对", "是", "不是", "谢谢", "再见",
}


def _response_text(text: str) -> str:
    return "".join(ch for ch in text.casefold()
                   if unicodedata.category(ch)[0] in "LMN")


def _connected(left: dict, right: dict, pause: float) -> bool:
    gap = float(right["start"]) - float(left["end"])
    return -0.011 <= gap < pause - 0.001


def _density_units(text: str) -> int:
    """仅用于极端密度复核提示；文字权重不能证明实际语速有错。"""
    return sum(
        3 if "\u2e80" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
        or "\uac00" <= ch <= "\ud7af" else 1
        for ch in text if unicodedata.category(ch)[0] in "LMN")


def semantic_quality_issues(parts: list[dict], pause: float) -> list[str]:
    """只返回明确时间重叠诊断，不按词形、短时长或密度拒绝整理稿。

    时间在声学重新锚定之后检查；内容边界与密度疑点交给
    semantic_boundary_warnings 提示复核，格式和正时长由解析器负责。
    """
    issues: list[str] = []
    for i, part in enumerate(parts):
        text = str(part["text"]).strip()
        duration = float(part["end"]) - float(part["start"])
        previous = parts[i - 1] if i else None
        reasons: list[str] = []
        if previous is not None and float(part["start"]) < float(previous["end"]) - 0.011:
            reasons.append("与上一句时间重叠")
        if reasons:
            excerpt = text[:100].replace("\n", " ")
            issues.append(f"第{i + 1}句（{duration:.3f}秒，{excerpt!r}）：" + "；".join(reasons))
        if len(issues) >= 8:
            break
    return issues


def semantic_boundary_warnings(parts: list[dict], pause: float) -> list[str]:
    """给出有邻句和时长依据的内容疑点，明确不能据此确认分句错误。"""
    warnings = []
    for i, part in enumerate(parts):
        text = str(part["text"]).strip()
        duration = float(part["end"]) - float(part["start"])
        previous = parts[i - 1] if i else None
        following = parts[i + 1] if i + 1 < len(parts) else None
        terminal = bool(_SENTENCE_END.search(text))
        response = _response_text(text)
        short_response = response in _SHORT_RESPONSES or response.isnumeric()
        left_connected = (previous is not None and _connected(previous, part, pause)
                          and not _SENTENCE_END.search(str(previous["text"])))
        next_connected = following is not None and _connected(part, following, pause)
        reasons = []
        if (duration < 0.5 - 0.001 and not terminal and not short_response
                and (left_connected or (next_connected and not terminal))):
            reasons.append("不足0.5秒且紧连邻句，可能是词或半句，也可能是正常短表达")
        density = _density_units(text)
        if density > max(20, duration * 65):
            reasons.append(f"文字密度较高（{density}个加权单位/{duration:.3f}秒），"
                           "仅凭文字长度不能判断实际语速或时间有错")
        words = _LATIN_WORDS.findall(text)
        if next_connected and words:
            tail_word = words[-1].casefold()
            next_words = _LATIN_WORDS.findall(str(following["text"]))
            next_response = _response_text(str(following["text"]))
            short_tail = (0 < len(next_words) <= 2
                          and float(following["end"]) - float(following["start"]) < 1.0
                          and next_response not in _SHORT_RESPONSES
                          and not next_response.isnumeric())
            # 介词可以正常位于句尾；冠词等也可能来自口语中断，均只提示。
            dependent_tail = (tail_word in _STRICT_DEPENDENT_ENDS
                              and not (tail_word == "i" and terminal))
            if next_words and ((tail_word in _DEPENDENT_ENDS and short_tail) or dependent_tail):
                gap = float(following["start"]) - float(part["end"])
                reasons.append(f"句尾{tail_word!r}可能接续第{i + 2}句（间隔{gap:.3f}秒）")
        if not reasons:
            continue
        excerpt = text[:100].replace("\n", " ")
        warnings.append(f"第{i + 1}句（{duration:.3f}秒，{excerpt!r}）：" + "；".join(reasons)
                        + "；仅凭词形、短时长或文字密度不能确认错误，请结合上下文复核，"
                        "不据此修改文字或时间。")
        if len(warnings) >= 8:
            break
    return warnings


def semantic_pause_issues(parts: list[dict], words: list[dict], pause: float) -> list[str]:
    """只在文本可靠匹配词级锚点时调用，识别句内明显无声区。

    两秒及以上无台词区间独立于普通停顿阈值，不应被长字幕覆盖。
    与提示词和时间修复共用有声区间并集，不把重叠词误判成静音。
    只提出重组要求，不填时间、不把孤立词机械挂到邻句。
    """
    from dubforge.pipeline.speech_gaps import speech_gaps

    gaps = [(gap.start, gap.end) for gap in speech_gaps(words)]
    issues: list[str] = []
    if not gaps:
        return issues
    # 两个有序时间序列用游标扫描，长稿不会按句数乘以词数重复搜索。
    cursor = 0
    for i, part in enumerate(parts):
        start, end = float(part["start"]), float(part["end"])
        while cursor < len(gaps) and gaps[cursor][0] <= start + 0.011:
            cursor += 1
        position = cursor
        while position < len(gaps) and gaps[position][1] < end - 0.011:
            lo, hi = gaps[position]
            issues.append(f"第{i + 1}句跨越词级时间轴{lo:.2f}–{hi:.2f}秒"
                          f"的{hi - lo:.2f}秒长停顿，请保留真实无声区并重新分句")
            if len(issues) >= 8:
                return issues
            position += 1
    return issues
