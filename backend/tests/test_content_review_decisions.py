"""人工复核绑定证据、并发保护与派生输出/评论门禁的隔离回归。"""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import comment_studio, config, content_review_decisions as decisions, content_review_service as service
from dubforge.domain import crud
from test_configuration import save
from dubforge.pipeline import assembly_versions, content_redaction as redaction
from dubforge.pipeline.dependencies import Dependencies
from dubforge.runner import compute_fingerprint
from dubforge.utils.fs import atomic_write_bytes
from test_content_redaction import _seed_text
from test_content_review_publication_comments import _output, _overlay

pytestmark = pytest.mark.usefixtures('isolated_env')


def _body(report, finding, decision='allow', **updates):
    return {**dict(key=finding['key'], evidence_fingerprint=finding['evidence_fingerprint'],
        decision_revision=report['decision_revision'], revision=report['revision'],
        configuration_fingerprint=report['configuration_fingerprint'], decision=decision,
        reason='人工听验，确认此处为误报'), **updates}


def _decide(uid, scope='findings', decision='allow'):
    report = service.report(uid)
    return service.save_decision(uid, _body(report, report[scope][0], decision))


def test_pending_block_allow_reset_preserve_evidence_and_independent_original(make_project):
    project, root, paths = _seed_text(make_project())
    source = {path: path.read_bytes() for path in paths.values()}
    initial = service.report(project.uid)
    assert initial['coverage'] == dict(original=3, source=3, translated=3, comments=0, complete_text=True)
    assert initial['decision_revision'] == '0' and initial['checked_at']
    item = initial['findings'][0]
    assert item['decision'] == 'pending' and item['active']
    assert item['fields'] and item['source_audio_text']
    assert [row['id'] for row in item['context_rows']] == [1, 2, 3]
    assert item['context_ids'] == [2]
    confirmed = _decide(project.uid, decision='block')
    assert confirmed['findings'][0]['decision'] == 'block'
    assert confirmed['ranges'] == initial['ranges']
    allowed = _decide(project.uid)
    assert allowed['findings'][0]['decision'] == 'allow' and not allowed['findings'][0]['active']
    assert allowed['summary']['blocked_lines'] == 1  # 原始 ASR 仍需独立复核。
    cleared = _decide(project.uid, 'original_findings')
    assert cleared['ranges'] == cleared['effective_audio_ranges'] == []
    assert cleared['summary']['blocked_lines'] == 0
    assert cleared['summary']['allowed_findings'] == 2
    assert redaction.redact_document(Dependencies(project), [dict(id=2, start=1., end=2.)])
    reset = _decide(project.uid, decision='reset')
    assert reset['findings'][0]['decision'] == 'pending' and reset['ranges']
    assert source == {path: path.read_bytes() for path in paths.values()}
    stored = json.loads((root / 'content_review_decisions.json').read_text(encoding='utf-8'))
    assert stored['revision'] == 4
    assert not list(root.glob('*.tmp'))


def test_manual_ranges_remain_explicit_and_allow_cannot_clear_them(make_project):
    project, _, _ = _seed_text(make_project())
    save('project:' + project.uid, {'content_review.blocked_ranges': '0.8-2.2:人工确认完整片段'})
    _decide(project.uid)
    result = _decide(project.uid, 'original_findings')
    assert result['ranges'] == [dict(start=.8, end=2.2)]
    assert result['manual_ranges'][0]['reason'] == '人工确认完整片段'
    audio = redaction.audio_ranges(Dependencies(project), start=.5, duration=2)
    assert audio[0]['start'] == pytest.approx(.15) and audio[0]['end'] == pytest.approx(1.85)


