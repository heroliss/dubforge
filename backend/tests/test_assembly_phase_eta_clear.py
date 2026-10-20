"""阶段编码结束后清除 ETA，实际进度、整步估时和执行控制保持原样。"""
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session

from dubforge import release_batches
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.execution_progress import report_stage
from dubforge.pipeline import audio_quality, assembly_timeline
from dubforge.pipeline.version_render import VersionContext
from dubforge.progress import RateProgress
from dubforge.release_batch_models import ReleaseBatch
from dubforge.runner import StepCancelled, StepContext, runner
from dubforge.workflow_models import WorkflowRun


@pytest.fixture
def context(make_project, monkeypatch):
    project = crud.get_project(make_project(title='阶段计时隔离夹具'))
    root = crud.project_dir(project.uid)
    logs = root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    job = crud.add_job(project.id, 'assemble', 'logs/assemble.log')
    ctx = StepContext(project, 'assemble', root, logs / 'assemble.log', threading.Event(),
        job_id=job.id, defer_snapshot=True)
    ctx._job = job
    # 所有实际配置/数据来自隔离环境；测试中不调用真实进程、媒体或外部服务。
    ctx._config_snapshot = {'assemble': {'final_loudness_mode': 'off'}}
    clock = [0.0]
    ctx.active_seconds = lambda: clock[0]
    monkeypatch.setattr(ctx, 'run_subprocess', lambda *_a, **_kw: pytest.fail('禁止启动实际进程'))
    step = crud.get_step(project.id, 'assemble')
    step.status = 'running'
    crud.save_step(step)
    monkeypatch.setitem(runner._ctxs, (project.id, 'assemble'), ctx)
    return ctx, clock


def saved(ctx):
    return crud.get_step(ctx.project.id, ctx.step_key)


def prime(ctx, *, scope='phase', eta=0.1, pct=49.5):
    ctx.set_eta_provider(lambda: eta, scope=scope)
    ctx.report_progress(pct)
    report_stage(ctx, 'encoding', '编码成片', completed=9.99, total=10, unit='成片秒')


def measurement():
    return dict(schema=1, integrated_lufs=-16., true_peak_db=-2., loudness_range_lu=3., issues=[])


def make_batch(ctx):
    with Session(get_engine()) as session:
        run = WorkflowRun(project_uid=ctx.project.uid, request_id=uuid.uuid4().hex,
            status='running', cursor=0, phase='合成成片', plan=json.dumps({'steps': ['assemble']}))
        session.add(run)
        session.commit()
        session.refresh(run)
        batch = ReleaseBatch(request_id=uuid.uuid4().hex, status='running', cursor=0,
            items=json.dumps([dict(project_uid=ctx.project.uid, title=ctx.project.title,
                status='running', workflow_id=run.id)]))
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch.id


def test_phase_clear_updates_real_step_live_runner_and_release_batch(context, monkeypatch):
    ctx, _clock = context
    prime(ctx)
    batch_id = make_batch(ctx)
    before = release_batches.get(batch_id)['current_item']
    assert before['percentage'] == 49.5 and before['eta_seconds'] == 0.1
    ctx.clear_phase_eta()
    assert saved(ctx).progress == 49.5 and saved(ctx).eta_seconds == 0.
    live = runner.progress_eta(ctx.project.id, 'assemble')
    assert live['eta_seconds'] == 0. and live['eta_scope'] == 'phase'
    # 本测试仍在编码标签下核对：不是靠 indeterminate 隐藏旧值。
    after = release_batches.get(batch_id)['current_item']
    assert after['percentage'] == 49.5 and not after['indeterminate']
    assert after['eta_seconds'] is None
    assert before['execution']['phase'] == after['execution']['phase'] == 'encoding'
    # 实时 ctx 消失时也必须回退到已清零的持久值，而不是旧 0.1 秒。
    monkeypatch.delitem(runner._ctxs, (ctx.project.id, 'assemble'))
    fallback = release_batches.get(batch_id)['current_item']
    assert fallback['percentage'] == 49.5 and fallback['eta_seconds'] is None
    assert not fallback['indeterminate']


@pytest.mark.parametrize('provider', [None, lambda: None, lambda: 0.])
def test_already_unbound_zero_or_none_phase_still_removes_persisted_eta(context, provider):
    ctx, _clock = context
    prime(ctx, eta=18.)
    ctx.set_eta_provider(provider, scope='phase')
    ctx.clear_phase_eta()
    assert saved(ctx).progress == 49.5 and saved(ctx).eta_seconds == 0.
    assert ctx._eta_provider is None and ctx._eta_scope == 'phase'


