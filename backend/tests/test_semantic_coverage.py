"""整理稿允许全文纠错、内容变化留复核记录；夹具不读取项目或调用模型。"""
import pytest

from dubforge.pipeline import semantic_coverage as coverage


@pytest.fixture(autouse=True)
def isolated_source_environment(isolated_env):
    """沿用仓库数据隔离约定，即使本模块只检查内存文本。"""


def validate(source, *parts):
    return coverage.validate_source_preservation([dict(text=text) for text in parts], source)


@pytest.mark.parametrize('source,output', [
    ('one really cool thing about taking the train in china', 'really cool thing about taking the train in china'),
    ('They are just scaring me before we leave.', 'They are before we leave.'),
    ('It cost 100 dollars for two nights.', 'It cost 100 for two nights.'),
    ('It costs $100.', 'It costs 100.'),
    ('It costs €100.', 'It costs 100.'),
    ('The rate is 100%.', 'The rate is 100.'),
    ('The angle is 100°.', 'The angle is 100.'),
    ('We leave just scaring me', 'We leave'),
])
def test_local_content_cleanup_is_audited_instead_of_rejecting_the_document(source, output):
    notes = validate(source, output)
    assert notes and any('→' in note and '原文' in note for note in notes)


@pytest.mark.parametrize('source,output', [
    ('Helo world', 'Hello world'),
    ('We visited Chaina yesterday.', 'We visited China yesterday.'),
    ('We took a quiet street.', 'We took a calm street.'),
    ('这是一条安静的路。', '这是一条平静的路。'),
    ('We use go pro here', 'We use GoPro here'),
    ('We use GoPro here', 'We use go pro here'),
    ('foo bar foo', 'foobar foo'),
    ('This doesnt end here.', 'This doesn’t end here!'),
])
def test_local_asr_substitution_join_and_split_remain_allowed(source, output):
    validate(source, output)


@pytest.mark.parametrize('source,output', [
    ('the the train goes there', 'the train goes there'),
    ('the train train goes there', 'the train goes there'),
    ('we go there there there', 'we go there'),
])
def test_adjacent_repeated_recognition_word_can_be_conservatively_removed(source, output):
    validate(source, output)


@pytest.mark.parametrize('source,output', [
    ('we take the train and the bus', 'we take the train and bus'),
    ('we go there and there', 'we go there'),
    ('人人都喜欢这里', '人都喜欢这里'),
])
def test_nonadjacent_skip_and_cjk_reduplication_warn_without_claiming_proven_repeat(source, output):
    notes = validate(source, output)
    assert len(notes) == 1
    assert '少量ASR原词未保留' in notes[0] and '同词识别重复' not in notes[0]


@pytest.mark.parametrize('source,output', [
    ('It costs 100 dollars.', 'It costs 200 dollars.'),
    ('It costs 100 dollars.', 'It costs dollars.'),
    ('Stop 10 comes before stop 20.', 'Stop 20 comes before stop 10.'),
    ('We paid 2 3 dollars.', 'We paid 23 dollars.'),
])
def test_numbers_can_be_contextually_corrected_with_before_after_audit(source, output):
    notes = validate(source, output)
    assert any('数字' in note and '→' in note and '原文约第' in note for note in notes)


@pytest.mark.parametrize('source,output', [
    ('It costs 1000 dollars.', 'It costs 1,000 dollars.'),
    ('It costs 1,000 dollars.', 'It costs 1000 dollars.'),
    ('It costs 1,000.00 dollars.', 'It costs 1000 dollars.'),
    ('It costs 2.50 dollars.', 'It costs 2.5 dollars.'),
    ('It costs ２．５０ dollars.', 'It costs 2.5 dollars.'),
    ('The temperature is −2.50 degrees.', 'The temperature is -2.5 degrees.'),
])
def test_explicit_equivalent_number_formatting_is_allowed(source, output):
    validate(source, output)


