"""长任务的旧项目快照不能回滚编辑；重启不留下隐形续跑队列。"""
import json

import pytest

from dubforge.domain import crud
from dubforge.runner import runner


@pytest.mark.parametrize('action', ['continue', 'clear'])
def test_chain_keeps_later_project_edits(make_project, monkeypatch, action):
    uid = make_project(params={'_chain': ['translate', 'assemble'],
                               'step_params': {'assemble.bgm_volume': 1}})
    old = crud.get_project(uid)
    latest = crud.get_project(uid)
    params = crud.project_params(latest)
    params.update(auto_steps=['download'])
    from test_configuration import save
    save('project:' + uid, {'assemble.bgm_volume': .25})
    latest.title = '运行期间修改的标题'
    latest.params = json.dumps(params)
    crud.save_project(latest)
    calls = []
    monkeypatch.setattr(runner, 'start_step', lambda uid, key: calls.append(key) or {'status': 'started'})
    if action == 'continue':
        runner.continue_chain(old)
    else:
        runner._clear_chain(old, '测试停止')
    saved = crud.get_project(uid)
    assert saved.title == latest.title
    assert crud.project_params(saved)['auto_steps'] == ['download']
    assert crud.project_step_params(saved)['assemble.bgm_volume'] == .25
    assert calls == (['translate'] if action == 'continue' else [])


@pytest.mark.parametrize('status', ['running', 'queued', 'done'])
def test_recovery_holds_chain_even_between_steps(make_project, monkeypatch, status):
    """重启不销毁计划: 链保留并挂起, 中断步骤压回链头; 挂起期间不自动续跑。"""
    uid = make_project(params={'_chain': ['translate', 'assemble'], 'auto_steps': ['split']})
    project = crud.get_project(uid)
    step = crud.get_step(project.id, 'split')
    step.status = status
    crud.save_step(step)
    runner.recover()
    params = crud.project_params(crud.get_project(uid))
    assert params['auto_steps'] == ['split']
    assert params['_chain_state'] == 'held'
    if status in ('running', 'queued'):
        # 中断的 split 不在原链里 → 压回链头; 挂起原因可追溯。
        assert params['_chain'] == ['split', 'translate', 'assemble']
        assert params['_chain_hold']['code'] == 'STEP_INTERRUPTED'
        assert params['_chain_hold']['step'] == 'split'
    else:
        # 步骤之间重启: 计划原样保留, 同样挂起等待用户确认。
        assert params['_chain'] == ['translate', 'assemble']
        assert params['_chain_hold']['code'] == 'STEP_INTERRUPTED'
    # 挂起期间不自动续跑; 恢复必须由用户显式触发。
    monkeypatch.setattr(runner, 'start_step', lambda *args: pytest.fail('挂起的链不应自动续跑'))
    runner.continue_chain(project)


def test_clearing_deleted_project_does_not_restore_it(make_project):
    uid = make_project(params={'_chain': ['translate']})
    old = crud.get_project(uid)
    # 模拟无关联步骤的已删除项目对象；清队列不得 merge 旧对象复活项目。
    old.uid = 'already-removed'
    runner._clear_chain(old, '测试')
    assert crud.get_project('already-removed') is None
