"""选择性成片更新：目标合同与工作稿分离，只保存并执行明确勾选内容。"""
import copy
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_manifest as manifests, assembly_sentence_changes as changes
from dubforge.pipeline import assembly_updates as updates, assembly_versions as av, output_cuts, assembly_partial, segment_replace
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from test_assemble import _prepare_outputs_project
from test_assembly_versions import wait
from test_configuration import save
from test_content_review_publication_comments import _item, _overlay
from test_subtitle_document import edit

_REAL_PARTIAL_INSPECT = assembly_partial._inspect
_REAL_SEGMENT_WINDOWS = segment_replace.plan_windows


def _manifest():
    """405、406句与重叠评论共用窗口，声音引用能区别历史和当前版本。"""
    version = dict(id='base', name='成片基准', dubbing=True, burn_subtitles=True,
                   include_comments=True, overrides={}, values={
                       'assemble.crf': 22, 'assemble.subtitle_track': 'source',
                       'assemble.subtitle_color': '#FFFFFF', 'assemble.subtitle_font_size': 40,
                       'assemble.bgm_volume': 0, 'assemble.voice_volume': 1,
                       'assemble.loudnorm': False, 'assemble.final_loudness_mode': 'off',
                       'content_review.enabled': False})
    lines, subtitles, audio = {}, [], []
    for ident, start in [(405, 1.), (406, 3.)]:
        text = f'Historical English {ident}'
        event = dict(start=start, end=start + 1, text=text)
        sound = dict(start=start, end=start + 1, source='dub', gain_db=0,
                     identity={'sha256': str(ident) * 16, 'size': 100})
        lines[str(ident)] = dict(id=ident, known=True, range={'start': start, 'end': start + 1},
            source_range={'start': start, 'end': start + 1},
            document=dict(source_text=text, text=f'旧译文{ident}', start=start, end=start + 1),
            subtitle=[event], audio=[sound])
        subtitles.append(dict(id=ident, **event, parts=[['source', text]]))
        audio.append(dict(id=ident, source='dub', start=start, end=start + 1, audio_offset=0,
                          gain_db=0, media=dict(path=f'old-{ident}.wav', identity=copy.deepcopy(sound['identity']))))
    comment = dict(id='c1', start=1., end=2., parts=['成片中的评论'], style_signature='old-style')
    return dict(schema=1, duration=6., version=version,
        inputs=dict(schema=1, coverage='known', duration=6., lines=lines,
            global_inputs=dict(values=av.active_values(version), source={'sha256': 'old-source'},
                               dimensions={key: version[key] for key in ('dubbing', 'burn_subtitles', 'include_comments')}),
            comment_inputs=dict(known=True, events=[comment], render_version=1)),
        subtitles=subtitles, audio=audio,
        comments=[dict(id='c1', start=1., end=2., item={'display_text': '成片中的评论'}, settings={'color': 'old'})],
        media=dict(source={'path': 'source.mp4', 'identity': {'sha256': 'old-source'}},
                   background={'path': 'old-bgm.wav', 'identity': {'sha256': 'old-bgm'}}),
        review=dict(plan={'ranges': []}, audio_plan={'ranges': []}))


def _changed(before):
    current = copy.deepcopy(before)
    for ident in [405, 406]:
        text = f'Corrected English {ident}'
        current['inputs']['lines'][str(ident)]['document']['source_text'] = text
        current['inputs']['lines'][str(ident)]['subtitle'][0]['text'] = text
        next(row for row in current['subtitles'] if row['id'] == ident).update(text=text, parts=[['source', text]])
        current['inputs']['lines'][str(ident)]['audio'][0]['identity'] = {'sha256': f'new-{ident}', 'size': 200}
        next(row for row in current['audio'] if row['id'] == ident)['media'] = {
            'path': f'new-{ident}.wav', 'identity': {'sha256': f'new-{ident}', 'size': 200}}
    current['comments'][0]['item']['display_text'] = '工作稿中的新评论'
    current['inputs']['comment_inputs']['events'][0]['parts'] = ['工作稿中的新评论']
    current['version']['values'].update({'assemble.subtitle_color': '#00FF00', 'assemble.subtitle_font_size': 52})
    return current


def _entries():
    return {**{f'{kind}:{ident}': (dict(id=f'{kind}:{ident}', line_id=ident), kind)
               for kind in ['subtitle', 'audio'] for ident in [405, 406]},
            'comment:c1': (dict(id='comment:c1', comment_id='c1'), 'comment'),
            'assemble.subtitle_color': (dict(id='assemble.subtitle_color'), 'settings'),
            'assemble.subtitle_font_size': (dict(id='assemble.subtitle_font_size'), 'settings')}