def test_context_time_rule_changes_expire_only_relevant_decisions():
    rows = [dict(id=1, start=0., end=1., source_text='tragedy of', text='悲剧'),
            dict(id=2, start=1.1, end=2., source_text='Tiananmen Square', text='天安门广场'),
            dict(id=3, start=2.1, end=3., source_text='Beautiful city', text='美丽城市')]
    from dubforge.content_review import review_lines
    findings = decisions.bind_lines(review_lines(rows), rows, 'subtitle', dict(entries={}))
    first = findings[0]
    store = dict(entries={first['key']: dict(decision='allow', evidence_fingerprint=first['evidence_fingerprint'])})
    changed = copy.deepcopy(rows)
    changed[2]['text'] = '无关附近文字变化'
    assert decisions.bind_lines(review_lines(changed, ['无关词']), changed, 'subtitle', store)[0]['decision'] == 'allow'
    changed[1]['source_text'] += ' in 1989'
    assert decisions.bind_lines(review_lines(changed), changed, 'subtitle', store)[0]['decision'] == 'pending'
    changed = copy.deepcopy(rows)
    changed[1]['end'] += .1
    assert decisions.bind_lines(review_lines(changed), changed, 'subtitle', store)[0]['decision_stale']
    source_only = [dict(id=1, start=0., end=1., source_text='the tragedy of Tiananmen Square', text='普通介绍')]
    item = decisions.bind_lines(review_lines(source_only), source_only, 'subtitle', dict(entries={}))[0]
    one_store = dict(entries={item['key']: dict(decision='allow', evidence_fingerprint=item['evidence_fingerprint'])})
    source_only[0]['text'] = '只修改无关译文'
    assert decisions.bind_lines(review_lines(source_only), source_only, 'subtitle', one_store)[0]['decision'] == 'allow'


def test_allow_changes_only_subtitle_assembly_fingerprints_and_confirmation_does_not(make_project):
    project, _, _ = _seed_text(make_project(), original=[])
    before = {key: compute_fingerprint(project, key) for key in ('subtitles', 'assemble', 'tts', 'translate')}
    version = assembly_versions.resolve(Dependencies(project), dict(id='src', name='原声版',
        dubbing=False, burn_subtitles=False, include_comments=False, overrides={}))
    version_before = assembly_versions.fingerprint(Dependencies(project), version)
    _decide(project.uid, decision='block')
    assert before == {key: compute_fingerprint(project, key) for key in before}
    _decide(project.uid)
    after = {key: compute_fingerprint(project, key) for key in before}
    assert after['subtitles'] != before['subtitles'] and after['assemble'] != before['assemble']
    assert after['tts'] == before['tts'] and after['translate'] == before['translate']
    assert assembly_versions.fingerprint(Dependencies(project), version) != version_before
    current = service.report(project.uid)
    service.save_decision(project.uid, _body(current, current['findings'][0], reason='只改备注'))
    assert after == {key: compute_fingerprint(project, key) for key in after}


def test_stale_evidence_configuration_and_parallel_decisions_return_conflict(make_project):
    project, _, _ = _seed_text(make_project())
    current = service.report(project.uid)
    body = _body(current, current['findings'][0])
    client = TestClient(app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: client.post(f'/api/projects/{project.uid}/content-review/decisions', json=body).status_code, range(2)))
    assert sorted(statuses) == [200, 409]
    response = client.post(f'/api/projects/{project.uid}/content-review/decisions', json=body)
    assert response.status_code == 409 and response.json()['code'] == 'CONTENT_REVIEW_DECISION_CONFLICT'
    current = service.report(project.uid)
    body = _body(current, current['original_findings'][0])
    body['evidence_fingerprint'] = 'different-evidence'
    assert client.post(f'/api/projects/{project.uid}/content-review/decisions', json=body).status_code == 409
    save('project:' + project.uid, {'content_review.blocked_terms': '新规则'})
    assert client.post(f'/api/projects/{project.uid}/content-review/decisions', json=_body(current, current['original_findings'][0])).status_code == 409


def _comment_project(uid):
    project, root, _ = _seed_text(uid, source=[dict(id=1, start=0., end=1., text='Beautiful city')], target={1: '美丽城市'})
    atomic_write_bytes(root / 'artifacts/download/video_info.json', json.dumps(dict(comments=dict(items=[
        dict(id='c1', text='the tragedy of Tiananmen Square', author='观众')]))).encode())
    return project, root