def test_repeated_clear_is_idempotent_without_extra_step_writes(context, monkeypatch):
    ctx, _clock = context
    prime(ctx)
    writes, save = [], crud.save_step
    monkeypatch.setattr(crud, 'save_step', lambda step: (writes.append((step.progress, step.eta_seconds)), save(step))[1])
    ctx.clear_phase_eta()
    ctx.clear_phase_eta()
    assert writes == [(49.5, 0.)]
    assert ctx._last_pct == 49.5 and ctx._last_progress == (49.5, 0.)


@pytest.mark.parametrize('eta', [None, 0., 120.])
def test_clear_preserves_step_level_provider_and_persisted_eta(context, eta):
    ctx, _clock = context
    prime(ctx, scope='step', eta=eta)
    provider = ctx._eta_provider
    before = saved(ctx).model_dump()
    ctx.clear_phase_eta()
    assert ctx._eta_provider is provider and ctx._eta_scope == 'step'
    assert saved(ctx).model_dump() == before


def test_phase_clear_before_any_progress_does_not_advance_zero(context):
    ctx, _clock = context
    ctx.set_eta_provider(lambda: 4., scope='phase')
    assert not hasattr(ctx, '_last_pct')
    ctx.clear_phase_eta()
    assert saved(ctx).progress == 0. and saved(ctx).eta_seconds == 0.


@pytest.mark.parametrize('absolute_pct', [49.5, 83.6])
def test_version_context_binds_root_method_without_remapping_absolute_percentage(context, absolute_pct):
    ctx, _clock = context
    prime(ctx, pct=absolute_pct)
    version = VersionContext(ctx, {'values': {}})
    # 若误用版本包装的 report_progress，绝对进度会再次映射并推进。
    version._last_pct = 10.
    version.report_progress = lambda pct: pytest.fail('清理必须使用根 ctx 的绝对进度')
    assert version.clear_phase_eta.__self__ is ctx
    version.clear_phase_eta()
    assert ctx._last_pct == absolute_pct
    assert saved(ctx).progress == absolute_pct and saved(ctx).eta_seconds == 0.


def test_next_version_can_bind_fresh_phase_meter_without_old_tail(context):
    ctx, clock = context
    first = VersionContext(ctx, {'values': {}})
    first_meter = RateProgress(first, 10., start=0., end=49.5, scope='phase')
    clock[0] = 10.
    first_meter.update(9.99)
    assert saved(ctx).progress == 49.5 and ctx.eta_seconds() == .0
    # 使用确实为正数的旧尾值，以覆盖生产 0.1 秒残留形状。
    first.set_eta_provider(lambda: .1, scope='phase')
    first.report_progress(49.5)
    first.clear_phase_eta()
    assert saved(ctx).eta_seconds == 0.
    second = VersionContext(ctx, {'values': {}})
    second_meter = RateProgress(second, 10., start=49.5, end=99., scope='phase')
    clock[0] = 20.
    second_meter.update(1.)
    assert ctx.eta_seconds() == 90. and saved(ctx).progress == 54.5
    second.clear_phase_eta()
    assert saved(ctx).progress == 54.5 and saved(ctx).eta_seconds == 0.


@pytest.mark.parametrize('scope', ['phase', 'step'])
def test_finalize_without_audio_clears_phase_before_early_return_only(context, scope):
    ctx, _clock = context
    prime(ctx, scope=scope, eta=120.)
    result = audio_quality.finalize(VersionContext(ctx, {'values': {}}), Path('not-read.mp4'), has_audio=False)
    assert result['has_audio'] is False and result['issues'] == []
    assert saved(ctx).progress == 49.5
    assert saved(ctx).eta_seconds == (0. if scope == 'phase' else 120.)


@pytest.mark.parametrize('scope', ['phase', 'step'])
def test_finalize_clears_before_real_measurement_entry_and_preserves_report(context, monkeypatch, scope):
    ctx, _clock = context
    prime(ctx, scope=scope, eta=120.)
    def measure(_path, *, ctx):
        assert saved(ctx).eta_seconds == (0. if scope == 'phase' else 120.)
        assert ctx.eta_seconds() == (0. if scope == 'phase' else 120.)
        assert saved(ctx).progress == 49.5
        return measurement()
    monkeypatch.setattr(audio_quality, 'measure', measure)
    report = audio_quality.finalize(VersionContext(ctx, {'values': {}}), Path('not-read.mp4'))
    assert report['true_peak_db'] == -2. and report['processing']['gain_db'] == 0.
    assert report['target_reached'] is None
    assert runner.progress_eta(ctx.project.id, 'assemble')['execution']['phase'] == 'measuring_output_audio'


