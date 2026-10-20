"""模板删除先留备份，找回不覆盖现存模板或重新绑定项目。"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from dubforge import config, configuration as cfg, configuration_trash as trash, runtime_edit
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.errors import DubForgeError


def archived_template():
    row = cfg.create_template('旅行系列')
    row = cfg.save(row['key'], dict(revision=row['revision'], updates={
        'assemble.subtitle_color': '#FFE082', 'publish.title': '中国旅行'}))
    return cfg.archive(row['key'], dict(revision=row['revision'], archived=True))


def delete(row):
    return trash.delete(row['key'], dict(revision=row['revision'], confirmed=True))


def backup_file(result):
    return config.data_root() / 'trash' / 'templates' / (result['backup_id'] + '.json')


def bind(uid, template):
    plan = cfg.binding_plan([uid], template['key'][7:])
    cfg.bind(dict(uids=[uid], template_id=template['key'][7:],
        fingerprint=plan['fingerprint'], confirmed=True))


def test_delete_keeps_complete_atomic_backup_and_restore_keeps_archive(isolated_env):
    row = archived_template()
    result = delete(row)
    assert result['deleted'] is True
    assert result['recovery'] == 'application-trash'
    assert cfg.list_templates() == []
    path = backup_file(result)
    assert path.is_file()
    assert path.parent.is_relative_to(isolated_env)
    snapshot = json.loads(path.read_text(encoding='utf-8'))
    assert snapshot['format_version'] == 1
    assert snapshot['key'] == row['key']
    assert snapshot['name'] == row['name']
    assert snapshot['overrides'] == row['overrides']
    assert snapshot['revision'] == row['revision']
    assert snapshot['archived'] is True
    assert list(path.parent.glob('*.tmp')) == []
    listed = trash.list_deleted()
    assert len(listed) == 1 and listed[0]['restorable'] is True
    assert 'overrides' not in listed[0]
    assert listed[0]['backup_file'] == path.name
    assert listed[0]['backup_path'] == str(path)
    restored = trash.restore(result['backup_id'], {'confirmed': True})
    assert restored['key'] == row['key']
    assert restored['revision'] == row['revision'] + 1
    assert restored['archived'] is True
    current = cfg.resolve(key=row['key'])
    assert current['overrides'] == row['overrides']
    assert current['parent'] == ''
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'CONFIGURATION_CONFLICT'
    assert trash.list_deleted() == []
    saved_backup = json.loads(path.read_text(encoding='utf-8'))
    assert saved_backup['restored_revision'] == restored['revision']
    assert saved_backup['overrides'] == row['overrides']
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_ALREADY_RESTORED'


@pytest.mark.parametrize('confirmation', [None, False, 1, 'true'])
def test_delete_and_restore_require_boolean_confirmation(isolated_env, confirmation):
    row = archived_template()
    with pytest.raises(DubForgeError) as error:
        trash.delete(row['key'], {'revision': row['revision'], 'confirmed': confirmation})
    assert error.value.code == 'TEMPLATE_CONFIRMATION_REQUIRED'
    assert cfg.resolve(key=row['key'])['archived'] is True
    result = delete(row)
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': confirmation})
    assert error.value.code == 'TEMPLATE_CONFIRMATION_REQUIRED'
    assert len(trash.list_deleted()) == 1


@pytest.mark.parametrize('revision', [None, True, False, 2.0, '2', -1, 100])
def test_delete_strict_revision_conflict_has_no_backup(isolated_env, revision):
    row = archived_template()
    with pytest.raises(DubForgeError) as error:
        trash.delete(row['key'], {'revision': revision, 'confirmed': True})
    assert error.value.code == 'CONFIGURATION_CONFLICT'
    assert cfg.resolve(key=row['key'])['revision'] == row['revision']
    assert trash.list_deleted() == []


@pytest.mark.parametrize('key', ['global', 'project:test', 'invalid'])
def test_delete_cannot_remove_other_scope_types(isolated_env, key):
    with pytest.raises(DubForgeError) as error:
        trash.delete(key, {'revision': 0, 'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_INVALID'


def test_active_template_must_be_archived_before_deletion(isolated_env):
    row = cfg.create_template('工作模板')
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'TEMPLATE_NOT_ARCHIVED'
    assert len(cfg.list_templates()) == 1
    assert trash.list_deleted() == []


@pytest.mark.parametrize('archived_project', [False, True])
def test_bound_projects_including_archived_block_deletion(make_project, archived_project):
    uid = make_project()
    row = cfg.create_template('绑定模板')
    bind(uid, row)
    row = cfg.archive(row['key'], dict(revision=row['revision'], archived=True))
    if archived_project:
        from dubforge.project_archive import set_archived
        set_archived(uid, True)
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'TEMPLATE_IN_USE'
    assert error.value.detail['project_count'] == 1
    assert cfg.resolve(uid)['parent'] == row['key']
    assert trash.list_deleted() == []


def test_running_bound_project_rejects_before_backup(make_project):
    from dubforge.domain import crud
    uid = make_project()
    row = cfg.create_template('运行模板')
    bind(uid, row)
    row = cfg.archive(row['key'], dict(revision=row['revision'], archived=True))
    step = crud.get_step(crud.get_project(uid).id, 'split')
    step.status = 'running'
    crud.save_step(step)
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'CONFIGURATION_BUSY'
    assert trash.list_deleted() == []


def test_delete_checks_runtime_edit_permission(isolated_env, monkeypatch):
    row = archived_template()
    def blocked(key):
        assert key == row['key']
        raise runtime_edit.RuntimeEditError('任务尚未结束')
    monkeypatch.setattr(runtime_edit, 'assert_editable', blocked)
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'CONFIGURATION_BUSY'
    assert cfg.resolve(key=row['key'])['archived'] is True


def test_failed_backup_never_deletes_database_row(isolated_env, monkeypatch):
    row = archived_template()
    def fail(*args, **kwargs):
        raise PermissionError('磁盘暂时不可写')
    monkeypatch.setattr(trash, 'atomic_write_bytes', fail)
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'TEMPLATE_BACKUP_FAILED'
    assert cfg.resolve(key=row['key'])['overrides'] == row['overrides']
    assert trash.list_deleted() == []


def test_failed_delete_commit_retains_original_and_complete_backup(isolated_env, monkeypatch):
    row = archived_template()
    def fail(session):
        raise RuntimeError('提交失败')
    monkeypatch.setattr(Session, 'commit', fail)
    with pytest.raises(DubForgeError) as error:
        delete(row)
    assert error.value.code == 'TEMPLATE_DELETE_FAILED'
    assert cfg.resolve(key=row['key'])['name'] == row['name']
    listed = trash.list_deleted()
    assert len(listed) == 1 and listed[0]['restorable'] is False
    assert backup_file(listed[0]).is_file()


def test_restore_never_overwrites_a_later_same_key_template(isolated_env):
    row = archived_template()
    result = delete(row)
    with Session(get_engine()) as session:
        session.add(ConfigurationScope(key=row['key'], name='后来建立的模板',
            values='{}', revision=100, archived=False))
        session.commit()
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'CONFIGURATION_CONFLICT'
    current = cfg.resolve(key=row['key'])
    assert current['name'] == '后来建立的模板'
    assert current['revision'] == 100
    assert current['overrides'] == {}
    assert trash.list_deleted()[0]['restorable'] is False
    assert '已存在' in trash.list_deleted()[0]['reason']


def test_restore_never_reconnects_dangling_project_bindings(make_project):
    uid = make_project()
    row = archived_template()
    result = delete(row)
    with Session(get_engine()) as session:
        scope = session.get(ConfigurationScope, 'project:' + uid)
        scope.parent = row['key']
        session.add(scope)
        session.commit()
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'TEMPLATE_IN_USE'
    assert cfg.list_templates() == []
    assert backup_file(result).is_file()
    assert trash.list_deleted()[0]['restorable'] is False
    assert '解除绑定' in trash.list_deleted()[0]['reason']


@pytest.mark.parametrize('backup_id', ['../global', 'c:' + '\\' + 'secret', 'A' * 32,
                                     '0' * 31, '0' * 33, '', None])
def test_backup_identifier_cannot_read_an_arbitrary_path(isolated_env, backup_id):
    with pytest.raises(DubForgeError) as error:
        trash.restore(backup_id, {'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_INVALID'


def test_missing_backup_has_stable_error(isolated_env):
    with pytest.raises(DubForgeError) as error:
        trash.restore('0' * 32, {'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_NOT_FOUND'


def test_corrupted_backup_does_not_block_other_backups(isolated_env):
    first = delete(archived_template())
    second = delete(archived_template())
    backup_file(first).write_text('{坏 JSON', encoding='utf-8')
    listed = trash.list_deleted()
    assert [r['backup_id'] for r in listed] == [second['backup_id'], first['backup_id']]
    assert listed[1]['restorable'] is False
    assert '损坏' in listed[1]['error']
    assert listed[1]['backup_path'] == str(backup_file(first))
    with pytest.raises(DubForgeError) as error:
        trash.restore(first['backup_id'], {'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_INVALID'
    assert cfg.list_templates() == []


def test_non_identifier_json_name_is_display_only_in_trash(isolated_env):
    folder = config.data_root() / 'trash' / 'templates'
    folder.mkdir(parents=True)
    path = folder / '手动拷来的文件.json'
    path.write_text('{}', encoding='utf-8')
    listed = trash.list_deleted()
    assert len(listed) == 1
    assert listed[0]['backup_id'] is None
    assert listed[0]['restorable'] is False
    assert listed[0]['backup_file'] == path.name
    assert listed[0]['backup_path'] == str(path)
    assert listed[0]['error']


def test_changed_backup_values_fail_integrity_check(isolated_env):
    result = delete(archived_template())
    path = backup_file(result)
    data = json.loads(path.read_text(encoding='utf-8'))
    data['overrides']['publish.title'] = '未记录的修改'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_INVALID'
    assert cfg.list_templates() == []


def test_restore_commit_failure_keeps_backup_unmarked(isolated_env, monkeypatch):
    result = delete(archived_template())
    def fail(session):
        raise RuntimeError('提交失败')
    monkeypatch.setattr(Session, 'commit', fail)
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'TEMPLATE_RESTORE_FAILED'
    assert cfg.list_templates() == []
    assert 'restored_at' not in json.loads(backup_file(result).read_text(encoding='utf-8'))
    assert trash.list_deleted()[0]['restorable'] is True


def test_restoration_revalidates_against_current_global_settings(isolated_env):
    row = cfg.create_template('旧的评论配方')
    row = cfg.save(row['key'], {'revision': row['revision'], 'updates': {'comments.count': 40}})
    row = cfg.archive(row['key'], {'revision': row['revision'], 'archived': True})
    result = delete(row)
    global_scope = cfg.resolve(key='global')
    cfg.save('global', {'revision': global_scope['revision'], 'updates': {'comments.limit': 20}})
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'TEMPLATE_TRASH_INVALID'
    assert cfg.list_templates() == []
    assert backup_file(result).is_file()


def test_manual_backup_file_copy_back_can_be_found_and_restored(isolated_env):
    row = archived_template()
    result = delete(row)
    path = backup_file(result)
    payload = path.read_bytes()
    path.unlink()
    assert trash.list_deleted() == []
    path.write_bytes(payload)
    assert trash.list_deleted()[0]['backup_id'] == result['backup_id']
    restored = trash.restore(result['backup_id'], {'confirmed': True})
    assert restored['key'] == row['key']
    assert cfg.resolve(key=row['key'])['overrides'] == row['overrides']


def test_restore_marker_failure_reports_success_and_never_replaces_it(isolated_env, monkeypatch):
    row = archived_template()
    result = delete(row)
    def fail(*args, **kwargs):
        raise PermissionError('文件被占用')
    monkeypatch.setattr(trash, 'atomic_write_bytes', fail)
    restored = trash.restore(result['backup_id'], {'confirmed': True})
    assert restored['restored'] is True
    assert restored['warning']
    assert cfg.resolve(key=row['key'])['revision'] == row['revision'] + 1
    assert trash.list_deleted()[0]['restorable'] is False
    with pytest.raises(DubForgeError) as error:
        trash.restore(result['backup_id'], {'confirmed': True})
    assert error.value.code == 'CONFIGURATION_CONFLICT'


def test_template_trash_routes_precede_generic_scope_routes(isolated_env):
    client = TestClient(app)
    row = archived_template()
    assert client.get('/api/configuration/templates/trash').json() == []
    response = client.request('DELETE', '/api/configuration/' + row['key'],
        json={'revision': row['revision'], 'confirmed': True})
    assert response.status_code == 200
    backup_id = response.json()['backup_id']
    listed = client.get('/api/configuration/templates/trash')
    assert listed.status_code == 200
    assert listed.json()[0]['backup_id'] == backup_id
    restored = client.post(f'/api/configuration/templates/trash/{backup_id}/restore',
        json={'confirmed': True})
    assert restored.status_code == 200
    assert restored.json()['key'] == row['key']
    assert restored.json()['archived'] is True
    assert client.get('/api/configuration/templates/trash').json() == []
