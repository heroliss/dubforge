"""翻译、字幕导出和配音统一读取语义整理结果；译文严格逐句对应。"""
import math

from dubforge.errors import DubForgeError, StepFailedError
from dubforge.utils.lineprotocols import parse_numbered_lines, parse_timed_lines


def source_lines(ctx):
    path = ctx.upstream("split", "split.txt")
    try:
        lines = parse_timed_lines(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DubForgeError("原文时间或编号格式无效，请检查语义整理结果", code="TEXT_ALIGNMENT") from exc
    if not lines or any(not l["text"].strip() for l in lines):
        raise DubForgeError("原文为空或含空句，请检查语义整理结果", code="TEXT_EMPTY")
    if len({l["id"] for l in lines}) != len(lines):
        raise DubForgeError("原文包含重复编号，请检查语义整理结果", code="TEXT_ALIGNMENT")
    if any(l["id"] != i for i, l in enumerate(lines, 1)):
        raise StepFailedError("原文编号必须从 1 连续递增，请检查语义整理结果", code="TEXT_ALIGNMENT")
    for i, line in enumerate(lines):
        start, end = line["start"], line["end"]
        if (not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end <= start
                or (i and start < lines[i - 1]["start"] - 0.011)):
            raise StepFailedError(
                f"原文第 {line['id']} 句时间无效，无法继续翻译或配音", code="TEXT_TIMELINE",
                suggestion="请重新生成语义整理结果，或在字幕编辑器中修正时间轴。")
    return lines


def translated_lines(ctx, lines):
    raw = ctx.upstream("translate", "translate.txt").read_text(encoding="utf-8")
    try:
        translations = parse_numbered_lines(raw)
    except ValueError as exc:
        raise DubForgeError("译文格式无效，请重新翻译", code="TEXT_ALIGNMENT") from exc
    if (list(translations) != [l["id"] for l in lines]
            or len([r for r in raw.splitlines() if r.strip()]) != len(lines)
            or any(not t.strip() for t in translations.values())):
        raise DubForgeError("译文与语义整理结果的句数、编号顺序不一致，或含空句", code="TEXT_ALIGNMENT",
                            suggestion="请按当前语义整理结果重新翻译。")
    return [{**l, "text": translations[l["id"]]} for l in lines]
