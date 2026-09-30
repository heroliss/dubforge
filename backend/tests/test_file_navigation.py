"""文件定位与用途清单：模拟资源管理器，绝不操作用户窗口。"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from dubforge.domain import crud
from dubforge.project_files import description
from dubforge.utils import reveal


def test_reveal_file_folder_and_missing_parent(make_project, monkeypatch):
    uid = make_project()
    root = crud.project_dir(uid)
    path = root / '文件带空格 & 中文.txt'
    path.write_text('test', encoding='utf-8')
    calls = []
    monkeypatch.setattr(reveal.subprocess, 'Popen', lambda command: calls.append(command))
    client = TestClient(app)
    for target, missing, expected in [(path, False, ['explorer.exe', '/select,', str(path)]),
        (root, False, ['explorer.exe', str(root)]),
        (root / '已移走' / '成品.mp4', True, ['explorer.exe', str(root)])]:
        response = client.post('/api/files/reveal', json={'path': str(target)})
        assert response.status_code == 200
        assert response.json()['missing'] is missing
        assert response.headers['cache-control'] == 'no-store'
        assert calls[-1] == expected
    assert not (root / '已移走').exists()


@pytest.mark.parametrize('path', ['', None, 123, '../outside', 'https://example.com/a',
                                  '\\\\server\\share\\video.mp4', 'C:\\name".mp4', 'C:\\bad\nname.mp4'])
def test_reveal_rejects_nonlocal_or_invalid_paths(isolated_env, monkeypatch, path):
    monkeypatch.setattr(reveal.subprocess, 'Popen', lambda *a: pytest.fail('不能打开无效路径'))
    response = TestClient(app).post('/api/files/reveal', json={'path': path})
    assert response.status_code == 400 and response.json()['code'] == 'PATH_INVALID'


def test_reveal_rejects_cross_site_and_reports_explorer_failure(make_project, monkeypatch):
    root = crud.project_dir(make_project())
    client = TestClient(app)
    for headers in [{'Origin': 'https://untrusted.example'}, {'Sec-Fetch-Site': 'cross-site'}]:
        response = client.post('/api/files/reveal', json={'path': str(root)}, headers=headers)
        assert response.status_code == 400 and response.json()['code'] == 'FILE_ORIGIN'
    def unavailable(*args):
        raise OSError('模拟资源管理器不可用')
    monkeypatch.setattr(reveal.subprocess, 'Popen', unavailable)
    response = client.post('/api/files/reveal', json={'path': str(root)})
    assert response.json()['code'] == 'PATH_REVEAL_FAILED'


def test_project_files_supply_full_paths_and_descriptions(make_project):
    uid = make_project()
    root = crud.project_dir(uid)
    path = root / 'artifacts' / 'tts' / '12_v2.wav'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'voice')
    result = TestClient(app).get(f'/api/projects/{uid}/files').json()
    item = next(row for row in result['entries'] if row['file'].endswith('12_v2.wav'))
    assert item['path'] == str(path)
    assert item['description'] == '逐句配音音频 · 备选版本'
    assert Path(result['dir']).is_absolute()


@pytest.mark.parametrize('path,text', [('srt/trans.srt', '目标语言字幕'),
    ('output/final_dub_hardsub_comments_20260927_120000_123456.mp4', '叠加评论'),
    ('logs/translate.llm.jsonl', 'AI 请求'), ('manifest.json', '配置快照'),
    ('comment_studio.json', '已确认'), ('artifacts/tts/1.take.json', '版本'),
    ('artifacts/download/video_info.json', '视频内容'),
    ('audio/dub_full.wav', '完整配音'), ('other.bin', '辅助文件')])
def test_file_descriptions(path, text):
    assert text in description(path)
