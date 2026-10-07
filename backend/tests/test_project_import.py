"""批量导入的解析、继承、只读预览、原子登记和断线恢复。"""
import json
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from dubforge import configuration as cfg, project_import as imp
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.domain.models import Project, StepRun, Job
from dubforge.project_import_models import ProjectImport
from test_configuration import save


@pytest.fixture(autouse=True)
def sandbox(isolated_env, monkeypatch):
    from dubforge.backends import llm
    from dubforge.publishing import transport
    from dubforge.runner import runner
    import yt_dlp
    def forbidden(*a, **k):
        pytest.fail('导入不得下载视频、调用模型、查询B站或启动步骤')
    for target, name in [(llm, 'chat_json'), (transport, 'call'), (runner, 'start_step'), (yt_dlp.YoutubeDL, 'extract_info')]:
        monkeypatch.setattr(target, name, forbidden)


def document(projects, defaults=None):
    return {'text': json.dumps({'schema': imp.SCHEMA, 'defaults': defaults or {}, 'projects': projects}, ensure_ascii=False)}


def command(body, selected=None):
    plan = imp.preview(body)
    return dict(input=body, selected=plan['selected'] if selected is None else selected,
                fingerprint=plan['fingerprint'], request_id=str(uuid.uuid4()), confirmed=True)


def test_lines_preview_is_readonly_and_creation_never_runs(tmp_path):
    local = tmp_path / '本地 视频.mp4'; local.write_bytes(b'video')
    body = {'text': f'\ufeffhttps://youtu.be/BaW_jenozKc\n\n# 备注\n"{local}"'}
    plan = imp.preview(body)
    assert plan['selected'] == [0, 1]
    assert crud.list_projects() == []
    assert plan['items'][1]['source_kind'] == 'local'
    result = imp.execute(command(body))
    assert result['created'] == 2 and result['complete']
    for item in result['items']:
        project = crud.get_project(item['uid'])
        assert all(s.status == 'pending' for s in crud.get_steps(project.id))
        assert (crud.project_dir(project.uid) / 'manifest.json').is_file()
    assert local.read_bytes() == b'video'
    with Session(get_engine()) as session:
        assert session.exec(select(Job)).all() == []


def test_template_and_sparse_project_settings_keep_live_inheritance():
    first, second = cfg.create_template('旅行'), cfg.create_template('美食')
    save('global', {'assemble.subtitle_color': '#FFFFFF'})
    save(first['key'], {'comments.count': 8, 'publish.season_id': 12})
    save(second['key'], {'comments.count': 7})
    body = document([
        {'source': 'https://example.com/1', 'title': '第一集', 'settings': {'comments.count': 6}},
        {'source': 'https://example.com/2', 'template': '美食'},
        {'source': 'https://example.com/3', 'template': None},
    ], {'template': first['key'], 'settings': {'comments.show_author': False, 'comments.background_opacity': 0}})
    result = imp.execute(command(body))
    one, two, three = [cfg.resolve(i['uid']) for i in result['items']]
    assert [r['parent'] for r in [one,two,three]] == [first['key'], second['key'], '']
    assert one['overrides'] == {'comments.count': 6, 'comments.show_author': False, 'comments.background_opacity': 0}
    assert two['values']['comments.count'] == 7
    assert one['sources']['publish.season_id']['kind'] == 'series'
    save(first['key'], {'publish.season_id': 13, 'comments.count': 9})
    after = cfg.resolve(result['items'][0]['uid'])
    assert after['values']['publish.season_id'] == 13 and after['values']['comments.count'] == 6


def test_row_selection_overrides_json_and_ui_default_only_fills_missing():
    a, b = cfg.create_template('A'), cfg.create_template('B')
    body = document([{'source':'https://example.com/1'}, {'source':'https://example.com/2', 'template':None}])
    body.update(default_template=a['key'], template_overrides={'0': b['key']})
    assert [r['template'] for r in imp.preview(body)['items']] == [b['key'], '']
    body['template_overrides'] = {'0': None}
    assert imp.preview(body)['items'][0]['template'] == ''


