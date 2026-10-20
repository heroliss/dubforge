"""文本规则与整句展示过滤：不调用 LLM，也不修改真实项目或配置。"""
import copy
import math

import pytest

from dubforge import content_review as review

pytestmark = pytest.mark.usefixtures('isolated_env')


def row(line_id=1, source='普通旅行介绍', target='普通旅行介绍', start=0.0, end=2.0, **extra):
    return dict(id=line_id, start=start, end=end, source_text=source, text=target, **extra)


@pytest.mark.parametrize('text', [
    '这里谈到了天安门的悲剧',
    '天安门广场的悲剧',
    '发生在天安门广场的那场悲剧',
    '天 安 门 广 场 的 悲 剧',
    'the tragedy of Tiananmen Square',
    'Tiananmen Square massacre',
    'Tiananmen Square was the site of a tragedy',
    'the massacre that happened at Tiananmen Square',
    'ＴＩＡＮＡＮＭＥＮ　ＳＱＵＡＲＥ　ＭＡＳＳＡＣＲＥ',
])
def test_context_rule_matches_specific_event_wording(text):
    reasons = review.blocked_comment(text)
    assert len(reasons) == 1
    assert '紧邻语境' in reasons[0]


@pytest.mark.parametrize('text', [
    '今天我们来到天安门广场拍照',
    'We are visiting Tiananmen Square for the architecture.',
    '历史纪念馆里记录了许多往事',
    'We visited the museum and learned about its history.',
    '天安门广场很漂亮，之后酒店发生火灾真是悲剧',
    'We visited Tiananmen Square. Our delayed flight was a tragedy.',
    'We visited Tiananmen Square and the hotel fire was a tragedy.',
    'We visited Tiananmen Square; the airport closure was a tragedy.',
    'Tiananmen is beautiful and I loved it, despite a tragedy in my family.',
])
def test_normal_tourism_and_unrelated_tragedy_do_not_match(text):
    assert review.blocked_comment(text) == []


def test_checks_both_languages_without_combining_unrelated_tracks():
    assert review.blocked_comment('the tragedy of Tiananmen Square', '普通旅游介绍')
    assert review.blocked_comment('普通旅游介绍', '天安门广场的悲剧')
    assert review.blocked_comment('Tiananmen Square', '这是一场悲剧') == []


def test_comment_platform_risk_phrases_are_screened_in_both_source_and_translation():
    source = ('That square is the most sensitive place in the country. Cameras are useful, '
              'especially at the square, a place where terrorism has actually happened.')
    translation = ('那个广场是全国最敏感的地方，摄像头很有用，尤其是在那个广场——一个确实发生过恐怖袭击的地方。')
    reasons = review.blocked_comment(source, translation)
    assert any('高敏感' in reason for reason in reasons)
    assert any('人工复核' in reason for reason in reasons)
    assert review.blocked_comment(source) and review.blocked_comment(translation)


@pytest.mark.parametrize('text', [
    '我们参观了广场，了解当地的建筑和历史。',
    'The square is beautiful at night and the cameras make the walk easy.',
    '这条评论讨论了敏感词检测，但没有描述敏感地点。',
    '报道介绍了反恐行动和恐怖袭击的受害者，呼吁和平。',
    'A museum exhibit explains how terrorism harms civilians.',
    'That square is the most sensitive place in the country, so we follow the security rules.',
])
def test_comment_risk_screen_does_not_block_normal_tourism_or_meta_discussion(text):
    assert review.blocked_comment(text) == []


def test_custom_terms_accept_newlines_nfkc_case_whitespace_and_duplicates():
    assert review.normalize_terms('  禁 止 词 \n\nＢＡＤ　ＷＯＲＤ\nbad word\n禁止词') == [
        '禁 止 词', 'ＢＡＤ　ＷＯＲＤ']
    assert review.blocked_comment('这里出现了禁\t止\n词', custom_terms='禁止词')
    assert review.blocked_comment('That is a BAD   WORD.', custom_terms=['bad word'])


