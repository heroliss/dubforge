"""派生字幕、真实 FFmpeg 混音、项目审查和发布凭据的隔离集成测试。"""
import copy
import json
import math
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import config, content_review_service
from dubforge.domain import crud
from dubforge.pipeline import content_redaction as redaction
from dubforge.pipeline.dependencies import Dependencies
from dubforge.utils.lineprotocols import serialize_numbered_lines, serialize_timed_lines
from dubforge.utils.srt import SubLine, parse_srt, write_srt

pytestmark = pytest.mark.usefixtures('isolated_env')


def _seed_text(uid, source=None, target=None, original=None):
    project, root = crud.get_project(uid), crud.project_dir(uid)
    source = source or [
        dict(id=1, start=0.1, end=0.3, text='Welcome to the city'),
        dict(id=2, start=1.0, end=2.0, text='the tragedy of Tiananmen Square'),
        dict(id=3, start=3.0, end=3.8, text='The architecture is beautiful'),
    ]
    target = target or {1: '欢迎来到这座城市', 2: '天安门广场的悲剧', 3: '建筑很漂亮'}
    original = source if original is None else original
    paths = {}
    for step, name, content in [
        ('split', 'split.txt', serialize_timed_lines(source)),
        ('translate', 'translate.txt', serialize_numbered_lines(target)),
    ]:
        path = root / 'artifacts' / step / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        crud.add_artifact(project.id, step, 'txt', name, path, str(path.relative_to(root)))
        paths[(step, name)] = path
    path = root / 'srt' / 'src.srt'
    write_srt([SubLine(row['id'], row['start'], row['end'], row['text']) for row in original], path)
    crud.add_artifact(project.id, 'transcribe', 'srt', path.name, path, 'srt/src.srt')
    paths[('transcribe', 'src.srt')] = path
    return project, root, paths


class MediaContext:
    """执行真正的封装函数，网络、模型和发布不参与本夹具。"""
    def __init__(self, project, root, paths, values=None):
        self.project, self.pdir, self.paths = project, root, paths
        self.settings = config.load_config()
        self.values = {'content_review.enabled': True, 'content_review.mute_audio': True,
                       'assemble.loudnorm': False, **(values or {})}
        self.task_data = {}

    def cfg(self, key, default=None):
        return self.values.get(key, config.get(key, default, settings=self.settings))

    def dependencies(self, *, assemble_variants=None):
        # 与正式执行器一致，媒体夹具也使用自身冻结的设置。
        return Dependencies(self.project, settings=self.settings,
                            overrides=self.values, assemble_variants=assemble_variants)

    def upstream(self, step, name):
        return self.paths[(step, name)]

    def log(self, message):
        pass

    def report_progress(self, progress):
        pass

    def check_cancel(self):
        pass

    def check_pause(self):
        pass

    def register_file(self, name, path, kind, **kwargs):
        crud.add_artifact(self.project.id, 'assemble', kind, name, path,
                          str(path.relative_to(self.pdir)))

    def run_subprocess(self, command, cwd=None, on_stdout=None, **kwargs):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=30)
        if on_stdout:
            for line in result.stdout.splitlines():
                on_stdout(line)
        return result.returncode, result.stdout, result.stderr


def _media(ctx, duration=4):
    media = ctx.pdir / 'media'
    media.mkdir(parents=True, exist_ok=True)
    video = media / 'video.mp4'
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                    '-f', 'lavfi', '-i', f'testsrc2=size=160x120:rate=10:duration={duration}',
                    '-f', 'lavfi', '-i', f'sine=frequency=200:duration={duration}',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(video)],
                   check=True, timeout=30)
    audio = ctx.pdir / 'audio'
    audio.mkdir(parents=True, exist_ok=True)
    voice, background = audio / 'test_voice.wav', audio / 'test_background.wav'
    for path, frequency in [(voice, 700), (background, 150)]:
        subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration={duration}',
                        '-ar', '32000', '-ac', '1', str(path)], check=True, timeout=30)
    ctx.paths[('preprocess', 'background.wav')] = background
    return video, voice, background


def _samples(path, rate=32000):
    result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(path),
                             '-vn', '-ar', str(rate), '-ac', '1', '-f', 'f32le', 'pipe:1'],
                            capture_output=True, check=True, timeout=30)
    return np.frombuffer(result.stdout, dtype=np.float32), rate


