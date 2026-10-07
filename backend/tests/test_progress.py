"""用可控时钟验证估算偏差、停滞、缓存和 LLM 重试，不调用外部服务。"""
import json

import pytest

from dubforge.progress import LlmProgress, RateProgress, _save_sample, middle_third_mean, timing_history

pytestmark = pytest.mark.usefixtures("isolated_env")


class Ctx:
    def __init__(self):
        self.now = 0.0
        self.percent = []

    def active_seconds(self):
        return self.now

    def set_eta_provider(self, provider, *, scope="step"):
        self.eta = provider
        self.scope = scope

    def set_progress_provider(self, provider):
        self.progress = provider

    def report_progress(self, value):
        self.percent.append(value)

    def llm_trace(self, *args, **kwargs):
        pass


def test_download_uses_cumulative_average_including_stall():
    ctx = Ctx()
    progress = RateProgress(ctx, 1000, end=100)
    ctx.now = 10
    progress.update(100)
    assert ctx.eta() == 90
    ctx.now = 20
    progress.update(300)
    assert ctx.eta() == pytest.approx(700 / 15)
    assert ctx.percent[-1] == 30  # 实际完成 300/1000，无额外阶段权重。
    ctx.now = 25
    assert ctx.eta() == pytest.approx(700 / 12)
    ctx.now = 30
    assert ctx.eta() == 70  # 没有新字节，累计平均速度随时间下降。


def test_download_uses_available_startup_time():
    ctx = Ctx()
    progress = RateProgress(ctx, 1000)
    ctx.now = 4
    progress.update(40)
    assert ctx.eta() == 96
    ctx.now = 12
    progress.update(200)
    assert ctx.eta() == 48


def test_batch_eta_counts_down_between_results_without_moving_percent():
    ctx = Ctx()
    progress = RateProgress(ctx, 120, eta_on_update=True)
    ctx.now = 10
    progress.update(30)
    assert ctx.eta() == 30
    percent = ctx.percent[-1]
    ctx.now = 15
    assert ctx.eta() == 25  # 等结果时不能像旧算法那样反而升到45秒。
    progress.update(30)
    assert ctx.eta() == 25  # 重复回调不重启倒计时。
    assert ctx.percent[-1] == percent
    ctx.now = 20
    progress.update(60)
    assert ctx.eta() == 20
    ctx.now = 50
    assert ctx.eta() is None  # 新结果迟迟未到，撤下已经耗尽的估计。
    progress.update(90)
    assert ctx.eta() == pytest.approx(50 / 3)
    progress.update(120)
    assert ctx.eta() is None
    assert ctx.percent[-1] == 99


def test_cached_work_increases_completion_but_not_processing_speed():
    ctx = Ctx()
    progress = RateProgress(ctx, 100)
    progress.update(80, cached=True)
    assert ctx.eta() is None
    ctx.now = 10
    progress.update(90)
    assert ctx.eta() == 10  # 真正处理 10 单位用 10 秒，剩余 10 单位。
    progress.update(100)
    assert ctx.eta() is None
    assert ctx.percent[-1] == 99


PROFILE = [{"name": "测试", "model": "m", "base_url": "http://mock", "api_key": "private"}]


def make_llm(ctx, weights=(100, 50), profile=None):
    return LlmProgress(ctx, weights, step="split", profile=profile or PROFILE)


def test_first_llm_run_advances_without_stream_callbacks():
    ctx = Ctx()
    progress = make_llm(ctx)
    progress.begin(0)
    assert ctx.eta() == 600  # 多窗口合计十分钟，并非每窗十分钟。
    ctx.now = 90
    before = ctx.progress()
    assert 1 < before["progress"] < 61
    assert before["progress_estimated"] is True
    assert ctx.eta() == 510
    # 思考字符多少不改变估计，避免把内部输出长度当作完成度。
    progress.trace("reasoning_delta", "思考" * 1000)
    assert ctx.progress()["progress"] == before["progress"]
    progress.finish(success=False)
    assert ctx.percent[-1] == pytest.approx(1 + 98 * 2 / 3)
    assert timing_history() == {}


def test_llm_trimmed_mean_resists_outliers_and_rejects_unrelated_sizes():
    ctx = Ctx()
    key = make_llm(ctx).key
    for seconds in (1, 2, 90, 120, 900, 1000):
        _save_sample(key, "测试", 100, seconds)
    progress = make_llm(ctx)
    progress.begin(0)
    assert ctx.eta() == pytest.approx(157.5)  # 中间 90、120 平均 105，再按长度换算。
    ctx.now = 20
    assert ctx.eta() == pytest.approx(137.5)
    ctx.now = 120
    assert ctx.eta() is None  # 当前窗超时后不显示后续窗的剩余时间。
    unrelated = make_llm(ctx, weights=(1000,))
    unrelated.begin(0)
    assert ctx.eta() == 600


