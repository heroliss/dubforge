"""首次发布缺省不落库；任何稿件线索都必须保留选择边界。"""
import json

import pytest
from sqlmodel import Session

from dubforge import configuration
from dubforge.configuration_models import ConfigurationScope
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.project_publish_target import history_projects
from dubforge.project_settings_summary import project_summaries
from dubforge.publishing.models import Publication, PublicationUpdate
from dubforge.workflow_models import WorkflowRun
from test_configuration import bind, save


def test_first_publish_default_is_effective_only_and_shared_by_cards(make_project):
    uid = make_project()
    data = configuration.resolve(uid)
    assert data['values']['publish.project_target'] == {'kind': 'new'}
    assert data['target_selection'] == dict(source='first_publish',
        reason='项目尚无发布记录，首次发布默认创建新稿', has_history=False, explicit=False)
    assert data['defaults']['publish.project_target'] is None
    assert 'publish.project_target' not in data['overrides']
    assert 'publish.project_target' not in data['configured']
    assert data['origins']['publish.project_target'] == '首次发布默认新稿'
    summary = project_summaries([crud.get_project(uid)])[uid]
    assert summary['target_kind'] == 'new'
    assert summary['target_selection'] == data['target_selection']
    with Session(get_engine()) as session:
        assert 'publish.project_target' not in json.loads(session.get(ConfigurationScope, 'project:' + uid).values)


def test_template_apply_never_persists_or_inherits_the_first_publish_default(make_project):
    uid = make_project()
    template = configuration.create_template('批次系列')
    save(template['key'], {'run.mode': 'publish'})
    bind([uid], template)
    data = configuration.resolve(uid)
    assert data['values']['run.mode'] == 'publish'
    assert data['values']['publish.project_target'] == {'kind': 'new'}
    assert data['baseline']['values']['publish.project_target'] is None
    assert 'publish.project_target' not in data['baseline']['configured']
    assert 'publish.project_target' not in data['overrides']


def test_explicit_null_stays_unselected_until_reset_without_history(make_project):
    uid = make_project()
    save('project:' + uid, {'publish.project_target': None})
    data = configuration.resolve(uid)
    assert data['values']['publish.project_target'] is None
    assert data['target_selection']['source'] == 'explicit_unset'
    assert data['target_selection']['has_history'] is False
    assert 'publish.project_target' in data['overrides']
    assert project_summaries([crud.get_project(uid)])[uid]['target_kind'] == ''
    save('project:' + uid, reset=['publish.project_target'])
    assert configuration.resolve(uid)['target_selection']['source'] == 'first_publish'


def test_explicit_null_from_create_input_is_not_pruned(isolated_env):
    project = crud.create_project('明确保留目标为空', params={'step_params': {'publish.project_target': None}})
    data = configuration.resolve(project.uid)
    assert data['values']['publish.project_target'] is None
    assert data['target_selection']['source'] == 'explicit_unset'


@pytest.mark.parametrize('status,marker', [('draft', ''), ('uploading', ''), ('unknown', ''),
    ('published', ''), ('failed', ''), ('draft', 'deleted'), ('published', 'archived')])
def test_any_publication_history_requires_choice_even_without_a_bvid(make_project, status, marker):
    uid = make_project()
    before = configuration.resolve(uid)
    with Session(get_engine()) as session:
        publication = Publication(project_uid=uid, status=status)
        if marker:
            from dubforge.publishing.models import now
            setattr(publication, marker + '_at', now())
        session.add(publication); session.commit()
    after = configuration.resolve(uid)
    assert after['values']['publish.project_target'] is None
    assert after['target_selection']['source'] == 'history_requires_choice'
    assert after['target_selection']['has_history'] is True
    assert after['fingerprint'] != before['fingerprint']
    summary = project_summaries([crud.get_project(uid)])[uid]
    assert summary['target_kind'] == '' and summary['target_selection'] == after['target_selection']


