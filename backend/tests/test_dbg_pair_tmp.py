import json
from test_text_chain import _seed_transcribe, _mock_llm, _run_until
from dubforge.domain import crud


def test_A_polluter(make_project, isolated_env, monkeypatch):
    uid = make_project()
    text = "这是一个很长的中文句子，需要在自然标点附近拆开，同时保留全部内容。"
    _seed_transcribe(uid, [(0.0, 6.0, text)])
    import dubforge.config as cfgmod
    cfgmod.set_key("split.use_llm", False)
    cfgmod.set_key("split.max_split_length", 12)
    assert _run_until(uid, "split") == "done"


def test_B_victim(make_project, isolated_env, monkeypatch):
    uid = make_project()
    _seed_transcribe(uid, [(0.0, 1.8, "这是第一句，"), (1.8, 3.5, "接着是第二句。")])
    def responder(prompt, system):
        return "1. 0.00-3.50: 这是第一句，接着是第二句。"
    _mock_llm(monkeypatch, responder)
    import dubforge.config as cfgmod
    cfgmod.set_key("asr.segmentation_mode", "phrase")
    from dubforge import config as cfg
    print("use_llm =", cfg.get("split.use_llm"), "| window_max =", cfg.get("split.window_max_chars"))
    status = _run_until(uid, "split")
    step = crud.get_step(crud.get_project(uid).id, 'split')
    print("STATUS:", status, "| ERR:", step.error_code, step.error_message[:160])
    assert status == 'done'
