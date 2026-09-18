# -*- coding: utf-8 -*-
"""
步骤 3: transcribe —— 转录
==========================
faster-whisper 词级时间戳 → 句级 SRT (源语言)
产物: srt/src.srt + transcribe.json (语言/时长统计)

- 模型从 config.asr.model 读取 (默认 large-v3-turbo; vl_cache 有 large-v3, 首次用 turbo 会下载)
- 词级时间戳 + 依标点断句: 相邻词间隔 > gap阈值 或遇句末标点 → 切句
- GPU: float16 (8G 显存跑 large-v3-turbo 无压力)
"""
import json

from dubforge.utils.srt import SubLine, write_srt

# 句末标点 (中英通用)
_SENT_END = set("。！？；.!?,；;…")
_GAP_BREAK = 0.8          # 词间静默 > 0.8s 也切句
_MAX_LINE = 12.0          # 单句最长 12s (超出强制切)
_MIN_LINE = 0.4


def _segment_from_words(words: list[dict]) -> list[tuple[float, float, str]]:
    """词列表 → 句级 (start, end, text) 列表 (按标点/停顿/时长切)

    切句判断都在"追加当前词之前"做 (gap 是上一个词与当前词之间的停顿,
    归属于上一句的结尾); 追加后再检查标点与超长。
    """
    segs: list[tuple[float, float, str]] = []
    cur_words: list[dict] = []

    def flush():
        if cur_words:
            text = "".join(w["word"] for w in cur_words).strip()
            if text:
                segs.append((cur_words[0]["start"], cur_words[-1]["end"], text))
        cur_words.clear()

    for w in words:
        # 1) 词前检查: 与上一词的停顿是否该切句
        if cur_words and w["start"] - cur_words[-1]["end"] > _GAP_BREAK:
            flush()
        # 2) 超长保护 (追加前判断, 保证 cur+当前词 > 上限才切)
        if cur_words and (w["end"] - cur_words[0]["start"]) > _MAX_LINE:
            flush()
        cur_words.append(w)
        # 3) 词后检查: 句末标点
        if w["word"].rstrip() and w["word"].rstrip()[-1] in _SENT_END:
            flush()

    flush()
    return segs


def run(ctx) -> None:
    from dubforge import config
    config.ensure_hf_env()
    raw_wav = ctx.upstream("preprocess", "raw.wav")

    model_name = ctx.cfg("asr.model", "large-v3-turbo")
    lang = ctx.cfg("asr.language", "auto")
    ctx.log(f"加载 faster-whisper 模型: {model_name}")

    from faster_whisper import WhisperModel
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_name, device=device, compute_type=compute)

    ctx.log(f"转录中 (device={device}, lang={lang})...")
    segments, info = model.transcribe(
        str(raw_wav),
        language=None if lang == "auto" else lang.lower(),  # whisper 要小写 ISO 码
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    words: list[dict] = []
    for seg in segments:
        ctx.check_cancel()
        for w in (seg.words or []):
            words.append({"start": w.start, "end": w.end, "word": w.word})
        if info.duration:
            ctx.report_progress(seg.end / info.duration * 100)
        if int(seg.end) % 30 == 0:
            ctx.log(f"进度: {seg.end:.0f}s")

    segs = _segment_from_words(words)
    srt_lines = [SubLine(i + 1, s, max(e, s + _MIN_LINE), t)
                 for i, (s, e, t) in enumerate(segs)]

    # 词级时间戳落盘 (下游 split 拆分超长行时保时间轴)
    ctx.write_json("words.json", words)

    srt_dir = ctx.pdir / "srt"
    srt_dir.mkdir(exist_ok=True)
    out = srt_dir / "src.srt"
    write_srt(srt_lines, out)
    ctx.register_file("src.srt", out, "srt")

    ctx.write_json("transcribe.json", {
        "language": info.language,
        "duration": round(info.duration, 1),
        "lines": len(srt_lines),
        "model": model_name,
    })
    ctx.log(f"✅ 转录完成: {info.language}, {len(srt_lines)} 句 / {info.duration:.0f}s")
