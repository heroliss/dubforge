"""自动章节交付走真实 studio 状态机，网络与翻译全部隔离。"""
import copy
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from dubforge import db
from dubforge.errors import DubForgeError
from dubforge.errors import StepFailedError
from dubforge.publishing import chapter_sync as sync, chapters, credentials, service, studio, transport
from dubforge.publishing.models import Publication, PublishAccount, PublishChange, now

BV = 'BV1234567890'
SOURCE = [dict(title='Introduction', start_time=0., end_time=10.),
          dict(title='Main content', start_time=10., end_time=30.)]


@pytest.fixture
def env(make_project, monkeypatch):
    uid = make_project()
    sync.startup()
    with sync._lock:
        sync._running.clear()
    with Session(db.get_engine()) as session:
        session.add(PublishAccount(id='123', name='测试账号', credential='fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda _: {'fixture': True})
    value = SimpleNamespace(uid=uid, calls=[], adapted=[], translated=[], current={}, hooks={},
                            review_findings=[], review_summary='已逐条核对标题，表述客观且符合源主题',
                            rules=dict(limit=10, stay=5, space=0, max_title_length=16))
    value.detail = dict(bvid=BV, aid=10, mid='123', version='v1',
                        videos=[dict(cid=1, title='原声'), dict(cid=2, title='配音')])

    def adapt(items, *, duration=None, translate=True, rules=None, instruction='', context_title='', cancel_check=None, progress=None):
        if cancel_check:
            cancel_check()
        value.adapted.append(dict(items=copy.deepcopy(items), duration=duration, translate=translate,
                                  rules=copy.deepcopy(rules), instruction=instruction))
        if translate:
            value.translated.append(copy.deepcopy(items))
        limits = chapters.platform_constraints(rules)
        count = min(len(items), limits['max_chapters'])
        output = []
        for index in range(count):
            start, end = index * len(items) // count, (index + 1) * len(items) // count - 1
            title = {'Introduction': '开场', 'Main content': '主体'}.get(items[start]['title'], f'主题{index + 1}') if translate else items[start]['title']
            output.append(dict(title=title, start_time=0 if index == 0 else math.floor(items[start]['start_time'] + .5),
                end_time=math.floor((items[end + 1]['start_time'] if end + 1 < len(items)
                                     else duration if duration is not None else items[-1]['end_time']) + .5),
                source_indices=list(range(start, end + 1))))
        return dict(items=output, source='llm.chapter_adaptation',
                    adaptation=dict(schema=chapters.ADAPTATION_VERSION, source_count=len(items),
                                    output_count=count, merged_count=len(items) - count, rules=limits, notes=[],
                                    reviewed_count=count, instruction=instruction.strip(), context_title=context_title,
                                    review=dict(checked=True, summary=value.review_summary,
                                                findings=copy.deepcopy(value.review_findings))))

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
            target = dict(bvid=BV, aid=10, cid=cid)
            cards = [dict(content=row['title'], **{'from': row['start_time'], 'to': row['end_time']})
                     for row in payload['items']]
            return dict(**target, rules=copy.deepcopy(value.rules),
                        current=copy.deepcopy(current['cards']), current_permanent=current['permanent'],
                        cards=cards, permanent=payload['permanent'], fingerprint=f'fp-{cid}',
                        snapshot=dict(target=target, fingerprint=f'fp-{cid}'))
        if operation == 'studio_chapter_write':
            value.current[cid] = dict(cards=copy.deepcopy(payload['cards']), permanent=payload['permanent'])
            return dict(bvid=BV, aid=10, cid=cid, accepted=True, verified=True,
                        **copy.deepcopy(value.current[cid]))
        if operation == 'studio_chapter_read':
            return dict(bvid=BV, aid=10, cid=cid,
                        **copy.deepcopy(value.current.get(cid, dict(cards=[], permanent=False))))
        pytest.fail(f'测试未声明网络操作：{operation}')

    monkeypatch.setattr(chapters, 'adapt', adapt)
    monkeypatch.setattr(chapters.llm, 'chat_json', lambda *a, **k: pytest.fail('同步测试不得调用真实模型'))
    monkeypatch.setattr(transport, 'call', remote)
    yield value
    sync.startup()


def publication(env, *, enabled=True, translate=True, parts=None, transfers=True, source=None, instruction=''):
    source = SOURCE if source is None else source
    if parts is None:
        parts = [dict(artifact_id=100+index, title=title, sha256=f'file-{index}', duration=30,
                      output_cuts=None, chapter_items=copy.deepcopy(source), chapter_timeline=dict(kind='source'))
                 for index, title in enumerate(('原声', '配音'), 1)]
    options = dict(enabled=enabled, translate=translate, permanent=True, items=copy.deepcopy(source))
    if instruction:
        options['instruction'] = instruction
    snapshot = dict(account_id='123', chapter_options=options, parts=parts, settings=dict(service.DEFAULTS), origins={})
    receipt = dict(bvid=BV, aid=10, remote_state=0, season_id=7,
                   videos=copy.deepcopy(env.detail['videos']))
    uploads = {str(part['artifact_id']): dict(cid=index, filename=f'upload-{index}')
               for index, part in enumerate(parts, 1)} if transfers else {}
    with Session(db.get_engine()) as session:
        row = Publication(project_uid=env.uid, status='published', snapshot=service._json(snapshot),
                          draft=service._json(dict(chapter_options=options)), transfers=service._json(uploads),
                          receipt=service._json(receipt))
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def saved(key):
    with Session(db.get_engine()) as session:
        return session.get(Publication, key)


def edit(key, **values):
    with Session(db.get_engine()) as session:
        row = session.get(Publication, key)
        for name, value in values.items():
            setattr(row, name, value)
        session.add(row)
        session.commit()


def calls(env, operation):
    return [payload for action, payload in env.calls if action == 'studio_' + operation]


def make_change(*, cid=1, status='prepared', existing=False):
    items = [dict(title='开场', start_time=0, end_time=10), dict(title='主体', start_time=10, end_time=30)]
    cards = [dict(content=row['title'], **{'from': row['start_time'], 'to': row['end_time']}) for row in items]
    with Session(db.get_engine()) as session:
        row = PublishChange(account_id='123', bvid=BV, kind='chapters', status=status,
            payload=service._json(dict(account_id='123', bvid=BV, aid=10, cid=cid, cards=cards, permanent=True,
                                      prepared=dict(fingerprint=f'fp-{cid}'))),
            preview=service._json(dict(before=dict(items=items if existing else []),
                                      after=dict(items=items, permanent=True))))
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_two_parts_share_same_timeline_adaptation_and_preserve_publication_then_repeat_is_noop(env):
    key = publication(env)
    before = saved(key)
    result = sync.run(key)
    assert result['status'] == 'completed'
    assert [part['cid'] for part in result['parts']] == [1, 2]
    assert len(env.translated) == 1
    assert len(env.adapted) == 1
    assert [row['cid'] for row in calls(env, 'chapter_rules')] == [1, 2]
    assert len(result['plans']) == 1
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert all(row['cards'] == [{'from': 0, 'to': 10, 'content': '开场'},
                                 {'from': 10, 'to': 30, 'content': '主体'}]
               for row in calls(env, 'chapter_write'))
    after = saved(key)
    for path in ('status', 'snapshot', 'receipt', 'draft', 'transfers', 'revision', 'updated_at'):
        assert getattr(after, path) == getattr(before, path)
    with Session(db.get_engine()) as session:
        rows = session.exec(select(PublishChange)).all()
        assert len(rows) == 2 and all(row.publication_id == '' for row in rows)
    assert sync.run(key)['status'] == 'completed'
    assert len(calls(env, 'chapter_write')) == 2 and len(env.translated) == 1


def test_cut_part_is_adapted_after_frozen_mapping_and_keeps_source_index(env):
    key = publication(env)
    snapshot = json.loads(saved(key).snapshot)
    cut = dict(schema=1, source_fingerprint='cut-source', source_duration=30., output_duration=20.,
               ranges=[dict(start=0., end=10.)])
    part = snapshot['parts'][1]
    part['output_cuts'], part['duration'] = cut, 20
    part['chapter_items'], part['chapter_timeline'] = chapters.map_items(cut, SOURCE)
    edit(key, snapshot=service._json(snapshot))
    assert sync.run(key)['status'] == 'completed'
    writes = calls(env, 'chapter_write')
    assert len(writes[0]['cards']) == 2
    assert writes[1]['cards'] == [{'from': 0, 'to': 20, 'content': '主体'}]
    assert env.translated == [SOURCE, [dict(title='Main content', start_time=0., end_time=20.)]]
    assert [call['duration'] for call in env.adapted] == [30, 20]
    assert len(sync.view(key)['plans']) == 2
    assert sync.view(key)['parts'][1]['items'][0]['source_indices'] == [1]


@pytest.mark.parametrize('damage', ['missing', 'different', 'cut_without_snapshot'])
def test_unproven_or_changed_part_timeline_never_falls_back_to_source(env, damage):
    key = publication(env)
    snapshot = json.loads(saved(key).snapshot)
    part = snapshot['parts'][1]
    if damage == 'missing':
        part.pop('chapter_items')
    elif damage == 'different':
        part['chapter_items'][0]['start_time'] = 1
    else:
        part['chapter_timeline']['kind'] = 'cut'
    edit(key, snapshot=service._json(snapshot))
    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_TIMELINE_INVALID'
    assert not env.calls and not env.translated


def test_confirmed_upload_cids_allow_remote_part_order_changes(env):
    key = publication(env)
    receipt = json.loads(saved(key).receipt)
    receipt['videos'].reverse()
    edit(key, receipt=service._json(receipt))
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]


def test_legacy_binding_requires_exact_unique_title_and_order(env):
    key = publication(env, transfers=False)
    receipt = json.loads(saved(key).receipt)
    receipt['videos'].reverse()
    edit(key, receipt=service._json(receipt))
    result = sync.run(key)
    assert result['error_code'] == 'PUBLISH_CHAPTER_BINDING_INVALID'
    assert not env.calls and not env.translated


def test_pending_receipt_checks_are_delayed_bounded_and_do_not_touch_remote(env, monkeypatch):
    key = publication(env)
    receipt = json.loads(saved(key).receipt)
    receipt['videos'] = []
    edit(key, receipt=service._json(receipt))
    env.detail['videos'] = []
    clock = [1000.]
    monkeypatch.setattr(sync.time, 'time', lambda: clock[0])
    for attempt in range(1, sync.MAX_PENDING_ATTEMPTS + 1):
        result = sync.run(key)
        assert result['attempts'] == attempt
        if attempt < sync.MAX_PENDING_ATTEMPTS:
            assert result['status'] == 'pending' and result['next_check'] == clock[0] + 120
            assert sync.run(key)['attempts'] == attempt
            clock[0] += 121
    assert result['status'] == 'failed'
    assert sync.run(key)['attempts'] == sync.MAX_PENDING_ATTEMPTS
    assert len(calls(env, 'detail')) == sync.MAX_PENDING_ATTEMPTS
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write') and not env.translated


def test_incomplete_approved_receipt_is_refreshed_read_only_then_can_finish(env):
    key = publication(env)
    receipt = json.loads(saved(key).receipt)
    receipt['videos'] = []
    edit(key, receipt=service._json(receipt))
    original_receipt = saved(key).receipt
    env.detail['videos'] = [dict(cid=1, title='原声')]
    result = sync.run(key)
    assert result['status'] == 'pending' and result['error_code'] == 'PUBLISH_CHAPTER_NOT_READY'
    assert len(calls(env, 'detail')) == 1 and not calls(env, 'chapter_write') and not env.translated
    env.detail['videos'].append(dict(cid=2, title='配音'))
    result['next_check'] = 0
    edit(key, chapter_sync=service._json(result))
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert saved(key).receipt == original_receipt
    assert len(env.translated) == 1


@pytest.mark.parametrize('mismatch', ['bvid', 'aid', 'mid', 'cid'])
def test_incomplete_receipt_readback_must_match_account_bv_aid_and_uploaded_cids(env, mismatch):
    key = publication(env)
    receipt = json.loads(saved(key).receipt)
    receipt['videos'] = []
    edit(key, receipt=service._json(receipt))
    if mismatch == 'cid':
        env.detail['videos'][1]['cid'] = 99
    else:
        env.detail[mismatch] = {'bvid': 'BV9999999999', 'aid': 99, 'mid': '999'}[mismatch]
    result = sync.run(key)
    assert result['status'] == ('pending' if mismatch == 'cid' else 'failed')
    assert result['error_code'] == ('PUBLISH_CHAPTER_NOT_READY' if mismatch == 'cid'
                                    else 'PUBLISH_CHAPTER_BINDING_INVALID')
    assert not env.translated and not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')


def test_incomplete_receipt_temporary_read_failure_is_bounded_pending(env):
    key = publication(env)
    receipt = json.loads(saved(key).receipt)
    receipt['videos'] = []
    edit(key, receipt=service._json(receipt))
    env.hooks['studio_detail'] = lambda _: (_ for _ in ()).throw(
        transport.TransportError('远端查询暂时超时', code='PUBLISH_WORKER_TIMEOUT'))
    result = sync.run(key)
    assert result['status'] == 'pending' and result['attempts'] == 1
    assert result['error_code'] == 'PUBLISH_WORKER_TIMEOUT'
    assert not env.translated and not calls(env, 'chapter_write')


def test_rule_delay_retries_only_unfinished_part_and_reuses_translation(env, monkeypatch):
    key = publication(env)
    failures = [True]
    def prepare(payload):
        if payload['cid'] == 2 and failures[0]:
            raise transport.TransportError('平台章节规则尚未就绪', code='PUBLISH_CHAPTER_RULE_UNAVAILABLE')
        del env.hooks['studio_chapter_prepare']
        try:
            return transport.call('studio_chapter_prepare', payload)
        finally:
            env.hooks['studio_chapter_prepare'] = prepare
    env.hooks['studio_chapter_prepare'] = prepare
    result = sync.run(key)
    assert result['status'] == 'pending'
    assert [part['status'] for part in result['parts']] == ['completed', 'failed']
    failures[0] = False
    state = copy.deepcopy(result)
    state['next_check'] = 0
    edit(key, chapter_sync=service._json(state))
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert len(env.translated) == 1


def test_legacy_twenty_one_title_cache_is_replanned_and_both_parts_receive_at_most_ten(env):
    source = [dict(title=f'Source topic {index}', start_time=index * 10, end_time=(index + 1) * 10)
              for index in range(21)]
    parts = [dict(artifact_id=100 + index, title=title, sha256=f'file-{index}', duration=210,
                  output_cuts=None, chapter_items=copy.deepcopy(source), chapter_timeline=dict(kind='source'))
             for index, title in enumerate(('原声', '配音'), 1)]
    key = publication(env, source=source, parts=parts)
    legacy = dict(schema=1, status='failed', error_code='PUBLISH_CHAPTER_LIMIT_EXCEEDED',
                  translation=[f'旧译文{index}' for index in range(21)], parts=[])
    edit(key, chapter_sync=service._json(legacy))
    before = saved(key)

    sync.reset_for_retry(key)
    result = sync.run(key)

    assert result['status'] == 'completed'
    assert len(env.adapted) == 1 and len(result['plans']) == 1
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    for part in result['parts']:
        assert len(part['items']) == 10
        assert part['adaptation']['source_count'] == 21
        assert part['adaptation']['output_count'] == 10
        assert [index for item in part['items'] for index in item['source_indices']] == list(range(21))
        assert all(not item['title'].startswith('旧译文') for item in part['items'])
    for write in calls(env, 'chapter_write'):
        assert len(write['cards']) == 10
        assert write['cards'][0]['from'] == 0 and write['cards'][-1]['to'] == 210
    after = saved(key)
    assert (after.status, after.receipt, after.snapshot, after.transfers) == (before.status, before.receipt, before.snapshot, before.transfers)
    assert all(action.startswith('studio_') for action, _ in env.calls)


def test_legacy_twenty_one_fingerprint_and_title_only_cache_can_retry_under_new_review_contract(env):
    source = [dict(title=f'Source topic {index}', start_time=index * 10, end_time=(index + 1) * 10)
              for index in range(21)]
    parts = [dict(artifact_id=100 + index, title=title, sha256=f'file-{index}', duration=210,
                  output_cuts=None, chapter_items=copy.deepcopy(source), chapter_timeline=dict(kind='source'))
             for index, title in enumerate(('原声', '配音'), 1)]
    key = publication(env, source=source, parts=parts)
    before = saved(key)
    snapshot, receipt, transfers = map(json.loads, (before.snapshot, before.receipt, before.transfers))
    bound = sync._bind(snapshot, receipt, transfers)
    old_options = service._chapter_options(snapshot['chapter_options'])
    old_options.pop('instruction')
    old_fingerprint = service._hash(dict(account='123', bvid=BV, aid=10, options=old_options,
        parts=[{name: part.get(name) for name in ('artifact_id', 'sha256', 'cid', 'chapter_items', 'output_cuts', 'duration')}
               for part in bound]))
    # 旧记录既没有 instruction，也没有review，不能因加了空默认项而误判源资料变化。
    edit(key, chapter_sync=service._json(dict(schema=1, status='failed',
        error_code='PUBLISH_CHAPTER_LIMIT_EXCEEDED', fingerprint=old_fingerprint, parts=[],
        translation=[f'旧逐条译文{index}' for index in range(21)])))

    sync.reset_for_retry(key)
    result = sync.run(key)

    assert result['status'] == 'completed' and result['fingerprint'] == old_fingerprint
    assert len(env.adapted) == 1 and env.adapted[0]['instruction'] == ''
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert all(len(part['items']) == 10 and part['adaptation']['review']['checked']
               and part['adaptation']['schema'] == chapters.ADAPTATION_VERSION for part in result['parts'])
    after = saved(key)
    for name in ('status', 'receipt', 'snapshot', 'transfers'):
        assert getattr(after, name) == getattr(before, name)


def test_original_language_still_gets_review_and_same_timeline_reuses_reviewed_plan(env):
    key = publication(env, translate=False, instruction='保留型号版本，标题采用客观表述')
    result = sync.run(key)
    assert result['status'] == 'completed'
    assert len(env.adapted) == 1 and not env.translated
    assert env.adapted[0]['instruction'] == '保留型号版本，标题采用客观表述'
    assert len(result['plans']) == 1
    assert all(part['adaptation']['review']['checked'] and part['adaptation']['reviewed_count'] == 2
               and part['adaptation']['instruction'] == env.adapted[0]['instruction'] for part in result['parts'])


def test_review_needs_manual_check_preserves_candidate_but_never_prepares_or_writes_remote(env):
    env.review_findings = [dict(source_indices=[0], action='needs_review', reason='源标题的信息不足，请核对具体型号')]
    key = publication(env)
    before = saved(key)
    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_REVIEW_REQUIRED'
    assert result['parts'][0]['status'] == 'failed' and result['parts'][0]['items'][0]['source_indices'] == [0]
    assert result['parts'][0]['adaptation']['review']['findings'] == env.review_findings
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')
    assert len(env.adapted) == 1
    # 明确重试不能把仍需核对的缓存计划当成已审查通过，更不能为补章节重发视频。
    sync.reset_for_retry(key)
    retried = sync.run(key)
    assert retried['error_code'] == 'PUBLISH_CHAPTER_REVIEW_REQUIRED' and len(env.adapted) == 1
    after = saved(key)
    for name in ('status', 'receipt', 'snapshot', 'transfers'):
        assert getattr(after, name) == getattr(before, name)
    assert all(action.startswith('studio_') for action, _ in env.calls)
    assert not calls(env, 'chapter_write')


def test_cut_part_review_findings_are_remapped_to_original_source_indices(env):
    env.review_findings = [dict(source_indices=[0], action='rewritten', reason='保留原主题并精简标题')]
    key = publication(env)
    snapshot = json.loads(saved(key).snapshot)
    cut = dict(schema=1, source_fingerprint='cut-source', source_duration=30., output_duration=20.,
               ranges=[dict(start=0., end=10.)])
    part = snapshot['parts'][1]
    part['output_cuts'], part['duration'] = cut, 20
    part['chapter_items'], part['chapter_timeline'] = chapters.map_items(cut, SOURCE)
    edit(key, snapshot=service._json(snapshot))
    result = sync.run(key)
    assert result['status'] == 'completed' and len(env.adapted) == 2
    assert result['parts'][0]['adaptation']['review']['findings'][0]['source_indices'] == [0]
    assert result['parts'][1]['adaptation']['review']['findings'][0]['source_indices'] == [1]
    # 缓存使用当前成片的局部索引，展示映射不得反向污染共享计划。
    assert all(plan['adaptation']['review']['findings'][0]['source_indices'] == [0]
               for plan in result['plans'].values())


def test_changed_nonempty_instruction_gets_a_new_reviewed_cache_entry(env):
    key = publication(env)
    record = saved(key)
    snapshot, receipt, transfers = map(json.loads, (record.snapshot, record.receipt, record.transfers))
    target = sync._bind(snapshot, receipt, transfers)[0]
    state = dict(schema=1, status='running', parts=[], plans={})
    indices = sync._source_indices(target, SOURCE)
    part = dict(cid=1, title='原声')
    options = service._chapter_options(snapshot['chapter_options'])
    options['instruction'] = '保留必要的英文型号'
    sync._adapt(key, state, '123', BV, 10, part, target, options, indices, project_uid=env.uid)
    first_key = part['plan_key']
    # 相同要求复用，修改补充要求后只重新整理标题；不改变时点也不写远端。
    sync._adapt(key, state, '123', BV, 10, part, target, options, indices, project_uid=env.uid)
    assert len(env.adapted) == 1 and part['plan_key'] == first_key
    options['instruction'] = '使用简洁客观的技术标题'
    sync._adapt(key, state, '123', BV, 10, part, target, options, indices, project_uid=env.uid)
    assert len(env.adapted) == 2 and len(state['plans']) == 2 and part['plan_key'] != first_key
    assert [call['instruction'] for call in env.adapted] == ['保留必要的英文型号', '使用简洁客观的技术标题']
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')


def test_cached_plan_without_review_is_regenerated_before_any_write(env):
    key = publication(env)
    env.hooks['studio_chapter_prepare'] = lambda _: (_ for _ in ()).throw(
        transport.TransportError('预检稍后重试', code='PUBLISH_CHAPTER_RULE_UNAVAILABLE'))
    first = sync.run(key)
    assert first['status'] == 'pending' and len(env.adapted) == 1
    plan = first['plans'][first['parts'][0]['plan_key']]
    plan['adaptation'].pop('review')
    edit(key, chapter_sync=service._json(first))
    env.hooks.pop('studio_chapter_prepare')
    sync.reset_for_retry(key)
    result = sync.run(key)
    assert result['status'] == 'completed' and len(env.adapted) == 2
    assert all(part['adaptation']['review']['checked'] for part in result['parts'])
    assert len(calls(env, 'chapter_write')) == 2


def test_changed_live_rules_invalidate_saved_plan_before_retry(env):
    key = publication(env)
    env.hooks['studio_chapter_prepare'] = lambda _: (_ for _ in ()).throw(
        transport.TransportError('模拟预检暂时失败', code='PUBLISH_CHAPTER_RULE_UNAVAILABLE'))
    first = sync.run(key)
    assert first['status'] == 'pending' and len(first['plans']) == 1
    assert len(env.adapted) == 1 and not calls(env, 'chapter_write')
    old_key = first['parts'][0]['plan_key']
    env.hooks.pop('studio_chapter_prepare')
    env.rules['limit'] = 1

    sync.reset_for_retry(key)
    result = sync.run(key)

    assert result['status'] == 'completed'
    assert len(env.adapted) == 2
    assert len(result['plans']) == 2
    assert result['parts'][0]['plan_key'] != old_key
    assert result['parts'][0]['plan_key'] == result['parts'][1]['plan_key']
    assert all(len(row['cards']) == 1 for row in calls(env, 'chapter_write'))
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]


