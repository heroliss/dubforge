"""业务归属在实际模型调用线程绑定；测试使用本地模型桩，不发送网络请求。"""
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from dubforge import comment_studio, config, llm_conversations, video_info
from dubforge.backends import llm
from dubforge.pipeline import step_split, step_translate
from dubforge.publishing import copywriting
from dubforge.runner import StepContext
from dubforge.domain import crud
from dubforge.utils.srt import SubLine


@pytest.fixture
def ownership(monkeypatch):
    current = contextvars.ContextVar('test_llm_business', default=None)

    @contextmanager
    def capture(**identity):
        token = current.set(identity)
        try:
            yield
        finally:
            current.reset(token)

    monkeypatch.setattr(llm_conversations, 'context', capture)
    return current


@pytest.mark.parametrize('step_context', [False, True])
def test_summary_binds_independent_or_step_project_inside_actual_thread(ownership, monkeypatch, step_context):
    ctx = SimpleNamespace(check_cancel=lambda: None, log=lambda _: None, job_id=42)
    if step_context:
        ctx.project = SimpleNamespace(uid='summary-project')
    else:
        ctx.project_uid = 'summary-project'

    def chat(prompt, *, validator, **kwargs):
        assert ownership.get() == dict(project_uid='summary-project', operation='summary',
                                       label='视频中文概要', owner_id='42')
        return validator(dict(title_zh='中文标题', summary_zh='可确认的概要'))

    monkeypatch.setattr(video_info, 'chat_json', chat)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(video_info._generate_zh_summary, {'title_original': 'Source title'}, ctx).result()
    assert result[:3] == ('中文标题', '可确认的概要', 'generated')
    assert ownership.get() is None
    assert video_info._SummaryContext('independent-project').project_uid == 'independent-project'


def test_publish_copy_leaf_binds_project_for_all_entrypoints(ownership, monkeypatch):
    monkeypatch.setattr(copywriting, 'evidence', lambda *a: ({'title': 'Source title'}, '', ['元数据'], []))
    project = SimpleNamespace(uid='copy-project')
    progress = SimpleNamespace(data={'run_id': 'publication-or-workflow'}, phase=lambda *a, **k: None,
                               llm_trace=lambda *a, **k: None)

    def chat(prompt, *, validator, **kwargs):
        assert ownership.get() == dict(project_uid='copy-project', operation='publish_copy', label='发布文案',
                                       owner_id='publication-or-workflow')
        return validator(dict(title='准确标题', description='根据来源写出的简介', tags=['技术', '工具', '演示'], dynamic=''))

    monkeypatch.setattr(llm, 'chat_json', chat)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(copywriting.generate, project, {}, '', progress=progress).result()
    assert result['values']['title'] == '准确标题'
    assert ownership.get() is None


@pytest.mark.parametrize('refresh_publication', ['', 'existing-publication'])
def test_workflow_copy_conversation_belongs_to_final_publication_instead_of_workflow_id(
        ownership, monkeypatch, make_project, refresh_publication):
    from dubforge import workflows
    from dubforge.publishing import service

    uid = make_project()
    expected_owner = refresh_publication or 'workflow-run-fixture'
    run = dict(id='run-fixture', project_uid=uid, recipe={'ai_copy': True},
               plan=dict(settings=dict(service.DEFAULTS), origins={key: '默认' for key in copywriting.COPY_FIELDS}))
    if refresh_publication:
        run['plan']['refresh_publication'] = refresh_publication
    progress = SimpleNamespace(data={'run_id': run['id']}, phase=lambda *a, **k: None, llm_trace=lambda *a, **k: None)
    monkeypatch.setattr(workflows, '_check_dubbing', lambda _: None)
    monkeypatch.setattr(service, 'candidates', lambda _: {'suggestion': {}})
    monkeypatch.setattr(copywriting, 'evidence', lambda *a: ({'title': 'Source title'}, '', ['元数据'], []))
    calls = []

    def chat(prompt, *, validator, **kwargs):
        active = ownership.get()
        assert active['project_uid'] == uid and active['operation'] == 'publish_copy'
        assert active['owner_id'] == expected_owner and active['owner_id'] != run['id']
        calls.append(active)
        return validator(dict(title='准确标题', description='有依据的简介', tags=['技术', '工具', '演示'], dynamic=''))

    def stop_before_artifacts(*args):
        raise RuntimeError('测试停在文案之后，不创建草稿或执行投稿')

    monkeypatch.setattr(llm, 'chat_json', chat)
    monkeypatch.setattr(workflows, '_check_configuration', stop_before_artifacts)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match='测试停在文案之后'):
            pool.submit(workflows._prepare_publication, run, threading.Event(), progress).result()
    assert len(calls) == 1 and ownership.get() is None
    assert not service.list_publications(uid)


