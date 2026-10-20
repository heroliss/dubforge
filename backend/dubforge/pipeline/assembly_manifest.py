"""成片的可重建呈现合同；比较证据与素材位置分开保存。"""
import copy
import hashlib
import math
import shutil
import tempfile
from pathlib import Path

from dubforge.errors import DubForgeError
from dubforge.utils.fs import atomic_replace

SCHEMA = 1
COMMENT_EVENTS = 'dubforge.comment-events.v1'


class ManifestError(DubForgeError):
    code = 'ASSEMBLY_MANIFEST_INVALID'
    suggestion = '请重新检查成片改动；缺少历史输入时需先建立完整成片基线。'


def _root(deps):
    return Path(getattr(deps, 'root', None) or deps.pdir).resolve()


def _path(deps, ref):
    value = ref.get('path') if isinstance(ref, dict) else None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    result = (_root(deps) / path).resolve()
    if not result.is_relative_to(_root(deps)):
        raise ManifestError('成片历史素材路径超出项目目录。')
    return result


def media_path(deps, ref):
    """仅解析已保存引用；读取者不能改用当前同名素材。"""
    path = _path(deps, ref)
    if path is None:
        raise ManifestError('成片缺少历史素材引用。', code='ASSEMBLY_HISTORY_MEDIA_MISSING')
    return path


def _ref(deps, path, *, identity=None):
    from dubforge.pipeline.dubbing_edit import file_version
    root = _root(deps)
    if path is None:
        return dict(path='', identity=copy.deepcopy(identity or {}), missing=True)
    path = Path(path).resolve()
    value = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    if identity is None or identity.get('missing'):
        art = next((a for a in getattr(deps, 'arts', {}).values()
                    if deps.path(a.step_key, a.name) == path), None)
        try:
            stat = path.stat()
        except OSError:
            return dict(path=value, identity={}, missing=True)
        identity = (dict(sha256=art.sha256, size=art.size) if art is not None and art.sha256
                    and art.size == stat.st_size else dict(file_version=file_version(path)))
    return dict(path=value, identity=copy.deepcopy(identity), **({} if path.is_file() else {'missing': True}))


def _json(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return copy.deepcopy(value)


def _version(version):
    # 运行方案、历史成片和不可变素材不得通过此处递归进入新合同。
    keys = ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides',
            'values', 'origins', 'output_cuts', 'audio_line_gains', 'encoder_profile')
    return {key: copy.deepcopy(version[key]) for key in keys if key in version}


