"""从统一校对稿导出字幕；手动设置的时间优先于最短显示时长。"""
from dubforge.pipeline.subtitle_document import read
from dubforge.utils.srt import SubLine, write_srt


def _extend_short_cues(lines, min_duration, total=None, fixed_ids=(), barriers=()):
    """只利用空隙延长，不引入重叠，也不越过已知内容终点。"""
    threshold = max(0.0, float(min_duration or 0))
    total = total if total is not None else max((l.end for l in lines), default=0)
    changed = 0
    for i, line in enumerate(lines):
        if line.index in fixed_ids:
            continue
        limit = lines[i + 1].start if i + 1 < len(lines) else total
        limit = min([limit, *(r['start'] for r in barriers if r['start'] >= line.end)])
        end = max(line.end, min(line.start + threshold, limit))
        if end > line.end:
            line.end = end
            changed += 1
    return changed


def build_tracks(ctx, mode=None):
    """预览与导出共用文字、双语顺序和最短显示时长。"""
    mode = mode or ctx.cfg("subtitles.mode", "all")
    if mode not in {"all", "source", "target", "bilingual"}:
        from dubforge.errors import DubForgeError
        raise DubForgeError("请选择有效的字幕导出类型", code="CONFIG_INVALID")
    document = read(ctx, translation=mode != 'source')
    from dubforge.pipeline.content_redaction import redact_document, plan
    document = redact_document(ctx, document)
    source = [{**l, 'text': l['source_text']} for l in document]
    target = document if mode != 'source' else []
    source_first = bool(ctx.cfg("subtitles.source_first", False))
    minimum = float(ctx.cfg("subtitles.min_duration", 1.5) or 0)
    tracks = {}
    if mode in {"all", "source"}:
        tracks["source.srt"] = source
    if mode in {"all", "target"}:
        tracks["trans.srt"] = target
    if mode in {"all", "bilingual"}:
        tracks["bilingual.srt"] = [
            {**a, "text": "\n".join((a["text"], b["text"]) if source_first
                                    else (b["text"], a["text"]))}
            for a, b in zip(source, target, strict=True)]
    for name, rows in tracks.items():
        lines = [SubLine(l["id"], l["start"], l["end"], l["text"]) for l in rows]
        for line, original in zip(lines, document, strict=True):
            source_part = ('source', original['source_text'])
            target_part = ('trans', original['text'])
            line.parts = (source_part,) if name == 'source.srt' else (target_part,) if name == 'trans.srt' else (
                (source_part, target_part) if source_first else (target_part, source_part))
        _extend_short_cues(lines, minimum, fixed_ids={l['id'] for l in rows if 'voice_timing' in l},
                           barriers=plan(ctx)['ranges'])
        tracks[name] = lines
    return tracks, document


def run(ctx):
    tracks, document = build_tracks(ctx)
    if getattr(ctx, "rebuild", False):
        from dubforge.domain import crud
        crud.delete_step_artifacts(ctx.project.id, "subtitles")
    for name, lines in tracks.items():
        ctx.check_cancel()
        path = ctx.pdir / "srt" / name
        write_srt(lines, path)
        ctx.register_file(name, path, "srt")
    ctx.write_json("subtitles.json", {"lines": len(document), "tracks": list(tracks),
        "source": "edited" if any(l['edited'] for l in document) else "split",
        "source_first": bool(ctx.cfg("subtitles.source_first", False)),
        "min_duration": float(ctx.cfg("subtitles.min_duration", 1.5) or 0)})
    ctx.log(f"✅ 字幕导出：{len(document)} 句，{' / '.join(tracks)}；已应用保存的文字与时间")
