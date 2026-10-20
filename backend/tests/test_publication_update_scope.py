"""原BV完整更新范围：平台替身，验证不上传旧P、不漏P与断点恢复。"""
import copy
import json

import pytest
from sqlmodel import Session

from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.publishing import service as pub, studio, updates, transport
from dubforge.publishing.models import PublicationUpdate
from test_publishing import setup_publish
from test_publication_updates import env, start


@pytest.fixture
def scope(env, monkeypatch):
    original = studio._remote
    def apply(account, operation, payload):
        result = original(account, operation, payload)
        if operation == 'write' and payload.get('kind') == 'edit' and payload.get('videos'):
            env['remote']['videos'] = copy.deepcopy(payload['videos'])
        return result
    monkeypatch.setattr(studio, '_remote', apply)
    return env


def test_part_names_and_order_update_without_any_video_upload(scope):
    old = copy.deepcopy(scope['remote']['videos'])
    task = updates.plan(scope['source']['id'], dict(part_titles={str(old[0]['cid']): '  更新后的原声  '},
        part_order=[old[1]['cid'], old[0]['cid']]))
    assert not scope['writes']
    result = start(task)
    assert result['status'] == 'completed', result
    assert not scope['uploads'] and len(scope['writes']) == 1
    final = scope['remote']['videos']
    assert [(p['cid'], p['title']) for p in final] == [(old[1]['cid'], old[1]['title']), (old[0]['cid'], '更新后的原声')]
    assert [p['filename'] for p in final] == [old[1]['filename'], old[0]['filename']]


def test_replacing_and_appending_reorders_explicit_full_parts_and_never_drops_original(scope):
    project = crud.get_project(scope['uid'])
    root = crud.project_dir(scope['uid'])
    path = root / 'output/final_src_new.mp4'
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b'new-third-video')
    extra = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    old = copy.deepcopy(scope['remote']['videos'])
    task = updates.plan(scope['source']['id'], dict(
        replacements=[dict(old_cid=old[0]['cid'], artifact_id=scope['artifacts'][0].id)],
        additions=[dict(artifact_id=extra.id, title='  新增解说  ')],
        part_titles={str(old[0]['cid']): '原声新版'}, part_order=[-1, old[1]['cid'], old[0]['cid']]))
    result = start(task)
    assert result['status'] == 'completed', result
    assert len(scope['uploads']) == 2 and len(scope['writes']) == 1
    payload = scope['writes'][0]
    assert [p['old_cid'] for p in payload['replacements']] == [old[0]['cid'], 0]
    assert [(p['cid'], p['title']) for p in scope['remote']['videos']] == [
        (2000 + extra.id, '新增解说'), (old[1]['cid'], old[1]['title']),
        (2000 + scope['artifacts'][0].id, '原声新版')]
    assert start(result, 'retry')['status'] == 'completed' and len(scope['writes']) == 1


@pytest.mark.parametrize('body', [dict(part_order=[1000]), dict(part_order=[1000, 1000]),
    dict(part_titles={'unbound': '新名称'}), dict(part_titles={'1000': ''}),
    dict(values={'visibility': 'self'}), dict(values={'copyright': 2, 'source': ''})])
def test_unconfirmed_removal_unknown_fields_and_invalid_combination_are_rejected(scope, body):
    with pytest.raises(DubForgeError):
        updates.plan(scope['source']['id'], body)
    assert not scope['uploads'] and not scope['writes']


def test_cover_only_uploads_once_and_does_not_upload_video(scope):
    key = pub.store_cover(b'\x89PNG\r\n\x1a\nfixture')['cover_id']
    task = updates.plan(scope['source']['id'], dict(cover_id=key))
    result = start(task)
    assert result['status'] == 'completed', result
    assert not scope['uploads'] and len(scope['writes']) == 1
    assert scope['writes'][0]['values'] == dict(cover='https://i0.hdslb.com/test.jpg')
    assert scope['remote']['settings']['cover'] == 'https://i0.hdslb.com/test.jpg'
    assert start(result, 'retry')['status'] == 'completed' and len(scope['writes']) == 1


def test_unchanged_metadata_and_part_names_are_successful_without_writes(scope):
    old = scope['remote']['videos']
    task = updates.plan(scope['source']['id'], dict(values={'title': scope['remote']['settings']['title']},
        part_titles={str(old[0]['cid']): old[0]['title']}, part_order=[p['cid'] for p in old]))
    assert start(task)['status'] == 'completed'
    assert not scope['uploads'] and not scope['writes']


def test_changed_cover_stops_before_upload(scope):
    key = pub.store_cover(b'\x89PNG\r\n\x1a\nfixture')['cover_id']
    task = updates.plan(scope['source']['id'], dict(cover_id=key))
    pub._cover_path(key).write_bytes(b'changed')
    result = start(task)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_UPDATE_FILE_CHANGED'
    assert not scope['uploads'] and not scope['writes']


def test_uploaded_cover_already_matches_remote_is_a_successful_noop(scope):
    scope['remote']['settings']['cover'] = 'https://i0.hdslb.com/test.jpg'
    key = pub.store_cover(b'\x89PNG\r\n\x1a\nfixture')['cover_id']
    task = updates.plan(scope['source']['id'], dict(cover_id=key))
    result = start(task)
    assert result['status'] == 'completed', result
    assert not scope['uploads'] and not scope['writes']
    assert result['execution']['stages']['edit']['status'] == 'completed'


def test_new_category_copyright_and_source_are_validated_as_merged_metadata(scope):
    values = dict(human_type2=1001, tid=21, copyright=1, source='https://example.test/original', declaration_id=1)
    task = updates.plan(scope['source']['id'], dict(values=values))
    assert start(task)['status'] == 'completed'
    assert not scope['uploads'] and len(scope['writes']) == 1
    assert scope['remote']['settings']['copyright'] == 1
    with pytest.raises(DubForgeError):
        studio.validate_edit_values({'cover': 'https://not-bili.example/private.png'})


def test_chapters_without_a_proven_timeline_cannot_silently_succeed(scope, monkeypatch):
    monkeypatch.setattr(updates, '_proven_parts', lambda source: {})
    with pytest.raises(DubForgeError, match='没有可核验的制作时间轴'):
        updates.plan(scope['source']['id'], dict(chapters={'enabled': True}))
    assert not scope['uploads'] and not scope['writes']


def test_other_metadata_updates_explain_unproved_chapters_are_preserved(scope, monkeypatch):
    monkeypatch.setattr(updates, '_proven_parts', lambda source: {})
    task = updates.plan(scope['source']['id'], dict(values={'description': '新简介'}, chapters={'enabled': True}))
    assert task['preview']['chapters']['parts'] == []
    assert any('本次保留其B站章节' in w for w in task['preview']['warnings'])


def test_invalid_appended_title_is_rejected_before_any_upload(scope):
    with pytest.raises(DubForgeError):
        updates.plan(scope['source']['id'], dict(additions=[dict(artifact_id=scope['artifacts'][0].id, title='新\n名称')]))
    assert not scope['uploads'] and not scope['writes']