def test_retry_with_stricter_rules_preserves_completed_part_and_replans_only_unfinished_part(env):
    key = publication(env)

    def second_unavailable(payload):
        if payload['cid'] == 2:
            raise transport.TransportError('第二P章节规则暂未就绪', code='PUBLISH_CHAPTER_RULE_UNAVAILABLE')
        return dict(bvid=BV, aid=10, cid=1, rules=copy.deepcopy(env.rules))

    env.hooks['studio_chapter_rules'] = second_unavailable
    first = sync.run(key)
    assert first['status'] == 'pending'
    assert [part['status'] for part in first['parts']] == ['completed', 'failed']
    completed_items = copy.deepcopy(first['parts'][0]['items'])
    env.hooks.pop('studio_chapter_rules')
    env.rules['limit'] = 1

    sync.reset_for_retry(key)
    result = sync.run(key)

    assert result['status'] == 'completed'
    assert result['parts'][0]['items'] == completed_items
    assert len(result['parts'][0]['items']) == 2 and len(result['parts'][1]['items']) == 1
    assert len(env.adapted) == 2
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert [row['cid'] for row in calls(env, 'chapter_prepare')] == [1, 2]


def test_damaged_cached_adaptation_is_revalidated_and_regenerated(env):
    key = publication(env)
    env.hooks['studio_chapter_prepare'] = lambda _: (_ for _ in ()).throw(
        transport.TransportError('预检稍后再试', code='PUBLISH_CHAPTER_RULE_UNAVAILABLE'))
    first = sync.run(key)
    assert first['status'] == 'pending' and len(env.adapted) == 1
    plan = first['plans'][first['parts'][0]['plan_key']]
    plan['items'][0]['source_indices'] = [0, 0]
    edit(key, chapter_sync=service._json(first))
    env.hooks.pop('studio_chapter_prepare')

    sync.reset_for_retry(key)
    result = sync.run(key)

    assert result['status'] == 'completed' and len(env.adapted) == 2
    assert result['parts'][0]['items'][0]['source_indices'] == [0]
    assert len(calls(env, 'chapter_write')) == 2