def test_duplicates_across_youtube_forms_and_existing_are_skipped_by_default():
    previous = crud.create_project('已有', 'https://www.youtube.com/watch?v=BaW_jenozKc', 'link')
    body = {'text': 'https://youtu.be/BaW_jenozKc?t=12\nhttps://www.youtube.com/shorts/BaW_jenozKc\nhttps://example.com/other'}
    plan = imp.preview(body)
    assert plan['selected'] == [2]
    assert plan['items'][0]['duplicates'][0]['uid'] == previous.uid
    assert '第1项' in plan['items'][1]['warnings'][-1]
    assert plan['items'][0]['selectable']
    result = imp.execute(command(body, selected=[0, 2]))
    assert result['created'] == 2 and len(crud.list_projects()) == 3


@pytest.mark.parametrize('bad', ['', '[]', '{}', '{"schema":"wrong","projects":[]}',
    '{"schema":"dubforge.batch.v1","schema":"dubforge.batch.v1","projects":[]}',
    '{"schema":"dubforge.batch.v1","projects":[{"source":"https://x.com/1","settings":{"comments.count":NaN}}]}',
    'https://example.com/v\n' * 101, 'x' * (imp.MAX_BYTES + 1)],
    ids=['empty','array','no-schema','wrong-schema','duplicate-key','nan','too-many','too-large'])
def test_invalid_document_writes_nothing(bad):
    with pytest.raises(imp.ImportError):
        imp.preview({'text':bad})
    assert crud.list_projects() == []


@pytest.mark.parametrize('row', [
    {'source':'https://example.com/1', 'settings':{'llm.api_key':'not-a-secret'}},
    {'source':'https://example.com/1', 'settings':{'download.proxy':'https://proxy.invalid'}},
    {'source':'https://example.com/1', 'settings':{'comments.count':501}},
    {'source':'https://example.com/1', 'settings':{'comments.show_author':'false'}},
    {'source':'https://example.com/1', 'settings':{'publish.version_ids':['missing']}},
    {'source':'https://example.com/1', 'template':'不存在'},
    {'source':'https://user:pass@example.com/1'}, {'source':'https://[broken'},
    {'source':'https://www.youtube.com/playlist?list=PL123'},
    {'source':'relative.mp4'}, {'source':'C:/not-existing-import-fixture.mp4'},
    {'source':'https://example.com/1', 'auto_run':True}, {'source':None}, 'not-object',
])
def test_bad_rows_are_visible_and_good_rows_can_be_selected(row):
    body = document([row, {'source':'https://example.com/good'}])
    plan = imp.preview(body)
    assert plan['selected'] == [1] and plan['items'][0]['errors']
    with pytest.raises(imp.ImportError):
        imp.execute(command(body, selected=[0]))
    assert crud.list_projects() == []
    assert imp.execute(command(body))['created'] == 1


def test_ambiguous_and_archived_templates_not_silently_selected():
    a = cfg.create_template('重复'); cfg.create_template('重复')
    body = document([{'source':'https://example.com/a', 'template':'重复'}])
    assert imp.preview(body)['items'][0]['errors']
    body['template_overrides'] = {'0':a['key']}
    assert imp.preview(body)['selected'] == [0]
    cfg.archive(a['key'], {'revision':a['revision'], 'archived':True})
    assert imp.preview(body)['items'][0]['errors']


def test_template_changes_between_preview_and_create_are_rejected():
    template = cfg.create_template('系列')
    body = command(document([{'source':'https://example.com/a'}], {'template':template['key']}))
    save(template['key'], {'comments.count':9})
    with pytest.raises(imp.ImportError, match='重新预览'):
        imp.execute(body)
    assert crud.list_projects() == []


