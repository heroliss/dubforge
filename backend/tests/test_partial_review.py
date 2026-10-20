"""局部审查凭证与实际新旧内容范围，保持现有命中规则的发布语义。"""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from dubforge.pipeline import partial_render, partial_review


def full(ranges, *, signature='current', mute=True):
    return dict(version=1, signature=signature, ranges=ranges, mute_audio=mute,
                audio_processing={'requested_mode': 'all_audio', 'effective_mode': 'all_audio'})


def window(start, end):
    return {'start': start, 'end': end}


def partial(current, base, ranges, *, replace_audio=False):
    ctx = SimpleNamespace(version={'partial_plan': dict(base_sha256='a'*64, ranges=ranges,
        replace_audio=replace_audio, base_version={'content_review_receipt': base})})
    return partial_render.review_receipt(ctx, current)


def test_local_receipt_cannot_claim_full_document_or_new_audio_processing():
    current = full([window(0, 1), window(4, 5)])
    base = full([window(0, 1)], signature='old')
    value = partial(current, base, [window(4, 5)])
    assert value['scope'] == 'partial' and value['whole_output_reviewed'] is False
    assert value['current_local']['ranges'] == [window(4, 5)]
    assert value['inherited_baseline'] == base
    assert not partial_review.covers_current_ranges(value, current), '保留音轨不能冒充当前命中声音已经处理'
    assert partial_review.covers_current_ranges(partial(current, base, [window(4, 5)], replace_audio=True), current)


def test_inherited_handled_ranges_do_not_require_unrelated_current_global_signature():
    current = full([window(0, 1)])
    base = full([window(0, 1)], signature='before local text edit')
    assert partial_review.covers_current_ranges(partial(current, base, [window(4, 5)]), current)


def test_unhandled_outside_range_still_requires_a_local_fix():
    current = full([window(8, 9)])
    value = partial(current, full([], signature='old'), [window(4, 5)], replace_audio=True)
    assert not partial_review.covers_current_ranges(value, current)


def test_missing_baseline_audit_does_not_add_blocker_when_no_ranges_match():
    current = full([])
    assert partial_review.covers_current_ranges(partial(current, None, [window(4, 5)]), current)


def test_prior_partial_receipt_retains_actual_handled_coverage_after_next_local_edit():
    earlier = full([window(0, 1)], signature='earlier')
    first = partial(earlier, full([], signature='original'), [window(0, 1)], replace_audio=True)
    current = full([window(0, 1), window(4, 5)])
    second = partial(current, first, [window(4, 5)], replace_audio=True)
    assert partial_review.covers_current_ranges(second, current)
    second['current_local']['signature'] = 'stale current local settings'
    assert not partial_review.covers_current_ranges(second, current)


def _overlay(items):
    return dict(settings={'start': 0., 'interval': 1., 'duration': 1.}, items=items)


@pytest.mark.parametrize('risky_old,risky_new,ranges,blocked', [
    (0, None, [window(0, 1)], False),
    (1, None, [window(0, 1)], True),
    (None, 1, [window(0, 1)], False),
    (None, 0, [window(0, 1)], True),
])
def test_only_actual_old_and_new_comment_ranges_are_checked(risky_old, risky_new, ranges, blocked):
    def items(risky):
        return [{'text': 'risky' if i == risky else 'safe'} for i in range(2)]
    base = dict(include_comments=True, comment_overlay=_overlay(items(risky_old)))
    snapshot = dict(partial={'plan_id': 'a'}, partial_plan={'ranges': ranges, 'base_version': base},
                    include_comments=True, comment_overlay=_overlay(items(risky_new)))
    before = copy.deepcopy(snapshot)
    def check(artifact, scoped, terms, **kwargs):
        return '已烧录评论命中' if any(item['text'] == 'risky' for item in scoped['comment_overlay']['items']) else ''
    issue = partial_review.comment_issue(SimpleNamespace(), snapshot, [], {}, check)
    assert bool(issue) is blocked
    assert snapshot == before


