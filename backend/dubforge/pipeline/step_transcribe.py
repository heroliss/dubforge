"""
步骤 3: transcribe —— 转录
==========================
faster-whisper 词级时间戳 → 短语级 SRT (源语言)
产物: srt/src.srt + transcribe.json (语言/时长统计)

- 模型从 config.asr.model 读取 (默认 large-v3-turbo；首次使用时按需下载到 HF 缓存目录)
- 词级时间戳 + 识别后短语锚点: 相邻词间隔 > gap阈值、遇标点或超过 2s → 整理锚点
- 2s 只作用于 Whisper 返回结果的后处理，不会把音频切成 2s 小块送入识别模型
- 逗号/分号也先形成短语锚点，但是否合并由语义整理步骤结合长度、时长和上下文决定
- 有 CUDA 时使用 float16，否则使用 CPU int8；实际资源需求取决于模型和音频。
"""
import math
from numbers import Real

from dubforge.utils.srt import SubLine, write_srt
from dubforge.execution_progress import report_stage

# 短语锚点标点：逗号/分号也先切出锚点，后续语义整理可根据上下文重新合并。
_SENT_END = set("。！？.!?…，,；;：:、")
_GAP_BREAK = 0.6          # 词间静默 > 0.6s 也切短语锚点
_MAX_LINE = 2.0           # 短语锚点最长 2s (最终句子交给下游 LLM 整理)
_MIN_LINE = 0.4


def _recognition_evidence(word, segment) -> dict:
    """保留模型已经返回的可选证据，不补造缺失值或改变识别结果。"""
    evidence = {}
    for key, owner in (("probability", word), ("no_speech_prob", segment),
                       ("avg_logprob", segment)):
        value = getattr(owner, key, None)
        if isinstance(value, bool) or not isinstance(value, Real):
            continue
        value = float(value)
        if not math.isfinite(value):
            continue
        if key == "avg_logprob":
            if value > 0:
                continue
        elif not 0 <= value <= 1:
            continue
        evidence[key] = value
    segment_id = getattr(segment, "id", None)
    if isinstance(segment_id, int) and not isinstance(segment_id, bool) and segment_id >= 0:
        evidence["segment_id"] = segment_id
    return evidence


def _segment_from_words(words: list[dict], *, gap_break: float = _GAP_BREAK,
                        max_line: float = _MAX_LINE) -> list[tuple[float, float, str]]:
    """词列表 → 句级 (start, end, text) 列表 (按标点/停顿/时长整理锚点)。

    这里处理的是 Whisper 已经返回的词级结果，不参与 Whisper 的识别窗口，
    所以 max_line 变小不会削弱识别上下文，只会让下游获得更细的时间锚点。

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
        if cur_words and w["start"] - cur_words[-1]["end"] > gap_break:
            flush()
        # 2) 超长保护 (追加前判断, 保证 cur+当前词 > 上限才切)
        if cur_words and (w["end"] - cur_words[0]["start"]) > max_line:
            flush()
        cur_words.append(w)
        # 3) 词后检查: 标点先形成短语锚点，是否最终合并交给语义整理
        if w["word"].rstrip() and w["word"].rstrip()[-1] in _SENT_END:
            flush()

    flush()
    return segs


def run(ctx) -> None:
    report_stage(ctx, 'checking_transcription_source', '检查转录来源与输入音轨')
    if ctx.cfg('asr.input_mode', 'speech') == 'platform_subtitle':
        from dubforge.publishing.imports import selected
        data = selected(ctx.pdir)
        ctx.check_cancel()
        report_stage(ctx, 'importing_subtitles', '使用已确认导入的平台字幕', total=len(data['lines']), unit='句', reused=True)
        lines = [SubLine(i, row['start'], row['end'], ' '.join(row['text'].split())) for i, row in enumerate(data['lines'], 1)]
        out = ctx.pdir / 'srt/src.srt'
        write_srt(lines, out)
        ctx.register_file('src.srt', out, 'srt')
        # 平台只提供句级时间，不能伪造词级时间；split 已有无词时间戳回退。
        ctx.write_json('words.json', [])
        ctx.write_json('transcribe.json', {'language': data['language'], 'duration': max(l.end for l in lines),
                       'lines': len(lines), 'model': 'platform_subtitle', 'source_bvid': data['bvid'], 'source_cid': data['cid']})
        ctx.log(f'已使用确认导入的平台字幕：{len(lines)}句；未运行语音识别')
        return
    from dubforge import config
    from dubforge.environment import require_component
    require_component("asr")
    from dubforge.progress import RateProgress
    report_stage(ctx, 'loading_asr_model', '加载语音识别模型')
    config.ensure_hf_env()
    raw_wav = ctx.upstream("preprocess", "raw.wav")

    model_name = ctx.cfg("asr.model", "large-v3-turbo")
    lang = ctx.cfg("asr.language", "auto")
    gap_break = max(0.1, float(ctx.cfg("asr.pause_threshold_seconds", _GAP_BREAK)))
    max_line = max(1.5, float(ctx.cfg("asr.max_segment_seconds", _MAX_LINE)))
    ctx.log(f"加载 faster-whisper 模型: {model_name}; 识别后短语锚点: 停顿>{gap_break:g}s 或时长>{max_line:g}s，"
            "每个词保留起止时间并交给下游语义整理；强句末标点立即结束，逗号/分号交给模型判断")

    import torch
    from faster_whisper import WhisperModel
    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_name, device=device, compute_type=compute)

    ctx.check_cancel()
    report_stage(ctx, 'detecting_speech', '准备音频并检测人声与语言')
    ctx.log(f"转录中 (device={device}, lang={lang})...")
    segments, info = model.transcribe(
        str(raw_wav),
        language=None if lang == "auto" else lang.lower(),  # whisper 要小写 ISO 码
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    report_stage(ctx, 'waiting_first_segment', '识别首批语音，等待结果')
    # 模型加载与音频准备不计入识别速度；批次之间按上一次实测 ETA 倒计时。
    progress = RateProgress(ctx, float(info.duration or 0), eta_on_update=True)
    words: list[dict] = []
    for seg in segments:
        ctx.check_pause()
        ctx.check_cancel()
        report_stage(ctx, 'transcribing', '识别语音（按已返回结果更新）',
                     completed=round(seg.end, 2), total=round(float(info.duration or 0), 2), unit='音频秒')
        for w in (seg.words or []):
            words.append({"start": w.start, "end": w.end, "word": w.word,
                          **_recognition_evidence(w, seg)})
        if info.duration:
            progress.update(seg.end)
        if int(seg.end) % 30 == 0:
            ctx.log(f"进度: {seg.end:.0f}s")

    report_stage(ctx, 'saving', '整理并保存字幕与词级时间戳', completed=len(words), total=len(words), unit='词')
    progress.update(float(info.duration or 0))
    segs = _segment_from_words(words, gap_break=gap_break, max_line=max_line)
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
    ctx.log(f"✅ 转录完成: {info.language}, {len(srt_lines)} 个短语锚点 / {len(words)} 个词 / {info.duration:.0f}s")
