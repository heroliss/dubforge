"""隔离假SSE复现快照重放、交错候选与终态；不调用真实模型。"""
import json

import pytest

from dubforge.backends import llm
from dubforge.errors import StepFailedError


@pytest.fixture(autouse=True)
def isolated(isolated_env):
    return isolated_env


class Stream:
    def __init__(self, events, *, done=True):
        self.events, self.done = events, done

    def iter_lines(self):
        for event in self.events:
            yield 'data: ' + json.dumps(event)
        if self.done:
            yield 'data: [DONE]'


def frame(*, content=None, reasoning=None, message=False, index=None, finish=None):
    part = {}
    if content is not None:
        part['content'] = content
    if reasoning is not None:
        part['reasoning_content'] = reasoning
    choice = {'message' if message else 'delta': part}
    if index is not None:
        choice['index'] = index
    if finish is not None:
        choice['finish_reason'] = finish
    return {'choices': [choice]}


def read(events, *, done=True, guard=None):
    traces = []
    value = llm._stream_content(Stream(events, done=done), name='隔离模型', request_id='fixture',
        guard=guard, trace=lambda event, content='', **meta: traces.append((event, content, meta)))
    return value, traces


@pytest.mark.parametrize('events,expected', [
    ([frame(content='A', message=True), frame(content='AB', message=True),
      frame(content='ABC', message=True, finish='stop')], 'ABC'),
    ([frame(content='ANSWER'), frame(content='ANSWER', message=True, finish='stop')], 'ANSWER'),
    ([frame(content='A', message=True), frame(content='B'), frame(content='ABC', message=True,
      finish='stop')], 'ABC'),
    ([{'choices': [{'delta': {'content': 'A'}, 'message': {'content': 'AB'}}]},
      frame(content='ABC', message=True, finish='stop')], 'ABC'),
    ([frame(content='again '), frame(content='again '), frame(content='again ', finish='stop')],
     'again again again '),
])
def test_message_snapshots_do_not_replay_but_equal_deltas_remain_real(events, expected):
    value, traces = read(events)
    assert value == expected
    assert ''.join(text for event, text, _ in traces if event == 'assistant_delta') == expected


def test_cumulative_reasoning_is_independent_of_final_text():
    value, traces = read([frame(reasoning='THOUGHT', message=True),
        frame(reasoning='THOUGHT MORE', message=True),
        frame(content='ANSWER', reasoning='THOUGHT MORE', message=True, finish='stop')])
    assert value == 'ANSWER'
    assert ''.join(text for event, text, _ in traces if event == 'reasoning_delta') == 'THOUGHT MORE'
    assert next(meta for event, _, meta in traces if event == 'response_metadata')['reasoning_chars'] == 12


def test_conflicting_snapshot_is_not_guessed_or_fuzzily_merged():
    with pytest.raises(StepFailedError, match='快照'):
        read([frame(content='first'), frame(content='changed', message=True, finish='stop')])


def test_interleaved_candidates_always_choose_index_zero():
    value, traces = read([
        {'choices': [{'index': 1, 'delta': {'content': 'WRONG'}},
                     {'index': 0, 'delta': {'content': 'A'}}]},
        frame(content='IGNORED', index=1, finish='length'),
        {'choices': [{'index': 0, 'delta': {'content': 'B'}, 'finish_reason': 'stop'},
                     {'index': 1, 'delta': {'content': 'WRONG'}}]},
    ])
    assert value == 'AB'
    metadata = next(meta for event, _, meta in traces if event == 'response_metadata')
    assert metadata['selected_choice'] == 0 and metadata['choice_count'] == 2


@pytest.mark.parametrize('choices', [
    [{'delta': {'content': 'A'}}, {'delta': {'content': 'B'}}],
    [{'index': 0, 'delta': {'content': 'A'}}, {'index': 0, 'delta': {'content': 'B'}}],
    [{'index': '0', 'delta': {'content': 'A'}}],
])
def test_ambiguous_candidate_identity_is_rejected(choices):
    with pytest.raises(StepFailedError) as error:
        read([{'choices': choices}])
    assert error.value.code == 'LLM_RESPONSE_INVALID'


