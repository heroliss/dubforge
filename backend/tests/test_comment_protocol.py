"""评论逐行协议的转义、局部补齐与编号隔离。"""
import pytest

from dubforge import comment_studio as cs
from dubforge.utils import comment_protocol as protocol
from test_comment_studio import uid, seed
from test_comment_diagnostics import model


@pytest.mark.parametrize('text', ['中文 🎉\n下一行', r'字面量\n不是换行', r'C:\new\text',
                                 'a\\\nb\\\\\nc', '含 | 分隔符、引号 " 和 {括号}', '末尾反斜杠\\'])
def test_text_escape_round_trip(text):
    encoded = protocol.escape_text(text)
    assert '\n' not in encoded and '\r' not in encoded
    assert protocol.unescape_text(encoded) == text


def test_crlf_and_cr_normalize_to_literal_newline():
    assert protocol.escape_text('甲\r\n乙\r丙') == r'甲\n乙\n丙'
    assert protocol.escape_text('甲\u2028乙\u2029丙') == r'甲\n乙\n丙'


def test_plain_request_omits_long_ids_and_keeps_all_context_one_line_each():
    request = protocol.request_text([dict(id='YouTube-long-id', text='第一行\n2. 第二行')],
        {'YouTube-long-id': 7}, {'criteria': '真实\n体验', 'video_context': {'title': '视频', 'description': '内容\n说明'}})
    assert 'YouTube-long-id' not in request and '{' not in request
    assert request.endswith(r'7. 第一行\n2. 第二行')
    assert '简介：内容\\n说明' in request


def test_translation_uses_exact_number_mapping_and_retains_duplicates():
    result = protocol.parse_response('```text\n7. 第一行\\n第二行\n7. 重复\n9. 未请求\n```', {7: 'platform-id'}, 'translate')
    assert [r['id'] for r in result['translations']] == ['platform-id', 'platform-id']
    assert result['translations'][0]['text_zh'] == '第一行\n第二行'
    assert result['_protocol_errors']
    with pytest.raises(ValueError, match='重复'):
        cs._validate_result(result, [{'id': 'platform-id'}], 'translate')


@pytest.mark.parametrize('score', ['nan', 'inf', '-1', '101', '百分之八十'])
def test_invalid_scores_not_invented(score):
    result = protocol.parse_response(f'1. {score} | 是 | 理由', {1: 'a'}, 'filter')
    with pytest.raises(ValueError, match='分数'):
        cs._validate_result(result, [{'id': 'a'}], 'filter')


def test_scoring_allows_pipe_in_reason_and_unescapes_only_defined_sequences():
    result = protocol.parse_response(r'1、 82.5 | 否 | 有 | 内容\n但是不符合\t要求', {1: 'a'}, 'filter')
    cs._validate_result(result, [{'id': 'a'}], 'filter')
    assert result['assessments'] == [dict(id='a', score=82.5, eligible=False, reason='有 | 内容\n但是不符合\\t要求')]


def test_raw_newline_must_not_save_truncated_translation():
    result = protocol.parse_response('1. 第一行\n第二行\n2. 完整译文', {1: 'a', 2: 'b'}, 'translate')
    with pytest.raises(ValueError, match='裸换行'):
        cs._validate_result(result, [{'id': 'a'}, {'id': 'b'}], 'translate')
    assert '_line_error' not in result['translations'][1]


def test_partial_translation_repairs_only_bad_line_and_preserves_literal_slash(uid, monkeypatch):
    seed(uid, 3)
    replies = ['1. 第一行\n漏转义的第二行\n2. 完整译文\n3. 字面量\\\\n与换行\\n并存',
               r'1. 第一行\n漏转义的第二行']
    calls = model(monkeypatch, lambda *_: replies.pop(0))
    state = cs.run(uid, 'translate')
    assert [row['id'] for row in calls[1]] == ['1']
    assert state['job']['processed'] == 3
    assert state['rows'][0]['text_zh'] == '第一行\n漏转义的第二行'
    assert state['rows'][2]['text_zh'] == '字面量\\n与换行\n并存'


def test_malformed_duplicate_is_not_accepted_as_unique(uid, monkeypatch):
    replies = ['1. 90 | 是 | 很好\n1. 坏行\n2. 70 | 是 | 可以\n3. 20 | 否 | 不符合',
               '1. 88 | 是 | 有共鸣']
    calls = model(monkeypatch, lambda *_: replies.pop(0))
    result = cs.run(uid, 'filter', prompt='观众反馈')
    assert [row['id'] for row in calls[1]] == ['1']
    assert next(row for row in result['filter']['assessments'] if row['id'] == '0')['score'] == 88


def test_comment_protocol_overrides_json_response_format_without_changing_settings(uid, monkeypatch):
    from dubforge import config
    from dubforge.backends import llm
    config.set_key('llm.profiles', [dict(name='test', model='test', params={'response_format': {'type': 'json_object'}, 'top_p': .8})])
    def chat(prompt, **kwargs):
        assert kwargs['profile'][0]['params'] == {'top_p': .8}
        return '1. 80 | 是 | 很好\n2. 80 | 是 | 很好\n3. 80 | 是 | 很好'
    monkeypatch.setattr(llm, 'chat', chat)
    cs.run(uid, 'filter', prompt='观众反馈')
    assert config.get('llm.profiles')[0]['params']['response_format'] == {'type': 'json_object'}
