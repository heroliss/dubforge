"""三个独立维度贯穿依赖、批任务与真实试片；不调用外部 TTS/LLM。"""
import itertools

import pytest
from fastapi.testclient import TestClient
from app.main import app
from conftest import wait_status
from dubforge import comment_studio as cs
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import step_assemble as sa
from dubforge.pipeline.comment_overlay import ass_document
from dubforge.pipeline.assembly_outputs import DIMENSIONS, OUTPUT_VARIANTS, dimensions, resolve_outputs
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import runner
from test_assemble import _prepare_outputs_project
from test_comment_studio import seed, plan_body


@pytest.mark.parametrize('flags', list(itertools.product((False, True), repeat=3)))
def test_all_eight_dimension_combinations(flags, make_project):
    output = dict(zip(DIMENSIONS, flags))
    if not any(flags):
        with pytest.raises(DubForgeError, match='无需合成'):
            resolve_outputs(outputs=[output])
        return
    variants = resolve_outputs(outputs=[output, output])
    assert len(variants) == 1 and dimensions(variants[0]) == output
    inputs = Dependencies(crud.get_project(make_project()), assemble_variants=variants).inputs('assemble')
    assert any(step == 'subtitles' for step, _, _ in inputs) == output['burn_subtitles']
    assert any(step == 'tts' for step, _, _ in inputs) == output['dubbing']


@pytest.mark.parametrize('outputs', [[], {}, [None], [{}], [{'dubbing': 'false', 'burn_subtitles': False, 'include_comments': True}],
    [{'dubbing': False, 'burn_subtitles': False, 'include_comments': True, 'extra': True}]])
def test_invalid_dimensions_never_queue(outputs, make_project):
    uid = make_project(); api = TestClient(app)
    for endpoint in ('output_plan', 'outputs'):
        response = api.post(f'/api/projects/{uid}/steps/assemble/{endpoint}', json={'outputs': outputs})
        assert response.status_code == 400 and response.json()['code'] == 'ASSEMBLE_VARIANTS'
    assert crud.latest_job(crud.get_project(uid).id, 'assemble') is None


def test_new_mixed_payload_freezes_only_explicit_combinations(make_project, monkeypatch):
    uid = make_project(); seed(uid); state = cs.save(uid, plan_body(uid))
    outputs = [dimensions('dub'), dimensions('src_comments')]
    tasks = []
    monkeypatch.setattr(runner, '_start_step', lambda *a, **k: tasks.append(k['task_data']) or {'status': 'started'})
    runner.start_assemble_outputs(uid, outputs=outputs, overlay_revision=state['overlay_revision'])
    outputs[0]['include_comments'] = True
    assert tasks[0]['variants'] == ['dub', 'src_comments']
    assert tasks[0]['comment_overlay'] == state['overlay']
    with pytest.raises(DubForgeError):
        runner.start_assemble_outputs(uid, ['dub'], outputs=outputs)
    with pytest.raises(DubForgeError):
        runner.start_assemble_outputs(uid, outputs=outputs, overlay_revision='stale')


def test_seven_outputs_and_original_previews_use_exact_dimensions(make_project, monkeypatch):
    uid, root = _prepare_outputs_project(make_project, monkeypatch); seed(uid)
    state = cs.save(uid, plan_body(uid, color='#00FF00'))
    calls = []
    original_mux, dubbed_mux = sa._mux_original_sub, sa._mux
    def original(ctx, video, out, *args, **kwargs):
        calls.append((out.name, False, kwargs['burn'], bool(kwargs['comment_plan'])))
        return original_mux(ctx, video, out, *args, **kwargs)
    def dubbed(ctx, pdir, video, dub, out, *args, **kwargs):
        calls.append((out.name, True, kwargs['burn'], bool(kwargs['comment_plan'])))
        return dubbed_mux(ctx, pdir, video, dub, out, *args, **kwargs)
    monkeypatch.setattr(sa, '_mux_original_sub', original); monkeypatch.setattr(sa, '_mux', dubbed)
    # 没有任何配音产物也能独立试原声 + 字幕 / 评论三种组合。
    for key in ('src_comments', 'src_hardsub', 'src_hardsub_comments'):
        runner.start_assemble_preview(uid, 1, 3, output=dimensions(key), overlay_revision=state['overlay_revision'])
        wait_status(uid, 'assemble', {'done'}, timeout=30)
        out = next((root / 'output').glob(f'preview_{key}_*.mp4'))
        assert 1.8 <= sa._ffprobe_duration(out) <= 2.2
        assert calls[-1][1:] == tuple(dimensions(key).values())
    assert not (root / 'audio/dub_full.wav').exists()
    runner.start_step(uid, 'tts'); wait_status(uid, 'tts', {'done'})
    runner.start_assemble_outputs(uid, outputs=[dimensions(key) for key in OUTPUT_VARIANTS], overlay_revision=state['overlay_revision'])
    wait_status(uid, 'assemble', {'done'}, timeout=60)
    assert len(list((root / 'output').glob('final_*.mp4'))) == 7
    for key, call in zip(OUTPUT_VARIANTS, calls[-7:]):
        assert call[0].startswith(f'.tmp_pending_final_{key}_')
        assert (root / 'output' / call[0].removeprefix('.tmp_pending_')).is_file()
        assert not (root / 'output' / call[0]).exists()
        assert call[1:] == tuple(dimensions(key).values())
    assert Dependencies(crud.get_project(uid)).fresh('assemble')  # 本批包含不叠加评论的流程默认版。
    for key in ('dub', 'dub_hardsub', 'dub_comments', 'dub_hardsub_comments'):
        runner.start_assemble_preview(uid, 1, 3, output=dimensions(key), overlay_revision=state['overlay_revision'])
        wait_status(uid, 'assemble', {'done'}, timeout=30)
        out = next((root / 'output').glob(f'preview_{key}_*.mp4'))
        assert 1.8 <= sa._ffprobe_duration(out) <= 2.2
        assert calls[-1][1:] == tuple(dimensions(key).values())
    assert not list(root.rglob('.comments_*.ass')) and not list(root.rglob('.preview_*.srt'))


def test_comment_text_color_is_validated_and_shared_by_preview_and_output(make_project):
    uid = make_project(); seed(uid)
    body = plan_body(uid, color='#12aB34')
    state = cs.save(uid, body)
    assert '&H0034AB12' in ass_document(state['overlay'], (1920, 1080))
    for bad in ('red', '#FFFFFF00', None, 123):
        body['settings']['color'] = bad
        with pytest.raises(DubForgeError):
            cs.draft(uid, body)
