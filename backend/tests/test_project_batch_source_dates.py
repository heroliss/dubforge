"""批量日期检查只补资料，不改变执行目标；所有外部边界使用隔离替身。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import project_batches as batches, release_batches, source_dates
from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes
from test_configuration import save
from test_project_batches import mixed
from test_release_batch_targets import targets
from test_release_batches import batch_env


def cache_date(uid, value):
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    atomic_write_bytes(path, json.dumps({'source': {'upload_date': value}}).encode())


def test_only_explicit_post_check_fills_dates_and_keeps_requested_order(mixed, monkeypatch):
    ids = list(reversed(mixed['uids']))
    calls = []

    def fill(uids):
        calls.append(list(uids))
        for index, uid in enumerate(uids):
            cache_date(uid, f'2026-10-{10 + index:02}')
        return {uid: source_dates.read(uid) for uid in uids}

    monkeypatch.setattr(source_dates, 'ensure_many', fill)
    client = TestClient(app)
    before = client.get('/api/project-batches/candidates', params={'uids': ','.join(ids)})
    assert before.status_code == 200 and not calls
    assert all(item['source_date'] is None for item in before.json()['items'])
    response = client.post('/api/project-batches/candidates', json={'project_uids': ids})
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'no-store'
    assert calls == [ids]
    assert [item['project_uid'] for item in response.json()['items']] == ids
    assert all(item['source_date'] for item in response.json()['items'])
    assert not response.json()['warnings']
    client.post('/api/project-batches/plan', json={'project_uids': ids, 'order_confirmed': True})
    client.get('/api/project-batches')
    assert calls == [ids], '普通预检、排序与列表不能再次联网采集'
    assert not mixed['started'] and not batches._leases


@pytest.mark.parametrize('extra', [{'project_uids': []}, {'scope': {}}, {'order_confirmed': True}])
def test_bad_date_check_validates_before_any_fetch(mixed, monkeypatch, extra):
    monkeypatch.setattr(source_dates, 'ensure_many', lambda *_: pytest.fail('无效请求不能采集'))
    body = {'project_uids': [mixed['uids'][0]], **extra}
    response = TestClient(app).post('/api/project-batches/candidates', json=body)
    assert response.status_code == 400


def test_nonexistent_project_rejects_entire_check_before_fetch(mixed, monkeypatch):
    monkeypatch.setattr(source_dates, 'ensure_many', lambda *_: pytest.fail('无效项目不能部分采集'))
    response = TestClient(app).post('/api/project-batches/candidates',
        json={'project_uids': [mixed['uids'][0], 'ffffff']})
    assert response.status_code == 400


def test_unknown_date_does_not_warn_for_independent_or_single_item_collections(mixed):
    ids = [mixed['uids'][0], *mixed['independent']]
    assert not batches.candidates(ids)['warnings']
    result = batches.preview({'project_uids': ids, 'order_confirmed': True})
    assert release_batches.SOURCE_DATE_WARNING not in result['warnings']
    assert not any(release_batches.SOURCE_DATE_WARNING in group['warnings'] for group in result['groups'])
    assert not release_batches.candidates([mixed['uids'][0]], target_driven=True)['warnings']


def test_only_same_account_collection_section_requires_date_warning(mixed):
    left, right = mixed['uids'][:2]
    for changes in ({'publish.section_id': 9}, {'publish.season_id': 9}, {'publish.account_id': '456'}):
        save('project:' + right, {'publish.account_id': '123', 'publish.season_id': 1,
                                'publish.section_id': 2, **changes})
        assert not batches.candidates([left, right])['warnings']


def test_multivideo_missing_date_warns_once_in_relevant_group_and_never_blocks(mixed):
    ids = [*mixed['uids'][:2], *mixed['independent']]
    assert batches.candidates(ids)['warnings'] == [release_batches.SOURCE_DATE_WARNING]
    result = batches.preview({'project_uids': ids, 'order_confirmed': True})
    assert not result['blockers']
    assert release_batches.SOURCE_DATE_WARNING in result['groups'][0]['warnings']
    assert release_batches.SOURCE_DATE_WARNING not in result['warnings']
    for uid in mixed['uids'][:2]:
        cache_date(uid, '2026-10-19')
    assert not batches.candidates(ids)['warnings']
