import importlib.util
import json
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).parent / "cortex_proxy.py"
SPEC = importlib.util.spec_from_file_location("cortex_proxy", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _models_file(tmp_path):
    data = {
        "models": {"model-a": 200000},
        "tools_require_reasoning_effort_none": ["model-a"],
        "tools_unsupported": ["model-b"],
    }
    p = tmp_path / "models.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_rewrites_max_tokens_and_reasoning(tmp_path, monkeypatch):
    models_file = _models_file(tmp_path)
    # MODELS_PATHS is built at import time; patch it and invalidate the cache.
    monkeypatch.setattr(MODULE, "MODELS_PATHS", [models_file])
    MODULE._models_cache.update(path=None, mtime=None, models=None, no_reasoning=None)
    request = {
        "model": "model-a",
        "max_tokens": 100,
        "tools": [{"type": "function"}],
    }
    encoded, body = MODULE.adapt_payload(json.dumps(request).encode())
    assert body is not None
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 100
    assert body.get("reasoning_effort") == "none"


def test_normalizes_finish_reason_stop():
    raw = json.dumps({"choices": [{"finish_reason": "", "message": {"content": "hi"}}]}).encode()
    out = json.loads(MODULE.normalize_finish_reason(raw))
    assert out["choices"][0]["finish_reason"] == "stop"


def test_normalizes_finish_reason_tool_calls():
    raw = json.dumps({
        "choices": [{"finish_reason": "", "message": {"tool_calls": [{"id": "x"}]}}]
    }).encode()
    out = json.loads(MODULE.normalize_finish_reason(raw))
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_injects_terminal_chunk():
    raw = (
        b'data: {"id":"x","model":"m","choices":[{"delta":{"content":"hi"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    out = MODULE.normalize_stream(raw)
    assert b"finish_reason" in out
    assert b"stop" in out
    assert out.endswith(b"data: [DONE]\n\n")


def _parallel_turn():
    """Assistant turn with two tool calls plus one tool result for each.

    This is the exact shape Cortex rejects with HTTP 400 "Each 'toolUse' block
    must be accompanied with a matching 'toolResult' block".
    """
    return {
        "messages": [
            {"role": "user", "content": "what time is it and what crons exist?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "terminal"}},
                    {"id": "c2", "type": "function", "function": {"name": "cron"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "R1"},
            {"role": "tool", "tool_call_id": "c2", "content": "R2"},
        ]
    }


def test_collapses_parallel_tool_calls():
    body = _parallel_turn()
    assert MODULE.collapse_parallel_tool_calls(body) is True
    msgs = body["messages"]
    # One assistant turn with a single tool call, followed by a single result:
    # the 1:1 constraint Cortex enforces.
    assert len(msgs) == 3
    assert len(msgs[1]["tool_calls"]) == 1
    assert msgs[1]["tool_calls"][0]["id"] == "c1"
    assert len([m for m in msgs if m.get("role") == "tool"]) == 1
    # Nothing is dropped: the second result is merged into the first.
    assert "R1" in msgs[2]["content"]
    assert "R2" in msgs[2]["content"]


def test_leaves_single_tool_call_untouched():
    body = {
        "messages": [
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "R1"},
        ]
    }
    before = json.dumps(body, sort_keys=True)
    assert MODULE.collapse_parallel_tool_calls(body) is False
    assert json.dumps(body, sort_keys=True) == before


def test_collapse_is_idempotent():
    body = _parallel_turn()
    assert MODULE.collapse_parallel_tool_calls(body) is True
    once = json.dumps(body, sort_keys=True)
    # A second pass must be a no-op, otherwise repeated turns would keep
    # rewriting the same history.
    assert MODULE.collapse_parallel_tool_calls(body) is False
    assert json.dumps(body, sort_keys=True) == once


def test_adapt_payload_collapses_parallel_tool_calls(tmp_path, monkeypatch):
    """The collapse must run on the real request path, not just in isolation."""
    models_file = _models_file(tmp_path)
    monkeypatch.setattr(MODULE, "MODELS_PATHS", [models_file])
    MODULE._models_cache.update(path=None, mtime=None, models=None, no_reasoning=None)
    request = _parallel_turn()
    request["model"] = "model-a"
    _, body = MODULE.adapt_payload(json.dumps(request).encode())
    assert body is not None
    assert len(body["messages"]) == 3
    assert len(body["messages"][1]["tool_calls"]) == 1