@pytest.mark.parametrize("values, expected", [([], None), ([30], 30), ([10, 30], 20),
                                                   ([1, 10, 99], 10), ([1, 10, 20, 99], 15)])
def test_small_sample_trimmed_mean(values, expected):
    assert middle_third_mean(values) == expected


def test_stage_transition_overrun_retry_and_completion_never_fake_success():
    ctx = Ctx()
    progress = make_llm(ctx, weights=(100,))
    progress.begin(0)
    progress.trace("request", request_id="first", profile="测试")
    ctx.now = 100
    first = ctx.progress()
    assert 1 < first["progress"] < 61
    ctx.now = 1200
    late = ctx.progress()
    assert first["progress"] < late["progress"] < 61
    assert late["estimate_overdue"] and ctx.eta() is None
    progress.trace("assistant_delta", "1. 正文")
    assert ctx.progress()["progress"] == 61
    assert ctx.progress()["progress_phase"] == "模型输出"
    assert ctx.eta() == pytest.approx(round(600 * 38 / 98, 1))
    ctx.now += 60
    output = ctx.progress()["progress"]
    assert 61 < output < 99
    progress.trace("validation_error")
    progress.trace("request", request_id="retry", profile="测试")
    assert ctx.progress()["progress"] >= output
    assert ctx.eta() is None
    progress.finish()
    assert ctx.progress()["progress"] == 99
    assert timing_history() == {}


def test_cached_window_advances_without_a_fake_ten_minute_wait():
    ctx = Ctx()
    progress = make_llm(ctx, weights=(100, 100))
    progress.begin(0)
    progress.finish()
    assert ctx.progress()["progress"] == 50
    progress.begin(1)
    assert ctx.eta() == 300
    ctx.now = 60
    assert 50 < ctx.progress()["progress"] < 99
    assert timing_history() == {}


def test_history_reused_across_contexts_and_isolates_model_settings():
    for seconds in (10, 12, 14):
        ctx = Ctx()
        progress = make_llm(ctx, weights=(100,))
        progress.begin(0)
        progress.trace("request", request_id="one", profile="测试")
        ctx.now = seconds
        progress.finish()
    ctx = Ctx()
    progress = make_llm(ctx, weights=(100,))
    progress.begin(0)
    assert ctx.eta() == 12
    changed = [{**PROFILE[0], "model": "another"}]
    assert make_llm(ctx, profile=changed).key != progress.key
    rotated = [{**PROFILE[0], "api_key": "new-secret"}]
    assert make_llm(ctx, profile=rotated).key == progress.key
    assert "private" not in json.dumps(timing_history())


@pytest.mark.parametrize("events", [
    [],  # 缓存命中
    [("request", {"request_id": "1", "profile": "测试"}), ("validation_error", {})],
    [("request", {"request_id": "1", "profile": "测试"}), ("retry", {})],
    [("request", {"request_id": "1", "profile": "测试"}), ("request", {"request_id": "2", "profile": "备用"})],
    [("request", {"request_id": "1", "profile": "备用"})],  # 主服务处于冷却，直接跳过
])
def test_cache_retry_and_fallback_do_not_pollute_history(events):
    ctx = Ctx()
    progress = make_llm(ctx, weights=(100,))
    progress.begin(0)
    for event, meta in events:
        progress.trace(event, **meta)
    ctx.now = 200
    progress.finish()
    assert timing_history() == {}


