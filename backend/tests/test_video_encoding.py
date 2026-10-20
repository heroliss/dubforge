"""显式硬件合同只用隔离能力替身；不探测真实GPU、不运行FFmpeg或模型。"""
import copy
import hashlib
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from dubforge.errors import DubForgeError
from dubforge.pipeline import video_encoding as ve


LEGACY_CANONICAL = b'{"dimensions": {"burn_subtitles": true, "dubbing": false, "include_comments": false}, "edits": {}, "inputs": {"download/video.mp4": "source-sha"}, "schema": 1, "values": {"assemble.crf": 22, "assemble.subtitle_auto_fit": false, "assemble.subtitle_color": "#FFFFFF", "assemble.subtitle_track": "trans", "content_review.enabled": false}}'


@pytest.fixture(autouse=True)
def forbid_real_encoding(monkeypatch):
    monkeypatch.setattr(ve, '_run', lambda *_args: pytest.fail('隔离测试禁止真实硬件、FFprobe或FFmpeg请求'))


def legacy_version(**values):
    return dict(id='fixture', name='普通版本', dubbing=False, burn_subtitles=True, include_comments=False,
        values={'assemble.crf': 22, 'assemble.subtitle_track': 'trans', 'assemble.subtitle_auto_fit': False,
                'assemble.subtitle_source_style_enabled': False, 'assemble.subtitle_color': '#FFFFFF',
                'content_review.enabled': False, **values})


def cfg(values=None):
    values = values or {}
    return lambda key, default=None: values.get(key, default)


def fingerprint(version, monkeypatch):
    from dubforge.pipeline import assembly_versions as av, subtitle_document
    monkeypatch.setattr(subtitle_document, 'records', lambda *_: {})
    deps = SimpleNamespace(project=SimpleNamespace(uid='fixture'), arts={'download/video.mp4': SimpleNamespace(sha256='source-sha')},
        cfg=cfg(version['values']), inputs=lambda *_: [('download', 'video.mp4', '源视频')])
    deps.for_version = lambda _version: deps
    return av.fingerprint(deps, version)


def test_software_exact_legacy_bytes_nondefault_crf_and_inactive_cq(monkeypatch):
    from dubforge.pipeline import assembly_versions as av
    old = legacy_version()
    assert av.active_values(old) == json.loads(LEGACY_CANONICAL)['values']
    expected = hashlib.sha256(LEGACY_CANONICAL).hexdigest()
    assert fingerprint(old, monkeypatch) == expected
    for cq in (14, 18, 28):
        new = legacy_version(**{ve.ENCODER_KEY: 'software', ve.CQ_KEY: cq})
        new['encoder_profile'] = ve.profile(new['values'])
        assert fingerprint(new, monkeypatch) == expected
    assert fingerprint(legacy_version(**{'assemble.crf': 23}), monkeypatch) != expected
    assert ve.arguments(ve.profile(old['values'])) == ['-c:v', 'libx264', '-crf', '22', '-preset', 'medium']


def test_nvenc_uses_independent_cq_and_versioned_profile(monkeypatch):
    old = legacy_version()
    base = legacy_version(**{ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 18})
    assert fingerprint(base, monkeypatch) != fingerprint(old, monkeypatch)
    assert fingerprint(base, monkeypatch) == fingerprint({**base, 'values': {**base['values'], 'assemble.crf': 27}}, monkeypatch)
    assert fingerprint(base, monkeypatch) != fingerprint({**base, 'values': {**base['values'], ve.CQ_KEY: 20}}, monkeypatch)
    expected = ve.profile(base['values'])
    assert expected['version'] == ve.NVENC_PROFILE and expected['quality'] == 18
    arguments = ve.arguments(expected)
    assert arguments == ['-c:v', 'h264_nvenc', '-gpu', '0', '-preset', 'p6', '-tune', 'hq', '-rc', 'vbr', '-cq', '18', '-b:v', '0']
    assert '-r' not in arguments and '-s' not in arguments and '-crf' not in arguments


def test_legacy_integer_valued_float_keeps_old_fingerprint_and_command(monkeypatch):
    version = legacy_version(**{'assemble.crf': 22.0})
    expected = LEGACY_CANONICAL.replace(b'"assemble.crf": 22,', b'"assemble.crf": 22.0,')
    assert fingerprint(version, monkeypatch) == hashlib.sha256(expected).hexdigest()
    assert ve.arguments(ve.profile(version['values'])) == ['-c:v', 'libx264', '-crf', '22', '-preset', 'medium']