def test_rules_are_read_and_bound_to_target_before_adaptation_or_write(env):
    key = publication(env)
    env.hooks['studio_chapter_rules'] = lambda payload: dict(bvid=BV, aid=10, cid=99,
                                                           rules=copy.deepcopy(env.rules))
    result = sync.run(key)
    assert result['status'] == 'pending' and result['error_code'] == 'PUBLISH_CHAPTER_RULE_UNAVAILABLE'
    assert len(calls(env, 'chapter_rules')) == 1
    assert not env.adapted and not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')


def test_unknown_commit_is_never_resent_and_readback_does_not_start_other_parts(env):
    key = publication(env)
    def timeout(payload):
        env.current[payload['cid']] = dict(cards=copy.deepcopy(payload['cards']), permanent=payload['permanent'])
        raise transport.TransportError('写入后连接中断')
    env.hooks['studio_chapter_write'] = timeout
    result = sync.run(key)
    assert result['status'] == 'unknown' and result['parts'][0]['change_id']
    with pytest.raises(DubForgeError) as error:
        sync.reset_for_retry(key)
    assert error.value.code == 'PUBLISH_CHAPTER_UNKNOWN'
    env.hooks.pop('studio_chapter_write')
    reconciled = sync.run(key)
    assert reconciled['status'] == 'failed'
    assert [part['status'] for part in reconciled['parts']] == ['completed', 'pending']
    assert len(calls(env, 'chapter_read')) == 1 and len(calls(env, 'chapter_write')) == 1
    assert sync.run(key)['status'] == 'failed'
    sync.reset_for_retry(key)
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert len(env.translated) == 1