def test_eof_does_not_turn_partial_text_into_completed_response():
    with pytest.raises(StepFailedError) as error:
        read([frame(content='PARTIAL')], done=False)
    assert error.value.code == 'LLM_RESPONSE_INCOMPLETE'


@pytest.mark.parametrize('done,finish', [(True, None), (False, 'stop')])
def test_either_explicit_done_or_finish_can_complete_compatible_stream(done, finish):
    assert read([frame(content='FULL', finish=finish)], done=done)[0] == 'FULL'


def test_usage_and_identical_final_snapshot_after_stop_are_allowed():
    value, traces = read([frame(content='FULL', finish='stop'),
                         frame(content='FULL', message=True, finish='stop'),
                         {'choices': [], 'usage': {'completion_tokens': 2}}])
    assert value == 'FULL'
    metadata = next(meta for event, _, meta in traces if event == 'response_metadata')
    assert metadata['stream_mode'] == 'mixed' and metadata['done_received'] is True
    assert metadata['terminal_received'] is True and metadata['usage']['completion_tokens'] == 2


@pytest.mark.parametrize('last', [frame(content='EXTRA'), frame(content='FULL EXTRA', message=True)])
def test_new_content_after_terminal_is_rejected(last):
    with pytest.raises(StepFailedError, match='结束后'):
        read([frame(content='FULL', finish='stop'), last])


def test_length_terminal_cannot_be_hidden_by_later_stop():
    with pytest.raises(StepFailedError) as error:
        read([frame(content='PARTIAL', finish='length'), frame(finish='stop')])
    assert error.value.code == 'LLM_OUTPUT_TRUNCATED'


@pytest.mark.parametrize('plain', [False, True])
def test_typed_thinking_blocks_are_separate_and_text_blocks_compatible(plain):
    blocks = [{'type': 'reasoning', 'text': 'FIRST THOUGHT'},
              {'type': 'thinking', 'thinking': 'SECOND THOUGHT'},
              {'type': 'output_text', 'text': 'A'}, {'text': 'B'}, {'type': 'text', 'text': 'C'}]
    if plain:
        traces = []
        value = llm._plain_content({'choices': [{'message': {'content': blocks}, 'finish_reason': 'stop'}]},
            trace=lambda event, content='', **meta: traces.append((event, content, meta)))
    else:
        value, traces = read([frame(content=blocks, finish='stop')])
    assert value == 'ABC'
    assert ''.join(text for event, text, _ in traces if event == 'reasoning_delta') == 'FIRST THOUGHTSECOND THOUGHT'


@pytest.mark.parametrize('plain', [False, True])
def test_unknown_blocks_cannot_become_text_even_if_they_have_text_field(plain):
    blocks = [{'type': 'tool_call', 'text': 'FAKE ANSWER'}]
    with pytest.raises(StepFailedError) as error:
        if plain:
            llm._plain_content({'choices': [{'message': {'content': blocks}}]})
        else:
            read([frame(content=blocks, finish='stop')])
    assert error.value.code == 'LLM_RESPONSE_INVALID'


def test_guard_receives_only_real_increments_in_each_channel():
    class Guard:
        def __init__(self): self.pieces, self.finished = [], False
        def check(self): pass
        def feed(self, piece, channel='content'): self.pieces.append((channel, piece))
        def finish(self): self.finished = True
    guard = Guard()
    assert read([frame(content='A', reasoning='THOUGHT'),
                 frame(content='AB', reasoning='THOUGHT MORE', message=True, finish='stop')],
                guard=guard)[0] == 'AB'
    assert guard.finished
    assert ''.join(piece for channel, piece in guard.pieces if channel == 'content') == 'AB'
    assert ''.join(piece for channel, piece in guard.pieces if channel == 'reasoning') == 'THOUGHT MORE'
