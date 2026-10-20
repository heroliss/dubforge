"""项目目标的唯一来源、保守迁移和动态依赖；不启动制作或访问B站。"""
import pytest

from dubforge import configuration, workflow_plan, workflows
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions, assembly_version_deletion
from dubforge.publishing.models import Publication


def version(identifier, **options):
    return {**dict(id=identifier, name=identifier, dubbing=False, burn_subtitles=False,
                   include_comments=False, overrides={}), **options}


def save(uid, updates):
    data = configuration.resolve(uid)
    return configuration.save('project:' + uid, dict(revision=data['revision'],
        fingerprint=data['fingerprint'], updates=updates))


def source_project(make_project, versions=None, **values):
    uid = make_project({'auto_steps': ['download', 'preprocess', 'transcribe', 'split', 'translate', 'tts', 'assemble']})
    save(uid, {'assembly.versions': versions or [version('source')], 'run.mode': 'produce',
               'content_review.enabled': False, **values})
    return uid


def existing(uid, **values):
    with crud.session() as session:
        row = Publication(project_uid=uid, status='published', receipt='{"bvid":"BVfixture"}')
        session.add(row)
        session.commit()
        session.refresh(row)
        return {**dict(kind='existing', publication_id=row.id, replace_videos=False,
                       replacements=[], sync_fields=['chapters']), **values}


def test_legacy_goal_migration_requires_unambiguous_evidence(make_project):
    uid = make_project()
    unresolved = assembly_versions.catalog(uid)
    assert unresolved['target_version_ids'] is None
    assert unresolved['target_selection']['source'] == 'unresolved'
    assert unresolved['target_selection']['needs_confirmation']
    save(uid, {'publish.version_ids': ['src']})
    assert assembly_versions.catalog(uid)['target_selection'] == dict(
        version_ids=['src'], source='publish_selection', needs_confirmation=False, blockers=[])
    save(uid, {'assembly.target_version_ids': ['dub']})
    assert assembly_versions.catalog(uid)['target_selection']['version_ids'] == ['dub']
    assert assembly_versions.catalog(uid)['target_selection']['source'] == 'explicit'
    uid2 = source_project(make_project)
    assert assembly_versions.catalog(uid2)['target_selection']['source'] == 'single_version'


@pytest.mark.parametrize('targets', [[], ['missing'], ['dub', 'dub'], ['dub', 1], {}, True])
def test_invalid_project_targets_never_partially_save(make_project, targets):
    uid = make_project()
    before = configuration.resolve(uid)
    with pytest.raises(DubForgeError) as error:
        save(uid, {'assembly.target_version_ids': targets, 'run.mode': 'upload'})
    assert error.value.code == 'PROJECT_ASSEMBLY_TARGET_INVALID'
    assert configuration.resolve(uid)['fingerprint'] == before['fingerprint']


def test_versions_and_targets_save_in_one_cas(make_project):
    uid = make_project()
    before = assembly_versions.catalog(uid)
    result = assembly_versions.save(uid, dict(versions=[version('new')], target_version_ids=['new'],
        revision=before['revision'], fingerprint=before['fingerprint']))
    assert result['target_version_ids'] == ['new']
    assert result['revision'] == before['revision'] + 1
    with pytest.raises(DubForgeError) as error:
        assembly_versions.save(uid, dict(versions=[version('lost')], target_version_ids=['lost'],
            revision=before['revision'], fingerprint=before['fingerprint']))
    assert error.value.code == 'CONFIGURATION_CONFLICT'
    assert assembly_versions.catalog(uid)['target_version_ids'] == ['new']


def test_delete_filters_target_references_and_protects_only_target(make_project):
    uid = source_project(make_project, [version('a'), version('b'), version('c')],
                         **{'assembly.target_version_ids': ['a', 'b']})
    checked = assembly_version_deletion.plan(uid, dict(version_id='a'))
    assembly_version_deletion.delete(uid, {key: checked[key] for key in
        ('version_id', 'revision', 'fingerprint')} | {'confirmed': True})
    assert assembly_versions.catalog(uid)['target_version_ids'] == ['b']
    checked = assembly_version_deletion.plan(uid, dict(version_id='b'))
    assert not checked['can_delete']
    assert any('唯一项目目标' in item for item in checked['blockers'])