def test_selected_405_preserves_unselected_406_comment_and_historical_audio():
    before = _manifest()
    current = _changed(before)
    historical, working = copy.deepcopy(before), copy.deepcopy(current)
    target = updates.target_manifest(before, current, _entries(), {'subtitle:405'})
    assert target['inputs']['lines']['405']['document']['source_text'] == 'Corrected English 405'
    assert target['inputs']['lines']['406'] == before['inputs']['lines']['406']
    assert target['subtitles'][1] == before['subtitles'][1]
    assert target['comments'] == before['comments'], '同一时间窗口的新评论不可随字幕被带入'
    assert target['inputs']['comment_inputs'] == before['inputs']['comment_inputs']
    assert target['audio'] == before['audio'], '未选声音必须保留旧WAV路径与身份'
    assert target['version']['values'] == before['version']['values']
    assert before == historical and current == working, '构建目标不能修改成片记录或工作稿'


def test_only_one_global_setting_or_one_audio_uses_current_value():
    before = _manifest()
    current = _changed(before)
    color = updates.target_manifest(before, current, _entries(), {'assemble.subtitle_color'})
    assert color['version']['values']['assemble.subtitle_color'] == '#00FF00'
    assert color['version']['values']['assemble.subtitle_font_size'] == 40
    assert color['subtitles'] == before['subtitles'] and color['audio'] == before['audio']
    assert color['inputs']['global_inputs']['values']['assemble.subtitle_color'] == '#00FF00'
    audio = updates.target_manifest(before, current, _entries(), {'audio:405'})
    assert audio['audio'][0]['media']['path'] == 'new-405.wav'
    assert audio['audio'][1] == before['audio'][1]
    assert audio['subtitles'] == before['subtitles']
    assert audio['inputs']['lines']['405']['document'] == before['inputs']['lines']['405']['document']
    assert updates._method(before, audio, {'audio:405'}, _entries()) == 'full'


def test_comment_only_selection_preserves_subtitles_and_other_comment_events():
    before = _manifest()
    before['comments'].append(dict(id='c2', start=3., end=4., item={'display_text': '另一条旧评论'}, settings={}))
    before['inputs']['comment_inputs']['events'].append(dict(id='c2', start=3., end=4., parts=['另一条旧评论']))
    current = _changed(before)
    current['comments'][1]['item']['display_text'] = '另一条新评论'
    target = updates.target_manifest(before, current, _entries(), {'comment:c1'})
    assert target['comments'][0]['item']['display_text'] == '工作稿中的新评论'
    assert target['comments'][1] == before['comments'][1]
    assert target['subtitles'] == before['subtitles'] and target['audio'] == before['audio']


def test_actual_time_and_track_dependencies_are_explicit_and_symmetric():
    data = dict(coverage='known', sentences=[dict(id=405,
        before_events=[dict(start=1., end=2.)], current_events=[dict(start=1.2, end=2.2)])], groups=[
        dict(kind='subtitle', changes=[dict(id='subtitle:405'), dict(id='subtitle:406')]),
        dict(kind='audio', changes=[dict(id='audio:405')]),
        dict(kind='settings', changes=[dict(id='assemble.subtitle_track')])])
    result = updates.decorate(data)
    rows = {row['id']: row for group in result['groups'] for row in group['changes']}
    assert rows['subtitle:405']['linked_ids'] == ['assemble.subtitle_track', 'audio:405']
    assert rows['audio:405']['linked_ids'] == ['subtitle:405']
    assert rows['assemble.subtitle_track']['linked_ids'] == ['subtitle:405', 'subtitle:406']
    assert rows['subtitle:406']['linked_ids'] == ['assemble.subtitle_track']
    assert all(row['selectable'] for row in rows.values())


@pytest.mark.parametrize('body', [None, [], {}, dict(base_version_id='base', change_ids=None),
    dict(base_version_id='base', change_ids='subtitle:405'),
    dict(base_version_id='base', change_ids=[None]), dict(base_version_id='base', change_ids=[True]),
    dict(base_version_id='base', change_ids=['']), dict(base_version_id=7, change_ids=['subtitle:405']),
    dict(base_version_id='base', change_ids=['subtitle:405'], target={'forged': True})])
def test_invalid_plan_contract_is_rejected_before_any_project_read(body, monkeypatch):
    monkeypatch.setattr(crud, 'get_project', lambda *_: pytest.fail('格式错误的选择不可读取项目'))
    with pytest.raises(updates.UpdateError) as error:
        updates.plan('unused', body)
    assert error.value.code == 'ASSEMBLY_UPDATE_INVALID'


