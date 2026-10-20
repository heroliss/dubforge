"""评分负责选人，发布时间负责默认上屏顺序；模型与工程数据全部隔离。"""
import copy
import json

import pytest

from dubforge import comment_preparation as cp, comment_studio as cs
from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes
from test_comment_preparation import cache, forbid, request_body, selected
from test_comment_studio import seed


@pytest.fixture
def uid(make_project):
    uid = make_project()
    seed(uid, 5)
    return uid


def timestamps(uid, values):
    path = crud.project_dir(uid) / 'artifacts/download/video_info.json'
    info = json.loads(path.read_text(encoding='utf-8'))
    for row, value in zip(info['comments']['items'], values, strict=True):
        row['timestamp'] = value
    atomic_write_bytes(path, json.dumps(info).encode())


def test_preparation_selects_high_scores_then_orders_chronologically(uid, monkeypatch):
    timestamps(uid, [300, 100, 200, 1, 2])
    cache(uid, translated=[str(i) for i in range(5)])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 3))['preparation']['draft']
    assert [row['id'] for row in prepared['items']] == ['1', '2', '0']
    assert [row['timestamp'] for row in prepared['items']] == [100, 200, 300]
    assert prepared['order_mode'] == 'chronological'
    assert cs.view(uid)['overlay'] is None


def test_manual_selection_is_retained_without_being_pinned_before_earlier_comments(uid, monkeypatch):
    timestamps(uid, [300, 100, 200, 1, 400])
    cache(uid, translated=[str(i) for i in range(5)])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 3, items=selected(uid, ['4'])))['preparation']['draft']
    assert [row['id'] for row in prepared['items']] == ['1', '0', '4']
    assert prepared['items'][-1]['selection_origin'] == 'manual'
    assert prepared['items'][-1]['display_text'] == '人工已核对文字'


def test_legacy_cache_without_times_falls_back_to_collection_order_after_score_selection(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    project = crud.get_project(uid)
    doc = cs._read(project)
    for row in doc['rating_cache']['assessments']:
        row['score'] = 80 + int(row['id'])
    cs._write(project, doc)
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 3))['preparation']['draft']
    assert [row['id'] for row in prepared['items']] == ['2', '3', '4']
    assert all(row['timestamp'] is None for row in prepared['items'])


def test_unknown_and_equal_timestamps_keep_source_order_not_score_or_manual_order(uid, monkeypatch):
    timestamps(uid, [200, None, 100, 100, -1])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 5, items=selected(uid, ['4', '3', '2', '1', '0'])))['preparation']['draft']
    assert [row['id'] for row in prepared['items']] == ['2', '3', '0', '1', '4']
    assert [row['timestamp'] for row in prepared['items']] == [100, 100, 200, None, None]


def test_old_source_draft_uses_its_saved_timestamp_instead_of_new_source_time():
    sources = [dict(id='old', source_hash='new-hash', timestamp=10),
               dict(id='current', source_hash='current-hash', timestamp=100)]
    old = dict(id='old', source_hash='old-hash', timestamp=200)
    current = dict(id='current', source_hash='current-hash', timestamp=999)
    assert [row['id'] for row in cs.chronological_items([old, current], sources)] == ['current', 'old']
    old.pop('timestamp')
    assert [row['id'] for row in cs.chronological_items([old, current], sources)] == ['current', 'old']


def test_explicit_manual_order_keeps_survivors_then_appends_new_items_by_time(uid, monkeypatch):
    timestamps(uid, [300, 100, 200, 50, 400])
    cache(uid, translated=[str(i) for i in range(5)])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    old = selected(uid, ['2'], origin='automatic') + selected(uid, ['4']) + selected(uid, ['0'], origin='automatic')
    prepared = cp.prepare(uid, request_body(uid, 5, items=old, order_mode='manual'))['preparation']['draft']
    assert [row['id'] for row in prepared['items']] == ['2', '4', '0', '3', '1']
    assert prepared['order_mode'] == 'manual'
    assert [row['selection_origin'] for row in prepared['items']] == ['automatic', 'manual', 'automatic', 'automatic', 'automatic']