def test_original_publication_target_cannot_be_saved_or_inherited_from_templates(make_project):
    uid = make_project()
    template = configuration.create_template('测试模板')
    for key in ('global', template['key']):
        data = configuration.resolve(key=key)
        with pytest.raises(DubForgeError) as error:
            configuration.save(key, dict(revision=data['revision'], fingerprint=data['fingerprint'],
                updates={'publish.project_target': {'kind': 'new'}}))
        assert error.value.code == 'PROJECT_PUBLISH_TARGET_SCOPE'
    target = existing(uid)
    save(uid, {'publish.project_target': target})
    baseline = configuration.baseline_from(configuration.resolve(uid))
    assert baseline['values']['publish.project_target'] is None
    baseline['values']['publish.project_target'] = target
    baseline['configured'].append('publish.project_target')
    baseline['origins']['publish.project_target'] = '测试模板'
    baseline['sources']['publish.project_target'] = dict(kind='series', name='测试模板')
    restored = configuration.read_baseline(baseline)
    assert restored['values']['publish.project_target'] is None
    assert 'publish.project_target' not in restored['configured']


def test_other_project_publication_is_rejected(make_project):
    first, second = make_project(), make_project()
    target = existing(first)
    with pytest.raises(DubForgeError) as error:
        save(second, {'publish.project_target': target})
    assert error.value.code == 'PROJECT_PUBLISH_TARGET_INVALID'


@pytest.mark.parametrize('mode', ['produce', 'draft', 'upload', 'publish'])
def test_existing_bv_does_not_increase_saved_delivery_authority(make_project, mode):
    uid = source_project(make_project)
    save(uid, {'publish.project_target': existing(uid), 'run.mode': mode})
    result = configuration.run_spec(uid, target_driven=True)
    assert result['delivery_mode'] == mode
    assert result['mode'] == ('produce' if mode == 'produce' else 'update')
    assert ('update' in result) == (mode != 'produce')


def test_production_and_publication_lists_remain_distinct(make_project):
    uid = source_project(make_project, [version('a'), version('b'), version('history')],
                         **{'assembly.target_version_ids': ['a'], 'publish.version_ids': ['b'],
                            'run.mode': 'draft', 'publish.project_target': {'kind': 'new'}})
    result = configuration.run_spec(uid, target_driven=True)
    assert result['production_version_ids'] == ['a', 'b']
    assert result['version_ids'] == ['b']
    assert not result['project_goal_issues']
    save(uid, {'run.mode': 'produce'})
    assert configuration.run_spec(uid, target_driven=True)['production_version_ids'] == ['a']


def test_saved_publication_order_and_titles_survive_recipe_binding_independently_of_production(make_project):
    uid = source_project(make_project, [version('a'), version('b'), version('history')],
                         **{'assembly.target_version_ids': ['a', 'b'], 'publish.version_ids': ['b', 'a'],
                            'publish.part_titles': {'a': '原声', 'b': '配音'},
                            'run.mode': 'draft', 'publish.project_target': {'kind': 'new'}})
    before = configuration.resolve(uid)
    spec = workflow_plan.recipe(configuration.run_spec(uid, target_driven=True))
    assert before['values']['publish.version_ids'] == spec['version_ids'] == ['b', 'a']
    assert spec['production_version_ids'] == ['a', 'b']
    assert spec['part_titles'] == {'a': '原声', 'b': '配音'}
    save(uid, {'publish.version_ids': ['a', 'b']})
    after = configuration.resolve(uid)
    assert after['fingerprint'] != before['fingerprint']
    assert configuration.run_spec(uid, target_driven=True)['version_ids'] == ['a', 'b']
    assert after['values']['assembly.target_version_ids'] == ['a', 'b']
    assert after['values']['publish.part_titles'] == before['values']['publish.part_titles']


def test_source_only_goal_ignores_old_technical_checkboxes(make_project, monkeypatch):
    uid = source_project(make_project, [version('source'), version('unused-dub', dubbing=True)],
                         **{'assembly.target_version_ids': ['source']})
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert result['steps'] == ['download', 'assemble']
    assert [item['id'] for item in result['version_specs']] == ['source']
    assert not result['dubbing']['needed']
    assert not result['blockers']
    assert set(result['configuration_snapshot']['settings']) == {'download'}


def test_source_subtitles_add_only_their_actual_dependencies(make_project, monkeypatch):
    uid = source_project(make_project, [version('sub', burn_subtitles=True,
                         overrides={'assemble.subtitle_track': 'source'})])
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert 'split' in result['steps'] and 'transcribe' in result['steps']
    assert 'translate' not in result['steps'] and 'tts' not in result['steps']


