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


@pytest.mark.parametrize('tail', ['the', 'i', 'you', 'and', 'we'])
def test_model_tail_noise_cleanup_is_accepted_without_changing_asr(
        make_project, pdir, monkeypatch, tail):
    words = [dict(word='We', start=0.0, end=.6),
             dict(word='arrived', start=.6, end=1.5),
             dict(word='safely.', start=1.5, end=3.0),
             dict(word=tail, start=3.0, end=3.15)]
    uid, ctx = _ctx(make_project, pdir,
        lines=[(0, 3.15, f'We arrived safely. {tail}')], words=words)
    original = pdir(uid) / 'srt/src.srt'
    snapshot = original.read_bytes()
    # 模拟模型基于上下文判断为噪声；程序只验收候选，不按词表自动删词。
    calls = _responses(monkeypatch, '1. 0.00-3.00: We arrived safely.')

    step_split.run(ctx)

    assert len(calls) == 1
    assert read_split(uid)['lines'] == [
        dict(id=1, text='We arrived safely.', start=0.0, end=3.0)]
    assert original.read_bytes() == snapshot


def test_reassigned_starter_keeps_its_word_time_and_original_asr(
        make_project, pdir, monkeypatch):
    tokens = [('We', 0, .4), ('arrived.', .4, 1), ('I', 1, 1.6),
              ('mean,', 1.6, 1.9), ('we', 1.9, 2.2), ('made', 2.2, 2.6), ('it.', 2.6, 3)]
    words = [dict(word=text, start=start, end=end) for text, start, end in tokens]
    uid, ctx = _ctx(make_project, pdir,
        lines=[(0, 1.6, 'We arrived. I'), (1.6, 3, 'mean, we made it.')], words=words)
    original = pdir(uid) / 'srt/src.srt'
    snapshot = original.read_bytes()
    calls = _responses(monkeypatch,
        '1. 0.00-1.00: We arrived.\n2. 1.00-3.00: I mean, we made it.')

    step_split.run(ctx)

    assert len(calls) == 1
    assert read_split(uid)['lines'] == [
        dict(id=1, text='We arrived.', start=0.0, end=1.0),
        dict(id=2, text='I mean, we made it.', start=1.0, end=3.0)]
    assert original.read_bytes() == snapshot


def test_uncertain_interrupted_tail_is_preserved_without_crossing_silence(
        make_project, pdir, monkeypatch):
    words = [dict(word='We', start=0, end=.4), dict(word='arrived.', start=.4, end=1),
             dict(word='I', start=1, end=2), dict(word='think', start=5, end=5.5),
             dict(word='we', start=5.5, end=6), dict(word='made', start=6, end=6.5),
             dict(word='it.', start=6.5, end=7)]
    uid, ctx = _ctx(make_project, pdir,
        lines=[(0, 2, 'We arrived. I'), (5, 7, 'think we made it.')], words=words)
    calls = _responses(monkeypatch,
        '1. 0.00-2.00: We arrived. I\n2. 5.00-7.00: think we made it.')

    step_split.run(ctx)

    assert len(calls) == 1
    assert read_split(uid)['lines'] == [
        dict(id=1, text='We arrived. I', start=0.0, end=2.0),
        dict(id=2, text='think we made it.', start=5.0, end=7.0)]


@pytest.mark.parametrize(('starter', 'rest'), [('I', 'got a Tsingtao.'), ('And', 'this one is comfy.')])
def test_complete_expression_survives_a_short_hesitation_without_local_word_split(
        make_project, pdir, monkeypatch, starter, rest):
    words = [dict(word=starter, start=0, end=.4)]
    tokens = rest.split()
    words.extend(dict(word=token, start=1.6 + i * .4, end=2.0 + i * .4)
                 for i, token in enumerate(tokens))
    end = words[-1]['end']
    uid, ctx = _ctx(make_project, pdir,
        lines=[(0, .4, starter), (1.6, end, rest)], words=words)
    original = pdir(uid) / 'srt/src.srt'
    snapshot = original.read_bytes()
    calls = _responses(monkeypatch, f'1. 0.00-{end:.2f}: {starter} {rest}')
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)['lines'] == [dict(id=1, text=f'{starter} {rest}', start=0.0, end=end)]
    assert original.read_bytes() == snapshot


def test_repeated_anchor_does_not_split_a_corrected_complete_phrase_in_the_final_pipeline(
        make_project, pdir, monkeypatch):
    words = [dict(word='I', start=0, end=1), dict(word='I', start=8, end=8.2),
             dict(word='think.', start=8.2, end=9)]
    uid, ctx = _ctx(make_project, pdir, lines=[(0, 1, 'I'), (8, 9, 'I think.')], words=words)
    original = pdir(uid) / 'srt/src.srt'
    snapshot = original.read_bytes()
    calls = _responses(monkeypatch, '1. 8.00-9.00: I think.')
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)['lines'] == [dict(id=1, text='I think.', start=8.0, end=9.0)]
    assert original.read_bytes() == snapshot
