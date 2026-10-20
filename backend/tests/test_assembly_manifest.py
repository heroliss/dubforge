"""选择性更新必须能重建真实旧内容，不能读取已被覆盖的当前素材。"""
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from dubforge.pipeline import assembly_manifest as manifests
from dubforge.pipeline.comment_overlay import ass_document
from dubforge.comment_studio import validate_settings

pytestmark = pytest.mark.usefixtures('isolated_env')


def deps(tmp_path):
    root = tmp_path / 'project'
    root.mkdir(exist_ok=True)
    source = root / 'source.mp4'
    source.write_bytes(b'original-video')
    return SimpleNamespace(root=root, arts={}, project=SimpleNamespace(source_kind='local', source_url=str(source)))


def version():
    return dict(id='test', name='测试成片', dubbing=True, burn_subtitles=True, include_comments=False,
                overrides={}, values={'assemble.subtitle_track': 'bilingual', 'assemble.bgm_volume': 0,
                                      'subtitles.source_first': False})


def manifest(dependencies, wav=None):
    ref = manifests._ref(dependencies, dependencies.project.source_url)
    return dict(schema=1, duration=5, version=version(), inputs={'stable': '比较证据'},
                media={'source': ref}, subtitles=[], comments=[], review={},
                audio=([dict(id=1, source='dub', start=1, end=2, audio_offset=0, gain_db=0,
                             media=manifests._ref(dependencies, wav))] if wav else []))


def wav_at(path, value=.1, seconds=2):
    sf.write(path, np.full(round(seconds * 32000), value, dtype=np.float32), 32000, subtype='FLOAT')
    return path


def test_materialize_preserves_old_sound_after_current_take_is_overwritten(tmp_path):
    dependencies = deps(tmp_path)
    wav = wav_at(dependencies.root / '1.wav')
    original = manifest(dependencies, wav)
    frozen = manifests.materialize(dependencies, original)
    cached = manifests.media_path(dependencies, frozen['audio'][0]['media'])
    assert cached != wav and cached.is_relative_to(dependencies.root / 'output/.render-assets')
    before = cached.read_bytes()
    wav_at(wav, value=.7)
    manifests.validate_media(dependencies, frozen)
    assert cached.read_bytes() == before and sf.read(cached)[0][100] == pytest.approx(.1)
    assert frozen['inputs'] == original['inputs'] and original['audio'][0]['media']['path'] == '1.wav'
    assert not list(cached.parent.glob('*.tmp'))


def test_materialize_deduplicates_same_content_and_remains_idempotent(tmp_path):
    dependencies = deps(tmp_path)
    a = wav_at(dependencies.root / '1.wav')
    b = wav_at(dependencies.root / '2.wav')
    original = manifest(dependencies, a)
    original['audio'].append({**copy.deepcopy(original['audio'][0]), 'id': 2,
                              'media': manifests._ref(dependencies, b)})
    frozen = manifests.materialize(dependencies, original)
    assert frozen['audio'][0]['media'] == frozen['audio'][1]['media']
    assert manifests.materialize(dependencies, frozen) == frozen
    assert len(list((dependencies.root / 'output/.render-assets').glob('*.wav'))) == 1


def test_materialize_does_not_rebind_conflicting_history_ref_via_same_path_cache(tmp_path):
    dependencies = deps(tmp_path)
    wav = wav_at(dependencies.root / '1.wav')
    value = manifest(dependencies, wav)
    outdated = copy.deepcopy(value['audio'][0])
    outdated.update(id=406)
    outdated['media']['identity'] = {'sha256': 'f' * 64, 'size': wav.stat().st_size}
    value['audio'].append(outdated)
    with pytest.raises(manifests.ManifestError) as error:
        manifests.materialize(dependencies, value)
    assert error.value.code == 'ASSEMBLY_HISTORY_MEDIA_CHANGED'
    assert value['audio'][1]['media']['identity']['sha256'] == 'f' * 64


def test_visual_replacement_can_keep_missing_old_audio_but_audio_rebuild_cannot(tmp_path):
    dependencies = deps(tmp_path)
    value = manifest(dependencies)
    value['audio'] = [dict(id=405, media={'path': '', 'identity': {}, 'missing': True})]
    manifests.validate_media(dependencies, value, audio=False)
    with pytest.raises(manifests.ManifestError) as exc:
        manifests.validate_media(dependencies, value)
    assert exc.value.code == 'ASSEMBLY_HISTORY_MEDIA_MISSING' and '405' in str(exc.value)
    value['audio'][0]['media'] = {'path': 'not-actually-old.wav', 'identity': {'sha256': 'a' * 64, 'size': 10}}
    assert manifests.materialize(dependencies, value, audio=False) == value