@pytest.mark.parametrize('source,output', [
    ('The number is 2.', 'The number is -2.'),
    ('The number is 2.', 'The number is −2.'),
    ('It costs 2,50 dollars.', 'It costs 2.50 dollars.'),
    ('The range is 2-3.', 'The range is 2 3.'),
    ('The range is 2−3.', 'The range is 2 3.'),
    ('The date is 2026-10-06.', 'The date is 2026-10-6.'),
    ('We counted one item.', 'We counted two items.'),
    ('We counted one item.', 'We counted 1 item.'),
    ('We counted 1 item.', 'We counted one item.'),
])
def test_number_signs_ambiguous_formats_and_number_words_are_not_silently_changed(source, output):
    assert any('数字' in note and '→' in note for note in validate(source, output))


@pytest.mark.parametrize('source,output', [
    ('We enjoyed China and then Taiwan.', 'We enjoyed Taiwan and then China.'),
    ('We enjoyed China and then Taiwan.', 'We enjoyed Taiwan and then China recently.'),
    ('alpha beta gamma', 'gamma beta alpha'),
])
def test_short_word_order_or_entity_role_changes_are_audited_not_assumed_wrong(source, output):
    notes = validate(source, output)
    assert notes and any('→' in note and '原文第' in note for note in notes)


def test_multiple_candidate_lines_preserve_case_punctuation_and_word_boundaries():
    validate('HELLO, world! We paid １００ dollars.', 'hello WORLD.', 'We paid 100 dollars!')
    validate('It costs ￥100.', 'It costs ¥100.')


@pytest.mark.parametrize('source,output', [
    ('I did not enjoy China.', 'I did enjoy China.'),
    ('I enjoyed China.', 'I did not enjoy China.'),
    ('I never paid.', 'I paid.'),
    ('There is no fee.', 'There is a fee.'),
    ('Nobody paid.', 'Everybody paid.'),
    ('We went without tickets.', 'We went with tickets.'),
    ("We don't go there.", 'We go there.'),
    ('We dont go there.', 'We do go there.'),
    ("We can't go there.", 'We can go there.'),
    ('We cant go there.', 'We can go there.'),
    ('We cannot go there.', 'We can go there.'),
    ("They aren't ready.", 'They are ready.'),
    ("He wouldn't pay.", 'He would pay.'),
    ("It isn't available.", 'It is available.'),
    ("She hasn't left.", 'She has left.'),
    ("We couldn't wait.", 'We could wait.'),
    ('I do not like Beijing.', 'I cannot like Beijing.'),
])
def test_english_negation_can_be_corrected_both_ways_with_visible_audit(source, output):
    assert any('否定' in note and '→' in note for note in validate(source, output))


@pytest.mark.parametrize('source,output', [
    ("I don't go.", 'I do not go.'),
    ('I dont go.', 'I don’t go.'),
    ("We can't go.", 'We cannot go.'),
    ('We cannot go.', 'We can not go.'),
    ("It isn't available.", 'It is not available.'),
])
def test_unambiguous_negation_punctuation_and_contraction_expansion_are_allowed(source, output):
    assert validate(source, output) == []


@pytest.mark.parametrize('source,output,reason', [
    ('um we boarded', 'we boarded', '口头填充'),
    ('we uh boarded', 'we boarded', '口头填充'),
    ('we boarded erm', 'we boarded', '口头填充'),
    ('er we boarded', 'we boarded', '口头填充'),
    ('and last and not least a I stole pastry', 'and last and not least I stole pastry', '悬空冠词'),
    ('a we boarded the train', 'we boarded the train', '悬空冠词'),
    ('an I boarded the train', 'I boarded the train', '悬空冠词'),
    ('a the train arrived', 'the train arrived', '悬空冠词'),
    ('the a train arrived', 'a train arrived', '悬空冠词'),
    ('the train train arrived', 'the train arrived', '同词识别重复'),
])
def test_evidenced_local_cleanup_has_a_position_and_reason(source, output, reason):
    notes = validate(source, output)
    assert len(notes) == 1
    assert '原文第' in notes[0] and reason in notes[0]