def test_metadata_only_update_has_no_source_or_model_requirements(make_project, monkeypatch):
    uid = make_project()
    save(uid, {'publish.project_target': existing(uid), 'run.mode': 'draft'})
    from dubforge import workflow_update
    monkeypatch.setattr(workflow_update, 'prepare', lambda *args, **kwargs:
                        dict(blockers=[], warnings=[], account_name='隔离账号', settings_display={}))
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda _: None)
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert result['steps'] == []
    assert not result['blockers']
    assert not result['recipe']['production_version_ids']
    assert not result['recipe']['project_goal_issues']
    assert not result['configuration_snapshot']['settings']


def test_ambiguous_goals_return_readable_blockers_without_starting_jobs(make_project, monkeypatch):
    uid = make_project()
    save(uid, {'run.mode': 'produce'})
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert result['steps'] == []
    assert result['goal_issues'][0]['code'] == 'PROJECT_ASSEMBLY_TARGET_REQUIRED'
    assert any('合成成片' in item for item in result['blockers'])
    assert not crud.latest_job(crud.get_project(uid).id, 'download')
    save(uid, {'run.mode': 'upload', 'publish.project_target': None})
    spec = configuration.run_spec(uid, target_driven=True)
    assert {item['code'] for item in spec['project_goal_issues']} == {
        'PROJECT_ASSEMBLY_TARGET_REQUIRED', 'PROJECT_PUBLISH_TARGET_REQUIRED'}


def test_single_and_batch_read_the_same_saved_settings(make_project, monkeypatch):
    uid = source_project(make_project)
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    single = workflows.preview(uid, {'target_driven': True, 'mode': 'publish'})
    batch = configuration.batch_plan(dict(uids=[uid], target_driven=True, mode='publish'))['items'][0]['plan']
    assert single['recipe']['mode'] == batch['recipe']['mode'] == 'produce'
    assert single['recipe']['production_version_ids'] == batch['recipe']['production_version_ids'] == ['source']
    assert single['fingerprint'] == batch['fingerprint']
    assert configuration.run_spec(uid, target_driven=True, mode='publish')['mode'] == 'produce'
    assert not crud.latest_job(crud.get_project(uid).id, 'download')


def test_goal_recipe_rejects_a_second_technical_scope(make_project):
    uid = source_project(make_project)
    spec = configuration.run_spec(uid, target_driven=True)
    with pytest.raises(DubForgeError):
        workflow_plan.recipe({**spec, 'production_steps': ['tts']})
    legacy = workflow_plan.recipe(dict(mode='produce', production_steps=['transcribe']))
    assert not legacy.get('target_driven')
    assert legacy['production_steps'] == ['transcribe']


def test_from_step_checks_real_goal_dependencies_without_forcing_rebuild(make_project, monkeypatch):
    uid = source_project(make_project)
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    spec = configuration.run_spec(uid, target_driven=True)
    result = workflow_plan.plan(uid, {**spec, 'from_step': 'download'}, online=False)
    assert result['requested_start_step'] == 'download'
    assert not result['blockers']
    assert result['steps'] == ['download', 'assemble']
    assert 'force_rebuild' not in result['recipe']
    unused = workflow_plan.plan(uid, {**spec, 'from_step': 'tts'}, online=False)
    assert any(item['code'] == 'PROJECT_EXECUTION_START_UNUSED' for item in unused['goal_issues'])
    assert 'tts' not in unused['steps']
    with pytest.raises(DubForgeError):
        workflow_plan.recipe(dict(mode='produce', from_step='download'))


def test_metadata_only_update_rejects_an_unneeded_production_start(make_project, monkeypatch):
    uid = make_project()
    save(uid, {'publish.project_target': existing(uid), 'run.mode': 'draft'})
    from dubforge import workflow_update
    monkeypatch.setattr(workflow_update, 'prepare', lambda *args, **kwargs:
                        dict(blockers=[], warnings=[], account_name='隔离账号', settings_display={}))
    spec = configuration.run_spec(uid, target_driven=True)
    result = workflow_plan.plan(uid, {**spec, 'from_step': 'assemble'}, online=False)
    assert result['steps'] == []
    assert any(item['code'] == 'PROJECT_EXECUTION_START_UNUSED' for item in result['goal_issues'])


