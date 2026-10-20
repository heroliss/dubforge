"""制作范围与原稿更新范围独立；所有平台调用使用隔离替身。"""
import copy
import threading

import pytest

from dubforge import workflow_update, workflows
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions
from dubforge.publishing import service as pub, updates
from test_workflow_update_handoff import target, env, setup_publish, _run
from test_publication_updates import finish


@pytest.fixture
def scoped(target, monkeypatch):
    monkeypatch.setattr(assembly_versions, 'catalog', lambda uid: dict(versions=target['definitions']))
    monkeypatch.setattr(assembly_versions, 'resolve', lambda *args, **kwargs: pytest.fail('资料预检不能解析音视频依赖'))
    return target


def test_metadata_only_requires_no_output_and_uses_only_explicit_fields(scoped):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['description'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers']
    assert data['values'] == {'description': '新的简介'}
    assert not data['chapters']['enabled'] and not data['collection']['enabled']
    assert all(not part['replaced'] and not part['added'] for part in data['parts'])
    assert [part['version_id'] for part in data['parts']] == ['source-v', 'dub-v']
    assert data['sync_fields'] == ['description']
    assert next(option for option in data['scope_options'] if option['key'] == 'description')['selected']
    assert not scoped['uploads'] and not scoped['writes']


@pytest.mark.parametrize('state,rejected', [(0, False), (-2, True), (-16, True)])
def test_rejection_warning_reflects_the_actual_remote_state(scoped, state, rejected):
    scoped['remote']['state'] = state
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['description'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert any('原稿驳回' in warning for warning in data['warnings']) is rejected
    offline = workflow_update.prepare(scoped['uid'], scoped['spec'], online=False, previous=data)
    assert any('原稿驳回' in warning for warning in offline['warnings']) is rejected


def test_metadata_only_does_not_require_every_original_part_to_be_mapped(scoped):
    scoped['records']['source-v'].artifact_hash = 'unproven'
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['description'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers'] and not data['parts'][0]['version_id']


def test_explicit_empty_scope_does_not_inherit_chapters_collection_or_copy(scoped):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=[])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert data['blockers'] and '仅制作' in data['blockers'][0]
    assert not data['values'] and not data['chapters']['enabled'] and not data['collection']['enabled']
    assert not any(option['selected'] for option in data['scope_options'])


def test_unsupported_fields_are_visible_with_reasons_and_cannot_be_submitted(scoped):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['selected_comments', 'remove_parts'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert any('精选评论' in issue for issue in data['blockers'])
    for key in ('selected_comments', 'close_comments', 'close_danmaku', 'visibility', 'remove_parts'):
        option = next(option for option in data['scope_options'] if option['key'] == key)
        assert not option['supported'] and option['reason']


def test_part_titles_and_order_use_proven_version_identity_and_keep_all_original_parts(scoped):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['part_titles', 'part_order'])
    scoped['spec']['part_titles'] = {'source-v': '重命名原声'}
    definitions = list(reversed(scoped['definitions']))
    data = workflow_update.prepare(scoped['uid'], scoped['spec'], definitions)
    assert data['part_titles'] == {'1000': '重命名原声', '1001': '配音'}
    assert data['part_order'] == [1001, 1000]
    assert not data['blockers'] and not any(part['replaced'] for part in data['parts'])


@pytest.mark.parametrize('provided', [False, True])
def test_explicit_part_order_follows_publication_selection_instead_of_catalog(scoped, provided):
    scoped['spec']['version_ids'] = ['dub-v', 'source-v']
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['part_order'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'], scoped['definitions'] if provided else None)
    assert data['part_order'] == [1001, 1000]
    assert not data['blockers'] and not any(part['replaced'] for part in data['parts'])
    assert not scoped['uploads'] and not scoped['writes']


@pytest.mark.parametrize('reorder', [False, True])
@pytest.mark.parametrize('provided', [False, True])
def test_project_append_order_uses_publication_selection_without_implicitly_reordering_originals(scoped, provided, reorder):
    scoped['definitions'].extend([dict(id='extra-a', name='新增甲'), dict(id='extra-b', name='新增乙')])
    scoped['spec'].update(target_driven=True, version_ids=['extra-b', 'dub-v', 'extra-a', 'source-v'])
    scoped['spec']['update'].update(replace_videos=False,
        replacements=[dict(old_cid=1000, version_id='source-v'), dict(old_cid=1001, version_id='dub-v')],
        sync_fields=['add_parts', *(['part_order'] if reorder else [])])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'], scoped['definitions'] if provided else None)
    assert [part['version_id'] for part in data['parts']] == ['source-v', 'dub-v', 'extra-b', 'extra-a']
    assert [part['title'] for part in data['parts'][-2:]] == ['新增乙', '新增甲']
    assert data['part_order'] == ([-1, 1001, -2, 1000] if reorder else None)
    assert not data['blockers'] and not any(part['replaced'] for part in data['parts'])
    assert not scoped['uploads'] and not scoped['writes']


def test_reordered_selection_does_not_reorder_cid_replacements_without_explicit_scope(scoped):
    scoped['spec'].update(target_driven=True, version_ids=['dub-v', 'source-v'])
    scoped['spec']['update'].update(replace_videos=True, sync_fields=[],
        replacements=[dict(old_cid=1001, version_id='dub-v'), dict(old_cid=1000, version_id='source-v')])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'], scoped['definitions'])
    assert [(part['old_cid'], part['version_id']) for part in data['parts']] == [(1000, 'source-v'), (1001, 'dub-v')]
    assert all(part['replaced'] for part in data['parts']) and data['part_order'] is None
    assert not data['blockers'] and not scoped['uploads'] and not scoped['writes']


def test_metadata_scope_accepts_explicit_mapping_for_titles_without_replacing_videos(scoped):
    for row in scoped['records'].values():
        row.artifact_hash = 'unproven'
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['part_titles', 'part_order'],
        replacements=[dict(old_cid=1000, version_id='source-v'), dict(old_cid=1001, version_id='dub-v')])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers'] and data['part_titles'] == {'1000': '原声', '1001': '配音'}
    assert data['part_order'] == [1000, 1001]
    assert not any(part['replaced'] for part in data['parts'])
    assert not scoped['uploads'] and not scoped['writes']


def test_add_parts_does_not_replace_existing_versions_when_video_replacement_is_off(scoped):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['add_parts', 'part_order'])
    definitions = [*scoped['definitions'], dict(id='extra', name='新增比较版')]
    data = workflow_update.prepare(scoped['uid'], scoped['spec'], definitions)
    assert not data['blockers']
    assert data['parts'][-1]['added'] and data['parts'][-1]['old_cid'] == -1
    assert data['parts'][-1]['version_id'] == 'extra'
    assert data['part_order'] == [1000, 1001, -1]
    assert not any(part['replaced'] for part in data['parts'])


def test_keep_equal_selected_values_for_zero_write_reconciliation(scoped, monkeypatch):
    from dubforge.publishing import project_copy
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['title'])
    monkeypatch.setattr(project_copy, 'preview', lambda uid: dict(
        values={'title': scoped['remote']['settings']['title']}, configured=[]))
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers']
    assert data['values'] == {'title': scoped['remote']['settings']['title']}