def test_unknown_unconfirmed_readback_remains_unknown_without_writes(env):
    key = publication(env)
    env.hooks['studio_chapter_write'] = lambda payload: (_ for _ in ()).throw(transport.TransportError('连接中断'))
    assert sync.run(key)['status'] == 'unknown'
    assert sync.run(key, reconcile_only=True)['status'] == 'unknown'
    assert sync.run(key)['status'] == 'unknown'
    assert len(calls(env, 'chapter_write')) == 1 and len(calls(env, 'chapter_read')) == 2


def test_existing_remote_manual_chapters_are_not_overwritten_even_after_prepared_resume(env, monkeypatch):
    key = publication(env)
    env.current[1] = dict(cards=[{'from': 0, 'to': 30, 'content': '人工章节'}], permanent=True)
    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_EXISTING'
    assert not calls(env, 'chapter_write')
    # 模拟 prepare 已持久化后进程中断，恢复不能绕过现有章节保护。
    result.update(status='pending')
    result['parts'][0]['status'] = 'pending'
    edit(key, chapter_sync=service._json(result))
    assert sync.run(key)['error_code'] == 'PUBLISH_CHAPTER_EXISTING'
    assert not calls(env, 'chapter_write')


def test_remote_already_identical_is_completed_without_post(env):
    key = publication(env)
    expected = dict(cards=[{'from': 0, 'to': 10, 'content': '开场'},
                           {'from': 10, 'to': 30, 'content': '主体'}], permanent=True)
    env.current.update({1: copy.deepcopy(expected), 2: copy.deepcopy(expected)})
    assert sync.run(key)['status'] == 'completed'
    assert not calls(env, 'chapter_write')


