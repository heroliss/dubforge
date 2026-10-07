"""依据可靠词序切开长无声区，保留模型纠错后的全部文字。"""
import difflib
import math
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter

from .speech_gaps import speech_gaps


_LOOKAHEAD = 32
_LOCAL_UNITS = 3
_LOCAL_CHARS = 4_000


class PauseAlignmentError(ValueError):
    """有长停顿但无法证明文字切口，不能猜测归属或覆盖无声区。"""


def _normalized(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if unicodedata.category(char)[0] in "LMN")


def _cjk(char):
    return ("\u2e80" <= char <= "\u9fff" or "\u3040" <= char <= "\u30ff"
            or "\uac00" <= char <= "\ud7af")


def _text_data(text):
    """归一字串、原字符切片位置和词单位；绝不重新生成候选文字。"""
    chars, spans, units, word = [], [], [], []

    def flush():
        if word:
            units.append("".join(word))
            word.clear()

    i = 0
    while i < len(text):
        end = i + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        value = unicodedata.normalize("NFKC", text[i:end]).casefold()
        for char in value:
            if unicodedata.category(char)[0] in "LMN":
                chars.append(char)
                spans.append((i, end))
                if _cjk(char):
                    flush()
                    units.append(char)
                else:
                    word.append(char)
            elif char not in "'’":
                flush()
        i = end
    flush()
    normalized = "".join(chars)
    if normalized != _normalized(text):
        raise PauseAlignmentError("长停顿附近的文字归一无法可靠定位，请核对该处文字和词级时间轴")
    return normalized, spans, units


def _blocks(source_units, target_units, cancel_check):
    """固定前向范围匹配；长稿不进入全文字符 diff。"""
    source_offsets, target_offsets = [0], [0]
    for value in source_units:
        source_offsets.append(source_offsets[-1] + len(value))
    for value in target_units:
        target_offsets.append(target_offsets[-1] + len(value))
    result = []
    i = j = 0
    while i < len(source_units) or j < len(target_units):
        if cancel_check and (i + j) % 64 == 0:
            cancel_check()
        si, tj = i, j
        if i == len(source_units):
            j = len(target_units)
        elif j == len(target_units):
            i = len(source_units)
        elif source_units[i] == target_units[j]:
            i, j = i + 1, j + 1
        else:
            # 粘词、拆词先证明归一字符完全一致，再查少量有上下文锚点的纠错。
            left, right = source_units[i], target_units[j]
            ii, jj = i + 1, j + 1
            while left != right:
                if left.startswith(right) and jj < min(len(target_units), j + _LOOKAHEAD):
                    right += target_units[jj]
                    jj += 1
                elif right.startswith(left) and ii < min(len(source_units), i + _LOOKAHEAD):
                    left += source_units[ii]
                    ii += 1
                else:
                    break
            if left == right:
                i, j = ii, jj
            else:
                positions = {}
                for offset, value in enumerate(target_units[j:j + _LOOKAHEAD]):
                    positions.setdefault(value, offset)
                anchors = [(offset + positions[value], offset, positions[value])
                           for offset, value in enumerate(source_units[i:i + _LOOKAHEAD])
                           if value in positions]
                if anchors:
                    _, skipped_source, skipped_target = min(anchors)
                    i, j = i + skipped_source, j + skipped_target
                else:
                    i, j = i + 1, j + 1
        original = "".join(source_units[si:i])
        candidate = "".join(target_units[tj:j])
        exact = original == candidate
        known = exact or (i - si <= _LOCAL_UNITS and j - tj <= _LOCAL_UNITS
                          and max(len(original), len(candidate)) <= _LOCAL_CHARS
                          and (not original or not candidate or difflib.SequenceMatcher(
                              a=original, b=candidate, autojunk=False).ratio() >= .5))
        result.append((source_offsets[si], source_offsets[i],
                       target_offsets[tj], target_offsets[j], exact, known))
    return result


def _project(blocks, position, *, ending):
    eligible = [block for block in blocks if block[2] <= position <= block[3]
                and block[3] > block[2]]
    if not eligible:
        raise PauseAlignmentError("长停顿附近缺少可验证的声学文字锚点，请核对该处原文")
    block = eligible[0] if ending else eligible[-1]
    s0, s1, t0, t1, exact, known = block
    if not known or s1 <= s0:
        raise PauseAlignmentError("长停顿附近的纠错文字无法可靠映射到原词，请核对该处原文")
    return s0 + (s1 - s0) * (position - t0) / (t1 - t0)


