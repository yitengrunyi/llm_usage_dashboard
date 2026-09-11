"""llm 单测 — 循环逻辑 monkeypatch 假响应; HTTP 层用 httpx.MockTransport."""
from __future__ import annotations

import json

import httpx
import pytest

import ingest.agent.llm as llm


class _StubExecutor:
    def specs(self):
        return [{"type": "function", "function": {"name": "get_run_detail",
                "parameters": {"type": "object", "properties": {}}}}]

    def execute(self, name, args):
        return {"stub": True, "name": name}

    def __init__(self):
        self.calls = []


def _resp(content=None, tool_calls=None, usage=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}], "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1}}


def _tc(name, args):
    return {"id": f"c_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


SNAP = {"today_cst": "2026-08-27", "signals": [], "context": {"running_vendors": []}}


def test_happy_path_reads_then_submits(monkeypatch):
    seq = [
        _resp(tool_calls=[_tc("get_run_detail", {"run_id": 42})]),
        _resp(tool_calls=[_tc("submit_report", {
            "summary": "已修复", "diagnosis_md": "kimi session 失效",
            "escalate": False, "per_vendor": [{"vendor_id": "kimi", "diagnosis": "d", "action": "login"}]})],
            usage={"prompt_tokens": 10, "completion_tokens": 20}),
    ]
    it = iter(seq)
    monkeypatch.setattr(llm, "chat_completions", lambda m, tools=None: next(it))
    ex = _StubExecutor()
    report = llm.run_diagnosis_loop(SNAP, ex, "sys")
    assert report["summary"] == "已修复"
    assert report["escalate"] is False
    assert report["per_vendor"][0]["vendor_id"] == "kimi"
    assert report["llm_meta"]["turns"] == 2
    assert report["llm_meta"]["prompt_tokens"] == 11  # 两轮累加


def test_invalid_tool_arguments_json_fed_back_not_raised(monkeypatch):
    bad = {"id": "c1", "type": "function",
           "function": {"name": "get_run_detail", "arguments": "{not json"}}
    seq = [
        _resp(tool_calls=[bad]),
        _resp(tool_calls=[_tc("submit_report", {"summary": "ok"})]),
    ]
    it = iter(seq)
    monkeypatch.setattr(llm, "chat_completions", lambda m, tools=None: next(it))
    report = llm.run_diagnosis_loop(SNAP, _StubExecutor(), "sys")
    assert report["summary"] == "ok"


def test_content_only_turn_not_echoed_with_empty_tool_calls(monkeypatch):
    """content-only 轮回显 tool_calls=[] 会被部分 OpenAI 兼容 API 拒 (HTTP 400)."""
    seen_messages = []
    seq = [
        _resp(content="让我先想想..."),          # content-only, 无 tool_calls
        _resp(tool_calls=[_tc("get_run_detail", {"run_id": 1})]),
        _resp(tool_calls=[_tc("submit_report", {"summary": "ok"})]),
    ]
    it = iter(seq)

    def fake_chat(messages, tools):
        seen_messages.append([dict(m) for m in messages])
        return next(it)
    monkeypatch.setattr(llm, "chat_completions", fake_chat)
    llm.run_diagnosis_loop(SNAP, _StubExecutor(), "sys")
    # 每一轮发出的历史里, assistant 消息要么没有 tool_calls 键, 要么非空
    for msgs in seen_messages:
        for m in msgs:
            if m["role"] == "assistant":
                assert m.get("tool_calls") != [], "assistant 回显了空 tool_calls 数组"


def test_submit_report_bad_json_raises(monkeypatch):
    bad = {"id": "c1", "type": "function",
           "function": {"name": "submit_report", "arguments": "{oops"}}
    monkeypatch.setattr(llm, "chat_completions", lambda m, t: _resp(tool_calls=[bad]))
    with pytest.raises(llm.LLMError, match="submit_report"):
        llm.run_diagnosis_loop(SNAP, _StubExecutor(), "sys")


def test_max_turns_exhausted_forced_wrapup_succeeds(monkeypatch):
    """轮次耗尽 → 无工具强制收尾; 模型直接吐 JSON 也能拿到结构化报告."""
    seq = [_resp(tool_calls=[_tc("get_run_detail", {"run_id": 1})])] * 3
    seq.append(_resp(content="```json\n{\"summary\": \"强制收尾结论\", \"escalate\": true,"
                             " \"diagnosis_md\": \"d\"}\n```"))
    it = iter(seq)
    monkeypatch.setattr(llm, "chat_completions", lambda m, tools=None: next(it))
    report = llm.run_diagnosis_loop(SNAP, _StubExecutor(), "sys", max_turns=3)
    assert report["summary"] == "强制收尾结论"
    assert report["escalate"] is True
    assert report["llm_meta"]["forced"] is True


def test_max_turns_exhausted_wrapup_garbage_raises(monkeypatch):
    seq = [_resp(tool_calls=[_tc("get_run_detail", {"run_id": 1})])] * 3
    seq.append(_resp(content="抱歉我无法继续"))  # 收尾输出不是 JSON
    it = iter(seq)
    monkeypatch.setattr(llm, "chat_completions", lambda m, tools=None: next(it))
    with pytest.raises(llm.LLMError, match="轮用尽"):
        llm.run_diagnosis_loop(SNAP, _StubExecutor(), "sys", max_turns=3)


def test_two_rounds_no_tool_raises(monkeypatch):
    monkeypatch.setattr(llm, "chat_completions", lambda m, t: _resp(content="thinking..."))
    with pytest.raises(llm.LLMError, match="未调用任何工具"):
        llm.run_diagnosis_loop(SNAP, _StubExecutor(), "sys", max_turns=5)


def test_no_key_not_configured_and_raises(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert llm.llm_configured() is False
    with pytest.raises(llm.LLMError, match="key"):
        llm.chat_completions([], [])


_ORIG_CLIENT = httpx.Client  # patch httpx.Client 前先留原始引用, 工厂里再调它会递归


def _mock_client(monkeypatch, handler):
    monkeypatch.setattr(llm.httpx, "Client",
                        lambda **kw: _ORIG_CLIENT(transport=httpx.MockTransport(handler),
                                                  timeout=kw.get("timeout")))


def test_chat_completions_http_error(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_API_KEY", "sk-test")
    # 钉住 openai 风格 — conftest 导入链会 load_dotenv, 真实 .env 的 anthropic 配置不能漏进来
    monkeypatch.setenv("AGENT_LLM_API_STYLE", "openai")

    def handler(request):
        return httpx.Response(500, text="upstream boom")

    _mock_client(monkeypatch, handler)
    with pytest.raises(llm.LLMError, match="500"):
        llm.chat_completions([], [])


def test_chat_completions_success(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_LLM_API_STYLE", "openai")

    def handler(request):
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json=_resp(content="hi"))

    _mock_client(monkeypatch, handler)
    out = llm.chat_completions([{"role": "user", "content": "x"}], [])
    assert out["choices"][0]["message"]["content"] == "hi"


def test_validate_report_defaults():
    out = llm._validate_report({"summary": None, "escalate": "yes",
                                "per_vendor": [{"vendor_id": "kimi"},
                                               "garbage", None]})
    assert out["summary"]  # 空 summary 兜底
    assert out["escalate"] is True
    assert len(out["per_vendor"]) == 1
    assert out["escalate_reason"] is None


# ───────────────── Anthropic Messages API 适配层 ─────────────────
def test_api_style_env_and_autodetect(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_API_STYLE", raising=False)
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://api.openai.com/v1")
    assert llm._api_style() == "openai"
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    assert llm._api_style() == "anthropic"
    monkeypatch.setenv("AGENT_LLM_API_STYLE", "anthropic")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://api.openai.com/v1")
    assert llm._api_style() == "anthropic"  # env 显式覆盖优先


def test_oa_messages_to_anthropic():
    msgs = [
        {"role": "system", "content": "sys-A"},
        {"role": "system", "content": "sys-B"},
        {"role": "user", "content": "快照"},
        {"role": "assistant", "content": "思考中",
         "tool_calls": [
             {"id": "c1", "type": "function",
              "function": {"name": "get_run_detail", "arguments": '{"run_id": 42}'}},
             {"id": "c2", "type": "function",
              "function": {"name": "get_session_status", "arguments": '{"vendor_id": "kimi"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"status": "failed"}'},
        {"role": "tool", "tool_call_id": "c2", "content": '{"status": "ok"}'},
        {"role": "user", "content": "收尾指令"},
    ]
    system, turns = llm._oa_messages_to_anthropic(msgs)
    assert system == "sys-A\n\nsys-B"
    # 并行 tool_result + 随后的 user 收尾指令 → 合并进同一个 user turn
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    tool_results = [b for b in turns[2]["content"] if b["type"] == "tool_result"]
    assert len(tool_results) == 2
    assert tool_results[0]["tool_use_id"] == "c1"
    assert any(b["type"] == "text" and b["text"] == "收尾指令" for b in turns[2]["content"])
    # assistant tool_calls → tool_use block, arguments 已解析成 dict
    tool_uses = [b for b in turns[1]["content"] if b["type"] == "tool_use"]
    assert tool_uses[0]["input"] == {"run_id": 42}
    assert tool_uses[1]["name"] == "get_session_status"


def test_anthropic_response_to_openai_shape():
    resp = {"content": [
                {"type": "thinking", "thinking": "..."},  # 思考 block 忽略
                {"type": "text", "text": "诊断结论"},
                {"type": "tool_use", "id": "t1", "name": "submit_report",
                 "input": {"summary": "ok", "escalate": False}}],
            "usage": {"input_tokens": 100, "output_tokens": 30}}
    out = llm._anthropic_to_oa_response(resp)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "诊断结论"
    assert msg["tool_calls"][0]["id"] == "t1"
    import json as _json
    assert _json.loads(msg["tool_calls"][0]["function"]["arguments"])["summary"] == "ok"
    assert out["usage"] == {"prompt_tokens": 100, "completion_tokens": 30}


def test_chat_anthropic_request_and_dispatch(monkeypatch):
    monkeypatch.setenv("AGENT_LLM_API_KEY", "glm-test")
    monkeypatch.setenv("AGENT_LLM_API_STYLE", "anthropic")
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    captured = {}

    def handler(request):
        captured["path"] = request.url.path
        captured["x-api-key"] = request.headers.get("x-api-key")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 5, "output_tokens": 2}})

    monkeypatch.setattr(llm.httpx, "Client",
                        lambda **kw: _ORIG_CLIENT(transport=httpx.MockTransport(handler),
                                                  timeout=kw.get("timeout")))
    out = llm.chat_completions(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}], [])
    assert out["choices"][0]["message"]["content"] == "hi"
    assert captured["path"] == "/api/anthropic/v1/messages"
    assert captured["x-api-key"] == "glm-test"
    assert captured["body"]["system"] == "sys"
    assert captured["body"]["max_tokens"] > 0
    assert "tools" not in captured["body"]  # 空 tools 不发键 (强制收尾路径)