def _rms(audio, rate, start, end):
    values = audio[round(start * rate):round(end * rate)]
    assert len(values)
    return float(np.sqrt(np.mean(values.astype(np.float64) ** 2)))


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_real_original_audio_is_muted_in_detected_english_or_chinese_range(make_project, language):
    from dubforge.pipeline.step_assemble import _mux_original_sub
    uid = make_project()
    source = [dict(id=1, start=1.0, end=2.0,
                   text='the tragedy of Tiananmen Square' if language == 'en' else '天安门广场的悲剧')]
    project, root, paths = _seed_text(uid, source=source, target={1: '普通介绍'})
    ctx = MediaContext(project, root, paths)
    video, _, _ = _media(ctx)
    output = root / 'output' / 'original_reviewed.mp4'
    _mux_original_sub(ctx, video, output, 4.0, (0, 99), burn=False)
    samples, rate = _samples(output)
    assert _rms(samples, rate, 1.25, 1.75) < 0.0001
    assert _rms(samples, rate, 0.3, 0.7) > 0.01
    assert _rms(samples, rate, 2.7, 3.1) > 0.01
    assert not list(output.parent.glob('.tmp_*'))


@pytest.mark.parametrize('ducking', [False, True])
def test_real_final_dub_and_background_mix_has_no_audio_leak(make_project, ducking):
    from dubforge.pipeline.step_assemble import _mux
    project, root, paths = _seed_text(make_project())
    ctx = MediaContext(project, root, paths,
                       {'assemble.bgm_volume': 1.0, 'assemble.ducking': ducking})
    video, voice, _ = _media(ctx)
    output = root / 'output' / 'dub_reviewed.mp4'
    _mux(ctx, root, video, voice, output, 4.0, burn=False)
    samples, rate = _samples(output)
    assert _rms(samples, rate, 1.25, 1.75) < 0.0001
    assert _rms(samples, rate, 0.3, 0.7) > 0.01
    assert _rms(samples, rate, 2.7, 3.1) > 0.01


def test_real_preview_audio_uses_offset_from_original_timeline(make_project):
    from dubforge.pipeline.step_assemble import _mux_original_sub
    project, root, paths = _seed_text(make_project())
    ctx = MediaContext(project, root, paths)
    video, _, _ = _media(ctx)
    output = root / 'output' / 'preview_reviewed.mp4'
    _mux_original_sub(ctx, video, output, 2.5, (0, 99), burn=False, start=0.8)
    samples, rate = _samples(output)
    assert 2.4 < len(samples) / rate < 2.7
    assert _rms(samples, rate, 0.4, 0.9) < 0.0001
    assert _rms(samples, rate, 1.6, 2.0) > 0.01


def test_all_subtitle_tracks_omit_whole_line_and_do_not_extend_into_blocked_range(make_project):
    from dubforge.pipeline.step_subtitles import build_tracks
    project, root, paths = _seed_text(make_project())
    originals = {path: path.read_bytes() for path in paths.values()}
    ctx = MediaContext(project, root, paths, {'subtitles.min_duration': 1.5})
    tracks, document = build_tracks(ctx, mode='all')
    assert [line['id'] for line in document] == [1, 3]
    for lines in tracks.values():
        assert [line.index for line in lines] == [1, 3]
        assert lines[0].end == 1.0
        assert all('tragedy' not in line.text and '悲剧' not in line.text for line in lines)
    assert all(path.read_bytes() == data for path, data in originals.items())


@pytest.mark.parametrize('separate_style', [False, True])
def test_burn_preparation_rebuilds_old_srt_and_shifts_safe_preview_cues(make_project, monkeypatch, separate_style):
    from dubforge.pipeline import subtitle_style
    project, root, paths = _seed_text(make_project())
    source = root / 'srt' / 'trans.srt'
    source.write_text('1\n00:00:01,000 --> 00:00:02,000\n天安门广场的悲剧\n', encoding='utf-8')
    previous = source.read_bytes()
    ctx = MediaContext(project, root, paths, {'assemble.subtitle_track': 'bilingual' if separate_style else 'trans',
                       'assemble.subtitle_source_style_enabled': separate_style})
    monkeypatch.setattr(subtitle_style, 'video_canvas', lambda video: (160, 120))
    graph, generated = subtitle_style.prepare_burn_filter(ctx, root / 'video.mp4', source,
                                                        start=2.5, duration=1.5)
    assert generated.is_file() and generated != source
    text = generated.read_text(encoding='utf-8-sig')
    assert '悲剧' not in text and 'tragedy' not in text
    assert source.read_bytes() == previous
    if separate_style:
        assert generated.suffix == '.ass' and '0:00:00.50' in text and '0:00:01.30' in text
    else:
        lines = parse_srt(generated)
        assert len(lines) == 1 and lines[0].start == 0.5 and lines[0].end == 1.3
    generated.unlink()