def _exact_project(blocks, position, *, ending):
    """全文重锚只接受当前一侧的精确文字锚点，不在纠错块内按比例猜测。"""
    eligible = [block for block in blocks if block[2] <= position <= block[3]
                and block[3] > block[2]]
    if not eligible:
        return None
    s0, s1, t0, t1, exact, _ = eligible[0] if ending else eligible[-1]
    if not exact or s1 - s0 != t1 - t0:
        return None
    return s0 + position - t0


def _gap_cut(blocks, position):
    choices = []
    for s0, s1, t0, t1, exact, known in blocks:
        if s0 == s1 == position and t1 > t0:
            raise PauseAlignmentError("长停顿两侧有无法确定归属的新增文字，请核对该处原文")
        if s0 < position < s1:
            if t0 == t1:
                return t0
            if exact:
                return t0 + position - s0
            raise PauseAlignmentError("长停顿切口位于无法确定归属的纠错区，请核对该处原文")
        if s1 == position:
            choices.append(t1)
        if s0 == position:
            choices.append(t0)
    if choices and len(set(choices)) == 1:
        return choices[0]
    raise PauseAlignmentError("长停顿缺少唯一的文字切口，请核对该处词级时间轴")


def _separated_insert_gap(blocks, position, lo, hi, data):
    """短新增词已在分句一侧，且模型保留真实空档时，不强行判定其声学归属。"""
    inserts = [block for block in blocks
               if block[0] == block[1] == position and block[3] > block[2]]
    if len(inserts) != 1:
        return None
    _, _, first, last, _, known = inserts[0]
    if (not known or last - first > 32
            or _exact_project(blocks, first, ending=True) != position
            or _exact_project(blocks, last, ending=False) != position):
        return None
    tolerance = .011
    # 原空档内已有候选文字时，仍须证明切口，不能用相邻行掩盖跨静音的片段。
    for part, *_ in data:
        start, end = float(part["start"]), float(part["end"])
        if not math.isfinite(start + end) or end <= start:
            return None
        if start < hi - tolerance and end > lo + tolerance:
            return None
    for index, (left, right) in enumerate(zip(data, data[1:])):
        boundary = left[4]
        if (boundary != right[3] or boundary not in {first, last}
                or left[3] == left[4] or right[3] == right[4]):
            continue
        if (abs(float(left[0]["end"]) - lo) <= tolerance
                and abs(float(right[0]["start"]) - hi) <= tolerance):
            return index, index + 1
    return None


def _separated_corrected_gap(blocks, position, lo, hi, data, valid_words, word_ends, source_text):
    """已分句的纠错只验证声学区域，不把文字相似度当作纠错正确与否的裁决。"""
    tolerance = .011
    previous_end = None
    for part, *_ in data:
        start, end = float(part["start"]), float(part["end"])
        if (not math.isfinite(start + end) or end <= start
                or (previous_end is not None and start < previous_end - tolerance)
                or (start < hi - tolerance and end > lo + tolerance)):
            return None
        previous_end = end

    word_spans = {(first, last): source_text[first:last]
                  for first, last in zip([0, *word_ends], word_ends)}
    word_counts = Counter(word_spans.values())

    def region(item, *, left_side):
        part, _, _, first, last = item
        if first == last:
            return None
        starts = [index for index, word in enumerate(valid_words)
                  if abs(word["start"] - float(part["start"])) <= tolerance]
        ends = [index for index, word in enumerate(valid_words)
                if abs(word["end"] - float(part["end"])) <= tolerance]
        if not starts or not ends:
            return None
        first_word, last_word = starts[0], ends[-1]
        if first_word > last_word or not any(
                word["end"] > word["start"] for word in valid_words[first_word:last_word + 1]):
            return None
        source_first = word_ends[first_word - 1] if first_word else 0
        source_last = word_ends[last_word]
        # 同一停顿侧的相邻词错位仍可由正常重锚修复，不能当成跨声学区移词。
        # 重复词及拼合词块的 diff 位置也不是唯一证据；只有跨当前停顿的
        # 唯一完整源词才足以拒绝，否则保留已确认时间区域并提示复核。
        for s0, s1, t0, t1, exact, _ in blocks:
            a, b = max(first, t0), min(last, t1)
            if exact and a < b and (s0 + a - t0 < source_first
                                    or s0 + b - t0 > source_last):
                crosses_gap = s0 + b - t0 > position if left_side else s0 + a - t0 < position
                if not crosses_gap:
                    return None
                anchor = word_spans.get((s0, s1))
                if anchor is not None and word_counts[anchor] == 1 and a == t0 and b == t1:
                    raise PauseAlignmentError("长停顿两侧的唯一文字锚点与候选时间对应区域不符，"
                                              "请核对该处文字与时间")
        return source_first, source_last

    for index, (left, right) in enumerate(zip(data, data[1:])):
        if (abs(float(left[0]["end"]) - lo) > tolerance
                or abs(float(right[0]["start"]) - hi) > tolerance):
            continue
        left_region = region(left, left_side=True)
        right_region = region(right, left_side=False)
        if left_region is None or right_region is None:
            continue
        if left_region[1] > right_region[0]:
            continue
        if (source_text[slice(*left_region)] == _normalized(left[1])
                and source_text[slice(*right_region)] == _normalized(right[1])):
            continue
        return index, index + 1
    return None