def test_finalize_measurement_failure_keeps_original_error_and_clears_eta(context, monkeypatch):
    ctx, _clock = context
    prime(ctx)
    error = DubForgeError('隔离测量失败', code='AUDIO_MEASUREMENT_INVALID')
    def fail(*_a, **_kw):
        assert saved(ctx).eta_seconds == 0.
        raise error
    monkeypatch.setattr(audio_quality, 'measure', fail)
    with pytest.raises(DubForgeError) as caught:
        audio_quality.finalize(ctx, Path('not-read.mp4'))
    assert caught.value is error and saved(ctx).progress == 49.5


def test_finalize_invalid_settings_also_clear_before_validation_failure(context):
    ctx, _clock = context
    prime(ctx)
    ctx._config_snapshot['assemble']['final_loudness_mode'] = 'invalid'
    with pytest.raises(DubForgeError) as caught:
        audio_quality.finalize(ctx, Path('not-read.mp4'), has_audio=False)
    assert caught.value.code == 'AUDIO_LOUDNESS_INVALID'
    assert saved(ctx).eta_seconds == 0. and saved(ctx).progress == 49.5


def test_finalize_cancel_still_raises_and_does_not_measure_or_unset_cancel(context, monkeypatch):
    ctx, _clock = context
    prime(ctx)
    ctx._cancel.set()
    monkeypatch.setattr(audio_quality, 'measure', lambda *_a, **_kw: pytest.fail('取消后不能测量'))
    with pytest.raises(StepCancelled):
        audio_quality.finalize(ctx, Path('not-read.mp4'))
    assert ctx._cancel.is_set() and saved(ctx).progress == 49.5 and saved(ctx).eta_seconds == 0.


def test_clear_does_not_change_pause_clock_or_execution_snapshot(context):
    ctx, _clock = context
    prime(ctx)
    ctx._pause.set()
    ctx._pause_started, ctx._paused_seconds = 4., 7.
    ctx.execution.phase('paused', '已暂停', state='paused')
    before = ctx.execution.snapshot()
    ctx.clear_phase_eta()
    assert ctx._pause.is_set() and ctx._pause_started == 4. and ctx._paused_seconds == 7.
    assert ctx.execution.snapshot()['phase'] == before['phase']
    assert ctx.execution.snapshot()['state'] == before['state'] == 'paused'
    assert saved(ctx).progress == 49.5 and saved(ctx).eta_seconds == 0.


@pytest.mark.parametrize('audio', [False, True])
def test_timeline_finish_clears_before_probe_and_audio_measurement(context, monkeypatch, audio):
    ctx, _clock = context
    prime(ctx)
    from dubforge.pipeline import virtual_preview
    def probe(_path):
        assert ctx.eta_seconds() == 0. and saved(ctx).eta_seconds == 0.
        assert saved(ctx).progress == 49.5
        assert ctx.execution.snapshot()['phase'] == 'checking_timeline_output'
        return dict(duration=10., width=640, height=360, has_audio=audio)
    def measure(_path, *, ctx):
        assert ctx.eta_seconds() == 0. and saved(ctx).eta_seconds == 0.
        return measurement()
    monkeypatch.setattr(virtual_preview, '_probe', probe)
    monkeypatch.setattr(audio_quality, 'measure', measure)
    version = VersionContext(ctx, {'values': {}})
    assembly_timeline._finish(version, Path('not-read.mp4'),
        dict(canvas=dict(width=640, height=360), output_duration=10.), audio)
    assert version._audio_report['has_audio'] is audio
    assert version._audio_report['measurement_provenance']['kind'] == 'timeline_sources'
    assert saved(ctx).progress == 49.5


