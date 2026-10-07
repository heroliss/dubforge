"""模型软目标不机械重分句；关闭LLM的本地规则仍使用字符和时长上限。"""
from copy import deepcopy

import pytest

from dubforge.pipeline import step_split
from dubforge.pipeline.semantic_coverage import validate_source_preservation
from dubforge.utils.srt import SubLine


@pytest.fixture(autouse=True)
def isolated_source_environment(isolated_env):
    """测试只使用内存文本与声学夹具，不读取真实项目或请求模型。"""


WAYS = ("so there's a few different ways that you can enjoy pecking duck when you're in china "
        "or in beijing but the way we've decided to go about it is through pancakes")


def _word_timeline(text, start, duration):
    """合成不均匀词长，确保断点由声学词决定，而非字符比例均摊。"""
    tokens = text.split()
    weights = [.16 + .07 * (index % 4) for index in range(len(tokens))]
    total = sum(weights)
    cursor = start
    words = []
    for token, weight in zip(tokens, weights):
        end = cursor + duration * weight / total
        words.append(dict(word=token, start=cursor, end=end))
        cursor = end
    return words


@pytest.mark.parametrize('duration', [8.16, 19.5])
def test_long_complete_clauses_are_not_split_by_word_character_or_duration_goals(duration):
    # 真实失败句的最小文字片段；时间是隔离合成锚点，不借用生产候选的错误起点。
    start = 11.0
    words = _word_timeline(WAYS, start, duration)
    original = dict(id=7, text=WAYS, start=start, end=words[-1]['end'], provenance='candidate')
    frozen = deepcopy(original)
    assert step_split._wc(WAYS) > 100
    parts = step_split._enforce_final_limits([original], 20, 7, words, semantic=True)
    assert parts == [frozen]
    assert parts[0] is not original
    assert original == frozen
    assert validate_source_preservation(parts, WAYS) == []
    assert step_split._semantic_quality_issues(parts, .6) == []
    parts[0]['text'] = '副本编辑'
    assert original == frozen


def test_complete_chinese_part_is_preserved_even_far_above_duration_and_character_goals():
    text = '我们沿着河岸前行欣赏城市风景' * 5
    words = [dict(word=character, start=index * .5, end=(index + 1) * .5)
             for index, character in enumerate(text)]
    item = dict(text=text, start=0, end=35)
    parts = step_split._enforce_final_limits([item], 20, 7, words, semantic=True)
    assert parts == [item]
    assert parts[0] is not item
    assert validate_source_preservation(parts, text) == []


def test_prepare_keeps_one_complete_long_clause_and_real_word_times():
    words = _word_timeline(WAYS, 11, 8.16)
    start, end = words[0]['start'], words[-1]['end']
    candidate = [dict(text=WAYS, start=start, end=end)]
    warnings = []
    parts, aligned = step_split._prepare_semantic_parts(
        candidate, [SubLine(1, start, end, WAYS)], WAYS, words, 20, 7, .6,
        warnings=warnings)
    assert aligned is True
    assert len(parts) == 1
    assert parts[0]['text'] == WAYS
    assert parts[0]['start'] == pytest.approx(start)
    assert parts[0]['end'] == pytest.approx(end)
    assert warnings == []
    assert step_split._semantic_quality_issues(parts, .6) == []
    assert step_split._semantic_pause_issues(parts, words, .6) == []
    assert validate_source_preservation(parts, WAYS) == []


def test_local_rule_mode_keeps_strict_character_limit_for_the_same_long_sentence():
    parts = step_split._enforce_final_limits([dict(text=WAYS, start=0, end=8.16)], 50, 15)
    assert len(parts) == 4
    assert ''.join(part['text'] for part in parts) == WAYS
    assert all(step_split._wc(part['text']) <= 50 for part in parts)
    assert parts[0]['start'] == 0 and parts[-1]['end'] == 8.16


def test_local_rule_mode_keeps_strict_duration_limit_for_the_same_long_record():
    text = '我们沿着河岸前行欣赏城市风景' * 5
    parts = step_split._enforce_final_limits([dict(text=text, start=0, end=35)], 50, 15)
    assert len(parts) == 3
    assert ''.join(part['text'] for part in parts) == text
    assert all(part['end'] - part['start'] <= 15 for part in parts)
    assert parts[0]['start'] == 0 and parts[-1]['end'] == 35


@pytest.mark.parametrize('text', [
    ("Moving further into the room, we have the same espresso that we had at the last place, "
     "which I'm super excited to use. And then we have the amazing inside bed."),
    ("I'm going to grab some slices of cucumber, and a little bit of the whites of spring onion, "
     "and then I'm going to add in my slices of duck."),
])
def test_semantic_soft_targets_do_not_move_connectors_or_rewrite_the_candidate(text):
    words = _word_timeline(text, 4, 12)
    item = dict(text=text, start=4, end=16)
    parts = step_split._enforce_final_limits([item], 20, 7, words, semantic=True)
    assert parts == [item]
    assert validate_source_preservation(parts, text) == []


def test_private_rule_helper_compatibility_keeps_article_and_subject_on_the_right():
    # 此兼容 helper 的偏好不是模型生产路径的硬切逻辑。
    text = 'We are going to visit the market today and I think it is great'
    parts = step_split._rule_split(text, 22, semantic=True)
    assert ''.join(parts) == text
    assert [part.strip() for part in parts] == [
        'We are going to visit', 'the market today', 'and I think it is great']
    assert all(step_split._wc(part) <= 22 for part in parts)


def test_private_rule_helper_compatibility_avoids_isolating_the_next_subject():
    text = 'really nice and interesting things I think'
    parts = step_split._rule_split(text, 28, semantic=True)
    assert ''.join(parts) == text
    assert all(step_split._wc(part) <= 28 for part in parts)
    assert not any(part.strip().casefold().endswith(' i') for part in parts[:-1])