def capture(deps, version):
    """只读当前真实呈现；GET 不创建素材副本，也不重新扫描媒体摘要。"""
    from dubforge.pipeline import assembly_sentence_changes as changes, dubbing_edit, audio_post
    from dubforge.pipeline.version_render import subtitle_lines as current_subtitles
    from dubforge.pipeline.subtitle_document import read
    from dubforge.pipeline import content_redaction, voice_timing
    from dubforge.pipeline.dubbing_placement import build_plan, region_entries
    from dubforge.pipeline.video_source import resolve_video
    from dubforge.comment_studio import validate_settings

    scoped = deps.for_version(version)
    inputs = changes.capture(deps, version)
    subtitles = [dict(id=row.index, start=row.start, end=row.end, text=row.text,
                      parts=_json(row.parts)) for row in current_subtitles(scoped, version)]
    audio, media = [], {'source': _ref(deps, resolve_video(deps.project, _root(deps)),
                                     identity=inputs['global_inputs']['source'])}
    if version['dubbing']:
        rows = read(deps, translation=True)
        selected = dubbing_edit.production_lines(deps.project, rows, _root(deps), dependencies=deps)
        selected = [{**row, 'source': row['source'] if row['source'] == 'original'
                     or row.get('take') is not None else 'silent'} for row in selected]
        layout = voice_timing.plan(deps.project, _root(deps), selected, scoped.cfg,
                                   video_duration=inputs['duration'])
        by_id = {row['id']: row for row in selected}
        for event in region_entries(build_plan(layout['inputs'], inputs['duration']), 0, inputs['duration']):
            row = by_id[event['id']]
            if row['source'] == 'silent':
                continue
            step, name = (('preprocess', 'vocal.wav') if row['source'] == 'original' else
                          ('tts', dubbing_edit.take_name(row['id'], row['take'])))
            path = deps.path(step, name) or (_root(deps) / 'audio/vocal.wav' if step == 'preprocess'
                                            else dubbing_edit.take_path(_root(deps), row['id'], row['take']))
            gain = audio_post.line_state(deps.project, row, audio_version=dubbing_edit.file_version(path),
                                        saved=version.get('audio_line_gains'))['gain_db']
            audio.append(dict(id=row['id'], source=row['source'], start=event['clip_start'],
                              end=event['clip_end'], audio_offset=(event['clip_start'] if row['source'] == 'original'
                                                               else event.get('audio_offset', 0)),
                              gain_db=gain, media=_ref(deps, path)))
        if float(version['values'].get('assemble.bgm_volume', 0)) > 0:
            media['background'] = _ref(deps, deps.path('preprocess', 'background.wav'))
    overlay = (content_redaction.safe_overlay(scoped, version.get('comment_overlay'))
               if version['include_comments'] else None)
    comments = []
    if overlay:
        settings = validate_settings(overlay.get('settings', {}))
        for index, item in enumerate(overlay['items']):
            start = settings['start'] + index * settings['interval']
            end = min(inputs['duration'], start + settings['duration'])
            if end > start:
                comments.append(dict(id=str(item['id']), start=start, end=end,
                                     item=copy.deepcopy(item), settings=copy.deepcopy(settings)))
    audio_review = content_redaction.audio_plan(scoped, start=0, duration=inputs['duration'])
    if audio_review.get('background') is not None:
        media.setdefault('background', _ref(deps, audio_review['background']))
    result = dict(schema=SCHEMA, duration=inputs['duration'], version=_version(version), inputs=inputs,
                  subtitles=subtitles, audio=audio, comments=comments, media=media,
                  review=dict(plan=_json(content_redaction.plan(scoped)), audio_plan=_json(audio_review)))
    return result


def _history_error(message='旧成片缺少完整的实际呈现记录，无法保证仅应用所选改动。'):
    return ManifestError(message, code='ASSEMBLY_HISTORY_UNAVAILABLE')


def _same_identity(a, b):
    if a.get('sha256') and b.get('sha256'):
        return a['sha256'] == b['sha256'] and a.get('size') == b.get('size')
    return bool(a.get('file_version') and a.get('file_version') == b.get('file_version'))


def _historic_ref(deps, identity, paths):
    for path in paths:
        if path is None:
            continue
        candidate = _ref(deps, path)
        if not candidate.get('missing') and _same_identity(identity, candidate['identity']):
            return {**candidate, 'identity': copy.deepcopy(identity)}
    sha = identity.get('sha256')
    if isinstance(sha, str) and len(sha) == 64:
        folder = _root(deps) / 'output' / '.render-assets'
        for candidate in folder.glob(sha + '.*'):
            return _ref(deps, candidate, identity=identity)
    return dict(path='', identity=copy.deepcopy(identity), missing=True)


