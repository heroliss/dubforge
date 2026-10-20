"""自动评论准备复用可核验采集快照；隔离项目、伪采集，无网络和模型调用。"""
import copy
import json

import pytest

from dubforge import video_info as vi
from dubforge.domain import crud
from dubforge.utils.fs import atomic_write_bytes


SOURCE = 'https://www.youtube.com/watch?v=snapshot-one'
OTHER_SOURCE = 'https://www.youtube.com/watch?v=snapshot-two'


@pytest.fixture
def project(isolated_env, monkeypatch):
    monkeypatch.setattr(vi, 'chat_json', lambda *a, **k: pytest.fail('采集和复用评论不调用 LLM'))
    return crud.create_project(title='评论快照测试', source_kind='youtube', source_url=SOURCE)


def source_rows(count=3):
    return dict(id='snapshot-one', title='旅行视频', webpage_url=SOURCE, extractor='youtube',
                comments=[dict(id=f'c{i}', text=f'Comment {i}', author='Viewer') for i in range(count)])


def path(project):
    return crud.project_dir(project.uid) / 'artifacts/download/video_info.json'


def seed(project, comments, *, info_source=SOURCE):
    info = vi._build_video_info('youtube', '旅行视频', info_source, None,
                               dict(webpage_url=info_source, extractor='youtube'), summarize=False)
    info['comments'] = comments
    atomic_write_bytes(path(project), json.dumps(info, ensure_ascii=False).encode())
    return info


def snapshot(count=3, limit=50):
    return vi._comments(source_rows(count), limit, source_url=SOURCE)


def fetch_stub(monkeypatch, raw=None):
    calls = []
    def fetch(url, settings, *, comment_limit=0):
        calls.append((url, comment_limit))
        return raw if raw is not None else source_rows()
    monkeypatch.setattr(vi, '_fetch_source', fetch)
    return calls


def test_first_collection_records_origin_and_scope(project, monkeypatch):
    calls = fetch_stub(monkeypatch)
    result = vi.ensure_comments(project.uid, comment_limit=2)
    assert result['ids'] == ['c0', 'c1'] and not result['reused']
    assert result['count'] == result['available'] == result['requested_limit'] == 2
    saved = vi.view(project.uid)['info']['comments']
    assert saved['source_url'] == SOURCE and saved['snapshot_version'] == 1
    assert saved['collected_at'] == result['collected_at']
    assert calls == [(SOURCE, 2)]


@pytest.mark.parametrize('count', [0, 3])
def test_retry_reuses_completed_collection_including_empty_sample(project, monkeypatch, count):
    seed(project, snapshot(count))
    before = path(project).read_bytes()
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: pytest.fail('继续评论处理不能重抓有效采样'))
    result = vi.ensure_comments(project.uid, comment_limit=50)
    assert result['reused'] and result['ids'] == [f'c{i}' for i in range(count)]
    assert result['count'] == result['available'] == count
    assert path(project).read_bytes() == before


def test_lower_limit_scopes_ids_without_deleting_collection_or_caches(project, monkeypatch):
    seed(project, snapshot(5, 50))
    workspace = crud.project_dir(project.uid) / 'comment_studio.json'
    atomic_write_bytes(workspace, b'{"translations":{"c4":{"text_zh":"kept"}}}')
    before, workspace_before = path(project).read_bytes(), workspace.read_bytes()
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: pytest.fail('缩小处理范围无需重抓'))
    result = vi.ensure_comments(project.uid, comment_limit=2)
    assert result['ids'] == ['c0', 'c1'] and result['count'] == 2 and result['available'] == 5
    assert result['requested_limit'] == 50 and result['reused']
    assert path(project).read_bytes() == before and workspace.read_bytes() == workspace_before


def test_limit_expansion_recollects_once_then_reuses_short_platform_sample(project, monkeypatch):
    seed(project, snapshot(2, 5))
    calls = fetch_stub(monkeypatch)
    first = vi.ensure_comments(project.uid, comment_limit=6)
    second = vi.ensure_comments(project.uid, comment_limit=6)
    assert not first['reused'] and second['reused']
    assert first['ids'] == second['ids'] == ['c0', 'c1', 'c2']
    assert calls == [(SOURCE, 6)]


def test_source_change_does_not_reuse_explicit_old_origin(project, monkeypatch):
    seed(project, snapshot(), info_source=OTHER_SOURCE)
    project.source_url = OTHER_SOURCE
    crud.save_project(project)
    calls = fetch_stub(monkeypatch)
    result = vi.ensure_comments(project.uid, comment_limit=50)
    assert not result['reused'] and calls == [(OTHER_SOURCE, 50)]
    assert vi.view(project.uid)['info']['comments']['source_url'] == OTHER_SOURCE


def test_legacy_source_can_be_verified_without_rewriting_snapshot(project, monkeypatch):
    comments = snapshot()
    comments.pop('source_url')
    comments.pop('snapshot_version')
    seed(project, comments)
    before = path(project).read_bytes()
    monkeypatch.setattr(vi, '_fetch_source', lambda *a, **k: pytest.fail('来源可核验的历史采集应复用'))
    assert vi.ensure_comments(project.uid, comment_limit=50)['reused']
    assert path(project).read_bytes() == before


