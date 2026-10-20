"""500 候选恢复复用真实评分缓存；仅 HTTP 替身，不访问模型或生产数据。"""
import copy
import json
import re

import httpx
import pytest

from dubforge import comment_preparation as cp, comment_studio as cs, config, configuration
from dubforge.backends import llm
from dubforge.domain import crud
from dubforge.utils.comment_protocol import escape_text, unescape_text
from dubforge.utils.fs import atomic_write_bytes


def seed(uid,count):
    path=crud.project_dir(uid)/'artifacts/download/video_info.json'
    atomic_write_bytes(path,json.dumps({'title_original':'隔离评论视频','comments':{'items':[
        dict(id=str(index),text=f'Original opinion {index}',author='观众',like_count=index,
            url=f'https://example.invalid/comment/{index}') for index in range(count)]}}).encode())
    return path


def request_payload(prompt):
    rows=[]
    for line in prompt.split('【待处理评论】\n',1)[1].split('\n\n',1)[0].splitlines():
        match=re.fullmatch(r'(\d+)\. (.*)',line)
        if match:rows.append(dict(id=match[1],text=unescape_text(match[2])))
    return {'comments':rows}


def wire_response(result):
    if 'translations' in result:
        return '\n'.join(f'{row["id"]}. {escape_text(row["text_zh"])}' for row in result['translations'])
    return '\n'.join(f'{row["id"]}. {row["score"]} | {str(row["eligible"]).lower()} | {escape_text(row["reason"])}'
        for row in result['assessments'])


@pytest.fixture
def frozen_project(make_project, monkeypatch):
    uid = make_project()
    source = seed(uid, 500)
    config.set_key('llm.profiles', [dict(name='隔离模型',base_url='http://comments.fixture.invalid/v1',
        model='fixture',api_key='isolated-credential',enabled=True)])
    configuration.save('project:'+uid,dict(revision=configuration.resolve(uid)['revision'],updates={
        'comments.mode':'auto','comments.limit':500,'comments.count':30,'comments.failure_policy':'skip'}))
    monkeypatch.setattr(llm,'_rate_limit_until',{})
    monkeypatch.setattr(llm,'_sleep_retry',lambda *_:pytest.fail('明确业务错误不能重发或退避'))
    recipe = copy.deepcopy(configuration.run_spec(uid))
    state = cs.view(uid)
    body = dict(target=recipe['comment_count'],prompt=recipe['comment_prompt'],
        failure_policy=recipe['comment_failure_policy'],settings=copy.deepcopy(state['defaults']),
        ids=[row['id'] for row in state['rows']])
    assert (recipe['comments_mode'],recipe['comment_limit'],recipe['comment_count'],recipe['comment_failure_policy']) == ('auto',500,30,'skip')
    return uid,recipe,body,source.read_bytes(),crud.get_project(uid).params


def model_transport(monkeypatch, respond):
    calls = []
    def handle(request):
        body = json.loads(request.content)
        prompt = body['messages'][-1]['content']
        rows = request_payload(prompt)['comments']
        action = 'filter' if '【评分要求】' in prompt else 'translate'
        calls.append(dict(action=action,rows=copy.deepcopy(rows),body=body))
        failure = respond(action,rows)
        if failure:
            status,detail = failure
            return httpx.Response(status,json={'error':detail})
        result = ({'assessments':[dict(id=row['id'],eligible=True,score=80,reason='有效反馈') for row in rows]}
            if action=='filter' else {'translations':[dict(id=row['id'],text_zh='完整中文观众反馈') for row in rows]})
        return httpx.Response(200,json={'choices':[{'message':{'content':wire_response(result)},'finish_reason':'stop'}]})
    monkeypatch.setattr(llm,'_client',lambda base:httpx.Client(base_url=base,transport=httpx.MockTransport(handle)))
    return calls


def warm_cache(uid,recipe):
    result = cs.run(uid,'filter',ids=[str(index) for index in range(185)],
        prompt=recipe['comment_prompt'],limit=recipe['comment_count'],failure_policy=recipe['comment_failure_policy'])
    assert result['job']['processed'] == 185 and result['job']['cached'] == 0
    assert len(result['available_assessments']) == 185
    return copy.deepcopy(cs._read(crud.get_project(uid))['rating_cache']['assessments'])


def unchanged(uid,recipe,body,original_body,source,params):
    assert configuration.run_spec(uid) == recipe
    assert body == original_body and len(body['ids']) == 500 and body['target'] == 30
    assert crud.get_project(uid).params == params
    assert (crud.project_dir(uid)/'artifacts/download/video_info.json').read_bytes() == source