def test_raw_asr_still_blocks_audio_after_semantic_text_omits_original_words(make_project):
    source = [dict(id=1, start=1.0, end=2.0, text='The square is beautiful')]
    original = [dict(id=60, start=1.2, end=1.8, text='the tragedy of Tiananmen Square')]
    project, root, paths = _seed_text(make_project(), source=source, target={1: '广场很漂亮'}, original=original)
    current = redaction.plan(MediaContext(project, root, paths))
    assert current['findings'] == []
    assert [item['id'] for item in current['original_findings']] == [1]
    assert current['blocked_ids'] == [1]
    assert current['ranges'] == [{'start': 1.2, 'end': 1.8}]


@pytest.mark.parametrize('value', [
    '-1-2', '1-1', '2-1', '0-NaN', '0-inf', '0-36001', '1e3-2000',
    '\n'.join('0-1' for _ in range(201)), 'x' * 20001, None,
])
def test_invalid_manual_ranges_are_rejected_before_render(value):
    with pytest.raises(redaction.ContentRedactionError):
        redaction.parse_ranges(value)


def test_manual_ranges_are_merged_clamped_and_shifted_for_preview():
    ranges = redaction.parse_ranges('1-2:用户确认\n1.5-3.0:重叠范围\n5-6:后段')
    assert redaction.merge_ranges(ranges, start=1.5, duration=2) == [{'start': 0.0, 'end': 1.5}]
    assert redaction.merge_ranges(ranges, start=10, duration=1) == []


def test_content_review_get_is_read_only_and_post_uses_atomic_report(make_project, monkeypatch):
    from dubforge.utils import fs
    uid = make_project()
    project, root, paths = _seed_text(uid)
    previous = {path: path.read_bytes() for path in paths.values()}
    params = project.params
    report_path = root / 'content_review.json'
    api = TestClient(app)
    result = api.get(f'/api/projects/{uid}/content-review')
    assert result.status_code == 200
    assert result.json()['summary']['blocked_lines'] == 1
    assert not report_path.exists() and crud.get_project(uid).params == params
    replacements = []
    replace = fs.atomic_replace

    def checked_replace(source, target, *args, **kwargs):
        if Path(target) == report_path:
            assert Path(source).suffix == '.tmp'
            assert json.loads(Path(source).read_text(encoding='utf-8'))['summary']['blocked_lines'] == 1
            replacements.append((Path(source), Path(target)))
        return replace(source, target, *args, **kwargs)

    monkeypatch.setattr(fs, 'atomic_replace', checked_replace)
    saved = api.post(f'/api/projects/{uid}/content-review')
    assert saved.status_code == 200 and saved.json()['checked_at']
    assert len(replacements) == 1 and report_path.is_file()
    assert not list(root.glob('.dubforge-*.tmp'))
    assert all(path.read_bytes() == data for path, data in previous.items())


def test_content_review_put_is_revisioned_and_reset_restores_inheritance(make_project):
    uid = make_project()
    _seed_text(uid)
    api = TestClient(app)
    first = api.get(f'/api/projects/{uid}/content-review').json()
    body = dict(revision=first['revision'], configuration_fingerprint=first['configuration_fingerprint'],
                enabled=True, mute_audio=True, blocked_terms='词甲\n词乙', blocked_ranges='1.1-1.9:人工确认')
    response = api.put(f'/api/projects/{uid}/content-review', json=body)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['blocked_terms'] == ['词甲', '词乙'] and saved['inherited'] is False
    stale = api.put(f'/api/projects/{uid}/content-review', json=body)
    assert stale.status_code == 400 and stale.json()['code'] == 'CONTENT_REVIEW_CONFLICT'
    reset = api.put(f'/api/projects/{uid}/content-review', json={
        'revision': saved['revision'], 'configuration_fingerprint': saved['configuration_fingerprint'], 'reset': True})
    assert reset.status_code == 200, reset.text
    assert reset.json()['inherited'] is True and reset.json()['blocked_terms'] == []


def test_old_output_requires_matching_current_redaction_receipt_before_publish(make_project, monkeypatch):
    from dubforge.pipeline import assembly_versions
    uid = make_project()
    project, root, _ = _seed_text(uid)
    output = root / 'output' / 'old_final.mp4'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b'old final video')
    crud.add_artifact(project.id, 'assemble', 'video', output.name, output, 'output/' + output.name)
    artifact = crud.latest_artifacts(project.id, ['assemble'])['assemble/' + output.name]
    monkeypatch.setattr(assembly_versions, 'results', lambda uid: {})
    with pytest.raises(redaction.ContentRedactionError) as exc:
        content_review_service.assert_publishable(project, artifact)
    assert exc.value.code == 'CONTENT_REVIEW_OUTPUT_STALE'
    current = redaction.receipt(Dependencies(project))
    record = SimpleNamespace(artifact_id=artifact.id, artifact_hash=artifact.sha256,
                             snapshot=json.dumps({'content_review_receipt': current}))
    monkeypatch.setattr(assembly_versions, 'results', lambda uid: {'v': record})
    content_review_service.assert_publishable(project, artifact)
    record.snapshot = json.dumps({'content_review_receipt': {**current, 'signature': 'older-text-or-settings'}})
    with pytest.raises(redaction.ContentRedactionError, match='重新生成'):
        content_review_service.assert_publishable(project, artifact)


