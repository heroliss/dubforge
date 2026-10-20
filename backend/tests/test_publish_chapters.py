import pytest

from dubforge.publishing.service import PublishError, _chapter_options


def test_chapter_options_are_frozen_and_sorted():
    value = _chapter_options({'enabled': True, 'translate': False, 'permanent': False, 'items': [
        {'title': '第二节', 'start_time': 12.4, 'end_time': 20},
        {'title': '开场', 'start_time': 0, 'end_time': 12},
    ]})
    assert value['enabled'] is True
    assert value['translate'] is False
    assert value['permanent'] is False
    assert [row['title'] for row in value['items']] == ['开场', '第二节']
    assert value['items'][1]['start_time'] == 12.4


@pytest.mark.parametrize('items', [
    [{'title': '', 'start_time': 0, 'end_time': 5}],
    [{'title': '重复', 'start_time': 0, 'end_time': 5}, {'title': '重复2', 'start_time': 0, 'end_time': 8}],
    [{'title': '坏范围', 'start_time': 5, 'end_time': 5}],
])
def test_chapter_options_reject_invalid_rows(items):
    with pytest.raises(PublishError):
        _chapter_options({'items': items})