def test_comment_allow_is_shared_by_read_select_save_render_and_historical_publication(make_project):
    project, root = _comment_project(make_project())
    initial = service.report(project.uid)
    assert initial['summary']['blocked_comments'] == 1
    state = comment_studio.view(project.uid)
    raw = state['rows'][0]
    overlay = _overlay([dict(raw, source_text=raw['text'], display_text='普通中文介绍')])
    historical = _output(project, root, dict(include_comments=True, comment_overlay=overlay))
    with pytest.raises(redaction.ContentRedactionError):
        service.assert_publishable(project, historical)
    _decide(project.uid, 'comment_findings')
    allowed = service.report(project.uid)
    assert allowed['summary']['blocked_comments'] == 0 and len(allowed['comment_findings']) == 1
    assert not comment_studio.view(project.uid)['rows'][0]['review_blocked']
    body = dict(revision=state['overlay_revision'], items=overlay['items'], settings=overlay['settings'])
    assert len(comment_studio.draft(project.uid, body)['items']) == 1
    comment_studio.save(project.uid, body)
    assert len(comment_studio.confirmed(project.uid)['items']) == 1
    assert len(redaction.safe_overlay(Dependencies(project), overlay)['items']) == 1
    service.assert_publishable(project, historical)
    # 新的命中文字不能继承原证据的允许。
    modified = copy.deepcopy(overlay)
    modified['items'][0]['display_text'] = '天安门广场的悲剧'
    with pytest.raises(redaction.ContentRedactionError):
        redaction.safe_overlay(Dependencies(project), modified)
    new_output = _output(project, root, dict(include_comments=True, comment_overlay=modified), name='final_version_new.mp4')
    with pytest.raises(redaction.ContentRedactionError):
        service.assert_publishable(project, new_output)


def test_allowed_comment_does_not_change_llm_eligible_judgment(make_project, monkeypatch):
    project, _ = _comment_project(make_project())
    _decide(project.uid, 'comment_findings')
    monkeypatch.setattr(comment_studio, 'chat_validated', lambda *args, **kwargs: kwargs['validator'](
        kwargs['parser']('1. 30 | false | 与视频主题无关')))
    state = comment_studio.run(project.uid, 'filter', prompt=comment_studio.FILTER_PROMPT, limit=1)
    assert not state['filter']['assessments'][0]['eligible']
    assert not state['filter']['assessments'][0]['review_blocked']
    assert state['filter']['items'] == []


def test_comment_allow_refreshes_cached_local_exclusion_without_model_call(make_project, monkeypatch):
    project, _ = _comment_project(make_project())
    monkeypatch.setattr(comment_studio, 'chat_validated', lambda *args, **kwargs: kwargs['validator'](
        kwargs['parser']('1. 90 | true | 有参考价值')))
    before = comment_studio.run(project.uid, 'filter', prompt=comment_studio.FILTER_PROMPT, limit=1)
    assert before['filter']['items'] == [] and before['filter']['assessments'][0]['eligible']
    _decide(project.uid, 'comment_findings')
    monkeypatch.setattr(comment_studio, 'chat_validated', lambda *args, **kwargs: pytest.fail('人工复核不调用模型'))
    fresh = comment_studio.view(project.uid)
    assert [item['id'] for item in fresh['filter']['items']] == ['c1']
    assert fresh['filter']['assessments'][0]['outcome'] == 'selected'