def _raw_cut(text, spans, position):
    left_end, right_start = spans[position - 1][1], spans[position][0]
    if left_end > right_start:
        raise PauseAlignmentError("长停顿切口落在不可拆分字符内部，请核对该处原文")
    # 闭合标点与空白归左，开括号/开引号仍跟随下一片；切片拼回逐字符不变。
    for i in range(left_end, right_start):
        if unicodedata.category(text[i]) in {"Ps", "Pi"}:
            return i
    return right_start


def repair_long_pauses(parts, words, pause, *, cancel_check=None, align_all=False,
                       warnings=None):
    """只修复有原词锚点的长静音；模型时间错误不能限制查找真实声源词。

    少量纠错使用单调、固定前向匹配。切口不能穿过含义归属不明的替换块，
    无法证明时保持 typed quality 错误；既不删除半句，也不拿 ASR 覆盖纠错稿。
    align_all 还将两端都有精确文字及源词边界的片段重锚到真实时间；模糊边界
    保留模型时间，交后续校验，不为新增、替换或单个零时长词编造声学位置。
    """
    if cancel_check:
        cancel_check()
    if not parts or not words:
        return [dict(part) for part in parts]
    valid_words, source_units, word_ends = [], [], []
    source_length = 0
    for word_index, word in enumerate(words):
        if cancel_check and word_index % 64 == 0:
            cancel_check()
        value, _, units = _text_data(str(word.get("word", "")))
        if not value:
            continue
        start, end = float(word["start"]), float(word["end"])
        # Whisper 偶有零时长词：文字和顺序仍保留，只是不为它编造独立语音时间。
        if not math.isfinite(start + end) or end < start or (
                valid_words and start < valid_words[-1]["start"]):
            raise PauseAlignmentError("词级时间轴无效，无法可靠处理长停顿，请先核对语音识别结果")
        valid_words.append(dict(start=start, end=end))
        source_units.extend(units)
        source_length += len(value)
        word_ends.append(source_length)
    try:
        acoustic_gaps = speech_gaps(valid_words, cancel_check=cancel_check)
    except ValueError as exc:
        raise PauseAlignmentError(str(exc)) from exc
    gaps = []
    for gap in acoustic_gaps:
        # 无声区内部的零时长词没有可证明的归属，不能贴到任一侧编造时间。
        if any(word["start"] == word["end"] and gap.start < word["start"] < gap.end
               for word in valid_words[gap.left_index + 1:gap.right_index]):
            raise PauseAlignmentError("长停顿内部的零时长词没有有效语音时间，"
                                      "无法确定文字归属，请核对该处词级时间轴")
        gaps.append((word_ends[gap.boundary_index - 1], gap.start, gap.end))
    if not gaps and not align_all:
        return [dict(part) for part in parts]
    if not valid_words:
        return [dict(part) for part in parts]
    target_units, data, target_length = [], [], 0
    for part_index, part in enumerate(parts):
        if cancel_check and part_index % 64 == 0:
            cancel_check()
        text = str(part["text"])
        normalized, spans, units = _text_data(text)
        data.append((part, text, spans, target_length, target_length + len(normalized)))
        target_length += len(normalized)
        target_units.extend(units)
    if "".join(source_units) == "".join(target_units):
        blocks = [(0, source_length, 0, target_length, True, True)]
    else:
        blocks = _blocks(source_units, target_units, cancel_check)
    cuts, keep_model_times = [], set()
    warning_count = 0
    source_text = "".join(source_units)
    for position, lo, hi in gaps:
        if cancel_check:
            cancel_check()
        separated = _separated_corrected_gap(
            blocks, position, lo, hi, data, valid_words, word_ends, source_text)
        warning_detail = "纠错文字与原词不同，声学对应位置需复核"
        if (separated is not None
                and _separated_insert_gap(blocks, position, lo, hi, data) == separated):
            warning_detail = "新增短词声学归属不确定"
        if separated is not None:
            keep_model_times.update(separated)
            if warnings is not None and warning_count < 8:
                warnings.append(f"第 {separated[0] + 1}、{separated[1] + 1} 句长停顿边界的"
                                f"{warning_detail}，已保留分句与真实空档，请复核")
                warning_count += 1
            continue
        cuts.append((_gap_cut(blocks, position), lo, hi))
    word_boundaries = {0, *word_ends}

    def time_at(position, *, ending):
        if position >= source_length:
            last_word = valid_words[-1]
            return last_word["end"] if last_word["end"] > last_word["start"] else None
        index = bisect_left(word_ends, position) if ending else bisect_right(word_ends, position)
        index = min(index, len(valid_words) - 1)
        previous = word_ends[index - 1] if index else 0
        word = valid_words[index]
        if word["end"] == word["start"]:
            return None
        return word["start"] + (word["end"] - word["start"]) * (
            position - previous) / (word_ends[index] - previous)

    result, optional_reanchors = [], {}
    for part_index, (part, text, spans, first, last) in enumerate(data):
        if cancel_check:
            cancel_check()
        affected = [(cut, lo, hi) for cut, lo, hi in cuts if first <= cut <= last]
        if not affected:
            aligned = dict(part)
            if align_all and first < last and part_index not in keep_model_times:
                source_start = _exact_project(blocks, first, ending=False)
                source_end = _exact_project(blocks, last, ending=True)
                if (source_start in word_boundaries and source_end in word_boundaries
                        and source_start < source_end):
                    start = time_at(source_start, ending=False)
                    end = time_at(source_end, ending=True)
                    # 已有源词微小重叠仍交下游处理；绝不引入倒序起点或零时长。
                    if (start is not None and end is not None
                            and math.isfinite(start + end) and end > start
                            and (not result or start >= float(result[-1]["start"]))):
                        aligned.update(start=start, end=end)
                        optional_reanchors[len(result)] = dict(part)
            result.append(aligned)
            continue
        if first == last:
            raise PauseAlignmentError("长停顿附近存在没有声源词的空片段，请核对该处原文")
        interior = sorted(set(cut for cut, _, _ in affected if first < cut < last))
        normalized_cuts = [first, *interior, last]
        raw_cuts = [0, *[_raw_cut(text, spans, cut - first) for cut in interior], len(text)]
        for i, (a, b) in enumerate(zip(normalized_cuts, normalized_cuts[1:])):
            start = time_at(_project(blocks, a, ending=False), ending=False)
            end = time_at(_project(blocks, b, ending=True), ending=True)
            # 并集端点可能由更早的嵌套词提供，不能退回相邻短词的 end。
            starts = {hi for cut, _, hi in affected if cut == a}
            ends = {lo for cut, lo, _ in affected if cut == b}
            if len(starts) > 1 or len(ends) > 1:
                raise PauseAlignmentError("长停顿文字切口对应多个声学边界，请核对该处原文")
            if starts:
                start = starts.pop()
            if ends:
                end = ends.pop()
            # 片段外缘只有零时长词时保留合法候选时间，不凭该词扩大有声区。
            if start is None:
                start = float(part["start"])
            if end is None:
                end = float(part["end"])
            if not math.isfinite(start + end) or end <= start:
                raise PauseAlignmentError("长停顿修复后的片段没有有效语音时间，请核对该处原文")
            result.append(dict(part, text=text[raw_cuts[i]:raw_cuts[i + 1]], start=start, end=end))
    # 未知片段保留的模型时间可能早于前面的可靠重锚；只撤销可选重锚，
    # 不调整未知片段或放过真正的长停顿错误。每条最多撤销一次，过程有界。
    for _ in range(len(optional_reanchors) + 1):
        if cancel_check:
            cancel_check()
        changed = False
        for i in range(1, len(result)):
            if float(result[i]["start"]) >= float(result[i - 1]["start"]):
                continue
            for index in (i - 1, i):
                if index in optional_reanchors:
                    result[index] = optional_reanchors.pop(index)
                    changed = True
        if not changed:
            break
    if cancel_check:
        cancel_check()
    return result