def test_workflow_and_update_history_survive_missing_local_publications(make_project):
    uids = [make_project() for _ in range(6)]
    with Session(get_engine()) as session:
        for index, (uid, recipe, publication_id) in enumerate([
            (uids[0], '{}', 'missing-receipt'),
            (uids[1], '{"mode":"update"}', ''),
            (uids[2], '{"update":{"publication_id":"old"}}', ''),
            (uids[3], '{broken', ''),
            (uids[4], '{"mode":"produce"}', ''),
        ]):
            session.add(WorkflowRun(project_uid=uid, request_id='history-' + str(index),
                                    recipe=recipe, publication_id=publication_id))
        session.add(PublicationUpdate(project_uid=uids[5], source_publication_id='missing',
                                      account_id='fixture', bvid='BVfixture'))
        session.commit()
        assert history_projects(session, uids) == set(uids[:4] + uids[5:])
        assert history_projects(session, []) == set()
    assert configuration.resolve(uids[4])['target_selection']['source'] == 'first_publish'
    for uid in uids[:4] + uids[5:]:
        assert configuration.resolve(uid)['target_selection']['source'] == 'history_requires_choice'


def test_explicit_new_survives_history_and_target_reset_requires_choice(make_project):
    uid = make_project()
    save('project:' + uid, {'publish.project_target': {'kind': 'new'}})
    with Session(get_engine()) as session:
        session.add(Publication(project_uid=uid, status='draft')); session.commit()
    data = configuration.resolve(uid)
    assert data['values']['publish.project_target'] == {'kind': 'new'}
    assert data['target_selection']['source'] == 'project'
    assert data['target_selection']['has_history'] is True
    save('project:' + uid, reset=['publish.project_target'])
    assert configuration.resolve(uid)['values']['publish.project_target'] is None


@pytest.mark.parametrize('recipe', ['null', '[]', '{}', '{"mode":"unknown"}', '{"mode":"draft"}',
                                  '{"mode":"upload"}', '{"mode":"publish"}'])
def test_confirmed_delivery_or_unreadable_workflow_recipe_requires_choice(make_project, recipe):
    uid = make_project()
    with Session(get_engine()) as session:
        session.add(WorkflowRun(project_uid=uid, request_id='confirmed-workflow', recipe=recipe,
                                status='paused'))
        session.commit()
    assert configuration.resolve(uid)['target_selection']['source'] == 'history_requires_choice'


@pytest.mark.parametrize('target', [{}, {'kind': 'garbage'}, 'new', {'kind': 'new', 'publication_id': 'old'},
    {'kind': 'existing', 'publication_id': 'missing'}, {'kind': ['new']}])
def test_damaged_explicit_target_cannot_fall_back_to_new(make_project, target):
    uid = make_project()
    with Session(get_engine()) as session:
        own = session.get(ConfigurationScope, 'project:' + uid)
        own.values = json.dumps({'publish.project_target': target})
        session.add(own); session.commit()
    data = configuration.resolve(uid)
    assert data['values']['publish.project_target'] is None
    assert data['target_selection']['source'] == 'invalid'
    assert project_summaries([crud.get_project(uid)])[uid]['target_kind'] == ''


@pytest.mark.parametrize('mode', ['produce', 'draft', 'upload', 'publish'])
def test_only_saved_new_target_confirms_a_new_publication(make_project, mode):
    uid = make_project()
    save('project:' + uid, {'run.mode': mode})
    assert not configuration.run_spec(uid, target_driven=True).get('confirm_new_publication')
    save('project:' + uid, {'publish.project_target': {'kind': 'new'}})
    assert bool(configuration.run_spec(uid, target_driven=True).get('confirm_new_publication')) == (mode != 'produce')


def test_restored_parent_target_is_never_used_by_config_or_summary(make_project):
    uid = make_project()
    template = configuration.create_template('旧模板')
    with Session(get_engine()) as session:
        parent = session.get(ConfigurationScope, template['key'])
        parent.values = json.dumps({'publish.project_target': {'kind': 'existing', 'publication_id': 'foreign'}})
        own = session.get(ConfigurationScope, 'project:' + uid)
        own.parent, own.baseline = template['key'], '{}'
        session.add(parent); session.add(own); session.commit()
    assert configuration.resolve(uid)['values']['publish.project_target'] == {'kind': 'new'}
    assert project_summaries([crud.get_project(uid)])[uid]['target_kind'] == 'new'
    with Session(get_engine()) as session:
        session.add(Publication(project_uid=uid)); session.commit()
    assert configuration.resolve(uid)['values']['publish.project_target'] is None
    assert project_summaries([crud.get_project(uid)])[uid]['target_kind'] == ''
