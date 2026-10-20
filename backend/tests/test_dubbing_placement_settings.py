"""排布策略按项目保存，不覆盖音色预设或逐句声音精调。"""
import pytest

from dubforge.config import DEFAULTS
from dubforge.errors import ConfigError
from dubforge.pipeline.line_settings import LINE_PATHS
from dubforge.pipeline.step_params import STEP_PARAM_SCHEMA, validate_numeric_updates


def test_placement_defaults_and_schema_are_project_strategy():
    assert DEFAULTS['tts']['placement'] == dict(
        enabled=True, auto_repair=True, max_shift_seconds=2.0, window_lines=9, inter_sentence_gap=.08)
    fields = [field for field in STEP_PARAM_SCHEMA['tts'] if field.path.startswith('tts.placement.')]
    assert len(fields) == 5
    assert {field.group for field in fields} == {'配音时间排布'}
    assert not set(field.path for field in fields) & LINE_PATHS
    repair = next(field for field in fields if field.path == 'tts.placement.auto_repair')
    assert repair.depends_on == (('tts.backend', 'indextts25'),)
    assert all(not any(key == 'tts.backend' for key, _ in field.depends_on)
               for field in fields if field is not repair)


@pytest.mark.parametrize('key,value', [
    ('enabled', 1), ('enabled', 'true'),
    ('auto_repair', 1), ('auto_repair', 'true'),
    ('max_shift_seconds', -1), ('max_shift_seconds', 16), ('max_shift_seconds', float('nan')),
    ('window_lines', 2), ('window_lines', 16), ('window_lines', 4.5),
    ('inter_sentence_gap', -.01), ('inter_sentence_gap', 1.1),
])
def test_invalid_placement_settings_are_rejected(key, value):
    with pytest.raises(ConfigError):
        validate_numeric_updates({'tts.placement.' + key: value})


def test_valid_placement_boundaries_are_accepted():
    validate_numeric_updates({'tts.placement.enabled': False, 'tts.placement.max_shift_seconds': 0,
                              'tts.placement.window_lines': 3, 'tts.placement.inter_sentence_gap': 0})
    validate_numeric_updates({'tts.placement.enabled': True, 'tts.placement.max_shift_seconds': 15,
                              'tts.placement.window_lines': 15, 'tts.placement.inter_sentence_gap': 1})