@pytest.fixture
def update_project(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    save('project:' + uid, {'assemble.subtitle_track': 'source', 'content_review.enabled': False})
    project = crud.get_project(uid)
    overlay = _overlay([_item(id='c1', display_text='成片中的评论')])
    overlay['settings'].update(start=.5, duration=1.5, interval=2.)
    comment_studio._write(project, {'overlay': overlay})
    definition = dict(id='base', name='原文与评论基准', dubbing=False, burn_subtitles=True,
                      include_comments=True, overrides={})
    av.save(uid, dict(revision=av.catalog(uid)['revision'], versions=[definition]))
    deps = Dependencies(crud.get_project(uid))
    version = av.resolve(deps, definition, comment_plan=overlay)
    manifest = manifests.capture(deps, version)
    version.update(render_manifest=manifest, sentence_inputs=copy.deepcopy(manifest['inputs']))
    output = root / 'output' / 'base.mp4'
    output.parent.mkdir(exist_ok=True)
    shutil.copyfile(root / 'media' / 'video.mp4', output)
    crud.add_artifact(project.id, 'assemble', 'video', output.name, output, 'output/' + output.name)
    av.record_result(SimpleNamespace(project=project), version, output.name, av.fingerprint(deps, version))
    from dubforge.pipeline import assembly_partial, segment_replace
    monkeypatch.setattr(assembly_partial, '_inspect', lambda *_: dict(duration=6., fps_num=10,
        fps_den=1, frame_count=60, width=320, height=240, keyframe_frames=[0], streams=[], x264_options={}))
    monkeypatch.setattr(segment_replace, 'plan_windows', lambda _info, ranges: dict(expanded_ranges=ranges,
        encode_seconds=sum(row['end'] - row['start'] for row in ranges)))
    return SimpleNamespace(client=TestClient(app), uid=uid, root=root, definition=definition,
                           manifest=manifest, output=output)


def _edit_both(case):
    for ident in [1, 2]:
        response = edit(case.client, case.uid, {'source_text': f'Corrected English {ident}'}, line_id=ident)
        assert response.status_code == 200, response.text
    project = crud.get_project(case.uid)
    document = comment_studio._read(project)
    document['overlay']['items'][0]['display_text'] = '工作稿中的新评论'
    comment_studio._write(project, document)


def _create(case, ids=('subtitle:1',)):
    body = dict(base_version_id='base', change_ids=list(ids))
    plan = updates.plan(case.uid, body)
    created = updates.create(case.uid, {**body, 'fingerprint': plan['fingerprint']})
    av.save(case.uid, dict(revision=av.catalog(case.uid)['revision'], versions=[created['version']]))
    return plan, created


def test_real_update_api_plan_create_resolve_and_plan_keep_unselected_inputs(update_project):
    case = update_project
    _edit_both(case)
    base = f'/api/projects/{case.uid}/assembly-versions'
    response = case.client.post(base + '/update-plan', json=dict(base_version_id='base', change_ids=['subtitle:1']))
    assert response.status_code == 200, response.text
    planned = response.json()
    assert planned['selection_count'] == 1 and planned['remaining_count'] == 2
    assert planned['method'] == 'partial'
    assert 'target' not in planned and 'base_path' not in planned
    made = case.client.post(base + '/update-create', json=dict(base_version_id='base', change_ids=['subtitle:1'], fingerprint=planned['fingerprint']))
    assert made.status_code == 200, made.text
    child = made.json()['version']
    assert child['id'] == 'base'
    av.save(case.uid, dict(revision=av.catalog(case.uid)['revision'], versions=[child]))
    deps = Dependencies(crud.get_project(case.uid))
    resolved = av.resolve(deps, child)
    target = resolved['render_manifest']
    assert target['inputs']['lines']['1']['document']['source_text'] == 'Corrected English 1'
    assert target['inputs']['lines']['2'] == case.manifest['inputs']['lines']['2']
    assert target['comments'] == case.manifest['comments']
    assert target['inputs']['comment_inputs'] == case.manifest['inputs']['comment_inputs']
    request = av.plan(case.uid, dict(version_ids=[child['id']]))
    assert request['blockers'] == []
    assert request['versions'][0]['update']['plan_id'] == child['update']['plan_id']
    assert request['versions'][0]['render_manifest'] == target


def test_historical_audio_materialization_keeps_old_wav_after_live_slot_overwrite(update_project):
    case = update_project
    deps = Dependencies(crud.get_project(case.uid))
    before = _manifest()
    before['media'].pop('background')
    for event in before['audio']:
        path = case.root / event['media']['path']
        path.write_bytes(f'old sound {event["id"]}'.encode())
        event['media'] = manifests._ref(deps, path)
        before['inputs']['lines'][str(event['id'])]['audio'][0]['identity'] = event['media']['identity']
    frozen = manifests.materialize(deps, before)
    current = _changed(before)
    for event in before['audio']:
        manifests.media_path(deps, event['media']).write_bytes(b'newly overwritten live slot')
    target = updates.target_manifest(frozen, current, _entries(), {'subtitle:405'})
    assert target['audio'] == frozen['audio']
    for event in target['audio']:
        assert '.render-assets' in event['media']['path']
        assert manifests.media_path(deps, event['media']).read_bytes() == f'old sound {event["id"]}'.encode()
    assert manifests.materialize(deps, target)['audio'] == frozen['audio']


@pytest.mark.parametrize('ids,code', [([], 'ASSEMBLY_UPDATE_INVALID'),
    (['subtitle:1', 'subtitle:1'], 'ASSEMBLY_UPDATE_INVALID'),
    (['subtitle:unknown'], 'ASSEMBLY_UPDATE_RECHECK')])
def test_empty_duplicate_and_forged_change_selection_is_rejected(update_project, ids, code):
    case = update_project
    _edit_both(case)
    response = case.client.post(f'/api/projects/{case.uid}/assembly-versions/update-plan',
        json=dict(base_version_id='base', change_ids=ids))
    assert response.status_code == 400
    assert response.json()['code'] == code


def test_missing_linked_change_is_rejected_instead_of_silently_added(update_project, monkeypatch):
    case = update_project
    _edit_both(case)
    real_view = changes.view
    def linked(*args):
        value = real_view(*args)
        item = next(row for group in value['groups'] for row in group['changes'] if row['id'] == 'subtitle:1')
        item['linked_ids'] = ['comment:c1']
        return value
    monkeypatch.setattr(changes, 'view', linked)
    with pytest.raises(DubForgeError) as error:
        updates.plan(case.uid, dict(base_version_id='base', change_ids=['subtitle:1']))
    assert error.value.code == 'ASSEMBLY_UPDATE_LINKED'
    assert error.value.detail['required_ids'] == ['comment:c1']


def test_stale_preflight_cannot_create_plan_after_working_text_changes(update_project):
    case = update_project
    _edit_both(case)
    request = dict(base_version_id='base', change_ids=['subtitle:1'])
    before = updates.plan(case.uid, request)
    assert edit(case.client, case.uid, {'source_text': 'Changed after checking'}, line_id=2).status_code == 200
    with pytest.raises(DubForgeError) as error:
        updates.create(case.uid, {**request, 'fingerprint': before['fingerprint']})
    assert error.value.code == 'ASSEMBLY_UPDATE_RECHECK'
    assert not (case.root / 'output/.update-plans').exists()


def test_global_setting_plan_freezes_one_choice_and_leaves_other_settings_pending(update_project):
    case = update_project
    save('project:' + case.uid, {'assemble.subtitle_color': '#00FF00', 'assemble.subtitle_size': 56})
    request = dict(base_version_id='base', change_ids=['assemble.subtitle_color'])
    planned = updates.plan(case.uid, request)
    assert planned['method'] == 'full'
    assert planned['selection_count'] == 1 and planned['remaining_count'] >= 1
    target = planned['target']
    assert target['version']['values']['assemble.subtitle_color'] == '#00FF00'
    assert target['version']['values']['assemble.subtitle_size'] == case.manifest['version']['values']['assemble.subtitle_size']
    assert target['subtitles'] == case.manifest['subtitles']
    assert target['comments'] == case.manifest['comments']


def test_saved_update_plan_is_immutable_and_rejects_modified_manifest(update_project):
    case = update_project
    _edit_both(case)
    _, made = _create(case)
    child = made['version']
    deps = Dependencies(crud.get_project(case.uid))
    original = updates.load_plan(deps, child)
    assert edit(case.client, case.uid, {'source_text': 'Later unselected text'}, line_id=2).status_code == 200
    assert updates.load_plan(Dependencies(crud.get_project(case.uid)), child) == original
    path = case.root / 'output/.update-plans' / (child['update']['plan_id'] + '.json')
    damaged = json.loads(path.read_text(encoding='utf-8'))
    damaged['target']['inputs']['lines']['2']['document']['source_text'] = 'forged replacement'
    path.write_text(json.dumps(damaged), encoding='utf-8')
    with pytest.raises(DubForgeError) as error:
        updates.load_plan(deps, child)
    assert error.value.code == 'ASSEMBLY_UPDATE_PLAN_MISSING'


def test_successful_automatic_version_run_records_only_frozen_target_and_remaining_diff(update_project, monkeypatch):
    case = update_project
    _edit_both(case)
    _, made = _create(case)
    child = made['version']
    seen = []
    def render(ctx, _source, out, _total):
        seen.append(copy.deepcopy(ctx.version['render_manifest']))
        shutil.copyfile(case.output, out)
    monkeypatch.setattr(updates, 'render', render)
    request = dict(version_ids=[child['id']])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    result = runner.start_versions(case.uid, request)
    assert result['status'] == 'started'
    try:
        wait(case.uid)
    except AssertionError as error:
        job = crud.latest_job(crud.get_project(case.uid).id, 'assemble')
        raise AssertionError(job.error_detail) from error
    assert len(seen) == 1
    saved = json.loads(av.results(case.uid)[child['id']].snapshot)
    assert saved['sentence_inputs'] == seen[0]['inputs']
    assert saved['render_manifest'] == seen[0]
    assert saved['sentence_inputs']['lines']['2'] == case.manifest['inputs']['lines']['2']
    assert saved['render_manifest']['comments'] == case.manifest['comments']
    pending = changes.view(case.uid, child['id'])
    ids = {row['id'] for group in pending['groups'] for row in group['changes']}
    assert ids == {'subtitle:2', 'comment:c1'}
    assert pending['needs_update'] and pending['coverage'] == 'known'


def test_same_version_updates_first_sentence_then_second_without_adding_definitions(update_project, monkeypatch):
    case = update_project
    _edit_both(case)
    seen = []
    def render(ctx, _source, out, _total):
        seen.append(copy.deepcopy(ctx.version['render_manifest']))
        plan = updates.load_plan(ctx.dependencies(), ctx.version, verify=True)
        shutil.copyfile(plan['base_path'], out)
    monkeypatch.setattr(updates, 'render', render)
    previous_plan = None
    for ident, expected in [(1, {'subtitle:2', 'comment:c1'}), (2, {'comment:c1'})]:
        _, made = _create(case, [f'subtitle:{ident}'])
        child = made['version']
        assert child['id'] == 'base'
        assert len(av.catalog(case.uid)['versions']) == 1
        assert child['update']['plan_id'] != previous_plan
        previous_plan = child['update']['plan_id']
        request = dict(version_ids=['base'])
        request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
        assert runner.start_versions(case.uid, request)['status'] == 'started'
        wait(case.uid)
        ids = {row['id'] for group in changes.view(case.uid, 'base')['groups'] for row in group['changes']}
        assert ids == expected
    assert len(seen) == 2
    assert seen[1]['inputs']['lines']['1'] == seen[0]['inputs']['lines']['1']
    assert seen[1]['inputs']['lines']['2']['document']['source_text'] == 'Corrected English 2'
    assert seen[1]['comments'] == case.manifest['comments']


def test_failed_same_version_update_keeps_previous_file_and_snapshot_and_can_retry(update_project, monkeypatch):
    from conftest import wait_status
    case = update_project
    _edit_both(case)
    _, made = _create(case)
    child = made['version']
    original = av.results(case.uid)['base']
    old_bytes = case.output.read_bytes()
    def failed(_ctx, _source, out, _total):
        out.write_bytes(b'incomplete update')
        raise DubForgeError('模拟所选更新失败', code='UPDATE_RENDER_FAILED')
    monkeypatch.setattr(updates, 'render', failed)
    request = dict(version_ids=['base'])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    wait_status(case.uid, 'assemble', {'failed'})
    with runner._lock:
        futures = list(runner._futures)
    for future in futures:
        future.result(timeout=30)
    assert case.output.read_bytes() == old_bytes
    assert av.results(case.uid)['base'].snapshot == original.snapshot
    assert av.results(case.uid)['base'].artifact_hash == original.artifact_hash
    assert av.catalog(case.uid)['versions'][0]['update'] == child['update']
    assert not list((case.root / 'output').glob('.tmp_pending_*'))
    def succeeds(ctx, _source, out, _total):
        plan = updates.load_plan(ctx.dependencies(), ctx.version, verify=True)
        shutil.copyfile(plan['base_path'], out)
    monkeypatch.setattr(updates, 'render', succeeds)
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    wait(case.uid)
    assert len(av.catalog(case.uid)['versions']) == 1
    ids = {row['id'] for group in changes.view(case.uid, 'base')['groups'] for row in group['changes']}
    assert ids == {'subtitle:2', 'comment:c1'}


def test_review_enabled_full_render_uses_historical_unselected_subtitles_and_comments(update_project, monkeypatch):
    """真实6秒FFmpeg完整生成；审查开启也不能绕过固定清单重新读工作稿。"""
    case = update_project
    save('project:' + case.uid, {'content_review.enabled': True})
    project = crud.get_project(case.uid)
    deps = Dependencies(project)
    overlay = comment_studio._read(project)['overlay']
    old = av.resolve(deps, case.definition, comment_plan=overlay)
    baseline = manifests.capture(deps, old)
    old.update(render_manifest=baseline, sentence_inputs=copy.deepcopy(baseline['inputs']))
    av.record_result(SimpleNamespace(project=project), old, case.output.name, av.fingerprint(deps, old))
    _edit_both(case)
    save('project:' + case.uid, {'assemble.subtitle_color': '#00FF00'})
    planned, made = _create(case, ['assemble.subtitle_color'])
    assert planned['method'] == 'full'
    from dubforge.pipeline import subtitle_style, comment_overlay
    text_calls, comment_calls = [], []
    real_subtitles, real_comments = subtitle_style.prepare_subtitle_lines, comment_overlay.ass_document
    def subtitles(lines, *args, **kwargs):
        text_calls.append([row.text for row in lines])
        return real_subtitles(lines, *args, **kwargs)
    def comments(plan, *args, **kwargs):
        if plan.get('internal_schema') == manifests.COMMENT_EVENTS:
            comment_calls.append(copy.deepcopy(plan['events']))
        return real_comments(plan, *args, **kwargs)
    monkeypatch.setattr(subtitle_style, 'prepare_subtitle_lines', subtitles)
    monkeypatch.setattr(comment_overlay, 'ass_document', comments)
    child = made['version']
    request = dict(version_ids=[child['id']])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    try:
        wait(case.uid)
    except AssertionError as error:
        job = crud.latest_job(project.id, 'assemble')
        raise AssertionError(job.error_detail) from error
    assert text_calls and text_calls[-1] == [row['text'] for row in baseline['subtitles']]
    assert comment_calls and comment_calls[-1] == baseline['comments']
    stored = av.results(case.uid)[child['id']]
    saved = json.loads(stored.snapshot)
    assert saved['sentence_inputs']['lines'] == baseline['inputs']['lines']
    assert saved['render_manifest']['comments'] == baseline['comments']
    assert saved['values']['assemble.subtitle_color'] == '#00FF00'
    artifact = av.result_artifact(Dependencies(crud.get_project(case.uid)), child)
    assert artifact and artifact.size > 1000
    pending = {row['id'] for group in changes.view(case.uid, child['id'])['groups'] for row in group['changes']}
    assert pending == {'subtitle:1', 'subtitle:2', 'comment:c1'}


def _audio_packet_hashes(path):
    """比较压缩包内容，不把MP4时间戳或元数据差异当成音轨被重编码。"""
    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_packets',
        '-show_entries', 'packet=data_hash', '-show_data_hash', 'sha256', '-of', 'json', str(path)],
        check=True, capture_output=True, text=True)
    return [packet['data_hash'] for packet in json.loads(probe.stdout).get('packets', [])]