@pytest.mark.parametrize('failure', ['probe', 'shape'])
def test_timeline_finish_failure_clears_eta_without_changing_original_error(context, monkeypatch, failure):
    ctx, _clock = context
    prime(ctx, pct=83.6)
    from dubforge.pipeline import virtual_preview
    error = DubForgeError('隔离探测失败', code='PROBE_FAILED')
    def probe(_path):
        assert saved(ctx).eta_seconds == 0.
        if failure == 'probe':
            raise error
        return dict(duration=9., width=640, height=360, has_audio=False)
    monkeypatch.setattr(virtual_preview, '_probe', probe)
    with pytest.raises(DubForgeError) as caught:
        assembly_timeline._finish(ctx, Path('not-read.mp4'),
            dict(canvas=dict(width=640, height=360), output_duration=10.), False)
    assert caught.value.code == ('PROBE_FAILED' if failure == 'probe' else 'ASSEMBLY_TIMELINE_OUTPUT_MISMATCH')
    assert saved(ctx).progress == 83.6 and saved(ctx).eta_seconds == 0.


def test_timeline_finish_preserves_step_eta(context, monkeypatch):
    ctx, _clock = context
    prime(ctx, scope='step', eta=120.)
    from dubforge.pipeline import virtual_preview
    monkeypatch.setattr(virtual_preview, '_probe', lambda *_: dict(duration=10., width=640, height=360, has_audio=False))
    assembly_timeline._finish(ctx, Path('not-read.mp4'), dict(canvas=dict(width=640, height=360), output_duration=10.), False)
    assert ctx.eta_seconds() == 120. and saved(ctx).eta_seconds == 120. and saved(ctx).progress == 49.5


def test_optional_boundary_calls_remain_compatible_with_old_context_without_helper(monkeypatch):
    old_ctx = SimpleNamespace(cfg=lambda _key, default=None: default)
    assert audio_quality.finalize(old_ctx, Path('not-read.mp4'), has_audio=False)['has_audio'] is False
    from dubforge.pipeline import virtual_preview
    monkeypatch.setattr(virtual_preview, '_probe', lambda *_: dict(duration=10., width=640, height=360, has_audio=False))
    assembly_timeline._finish(old_ctx, Path('not-read.mp4'), dict(canvas=dict(width=640, height=360), output_duration=10.), False)
    assert old_ctx._audio_report['has_audio'] is False


@pytest.mark.parametrize('original', [False, True])
def test_both_actual_mux_consumers_enter_real_finalize_without_old_encoding_eta(context, monkeypatch, original):
    ctx, clock = context
    from dubforge.pipeline import step_assemble as assemble, content_redaction, video_encoding
    monkeypatch.setattr(assemble, 'preflight_render', lambda *_: None)
    monkeypatch.setattr(assemble, 'prepare_filter', lambda *_a, **_kw: ('', None))
    monkeypatch.setattr(assemble, '_mixed_audio', lambda *_a, **_kw:
        (['ffmpeg', '-y', '-i', 'fixture-source.mp4'], ['[0:a]anull[a]'], '[a]'))
    monkeypatch.setattr(assemble, '_first_audio_stream', lambda *_: {'codec_name': 'aac'})
    monkeypatch.setattr(assemble, '_output_cut_plan', lambda *_: None)
    monkeypatch.setattr(assemble, '_compare_audio', lambda _ctx, report, *_a, **_kw: report)
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda _ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *_a, **_kw: dict(filter='', background=None, fallback_reason=''))
    root = ctx.pdir
    out = root / 'output' / ('original.mp4' if original else 'dub.mp4')
    out.parent.mkdir(exist_ok=True)
    def fake_encode(_ctx, _video, command, *, cwd, on_stdout, on_ready, **_kw):
        on_ready()
        clock[0] += 100.
        on_stdout('out_time_us=9990000')
        assert saved(ctx).eta_seconds == .1
        (Path(cwd) / command[-1]).write_bytes(b'isolated mux fixture, not media')
        return 0, '', ''
    monkeypatch.setattr(video_encoding, 'run', fake_encode)
    def measure(_path, *, ctx):
        assert ctx._eta_provider is None and saved(ctx).eta_seconds == 0.
        assert saved(ctx).progress == 49.5
        return measurement()
    monkeypatch.setattr(audio_quality, 'measure', measure)
    if original:
        assemble._mux_original_sub(ctx, root / 'source.mp4', out, 10., (0., 49.5), burn=False)
    else:
        assemble._mux(ctx, root, root / 'source.mp4', root / 'voice.wav', out, 10.,
            burn=False, progress_range=(0., 49.5))
    assert out.read_bytes() == b'isolated mux fixture, not media'
    assert saved(ctx).progress == 49.5 and saved(ctx).eta_seconds == 0.
    assert not list(out.parent.glob('.tmp*'))
