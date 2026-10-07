"""上下文纠错走完整整理链；不调用模型，不写生产产物。"""
import pytest

from dubforge.pipeline import step_split
from dubforge.utils.srt import SubLine
from test_split_semantic_quality import _ctx, _responses
from test_text_chain import read_split


@pytest.mark.parametrize(('source', 'corrected'), [
    ('Our train ticket in China cost 10 yen.',
     'Our train ticket in China cost 10 yuan.'),
    ('The train leaves in fifteen minutes.',
     'The train leaves in fifty minutes.'),
    ('We would go to the station.',
     "We won't go to the station."),
    ('kwa jude serves pecking duck.',
     'Quanjude serves Peking duck.'),
])
def test_context_correction_is_kept_without_retry_or_original_artifact_change(
        make_project, pdir, monkeypatch, source, corrected):
    words = [dict(word=token, start=i, end=i + 1)
             for i, token in enumerate(source.split())]
    duration = float(len(words))
    uid, ctx = _ctx(make_project, pdir, lines=[(0, duration, source)], words=words,
                    max_len=80)
    original = pdir(uid) / 'srt/src.srt'
    snapshot = original.read_bytes()
    calls = _responses(monkeypatch, f'1. 0.00-{duration:.2f}: {corrected}')

    step_split.run(ctx)

    assert len(calls) == 1
    assert read_split(uid)['lines'] == [
        dict(id=1, text=corrected, start=0.0, end=duration)]
    assert original.read_bytes() == snapshot
    assert '结合全文语境和常识' in calls[0][0]
    assert (pdir(uid) / 'logs/split.log').is_file()


def test_negation_correction_keeps_already_separated_real_silence(
        make_project, pdir, monkeypatch):
    source = [SubLine(1, 0, 1, 'would'), SubLine(2, 8, 9, 'go')]
    words = [dict(word='would', start=0.0, end=1.0),
             dict(word='go', start=8.0, end=9.0)]
    uid, ctx = _ctx(make_project, pdir,
                    lines=[(line.start, line.end, line.text) for line in source])
    calls = _responses(monkeypatch, "1. 0.00-1.00: won't\n2. 8.00-9.00: go")

    final = step_split._llm_resegment(ctx, source, 50, 15, 2, words)

    assert len(calls) == 1
    assert final == [dict(text="won't", start=0.0, end=1.0),
                     dict(text='go', start=8.0, end=9.0)]
    assert '复核' in (pdir(uid) / 'logs/split.log').read_text(encoding='utf-8')