def test_explicit_chapter_scope_overrides_disabled_project_default_for_this_run(scoped):
    scoped['values']['publish.chapters_enabled'] = False
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['chapters'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert data['chapters']['enabled']
    assert data['sync_fields'] == ['chapters']


def test_no_addition_target_requires_scope_change_unless_other_update_can_proceed(scoped):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['add_parts'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert any('没有可追加' in issue for issue in data['blockers'])
    scoped['spec']['update']['sync_fields'].append('description')
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers'] and any('没有可追加' in warning for warning in data['warnings'])


def test_unproven_titles_are_not_claimed_as_synced(scoped):
    for row in scoped['records'].values():
        row.artifact_hash = 'unproven'
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['part_titles'])
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['part_titles'] and any('没有可可靠对应' in issue for issue in data['blockers'])
    scoped['spec']['update']['sync_fields'].append('description')
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers'] and any('没有可可靠对应' in warning for warning in data['warnings'])


def test_metadata_delivery_needs_no_assembled_versions_and_reuses_the_same_task(scoped):
    run = _run(scoped)
    target_plan = run['plan']['update_plan']
    target_plan['replace_videos'] = False
    for part in target_plan['parts']:
        part['replaced'] = False
    run['plan'].pop('assembled_versions')
    workflows._leases[scoped['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = finish(workflow_update.deliver(run, threading.Event())['id'])
            assert task['status'] == 'completed'
            assert not scoped['uploads'] and len(scoped['writes']) == 1
            replay = workflow_update.deliver(run)
            assert replay['id'] == task['id'] and len(scoped['writes']) == 1
    finally:
        workflows._leases.pop(scoped['uid'], None)


def test_selected_equal_metadata_completes_without_upload_or_write(scoped):
    run = _run(scoped)
    target_plan = run['plan']['update_plan']
    target_plan['values'] = dict(title=scoped['remote']['settings']['title'])
    target_plan['replace_videos'] = False
    for part in target_plan['parts']:
        part['replaced'] = False
    run['plan'].pop('assembled_versions')
    workflows._leases[scoped['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = finish(workflow_update.deliver(run)['id'])
        assert task['status'] == 'completed'
        assert not scoped['uploads'] and not scoped['writes']
    finally:
        workflows._leases.pop(scoped['uid'], None)


def test_handoff_appends_actual_output_and_resolves_negative_order_identity(scoped):
    extra = dict(id='comparison', name='新增比较版')
    scoped['definitions'].append(extra)
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['add_parts', 'part_order'])
    run = _run(scoped)
    project = crud.get_project(scoped['uid'])
    root = crud.project_dir(scoped['uid'])
    path = root / 'artifacts/assemble/final_dub_hardsub_20261017_120000_123456.mp4'
    path.write_bytes(b'isolated-comparison')
    artifact = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
    run['plan']['assembled_versions'].append(dict(version=extra, artifact_id=artifact.id,
        artifact_sha256=artifact.sha256))
    workflows._leases[scoped['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = finish(workflow_update.deliver(run)['id'])
        assert task['status'] == 'completed', task
        assert len(scoped['uploads']) == 1 and len(scoped['writes']) == 1
        assert [part['cid'] for part in scoped['writes'][0]['videos']] == [1000, 1001, 2000 + artifact.id]
        assert scoped['writes'][0]['videos'][-1]['title'] == '新增比较版'
    finally:
        workflows._leases.pop(scoped['uid'], None)


@pytest.mark.parametrize('reorder', [False, True])
def test_project_handoff_submits_two_appended_parts_in_saved_selection_order(scoped, reorder):
    extras = [dict(id='extra-a', name='新增甲'), dict(id='extra-b', name='新增乙')]
    scoped['definitions'].extend(extras)
    scoped['spec'].update(target_driven=True, version_ids=['extra-b', 'dub-v', 'extra-a', 'source-v'])
    scoped['spec']['update'].update(replace_videos=False,
        replacements=[dict(old_cid=1000, version_id='source-v'), dict(old_cid=1001, version_id='dub-v')],
        sync_fields=['add_parts', *(['part_order'] if reorder else [])])
    run = _run(scoped)
    project, root = crud.get_project(scoped['uid']), crud.project_dir(scoped['uid'])
    cids = {}
    for index, version in enumerate(extras):
        path = root / f'artifacts/assemble/final_dub_hardsub_20261019_120000_00000{index}.mp4'
        path.write_bytes(version['id'].encode())
        artifact = crud.add_artifact(project.id, 'assemble', 'video', path.name, path, str(path.relative_to(root)))
        cids[version['id']] = 2000 + artifact.id
        run['plan']['assembled_versions'].append(dict(version=version, artifact_id=artifact.id,
            artifact_sha256=artifact.sha256))
    workflows._leases[scoped['uid']] = run['id']
    try:
        with workflows._owned(run['id']):
            task = finish(workflow_update.deliver(run)['id'])
        assert task['status'] == 'completed', task
        assert len(scoped['uploads']) == 2 and len(scoped['writes']) == 1
        actual = scoped['writes'][0]['videos']
        expected = ([cids['extra-b'], 1001, cids['extra-a'], 1000] if reorder else
                    [1000, 1001, cids['extra-b'], cids['extra-a']])
        assert [part['cid'] for part in actual] == expected
        assert [part['title'] for part in actual if part['cid'] in cids.values()] == ['新增乙', '新增甲']
    finally:
        workflows._leases.pop(scoped['uid'], None)


def test_custom_cover_is_frozen_by_hash_and_changes_block_before_upload(scoped, monkeypatch):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['cover'])
    scoped['spec'].update(cover_source='custom', cover_id='cover-reference')
    monkeypatch.setattr(pub, '_cover_path', lambda key: 'isolated-cover')
    monkeypatch.setattr(pub, '_file_hash', lambda path: 'initial-hash')
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers'] and data['cover']['sha256'] == 'initial-hash'
    run = dict(id='isolated-flow', project_uid=scoped['uid'], plan=dict(update_plan=data))
    monkeypatch.setattr(workflow_update, 'get_task', lambda run: None)
    monkeypatch.setattr(pub, '_file_hash', lambda path: 'changed-hash')
    with pytest.raises(DubForgeError) as error:
        workflow_update.deliver(run)
    assert error.value.code == 'WORKFLOW_OUTPUT_STALE'
    assert not scoped['uploads'] and not scoped['writes']


def test_platform_cover_source_is_frozen_and_not_downloaded_in_preflight(scoped, monkeypatch):
    from dubforge.publishing import project_copy
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['cover'])
    scoped['spec']['cover_source'] = 'platform'
    content = dict(thumbnail='https://i.ytimg.com/initial.jpg')
    monkeypatch.setattr(project_copy, 'source_content', lambda project: copy.deepcopy(content))
    monkeypatch.setattr(pub, 'platform_cover', lambda uid: pytest.fail('预检不能下载封面'))
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers'] and data['cover']['source'] == 'platform'
    content['thumbnail'] = 'https://i.ytimg.com/changed.jpg'
    monkeypatch.setattr(workflow_update, 'get_task', lambda run: None)
    with pytest.raises(DubForgeError) as error:
        workflow_update.deliver(dict(id='isolated', project_uid=scoped['uid'], plan=dict(update_plan=data)))
    assert error.value.code == 'PUBLISH_REMOTE_CHANGED'
    assert not scoped['writes'] and not scoped['uploads']


def test_chapter_only_without_proven_timeline_blocks_instead_of_claiming_success(scoped, monkeypatch):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['chapters'])
    monkeypatch.setattr(updates, '_proven_parts', lambda source: {})
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert any('没有可核验时间轴' in issue for issue in data['blockers'])
    option = next(option for option in data['scope_options'] if option['key'] == 'chapters')
    assert '可同步0个' in option['value_display'] and '保留2个' in option['reason']
    assert len(data['parts']) == 2 and not scoped['uploads'] and not scoped['writes']


def test_unproven_chapter_parts_are_named_as_preserved_while_other_updates_continue(scoped, monkeypatch):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['description', 'chapters'])
    monkeypatch.setattr(updates, '_proven_parts', lambda source: {})
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers']
    for part in scoped['remote']['videos']:
        assert any(part['title'] in warning and '章节时间轴无法核验' in warning for warning in data['warnings'])
    assert data['values'] == {'description': '新的简介'}


def test_chapter_scope_counts_proven_parts_without_requiring_all_parts(scoped, monkeypatch):
    scoped['spec']['update'].update(replace_videos=False, sync_fields=['chapters'])
    proven = updates._proven_parts(updates._source(scoped['source']['id']))
    monkeypatch.setattr(updates, '_proven_parts', lambda source: {1000: proven[1000]})
    data = workflow_update.prepare(scoped['uid'], scoped['spec'])
    assert not data['blockers']
    option = next(option for option in data['scope_options'] if option['key'] == 'chapters')
    assert '可同步1个' in option['value_display'] and '保留1个' in option['reason']
    assert any(scoped['remote']['videos'][1]['title'] in warning for warning in data['warnings'])