def test_other_unconfirmed_manual_write_does_not_claim_this_run_was_sent(env):
    key = publication(env)
    make_change(status='unknown')
    result = sync.run(key)
    assert result['status'] == 'failed'
    assert result['error_code'] == 'PUBLISH_WRITE_UNKNOWN'
    assert result['parts'][0]['status'] == 'failed' and not result['parts'][0]['change_id']
    assert not calls(env, 'chapter_write')
    # 此记录没有未知写入；不会冒充其它人工修改而发出错误的核对请求。
    sync.reset_for_retry(key)
    assert sync.run(key, reconcile_only=True)['status'] == 'failed'
    assert not calls(env, 'chapter_read')


@pytest.mark.parametrize('status,expected', [('committing', 'unknown'), ('unknown', 'unknown'),
                                           ('prepared', 'failed'), ('done', 'completed')])
def test_startup_recovery_uses_local_change_evidence_without_remote(env, status, expected):
    key = publication(env)
    change = make_change(status=status)
    state = dict(schema=1, status='running', parts=[dict(cid=1, title='原声', status='running', change_id=change)])
    edit(key, chapter_sync=service._json(state))
    sync.recover()
    result = sync.view(key)
    assert result['status'] == expected and result['parts'][0]['status'] == expected
    assert not env.calls and not env.translated