def test_matched_cached_translation_is_frozen_for_render_and_publication(make_project):
    project, root = _comment_project(make_project())
    state = comment_studio.view(project.uid)
    raw = state['rows'][0]
    comment_studio._write(project, dict(translations={raw['id']: dict(
        source_hash=raw['source_hash'], text_zh='天安门广场的悲剧')}))
    _decide(project.uid, 'comment_findings')
    saved = comment_studio.save(project.uid, dict(revision=state['overlay_revision'],
        items=[dict(raw, source_text=raw['text'], display_text='普通中文介绍')],
        settings=comment_studio.DEFAULT_SETTINGS))
    overlay = saved['overlay']
    assert set(overlay['items'][0]['review_evidence_texts']) == {raw['text'], '天安门广场的悲剧'}
    assert not service.report(project.uid)['comment_findings'][0]['active']
    assert len(comment_studio.confirmed(project.uid)['items']) == 1
    assert len(redaction.safe_overlay(Dependencies(project), overlay)['items']) == 1
    historical = _output(project, root, dict(include_comments=True, comment_overlay=overlay))
    service.assert_publishable(project, historical)


def test_comment_allow_reset_changes_selected_assembly_and_block_notes_do_not(make_project):
    project, _ = _comment_project(make_project())
    row = comment_studio.view(project.uid)['rows'][0]
    overlay = _overlay([dict(row, source_text=row['text'], display_text='普通中文介绍'),
                        dict(row, id='safe', text='Beautiful city', source_text='Beautiful city', display_text='美丽城市')])
    comment_studio._write(project, dict(overlay=overlay))
    spec = dict(id='comments', name='评论版', dubbing=False, burn_subtitles=False, include_comments=True, overrides={})
    def current_fingerprint():
        deps = Dependencies(project)
        version = assembly_versions.resolve(deps, spec, comment_plan=overlay)
        return assembly_versions.fingerprint(deps, version)
    before = current_fingerprint()
    step = crud.get_step(project.id, 'assemble')
    step.status = 'done'
    crud.save_step(step)
    _decide(project.uid, 'comment_findings', 'block')
    assert crud.get_step(project.id, 'assemble').status == 'done' and current_fingerprint() == before
    _decide(project.uid, 'comment_findings')
    after = current_fingerprint()
    assert crud.get_step(project.id, 'assemble').status == 'stale' and after != before
    current = service.report(project.uid)
    service.save_decision(project.uid, _body(current, current['comment_findings'][0], reason='更新说明'))
    assert current_fingerprint() == after
    _decide(project.uid, 'comment_findings', 'reset')
    assert current_fingerprint() == before


def test_disabled_report_does_not_call_existing_decisions_stale(make_project):
    project, _, _ = _seed_text(make_project())
    _decide(project.uid)
    save('project:' + project.uid, {'content_review.enabled': False})
    report = service.report(project.uid)
    assert report['stale_decision_count'] == 0 and report['unverified_decision_count'] == 1
    assert report['checked_at'] is None and not report['coverage']['complete_text']
    assert '关闭' in ' '.join(report['warnings'])


def test_partial_translation_is_reported_without_claiming_complete_coverage(make_project):
    project, _, paths = _seed_text(make_project())
    atomic_write_bytes(paths[('translate', 'translate.txt')], b'1. only one translated line')
    report = service.report(project.uid)
    assert report['coverage']['original'] == 3 and report['coverage']['source'] == 3
    assert report['coverage']['translated'] == 0 and not report['coverage']['complete_text']
    assert '译文尚未完整' in ' '.join(report['warnings'])
    paths[('split', 'split.txt')].unlink()
    raw_only = service.report(project.uid)
    assert raw_only['coverage']['original'] == 3 and raw_only['summary']['lines'] == 0
    assert not raw_only['coverage']['complete_text']


def test_invalid_or_corrupt_decisions_fail_with_stable_chinese_errors(make_project):
    project, root, _ = _seed_text(make_project())
    with pytest.raises(decisions.ContentReviewDecisionError, match='参数无效'):
        service.save_decision(project.uid, dict(decision='allow'))
    atomic_write_bytes(root / 'content_review_decisions.json', b'not json')
    with pytest.raises(decisions.ContentReviewDecisionError) as exc:
        service.report(project.uid)
    assert exc.value.code == 'CONTENT_REVIEW_DECISION_STORE'