@pytest.mark.parametrize("rejection", ["stream", "params"])
@pytest.mark.parametrize("cancel_on_reply", [False, True])
def test_http_compatibility_retry_excluded_from_timing(monkeypatch, rejection, cancel_on_reply):
    """HTTP 400 兼容重发也属于异常样本；回退请求期间取消后不能返回成功。"""
    import httpx

    from dubforge.backends import llm
    from dubforge.runner import StepCancelled

    ctx = Ctx()
    provider = dict(PROFILE[0])
    if rejection == "params":
        provider["params"] = {"reasoning_effort": "low"}
    progress = make_llm(ctx, weights=(100,), profile=[provider])
    progress.begin(0)
    requests = []
    cancelled = False

    def check_cancel():
        if cancelled:
            raise StepCancelled("步骤被用户取消")

    def respond(request):
        nonlocal cancelled
        requests.append(json.loads(request.content))
        ctx.now += 30
        if len(requests) == 1:
            # 只有明确点名不支持的可选参数才允许兼容重发，普通 400 不降参。
            rejected_parameter = 'reasoning_effort' if rejection == 'params' else 'stream'
            return httpx.Response(400, text=f"unsupported {rejected_parameter}")
        cancelled = cancel_on_reply
        return httpx.Response(200, json={"choices": [{"message": {"content": "1. 一"}}]})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(llm, "_client", lambda _: httpx.Client(transport=transport, base_url="http://mock"))
    body = llm._payload(provider, [{"role": "user", "content": "one"}], None, json_mode=False)
    if cancel_on_reply:
        with pytest.raises(StepCancelled):
            llm._post(provider, body, trace=progress.trace, cancel_check=check_cancel)
    else:
        assert llm._post(provider, body, trace=progress.trace, cancel_check=check_cancel) == "1. 一"
        progress.finish()
    assert len(requests) == 2 and progress.retried
    assert requests[0]["stream"] is True
    assert "stream" not in requests[1]
    if rejection == "params":
        assert "reasoning_effort" not in requests[1]
    assert timing_history() == {}


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_interruption_before_llm_sample_is_not_recorded(interruption):
    """取消或超时恰好发生在返回正文后、写入历史前，仍不采样。"""
    from dubforge.runner import StepCancelled, StepTimeout

    ctx = Ctx()
    progress = make_llm(ctx, weights=(100,))
    progress.begin(0)
    progress.trace("request", request_id="one", profile="测试")
    progress.trace("assistant_response", "1. 一")
    ctx.now = 100
    error = StepCancelled if interruption == "cancel" else StepTimeout

    def check_cancel():
        raise error("运行已终止")

    ctx.check_cancel = check_cancel
    with pytest.raises(error):
        progress.finish()
    assert timing_history() == {}


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_download_aggregates_video_and_audio_and_excludes_resumed_bytes(monkeypatch, channel):
    from dubforge.pipeline import step_download

    ctx = Ctx()
    ctx.log = lambda message: None
    monkeypatch.setattr(step_download, "_ytdlp_cmd", lambda *a, **kw: [])

    def run(cmd, *, on_stdout, on_stderr):
        on_stdout('DUBFORGE_PLAN:[{"format_id":"v","filesize":800},{"format_id":"a","filesize":200}]')
        def tick(track, done, total):
            callback = on_stdout if channel == "stdout" else on_stderr
            callback('DUBFORGE_BYTES:' + json.dumps(track) + '\t' + json.dumps(
                {"downloaded_bytes": done, "total_bytes": total}))
        tick("v", 400, 800)  # 已有的断点文件不算当前下载速度。
        ctx.now = 10
        tick("v", 600, 800)
        assert ctx.percent[-1] == 60
        assert ctx.eta() == 20
        ctx.now = 20
        tick("v", 800, 800)
        assert ctx.percent[-1] == 80
        assert ctx.eta() == 10
        tick("a", 0, 200)
        ctx.now = 30
        tick("a", 200, 200)
        assert ctx.percent[-1] == 100  # 真实 StepContext 在正式收尾前封顶为99。
        return 0, 'DUBFORGE_PLAN:[]\nD:/video.mp4\n测试视频\n', ''

    ctx.run_subprocess = run
    result = step_download._run_ytdlp(ctx, "http://mock", "video.mp4")
    assert result == {"filepath": "D:/video.mp4", "title": "测试视频",
                      "source_meta": {}}
    assert ctx.eta is None


def test_split_stream_progress_does_not_abort_a_valid_llm_response(monkeypatch, tmp_path):
    from dubforge.backends import llm
    from dubforge.pipeline.step_split import _llm_resegment
    from dubforge.utils.srt import SubLine

    ctx = Ctx()
    ctx.pdir = tmp_path
    logs = []
    ctx.log = logs.append
    ctx.cfg = lambda key, default=None: default
    ctx.check_pause = ctx.check_cancel = lambda: None

    def chat(prompt, *, parser, log, trace, **kw):
        trace("request", request_id="one")
        log("LLM 模型思考中: 已收到内部进度约 300 字符")
        log("LLM 首个可见 token: 测试")
        log("LLM 流式接收: 配置=测试, 已收到 200 字符")
        return parser("1. 0.000-1.000: hello")

    monkeypatch.setattr(llm, "chat_validated", chat)
    parts = _llm_resegment(ctx, [SubLine(1, 0, 1, "hello")], 20, 10, 0, [])
    assert parts[0]["text"] == "hello"
    assert not any("重组失败" in message for message in logs)
    assert ctx.percent == [1, 99]
