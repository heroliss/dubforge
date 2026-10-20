"""固定项目制作基础值，运行连接仍共享；所有数据与调用都在隔离环境。"""
import json

import pytest
from sqlmodel import select

from dubforge import config, configuration as cfg, db
from dubforge.configuration_models import ConfigurationScope
from dubforge.domain import crud
from dubforge.domain.models import Project, StepRun
from dubforge.errors import DubForgeError
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_params import RUNTIME_PARAM_PATHS
from test_configuration import save, bind


def _forget(uid):
    project = crud.get_project(uid)
    with crud.session() as session:
        for row in session.exec(select(StepRun).where(StepRun.project_id == project.id)).all():
            session.delete(row)
        session.delete(session.get(ConfigurationScope, 'project:' + uid))
        session.delete(session.get(Project, project.id))
        session.commit()


def test_global_defaults_apply_to_new_projects_and_existing_values_stay_fixed(make_project):
    save('global', {'assemble.crf': 19, 'publish.description': ''})
    first = make_project()
    before = cfg.resolve(first)
    manifest = (crud.project_dir(first) / 'manifest.json').read_bytes()
    save('global', {'assemble.crf': 25, 'publish.description': '新的简介'})
    second = make_project()
    assert cfg.resolve(first) == before
    assert cfg.resolve(second)['values']['assemble.crf'] == 25
    assert cfg.resolve(second)['values']['publish.description'] == '新的简介'
    assert (crud.project_dir(first) / 'manifest.json').read_bytes() == manifest
    assert before['overrides'] == {}
    assert cfg.run_spec(first)['overrides']['description'] == ''
    assert 'title' not in cfg.run_spec(first)['overrides']


def test_minimal_config_can_create_project_and_legacy_choices_remain_readable(make_project):
    config.config_path().write_text('llm:\n  profiles: []\nasr:\n  model: tiny\nsplit:\n  thinking_mode: fast\n', encoding='utf-8')
    uid = make_project()
    values = cfg.resolve(uid)['values']
    assert values['asr.language'] == 'auto'
    assert values['asr.model'] == 'tiny'
    assert values['split.thinking_mode'] == 'fast'
    assert (crud.project_dir(uid) / 'manifest.json').is_file()


def test_same_template_apply_previews_effective_changes_and_keeps_sparse_overrides(make_project):
    uid = make_project()
    template = cfg.create_template('系列')
    save(template['key'], {'assemble.crf': 19, 'translate.style_prompt': '旧私人提示'})
    bind([uid], template)
    save('project:' + uid, {'assemble.voice_volume': 0})
    before = cfg.resolve(uid)
    save(template['key'], {'assemble.crf': 24, 'assemble.voice_volume': .8,
                           'translate.style_prompt': '新私人提示', 'translate.custom_terms_path': 'D:/secret/terms.xlsx'})
    assert cfg.resolve(uid) == before
    plan = cfg.binding_plan([uid], template['key'][7:])
    item = plan['projects'][0]
    details = {row['path']: row for row in item['change_details']}
    assert details['assemble.crf'] == dict(path='assemble.crf', label='软件画质 CRF', before=19, after=24)
    assert 'assemble.voice_volume' not in item['changes']
    assert {'assemble', 'translate'} <= set(item['affected_steps'])
    assert '私人提示' not in json.dumps(plan, ensure_ascii=False)
    assert 'D:/secret' not in json.dumps(plan, ensure_ascii=False)
    bind([uid], template)
    result = cfg.resolve(uid)
    assert result['values']['assemble.crf'] == 24
    assert result['values']['assemble.voice_volume'] == 0
    assert result['overrides'] == before['overrides']
    assert result['sources']['assemble.crf'] == {'kind': 'series', 'name': '系列', 'frozen': True}
    assert result['sources']['assemble.voice_volume'] == {'kind': 'project'}