@pytest.mark.parametrize('action,operation', [('filter', 'comment_score'), ('translate', 'comment_translate')])
def test_comment_batch_binds_project_and_real_comment_job(ownership, monkeypatch, action, operation):
    rows = [dict(id='comment-1', text='A useful observation', source_hash='source')]
    job = dict(id='comment-job', processed=0, total=1)
    diag = SimpleNamespace(project_uid='comment-project', job=job, log=lambda _: None, trace=lambda *a, **k: None)
    result = {'translations': [dict(id='comment-1', text_zh='有用的观察')]} if action == 'translate' else {
        'assessments': [dict(id='comment-1', eligible=True, score=80, reason='相关反馈')]}

    def chat(prompt, *, validator, **kwargs):
        active = ownership.get()
        assert active['project_uid'] == 'comment-project'
        assert active['owner_id'] == 'comment-job'
        assert active['operation'] == operation
        return validator(result)

    def save(values):
        job['processed'] += len(values)

    monkeypatch.setattr(comment_studio, 'chat_validated', chat)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(comment_studio._complete_batch, rows, action, {}, '要求',
                    check=lambda: None, diag=diag, save=save, profiles=[]).result()
    assert job['processed'] == 1
    assert ownership.get() is None


def step_ctx(make_project, pdir, key):
    uid = make_project()
    root = pdir(uid)
    (root / 'logs').mkdir(exist_ok=True)
    return StepContext(crud.get_project(uid), key, root, root / 'logs' / (key + '.log'),
                       threading.Event(), job_id=81)


def test_semantic_call_binds_project_and_job_in_worker_thread(ownership, monkeypatch, make_project, pdir):
    ctx = step_ctx(make_project, pdir, 'split')
    def chat(*args, **kwargs):
        assert ownership.get() == dict(project_uid=ctx.project.uid, operation='split',
                                       label='语义整理', owner_id='81')
        return '1. 0.00-2.00: This is an example.'
    monkeypatch.setattr(llm, 'chat', chat)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(step_split._llm_resegment, ctx, [SubLine(1, 0, 2, 'This is an example.')],
                             50, 15, 0, []).result()
    assert result[0]['text'] == 'This is an example.'
    assert ownership.get() is None


@pytest.mark.parametrize('review', [False, True])
def test_translation_and_recovery_calls_each_bind_actual_worker(ownership, monkeypatch, make_project, pdir, review):
    ctx = step_ctx(make_project, pdir, 'translate')
    chunk = [dict(id=1, start=0, end=2, text='This is an example.')]
    def chat(*args, **kwargs):
        active = ownership.get()
        assert active['project_uid'] == ctx.project.uid and active['owner_id'] == '81'
        assert active['operation'] == 'translate'
        assert active['label'] == ('译文逐句校对' if review else '字幕翻译')
        return '1. 这是一个例子。'
    monkeypatch.setattr(llm, 'chat', chat)
    with ThreadPoolExecutor(max_workers=1) as pool:
        if review:
            result = pool.submit(step_translate._review_adaptive_window, ctx, chunk, '系统',
                                 {'lines': {1: '这是一个例子。'}}, '', True).result()
            assert result['lines'] == {1: '这是一个例子。'}
        else:
            result, _ = pool.submit(step_translate._translate_window, ctx, chunk, '系统', '', 1, 1).result()
            assert result == {1: '这是一个例子。'}
    assert ownership.get() is None


def test_parallel_provider_probes_bind_inside_pool_threads(ownership, monkeypatch):
    from app.api import settings
    visited = []
    def probe(profile):
        active = ownership.get()
        assert active['operation'] == 'provider_test' and active['label'] == '模型连通性测试'
        assert active['owner_id'] == str(profile['index_fixture'])
        visited.append(threading.get_ident())
        return {'ok': True}
    monkeypatch.setattr(llm, 'test_provider', probe)
    monkeypatch.setattr('dubforge.llm_request_preview.validate_profiles', lambda _: None)
    result = settings.test_llm({'profiles': [dict(name='one', index_fixture=0), dict(name='two', index_fixture=1)]})
    assert len(result['results']) == 2 and result['ok']
    assert all(thread != threading.get_ident() for thread in visited)
    assert ownership.get() is None


def test_legacy_step_trace_uses_same_recursive_redaction_without_truncation(make_project, pdir):
    secret = 'fixture-private-credential-value'
    config.set_key('llm.profiles', [dict(name='fixture', api_key=secret, model='fixture')])
    ctx = step_ctx(make_project, pdir, 'split')
    body = '正文' * 10000 + '\n' + secret + ' https://example.test/?token=private-url-token'
    ctx.llm_trace('request', body, headers={'Authorization': 'Bearer private-header'},
                  nested={'api_key': secret, 'text': secret}, token='private-token')
    record = json.loads(ctx._llm_trace_file.read_text(encoding='utf-8').splitlines()[-1])
    encoded = json.dumps(record, ensure_ascii=False)
    assert '正文' * 10000 in record['content']
    assert secret not in encoded and 'private-url-token' not in encoded
    assert 'private-header' not in encoded and 'private-token' not in encoded


def test_legacy_step_text_log_redacts_frozen_credentials_without_losing_useful_text(make_project, pdir):
    secret = 'fixture-private-text-log-credential'
    config.set_key('llm.profiles', [dict(name='fixture', api_key=secret, model='fixture')])
    ctx = step_ctx(make_project, pdir, 'split')
    message = ('保留运行详情 ' + '中文' * 5000 + ' ' + secret
               + ' Bearer fixture-private-bearer https://user:pass@example.test/?token=private-query')

    ctx.log(message)

    text = ctx._log_file.read_text(encoding='utf-8')
    assert '保留运行详情' in text and '中文' * 5000 in text
    for value in (secret, 'fixture-private-bearer', 'user:pass', 'private-query'):
        assert value not in text
    assert '[已隐藏]' in text
