"""选择性成片更新的边界合同；不能在全局设置或旧流程中绕过勾选。"""
import copy
import json
import shutil

import pytest

from dubforge.domain import crud
from dubforge import comment_studio
from dubforge.pipeline import assembly_partial, assembly_updates as updates, assembly_versions as av
from dubforge.pipeline import output_cuts
from dubforge.pipeline.assembly_models import AssemblyResult
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from test_assembly_updates import update_project, _create, _edit_both
from test_assembly_versions import wait
from test_configuration import save


@pytest.mark.parametrize('selected,required', [
    ('output_cuts', 'output_cuts_renderer'), ('output_cuts_renderer', 'output_cuts'),
])
def test_cut_timeline_and_renderer_revision_cannot_silently_apply_each_other(update_project, selected, required):
    case = update_project
    state = output_cuts.view(case.uid)
    output_cuts.save(case.uid, dict(revision=state['revision'], source_fingerprint=state['source_fingerprint'],
        enabled=True, ranges=[dict(id='new-cut', start=2., end=2.5, reason='新的删除片段', enabled=True)]))
    current = output_cuts.snapshot(Dependencies(crud.get_project(case.uid)))
    historical = {**copy.deepcopy(current), 'ranges': [dict(start=.5, end=1.)]}
    with crud.session() as session:
        record = session.get(AssemblyResult, case.uid + ':base')
        saved = json.loads(record.snapshot)
        saved['output_cuts'] = historical
        saved['sentence_inputs']['global_inputs'].update(output_cuts=historical, output_cuts_renderer=1)
        saved['render_manifest']['version']['output_cuts'] = historical
        saved['render_manifest']['inputs'] = copy.deepcopy(saved['sentence_inputs'])
        record.snapshot = json.dumps(saved, ensure_ascii=False)
        session.add(record)
        session.commit()
    response = case.client.post(f'/api/projects/{case.uid}/assembly-versions/update-plan',
        json=dict(base_version_id='base', change_ids=[selected]))
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'ASSEMBLY_UPDATE_LINKED'
    assert required in response.json()['detail']['required_ids']
    assert not (case.root / 'output/.update-plans').exists()


def test_mixed_selected_and_retained_subtitle_widths_are_validated_before_render(update_project):
    case = update_project
    # 原成片80/90与当前92/96各自有效；只选92会与保留的90冲突。
    assert case.manifest['version']['values']['assemble.subtitle_shrink_width_percent'] == 80
    assert case.manifest['version']['values']['assemble.subtitle_wrap_width_percent'] == 90
    save('project:' + case.uid, {'assemble.subtitle_shrink_width_percent': 92,
                               'assemble.subtitle_wrap_width_percent': 96})
    endpoint = f'/api/projects/{case.uid}/assembly-versions/update-plan'
    response = case.client.post(endpoint, json=dict(base_version_id='base',
        change_ids=['assemble.subtitle_shrink_width_percent']))
    assert response.status_code == 400, response.text
    assert response.json()['code'] == 'ASSEMBLY_UPDATE_SETTINGS_CONFLICT'
    assert '保留的成片设置' in response.json()['message']
    assert not (case.root / 'output/.update-plans').exists()
    together = case.client.post(endpoint, json=dict(base_version_id='base',
        change_ids=['assemble.subtitle_shrink_width_percent', 'assemble.subtitle_wrap_width_percent']))
    assert together.status_code == 200, together.text
    assert together.json()['method'] == 'full'


def test_legacy_partial_success_does_not_attach_unapplied_whole_working_manifest(update_project, monkeypatch):
    case = update_project
    _edit_both(case)
    body = dict(base_version_id='base', line_ids=[1], replace_audio=False)
    planned = assembly_partial.plan(case.uid, body)
    child = assembly_partial.create(case.uid, {**body, 'fingerprint': planned['fingerprint']})['version']
    av.save(case.uid, dict(revision=av.catalog(case.uid)['revision'], versions=[case.definition, child]))
    seen = []
    def encode(ctx, _source, out, _total):
        plan = assembly_partial.load_plan(ctx.dependencies(), ctx.version)
        seen.append(copy.deepcopy(plan['ranges']))
        shutil.copyfile(plan['base_path'], out)
    monkeypatch.setattr('dubforge.pipeline.partial_render.run', encode)
    overlay = comment_studio.effective_overlay(case.uid, comment_studio._read(crud.get_project(case.uid)).get('overlay'))
    request = dict(version_ids=[child['id']], overlay_revision=comment_studio._hash(overlay))
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    wait(case.uid)
    saved = json.loads(av.results(case.uid)[child['id']].snapshot)
    assert seen and 'render_manifest' not in saved
    assert saved['sentence_inputs']['lines']['1']['document']['source_text'] == 'Corrected English 1'
    assert saved['sentence_inputs']['lines']['2']['subtitle'] == case.manifest['inputs']['lines']['2']['subtitle']
    assert saved['sentence_inputs']['lines']['2']['document'] == case.manifest['inputs']['lines']['2']['document']


def test_pure_update_uses_frozen_source_even_when_project_source_becomes_unavailable(update_project, monkeypatch):
    case = update_project
    _edit_both(case)
    _, made = _create(case)
    child = made['version']
    frozen = updates.load_plan(Dependencies(crud.get_project(case.uid)), child)
    project = crud.get_project(case.uid)
    project.source_kind, project.source_url = 'local', str(case.root / 'later-missing-source.mp4')
    crud.save_project(project)
    seen = []
    def encode(ctx, source, out, total):
        seen.append((str(source.resolve()), total))
        plan = updates.load_plan(ctx.dependencies(), ctx.version, verify=True)
        shutil.copyfile(plan['base_path'], out)
    monkeypatch.setattr(updates, 'render', encode)
    monkeypatch.setattr('dubforge.pipeline.version_render.resolve_video',
                        lambda *_: pytest.fail('冻结更新不能解析之后修改的当前项目来源'))
    request = dict(version_ids=[child['id']])
    request['fingerprint'] = av.plan(case.uid, request)['fingerprint']
    assert runner.start_versions(case.uid, request)['status'] == 'started'
    try:
        wait(case.uid)
    except AssertionError as error:
        job = crud.latest_job(project.id, 'assemble')
        raise AssertionError(job.error_detail) from error
    assert seen == [(str((case.root / frozen['target']['media']['source']['path']).resolve()), 6.)]
    saved = json.loads(av.results(case.uid)[child['id']].snapshot)
    assert saved['render_manifest']['media']['source'] == frozen['target']['media']['source']
    assert saved['sentence_inputs']['lines']['2'] == case.manifest['inputs']['lines']['2']
