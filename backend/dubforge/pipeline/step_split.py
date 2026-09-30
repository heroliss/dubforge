"""
步骤 4: split —— 分句
=====================
输入: transcribe 的 src.srt + words.json (词级时间戳)
职责: 把 ASR 短语锚点整体重组为适合字幕/配音的句子, 并保持时间轴。
  - 规则切: 按标点/连接词等自然边界贪心切分到长度上限 —— 零成本兜底
  - LLM 语义重组: 按 token 预算把连续短语和可选的词级时间戳一次交给 LLM, 可合并/拆分并重排时间轴;
    输出“序号. 开始秒-结束秒: 片段”纯文本；无害格式差异警告，序号结构错误定向重试，仍失败整窗回退规则切
  - 时间轴: 优先用词级时间戳精确定位子句边界; 词缺失时按字符占比线性分摊

产物: split.txt 编号时间轴协议文本 (翻译的输入行)
"""
import difflib
import json
import math
import re
import unicodedata

from dubforge.errors import DubForgeError, StepFailedError
from dubforge.utils.srt import SubLine, parse_srt
from dubforge.progress import LlmProgress
from dubforge.pipeline.text_prompts import ASR_CORRECTION_GUIDANCE, SEMANTIC_TIMING_GUIDANCE

# 英文常见从句连接词 (规则切分点, 仅超长行兜底用)
_BREAK_WORDS = {"and", "but", "so", "because", "which", "that", "when",
                "while", "then", "or", "however", "therefore"}
_ASIAN_CHARS = r"\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af"
_TOKEN_RE = re.compile(rf"\s*(?:[A-Za-z0-9À-ÖØ-öø-ÿ']+|[{_ASIAN_CHARS}]|[^\s])")
_TIMED_LINE_RE = re.compile(
    r"^\s*(\d+)\s*\.\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*:\s*(.*)$")
_TIMED_CANONICAL_RE = re.compile(r"^\d+\. \d+(?:\.\d+)?-\d+(?:\.\d+)?: .*$")


def _llm_profile_arg(ctx, selected: str | None):
    """取本次运行固定的配置快照；单元测试里的轻量 Ctx 仍可传名称。"""
    getter = getattr(ctx, "llm_profile_snapshot", None)
    return getter(selected) if callable(getter) else selected


def _wc(text: str) -> int:
    """估算字幕显示长度: 所有可见字母/数字/符号按字符计。

    这不是模型 token 数，而是面向字幕可读性的长度单位。字幕宽度主要由显示
    字符数决定，因此比“英文单词数”更能反映一行会不会太长；拆分器仍然把
    英文单词当作不可拆 token，避免在单词中间断开。标点和空格不计数。
    """
    return sum(
        1 for ch in text
        if not ch.isspace() and not unicodedata.category(ch).startswith("P")
    )