def test_visual_full_update_copies_mastered_baseline_audio_without_remixing(update_project, monkeypatch):
    """实际母带先改变原片声音；全片改色仍逐包保留母带而非复用原片音轨。"""
    from dubforge.pipeline import audio_master, audio_quality, step_assemble
    case = update_project
    save('project:' + case.uid, {'assemble.final_loudness_mode': 'uniform',
        'assemble.final_loudness_lufs': -14., 'assemble.final_true_peak_db': -1.5})
    baseline_request = dict(version_ids=['base'], force_rebuild=True,
        overlay_revision=comment_studio.view(case.uid)['overlay_revision'])
    baseline_request['fingerprint'] = av.plan(case.uid, baseline_request)['fingerprint']
    assert runner.start_versions(case.uid, baseline_request)['status'] == 'started'
    wait(case.uid)
    baseline_record = av.results(case.uid)['base']
    saved_before = json.loads(baseline_record.snapshot)
    original = _audio_packet_hashes(case.root / 'media/video.mp4')
    mastered = _audio_packet_hashes(case.output)
    assert mastered and original and mastered != original, '基片应真实应用母带，不能恰好复制源片音轨'
    old_digest = hashlib.sha256('\n'.join(mastered).encode()).hexdigest()
    _edit_both(case)
    save('project:' + case.uid, {'assemble.subtitle_color': '#00FF00'})
    planned, made = _create(case, ['assemble.subtitle_color'])
    assert planned['method'] == 'full' and planned['replace_audio'] is False
    assert made['replace_audio'] is False
    def no_remix(*_args, **_kwargs):
        pytest.fail('仅完整重绘画面必须保留基片音轨，不能重新测量混音或处理声音')
    monkeypatch.setattr(audio_master, 'prepare_master', no_remix)
    monkeypatch.setattr(audio_quality, 'measure_mix', no_remix)
    monkeypatch.setattr(audio_quality, 'final_gain', no_remix)
    monkeypatch.setattr(audio_quality, 'normalize_voice', no_remix)
    monkeypatch.setattr(manifests, 'mix_voice', no_remix)
    monkeypatch.setattr(step_assemble, '_mix_timeline', no_remix)
    request = dict(version_ids=['base'])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    try:
        wait(case.uid)
    except AssertionError as error:
        job = crud.latest_job(crud.get_project(case.uid).id, 'assemble')
        raise AssertionError(job.error_detail) from error
    updated_audio = _audio_packet_hashes(case.output)
    assert updated_audio == mastered
    assert hashlib.sha256('\n'.join(updated_audio).encode()).hexdigest() == old_digest
    saved_after = json.loads(av.results(case.uid)['base'].snapshot)
    assert saved_after['sentence_inputs']['lines'] == saved_before['sentence_inputs']['lines']
    assert saved_after['render_manifest']['comments'] == saved_before['render_manifest']['comments']
    assert saved_after['values']['assemble.subtitle_color'] == '#00FF00'
    report = audio_quality.read_report(case.output)
    assert report['measurement_provenance']['kind'] == 'baseline_audio_reused'
    assert report['measurement_provenance']['base_sha256'] == baseline_record.artifact_hash
    pending = {row['id'] for group in changes.view(case.uid, 'base')['groups'] for row in group['changes']}
    assert pending == {'subtitle:1', 'subtitle:2', 'comment:c1'}


