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
