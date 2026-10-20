"""项目列表只读取批次摘要，不查询平台或逐项目重新预检。"""
import json

from fastapi.testclient import TestClient
from sqlmodel import Session


def test_project_list_batches_summary_and_detail_are_read_only(make_project, monkeypatch):
    from app.main import app
    from dubforge import release_batches
    from dubforge.db import get_engine
    from dubforge.publishing import transport
    from dubforge.release_batch_models import ReleaseBatch

    first, second = make_project(title='先发布'), make_project(title='后发布')
    with Session(get_engine()) as session:
        row = ReleaseBatch(request_id='00000000-0000-0000-0000-000000000001', status='running',
            scope=json.dumps(dict(account_id='1', season_id=2, section_id=3)),
            items=json.dumps([dict(project_uid=first, title='先发布', status='running', phase='制作中'),
                              dict(project_uid=second, title='后发布', status='queued', phase='等待前项完成')]))
        session.add(row)
        session.commit()
        session.refresh(row)
        batch_id = row.id
    monkeypatch.setattr(transport, 'call', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('摘要禁止平台请求')))
    with TestClient(app) as client:
        # 生命周期将重启前的活动记录暂停；模拟已确认运行态，保持测试与生产启动一致。
        release_batches._update(batch_id, status='running')
        single = client.get(f'/api/projects/{second}').json()
        assert single['release_batch']['id'] == batch_id
        assert single['release_batch']['position'] == 2
        navigation = client.get(f'/api/projects/{second}/navigation').json()
        assert navigation['release_batch'] == single['release_batch']
        def no_single(*_args, **_kwargs):
            raise AssertionError('列表不能对每个项目重新查询批次')
        monkeypatch.setattr(release_batches, 'project_status', no_single)
        rows = client.get('/api/projects').json()
        assert len(rows) == 2
        summaries = {p['uid']: p['release_batch'] for p in rows}
        assert summaries[first]['position'] == 1
        assert summaries[second]['total'] == 2
