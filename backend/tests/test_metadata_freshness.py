"""资料编辑不能污染制作缓存；覆盖旧指纹与页面实际读取的依赖状态。"""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge import config, video_info
from dubforge.domain import crud
from dubforge.pipeline.dependencies import Dependencies
from dubforge.pipeline.dubbing_edit import record_take
from dubforge.pipeline.registry import PIPELINE_KEYS, STEP_KEYS, STEP_MAP
from dubforge.pipeline.step_tts import line_fingerprint
from dubforge.pipeline.text_prompts import (
    ASR_CORRECTION_VERSION, TRANSLATION_PROMPT_VERSION, SEMANTIC_SEGMENTATION_VERSION,
    TRANSLATION_WINDOW_VERSION,
)
from dubforge.runner import compute_fingerprint, runner
from dubforge.utils.fs import atomic_write_bytes


def historical_fingerprint(project, key):
    """独立复原旧输入布局；同质量规则才兼容，不把旧整理策略模拟为有效。"""
    deps = Dependencies(project)
    cfg = {name: deps.cfg(name) for name in STEP_MAP[key].cfg_keys}
    if key == 'transcribe':
        cfg['asr'].pop('segmentation_mode', None)
        # 历史产物早于平台字幕导入；不能把后来新增的默认项写进旧指纹夹具。
        cfg['asr'].pop('input_mode', None)
    payload = {'step': key, 'cfg': cfg, 'upstream': sorted(
        f'{name}:{art.sha256}' for name, art in deps.arts.items()
        if art.step_key in STEP_KEYS[:STEP_KEYS.index(key)])}
    if key in {'split', 'translate'}:
        payload['correction_prompt'] = ASR_CORRECTION_VERSION
    if key == 'split':
        # 本夹具测试输入布局兼容；缺少新质量规则的真实旧产物应保持过期，
        # 已由 test_semantic_segmentation_version 独立覆盖，不能放宽生产门禁。
        payload['semantic_segmentation'] = SEMANTIC_SEGMENTATION_VERSION
    if key == 'translate':
        payload['translation_prompt'] = TRANSLATION_PROMPT_VERSION
        # 与整理一样，仅模拟旧存储布局，实际翻译策略必须相同。
        payload['translation_windows'] = TRANSLATION_WINDOW_VERSION
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@pytest.fixture(params=['legacy', 'current'])
def completed_project(make_project, request):
    uid = make_project(params={'step_params': {'assemble.burn_subtitles': True}})
    project = crud.get_project(uid)
    project.source_url = 'https://www.youtube.com/watch?v=abc123'
    crud.save_project(project)
    root = crud.project_dir(uid)
    srt = '1\n00:00:00,000 --> 00:00:01,000\nhello\n'
    files = {
        'download': {'video.mp4': 'video', 'meta.json': '{"duration": 60}'},
        'preprocess': {'raw.wav': 'audio', 'vocal.wav': 'voice', 'background.wav': 'background'},
        'transcribe': {'src.srt': srt, 'words.json': '[]'},
        'split': {'split.txt': '1. 0.000-1.000: hello\n'},
        'translate': {'translate.txt': '1. 你好\n'},
        'tts': {'1.wav': 'dub'},
        'subtitles': {'trans.srt': srt},
        'assemble': {'final_dub_hardsub.mp4': 'finished video'},
    }
    for key, items in files.items():
        for name, content in items.items():
            path = root / STEP_MAP[key].dir / name
            atomic_write_bytes(path, content.encode())
            crud.add_artifact(project.id, key, 'test', name, path, str(path.relative_to(root)))
        step = crud.get_step(project.id, key)
        step.status = 'done'
        crud.save_step(step)
    line = Dependencies(project).dubbing_lines(required=True)[0]
    record_take(project, line, root, 1, line_fingerprint(project, line), {})
    for key in PIPELINE_KEYS:
        step = crud.get_step(project.id, key)
        if request.param == 'legacy' and key in {'preprocess', 'transcribe', 'split', 'translate'}:
            step.input_fingerprint = historical_fingerprint(project, key)
            assert step.input_fingerprint != compute_fingerprint(project, key)
        else:
            step.input_fingerprint = compute_fingerprint(project, key)
        crud.save_step(step)
    assert_pipeline_ready(TestClient(app), project)
    return project


def assert_pipeline_ready(api, project):
    response = api.get(f'/api/projects/{project.uid}/steps')
    assert response.status_code == 200, response.text
    assert {row['key'] for row in response.json()} == set(PIPELINE_KEYS)
    for row in response.json():
        assert row['status'] == 'done', row['key']
        assert row['fresh'], row['key']
        assert not row['unready_inputs'], row['key']


def test_global_comment_defaults_do_not_stale_completed_pipeline(completed_project):
    api = TestClient(app)
    response = api.post('/api/settings', json={'updates': {'comments.overlay.color': '#FFD65A'}})
    assert response.status_code == 200
    assert_pipeline_ready(api, completed_project)


