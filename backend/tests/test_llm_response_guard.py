"""任务响应边界与持续退化检测；全部离线，不调用模型。"""
import pytest

from dubforge.backends import llm_guard
from dubforge.backends.llm_guard import ResponseGuard, ResponseGuardError


@pytest.fixture(autouse=True)
def isolated_guard_environment(isolated_env):
    """与领域测试保持数据根隔离。"""


def guard(**kwargs):
    return ResponseGuard(max_chars=30000, timeout_s=300, **kwargs)


@pytest.mark.parametrize('motif', ['Another identical instruction. ', '嗨，继续。 ', 'x'])
def test_long_continuous_repetition_stops_without_phrase_blacklist(motif):
    output = motif * 6000
    response = guard()
    with pytest.raises(ResponseGuardError) as error:
        for offset in range(0, len(output), 31):
            response.feed(output[offset:offset + 31])
    assert error.value.code == 'LLM_OUTPUT_DEGENERATED'
    assert response.counts['content'] < 9000
    assert len(error.value.detail['excerpt']) <= 1200


def test_repeated_source_dialogue_is_preserved_even_in_one_long_line():
    text = 'la la la ' * 1800
    response = guard(source_text=text)
    for character in text:
        response.feed(character)
    response.finish()
    assert response.counts['content'] == len(text)


def test_numbered_chants_and_similar_lines_are_not_continuous_generation_loops():
    response = guard()
    text = '\n'.join(f'{i}. Everybody sing together!' for i in range(1, 501))
    for offset in range(0, len(text), 7):
        response.feed(text[offset:offset + 7])
    response.finish()
    assert response.counts['content'] == len(text)


def test_complete_lines_only_and_final_line_without_newline():
    lines = []
    response = guard(line_check=lines.append)
    response.feed('1. hello\r')
    assert not lines
    response.feed('\n2. wor')
    assert lines == ['1. hello']
    response.feed('ld')
    response.finish()
    assert lines == ['1. hello', '2. world']


def test_reasoning_never_enters_body_protocol_checker():
    lines = []
    response = guard(line_check=lines.append)
    response.feed('Need to consider carefully.\n', channel='reasoning')
    response.feed('1. actual output')
    response.finish()
    assert lines == ['1. actual output']
    assert response.counts['reasoning'] > 0


def test_bad_completed_line_stops_before_later_body_is_received():
    def numbered(line):
        if not line.startswith('1. '):
            raise ValueError('正文应带编号')
    response = guard(line_check=numbered)
    response.feed('1. Good output\n')
    with pytest.raises(ResponseGuardError) as error:
        response.feed('Let me start another draft.\n')
    assert error.value.code == 'LLM_OUTPUT_CONTRACT'


def test_character_budget_stops_nonrepetitive_unfinished_line():
    response = ResponseGuard(max_chars=100, timeout_s=300)
    with pytest.raises(ResponseGuardError) as error:
        response.feed(''.join(str(i) + ' unique ' for i in range(100)))
    assert error.value.code == 'LLM_OUTPUT_BUDGET'


def test_reasoning_is_not_limited_by_character_budget():
    response = ResponseGuard(max_chars=20, reasoning_max_chars=1, timeout_s=300)
    response.feed('1. finished')
    response.feed(''.join(str(i) for i in range(100)), channel='reasoning')
    response.finish()
    assert response.counts['reasoning'] == 190


def test_total_deadline_expires_even_with_continuous_tokens(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(llm_guard.time, 'monotonic', lambda: now[0])
    response = ResponseGuard(max_chars=8000, timeout_s=20)
    for i in range(19):
        now[0] = float(i)
        response.feed(f'part {i} ')
    now[0] = 20.0
    with pytest.raises(ResponseGuardError) as error:
        response.check()
    assert error.value.code == 'LLM_REQUEST_DEADLINE'


def test_reasoning_is_still_stopped_by_total_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(llm_guard.time, 'monotonic', lambda: now[0])
    response = ResponseGuard(max_chars=20, timeout_s=20)
    response.feed('持续推理。' * 20_000, channel='reasoning')
    now[0] = 20.0
    with pytest.raises(ResponseGuardError) as error:
        response.feed('最后核对。', channel='reasoning')
    assert error.value.code == 'LLM_REQUEST_DEADLINE'


@pytest.mark.parametrize('seconds', [0, -1, float('inf'), float('nan')])
def test_request_limit_cannot_be_silently_disabled(seconds):
    with pytest.raises(ValueError):
        ResponseGuard(max_chars=8000, timeout_s=seconds)
