"""原稿换片后交付：真实修改状态机，远端与LLM全部替身。"""
import copy
import math
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from dubforge import db
from dubforge.errors import DubForgeError
from dubforge.publishing import chapters, credentials, service, studio, transport
from dubforge.publishing import update_delivery as delivery
from dubforge.publishing.models import PublishAccount, PublishChange

BV = 'BV1234567890'
SOURCE = [dict(title='Intro', start_time=0., end_time=10.),
          dict(title='Main', start_time=10., end_time=30.)]


@pytest.fixture
def env(make_project, monkeypatch):
    uid = make_project()
    with Session(db.get_engine()) as session:
        session.add(PublishAccount(id='123', name='测试账号', credential='fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda _: {'fixture': True})
    value = SimpleNamespace(calls=[], adaptations=[], hooks={}, current={}, saves=[], section_member=True,
        detail=dict(mid='123', bvid=BV, aid=10, version='v1', state=0, collection=None, in_season=False,
                    videos=[dict(cid=11, title='原声'), dict(cid=12, title='配音'), dict(cid=3, title='保留P')]),
        rules=dict(limit=10, stay=5, space=0, max_title_length=16))
    value.snapshot = dict(project_uid=uid, account_id='123', bvid=BV, aid=10, context_title='测试视频',
        parts=[dict(old_cid=index, artifact_id=100 + index, title=title, replaced=True, sha256=f'hash-{index}',
                    duration=30., output_cuts=None, chapter_items=copy.deepcopy(SOURCE),
                    chapter_timeline=dict(kind='source')) for index, title in enumerate(('原声', '配音'), 1)],
        chapters=dict(enabled=True, translate=True, instruction='', permanent=True, overwrite_existing=False,
                      items=copy.deepcopy(SOURCE)), collection=dict(enabled=False))
    value.execution = dict(id='update-fixture', stages={}, final_parts=copy.deepcopy(value.detail['videos']),
        transfers={str(100 + index): dict(cid=10 + index, filename=f'new-{index}') for index in (1, 2)})

    def adapt(items, *, duration=None, translate=True, rules=None, instruction='', context_title='', **kwargs):
        value.adaptations.append(copy.deepcopy(items))
        if kwargs.get('progress'):
            kwargs['progress'].phase('chapters.adapt', '接收模型输出', state='running')
        if 'adapt' in value.hooks:
            value.hooks['adapt'](items)
        limits = chapters.platform_constraints(rules)
        output = [dict(title='开场' if index == 0 else '主体', start_time=0 if index == 0 else math.floor(item['start_time'] + .5),
                       end_time=math.floor((items[index + 1]['start_time'] if index + 1 < len(items) else duration) + .5),
                       source_indices=[index]) for index, item in enumerate(items)]
        return dict(items=output, source='llm.chapter_adaptation', adaptation=dict(schema=chapters.ADAPTATION_VERSION,
            source_count=len(items), output_count=len(items), merged_count=0, rules=limits, notes=[],
            reviewed_count=len(items), instruction=instruction, context_title=context_title,
            review=dict(checked=True, summary='已审查标题，表述准确', findings=[])))

    def remote(operation, payload=None, **kwargs):
        value.calls.append((operation, copy.deepcopy(payload)))
        if operation in value.hooks:
            return value.hooks[operation](payload)
        if operation == 'studio_detail':
            return copy.deepcopy(value.detail)
        cid = payload.get('cid')
        if operation == 'studio_chapter_rules':
            return dict(bvid=BV, aid=10, cid=cid, rules=copy.deepcopy(value.rules))
        if operation == 'studio_chapter_prepare':
            current = value.current.get(cid, dict(cards=[], permanent=False))
            cards = [dict(content=row['title'], **{'from': row['start_time'], 'to': row['end_time']}) for row in payload['items']]
            target = dict(bvid=BV, aid=10, cid=cid)
            return dict(**target, rules=copy.deepcopy(value.rules), current=copy.deepcopy(current['cards']),
                        current_permanent=current['permanent'], cards=cards, permanent=payload['permanent'],
                        fingerprint=f'fp-{cid}', snapshot=dict(target=target, fingerprint=f'fp-{cid}'))
        if operation == 'studio_chapter_write':
            assert any(node['status'] == 'committing' and node['cid'] == cid
                       for node in value.saves[-1]['delivery']['parts'])
            value.current[cid] = dict(cards=copy.deepcopy(payload['cards']), permanent=payload['permanent'])
            return dict(bvid=BV, aid=10, cid=cid, accepted=True, verified=True, **copy.deepcopy(value.current[cid]))
        if operation == 'studio_chapter_read':
            return dict(bvid=BV, aid=10, cid=cid, **copy.deepcopy(value.current.get(cid, dict(cards=[], permanent=False))))
        if operation == 'metadata':
            return dict(seasons=[dict(id=4, title='测试合集', sections=[dict(id=5, title='小节')])])
        if operation == 'studio_write':
            assert value.saves[-1]['delivery']['collection']['status'] == 'committing'
            value.detail['collection'] = (dict(id=payload['values']['season_id'], title='测试合集')
                                           if payload['values']['season_id'] is not None else None)
            value.detail['in_season'] = payload['values']['season_id'] is not None
            return dict(bvid=BV, aid=10, accepted=True)
        pytest.fail(f'未声明外部操作：{operation}')

    def collection_members(account, season, section, **kwargs):
        value.calls.append(('collection_order_read', dict(season_id=season, section_id=section)))
        return dict(episodes=[dict(aid=10)] if value.section_member else [])

    monkeypatch.setattr(chapters, 'adapt', adapt)
    monkeypatch.setattr(chapters.llm, 'chat_json', lambda *a, **k: pytest.fail('禁止真实模型请求'))
    monkeypatch.setattr(transport, 'call', remote)
    monkeypatch.setattr(service, 'read_collection_order', collection_members)
    value.remote = remote
    value.run = lambda **kwargs: delivery.run(value.snapshot, value.execution,
        save=lambda state: value.saves.append(copy.deepcopy(state)), **kwargs)
    return value


def calls(env, name):
    return [payload for operation, payload in env.calls if operation == name]


def test_new_cid_binding_shared_adaptation_and_completed_parts_are_not_repeated(env):
    original = copy.deepcopy(env.snapshot)
    result = env.run()
    assert result['outcome'] == 'completed'
    assert [node['cid'] for node in result['delivery']['parts']] == [11, 12]
    assert len(env.adaptations) == 1
    assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [11, 12]
    assert result['delivery']['collection']['status'] == 'skipped'
    assert env.snapshot == original
    assert env.run()['outcome'] == 'completed'
    assert len(calls(env, 'studio_chapter_write')) == 2
    assert result['stages']['verify']['status'] == 'completed'


@pytest.mark.parametrize('state', [None, 1, -1, False])
def test_review_wait_does_not_sleep_or_create_chapter_plans(env, state):
    env.detail['state'] = state
    result = env.run()
    assert result['outcome'] == 'waiting' and result['next_check'] > 0
    assert result['stages']['review']['status'] == 'waiting'
    assert not env.adaptations and not calls(env, 'studio_chapter_prepare')


@pytest.mark.parametrize('state', [-2, -16])
def test_review_rejection_reports_reason_and_preserves_new_video(env, state):
    env.detail.update(state=state, reject_reason='视频内容需修改')
    result = env.run()
    assert result['outcome'] == 'failed' and result['error_code'] == 'PUBLISH_UPDATE_REJECTED'
    assert '视频内容' in result['error']
    assert not env.adaptations and not calls(env, 'studio_chapter_write')


@pytest.mark.parametrize('damage', ['old_cid', 'missing_transfer', 'unrelated_cid', 'changed_order', 'wrong_owner', 'wrong_bv', 'wrong_aid'])
def test_unproven_binding_never_guesses_titles_or_reuses_old_cid(env, damage):
    if damage == 'old_cid':
        env.execution['transfers']['101']['cid'] = 1
    elif damage == 'missing_transfer':
        env.execution['transfers'].pop('101')
    elif damage == 'unrelated_cid':
        env.execution['transfers']['101']['cid'] = 3
        env.execution['transfers']['102']['cid'] = 3
    elif damage == 'changed_order':
        env.detail['videos'].reverse()
    elif damage == 'wrong_owner':
        env.detail['mid'] = '456'
    elif damage == 'wrong_bv':
        env.detail['bvid'] = 'BV0000000000'
    else:
        env.detail['aid'] = 20
    assert env.run()['error_code'] == 'PUBLISH_UPDATE_BINDING_INVALID'
    assert not env.adaptations and not calls(env, 'studio_chapter_write')


def test_new_cut_timeline_is_used_and_source_axis_is_not_reused(env):
    part = env.snapshot['parts'][1]
    cuts = dict(schema=1, source_fingerprint='source-hash', source_duration=30., output_duration=20.,
                ranges=[dict(start=0., end=10.)])
    part.update(output_cuts=cuts, duration=20.)
    part['chapter_items'], part['chapter_timeline'] = chapters.map_items(cuts, SOURCE)
    assert env.run()['outcome'] == 'completed'
    writes = calls(env, 'studio_chapter_write')
    assert writes[1]['cards'] == [dict(content='开场', **{'from': 0, 'to': 20})]
    assert env.adaptations[1] == [dict(title='Main', start_time=0., end_time=20.)]
    assert env.execution['delivery']['parts'][1]['items'][0]['source_indices'] == [1]


@pytest.mark.parametrize('damage', ['missing_items', 'missing_cut', 'wrong_duration', 'wrong_mapping'])
def test_bad_frozen_timeline_fails_only_affected_part(env, damage):
    part = env.snapshot['parts'][0]
    if damage == 'missing_items':
        part.pop('chapter_items')
    elif damage == 'missing_cut':
        part['chapter_timeline']['kind'] = 'cut'
    elif damage == 'wrong_duration':
        cuts = dict(schema=1, source_fingerprint='source-hash', source_duration=30., output_duration=20.,
                    ranges=[dict(start=0., end=10.)])
        part.update(output_cuts=cuts, chapter_timeline=dict(kind='cut'))
    else:
        part['chapter_items'][0]['start_time'] = 1
    result = env.run()
    assert result['outcome'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_TIMELINE_INVALID'
    assert [node['status'] for node in result['delivery']['parts']] == ['failed', 'completed']
    assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [12]


def test_one_adaptation_failure_keeps_other_part_and_retry_reuses_successful_cache(env):
    def fail_once(items):
        if len(env.adaptations) == 1:
            raise service.PublishError('整理暂时失败', code='PUBLISH_CHAPTER_ADAPT_FAILED')
    env.hooks['adapt'] = fail_once
    first = env.run()
    assert first['outcome'] == 'failed'
    assert [node['status'] for node in first['delivery']['parts']] == ['failed', 'completed']
    assert len(env.adaptations) == 2
    assert env.run()['outcome'] == 'completed'
    assert len(env.adaptations) == 2
    assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [12, 11]


@pytest.mark.parametrize('applied', [True, False])
def test_unknown_write_is_read_only_reconciled_before_any_more_parts(env, applied):
    def unknown(payload):
        if applied:
            env.current[payload['cid']] = dict(cards=copy.deepcopy(payload['cards']), permanent=payload['permanent'])
        raise transport.TransportError('网络响应中断')
    env.hooks['studio_chapter_write'] = unknown
    assert env.run()['outcome'] == 'unknown'
    assert len(calls(env, 'studio_chapter_write')) == 1
    env.hooks.pop('studio_chapter_write')
    result = env.run()
    assert result['outcome'] == ('waiting' if applied else 'unknown')
    assert len(calls(env, 'studio_chapter_write')) == 1
    assert len(calls(env, 'studio_chapter_read')) == 1
    if applied:
        assert env.run()['outcome'] == 'completed'
        assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [11, 12]
        assert len(env.adaptations) == 1


def test_different_existing_chapters_require_explicit_overwrite(env):
    env.current[11] = dict(cards=[dict(content='已有章节', **{'from': 0, 'to': 30})], permanent=True)
    assert env.run()['error_code'] == 'PUBLISH_CHAPTER_EXISTING'
    assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [12]


def test_explicit_overwrite_can_replace_existing_chapters(env):
    env.snapshot['chapters']['overwrite_existing'] = True
    env.current[11] = dict(cards=[dict(content='已有章节', **{'from': 0, 'to': 30})], permanent=True)
    assert env.run()['outcome'] == 'completed'
    assert env.current[11]['cards'][0]['content'] == '开场'


def test_noop_chapters_are_verified_without_write(env):
    current = dict(cards=[dict(content='开场', **{'from': 0, 'to': 10}),
                          dict(content='主体', **{'from': 10, 'to': 30})], permanent=True)
    env.current.update({11: copy.deepcopy(current), 12: copy.deepcopy(current)})
    assert env.run()['outcome'] == 'completed'
    assert not calls(env, 'studio_chapter_write')


def test_retained_part_requires_explicit_file_hash_binding(env):
    env.snapshot['parts'] = env.snapshot['parts'][:1]
    part = env.snapshot['parts'][0]
    part.update(replaced=False, old_cid=11)
    assert env.run()['error_code'] == 'PUBLISH_UPDATE_BINDING_INVALID'
    env.execution['transfers']['101']['sha256'] = part['sha256']
    assert env.run()['outcome'] == 'completed'
    assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [11]


def test_no_replacement_and_chapters_disabled_keep_remote_settings_untouched(env):
    env.snapshot.update(parts=[], chapters=dict(enabled=False), collection=dict(enabled=False))
    assert env.run()['outcome'] == 'completed'
    assert not env.adaptations and not calls(env, 'studio_chapter_write') and not calls(env, 'studio_write')


def test_collection_moves_and_verifies_exact_section_membership(env):
    env.snapshot.update(parts=[], chapters=dict(enabled=False), collection=dict(enabled=True, season_id=4, section_id=5))
    assert env.run()['outcome'] == 'completed'
    assert len(calls(env, 'studio_write')) == 1
    assert len(calls(env, 'collection_order_read')) == 1
    assert env.run()['outcome'] == 'completed'
    assert len(calls(env, 'studio_write')) == 1


def test_collection_noop_requires_section_not_just_season_id(env):
    env.snapshot.update(parts=[], chapters=dict(enabled=False), collection=dict(enabled=True, season_id=4, section_id=5))
    env.detail['collection'] = dict(id=4, title='测试合集')
    assert env.run()['outcome'] == 'completed'
    assert len(calls(env, 'collection_order_read')) == 1
    assert not calls(env, 'studio_write')


def test_collection_missing_membership_is_unknown_and_never_resubmitted(env):
    env.snapshot.update(parts=[], chapters=dict(enabled=False), collection=dict(enabled=True, season_id=4, section_id=5))
    env.section_member = False
    assert env.run()['outcome'] == 'unknown'
    assert len(calls(env, 'studio_write')) == 1
    assert env.run()['outcome'] == 'unknown'
    assert len(calls(env, 'studio_write')) == 1
    env.section_member = True
    assert env.run()['outcome'] == 'completed'
    assert len(calls(env, 'studio_write')) == 1


def test_frozen_identity_change_does_not_reuse_completed_results(env):
    assert env.run()['outcome'] == 'completed'
    env.snapshot['parts'][0]['sha256'] = 'other-file'
    assert env.run()['error_code'] == 'PUBLISH_UPDATE_SNAPSHOT_CHANGED'
    assert len(calls(env, 'studio_chapter_write')) == 2


def test_completed_change_receipt_recovers_after_local_checkpoint_loss(env):
    assert env.run()['outcome'] == 'completed'
    env.execution['delivery']['parts'][0]['status'] = 'committing'
    assert env.run()['outcome'] == 'completed'
    assert len(calls(env, 'studio_chapter_write')) == 2
    with Session(db.get_engine()) as session:
        assert all(row.status == 'done' for row in session.exec(select(PublishChange)).all())


def test_read_only_pending_chapters_and_collection_never_adapt_or_prepare_or_send(env):
    env.snapshot['collection'] = dict(enabled=True, season_id=4, section_id=5)
    result = env.run(read_only=True)
    assert result['outcome'] == 'waiting' and result['next_check'] == 0
    assert result['phase'] == '核对已完成，可继续剩余步骤'
    assert not env.adaptations
    assert not calls(env, 'studio_chapter_prepare') and not calls(env, 'studio_chapter_write')
    assert not calls(env, 'studio_write')


def test_read_only_prepared_chapter_plan_is_not_committed(env):
    assert env.run(read_only=True)['outcome'] == 'waiting'
    plan = studio.prepare('123', dict(bvid=BV, kind='chapters', values=dict(aid=10, cid=11, permanent=True,
        items=[dict(title='开场', start_time=0, end_time=10), dict(title='主体', start_time=10, end_time=30)])))
    node = env.execution['delivery']['parts'][0]
    node.update(change_id=plan['id'], status='prepared')
    env.calls.clear()
    assert env.run(read_only=True)['outcome'] == 'waiting'
    assert not env.adaptations and not calls(env, 'studio_chapter_prepare') and not calls(env, 'studio_chapter_write')
    with Session(db.get_engine()) as session:
        assert session.get(PublishChange, plan['id']).status == 'prepared'


def test_read_only_unknown_chapter_resolves_without_starting_any_remaining_parts(env):
    def unknown(payload):
        env.current[payload['cid']] = dict(cards=copy.deepcopy(payload['cards']), permanent=payload['permanent'])
        raise transport.TransportError('网络响应中断')
    env.hooks['studio_chapter_write'] = unknown
    assert env.run()['outcome'] == 'unknown'
    env.hooks.pop('studio_chapter_write')
    result = env.run(read_only=True)
    assert result['outcome'] == 'waiting' and result['next_check'] == 0
    assert [node['status'] for node in result['delivery']['parts']] == ['completed', 'pending']
    assert len(calls(env, 'studio_chapter_write')) == 1
    assert len(calls(env, 'studio_chapter_prepare')) == 1 and len(env.adaptations) == 1
    assert env.run(read_only=True)['outcome'] == 'waiting'
    assert len(calls(env, 'studio_chapter_write')) == 1


def test_read_only_completed_delivery_keeps_success_without_reapplying(env):
    assert env.run()['outcome'] == 'completed'
    before = (len(env.adaptations), len(calls(env, 'studio_chapter_write')), len(calls(env, 'studio_chapter_prepare')))
    assert env.run(read_only=True)['outcome'] == 'completed'
    assert before == (len(env.adaptations), len(calls(env, 'studio_chapter_write')), len(calls(env, 'studio_chapter_prepare')))


def test_adaptation_recorder_finishes_before_remote_chapter_preparation(env):
    original = env.remote
    # 适配完成后的首次准备请求必须已经看到诊断终态，而非仍在接收模型回复。
    def prepare(payload):
        progress = env.execution['delivery']['execution']
        assert progress['state'] == 'completed' and progress['finished_at']
        assert progress['phase'] in {'chapters.adapt.completed', 'chapters.adapt.cached'}
        env.hooks.pop('studio_chapter_prepare')
        try:
            return original('studio_chapter_prepare', payload)
        finally:
            env.hooks['studio_chapter_prepare'] = prepare
    env.hooks['studio_chapter_prepare'] = prepare
    assert env.run()['outcome'] == 'completed'
    assert env.execution['delivery']['execution']['state'] == 'completed'


@pytest.mark.parametrize(('code', 'state'), [('PUBLISH_CHAPTER_ADAPT_FAILED', 'failed'), ('PUBLISH_UPDATE_PAUSED', 'paused')])
def test_adaptation_exception_closes_recorder_with_actual_terminal_state(env, code, state):
    def fail(items):
        raise service.PublishError('章节整理暂未完成', code=code)
    env.hooks['adapt'] = fail
    result = env.run()
    progress = result['delivery']['execution']
    assert result['outcome'] == 'failed' and result['error_code'] == code
    assert progress['state'] == state and progress['finished_at']
    assert progress['phase'] == f'chapters.adapt.{state}'
    assert progress['label'] != '接收模型输出'
    assert not calls(env, 'studio_chapter_write')


@pytest.mark.parametrize('code', ['LLM_QUOTA_EXHAUSTED', 'LLM_SUBSCRIPTION_INVALID', 'LLM_PROVIDERS_FAILED'])
def test_model_service_failure_survives_updated_video_delivery_and_explicit_retry(env, code):
    from dubforge.errors import StepFailedError
    message = '模型服务不可用，请在模型设置中修复后继续'
    def unavailable(items):
        raise StepFailedError(message, code=code)
    env.hooks['adapt'] = unavailable

    failed = env.run()
    assert failed['outcome'] == 'failed'
    assert failed['error_code'] == failed['stages']['chapters']['error_code'] == code
    assert failed['error'] == failed['stages']['chapters']['error'] == message
    assert all(node['error_code'] == code for node in failed['delivery']['parts'])
    assert failed['delivery']['execution']['state'] == 'failed'
    assert not calls(env, 'studio_chapter_prepare') and not calls(env, 'studio_chapter_write')

    env.hooks.pop('adapt')
    assert env.run()['outcome'] == 'completed'
    assert [row['cid'] for row in calls(env, 'studio_chapter_write')] == [11, 12]
    assert all(node['error_code'] == '' for node in env.execution['delivery']['parts'])


def test_adaptation_validation_failure_also_finishes_recorder(env, monkeypatch):
    def reject(*args, **kwargs):
        raise service.PublishError('整理结果校验不通过', code='PUBLISH_CHAPTER_ADAPT_CACHE_INVALID')
    monkeypatch.setattr(chapters, 'validate_adaptation', reject)
    result = env.run()
    assert result['outcome'] == 'failed'
    assert result['delivery']['execution']['state'] == 'failed'
    assert result['delivery']['execution']['finished_at']
    assert not calls(env, 'studio_chapter_write')


def test_cached_adaptation_clears_old_active_recorder_without_new_model_call(env):
    assert env.run()['outcome'] == 'completed'
    node = env.execution['delivery']['parts'][0]
    node.update(status='failed', change_id='')
    env.execution['delivery']['execution'].update(state='running', label='接收模型输出', finished_at=None)
    count = len(env.adaptations)
    assert env.run()['outcome'] == 'completed'
    progress = env.execution['delivery']['execution']
    assert len(env.adaptations) == count
    assert progress['state'] == 'completed' and progress['finished_at']
    assert progress['phase'] == 'chapters.adapt.cached'
    assert progress['metrics'] == dict(cached=len(env.execution['delivery']['parts'][0]['items']))


@pytest.mark.parametrize('state', [-2, -16, -4, None])
def test_metadata_only_update_does_not_wait_for_unchanged_video_review(env, state):
    env.snapshot['parts'] = []
    env.snapshot['chapters']['enabled'] = False
    env.detail['state'] = state
    result = env.run()
    assert result['outcome'] == 'completed'
    assert result['stages']['review']['status'] == 'skipped'
    assert not calls(env, 'studio_chapter_write')