def test_visual_full_update_preserves_absent_audio_without_adding_silent_track(update_project):
    case = update_project
    source = case.root / 'media/video.mp4'
    pending = source.with_name('silent-source.tmp.mp4')
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(source),
        '-map', '0:v:0', '-c:v', 'copy', '-an', str(pending)], check=True)
    pending.replace(source)
    project = crud.get_project(case.uid)
    crud.add_artifact(project.id, 'download', 'video', 'video.mp4', source, 'media/video.mp4')
    baseline_request = dict(version_ids=['base'], force_rebuild=True,
        overlay_revision=comment_studio.view(case.uid)['overlay_revision'])
    baseline_request['fingerprint'] = av.plan(case.uid, baseline_request)['fingerprint']
    assert runner.start_versions(case.uid, baseline_request)['status'] == 'started'
    wait(case.uid)
    assert _audio_packet_hashes(case.output) == []
    _edit_both(case)
    save('project:' + case.uid, {'assemble.subtitle_color': '#00FF00'})
    planned, _ = _create(case, ['assemble.subtitle_color'])
    assert planned['method'] == 'full' and planned['replace_audio'] is False
    request = dict(version_ids=['base'])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    wait(case.uid)
    assert _audio_packet_hashes(case.output) == []
    from dubforge.pipeline.audio_quality import read_report
    report = read_report(case.output)
    assert report['has_audio'] is False
    assert report['measurement_provenance']['kind'] == 'baseline_audio_reused'
    pending_ids = {row['id'] for group in changes.view(case.uid, 'base')['groups'] for row in group['changes']}
    assert pending_ids == {'subtitle:1', 'subtitle:2', 'comment:c1'}


