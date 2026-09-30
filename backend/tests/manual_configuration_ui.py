"""独立的界面验收实例，不读取用户配置、账号或媒体。"""
import os
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix='dubforge-config-ui-'))
os.environ['DUBFORGE_DATA_ROOT'] = str(root / 'data')
os.environ['DUBFORGE_CONFIG'] = str(root / 'config.yaml')

from dubforge.db import init_db
from dubforge import configuration as cfg
from dubforge.domain import crud

init_db()
projects = [crud.create_project(title=title, source_kind='youtube', source_url='https://www.youtube.com/watch?v=fixture1234')
            for title in ['越南旅行 · 河内街头', '越南旅行 · 城市漫步']]
template = cfg.create_template('越南旅行')
cfg.save(template['key'], {'revision':template['revision'],'updates':{
    'assemble.subtitle_color':'#FFD65A', 'assemble.subtitle_outline_color':'#30213A',
    'comments.mode':'auto','comments.language_mode':'bilingual','run.mode':'produce'}})
plan = cfg.binding_plan([p.uid for p in projects], template['key'][7:])
cfg.bind(dict(uids=[p.uid for p in projects], template_id=template['key'][7:], fingerprint=plan['fingerprint'], confirmed=True))
print('隔离测试目录:', root, '项目:', [p.uid for p in projects], flush=True)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('app.main:app', host='127.0.0.1', port=8611)
