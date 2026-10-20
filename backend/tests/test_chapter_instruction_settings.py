"""自定义标题要求可继承/冻结，平台硬约束不成为用户配置。"""
import copy

import pytest

from dubforge import configuration, workflow_plan
from dubforge.publishing import chapters, service


def save(key, updates):
    current = configuration.view(key)
    return configuration.save(key, dict(revision=current['revision'], updates=updates))


def test_title_instruction_inherits_and_freezes_without_exposing_platform_limits(make_project):
    instruction = '使用简短名词短语；保留产品英文名称；避免夸张。'
    save('global', {'publish.chapters_instruction': instruction})
    uid = make_project()
    assert configuration.resolve(uid)['values']['publish.chapters_instruction'] == instruction
    spec = configuration.run_spec(uid)
    assert workflow_plan.recipe(spec)['chapters_instruction'] == instruction
    local = service._chapter_options(dict(enabled=True, instruction=instruction,
        items=[dict(title='Introduction', start_time=0, end_time=10)]))
    save('global', {'publish.chapters_instruction': '新的全局要求'})
    assert local['instruction'] == instruction
    assert configuration.resolve(uid)['values']['publish.chapters_instruction'] == instruction
    fields = configuration.schema()['publication']['fields']
    assert [row['path'] for row in fields if row['path'] == 'publish.chapters_instruction'] == ['publish.chapters_instruction']
    assert not any(any(word in row['path'] for word in ('max_chapters', 'min_duration', 'title_max_length')) for row in fields)
    assert chapters.platform_constraints()['max_chapters'] == 10


@pytest.mark.parametrize('bad', [True, None, [], 'x' * 2001])
def test_instruction_shape_is_checked_at_configuration_recipe_and_frozen_options(make_project, bad):
    uid = make_project()
    with pytest.raises(configuration.ConfigurationError) as error:
        save('project:' + uid, {'publish.chapters_instruction': bad})
    assert error.value.code != 'CONFIGURATION_CONFLICT'
    with pytest.raises(workflow_plan.WorkflowError):
        workflow_plan.recipe(dict(mode='produce', chapters_instruction=bad))
    with pytest.raises(service.PublishError):
        service._chapter_options(dict(instruction=bad))


def test_custom_instruction_does_not_enter_production_artifact_fingerprints(make_project):
    uid = make_project()
    from dubforge.pipeline import config_fingerprint
    from dubforge import config
    from dubforge.domain import crud
    from dubforge.pipeline.dependencies import Dependencies
    original = config.load_config()
    changed = copy.deepcopy(original)
    changed.setdefault('publish', {})['chapters_instruction'] = '标题格式和内容的新要求'
    def deps(settings):
        return Dependencies(crud.get_project(uid), settings=settings)
    for key in ('split', 'translate', 'tts', 'subtitles', 'assemble'):
        assert config_fingerprint.snapshot(deps(original), key) == config_fingerprint.snapshot(deps(changed), key)