def test_real_http_content_risk_resumes_185_cached_scores_isolates_one_blocked_and_prepares_frozen_thirty(frozen_project,monkeypatch):
    uid,recipe,body,source,params = frozen_project
    mode = {'resume':False}
    def respond(action,rows):
        if mode['resume'] and action=='filter' and any(row['text']=='Original opinion 185' for row in rows):
            return 400,{'type':'invalid_request_error','code':'invalid_request_error','message':'Content Exists Risk'}
    calls = model_transport(monkeypatch,respond)
    cached = warm_cache(uid,recipe)
    initial_requests = len(calls)
    original_body = copy.deepcopy(body)
    mode['resume'] = True
    result = cp.prepare(uid,body)
    preparation = result['preparation']
    assert preparation['summary']['reused_ratings'] == 185
    assert preparation['summary']['new_ratings'] == 314
    assert preparation['summary']['ready'] == 30 and preparation['summary']['shortfall'] == 0
    assert len(preparation['draft']['items']) == 30
    assert result['prepare_job']['status'] == 'done'
    filter_job = cs.diagnostics(uid,'filter')['job']
    assert (filter_job['cached'],filter_job['processed'],filter_job['blocked'],filter_job['deferred']) == (185,314,1,0)
    assert filter_job['status'] == 'partial' and 0 < filter_job['isolation_requests'] <= 24
    resumed_scores = [call for call in calls[initial_requests:] if call['action']=='filter']
    cached_text = {f'Original opinion {index}' for index in range(185)}
    assert not any(row['text'] in cached_text for call in resumed_scores for row in call['rows'])
    assert len(resumed_scores) <= 32+24
    state = cs.view(uid)
    issue = next(row for row in state['rows'] if row['id']=='185')['ai_issues']['filter']
    assert issue['status'] == 'blocked' and issue['code'] == 'LLM_CONTENT_REJECTED'
    assert issue['request_scope'] == 'item' and issue['retryable'] is False and 'score' not in issue
    assert set(issue['request_sources']) == {'185'}
    assert issue['cause'][0]['provider_code'] == 'invalid_request_error'
    assert '拒绝' in issue['message'],'逐条问题保留中文拒绝说明与服务商错误码'
    current = {row['id']:row for row in cs._read(crud.get_project(uid))['rating_cache']['assessments']}
    assert all(current[row['id']] == row for row in cached)
    assert len(current) == 499 and '185' not in current
    assert state['overlay'] is None,'准备草稿不扩大为自动确认授权'
    unchanged(uid,recipe,body,original_body,source,params)
    before = len(calls)
    repeated = cp.prepare(uid,body)['preparation']
    assert repeated['summary']['ready'] == 30 and len(calls) == before,'普通继续复用成功评分/译文，已拦截原文不重发'


@pytest.mark.parametrize('status,detail,code',[
    (400,{'type':'invalid_request_error','message':'Invalid JSON schema'},'LLM_HTTP_400'),
    (401,{'code':'invalid_api_key','message':'Invalid authentication credentials'},'LLM_AUTH'),
    (429,{'code':'1113','message':'余额不足或无可用资源包,请充值。'},'LLM_QUOTA_EXHAUSTED'),
])
def test_non_content_service_errors_stop_without_isolation_and_keep_185_valid_scores(frozen_project,monkeypatch,status,detail,code):
    uid,recipe,body,source,params = frozen_project
    mode = {'fail':False}
    calls = model_transport(monkeypatch,lambda *_:(status,detail) if mode['fail'] else None)
    cached = warm_cache(uid,recipe)
    before = len(calls)
    original_body = copy.deepcopy(body)
    mode['fail'] = True
    with pytest.raises(cs.CommentError) as caught:
        cp.prepare(uid,body)
    assert caught.value.code == code
    assert len(calls) == before+1,'账户/请求错误停止，不拆单也不进入格式重试'
    state = cs.view(uid)
    assert state['rating_progress'] == dict(saved=185,total=500,remaining=315)
    assert state['job']['status'] == 'failed' and state['job']['isolation_requests'] == 0
    assert not cs._read(crud.get_project(uid)).get('ai_issues',{}).get('filter')
    assert cs._read(crud.get_project(uid))['rating_cache']['assessments'] == cached
    unchanged(uid,recipe,body,original_body,source,params)
