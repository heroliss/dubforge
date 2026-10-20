"""自动命名穿过导入、下载、改名和清单恢复的行为。"""
import json
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from dubforge import db, project_import
from dubforge.domain import crud
from dubforge.pipeline.step_download import _ensure_video_info


def test_batch_name_intent_and_receipt_follow_current_project(isolated_env):
    body = {'text': json.dumps({'schema': project_import.SCHEMA, 'projects': [
        {'source': 'https://youtu.be/BaW_jenozKc'},
        {'source': 'https://example.com/video/2', 'title': '  自选名称  '},
    ]})}
    plan = project_import.preview(body)
    assert [(r['title_mode'], r['requested_title']) for r in plan['items']] == [
        ('auto', ''), ('manual', '自选名称')]
    assert plan['items'][0]['title'] == 'YouTube 视频 · BaW_jenozKc'
    request = dict(input=body, selected=plan['selected'], fingerprint=plan['fingerprint'],
                   request_id=str(uuid.uuid4()), confirmed=True)
    result = project_import.execute(request)
    one, two = [crud.get_project(r['uid']) for r in result['items']]
    assert (one.title_mode, two.title_mode) == ('auto', 'manual')
    crud.update_project_title(one.uid, '后来的中文标题', title_mode='auto')
    recovered = project_import.status(result['request_id'])
    assert recovered['items'][0]['title'] == '后来的中文标题'
    assert recovered['items'][0]['requested_title'] == ''
    assert recovered['items'][1]['requested_title'] == '自选名称'
    assert project_import.execute(request) == recovered
    assert len(crud.list_projects()) == 2


def test_download_cache_repairs_only_auto_name(isolated_env, monkeypatch):
    from dubforge.pipeline import step_download
    for mode in ('auto', 'manual'):
        project = crud.create_project('原项目名', 'https://youtu.be/BaW_jenozKc', 'link', title_mode=mode)
        root = crud.project_dir(project.uid)
        info = dict(title_original='Original title', title_zh='中文标题', summary_status='generated',
                    source={'url': project.source_url})
        path = root / 'artifacts/download/video_info.json'
        path.write_text(json.dumps(info), encoding='utf-8')
        ctx = SimpleNamespace(project=project, artifact_dir=lambda: path.parent,
                              write_json=lambda *a: None, log=lambda *a: None)
        monkeypatch.setattr(step_download, '_build_video_info', lambda *a: (_ for _ in ()).throw(
            AssertionError('复用缓存不能调用模型或重建资料')))
        steps = [s.model_dump() for s in crud.get_steps(project.id)]
        _ensure_video_info(ctx, 'link', 'Original title', project.source_url, refresh=False)
        assert crud.get_project(project.uid).title == ('中文标题' if mode == 'auto' else '原项目名')
        assert crud.project_dir(project.uid) == root
        assert [s.model_dump() for s in crud.get_steps(project.id)] == steps


def test_api_restore_auto_then_manual_same_name_stays_manual(isolated_env):
    from app.main import app
    from dubforge import project_titles
    project = crud.create_project('自己的名称', 'https://youtu.be/BaW_jenozKc', 'link')
    root = crud.project_dir(project.uid)
    info = dict(title_original='Original title', title_zh='已缓存中文标题', summary_status='generated',
                source={'url': project.source_url})
    path = root / 'artifacts/download/video_info.json'
    path.write_text(json.dumps(info), encoding='utf-8')
    client = TestClient(app)
    response = client.patch(f'/api/projects/{project.uid}', json={'title_mode': 'auto'})
    assert response.status_code == 200, response.text
    assert (response.json()['title'], response.json()['title_mode']) == ('已缓存中文标题', 'auto')
    stale_project = crud.get_project(project.uid)
    response = client.patch(f'/api/projects/{project.uid}', json={'title': '已缓存中文标题'})
    assert response.status_code == 200
    assert response.json()['title_mode'] == 'manual'
    assert not project_titles.sync(stale_project, {**info, 'title_zh': '新中文标题'})
    assert crud.get_project(project.uid).title == '已缓存中文标题'
    assert crud.project_dir(project.uid) == root
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['title_mode'] == 'manual'
    assert client.patch(f'/api/projects/{project.uid}', json={'title': '  '}).json()['code'] == 'TITLE_EMPTY'


def test_auto_title_mode_survives_manifest_rebuild(isolated_env):
    from sqlmodel import delete
    from dubforge.domain.models import Artifact, Job, Project, StepRun
    project = crud.create_project('自动中文标题', 'https://example.com/v', title_mode='auto')
    root = crud.project_dir(project.uid)
    with crud.session() as session:
        for model in (Artifact, Job, StepRun, Project):
            session.exec(delete(model))
        session.commit()
    assert crud.rebuild_from_dirs()['projects'] == 1
    restored = crud.get_project(project.uid)
    assert (restored.title, restored.title_mode) == ('自动中文标题', 'auto')
    assert crud.project_dir(project.uid) == root


def test_old_database_name_is_protected_by_column_migration(isolated_env):
    project = crud.create_project('旧版自定义名称')
    with db.get_engine().connect() as conn:
        conn.exec_driver_sql('ALTER TABLE project DROP COLUMN title_mode')
        conn.commit()
    db._ensure_columns()
    assert crud.get_project(project.uid).title_mode == 'manual'
    assert crud.get_project(project.uid).title == '旧版自定义名称'


def test_same_name_manual_commit_wins_over_interleaved_auto_sync(isolated_env):
    from sqlalchemy import event
    from dubforge import project_titles
    project = crud.create_project('用户选定的名称', 'https://youtu.be/BaW_jenozKc', title_mode='auto')
    injected = False

    def interleave(conn, cursor, statement, parameters, context, executemany):
        nonlocal injected
        if injected or not statement.lstrip().upper().startswith('UPDATE PROJECT SET'):
            return
        injected = True
        # 模拟手工保存已经准备写库时，平台资料先一步同步了新标题。
        assert project_titles.sync(project, dict(title_original='迟到的平台标题'))

    engine = db.get_engine()
    event.listen(engine, 'before_cursor_execute', interleave)
    try:
        crud.update_project_title(project.uid, '用户选定的名称')
    finally:
        event.remove(engine, 'before_cursor_execute', interleave)
    assert injected
    current = crud.get_project(project.uid)
    assert (current.title, current.title_mode) == ('用户选定的名称', 'manual')
