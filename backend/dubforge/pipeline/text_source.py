"""翻译、字幕导出和配音统一读取语义整理结果；译文严格逐句对应。"""
from dubforge.errors import DubForgeError
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