def test_changed_source_is_rejected_instead_of_using_current_same_name(tmp_path):
    dependencies = deps(tmp_path)
    value = manifest(dependencies)
    source = Path(dependencies.project.source_url)
    with source.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    value['media']['source']['identity'] = dict(sha256=digest, size=source.stat().st_size)
    source.write_bytes(b'new-video!!!!!')
    with pytest.raises(manifests.ManifestError) as exc:
        manifests.validate_media(dependencies, value, audio=False)
    assert exc.value.code == 'ASSEMBLY_HISTORY_MEDIA_CHANGED'


def test_relative_media_path_cannot_escape_project(tmp_path):
    dependencies = deps(tmp_path)
    with pytest.raises(manifests.ManifestError):
        manifests.media_path(dependencies, {'path': '../another-project/audio.wav'})


def test_subtitle_parts_and_overlapping_event_id_are_preserved(tmp_path):
    dependencies = deps(tmp_path)
    value = manifest(dependencies)
    value['subtitles'] = [dict(id=405, start=2, end=3, text='新中文\nold English',
                               parts=[['trans', '新中文'], ['source', 'old English']])]
    row = manifests.subtitle_lines(value)[0]
    assert row.index == 405 and row.parts == (('trans', '新中文'), ('source', 'old English'))
    assert row.start == 2 and row.end == 3


def test_explicit_comments_keep_independent_old_and_new_style_and_fractional_time(tmp_path):
    dependencies = deps(tmp_path)
    value = manifest(dependencies)
    old = validate_settings({'start': 30, 'interval': 25, 'duration': 15,
                             'color': '#FF0000', 'position': 'top_left', 'language_mode': 'translated'})
    new = validate_settings({**old, 'color': '#00FF00', 'position': 'bottom_right'})
    value['comments'] = [dict(id='a', start=1.25, end=1.75,
        item={'id': 'a', 'text': 'old', 'display_text': '保留的旧评论'}, settings=old),
        dict(id='b', start=3, end=4,
        item={'id': 'b', 'text': 'new', 'display_text': '选中的新评论'}, settings=new)]
    document = ass_document(manifests.comment_plan(value), (1280, 720))
    assert '0:00:01.25,0:00:01.75,Comment0' in document
    assert '0:00:03.00,0:00:04.00,Comment1' in document
    assert 'Style: Comment0,' in document and 'Style: Comment1,' in document
    assert '保留的旧评论' in document and '选中的新评论' in document
    assert '&H000000FF&' in document and '&H0000FF00&' in document
    clipped = ass_document(manifests.comment_plan(value), (1280, 720), start=1.5, end=2)
    assert '0:00:00.00,0:00:00.25,Comment0' in clipped and '选中的新评论' not in clipped


def test_explicit_historical_long_comment_does_not_reapply_auto_candidate_limit(tmp_path):
    dependencies = deps(tmp_path)
    value = manifest(dependencies)
    settings = validate_settings({'language_mode': 'translated'})
    value['comments'] = [dict(id='long', start=0, end=5,
        item={'id': 'long', 'text': 'source', 'display_text': '中' * 321}, settings=settings)]
    assert ass_document(manifests.comment_plan(value), (1920, 1080)).count('中') == 321


def test_mix_voice_honors_frozen_position_offset_gain_without_current_selection(tmp_path, monkeypatch):
    dependencies = deps(tmp_path)
    wav = dependencies.root / '1.wav'
    sf.write(wav, np.concatenate([np.full(32000, .1), np.full(32000, .3)]).astype(np.float32),
             32000, subtype='FLOAT')
    value = manifest(dependencies, wav)
    value['audio'][0].update(start=2, end=2.5, audio_offset=1, gain_db=-6)
    cfg = {'assemble.loudnorm': True}
    context = SimpleNamespace(root=dependencies.root, cfg=lambda key, default=None: cfg.get(key, default),
                              check_cancel=lambda: None)
    monkeypatch.setattr('dubforge.pipeline.audio_quality.normalize_voice', lambda *args: None)
    output = manifests.mix_voice(context, value, dependencies.root / 'voice.wav', 5)
    samples, rate = sf.read(output)
    assert rate == 32000 and len(samples) == 5 * 32000
    assert np.max(np.abs(samples[:int(1.9 * rate)])) == 0
    assert samples[int(2.2 * rate)] == pytest.approx(.3 * 10 ** (-6 / 20), abs=1e-6)
    assert np.max(np.abs(samples[int(2.6 * rate):])) == 0
    assert context._audio_line_report[0]['start'] == 2


def test_mix_original_uses_absolute_source_offset(tmp_path, monkeypatch):
    dependencies = deps(tmp_path)
    wav = dependencies.root / 'vocal.wav'
    sf.write(wav, np.concatenate([np.full(32000, .1), np.full(32000, .4)]).astype(np.float32),
             32000, subtype='FLOAT')
    value = manifest(dependencies, wav)
    value['audio'][0].update(source='original', start=1.25, end=1.75, audio_offset=1.25)
    context = SimpleNamespace(root=dependencies.root, cfg=lambda key, default=None:
                              True if key == 'assemble.loudnorm' else default, check_cancel=lambda: None)
    monkeypatch.setattr('dubforge.pipeline.audio_quality.normalize_voice', lambda *args: None)
    output = manifests.mix_voice(context, value, dependencies.root / 'original.wav', 5)
    samples, rate = sf.read(output)
    assert samples[int(1.5 * rate)] == pytest.approx(.4)


