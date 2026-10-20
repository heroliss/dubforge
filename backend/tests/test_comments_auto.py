"""comments.mode=auto 时手动合成自动准备评论上屏方案；saved 模式维持显式确认。
真实合成步骤与 FFmpeg，只有评论采集与 LLM 使用固定响应。"""
import subprocess

import pytest

from dubforge import comment_studio, config, video_info
from dubforge.domain import crud
from dubforge.errors import DubForgeError
from dubforge.pipeline import assembly_versions
from test_comment_studio import request_payload, validated_result


def _setup(isolated_env, monkeypatch, mode):
    config.update_config({'separate.enabled': False, 'split.use_llm': False,
        'tts.backend': 'gptsovits', 'assemble.bgm_volume': 0, 'assemble.loudnorm': False,
        'llm.profiles': [{'name': 'fixture', 'enabled': True, 'base_url': 'http://fixture.invalid/v1',
                          'api_key': 'fixture', 'model': 'fixture'}]})
    project = crud.create_project(title='自动评论测试', source_kind='youtube',
                                  source_url='https://www.youtube.com/watch?v=fixture1234')
    uid = project.uid
    assembly_versions.save(uid, dict(revision=0, versions=[
        dict(id='original', name='原声双语', dubbing=False, burn_subtitles=True,
             include_comments=True, overrides={})]))
    # comments.* 是配置作用域字段 (不在 config.yaml), 走 configuration.save。
    from test_configuration import save as conf_save
    conf_save('project:' + uid, {'comments.mode': mode, 'comments.count': 2, 'comments.limit': 50,
                                 'comments.start': 0, 'comments.interval': 1.5, 'comments.duration': 1})
    from conftest import mark_upstream_done
    mark_upstream_done(uid, 'assemble')
    # 真实短视频: 下载产物直接落盘登记, 不跑 yt-dlp。
    root = crud.project_dir(uid)
    video = root / 'media' / 'video.mp4'
    video.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=320x240:rate=10:duration=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(video)], check=True, capture_output=True)
    crud.add_artifact(project.id, 'download', 'media', 'video.mp4', video, 'media/video.mp4')

    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id='c1', text='What a lovely couple!', author='Viewer', like_count=20),
        dict(id='c2', text='Wonderful food trip!', author='Friend', like_count=10)]))
    monkeypatch.setattr(video_info, '_generate_zh_summary', lambda *a: ('旅行测试', '短片简介', 'generated', ''))
    calls = []

    def comment_ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            calls.append('score')
            return validated_result({'assessments': [dict(id=r['id'], eligible=True, score=90-i, reason='积极观众反馈')
                                                      for i, r in enumerate(rows)]}, kwargs)
        calls.append('comment_translate')
        return validated_result({'translations': [dict(id=r['id'], text_zh='真是美好的旅行！') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', comment_ai)
    return uid, calls


def test_auto_mode_prepares_comments_inside_assemble(isolated_env, monkeypatch, stub_heavy_steps):
    """auto 模式且无已确认方案: 合成步骤内自动采集并保存上屏方案, 随后正常渲染。"""
    uid, calls = _setup(isolated_env, monkeypatch, 'auto')
    from dubforge.runner import runner
    from conftest import wait_status
    r = runner.start_step(uid, 'assemble')
    assert r['status'] == 'started'
    wait_status(uid, 'assemble', {'done'})
    overlay = comment_studio.view(uid)['overlay']
    assert len(overlay['items']) == 2
    assert all(item['display_text'] == '真是美好的旅行！' for item in overlay['items'])
    assert 'score' in calls and 'comment_translate' in calls
    # 二次运行: 已有确认方案, 不再重新采集, 且指纹一致被跳过。
    calls.clear()
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: pytest.fail('已有方案不应重新采集'))
    r = runner.start_step(uid, 'assemble')
    assert r['status'] == 'skipped'


def test_saved_mode_still_requires_explicit_confirmation(isolated_env, monkeypatch):
    """saved 模式 (默认) 没有已确认方案: 维持拦截, 不自动采集。"""
    uid, _ = _setup(isolated_env, monkeypatch, 'saved')
    from dubforge.runner import runner
    with pytest.raises(DubForgeError) as exc:
        runner.start_step(uid, 'assemble')
    assert exc.value.code == 'COMMENTS_NOT_CONFIRMED'
    assert comment_studio.view(uid)['overlay'] in (None, {}) or not comment_studio.view(uid)['overlay'].get('items')


def test_auto_mode_zero_usable_comments_fails_loudly(isolated_env, monkeypatch):
    """auto 模式零条可用: 明确报错挂起, 不保存空方案也不渲染无声评论版。"""
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    def empty_ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            return validated_result({'assessments': [dict(id=r['id'], eligible=False, score=0, reason='不相关')
                                                      for r in rows]}, kwargs)
        return validated_result({'translations': [dict(id=r['id'], text_zh='x') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', empty_ai)
    from dubforge.runner import runner
    from conftest import wait_status
    runner.start_step(uid, 'assemble')
    wait_status(uid, 'assemble', {'failed'})
    assert not (comment_studio.view(uid)['overlay'] or {}).get('items')


def test_auto_mode_run_plan_does_not_block_on_unconfirmed_comments(isolated_env, monkeypatch):
    """核对运行步骤 (run_plan) 在 auto 模式且无已确认方案时不再拦截:
    规划放行并在说明里告知合成时将自动采集; saved 模式维持拦截。"""
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    from fastapi.testclient import TestClient
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        r = client.post(f'/api/projects/{uid}/run_plan',
                        json={'steps': ['assemble'], 'include_dependencies': False})
        assert r.status_code == 200, r.json()
        assert not r.json()['blockers']
        assert any('自动采集' in e for e in r.json()['effects'])

    uid2, _ = _setup(isolated_env, monkeypatch, 'saved')
    with TestClient(__import__('app.main', fromlist=['app']).app) as client:
        r = client.post(f'/api/projects/{uid2}/run_plan',
                        json={'steps': ['assemble'], 'include_dependencies': False})
        assert r.status_code == 400 and '评论精选' in (r.json()['message'] or '')


def test_auto_replenishes_after_original_and_hidden_translation_are_blocked(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id='c1', text='The Tiananmen Square massacre', author='One'),
        dict(id='c2', text='A lovely trip', author='Two'),
        dict(id='c3', text='God bless your family', author='Three'),
        dict(id='c4', text='Wonderful trip to Tiananmen Square', author='Four')]))
    def trusting_ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            return validated_result({'assessments': [dict(id=r['id'], eligible=True, score=100-i,
                reason='模型全部允许') for i, r in enumerate(rows)]}, kwargs)
        return validated_result({'translations': [dict(id=r['id'], text_zh=
            '天安门广场的悲剧' if r['text'] == 'A lovely trip' else '祝家庭幸福，旅程美好') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', trusting_ai)
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    # 原文版不生成不需要的中文；但已有隐藏中文仍必须检查，不能借换语言绕过。
    video_info.update(uid, 'comments', comment_limit=spec['comment_limit'])
    c2 = next(row for row in comment_studio.view(uid)['rows'] if row['id'] == 'c2')
    comment_studio._write(crud.get_project(uid), {'translations': {
        'c2': dict(source_hash=c2['source_hash'], text_zh='天安门广场的悲剧')}})
    spec['comment_settings']['language_mode'] = 'original'
    warnings, count = comments_auto.prepare_overlay(uid, spec)
    state = comment_studio.view(uid)
    assert count == 2 and [r['id'] for r in state['overlay']['items']] == ['c3', 'c4']
    assert state['rows'][0]['review_blocked'] and state['rows'][1]['review_blocked']
    assert not state['rows'][2]['review_blocked'] and not state['rows'][3]['review_blocked']
    assert any('内容检查' in warning and '递补' in warning for warning in warnings)


def test_auto_all_locally_blocked_is_explicit_even_when_llm_accepts(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id='c1', text='The Tiananmen Square massacre', author='One')]))
    def trusting_ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            return validated_result({'assessments': [dict(id=r['id'], eligible=True, score=100,
                reason='模型未识别风险') for r in rows]}, kwargs)
        return validated_result({'translations': [dict(id=r['id'], text_zh='安全文字') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', trusting_ai)
    from dubforge import comments_auto
    with pytest.raises(DubForgeError) as exc:
        comments_auto.prepare_overlay(uid, comments_auto.spec_from_config(uid))
    assert exc.value.code == 'COMMENTS_AUTO_EMPTY' and '内容检查' in exc.value.message
    assert not (comment_studio.view(uid)['overlay'] or {}).get('items')


def test_auto_translates_only_ranked_candidates_and_replenishes_after_review(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id=f'c{i}', text=f'Feedback {i}', author='Viewer') for i in range(1, 7)]))
    translated = []
    def ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            return validated_result({'assessments': [dict(id=r['id'], eligible=r['text'] != 'Feedback 1',
                score=100-int(r['text'].split()[-1]), reason='观众反馈') for r in rows]}, kwargs)
        translated.extend(r['text'] for r in rows)
        return validated_result({'translations': [dict(id=r['id'], text_zh=
            '天安门广场的悲剧' if r['text'] == 'Feedback 2' else '美好的旅途') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', ai)
    from dubforge import comments_auto
    phases = []
    warnings, count = comments_auto.prepare_overlay(uid, comments_auto.spec_from_config(uid), phase=phases.append)
    state = comment_studio.view(uid)
    assert count == 2 and [r['id'] for r in state['overlay']['items']] == ['c3', 'c4']
    assert translated == ['Feedback 2', 'Feedback 3', 'Feedback 4']
    assert not next(r for r in state['rows'] if r['id'] == 'c6')['text_zh']
    assert any('内容检查' in warning and '递补' in warning for warning in warnings)
    assert any(label.startswith('评论翻译：已选 1/2 条，本批 1 条') for label in phases)
    assert phases[-1] == '评论确认：保存 2 条上屏方案'


def test_auto_replenishes_rejected_candidate_and_reuses_collection_on_retry(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    fetched, translated = [], []
    def fetch(*a, **kw):
        fetched.append(True)
        return dict(title='Travel test', comments=[
            dict(id=f'c{i}', text=f'Feedback {i}', author='Viewer') for i in range(1, 5)])
    monkeypatch.setattr(video_info, '_fetch_source', fetch)
    def ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            return validated_result({'assessments': [dict(id=r['id'], eligible=True,
                score=100-int(r['text'].split()[-1]), reason='观众反馈') for r in rows]}, kwargs)
        translated.extend(r['text'] for r in rows)
        if any(r['text'] == 'Feedback 1' for r in rows):
            raise DubForgeError('服务商拒绝本条', code='LLM_CONTENT_REJECTED')
        return validated_result({'translations': [dict(id=r['id'], text_zh='美好的旅途') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', ai)
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    assert comments_auto.prepare_overlay(uid, spec)[1] == 2
    assert [r['id'] for r in comment_studio.view(uid)['overlay']['items']] == ['c2', 'c3']
    assert set(translated) == {'Feedback 1', 'Feedback 2', 'Feedback 3'}
    translated.clear()
    assert comments_auto.prepare_overlay(uid, spec)[1] == 2
    assert len(fetched) == 1 and not translated


def test_auto_service_fault_stops_and_retains_scoring_for_resume(isolated_env, monkeypatch):
    uid, calls = _setup(isolated_env, monkeypatch, 'auto')
    base = comment_studio.chat_validated
    def ai(prompt, **kwargs):
        if '【评分要求】' not in prompt:
            raise DubForgeError('服务额度不足', code='LLM_RATE_LIMITED')
        return base(prompt, **kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', ai)
    from dubforge import comments_auto
    with pytest.raises(DubForgeError) as exc:
        comments_auto.prepare_overlay(uid, comments_auto.spec_from_config(uid))
    assert exc.value.code == 'LLM_RATE_LIMITED'
    assert comment_studio.view(uid)['rating_progress']['saved'] == 2
    assert not (comment_studio.view(uid)['overlay'] or {}).get('items')
    assert calls == ['score']


def test_auto_stops_at_three_independent_rejections_across_candidate_batches(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id=f'c{i}', text=f'Feedback {i}', author='Viewer') for i in range(1, 7)]))
    independently_tested = []
    def ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            return validated_result({'assessments': [dict(id=r['id'], eligible=True,
                score=100-int(r['text'].split()[-1]), reason='观众反馈') for r in rows]}, kwargs)
        if len(rows) == 1:
            independently_tested.append(rows[0]['text'])
        raise DubForgeError('服务商拒绝请求', code='LLM_CONTENT_REJECTED')
    monkeypatch.setattr(comment_studio, 'chat_validated', ai)
    from dubforge import comments_auto
    with pytest.raises(DubForgeError) as exc:
        comments_auto.prepare_overlay(uid, comments_auto.spec_from_config(uid))
    assert exc.value.code == 'COMMENTS_AUTO_EMPTY'
    assert independently_tested == ['Feedback 1', 'Feedback 2', 'Feedback 3']
    rows = {r['id']: r for r in comment_studio.view(uid)['rows']}
    assert rows['c4']['ai_issues']['translate']['status'] == 'deferred'
    assert not rows['c5'].get('ai_issues', {}).get('translate')


def test_original_only_actual_version_does_not_translate_or_change_default_language(isolated_env, monkeypatch, stub_heavy_steps):
    uid, calls = _setup(isolated_env, monkeypatch, 'auto')
    view = assembly_versions.view(uid)
    versions = [{key: row[key] for key in ('id', 'name', 'dubbing', 'burn_subtitles', 'include_comments', 'overrides')}
                for row in view['versions']]
    versions[0]['overrides']['comments.language_mode'] = 'original'
    assembly_versions.save(uid, dict(revision=view['revision'], versions=versions))
    from dubforge import configuration
    before = configuration.resolve(uid)
    assert before['values']['comments.language_mode'] == 'translated'
    from dubforge.runner import runner
    from conftest import wait_status
    runner.start_step(uid, 'assemble')
    wait_status(uid, 'assemble', {'done'})
    assert calls == ['score']
    state = comment_studio.view(uid)
    assert len(state['overlay']['items']) == 2
    assert all(item['display_text'] == '' for item in state['overlay']['items'])
    assert configuration.resolve(uid)['fingerprint'] == before['fingerprint']


def _save_manual_plan(uid, spec, *, excluded_ids=(), source_text='人工精简的原文', origin='manual'):
    """人工确认与来源不明旧方案都应保护，但不能混淆来源计数。"""
    video_info.ensure_comments(uid, comment_limit=spec['comment_limit'])
    state = comment_studio.view(uid)
    row = next(item for item in state['rows'] if item['id'] == 'c2')
    return comment_studio.save(uid, {
        'revision': state['overlay_revision'], 'settings': spec['comment_settings'],
        'items': [{'id': row['id'], 'source_hash': row['source_hash'],
                   'source_text': source_text, 'display_text': '人工保留的上屏文字',
                   **({'selection_origin': origin} if origin is not None else {})}],
        'excluded_ids': list(excluded_ids),
    })


def test_auto_treats_missing_selection_origin_as_automatic(isolated_env, monkeypatch):
    uid, calls = _setup(isolated_env, monkeypatch, 'auto')
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    _save_manual_plan(uid, spec, origin=None)
    calls.clear()
    spec['comment_count'] = 1
    assert comments_auto.prepare_overlay(uid, spec)[1] == 1
    items = comment_studio.view(uid)['overlay']['items']
    assert [item['id'] for item in items] == ['c1']
    assert items[0]['selection_origin'] == 'automatic'
    assert 'comment_translate' in calls


def test_auto_respects_manual_exclusion_and_labels_new_automatic_items(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id=f'c{i}', text=f'Feedback {i}', author='Viewer') for i in range(1, 5)]))
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    video_info.ensure_comments(uid, comment_limit=spec['comment_limit'])
    c1 = next(item for item in comment_studio.view(uid)['rows'] if item['id'] == 'c1')
    exclusion = {'id': 'c1', 'source_hash': c1['source_hash']}
    _save_manual_plan(uid, spec, excluded_ids=[exclusion])
    assert comments_auto.prepare_overlay(uid, spec)[1] == 2
    overlay = comment_studio.view(uid)['overlay']
    assert [item['id'] for item in overlay['items']] == ['c2', 'c3']
    assert [item['selection_origin'] for item in overlay['items']] == ['manual', 'automatic']
    assert overlay['excluded_ids'] == [exclusion]


def test_auto_lower_target_removes_only_automatic_items(isolated_env, monkeypatch):
    uid, calls = _setup(isolated_env, monkeypatch, 'auto')
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    assert comments_auto.prepare_overlay(uid, spec)[1] == 2
    assert all(item['selection_origin'] == 'automatic' for item in comment_studio.view(uid)['overlay']['items'])
    calls.clear()
    spec['comment_count'] = 1
    assert comments_auto.prepare_overlay(uid, spec)[1] == 1
    assert [item['id'] for item in comment_studio.view(uid)['overlay']['items']] == ['c1']
    assert calls == []


def test_auto_long_manual_keeps_full_text_and_completes(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    _save_manual_plan(uid, spec, source_text='original ' * 100)
    spec['comment_settings']['language_mode'] = 'original'
    warnings, count = comments_auto.prepare_overlay(uid, spec)
    assert count == 2
    kept = next(row for row in comment_studio.view(uid)['overlay']['items'] if row['id'] == 'c2')
    assert kept['source_text'] == ('original ' * 100).strip()
    assert kept['selection_origin'] == 'manual'
    assert any('超过建议字数' in warning for warning in warnings)


def test_auto_never_discards_manual_items_to_fit_video_capacity(isolated_env, monkeypatch):
    uid, _ = _setup(isolated_env, monkeypatch, 'auto')
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id=f'c{i}', text=f'Feedback {i}', author='Viewer') for i in range(1, 4)]))
    from dubforge import comments_auto
    spec = comments_auto.spec_from_config(uid)
    video_info.ensure_comments(uid, comment_limit=spec['comment_limit'])
    state = comment_studio.view(uid)
    before = comment_studio.save(uid, {
        'revision': state['overlay_revision'], 'settings': spec['comment_settings'],
        'items': [{'id': row['id'], 'source_hash': row['source_hash'],
                   'source_text': row['text'], 'display_text': '人工保留', 'selection_origin': 'manual'} for row in state['rows']],
    })['overlay']
    with pytest.raises(DubForgeError) as exc:
        comments_auto.prepare_overlay(uid, spec)
    assert exc.value.code == 'COMMENTS_MANUAL_INVALID'
    assert exc.value.detail['capacity'] == 2
    assert comment_studio.view(uid)['overlay'] == before