def test_interrupted_preparation_is_not_reported_completed_and_corrupt_record_does_not_block_recovery(env):
    key = publication(env)
    bad = publication(env)
    edit(key, chapter_sync=service._json(dict(schema=1, status='running', parts=[])))
    edit(bad, chapter_sync='not-json')
    sync.recover()
    assert sync.view(key)['status'] == 'failed'
    assert saved(bad).chapter_sync == 'not-json'
    assert not env.calls


def test_successful_commit_not_yet_reflected_in_part_state_is_not_repeated(env, monkeypatch):
    key = publication(env)
    original = studio.commit
    def interrupted_save(*args):
        original(*args)
        raise RuntimeError('模拟回执落库后、章节状态落库前中断')
    monkeypatch.setattr(studio, 'commit', interrupted_save)
    assert sync.run(key)['status'] == 'failed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1]
    # 重试也必须识别已经 done 的 studio 回执，不能重复发送这一 P。
    sync.reset_for_retry(key)
    monkeypatch.setattr(studio, 'commit', original)
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]
    assert [row['cid'] for row in calls(env, 'chapter_prepare')] == [1, 2]


def test_disabled_old_record_only_enabled_by_explicit_confirmation(env):
    key = publication(env, enabled=False)
    assert sync.run(key)['status'] == 'skipped'
    assert not env.translated and not env.calls
    with pytest.raises(DubForgeError) as error:
        sync.reset_for_retry(key)
    assert error.value.code == 'PUBLISH_CHAPTER_DISABLED'
    revision = saved(key).revision
    sync.reset_for_retry(key, enable=True)
    assert saved(key).revision == revision + 1
    assert json.loads(saved(key).snapshot)['chapter_options']['enabled'] is True
    assert json.loads(saved(key).draft)['chapter_options']['enabled'] is True
    assert sync.run(key)['status'] == 'completed'


