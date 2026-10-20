"""自动名称只使用现有资料，条件更新保护用户名称和制作状态。"""
import json

import pytest
from sqlalchemy import update

from dubforge import project_titles as titles
from dubforge.domain import crud
from dubforge.domain.models import Project

URL = 'https://www.youtube.com/watch?v=BaW_jenozKc'


def info(**changes):
    return {'title_original': 'Riding the train', 'title_zh': '乘坐高铁',
            'summary_status': 'generated', 'source': {'url': URL}, **changes}


@pytest.fixture
def auto_project(isolated_env):
    return crud.create_project(title=titles.placeholder(URL, 'link'),
                               source_url=URL, source_kind='link', title_mode='auto')


@pytest.mark.parametrize(('url', 'kind', 'expected'), [
    (URL, 'link', 'YouTube 视频 · BaW_jenozKc'),
    ('https://youtu.be/BaW_jenozKc?t=10', 'youtube', 'YouTube 视频 · BaW_jenozKc'),
    ('https://www.youtube.com/shorts/BaW_jenozKc', 'link', 'YouTube 视频 · BaW_jenozKc'),
    ('https://www.youtube.com/embed/BaW_jenozKc', 'link', 'YouTube 视频 · BaW_jenozKc'),
    ('https://www.youtube.com/live/BaW_jenozKc', 'link', 'YouTube 视频 · BaW_jenozKc'),
    ('https://www.youtube.com/', 'link', 'YouTube 视频'),
    ('https://user:password@example.com/videos/a%20trip?token=private', 'link', 'example.com · a trip'),
    ('https://example.com/', 'link', 'example.com'),
    ('https://[broken', 'link', '链接视频'),
    ('', 'link', '链接视频'),
    ('"D:\\Videos\\北京旅行.mp4"', 'local', '北京旅行'),
    ('/videos/a trip.mkv', 'local', 'a trip'),
    ('media/导入视频.mp4', 'local', '导入视频'),
    ('', 'local', '本地视频'),
])
def test_placeholder_uses_readable_source_identity_without_credentials(url, kind, expected):
    assert titles.placeholder(url, kind) == expected


@pytest.mark.parametrize(('data', 'expected'), [
    (info(), '乘坐高铁'),
    (info(summary_stale=True), 'Riding the train'),
    (info(summary_status='llm_unavailable'), 'Riding the train'),
    (info(title_zh=''), 'Riding the train'),
    (info(title_zh=42), 'Riding the train'),
    (info(title_zh='  乘坐\n高铁\x00 '), '乘坐 高铁'),
    ({'title_original': '实际标题'}, '实际标题'),
    ({'title_zh': '未经生成的标题'}, '占位名'),
    (None, '占位名'),
    ([], '占位名'),
])
def test_best_title_prefers_valid_generated_chinese_then_original(data, expected):
    assert titles.best_title(data, '占位名') == expected


def test_sync_updates_only_name_and_manifest_preserving_files_and_step_state(auto_project):
    project = auto_project
    root = crud.project_dir(project.uid)
    video = root / 'media/video.mp4'
    video.write_bytes(b'isolated media')
    before_steps = [(s.id, s.status, s.progress, s.input_fingerprint) for s in crud.get_steps(project.id)]
    before_fields = (project.dir_name, project.params, project.source_url, project.status)
    assert titles.sync(project, info()) is True
    current = crud.get_project(project.uid)
    assert current.title == '乘坐高铁' and current.title_mode == 'auto'
    assert (current.dir_name, current.params, current.source_url, current.status) == before_fields
    assert [(s.id, s.status, s.progress, s.input_fingerprint) for s in crud.get_steps(project.id)] == before_steps
    assert crud.project_dir(project.uid) == root and video.read_bytes() == b'isolated media'
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['title'] == '乘坐高铁' and manifest['title_mode'] == 'auto'


def test_sync_original_title_when_chinese_summary_is_stale(auto_project):
    assert titles.sync(auto_project, info(summary_stale=True)) is True
    assert crud.get_project(auto_project.uid).title == 'Riding the train'


def test_manual_name_is_never_updated(isolated_env):
    project = crud.create_project(title='我的管理名称', source_url=URL, source_kind='link')
    assert project.title_mode == 'manual'
    assert titles.sync(project, info()) is False
    assert crud.get_project(project.uid).title == '我的管理名称'


def test_late_auto_snapshot_cannot_overwrite_concurrent_manual_rename(auto_project):
    frozen = auto_project
    crud.update_project_title(frozen.uid, '用户刚改的新名称', title_mode='manual')
    assert frozen.title_mode == 'auto'  # 真实迟到结果仍持有旧运行快照。
    assert titles.sync(frozen, info()) is False
    current = crud.get_project(frozen.uid)
    assert current.title == '用户刚改的新名称' and current.title_mode == 'manual'