@pytest.mark.parametrize('source,output', [
    ('We visited The Hague.', 'We visited Hague.'),
    ('I received grade A.', 'I received grade.'),
    ('I received grade A I scored well.', 'I received grade I scored well.'),
    ('The ER treats patients.', 'The treats patients.'),
    ('The you I know is kind.', 'You I know is kind.'),
    ('I was just scaring them.', 'I was them.'),
    ('I was just scaring them.', 'I was scaring them.'),
    ('I like China.', 'I China.'),
    ('We took the train to China.', 'We took the train to.'),
    ('very very good', 'very good'),
    ('really really good', 'really good'),
    ('Duran Duran performed.', 'Duran performed.'),
    ('duran duran performed.', 'duran performed.'),
    ('Bora Bora is beautiful.', 'Bora is beautiful.'),
    ('bye bye', 'bye'),
    ('we saw you', 'we saw'),
    ('see you in the next one you will enjoy', 'see you in the next one will enjoy'),
])
def test_uncertain_single_words_names_and_emphasis_warn_instead_of_claiming_semantic_error(source, output):
    notes = validate(source, output)
    assert len(notes) == 1
    assert '原文第' in notes[0] and '无法仅凭文本确认' in notes[0]
    assert '不代表语义正确' in notes[0]


def test_cleanup_budget_counts_all_deleted_tokens_and_can_be_scaled_for_stitched_windows():
    source = 'um we boarded uh the train erm and arrived er safely'
    output = 'we boarded the train and arrived safely'
    assert any('超过参考3词' in note for note in validate(source, output))
    notes = coverage.validate_source_preservation([{'text': output}], source, cleanup_budget=4)
    assert len(notes) == 4
    assert any('超过参考0词' in note for note in coverage.validate_source_preservation(
        [{'text': 'we boarded'}], 'um we boarded', cleanup_budget=0))


def test_audit_note_cap_does_not_stop_counting_later_cleanup_tokens():
    source = ' '.join(f'um station{i} arrived' for i in range(10))
    output = ' '.join(f'station{i} arrived' for i in range(10))
    assert len(coverage.validate_source_preservation(
        [{'text': output}], source, cleanup_budget=10)) == 8
    notes = coverage.validate_source_preservation([{'text': output}], source, cleanup_budget=9)
    assert len(notes) == 8 and '超过参考9词' in notes[-1]


@pytest.mark.parametrize('source,output', [
    ('we missed the bus and booked a room', 'we missed the bus we missed the bus and booked a room'),
    ('China welcomed travelers and Beijing amazed visitors',
     'China welcomed travelers and Beijing amazed visitors China welcomed travelers'),
    ('we booked a room and walked outside',
     'we booked a room and walked outside we booked a room'),
])
def test_added_copy_of_substantial_source_phrase_is_rejected(source, output):
    with pytest.raises(ValueError, match='重复的原文片段'):
        validate(source, output)


@pytest.mark.parametrize('source,output', [
    ('It was very good very good indeed.', 'It was very good, very good indeed!'),
    ('we booked a room and we booked a room again', 'we booked a room. And we booked a room again.'),
    ('we went home', 'we went home and we are happy'),
])
def test_original_emphasis_and_unrelated_short_asr_repair_are_not_duplicate_hallucination(source, output):
    validate(source, output)


@pytest.mark.parametrize('source,output', [
    ('我喜欢北京', '我不喜欢北京'),
    ('我不喜欢北京', '我喜欢北京'),
    ('我没有车', '我拥有车'),
    ('我不能去', '我能去'),
    ('我会去', '我不会去'),
    ('这不是答案', '这是答案'),
    ('我去北京', '我不去北京'),
    ('我没去北京', '我去北京'),
    ('我去北京', '我未去北京'),
    ('我有车', '我无车'),
    ('我无车', '我有车'),
    ('你去北京', '你别去北京'),
])
def test_common_cjk_negation_phrases_and_standalone_edits_are_audited(source, output):
    assert any('否定' in note and '→' in note for note in validate(source, output))


