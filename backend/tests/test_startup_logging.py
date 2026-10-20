"""初始化日志需要指出具体阶段，并保留真正失败的异常。"""
import logging

import pytest
from fastapi.testclient import TestClient


def test_ready_backend_logs_stage_durations(isolated_env, caplog):
    from app.main import app

    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        with TestClient(app) as client:
            assert client.get('/api/health').json()['ok'] is True
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith('启动初始化：数据库与配置迁移') for message in messages)
    assert any(message.startswith('启动初始化完成：兼容旧产物指纹（') for message in messages)
    assert any(message.startswith('后端初始化完成，总耗时 ') for message in messages)


def test_failed_startup_logs_phase_and_preserves_domain_error(isolated_env, monkeypatch, caplog):
    from app import main
    from dubforge.errors import ConfigError

    failure = ConfigError('测试配置无法读取')

    def fail():
        raise failure

    monkeypatch.setattr(main, 'init_db', fail)
    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        with pytest.raises(ConfigError) as caught:
            with TestClient(main.app):
                pytest.fail('初始化失败时不能接受请求')
    assert caught.value is failure
    records = [record for record in caplog.records if record.getMessage().startswith('启动初始化失败：')]
    assert len(records) == 1
    assert '数据库与配置迁移' in records[0].getMessage()
    assert records[0].exc_info[1] is failure
    assert not any('后端初始化完成' in record.getMessage() for record in caplog.records)
