# -*- coding: utf-8 -*-
"""
步骤 6: subtitles —— 字幕生成
=============================
输入: split/split.json (时间轴) + translate/translate.json (译文)
产物:
  srt/trans.srt      目标语言字幕 (split 后的时间轴)
  srt/bilingual.srt  双语字幕 (译文上/原文下, 幂等约定与 VideoLingo 工具箱一致)
  srt/src.srt        源语言字幕 (transcribe 原句, 不在本步重写)
行宽: 译文超 max_length 时按标点/字数插入换行 (SRT 内 \n)
"""
import json
import re

from dubforge.utils.srt import SubLine, write_srt


def _wrap(text: str, max_chars: int) -> str:
    """按 max_chars 硬换行: CJK 按字数, 拉丁按词; 优先在标点后断"""
    if len(text) <= max_chars:
        return text
    # 优先标点断点
    best = None
    for m in re.finditer(r"[,，。.!?!?；;、：:]", text):
        if m.end() <= max_chars:
            best = m.end()
    if best and best >= max_chars * 0.5:
        return text[:best].rstrip() + "\n" + text[best:].lstrip()
    # 通用: 找最近空格 (拉丁) 或硬切 (CJK)
    out, cur, width = [], "", 0
    for word in re.findall(r"[\u4e00-\u9fff]|[^\s\u4e00-\u9fff]+|\s+", text):
        w = len(word)
        if width + w > max_chars and cur:
            out.append(cur.rstrip())
            cur, width = "", 0
        cur += word
        width += w
    if cur:
        out.append(cur.rstrip())
    return "\n".join(out)


def run(ctx) -> None:
    split_data = json.loads(ctx.upstream("split", "split.json").read_text(encoding="utf-8"))
    trans_data = json.loads(ctx.upstream("translate", "translate.json").read_text(encoding="utf-8"))
    dst_map = {l["id"]: l["dst"] for l in trans_data["lines"]}

    max_chars = int(ctx.cfg("tts.subtitles.max_length", 75))
    srt_dir = ctx.pdir / "srt"
    srt_dir.mkdir(exist_ok=True)

    src_lines, dst_lines = [], []
    for l in split_data["lines"]:
        start, end = l["start"], l["end"]
        text_src = l["text"]
        text_dst = dst_map.get(l["id"], "")
        src_lines.append(SubLine(l["id"], start, end, text_src))
        dst_lines.append(SubLine(l["id"], start, end, _wrap(text_dst, max_chars)))

    write_srt(dst_lines, srt_dir / "trans.srt")
    ctx.register_file("trans.srt", srt_dir / "trans.srt", "srt")

    # 双语: 译文上 / 原文下 (B 站字幕上传用; 时间轴来自 split)
    write_srt([SubLine(l["id"], l["start"], l["end"],
                       f"{dst_map.get(l['id'], '')}\n{l['text']}")
               for l in split_data["lines"]],
              srt_dir / "bilingual.srt")
    ctx.register_file("bilingual.srt", srt_dir / "bilingual.srt", "srt")

    ctx.write_json("subtitles.json", {
        "lines": len(dst_lines),
        "max_chars": max_chars,
        "target_language": trans_data["target_language"],
    })
    ctx.log(f"✅ 字幕生成: {len(dst_lines)} 句 (trans.srt / bilingual.srt)")