def test_metadata_refresh_binds_legacy_comments_to_old_source(project, monkeypatch):
    comments = snapshot()
    comments.pop('source_url')
    comments.pop('snapshot_version')
    seed(project, comments)
    project.source_url = OTHER_SOURCE
    crud.save_project(project)
    raw = source_rows()
    raw['webpage_url'] = OTHER_SOURCE
    calls = fetch_stub(monkeypatch, raw)
    refreshed = vi.update(project.uid, 'refresh')
    assert refreshed['source']['url'] == OTHER_SOURCE
    assert refreshed['comments']['source_url'] == SOURCE
    assert not vi.ensure_comments(project.uid, comment_limit=50)['reused']
    assert calls == [(OTHER_SOURCE, 0), (OTHER_SOURCE, 50)]


@pytest.mark.parametrize('action', ['comments', 'refresh'])
def test_source_change_rebuilds_video_context_without_old_summary_or_statistics(project, monkeypatch, action):
    info = seed(project, snapshot())
    info.update(title_zh='旧视频中文标题', summary_zh='旧视频的概要', description='旧视频简介',
                summary_status='generated', summary_generated_at='2026-10-01T00:00:00+00:00')
    info['source']['comment_count'] = 9000
    atomic_write_bytes(path(project), json.dumps(info, ensure_ascii=False).encode())
    project.source_url = OTHER_SOURCE
    crud.save_project(project)
    raw = source_rows()
    raw.update(id='snapshot-two', title='New video', description='New source description',
               webpage_url=OTHER_SOURCE, comment_count=17)
    fetch_stub(monkeypatch, raw)
    result = vi.update(project.uid, action, comment_limit=50)
    assert result['title_original'] == result['title_zh'] == 'New video'
    assert result['description'] == 'New source description'
    assert result['summary_zh'] == '' and result['summary_status'] == 'not_generated'
    assert 'summary_generated_at' not in result
    assert result['source']['url'] == OTHER_SOURCE
    assert result['source']['comment_count'] == (None if action == 'comments' else 17)
    from dubforge import comment_studio
    assert comment_studio._context(project) == dict(title='New video', summary='', description='New source description')


def test_collecting_through_same_video_short_url_retains_verified_metadata(project, monkeypatch):
    info = seed(project, snapshot())
    info.update(title_zh='已核对中文标题', summary_zh='有效概要', summary_status='generated')
    info['source']['comment_count'] = 9000
    atomic_write_bytes(path(project), json.dumps(info, ensure_ascii=False).encode())
    project.source_url = 'https://youtu.be/snapshot-one'
    crud.save_project(project)
    fetch_stub(monkeypatch, source_rows())
    result = vi.update(project.uid, 'comments', comment_limit=50)
    assert result['title_zh'] == '已核对中文标题' and result['summary_zh'] == '有效概要'
    assert result['source']['comment_count'] == 9000
    assert result['comments']['source_url'] == project.source_url


@pytest.mark.parametrize('broken', [
    {'collected_at': ''}, {'collected_at': 'not-a-date'}, {'collected_at': '2026-10-07T11:00:00'},
    {'sort': 'new'}, {'requested_limit': True}, {'requested_limit': 501}, {'count': 2},
    {'items': None}, {'items': [dict(id='c0', text='one'), dict(id='c0', text='duplicate')], 'count': 2},
    {'items': [dict(id='c0', text=' ')], 'count': 1}, {'source_url': ''},
    {'source_url': OTHER_SOURCE}, {'snapshot_version': 2}, {'snapshot_version': True},
])
def test_unverifiable_snapshot_is_refetched(project, monkeypatch, broken):
    comments = {**snapshot(), **copy.deepcopy(broken)}
    seed(project, comments)
    calls = fetch_stub(monkeypatch)
    assert not vi.ensure_comments(project.uid, comment_limit=50)['reused']
    assert calls == [(SOURCE, 50)]


def test_new_snapshot_missing_origin_cannot_fall_back_to_metadata(project, monkeypatch):
    comments = snapshot()
    comments.pop('source_url')
    seed(project, comments)
    calls = fetch_stub(monkeypatch)
    assert not vi.ensure_comments(project.uid, comment_limit=50)['reused']
    assert calls == [(SOURCE, 50)]


def test_explicit_recollection_always_fetches_and_retains_snapshot_on_error(project, monkeypatch):
    seed(project, snapshot())
    calls = fetch_stub(monkeypatch)
    assert not vi.ensure_comments(project.uid, comment_limit=50, refresh=True)['reused']
    # 页面“重新获取评论”仍直接调用 update，而非采用自动恢复的缓存策略。
    vi.update(project.uid, 'comments', comment_limit=50)
    assert calls == [(SOURCE, 50), (SOURCE, 50)]
    before = path(project).read_bytes()
    def fail(*a, **k):
        raise vi.VideoInfoError('模拟采集失败')
    monkeypatch.setattr(vi, '_fetch_source', fail)
    with pytest.raises(vi.VideoInfoError):
        vi.ensure_comments(project.uid, comment_limit=50, refresh=True)
    assert path(project).read_bytes() == before


@pytest.mark.parametrize('limit', [0, 501, True, '50', None])
def test_limit_validation_is_identical_when_cache_exists(project, limit):
    seed(project, snapshot())
    with pytest.raises(vi.VideoInfoError) as error:
        vi.ensure_comments(project.uid, comment_limit=limit)
    assert error.value.code == 'VIDEO_INFO_COMMENT_LIMIT'