def test_original_titles_when_translation_disabled_and_missing_last_end_uses_part_duration(env):
    key = publication(env, translate=False)
    snapshot = json.loads(saved(key).snapshot)
    snapshot['chapter_options']['items'][-1]['end_time'] = None
    for part in snapshot['parts']:
        part['chapter_items'][-1]['end_time'] = None
    edit(key, snapshot=service._json(snapshot))
    assert sync.run(key)['status'] == 'completed'
    assert not env.translated
    assert calls(env, 'chapter_write')[0]['cards'][-1] == {'from': 10, 'to': 30, 'content': 'Main content'}


def test_concurrent_run_does_not_start_duplicate_preparation_or_commit(env, monkeypatch):
    key = publication(env)
    entered, release = threading.Event(), threading.Event()
    original = chapters.adapt
    def blocked(items, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(items, **kwargs)
    monkeypatch.setattr(chapters, 'adapt', blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(sync.run, key)
        assert entered.wait(5)
        try:
            assert sync.run(key)['busy'] is True
            with pytest.raises(DubForgeError) as error:
                sync.reset_for_retry(key)
            assert error.value.code == 'PUBLISH_CHAPTER_BUSY'
        finally:
            release.set()
        assert future.result(timeout=5)['status'] == 'completed'
    assert len(env.translated) == 1 and len(calls(env, 'chapter_write')) == 2


def test_shutdown_stops_between_parts_and_explicit_retry_preserves_first_part(env, monkeypatch):
    key = publication(env)
    original = studio.commit
    def stop_after_first(*args):
        result = original(*args)
        sync.shutdown()
        return result
    monkeypatch.setattr(studio, 'commit', stop_after_first)
    result = sync.run(key)
    assert result['error_code'] == 'PUBLISH_CHAPTER_STOPPED'
    assert [part['status'] for part in result['parts']] == ['completed', 'pending']
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1]
    sync.startup()
    sync.reset_for_retry(key)
    monkeypatch.setattr(studio, 'commit', original)
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]


def test_adaptation_receives_cancel_check_and_shutdown_does_not_prepare_remote(env, monkeypatch):
    key = publication(env)
    def cancelled(items, *, cancel_check, **kwargs):
        sync.shutdown()
        cancel_check()
        pytest.fail('取消检查必须停止后续翻译')
    monkeypatch.setattr(chapters, 'adapt', cancelled)
    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_STOPPED'
    assert len(calls(env, 'chapter_rules')) == 1
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write') and not result.get('plans')


def test_adaptation_failure_is_explicit_and_retry_does_not_reupload(env, monkeypatch):
    key = publication(env)
    original = chapters.adapt
    monkeypatch.setattr(chapters, 'adapt', lambda *a, **k: (_ for _ in ()).throw(
        service.PublishError('章节整理失败', code='PUBLISH_CHAPTER_ADAPT_FAILED')))
    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_ADAPT_FAILED'
    assert len(calls(env, 'chapter_rules')) == 1
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')
    sync.reset_for_retry(key)
    monkeypatch.setattr(chapters, 'adapt', original)
    assert sync.run(key)['status'] == 'completed'
    assert all(action.startswith('studio_') for action, _ in env.calls)