@pytest.mark.parametrize('source,output', [
    ('我喜欢不丹', '我喜欢不丹'),
    ('我喜欢无锡', '我喜欢无锡'),
    ('丹是一个国家', '不丹是一个国家'),
    ('锡是一座城市', '无锡是一座城市'),
    ('不丹很美丽', '不丹很漂亮'),
    ('无锡很美丽', '无锡很漂亮'),
])
def test_cjk_place_names_do_not_supply_spurious_negation(source, output):
    assert not any('否定' in note for note in validate(source, output))


@pytest.mark.parametrize('source,output', [
    ('我喜欢不丹', '我喜欢丹'),
    ('我喜欢无锡', '我喜欢锡'),
])
def test_uncertain_place_name_character_deletion_warns_without_misclassifying_negation(source, output):
    notes = validate(source, output)
    assert len(notes) == 1
    assert '无法仅凭文本确认' in notes[0]


@pytest.mark.parametrize('omit_position', ['middle', 'tail'])
def test_long_repeated_source_uses_bounded_algorithm_and_audits_local_phrase_cleanup(monkeypatch, omit_position):
    def unbounded(*args, **kwargs):
        pytest.fail('超过2000词不得进入SequenceMatcher')
    monkeypatch.setattr(coverage.difflib, 'SequenceMatcher', unbounded)
    source = ['alpha', 'beta', 'gamma'] * 900
    position = 1350 if omit_position == 'middle' else len(source)
    source[position:position] = ['just', 'scaring', 'me']
    output = source[:position] + source[position + 3:]
    notes = validate(' '.join(source), ' '.join(output))
    assert any('just scaring me' in note and '→ ∅' in note for note in notes)


def test_long_source_allows_bounded_join_correction_and_adjacent_repeat(monkeypatch):
    monkeypatch.setattr(coverage.difflib, 'SequenceMatcher', lambda *a, **k:
                        pytest.fail('长稿不能进入无界diff'))
    source = ['alpha', 'beta', 'gamma'] * 900 + ['go', 'pro', 'train', 'train']
    output = ['alpha', 'beta', 'gamma'] * 900 + ['gopro', 'train']
    validate(' '.join(source), ' '.join(output))


def test_long_source_number_cleanup_is_audited_without_unbounded_diff(monkeypatch):
    monkeypatch.setattr(coverage.difflib, 'SequenceMatcher', lambda *a, **k:
                        pytest.fail('长稿不能进入无界diff'))
    prefix = ['alpha', 'beta', 'gamma'] * 900
    sentence = ['one', 'really', 'cool', 'thing', 'about', 'taking', 'the', 'train', 'in', 'china']
    notes = validate(' '.join(prefix + sentence), ' '.join(prefix + sentence[1:]))
    assert any('数字' in note and 'one → ∅' in note for note in notes)


def test_empty_candidate_cannot_delete_whole_window():
    with pytest.raises(ValueError, match='未保留'):
        validate('one', '')


def test_actual_hug_asr_fragment_is_a_warning_without_word_whitelist():
    source = 'I got the hug the duck is such a rich and flavorful bean'
    output = 'I got the— the duck is such a rich and flavorful bean.'
    notes = validate(source, output)
    assert len(notes) == 1
    assert '第4个词' in notes[0] and '（hug）' in notes[0]
    assert '无法仅凭文本确认' in notes[0] and '结合原音核对' in notes[0]
    assert '不代表语义正确' in notes[0]


def test_actual_1722nd_hug_warning_is_locatable_with_bounded_long_window(monkeypatch):
    monkeypatch.setattr(coverage.difflib, 'SequenceMatcher', lambda *a, **k:
                        pytest.fail('长稿不能进入无界diff'))
    prefix = ['lead'] * 1718
    local = coverage._tokens('I got the hug the duck is such a rich and flavorful bean')
    suffix = ['tail'] * 500
    source = ' '.join(prefix + local + suffix)
    output = ' '.join(prefix + local[:3] + local[4:] + suffix)
    notes = validate(source, output)
    assert len(notes) == 1
    assert '第1722个词' in notes[0] and '（hug）' in notes[0]
    assert '无法仅凭文本确认' in notes[0]