def test_request_replay_and_parallel_double_click_do_not_duplicate():
    body = command({'text':'https://example.com/a\nhttps://example.com/b'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(imp.execute, [body,body]))
    assert results[0] == results[1] and len(crud.list_projects()) == 2
    assert imp.execute(body) == results[0]
    changed = {**body, 'selected':[0]}
    with pytest.raises(imp.ImportError, match='内容已改变'):
        imp.execute(changed)


def test_partial_failure_resume_only_failed_items_and_settings_not_half_written(monkeypatch):
    body = command({'text':'https://example.com/a\nhttps://example.com/b'})
    original = crud.create_project
    def fail_second(*a, **k):
        if k['source_url'].endswith('/b'):
            initialize = k['initialize']
            def fail_after_initialize(session, project):
                initialize(session, project)
                raise OSError('模拟事务失败')
            k['initialize'] = fail_after_initialize
        return original(*a, **k)
    monkeypatch.setattr(crud, 'create_project', fail_second)
    first = imp.execute(body)
    assert [r['status'] for r in first['items']] == ['created','error']
    assert len(crud.list_projects()) == 1
    with Session(get_engine()) as session:
        assert len(session.exec(select(StepRun)).all()) == 9
    monkeypatch.setattr(crud, 'create_project', original)
    second = imp.execute(body)
    assert second['created'] == 2 and second['items'][0]['uid'] == first['items'][0]['uid']


def test_manifest_failure_keeps_created_project_and_receipt(monkeypatch):
    body = command({'text':'https://example.com/a'})
    monkeypatch.setattr(crud, 'write_manifest', lambda uid: (_ for _ in ()).throw(OSError('磁盘满')))
    result = imp.execute(body)
    assert result['created'] == 1 and result['items'][0]['error']
    assert crud.project_dir(result['items'][0]['uid']).is_dir()
    assert imp.execute(body)['created'] == 1 and len(crud.list_projects()) == 1


def test_deleted_created_project_is_not_resurrected_on_replay():
    body = command({'text':'https://example.com/a'})
    result = imp.execute(body)
    crud.delete_project(result['items'][0]['uid'])
    assert imp.execute(body)['items'][0]['status'] == 'removed'
    assert crud.list_projects() == []


def test_code_fence_and_catalog_without_sensitive_values(monkeypatch):
    from dubforge import config
    original = config.load_config
    def settings():
        value = original(); value.setdefault('download', {})['proxy'] = 'https://secret-proxy.invalid'
        return value
    monkeypatch.setattr(config, 'load_config', settings)
    guide = imp.catalog()
    assert 'secret-proxy' not in json.dumps(guide)
    assert 'download.proxy' not in [f['path'] for f in guide['fields']]
    assert imp.preview({'text':'```json\n'+json.dumps(guide['sample'])+'\n```'})['selected'] == [0,1]
    detailed = imp.preview({'text':json.dumps(guide['examples']['two_parts'])})
    assert detailed['selected'] == [0,1]
    assert detailed['items'][0]['settings']['publish.version_ids'] == ['dub','src']
    assert all('value' not in f for f in guide['fields'])


@pytest.mark.parametrize('text', ['\ud800', '{"schema":"dubforge.batch.v1","projects":[{"source":"\\ud800"}]}',
    '{"schema":"dubforge.batch.v1","projects":[{"source":"https://x.com/v","settings":{"comments.count":1e999}}]}'],
    ids=['invalid-utf8','escaped-surrogate','infinite-exponent'])
def test_invalid_unicode_and_non_finite_numbers_report_readable_error(text):
    with pytest.raises(imp.ImportError):
        imp.preview({'text':text})


def test_full_batch_keeps_all_project_directories_unique():
    body = document([{'source':f'https://example.com/video/{i}', 'title':'同名项目'} for i in range(100)])
    result = imp.execute(command(body))
    assert result['created'] == 100
    assert len({p.dir_name for p in crud.list_projects()}) == 100
    assert all(crud.project_dir(r['uid']).is_dir() for r in result['items'])


def test_receipt_contains_sparse_settings_for_failed_row_after_reload(monkeypatch):
    body = command(document([{'source':'https://example.com/a', 'settings':{'comments.count':5}}]))
    monkeypatch.setattr(crud, 'create_project', lambda **k: (_ for _ in ()).throw(OSError('磁盘满')))
    imp.execute(body)
    row = imp.status(body['request_id'])['items'][0]
    assert row['status'] == 'error' and row['settings'] == {'comments.count':5} and row['template'] == ''


def test_create_validates_even_malformed_replay_request():
    body = command({'text':'https://example.com/a'})
    imp.execute(body)
    for patch in ({'input':[]}, {'input':{'text':'\ud800'}}, {'fingerprint':float('nan')}):
        with pytest.raises(imp.ImportError):
            imp.execute({**body, **patch})
    assert len(crud.list_projects()) == 1


@pytest.mark.parametrize('name', ['batch-projects.json', 'batch-projects-two-parts.json'])
def test_shipped_examples_stay_valid(name):
    text = (Path(__file__).resolve().parents[2] / 'docs' / 'examples' / name).read_text(encoding='utf-8')
    plan = imp.preview({'text':text})
    assert plan['selected'] == [0,1], plan['items']


def test_api_preview_create_receipt_and_cross_origin_protection():
    from app.main import app
    client = TestClient(app)
    body = {'text':'https://example.com/a'}
    assert client.get('/api/project-import/catalog').status_code == 200
    assert client.post('/api/project-import/preview', json=body).json()['selected'] == [0]
    command_body = command(body)
    assert client.post('/api/project-import/create', json=command_body, headers={'Origin':'https://hostile.invalid'}).status_code == 400
    assert crud.list_projects() == []
    response = client.post('/api/project-import/create', json=command_body)
    assert response.status_code == 200, response.text
    assert client.get('/api/project-import/'+command_body['request_id']).json() == response.json()


def test_restart_history_and_resume_without_browser_text(monkeypatch):
    from dubforge import db
    body = command({'text':'https://example.com/a\nhttps://example.com/b'})
    original = crud.create_project
    def interrupt(**kwargs):
        if kwargs['source_url'].endswith('/b'):
            raise SystemExit('模拟重启')
        return original(**kwargs)
    monkeypatch.setattr(crud, 'create_project', interrupt)
    with pytest.raises(SystemExit):
        imp.execute(body)
    db.reset_engine();db.init_db()
    recent = imp.history()['items'][0]
    assert recent['request_id'] == body['request_id'] and recent['created'] == 1 and recent['pending'] == 1
    assert recent['created_at'] and not recent['running']
    assert 'settings' not in recent and 'plan' not in recent
    with pytest.raises(imp.ImportError):
        imp.resume(recent['request_id'], {'confirmed':False})
    assert len(crud.list_projects()) == 1
    monkeypatch.setattr(crud, 'create_project', original)
    first = imp.status(recent['request_id'])['items'][0]['uid']
    done = imp.resume(recent['request_id'], {'confirmed':True})
    assert done['created'] == 2 and done['items'][0]['uid'] == first
    assert imp.resume(recent['request_id'], {'confirmed':True}) == done
    assert len(crud.list_projects()) == 2


def test_history_and_status_query_projects_in_one_batch():
    from sqlalchemy import event
    result = imp.execute(command({'text':'\n'.join(f'https://example.com/{i}' for i in range(5))}))
    queries = []
    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            queries.append(statement)
    engine = get_engine();event.listen(engine, 'before_cursor_execute', record)
    try:
        assert imp.status(result['request_id'])['created'] == 5
        assert len(queries) == 2
        queries.clear()
        assert imp.history()['items'][0]['created'] == 5
        assert len(queries) == 2
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def test_history_limit_and_http_resume_are_validated():
    from app.main import app
    body = command({'text':'https://example.com/a'})
    imp.execute(body)
    client = TestClient(app)
    assert client.get('/api/project-import/history?limit=0').status_code == 400
    assert client.get('/api/project-import/history?limit=1').json()['items'][0]['request_id'] == body['request_id']
    assert client.post('/api/project-import/'+body['request_id']+'/resume', json={'confirmed':True}).json()['created'] == 1
    assert client.post('/api/project-import/'+body['request_id']+'/resume', json={'confirmed':True,'settings':{}}).status_code == 400
    assert client.post('/api/project-import/missing/resume', json={'confirmed':True}).status_code == 400


def test_created_time_column_upgrade_preserves_receipts():
    from dubforge import db
    result = imp.execute(command({'text':'https://example.com/a'}))
    with get_engine().connect() as connection:
        connection.exec_driver_sql('ALTER TABLE projectimport DROP COLUMN created_at');connection.commit()
    db.init_db()
    assert imp.history()['items'][0]['created'] == 1
    assert imp.status(result['request_id'])['created_at'] == ''


def test_different_non_default_url_ports_are_not_duplicates():
    plan = imp.preview({'text':'https://example.com:80/video\nhttps://example.com/video\nhttp://example.com:443/video'})
    assert plan['selected'] == [0,1,2]
    assert imp.source('http://[::1]:8600/video')['identity'] == 'http://[::1]:8600/video'
