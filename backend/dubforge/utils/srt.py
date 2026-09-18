# -*- coding: utf-8 -*-
"""SRT 读写工具 (无第三方依赖; 手写 30 行, 少一个依赖)"""
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubLine:
    index: int
    start: float          # 秒
    end: float
    text: str


def _fmt_ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_ts(ts: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", ts.strip())
    h, mi, s, ms = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def write_srt(lines: list[SubLine], path: Path) -> None:
    """原子写 SRT (UTF-8)"""
    blocks = [f"{i}\n{_fmt_ts(l.start)} --> {_fmt_ts(l.end)}\n{l.text}\n"
              for i, l in enumerate(lines, 1)]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(blocks), encoding="utf-8")
    Path(tmp).replace(path)


def parse_srt(path: Path) -> list[SubLine]:
    raw = Path(path).read_text(encoding="utf-8").replace("\r\n", "\n")
    out: list[SubLine] = []
    for block in [b for b in re.split(r"\n\s*\n", raw) if b.strip()]:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        t1, t2 = [x.strip() for x in lines[1].split("-->")]
        text = " ".join(lines[2:]) if len(lines) > 2 else ""
        try:
            idx = int(lines[0])
        except ValueError:
            idx = len(out) + 1      # 索引行缺失/非数字: 容错重排, 不丢整块字幕
        out.append(SubLine(idx, _parse_ts(t1), _parse_ts(t2), text))
    return out


def write_bilingual(src_lines: list[SubLine], dst_lines: list[SubLine],
                    path: Path, dst_top: bool = True) -> None:
    """按行号合并双语 (源/译行数一致的约定由上游翻译步骤保证)"""
    blocks = []
    for i, (a, b) in enumerate(zip(src_lines, dst_lines), 1):
        top, bottom = (b.text, a.text) if dst_top else (a.text, b.text)
        blocks.append(f"{i}\n{_fmt_ts(a.start)} --> {_fmt_ts(a.end)}\n{top}\n{bottom}\n")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(blocks), encoding="utf-8")
    Path(tmp).replace(path)