@pytest.mark.parametrize('source,output', [
    ('We took the train in China and returned.', 'We took the train in and returned.'),
    ('We travel to China', 'We travel to'),
    ('We saw bright flowers.', 'We saw flowers.'),
    ('We saw bright blue flowers.', 'We saw flowers.'),
])
def test_at_most_two_uncertain_deleted_words_are_audited_not_silently_accepted(source, output):
    notes = validate(source, output)
    assert len(notes) == 1
    assert '少量ASR原词未保留' in notes[0]
    assert '原文第' in notes[0] and '不代表语义正确' in notes[0]


@pytest.mark.parametrize('source,output', [
    ('We saw bright blue spring flowers.', 'We saw flowers.'),
    ('We arrived and booked a quiet riverside room before dinner.', 'We arrived before dinner.'),
    ('We arrive at home and will meet friendly travelers nearby.', 'We arrive at home.'),
])
def test_three_or_more_local_words_do_not_prove_major_omission(source, output):
    assert any('→ ∅' in note for note in validate(source, output))


def test_unknown_cleanup_and_evidenced_cleanup_share_a_review_only_budget():
    source = 'um we saw bright blue flowers and lovely trees'
    output = 'we saw flowers and trees'
    assert any('超过参考3词' in note for note in validate(source, output))
    notes = coverage.validate_source_preservation([{'text': output}], source, cleanup_budget=4)
    assert len(notes) == 3
    assert '口头填充词' in notes[0]
    assert sum('无法仅凭文本确认' in note for note in notes) == 2


def test_unknown_note_cap_does_not_exempt_later_deletions_from_budget():
    names = ['amber', 'cobalt', 'crimson', 'ivory', 'indigo', 'maroon', 'ochre', 'scarlet', 'violet']
    source = ' '.join(f'{name} station{chr(97 + index)} arrived' for index, name in enumerate(names))
    output = ' '.join(f'station{chr(97 + index)} arrived' for index in range(len(names)))
    assert len(coverage.validate_source_preservation(
        [{'text': output}], source, cleanup_budget=9)) == 8
    notes = coverage.validate_source_preservation([{'text': output}], source, cleanup_budget=8)
    assert len(notes) == 8 and '超过参考8词' in notes[-1]


@pytest.mark.parametrize('source,output', [
    ('I paid dollars.', 'I paid.'),
    ('We paid 100 yen.', 'We paid 100.'),
    ('We paid 100 yuan.', 'We paid 100 dollars.'),
    ('We paid 100 euros.', 'We paid 100 yen.'),
    ('We paid 100 pounds.', 'We paid 100.'),
    ('We walked 10 meters.', 'We walked 10.'),
    ('We waited twenty minutes.', 'We waited twenty hours.'),
    ('It weighs 10 grams.', 'It weighs 10 meters.'),
    ('We paid 100.', 'We paid $100.'),
    ('It costs $100.', 'It costs 100.'),
    ('The rate is 100%.', 'The rate is 100.'),
])
def test_currency_and_measurement_units_can_be_corrected_with_before_after_audit(source, output):
    assert any('单位/币种' in note and '→' in note for note in validate(source, output))


@pytest.mark.parametrize('source,output', [
    ('It costs 100 dollar.', 'It costs 100 dollars.'),
    ('We walked 10 metres.', 'We walked 10 metre.'),
    ('We can see a second door.', 'We can see a door.'),
    ('We pound the dough.', 'We the dough.'),
    ('We visited Sterling.', 'We visited.'),
    ('A degree is available.', 'A is available.'),
    ('We visited Cent.', 'We visited.'),
    ('We met Yuan yesterday.', 'We met yesterday.'),
    ('Yen greeted us.', 'greeted us.'),
    ('Euro appeared in the show.', 'appeared in the show.'),
    ('Dollars performed at the concert.', 'performed at the concert.'),
    ('We paid Cent for the show.', 'We paid for the show.'),
])
def test_plural_unit_spelling_and_unproven_temporal_sense_do_not_create_false_unit_error(source, output):
    validate(source, output)