def test_english_custom_word_does_not_match_inside_other_words():
    assert review.blocked_comment('This is a passage about a classic meal.', custom_terms=['ass']) == []
    assert review.blocked_comment('An ASS!', custom_terms=['ass'])


@pytest.mark.parametrize('terms', [123, ['valid', None], ['x' * 201],
                                  [f'term{i}' for i in range(501)]])
def test_invalid_custom_terms_have_stable_domain_error(terms):
    with pytest.raises(review.ContentReviewError) as exc:
        review.normalize_terms(terms)
    assert exc.value.code == 'CONTENT_REVIEW_SETTINGS'


def test_settings_accept_frozen_cfg_callable_and_nested_or_flat_mapping():
    for cfg in [
        lambda key, default=None: {'content_review.enabled': True,
                                  'content_review.blocked_terms': '词甲\n词乙'}.get(key, default),
        {'content_review': {'enabled': True, 'blocked_terms': ['词甲', '词乙']}},
        {'content_review.enabled': True, 'content_review.blocked_terms': '词甲\n词乙'},
    ]:
        assert review.settings_from_cfg(cfg) == (True, ['词甲', '词乙'])
    with pytest.raises(review.ContentReviewError, match='开关'):
        review.settings_from_cfg({'content_review': {'enabled': 'false'}})


def test_adjacent_context_marks_both_contributing_rows_and_preserves_ids():
    lines = [row(1, 'We remember the tragedy of', '天安门广场的', 0.0, 1.0),
             row(2, 'Tiananmen Square', '悲剧', 1.6, 2.6)]
    findings = review.review_lines(lines)
    assert [item['id'] for item in findings] == [1, 2]
    assert all(item['ids'] == [item['id']] for item in findings)
    assert all(item['context_ids'] == [1, 2] for item in findings)
    assert all(set(item['fields']) == {'source_text', 'text'} for item in findings)
    assert [(item['start'], item['end']) for item in findings] == [(0.0, 1.0), (1.6, 2.6)]


@pytest.mark.parametrize('next_id,next_start,first_text', [
    (2, 1.61, '天安门广场的'),
    (3, 1.2, '天安门广场的'),
    (2, 1.2, '天安门广场。'),
])
def test_distant_missing_or_sentence_separated_context_does_not_join(next_id, next_start, first_text):
    lines = [row(1, first_text, '', 0.0, 1.0),
             row(next_id, '悲剧', '', next_start, next_start + 1)]
    assert review.review_lines(lines) == []


def test_does_not_block_normal_neighbor_of_single_line_finding():
    lines = [row(1, '天安门广场的悲剧', '', 0.0, 1.0),
             row(2, '这里的建筑很漂亮', '', 1.0, 2.0)]
    findings = review.review_lines(lines)
    assert [item['id'] for item in findings] == [1]
    assert findings[0]['context_ids'] == [1]


def test_custom_term_can_span_only_two_adjacent_chinese_rows():
    lines = [row(1, '这里出现了屏蔽', '', 0.0, 1.0),
             row(2, '词组合', '', 1.1, 2.0)]
    assert [item['id'] for item in review.review_lines(lines, ['屏蔽词'])] == [1, 2]


def test_edited_display_cannot_hide_original_source_audio_and_timing():
    lines = [row(source='我们参观了广场', target='我们参观了广场', start=20.0, end=22.0,
                 voice_timing=[10.0, 12.0],
                 base={'source_text': 'the tragedy of Tiananmen Square', 'text': '',
                       'start': 10.0, 'end': 12.0})]
    finding = review.review_lines(lines)[0]
    assert finding['fields'] == ['source_audio']
    assert finding['start'] == 20.0 and finding['end'] == 22.0
    assert finding['source_audio_start'] == 10.0 and finding['source_audio_end'] == 12.0
    assert finding['source_audio_text'] == 'the tragedy of Tiananmen Square'


