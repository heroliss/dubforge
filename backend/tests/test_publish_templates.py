"""发布模板已并入系列；验证统一模板的发布语义与归档边界。"""
import pytest
from dubforge import configuration as cfg
from dubforge.publishing import service
from dubforge.errors import DubForgeError
from test_configuration import save, bind
from test_publishing import setup_publish


def test_series_publication_values_follow_same_parent(setup_publish):
    uid,pub,_,_=setup_publish
    template=cfg.create_template('旅行系列')
    save(template['key'],{'publish.close_comments':True,'publish.season_id':31})
    bind([uid],template)
    data=service.view(pub['id'])
    assert data['base_settings']['close_comments'] is True
    assert data['base_settings']['season_id']==31
    assert data['base_origins']['season_id']=='旅行系列'
    assert data['settings']['season_id']==1  # 当前稿件已有明确覆盖，不能被模板替换。


def test_archive_retains_values_and_restores_selection(make_project):
    a,b=make_project(),make_project();template=cfg.create_template('旅行')
    save(template['key'],{'publish.season_id':31});bind([a],template)
    current=cfg.resolve(key=template['key'])
    cfg.archive(template['key'],dict(revision=current['revision'],archived=True))
    assert cfg.resolve(a)['values']['publish.season_id']==31
    with pytest.raises(DubForgeError):bind([b],template)
    current=cfg.resolve(key=template['key'])
    cfg.archive(template['key'],dict(revision=current['revision'],archived=False))
    bind([b],template)
    assert cfg.resolve(b)['values']['publish.season_id']==31


def test_old_publishing_template_routes_are_removed(setup_publish):
    from fastapi.testclient import TestClient
    from app.main import app
    client=TestClient(app)
    assert client.post('/api/publishing/templates',json={}).status_code in {404,405}
    assert client.post('/api/publishing/templates/x/delete',json={}).status_code in {404,405}