@pytest.mark.parametrize('code', ['LLM_QUOTA_EXHAUSTED', 'LLM_SUBSCRIPTION_INVALID', 'LLM_PROVIDERS_FAILED'])
def test_model_service_failure_is_visible_per_part_and_retry_clears_old_error(env, monkeypatch, code):
    key = publication(env)
    original = chapters.adapt
    failure = StepFailedError('模型服务订阅未开通或已失效，请核对模型设置', code=code)
    def unavailable(*args, **kwargs):
        raise failure
    monkeypatch.setattr(chapters, 'adapt', unavailable)

    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == code
    assert result['error'] == failure.message
    assert result['parts'][0]['error_code'] == code
    assert result['parts'][0]['error'] == failure.message
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')

    queued = sync.reset_for_retry(key)
    assert queued['status'] == 'pending' and queued['error_code'] == ''
    assert queued['parts'][0]['status'] == 'pending' and queued['parts'][0]['error_code'] == ''
    monkeypatch.setattr(chapters, 'adapt', original)
    completed = sync.run(key)
    assert completed['status'] == 'completed'
    assert all(part['error_code'] == '' for part in completed['parts'])
    assert all(action.startswith('studio_') for action, _ in env.calls)


def _workflow_for(key):
    from dubforge.workflow_models import WorkflowRun
    with Session(db.get_engine()) as session:
        publication = session.get(Publication, key)
        row = WorkflowRun(project_uid=publication.project_uid, request_id='workflow-' + key,
                          publication_id=key, status='monitoring')
        session.add(row); session.commit(); session.refresh(row)
        return row.id


def test_stopping_post_processing_cancels_translation_and_resume_continues(env, monkeypatch):
    from dubforge import workflows
    key = publication(env)
    workflow = _workflow_for(key)
    original = chapters.adapt
    def stop_during_translation(items, *, cancel_check, **kwargs):
        workflows.stop(workflow)
        cancel_check()
        pytest.fail('停止后不得发送章节')
    monkeypatch.setattr(chapters, 'adapt', stop_during_translation)
    result = sync.run(key)
    assert result['status'] == 'pending' and result['error_code'] == 'PUBLISH_CHAPTER_PAUSED'
    assert sync.view(key)['execution']['state'] == 'paused'
    assert len(calls(env, 'chapter_rules')) == 1
    assert not calls(env, 'chapter_prepare') and not calls(env, 'chapter_write')
    assert workflows.post_processing_paused(key)
    workflows.resume(workflow, {'confirmed': True})
    assert not workflows.post_processing_paused(key)
    monkeypatch.setattr(chapters, 'adapt', original)
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]


def test_stopping_after_prepare_prevents_chapter_post_and_preserves_prepared_evidence(env, monkeypatch):
    from dubforge import workflows
    key = publication(env)
    workflow = _workflow_for(key)
    prepare = studio.prepare
    def stop_before_post(*args):
        result = prepare(*args)
        workflows.stop(workflow)
        return result
    monkeypatch.setattr(studio, 'prepare', stop_before_post)
    result = sync.run(key)
    assert result['status'] == 'pending' and result['parts'][0]['change_id']
    assert not calls(env, 'chapter_write')
    workflows.resume(workflow, {'confirmed': True})
    monkeypatch.setattr(studio, 'prepare', prepare)
    assert sync.run(key)['status'] == 'completed'
    assert [row['cid'] for row in calls(env, 'chapter_write')] == [1, 2]


def test_no_source_chapters_are_skipped_with_a_terminal_execution(env):
    key = publication(env)
    snapshot = json.loads(saved(key).snapshot)
    snapshot['chapter_options']['items'] = []
    edit(key, snapshot=service._json(snapshot))
    assert sync.view(key)['status'] == 'skipped'
    assert sync.run(key)['status'] == 'skipped'
    assert service.view(key)['chapter_sync']['execution']['state'] == 'skipped'
    assert not env.calls and not env.translated


def test_archived_record_rejected_before_llm_or_remote(env):
    key = publication(env)
    edit(key, archived_at=now())
    with pytest.raises(DubForgeError) as error:
        sync.run(key)
    assert error.value.code == 'PUBLISH_RECORD_INACTIVE'
    assert not env.translated and not env.calls


def test_target_change_after_partial_failure_cannot_reuse_old_progress(env):
    key = publication(env)
    env.hooks['studio_chapter_prepare'] = lambda _: (_ for _ in ()).throw(
        transport.TransportError('平台不可用', code='PUBLISH_CHAPTER_RULE_UNAVAILABLE'))
    assert sync.run(key)['status'] == 'pending'
    snapshot = json.loads(saved(key).snapshot)
    snapshot['parts'][0]['sha256'] = 'different-file'
    edit(key, snapshot=service._json(snapshot))
    sync.reset_for_retry(key)
    result = sync.run(key)
    assert result['status'] == 'failed' and result['error_code'] == 'PUBLISH_CHAPTER_CONFLICT'
    assert not calls(env, 'chapter_write')


@pytest.mark.parametrize('value', [None, 1, 'true', [], {}])
def test_request_flags_require_booleans(env, value):
    key = publication(env)
    with pytest.raises(DubForgeError):
        sync.run(key, reconcile_only=value)
    with pytest.raises(DubForgeError):
        sync.reset_for_retry(key, enable=value)
    assert not env.calls
