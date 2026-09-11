"""OpenAI 兼容 chat client + Agent 诊断循环.

零新增依赖: 直接 httpx 打 /v1/chat/completions (tools = function calling).
换供应商 (DeepSeek/GLM/Kimi/Qwen 均为 OpenAI 兼容) 只改 env:
  AGENT_LLM_BASE_URL / AGENT_LLM_API_KEY / AGENT_LLM_MODEL

循环终止条件 (按序):
  1. 模型调 submit_report → 正常结束, 返回结构化结论
  2. max_turns 用尽 → 抛 LLMError (patrol 走降级路径)
  3. 连续 2 轮无 tool_call 且无 submit_report → 抛 LLMError
"""
from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger("ingest.agent.llm")


class LLMError(Exception):
    """LLM 调用/循环异常 — patrol 统一捕获走降级路径."""


class PatrolCancelled(RuntimeError):
    """巡检被手动取消 (patrol.request_cancel) — patrol 捕获后走 cancelled 收尾,
    与 LLMError 的降级路径区分开."""


def llm_configured() -> bool:
    return bool(_api_key())


def _base_url() -> str:
    return os.environ.get("AGENT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def _api_key() -> str:
    return (os.environ.get("AGENT_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY", "")).strip()


def _model() -> str:
    return os.environ.get("AGENT_LLM_MODEL", "gpt-4.1-mini")


def _timeout() -> int:
    # 思考型模型 (glm-5.x) 单轮可到 1~2 分钟, 默认放宽
    try:
        return int(os.environ.get("AGENT_LLM_TIMEOUT", "120"))
    except ValueError:
        return 120


def _api_style() -> str:
    """openai (默认, /chat/completions) | anthropic (/v1/messages, Messages 格式).

    GLM coding plan 订阅只覆盖 Anthropic 兼容端点 (open.bigmodel.cn/api/anthropic),
    v4 OpenAI 端点是按量付费 — 同一把 key 两个端点两种额度池, 这是配置来源之一.
    """
    style = os.environ.get("AGENT_LLM_API_STYLE", "").strip().lower()
    if style in ("anthropic", "openai"):
        return style
    return "anthropic" if "/anthropic" in _base_url().lower() else "openai"


def chat_completions(messages: list[dict], tools: list[dict]) -> dict:
    """一轮 LLM 调用. 返回 OpenAI 形状的 dict (anthropic 风格内部转换); 失败抛 LLMError."""
    if not _api_key():
        raise LLMError("LLM key 未配置 (AGENT_LLM_API_KEY / OPENAI_API_KEY)")
    if _api_style() == "anthropic":
        return _chat_anthropic(messages, tools)
    payload = {"model": _model(), "messages": messages, "tools": tools, "temperature": 0}
    url = f"{_base_url()}/chat/completions"
    headers = {"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=httpx.Timeout(_timeout())) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    except LLMError:
        raise
    except Exception as e:  # httpx 超时 / 连接失败 / JSON 解析
        raise LLMError(f"LLM 调用失败: {type(e).__name__}: {e}") from e


# ───────────────────── Anthropic Messages API 适配层 ─────────────────────
# 循环内部始终维护 OpenAI 格式历史 (system/assistant.tool_calls/tool role), 这里
# 出入两侧做格式转换, run_diagnosis_loop 完全无感.

def _anthropic_max_tokens() -> int:
    # 思考型模型 thinking 计入输出 token: 实测 glm-5.3 处理巡检快照要 ~10k 思考
    # + 正文/工具参数, 4096 会被截成"只有 thinking 块" (看起来像不调用工具)
    try:
        return int(os.environ.get("AGENT_LLM_MAX_TOKENS", "16384"))
    except ValueError:
        return 16384


def _oa_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


def _append_turn(turns: list[dict], role: str, blocks: list[dict]) -> None:
    """连续同角色 turn 合并成一个 (Anthropic 要求 user/assistant 交替;
    并行 tool_result 是同一 user turn 里的多个 block, 不是多条消息)."""
    if turns and turns[-1]["role"] == role:
        turns[-1]["content"].extend(blocks)
    else:
        turns.append({"role": role, "content": blocks})


def _oa_messages_to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 格式历史 → (system 文本, anthropic turns)."""
    system_parts: list[str] = []
    turns: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(str(m["content"]))
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": str(m["content"])})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id"),
                               "name": fn.get("name") or "", "input": args})
            _append_turn(turns, "assistant", blocks or [{"type": "text", "text": ""}])
        elif role == "tool":
            _append_turn(turns, "user", [{
                "type": "tool_result", "tool_use_id": m.get("tool_call_id"),
                "content": str(m.get("content") or ""),
            }])
        else:  # user
            _append_turn(turns, "user", [{"type": "text", "text": str(m.get("content") or "")}])
    return "\n\n".join(system_parts), turns


def _anthropic_to_oa_response(resp: dict) -> dict:
    """anthropic /v1/messages 响应 → OpenAI 形状 (thinking 等其他 block 忽略)."""
    blocks = resp.get("content") or []
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    tool_calls = [{
        "id": b.get("id"), "type": "function",
        "function": {"name": b.get("name"),
                     "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
    } for b in blocks if b.get("type") == "tool_use"]
    msg: dict = {"role": "assistant", "content": "".join(texts) if any(texts) else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    usage = resp.get("usage") or {}
    return {
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": usage.get("input_tokens", 0) or 0,
                  "completion_tokens": usage.get("output_tokens", 0) or 0},
    }


def _chat_anthropic(messages: list[dict], tools: list[dict]) -> dict:
    """Anthropic Messages API 调用. BASE_URL 配到端点前缀 (如
    https://open.bigmodel.cn/api/anthropic), 这里拼 /v1/messages."""
    system, turns = _oa_messages_to_anthropic(messages)
    payload: dict = {"model": _model(), "max_tokens": _anthropic_max_tokens(),
                     "messages": turns}
    # 思考型模型对 temperature 有限制, anthropic 风格不传 (用服务端默认)
    if system:
        payload["system"] = system
    atools = _oa_tools_to_anthropic(tools)
    if atools:
        payload["tools"] = atools
    url = f"{_base_url()}/v1/messages"
    headers = {"x-api-key": _api_key(), "Authorization": f"Bearer {_api_key()}",
               "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=httpx.Timeout(_timeout())) as client:
            r = client.post(url, json=payload, headers=headers)
        if r.status_code != 200:
            raise LLMError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        return _anthropic_to_oa_response(r.json())
    except LLMError:
        raise
    except Exception as e:
        raise LLMError(f"LLM 调用失败: {type(e).__name__}: {e}") from e


def run_diagnosis_loop(snapshot: dict, executor, system_prompt: str,
                       max_turns: int | None = None,
                       cancel_event=None) -> dict:
    """LLM 诊断循环: 信号快照 + 工具集 → 结构化报告.

    executor: tools.ToolExecutor (specs() 给 schema, execute() 执行).
    返回 {"summary","diagnosis_md","escalate","escalate_reason","per_vendor",...}.
    submit_report 的参数经 _validate_report 校验后才被采纳.
    max_turns 默认读 AGENT_LLM_MAX_TURNS (默认 8) — 信号多时模型逐家下钻需要轮数,
    太小会烧尽轮次走降级.
    """
    if max_turns is None:
        try:
            max_turns = int(os.environ.get("AGENT_LLM_MAX_TURNS", "8"))
        except ValueError:
            max_turns = 8
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "当前巡检信号快照 (JSON):\n"
            + json.dumps(snapshot, ensure_ascii=False, default=str)
            + "\n\n请诊断并处置。完成后调用 submit_report。")},
    ]
    tools = executor.specs()
    no_tool_rounds = 0
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    for turn in range(1, max_turns + 1):
        if cancel_event is not None and cancel_event.is_set():
            raise PatrolCancelled("手动取消 (轮次开始处检出)")
        resp = chat_completions(messages, tools)
        try:
            choice = resp["choices"][0]["message"]
            usage = resp.get("usage") or {}
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0) or 0
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应结构异常: {e}: {str(resp)[:300]}") from e

        tool_calls = choice.get("tool_calls") or []
        _names = ",".join((tc.get("function") or {}).get("name", "?") for tc in tool_calls) or "-"
        log.info(f"[llm] turn {turn}/{max_turns}: tools=[{_names}] content={bool(choice.get('content'))}")

        # 终态: submit_report
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            if fn.get("name") == "submit_report":
                try:
                    report = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as e:
                    raise LLMError(f"submit_report 参数不是合法 JSON: {e}") from e
                report = _validate_report(report)
                report["llm_meta"] = {**total_usage, "turns": turn}
                # submit_report 之外如果还有并行 tool_call, 丢弃 (终态优先)
                return report

        if not tool_calls:
            no_tool_rounds += 1
            if no_tool_rounds >= 2:
                raise LLMError("LLM 连续 2 轮未调用任何工具且未提交报告")
        else:
            no_tool_rounds = 0

        # 把 assistant 消息 + 逐个工具结果回填, 进入下一轮
        # 注意: content-only 轮不回显 tool_calls 键 — 部分 OpenAI 兼容 API 对
        # tool_calls=[] 直接 HTTP 400 (空数组 ≠ 没有调用)
        echo: dict = {"role": "assistant", "content": choice.get("content")}
        if tool_calls:
            echo["tool_calls"] = tool_calls
        messages.append(echo)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                result = {"error": f"arguments 不是合法 JSON: {(fn.get('arguments') or '')[:200]}"}
            else:
                result = executor.execute(name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": json.dumps(result, ensure_ascii=False, default=str)[:4000],
            })

    # 轮次用尽 → 强制收尾: 去掉工具, 模型无法再"再查一家", 只能直接输出报告 JSON.
    # 这是弱模型兜底 (实测 glm-4-flash 会一家一轮地读工具烧光轮次); 已执行的动作
    # 已在 executor 里, 收尾成功照样出正常报告.
    forced = _forced_final_report(messages, total_usage)
    if forced is not None:
        return forced
    raise LLMError(f"LLM {max_turns} 轮用尽且强制收尾失败")


