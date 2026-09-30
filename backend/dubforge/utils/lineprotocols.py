"""行协议读写: split 时间轴 / translate 编号译文 的统一纯文本格式
==============================================================
split.txt   每行: 序号. 开始秒-结束秒: 原文片段   (与 LLM 输出协议一致)
translate.txt 每行: 序号. 译文                 (与 LLM 输出协议一致; 译文可为空)

校验通过后直接以协议文本落盘，产物即模型输出本身，人工可读、可 diff。
读取端兼容常见的无害空格差异；结构性对齐仍由各步骤校验。
"""
import re

from dubforge.utils.fs import atomic_replace

TIMED_LINE_RE = re.compile(
    r"^\s*(\d+)\s*[.、．]\s*(\d+(?:\.\d+)?)\s*[-–—~]\s*"
    r"(\d+(?:\.\d+)?)\s*[:：]\s*(.*?)\s*$")
NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、．]\s*(.*?)\s*$")


def serialize_timed_lines(lines: list[dict]) -> str:
    """[{id, start, end, text}] -> 协议文本 (末尾带换行)"""
    rows = [f"{int(l['id'])}. {float(l['start']):.3f}-{float(l['end']):.3f}: {l['text']}"
            for l in lines]
    return "\n".join(rows) + ("\n" if rows else "")


def parse_timed_lines(text: str) -> list[dict]:
    """协议文本 -> [{id, start, end, text}] (忽略空行; 不做窗口校验)"""
    out: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = TIMED_LINE_RE.match(line)
        if not m:
            raise ValueError(f"split 时间轴行格式错误: {line[:80]}")
        out.append({"id": int(m.group(1)), "start": float(m.group(2)),
                    "end": float(m.group(3)), "text": m.group(4)})
    return out


def serialize_numbered_lines(dst_by_id: dict[int, str]) -> str:
    """{id: 译文} -> 编号文本 (末尾带换行; 空译文输出 "N. ")"""
    rows = [f"{line_id}. {dst_by_id[line_id]}" for line_id in sorted(dst_by_id)]
    return "\n".join(rows) + ("\n" if rows else "")


def parse_numbered_lines(text: str) -> dict[int, str]:
    """编号文本 -> {id: 译文} (忽略空行)"""
    out: dict[int, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = NUMBERED_LINE_RE.match(line)
        if not m:
            raise ValueError(f"编号译文行格式错误: {line[:80]}")
        out[int(m.group(1))] = m.group(2)
    return out


def write_protocol_text(path, text: str) -> None:
    """原子写协议文本产物。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        atomic_replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
