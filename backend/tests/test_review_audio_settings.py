"""声音屏蔽设置沿用版本校验，兼容旧页面并按层级恢复。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import content_review_service as service
from test_content_redaction import _seed_text

pytestmark = pytest.mark.usefixtures('isolated_env')


def _body(report, **updates):
    return dict(revision=report['revision'], configuration_fingerprint=report['configuration_fingerprint'],
                enabled=True, mute_audio=True, **updates)


def test_audio_mode_default_save_legacy_page_and_reset(make_project):
    uid = make_project()
    _seed_text(uid)
    first = service.report(uid)
    assert first['audio_mode'] == 'voice_only'
    saved = service.save(uid, _body(first, audio_mode='all_audio'))
    assert saved['audio_mode'] == 'all_audio' and not saved['inherited']
    # 缺少模式字段的旧页面不得把用户选择重置成新默认。
    legacy = service.save(uid, _body(saved, blocked_terms='某词'))
    assert legacy['audio_mode'] == 'all_audio'
    restored = service.save(uid, dict(revision=legacy['revision'],
        configuration_fingerprint=legacy['configuration_fingerprint'], reset=True))
    assert restored['audio_mode'] == 'voice_only' and restored['inherited']


@pytest.mark.parametrize('mode', ['original_audio', None, [], {'mode': 'all_audio'}])
def test_invalid_audio_mode_is_rejected_without_saving(make_project, mode):
    from dubforge.pipeline import content_redaction as redaction
    with pytest.raises(redaction.ContentRedactionError):
        redaction.validate_settings({'content_review.audio_mode': mode})
    uid = make_project()
    first = service.report(uid)
    with TestClient(app) as client:
        response = client.put(f'/api/projects/{uid}/content-review', json=_body(first, audio_mode=mode))
    assert response.status_code == 400
    assert response.json()['code'] == 'CONTENT_REVIEW_INVALID'
    fresh = service.report(uid)
    assert fresh['revision'] == first['revision'] and fresh['audio_mode'] == 'voice_only'


def test_audio_mode_cannot_be_changed_from_stale_report(make_project):
    uid = make_project()
    first = service.report(uid)
    service.save(uid, _body(first, audio_mode='all_audio'))
    with TestClient(app) as client:
        response = client.put(f'/api/projects/{uid}/content-review', json=_body(first, audio_mode='voice_only'))
    assert response.status_code == 400 and response.json()['code'] == 'CONTENT_REVIEW_CONFLICT'
    assert service.report(uid)['audio_mode'] == 'all_audio'


@pytest.mark.parametrize('settings', [{}, {'content_review.enabled': False}, {'content_review.mute_audio': False}])
def test_no_audio_effect_does_not_probe_video_during_status_reads(make_project, monkeypatch, settings):
    from dubforge.pipeline import content_redaction as redaction
    from test_content_redaction import MediaContext, _media
    project, root, paths = _seed_text(make_project(),
        source=[dict(id=1, start=0, end=1, text='A clean sentence')], target={1: '正常介绍'})
    ctx = MediaContext(project, root, paths, values=settings)
    _media(ctx)
    monkeypatch.setattr(redaction, '_video_duration', lambda *args: pytest.fail('无声音处理不能启动媒体探测'))
    audio = redaction.audio_plan(ctx)
    assert not audio['ranges'] and audio['effective_mode'] == 'unchanged'