def test_no_video_effects_do_not_consume_encoder_or_quality(monkeypatch):
    version = legacy_version()
    version['burn_subtitles'] = False
    assert fingerprint(version, monkeypatch) == fingerprint({**version,
        'values': {**version['values'], ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 24}}, monkeypatch)


def test_old_frozen_values_never_inherit_new_global_nvenc():
    from dubforge.pipeline.version_render import VersionContext
    from dubforge.pipeline.dependencies import Dependencies
    parent = SimpleNamespace(cfg=cfg({ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 26}))
    version = legacy_version()
    ctx = VersionContext(parent, version)
    assert ctx.cfg(ve.ENCODER_KEY) == 'software' and ctx.cfg(ve.CQ_KEY) == 18
    assert ve.context_profile(ctx)['quality'] == 22
    deps = object.__new__(Dependencies)
    deps.version, deps.overrides, deps.settings = version, {}, {'assemble': {'video_encoder': 'nvenc', 'nvenc_cq': 26}}
    assert deps.cfg(ve.ENCODER_KEY) == 'software' and deps.cfg(ve.CQ_KEY) == 18
    assert version['values'].get(ve.ENCODER_KEY) is None, '读取兼容不改写原冻结记录'


def test_frozen_actual_profile_cannot_be_tampered_or_replaced_by_current_parent():
    version = legacy_version(**{ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 20})
    version['encoder_profile'] = ve.profile(version['values'])
    ctx = SimpleNamespace(version=version, cfg=cfg({ve.ENCODER_KEY: 'software'}))
    assert ve.context_profile(ctx)['encoder'] == 'h264_nvenc'
    version['encoder_profile']['preset'] = 'p1'
    with pytest.raises(ve.EncodingError):
        ve.context_profile(ctx)


@pytest.mark.parametrize('values', [{ve.ENCODER_KEY: 'auto'}, {ve.ENCODER_KEY: []},
    {ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: True}, {ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 99}])
def test_invalid_modes_never_fall_back(values):
    with pytest.raises(ve.EncodingError):
        ve.profile(values)


def test_generic_snapshot_exact_software_defaults_and_upstream_unaffected(make_project):
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline.config_fingerprint import snapshot
    uid = make_project()
    deps = Dependencies(crud.get_project(uid))
    deps.settings['assemble']['crf'] = 22
    deps.overrides['assemble.crf'] = 22
    software = snapshot(deps, 'assemble')
    assert software['assemble']['crf'] == 22
    assert 'video_encoder' not in software['assemble'] and 'nvenc_cq' not in software['assemble']
    # 独立旧配置只移除新字段，与明确旧规范键集比较，而非比较两次新profile输出。
    old_settings = copy.deepcopy(deps.settings)
    old_settings['assemble'].pop('video_encoder', None); old_settings['assemble'].pop('nvenc_cq', None)
    old = Dependencies(deps.project, settings=old_settings, overrides=deps.overrides)
    assert snapshot(old, 'assemble') == software
    upstream = snapshot(deps, 'tts')
    deps.overrides.update({ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 20})
    assert snapshot(deps, 'tts') == upstream
    nvenc = snapshot(deps, 'assemble')['assemble']
    assert nvenc['nvenc_cq'] == 20 and nvenc['encoder_profile'] == ve.NVENC_PROFILE
    assert 'crf' not in nvenc


def test_legacy_config_snapshot_still_has_exact_old_software_keys():
    from dubforge.pipeline.config_fingerprint import snapshot
    # 原legacy合同为原样读取assemble段；新增缺省不能破坏这个旧字节序列。
    deps = SimpleNamespace(cfg=cfg({'assemble': {'crf': 22, 'video_encoder': 'software', 'nvenc_cq': 28}}))
    result = snapshot(deps, 'assemble', keys=('assemble',), legacy=True)
    assert json.dumps(result, sort_keys=True).encode() == b'{"assemble": {"crf": 22}}'


def test_audio_master_cache_contract_does_not_consume_video_encoder(tmp_path, monkeypatch):
    from dubforge.pipeline import audio_master
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'isolated source')
    ctx = SimpleNamespace(cfg=cfg({ve.ENCODER_KEY: 'software'}), check_cancel=lambda: None)
    command = ['ffmpeg', '-i', str(source)]
    options = dict(target=-16, peak=-1.5)
    a = audio_master._signature(ctx, command, ['[0:a]anull[a]'], '[a]', None, 10, options)
    ctx.cfg = cfg({ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 20})
    assert audio_master._signature(ctx, command, ['[0:a]anull[a]'], '[a]', None, 10, options) == a