def from_saved(deps, saved):
    """旧记录只从真实历史证据恢复；不把现在的工作稿伪装为旧成片。"""
    existing = saved.get('render_manifest')
    if isinstance(existing, dict) and existing.get('schema') == SCHEMA:
        if not all(isinstance(existing.get(key), list) for key in ('subtitles', 'audio', 'comments')):
            raise _history_error('成片历史呈现记录损坏，请先建立完整成片基线。')
        return copy.deepcopy(existing)
    from dubforge.pipeline import assembly_sentence_changes as changes, dubbing_edit
    from dubforge.pipeline.assembly_change_groups import saved_comments, _comment_inputs
    from dubforge.pipeline.video_source import resolve_video
    from dubforge.comment_studio import validate_settings
    inputs = saved.get('sentence_inputs')
    if (not changes._valid(inputs) or inputs.get('coverage', 'known') != 'known'
            or inputs.get('mixed_globals') or any(not row.get('known', True) for row in inputs['lines'].values())):
        raise _history_error()
    duration = inputs.get('duration')
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise _history_error('旧成片没有有效时长记录，无法重建所选改动。')
    global_inputs = inputs['global_inputs']
    if (global_inputs.get('redaction') or {}).get('ranges') or global_inputs.get('audio_redaction'):
        raise _history_error('旧成片未记录审查声音的最终处理时段，无法准确保留未选内容。')
    media = dict(source=_historic_ref(deps, global_inputs['source'],
                                    [resolve_video(deps.project, _root(deps))]))
    if global_inputs.get('background'):
        media['background'] = _historic_ref(deps, global_inputs['background'],
                                            [deps.path('preprocess', 'background.wav')])
    subtitles, audio = [], []
    track = saved['values'].get('assemble.subtitle_track', 'trans')
    for key, row in inputs['lines'].items():
        for event in row.get('subtitle', []):
            if event.get('parts') is not None:
                parts = copy.deepcopy(event['parts'])
            elif track != 'bilingual':
                parts = [[track, event['text']]]
            else:
                doc = row.get('document')
                if not isinstance(doc, dict):
                    raise _history_error('旧双语字幕缺少语言分段记录，无法准确保留独立样式。')
                parts = [['source', doc['source_text']], ['trans', doc['text']]]
                if not saved['values'].get('subtitles.source_first', False):
                    parts.reverse()
                if '\n'.join(part[1] for part in parts) != event['text']:
                    raise _history_error('旧双语字幕与保存的文字稿不一致，无法准确还原。')
            subtitles.append(dict(id=int(key), start=event['start'], end=event['end'],
                                  text=event['text'], parts=parts))
        for event in row.get('audio', []):
            paths = ([deps.path('preprocess', 'vocal.wav')] if event['source'] == 'original' else
                     [deps.path('tts', dubbing_edit.take_name(int(key), take))
                      or dubbing_edit.take_path(_root(deps), int(key), take)
                      for take in dubbing_edit.take_ids(_root(deps), int(key))])
            audio.append(dict(id=int(key), source=event['source'], start=event['start'], end=event['end'],
                              audio_offset=event['start'] if event['source'] == 'original' else event.get('audio_offset', 0),
                              gain_db=event.get('gain_db', 0), media=_historic_ref(deps, event['identity'], paths)))
    comments, known = [], saved_comments(saved)
    if not known.get('known'):
        raise _history_error('旧成片没有完整的上屏评论记录，无法保证保留未选评论。')
    if known['events']:
        overlay = saved.get('comment_overlay')
        if not overlay:
            raise _history_error('旧成片只保存了评论样式摘要，缺少可重建样式。')
        settings = validate_settings(overlay['settings'])
        replay = _comment_inputs(overlay, duration, historical=True)
        by_id = {str(item['id']): item for item in overlay['items']}
        signatures = {event['id']: event for event in replay['events']}
        for event in known['events']:
            check = signatures.get(event['id'])
            if (check is None or check['style_signature'] != event['style_signature']
                    or check['parts'] != event['parts']):
                raise _history_error('旧局部成片混用了不同的评论文字或样式，缺少完整重建记录。')
            comments.append(dict(id=event['id'], start=event['start'], end=event['end'],
                                 item=copy.deepcopy(by_id[event['id']]), settings=copy.deepcopy(settings)))
    review = dict(plan=dict(enabled=False, ranges=[], blocked_ids=[], findings=[], checked_lines=0),
                  audio_plan=dict(requested_mode='voice_only', effective_mode='unchanged', ranges=[],
                                  filter='', background=None, background_identity=None,
                                  fallback_reason='', bgm_volume=float(saved['values'].get('assemble.bgm_volume', 0))))
    restored_inputs = copy.deepcopy(inputs)
    restored_inputs['comment_inputs'] = copy.deepcopy(known)
    return dict(schema=SCHEMA, duration=duration, version=_version(saved), inputs=restored_inputs,
                subtitles=subtitles, audio=audio, comments=comments, media=media, review=review)