def _immutable_partial_output(make_project, outside_risk=False):
    from dubforge.pipeline import assembly_versions, content_redaction
    from dubforge.pipeline.assembly_partial import _digest
    from dubforge.pipeline.dependencies import Dependencies
    from dubforge.utils.fs import atomic_write_bytes
    from test_content_review_publication_comments import _safe_project, _item, _overlay, _output
    project, root, _ = _safe_project(make_project())
    safe, risky = _item(), _item(display_text='the tragedy of Tiananmen Square')
    old_items = [safe, risky] if outside_risk else [risky, safe]
    current = content_redaction.receipt(Dependencies(project))
    base = dict(id='baseline', name='已生成基片', dubbing=False, burn_subtitles=False,
        include_comments=True, overrides={}, values={'assemble.crf': 18},
        comment_overlay=_overlay(old_items), content_review_receipt=current)
    artifact = _output(project, root, base, name='final_version_baseline.mp4')
    base = json.loads(assembly_versions.results(project.uid)['baseline'].snapshot)
    baseline = root / 'output' / '.partial-baselines' / (artifact.sha256 + '.mp4')
    atomic_write_bytes(baseline, (root / artifact.rel_path).read_bytes())
    ranges = [window(0., 1.1)]
    identifier = uuid.uuid4().hex
    immutable = dict(schema=1, id=identifier, project_uid=project.uid, base_version_id='baseline',
        base_name='已生成基片', base_artifact_id=artifact.id, base_path=str(baseline),
        base_sha256=artifact.sha256, base_size=baseline.stat().st_size, base_version=base,
        ranges=ranges, replace_audio=False, source_identity={'source_fingerprint': 'historical-source', 'duration': 3.})
    immutable['fingerprint'] = _digest(immutable)
    path = root / 'output' / '.partial-plans' / (identifier + '.json')
    atomic_write_bytes(path, json.dumps(immutable, ensure_ascii=False).encode())
    received = partial(current, current, ranges)
    received['base_sha256'] = artifact.sha256
    snapshot = dict(id='child', partial={'plan_id': identifier}, partial_plan=immutable,
        include_comments=True, comment_overlay=_overlay([safe, safe]), content_review_receipt=received)
    artifact = _output(project, root, snapshot)
    assert 'partial_plan' not in json.loads(assembly_versions.results(project.uid)['child'].snapshot)
    return project, artifact, path, baseline, immutable


@pytest.mark.parametrize('outside_risk', [False, True])
def test_publishing_checks_actual_partial_comments_and_explains_review_scope(make_project, outside_risk):
    from dubforge import content_review_service
    from dubforge.errors import DubForgeError
    project, artifact, path, _baseline, _immutable = _immutable_partial_output(make_project, outside_risk)
    before = path.read_bytes()
    if outside_risk:
        with pytest.raises(DubForgeError, match='已烧录的评论'):
            content_review_service.assert_publishable(project, artifact)
    else:
        content_review_service.assert_publishable(project, artifact)
        state = content_review_service.candidate_states(project, [artifact])[artifact.id]
        assert state['review_scope'] == 'partial'
        assert state['review_ready'] is None
        assert '范围外沿用基片' in state['review_status']
        assert '未重新审查全片' in state['review_reason']
    assert path.read_bytes() == before, '发布前只读核对不能重写不可变计划'


@pytest.mark.parametrize('damage', ['missing_plan', 'other_project', 'fingerprint', 'missing_baseline', 'changed_baseline', 'cycle', 'too_deep'])
def test_historical_partial_reference_requires_proven_local_plan_and_baseline(make_project, damage):
    from dubforge import content_review_service
    from dubforge.errors import DubForgeError
    from dubforge.pipeline.assembly_partial import _digest
    project, artifact, path, baseline, immutable = _immutable_partial_output(make_project)
    if damage == 'missing_plan':
        path.unlink()
    elif damage == 'missing_baseline':
        baseline.unlink()
    elif damage == 'changed_baseline':
        baseline.write_bytes(b'changed baseline')
    else:
        if damage == 'other_project':
            immutable['project_uid'] = 'another-project'
        elif damage == 'fingerprint':
            immutable['base_name'] = '改写后的基片'
        elif damage == 'too_deep':
            base = immutable['base_version']
            for _index in range(18):
                identifier = uuid.uuid4().hex
                nested = {**copy.deepcopy(immutable), 'id': identifier, 'base_version': base}
                nested['fingerprint'] = _digest({key: value for key, value in nested.items() if key != 'fingerprint'})
                path.with_name(identifier + '.json').write_text(json.dumps(nested), encoding='utf-8')
                base = dict(partial={'plan_id': identifier}, include_comments=False,
                            content_review_receipt=dict(scope='partial', base_sha256=immutable['base_sha256'], ranges=immutable['ranges']))
            immutable['base_version'] = base
        else:
            immutable['base_version']['partial'] = {'plan_id': immutable['id']}
            immutable['base_version']['content_review_receipt'] = dict(scope='partial',
                base_sha256=immutable['base_sha256'], ranges=immutable['ranges'])
        if damage != 'fingerprint':
            immutable['fingerprint'] = _digest({key: value for key, value in immutable.items() if key != 'fingerprint'})
        path.write_text(json.dumps(immutable, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(DubForgeError, match='缺少可核对的基片生成记录'):
        content_review_service.assert_publishable(project, artifact)
