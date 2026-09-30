"""本地UI验收夹具：临时数据，不读用户配置，不访问外部服务，仅8611。"""
import os
import tempfile
from pathlib import Path


if __name__ == '__main__':
    import pytest
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='dubforge-version-ui-') as folder:
        os.environ['DUBFORGE_DATA_ROOT'] = str(Path(folder) / 'data')
        os.environ['DUBFORGE_CONFIG'] = str(Path(folder) / 'config.yaml')
        from dubforge import db
        from dubforge.domain import crud
        from dubforge.pipeline import assembly_versions as versions
        from test_assemble import _prepare_outputs_project
        db.init_db()
        with pytest.MonkeyPatch.context() as patch:
            uid, _ = _prepare_outputs_project(lambda: crud.create_project(title='版本布局验收').uid, patch)
            versions.save(uid, dict(revision=0, versions=[
                dict(id='zh', name='中文原声版', dubbing=False, burn_subtitles=True, include_comments=False,
                     overrides={'assemble.subtitle_track': 'trans'}),
                dict(id='dual', name='双语原声版', dubbing=False, burn_subtitles=True, include_comments=False,
                     overrides={'assemble.subtitle_track': 'bilingual'})]))
            print(f'UI_FIXTURE_URL=http://127.0.0.1:8611/projects/{uid}/steps/assemble', flush=True)
            uvicorn.run('app.main:app', host='127.0.0.1', port=8611)