def test_original_source_audio_adjacent_context_uses_original_timing():
    lines = [row(1, '正常介绍', '正常介绍', 20.0, 22.0,
                 base={'source_text': 'the tragedy of', 'start': 10.0, 'end': 11.0}),
             row(2, '正常介绍', '正常介绍', 30.0, 32.0,
                 base={'source_text': 'Tiananmen Square', 'start': 11.2, 'end': 12.0})]
    findings = review.review_lines(lines)
    assert [item['id'] for item in findings] == [1, 2]
    assert all(item['fields'] == ['source_audio'] for item in findings)


def test_old_target_base_is_not_assumed_to_be_active_audio_or_caption():
    lines = [row(base={'source_text': '普通旅游介绍', 'text': '天安门广场的悲剧',
                       'start': 0.0, 'end': 2.0})]
    assert review.review_lines(lines) == []


def test_source_only_rows_are_reviewed_without_translation():
    lines = [dict(id=1, start=0.0, end=1.0, text='天安门广场的悲剧')]
    assert review.review_lines(lines)[0]['fields'] == ['text']


@pytest.mark.parametrize('start,end', [(-1.0, 1.0), (2.0, 1.0), (1.0, 1.0),
                                      (math.nan, 2.0), (0.0, math.inf), (True, 2.0), ('0', 1.0)])
def test_invalid_timeline_cannot_create_redaction_interval(start, end):
    with pytest.raises(review.ContentReviewError) as exc:
        review.review_lines([row(start=start, end=end)])
    assert exc.value.code == 'CONTENT_REVIEW_TIMELINE'


def test_invalid_original_audio_timing_and_duplicate_id_are_rejected():
    with pytest.raises(review.ContentReviewError, match='时间无效'):
        review.review_lines([row(voice_timing=[3.0, 2.0])])
    with pytest.raises(review.ContentReviewError, match='编号重复'):
        review.review_lines([row(), row()])
    with pytest.raises(review.ContentReviewError, match='顺序无效'):
        review.review_lines([row(1, start=2.0, end=3.0), row(2, start=0.0, end=1.0)])


def test_redaction_omits_entire_bilingual_row_without_mutating_original():
    lines = [row(1, 'the tragedy of Tiananmen Square', '天安门广场的悲剧', 0.0, 1.0),
             row(2, 'A lovely building', '漂亮的建筑', 1.0, 2.0,
                 base={'source_text': 'A lovely building', 'start': 1.0, 'end': 2.0})]
    original = copy.deepcopy(lines)
    exported = review.redact_lines(lines)
    assert [item['id'] for item in exported] == [2]
    assert lines == original
    exported[0]['base']['source_text'] = '修改返回副本'
    assert lines == original


def test_report_is_transparent_about_empty_disabled_and_text_only_scope():
    empty = review.review_report([])
    assert empty['checked_lines'] == 0 and empty['blocked_ids'] == []
    assert any('没有可检查' in warning for warning in empty['warnings'])
    assert empty['scope'] == 'text_only' and '画面' in empty['limitation']
    assert '保证' in empty['limitation'] and 'passed' not in empty
    lines = [row(source='天安门广场的悲剧')]
    disabled = review.review_report(lines, enabled=False)
    assert disabled['findings'] == []
    assert any('已关闭' in warning for warning in disabled['warnings'])
    report = review.review_report(lines, custom_terms='word')
    assert report['summary'] == {'lines': 1, 'blocked_lines': 1, 'blocked_comments': 0}
    assert report['mute_audio'] is True and report['blocked_terms'] == ['word']
    assert review.redact_lines(lines, enabled=False) == lines


def test_long_unrelated_context_is_not_joined_by_keyword_search():
    comment = 'Tiananmen Square ' + 'architecture ' * 10_000 + 'the tragedy of my family'
    assert review.blocked_comment(comment) == []
