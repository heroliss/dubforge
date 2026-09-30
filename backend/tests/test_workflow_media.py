"""一键双分P集成：真实制作步骤与FFmpeg，只有外部网络/模型使用固定响应。"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import pytest
from types import SimpleNamespace

from sqlmodel import Session

from dubforge import comment_studio, config, environment, video_info, workflows
from dubforge.backends import llm, tts_gptsovits
from dubforge.db import get_engine
from dubforge.domain import crud
from dubforge.pipeline import step_download
from dubforge.pipeline.assembly_outputs import final_variant
from dubforge.publishing import credentials, service, transport
from dubforge.publishing.models import PublishAccount
from test_workflows import configure
from test_comment_studio import request_payload, validated_result


@pytest.mark.parametrize('independent', [False, True])
def test_download_to_two_comment_versions_and_one_publication(isolated_env, monkeypatch, independent):
    config.update_config({'separate.enabled': False, 'split.use_llm': False,
        'tts.backend': 'gptsovits', 'assemble.bgm_volume': 0, 'assemble.loudnorm': False,
        'llm.profiles': [{'name': 'fixture', 'enabled': True, 'base_url': 'http://fixture.invalid/v1',
                          'api_key': 'fixture', 'model': 'fixture'}]})
    source = isolated_env / 'source.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=320x240:rate=10:duration=3', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True, capture_output=True)
    project = crud.create_project(title='自动双版本测试', source_kind='youtube', source_url='https://www.youtube.com/watch?v=fixture1234')
    uid, calls = project.uid, []

    def download(ctx, url, template):
        from pathlib import Path
        calls.append('download')
        out = Path(template.replace('%(ext)s', 'mp4'))
        tmp = out.with_suffix('.mp4.tmp')
        shutil.copyfile(source, tmp); os.replace(tmp, out)
        return dict(title='Travel test', filepath=str(out), source_meta={'title': 'Travel test', 'description': 'A short trip', 'uploader': 'Fixture'})
    monkeypatch.setattr(step_download, '_run_ytdlp', download)
    monkeypatch.setattr(video_info, '_generate_zh_summary', lambda *a: ('旅行测试', '短片简介', 'generated', ''))
    monkeypatch.setattr(environment, 'require_component', lambda *a: None)
    class Whisper:
        def __init__(self, *args, **kwargs): pass
        def transcribe(self, *args, **kwargs):
            calls.append('transcribe')
            words = [SimpleNamespace(start=.2, end=.7, word='Hello '), SimpleNamespace(start=.7, end=1.4, word='world.')]
            return iter([SimpleNamespace(start=.2, end=1.4, words=words)]), SimpleNamespace(duration=3, language='en')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=SimpleNamespace(
        is_available=lambda: False, is_initialized=lambda: False)))
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace(WhisperModel=Whisper))
    def translate(prompt, parser, **kwargs):
        calls.append('translate')
        return parser('1. 你好世界。')
    monkeypatch.setattr(llm, 'chat_validated', translate)
    monkeypatch.setattr(tts_gptsovits, 'is_online', lambda *a: True)
    def synth(url, text, out_path, *a, **kwargs):
        calls.append('tts')
        tmp = out_path.with_suffix('.wav.tmp')
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=1',
                        '-ar', '32000', '-ac', '1', '-f', 'wav', str(tmp)], check=True, capture_output=True)
        os.replace(tmp, out_path)
        return out_path
    monkeypatch.setattr(tts_gptsovits, 'synth', synth)
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: dict(title='Travel test', comments=[
        dict(id='c1', text='What a lovely couple!', author='Viewer', like_count=20),
        dict(id='c2', text='Wonderful food trip!', author='Friend', like_count=10)]))
    def comment_ai(prompt, **kwargs):
        rows = request_payload(prompt)['comments']
        if '【评分要求】' in prompt:
            calls.append('score')
            return validated_result({'assessments': [dict(id=r['id'], eligible=True, score=90-i, reason='积极观众反馈') for i, r in enumerate(rows)]}, kwargs)
        calls.append('comment_translate')
        return validated_result({'translations': [dict(id=r['id'], text_zh='真是美好的旅行！') for r in rows]}, kwargs)
    monkeypatch.setattr(comment_studio, 'chat_validated', comment_ai)

    with Session(get_engine()) as session:
        session.add(PublishAccount(id='123', name='测试账号', credential='fixture'))
        session.commit()
    monkeypatch.setattr(credentials, 'unprotect', lambda *a: {'fixture': True})
    monkeypatch.setattr(transport, 'available', lambda: True)
    transfers, submitted = [], []
    def network(op, payload=None, **kwargs):
        calls.append(op)
        if op == 'metadata':
            return dict(human_categories=[dict(id=1001, name='旅行')], categories=[dict(id=21)],
                declarations=[dict(value=1, label='含AI生成内容')], seasons=[dict(id=91, title='旅行合集', sections=[dict(id=92, title='默认')])])
        if op == 'upload':
            value = dict(cid=payload['artifact_id']+100, filename=f"remote-{payload['artifact_id']}", title=payload['title'])
            transfers.append(value)
            return value
        if op == 'cover': return dict(url='https://i0.hdslb.com/fixture.jpg')
        if op == 'submit': submitted.append(payload); return dict(bvid='BV1234567890', aid=22)
        if op == 'status': return dict(bvid='BV1234567890', aid=22, state=0, state_desc='审核通过', videos=transfers)
        if op == 'collection': return dict(joined=True)
        raise AssertionError(op)
    monkeypatch.setattr(transport, 'call', network)
    cover = service.store_cover(b'\x89PNG\r\n\x1a\nfixture')['cover_id']
    spec = dict(mode='publish', variants=['dub_hardsub_comments', 'src_hardsub_comments'],
        part_titles=dict(dub_hardsub_comments='中文配音', src_hardsub_comments='原声版'),
        account_id='123', ai_copy=False, cover_source='custom', cover_id=cover,
        comments_mode='auto', comment_count=2, comment_settings=dict(start=0, interval=1.5, duration=1, font_size=18, margin=8),
        overrides=dict(tags=['旅行'], human_type2=1001, tid=21, declaration_id=1, copyright=3, season_id=91, section_id=92))
    from dubforge.pipeline import assembly_versions as av
    av.save(uid,dict(revision=0,versions=[
            dict(id='dub',name='配音中文字幕',dubbing=True,burn_subtitles=True,include_comments=True,
                 overrides={'assemble.subtitle_track':'trans','comments.language_mode':'translated'}),
            dict(id='original',name='原声双语',dubbing=False,burn_subtitles=True,include_comments=True,
                 overrides={'assemble.subtitle_track':'bilingual','comments.language_mode':'bilingual'} if independent else {})]))
    spec.update(version_ids=['dub','original'],part_titles={'dub':'中文配音','original':'原声版'})
    configure(uid, spec)
    plan = workflows.preview(uid, spec)
    assert not plan['blockers'], plan['blockers']
    assert plan['steps'] == ['download', 'preprocess', 'transcribe', 'split', 'translate', 'tts', 'comments', 'assemble']
    row = workflows.start(uid, dict(recipe=plan['recipe'], fingerprint=plan['fingerprint'], confirmed=True, request_id=str(uuid.uuid4())))
    try:
        deadline = time.monotonic() + 40
        while row['id'] in workflows._running and time.monotonic() < deadline:
            time.sleep(.05)
        assert row['id'] not in workflows._running
        result = workflows.get(row['id'])
        assert result['status'] == 'monitoring', result
        assert [p['title'] for p in submitted[0]['videos']] == ['中文配音', '原声版']
        assert submitted[0]['settings']['title'] == '旅行测试'
        assert submitted[0]['settings']['season_id'] == 91
        assert calls.count('submit') == calls.count('collection') == 1
        assert len(transfers) == 2
        files = service.candidates(uid)['files']
        outputs = [f for f in files if not f['is_source']]
        assert {f['version_id'] for f in outputs} == set(spec['version_ids'])
        assert {f['version_id']:f['has_dubbing'] for f in outputs} == {'dub':True,'original':False}
        assert [p['has_dubbing'] for p in service.view(result['publication_id'])['snapshot']['parts']] == [True,False]
        assert all(v['fresh'] for v in av.view(uid)['versions'])
        for file in outputs:
            path = crud.project_dir(uid) / 'output' / file['name']
            assert 2.9 <= service._probe(path) <= 3.2
            streams = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)],
                check=True, capture_output=True).stdout)['streams']
            assert {s['codec_type'] for s in streams} == {'video', 'audio'}
        assert len(comment_studio.view(uid)['overlay']['items']) == 2
        assert calls.count('score') == calls.count('comment_translate') == 1
        workflows.monitor_once()
        assert workflows.get(row['id'])['status'] == 'completed'
    finally:
        if row['id'] in workflows._running:
            workflows.stop(row['id'])
            deadline = time.monotonic() + 10
            while row['id'] in workflows._running and time.monotonic() < deadline: time.sleep(.05)
