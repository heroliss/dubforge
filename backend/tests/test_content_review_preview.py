"""短片实际静音、缓存、并发与旧证据阻断；所有媒体都来自隔离夹具。"""
import pytest
import numpy as np
from fastapi.testclient import TestClient

from app.main import app
from dubforge import content_review_preview as preview, content_review_service as service
from dubforge.domain import crud
from dubforge.pipeline.content_redaction import plan
from dubforge.pipeline.dependencies import Dependencies
from test_content_redaction import MediaContext, _media, _rms, _samples, _seed_text

pytestmark = pytest.mark.usefixtures('isolated_env')


def _request(uid):
    report = service.report(uid)
    return {**{key: report[key] for key in ('revision', 'configuration_fingerprint', 'decision_revision')},
            **{key: report['findings'][0][key] for key in ('key', 'evidence_fingerprint')}}


def _fixture(make_project):
    uid = make_project()
    project, root, paths = _seed_text(uid)
    _media(MediaContext(project, root, paths))
    return uid, project, root


def test_real_source_comparison_mutes_only_current_effect_and_preserves_inputs(make_project, monkeypatch):
    uid, project, root = _fixture(make_project)
    monkeypatch.setattr(preview, '_prune', lambda *args: None)
    text_before = (root / 'artifacts' / 'split' / 'split.txt').read_bytes()
    steps_before = {step.step_key: step.status for step in crud.get_steps(project.id)}
    result = preview.generate(uid, _request(uid))
    assert result['scope'] == 'source_audio'
    assert result['subtitles_available'] is True
    assert result['end'] - result['start'] <= 20
    assert result['hidden_ids'] == [2]
    assert result['muted_ranges'] == [{'start': .85, 'end': 2.15}]
    original = preview.media(uid, result['fingerprint'], 'original')
    processed = preview.media(uid, result['fingerprint'], 'processed')
    raw, rate = _samples(original)
    masked, rate = _samples(processed)
    assert _rms(raw, rate, 1.25, 1.75) > .04
    assert _rms(masked, rate, 1.25, 1.75) < .001
    assert _rms(masked, rate, 2.6, 3.0) > .04
    assert (root / 'artifacts' / 'split' / 'split.txt').read_bytes() == text_before
    assert {step.step_key: step.status for step in crud.get_steps(project.id)} == steps_before
    assert not list((root / 'artifacts' / 'content_review_preview').glob('*.tmp'))
    monkeypatch.setattr(preview, '_encode', lambda *args: pytest.fail('相同输入应复用核对短片'))
    assert preview.generate(uid, _request(uid)) == result
    raw_report = service.report(uid)
    raw_body = {**_request(uid), **{key: raw_report['original_findings'][0][key] for key in ('key', 'evidence_fingerprint')}}
    # 同一片段可被原稿与字幕命中引用，缓存响应仍携带本次请求的证据键。
    assert preview.generate(uid, raw_body)['key'] == raw_body['key']
    with TestClient(app) as client:
        response = client.get(result['processed_url'], headers={'Range': 'bytes=0-15'})
        assert response.status_code == 206
        assert len(response.content) == 16
        assert client.head(result['original_url']).status_code == 200


def test_long_finding_is_bounded_and_marks_truncation():
    result = preview._clip(dict(start=100, end=160, source_audio_start=99, source_audio_end=160), 300)
    assert result['start'] == 97 and result['end'] == 117
    assert result['truncated'] is True
    with pytest.raises(preview.ReviewPreviewError):
        preview._clip(dict(start=300, end=310), 200)


def test_original_and_masked_subtitles_share_track_and_timing(make_project):
    uid, _, _ = _fixture(make_project)
    deps = Dependencies(crud.get_project(uid))
    from dubforge.pipeline.subtitle_document import read
    rows = read(deps, translation=False, strict=False)
    ranges = plan(deps)['ranges']
    original = preview._cues(deps, rows, ranges, .5, 3, processed=False)
    processed = preview._cues(deps, rows, ranges, .5, 3, processed=True)
    assert 2 in [cue.index for cue in original]
    assert 2 not in [cue.index for cue in processed]
    assert all(cue.start >= 0 and cue.end <= 2.5 for cue in original + processed)
    assert any('悲剧' in cue.text for cue in original)
    assert not any('悲剧' in cue.text for cue in processed)