def test_global_apply_is_explicit_and_source_changes_require_new_confirmation(make_project):
    uid = make_project()
    save('global', {'translate.target_language': '日本語'})
    assert cfg.resolve(uid)['values']['translate.target_language'] == '简体中文'
    plan = cfg.binding_plan([uid], '')
    assert 'translate' in plan['projects'][0]['affected_steps']
    save('global', {'translate.target_language': 'English'})
    with pytest.raises(DubForgeError) as caught:
        cfg.bind(dict(uids=[uid], template_id='', confirmed=True, fingerprint=plan['fingerprint']))
    assert caught.value.code == 'CONFIGURATION_CONFLICT'
    fresh = cfg.binding_plan([uid], '')
    cfg.bind(dict(uids=[uid], template_id='', confirmed=True, fingerprint=fresh['fingerprint']))
    assert cfg.resolve(uid)['values']['translate.target_language'] == 'English'


def test_binding_plan_uses_user_facing_labels_for_publication_fields(make_project):
    uid = make_project()
    save('global', {
        'publish.human_type2': 1029,
        'publish.season_id': 9278299,
        'publish.section_id': 10376340,
    })
    plan = cfg.binding_plan([uid], '')
    labels = {row['path']: row['label'] for row in plan['projects'][0]['change_details']}
    assert labels['publish.human_type2'] == '分区'
    assert labels['publish.season_id'] == '合集'
    assert labels['publish.section_id'] == '合集小节'
    context = plan['projects'][0]['publication_context']
    assert set(context) == {'before', 'after'}
    assert set(context['before']) == {'account_id', 'season_id'}
    assert set(context['after']) == {'account_id', 'season_id'}


def test_runtime_connections_follow_global_and_explicit_project_override_still_wins(make_project):
    config.update_config({'llm.profiles': [dict(name='服务', model='example', api_key='secret-fixture-key')],
                          'download.proxy': 'http://user:proxy-private@localhost:7897',
                          'download.cookies_path': 'D:/private/cookies.txt'})
    uid = make_project()
    root = crud.project_dir(uid)
    assert not set(cfg.resolve(uid)['baseline']['values']) & RUNTIME_PARAM_PATHS
    assert 'secret-fixture-key' not in (root / 'manifest.json').read_text(encoding='utf-8')
    assert 'proxy-private' not in (root / 'manifest.json').read_text(encoding='utf-8')
    assert 'D:/private/cookies.txt' not in (root / 'manifest.json').read_text(encoding='utf-8')
    save('global', {'download.proxy': 'http://localhost:9000', 'tts.indextts25.url': 'http://localhost:9001',
                    'tts.indextts25.request_timeout_seconds': 1200,
                    'runner.step_timeout_seconds.split': 1800})
    values = cfg.resolve(uid)
    assert values['values']['download.proxy'] == 'http://localhost:9000'
    assert values['values']['tts.indextts25.url'] == 'http://localhost:9001'
    assert values['values']['tts.indextts25.request_timeout_seconds'] == 1200
    assert values['values']['runner.step_timeout_seconds.split'] == 1800
    assert values['sources']['download.proxy'] == {'kind': 'global'}
    assert Dependencies(crud.get_project(uid)).cfg('tts.indextts25')['url'] == 'http://localhost:9001'
    save('project:' + uid, {'tts.indextts25.request_timeout_seconds': 600})
    save('global', {'tts.indextts25.request_timeout_seconds': 1600})
    assert cfg.resolve(uid)['values']['tts.indextts25.request_timeout_seconds'] == 600
    save('project:' + uid, reset=['tts.indextts25.request_timeout_seconds'])
    assert cfg.resolve(uid)['values']['tts.indextts25.request_timeout_seconds'] == 1600
    persisted = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))['configuration']
    assert not set(persisted['baseline']['values']) & RUNTIME_PARAM_PATHS
    assert all(not set(layer['values']) & RUNTIME_PARAM_PATHS for layer in persisted['layers'])