@pytest.fixture
def platform(monkeypatch):
    source = {'id': 'abc123', 'title': '原标题', 'description': '简介', 'extractor': 'youtube',
              'comment_count': 40, 'comments': [{'id': 'one', 'text': 'Great trip!', 'author': '观众'}]}
    monkeypatch.setattr(video_info, '_fetch_source', lambda *a, **kw: source)
    monkeypatch.setattr(video_info, 'chat_json', lambda *a, **kw: {'title_zh': '中文标题', 'summary_zh': '概要'})
    return source


def test_metadata_refresh_and_comment_plan_preserve_pipeline(completed_project, platform):
    project = completed_project
    api = TestClient(app)
    base = f'/api/projects/{project.uid}'
    before_steps = [s.model_dump() for s in crud.get_steps(project.id)]
    deps = Dependencies(project)
    before_files = {name: deps.path(a.step_key, a.name).read_bytes() for name, a in deps.arts.items()}

    # 首次补资料、再次更新资料、概要与评论采集都必须检查动态 fresh，而不只是数据库状态。
    for action in ('refresh', 'refresh', 'summarize', 'comments'):
        platform['description'] += '更新'
        response = api.post(f'{base}/steps/download/video-info/{action}', json={'comment_limit': 20})
        assert response.status_code == 200, response.text
        assert_pipeline_ready(api, project)

    # 上屏方案单独保存与修改；已有普通成片、字幕和逐句音频都保持有效。
    state = api.get(f'{base}/comments').json()
    for color in ('#FFD65A', '#8DE6FF'):
        response = api.put(f'{base}/comments/overlay', json={
            'revision': state['overlay_revision'],
            'items': [dict(row, display_text='很棒的旅行') for row in state['rows']],
            'settings': {'color': color},
        })
        assert response.status_code == 200, response.text
        state = response.json()
        assert_pipeline_ready(api, project)

    assert [s.model_dump() for s in crud.get_steps(project.id)] == before_steps
    assert all(deps.path(*name.split('/', 1)).read_bytes() == data for name, data in before_files.items())
    # 再点运行应直接复用，不调外部服务或创建后台任务。
    for key in ('preprocess', 'transcribe', 'split', 'translate'):
        assert runner.start_step(project.uid, key)['status'] == 'skipped'
        assert crud.latest_job(project.id, key) is None


@pytest.mark.parametrize('change,first_stale', [
    ('video', 'preprocess'), ('audio', 'transcribe'),
    ('separate_config', 'preprocess'), ('asr_config', 'transcribe'),
])
def test_real_inputs_still_invalidate_after_metadata_refresh(completed_project, platform, change, first_stale):
    project = completed_project
    # Capture the storage-layout variant before mutating any real input.  A
    # post-mutation comparison would confuse a valid current project with a
    # legacy fingerprint because the changed input is expected to differ.
    legacy = crud.get_step(project.id, 'preprocess').input_fingerprint != compute_fingerprint(project, 'preprocess')
    video_info.update(project.uid, 'refresh')
    if change in {'video', 'audio'}:
        step, name = ('download', 'video.mp4') if change == 'video' else ('preprocess', 'raw.wav')
        deps = Dependencies(project)
        path = deps.path(step, name)
        # 同样大小的新内容也必须识别：保留实际产物哈希校验。
        atomic_write_bytes(path, b'x' * path.stat().st_size)
        crud.add_artifact(project.id, step, 'test', name, path, str(path.relative_to(deps.root)))
    else:
        from dubforge import configuration
        current = configuration.resolve(project.uid)
        configuration.save('project:' + project.uid, dict(revision=current['revision'], updates={
            'separate.enabled' if change == 'separate_config' else 'asr.model':
            False if change == 'separate_config' else 'large-v3'}))
    deps = Dependencies(project)
    assert deps.fresh('download')
    # 产物依赖按实际消费边界核对：输入或设置变化先使直接消费者过期；
    # 仅步骤状态 stale、尚未替换文件时，不把所有下游缓存一并误判为过期。
    expected_stale = {
        ('video', True): {'preprocess', 'transcribe', 'split', 'translate', 'subtitles', 'assemble'},
        ('audio', True): {'transcribe', 'split', 'translate'},
        ('separate_config', True): {'preprocess', 'assemble'},
        # 旧项目仍可能保存旧版下游指纹，但 ASR 设置只由转写消费；
        # split/translate 消费已登记产物，仍可复用。
        ('asr_config', True): {'transcribe'},
        # 当前项目同样按直接消费者限定原地替换的影响范围；
        # transcribe/split/translate 的登记产物在自身输入替换前仍有效。
        # 字幕消费当前配音排布；排布的片尾边界绑定源视频，因此视频替换
        # 还需要重导字幕，但不因此重做已登记的翻译或配音声音。
        ('video', False): {'preprocess', 'subtitles', 'assemble'},
        ('audio', False): {'transcribe'},
        ('separate_config', False): {'preprocess', 'assemble'},
        ('asr_config', False): {'transcribe'},
    }[(change, legacy)]
    assert {key for key in PIPELINE_KEYS if not deps.fresh(key)} == expected_stale


def test_unknown_saved_fingerprint_is_not_accepted(completed_project, platform):
    project = completed_project
    video_info.update(project.uid, 'refresh')
    step = crud.get_step(project.id, 'preprocess')
    step.input_fingerprint = 'unrecognized-inputs'
    crud.save_step(step)
    assert not Dependencies(project).fresh('preprocess')