def test_mute_disabled_is_honest_and_keeps_audio(make_project, monkeypatch):
    uid, _, _ = _fixture(make_project)
    report = service.report(uid)
    service.save(uid, {**{key: report[key] for key in ('revision', 'configuration_fingerprint')},
                       'enabled': True, 'mute_audio': False})
    # 只检查下发到实际编码器的滤镜，真静音已由上面的集成样例验证。
    filters = []
    def encode(video, target, deps, cues, clip, audio):
        filters.append(audio)
        target.write_bytes(b'isolated-preview')
    monkeypatch.setattr(preview, '_encode', encode)
    result = preview.generate(uid, _request(uid))
    assert filters == ['', '']
    assert result['muted_ranges'] == []
    assert result['hidden_ids'] == [2]


def test_old_finding_revision_is_rejected_before_encoding(make_project, monkeypatch):
    uid, _, _ = _fixture(make_project)
    body = _request(uid)
    service.save_decision(uid, {**body, 'decision': 'block'})
    monkeypatch.setattr(preview, '_encode', lambda *args: pytest.fail('旧证据不能启动编码'))
    with TestClient(app) as client:
        result = client.post(f'/api/projects/{uid}/content-review/preview', json=body)
    assert result.status_code == 409
    assert result.json()['code'] == 'CONTENT_REVIEW_PREVIEW_CONFLICT'


def test_changes_during_encoding_never_publish_old_comparison(make_project, monkeypatch):
    uid, _, root = _fixture(make_project)
    body = _request(uid)
    calls = []
    def encode(video, target, deps, cues, clip, audio):
        target.write_bytes(b'isolated-preview')
        calls.append(audio)
        if len(calls) == 1:
            service.save_decision(uid, {**body, 'decision': 'block'})
    monkeypatch.setattr(preview, '_encode', encode)
    with pytest.raises(preview.ReviewPreviewError) as raised:
        preview.generate(uid, body)
    assert raised.value.code == 'CONTENT_REVIEW_PREVIEW_CONFLICT'
    folder = root / 'artifacts' / 'content_review_preview'
    assert not list(folder.glob('*.mp4')) and not list(folder.glob('*.tmp'))


def test_encoder_exception_releases_slot_and_does_not_leave_partial_output(make_project, monkeypatch):
    uid, _, root = _fixture(make_project)
    def fail(*args):
        args[1].write_bytes(b'partial')
        raise preview.ReviewPreviewError('测试编码失败')
    monkeypatch.setattr(preview, '_encode', fail)
    with pytest.raises(preview.ReviewPreviewError):
        preview.generate(uid, _request(uid))
    assert preview._ENCODING.acquire(blocking=False)
    preview._ENCODING.release()
    assert not list((root / 'artifacts' / 'content_review_preview').glob('*.tmp'))


def test_busy_is_immediate_and_invalid_media_path_cannot_escape_project(make_project):
    uid, _, _ = _fixture(make_project)
    assert preview._ENCODING.acquire(blocking=False)
    try:
        with TestClient(app) as client:
            response = client.post(f'/api/projects/{uid}/content-review/preview', json=_request(uid))
        assert response.status_code == 429
    finally:
        preview._ENCODING.release()
    with pytest.raises(preview.ReviewPreviewError):
        preview.media(uid, '..', 'processed')
    with pytest.raises(preview.ReviewPreviewError):
        preview.media(uid, 'a' * 64, '../original')


def test_voice_only_comparison_fills_background_at_nonzero_clip_offset(make_project, monkeypatch):
    uid = make_project()
    source = [dict(id=1, start=10.0, end=11.0, text='the tragedy of Tiananmen Square')]
    project, root, paths = _seed_text(uid, source=source, target={1: '天安门广场的悲剧'})
    _, _, background = _media(MediaContext(project, root, paths), duration=14)
    crud.add_artifact(project.id, 'preprocess', 'audio', 'background.wav', background,
                      str(background.relative_to(root)))
    # 此测试只核对短片的寻址和音频处理；模型分离步骤由独立测试覆盖。
    fresh = Dependencies.fresh
    monkeypatch.setattr(Dependencies, 'fresh', lambda self, key: True if key == 'preprocess' else fresh(self, key))
    result = preview.generate(uid, _request(uid))
    assert result['start'] == 8.0
    assert result['audio_processing']['effective_mode'] == 'voice_only'
    original, rate = _samples(preview.media(uid, result['fingerprint'], 'original'))
    processed, rate = _samples(preview.media(uid, result['fingerprint'], 'processed'))
    def tone(samples, frequency, start=2.3, end=2.7):
        segment = samples[round(start * rate):round(end * rate)]
        times = np.arange(len(segment)) / rate
        return abs(2 * np.mean(segment * np.exp(-2j * np.pi * frequency * times)))
    assert tone(original, 200) > .05
    assert tone(processed, 150) > .05
    assert tone(processed, 200) < .005
    assert _rms(processed, rate, .4, .8) > .04
    assert not any('改为全部声音静音' in warning for warning in result['warnings'])
