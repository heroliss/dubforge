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