def test_old_database_column_and_projects_are_fixed_once_without_changing_steps(make_project):
    uid = make_project()
    save('project:' + uid, {'assemble.voice_volume': 0})
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'translate')
    step.status, step.input_fingerprint, step.input_snapshot = 'stale', 'old-input', '{"history":"old"}'
    crud.save_step(step)
    with db.get_engine().begin() as connection:
        connection.exec_driver_sql('ALTER TABLE configurationscope DROP COLUMN baseline')
    save_global = {'assemble.crf': 22}
    config.update_config(save_global)
    db.init_db()
    data = cfg.resolve(uid)
    assert data['values']['assemble.crf'] == 22
    assert data['overrides'] == {'assemble.voice_volume': 0}
    assert data['sources']['assemble.crf']['frozen'] is True
    record = crud.get_step(project.id, 'translate')
    assert (record.status, record.input_fingerprint, record.input_snapshot) == ('stale', 'old-input', '{"history":"old"}')
    manifest = (crud.project_dir(uid) / 'manifest.json').read_bytes()
    save('global', {'assemble.crf': 28})
    db.init_db()
    assert cfg.resolve(uid) == data
    assert (crud.project_dir(uid) / 'manifest.json').read_bytes() == manifest


def test_startup_adds_baseline_for_old_project_without_any_scope(make_project):
    uid = make_project()
    with crud.session() as session:
        session.delete(session.get(ConfigurationScope, 'project:' + uid))
        session.commit()
    save('global', {'translate.target_language': 'English'})
    db.init_db()
    assert cfg.resolve(uid)['values']['translate.target_language'] == 'English'
    save('global', {'translate.target_language': '日本語'})
    assert cfg.resolve(uid)['values']['translate.target_language'] == 'English'


def test_startup_fills_new_missing_baseline_field_once_and_preserves_others(make_project):
    uid = make_project()
    before = cfg.resolve(uid)
    with crud.session() as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        baseline = json.loads(row.baseline)
        for field in ('values', 'origins', 'sources'):
            baseline[field].pop('split.target_word_count')
        row.baseline = cfg._json(baseline)
        session.add(row)
        session.commit()
    save('global', {'split.target_word_count': 16, 'assemble.crf': 25})
    db.init_db()
    result = cfg.resolve(uid)
    assert result['values']['split.target_word_count'] == 16
    assert result['values']['assemble.crf'] == before['values']['assemble.crf']
    save('global', {'split.target_word_count': 20})
    db.init_db()
    assert cfg.resolve(uid)['values']['split.target_word_count'] == 16


@pytest.mark.parametrize('source', ['global', 'series'])
def test_rebuild_fills_new_missing_field_once_and_preserves_other_values_and_metadata(make_project, source):
    template = cfg.create_template('旧系列') if source == 'series' else None
    uid = make_project()
    if template:
        bind([uid], template)
    before = cfg.resolve(uid)
    path = crud.project_dir(uid) / 'manifest.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    baseline = data['configuration']['baseline']
    for group in ('values', 'origins', 'sources'):
        baseline[group].pop('split.target_word_count')
    baseline['configured'] = [key for key in baseline['configured'] if key != 'split.target_word_count']
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    _forget(uid)
    parent = template['key'] if template else 'global'
    save(parent, {'split.target_word_count': 16, 'assemble.crf': 25})
    assert crud.rebuild_from_dirs()['projects'] == 1
    restored = cfg.resolve(uid)
    assert restored['values']['split.target_word_count'] == 16
    assert restored['values']['assemble.crf'] == before['values']['assemble.crf']
    assert restored['baseline']['sources']['split.target_word_count']['kind'] == source
    assert restored['baseline']['sources']['assemble.crf'] == before['baseline']['sources']['assemble.crf']
    assert 'split.target_word_count' in restored['configured']
    saved = json.loads(path.read_text(encoding='utf-8'))['configuration']['baseline']
    assert saved == restored['baseline']
    save(parent, {'split.target_word_count': 20})
    db.init_db()
    assert cfg.resolve(uid)['values']['split.target_word_count'] == 16
    _forget(uid)
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert cfg.resolve(uid)['baseline'] == saved