@pytest.mark.parametrize('american,british', [
    ('meter', 'metre'), ('kilometers', 'kilometres'), ('centimeter', 'centimetre'),
    ('millimeters', 'millimetres'), ('liters', 'litres'),
])
def test_known_unit_spelling_variants_are_equivalent_without_changing_magnitude(american, british):
    assert validate(f'The measurement is 10 {american}.', f'The measurement is 10 {british}.') == []
    assert validate(f'The measurement is 10 {british}.', f'The measurement is 10 {american}.') == []


@pytest.mark.parametrize('before,after', [('meters', 'kilometers'), ('litres', 'ml')])
def test_unit_magnitude_changes_are_reviewed_instead_of_assuming_original_asr_is_true(before, after):
    assert any('单位/币种' in note and '→' in note for note in validate(
        f'The measurement is 10 {before}.', f'The measurement is 10 {after}.'))


def test_general_name_correction_has_original_position_and_candidate_text():
    notes = validate('We visited kwa jude in Beijing.', 'We visited Quanjude in Beijing.')
    assert any('第3个词' in note and 'kwa jude → Quanjude' in note for note in notes)


def test_currency_correction_is_general_and_does_not_need_a_yen_exception():
    for before, after in [('yen', 'yuan'), ('euros', 'dollars'), ('100', '200')]:
        notes = validate(f'The ticket cost {before}.', f'The ticket cost {after}.')
        assert notes and any(before in note and after in note and '→' in note for note in notes)


def test_local_grammar_permutation_inside_a_long_document_is_not_whole_document_reordering():
    prefix = 'We carefully described the station building before we waited for our train. '
    source = prefix + 'We quickly boarded the train and settled into our seats.'
    output = prefix + 'We boarded the train quickly and settled into our seats.'
    assert any('→' in note for note in validate(source, output))


def test_repeated_multiword_stutter_can_be_cleaned_even_when_most_source_words_disappear():
    phrase = 'we reached the quiet railway station'
    notes = coverage.validate_source_preservation(
        [{'text': phrase + ' safely'}], (phrase + ' ') * 10 + 'safely', cleanup_budget=0)
    assert any('重复文字或口吃' in note for note in notes)
    assert any('超过参考0词' in note for note in notes)


@pytest.mark.parametrize('scattered', [False, True])
def test_clear_half_document_omission_still_blocks_without_a_three_word_rule(scattered):
    source = [f'word{chr(97 + index // 26)}{chr(97 + index % 26)}' for index in range(40)]
    output = source[::2] if scattered else source[:20]
    with pytest.raises(ValueError, match='明显大幅遗漏'):
        validate(' '.join(source), ' '.join(output))


def test_fifty_percent_net_shrink_cannot_be_hidden_in_a_single_replacement_block():
    source = ' '.join(f'word{chr(97 + index)}' for index in range(24))
    with pytest.raises(ValueError, match='明显大幅遗漏'):
        validate(source, 'A summary of the trip')


def test_less_than_half_document_cleanup_is_reviewed_even_if_it_exceeds_the_old_budget():
    source = [f'word{chr(97 + index // 26)}{chr(97 + index % 26)}' for index in range(40)]
    notes = coverage.validate_source_preservation([{'text': ' '.join(source[:21])}],
                                                  ' '.join(source), cleanup_budget=0)
    assert any('超过参考0词' in note for note in notes)


def test_uniquely_identifiable_long_clause_movement_remains_a_hard_error():
    first = 'Travelers admired peaceful gardens beside the historic river before returning home'
    second = 'Families boarded crowded railway carriages outside the modern station after sunset'
    with pytest.raises(ValueError, match='搬移|重排'):
        validate(first + ' ' + second, second + ' ' + first)


def test_long_entire_word_reversal_remains_a_hard_error():
    words = [f'word{chr(97 + index)}' for index in range(20)]
    with pytest.raises(ValueError, match='重排'):
        validate(' '.join(words), ' '.join(reversed(words)))


def test_audit_values_and_count_are_bounded_for_extremely_long_local_correction():
    source = ' '.join(['noise'] * 300) + ' We reached home safely'
    notes = validate(source, 'noise We reached home safely')
    assert 1 <= len(notes) <= 8
    assert all(len(note) < 400 for note in notes)