def test_partial_keeps_old_baseline_encoding_despite_new_project_default(make_project, monkeypatch):
    from dubforge.domain import crud
    from dubforge.pipeline import assembly_versions as av, assembly_partial
    from dubforge.pipeline.dependencies import Dependencies
    uid = make_project()
    deps = Dependencies(crud.get_project(uid))
    deps.overrides.update({ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 26})
    raw = dict(id='fixture', name='局部', dubbing=False, burn_subtitles=True, include_comments=False,
               overrides={'assemble.crf': 22}, partial={'plan_id': 'a' * 32})
    monkeypatch.setattr(assembly_partial, 'load_plan', lambda *_: {'base_version': legacy_version()})
    result = av.resolve(deps, raw)
    assert result['values'][ve.ENCODER_KEY] == 'software' and result['values'][ve.CQ_KEY] == 18
    assert result['values']['assemble.crf'] == 22 and result['origins'][ve.ENCODER_KEY] == '基准成片'
    with pytest.raises(DubForgeError) as error:
        av.resolve(deps, {**raw, 'overrides': {**raw['overrides'], ve.ENCODER_KEY: 'nvenc'}})
    assert error.value.code == 'ASSEMBLY_PARTIAL_ENCODING'


@pytest.mark.parametrize('kind', ['partial', 'update', 'timeline'])
def test_hardware_baselines_are_rejected_before_unsupported_partial_render(make_project, monkeypatch, kind):
    from dubforge.domain import crud
    from dubforge.pipeline import assembly_versions as av, assembly_partial, assembly_updates, assembly_timeline
    from dubforge.pipeline.dependencies import Dependencies
    uid = make_project(); deps = Dependencies(crud.get_project(uid))
    raw = dict(id='fixture', name='历史', dubbing=False, burn_subtitles=True, include_comments=False,
               overrides={}, **{kind: {'plan_id': 'a' * 32}})
    saved = legacy_version(**{ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 18})
    if kind == 'partial':
        monkeypatch.setattr(assembly_partial, 'load_plan', lambda *_: {'base_version': saved})
    else:
        module = assembly_updates if kind == 'update' else assembly_timeline
        monkeypatch.setattr(module, 'resolve', lambda *_: saved)
    with pytest.raises(ve.EncodingError) as error:
        av.resolve(deps, raw)
    assert error.value.code == 'ASSEMBLY_ENCODER_BASELINE_UNSUPPORTED'


@pytest.mark.parametrize('evidence', [
    {'review_render': {'encoder': {'name': 'h264_nvenc'}}},
    {'video_encoding': {'profile': {'mode': 'nvenc', 'encoder': 'h264_nvenc'}}},
    {'encoder_profile': {'mode': 'nvenc', 'encoder': 'h264_nvenc'}},
])
def test_explicit_historical_hardware_evidence_is_not_mislabeled_legacy_software(evidence):
    saved = {**legacy_version(), **evidence}
    assert ve.profile(saved['values'])['mode'] == 'software', '旧values缺编码键，不代表明确硬件回执不存在'
    with pytest.raises(ve.EncodingError) as error: ve.require_software_baseline(saved)
    assert error.value.code == 'ASSEMBLY_ENCODER_BASELINE_UNSUPPORTED'
    ve.require_software_baseline(legacy_version())


def _fake_capability(monkeypatch, *, free='6300', media=None, encoders='V..... h264_nvenc', fail_init=None):
    calls = []
    media = media or dict(width=3840, height=2160, pix_fmt='yuv420p', color_space='bt709',
                           color_transfer='bt709', r_frame_rate='24/1')
    def execute(command):
        calls.append(command)
        if '-encoders' in command: return encoders
        if '-h' in command: return 'p6 hq'
        if command[0] == 'nvidia-smi': return free
        if command[0] == 'ffprobe': return json.dumps({'streams': [media]})
        if fail_init: raise fail_init
        return ''
    monkeypatch.setattr(ve, '_run', execute)
    return calls


def test_capability_uses_one_frame_source_dimensions_not_tiny_false_negative(monkeypatch):
    calls = _fake_capability(monkeypatch)
    result = ve.inspect_capability(Path('isolated-source.mp4'), ve.profile({ve.ENCODER_KEY: 'nvenc'}))
    assert result['source']['width'] == 3840 and result['free_mib'] == 6300
    command = calls[-1]
    assert 'color=c=black:s=3840x2160:r=1' in command
    assert command[command.index('-frames:v') + 1] == '1' and command[-3:] == ['-f', 'null', '-']
    assert not any(str(part).endswith('.mp4') for part in command)