def saved_value(dependencies):
    identity = manifests._ref(dependencies, dependencies.project.source_url)['identity']
    value = version()
    value['sentence_inputs'] = dict(schema=1, coverage='known', duration=5,
        global_inputs={'source': identity, 'redaction': {'ranges': []}}, comment_inputs={'known': True, 'events': []},
        lines={'405': dict(id=405, known=True, document={'source_text': 'Old English', 'text': '旧中文'},
                           subtitle=[{'start': 1, 'end': 2, 'text': '旧中文\nOld English'}], audio=[])})
    return value


def test_legacy_without_actual_inputs_never_uses_current_working_copy(tmp_path):
    dependencies = deps(tmp_path)
    with pytest.raises(manifests.ManifestError) as exc:
        manifests.from_saved(dependencies, version())
    assert exc.value.code == 'ASSEMBLY_HISTORY_UNAVAILABLE'


def test_historical_known_inputs_restore_bilingual_parts_from_saved_document(tmp_path):
    dependencies = deps(tmp_path)
    value = manifests.from_saved(dependencies, saved_value(dependencies))
    assert value['subtitles'][0]['parts'] == [['trans', '旧中文'], ['source', 'Old English']]
    manifests.validate_media(dependencies, value)


def test_historical_missing_audio_is_explicit_not_replaced_with_current_take(tmp_path):
    dependencies = deps(tmp_path)
    value = saved_value(dependencies)
    value['sentence_inputs']['lines']['405']['audio'] = [dict(source='dub', start=1, end=2,
                                                            identity={'sha256': 'f' * 64, 'size': 100})]
    restored = manifests.from_saved(dependencies, value)
    assert restored['audio'][0]['media']['missing']
    manifests.validate_media(dependencies, restored, audio=False)


def test_new_manifest_is_copied_and_review_audio_padding_is_not_recomputed(tmp_path):
    dependencies = deps(tmp_path)
    original = manifest(dependencies)
    original['review'] = {'audio_plan': {'ranges': [{'start': .85, 'end': 2.15}]}}
    result = manifests.from_saved(dependencies, {'render_manifest': original})
    assert result['review']['audio_plan']['ranges'] == [{'start': .85, 'end': 2.15}]
    result['review']['audio_plan']['ranges'][0]['start'] = 0
    assert original['review']['audio_plan']['ranges'][0]['start'] == .85


def test_capture_is_read_only_and_keeps_real_language_parts(make_project, monkeypatch):
    from test_assemble import _prepare_outputs_project
    from test_configuration import save
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline import assembly_versions as av
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    save('project:' + uid, {'content_review.enabled': False})
    dependencies = Dependencies(crud.get_project(uid))
    spec = {key: value for key, value in version().items() if key != 'values'}
    spec.update(dubbing=False, overrides={'assemble.subtitle_track': 'bilingual'})
    resolved = av.resolve(dependencies, spec)
    monkeypatch.setattr(hashlib, 'file_digest', lambda *args, **kwargs: pytest.fail('只读capture不能hash媒体'))
    result = manifests.capture(dependencies, resolved)
    assert result['subtitles'][0]['parts'][0][0] == 'trans'
    assert result['subtitles'][0]['parts'][1][0] == 'source'
    assert not (root / 'output/.render-assets').exists()


def test_capture_original_audio_records_actual_absolute_source_offset(make_project, monkeypatch):
    from test_assemble import _prepare_outputs_project
    from test_configuration import save
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.pipeline.subtitle_document import read
    from dubforge.pipeline import assembly_versions as av, dubbing_edit
    uid, root = _prepare_outputs_project(make_project, monkeypatch)
    save('project:' + uid, {'content_review.enabled': False})
    project = crud.get_project(uid)
    dependencies = Dependencies(project)
    rows = read(dependencies, translation=True)
    for row in rows:
        dubbing_edit.save_selection(project, row, 'original', None)
    (root / 'audio').mkdir(exist_ok=True)
    vocal = wav_at(root / 'audio/vocal.wav', seconds=6)
    crud.add_artifact(project.id, 'preprocess', 'audio', 'vocal.wav', vocal, 'audio/vocal.wav')
    dependencies = Dependencies(crud.get_project(uid))
    spec = {key: value for key, value in version().items() if key != 'values'}
    spec.update(overrides={'assemble.subtitle_track': 'bilingual'})
    result = manifests.capture(dependencies, av.resolve(dependencies, spec))
    assert [(row['start'], row['audio_offset']) for row in result['audio']] == [(.5, .5), (3, 3)]