def test_queued_goal_is_locked_while_ordinary_step_parameters_remain_editable(make_project):
    uid = source_project(make_project)
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'transcribe')
    step.status = 'queued'
    crud.save_step(step)
    save(uid, {'assemble.crf': 19})
    assert configuration.resolve(uid)['values']['assemble.crf'] == 19
    before = configuration.resolve(uid)
    with pytest.raises(DubForgeError) as error:
        save(uid, {'assembly.target_version_ids': ['source']})
    assert error.value.code == 'CONFIGURATION_BUSY'
    assert configuration.resolve(uid)['fingerprint'] == before['fingerprint']


@pytest.mark.parametrize('mode', ['draft', 'upload', 'publish'])
def test_new_existing_video_delivery_requires_explicit_replacement_mapping(make_project, monkeypatch, mode):
    uid = source_project(make_project)
    save(uid, {'publish.project_target': existing(uid, replace_videos=True), 'run.mode': mode})
    from dubforge import workflow_update
    monkeypatch.setattr(workflow_update, 'prepare', lambda *args, **kwargs:
                        dict(blockers=[], warnings=[], account_name='隔离账号', settings_display={}))
    monkeypatch.setattr(workflow_plan, 'production_checks', lambda *args, **kwargs: [])
    spec = configuration.run_spec(uid, target_driven=True)
    assert any(item['code'] == 'PROJECT_UPDATE_REPLACEMENTS_REQUIRED' for item in spec['project_goal_issues'])
    result = workflow_plan.plan(uid, spec, online=False)
    assert any('选择要替换的分P与成片版本' in message for message in result['blockers'])
    assert not crud.latest_job(crud.get_project(uid).id, 'download')


def _prepared_subtitle_project(make_project, *, dubbing=False):
    from conftest import mark_upstream_done
    from dubforge.runner import compute_fingerprint
    uid = source_project(make_project, [version('sub', burn_subtitles=True, dubbing=dubbing)])
    if dubbing:
        save(uid, {'tts.backend': 'gptsovits'})
    mark_upstream_done(uid, 'assemble')
    project = crud.get_project(uid)
    for key in ('download', 'preprocess', 'transcribe', 'split', 'translate'):
        step = crud.get_step(project.id, key)
        step.input_fingerprint = compute_fingerprint(project, key)
        crud.save_step(step)
    return uid


def test_changed_upstream_settings_are_checked_even_if_old_files_remain_valid(make_project, monkeypatch):
    uid = _prepared_subtitle_project(make_project)
    save(uid, {'translate.style_prompt': '使用新的翻译风格'})
    checked = []
    def checks(_deps, steps, **_options):
        checked.extend(steps)
        return []
    monkeypatch.setattr(workflow_plan, 'production_checks', checks)
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert result['steps'] == ['download', 'preprocess', 'transcribe', 'split', 'translate', 'assemble']
    assert checked == ['translate', 'assemble']
    assert result['pending_production_steps'] == ['translate', 'assemble']
    assert not result['production_versions'][0]['fresh']
    assert '上游' in result['production_versions'][0]['reason']


def test_valid_cached_text_does_not_require_asr_or_llm_services_again(make_project, monkeypatch):
    uid = _prepared_subtitle_project(make_project)
    monkeypatch.setattr(workflow_plan.shutil, 'which', lambda _name: '隔离工具')
    monkeypatch.setattr(workflow_plan.environment, 'require_component', lambda _name:
                        pytest.fail('有效缓存不应再次要求识别或分离模型'))
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert result['pending_production_steps'] == ['assemble']
    assert not result['blockers']


def test_dubbing_count_is_deferred_when_selected_text_is_about_to_change(make_project, monkeypatch):
    uid = _prepared_subtitle_project(make_project, dubbing=True)
    save(uid, {'translate.style_prompt': '新的逐句表达风格'})
    checked = []
    def checks(_deps, steps, **options):
        checked.append(options['dubbing_summary'])
        return []
    monkeypatch.setattr(workflow_plan, 'production_checks', checks)
    result = workflow_plan.plan(uid, configuration.run_spec(uid, target_driven=True), online=False)
    assert 'translate' in result['pending_production_steps'] and 'tts' in result['pending_production_steps']
    assert checked[0]['deferred'] is True
    assert result['dubbing']['deferred'] is True