def test_new_project_cuts_after_queue_do_not_enter_fixed_update_version(update_project, monkeypatch):
    case = update_project
    _edit_both(case)
    _, made = _create(case)
    child = made['version']
    request = dict(version_ids=[child['id']])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    captured = {}
    monkeypatch.setattr(runner, '_start_step', lambda uid, key, **kw: captured.update(kw) or {'status': 'queued'})
    assert runner.start_versions(case.uid, request)['status'] == 'queued'
    before = copy.deepcopy(captured['task_data']['version_specs'][0])
    cut_state = output_cuts.view(case.uid)
    output_cuts.save(case.uid, dict(revision=cut_state['revision'], source_fingerprint=cut_state['source_fingerprint'],
        enabled=True, ranges=[dict(id='new-cut', start=4., end=5., enabled=True, reason='排队后新增')]))
    after = av.resolve(Dependencies(crud.get_project(case.uid)), child)
    assert not before.get('output_cuts') and not after.get('output_cuts')
    assert before['render_manifest'] == after['render_manifest']
    assert captured['task_data']['version_specs'][0] == before
    assert 'output_cuts' in {row['id'] for group in changes.view(case.uid, 'base')['groups'] for row in group['changes']}


def test_workflows_produce_executes_only_selected_update_without_tts_comments_or_publication(update_project, monkeypatch):
    """真正穿过自动流程协调器、Runner和FFmpeg；1/2句对应405/406的同一选择合同。"""
    from dubforge import workflows, comments_auto
    from dubforge.backends import llm
    from dubforge.publishing import transport, service
    case = update_project
    def forbidden(*_args, **_kwargs):
        pytest.fail('纯成片所选更新不能执行LLM、评论采集或平台发布')
    monkeypatch.setattr(llm, 'chat', forbidden)
    monkeypatch.setattr(llm, 'chat_validated', forbidden)
    monkeypatch.setattr(llm, 'chat_json', forbidden)
    monkeypatch.setattr(comments_auto, 'prepare_overlay', forbidden)
    monkeypatch.setattr(transport, 'call', forbidden)
    monkeypatch.setattr(service, 'enqueue', forbidden)
    monkeypatch.setattr(workflows, '_comments', forbidden)
    # 先真实生成一个标准编码基片，避免用复制源视频冒充已经烧录的成片。
    before = dict(version_ids=['base'], force_rebuild=True,
                  overlay_revision=comment_studio.view(case.uid)['overlay_revision'])
    before['fingerprint'] = av.plan(case.uid, before)['fingerprint']
    assert runner.start_versions(case.uid, before)['status'] == 'started'
    wait(case.uid)
    case.manifest = json.loads(av.results(case.uid)['base'].snapshot)['render_manifest']
    monkeypatch.setattr(assembly_partial, '_inspect', _REAL_PARTIAL_INSPECT)
    monkeypatch.setattr(segment_replace, 'plan_windows', _REAL_SEGMENT_WINDOWS)
    _edit_both(case)
    _, made = _create(case, ['subtitle:1'])
    assert made['method'] == 'partial' and made['version']['id'] == 'base'
    save('project:' + case.uid, {'run.mode': 'produce', 'publish.version_ids': ['base'],
        'publish.ai_copy': False, 'comments.mode': 'auto'})
    intent = dict(mode='produce', production_steps=['assemble'])
    candidate = workflows.preview(case.uid, intent)
    assert candidate['blockers'] == [], candidate['blockers']
    assert candidate['steps'] == ['assemble']
    assert candidate['requested_steps'] == ['assemble']
    assert candidate['added_steps'] == []
    assert candidate['version_specs'][0]['update'] == made['version']['update']
    assert candidate['dubbing']['needed'] is False
    request = dict(recipe=candidate['recipe'], fingerprint=candidate['fingerprint'],
                   confirmed=True, request_id=str(uuid.uuid4()))
    futures = []
    submit = workflows._pool.submit
    def tracked(*args, **kwargs):
        future = submit(*args, **kwargs)
        futures.append(future)
        return future
    monkeypatch.setattr(workflows._pool, 'submit', tracked)
    row = workflows.start(case.uid, request)
    try:
        deadline = time.monotonic() + 40
        while row['id'] in workflows._running and time.monotonic() < deadline:
            time.sleep(.025)
        assert row['id'] not in workflows._running, workflows.get(row['id'])
        result = workflows.get(row['id'])
        assert result['status'] == 'completed', result
        assert result['publication_id'] == ''
        assert result['cursor'] == 1
        assert result['plan']['steps'] == ['assemble']
        assert len(av.catalog(case.uid)['versions']) == 1
        saved = json.loads(av.results(case.uid)['base'].snapshot)
        assert saved['sentence_inputs']['lines']['1']['document']['source_text'] == 'Corrected English 1'
        assert saved['sentence_inputs']['lines']['2'] == case.manifest['inputs']['lines']['2']
        assert saved['render_manifest']['comments'] == case.manifest['comments']
        pending = {item['id'] for group in changes.view(case.uid, 'base')['groups'] for item in group['changes']}
        assert pending == {'subtitle:2', 'comment:c1'}
        project = crud.get_project(case.uid)
        assert crud.latest_job(project.id, 'tts') is None
        assert crud.latest_job(project.id, 'translate') is None
        assert av.is_current(Dependencies(project), av.resolve(Dependencies(project), made['version']), verify_hash=True)
    finally:
        if row['id'] in workflows._running:
            workflows._running[row['id']].set()
        for future in futures:
            future.result(timeout=30)