def _rule_split(text: str, max_len: int) -> list[str]:
    """规则切分: 长度是硬上限, 标点/连接词是软边界。

    不会见到标点就立即切开: 只有当前组达到合理长度后, 才优先选最近的
    自然边界; 找不到自然边界时才在 max_len 处硬切。这样可以避免把长句
    切成很多过短片段, 同时保证本地回退结果可控。

    保真约定: "".join(parts) 必须与原文逐字符一致 (token 自带空格),
    否则英文行拼接处会出现 wordword 粘连。
    """
    if _wc(text) <= max_len:
        return [text]
    # 不能用 ``\S+``：中文/日文没有空格，会被当成一个超长 token，无法切分。
    # 这里按 CJK 单字、拉丁词、其余单字符拆成 token，同时保留 token 前的空白，
    # 所有 token 拼回去后仍与原文逐字符一致。
    tokens = _TOKEN_RE.findall(text)
    groups: list[str] = []
    start = 0
    # 软边界至少让一段达到约半个上限，防止“某某，and ...”中的
    # 前半句只有几个词就被单独拎出来；句尾剩余很短时仍允许提前收束。
    min_soft_len = max(4, max_len // 2)
    boundary_chars = set("。！？!?；;，,、:：")

    while start < len(tokens):
        count = 0
        end = start
        preferred_end: int | None = None
        while end < len(tokens):
            tok = tokens[end]
            w = _wc(tok)
            # 极端情况下单个 token 自身超过上限, 也必须向前推进避免死循环。
            if end > start and count + w > max_len:
                break
            count += w
            end += 1

            stripped = tok.strip()
            last = stripped[-1:] if stripped else ""
            # 只有边界前已经有足够内容，或边界后的尾巴本来就很短时才采用
            # 软边界。否则像英文 ``sentence, and ...`` 这样的早期逗号会
            # 把 ``sentence,`` 单独留成一条短字幕。
            remaining_units = _wc("".join(tokens[end:]))
            if last in boundary_chars and (
                    count >= min_soft_len or remaining_units <= min_soft_len):
                preferred_end = end

            # 英文连接词更适合在连接词前断开: "..., because ..."。
            word = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", stripped).lower()
            if (word in _BREAK_WORDS and end - 1 > start
                    and count - w >= min_soft_len):
                preferred_end = end - 1

        cut = preferred_end if preferred_end and preferred_end > start else end
        if cut <= start:  # 防御性兜底, 理论上只会发生在空 token 输入
            cut = start + 1
        groups.append("".join(tokens[start:cut]))
        start = cut

    return groups if groups else [text]


def _restore_source_spacing(parts: list[str], source: str) -> list[str] | None:
    """模型只改了空白时, 按模型的边界从原句恢复原始字符。

    LLM 常会把英文片段两端的空格自动 trim。直接把这种结果判为失败会浪费
    两三次慢请求; 这里只在去掉所有空白后字符完全一致时修复, 标点、大小写或
    单词发生任何变化都返回 None, 仍走严格失败回退。
    """
    compact_parts = "".join(re.sub(r"\s+", "", p) for p in parts)
    compact_source = re.sub(r"\s+", "", source)
    if compact_parts != compact_source:
        return None
    restored: list[str] = []
    cursor = 0
    for part in parts:
        content = re.sub(r"\s+", "", part)
        if not content:
            return None
        start = cursor
        seen = 0
        while cursor < len(source) and seen < len(content):
            if not source[cursor].isspace():
                seen += 1
            cursor += 1
        if seen != len(content):
            return None
        restored.append(source[start:cursor])
    if cursor < len(source):
        # 句尾空白属于最后一段, 不能额外生成一个只有空格的字幕段。
        restored[-1] += source[cursor:]
    return restored if "".join(restored) == source else None


def _mismatch_summary(expected: str, actual: str) -> tuple[str, str]:
    """返回适合重试提示的摘要和适合运行日志的可读差异详情。"""
    matcher = difflib.SequenceMatcher(a=expected, b=actual, autojunk=False)
    first = next((op for op in matcher.get_opcodes() if op[0] != "equal"),
                 ("replace", 0, len(expected), 0, len(actual)))
    tag, i1, i2, j1, j2 = first
    names = {"replace": "替换", "delete": "删除", "insert": "插入"}
    summary = (f"原句 {len(expected)} 字符/拼接 {len(actual)} 字符, "
               f"首个差异在第 {i1 + 1} 个字符 ({names.get(tag, tag)})")

    def quote_visible(value: str) -> str:
        # 用可见符号标出空格, 让日志直接看出 ``morning·today`` 与
        # ``morningtoday`` 的区别, 不必复制文本到编辑器再数空格。
        visible = (value.replace(" ", "·").replace("\t", "⇥")
                   .replace("\r", "␍").replace("\n", "↵"))
        return json.dumps(visible, ensure_ascii=False)

    def excerpt(text: str, start: int, end: int) -> str:
        lo = max(0, start - 36)
        hi = min(len(text), max(end, start + 1) + 36)
        value = text[lo:hi]
        prefix = "…" if lo else ""
        suffix = "…" if hi < len(text) else ""
        # JSON 字符串表示会把空格、换行等不可见差异明确显示出来。
        return prefix + quote_visible(value) + suffix

    detail = (f"{summary}; 原句片段={excerpt(expected, i1, i2)}, "
              f"模型拼接片段={excerpt(actual, j1, j2)}; "
              f"原句全文={quote_visible(expected[:600])}, "
              f"模型拼接全文={quote_visible(actual[:600])}")
    return summary, detail


def _join_coarse_lines(lines: list[SubLine],
                       merge_boundaries: set[int] | None = None) -> str:
    """把 ASR 短语锚点拼成本地校验用的 canonical source。

    这个结果只用于判断模型输出的逐字保真，不随请求发送。拉丁词跨短语
    边界时补一个空格，中文/标点直接相连，避免外部拼接原文进入提示词。
    """
    out = ""
    for i, line in enumerate(lines):
        text = line.text
        should_join = merge_boundaries is None or i in merge_boundaries
        if (should_join and out and text
                and re.search(r"[A-Za-z0-9]$", out)
                and re.match(r"[A-Za-z0-9]", text)):
            out += " "
        out += text
    return out


def _needs_boundary_space(previous: SubLine | None, current: SubLine) -> bool:
    """判断两个 ASR 短语之间是否需要保留英文词间空格。"""
    if previous is None or not previous.text or not current.text:
        return False
    return bool(re.search(r"[A-Za-z0-9]$", previous.text)
                and re.match(r"[A-Za-z0-9]", current.text))


def _short_gap_boundaries(lines: list[SubLine],
                          pause: float) -> set[int]:
    """找出可交给语义重组的短间隔边界；长间隔保留为时间保护边界。"""
    mergeable: set[int] = set()
    for i in range(1, len(lines)):
        gap = max(0.0, lines[i].start - lines[i - 1].end)
        if gap <= pause:
            mergeable.add(i)
    return mergeable

def _protected_gap_warnings(parts: list[dict], lines: list[SubLine],
                            merge_boundaries: set[int],
                            pause: float) -> list[str]:
    """长停顿只提示，不修复；是否保留停顿仍由模型根据语义判断。"""
    warnings: list[str] = []
    tolerance = 0.011
    for i, line in enumerate(lines):
        if i in merge_boundaries:
            continue
        previous = lines[i - 1]
        gap = max(0.0, line.start - previous.end)
        if gap < pause:
            continue
        for index, part in enumerate(parts, 1):
            start = float(part.get("start", 0.0) or 0.0)
            end = float(part.get("end", 0.0) or 0.0)
            if start < line.start - tolerance and end > previous.end + tolerance:
                warnings.append(
                    f"第 {index} 行跨越了 {gap:.2f}s 长间隔；已按模型结果保留，"
                    "建议确认它不会把字幕拉得离音频过远")
    return warnings


def _estimate_tokens(text: str) -> int:
    """估算请求文本的 token 数，不绑定某一家模型的 tokenizer。

    中文/日文/韩文通常接近一个 token，拉丁文字和空白更紧凑。这个估算用于
    安全分窗而不是计费；实际请求还会预留提示词、时间戳和模型输出空间。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text
              if ("\u2e80" <= ch <= "\u9fff"
                  or "\u3040" <= ch <= "\u30ff"
                  or "\uac00" <= ch <= "\ud7af"))
    other = len(text) - cjk
    return max(1, int(cjk * 1.2 + other * 0.45 + 0.999))


def _words_for_lines(lines: list[SubLine], words: list[dict] | None) -> list[dict]:
    """取得一组短语覆盖的词级时间戳，边界允许少量浮点误差。"""
    if not lines or not words:
        return []
    start, end = lines[0].start, lines[-1].end
    return [w for w in words
            if w.get("start", 0) >= start - 0.08
            and w.get("end", 0) <= end + 0.08]


def _timeline_text(text: object) -> str:
    """时间轴行内文本必须是单行；ASR 换行等异常只归一为空格。"""
    return re.sub(r"[\r\n]+", " ", str(text))


def _format_word_timeline(words: list[dict], pause_threshold: float = 0.6) -> str:
    """把词级时间戳输出为逐行时间轴；长停顿只作为提示性注释。"""
    rows: list[str] = []
    previous_end: float | None = None
    for word in words:
        if not isinstance(word, dict) or "word" not in word:
            continue
        start = float(word.get("start", 0.0))
        end = float(word.get("end", start))
        if previous_end is not None and start - previous_end >= pause_threshold:
            rows.append(f"# 停顿 {start - previous_end:.2f}s")
        rows.append(f"{start:.2f}-{end:.2f}: {_timeline_text(word['word'])}")
        previous_end = max(previous_end or end, end)
    return "\n".join(rows)


def _format_phrase_timeline(lines: list[SubLine], pause_threshold: float = 0.6) -> str:
    """把短语锚点输出为逐行时间轴；长停顿只作为提示性注释。"""
    rows: list[str] = []
    previous_end: float | None = None
    for line in lines:
        if previous_end is not None and line.start - previous_end >= pause_threshold:
            rows.append(f"# 停顿 {line.start - previous_end:.2f}s")
        rows.append(f"{line.start:.2f}-{line.end:.2f}: {_timeline_text(line.text)}")
        previous_end = max(previous_end or line.end, line.end)
    return "\n".join(rows)


def _resegment_batches(lines: list[SubLine], *, token_budget: int,
                       words: list[dict] | None = None,
                       include_words: bool = True,
                       max_input_chars: int = 0,
                       ) -> list[list[SubLine]]:
    """按 token 预算 + 输出规模上限组织连续短语窗口。

    一条短语不会被拆开；如果单条本身超过预算，会独立成窗，再由本地规则
    保证最终字幕上限。上下文预算只保证「装得下」；但单次响应要生成的正文
    过长时，长输出会让模型退化（实测同一 448 行输入的重组结果在 281~448 句
    之间随机波动，甚至原样复述输入、行协议劣化）。因此输入字符数超过
    max_input_chars（预期输出约为其一半）即开新窗——普通稿件（约 12k 字符）
    仍是单窗，长视频稿件被稳定拆成若干小窗，逐窗校验、定点重试、互不牵连。
    """
    batches: list[list[SubLine]] = []
    current: list[SubLine] = []
    tokens = 0
    chars = 0
    for line in lines:
        line_words = _words_for_lines([line], words)
        word_row = (
            f"  词级: {_format_word_timeline(line_words)}\n"
            if include_words and line_words else "")
        cost = _estimate_tokens(
            f"{line.index}. [{line.start:.3f}-{line.end:.3f}s] {line.text}\n{word_row}")
        if current and (tokens + cost > token_budget
                        or (max_input_chars > 0
                            and chars + len(line.text) > max_input_chars)):
            batches.append(current)
            current, tokens, chars = [], 0, 0
        current.append(line)
        tokens += cost
        chars += len(line.text)
    if current:
        batches.append(current)
    return batches


def _coarse_resegment_prompt(lines: list[SubLine], max_len: int,
                             max_duration: float, context_before: str = "",
                             context_after: str = "",
                             words: list[dict] | None = None,
                             pause_threshold: float = 0.6,
                             include_words: bool = True) -> str:
    """构建轻指导的整体重组提示词；语义边界由模型自主判断。"""
    input_subject = "词级时间轴" if include_words else "短语锚点"
    context = ""
    if context_before:
        context += "\n【窗口前上下文，仅作理解参考】\n" + context_before + "\n"
    if context_after:
        context += "\n【窗口后上下文，仅作理解参考】\n" + context_after + "\n"
    timeline = (_format_word_timeline(_words_for_lines(lines, words), pause_threshold)
                if include_words else
                _format_phrase_timeline(lines, pause_threshold))
    return "".join([
        "【任务：整体语义重组】\n",
        f"下面是 ASR 给出的{input_subject}。请保留源语言，结合上下文整理成自然、完整、适合字幕阅读和配音的句子。"
        "如何合并、拆分或调整词语归属，由你根据语义和节奏判断，兼顾原意和说话人的表达。\n\n",
        ASR_CORRECTION_GUIDANCE,
        "\n【时间与分句建议】\n",
        f"单句显示字符参考上限 {max_len}，语音时长参考上限 {max_duration:g} 秒，起止时间尽量贴近语音。\n",
        SEMANTIC_TIMING_GUIDANCE,
        f"`# 停顿 X.XXs` 表示真实间隔达到 {pause_threshold:.2f} 秒；跨长停顿合句可能拉长字幕并影响同步。\n",
        "ASR 可能把后句开头的短词挂到前句末尾，可结合语境调整归属；遇到刻意停顿、犹豫或归属不明时，保留原时间通常更稳妥。\n",
        "\n【输出约定】只返回本窗口的编号时间轴文本，每行“序号. 开始秒-结束秒: 原文片段”，"
        "序号从 1 连续、片段非空，方便后续字幕对齐。\n",
        "\n【输出示例】输入 3 行短语，第 1+2 句语义连续被合并，时间取跨度:\n"
        "1. 0.000-4.050: we tried the best food in hanoi\n"
        "2. 4.050-6.200: and it was amazing\n"
        "【常见错误（任一都会让本窗口整体作废）】\n"
        "- 原样复述输入、一行不合并（输出行数与输入完全相同通常意味着没有整理）\n"
        "- 改写、翻译或增删原文内容；只允许调整边界与词语归属\n"
        "- 输出 Markdown 代码块、解释或致歉等协议之外的文字\n"
        "- 时间戳乱序、越界、缺秒或用逗号作小数点；序号不连续或重复\n",
        f"{context}\n",
        "【本窗口时间轴输入】\n",
        timeline,
    ])

class _ProtocolStructureError(ValueError):
    """序号缺失、重复或错位；这是必须定向重试的结构问题。"""

    def __init__(self, message: str, missing: list[int] | None = None,
                 partial: list[dict] | None = None):
        super().__init__(message)
        self.missing = list(missing or [])
        self.partial = list(partial or [])


def _parse_timed_protocol(raw: str, window_start: float,
                          window_end: float,
                          warnings: list[str] | None = None) -> list[dict]:
    """解析编号时间轴；无害格式差异兼容，结构错位才失败。

    空行、前后空格、冒号/序号附近的多余空格只产生警告；
    序号缺失、重复、错位和无法识别的前缀仍属于严重错误。
    """
    warnings = warnings if warnings is not None else []
    text = raw.lstrip("\ufeff\r\n").rstrip("\r\n")
    if not text.strip():
        raise ValueError("模型输出为空")

    parts: list[dict] = []
    previous_start: float | None = None
    tolerance = 0.011
    ignored_blank = 0
    ignored_comments = 0
    for physical_index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            ignored_blank += 1
            continue
        if stripped.startswith("#"):
            ignored_comments += 1
            continue
        if stripped in {"```", "```text", "```txt"}:
            warnings.append("已忽略 Markdown 代码块围栏")
            continue
        if not _TIMED_CANONICAL_RE.fullmatch(stripped):
            warnings.append(f"第 {physical_index} 行序号/时间格式空格不规范，已兼容")

        match = _TIMED_LINE_RE.fullmatch(line)
        if not match:
            raise ValueError(
                f"第 {physical_index} 行格式错误：缺少“序号. 开始秒-结束秒: 片段”前缀")
        seq = int(match.group(1))
        expected_seq = len(parts) + 1
        if seq != expected_seq:
            raise _ProtocolStructureError(
                f"第 {physical_index} 行序号应为 {expected_seq}，实际为 {seq}",
                missing=[expected_seq], partial=parts)
        try:
            start = float(match.group(2))
            end = float(match.group(3))
        except ValueError:
            raise ValueError(f"第 {physical_index} 行时间不是有限数字") from None
        content = match.group(4).strip()
        if content != match.group(4):
            warnings.append(f"第 {physical_index} 行片段两端空白已移除")
        if not content:
            raise ValueError(f"第 {physical_index} 行片段为空")
        bounded_start = min(max(start, window_start), window_end)
        bounded_end = min(max(end, window_start), window_end)
        if abs(bounded_start - start) > tolerance or abs(bounded_end - end) > tolerance:
            warnings.append(
                f"第 {physical_index} 行时间超出窗口范围，已收拢到 "
                f"{window_start:.2f}-{window_end:.2f}s")
        if bounded_end <= bounded_start:
            raise ValueError(f"第 {physical_index} 行结束时间必须大于开始时间")
        if previous_start is not None and bounded_start < previous_start - tolerance:
            warnings.append(f"第 {physical_index} 行开始时间与上一行乱序，已按模型结果保留")
        previous_start = bounded_start
        parts.append({"text": content, "start": bounded_start, "end": bounded_end})

    if ignored_blank:
        warnings.append(f"已忽略 {ignored_blank} 个空行")
    if ignored_comments:
        warnings.append(f"已忽略 {ignored_comments} 条停顿/解释注释")
    return parts

def _split_repair_prompt(batch: list[SubLine], timeline: str,
                         error: Exception) -> str:
    """结构错位的定向重试；不携带上一轮输出和窗口上下文。"""
    return (
        f"【定向结构重试】上次结果未能对齐：{error}\n"
        "请根据同一时间轴重新整理，保留源语言，兼顾语义完整和语音时间。"
        "直接返回完整编号文本，每行“序号. 开始秒-结束秒: 原文片段”，序号从 1 连续、片段非空。\n\n"
        + ASR_CORRECTION_GUIDANCE + SEMANTIC_TIMING_GUIDANCE + "\n【本窗口时间轴输入】\n" + timeline)



def _llm_resegment(ctx, lines: list[SubLine], max_len: int,
                   max_duration: float, context_lines: int,
                   words: list[dict]) -> list[dict]:
    """批量语义重组短语；单窗口失败时已经在内部回退本地切分。"""
    from dubforge.backends import llm
    context_limit = max(8_000, int(ctx.cfg("split.llm_context_tokens", 200_000)
                                   or 200_000))
    segmentation_mode = str(ctx.cfg("asr.segmentation_mode", "phrase") or "phrase").lower()
    word_mode = segmentation_mode == "word" and bool(words)
    pause = max(0.1, float(ctx.cfg("asr.pause_threshold_seconds", 0.6) or 0.6))
    short_gap_boundaries = _short_gap_boundaries(lines, pause)
    # 给系统提示词、时间戳格式、上下文和模型输出保留空间。默认 0.2M
    # 预算时约留 40k；预算放宽到 1M 时同步扩大预留，但仍限制在 120k，
    # 避免长输出或 tokenizer 估算误差把请求顶到服务商上限。
    output_reserve = min(120_000, max(4_000, context_limit // 5))
    prompt_reserve = 2_000 + _estimate_tokens(
        f"每句显示字符尽量不超过 {max_len}，语音时长尽量不超过 {max_duration:g} 秒。"
        "每行输出“序号. 开始秒-结束秒: 原文片段”，不要 Markdown、解释或其他文字。")
    token_budget = max(2_000, context_limit - output_reserve - prompt_reserve)
    max_input_chars = int(ctx.cfg("split.window_max_chars", 16000) or 0)
    batches = _resegment_batches(lines, token_budget=token_budget, words=words,
                                  include_words=word_mode, max_input_chars=max_input_chars)
    ctx.log(f"LLM 整体语义重组: {len(lines)} 条短语 → {len(batches)} 个连续窗口，"
            f"总上下文上限={context_limit:,} token，正文窗口预算约={token_budget:,} token，"
            f"单窗输入上限={max_input_chars:,} 字符"
            f"（预留提示词约{prompt_reserve:,}、输出约{output_reserve:,}），"
            f"时间轴粒度={'词级' if word_mode else '短语级'}，长停顿阈值={pause:.2f}s，"
            f"可重组短间隔边界={len(short_gap_boundaries)}处")
    all_parts: list[dict] = []
    profile = str(ctx.cfg("split.llm_profile", "") or "").strip() or None
    profile_arg = _llm_profile_arg(ctx, profile)
    weights = [sum(len(line.text) for line in batch) for batch in batches]
    progress = LlmProgress(ctx, weights, step="split", profile=profile_arg,
                           mode=f"{segmentation_mode}:{max_len}:{max_duration}",
                           start=1.0, end=99.0)
    ctx.log("LLM 显示预计进度：无相近记录时整步按10分钟估算，有记录用中间约三分之一的平均耗时；思考/输出按实际事件切换")
    for bi, batch in enumerate(batches, 1):
        ctx.check_pause()
        ctx.check_cancel()
        progress.begin(bi - 1)
        # 批次按原列表连续构造；用对象身份查找，避免重复文本导致 index() 取错位置。
        first_index = next(i for i, x in enumerate(lines) if x is batch[0])
        last_index = next(i for i, x in enumerate(lines) if x is batch[-1])
        before = "\n".join(
            f"{x.start:.2f}-{x.end:.2f}: {_timeline_text(x.text)}"
            for x in lines[max(0, first_index - context_lines):first_index])
        after_index = last_index + 1
        after = "\n".join(
            f"{x.start:.2f}-{x.end:.2f}: {_timeline_text(x.text)}"
            for x in lines[after_index:after_index + context_lines])
        local_merge_boundaries = {
            i - first_index for i in short_gap_boundaries
            if first_index < i <= last_index
        }
        source = _join_coarse_lines(batch, local_merge_boundaries)
        prompt = _coarse_resegment_prompt(
            batch, max_len, max_duration, before, after,
            words, pause, word_mode)

        timeline = (_format_word_timeline(_words_for_lines(batch, words), pause)
                    if word_mode else
                    _format_phrase_timeline(batch, pause))
        parser_warnings: list[str] = []

        def valid(raw):
            parser_warnings.clear()
            parsed = _parse_timed_protocol(
                raw, batch[0].start, batch[-1].end, parser_warnings)
            parser_warnings.extend(_protected_gap_warnings(
                parsed, batch, local_merge_boundaries, pause))
            return parsed

        def retry_hint(error, attempt: int) -> str:
            return f"【第 {attempt} 次尝试】请留意上述对齐问题。"

        def choose_retry_context(error, attempt: int, last_raw: str) -> str:
            return "fresh"

        def retry_request(error, attempt: int) -> str:
            return _split_repair_prompt(batch, timeline, error)

        boundary_index = first_index
        boundary_space = (
            boundary_index in short_gap_boundaries
            and _needs_boundary_space(
                lines[first_index - 1] if first_index else None, batch[0])
        )

        try:
            parts = llm.chat_validated(
                prompt, parser=valid, max_retries=2, profile=profile_arg,
                log=ctx.log, cancel_check=ctx.check_cancel,
                trace=progress.trace, retry_prompt=retry_hint,
                retry_request=retry_request,
                retry_context=choose_retry_context)
            for warning in parser_warnings:
                ctx.log(f"⚠ {warning}")
                ctx.llm_trace("protocol_warning", warning)
            # 长间隔合并只提示，不再本地拆回；保留模型的语义判断。
            # _join_coarse_lines 会在同一窗口内补英文词间空格；窗口边界也要补，
            # 否则 token 分窗恰好落在两个 ASR 短语之间时会出现 wordword。
            if boundary_space and parts:
                if not parts[0]["text"].startswith((" ", "\t", "\n", "\r")):
                    parts[0]["text"] = " " + parts[0]["text"]
            # 模型时间不可靠或缺失时按每个窗口字符比例补齐；有效时间则限制在窗口内。
            span = max(0.001, batch[-1].end - batch[0].start)
            cursor = 0
            previous_end = batch[0].start
            for part in parts:
                plen = max(1, len(part["text"]))
                start = part["start"] if part["start"] is not None else batch[0].start + span * cursor / max(1, len(source))
                end_cursor = cursor + plen
                end = part["end"] if part["end"] is not None else batch[0].start + span * end_cursor / max(1, len(source))
                part_start = max(previous_end, batch[0].start,
                                 min(batch[-1].end, float(start)))
                part_end = min(batch[-1].end, max(part_start + 0.01, float(end)))
                if part_end <= part_start:
                    part_start = max(batch[0].start, batch[-1].end - 0.01)
                    part_end = batch[-1].end
                part["start"] = part_start
                part["end"] = part_end
                part["_window"] = bi
                all_parts.append(part)
                previous_end = part["end"]
                cursor = end_cursor
            progress.finish()
            ctx.log(f"LLM 窗口 {bi}/{len(batches)} 完成: {len(batch)} 条短语 → {len(parts)} 条字幕句")
        except DubForgeError:
            # LLM 层已给出明确错误码 (未配置/超时/限流/输出截断), 原样上抛不改写
            raise
        except Exception as e:  # noqa: BLE001
            ctx.check_cancel()
            progress.finish(success=False)
            # 不回退本地规则切分: 机械切分会把无声学语义的结果静默送进翻译/配音/
            # 字幕，全自动流程里宁可响亮失败——链会挂起，「重试并继续」即可恢复；
            # 单窗输入上限保证一次退化不牵连其他窗口。
            ctx.log(f"LLM 窗口 {bi}/{len(batches)} 输出不可信，不回退本地切分，任务失败: {e}")
            ctx.llm_trace("fallback",
                          f"窗口 {bi}/{len(batches)} LLM 输出不可信，任务失败等待重试")
            raise StepFailedError(
                f"LLM 窗口 {bi}/{len(batches)} 重组结果不可信: {e}",
                code="SPLIT_WINDOW_UNTRUSTED",
                suggestion="可直接重跑本步骤；反复出现时在设置中更换语义整理模型、"
                           "调小「单窗输入上限」，或检查模型输出 token 上限与思考预算。")
    stitched = _stitch_window_edges(all_parts, max_len, max_duration, pause)
    if len(stitched) != len(all_parts):
        ctx.log(f"窗口边界整理: 合并 {len(all_parts) - len(stitched)} 个跨窗口技术断点")
    return stitched


def _enforce_final_limits(parts: list[dict], max_len: int,
                          max_duration: float,
                          words: list[dict] | None = None) -> list[dict]:
    """对模型的整体重组结果做最后一道字幕可用性约束。

    LLM 负责语义边界和跨短语合并；长度/时长是程序必须保证的硬约束。只有
    超出硬约束的模型片段才在这里用本地规则细分，不会再逐条请求 LLM；
    细分时优先复用词级时间戳，避免按字符比例把字幕边界挪离人声。
    """
    out: list[dict] = []
    for item in parts:
        text = str(item.get("text", ""))
        if not text.strip():
            continue
        start = float(item.get("start", 0.0) or 0.0)
        end = float(item.get("end", start + 0.01) or (start + 0.01))
        if end <= start:
            end = start + 0.01
        units = _wc(text)
        duration = end - start
        effective = max_len
        if duration > max_duration and units >= 8:
            needed = max(2, math.ceil(duration / max_duration))
            effective = max(4, min(max_len, (units + needed - 1) // needed))
        if units <= max_len and duration <= max_duration:
            out.append({"text": text, "start": start, "end": end})
            continue
        local_parts = _rule_split(text, effective)
        line_words = _words_in_range(words or [], start, end)
        for local_text, (local_start, local_end) in zip(
                local_parts,
                _times_for_parts(local_parts, SubLine(0, start, end, text), line_words)):
            out.append({"text": local_text, "start": local_start, "end": local_end})
    return out


def _stitch_window_edges(parts: list[dict], max_len: int,
                         max_duration: float, pause: float) -> list[dict]:
    """修复 token 分窗恰好落在同一句中间造成的边界。

    相邻窗口只共享只读上下文，不能重复输出；如果硬窗口边界两侧没有停顿、
    没有句末标点且合并后仍符合最终上限，则在本地拼回，避免长稿窗口边界产生
    不自然的断句。带有语义停顿或超过上限时保持两个结果。
    """
    if len(parts) < 2:
        return parts
    sentence_end = set("。！？.!?…")
    out: list[dict] = []
    for part in parts:
        current = dict(part)
        if out and out[-1].get("_window") != current.get("_window"):
            previous = out[-1]
            left = str(previous.get("text", ""))
            right = str(current.get("text", ""))
            gap = max(0.0, float(current.get("start", 0))
                      - float(previous.get("end", 0)))
            joined = left + (" " if _needs_boundary_space(
                SubLine(0, 0, 0, left), SubLine(0, 0, 0, right)) else "") + right
            if (gap <= pause and left.rstrip()[-1:] not in sentence_end
                    and _wc(joined) <= max_len
                    and float(current.get("end", 0))
                    - float(previous.get("start", 0)) <= max_duration):
                previous["text"] = joined
                previous["end"] = current.get("end", previous.get("end"))
                continue
        out.append(current)
    # 仅供本函数使用的窗口标记不能落到产物里。
    for item in out:
        item.pop("_window", None)
    return out


def _words_in_range(words: list[dict], start: float, end: float) -> list[dict]:
    return [w for w in words if w["start"] >= start - 0.08 and w["end"] <= end + 0.08]



def _assign_times(text: str, line: SubLine, line_words: list[dict]) -> tuple[float, float]:
    """按字符占比在行时间窗内线性分摊 (词精确对齐的优化留后续)"""
    return line.start, line.end


def _times_for_parts(parts: list[str], line: SubLine,
                     line_words: list[dict]) -> list[tuple[float, float]]:
    """给拆分出的子句分配时间窗; 词级数据可用时用词边界, 否则字符占比"""
    times: list[tuple[float, float]] = []
    weights = [max(1, _wc(p)) for p in parts]
    total_weight = sum(weights)
    if line_words:
        idx = 0
        cursor = 0
        for p, weight in zip(parts, weights):
            # 按显示字符占比找到应消耗的词数
            target = (cursor + weight) / total_weight
            end_idx = max(idx + 1, round(target * len(line_words)))
            end_idx = min(end_idx, len(line_words))
            seg_words = line_words[idx:end_idx]
            if seg_words:
                times.append((seg_words[0]["start"], seg_words[-1]["end"]))
            else:
                times.append(_assign_times(p, line, line_words))
            cursor += weight
            idx = end_idx
    else:
        span = line.end - line.start
        acc = 0
        for weight in weights:
            t0 = line.start + span * acc / total_weight
            acc += weight
            times.append((t0, line.start + span * acc / total_weight))
    # 修正相邻边界与单调性
    for i in range(len(times) - 1):
        if times[i][1] > times[i + 1][0]:
            mid = (times[i][1] + times[i + 1][0]) / 2
            times[i] = (times[i][0], mid)
            times[i + 1] = (mid, times[i + 1][1])
    return times


def _normalized_split_lines(lines: list[dict]) -> list[dict]:
    """输出给翻译/字幕/配音的每句文本必须去掉两端空白。

    模型输出可能在片段边界保留空格；正式 split.txt、翻译输入、
    字幕渲染和普通翻译软件回传都要求每句从非空白字符开始和结束。
    """
    out: list[dict] = []
    for item in lines:
        text = str(item.get("text", "")).strip()
        if not text:
            raise DubForgeError("语义整理结果包含空句，无法生成翻译输入")
        normalized = dict(item)
        normalized["text"] = text
        out.append(normalized)
    return out


def run(ctx) -> None:
    src_srt = ctx.upstream("transcribe", "src.srt")
    words: list[dict] = json.loads(ctx.upstream("transcribe", "words.json").read_text(encoding="utf-8"))
    max_len = max(1, int(ctx.cfg("split.max_split_length", 22)))
    max_duration = max(1.0, float(ctx.cfg("split.max_segment_seconds", 8.0)))
    context_lines = max(0, int(ctx.cfg("split.context_lines", 2)))
    # 旧版配置可能保存过 null; null 表示未明确设置, 应回到默认启用而不是
    # 被 Python 的 bool(None) 误解释成关闭 LLM 语义整理。
    use_llm_value = ctx.cfg("split.use_llm", True)
    use_llm = True if use_llm_value is None else bool(use_llm_value)
    profile = str(ctx.cfg("split.llm_profile", "") or "").strip() or None
    if use_llm:
        from dubforge.backends import llm
        ctx.log(f"语义整理: 目标句长={max_len}, 目标时长={max_duration:g}s, "
                f"窗口前后参考各{context_lines}句, "
                f"分窗=按单次语义整理上下文预算，"
                f"时间轴粒度={'词级' if str(ctx.cfg('asr.segmentation_mode', 'phrase') or 'phrase').lower() == 'word' else '短语级'}，"
                f"模型配置={profile or '全局降级链'}，"
                f"候选模型={llm.describe_selection(_llm_profile_arg(ctx, profile))}")
    else:
        mode_label = "词级" if str(ctx.cfg("asr.segmentation_mode", "phrase") or "phrase").lower() == "word" else "短语级"
        ctx.log(f"语义整理: LLM 已关闭（配置值={use_llm_value!r}），"
                f"仅使用本地规则和{mode_label}时间锚点（内部仍保留词级时间戳校正）")

    lines = parse_srt(src_srt)
    # 时间轴协议依赖物理换行分条；这里再次保证 ASR 短语文本本身是单行。
    for line in lines:
        line.text = _timeline_text(line.text)
    out: list[dict] = []
    next_id = 1
    if use_llm:
        # 一次把连续短语和所选粒度的时间轴交给模型，让它同时完成合并、句内拆分
        # 和时间轴重组；超出 token 预算才进入下一个窗口，避免逐句请求造成延迟和限流。
        ctx.report_progress(1.0)
        parts = _llm_resegment(ctx, lines, max_len, max_duration,
                               context_lines, words)
        parts = _enforce_final_limits(parts, max_len, max_duration, words)
        for part in parts:
            out.append({"id": next_id, "text": part["text"],
                        "start": round(part["start"], 3),
                        "end": round(part["end"], 3)})
            next_id += 1
        ctx.report_progress(99.0)
        ctx.log(f"LLM 语义整理后完成兜底约束整理: {len(parts)} 句")
    else:
        for li, line in enumerate(lines):
            ctx.check_pause()
            ctx.check_cancel()
            ctx.report_progress(max(0.5, li / max(1, len(lines)) * 100))
            lw = _words_in_range(words, line.start, line.end)
            speech_duration = ((lw[-1]["end"] - lw[0]["start"]) if lw
                               else (line.end - line.start))
            units = _wc(line.text)
            duration_too_long = speech_duration > max_duration
            if units <= max_len and not duration_too_long:
                out.append({"id": next_id, "text": line.text,
                            "start": round(line.start, 3), "end": round(line.end, 3)})
                next_id += 1
                continue

            effective_max = max_len
            if duration_too_long and units >= 8:
                needed = max(2, math.ceil(speech_duration / max_duration))
                effective_max = max(4, (units + needed - 1) // needed)
                effective_max = min(max_len, effective_max)
            parts = _rule_split(line.text, effective_max)
            times = _times_for_parts(parts, line, lw)
            for text, (s, e) in zip(parts, times):
                out.append({"id": next_id, "text": text,
                            "start": round(s, 3), "end": round(e, 3)})
                next_id += 1
            ctx.log(f"原句 #{line.index} ({units}单位/{speech_duration:.2f}s) "
                    f"按本地兜底长度 {effective_max} 拆为 {len(parts)} 句")

    out = _normalized_split_lines(out)
    # 产物 = 校验通过的编号时间轴协议文本，与 LLM 输出结构一致
    from dubforge.utils.lineprotocols import serialize_timed_lines, write_protocol_text
    write_protocol_text(ctx.artifact_dir() / "split.txt",
                        serialize_timed_lines(out))
    ctx.register_file("split.txt", ctx.artifact_dir() / "split.txt", "txt")
    ctx.log(f"✅ 分句完成: {len(lines)} 行 → {len(out)} 句 "
            f"(目标句长={max_len}, 目标时长={max_duration:g}s, "
            f"LLM={'开' if use_llm else '关'})")