def _verify(deps, ref, label):
    from dubforge.pipeline.dubbing_edit import file_version
    path = _path(deps, ref)
    if not isinstance(ref, dict) or ref.get('missing') or path is None or not path.is_file():
        raise ManifestError(f'{label}的历史素材已缺失，无法保留未勾选内容。', code='ASSEMBLY_HISTORY_MEDIA_MISSING')
    identity = ref.get('identity') or {}
    try:
        if identity.get('sha256'):
            valid = path.stat().st_size == identity.get('size')
            if valid:
                with path.open('rb') as stream:
                    valid = hashlib.file_digest(stream, 'sha256').hexdigest() == identity['sha256']
        else:
            valid = bool(identity.get('file_version') and file_version(path) == identity['file_version'])
    except OSError as exc:
        raise ManifestError(f'无法读取{label}的历史素材。', code='ASSEMBLY_HISTORY_MEDIA_MISSING') from exc
    if not valid:
        raise ManifestError(f'{label}的素材已被替换，不能用当前同名文件代替旧内容。', code='ASSEMBLY_HISTORY_MEDIA_CHANGED')
    return path


def validate_media(deps, manifest, *, audio=True):
    """运行边界核验真实内容；纯视觉局部替换允许沿用基片完整音轨。"""
    _verify(deps, manifest.get('media', {}).get('source'), '原视频')
    if audio:
        if manifest.get('media', {}).get('background'):
            _verify(deps, manifest['media']['background'], '背景音轨')
        seen = set()
        for row in manifest.get('audio', []):
            token = repr(row.get('media'))
            if token not in seen:
                _verify(deps, row.get('media'), f'第 {row["id"]} 句声音')
                seen.add(token)


def materialize(deps, manifest, *, audio=True):
    """正式计划/成功记录时保存内容寻址音频；不改比较输入和原素材。"""
    result, cache = copy.deepcopy(manifest), {}
    if not audio:
        # 视觉局部更新直接保留基片音轨；不要求重建、冻结或重新验证旧声音。
        return result
    refs = [row['media'] for row in result.get('audio', [])]
    if result.get('media', {}).get('background'):
        refs.append(result['media']['background'])
    folder = _root(deps) / 'output' / '.render-assets'
    for ref in refs:
        if ref.get('missing'):
            continue
        candidate = _path(deps, ref)
        try:
            stat = candidate.stat() if candidate else None
        except OSError:
            stat = None
        token = (str(candidate), stat.st_mtime_ns if stat else None, stat.st_size if stat else None,
                 repr(sorted((ref.get('identity') or {}).items())))
        if token in cache:
            ref.clear(); ref.update(copy.deepcopy(cache[token]))
            continue
        path = _verify(deps, ref, '声音')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        # 首次只读合同可能只有文件版本；读取后再检查一次，避免恰好换槽时
        # 把新内容存到旧输入合同。已有摘要也必须与本次实际读取一致。
        expected = ref.get('identity', {})
        from dubforge.pipeline.dubbing_edit import file_version
        if ((expected.get('sha256') and expected['sha256'] != digest)
                or (expected.get('file_version') and expected['file_version'] != file_version(path))):
            raise ManifestError('保存成片历史声音时素材发生变化，请重新检查改动。',
                                code='ASSEMBLY_HISTORY_MEDIA_CHANGED')
        suffix = path.suffix.lower() if path.suffix.lower() in {'.wav', '.flac', '.mp3', '.m4a', '.ogg'} else '.wav'
        target = folder / (digest + suffix)
        identity = dict(sha256=digest, size=path.stat().st_size)
        folder.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _verify(deps, _ref(deps, target, identity=identity), '缓存声音')
        else:
            handle = tempfile.NamedTemporaryFile(prefix='.asset-', suffix='.tmp', dir=folder, delete=False)
            pending = Path(handle.name)
            handle.close()
            try:
                shutil.copyfile(path, pending)
                # 防止外部工具恰在复制期间覆盖/原地写入声音。
                _verify(deps, _ref(deps, pending, identity=identity), '保存中的声音')
                atomic_replace(pending, target)
            finally:
                pending.unlink(missing_ok=True)
        value = _ref(deps, target, identity=identity)
        cache[token] = value
        ref.clear(); ref.update(copy.deepcopy(value))
    return result