def test_rebuild_uses_saved_baseline_against_different_global_and_template(make_project):
    template = cfg.create_template('旅行')
    save(template['key'], {'assemble.crf': 19, 'publish.description': ''})
    uid = make_project()
    bind([uid], template)
    before = cfg.resolve(uid)
    _forget(uid)
    save(template['key'], {'assemble.crf': 28, 'publish.description': '模板后来编辑'})
    save('global', {'translate.target_language': '日本語'})
    assert crud.rebuild_from_dirs()['projects'] == 1
    restored = cfg.resolve(uid)
    assert restored['values'] == before['values']
    assert restored['baseline'] == before['baseline']
    assert restored['configured'] == before['configured']
    assert cfg.run_spec(uid)['overrides']['description'] == ''
    assert 'title' not in cfg.run_spec(uid)['overrides']


def test_legacy_manifest_layers_supply_its_own_baseline(make_project):
    save('global', {'assemble.crf': 19})
    uid = make_project()
    root = crud.project_dir(uid)
    raw = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    raw['configuration'].pop('baseline')
    for layer in raw['configuration']['layers']:
        layer.pop('configured', None)
    (root / 'manifest.json').write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    _forget(uid)
    save('global', {'assemble.crf': 28})
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert cfg.resolve(uid)['values']['assemble.crf'] == 19
    assert not {'title', 'description', 'tags', 'dynamic'} & set(cfg.run_spec(uid)['overrides'])
    save('global', {'assemble.crf': 24})
    assert cfg.resolve(uid)['values']['assemble.crf'] == 19


@pytest.mark.parametrize('scope', ['series', 'project'])
def test_legacy_manifest_preserves_explicit_empty_copy_fields(scope, make_project):
    template = cfg.create_template('旧系列')
    if scope == 'series':
        save(template['key'], {'publish.description': ''})
    uid = make_project()
    bind([uid], template)
    if scope == 'project':
        save('project:' + uid, {'publish.description': ''})
    root = crud.project_dir(uid)
    raw = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    raw['configuration'].pop('baseline')
    for layer in raw['configuration']['layers']:
        layer.pop('configured', None)
    (root / 'manifest.json').write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')
    _forget(uid)
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert cfg.run_spec(uid)['overrides']['description'] == ''
    assert not {'title', 'tags', 'dynamic'} & set(cfg.run_spec(uid)['overrides'])


def test_invalid_restored_baseline_rolls_back_and_corrected_manifest_can_be_retried(make_project):
    uid = make_project()
    root = crud.project_dir(uid)
    valid = (root / 'manifest.json').read_bytes()
    raw = json.loads(valid)
    raw['configuration']['baseline'] = []
    damaged = json.dumps(raw, ensure_ascii=False).encode('utf-8')
    (root / 'manifest.json').write_bytes(damaged)
    _forget(uid)
    with pytest.raises(DubForgeError) as caught:
        crud.rebuild_from_dirs()
    assert caught.value.code == 'CONFIGURATION_BASELINE_INVALID'
    assert crud.get_project(uid) is None
    assert (root / 'manifest.json').read_bytes() == damaged
    with crud.session() as session:
        assert session.get(ConfigurationScope, 'project:' + uid) is None
    (root / 'manifest.json').write_bytes(valid)
    assert crud.rebuild_from_dirs()['projects'] == 1
    assert cfg.resolve(uid)['baseline'] is not None


@pytest.mark.parametrize('baseline', ['{bad json', '[]', 'null', 'false', '{"schema":1,"values":{}}'])
def test_corrupt_baseline_is_reported_without_falling_back_to_global(make_project, baseline):
    uid = make_project()
    with crud.session() as session:
        row = session.get(ConfigurationScope, 'project:' + uid)
        row.baseline = baseline
        session.add(row)
        session.commit()
    with pytest.raises(DubForgeError) as caught:
        cfg.resolve(uid)
    assert caught.value.code == 'CONFIGURATION_BASELINE_INVALID'
    with pytest.raises(DubForgeError):
        db.init_db()