@pytest.mark.parametrize('case,code', [('unsupported', 'ASSEMBLE_ENCODER_UNAVAILABLE'),
    ('memory', 'ASSEMBLE_ENCODER_MEMORY'), ('unknown', 'ASSEMBLE_ENCODER_UNAVAILABLE'),
    ('hdr', 'ASSEMBLE_ENCODER_FORMAT'), ('depth', 'ASSEMBLE_ENCODER_FORMAT')])
def test_known_capability_failures_do_not_encode_or_fallback(monkeypatch, case, code):
    media = dict(width=3840, height=2160, pix_fmt='yuv420p', color_transfer='bt709')
    if case == 'hdr': media['color_transfer'] = 'smpte2084'
    if case == 'depth': media['pix_fmt'] = 'yuv420p10le'
    calls = _fake_capability(monkeypatch, free='512' if case == 'memory' else 'unknown' if case == 'unknown' else '6300',
        media=media, encoders='' if case == 'unsupported' else 'h264_nvenc')
    with pytest.raises(ve.EncodingError) as error:
        ve.inspect_capability(Path('isolated.mp4'), ve.profile({ve.ENCODER_KEY: 'nvenc'}))
    assert error.value.code == code
    assert not any('-frames:v' in command for command in calls), '拒绝后不能启动实际编码或改CPU'


def _runtime(monkeypatch, *, slot='assemble', failure=None):
    from dubforge.runner import runner
    from dubforge import tts_service
    events = []
    monkeypatch.setattr(runner, '_slot_key', lambda _key: 'gpu')
    monkeypatch.setattr(runner, 'acquire_slot', lambda *a: events.append('acquire') or 'gpu')
    monkeypatch.setattr(runner, 'release_slot', lambda *a: events.append('release'))
    @contextmanager
    def reserve(_cfg):
        events.append('reserve')
        try: yield
        finally: events.append('unreserve')
    monkeypatch.setattr(tts_service, 'reserve_for_render', reserve)
    def capability(*args, **kwargs):
        events.append('inspect')
        if failure: raise failure
        return dict(source={'width': 3840, 'height': 2160}, free_mib=6300)
    monkeypatch.setattr(ve, 'inspect_capability', capability)
    ctx = SimpleNamespace(cfg=cfg({ve.ENCODER_KEY: 'nvenc'}), check_cancel=lambda: None,
        _resource_slot=slot, _cancel=threading.Event(), log=lambda _: None)
    return ctx, events


@pytest.mark.parametrize('slot,expected', [('assemble', ['acquire', 'reserve', 'inspect', 'body', 'unreserve', 'release']),
                                        ('gpu', ['reserve', 'inspect', 'body', 'unreserve'])])
def test_gpu_slot_double_admission_and_no_same_group_reentry(monkeypatch, slot, expected):
    ctx, events = _runtime(monkeypatch, slot=slot)
    with ve.admission(ctx, Path('fixture.mp4'), ve.context_profile(ctx)):
        events.append('body')
    assert events == expected


def test_preflight_failure_and_cancel_release_both_resources(monkeypatch):
    cancelled = DubForgeError('隔离取消', code='CANCELLED')
    ctx, events = _runtime(monkeypatch, failure=cancelled)
    with pytest.raises(DubForgeError) as error:
        with ve.admission(ctx, Path('fixture.mp4'), ve.context_profile(ctx)):
            pytest.fail('取消后不得编码')
    assert error.value.code == 'CANCELLED'
    assert events == ['acquire', 'reserve', 'inspect', 'unreserve', 'release']


def test_software_and_copy_never_touch_hardware_or_tts(monkeypatch, tmp_path):
    from dubforge.runner import runner
    from dubforge import tts_service
    def forbidden(*a, **kw): pytest.fail('软件及视频复制不得调用GPU或TTS资源管理')
    monkeypatch.setattr(runner, 'acquire_slot', forbidden)
    monkeypatch.setattr(tts_service, 'reserve_for_render', forbidden)
    monkeypatch.setattr(ve, 'inspect_capability', forbidden)
    ctx = SimpleNamespace(cfg=cfg({'assemble.crf': 22}), run_subprocess=lambda *_a, **_kw: (0, '', ''))
    assert ve.run(ctx, tmp_path / 'source.mp4', ['ffmpeg', '-i', 'source', 'output.mp4'], cwd=tmp_path)[0] == 0
    ctx.cfg = cfg({ve.ENCODER_KEY: 'nvenc'})
    assert ve.run(ctx, tmp_path / 'source.mp4', ['ffmpeg', '-i', 'source', 'output.mp4'], cwd=tmp_path, required=False)[0] == 0


