"""适中语义单元和识别噪声处理：离线候选验收，不请求模型。"""
from copy import deepcopy

import pytest

from dubforge.pipeline import step_split
from dubforge.pipeline.semantic_quality import semantic_quality_issues
from dubforge.utils.srt import SubLine
from test_semantic_context_correction import _ctx, _responses, read_split


@pytest.fixture(autouse=True)
def isolated(isolated_env):
    """每个用例隔离数据，不修改生产字幕。"""


def test_prompt_asks_for_moderate_natural_units_without_minimum_length():
    lines = [SubLine(1, 0, 1, 'We arrived.'), SubLine(2, 4, 4.3, 'Yeah!')]
    prompt = step_split._coarse_resegment_prompt(lines, 20, 7, include_words=False)
    guidance, body = prompt.split('【全文时间轴输入】\n', 1)
    assert '长度适中、适合连贯配音' in guidance
    assert '一两词碎片优先就近归并' in guidance
    assert '不要为了不超上限切得过碎' in guidance
    assert '不为凑满长度堆成长句' in guidance
    assert '有明显间隔且有意义的短句、独立应答、感叹和真实口语中断保留' in guidance
    assert '无人说话时误识别出孤词' in guidance
    assert '无意义识别残留时直接清理，不为保留残词补造台词' in guidance
    assert '不按词表或仅凭位置、大小写、短时长删词' in guidance
    assert '未提供音频，不能声称核听原音' in guidance
    assert '10个词、7秒' in guidance and '作为软参考' in guidance
    assert '最少' not in guidance and '至少' not in guidance
    for line in lines:
        assert body.count(f'{line.start:.2f}-{line.end:.2f}: {line.text}') == 1


def test_custom_goals_remain_soft_and_full_document():
    lines = [SubLine(1, 0, 4, 'This is one natural sentence.')]
    prompt = step_split._coarse_resegment_prompt(lines, 20, 9, target_word_count=14,
                                                include_words=False)
    assert '14个词、9秒，作为软参考' in prompt
    assert '长度适中' in prompt
    assert prompt.count('【全文时间轴输入】') == 1


@pytest.mark.parametrize('word_mode', [False, True])
def test_short_anchor_exposes_zero_time_evidence_without_removing_input(word_mode):
    lines = [SubLine(1, 0, .4, 'The'), SubLine(2, 3, 4, 'We arrived.')]
    words = [dict(word='The', start=0, end=0), dict(word='We', start=3, end=3.4),
             dict(word='arrived.', start=3.4, end=4)]
    before = deepcopy(words)
    prompt = step_split._coarse_resegment_prompt(lines, 20, 7, words=words,
                                                include_words=word_mode)
    body = prompt.split('【全文时间轴输入】\n', 1)[1]
    assert '# 识别参考 0.00–0.40 秒：原始词轴全部为零时长' in body
    assert '仅作识别参考' in body
    assert 'The' in body and 'We' in body and 'arrived.' in body
    assert '已确认无声' not in body
    assert body.count('原始词轴全部为零时长') == 1
    assert words == before


def test_genuine_short_questions_answers_and_pauses_survive_final_pipeline(
        make_project, pdir, monkeypatch):
    lines = [(0, 1, 'What is it?'), (4, 4.2, 'Yeah!'), (7, 7.3, '11')]
    words = [dict(word=text, start=start, end=end) for text, start, end in [
        ('What', 0, .3), ('is', .3, .5), ('it?', .5, 1), ('Yeah!', 4, 4.2), ('11', 7, 7.3)]]
    uid, ctx = _ctx(make_project, pdir, lines=lines, words=words)
    source = pdir(uid) / 'srt/src.srt'
    original = source.read_bytes()
    calls = _responses(monkeypatch,
        '1. 0.00-1.00: What is it?\n2. 4.00-4.20: Yeah!\n3. 7.00-7.30: 11')
    step_split.run(ctx)
    assert len(calls) == 1
    assert read_split(uid)['lines'] == [dict(id=i, text=t, start=s, end=e)
                                       for i, (s, e, t) in enumerate(lines, 1)]
    assert source.read_bytes() == original


def test_model_can_clean_recognition_noise_without_mutating_original_asr(
        make_project, pdir, monkeypatch):
    tokens = ['We', 'finally', 'made', 'it', 'to', 'the', 'station.']
    words = [dict(word=text, start=i * .4, end=(i + 1) * .4)
             for i, text in enumerate(tokens)]
    words += [dict(word='The', start=3, end=3), dict(word='Yeah!', start=8, end=8.3)]
    uid, ctx = _ctx(make_project, pdir,
        lines=[(0, 2.8, 'We finally made it to the station.'), (3, 3.4, 'The'), (8, 8.3, 'Yeah!')],
        words=words)
    source = pdir(uid) / 'srt/src.srt'
    original = source.read_bytes()
    calls = _responses(monkeypatch,
        '1. 0.00-2.80: We finally made it to the station.\n2. 8.00-8.30: Yeah!')
    step_split.run(ctx)
    assert len(calls) == 1
    assert [row['text'] for row in read_split(uid)['lines']] == [
        'We finally made it to the station.', 'Yeah!']
    assert source.read_bytes() == original


def test_shortness_alone_never_rejects_or_rewrites_candidate():
    parts = [dict(text='pretty good', start=0, end=.4), dict(text='Yeah', start=3, end=3.2)]
    original = deepcopy(parts)
    assert semantic_quality_issues(parts, .6) == []
    assert parts == original