def test_late_auto_snapshot_cannot_update_changed_source(auto_project):
    frozen = auto_project
    with crud.session() as session:
        session.exec(update(Project).where(Project.uid == frozen.uid).values(source_url='https://youtu.be/new-video'))
        session.commit()
    assert titles.sync(frozen, info()) is False
    current = crud.get_project(frozen.uid)
    assert current.title == frozen.title and current.source_url == 'https://youtu.be/new-video'


def test_same_title_is_idempotent_even_with_an_old_project_snapshot(auto_project, monkeypatch):
    assert titles.sync(auto_project, info()) is True
    before = crud.get_project(auto_project.uid).updated_at
    def forbidden_manifest(*_args):
        pytest.fail('同值同步不应重复写清单')
    monkeypatch.setattr(crud, 'write_manifest', forbidden_manifest)
    assert titles.sync(auto_project, info()) is False
    assert crud.get_project(auto_project.uid).updated_at == before


def test_empty_late_info_cannot_downgrade_an_already_resolved_auto_name(auto_project):
    assert titles.sync(auto_project, info()) is True
    assert titles.sync(auto_project, {}) is False
    assert crud.get_project(auto_project.uid).title == '乘坐高铁'


def test_manifest_failure_does_not_undo_successful_auxiliary_name(auto_project, monkeypatch, caplog):
    def failed_manifest(*_args):
        raise OSError('隔离测试模拟清单失败')
    monkeypatch.setattr(crud, 'write_manifest', failed_manifest)
    assert titles.sync(auto_project, info()) is True
    assert crud.get_project(auto_project.uid).title == '乘坐高铁'
    assert '项目自动名称已更新，但备份清单更新失败' in caplog.text


def write_info(project, value):
    path = crud.project_dir(project.uid) / 'artifacts/download/video_info.json'
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return path


def test_cached_title_reads_chinese_without_database_or_manifest_writes(auto_project, monkeypatch):
    path = write_info(auto_project, info())
    before = (path.read_bytes(), crud.get_project(auto_project.uid).model_dump())
    manifest = crud.project_dir(auto_project.uid) / 'manifest.json'
    before_manifest = manifest.read_bytes()
    monkeypatch.setattr(crud, 'write_manifest', lambda *_: pytest.fail('读取名称不应写清单'))
    assert titles.cached_title(auto_project) == '乘坐高铁'
    assert (path.read_bytes(), crud.get_project(auto_project.uid).model_dump()) == before
    assert manifest.read_bytes() == before_manifest


@pytest.mark.parametrize('raw', ['{broken', '[]', 'null', '\ufeff{"title_original":"平台标题"}'])
def test_bad_or_legacy_title_cache_uses_media_metadata(auto_project, raw):
    root = crud.project_dir(auto_project.uid)
    (root / 'artifacts/download/video_info.json').write_text(raw, encoding='utf-8')
    (root / 'media/meta.json').write_text('{"title":"媒体标题"}', encoding='utf-8')
    expected = '平台标题' if raw.startswith('\ufeff') else '媒体标题'
    assert titles.cached_title(auto_project) == expected


def test_oversized_cache_is_bounded_and_uses_small_media_metadata(auto_project, monkeypatch):
    root = crud.project_dir(auto_project.uid)
    write_info(auto_project, {'title_original': 'x' * 500})
    (root / 'media/meta.json').write_text('{"title":"媒体标题"}', encoding='utf-8')
    monkeypatch.setattr(titles, '_MAX_CACHE_BYTES', 100)
    assert titles.cached_title(auto_project) == '媒体标题'


def test_restore_auto_without_cache_uses_source_not_manual_name(isolated_env):
    project = crud.create_project(title='手动管理名称', source_url=URL, source_kind='link')
    assert titles.cached_title(project) == 'YouTube 视频 · BaW_jenozKc'
    assert crud.get_project(project.uid).title == '手动管理名称'


def test_local_cache_missing_uses_original_filename(isolated_env):
    project = crud.create_project(title='手动管理名称', source_url='D:/Videos/北京旅行.mp4', source_kind='local')
    assert titles.cached_title(project) == '北京旅行'


def test_explicit_different_cached_source_cannot_leak_old_video_title(auto_project):
    write_info(auto_project, info(source={'url': 'https://youtu.be/different-video'}))
    root = crud.project_dir(auto_project.uid)
    (root / 'media/meta.json').write_text('{"title":"旧来源标题"}', encoding='utf-8')
    assert titles.cached_title(auto_project) == 'YouTube 视频 · BaW_jenozKc'
    assert titles.sync(auto_project, info(source={'url': 'https://youtu.be/different-video'})) is False


def test_equivalent_youtube_permalink_keeps_title_cache_valid(auto_project):
    data = info(source={'url': 'https://youtu.be/BaW_jenozKc?t=99'})
    write_info(auto_project, data)
    assert titles.cached_title(auto_project) == '乘坐高铁'
    assert titles.sync(auto_project, data) is True
