"""评论采集进度只读取真实快照与生命周期；所有采集使用隔离桩。"""
import json

import pytest

from test_comment_collection_snapshot import project, seed, snapshot, source_rows, path, OTHER_SOURCE
from dubforge import video_info as vi
from dubforge.domain import crud


def test_existing_snapshot_has_real_counts_without_inventing_execution_times(project):
    initial = vi.comment_progress(project.uid)
    assert initial['status'] == 'idle' and initial['count'] == 0 and not initial['busy']
    assert initial['started_at'] is initial['finished_at'] is initial['elapsed_seconds'] is None
    saved = snapshot(3)
    seed(project, saved)
    state = vi.comment_progress(project.uid)
    assert state['status'] == 'completed' and state['count'] == 3 and state['limit'] == 50
    assert state['collected_at'] == saved['collected_at'] and not state['reused']
    assert state['started_at'] is state['finished_at'] is state['elapsed_seconds'] is None
    assert vi.view(project.uid)['collection'] == state


def test_collection_reports_previous_snapshot_during_network_then_committed_result(project, monkeypatch):
    old = snapshot(3)
    seed(project, old)
    observed = []

    def fetch(*args, **kwargs):
        state = vi.comment_progress(project.uid)
        observed.append(state)
        assert state['status'] == 'running' and state['busy']
        assert state['count'] == 3 and state['limit'] == 7
        assert state['collected_at'] == old['collected_at']
        assert state['started_at'] and state['finished_at'] is None
        assert isinstance(state['elapsed_seconds'], (int, float))
        assert not {'percent', 'progress', 'current'} & state.keys()
        return source_rows(2)

    monkeypatch.setattr(vi, '_fetch_source', fetch)
    vi.update(project.uid, 'comments', comment_limit=7)
    state = vi.comment_progress(project.uid)
    assert observed and state['status'] == 'completed' and not state['busy']
    assert state['count'] == 2 and state['limit'] == 7 and not state['reused']
    assert state['started_at'] and state['finished_at']
    assert state['elapsed_seconds'] >= 0
    assert state['error'] == state['code'] == ''


@pytest.mark.parametrize('error,code', [
    (vi.VideoInfoError('private-token=secret', code='VIDEO_INFO_FETCH_FAILED'), 'VIDEO_INFO_FETCH_FAILED'),
    (RuntimeError('private-token=secret'), 'VIDEO_INFO_COMMENTS_FAILED'),
])
def test_failed_collection_keeps_snapshot_and_records_only_safe_error(project, monkeypatch, error, code):
    seed(project, snapshot(3))
    original = path(project).read_bytes()

    def fetch(*args, **kwargs):
        raise error

    monkeypatch.setattr(vi, '_fetch_source', fetch)
    with pytest.raises(type(error)):
        vi.ensure_comments(project.uid, comment_limit=4, refresh=True)
    state = vi.comment_progress(project.uid)
    assert state['status'] == 'failed' and not state['busy']
    assert state['count'] == 3 and state['limit'] == 4
    assert state['code'] == code and state['error']
    assert 'secret' not in json.dumps(state) and 'private-token' not in json.dumps(state)
    assert state['started_at'] and state['finished_at'] and state['elapsed_seconds'] >= 0
    assert path(project).read_bytes() == original


def test_reuse_records_completed_check_but_does_not_truncate_collection_or_fetch(project, monkeypatch):
    seed(project, snapshot(5, 50))
    original = path(project).read_bytes()
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: pytest.fail('复用快照不得联网'))
    result = vi.ensure_comments(project.uid, comment_limit=2)
    state = vi.comment_progress(project.uid)
    assert result['count'] == 2 and state['count'] == 5
    assert state['status'] == 'completed' and state['reused'] and not state['busy']
    assert state['limit'] == 2 and state['started_at'] and state['finished_at']
    assert path(project).read_bytes() == original


def test_concurrent_snapshot_reuse_does_not_replace_active_network_lifecycle(project, monkeypatch):
    seed(project, snapshot(5))

    def fetch(*args, **kwargs):
        assert vi.ensure_comments(project.uid, comment_limit=2)['reused']
        state = vi.comment_progress(project.uid)
        assert state['busy'] and state['status'] == 'running'
        assert state['limit'] == 7 and not state['reused'] and state['finished_at'] is None
        return source_rows(3)

    monkeypatch.setattr(vi, '_fetch_source', fetch)
    vi.update(project.uid, 'comments', comment_limit=7)
    state = vi.comment_progress(project.uid)
    assert state['status'] == 'completed' and state['count'] == 3 and not state['reused']


def test_ai_operation_is_not_misreported_as_comment_collection(project, monkeypatch):
    seed(project, snapshot(3))
    monkeypatch.setitem(vi._active, project.uid, 'comment_filter')
    state = vi.comment_progress(project.uid)
    assert state['status'] == 'completed' and not state['busy']


def test_progress_reads_actual_items_and_discards_other_source_lifecycle(project, monkeypatch):
    seed(project, snapshot(3))
    monkeypatch.setattr(vi, '_fetch_source', lambda *args, **kwargs: source_rows(2))
    vi.update(project.uid, 'comments', comment_limit=7)
    project.source_url = OTHER_SOURCE
    crud.save_project(project)
    state = vi.comment_progress(project.uid)
    assert state['status'] == 'idle' and not state['reused'] and state['started_at'] is None
    assert state['count'] == 2
