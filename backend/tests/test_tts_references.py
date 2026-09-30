"""真实切片验证：短句动态参考不能悄悄借用视频开头的声音。"""
import numpy as np
import pytest
import soundfile as sf

from test_dubbing_preparation import index_prepared, prepared
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.step_tts import _resolve_ref, synth_line
from dubforge.utils.fs import atomic_replace


def vocal_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.concatenate([np.full(32000 * 8, .25), np.full(32000 * 5, -.25)])
    tmp = path.with_suffix('.wav.tmp')
    sf.write(tmp, samples, 32000, format='WAV')
    atomic_replace(tmp, path)


@pytest.mark.parametrize('tag', ['timbre', 'emo'])
@pytest.mark.parametrize('seconds', [.3, 1.84, 2.5])
def test_dynamic_uses_exact_sentence_even_when_short(tmp_path, tag, seconds):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    logs = []
    line = {'id': 9, 'start': 10, 'end': 10 + seconds}
    path = _resolve_ref(None, 'dynamic', '', line, source, tmp_path / 'refs', tag, logs.append)
    samples, rate = sf.read(path)
    assert len(samples) / rate == pytest.approx(seconds, abs=.01)
    assert samples.mean() == pytest.approx(-.25, abs=.001)
    assert path.name == f'ref_{tag}_9.wav'
    assert '本句 10.00' in logs[-1]
    assert ('不足 2 秒' in logs[-1]) == (seconds < 2)


def test_stable_explicitly_uses_start_of_video(tmp_path):
    source = tmp_path / 'vocal.wav'
    vocal_file(source)
    path = _resolve_ref(None, 'stable', '', {'id': 1}, source, tmp_path / 'refs', 'timbre')
    samples, rate = sf.read(path)
    assert len(samples) / rate == 8
    assert samples.mean() == pytest.approx(.25, abs=.001)


def test_sentence_references_reach_model_separately(index_prepared):
    client, uid, received = index_prepared
    client.put(f'/api/projects/{uid}/step_params', json={'updates': {
        'tts.indextts25.timbre_mode': 'dynamic', 'tts.indextts25.emo_mode': 'dynamic',
        'tts.indextts25.duration_retry_factors': ''}})
    project = crud.get_project(uid)
    root = crud.project_dir(uid)
    vocal_file(root / 'audio/vocal.wav')
    line = {**Dependencies(project).dubbing_lines()[0], 'start': 10, 'end': 10.5}
    synth_line(project, line, root)
    args = received[-1]
    for key in ('timbre_ref', 'emo_ref'):
        samples, rate = sf.read(args[key])
        assert len(samples) / rate == .5
        assert samples.mean() == pytest.approx(-.25, abs=.001)
    assert args['timbre_ref'] != args['emo_ref']
