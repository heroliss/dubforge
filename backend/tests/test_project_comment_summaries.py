"""项目卡片复用轻量评论真值，不为每张卡片重跑工作区预检。"""
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_diagnostics as cd, comment_studio as cs, configuration, video_info
from dubforge.domain import crud
from dubforge.project_archive import set_archived
from dubforge.utils.fs import atomic_write_bytes
from test_comment_studio import seed, plan_body


def test_domain_batch_uses_loaded_projects_and_no_full_workspace_or_source_read(make_project, monkeypatch):
    uids = [make_project(title=f'轻量评论项目 {index}') for index in range(3)]
    for uid in uids:
        seed(uid)
    cs.save(uids[0], plan_body(uids[0]))
    state = cs.view(uids[1])
    cs.save(uids[1], {'revision': state['overlay_revision'], 'settings': state['defaults'], 'items': []})
    projects = [crud.get_project(uid) for uid in uids]
    def forbidden(*_args, **_kwargs):
        raise AssertionError('列表摘要不得重取项目、完整评论工作区、来源原文或配置')
    monkeypatch.setattr(crud, 'get_project', forbidden)
    monkeypatch.setattr(cs, 'view', forbidden)
    monkeypatch.setattr(cs, 'progress', forbidden)
    monkeypatch.setattr(cs, '_read', forbidden)
    monkeypatch.setattr(cs, '_rows', forbidden)
    monkeypatch.setattr(configuration, 'resolve', forbidden)
    monkeypatch.setattr(video_info, 'comment_progress', forbidden)
    monkeypatch.setattr(video_info, '_read_json', forbidden)
    result = cs.workspace_summaries(projects)
    assert result[uids[0]]['state'] == 'done' and result[uids[0]]['confirmed_count'] == 3
    assert result[uids[1]]['state'] == 'done' and result[uids[1]]['confirmed_count'] == 0
    assert '空上屏方案' in result[uids[1]]['detail']
    assert result[uids[2]]['state'] == 'pending'
    assert cs.workspace_summaries([]) == {}


def test_list_api_calls_comment_batch_once_and_preserves_publication_and_steps(make_project, monkeypatch):
    uids = [make_project(title=f'项目卡片 {index}') for index in range(3)]
    seed(uids[0])
    cs.save(uids[0], plan_body(uids[0]))
    calls = []
    original = cs.workspace_summaries
    def tracked(projects):
        projects = list(projects)
        calls.append([project.uid for project in projects])
        return original(projects)
    monkeypatch.setattr(cs, 'workspace_summaries', tracked)
    response = TestClient(app).get('/api/projects')
    assert response.status_code == 200
    rows = {row['uid']: row for row in response.json()}
    assert len(calls) == 1 and set(calls[0]) == set(uids)
    for uid in uids:
        row = rows[uid]
        assert set(row['comments_workspace']) == {'state', 'label', 'detail', 'confirmed_count'}
        assert row['publication']['status'] == 'pending'
        assert 'release_batch' in row and 'workflow_status' in row
        assert 'comments' not in {step['key'] for step in row['steps']}
        assert 'publish' not in {step['key'] for step in row['steps']}
    assert rows[uids[0]]['comments_workspace'] == cs.progress(uids[0])['workspace']
    assert rows[uids[0]]['comments_workspace']['confirmed_count'] == 3


def test_batch_projects_keep_running_and_waiting_confirmation_separate(make_project, monkeypatch):
    uids = [make_project(title=f'评论状态 {index}') for index in range(3)]
    projects = [crud.get_project(uid) for uid in uids]
    monkeypatch.setattr(cs, '_jobs', {uids[0]: {'id': 'active', 'action': 'translate', 'status': 'running'}})
    monkeypatch.setattr(cs, '_diagnostics', {})
    monkeypatch.setattr(cs, '_prepare_jobs', {uids[1]: {'id': 'prepared', 'status': 'done',
        'overlay_signature': cs._hash(None)}})
    monkeypatch.setattr(video_info, '_active', {uids[2]: 'comments'})
    result = cs.workspace_summaries(projects)
    assert result[uids[0]]['state'] == 'running' and result[uids[0]]['label'] == '正在翻译评论'
    assert result[uids[1]]['state'] == 'attention' and result[uids[1]]['label'] == '待确认方案'
    assert result[uids[2]]['state'] == 'running' and result[uids[2]]['label'] == '正在获取评论'


def test_archived_listing_has_saved_comment_summary_without_refetching_sources(make_project):
    uid = make_project(title='已归档评论项目')
    seed(uid)
    cs.save(uid, plan_body(uid))
    set_archived(uid, True)
    client = TestClient(app)
    assert client.get('/api/projects').json() == []
    rows = client.get('/api/projects?archive=archived').json()
    assert len(rows) == 1 and rows[0]['uid'] == uid
    assert rows[0]['comments_workspace']['state'] == 'done'
    assert rows[0]['comments_workspace']['confirmed_count'] == 3


def test_one_bad_comment_document_does_not_fail_other_project_cards(make_project):
    healthy = make_project(title='正常项目')
    broken = make_project(title='评论资料待核对')
    atomic_write_bytes(crud.project_dir(broken) / 'comment_studio.json', b'{broken')
    response = TestClient(app).get('/api/projects')
    assert response.status_code == 200
    rows = {row['uid']: row for row in response.json()}
    assert rows[broken]['comments_workspace']['state'] == 'attention'
    assert rows[healthy]['comments_workspace']['state'] == 'pending'


def test_batch_restores_restart_interruption_without_reviving_running_state(make_project, monkeypatch):
    uid = make_project(title='重启后的评论项目')
    seed(uid)
    cs.save(uid, plan_body(uid))
    project = crud.get_project(uid)
    cd.Diagnostics(project, {'id': 'interrupted', 'action': 'filter', 'status': 'running'})
    monkeypatch.setattr(cs, '_jobs', {})
    monkeypatch.setattr(cs, '_diagnostics', {})
    monkeypatch.setattr(cs, '_prepare_jobs', {})
    monkeypatch.setattr(video_info, '_active', {})
    state = cs.workspace_summaries([project])[uid]
    assert state['state'] != 'running'
    assert state['confirmed_count'] == 3 and '已保存 3 条' in state['detail']


def test_collection_history_matches_source_and_directory_before_affecting_card(make_project, monkeypatch):
    uid = make_project(title='评论采集记录')
    project = crud.get_project(uid)
    root = crud.project_dir_by_project(project)
    record = {'source_url': video_info._url(project.source_url), 'root': str(root),
              'status': 'failed', 'finished_at': '2026-10-17T01:00:00+00:00'}
    monkeypatch.setattr(video_info, '_comment_runs', {uid: record})
    assert cs.workspace_summaries([project])[uid]['state'] == 'failed'
    record['root'] = str(root / '旧目录')
    assert cs.workspace_summaries([project])[uid]['state'] == 'pending'
    record['root'] = str(root)
    record['source_url'] = 'https://www.youtube.com/watch?v=other-source'
    assert cs.workspace_summaries([project])[uid]['state'] == 'pending'