def _forced_final_report(messages: list[dict], total_usage: dict) -> dict | None:
    """无工具强制收尾. 成功返回报告 dict, 失败返回 None."""
    messages = messages + [{
        "role": "user",
        "content": ("工具调用轮次已用完，现在不再提供任何工具。"
                    "基于以上全部信息，立即输出 submit_report 的 JSON 参数"
                    "（只输出 JSON，不要任何其他文字）。"),
    }]
    try:
        resp = chat_completions(messages, tools=[])
        content = (resp["choices"][0]["message"].get("content") or "").strip()
        usage = resp.get("usage") or {}
        total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    except LLMError as e:
        log.warning(f"[llm] 强制收尾调用失败: {e}")
        return None
    # 剥掉 ```json 围栏 / 前后杂文字, 取第一个 { 到最后一个 }
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        log.warning(f"[llm] 强制收尾输出不像 JSON: {content[:200]}")
        return None
    try:
        report = json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        log.warning(f"[llm] 强制收尾 JSON 解析失败: {content[:200]}")
        return None
    try:
        report = _validate_report(report)
    except LLMError:
        return None
    report["llm_meta"] = {**total_usage, "forced": True}
    log.info("[llm] 强制收尾成功 (轮次耗尽但拿到结构化报告)")
    return report


def _validate_report(report: dict) -> dict:
    """兜底校验 submit_report 的参数形状 (LLM 输出不可信)."""
    if not isinstance(report, dict):
        raise LLMError("submit_report 参数必须是 JSON 对象")
    out = {
        "summary": str(report.get("summary") or "")[:500] or "巡检完成",
        "diagnosis_md": str(report.get("diagnosis_md") or report.get("summary") or "")[:6000],
        "escalate": bool(report.get("escalate")),
        "escalate_reason": str(report.get("escalate_reason") or "")[:1000] or None,
    }
    per_vendor = []
    for item in (report.get("per_vendor") or [])[:12]:
        if not isinstance(item, dict) or not item.get("vendor_id"):
            continue
        per_vendor.append({
            "vendor_id": str(item["vendor_id"])[:64],
            "diagnosis": str(item.get("diagnosis") or "")[:500],
            "action": str(item.get("action") or "none")[:200],
            "escalate": bool(item.get("escalate")),
        })
    out["per_vendor"] = per_vendor
    return out