def subtitle_lines(manifest):
    from dubforge.utils.srt import SubLine
    return [SubLine(row['id'], row['start'], row['end'], row['text'],
                    tuple(tuple(part) for part in row['parts']) if row.get('parts') is not None else None)
            for row in sorted(manifest.get('subtitles', []), key=lambda item: (item['start'], item['id']))]


def comment_plan(manifest):
    """仅供内部渲染；不冒充评论工作区的统一等间隔确认方案。"""
    events = copy.deepcopy(manifest.get('comments', []))
    return dict(internal_schema=COMMENT_EVENTS, events=events) if events else None


def mix_voice(ctx, manifest, out, total):
    """使用冻结事件直接铺轨；不再读取选槽、当前台词或重新自适应排布。"""
    import soundfile as sf
    from dubforge.utils import audio as au
    from dubforge.pipeline import audio_post
    from dubforge.pipeline.audio_quality import annotate_lines, normalize_voice
    from dubforge.pipeline.render_resources import preflight_render
    from dubforge.execution_progress import report_stage
    out = Path(out)
    preflight_render(ctx, out.parent, required_bytes=int(total * au.SR * 4) + 65536)
    entries, reports, loaded, verified = [], [], {}, {}
    for row in manifest.get('audio', []):
        ctx.check_cancel()
        left, right = max(0., row['start']), min(total, row['end'])
        if right <= left:
            continue
        token = repr(row.get('media'))
        if token not in verified:
            verified[token] = _verify(ctx, row.get('media'), f'第 {row["id"]} 句声音')
        path = verified[token]
        # 原声按窗口读取，避免每句重复读整条长人声；短配音按同文件复用。
        offset = row.get('audio_offset', 0) + left - row['start']
        if row.get('source') == 'original' and sf.info(str(path)).samplerate == au.SR:
            with sf.SoundFile(path) as stream:
                stream.seek(round(offset * au.SR))
                data = stream.read(round((right - left) * au.SR), dtype='float32', always_2d=True)
            samples = data.mean(axis=1)
        else:
            if str(path) not in loaded:
                loaded[str(path)] = au.load_mono_32k(path)
            samples = loaded[str(path)][round(offset * au.SR):round((offset + right - left) * au.SR)]
        if len(samples) + 1 < round((right - left) * au.SR):
            raise ManifestError(f'第 {row["id"]} 句历史声音长度不足，已停止以免截断。', code='ASSEMBLY_HISTORY_MEDIA_CHANGED')
        entries.append((left, au.fade_in_out(samples)))
        reports.append(dict(id=row['id'], start=left, end=right, gain_db=row.get('gain_db', 0), gain_stale=False))
    report_stage(ctx, 'mixing_voice', '按照所选改动合成人声轨，保留未选句子的声音与位置', total=len(entries), unit='句')
    entries, levels, corrections = audio_post.balance_entries(entries,
        enabled=bool(ctx.cfg('assemble.sentence_leveling', False)),
        max_db=ctx.cfg('assemble.sentence_leveling_max_db', 3.))
    entries = [(offset, samples if not row['gain_db'] else samples * 10 ** (row['gain_db'] / 20))
               for (offset, samples), row in zip(entries, reports)]
    ctx._audio_line_report = annotate_lines([{**row, 'active_rms_db': level, 'leveling_gain_db': correction}
        for row, level, correction in zip(reports, levels, corrections)], [audio for _, audio in entries])
    mixed = au.mix_over_timeline(entries, total)
    if not ctx.cfg('assemble.loudnorm', True):
        mixed = au.peak_normalize(mixed, -3.)
    out.parent.mkdir(parents=True, exist_ok=True)
    pending = out.with_name('.tmp_' + out.name)
    try:
        sf.write(str(pending), mixed, au.SR, subtype='FLOAT', format='WAV')
        atomic_replace(pending, out)
    finally:
        pending.unlink(missing_ok=True)
    if ctx.cfg('assemble.loudnorm', True):
        normalize_voice(out, ctx)
    return out