def test_real_final_mix_mutes_full_selected_waveform_beyond_subtitle_end(make_project):
    from dubforge.pipeline.step_assemble import _mux
    from dubforge.pipeline.dubbing_edit import record_take
    from dubforge.pipeline.step_tts import line_fingerprint
    project, root, paths = _seed_text(make_project())
    ctx = MediaContext(project, root, paths,
                       {'assemble.bgm_volume': 1.0, 'assemble.ducking': False})
    video, voice, _ = _media(ctx)
    selected = root / 'artifacts' / 'tts' / '2.wav'
    selected.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(voice),
                    '-t', '2', str(selected)], check=True, timeout=30)
    crud.add_artifact(project.id, 'tts', 'audio', selected.name, selected, 'artifacts/tts/2.wav')
    deps = Dependencies(project)
    line = next(item for item in deps.dubbing_lines(required=True) if item['id'] == 2)
    record_take(project, line, root, 1, line_fingerprint(project, line, dependencies=deps), {})
    output = root / 'output' / 'long_take_reviewed.mp4'
    _mux(ctx, root, video, voice, output, 4.0, burn=False)
    samples, rate = _samples(output)
    assert _rms(samples, rate, 1.3, 1.7) < 0.0001
    # 字幕结束在 2 秒，选中 wav 从 1 秒开始持续 2 秒；尾音及背景同样静音。
    assert _rms(samples, rate, 2.4, 2.8) < 0.0001
    assert _rms(samples, rate, 3.4, 3.7) > 0.01


def test_missing_original_asr_is_reported_even_when_semantic_text_exists(make_project):
    uid = make_project()
    _, _, paths = _seed_text(uid)
    paths[('transcribe', 'src.srt')].unlink()
    report = content_review_service.report(uid)
    assert report['summary']['lines'] == 3
    assert report['original_checked_lines'] == 0
    assert any('原始识别稿' in warning for warning in report['warnings'])


@pytest.mark.parametrize('burn', [False, True])
def test_review_fingerprint_includes_source_edits_even_for_no_subtitles_or_target_track(make_project, burn):
    from dubforge.pipeline import assembly_versions
    uid = make_project()
    project, _, _ = _seed_text(uid)
    definition = dict(id='source-review', name='原声审查', dubbing=False,
                      burn_subtitles=burn, include_comments=False,
                      overrides={'assemble.subtitle_track': 'trans'})
    deps = Dependencies(project)
    before = assembly_versions.fingerprint(deps, assembly_versions.resolve(deps, definition))
    params = crud.project_params(project)
    params['subtitle_edits'] = {'1': {'base': {
        'source_text': 'Welcome to the city', 'text': '欢迎来到这座城市', 'start': 0.1, 'end': 0.3},
        'values': {'source_text': '天安门广场的悲剧'}}}
    project.params = json.dumps(params, ensure_ascii=False)
    crud.save_project(project)
    deps = Dependencies(crud.get_project(uid))
    after = assembly_versions.fingerprint(deps, assembly_versions.resolve(deps, definition))
    assert after != before


def test_version_renderer_creates_reviewed_original_only_output_and_receipt(make_project):
    from dubforge.pipeline import assembly_versions, version_render
    project, root, paths = _seed_text(make_project())
    ctx = MediaContext(project, root, paths)
    video, _, _ = _media(ctx)
    crud.add_artifact(project.id, 'download', 'video', video.name, video, 'media/video.mp4')
    definition = dict(id='source-review', name='原声处理版', dubbing=False,
                      burn_subtitles=False, include_comments=False, overrides={})
    version = assembly_versions.resolve(Dependencies(project), definition)
    ctx.task_data = {'version_specs': [version]}
    version_render.run(ctx)
    output = root / 'output' / 'final_version_source-review.mp4'
    assert output.is_file()
    samples, rate = _samples(output)
    assert _rms(samples, rate, 1.3, 1.7) < 0.0001
    records = assembly_versions.results(project.uid)
    stored = json.loads(records['source-review'].snapshot)['content_review_receipt']
    assert stored['signature'] and stored['mute_audio'] is True
    artifact = crud.latest_artifacts(project.id, ['assemble'])['assemble/' + output.name]
    content_review_service.assert_publishable(project, artifact)
