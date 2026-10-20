"""评论阶段使用真实保存与处理事实，不伪造管线步骤或制作就绪。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_diagnostics as cd, comment_preparation as cp, comment_studio as cs
from dubforge import comment_workspace as cw, configuration, video_info
from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes
from test_comment_preparation import cache, request_body, forbid
from test_comment_studio import uid, plan_body


def write_document(path, plan):
    atomic_write_bytes(path, json.dumps({'overlay': plan}, ensure_ascii=False).encode())


def test_pending_and_explicit_empty_confirmation_are_different(tmp_path):
    path = tmp_path / 'comment_studio.json'
    assert cw.workspace(cw.saved_overlay(path))['state'] == 'pending'
    write_document(path, None)
    assert cw.workspace(cw.saved_overlay(path))['state'] == 'pending'
    write_document(path, {'items': [], 'settings': {}, 'confirmed_at': '2026-10-17T01:00:00+00:00'})
    result = cw.workspace(cw.saved_overlay(path))
    assert result['state'] == 'done' and result['label'] == '已保存'
    assert result['confirmed_count'] == 0 and '空上屏方案' in result['detail']
    assert '已就绪' not in result['detail']


@pytest.mark.parametrize('kind,record,label', [
    ('collection', {'busy': True, 'status': 'running', 'count': 10}, '正在获取评论'),
    ('filter', {'action': 'filter', 'status': 'running'}, '正在评论评分'),
    ('translate', {'action': 'translate', 'status': 'running'}, '正在翻译评论'),
    ('prepare', {'action': 'prepare', 'status': 'running'}, '正在准备上屏评论'),
])
def test_real_activity_has_priority_and_retains_saved_count(kind, record, label):
    saved = cw.SavedOverlay(saved=True, count=4)
    kwargs = {'collection': record} if kind == 'collection' else {'prepare_job': record} if kind == 'prepare' else {'jobs': {kind: record}}
    result = cw.workspace(saved, **kwargs)
    assert result['state'] == 'running' and result['label'] == label
    assert result['confirmed_count'] == 4 and '已保存 4 条' in result['detail']


@pytest.mark.parametrize('status,expected', [('failed', 'failed'), ('partial', 'attention'),
                                          ('interrupted', 'attention'), ('cancelled', 'attention')])
def test_unfinished_processing_without_confirmation_is_not_reported_as_saved(status, expected):
    result = cw.workspace(cw.SavedOverlay(), jobs={'filter': {'status': status}})
    assert result['state'] == expected and result['confirmed_count'] == 0
    assert '尚未保存' in result['detail'] and '仍保留' in result['detail']


def test_older_failure_does_not_erase_later_saved_plan_but_newer_failure_is_attention():
    saved = cw.SavedOverlay(saved=True, count=3, confirmed_at='2026-10-17T01:00:00+00:00')
    old = {'status': 'failed', 'finished_at': '2026-10-17T00:59:59+00:00'}
    result = cw.workspace(saved, jobs={'translate': old})
    assert result['state'] == 'done' and result['confirmed_count'] == 3
    assert result['label'] == '已保存' and '未完成处理记录' in result['detail']
    recent = {**old, 'finished_at': '2026-10-17T01:00:01+00:00'}
    result = cw.workspace(saved, jobs={'translate': recent})
    assert result['state'] == 'attention' and result['confirmed_count'] == 3
    assert '已有方案仍保留' in result['detail']


def test_draft_complete_is_attention_even_if_no_model_request_was_needed(uid, monkeypatch):
    cache(uid, translated=['0', '1', '2'])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    result = cp.prepare(uid, request_body(uid))
    assert result['prepare_job']['status'] == 'done'
    state = cs.progress(uid)
    assert state['workspace']['state'] == 'attention'
    assert state['workspace']['label'] == '待确认方案'
    assert state['workspace']['confirmed_count'] == 0
    assert cs.view(uid)['overlay'] is None


def test_same_second_same_content_save_is_distinguished_from_prepared_draft(uid, monkeypatch):
    cache(uid, translated=['0', '1', '2'])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    monkeypatch.setattr(cs, '_now', lambda: '2026-10-17T01:02:03+00:00')
    initial = cp.prepare(uid, request_body(uid))
    cs.save(uid, {**initial['preparation']['draft'], 'revision': initial['overlay_revision']})
    saved_signature = cs._hash(cs._read(crud.get_project(uid))['overlay'])
    assert cs.progress(uid)['workspace']['state'] == 'done'
    next_draft = cp.prepare(uid, request_body(uid))
    state = cs.progress(uid)
    assert next_draft['prepare_job']['overlay_signature'] == saved_signature
    assert state['workspace']['state'] == 'attention' and state['workspace']['confirmed_count'] == 2
    assert '已保存 2 条' in state['workspace']['detail']
    cs.save(uid, {**next_draft['preparation']['draft'], 'revision': next_draft['overlay_revision']})
    assert cs._hash(cs._read(crud.get_project(uid))['overlay']) == saved_signature
    assert cs.progress(uid)['workspace']['state'] == 'done'
    assert cs._prepare_jobs[uid]['confirmed_overlay_signature'] == saved_signature
    cp.prepare(uid, request_body(uid))
    assert 'confirmed_overlay_signature' not in cs._prepare_jobs[uid]
    assert cs.progress(uid)['workspace']['state'] == 'attention'


def test_rating_cache_write_is_not_mistaken_for_confirmation(uid, monkeypatch):
    cache(uid, translated=['0', '1', '2'])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    initial = cp.prepare(uid, request_body(uid))
    cs.save(uid, {**initial['preparation']['draft'], 'revision': initial['overlay_revision']})
    cp.prepare(uid, request_body(uid))
    before = cs.progress(uid)['workspace']
    project = crud.get_project(uid)
    doc = cs._read(project)
    doc['translations']['0']['text_zh'] += '，新增缓存内容'
    cs._write(project, doc)
    after = cs.progress(uid)['workspace']
    assert before == after and after['state'] == 'attention'


def test_failed_save_cannot_acknowledge_prepared_draft(uid, monkeypatch):
    cache(uid, translated=['0', '1', '2'])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid))
    monkeypatch.setattr(cs, '_write', lambda *_: (_ for _ in ()).throw(cs.CommentError('测试保存失败')))
    with pytest.raises(cs.CommentError, match='测试保存失败'):
        cs.save(uid, {**prepared['preparation']['draft'], 'revision': prepared['overlay_revision']})
    assert 'confirmed_overlay_signature' not in cs._prepare_jobs[uid]
    assert cs.progress(uid)['workspace']['state'] == 'attention'


def test_successfully_saved_empty_plan_is_exposed_through_real_api(uid):
    state = cs.view(uid)
    cs.save(uid, {'items': [], 'settings': state['defaults'], 'revision': state['overlay_revision']})
    response = TestClient(app).get(f'/api/projects/{uid}/comments/progress')
    assert response.status_code == 200
    value = response.json()['workspace']
    assert value['state'] == 'done' and value['label'] == '已保存'
    assert value['confirmed_count'] == 0 and '空上屏方案' in value['detail']


def test_progress_api_adds_summary_without_full_view_configuration_or_review(uid, monkeypatch):
    saved = cs.save(uid, plan_body(uid))
    def forbidden(*_args, **_kwargs):
        raise AssertionError('进度投影不能解析完整配置、逐条审核或读取完整工作区')
    monkeypatch.setattr(cs, 'view', forbidden)
    monkeypatch.setattr(cs, '_read', forbidden)
    monkeypatch.setattr(cs, '_rows', forbidden)
    monkeypatch.setattr(cs, '_review_settings', forbidden)
    monkeypatch.setattr(cs, 'effective_overlay', forbidden)
    monkeypatch.setattr(configuration, 'resolve', forbidden)
    response = TestClient(app).get(f'/api/projects/{uid}/comments/progress')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    value = response.json()
    assert {'job', 'jobs', 'collection', 'prepare_job', 'data_revision', 'workspace'} <= value.keys()
    assert value['workspace']['confirmed_count'] == len(saved['overlay']['items'])
    assert value['workspace']['state'] == 'done'
    assert set(value['workspace']) == {'state', 'label', 'detail', 'confirmed_count'}
    assert 'settings' not in value['workspace'] and 'items' not in value['workspace']


def test_restart_uses_interrupted_disk_diagnostics_and_retains_saved_plan(uid, monkeypatch):
    cs.save(uid, plan_body(uid))
    cd.Diagnostics(crud.get_project(uid), {'id': 'restart-fixture', 'action': 'filter', 'status': 'running'})
    monkeypatch.setattr(cs, '_jobs', {})
    monkeypatch.setattr(cs, '_prepare_jobs', {})
    monkeypatch.setattr(cs, '_diagnostics', {})
    monkeypatch.setattr(video_info, '_active', {})
    state = cs.progress(uid)
    assert state['prepare_job'] is None
    assert state['jobs']['filter']['status'] == 'interrupted'
    assert state['workspace']['state'] != 'running'
    assert state['workspace']['confirmed_count'] == 3
    assert '已保存 3 条' in state['workspace']['detail']


def test_invalid_document_yields_attention_without_breaking_other_progress_fields(uid):
    path = crud.project_dir(uid) / 'comment_studio.json'
    atomic_write_bytes(path, b'{broken')
    response = TestClient(app).get(f'/api/projects/{uid}/comments/progress')
    assert response.status_code == 200
    value = response.json()
    assert value['workspace']['state'] == 'attention'
    assert '无法读取' in value['workspace']['detail']
    assert 'collection' in value and 'data_revision' in value


def test_summary_cache_reuses_unchanged_file_and_invalidates_on_atomic_replace(tmp_path, monkeypatch):
    cw._read_saved_overlay.cache_clear()
    path = tmp_path / 'comment_studio.json'
    write_document(path, {'items': [], 'settings': {}})
    calls = []
    original = Path.read_text
    def tracked(file, *args, **kwargs):
        calls.append(file)
        return original(file, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', tracked)
    first = cw.saved_overlay(path)
    assert cw.saved_overlay(path) == first and len(calls) == 1
    write_document(path, {'items': [{'id': 'new'}], 'settings': {}})
    second = cw.saved_overlay(path)
    assert second.count == 1 and second.signature != first.signature and len(calls) == 2
    path.unlink()
    assert cw.saved_overlay(path).saved is False


def test_summary_cache_keeps_at_most_64_small_records(tmp_path):
    cw._read_saved_overlay.cache_clear()
    for index in range(70):
        path = tmp_path / f'project-{index}.json'
        write_document(path, {'items': [], 'settings': {}})
        cw.saved_overlay(path)
    info = cw._read_saved_overlay.cache_info()
    assert info.maxsize == info.currsize == 64