@pytest.mark.parametrize('original', [False, True])
@pytest.mark.parametrize('fail', [False, 'memory', 'cancel'])
def test_both_real_mux_paths_share_profile_atomic_success_and_failure(tmp_path, monkeypatch, original, fail):
    from dubforge.pipeline import step_assemble as assemble, content_redaction, subtitle_style, audio_quality
    ctx, events = _runtime(monkeypatch)
    clock, eta = [0.0], [lambda: 987]
    ctx.active_seconds = lambda: clock[0]
    ctx.set_eta_provider = lambda provider, **kw: eta.append(provider)
    def delayed_capability(*args, **kwargs):
        assert eta[-1] is None, '等待/核验阶段不能泄露上一个成片版本剩下的ETA'
        events.append('inspect'); clock[0] += 120
        return dict(source={'width': 3840, 'height': 2160}, free_mib=6300)
    monkeypatch.setattr(ve, 'inspect_capability', delayed_capability)
    monkeypatch.setattr(assemble, 'preflight_render', lambda *_: None)
    monkeypatch.setattr(assemble, 'prepare_filter', lambda *a, **kw: ('', None))
    monkeypatch.setattr(assemble, '_mixed_audio', lambda *a, **kw:
        (['ffmpeg', '-y', '-i', 'fixture-source.mp4'], ['[0:a]anull[a]'], '[a]'))
    monkeypatch.setattr(assemble, '_first_audio_stream', lambda *_: {'codec_name': 'aac'})
    monkeypatch.setattr(assemble, '_compare_audio', lambda _ctx, report, *a, **kw: report)
    monkeypatch.setattr(content_redaction, 'safe_overlay', lambda _ctx, plan: plan)
    monkeypatch.setattr(content_redaction, 'audio_plan', lambda *a, **kw:
        dict(filter='', background=None, fallback_reason=''))
    monkeypatch.setattr(subtitle_style, 'prepare_burn_filter', lambda *a, **kw: ('ass=fixture.ass', None))
    monkeypatch.setattr(audio_quality, 'finalize', lambda *a, **kw: {'fixture': 'audio-unchanged'})
    monkeypatch.setattr(audio_quality, 'write_report', lambda *a, **kw: None)
    root = tmp_path / 'project'; root.mkdir(); out = root / 'output' / 'formal.mp4'; out.parent.mkdir()
    out.write_bytes(b'previous formal result')
    commands = []
    def execute(command, **kwargs):
        commands.append(command)
        assert clock[0] == 120
        clock[0] += 10
        kwargs['on_stdout']('out_time_us=1000000')
        assert eta[-1]() == pytest.approx(90), '10秒成片完成1秒/活动10秒，ETA不含此前120秒准入等待'
        (kwargs['cwd'] / command[-1]).write_bytes(b'new full encoding')
        if fail == 'cancel':
            raise DubForgeError('隔离编码中取消', code='CANCELLED')
        return (1, '', 'h264_nvenc CUDA out of memory') if fail else (0, '', '')
    ctx.task_data, ctx.run_subprocess = {}, execute
    ctx.upstream = lambda *_: root / 'subtitle.srt'
    ctx.report_progress = lambda *_: None
    ctx.cfg = cfg({ve.ENCODER_KEY: 'nvenc', ve.CQ_KEY: 20})
    def call():
        if original:
            assemble._mux_original_sub(ctx, root / 'source.mp4', out, 10, (0, 99), burn=True)
        else:
            assemble._mux(ctx, root, root / 'source.mp4', root / 'voice.wav', out, 10, burn=True)
    if fail:
        with pytest.raises(DubForgeError) as error: call()
        assert error.value.code == ('CANCELLED' if fail == 'cancel' else 'ASSEMBLE_ENCODER_MEMORY')
        assert out.read_bytes() == b'previous formal result'
    else:
        call(); assert out.read_bytes() == b'new full encoding'
        assert ctx._audio_report == {'fixture': 'audio-unchanged'}
        assert ctx._video_encoding_report['profile']['quality'] == 20
    assert len(commands) == 1 and '-cq' in commands[0] and '-crf' not in commands[0]
    assert 'ass=fixture.ass' in str(commands[0])
    if original:
        assert commands[0][commands[0].index('-c:a') + 1] == 'copy', '关闭声音处理时原AAC沿用原复制策略'
    else:
        assert commands[0][commands[0].index('-b:a') + 1] == '192k'
    assert not list(out.parent.glob('.tmp*')) and events[-2:] == ['unreserve', 'release']
