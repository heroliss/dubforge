"""任务277两轮整理稿的最小漏词回归；仅内存夹具，不读取真实项目。"""
import difflib

import pytest

from dubforge.pipeline import semantic_coverage as coverage
from dubforge.pipeline.step_split import _validate_source_coverage


# 2026-10-06失败轨迹中342.08–348.60秒的共同文本片段。
# 首轮候选保留了least之后的a，第二轮改分句时删除它；其声学锚点为343.50–343.84秒。
SOURCE = (
    'and last and not least a i stole a little pastry from our breakfast buffet '
    'this morning for us to snack on'
)
FIRST_CANDIDATE = [
    {'text': 'and last and not least a I stole a little pastry from our breakfast buffet this morning'},
    {'text': 'for us to snack on'},
]
SECOND_CANDIDATE = [
    {'text': 'and last and not least i stole a little pastry from our breakfast buffet this morning for us to snack on'},
]


@pytest.fixture(autouse=True)
def isolated_source_environment(isolated_env):
    """即使只校验内存文本，也沿用仓库的数据隔离约定。"""


def test_actual_candidate_difference_is_one_deleted_a_not_case_or_line_count():
    source = coverage._tokens(SOURCE)
    first = coverage._tokens(' '.join(part['text'] for part in FIRST_CANDIDATE))
    second = coverage._tokens(' '.join(part['text'] for part in SECOND_CANDIDATE))
    assert first == source
    changed = [op for op in difflib.SequenceMatcher(
        a=source, b=second, autojunk=False).get_opcodes() if op[0] != 'equal']
    assert changed == [('delete', 5, 6, 5, 5)]
    coverage.validate_source_preservation(FIRST_CANDIDATE, SOURCE)


def test_actual_suspended_a_is_audited_as_asr_cleanup_not_substantial_omission():
    _validate_source_coverage(SECOND_CANDIDATE, SOURCE)
    notes = coverage.validate_source_preservation(SECOND_CANDIDATE, SOURCE)
    assert len(notes) == 1
    assert '第6个词' in notes[0] and '（a）' in notes[0] and '悬空冠词' in notes[0]


def test_original_904th_word_is_locatable_with_bounded_long_window(monkeypatch):
    # 以无关占位词保留原来的位置和2400词规模，不把整片台词复制进测试仓库。
    prefix = ['lead'] * 898
    local = coverage._tokens(SOURCE)
    suffix = ['tail'] * (2400 - len(prefix) - len(local))
    source = ' '.join(prefix + local + suffix)
    candidate = ' '.join(prefix + coverage._tokens(SECOND_CANDIDATE[0]['text']) + suffix)
    monkeypatch.setattr(coverage.difflib, 'SequenceMatcher', lambda *a, **kw:
                        pytest.fail('长窗应使用有界前向校验'))
    notes = coverage.validate_source_preservation([{'text': candidate}], source)
    assert len(notes) == 1
    assert '第904个词' in notes[0] and '悬空冠词' in notes[0]


def test_second_candidate_fixed_farewell_tail_you_has_specific_cleanup_audit():
    source = "and that's it for this video see you in the next one you"
    candidate = "and that's it for this video see you in the next one"
    notes = coverage.validate_source_preservation([{'text': candidate}], source)
    assert len(notes) == 1
    assert '重复you' in notes[0]


def test_real_candidate_added_not_is_audited_without_blocking_model_correction():
    # 原词413.03–414.83秒没有not；文本本身无法判定新增否定是否修正了ASR。
    source = 'so far classic is still our favorite definitely durian'
    candidate = 'so far classic is still our favorite definitely not durian'
    notes = coverage.validate_source_preservation([{'text': candidate}], source)
    assert any('否定' in note for note in notes)
