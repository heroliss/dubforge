"""过程预览不得替代正式下游输入，也不能串到另一次任务。"""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from dubforge.domain import crud
from dubforge.pipeline import translation_preview


def test_api_only_uses_partial_for_explicit_preview_of_current_job(make_project):
    from app.main import app
    uid = make_project()
    project = crud.get_project(uid)
    pdir = crud.project_dir(uid)
    final = pdir / 'artifacts' / 'translate' / 'translate.txt'
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_text('1. 上次完整一\n2. 上次完整二\n', encoding='utf-8')
    crud.add_artifact(project.id, 'translate', 'txt', 'translate.txt', final, str(final.relative_to(pdir)))
    job = crud.add_job(project.id, 'translate', '')
    ctx = SimpleNamespace(pdir=pdir, job_id=job.id, check_cancel=lambda: None)
    translation_preview.save(ctx, '1. 本次第一句\n', 1, 2)
    client = TestClient(app)
    url = f'/api/projects/{uid}/steps/translate/artifacts/translate.txt/raw'
    assert client.get(url).content == final.read_bytes()
    response = client.get(url + '?preview=true')
    assert response.headers['X-DubForge-Preview'] == 'partial'
    assert response.headers['X-DubForge-Completed'] == '1'
    assert response.text == '1. 本次第一句\n'
    rows = client.get(f'/api/projects/{uid}/steps').json()
    assert next(row for row in rows if row['key'] == 'translate')['partial_preview_job_id'] == job.id
    crud.add_job(project.id, 'translate', '')
    assert client.get(url + '?preview=true').content == final.read_bytes()


def test_failed_initial_translation_has_preview_but_no_formal_artifact(make_project):
    from app.main import app
    uid = make_project()
    project = crud.get_project(uid)
    job = crud.add_job(project.id, 'translate', '')
    job.status = 'failed'
    crud.update_job(job)
    translation_preview.save(SimpleNamespace(pdir=crud.project_dir(uid), job_id=job.id, check_cancel=lambda: None), '1. 本次第一句\n', 1, 2)
    client = TestClient(app)
    row = next(row for row in client.get(f'/api/projects/{uid}/steps').json() if row['key'] == 'translate')
    assert row['partial_preview_job_id'] == job.id
    assert row['artifacts'] == []


def test_initial_translation_waits_for_first_window_without_claiming_artifact(make_project):
    from app.main import app
    uid = make_project()
    project = crud.get_project(uid)
    crud.add_job(project.id, 'translate', '')
    client = TestClient(app)
    url = f'/api/projects/{uid}/steps/translate/artifacts/translate.txt/raw'
    response = client.get(url + '?preview=true')
    assert response.status_code == 200 and response.text == ''
    assert response.headers['X-DubForge-Preview'] == 'waiting'
    assert client.get(url).json()['code'] == 'ARTIFACT_NOT_FOUND'


def test_domain_has_no_web_framework_or_api_imports():
    import ast
    from pathlib import Path
    import dubforge
    violations = []
    for path in Path(dubforge.__file__).parent.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8-sig'))):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ''] if isinstance(node, ast.ImportFrom) and not node.level else [])
            if any(name.split('.')[0] in {'app', 'fastapi', 'starlette'} for name in names):
                violations.append(f'{path.name}:{node.lineno}')
    assert violations == [], f'领域层越过界面边界：{violations}'


def test_preview_cleanup_failure_does_not_fail_complete_translation(tmp_path, monkeypatch):
    from pathlib import Path
    logs = []
    ctx = SimpleNamespace(pdir=tmp_path, job_id=17, log=logs.append)
    monkeypatch.setattr(Path, 'unlink', lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError('模拟瞬时占用')))
    translation_preview.clear(ctx)
    assert logs == ['翻译过程预览暂未清理，完整产物不受影响。']
