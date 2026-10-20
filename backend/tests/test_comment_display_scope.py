"""自动上屏按真实分P显示语言校验；公共参数不自动变化，审查与人工入口不放宽。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio as cs, configuration
from dubforge.utils.fs import atomic_write_bytes
from test_comment_studio import uid, replace_comment_text


def body(uid, *, translated=''):
    state = cs.view(uid)
    cfg = state['configuration']
    settings = {key: cfg['values']['comments.' + key] for key in cs.DEFAULT_SETTINGS}
    row = state['rows'][0]
    return dict(revision=state['overlay_revision'], configuration_fingerprint=cfg['fingerprint'],
                settings=settings, items=[dict(id=row['id'], source_hash=row['source_hash'],
                    display_text=translated, source_text=row['text'])])


def displays(value, *modes):
    return [{**value['settings'], 'language_mode': mode} for mode in modes]


def test_original_only_internal_confirmation_keeps_public_language_and_configuration(uid):
    value = body(uid)
    assert value['settings']['language_mode'] == 'translated'
    before = configuration.resolve(uid)
    result = cs.save(uid, value, display_settings=displays(value, 'original'))
    after = configuration.resolve(uid)
    assert after['revision'] == before['revision'] and after['fingerprint'] == before['fingerprint']
    assert after['overrides'] == before['overrides']
    assert result['overlay']['settings'] == value['settings']
    assert result['overlay']['items'][0]['display_text'] == ''
    assert cs.confirmed(uid)['items'][0]['source_text'] == value['items'][0]['source_text']


@pytest.mark.parametrize('modes', [('translated',), ('original', 'translated'), ('original', 'bilingual')])
def test_internal_confirmation_checks_every_actual_version(uid, modes):
    value = body(uid)
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, value, display_settings=displays(value, *modes))
    assert error.value.code == 'COMMENTS_DISPLAY_TEXT'
    assert cs.view(uid)['overlay'] is None


def test_complete_translation_supports_mixed_display_without_changing_defaults(uid):
    value = body(uid, translated='完整中文译文')
    before = configuration.resolve(uid)
    result = cs.save(uid, value, display_settings=displays(value, 'original', 'bilingual', 'translated'))
    assert len(result['overlay']['items']) == 1
    assert configuration.resolve(uid)['fingerprint'] == before['fingerprint']


def test_hidden_existing_translation_still_undergoes_local_review_in_original_only_mode(uid):
    value = body(uid, translated='天安门广场的悲剧')
    value['items'][0]['selection_origin'] = 'automatic'
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, value, display_settings=displays(value, 'original'))
    assert error.value.code == 'COMMENTS_REVIEW_EMPTY'


def test_authoritative_translation_cache_cannot_be_hidden_by_blank_display(uid):
    value = body(uid)
    value['items'][0]['selection_origin'] = 'automatic'
    project = cs._project(uid)
    doc = dict(translations={value['items'][0]['id']: dict(source_hash=value['items'][0]['source_hash'],
                                                        text_zh='天安门广场的悲剧')})
    atomic_write_bytes(cs._path(project), json.dumps(doc, ensure_ascii=False).encode())
    # 当前 draft 审查权威原文与提交文字；不依赖客户端提供隐藏译文。
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, value, display_settings=displays(value, 'original'))
    assert error.value.code == 'COMMENTS_REVIEW_EMPTY'


def test_stale_translation_cache_does_not_block_new_source(uid):
    value = body(uid)
    project = cs._project(uid)
    doc = dict(translations={value['items'][0]['id']: dict(source_hash='old-source-hash', text_zh='天安门广场的悲剧')})
    atomic_write_bytes(cs._path(project), json.dumps(doc, ensure_ascii=False).encode())
    result = cs.save(uid, value, display_settings=displays(value, 'original'))
    assert len(result['overlay']['items']) == 1


@pytest.mark.parametrize('hash_valid', [True, False])
def test_confirmed_original_plan_rechecks_later_valid_translation_without_rewriting(uid, hash_valid):
    value = body(uid)
    cs.save(uid, value, display_settings=displays(value, 'original'))
    project = cs._project(uid)
    doc = cs._read(project)
    plan_before = json.dumps(doc['overlay'], ensure_ascii=False, sort_keys=True)
    raw_path = cs.crud.project_dir(uid) / 'artifacts/download/video_info.json'
    raw_before = raw_path.read_bytes()
    doc['translations'] = {value['items'][0]['id']: dict(
        source_hash=value['items'][0]['source_hash'] if hash_valid else 'old-source-hash',
        text_zh='天安门广场的悲剧')}
    atomic_write_bytes(cs._path(project), json.dumps(doc, ensure_ascii=False).encode())
    document_before = cs._path(project).read_bytes()
    state = cs.view(uid)
    assert bool(state['overlay']['items'][0].get('review_blocked')) == hash_valid
    if hash_valid:
        with pytest.raises(cs.CommentError) as error:
            cs.confirmed(uid)
        assert error.value.code == 'COMMENTS_REVIEW_EMPTY'
    else:
        assert len(cs.confirmed(uid)['items']) == 1
    assert cs._path(project).read_bytes() == document_before
    assert json.dumps(cs._read(project)['overlay'], ensure_ascii=False, sort_keys=True) == plan_before
    assert raw_path.read_bytes() == raw_before


def test_authoritative_original_remains_checked_for_trusted_display_settings(uid):
    replace_comment_text(uid, '0', 'The Tiananmen Square massacre')
    value = body(uid, translated='普通中文')
    value['items'][0]['selection_origin'] = 'automatic'
    value['items'][0]['source_text'] = 'A lovely trip'
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, value, display_settings=displays(value, 'original'))
    assert error.value.code == 'COMMENTS_REVIEW_EMPTY'


def test_display_scope_in_request_body_cannot_weaken_manual_confirmation(uid):
    value = body(uid)
    value['display_settings'] = displays(value, 'original')
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, value)
    assert error.value.code == 'COMMENTS_DISPLAY_TEXT'
    with TestClient(app) as client:
        response = client.put(f'/api/projects/{uid}/comments/overlay', json=value)
    assert response.status_code == 400 and response.json()['code'] == 'COMMENTS_DISPLAY_TEXT'


@pytest.mark.parametrize('invalid', [[], {}, 'original', [None]])
def test_internal_display_scope_cannot_skip_all_validation(uid, invalid):
    with pytest.raises(cs.CommentError):
        cs.save(uid, body(uid), display_settings=invalid)


def test_original_length_is_checked_even_when_public_default_is_translated(uid):
    replace_comment_text(uid, '0', 'word ' * 130)
    value = body(uid, translated='短译文')
    with pytest.raises(cs.CommentError) as error:
        cs.save(uid, value, display_settings=displays(value, 'original'))
    assert error.value.code == 'COMMENTS_DISPLAY_TEXT'