def test_save_preserves_preview_order_and_mode_but_not_client_timestamps(uid):
    timestamps(uid, [300, 100, 200, None, 400])
    state = cs.view(uid)
    items = selected(uid, ['2', '0', '1'])
    items[0]['timestamp'] = 999999999
    saved = cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'], items=items,
                             order_mode='chronological'))['overlay']
    assert [row['id'] for row in saved['items']] == ['2', '0', '1']
    assert [row['timestamp'] for row in saved['items']] == [200, 300, 100]
    assert saved['order_mode'] == 'chronological'
    body = dict(revision=cs.view(uid)['overlay_revision'], settings=state['defaults'], items=saved['items'], order_mode='manual')
    saved = cs.save(uid, body)['overlay']
    assert saved['order_mode'] == 'manual'
    assert cs.draft(uid, dict(settings=state['defaults'], items=saved['items']))['order_mode'] == 'manual'


def test_saved_manual_order_is_reused_by_later_preparation_unless_explicitly_reset(uid, monkeypatch):
    timestamps(uid, [300, 100, 200, None, 400])
    state = cs.view(uid)
    cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'],
                     items=selected(uid, ['0', '1']), order_mode='manual'))
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    manual = cp.prepare(uid, request_body(uid, 2))['preparation']['draft']
    assert manual['order_mode'] == 'manual'
    assert [row['id'] for row in manual['items']] == ['0', '1']
    chronological = cp.prepare(uid, request_body(uid, 2, order_mode='chronological'))['preparation']['draft']
    assert [row['id'] for row in chronological['items']] == ['1', '0']


def test_legacy_saved_order_is_not_rewritten_until_prepare(uid, monkeypatch):
    timestamps(uid, [300, 100, 200, None, 400])
    state = cs.view(uid)
    cs.save(uid, dict(revision=state['overlay_revision'], settings=state['defaults'], items=selected(uid, ['0', '1'])))
    project = crud.get_project(uid)
    doc = cs._read(project)
    doc['overlay'].pop('order_mode')
    cs._write(project, doc)
    before = cs._path(project).read_bytes()
    assert [row['id'] for row in cs.view(uid)['overlay']['items']] == ['0', '1']
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    prepared = cp.prepare(uid, request_body(uid, 2))['preparation']['draft']
    assert [row['id'] for row in prepared['items']] == ['1', '0']
    assert cs._path(project).read_bytes() == before


def test_timestamp_metadata_does_not_invalidate_translation_rating_or_source_cache(uid, monkeypatch):
    cache(uid, translated=[str(i) for i in range(5)])
    before = cs.view(uid)
    timestamps(uid, [300, 100, 200, None, 400])
    after = cs.view(uid)
    assert before['source_version'] == after['source_version']
    assert [row['source_hash'] for row in before['rows']] == [row['source_hash'] for row in after['rows']]
    assert after['rating_progress']['saved'] == 5
    assert all(row['text_zh'] for row in after['rows'])
    monkeypatch.setattr(cs, 'chat_validated', forbid)
    assert cp.prepare(uid, request_body(uid, 2))['preparation']['summary']['new_ratings'] == 0


def test_timestamp_and_order_mode_are_not_render_input_but_actual_array_order_is(uid):
    items = selected(uid, ['0', '1'])
    original = dict(settings=cs.default_settings(), items=items, order_mode='chronological')
    modified = copy.deepcopy(original)
    modified['order_mode'] = 'manual'
    modified['items'][0]['timestamp'] = 9999
    assert cs.render_fingerprint_plan(original) == cs.render_fingerprint_plan(modified)
    modified['items'].reverse()
    assert cs.render_fingerprint_plan(original) != cs.render_fingerprint_plan(modified)


@pytest.mark.parametrize('value', [None, 0, -1, True, '100', float('nan'), float('inf'), 10 ** 400])
def test_invalid_publication_times_are_unknown(value):
    assert cs._comment_timestamp(value) is None


@pytest.mark.parametrize('value', ['score', '', None, 1, [], {}])
def test_invalid_order_mode_is_rejected_before_saving(uid, value):
    state = cs.view(uid)
    with pytest.raises(cs.CommentError, match='上屏顺序无效'):
        cs.draft(uid, dict(settings=state['defaults'], items=[], order_mode=value))
